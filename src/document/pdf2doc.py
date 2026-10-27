"""PDF 转结构化文档：加载、方向校正、版面分析、OCR、组装与导出。

统一执行步骤 1–6，保留源文件、OCR 原始证据、单元格引用及坐标变换。
输出 JSON、JSONL、Markdown 和纯文本；--layout-only 可单独运行裁图阶段。
"""

from __future__ import annotations

import argparse
import copy
import fcntl
import importlib.metadata
import multiprocessing
import unicodedata
import hashlib
import json
import math
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import time
from abc import ABC, abstractmethod
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from time import perf_counter
from typing import Any, Callable, Dict, Iterable, Iterator, List, Mapping, Sequence

import pymupdf
from lxml import html

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from units.cache_policy import cache_enabled
from document.cell_geometry import locate_grid


# 1. 加载 PDF：文档信息、页数、页面尺寸
def load_pdf(pdf_path: str | Path) -> dict:
    """读取源 PDF 的基础信息，返回可 JSON 序列化的字典。

    页码从 1 开始，尺寸单位为 pt（1/72 英寸）。width_pt、height_pt
    与 rect 属于已有 PDF 旋转后的显示页面；mediabox、cropbox 保留
    PyMuPDF 返回的原始页面框，不作为显示坐标下的检测框使用。
    rotation 仅记录 PDF 的旋转属性，实际内容方向由后续步骤检测。
    本步骤不修改源文件、不渲染页面，也不提取正文或执行 OCR。
    """
    path = Path(pdf_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"PDF 文件不存在或不是普通文件：{path}")

    try:
        document = pymupdf.open(path)
    except pymupdf.FileDataError as exc:
        raise ValueError(f"无法读取 PDF，文件为空或已损坏：{path}") from exc

    with document as pdf:
        if not pdf.is_pdf:
            raise ValueError(f"文件不是 PDF：{path}")
        if pdf.needs_pass:
            raise ValueError(f"PDF 需要密码：{path}")
        if not pdf.page_count:
            raise ValueError(f"PDF 没有页面：{path}")

        pages = []
        for index, page in enumerate(pdf):
            pages.append({
                "page": index + 1,
                "width_pt": float(page.rect.width),
                "height_pt": float(page.rect.height),
                "rotation": page.rotation,
                "rect": list(page.rect),
                "mediabox": list(page.mediabox),
                "cropbox": list(page.cropbox),
            })
        metadata = dict(pdf.metadata or {})
        total_pages = pdf.page_count

    return {
        "document_id": path.stem,
        "source_pdf": str(path),
        "source_pdf_sha256": file_digest(path),
        "size_bytes": path.stat().st_size,
        "metadata": metadata,
        "total_pages": total_pages,
        "coordinate_space": "pdf_points",
        "coordinate_frame": "rendered_page",
        "pages": pages,
    }


# 2. 校正页面方向：按需旋转
# 保留源 PDF，生成 prepared.pdf，并记录源坐标与工作坐标的双向变换。
ORIENTATION_VERSION = 1
WORKING_PDF = "prepared.pdf"
MANIFEST = "page_orientation.json"


def find_tesseract():
    """Find Tesseract OSD without importing another Python environment."""
    configured = os.environ.get("SDA_TESSERACT")
    if configured:
        executable = shutil.which(configured)
        if not executable:
            raise RuntimeError(f"SDA_TESSERACT is unavailable: {configured}")
        return str(Path(executable).resolve())
    installed = shutil.which("tesseract")
    if installed:
        return str(Path(installed).resolve())
    environments = Path(__file__).resolve().parents[3] / "envs"
    return next((str(p.resolve()) for p in sorted(environments.glob("*/bin/tesseract"))
                 if p.is_file() and os.access(p, os.X_OK)), None)


def tesseract_env(executable, lang):
    env = os.environ.copy()
    prefix = Path(executable).parent.parent
    data = env.get("SDA_TESSDATA") or env.get("TESSDATA_PREFIX")
    if not data and (prefix / "share/tessdata").is_dir():
        data = str(prefix / "share/tessdata")
    if data:
        for language in lang.split("+"):
            if not (Path(data) / f"{language}.traineddata").is_file():
                raise RuntimeError(f"Missing Tesseract language data: {language}.traineddata in {data}")
        env["TESSDATA_PREFIX"] = data
    libraries = prefix / "lib"
    if libraries.is_dir():
        env["LD_LIBRARY_PATH"] = os.pathsep.join([str(libraries), env.get("LD_LIBRARY_PATH", "")]).rstrip(os.pathsep)
    return env


def file_digest(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def rotation_matrix(width: float, height: float, clockwise: int) -> Any:
    """Map top-left coordinates through a clockwise quarter-turn."""
    return {
        0: pymupdf.Matrix(1, 0, 0, 1, 0, 0),
        90: pymupdf.Matrix(0, 1, -1, 0, height, 0),
        180: pymupdf.Matrix(-1, 0, 0, -1, width, height),
        270: pymupdf.Matrix(0, -1, 1, 0, 0, width),
    }[clockwise % 360]


class OrientationDetector:
    """用 Tesseract OSD 检测方向；非零旋转须在第二种分辨率下确认。"""

    def __init__(self) -> None:
        self.executable = find_tesseract()
        if not self.executable:
            raise RuntimeError("PDF orientation requires Tesseract and osd.traineddata")
        self.env = tesseract_env(self.executable, lang="osd")
        self.env["OMP_THREAD_LIMIT"] = "1"

    def _probe(self, page: Any, directory: Path, dpi: int) -> dict[str, Any]:
        # Bound memory for unusually large engineering sheets.
        scale = min(dpi / 72, 4096 / max(page.rect.width, page.rect.height))
        image = directory / f"orientation-{dpi}.png"
        page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False).save(image)
        try:
            result = subprocess.run(
                [self.executable, str(image), "stdout", "--psm", "0", "-l", "osd",
                 "--dpi", str(round(scale * 72))],
                capture_output=True, text=True, env=self.env, timeout=30,
            )
        except subprocess.TimeoutExpired:
            return {"dpi": dpi, "error": "orientation_timeout"}
        rotation = re.search(r"^Rotate:\s*(0|90|180|270)\s*$", result.stdout, re.M)
        confidence = re.search(r"^Orientation confidence:\s*([\d.]+)", result.stdout, re.M)
        if result.returncode or not rotation or not confidence:
            return {"dpi": dpi, "error": "insufficient_text_or_detection_failed",
                    "detail": result.stderr.strip()[-500:]}
        return {"dpi": dpi, "clockwise": int(rotation[1]), "confidence": float(confidence[1])}

    def __call__(self, page: Any, directory: Path) -> dict[str, Any]:
        primary = self._probe(page, directory, 160)
        probes = [primary]
        if "error" in primary or primary["confidence"] < 8:
            return {"clockwise": 0, "status": "needs_review", "probes": probes}
        if primary["clockwise"] == 0:
            return {"clockwise": 0, "status": "upright", "probes": probes}
        confirmation = self._probe(page, directory, 110)
        probes.append(confirmation)
        if (confirmation.get("clockwise") == primary["clockwise"]
                and confirmation.get("confidence", 0) >= 3):
            return {"clockwise": primary["clockwise"], "status": "rotated", "probes": probes}
        return {"clockwise": 0, "status": "needs_review", "probes": probes}


def prepare_pdf(
    source: Path, output: Path, manifest_path: Path, *,
    detector: Callable | None = None, progress: Callable | None = None,
    selected_pages: list[int] | None = None,
) -> dict[str, Any]:
    """生成方向校正后的工作 PDF，返回并保存逐页校正记录。

    不确定的页面不额外旋转，记录为 needs_review；源文件保持不变。
    输出页将原有 PDF 旋转属性合入内容，统一为 rotation=0。
    manifest 保存原始页与工作页的双向坐标变换，并校验缓存文件哈希。
    后续版面分析和 OCR 必须使用 output，再通过变换追溯源 PDF。
    """
    source, output, manifest_path = source.resolve(), output.resolve(), manifest_path.resolve()
    if source == output or manifest_path in {source, output}:
        raise ValueError("Source, working PDF and orientation manifest must be distinct")
    if manifest_path.is_file() and cache_enabled():
        result = validate_preparation(source, output, manifest_path)
        if result.get("selected_pages") != (sorted(set(selected_pages)) if selected_pages is not None else None):
            raise ValueError("PDF orientation cache has a different selected page range")
        return result
    # A leftover working copy without a manifest was never committed. Rebuild it.
    detector = detector or OrientationDetector()
    source_hash = file_digest(source)
    output.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    pages = []
    with tempfile.TemporaryDirectory(prefix="pdf-orientation-", dir=output.parent) as temporary:
        directory = Path(temporary)
        with pymupdf.open(source) as original, pymupdf.open() as working:
            if selected_pages is not None and (not selected_pages or any(
                isinstance(p, bool) or not isinstance(p, int) or not 1 <= p <= len(original)
                for p in selected_pages
            )):
                raise ValueError("Requested orientation pages are empty or outside the PDF")
            for index, page in enumerate(original):
                original_rotation = page.rotation
                display_width, display_height = page.rect.width, page.rect.height
                decision = detector(page, directory) if selected_pages is None or index + 1 in selected_pages else {
                    "clockwise": 0, "status": "not_selected", "probes": []}
                correction = int(decision["clockwise"])
                if correction not in {0, 90, 180, 270}:
                    raise ValueError(f"Invalid page orientation: {correction}")
                # Flatten /Rotate into the content so every consumer uses the
                # same unrotated coordinate frame, including crop/text APIs.
                page.set_rotation(0)
                width, height = page.rect.width, page.rect.height
                total_rotation = (original_rotation + correction) % 360
                matrix = rotation_matrix(width, height, total_rotation)
                target_rect = page.rect * matrix
                target = working.new_page(width=target_rect.width, height=target_rect.height)
                if page.get_contents():
                    target.show_pdf_page(target.rect, original, index, rotate=-total_rotation)
                pages.append({
                    "page": index + 1, "original_rotation": original_rotation,
                    "correction_clockwise": correction, "status": decision["status"],
                    "probes": decision.get("probes", []),
                    "source_size": [width, height],
                    "working_size": [target_rect.width, target_rect.height],
                    "source_to_working": list(matrix), "working_to_source": list(~matrix),
                    "source_display_to_working": list(rotation_matrix(display_width, display_height, correction)),
                })
                if progress:
                    progress(index + 1, len(original), pages[-1])
            working.set_metadata(original.metadata)
            if original.get_toc():
                working.set_toc(original.get_toc())
            candidate = directory / "prepared.pdf"
            working.save(candidate, garbage=3, deflate=True)
        if file_digest(source) != source_hash:
            raise RuntimeError("Source PDF changed during orientation preparation")
        result = {
            "version": ORIENTATION_VERSION, "source": source.name, "source_sha256": source_hash,
            "working_pdf": output.name, "working_sha256": file_digest(candidate),
            "coordinate_space": "unrotated_cropbox_points_top_left",
            "detector": "tesseract_osd_160dpi_confirm_110dpi_min8_min3",
            "page_count": len(pages), "pages": pages,
            "selected_pages": sorted(set(selected_pages)) if selected_pages is not None else None,
            "rotated_pages": [p["page"] for p in pages if p["correction_clockwise"]],
            "review_pages": [p["page"] for p in pages if p["status"] == "needs_review"],
        }
        pending = directory / "manifest.json"
        pending.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        candidate.replace(output)
        pending.replace(manifest_path)
    return result


def validate_preparation(source: Path, output: Path, manifest_path: Path) -> dict[str, Any]:
    result = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (result.get("version") != ORIENTATION_VERSION or result.get("source") != source.name
            or result.get("working_pdf") != output.name
            or result.get("source_sha256") != file_digest(source)
            or not output.is_file() or result.get("working_sha256") != file_digest(output)):
        raise ValueError("PDF preparation is stale or incomplete; rebuild derived artifacts together")
    return result


# 兼容入口位于 pdf_orientation.py；新流程可直接调用上述 prepare_pdf。

# 3. 版面定位与裁图
#    渲染 → 多后端检测 → 框融合与坐标换算 → 高清裁图 → 续表候选及来源清单。
#    默认使用 PP-DocLayoutV3；模型权重仅在首次预测时加载。
ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT / "config/layout_runtime.json"
WORKER_EVENT = "layout-worker-page "
# 默认只用 PP 后端；BACKEND_NAMES 同时列出可选的 YOLO 和 TATR。
BACKEND_NAMES = ("pp_doclayoutv3", "doclayout_yolo", "tatr")
PATENT_ID = re.compile(r"(?<![A-Z0-9])(?:WO|US|EP|CN|JP|KR)\d{6,}[A-Z]\d?(?![A-Z0-9])", re.I)


# 检测结果与坐标工具：检测模型使用像素坐标，输出时转换为 PDF 点坐标。

MODEL_SOURCE = {
    "PP-DocLayoutV3": "https://github.com/PaddlePaddle/PaddleOCR",
    "DocLayout-YOLO": "https://github.com/opendatalab/DocLayout-YOLO",
    "Table Transformer (TATR)": "https://github.com/microsoft/table-transformer",
}


@dataclass
class RawDetection:
    """保存单个模型的原始检测结果；bbox_px 为整页图片上的像素坐标。"""

    label: str
    score: float
    bbox_px: Sequence[float]
    model: str
    model_id: str
    backend: str
    category_id: int | None = None
    raw_label: str | None = None
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """转为 JSON 可保存的字典，保留模型来源和原始标签。"""
        return {
            "label": canonical_label(self.label),
            "raw_label": self.raw_label or self.label,
            "confidence": round(float(self.score), 6),
            "bbox_px": [round(float(v), 3) for v in self.bbox_px],
            "model": self.model,
            "model_id": self.model_id,
            "backend": self.backend,
            "category_id": self.category_id,
            "extra": self.extra,
        }


def canonical_label(label: str) -> str:
    """统一三种模型的类别名称，例如 image、picture 均归为 figure。"""

    value = str(label or "unknown").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "table": "table",
        "table_cell": "table_cell",
        "table_caption": "table_caption",
        "table_title": "table_caption",
        "figure_table_title": "figure_title",
        "figure_caption": "figure_caption",
        "image_caption": "figure_caption",
        "picture": "figure",
        "image": "figure",
        "title": "document_title",
        "doc_title": "document_title",
        "paragraph_title": "paragraph_title",
        "text": "text",
        "plain_text": "text",
    }
    return aliases.get(value, value)


def clamp_bbox(bbox: Sequence[float], width: float, height: float) -> List[float]:
    """整理 xyxy 坐标并裁到页面边界，拒绝空框和非有限数值。"""

    if len(bbox) != 4:
        raise ValueError(f"bbox must contain four values, got {bbox!r}")
    x1, y1, x2, y2 = (float(v) for v in bbox)
    if not all(math.isfinite(v) for v in (x1, y1, x2, y2)):
        raise ValueError(f"non-finite bbox {bbox!r}")
    x1, x2 = sorted((max(0.0, min(width, x1)), max(0.0, min(width, x2))))
    y1, y2 = sorted((max(0.0, min(height, y1)), max(0.0, min(height, y2))))
    if x2 <= x1 or y2 <= y1:
        raise ValueError(f"degenerate bbox {bbox!r}")
    return [x1, y1, x2, y2]


def iou(left: Sequence[float], right: Sequence[float]) -> float:
    """计算两个 xyxy 检测框的交并比，用于判断是否为同一区域。"""

    ix1 = max(float(left[0]), float(right[0]))
    iy1 = max(float(left[1]), float(right[1]))
    ix2 = min(float(left[2]), float(right[2]))
    iy2 = min(float(left[3]), float(right[3]))
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    intersection = iw * ih
    if intersection == 0:
        return 0.0
    left_area = max(0.0, float(left[2]) - float(left[0])) * max(
        0.0, float(left[3]) - float(left[1])
    )
    right_area = max(0.0, float(right[2]) - float(right[0])) * max(
        0.0, float(right[3]) - float(right[1])
    )
    union = left_area + right_area - intersection
    return intersection / union if union else 0.0


def weighted_box(detections: Iterable[RawDetection]) -> List[float]:
    """按置信度加权合并同一组检测框的坐标。"""

    items = list(detections)
    weights = [max(1e-6, float(item.score)) for item in items]
    total = sum(weights)
    return [
        sum(weight * float(item.bbox_px[index]) for item, weight in zip(items, weights))
        / total
        for index in range(4)
    ]


def cluster_detections(
    detections: Sequence[RawDetection], merge_iou: float = 0.5
) -> List[List[RawDetection]]:
    """将重叠的同类检测框分组；表格与插图不会因重叠而合并。"""

    clusters: List[List[RawDetection]] = []
    for detection in sorted(detections, key=lambda item: float(item.score), reverse=True):
        label = canonical_label(detection.label)
        for cluster in clusters:
            if canonical_label(cluster[0].label) == label and any(
                iou(detection.bbox_px, member.bbox_px) >= merge_iou for member in cluster
            ):
                cluster.append(detection)
                break
        else:
            clusters.append([detection])
    return clusters


def fused_detection(cluster: Sequence[RawDetection]) -> Dict[str, Any]:
    """合并同一区域的检测结果，同时保留每个模型的原始预测。"""

    if not cluster:
        raise ValueError("cannot fuse an empty detection cluster")
    label = canonical_label(cluster[0].label)
    best = max(cluster, key=lambda item: float(item.score))
    # 融合置信度只表示多模型支持程度；限制最大值为 1，避免产生非法置信度。
    confidence = min(1.0, float(best.score) + 0.05 * (len(cluster) - 1))
    provenance = []
    for item in cluster:
        nested_provenance = item.extra.get("source_provenance") if item.extra else None
        if isinstance(nested_provenance, list) and nested_provenance:
            provenance.extend(nested_provenance)
            continue
        provenance.append(
            {
                "model": item.model,
                "model_id": item.model_id,
                "backend": item.backend,
                "source": MODEL_SOURCE.get(item.model),
                "raw_label": item.raw_label or item.label,
                "confidence": round(float(item.score), 6),
                "bbox_px": [round(float(v), 3) for v in item.bbox_px],
                "category_id": item.category_id,
                "extra": item.extra,
            }
        )
    return {
        "label": label,
        "confidence": round(confidence, 6),
        "bbox_px": [round(float(v), 3) for v in weighted_box(cluster)],
        "provenance": provenance,
    }


# 模型接口：各模型延迟加载，使用某个后端时才导入它的依赖。

class DetectorBackend(ABC):
    """统一检测模型接口；模型只在首次预测时加载。"""
    name: str
    model: str
    model_id: str

    @property
    def metadata(self) -> Dict[str, Any]:
        """记录模型名称、权重路径及推理参数，便于追溯结果。"""
        return {
            "backend": self.name,
            "model": self.model,
            "model_id": self.model_id,
        }

    @abstractmethod
    def predict(self, image_path: Path) -> List[RawDetection]:
        """检测整页图片，返回以像素为单位的原始区域框。"""


# 默认后端：用 PaddleOCR 的 PP-DocLayoutV3 检测版面区域。

class PPDocLayoutV3Backend(DetectorBackend):
    """使用 PaddleOCR 的版面检测模型，默认检测后端。"""
    name = "pp_doclayoutv3"
    model = "PP-DocLayoutV3"
    model_id = "PaddlePaddle/PP-DocLayoutV3"

    def __init__(
        self,
        model_dir: str | Path | None = None,
        threshold: float = 0.2,
        device: str | None = None,
        engine: str | None = None,
        cpu_threads: int | None = None,
    ) -> None:
        """保存推理配置；此处不加载模型权重。"""
        self.model_dir = Path(model_dir).expanduser() if model_dir else None
        self.threshold = threshold
        self.device = device
        self.engine = engine
        self.cpu_threads = cpu_threads
        self._model = None

    @property
    def metadata(self) -> Dict[str, Any]:
        """记录模型名称、权重路径及推理参数，便于追溯结果。"""
        data = super().metadata
        data.update(
            {
                "model_source": self.model_id,
                "threshold": self.threshold,
                "model_dir": str(self.model_dir) if self.model_dir else None,
                "engine": self.engine or "paddle_static(default)",
                "cpu_threads": self.cpu_threads,
            }
        )
        return data

    def _load(self) -> Any:
        """第一次推理时加载并缓存模型，后续页面复用同一实例。"""
        if self._model is not None:
            return self._model
        try:
            from paddleocr import LayoutDetection  # type: ignore
        except ImportError as exc:  # pragma: no cover（可选依赖，取决于运行环境）
            raise RuntimeError(
                "PP-DocLayoutV3 requires paddleocr and paddlepaddle; "
                "install SDA/environment.yml"
            ) from exc
        kwargs: Dict[str, Any] = {
            "model_name": "PP-DocLayoutV3",
            "model_dir": str(self.model_dir) if self.model_dir else None,
            "device": self.device,
            "engine": self.engine,
            "cpu_threads": self.cpu_threads,
        }
        kwargs = {key: value for key, value in kwargs.items() if value is not None}
        try:
            self._model = LayoutDetection(**kwargs)
        except TypeError:
            # 兼容未提供全部 v3 参数的旧版 PaddleOCR。
            for key in ("engine", "device"):
                kwargs.pop(key, None)
            self._model = LayoutDetection(**kwargs)
        return self._model

    @staticmethod
    def _as_dict(result: Any) -> Dict[str, Any]:
        """兼容 PaddleOCR/PaddleX 返回的字典及 JSON 结果对象。"""
        if isinstance(result, dict):
            return result
        value = getattr(result, "json", None)
        if callable(value):
            value = value()
        if isinstance(value, str):
            value = json.loads(value)
        if isinstance(value, dict):
            return value
        # 部分 PaddleX 结果通过 res 属性提供可序列化的数据。
        value = getattr(result, "res", None)
        if isinstance(value, dict):
            return {"res": value}
        raise TypeError(f"Unsupported PP-DocLayoutV3 result type: {type(result)!r}")

    def predict(self, image_path: Path) -> List[RawDetection]:
        """检测整页图片，返回以像素为单位的原始区域框。"""
        model = self._load()
        results = model.predict(str(image_path), batch_size=1, layout_nms=True)
        detections: List[RawDetection] = []
        for result in results:
            payload = self._as_dict(result)
            boxes = payload.get("res", payload).get("boxes", [])
            for box in boxes:
                score = float(box.get("score", 0.0))
                if score < self.threshold:
                    continue
                label = str(box.get("label", box.get("cls_id", "unknown")))
                coordinate = box.get("coordinate", box.get("bbox"))
                if coordinate is None:
                    continue
                detections.append(
                    RawDetection(
                        label=label,
                        raw_label=label,
                        score=score,
                        bbox_px=coordinate,
                        model=self.model,
                        model_id=self.model_id,
                        backend=self.name,
                        category_id=int(box["cls_id"]) if box.get("cls_id") is not None else None,
                    )
                )
        return detections


# 可选后端：DocLayout-YOLO，可与默认模型共同检测。

class DocLayoutYOLOBackend(DetectorBackend):
    """使用 DocLayout-YOLO 检测表格、插图等版面区域。"""
    name = "doclayout_yolo"
    model = "DocLayout-YOLO"
    model_id = "juliozhao/DocLayout-YOLO-DocStructBench"

    def __init__(
        self,
        model_path: str | Path | None = None,
        threshold: float = 0.2,
        imgsz: int = 1024,
        device: str | None = None,
    ) -> None:
        """保存推理配置；此处不加载模型权重。"""
        self.model_path = Path(model_path).expanduser() if model_path else None
        self.threshold = threshold
        self.imgsz = imgsz
        self.device = device
        self._model = None

    @property
    def metadata(self) -> Dict[str, Any]:
        """记录模型名称、权重路径及推理参数，便于追溯结果。"""
        data = super().metadata
        data.update(
            {
                "model_source": self.model_id,
                "threshold": self.threshold,
                "imgsz": self.imgsz,
                "model_path": str(self.model_path) if self.model_path else None,
            }
        )
        return data

    def _resolve_model_path(self) -> str:
        """优先使用本地权重；未指定时从模型仓库获取默认权重。"""
        if self.model_path and self.model_path.is_file():
            return str(self.model_path)
        if self.model_path and self.model_path.is_dir():
            candidates = sorted(self.model_path.glob("*.pt"))
            if candidates:
                return str(candidates[0])
        if self.model_path:
            raise FileNotFoundError(f"DocLayout-YOLO checkpoint not found: {self.model_path}")
        try:
            from huggingface_hub import hf_hub_download  # type: ignore
        except ImportError as exc:  # pragma: no cover（可选依赖，取决于运行环境）
            raise RuntimeError(
                "DocLayout-YOLO requires doclayout-yolo and huggingface-hub; "
                "pass its checkpoint with --yolo-model"
            ) from exc
        return hf_hub_download(
            repo_id=self.model_id,
            filename="doclayout_yolo_docstructbench_imgsz1024.pt",
        )

    def _load(self) -> Any:
        """第一次推理时加载并缓存模型，后续页面复用同一实例。"""
        if self._model is not None:
            return self._model
        try:
            from doclayout_yolo import YOLOv10  # type: ignore
        except ImportError as exc:  # pragma: no cover（可选依赖，取决于运行环境）
            raise RuntimeError(
                "DocLayout-YOLO requires the doclayout-yolo package; "
                "install the optional dependencies in SDA/requirements-pdf2fig-extra.txt"
            ) from exc
        self._model = YOLOv10(self._resolve_model_path())
        return self._model

    def predict(self, image_path: Path) -> List[RawDetection]:
        """检测整页图片，返回以像素为单位的原始区域框。"""
        model = self._load()
        kwargs: Dict[str, Any] = {
            "imgsz": self.imgsz,
            "conf": self.threshold,
        }
        if self.device:
            kwargs["device"] = self.device
        results = model.predict(str(image_path), **kwargs)
        detections: List[RawDetection] = []
        for result in results:
            names = getattr(result, "names", {})
            boxes = getattr(result, "boxes", None)
            if boxes is None:
                continue
            xyxy = boxes.xyxy.detach().cpu().tolist()
            confidences = boxes.conf.detach().cpu().tolist()
            classes = boxes.cls.detach().cpu().tolist()
            for coordinate, score, category_id in zip(xyxy, confidences, classes):
                category_id = int(category_id)
                label = names.get(category_id, str(category_id)) if isinstance(names, dict) else str(category_id)
                detections.append(
                    RawDetection(
                        label=str(label),
                        raw_label=str(label),
                        score=float(score),
                        bbox_px=coordinate,
                        model=self.model,
                        model_id=self.model_id,
                        backend=self.name,
                        category_id=category_id,
                    )
                )
        return detections


# 可选后端：Table Transformer，仅检测表格。

class TableTransformerBackend(DetectorBackend):
    """使用 Table Transformer 检测表格区域。"""
    name = "tatr"
    model = "Table Transformer (TATR)"
    model_id = "microsoft/table-transformer-detection"

    def __init__(
        self,
        model_dir: str | Path | None = None,
        threshold: float = 0.7,
        device: str | None = None,
    ) -> None:
        """保存推理配置；此处不加载模型权重。"""
        self.model_dir = Path(model_dir).expanduser() if model_dir else None
        self.threshold = threshold
        self.device_name = device
        self._processor = None
        self._model = None
        self._device = None

    @property
    def metadata(self) -> Dict[str, Any]:
        """记录模型名称、权重路径及推理参数，便于追溯结果。"""
        data = super().metadata
        data.update(
            {
                "model_source": self.model_id,
                "threshold": self.threshold,
                "model_dir": str(self.model_dir) if self.model_dir else None,
            }
        )
        return data

    def _load(self) -> tuple[Any, Any, Any]:
        """第一次推理时加载并缓存模型，后续页面复用同一实例。"""
        if self._model is not None:
            return self._processor, self._model, self._device
        try:
            import torch  # type: ignore
            from transformers import AutoImageProcessor, AutoModelForObjectDetection  # type: ignore
        except ImportError as exc:  # pragma: no cover（可选依赖，取决于运行环境）
            raise RuntimeError(
                "TATR requires torch and transformers; install SDA/requirements-pdf2fig-extra.txt"
            ) from exc
        source = str(self.model_dir) if self.model_dir else self.model_id
        self._processor = AutoImageProcessor.from_pretrained(source)
        self._model = AutoModelForObjectDetection.from_pretrained(source)
        if self.device_name:
            self._device = torch.device(self.device_name)
        else:
            self._device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self._model.to(self._device)
        self._model.eval()
        return self._processor, self._model, self._device

    def predict(self, image_path: Path) -> List[RawDetection]:
        """检测整页图片，返回以像素为单位的原始区域框。"""
        from PIL import Image

        processor, model, device = self._load()
        image = Image.open(image_path).convert("RGB")
        inputs = processor(images=image, return_tensors="pt")
        inputs = {key: value.to(device) for key, value in inputs.items()}
        with __import__("torch").no_grad():
            outputs = model(**inputs)
        target_sizes = __import__("torch").tensor([[image.height, image.width]], device=device)
        processed = processor.post_process_object_detection(
            outputs, threshold=self.threshold, target_sizes=target_sizes
        )[0]
        id2label = getattr(model.config, "id2label", {})
        detections: List[RawDetection] = []
        for score, label_id, box in zip(
            processed["scores"].detach().cpu().tolist(),
            processed["labels"].detach().cpu().tolist(),
            processed["boxes"].detach().cpu().tolist(),
        ):
            label = str(id2label.get(int(label_id), "table")).lower()
            # TATR 默认只检测表格；自定义权重也仅保留表格类别。
            if "table" not in label:
                continue
            detections.append(
                RawDetection(
                    label="table",
                    raw_label=label,
                    score=float(score),
                    bbox_px=box,
                    model=self.model,
                    model_id=self.model_id,
                    backend=self.name,
                    category_id=int(label_id),
                )
            )
        return detections


# 整页渲染：低分辨率图片供检测使用，记录像素与 PDF 页面尺寸。

@dataclass
class RenderedPage:
    """记录渲染页的图片路径、像素尺寸和 PDF 点尺寸；页码从 1 开始。"""
    number: int
    image_path: Path
    width_px: int
    height_px: int
    width_pt: float
    height_pt: float


def render_pdf(
    pdf_path: str | Path,
    output_dir: str | Path,
    dpi: int = 150,
    pages: Iterable[int] | None = None,
) -> Iterator[RenderedPage]:
    """将选定 PDF 页渲染为 PNG，保留页面尺寸供后续坐标换算。"""

    if isinstance(dpi, bool) or not isinstance(dpi, int) or dpi <= 0:
        raise ValueError("dpi must be a positive integer")
    pdf_path = Path(pdf_path).expanduser().resolve()
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    requested = set(pages) if pages is not None else None
    scale = float(dpi) / 72.0
    document = pymupdf.open(pdf_path)
    try:
        if requested is not None and (not requested or any(
            isinstance(p, bool) or not isinstance(p, int) or not 1 <= p <= len(document)
            for p in requested
        )):
            raise ValueError(f"Requested PDF pages are empty or outside 1..{len(document)}")
        for index in range(len(document)):
            number = index + 1
            if requested is not None and number not in requested:
                continue
            page = document.load_page(index)
            rectangle = page.rect
            pixmap = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
            image_path = output_dir / f"page-{number:05d}.png"
            pixmap.save(str(image_path))
            yield RenderedPage(
                number=number,
                image_path=image_path,
                width_px=int(pixmap.width),
                height_px=int(pixmap.height),
                width_pt=float(rectangle.width),
                height_pt=float(rectangle.height),
            )
    finally:
        document.close()


# 串行检测：逐页调用模型，融合同类检测框并记录模型来源。

_sha256 = file_digest


def _convert_to_pdf_points(
    bbox_px: Sequence[float], width_px: int, height_px: int, width_pt: float, height_pt: float
) -> List[float]:
    """将检测图片的像素坐标换算为 PDF 点坐标，1 点为 1/72 英寸。"""
    scale_x = width_pt / float(width_px)
    scale_y = height_pt / float(height_px)
    return [
        round(float(bbox_px[0]) * scale_x, 3),
        round(float(bbox_px[1]) * scale_y, 3),
        round(float(bbox_px[2]) * scale_x, 3),
        round(float(bbox_px[3]) * scale_y, 3),
    ]


def fuse_page(rendered: Any, raw_detections: Sequence[RawDetection], merge_iou: float) -> tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """校验并融合单页检测框；串行和并行推理共用同一套坐标转换逻辑。"""
    valid: List[RawDetection] = []
    errors: List[Dict[str, Any]] = []
    for detection in raw_detections:
        try:
            detection.bbox_px = clamp_bbox(detection.bbox_px, rendered.width_px, rendered.height_px)
            valid.append(detection)
        except ValueError:
            errors.append({"page": rendered.number, "backend": detection.backend,
                           "error_type": "InvalidBoundingBox", "error": repr(detection.bbox_px)})
    regions: List[Dict[str, Any]] = []
    for index, cluster in enumerate(cluster_detections(valid, merge_iou), start=1):
        fused = fused_detection(cluster)
        fused.update(id=f"p{rendered.number:04d}-r{index:04d}", page=rendered.number,
                     bbox=_convert_to_pdf_points(fused["bbox_px"], rendered.width_px,
                                                rendered.height_px, rendered.width_pt, rendered.height_pt),
                     bbox_format="xyxy", coordinate_space="pdf_points", bbox_px_format="xyxy",
                     coordinate_space_px="rendered_image_pixels")
        regions.append(fused)
    regions.sort(key=lambda item: (item["bbox"][1], item["bbox"][0], item["label"]))
    return {"page": rendered.number, "width_pt": round(rendered.width_pt, 3),
            "height_pt": round(rendered.height_pt, 3), "width_px": rendered.width_px,
            "height_px": rendered.height_px, "detections": regions}, errors


class PatentRegionLocator:
    """逐页定位 PDF 中的版面区域，支持多模型检测与同类框融合。"""

    def __init__(
        self,
        backends: Sequence[str] = BACKEND_NAMES,
        *,
        model_dirs: Mapping[str, str | Path] | None = None,
        thresholds: Mapping[str, float] | None = None,
        dpi: int = 150,
        merge_iou: float = 0.5,
        device: str | None = None,
        pp_engine: str | None = None,
        allow_missing_backends: bool = False,
        cpu_threads: int | None = None,
    ) -> None:
        """保存推理配置；此处不加载模型权重。"""
        if not backends or len(set(backends)) != len(backends):
            raise ValueError("Select at least one backend, without duplicates")
        if dpi <= 0 or not 0 < merge_iou <= 1:
            raise ValueError("dpi must be positive and merge_iou must be in (0, 1]")
        if any(not 0 <= value <= 1 for value in (thresholds or {}).values()):
            raise ValueError("Detection thresholds must be in [0, 1]")
        self.backend_names = list(backends)
        self.model_dirs = {key: Path(value) for key, value in (model_dirs or {}).items()}
        for name, path in self.model_dirs.items():
            if name in self.backend_names and not path.expanduser().exists():
                raise FileNotFoundError(f"{name} model path not found: {path}")
        self.thresholds = {
            "pp_doclayoutv3": 0.2,
            "doclayout_yolo": 0.2,
            "tatr": 0.7,
        }
        self.thresholds.update(thresholds or {})
        self.dpi = dpi
        self.merge_iou = merge_iou
        self.device = device
        self.pp_engine = pp_engine
        self.allow_missing_backends = allow_missing_backends
        self.cpu_threads = cpu_threads
        self.backends: List[DetectorBackend] = []
        self.backend_status: List[Dict[str, Any]] = []
        self._build_backends()

    def _build_backends(self) -> None:
        """根据配置创建后端，分别转换 Paddle 与 Torch 的设备名称。"""
        constructors = {
            "pp_doclayoutv3": lambda: PPDocLayoutV3Backend(
                model_dir=self.model_dirs.get("pp_doclayoutv3"),
                threshold=self.thresholds["pp_doclayoutv3"],
                device=self.device.replace("cuda:", "gpu:") if self.device else None,
                engine=self.pp_engine,
                cpu_threads=self.cpu_threads,
            ),
            "doclayout_yolo": lambda: DocLayoutYOLOBackend(
                model_path=self.model_dirs.get("doclayout_yolo"),
                threshold=self.thresholds["doclayout_yolo"],
                device=self.device.replace("gpu:", "cuda:") if self.device else None,
            ),
            "tatr": lambda: TableTransformerBackend(
                model_dir=self.model_dirs.get("tatr"),
                threshold=self.thresholds["tatr"],
                device=self.device.replace("gpu:", "cuda:") if self.device else None,
            ),
        }
        for name in self.backend_names:
            if name not in constructors:
                raise ValueError(f"Unknown backend {name!r}; choose from {BACKEND_NAMES}")
            backend = constructors[name]()
            self.backends.append(backend)
            self.backend_status.append({"backend": name, "status": "configured", **backend.metadata})

    def locate(
        self,
        pdf_path: str | Path,
        *,
        pages: Iterable[int] | None = None,
        output_path: str | Path | None = None,
    ) -> Dict[str, Any]:
        """串行处理选定页面，返回检测区域、模型来源、错误记录和耗时。"""
        pdf_path = Path(pdf_path).expanduser().resolve()
        if not pdf_path.is_file():
            raise FileNotFoundError(pdf_path)
        requested_pages = list(pages) if pages is not None else None
        started = time.perf_counter()
        backend_seconds = {backend.name: 0.0 for backend in self.backends}
        results: List[Dict[str, Any]] = []
        errors: List[Dict[str, Any]] = []
        with tempfile.TemporaryDirectory(prefix="patent-layout-") as temporary_dir:
            for rendered in render_pdf(pdf_path, temporary_dir, dpi=self.dpi, pages=requested_pages):
                raw_detections: List[RawDetection] = []
                successful_backends = 0
                for backend in self.backends:
                    backend_started = time.perf_counter()
                    try:
                        raw_detections.extend(backend.predict(rendered.image_path))
                        self._set_status(backend.name, "ok")
                        successful_backends += 1
                    except Exception as exc:  # 单个后端失败时，可按配置保留其他模型的结果。
                        error = {
                            "page": rendered.number,
                            "backend": backend.name,
                            "error_type": type(exc).__name__,
                            "error": str(exc),
                        }
                        errors.append(error)
                        self._set_status(backend.name, "error", error)
                        if not self.allow_missing_backends:
                            raise RuntimeError(
                                f"{backend.name} failed on PDF page {rendered.number}: {exc}"
                            ) from exc
                    finally:
                        backend_seconds[backend.name] += time.perf_counter() - backend_started
                if not successful_backends:
                    detail = "; ".join(f"{e['backend']}: {e['error']}" for e in errors if e["page"] == rendered.number)
                    raise RuntimeError(f"No layout backend succeeded on PDF page {rendered.number}; check the model runtime. {detail}")
                result, invalid = fuse_page(rendered, raw_detections, self.merge_iou)
                errors.extend(invalid)
                results.append(result)

        payload: Dict[str, Any] = {
            "schema_version": "patent-layout-regions/v1",
            "document": {
                "path": str(pdf_path),
                "filename": pdf_path.name,
                "sha256": _sha256(pdf_path),
            },
            "settings": {
                "dpi": self.dpi,
                "merge_iou": self.merge_iou,
                "backends": self.backend_names,
                "coordinate_space": "pdf_points",
                "bbox_format": "xyxy",
                "device": self.device,
            },
            "models": self.backend_status,
            "errors": errors,
            "pages": results,
            "performance": {"execution": "serial", "elapsed_seconds": round(time.perf_counter() - started, 3),
                            "backend_seconds": {name: round(value, 3) for name, value in backend_seconds.items()}},
        }
        if output_path is not None:
            destination = Path(output_path).expanduser()
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return payload

    def _set_status(self, backend_name: str, status: str, error: Dict[str, Any] | None = None) -> None:
        """更新各模型的运行状态，失败时附上最后一次错误。"""
        for entry in self.backend_status:
            if entry["backend"] == backend_name:
                entry["status"] = status
                if error:
                    entry["last_error"] = error
                return


# 高清裁图：按 PDF 坐标重新渲染原文区域，避免放大检测用的低清图片。

def _fitz() -> Any:
    """兼容旧的内部调用，统一使用文件头导入的 PyMuPDF。"""
    return pymupdf


def _as_xyxy(value: Sequence[float]) -> List[float]:
    """校验裁图坐标为 [左, 上, 右, 下]，拒绝反向框或空框。"""
    if len(value) != 4:
        raise ValueError(f"bbox must contain four values, got {value!r}")
    x1, y1, x2, y2 = (float(item) for item in value)
    if not all(math.isfinite(v) for v in (x1, y1, x2, y2)):
        raise ValueError(f"non-finite bbox {value!r}")
    if x2 <= x1 or y2 <= y1:
        raise ValueError(f"degenerate bbox {value!r}")
    return [x1, y1, x2, y2]


def _padded_rect(fitz: Any, page: Any, bbox: Sequence[float], padding_pt: float) -> Any:
    """给检测框扩边并限制在页面范围内，防止裁图越界。"""
    page_rect = page.rect
    x1, y1, x2, y2 = _as_xyxy(bbox)
    if not math.isfinite(padding_pt) or padding_pt < 0:
        raise ValueError("padding_pt must be non-negative")
    # PDF 坐标以页面左上角为原点，扩边后仍需限制在页面内部。
    rect = fitz.Rect(
        max(page_rect.x0, x1 - padding_pt),
        max(page_rect.y0, y1 - padding_pt),
        min(page_rect.x1, x2 + padding_pt),
        min(page_rect.y1, y2 + padding_pt),
    )
    if rect.x1 <= rect.x0 or rect.y1 <= rect.y0:
        raise ValueError(f"bbox outside page bounds: {bbox!r}")
    return rect


def _table_regions(
    payload: Mapping[str, Any],
    *,
    labels: Iterable[str] = ("table",),
    min_confidence: float = 0.0,
) -> List[Dict[str, Any]]:
    """按类别和置信度筛选区域，再按页码、从上到下、从左到右排序。"""
    accepted = {str(label).strip().lower() for label in labels}
    regions: List[Dict[str, Any]] = []
    for page in payload.get("pages", []):
        page_number = int(page["page"])
        for region in page.get("detections", []):
            label = str(region.get("label", "")).strip().lower()
            confidence = float(region.get("confidence", 0.0))
            if label not in accepted or confidence < min_confidence:
                continue
            bbox = region.get("bbox")
            if bbox is None:
                continue
            normalized = dict(region)
            normalized["page"] = page_number
            normalized["bbox"] = _as_xyxy(bbox)
            regions.append(normalized)
    regions.sort(
        key=lambda item: (
            int(item["page"]),
            float(item["bbox"][1]),
            float(item["bbox"][0]),
            -float(item.get("confidence", 0.0)),
        )
    )
    return regions


def crop_table_regions(
    payload: Mapping[str, Any],
    output_dir: str | Path,
    *,
    pdf_path: str | Path | None = None,
    dpi: int = 400,
    padding_pt: float = 6.0,
    labels: Iterable[str] = ("table",),
    min_confidence: float = 0.0,
) -> List[Dict[str, Any]]:
    """按 PDF 点坐标从原文重新渲染高清区域，返回图片及裁图信息。"""

    if dpi <= 0:
        raise ValueError("dpi must be positive")
    output_dir = Path(output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    source = Path(pdf_path or payload.get("document", {}).get("path", "")).expanduser()
    if not source.is_file():
        raise FileNotFoundError(f"source PDF not found: {source}")

    fitz = _fitz()
    regions = _table_regions(payload, labels=labels, min_confidence=min_confidence)
    matrix = fitz.Matrix(float(dpi) / 72.0, float(dpi) / 72.0)
    results: List[Dict[str, Any]] = []
    document = fitz.open(source)
    try:
        for index, region in enumerate(regions, start=1):
            page_number = int(region["page"])
            if page_number < 1 or page_number > len(document):
                raise ValueError(f"page {page_number} is outside PDF range 1..{len(document)}")
            page = document.load_page(page_number - 1)
            crop_rect = _padded_rect(fitz, page, region["bbox"], padding_pt)
            pixmap = page.get_pixmap(matrix=matrix, clip=crop_rect, alpha=False)
            kind = "figure" if region.get("label") == "figure" else "table"
            filename = f"{kind}-{index:04d}-page-{page_number:04d}.png"
            image_path = output_dir / filename
            pixmap.save(str(image_path))
            results.append(
                {
                    "index": index,
                    "id": region.get("id", f"p{page_number:04d}-table-{index:04d}"),
                    "page": page_number,
                    "label": region.get("label", "table"),
                    "confidence": round(float(region.get("confidence", 0.0)), 6),
                    "bbox": [round(float(value), 3) for value in region["bbox"]],
                    "bbox_padded": [
                        round(float(crop_rect.x0), 3),
                        round(float(crop_rect.y0), 3),
                        round(float(crop_rect.x1), 3),
                        round(float(crop_rect.y1), 3),
                    ],
                    "dpi": dpi,
                    "padding_pt": padding_pt,
                    "width_px": int(pixmap.width),
                    "height_px": int(pixmap.height),
                    "crop_to_pdf": [72.0 / dpi, 0, 0, 72.0 / dpi,
                                    pixmap.x * 72.0 / dpi, pixmap.y * 72.0 / dpi],
                    "image": filename,
                }
            )
    finally:
        document.close()

    metadata_path = output_dir / "table_crops.json"
    metadata_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    return results


# 并行检测：主进程渲染页面，独立子进程分别加载 Paddle/Torch 模型。

def report_progress(completed: int, total: int, detail: str) -> None:
    """输出已完成的页数；保留进度事件格式以兼容现有调用方。"""
    print(f'layout-page {completed}/{total}: {detail}', flush=True)
    # 保留原进度事件格式；使用实际完成页数，避免乱序完成时进度跳跃。
    print(f'layout-progress page-{completed:05d}.png completed={completed}/{total}', flush=True)


def isolated_env(python: str, threads: int, *, paddle: bool = False) -> dict[str, str]:
    """隔离子进程的 Python/CUDA 动态库路径，并限制本地计算库线程数。"""
    env = os.environ.copy()
    env.pop('PYTHONPATH', None)
    env.pop('PYTHONHOME', None)
    prefix = Path(python).resolve().parent.parent
    libraries = [prefix / 'lib', Path('/usr/local/nvidia/lib'),
                 Path('/usr/local/nvidia/lib64'), Path('/usr/local/cuda/lib64')]
    env['LD_LIBRARY_PATH'] = os.pathsep.join(str(p) for p in libraries if p.is_dir())
    for key in ['OMP_NUM_THREADS', 'MKL_NUM_THREADS', 'OPENBLAS_NUM_THREADS',
                'NUMEXPR_NUM_THREADS', 'PADDLE_PDX_CPU_NUM_THREADS']:
        env[key] = str(threads)
    if paddle:
        # Paddle 自己控制推理线程数，关闭嵌套 BLAS/OMP 线程池以免进程间争抢 CPU。
        for key in ['OMP_NUM_THREADS', 'MKL_NUM_THREADS', 'OPENBLAS_NUM_THREADS']:
            env[key] = '1'
    env['PYTHONUNBUFFERED'] = '1'
    return env


def load_runtime() -> dict[str, Any]:
    """读取本项目的运行配置；SDA_LAYOUT_* 环境变量可覆盖对应参数。"""
    config = json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.is_file() else {}
    for key in ['execution', 'torch_python', 'torch_device', 'pp_device',
                'cpu_workers', 'max_workers', 'threads_per_worker']:
        value = os.environ.get('SDA_LAYOUT_' + key.upper())
        if value is not None:
            config[key] = int(value) if key in {'cpu_workers', 'max_workers', 'threads_per_worker'} else value
    return config


def run_worker(job_path: Path) -> int:
    """内部子进程入口：每个进程加载一个模型，逐页写出 JSONL 检测结果。"""
    job = json.loads(job_path.read_text())
    name = job['backend']
    locator = PatentRegionLocator(backends=[name], model_dirs=job['model_dirs'],
                                  thresholds=job['thresholds'], device=job['device'], pp_engine=job['pp_engine'],
                                  cpu_threads=job['threads'])
    backend = locator.backends[0]
    try:
        import cv2
        cv2.setNumThreads(job['threads'])
    except ImportError:
        pass
    if name != 'pp_doclayoutv3':
        import torch
        torch.set_num_threads(job['threads'])
    with Path(job['results']).open('w') as output:
        for page in job['pages']:
            started = time.perf_counter()
            row = {'page': page['number'], 'backend': name, 'ok': False, 'detections': []}
            try:
                predictions = backend.predict(Path(page['image_path']))
                row.update(ok=True, detections=[{**asdict(item), 'bbox_px': [float(v) for v in item.bbox_px],
                                                'score': float(item.score)} for item in predictions])
            except Exception as exc:
                row.update(error_type=type(exc).__name__, error=str(exc))
            row['seconds'] = round(time.perf_counter() - started, 6)
            output.write(json.dumps(row) + '\n')
            output.flush()
            print(WORKER_EVENT + json.dumps({'backend': name, 'page': row['page'], 'ok': row['ok']}), flush=True)
    return 0


def plan_jobs(locator: PatentRegionLocator, pages: list[RenderedPage], directory: Path,
              torch_python: str | None, torch_device: str, pp_device: str,
              cpu_workers: int, threads: int) -> list[dict[str, Any]]:
    """为各后端分配页面；GPU 每个模型只开一个进程，CPU 可分片处理。"""
    jobs = []
    for name in locator.backend_names:
        device = pp_device if name == 'pp_doclayoutv3' else torch_device
        python = (torch_python or sys.executable) if name != 'pp_doclayoutv3' else sys.executable
        if not Path(python).is_file():
            raise ValueError(f'Layout interpreter is missing: {python}')
        # GPU 每个模型仅加载一次权重；CPU 按限定进程数分配页面。
        shards = min(cpu_workers if device == 'cpu' else 1, len(pages))
        for index in range(shards):
            identity = f'{name}-{index + 1}'
            jobs.append({'id': identity, 'backend': name, 'python': python, 'device': device,
                         'threads': threads, 'pp_engine': locator.pp_engine,
                         'model_dirs': {k: str(v) for k, v in locator.model_dirs.items()},
                         'thresholds': locator.thresholds,
                         'pages': [{**asdict(p), 'image_path': str(p.image_path)} for p in pages[index::shards]],
                         'results': str(directory / (identity + '.jsonl')),
                         'log': str(directory / (identity + '.log'))})
    return jobs


def supervise(job: dict[str, Any], job_path: Path, events: queue.Queue) -> dict[str, Any]:
    """启动并监控推理子进程，转发进度事件并保存完整日志。"""
    started = time.perf_counter()
    command = [job['python'], str(Path(__file__).resolve()), '_layout-worker', '--job', str(job_path)]
    with Path(job['log']).open('w') as log:
        process = subprocess.Popen(command, env=isolated_env(job['python'], job['threads'], paddle=job['backend'] == 'pp_doclayoutv3'),
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            assert process.stdout is not None
            for line in process.stdout:
                log.write(line)
                log.flush()
                if line.startswith(WORKER_EVENT):
                    event = json.loads(line[len(WORKER_EVENT):])
                    events.put((event['backend'], int(event['page'])))
            code = process.wait()
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            if process.stdout is not None:
                process.stdout.close()
    return {'id': job['id'], 'backend': job['backend'], 'python': job['python'],
            'device': job['device'], 'pages': [p['number'] for p in job['pages']],
            'exit_code': code, 'elapsed_seconds': round(time.perf_counter() - started, 3),
            'log': job['log']}


def merge_results(locator: PatentRegionLocator, pages: list[RenderedPage], jobs: list[dict[str, Any]],
                  runs: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """汇总各页各模型的结果，记录失败后端，并按串行规则融合检测框。"""
    rows: dict[tuple[str, int], dict[str, Any]] = {}
    errors = []
    for job, run in zip(jobs, runs):
        path = Path(job['results'])
        expected = {p['number'] for p in job['pages']}
        if path.exists():
            for line in path.read_text().splitlines():
                row = json.loads(line)
                key = (row['backend'], row['page'])
                if row['backend'] != job['backend'] or row['page'] not in expected or key in rows:
                    raise ValueError('Unexpected or duplicate page/backend in layout worker output')
                rows[key] = row
        for number in expected:
            key = (job['backend'], number)
            if key not in rows:
                rows[key] = {'page': number, 'backend': job['backend'], 'ok': False,
                             'error_type': 'WorkerFailure',
                             'error': f"{job['id']} exited {run['exit_code']} without a page result; see {job['log']}"}
    results = []
    for page in sorted(pages, key=lambda p: p.number):
        detections = []
        successful = 0
        for name in locator.backend_names:
            row = rows[(name, page.number)]
            if row['ok']:
                successful += 1
                predictions = [RawDetection(**d) for d in row['detections']]
                if any(d.backend != name for d in predictions):
                    raise ValueError('Layout detection provenance does not match its worker')
                detections.extend(predictions)
            else:
                error = {k: row[k] for k in ['page', 'backend', 'error_type', 'error']}
                errors.append(error)
                if not locator.allow_missing_backends:
                    raise RuntimeError(f"{name} failed on PDF page {page.number}: {row['error']}")
        if not successful:
            raise RuntimeError(f'No layout backend succeeded on PDF page {page.number}; see layout worker logs')
        result, invalid = fuse_page(page, detections, locator.merge_iou)
        results.append(result)
        errors.extend(invalid)
    models = []
    for model in locator.backend_status:
        relevant = [r for (name, _), r in rows.items() if name == model['backend']]
        failed = [r for r in relevant if not r['ok']]
        models.append({**model, 'status': 'error' if len(failed) == len(relevant) else 'partial' if failed else 'ok',
                       'failed_pages': [r['page'] for r in failed],
                       'inference_seconds': round(sum(r.get('seconds', 0) for r in relevant), 3)})
    return results, errors, models


def locate_parallel(locator: PatentRegionLocator, pdf_path: Path, *, output_path: Path,
                    selected_pages: list[int] | None = None, torch_python: str | None = None,
                    torch_device: str = 'cpu', pp_device: str = 'cpu', cpu_workers: int = 2,
                    max_workers: int = 4, threads_per_worker: int = 4) -> dict[str, Any]:
    """有界并行推理；主进程统一渲染页面，子进程分别运行检测模型。"""
    if not locator.backend_names or min(cpu_workers, max_workers, threads_per_worker) < 1:
        raise ValueError('Parallel layout needs at least one backend and positive worker/thread limits')
    if len(set(locator.backend_names)) != len(locator.backend_names):
        raise ValueError('Parallel layout backends must be unique')
    pdf_path = pdf_path.resolve()
    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    # 子进程日志与结果一起保存，方便查看失败原因和推理耗时。
    work = Path(tempfile.mkdtemp(prefix=output_path.stem + '-workers-', dir=output_path.parent))
    with tempfile.TemporaryDirectory(prefix='patent-layout-images-') as images:
        render_started = time.perf_counter()
        pages = list(render_pdf(pdf_path, images, dpi=locator.dpi, pages=selected_pages))
        if not pages or (selected_pages is not None and {p.number for p in pages} != set(selected_pages)):
            raise ValueError('Requested PDF pages are empty or outside the document')
        render_seconds = time.perf_counter() - render_started
        jobs = plan_jobs(locator, pages, work, torch_python, torch_device, pp_device, cpu_workers, threads_per_worker)
        events: queue.Queue = queue.Queue()
        seen: dict[int, set[str]] = {p.number: set() for p in pages}
        completed = set()
        print(f'layout-runtime parallel processes={min(max_workers, len(jobs))} '
              f'torch={torch_device} paddle={pp_device} cpu_page_workers={cpu_workers}', flush=True)

        def progress():
            """消费子进程进度事件，全部后端完成当前页后再计入完成页数。"""
            while True:
                try:
                    name, number = events.get_nowait()
                except queue.Empty:
                    return
                if number not in seen or name not in locator.backend_names:
                    raise ValueError('Unexpected layout progress event')
                seen[number].add(name)
                if len(seen[number]) == len(locator.backend_names) and number not in completed:
                    completed.add(number)
                    report_progress(len(completed), len(pages), f'source-page-{number:05d}')

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = []
            for job in jobs:
                path = work / (job['id'] + '.json')
                path.write_text(json.dumps(job))
                futures.append(pool.submit(supervise, job, path, events))
            while not all(f.done() for f in futures):
                progress()
                time.sleep(0.05)
            progress()
            runs = [f.result() for f in futures]
        results, errors, models = merge_results(locator, pages, jobs, runs)
        if len(completed) < len(pages):
            report_progress(len(pages), len(pages), 'merged with optional-backend failures')
        payload = {'schema_version': 'patent-layout-regions/v1',
                   'document': {'path': str(pdf_path), 'filename': pdf_path.name, 'sha256': _sha256(pdf_path)},
                   'settings': {'dpi': locator.dpi, 'merge_iou': locator.merge_iou,
                                'backends': locator.backend_names, 'coordinate_space': 'pdf_points', 'bbox_format': 'xyxy'},
                   'models': models, 'errors': errors, 'pages': results,
                   'performance': {'execution': 'parallel', 'elapsed_seconds': round(time.perf_counter() - started, 3),
                                   'render_seconds': round(render_seconds, 3), 'max_workers': max_workers,
                                   'cpu_workers': cpu_workers, 'threads_per_worker': threads_per_worker, 'workers': runs}}
        staged = output_path.with_suffix(output_path.suffix + '.tmp')
        staged.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
        staged.replace(output_path)
        return payload


# 辅助命令：检查依赖、仅输出定位结果，以及启动内部推理子进程。

def build_layout_parser() -> argparse.ArgumentParser:
    """构建定位、依赖检查和内部推理子进程的命令行参数。"""
    parser = argparse.ArgumentParser(description="定位 PDF 中的版面和表格区域")
    subparsers = parser.add_subparsers(dest="command", required=True)

    locate = subparsers.add_parser("locate", help="运行选定的版面检测模型")
    locate.add_argument("--input", required=True, type=Path, help="输入 PDF 文件")
    locate.add_argument("--output", required=True, type=Path, help="输出检测结果 JSON")
    locate.add_argument(
        "--backends",
        nargs="+",
        choices=BACKEND_NAMES,
        default=list(BACKEND_NAMES),
        help="选择一个或多个检测后端",
    )
    locate.add_argument("--pp-model-dir", type=Path, help="本地 PP-DocLayoutV3 权重目录")
    locate.add_argument("--yolo-model", type=Path, help="本地 YOLO .pt 权重或目录")
    locate.add_argument("--tatr-model-dir", type=Path, help="本地 TATR 权重目录")
    locate.add_argument("--dpi", type=int, default=150)
    locate.add_argument("--merge-iou", type=float, default=0.5)
    locate.add_argument("--device", default=None, help="推理设备，如 cpu、cuda:0 或 gpu:0")
    locate.add_argument("--pp-engine", default=None, help="推理引擎：paddle_static、transformers 或 onnxruntime")
    locate.add_argument("--pp-threshold", type=float, default=0.2)
    locate.add_argument("--yolo-threshold", type=float, default=0.2)
    locate.add_argument("--tatr-threshold", type=float, default=0.7)
    locate.add_argument("--pages", nargs="+", type=int, help="页码从 1 开始，默认处理全部页面")
    locate.add_argument("--execution", choices=("auto", "serial", "parallel"), default="auto",
                        help="auto 读取运行配置，serial 使用串行推理并忽略运行配置")
    locate.add_argument("--torch-python", help="YOLO/TATR 使用的独立 Python 解释器")
    locate.add_argument("--torch-device", help="YOLO/TATR 设备，如 cpu 或 cuda:0")
    locate.add_argument("--pp-device", help="Paddle 设备，如 cpu 或 gpu:0")
    locate.add_argument("--cpu-workers", type=int, help="每个 CPU 后端的页面处理进程数，默认 2")
    locate.add_argument("--max-workers", type=int, help="同时运行的推理进程上限，默认 4")
    locate.add_argument("--threads-per-worker", type=int, help="每个进程的 CPU 线程数，默认 4")
    locate.add_argument(
        "--allow-missing-backends",
        action="store_true",
        help="允许部分后端失败，继续处理并记录错误",
    )
    locate.add_argument("--print-json", action="store_true", help="同时在终端输出结果 JSON")

    doctor = subparsers.add_parser("doctor", help="检查可选依赖及模型配置")
    doctor.add_argument("--pp-model-dir", type=Path)
    doctor.add_argument("--yolo-model", type=Path)
    doctor.add_argument("--tatr-model-dir", type=Path)
    worker = subparsers.add_parser("_layout-worker", help=argparse.SUPPRESS)
    worker.add_argument("--job", required=True, type=Path)
    return parser


def _doctor(args: argparse.Namespace) -> int:
    """检查依赖是否可导入并展示配置，不加载模型权重。"""
    import importlib.util

    modules = ["fitz", "paddleocr", "paddle", "doclayout_yolo", "transformers", "torch"]
    report = {module: bool(importlib.util.find_spec(module)) for module in modules}
    report["runtime"] = load_runtime()
    report["paths"] = {
        "pp_model_dir": str(args.pp_model_dir) if args.pp_model_dir else None,
        "yolo_model": str(args.yolo_model) if args.yolo_model else None,
        "tatr_model_dir": str(args.tatr_model_dir) if args.tatr_model_dir else None,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def layout_main(argv: Sequence[str] | None = None) -> int:
    """处理 doctor、locate 和内部 _layout-worker 命令。"""
    args = build_layout_parser().parse_args(argv)
    if args.command == "_layout-worker":
        return run_worker(args.job)
    if args.command == "doctor":
        return _doctor(args)
    model_dirs = {
        "pp_doclayoutv3": args.pp_model_dir,
        "doclayout_yolo": args.yolo_model,
        "tatr": args.tatr_model_dir,
    }
    locator = PatentRegionLocator(
        backends=args.backends,
        model_dirs={key: value for key, value in model_dirs.items() if value is not None},
        thresholds={
            "pp_doclayoutv3": args.pp_threshold,
            "doclayout_yolo": args.yolo_threshold,
            "tatr": args.tatr_threshold,
        },
        dpi=args.dpi,
        merge_iou=args.merge_iou,
        device=args.device,
        pp_engine=args.pp_engine,
        allow_missing_backends=args.allow_missing_backends,
        cpu_threads=args.threads_per_worker,
    )
    config = load_runtime() if args.execution != "serial" else {}
    execution = config.get("execution", "serial") if args.execution == "auto" else args.execution
    if execution == "parallel":
        pp_device = args.pp_device or args.device or config.get("pp_device", "cpu")
        pp_device = pp_device.replace("cuda:", "gpu:")
        result = locate_parallel(
            locator, args.input, output_path=args.output, selected_pages=args.pages,
            torch_python=args.torch_python or config.get("torch_python"),
            torch_device=args.torch_device or args.device or config.get("torch_device", "cpu"),
            pp_device=pp_device,
            cpu_workers=args.cpu_workers if args.cpu_workers is not None else int(config.get("cpu_workers", 2)),
            max_workers=args.max_workers if args.max_workers is not None else int(config.get("max_workers", 4)),
            threads_per_worker=args.threads_per_worker if args.threads_per_worker is not None else int(config.get("threads_per_worker", 4)),
        )
    else:
        result = locator.locate(args.input, pages=args.pages, output_path=args.output)
    print(f"wrote {args.output} ({sum(len(page['detections']) for page in result['pages'])} regions)")
    if args.print_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


# 主流程：校正页面方向 → 检测区域 → 高清裁图 → 生成续表上下文与来源清单。

def save_json(path, value):
    """先写临时文件再替换，避免中途退出留下不完整的 JSON。"""
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def parse_pages(values):
    """解析从 1 开始的页码，支持 9 10、9,10 和 9-10,13 等写法。"""
    if values is None:
        return None
    pages = set()
    for value in values:
        for part in str(value).split(","):
            if re.fullmatch(r"[1-9]\d*", part):
                pages.add(int(part))
            elif re.fullmatch(r"[1-9]\d*-[1-9]\d*", part):
                first, last = map(int, part.split("-"))
                if last < first:
                    raise ValueError(f"Invalid page range: {part}")
                pages.update(range(first, last + 1))
            else:
                raise ValueError(f"Invalid page: {part!r}")
    if not pages:
        raise ValueError("Select at least one page")
    return sorted(pages)


def document_name(pdf, explicit=None):
    """确定文档编号并校验文件名；优先使用显式编号，其次提取专利号。"""
    match = PATENT_ID.search(str(pdf))
    name = explicit or (match.group().upper() if match else pdf.stem)
    if name in {".", ".."} or not re.fullmatch(r"[\w.-]+", name):
        raise ValueError("document_id must contain only letters, numbers, '_', '-' or '.'")
    return name


def _validate_layout(payload, pdf, pages, digest, total_pages):
    """复用检测结果前核对 PDF 哈希、坐标格式、页码和区域编号。"""
    document = payload.get("document", {})
    if document.get("sha256") and document["sha256"] != digest:
        raise ValueError("Saved regions belong to a different PDF (SHA-256 mismatch)")
    settings = payload.get("settings", {})
    if settings.get("coordinate_space", "pdf_points") != "pdf_points":
        raise ValueError("Saved regions must use PDF-point coordinates")
    if settings.get("bbox_format", "xyxy") != "xyxy":
        raise ValueError("Saved regions must use xyxy boxes")
    selected, seen, region_ids = [], set(), set()
    for page in payload.get("pages", []):
        number = page["page"]
        if isinstance(number, bool) or not isinstance(number, int) or not 1 <= number <= total_pages or number in seen:
            raise ValueError(f"Invalid or duplicate layout page: {number}")
        seen.add(number)
        if pages is not None and number not in pages:
            continue
        for index, region in enumerate(page.get("detections", []), 1):
            if region.get("coordinate_space", "pdf_points") != "pdf_points" or region.get("bbox_format", "xyxy") != "xyxy":
                raise ValueError("Region boxes must use PDF points and xyxy format")
            region.setdefault("id", f"p{number:04d}-r{index:04d}")
            if region["id"] in region_ids:
                raise ValueError(f"Duplicate region ID: {region['id']}")
            region_ids.add(region["id"])
            region["label"] = canonical_label(region.get("label"))
        selected.append(page)
    if not selected or (pages is not None and set(pages) - seen):
        raise ValueError("Requested pages are absent from the saved layout")
    payload["pages"] = sorted(selected, key=lambda p: p["page"])
    payload["document"] = {**document, "path": str(pdf), "filename": pdf.name, "sha256": digest}
    return payload


def extract_pdf(pdf_path, output_dir, *, document_id=None, regions_json=None,
                backends=("pp_doclayoutv3",), model_dirs=None, thresholds=None,
                pages=None, dpi=150, crop_dpi=400, padding_pt=6.0,
                labels=("table",), min_confidence=0.8, device="cpu", pp_engine=None,
                execution="serial", cpu_workers=2, max_workers=4, threads_per_worker=4,
                torch_python=None, torch_device=None, pp_device=None,
                allow_missing_backends=False, merge_iou=0.5, orientation="auto"):
    """在新目录或空目录中完成 PDF 检测和高清裁图，并写出来源清单。

    只负责生成图片及续表上下文；OCR/LLM 由后续流程处理。
    图片文件名保留文档编号，便于 graph.py 识别所属专利。
    若步骤 2 已完成，传入工作 PDF 并设 orientation="off"，避免重复校正。
    """
    # 1. 校验输入与输出目录，防止已有图片和本次裁图混在一起。
    started = perf_counter()
    pdf, out = Path(pdf_path).expanduser().resolve(), Path(output_dir).expanduser().resolve()
    if not pdf.is_file():
        raise FileNotFoundError(pdf)
    name = document_name(pdf, document_id)
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise ValueError(f"Output directory must be new or empty: {out}")
    if min(dpi, crop_dpi, cpu_workers, max_workers, threads_per_worker) < 1:
        raise ValueError("DPI and worker/thread counts must be positive")
    if not math.isfinite(padding_pt) or padding_pt < 0 or not 0 <= min_confidence <= 1:
        raise ValueError("padding_pt must be nonnegative; min_confidence must be in [0, 1]")
    if execution not in {"serial", "parallel"}:
        raise ValueError("execution must be serial or parallel")
    if orientation not in {"auto", "off"}:
        raise ValueError("orientation must be auto or off")
    labels = tuple(dict.fromkeys(canonical_label(label) for label in labels))
    if not labels or any(label not in {"table", "figure"} for label in labels):
        raise ValueError("Select table and/or figure labels")
    with _fitz().open(pdf) as document:
        total_pages = len(document)
    if not total_pages or (pages is not None and (not pages or any(
        isinstance(p, bool) or not isinstance(p, int) or not 1 <= p <= total_pages for p in pages
    ))):
        raise ValueError(f"Requested PDF pages are empty or outside 1..{total_pages}")
    digest = _sha256(pdf)
    processing_pdf, preparation = pdf, None
    if regions_json:
        # 2. 复用已有检测框时保留原坐标，不再次校正页面方向。
        regions_path = Path(regions_json).expanduser().resolve()
        payload = json.loads(regions_path.read_text(encoding="utf-8"))
        if payload.get("document", {}).get("sha256") not in {None, digest}:
            # 新检测的框属于 prepared.pdf。核对原 PDF 和工作副本后一起复用，
            # 不能直接把这些框应用到仍有原始旋转的 PDF 上。
            prepared = Path(payload.get("document", {}).get("path", ""))
            orientation_path = regions_path.parent / "page_orientation.json"
            if prepared.is_file() and orientation_path.is_file():
                preparation = validate_preparation(pdf, prepared, orientation_path)
                if _sha256(prepared) != payload["document"]["sha256"]:
                    raise ValueError("Saved regions do not belong to the prepared PDF")
                out.mkdir(parents=True, exist_ok=True)
                processing_pdf = out / "prepared.pdf"
                shutil.copyfile(prepared, processing_pdf)
                save_json(out / "page_orientation.json", preparation)
        payload = _validate_layout(payload, processing_pdf, pages, _sha256(processing_pdf), total_pages)
        reused = {"file": str(regions_path), "sha256": _sha256(regions_path)}
    else:
        # 3. 先校正页面方向，再用低分辨率整页图检测表格或插图。
        if orientation == "auto":
            processing_pdf = out / "prepared.pdf"
            def progress(current, total, page):
                """输出页面方向校正进度，只展示所选页面。"""
                if pages is None or current in pages:
                    print(f"orientation-page {current}/{total}: {page['status']} "
                          f"clockwise={page['correction_clockwise']}", flush=True)
            preparation = prepare_pdf(pdf, processing_pdf, out / "page_orientation.json",
                                      selected_pages=pages, progress=progress)
        # CPU 推理关闭 oneDNN，与 table_ocr.py 的 Paddle 配置保持一致。
        os.environ.setdefault("PADDLE_PDX_ENABLE_MKLDNN_BYDEFAULT", "false")
        os.environ.setdefault("FLAGS_use_mkldnn", "0")
        locator = PatentRegionLocator(backends=backends, model_dirs=model_dirs,
            thresholds=thresholds, dpi=dpi, merge_iou=merge_iou, device=device,
            pp_engine=pp_engine, cpu_threads=threads_per_worker,
            allow_missing_backends=allow_missing_backends)
        out.mkdir(parents=True, exist_ok=True)
        if execution == "parallel":
            payload = locate_parallel(locator, processing_pdf, output_path=out / "regions.json",
                selected_pages=pages, torch_python=torch_python,
                torch_device=(torch_device or device).replace("gpu:", "cuda:"),
                pp_device=(pp_device or device).replace("cuda:", "gpu:"),
                cpu_workers=cpu_workers, max_workers=max_workers,
                threads_per_worker=threads_per_worker)
        else:
            payload = locator.locate(processing_pdf, pages=pages)
        payload = _validate_layout(payload, processing_pdf, pages, _sha256(processing_pdf), total_pages)
        reused = None
    out.mkdir(parents=True, exist_ok=True)
    save_json(out / "regions.json", payload)
    # 4. 从处理后的 PDF 重新渲染高清裁图，记录文件哈希和源文档坐标。
    crops = crop_table_regions(payload, out, pdf_path=processing_pdf, dpi=crop_dpi,
                              padding_pt=padding_pt, labels=labels, min_confidence=min_confidence)
    detections = {r["id"]: r for p in payload["pages"] for r in p["detections"]}
    for crop in crops:
        image = out / crop["image"]
        crop["image"] = f"{name}_{crop['image']}"
        image.rename(out / crop["image"])
        crop.update(document_id=name, sha256=_sha256(out / crop["image"]),
                    size_bytes=(out / crop["image"]).stat().st_size,
                    provenance=detections[crop["id"]].get("provenance", []))
        page = next(p for p in payload["pages"] if p["page"] == crop["page"])
        crop.update(page_width=page.get("width_pt"), page_height=page.get("height_pt"))
        if preparation:
            from pymupdf import Matrix, Rect
            transform = preparation["pages"][crop["page"] - 1]
            crop["bbox_source"] = list(Rect(crop["bbox"]) * Matrix(transform["working_to_source"]))
            crop["correction_clockwise"] = transform["correction_clockwise"]
    # 5. 补充表格上下文和续表分组，供后续 OCR/提取流程使用。
    contexts = build_table_contexts(processing_pdf, crops)
    by_id = {context["table_id"]: context for context in contexts}
    for crop in crops:
        if crop["id"] in by_id:
            crop["table_context"] = by_id[crop["id"]]
    save_json(out / "table_contexts.json", contexts)
    logical_tables = {}
    for context in contexts:
        logical_tables.setdefault(context["logical_table_id"], []).append(context["table_id"])
    save_json(out / "logical_tables.json", [{"logical_table_id": key, "table_ids": value}
                                            for key, value in logical_tables.items()])
    save_json(out / "table_crops.json", crops)
    manifest = {"schema_version": "sda-pdf2fig/v1", "document_id": name, "patent_id": name,
                "source_pdf": str(pdf), "source_pdf_sha256": digest,
                "processing_pdf": str(processing_pdf), "processing_pdf_sha256": _sha256(processing_pdf),
                "orientation": {"mode": orientation,
                    "status": "reused_preparation" if preparation and regions_json else "prepared" if preparation else "preserved_saved_geometry" if regions_json else "off",
                    "manifest": "page_orientation.json" if preparation else None,
                    "rotated_pages": preparation["rotated_pages"] if preparation else [],
                    "review_pages": preparation["review_pages"] if preparation else []},
                "source_regions": reused, "total_pages": total_pages,
                "selected_pages": [p["page"] for p in payload["pages"]],
                "page_numbering": "1-based PDF page numbers",
                "coordinate_space": "pdf_points", "bbox_format": "xyxy",
                "status": "partial" if payload.get("errors") or preparation and preparation["review_pages"] else "complete",
                "image_count": len(crops), "labels": labels, "images": crops,
                "crop_settings": {"dpi": crop_dpi, "padding_pt": padding_pt,
                                  "min_confidence": min_confidence},
                "regions_file": "regions.json", "crops_file": "table_crops.json",
                "table_contexts_file": "table_contexts.json", "logical_tables_file": "logical_tables.json",
                "errors": payload.get("errors", []),
                "wall_seconds": round(perf_counter() - started, 3),
                "scope": "Detected region crops; completeness of PDF tables/figures is not guaranteed."}
    save_json(out / f"{name}_manifest.json", manifest)
    return manifest


def _model_default(backend, variable):
    """优先读取环境变量，其次使用 models/layout 下已存在的本地权重。"""
    value = os.environ.get(variable)
    if value:
        return Path(value)
    local = ROOT / "models/layout" / backend
    return local if local.exists() else None


def build_crop_parser():
    """独立裁图和统一流程共享的版面参数。"""
    parser = argparse.ArgumentParser(description="校正 PDF 页面方向，定位表格/插图并高清裁图；当前执行步骤 2–3。")
    parser.add_argument("--input", "--pdf", required=True, type=Path, dest="pdf_path")
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--document-id")
    parser.add_argument("--regions-json", type=Path, help="复用已有区域 JSON，跳过模型推理")
    parser.add_argument("--backends", nargs="+", choices=BACKEND_NAMES, default=["pp_doclayoutv3"])
    parser.add_argument("--pp-model-dir", type=Path, default=_model_default("pp_doclayoutv3", "SDA_PP_LAYOUT_MODEL_DIR"))
    parser.add_argument("--yolo-model", type=Path, default=_model_default("doclayout_yolo", "SDA_YOLO_LAYOUT_MODEL"))
    parser.add_argument("--tatr-model-dir", type=Path, default=_model_default("tatr", "SDA_TATR_LAYOUT_MODEL_DIR"))
    parser.add_argument("--pages", nargs="+", help="页码从 1 开始，例如 9 10 或 9-10,13")
    parser.add_argument("--dpi", type=int, default=150, help="检测用整页图片的分辨率")
    parser.add_argument("--crop-dpi", type=int, default=400)
    parser.add_argument("--padding-pt", type=float, default=6.0)
    parser.add_argument("--labels", nargs="+", choices=["table", "figure"], default=["table"])
    parser.add_argument("--min-confidence", type=float, default=0.8)
    parser.add_argument("--merge-iou", type=float, default=0.5)
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--pp-engine")
    parser.add_argument("--orientation", choices=["auto", "off"], default="auto",
                        help="检测前自动校正页面方向；复用区域时保留已有坐标")
    parser.add_argument("--execution", choices=["serial", "parallel"], default="serial")
    parser.add_argument("--cpu-workers", type=int, default=2)
    parser.add_argument("--max-workers", type=int, default=4)
    parser.add_argument("--threads-per-worker", type=int, default=4)
    parser.add_argument("--torch-python", help="可选：YOLO/TATR 使用的独立 Python 解释器")
    parser.add_argument("--torch-device")
    parser.add_argument("--pp-device")
    parser.add_argument("--allow-missing-backends", action="store_true")
    for backend, default in [("pp", 0.2), ("yolo", 0.2), ("tatr", 0.7)]:
        parser.add_argument(f"--{backend}-threshold", type=float, default=default)
    return parser


def _crop_arguments(values):
    args = dict(values)
    args["model_dirs"] = {backend: args.pop(option) for backend, option in [
        ("pp_doclayoutv3", "pp_model_dir"), ("doclayout_yolo", "yolo_model"), ("tatr", "tatr_model_dir")]}
    args["model_dirs"] = {k: v for k, v in args["model_dirs"].items() if v is not None}
    args["thresholds"] = {backend: args.pop(option) for backend, option in [
        ("pp_doclayoutv3", "pp_threshold"), ("doclayout_yolo", "yolo_threshold"), ("tatr", "tatr_threshold")]}
    args["pages"] = parse_pages(args["pages"])
    return args


def layout_crop_main(argv=None):
    """旧的裁图命令；仅执行方向校正、版面分析和裁图。"""
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in {"doctor", "locate", "_layout-worker"}:
        return layout_main(argv)
    parser = build_crop_parser()
    try:
        args = _crop_arguments(vars(parser.parse_args(argv)))
        result = extract_pdf(**args)
    except (OSError, ValueError, RuntimeError) as exc:
        parser.exit(1, f"PDF 裁图失败：{exc}\n")
    print(f"{result['document_id']}：共 {result['image_count']} 张裁图，状态={result['status']}，"
          f"耗时 {result['wall_seconds']:.3f} 秒；保存至 {args['output_dir'].resolve()}")
    return int(result["status"] != "complete")


# 4. 内容识别：整页 OCR 与表格 OCR
# 4.1 整页 VL 的模型配置和结果转换
LAYOUT_MODEL = "PP-DocLayoutV3"
VL_MODEL = "PaddleOCR-VL-1.6-0.9B"


def model_paths(config, root):
    layout = Path(config.layout_model_dir or os.environ.get("SDA_LAYOUT_MODEL_DIR") or root / LAYOUT_MODEL).expanduser().resolve()
    vl = config.vl_model_dir or os.environ.get("SDA_VL_MODEL_DIR") or None
    if vl is None:
        vl = next((p for p in (root / VL_MODEL, root / "PaddleOCR-VL-1.6") if p.is_dir()), root / VL_MODEL)
    vl = Path(vl).expanduser().resolve()
    for directory, filenames, option in (
        (layout, ("inference.json", "inference.pdiparams", "inference.yml"), "--layout-model-dir"),
        (vl, ("model.safetensors", "config.json", "inference.yml", "tokenizer_config.json"), "--vl-model-dir"),
    ):
        for filename in filenames:
            if not (directory / filename).is_file():
                raise ValueError(f"缺少本地 VL 模型文件：{directory / filename}；请使用 {option} 指定目录")
    return layout, vl


def create_engine(config, device, root):
    from paddleocr import PaddleOCRVL

    layout, vl = model_paths(config, root)
    return PaddleOCRVL(
        pipeline_version="v1.6", device=device,
        layout_detection_model_dir=str(layout), vl_rec_model_dir=str(vl),
        use_layout_detection=True, use_doc_orientation_classify=False,
        use_doc_unwarping=False, use_chart_recognition=False,
        use_queues=False, format_block_content=False,
        enable_mkldnn=False, cpu_threads=config.cpu_threads,
    )


def normalise_result(raw, number, sx, sy):
    """Keep block order/content and map image coordinates to visible PDF points.

    Compatibility lines are blocks, not reconstructed text lines. VL supplies no
    comparable character confidence; null must not be replaced with a made-up score.
    """
    blocks, lines = [], []
    for index, item in enumerate(raw["res"]["parsing_res_list"]):
        content, label, box = item["block_content"], item["block_label"], item["block_bbox"]
        if not isinstance(content, str) or not isinstance(label, str):
            raise ValueError("VL 区块缺少有效文字或类别")
        if len(box) != 4 or not all(math.isfinite(float(v)) for v in box):
            raise ValueError("VL 区块坐标无效")
        box = [float(v) * (sx if i % 2 == 0 else sy) for i, v in enumerate(box)]
        if box[0] > box[2] or box[1] > box[3]:
            raise ValueError("VL 区块坐标顺序无效")
        polygon = [[box[0], box[1]], [box[2], box[1]], [box[2], box[3]], [box[0], box[3]]]
        identity = f"p{number:04d}-b{index + 1:04d}"
        block = {**item, "model_block_id": item.get("block_id"), "block_id": identity,
                 "bbox_image": item["block_bbox"], "block_bbox": box,
                 "bbox": box, "polygon": polygon, "coordinate_space": "pdf_points"}
        if "block_polygon_points" in item:
            block["polygon_image"] = item["block_polygon_points"]
            points = [[float(x) * sx, float(y) * sy] for x, y in item["block_polygon_points"]]
            if not all(math.isfinite(v) for point in points for v in point):
                raise ValueError("VL 区块多边形无效")
            block["block_polygon_points"] = points
            block["polygon"] = points
        blocks.append(block)
        if content.strip():
            lines.append({"line_id": identity, "block_id": identity,
                          "block_label": label, "block_order": item.get("block_order"),
                          "ocr_order": index, "text": content, "confidence": None,
                          "bbox": box, "polygon": block["polygon"], "granularity": "block"})
    return {"blocks": blocks, "lines": lines, "line_granularity": "block"}


def plain_text(content):
    """Readable text only; the evidence lines retain the original HTML unchanged."""
    if not re.search(r"<table\b", content, re.I):
        return content
    from lxml import html

    text = re.sub(r"</(?:td|th)\s*>", " | ", content, flags=re.I)
    text = re.sub(r"</tr\s*>", "\n", text, flags=re.I)
    return html.fromstring("<div>" + text + "</div>").text_content().strip()


def rebase_markdown(markdown, number):
    prefix = f"markdown/page-{number:04d}/imgs/"
    return (markdown.replace('src="imgs/', f'src="{prefix}')
            .replace("src='imgs/", f"src='{prefix}").replace("](imgs/", f"]({prefix}"))


# 4.2 整页 OCR、并行识别、缓存与逐页产物
OCR_VERSION = 2
DETECTOR = "PP-OCRv6_medium_det"
RECOGNIZER = "PP-OCRv6_medium_rec"
MODEL_FILES = ("inference.json", "inference.pdiparams", "inference.yml")


@dataclass(frozen=True)
class OCRConfig:
    device: str = "auto"
    max_side: int = 2600
    batch_size: int = 6
    cpu_threads: int = 4
    low_confidence: float = 0.8
    model_root: str | None = None
    render_prefetch: int = 0
    workers: int = 1
    backend: str = "vl"
    layout_model_dir: str | None = None
    vl_model_dir: str | None = None
    max_new_tokens: int = 4096

    def __post_init__(self):
        if self.backend not in {"vl", "paddle"}:
            raise ValueError("backend 必须为 vl 或 paddle")
        if self.device not in {"auto", "cpu"} and not re.fullmatch(r"gpu:\d+", self.device):
            raise ValueError("device 必须为 auto、cpu 或 gpu:编号")
        for name in ("max_side", "batch_size", "cpu_threads", "workers", "max_new_tokens"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} 必须为正整数")
        if isinstance(self.render_prefetch, bool) or not isinstance(self.render_prefetch, int) or self.render_prefetch < 0:
            raise ValueError("render_prefetch 必须为非负整数")
        if not math.isfinite(self.low_confidence) or not 0 <= self.low_confidence <= 1:
            raise ValueError("low_confidence 必须在 0 到 1 之间")


def _now():
    return datetime.now(timezone.utc).isoformat()


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     allow_nan=False).encode("utf-8")).hexdigest()


def _file_hash(path):
    return file_digest(Path(path))


def _write_text(path, text):
    """同目录临时文件 + replace；中断不损坏上一份完整产物。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}.", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(text)
        temporary.replace(path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def _write_json(path, value):
    _write_text(path, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def parse_ocr_pages(values, total_pages):
    """支持 1 3、1,3、1-3,7；省略时处理全篇，页码从 1 开始。"""
    if values is None:
        return list(range(1, total_pages + 1))
    if isinstance(values, (str, int)):
        values = [values]
    selected = set()
    for value in values:
        for part in str(value).split(","):
            part = part.strip()
            if re.fullmatch(r"[1-9]\d*", part):
                first = last = int(part)
            elif re.fullmatch(r"[1-9]\d*-[1-9]\d*", part):
                first, last = map(int, part.split("-"))
            else:
                raise ValueError(f"无效页码：{part!r}")
            if last < first or last > total_pages:
                raise ValueError(f"页码范围越界：{part}，PDF 共 {total_pages} 页")
            selected.update(range(first, last + 1))
    if not selected:
        raise ValueError("至少选择一页")
    return sorted(selected)


def _runtime_versions():
    versions = {}
    for name in ("paddlepaddle", "paddlepaddle-gpu", "paddleocr", "paddlex", "pymupdf"):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            pass
    return versions


def _resolve_device(device):
    if device != "auto":
        return device
    import paddle
    return "gpu:0" if paddle.is_compiled_with_cuda() and paddle.device.cuda.device_count() else "cpu"


def _model_signature(config):
    """使用明确的本地权重，不自动下载；权重改变时缓存失效。"""
    root = Path(config.model_root or os.environ.get("SDA_OCR_MODEL_ROOT")
                or Path.home() / ".paddlex/official_models").expanduser().resolve()
    if config.backend == "vl":


        layout, vl = model_paths(config, root)
        return root, {name: {p.name: _file_hash(p) for p in sorted(directory.iterdir()) if p.is_file()}
                      for name, directory in ((LAYOUT_MODEL, layout), (VL_MODEL, vl))}
    signatures = {}
    for name in (DETECTOR, RECOGNIZER):
        files = {}
        for filename in MODEL_FILES:
            path = root / name / filename
            if not path.is_file():
                raise ValueError(f"缺少本地 OCR 模型文件：{path}；请使用 --ocr-model-root 指定模型根目录")
            files[filename] = _file_hash(path)
        signatures[name] = files
    return root, signatures


def _create_page_ocr(config, device, model_root):
    os.environ.setdefault("PADDLE_PDX_ENABLE_MKLDNN_BYDEFAULT", "false")
    os.environ.setdefault("FLAGS_use_mkldnn", "0")
    os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "true")
    import paddle

    if device.startswith("gpu:"):
        index = int(device.split(":")[1])
        if not paddle.is_compiled_with_cuda() or index >= paddle.device.cuda.device_count():
            raise RuntimeError(f"当前 Paddle 环境无法使用 {device}；可指定 --device cpu 或使用 GPU 环境")
    if config.backend == "vl":


        return create_engine(config, device, model_root)
    from paddleocr import PaddleOCR

    return PaddleOCR(
        text_detection_model_name=DETECTOR, text_detection_model_dir=str(model_root / DETECTOR),
        text_recognition_model_name=RECOGNIZER, text_recognition_model_dir=str(model_root / RECOGNIZER),
        device=device, enable_mkldnn=False, cpu_threads=config.cpu_threads,
        use_doc_orientation_classify=False, use_doc_unwarping=False, use_textline_orientation=False,
        text_det_limit_side_len=config.max_side, text_det_limit_type="max",
        text_recognition_batch_size=config.batch_size,
        # 保留低置信度文本供后续核对，不在 OCR 阶段静默过滤。
        text_rec_score_thresh=0.0,
    )


def _normalise_result(result, number, sx, sy):
    texts, scores = result["rec_texts"], result["rec_scores"]
    polygons = result.get("rec_polys")
    if polygons is None:
        boxes = result.get("rec_boxes")
        if boxes is None:
            if len(texts):
                raise ValueError("OCR 有文字但没有位置坐标")
            boxes = []
        polygons = [[[b[0], b[1]], [b[2], b[1]], [b[2], b[3]], [b[0], b[3]]] for b in boxes]
    if not len(texts) == len(scores) == len(polygons):
        raise ValueError("OCR 文字、置信度和坐标数量不一致")
    lines = []
    for index, (text, score, polygon) in enumerate(zip(texts, scores, polygons)):
        score = float(score)
        if not isinstance(text, str) or not math.isfinite(score) or not 0 <= score <= 1:
            raise ValueError("OCR 文字或置信度无效")
        if len(polygon) < 3 or any(len(point) != 2 for point in polygon):
            raise ValueError("OCR 多边形无效")
        poly = [[float(x) * sx, float(y) * sy] for x, y in polygon]
        if not all(math.isfinite(v) for point in poly for v in point):
            raise ValueError("OCR 坐标包含非有限数")
        xs, ys = [p[0] for p in poly], [p[1] for p in poly]
        lines.append({"line_id": f"p{number:04d}-l{index + 1:04d}", "ocr_order": index,
                      "text": text, "confidence": score, "polygon": poly,
                      "bbox": [min(xs), min(ys), max(xs), max(ys)]})
    return lines


def _predict_page(engine, image, number, metadata, config):
    """Fully consume inference, then commit artifacts before the page cache."""
    results = list(engine.predict(str(image), **({"max_new_tokens": config.max_new_tokens}
                                                if config.backend == "vl" else {})))
    if len(results) != 1:
        raise ValueError(f"单页 OCR 应返回一个结果，实际为 {len(results)} 个")
    sx, sy = metadata["pixel_to_page_scale"]
    if config.backend == "paddle":
        return {"lines": _normalise_result(results[0], number, sx, sy)}


    result = results[0]
    raw = result.json
    parsed = normalise_result(raw, number, sx, sy)
    output = Path(image).parent.parent
    raw_path = output / "raw" / f"page-{number:04d}.json"
    md_dir = output / "markdown" / f"page-{number:04d}"
    md_dir.mkdir(parents=True, exist_ok=True)
    _write_json(raw_path, raw)
    result.save_to_markdown(str(md_dir))
    artifacts = [raw_path, *sorted(p for p in md_dir.rglob("*") if p.is_file())]
    parsed.update(markdown=rebase_markdown(result.markdown["markdown_texts"], number),
                  raw_result=str(raw_path.relative_to(output)),
                  artifacts={str(p.relative_to(output)): _file_hash(p) for p in artifacts})
    return parsed


def _render(page, image, max_side):
    import pymupdf
    scale = max_side / max(page.rect.width, page.rect.height)
    pixmap = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), colorspace=pymupdf.csRGB, alpha=False)
    image.parent.mkdir(parents=True, exist_ok=True)
    pixmap.save(image)
    return {"image_width": pixmap.width, "image_height": pixmap.height,
            "pixel_to_page_scale": [page.rect.width / pixmap.width, page.rect.height / pixmap.height],
            "image_sha256": _file_hash(image)}


def _render_pdf_page(pdf_path, number, image, max_side):
    """在独立 CPU 进程打开 PDF；不跨进程共享 PyMuPDF/Paddle 对象。"""
    import pymupdf
    tick = perf_counter()
    with pymupdf.open(pdf_path) as pdf:
        metadata = _render(pdf[number - 1], Path(image), max_side)
    return metadata, perf_counter() - tick


_WORKER_ENGINE = None
_WORKER_INIT_ERROR = None


def _ocr_page_job(pdf_path, number, image, config, device, model_root, rendered=None):
    """每个 spawn 进程只加载一次模型，所有进程显式使用同一个 device。"""
    global _WORKER_ENGINE, _WORKER_INIT_ERROR
    result = {"lines": [], "timings": {}, "status": "failed"}
    stage = "model_init"
    try:
        if _WORKER_INIT_ERROR:
            raise RuntimeError(_WORKER_INIT_ERROR)
        if _WORKER_ENGINE is None:
            tick = perf_counter()
            try:
                _WORKER_ENGINE = _create_page_ocr(config, device, Path(model_root))
            except Exception as exc:
                _WORKER_INIT_ERROR = f"{type(exc).__name__}: {exc}"
                raise
            finally:
                result["timings"]["model_init_seconds"] = perf_counter() - tick
        stage = "render"
        metadata, elapsed = rendered if rendered is not None else _render_pdf_page(pdf_path, number, image, config.max_side)
        result.update(metadata)
        result["timings"]["render_seconds"] = elapsed
        stage = "ocr"
        tick = perf_counter()
        try:
            result.update(_predict_page(_WORKER_ENGINE, image, number, metadata, config))
        finally:
            result["timings"]["ocr_seconds"] = perf_counter() - tick
        result["status"] = "success"
    except Exception as exc:
        result.update(error_stage=stage, error=f"{type(exc).__name__}: {exc}")
    return result


@contextmanager
def _parallel_ocr_pages(pdf_path, numbers, output_dir, config, device, model_root):
    """限制在途任务为进程数的两倍，主进程负责页缓存及最终导出。"""
    if config.workers == 1 or not numbers:
        yield None
        return
    workers = min(config.workers, len(numbers))
    executor = ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context("spawn"))
    remaining, futures = iter(numbers), {}
    render_context = _prefetched_pages(pdf_path, numbers, output_dir, config)
    render = None

    def submit_next():
        number = next(remaining, None)
        if number is not None:
            image = output_dir / "images" / f"page-{number:04d}.png"
            rendered = None
            if render is not None:
                try:
                    rendered = render(number)
                except Exception:
                    # 保留逐页失败语义；由 OCR worker 重试渲染并返回该页错误。
                    rendered = None
            futures[number] = executor.submit(_ocr_page_job, str(pdf_path), number, str(image), config, device, str(model_root), rendered)

    def take(number):
        future = futures.pop(number)
        try:
            return future.result()
        finally:
            submit_next()

    try:
        with render_context as render:
            for _ in range(min(2 * workers, len(numbers))):
                submit_next()
            yield take
    finally:
        for future in futures.values():
            future.cancel()
        executor.shutdown(wait=True, cancel_futures=True)


@contextmanager
def _prefetched_pages(pdf_path, numbers, output_dir, config):
    """最多预取指定页数；spawn 避免 fork 已初始化的 CUDA，退出前回收写图进程。"""
    if not config.render_prefetch or not numbers:
        yield None
        return
    executor = ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("spawn"))
    remaining, futures = iter(numbers), {}

    def submit_next():
        number = next(remaining, None)
        if number is not None:
            image = output_dir / "images" / f"page-{number:04d}.png"
            futures[number] = executor.submit(_render_pdf_page, str(pdf_path), number, str(image), config.max_side)

    def take(number):
        future = futures.pop(number)
        try:
            return future.result()
        finally:
            submit_next()

    try:
        for _ in range(min(config.render_prefetch, len(numbers))):
            submit_next()
        yield take
    finally:
        for future in futures.values():
            future.cancel()
        executor.shutdown(wait=True, cancel_futures=True)


def _cached_page(path, key, document_id, number):
    try:
        page = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(page, dict):
            return None
        content_hash = page.pop("result_sha256")
        if (page["status"] != "success" or page["cache_key"] != key
                or page["document_id"] != document_id or page["page_number"] != number
                or content_hash != _digest(page)):
            return None
        root = path.parent.parent.resolve()
        artifacts = dict(page.get("artifacts", {}))
        if page.get("image") and page.get("image_sha256"):
            artifacts[page["image"]] = page["image_sha256"]
        for relative, expected in artifacts.items():
            artifact = (root / relative).resolve()
            if not artifact.is_relative_to(root) or not artifact.is_file() or _file_hash(artifact) != expected:
                return None
        page["result_sha256"] = content_hash
        return page
    except (OSError, ValueError, KeyError, TypeError):
        return None


@contextmanager
def _output_lock(output_dir):
    output_dir.mkdir(parents=True, exist_ok=True)
    with (output_dir / ".ocr.lock").open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError(f"另一个 OCR 任务正在写入此目录：{output_dir}") from exc
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def _export(output_dir, manifest, records, timings):
    success = [p for p in records if p["status"] == "success"]
    failed = [p["page_number"] for p in records if p["status"] == "failed"]
    pending = [p["page_number"] for p in records if p["status"] == "pending"]
    status = "complete" if len(success) == len(records) else "partial" if success else "failed"
    if manifest.get("status") == "interrupted":
        status = "interrupted"
    summary = {
        "document_id": manifest["document_id"], "status": status,
        "total_pages": manifest["total_pages"], "selected_page_count": len(records),
        "successful_pages": len(success), "failed_pages": failed, "pending_pages": pending,
        "cached_pages": sum(p.get("cache_hit", False) for p in success),
        "newly_recognized_pages": sum(not p.get("cache_hit", False) for p in success),
        "unselected_pages": manifest["unselected_pages"],
        "full_document_ocr_complete": len(success) == manifest["total_pages"],
        "review_pages": [p["page_number"] for p in success if p.get("quality", {}).get("needs_review")],
        "lines": sum(len(p["lines"]) for p in success),
        "blocks": sum(len(p.get("blocks", [])) for p in success),
        "tables": sum(b.get("block_label") == "table" for p in success for b in p.get("blocks", [])),
        "backend": manifest["settings"]["backend"],
        "timings": timings,
        "timing_note": "wall_seconds is elapsed time; render/OCR/model-init seconds sum worker durations and may overlap.",
        "saved_page_ocr_seconds": sum(p.get("timings", {}).get("ocr_seconds", 0) for p in success),
        "llm_calls": 0, "llm_tokens": 0,
        "llm_usage_scope": "External model services only; local VL generation tokens are not measured.",
        "scope": "OCR execution/coverage only; successful OCR is not a transcription accuracy guarantee.",
    }
    _write_text(output_dir / "pages.jsonl", "".join(json.dumps(p, ensure_ascii=False, allow_nan=False) + "\n" for p in records))
    plain, markdown = [], []
    for record in records:
        number = record["page_number"]
        text = "\n".join(line["text"] for line in record.get("lines", []))
        markdown_text = record.get("markdown", text)
        if record.get("blocks") is not None:


            text = "\n\n".join(plain_text(b["block_content"]) for b in record["blocks"] if b["block_content"].strip())
        if record["status"] != "success":
            text = f"[OCR {record['status']}: {record.get('error', 'not processed')}]"
            markdown_text = text
        plain.append(f"[PDF page {number}]\n{text}\n")
        markdown.append(f"## PDF page {number}\n\n{markdown_text}\n")
    _write_text(output_dir / "document.txt", "\n".join(plain))
    _write_text(output_dir / "document.md", "\n".join(markdown))
    _write_json(output_dir / "summary.json", summary)
    manifest.update(status=status, finished_at_utc=_now(), summary_file="summary.json")
    _write_json(output_dir / "document.json", manifest)
    return summary


def ocr_pdf(pdf_path, output_dir, *, document_id=None, pages=None, config=None, force=False):
    """逐页 OCR；默认复用成功缓存，失败/损坏/过期页重试，force 强制重识别。

    只读取 PDF 图像，VL 默认恢复版面/表格；不做自动方向校正或跨页表格拼接。
    所选页存在失败时返回 partial/failed；CLI 相应退出码为 1。
    """
    import pymupdf

    started = perf_counter()
    config = config or OCRConfig()
    pdf_path, output_dir = Path(pdf_path).expanduser().resolve(), Path(output_dir).expanduser().resolve()
    if not pdf_path.is_file():
        raise ValueError(f"PDF 不存在：{pdf_path}")
    document_id = document_id or pdf_path.stem
    if (not isinstance(document_id, str) or not document_id.strip() or document_id in {".", ".."}
            or re.search(r"[/\\\x00-\x1f]", document_id)):
        raise ValueError("document_id 不能为空，且不能包含路径分隔符或控制字符")
    source_hash = _file_hash(pdf_path)
    model_root, model_hashes = _model_signature(config)
    device = _resolve_device(config.device)
    versions = _runtime_versions()
    settings = {**asdict(config), "device": device, "model_root": str(model_root),
                "document_orientation": False, "textline_orientation": False,
                "unwarping": False, "pdf_text_layer_used": False}
    if config.backend == "vl":


        layout, vl = model_paths(config, model_root)
        settings.update(layout_model=LAYOUT_MODEL, vl_model=VL_MODEL, pipeline_version="v1.6",
                        layout_model_dir=str(layout), vl_model_dir=str(vl),
                        use_layout_detection=True, use_chart_recognition=False,
                        use_queues=False, format_block_content=False)
    else:
        settings.update(text_detector=DETECTOR, text_recognizer=RECOGNIZER, text_rec_score_thresh=0.0)
    key = _digest({"version": OCR_VERSION, "pdf_sha256": source_hash,
                   "document_id": document_id, "settings": {k: v for k, v in settings.items()
                                                               if k not in {"model_root", "layout_model_dir", "vl_model_dir", "render_prefetch", "workers"}},
                   "models": model_hashes, "runtime": versions})
    with pymupdf.open(pdf_path) as pdf:
        if pdf.needs_pass or not len(pdf):
            raise ValueError("PDF 需要密码或没有页面")
        selected = parse_ocr_pages(pages, len(pdf))
        with _output_lock(output_dir):
            marker = output_dir / "document.json"
            if marker.exists():
                try:
                    previous = json.loads(marker.read_text(encoding="utf-8"))
                except (OSError, ValueError) as exc:
                    raise ValueError("输出目录的 document.json 损坏，请使用新输出目录") from exc
                if previous.get("producer") != "SDA/paten_ocr":
                    raise ValueError("输出目录不是本工具创建的，请使用新输出目录")
                if previous.get("source_pdf_sha256") != source_hash or previous.get("document_id") != document_id:
                    raise ValueError("输出目录属于另一份 PDF 或 document_id，请使用新输出目录")
            elif any(p.name != ".ocr.lock" for p in output_dir.iterdir()):
                raise ValueError("输出目录非空且没有本工具的 document.json，请使用新输出目录")
            manifest = {"schema_version": OCR_VERSION, "producer": "SDA/paten_ocr", "document_id": document_id,
                        "source_pdf": str(pdf_path), "source_pdf_sha256": source_hash,
                        "total_pages": len(pdf), "selected_pages": selected,
                        "unselected_pages": sorted(set(range(1, len(pdf) + 1)) - set(selected)),
                        "status": "running", "started_at_utc": _now(), "cache_key": key,
                        "settings": settings, "runtime": versions, "model_files": model_hashes,
                        "coordinate_space": "pdf_points", "coordinate_frame": "rendered_page",
                        "coordinate_note": "Top-left origin of page.rect after existing PDF rotation; no automatic rotation or unwarping.",
                        "source": "local_vl" if config.backend == "vl" else "local_ocr", "llm_calls": 0, "llm_tokens": 0,
                        "llm_usage_scope": "External model services only; local VL generation tokens are not measured."}
            _write_json(marker, manifest)
            records = {}
            for number in selected:
                cached = None if force or not cache_enabled() else _cached_page(output_dir / "pages" / f"page-{number:04d}.json", key, document_id, number)
                if cached:
                    records[number] = {**cached, "cache_hit": True}
            engine, initialization_error = None, None
            timings = {"preparation_seconds": perf_counter() - started, "model_init_seconds": 0.0,
                       "render_seconds": 0.0, "render_wait_seconds": 0.0, "worker_wait_seconds": 0.0, "ocr_seconds": 0.0}
            try:
                pending_numbers = [n for n in selected if n not in records]
                with _parallel_ocr_pages(pdf_path, pending_numbers, output_dir, config, device, model_root) as parallel, \
                        _prefetched_pages(pdf_path, pending_numbers if parallel is None else [], output_dir, config) as prefetched:
                    for index, number in enumerate(selected, 1):
                        if number in records:
                            print(f"ocr {index}/{len(selected)} page={number} status=cached lines={len(records[number]['lines'])}", flush=True)
                            continue
                        page = pdf[number - 1]
                        record = {"document_id": document_id, "pdf_sha256": source_hash, "cache_key": key,
                                  "page_number": number, "status": "failed", "source": manifest["source"],
                                  "backend": config.backend,
                                  "coordinate_space": "pdf_points", "coordinate_frame": "rendered_page",
                                  "width": page.rect.width, "height": page.rect.height, "rotation": page.rotation,
                                  "page_derotation_matrix": list(page.derotation_matrix),
                                  "lines": [], "timings": {}, "recognized_at_utc": _now()}
                        stage = "model_init"
                        try:
                            if parallel is not None:
                                stage = "worker"
                                tick = perf_counter()
                                record.update(parallel(number))
                                timings["worker_wait_seconds"] += perf_counter() - tick
                                for metric in ("model_init_seconds", "render_seconds", "ocr_seconds"):
                                    timings[metric] += record["timings"].get(metric, 0)
                                if record["status"] != "success":
                                    stage = record["error_stage"]
                                    raise RuntimeError(record["error"])
                                record["image"] = f"images/page-{number:04d}.png"
                            else:
                                if initialization_error:
                                    raise RuntimeError(initialization_error)
                                if engine is None:
                                    tick = perf_counter()
                                    try:
                                        engine = _create_page_ocr(config, device, model_root)
                                    except Exception as exc:
                                        initialization_error = f"{type(exc).__name__}: {exc}"
                                        raise
                                    finally:
                                        timings["model_init_seconds"] += perf_counter() - tick
                                stage = "render"
                                render_elapsed = None
                                image = output_dir / "images" / f"page-{number:04d}.png"
                                tick = perf_counter()
                                try:
                                    if prefetched is None:
                                        record.update(_render(page, image, config.max_side))
                                        render_elapsed = perf_counter() - tick
                                    else:
                                        metadata, render_elapsed = prefetched(number)
                                        record.update(metadata)
                                    record["image"] = str(image.relative_to(output_dir))
                                finally:
                                    elapsed = perf_counter() - tick
                                    record["timings"]["render_seconds"] = render_elapsed if render_elapsed is not None else elapsed
                                    record["timings"]["render_wait_seconds"] = elapsed
                                    timings["render_seconds"] += record["timings"]["render_seconds"]
                                    timings["render_wait_seconds"] += elapsed
                                stage = "ocr"
                                tick = perf_counter()
                                try:
                                    record.update(_predict_page(engine, image, number, record, config))
                                finally:
                                    elapsed = perf_counter() - tick
                                    record["timings"]["ocr_seconds"] = elapsed
                                    timings["ocr_seconds"] += elapsed
                            scores = [line["confidence"] for line in record["lines"] if line.get("confidence") is not None]
                            low = [line["line_id"] for line in record["lines"] if line.get("confidence") is not None
                                   and line["confidence"] < config.low_confidence]
                            record["quality"] = {"empty": not record["lines"], "low_confidence_line_ids": low,
                                                 "confidence_available": bool(scores),
                                                 "mean_confidence": sum(scores) / len(scores) if scores else None,
                                                 "needs_review": not record["lines"] or bool(low)}
                            record["status"] = "success"
                        except Exception as exc:
                            record.update(error_stage=stage, error=f"{type(exc).__name__}: {exc}")
                        record["result_sha256"] = _digest(record)
                        _write_json(output_dir / "pages" / f"page-{number:04d}.json", record)
                        records[number] = {**record, "cache_hit": False}
                        print(f"ocr {index}/{len(selected)} page={number} status={record['status']} "
                              f"lines={len(record['lines'])} seconds={record['timings'].get('ocr_seconds', 0):.3f}", flush=True)
            except BaseException:
                manifest["status"] = "interrupted"
                raise
            finally:
                timings["wall_seconds"] = perf_counter() - started
                exported = [records.get(n, {"document_id": document_id, "page_number": n, "status": "pending", "lines": []})
                            for n in selected]
                summary = _export(output_dir, manifest, exported, timings)
    return summary


def page_ocr_main(argv=None):
    parser = argparse.ArgumentParser(description="PDF 整页 OCR：本地 VL 结构化解析或普通文字识别", formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("pdf", nargs="?", type=Path, help="输入 PDF；也可使用 --input")
    parser.add_argument("--input", type=Path, dest="input_pdf")
    parser.add_argument("--output-dir", type=Path, help="默认 results/pdf_ocr/<document-id>")
    parser.add_argument("--document-id", help="默认使用 PDF 文件名，不含扩展名")
    parser.add_argument("--pages", nargs="+", help="例如 1-3 7,9；省略时识别全篇")
    parser.add_argument("--device", default="auto", help="auto、cpu、gpu:0 等；指定设备失败时不静默切换")
    parser.add_argument("--backend", choices=("vl", "paddle"), default="vl", help="vl：结构化文档（默认）；paddle：普通文字 OCR")
    parser.add_argument("--layout-model-dir", type=Path, help="VL 使用的本地 PP-DocLayoutV3 权重")
    parser.add_argument("--vl-model-dir", type=Path, help="本地 PaddleOCR-VL-1.6 权重")
    parser.add_argument("--max-new-tokens", type=int, default=4096, help="VL 每个区块的生成 token 上限")
    parser.add_argument("--max-side", type=int, default=2600)
    parser.add_argument("--batch-size", type=int, default=6, help="paddle 后端的文字识别裁块批大小")
    parser.add_argument("--workers", type=int, default=1, help="同一设备上的 OCR 进程数，每个进程独立加载模型")
    parser.add_argument("--render-prefetch", type=int, default=0, help="独立 CPU 进程预渲染页数，0 为串行")
    parser.add_argument("--cpu-threads", type=int, default=4)
    parser.add_argument("--low-confidence", type=float, default=0.8, help="仅标记低置信度文字，不删除")
    parser.add_argument("--ocr-model-root", type=Path, help="本地模型根目录，默认 ~/.paddlex/official_models")
    parser.add_argument("--force", action="store_true", help="强制重识别所选页面")
    args = parser.parse_args(argv)
    if bool(args.pdf) == bool(args.input_pdf):
        parser.error("请通过位置参数或 --input 提供一个 PDF")
    pdf = args.pdf or args.input_pdf
    identity = args.document_id or pdf.stem
    output = args.output_dir or Path(__file__).resolve().parents[2] / "results/pdf_ocr" / identity
    try:
        config = OCRConfig(device=args.device, max_side=args.max_side, batch_size=args.batch_size,
                           cpu_threads=args.cpu_threads, low_confidence=args.low_confidence,
                           model_root=str(args.ocr_model_root) if args.ocr_model_root else None, render_prefetch=args.render_prefetch, workers=args.workers,
                           backend=args.backend, layout_model_dir=str(args.layout_model_dir) if args.layout_model_dir else None,
                           vl_model_dir=str(args.vl_model_dir) if args.vl_model_dir else None, max_new_tokens=args.max_new_tokens)
        summary = ocr_pdf(pdf, output, document_id=identity, pages=args.pages, config=config, force=args.force)
    except KeyboardInterrupt:
        print("OCR 已中断，已完成页面保留；重新运行同一命令即可续跑。", flush=True)
        return 130
    except Exception as exc:
        print(f"PDF OCR 失败：{type(exc).__name__}: {exc}", flush=True)
        return 1
    print(f"PDF OCR {summary['status']}：成功 {summary['successful_pages']}/{summary['selected_page_count']} 页，"
          f"缓存 {summary['cached_pages']} 页，本次耗时 {summary['timings']['wall_seconds']:.3f} 秒；产物：{output.resolve()}", flush=True)
    return 0 if summary["status"] == "complete" else 1


# 4.3 表格裁图 OCR，保留原始 HTML 和单元格引用
# CPU 回退时关闭 oneDNN，避免 Paddle 推理兼容性问题。
os.environ.setdefault("PADDLE_PDX_ENABLE_MKLDNN_BYDEFAULT", "false")
os.environ.setdefault("FLAGS_use_mkldnn", "0")

# VL 固定使用指定设备；普通 OCR 保留 GPU 失败后回退 CPU 的行为。
_ocr_device = "gpu:0"
_cpu_threads = 12
_ocr_backend = "vl"
_ocr_batch_size = 1
_table_vl_model_dir = None
_sequence_model_root = None
OCR_BACKENDS = ("vl", "paddle")


def configure_ocr_worker(device="gpu:0", cpu_threads=4, backend="vl", batch_size=1, *, vl_model_dir=None, sequence_model_root=None):
    """在 spawn 创建的独立进程中设置参数，模型在第一次识别时初始化。"""
    global _ocr_device, _cpu_threads, _ocr_backend, _ocr_batch_size, _table_vl_model_dir, _sequence_model_root
    if backend not in OCR_BACKENDS:
        raise ValueError(f"未知 OCR backend：{backend}；可选 {OCR_BACKENDS}")
    if not isinstance(batch_size, int) or isinstance(batch_size, bool) or batch_size < 1:
        raise ValueError("表格 OCR 提交批大小必须为正整数")
    _ocr_device = device
    _cpu_threads = cpu_threads
    _ocr_backend = backend
    _ocr_batch_size = batch_size
    _table_vl_model_dir = str(Path(vl_model_dir).expanduser().resolve()) if vl_model_dir else None
    _sequence_model_root = sequence_model_root
    get_ocr.cache_clear()


def _create_table_ocr(device):
    """模型在每个工作进程内加载一次，表格裁图直接使用 table 提示。"""
    if _ocr_backend == "vl":
        print(f"正在初始化 PaddleOCR-VL-1.6：{device}，进程 {os.getpid()}", flush=True)
        try:
            from paddleocr import PaddleOCRVL
            from paddlex.inference import load_pipeline_config

            config = copy.deepcopy(load_pipeline_config("PaddleOCR-VL-1.6"))
            config["batch_size"] = _ocr_batch_size
            config["SubModules"]["VLRecognition"]["batch_size"] = _ocr_batch_size

            return PaddleOCRVL(
                pipeline_version="v1.6",
                paddlex_config=config,
                device=device,
                use_layout_detection=False,
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
                enable_mkldnn=False,
                cpu_threads=_cpu_threads,
                **({"vl_rec_model_dir": _table_vl_model_dir} if _table_vl_model_dir else {}),
            )
        except ImportError as exc:
            raise RuntimeError(
                'PaddleOCR-VL-1.6 缺少依赖，请在运行环境安装 '
                '"paddleocr[doc-parser]==3.7.0"。'
            ) from exc

    from paddleocr import PaddleOCR

    print(f"正在初始化 OCR：{device}，进程 {os.getpid()}", flush=True)
    return PaddleOCR(
        lang="ch",
        device=device,
        use_doc_orientation_classify=True,
        use_doc_unwarping=False,
        use_textline_orientation=True,
        enable_mkldnn=False,
        cpu_threads=_cpu_threads,
    )


@lru_cache(maxsize=1)
def get_ocr():
    """缓存所选模型；VL 失败直接上报，普通 OCR 可回退 CPU。"""
    global _ocr_device

    import paddle

    if _ocr_backend == "vl":
        if _ocr_device.startswith("gpu:"):
            device_index = int(_ocr_device.split(":", 1)[1])
            if device_index >= paddle.device.cuda.device_count():
                raise RuntimeError(f"PaddleOCR-VL-1.6 请求的设备 {_ocr_device} 不存在")
        return _create_table_ocr(_ocr_device)

    try:
        if _ocr_device.startswith("gpu:"):
            device_index = int(_ocr_device.split(":", 1)[1])
            if device_index >= paddle.device.cuda.device_count():
                raise RuntimeError(f"设备 {_ocr_device} 不存在")
            return _create_table_ocr(_ocr_device)
    except Exception as exc:
        print(f"GPU 不可用，改用 CPU：{exc}")

    _ocr_device = "cpu"
    return _create_table_ocr("cpu")


def extract_table_evidence(image_path, metadata=None):
    """Recognize once, retaining original blocks/boxes and source cell addresses."""
    global _ocr_device
    image_path = Path(image_path).resolve()
    if not image_path.is_file():
        raise FileNotFoundError(f"表格图片不存在：{image_path}")

    if _ocr_backend == "vl":
        return extract_table_evidence_batch([image_path], [metadata or {}])[0]

    ocr = get_ocr()

    # 2. 识别图片，得到文字以及对应的位置框 [左, 上, 右, 下]。
    try:
        result = ocr.predict(str(image_path))[0]
    except Exception as exc:
        if _ocr_device == "cpu":
            raise
        print(f"GPU OCR 推理失败，用 CPU 重试当前图片：{exc}")
        _ocr_device = "cpu"
        get_ocr.cache_clear()
        ocr = get_ocr()
        result = ocr.predict(str(image_path))[0]

    items = list(zip(result["rec_texts"], result["rec_boxes"]))
    items.sort(key=lambda item: (item[1][1] + item[1][3]) / 2)

    # 3. 按文字框中心分行，同一行从左往右排列。
    # 这是简单排版，暂不恢复空单元格、合并单元格或复杂表头。
    rows = []
    for text, box in items:
        center_y = (box[1] + box[3]) / 2
        height = max(box[3] - box[1], 1)
        if not rows or abs(center_y - rows[-1]["y"]) > height / 2:
            rows.append({"y": center_y, "cells": []})
        rows[-1]["cells"].append((box[0], text))

    text = "\n".join(
        " | ".join(text for _, text in sorted(row["cells"]))
        for row in rows
    )
    lines = [{"text": str(text), "bbox": [float(v) for v in box],
              "coordinate_space": "crop_pixels"} for text, box in items]
    return structure_table_evidence(text, image_path, metadata, lines=lines)


def ocr_runtime(ocr=None):
    """Report the live predictor's batch, including native backend clamping."""
    ocr = ocr if ocr is not None else get_ocr()
    pipeline = getattr(ocr, "paddlex_pipeline", None)
    predictor = getattr(pipeline, "vl_rec_model", None) if pipeline is not None else None
    effective = getattr(getattr(predictor, "batch_sampler", None), "batch_size", None)
    return {"backend": _ocr_backend, "device": _ocr_device,
            "configured_submission_batch_size": _ocr_batch_size,
            "effective_model_batch_size": effective, "worker_pid": os.getpid()}


def _table_ocr_event(phase, *, evidence=None, **details):
    evidence = evidence or {}
    print("table-ocr " + json.dumps({
        "timestamp": datetime.now(timezone.utc).isoformat(), "pid": os.getpid(),
        "phase": phase, "table_id": evidence.get("table_id"), "page": evidence.get("page"),
        **details,
    }, ensure_ascii=False), flush=True)


def extract_table_evidence_batch(image_paths, metadata_rows=None):
    """Submit a list in one pipeline call, associating results by input_path.

    Native PaddleOCR-VL may execute these images with model batch=1. The actual
    predictor batch is recorded instead of presenting submission size as GPU batch.
    No padding, truncation or precision/generation changes are made here.
    """
    paths = [Path(p).resolve() for p in image_paths]
    if not paths or len(set(paths)) != len(paths):
        raise ValueError("OCR batch requires nonempty, unique image paths")
    if any(not p.is_file() for p in paths):
        raise FileNotFoundError("OCR batch contains a missing image")
    metadata_rows = [{} for _ in paths] if metadata_rows is None else metadata_rows
    if len(metadata_rows) != len(paths):
        raise ValueError("OCR batch image/metadata counts differ")
    if _ocr_backend != "vl":
        return [extract_table_evidence(p, m) for p, m in zip(paths, metadata_rows)]
    started = perf_counter()
    _table_ocr_event("batch_start", table_ids=[m.get("id", p.stem) for p, m in zip(paths, metadata_rows)],
                     submission_size=len(paths), backend=_ocr_backend, device=_ocr_device)
    ocr = get_ocr()
    initialized = perf_counter()
    _table_ocr_event("vl_start", **ocr_runtime(ocr), initialization_seconds=round(initialized - started, 3))
    metadata = dict(zip(paths, metadata_rows))
    outputs = {}
    # Read all results before returning. Partial generator failures cannot be
    # mistaken for a completed batch or shift the following image's identity.
    for result in ocr.predict(input=[str(p) for p in paths], prompt_label="table"):
        raw = result.json["res"]
        identity = raw.get("input_path")
        if not isinstance(identity, str) or Path(identity).resolve() not in metadata:
            raise ValueError("OCR result has a missing/unknown input_path")
        path = Path(identity).resolve()
        if path in outputs:
            raise ValueError(f"Duplicate OCR output: {path.name}")
        blocks = raw["parsing_res_list"]
        contents = [b["block_content"] for b in blocks
                    if isinstance(b.get("block_content"), str) and b["block_content"].strip()]
        if not contents:
            raise ValueError(f"Empty OCR output: {path.name}")
        outputs[path] = structure_table_evidence("\n\n".join(contents), path, metadata[path], blocks=blocks)
        _table_ocr_event("vl_result", evidence=outputs[path], cells=len(outputs[path].get("cells", [])),
                         seconds=round(perf_counter() - initialized, 3))
    if len(outputs) != len(paths):
        raise ValueError("OCR batch returned fewer results than input images")
    # PPVL supplies physical structure; sequence characters are re-read from
    # complete raster line crops by the official PP-OCRv6 medium recognizer.
    from document.sequence_ocr import refine
    for path in paths:
        outputs[path] = refine(outputs[path], device=_ocr_device, threads=_cpu_threads, model_root=_sequence_model_root)
    runtime = {**ocr_runtime(ocr), "submission_size": len(paths),
               "initialization_seconds": initialized - started,
               "inference_wall_seconds": perf_counter() - initialized}
    for output in outputs.values():
        output["batch_runtime"] = runtime
    _table_ocr_event("batch_complete", table_ids=[outputs[p]["table_id"] for p in paths],
                     seconds=round(perf_counter() - started, 3))
    return [outputs[p] for p in paths]


def structure_table_evidence(text, image_path, metadata=None, *, blocks=None, lines=None):
    """Keep literal HTML and ground cells only in observed OCR rectangles."""

    metadata = metadata or {}
    path = Path(image_path).resolve()
    table_id = metadata.get("id") or path.stem
    grid = parse_grid(text, table_id)
    try:
        version = importlib.metadata.version("paddleocr")
    except importlib.metadata.PackageNotFoundError:
        version = None
    result = {"version": 1, "document_id": metadata.get("document_id"),
            "table_id": table_id, "page": metadata.get("page"),
            "bbox": metadata.get("bbox"), "coordinate_space": "pdf_points",
            "image_path": str(path), "image_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "backend": _ocr_backend, "backend_version": version, "device": _ocr_device,
            "text": text, "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "cells": [cell for row in grid["physical"] for cell in row] if grid else [],
            "grid": grid["rows"] if grid else [], "blocks": blocks or [], "lines": lines or []}
    _ground_table_cells(result, metadata)
    return result


def _ground_table_cells(evidence, metadata, *, page_lines=(), working_to_source=None):
    """Add derived geometry; never rewrite OCR text or source cell addresses."""
    metadata = {**{key: evidence[key] for key in
                  ("bbox", "bbox_padded", "crop_to_pdf", "width_px", "height_px") if key in evidence}, **metadata}
    extra = {c.get("ref"): {k: copy.deepcopy(c[k]) for k in ("sequence_ocr", "vl_text") if k in c}
             for c in evidence.get("cells", [])}
    grid = parse_grid(evidence.get("text", ""), evidence["table_id"])
    locate_grid(grid, evidence, metadata, page_lines=page_lines, working_to_source=working_to_source)
    if grid:
        for row in grid["physical"]:
            for cell in row:
                cell.update(extra.get(cell.get("ref"), {}))
    evidence.update({key: metadata[key] for key in
                     ("bbox_padded", "crop_to_pdf", "width_px", "height_px") if key in metadata})
    evidence["cells"] = [cell for row in grid["physical"] for cell in row] if grid else []
    evidence["grid"] = grid["rows"] if grid else []
    if page_lines and evidence.get("sequence_ocr"):
        from document.sequence_ocr import compare_page_text
        compare_page_text(evidence, metadata, page_lines)
    evidence["cell_locations"] = {
        "located": sum(cell.get("location_status") == "located" for cell in evidence["cells"]),
        "total": len(evidence["cells"]), "scope": "Observed OCR text/cell rectangles; unknown positions remain empty."}
    return grid


def extract_table_text(image_path):
    """Compatibility API: return the original HTML/plain text without rewriting."""
    return extract_table_evidence(image_path)["text"]




def table_ocr_main(argv=None):
    parser = argparse.ArgumentParser(description="表格裁图 OCR")
    parser.add_argument("image_path", type=Path, nargs="?",
                        default=Path(__file__).resolve().parents[2] / "data" / "table.png")
    parser.add_argument("--backend", choices=OCR_BACKENDS, default="vl")
    parser.add_argument("--device", default="gpu:0")
    parser.add_argument("--cpu-threads", type=int, default=4)
    args = parser.parse_args(argv)
    configure_ocr_worker(args.device, args.cpu_threads, args.backend)
    table_text = extract_table_text(args.image_path)
    print("\n识别出的表格文字：")
    print(table_text)
    return 0


def _ocr_branch_event(branch, status, **details):
    print("sda-ocr-progress " + json.dumps({"branch": branch, "status": status, **details},
                                          ensure_ascii=False), flush=True)


def _recognize_page_content(layout, output_dir, config):
    """全文 OCR 独立分支；不读取表格 OCR 的状态。"""
    started = perf_counter()
    _ocr_branch_event("text", "processing")
    try:
        directory = Path(output_dir) / "page_ocr"
        summary = ocr_pdf(layout["processing_pdf"], directory, document_id=layout["document_id"],
                          pages=layout["selected_pages"], config=config)
        pages = [json.loads(line) for line in (directory / "pages.jsonl").read_text().splitlines() if line.strip()]
        errors = [{"stage": "page_ocr", "page": p["page_number"],
                   "error": p.get("error", p["status"])} for p in pages if p["status"] != "success"]
        _ocr_branch_event("text", "completed" if summary["status"] == "complete" else summary["status"],
                          completed=summary["successful_pages"], total=len(pages))
        return {"page_summary": summary, "pages": pages, "errors": errors,
                "runtime": {"pid": os.getpid(), "started": started, "finished": perf_counter(),
                            "workers": config.workers}}
    except BaseException:
        _ocr_branch_event("text", "failed")
        raise


def _initialize_table_content_worker(device, threads, backend, batch_size, model_dir, sequence_model_root=None):
    configure_ocr_worker(device, threads, backend, batch_size, vl_model_dir=model_dir, sequence_model_root=sequence_model_root)


def _recognize_table_batch(batch):
    """每批独立返回结果或错误，保持图片和证据身份对应。"""
    try:
        results = extract_table_evidence_batch([m["image_path"] for m in batch], batch)
        if len(results) != len(batch) or any(row.get("table_id") != meta["id"] or
                Path(row.get("image_path", "")).resolve() != Path(meta["image_path"])
                for row, meta in zip(results, batch)):
            raise ValueError("Table OCR batch image/table identity mismatch")
        return [{**row, "status": "success"} for row in results]
    except Exception as exc:
        _table_ocr_event("batch_failed", table_ids=[m["id"] for m in batch],
                         error=f"{type(exc).__name__}: {exc}")
        return [{"table_id": m["id"], "page": m["page"], "status": "failed",
                 "error": f"{type(exc).__name__}: {exc}"} for m in batch]


def _recognize_table_content(layout, layout_dir, output_dir, config, backend, batch_size, enabled, workers):
    """裁图 OCR 独立分支；不等待正文，续表验证留到两路汇合之后。"""
    started = perf_counter()
    metadata = [{**crop, "image_path": str(Path(layout_dir) / crop["image"])}
                for crop in layout["images"] if crop["label"] == "table"]
    _ocr_branch_event("table", "processing", completed=0, total=len(metadata))
    batches = [metadata[start:start + batch_size] for start in range(0, len(metadata), batch_size)]
    errors, evidence = [], []
    try:
        with ExitStack() as resources:
            if enabled and metadata:
                model_dir = None
                if backend == "vl":
                    root = Path(config.model_root or os.environ.get("SDA_OCR_MODEL_ROOT")
                                or Path.home() / ".paddlex/official_models").expanduser().resolve()
                    model_dir = config.vl_model_dir or os.environ.get("SDA_VL_MODEL_DIR") or next(
                        (path for path in (root / VL_MODEL, root / "PaddleOCR-VL-1.6") if path.is_dir()), root / VL_MODEL)
                arguments = (_resolve_device(config.device), config.cpu_threads, backend, batch_size, model_dir, config.model_root)
                if workers > 1 and len(batches) > 1:
                    pool = resources.enter_context(ProcessPoolExecutor(max_workers=min(workers, len(batches)),
                            mp_context=multiprocessing.get_context("spawn"),
                            initializer=_initialize_table_content_worker, initargs=arguments))
                    results = pool.map(_recognize_table_batch, batches)
                else:
                    _initialize_table_content_worker(*arguments)
                    results = map(_recognize_table_batch, batches)
            else:
                results = [[{"table_id": m["id"], "page": m["page"], "status": "skipped"} for m in batch]
                           for batch in batches]
            for batch in results:
                for row in batch:
                    if row["status"] == "failed":
                        errors.append({"stage": "table_ocr", "table_id": row["table_id"],
                                       "page": row["page"], "error": row["error"]})
                    _write_json(Path(output_dir) / "table_ocr" / f"{row['table_id']}.json", row)
                    evidence.append(row)
                    _ocr_branch_event("table", "processing", completed=len(evidence), total=len(metadata),
                                      table_id=row["table_id"], page=row["page"], table_status=row["status"],
                                      failed=len(errors), seconds=round(perf_counter() - started, 3))
        _write_text(Path(output_dir) / "table_ocr" / "tables.jsonl",
                    "".join(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n" for row in evidence))
        _ocr_branch_event("table", "failed" if errors and len(errors) == len(evidence)
                          else "partial" if errors else "completed", completed=len(evidence), total=len(metadata))
        return {"tables": evidence, "errors": errors,
                "runtime": {"pid": os.getpid(), "started": started, "finished": perf_counter(), "workers": workers}}
    except BaseException:
        _ocr_branch_event("table", "failed")
        raise


def recognize_content(layout, layout_dir, output_dir, *, config=None,
                      table_backend="vl", table_batch_size=1, table_ocr=True,
                      parallel=True, table_workers=1):
    """裁图后并行识别正文与表格；独立 spawn 进程不共享模型或 CUDA 状态。"""
    config = config or OCRConfig()
    if table_backend not in OCR_BACKENDS:
        raise ValueError(f"未知表格 OCR backend：{table_backend}")
    for name, value in (("table_batch_size", table_batch_size), ("table_workers", table_workers)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{name} 必须为正整数")
    layout_dir, output_dir = Path(layout_dir).resolve(), Path(output_dir).resolve()
    if file_digest(Path(layout["processing_pdf"])) != layout["processing_pdf_sha256"]:
        raise ValueError("工作 PDF 已改变，不能混用旧检测框和新的 OCR")
    page_args = (layout, output_dir, config)
    table_args = (layout, layout_dir, output_dir, config, table_backend, table_batch_size, table_ocr, table_workers)
    actual_parallel = parallel and table_ocr and any(c["label"] == "table" for c in layout["images"])
    if actual_parallel:
        with ProcessPoolExecutor(max_workers=2, mp_context=multiprocessing.get_context("spawn")) as pool:
            page_future = pool.submit(_recognize_page_content, *page_args)
            table_future = pool.submit(_recognize_table_content, *table_args)
            # 两个任务先全部提交；组装必须等待两路，不能因某一路先结束提前导出。
            page_result, table_result = page_future.result(), table_future.result()
    else:
        page_result = _recognize_page_content(*page_args)
        table_result = _recognize_table_content(*table_args)
    errors = page_result["errors"] + table_result["errors"]
    # Join the two OCR branches before grounding: VL HTML has no cell boxes,
    # while the already computed page OCR may provide usable text-line boxes.
    page_by_number = {page["page_number"]: page for page in page_result["pages"]}
    crop_by_id = {crop["id"]: crop for crop in layout["images"] if crop["label"] == "table"}
    for table in table_result["tables"]:
        if table.get("status") == "success":
            _ground_table_cells(table, crop_by_id[table["table_id"]],
                                page_lines=page_by_number.get(table["page"], {}).get("lines", []))
            _write_json(output_dir / "table_ocr" / f"{table['table_id']}.json", table)
    _write_text(output_dir / "table_ocr" / "tables.jsonl",
                "".join(json.dumps(table, ensure_ascii=False, allow_nan=False) + "\n" for table in table_result["tables"]))
    execution = "parallel" if actual_parallel else "serial"
    runtime = {"text": page_result["runtime"], "table": table_result["runtime"]}
    result = {"page_summary": page_result["page_summary"], "pages": page_result["pages"],
              "tables": table_result["tables"], "layout_dir": str(layout_dir),
              "execution": execution, "runtime": runtime, "errors": errors}
    _write_json(output_dir / "recognition_summary.json", {
        "page_summary": page_result["page_summary"], "table_ocr_enabled": table_ocr,
        "execution": execution, "runtime": runtime,
        "successful_tables": sum(row["status"] == "success" for row in table_result["tables"]),
        "failed_tables": [row["table_id"] for row in table_result["tables"] if row["status"] == "failed"],
        "errors": errors,
    })
    return result


# 5. 结构组装：页面上下文、表格网格和续表验证
# 5.1 文档上下文与续表候选
TABLE_REF_RE = re.compile(r"(?:表|TABLE)([0-9]+(?:[-‐-―][0-9]+)?|[IVXLC]+)", re.I)
IMPLICIT_TABLE_RE = re.compile(
    r"(?:如下表|见下表|下表所示|结果列于下表|FOLLOWINGTABLE|TABLEBELOW)", re.I
)
FOOTNOTE_RE = re.compile(r"^(?:注|NOTE|NOTES|\*|#)", re.I)
CONTINUATION_RE = re.compile(r"(?:续表|表续|CONTINUED|CONTINUATION)", re.I)


def _clean_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    return re.sub(r"\s+", " ", text).strip()


def _compact(value: Any) -> str:
    return re.sub(r"\s+", "", _clean_text(value)).upper()


def _bbox(values: Iterable[Any]) -> list[float]:
    result = [float(value) for value in values]
    if len(result) != 4:
        return []
    return [round(value, 3) for value in result]


def _union_bbox(boxes: Iterable[Iterable[Any]]) -> list[float]:
    valid = [_bbox(box) for box in boxes]
    valid = [box for box in valid if len(box) == 4]
    if not valid:
        return []
    return [
        min(box[0] for box in valid),
        min(box[1] for box in valid),
        max(box[2] for box in valid),
        max(box[3] for box in valid),
    ]


def _area(box: list[float]) -> float:
    return max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1]) if len(box) == 4 else 0.0


def _intersection_area(left: list[float], right: list[float]) -> float:
    if len(left) != 4 or len(right) != 4:
        return 0.0
    return max(0.0, min(left[2], right[2]) - max(left[0], right[0])) * max(
        0.0, min(left[3], right[3]) - max(left[1], right[1])
    )


def _horizontal_overlap(left: list[float], right: list[float]) -> float:
    if len(left) != 4 or len(right) != 4:
        return 0.0
    overlap = max(0.0, min(left[2], right[2]) - max(left[0], right[0]))
    denominator = max(1.0, min(left[2] - left[0], right[2] - right[0]))
    return min(1.0, overlap / denominator)


def _overlap_fraction(block_box: list[float], table_box: list[float]) -> float:
    return _intersection_area(block_box, table_box) / max(1.0, _area(block_box))


def _is_running_header_or_footer(text: str, box: list[float], page_height: float) -> bool:
    compact = _compact(text)
    at_edge = box[1] < 75 or box[3] > page_height - 45
    if not at_edge:
        return False
    return bool(
        re.fullmatch(r"\d+", compact)
        or re.search(r"^(?:WO\d|PCT/|CN\d|US\d|EP\d)", compact, re.I)
    )


def _sentences(text: str) -> list[str]:
    """Split bilingual prose conservatively while retaining source wording."""

    parts = re.split(r"(?<=[。！？!?；;])\s*|(?<=\.)\s+(?=[A-Z])", _clean_text(text))
    return [part.strip() for part in parts if part.strip()]


def table_references(text: str) -> list[str]:
    """Return normalized table identifiers such as ``7`` or ``9-1``."""

    compact = _compact(text).replace("‐", "-").replace("‑", "-")
    compact = compact.replace("‒", "-").replace("–", "-").replace("—", "-")
    return list(dict.fromkeys(match.group(1).upper() for match in TABLE_REF_RE.finditer(compact)))


def _table_mentions(text: str) -> list[str]:
    compact = _compact(text).replace("‐", "-").replace("‑", "-")
    compact = compact.replace("‒", "-").replace("–", "-").replace("—", "-")
    return [match.group(1).upper() for match in TABLE_REF_RE.finditer(compact)]


def _looks_like_caption(text: str) -> bool:
    return bool(re.match(r"^(?:表|TABLE)(?:[0-9]+|[IVXLC]+)", _compact(text), re.I))


def _caption_excerpt(text: str, limit: int = 240) -> str:
    value = _clean_text(text)
    matches = list(
        re.finditer(
            r"(?:表\s*|TABLE\s*)(?:[0-9]+(?:\s*[-‐-―]\s*[0-9]+)?|[IVXLC]+)",
            value,
            re.I,
        )
    )
    if matches:
        # A PDF text block may contain the explanatory sentence followed by
        # the caption.  In that common layout the last marker is the caption.
        match = matches[0] if _looks_like_caption(value) else matches[-1]
        value = value[match.start() :]
    sentence = _sentences(value)
    if sentence:
        value = sentence[0]
    header = re.search(
        r"\s+(?:抗体\s+链|突变号(?:\s|$)|实验组(?:\s|$)|SEQ\s*ID\s*NO|序列编号(?:\s|$))",
        value,
        re.I,
    )
    if header:
        value = value[: header.start()]
    return value[:limit].rstrip()


def extract_document_blocks(pdf_path: str | Path) -> list[dict[str, Any]]:
    """Extract readable PDF text blocks with page coordinates and sentences."""

    fitz = _fitz()
    path = Path(pdf_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    document = fitz.open(path)
    output: list[dict[str, Any]] = []
    try:
        for page_index, page in enumerate(document):
            page_number = page_index + 1
            page_width = float(page.rect.width)
            page_height = float(page.rect.height)
            payload = page.get_text("dict")
            block_number = 0
            for raw_block in payload.get("blocks", []):
                if raw_block.get("type", 0) != 0:
                    continue
                kept_lines: list[tuple[str, list[float]]] = []
                for line in raw_block.get("lines", []):
                    spans = line.get("spans", [])
                    text = _clean_text("".join(str(span.get("text", "")) for span in spans))
                    box = _bbox(line.get("bbox", []))
                    if len(box) == 4 and page.rotation:
                        box = list(fitz.Rect(box) * page.rotation_matrix)
                    if not text or len(box) != 4:
                        continue
                    if _is_running_header_or_footer(text, box, page_height):
                        continue
                    kept_lines.append((text, box))
                if not kept_lines:
                    continue
                text = _clean_text(" ".join(line_text for line_text, _ in kept_lines))
                box = _union_bbox(line_box for _, line_box in kept_lines)
                if not text or len(box) != 4:
                    continue
                block_number += 1
                sentence_values = _sentences(text)
                block_id = f"p{page_number:04d}-b{block_number:04d}"
                output.append(
                    {
                        "block_id": block_id,
                        "page": page_number,
                        "page_width": round(page_width, 3),
                        "page_height": round(page_height, 3),
                        "bbox": box,
                        "text": text,
                        "normalized_text": _compact(text),
                        "block_type": "table_caption" if _looks_like_caption(text) else "paragraph",
                        "source": "pdf_text",
                        "sentences": [
                            {
                                "sentence_id": f"{block_id}-s{index:03d}",
                                "text": sentence,
                                # A sentence can span several PDF lines.  The containing
                                # block is the smallest reliable bbox without inventing
                                # character coordinates.
                                "bbox": box,
                            }
                            for index, sentence in enumerate(sentence_values, start=1)
                        ],
                    }
                )
    finally:
        document.close()
    return output


def _evidence(
    block: dict[str, Any],
    *,
    relation: str,
    confidence: float,
    text: str | None = None,
    evidence_id: str | None = None,
) -> dict[str, Any]:
    return {
        "evidence_id": evidence_id or block.get("block_id", ""),
        "page": block.get("page"),
        "bbox": block.get("bbox", []),
        "text": text if text is not None else block.get("text", ""),
        "source": block.get("source", "pdf_text"),
        "relation": relation,
        "confidence": round(float(confidence), 4),
    }


def _vertical_distance(block_box: list[float], table_box: list[float]) -> float:
    if _intersection_area(block_box, table_box) > 0:
        return 0.0
    if block_box[3] <= table_box[1]:
        return table_box[1] - block_box[3]
    if block_box[1] >= table_box[3]:
        return block_box[1] - table_box[3]
    return 0.0


def _caption_score(block: dict[str, Any], candidate: dict[str, Any]) -> float:
    block_box = _bbox(block.get("bbox", []))
    table_box = _bbox(candidate.get("bbox", []))
    if block.get("page") != candidate.get("page") or len(block_box) != 4 or len(table_box) != 4:
        return 0.0
    references = table_references(block.get("text", ""))
    if not references:
        return 0.0
    distance = _vertical_distance(block_box, table_box)
    if distance > 150:
        return 0.0
    caption_at_start = _looks_like_caption(block.get("text", ""))
    # Embedded captions occur when PyMuPDF combines the preceding prose and
    # the table title into one block.  Geometry keeps ordinary remote table
    # references from being promoted to captions.
    if not caption_at_start:
        mentions = _table_mentions(block.get("text", ""))
        # A combined prose+caption block repeats the table number: once in
        # "results are shown in Table N" and once at the title.  Requiring
        # that repetition rejects ordinary in-text references near a table.
        repeated = any(mentions.count(value) >= 2 for value in set(mentions))
        if distance > 15 or not repeated:
            return 0.0
    score = (0.35 if caption_at_start else 0.15) + 0.20
    score += 0.20 * max(0.0, 1.0 - distance / 150.0)
    score += 0.15 * _horizontal_overlap(block_box, table_box)
    if block_box[1] <= table_box[1] + 30:
        score += 0.10
    return min(1.0, score)


def _number_parts(value: Any) -> tuple[int, ...] | None:
    text = str(value or "")
    if not re.fullmatch(r"\d+(?:-\d+)?", text):
        return None
    return tuple(int(part) for part in text.split("-"))


def _between_number(left: Any, right: Any) -> str | None:
    before = _number_parts(left)
    after = _number_parts(right)
    if before is None or after is None or len(before) != len(after):
        return None
    if len(before) == 1 and after[0] == before[0] + 2:
        return str(before[0] + 1)
    if len(before) == 2 and before[0] == after[0] and after[1] == before[1] + 2:
        return f"{before[0]}-{before[1] + 1}"
    return None


def _infer_missing_table_numbers(
    contexts: list[dict[str, Any]], blocks: list[dict[str, Any]]
) -> None:
    """Conservatively fill one missing ordinal between two known captions."""

    ordered = sorted(contexts, key=lambda item: (item.get("page") or 0, item.get("bbox", [0, 0])[1]))
    groups: list[list[dict[str, Any]]] = []
    for context in ordered:
        if not groups or groups[-1][0].get("logical_table_id") != context.get("logical_table_id"):
            groups.append([context])
        else:
            groups[-1].append(context)
    for index in range(1, len(groups) - 1):
        group = groups[index]
        if any(item.get("table_number") for item in group):
            continue
        inferred = _between_number(
            groups[index - 1][0].get("table_number"),
            groups[index + 1][0].get("table_number"),
        )
        if not inferred:
            continue
        logical_id = f"table-{inferred.lower()}"
        for item in group:
            item["table_number"] = inferred
            item["logical_table_id"] = logical_id
            item["context_status"] = "review"
        first = group[0]
        page = int(first.get("page") or 0)
        table_box = _bbox(first.get("bbox", []))
        nearby = []
        for block in blocks:
            if block.get("page") != page:
                continue
            box = _bbox(block.get("bbox", []))
            if len(box) == 4 and box[3] <= table_box[1] + 4:
                distance = max(0.0, table_box[1] - box[3])
                if distance <= 45 and len(_clean_text(block.get("text", ""))) <= 300:
                    nearby.append((distance, block))
        if nearby and not first.get("caption"):
            _, block = min(nearby, key=lambda pair: pair[0])
            first["caption"] = _evidence(
                block,
                relation="caption",
                confidence=0.55,
                text=_caption_excerpt(block.get("text", "")),
            )


def _block_overlaps_a_table(
    block: dict[str, Any], candidates_by_page: dict[int, list[dict[str, Any]]]
) -> bool:
    block_box = _bbox(block.get("bbox", []))
    return any(
        _overlap_fraction(block_box, _bbox(candidate.get("bbox", []))) >= 0.18
        for candidate in candidates_by_page.get(int(block.get("page", 0)), [])
    )


def _nearby_blocks(
    candidate: dict[str, Any],
    blocks: list[dict[str, Any]],
    candidates_by_page: dict[int, list[dict[str, Any]]],
    caption_block_id: str | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    page = int(candidate.get("page") or 0)
    table_box = _bbox(candidate.get("bbox", []))
    if not page or len(table_box) != 4:
        return [], []
    before: list[tuple[float, dict[str, Any]]] = []
    after: list[tuple[float, dict[str, Any]]] = []
    notes: list[tuple[float, dict[str, Any]]] = []
    for block in blocks:
        if block.get("page") != page or block.get("block_id") == caption_block_id:
            continue
        if block.get("block_type") == "table_caption" or _block_overlaps_a_table(block, candidates_by_page):
            continue
        box = _bbox(block.get("bbox", []))
        if len(box) != 4:
            continue
        if box[3] <= table_box[1] + 3:
            distance = max(0.0, table_box[1] - box[3])
            if distance <= 180:
                before.append((distance, block))
        elif box[1] >= table_box[3] - 3:
            distance = max(0.0, box[1] - table_box[3])
            if distance <= 140:
                if FOOTNOTE_RE.match(_clean_text(block.get("text", ""))):
                    notes.append((distance, block))
                else:
                    after.append((distance, block))
    selected_before = [item for _, item in sorted(before, key=lambda pair: pair[0])[:2]]
    selected_after = [item for _, item in sorted(after, key=lambda pair: pair[0])[:2]]
    context = []
    for block in selected_before:
        distance = max(0.0, table_box[1] - _bbox(block["bbox"])[3])
        context.append(
            _evidence(block, relation="preceding_paragraph", confidence=max(0.55, 0.9 - distance / 400.0))
        )
    for block in selected_after:
        distance = max(0.0, _bbox(block["bbox"])[1] - table_box[3])
        context.append(
            _evidence(block, relation="following_paragraph", confidence=max(0.5, 0.85 - distance / 400.0))
        )
    footnotes = [
        _evidence(block, relation="footnote", confidence=max(0.6, 0.9 - distance / 300.0))
        for distance, block in sorted(notes, key=lambda pair: pair[0])[:3]
    ]
    return context, footnotes


def _nearest_table_after(
    block: dict[str, Any], contexts: list[dict[str, Any]]
) -> dict[str, Any] | None:
    page = int(block.get("page") or 0)
    box = _bbox(block.get("bbox", []))
    options: list[tuple[float, dict[str, Any]]] = []
    for context in contexts:
        table_page = int(context.get("page") or 0)
        table_box = _bbox(context.get("bbox", []))
        if table_page not in {page, page + 1} or len(table_box) != 4:
            continue
        if table_page == page and table_box[1] < box[3]:
            continue
        distance = (table_page - page) * 1000.0 + max(0.0, table_box[1] - box[3])
        options.append((distance, context))
    return min(options, key=lambda pair: pair[0])[1] if options else None


def _assign_logical_tables(contexts: list[dict[str, Any]], blocks: list[dict[str, Any]]) -> None:
    page_heights = {int(block["page"]): float(block.get("page_height", 842.0)) for block in blocks}
    ordered = sorted(contexts, key=lambda item: (item.get("page") or 0, item.get("bbox", [0, 0])[1]))
    for context in ordered:
        number = context.get("table_number")
        context["logical_table_id"] = f"table-{str(number).lower()}" if number else context["table_id"]
        context["continuation_of"] = None
        context["continuation_confidence"] = None
    for index, context in enumerate(ordered):
        if index == 0:
            continue
        previous = ordered[index - 1]
        same_number = bool(
            context.get("table_number")
            and context.get("table_number") == previous.get("table_number")
        )
        caption_text = (context.get("caption") or {}).get("text", "")
        explicit_continuation = bool(CONTINUATION_RE.search(_compact(caption_text)))
        page = int(context.get("page") or 0)
        previous_page = int(previous.get("page") or 0)
        table_box = _bbox(context.get("bbox", []))
        previous_box = _bbox(previous.get("bbox", []))
        page_height = page_heights.get(page, 842.0)
        previous_height = page_heights.get(previous_page, 842.0)
        geometric_continuation = bool(
            not context.get("caption")
            and page == previous_page + 1
            and len(table_box) == 4
            and len(previous_box) == 4
            and table_box[1] <= page_height * 0.22
            and previous_box[3] >= previous_height * 0.80
        )
        if same_number or explicit_continuation or geometric_continuation:
            context["logical_table_id"] = previous["logical_table_id"]
            context["continuation_of"] = previous["table_id"]
            context["continuation_confidence"] = 0.95 if same_number or explicit_continuation else 0.65


def link_table_contexts(
    candidates: list[dict[str, Any]], blocks: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Link captions, adjacent prose, references, and continuations to tables."""

    candidates_by_page: dict[int, list[dict[str, Any]]] = {}
    for candidate in candidates:
        page = int(candidate.get("page") or 0)
        if page:
            candidates_by_page.setdefault(page, []).append(candidate)

    caption_blocks = [block for block in blocks if table_references(block.get("text", ""))]
    pair_scores: list[tuple[float, str, str, dict[str, Any], dict[str, Any]]] = []
    for block in caption_blocks:
        for candidate in candidates_by_page.get(int(block.get("page") or 0), []):
            score = _caption_score(block, candidate)
            if score >= 0.55:
                pair_scores.append(
                    (score, str(candidate.get("table_id")), str(block.get("block_id")), candidate, block)
                )
    assigned_tables: dict[str, tuple[float, dict[str, Any]]] = {}
    assigned_blocks: set[str] = set()
    for score, table_id, block_id, _, block in sorted(pair_scores, key=lambda item: item[0], reverse=True):
        if table_id in assigned_tables or block_id in assigned_blocks:
            continue
        assigned_tables[table_id] = (score, block)
        assigned_blocks.add(block_id)

    contexts: list[dict[str, Any]] = []
    for candidate in candidates:
        table_id = str(candidate.get("table_id", ""))
        caption_pair = assigned_tables.get(table_id)
        caption: dict[str, Any] | None = None
        table_number: str | None = None
        caption_block_id: str | None = None
        if caption_pair:
            score, block = caption_pair
            caption_block_id = str(block.get("block_id"))
            references = table_references(block.get("text", ""))
            table_number = references[0] if references else None
            caption = _evidence(
                block,
                relation="caption",
                confidence=score,
                text=_caption_excerpt(block.get("text", "")),
            )
        nearby, footnotes = _nearby_blocks(
            candidate, blocks, candidates_by_page, caption_block_id
        )
        contexts.append(
            {
                "table_id": table_id,
                "image": candidate.get("image", ""),
                "page": candidate.get("page"),
                "bbox": candidate.get("bbox", []),
                "table_number": table_number,
                "logical_table_id": table_id,
                "caption": caption,
                "nearby_context": nearby,
                "referring_sentences": [],
                "footnotes": footnotes,
                "continuation_of": None,
                "continuation_confidence": None,
                "context_image": None,
                "context_status": "linked" if caption else ("review" if nearby or footnotes else "missing"),
            }
        )

    _assign_logical_tables(contexts, blocks)
    _infer_missing_table_numbers(contexts, blocks)

    by_number: dict[str, list[dict[str, Any]]] = {}
    for context in contexts:
        if context.get("table_number"):
            by_number.setdefault(str(context["table_number"]), []).append(context)

    caption_evidence_ids = {
        str(context["caption"].get("evidence_id"))
        for context in contexts
        if context.get("caption")
    }
    for block in blocks:
        if _block_overlaps_a_table(block, candidates_by_page):
            continue
        for sentence in block.get("sentences", []):
            sentence_text = str(sentence.get("text", ""))
            references = table_references(sentence_text)
            sentence_id = str(sentence.get("sentence_id") or block.get("block_id"))
            if (
                str(block.get("block_id")) in caption_evidence_ids
                and _looks_like_caption(sentence_text)
            ):
                continue
            for reference in references:
                options = by_number.get(reference, [])
                if not options:
                    continue
                target = min(
                    options,
                    key=lambda item: (
                        abs(int(item.get("page") or 0) - int(block.get("page") or 0)),
                        abs(_bbox(item.get("bbox", [0, 0, 0, 0]))[1] - _bbox(block.get("bbox", [0, 0, 0, 0]))[3]),
                    ),
                )
                evidence = _evidence(
                    block,
                    relation="explicit_reference",
                    confidence=0.98,
                    text=sentence_text,
                    evidence_id=sentence_id,
                )
                if evidence["evidence_id"] != ((target.get("caption") or {}).get("evidence_id")):
                    target["referring_sentences"].append(evidence)
                    target["context_status"] = "linked"
            if not references and IMPLICIT_TABLE_RE.search(_compact(sentence_text)):
                target = _nearest_table_after(block, contexts)
                if target:
                    target["referring_sentences"].append(
                        _evidence(
                            block,
                            relation="implicit_reference",
                            confidence=0.72,
                            text=sentence_text,
                            evidence_id=sentence_id,
                        )
                    )
                    if target["context_status"] == "missing":
                        target["context_status"] = "review"

    for context in contexts:
        deduplicated: dict[tuple[str, str], dict[str, Any]] = {}
        for evidence in context["referring_sentences"]:
            deduplicated[(str(evidence.get("evidence_id")), str(evidence.get("text")))] = evidence
        context["referring_sentences"] = list(deduplicated.values())
        if context.get("continuation_confidence") and context["continuation_confidence"] < 0.75:
            context["context_status"] = "review"
    return contexts


def render_context_images(
    pdf_path: str | Path,
    contexts: list[dict[str, Any]],
    output_dir: str | Path,
    *,
    dpi: int = 180,
    padding_pt: float = 12.0,
) -> dict[str, str]:
    """Render one auditable same-page context image per physical table."""

    fitz = _fitz()
    pdf_path = Path(pdf_path).expanduser().resolve()
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    document = fitz.open(pdf_path)
    written: dict[str, str] = {}
    try:
        for context in contexts:
            page_number = int(context.get("page") or 0)
            table_box = _bbox(context.get("bbox", []))
            if not page_number or page_number > len(document) or len(table_box) != 4:
                continue
            boxes = [table_box]
            for key in ("nearby_context", "footnotes"):
                boxes.extend(
                    _bbox(item.get("bbox", []))
                    for item in context.get(key, [])
                    if item.get("page") == page_number
                )
            caption = context.get("caption")
            if caption and caption.get("page") == page_number:
                boxes.append(_bbox(caption.get("bbox", [])))
            clip_box = _union_bbox(boxes)
            page = document.load_page(page_number - 1)
            clip = fitz.Rect(
                max(page.rect.x0, clip_box[0] - padding_pt),
                max(page.rect.y0, clip_box[1] - padding_pt),
                min(page.rect.x1, clip_box[2] + padding_pt),
                min(page.rect.y1, clip_box[3] + padding_pt),
            )
            pixmap = page.get_pixmap(matrix=fitz.Matrix(dpi / 72.0, dpi / 72.0), clip=clip, alpha=False)
            safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(context.get("table_id", "table")))
            destination = output_dir / f"context-{safe_id}.png"
            pixmap.save(destination)
            context["context_image"] = str(destination)
            written[str(context.get("table_id"))] = str(destination)
    finally:
        document.close()
    return written


def context_text(context: dict[str, Any]) -> str:
    """Flatten only linked prose for deterministic table triage."""

    values: list[str] = []
    if context.get("caption"):
        values.append(str(context["caption"].get("text", "")))
    for key in ("nearby_context", "referring_sentences", "footnotes"):
        values.extend(str(item.get("text", "")) for item in context.get(key, []))
    return " ".join(value for value in values if value)


# 5.2 单元格网格及跨页表头验证
ENDPOINT = re.compile(r"\b(IC\s*[₅5][₀0]|EC\s*[₅5][₀0]|KD|Ka|ka|Kd|kd|kon|koff)\b")
MISSING = {"", "NA", "N/A", "ND", "N.D.", "-", "—", "/"}


def build_table_contexts(pdf, crops):
    candidates = [{**r, "table_id": r["id"]} for r in crops if r.get("label") == "table"]
    blocks = extract_document_blocks(pdf)
    # Empty/scanned pages still need page dimensions for geometric candidates.
    import pymupdf
    with pymupdf.open(pdf) as document:
        for index, page in enumerate(document):
            blocks.append({"block_id": f"size-p{index + 1}", "page": index + 1,
                "page_width": page.rect.width, "page_height": page.rect.height,
                "bbox": [], "text": "", "sentences": []})
    return link_table_contexts(candidates, blocks)


def load_crop_metadata(image_dir):
    """Read both archived SDA manifests and fresh pdf2fig manifests."""
    root, result = Path(image_dir).resolve(), {}
    for path in sorted(root.rglob("*_manifest.json")):
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or not isinstance(manifest.get("images"), list):
            continue
        rows = copy.deepcopy(manifest["images"])
        source = Path(manifest.get("source_pdf") or "")
        if not source.is_absolute():
            source = path.parent / source
        if source.is_file() and manifest.get("source_pdf_sha256") and hashlib.sha256(
            source.read_bytes()).hexdigest() != manifest["source_pdf_sha256"]:
            raise ValueError(f"Original PDF changed after cropping: {source}")
        pdf = Path(manifest.get("processing_pdf") or manifest.get("source_pdf") or "")
        if not pdf.is_absolute():
            pdf = path.parent / pdf
        if pdf.is_file():
            expected = manifest.get("processing_pdf_sha256") or manifest.get("source_pdf_sha256")
            if expected and hashlib.sha256(pdf.read_bytes()).hexdigest() != expected:
                raise ValueError(f"PDF geometry source changed: {pdf}")
            contexts = {r["table_id"]: r for r in build_table_contexts(pdf, rows)} if any(
                "table_context" not in r for r in rows if r.get("label") == "table") else {}
        else:
            contexts = {}
        for row in rows:
            image = (path.parent / row["image"]).resolve()
            if not image.is_relative_to(root) or not image.is_file():
                continue
            if row.get("sha256") and hashlib.sha256(image.read_bytes()).hexdigest() != row["sha256"]:
                raise ValueError(f"Crop image no longer matches its coordinates: {image}")
            row.update(image_path=str(image), manifest_file=str(path),
                       document_id=manifest.get("document_id") or manifest.get("patent_id"),
                       processing_pdf=str(pdf), orientation_review_required=row.get("page") in
                       manifest.get("orientation", {}).get("review_pages", []))
            if row["id"] in contexts:
                row["table_context"] = contexts[row["id"]]
            key = str(image)
            if key in result:
                raise ValueError(f"Duplicate crop metadata: {image}")
            result[key] = row
    return result


def parse_grid(text, table_id):
    """Expand HTML spans while retaining each original cell's address."""
    if not re.search(r"<table\b", text, re.I):
        return None
    try:
        root = html.fromstring(text)
    except (ValueError, html.etree.ParserError):
        return None
    tables = [root] if root.tag == "table" else root.xpath(".//table")
    if len(tables) != 1:
        return None
    rows, physical, occupied = [], [], {}
    for ri, tr in enumerate(tables[0].xpath(".//tr")):
        cells, column = [], 0
        for td in tr.xpath("./td|./th"):
            while (ri, column) in occupied:
                column += 1
            try:
                rowspan, colspan = int(td.get("rowspan", 1)), int(td.get("colspan", 1))
            except ValueError:
                return None
            if not 1 <= rowspan <= 1000 or not 1 <= colspan <= 100:
                return None
            value = " ".join(td.itertext()).strip()
            cell = {"text": value, "ref": f"{table_id}:R{ri}C{column}", "row": ri, "column": column,
                    "rowspan": rowspan, "colspan": colspan}
            cells.append(cell)
            for rr in range(ri, ri + rowspan):
                for cc in range(column, column + colspan):
                    if (rr, cc) in occupied:
                        return None
                    occupied[rr, cc] = cell
            column += colspan
        physical.append(cells)
        width = max((cc + 1 for rr, cc in occupied if rr == ri), default=0)
        rows.append([occupied.get((ri, cc), {"text": "", "ref": None}) for cc in range(width)])
    return {"rows": rows, "physical": physical}


def _scalar(value):
    value = value.strip()
    if value.upper() in MISSING:
        return True, None
    try:
        number = Decimal(value)
        if not number.is_finite():
            return False, None
        return True, int(number) if number == number.to_integral() else float(number)
    except InvalidOperation:
        if re.fullmatch(r"[<>≤≥~≈]?\s*\d[\d.eE+\-\s±~–—/%]*", value):
            return True, value
        return False, None


def _numeric_data(row):
    return len(row) > 1 and any(c["text"].strip() and _scalar(c["text"])[0] for c in row[1:])


def _header_band(grid):
    rows = []
    for row, physical in zip(grid["rows"], grid["physical"]):
        if _numeric_data(physical):
            break
        rows.append(row)
    return rows if 0 < len(rows) <= 5 and any(ENDPOINT.search(c["text"]) for row in rows for c in row) else []


def _header_html(text, count):
    root = html.fromstring(text)
    table = root if root.tag == "table" else root.xpath(".//table")[0]
    for row in table.xpath(".//tr")[count:]:
        row.getparent().remove(row)
    return html.tostring(table, encoding="unicode")


def _width(grid, header=False):
    if header:
        return max((len(r) for r in grid["rows"]), default=0)
    widths = []
    for row in grid["rows"]:
        if not _numeric_data(row):
            if widths:
                break
            continue
        # A VL numeric cell can have a spurious colspan; count its original
        # cell once. A real row-spanned name still contributes its source cell.
        widths.append(len({c["ref"] for c in row if c.get("ref")}))
        if len(widths) == 3:
            break
    return widths[0] if widths and len(set(widths)) == 1 else 0


def verified_context(child, child_text, parents):
    """parents are nearest-first OCR results of the proposed continuation chain."""
    context = child.get("table_context", {})
    result = {"table_id": child["id"], "page": child["page"],
        "logical_table_id": context.get("logical_table_id", child["id"]),
        "continuation_of": context.get("continuation_of"), "support_tables": [], "issues": []}
    grid = parse_grid(child_text, child["id"])
    if not parents or not grid:
        return result
    # A local metric header takes precedence; do not reuse an unrelated header.
    if any(ENDPOINT.search(c["text"]) for c in grid["physical"][0]):
        return result
    current, current_grid = child, grid
    for parent, text in parents:
        parent_grid = parse_grid(text, parent["id"])
        if not parent_grid:
            result["issues"].append("parent_has_no_structured_ocr")
            break
        parent_context, current_context = parent.get("table_context", {}), current.get("table_context", {})
        boxes = [parent.get("bbox", []), current.get("bbox", [])]
        valid = (current_context.get("continuation_of") == parent["id"] and
            current["page"] == parent["page"] + 1 and
            current.get("document_id") == parent.get("document_id") and
            current_context.get("logical_table_id") == parent_context.get("logical_table_id") and
            all(len(b) == 4 for b in boxes) and
            all(abs(boxes[0][i] - boxes[1][i]) <= 10 for i in (0, 2)))
        if not valid:
            result["issues"].append("continuation_geometry_not_verified")
            break
        header_rows = _header_band(parent_grid)
        parent_width = max(map(len, header_rows)) if header_rows else _width(parent_grid)
        if _width(current_grid) != parent_width or not _width(current_grid):
            result["issues"].append("continuation_columns_do_not_align")
            break
        if header_rows:
            result["support_tables"].append({"table_id": parent["id"], "page": parent["page"],
                "image_path": parent["image_path"], "verification": "adjacent_page_bbox_and_ocr_columns",
                "header_rows": header_rows, "table_text": _header_html(text, len(header_rows)),
                "source_ocr_sha256": hashlib.sha256(text.encode()).hexdigest()})
            break
        current, current_grid = parent, parent_grid
    return result


def _metrics(rows):
    result = {}
    for row in rows:
        for col, cell in enumerate(row):
            match = ENDPOINT.search(cell["text"])
            if not match:
                continue
            unit = re.search(r"[（(]([^()（）]+)[)）]", cell["text"][match.end():])
            endpoint = match.group().replace(" ", "").translate(str.maketrans("₅₀", "50"))
            refs = list(dict.fromkeys(r[col]["ref"] for r in rows if col < len(r) and r[col].get("ref")))
            result[col] = {"metric": endpoint, "unit": unit.group(1).strip() if unit else None,
                "header_refs": refs, "unit_refs": [cell["ref"]] if unit else []}
    return result


def extract_continuation_assays(text, context):
    """Execute fully explicit inherited/local metric columns; otherwise defer to LLM.

    Preserve physical cell refs, independent endpoint regions, missing values and
    qualified values. Never project the inherited header past a new local title.
    """
    supports = context.get("support_tables", []) if context else []
    if len(supports) != 1:
        return None
    grid = parse_grid(text, context["table_id"])
    if not grid:
        return None
    active = _metrics(supports[0]["header_rows"])
    if not active or any(m["unit"] is None for m in active.values()):
        return None
    records = []
    for row, physical in zip(grid["rows"], grid["physical"]):
        if not physical:
            continue
        metrics = _metrics([row])
        if metrics:
            if _numeric_data(physical):
                return None
            active = metrics
            continue
        if len(physical) == 1:
            active = {}
            continue
        if not _numeric_data(physical):
            if any(c["text"].strip() for c in physical):
                return None
            continue
        if not active or not row[0]["text"] or any(c >= len(row) for c in active):
            return None
        name = row[0]["text"]
        for column, metric in active.items():
            valid, value = _scalar(row[column]["text"])
            if not valid or metric["unit"] is None:
                return None
            records.append({"name": name, "metric": metric["metric"], "value": value, "unit": metric["unit"],
                "evidence": {"method": "verified_continuation_grid", "source_table_id": context["table_id"],
                    "page": context["page"], "name_ref": row[0]["ref"], "value_ref": row[column]["ref"],
                    "raw_value": row[column]["text"], "header_refs": metric["header_refs"],
                    "unit_refs": metric["unit_refs"]}})
    return records or None


def _source_address(item, transform):
    """补充原文坐标；不覆盖工作页上的 OCR/检测坐标。"""
    result = copy.deepcopy(item)
    box = result.get("bbox", result.get("block_bbox"))
    if box is not None and len(box) == 4:
        result["bbox_source"] = list(pymupdf.Rect(box) * transform)
    return result


def _document_markdown(content, number):
    """将逐页 Markdown 的附件链接调整为统一导出目录下的相对路径。"""
    rendered = rebase_markdown(content, number)
    return (rendered.replace('src="markdown/', 'src="page_ocr/markdown/')
            .replace("src='markdown/", "src='page_ocr/markdown/")
            .replace("](markdown/", "](page_ocr/markdown/"))


def assemble_document(source, layout, recognition, *, orientation=None):
    """组装页面、正文和物理表格；只对已验证续表引用前页表头。

    工作页坐标统一为 PDF pt，源坐标的坐标系单独声明。原始 OCR 文本、
    HTML 和单元格引用保留；正文中的表格区块通过 table_id 关联裁图结果。
    """
    identity = layout["document_id"]
    if source["source_pdf_sha256"] != layout["source_pdf_sha256"]:
        raise ValueError("原始 PDF 信息与版面结果的哈希不一致")
    working = load_pdf(layout["processing_pdf"])
    if working["source_pdf_sha256"] != layout["processing_pdf_sha256"]:
        raise ValueError("组装时工作 PDF 与版面结果的哈希不一致")
    records = {p["page_number"]: p for p in recognition["pages"]}
    selected = layout["selected_pages"]
    if len(records) != len(recognition["pages"]) or set(records) != set(selected):
        raise ValueError("整页 OCR 页码与所选版面页码不一致")
    for record in records.values():
        if record.get("document_id") != identity or record.get("pdf_sha256") != working["source_pdf_sha256"]:
            raise ValueError("整页 OCR 不属于当前文档或工作 PDF")
    crop_dir = Path(recognition.get("layout_dir", Path(layout["processing_pdf"]).parent))
    crop_metadata = {c["id"]: {**c, "image_path": str(crop_dir / c["image"])}
                     for c in layout["images"] if c["label"] == "table"}
    raw_tables = {t["table_id"]: t for t in recognition["tables"]}
    if len(raw_tables) != len(recognition["tables"]) or set(raw_tables) - set(crop_metadata):
        raise ValueError("表格 OCR 包含重复或不属于当前版面的表格 ID")
    tables = []
    for crop in crop_metadata.values():
        evidence = copy.deepcopy(raw_tables.get(crop["id"], {"status": "skipped"}))
        if evidence.get("page", crop["page"]) != crop["page"]:
            raise ValueError("表格 OCR 页码与裁图页码不一致")
        if evidence.get("image_sha256", crop["sha256"]) != crop["sha256"]:
            raise ValueError("表格 OCR 与裁图的哈希不一致")
        tables.append({**evidence, "table_id": crop["id"], "page": crop["page"],
                       "document_id": identity, "bbox": crop["bbox"],
                       "image": f"layout/{crop['image']}", "image_path": crop["image_path"],
                       "crop_ocr_status": evidence["status"], "source": "table_ocr",
                       "candidate_context": copy.deepcopy(crop.get("table_context", {}))})
    pages = []
    for number in selected:
        raw = records[number]
        geometry = working["pages"][number - 1]
        mapping = orientation["pages"][number - 1] if orientation else None
        transform = pymupdf.Matrix(mapping["working_to_source"]) if mapping else pymupdf.Matrix(1, 0, 0, 1, 0, 0)
        has_blocks = "blocks" in raw
        blocks = copy.deepcopy(raw.get("blocks", [])) if has_blocks else [
            {**line, "block_id": line["line_id"], "block_label": "text",
             "block_content": line["text"], "granularity": "line"} for line in raw.get("lines", [])]
        def order(item):
            index, block = item
            value = block.get("block_order")
            return (value if isinstance(value, (int, float)) and math.isfinite(value) else index, index)
        if has_blocks:
            blocks = [block for _, block in sorted(enumerate(blocks), key=order)]
        else:
            blocks.sort(key=lambda b: (b["bbox"][1], b["bbox"][0]))
        page_tables = [t for t in tables if t["page"] == number]
        assigned = set()
        for block in blocks:
            if canonical_label(block.get("block_label")) != "table":
                continue
            options = [(iou(block["bbox"], table["bbox"]), table)
                       for table in page_tables if table["table_id"] not in assigned]
            score, table = max(options, key=lambda pair: pair[0]) if options else (0, None)
            if score < 0.3:
                table = {"table_id": block["block_id"], "document_id": identity, "page": number,
                         "bbox": block["bbox"], "source": "page_ocr", "status": "success",
                         "text": block["block_content"], "candidate_context": {}}
                tables.append(table)
                page_tables.append(table)
            elif table["status"] != "success":
                # 整页 VL 提供的表格 HTML 作为显式后备，保留裁图 OCR 的原状态。
                table.update(text=block["block_content"], source="page_ocr", status="success")
            block["table_id"] = table["table_id"]
            assigned.add(table["table_id"])
        for table in page_tables:
            if table["table_id"] in assigned:
                continue
            if not has_blocks and table["status"] == "success":
                # 普通逐行 OCR 的表格文字保留在 raw lines，正文用一个表格区块替代。
                blocks = [b for b in blocks if _overlap_fraction(b["bbox"], table["bbox"]) < 0.5]
            block = {"block_id": f"{table['table_id']}:block", "block_label": "table",
                     "block_content": table.get("text", ""), "bbox": table["bbox"],
                     "table_id": table["table_id"], "reading_order_source": "geometry"}
            position = next((i for i, b in enumerate(blocks) if b["bbox"][1] > block["bbox"][1]), len(blocks))
            blocks.insert(position, block)
        normalized = []
        for table in page_tables:
            grid = _ground_table_cells(table, crop_metadata.get(table["table_id"], {"bbox": table["bbox"]}),
                                       page_lines=raw.get("lines", []), working_to_source=list(transform))
            table["structure_status"] = "complete" if grid else "unavailable"
            table["coordinate_space"] = "pdf_points"
            table["logical_table_id"] = table["table_id"]
            table.update(_source_address(table, transform))
            normalized.append(table["table_id"])
        page = {**copy.deepcopy(raw), **geometry, "document_id": identity,
                "coordinate_space": "pdf_points", "coordinate_frame": "rendered_page",
                "source_geometry": copy.deepcopy(source["pages"][number - 1]),
                "blocks": [_source_address(b, transform) for b in blocks],
                "lines": [_source_address(line, transform) for line in raw.get("lines", [])],
                "reading_order": [b["block_id"] for b in blocks], "table_ids": normalized}
        for field in ("image", "raw_result"):
            if page.get(field):
                page[field] = f"page_ocr/{page[field]}"
        if page.get("artifacts"):
            page["artifacts"] = {f"page_ocr/{path}": digest for path, digest in page["artifacts"].items()}
        if page.get("markdown"):
            page["markdown"] = _document_markdown(page["markdown"], number)
        if mapping:
            page["orientation"] = copy.deepcopy(mapping)
        pages.append(page)
    tables.sort(key=lambda table: (table["page"], table["bbox"][1], table["bbox"][0], table["table_id"]))
    table_by_id = {table["table_id"]: table for table in tables}
    for table in sorted(tables, key=lambda t: (t["page"], t["table_id"])):
        metadata = crop_metadata.get(table["table_id"])
        parents, seen = [], {table["table_id"]}
        parent_id = table.get("candidate_context", {}).get("continuation_of")
        while parent_id and parent_id not in seen:
            seen.add(parent_id)
            parent = table_by_id.get(parent_id)
            parent_metadata = crop_metadata.get(parent_id)
            if not parent or not parent_metadata or parent["status"] != "success":
                break
            parents.append((parent_metadata, parent.get("text", "")))
            parent_id = parent_metadata.get("table_context", {}).get("continuation_of")
        context = verified_context(metadata, table.get("text", ""), parents) if metadata else {
            "support_tables": [], "issues": [], "continuation_of": None}
        table["continuation"] = context
        table["inherited_header"] = context["support_tables"]
        if context["support_tables"]:
            support = table_by_id[context["support_tables"][0]["table_id"]]
            table["logical_table_id"] = support["logical_table_id"]
    logical = {}
    for table in tables:
        logical.setdefault(table["logical_table_id"], []).append(table["table_id"])
    unavailable = [t["table_id"] for t in tables if t["structure_status"] != "complete"]
    errors = copy.deepcopy(recognition["errors"])
    succeeded = any(p["status"] == "success" for p in pages) or any(t["status"] == "success" for t in tables)
    incomplete = bool(errors or unavailable or layout["status"] != "complete")
    status = "failed" if not succeeded else "partial" if incomplete else "complete"
    figures = []
    for crop in layout["images"]:
        if crop["label"] != "figure":
            continue
        mapping = orientation["pages"][crop["page"] - 1] if orientation else None
        transform = pymupdf.Matrix(mapping["working_to_source"]) if mapping else pymupdf.Matrix(1, 0, 0, 1, 0, 0)
        figure = _source_address(crop, transform)
        figure.update(image=f"layout/{crop['image']}", image_path=str(crop_dir / crop["image"]))
        figures.append(figure)
    return {
        "schema_version": "sda-structured-document/v1", "producer": "SDA/pdf2doc",
        "document_id": identity, "status": status,
        "source_pdf": source["source_pdf"], "source_pdf_sha256": source["source_pdf_sha256"],
        "metadata": source["metadata"], "processing_pdf": layout["processing_pdf"],
        "processing_pdf_sha256": working["source_pdf_sha256"],
        "total_pages": source["total_pages"], "selected_pages": selected,
        "unselected_pages": sorted(set(range(1, source["total_pages"] + 1)) - set(selected)),
        "coordinate_space": "pdf_points", "coordinate_frame": "rendered_page",
        "source_coordinate_frame": "unrotated_cropbox_points_top_left" if orientation else "rendered_page",
        "orientation": copy.deepcopy(layout["orientation"]), "pages": pages, "tables": tables,
        "figures": figures,
        "logical_tables": [{"logical_table_id": key, "table_ids": values} for key, values in logical.items()],
        "tables_without_grid": unavailable, "errors": errors,
        "scope": "Requested-page OCR and recovered structure; detection/recognition accuracy is not guaranteed.",
    }


# 6. 导出结构化 doc：JSON、JSONL、Markdown 和纯文本
def export_document(document, output_dir):
    """原子写出统一文档及阅读版本；原始 OCR 产物在子目录单独保留。"""
    output = Path(output_dir).resolve()
    table_by_id = {t["table_id"]: t for t in document["tables"]}
    markdown, text = [], []
    for page in document["pages"]:
        markdown.append(f"## PDF page {page['page']}\n")
        text.append(f"[PDF page {page['page']}]\n")
        if page["status"] != "success":
            failure = f"[OCR {page['status']}: {page.get('error', 'not processed')}]"
            markdown.append(failure)
            text.append(failure)
        for block in page["blocks"]:
            table = table_by_id.get(block.get("table_id"))
            content = table.get("text", "") if table else block.get("block_content", "")
            if table and table["status"] != "success":
                content = content or f"[Table OCR {table['status']}: {table['table_id']}]"
            if content.strip():
                markdown.append(_document_markdown(content, page["page"]))
                if canonical_label(block.get("block_label")) != "figure":
                    text.append(plain_text(content))
        for figure in document["figures"]:
            if figure["page"] == page["page"]:
                markdown.append(f"![Figure {figure['id']}]({figure['image']})")
    summary = {
        "document_id": document["document_id"], "status": document["status"],
        "total_pages": document["total_pages"], "selected_pages": len(document["pages"]),
        "successful_pages": sum(p["status"] == "success" for p in document["pages"]),
        "full_document_complete": document["status"] == "complete" and not document["unselected_pages"],
        "blocks": sum(len(p["blocks"]) for p in document["pages"]),
        "tables": len(document["tables"]), "figures": len(document["figures"]),
        "logical_tables": len(document["logical_tables"]),
        "tables_without_grid": document["tables_without_grid"],
        "orientation_review_pages": document["orientation"]["review_pages"],
        "errors": document["errors"], "scope": document["scope"],
    }
    _write_text(output / "pages.jsonl", "".join(json.dumps(p, ensure_ascii=False, allow_nan=False) + "\n"
                                              for p in document["pages"]))
    _write_text(output / "document.md", "\n\n".join(markdown) + "\n")
    _write_text(output / "document.txt", "\n\n".join(text) + "\n")
    _write_json(output / "summary.json", summary)
    _write_json(output / "document.json", document)
    # The semantic pipeline reuses table_ocr, so persist the derived cell/source
    # geometry there as well as in the structured document.
    index_path = output / "table_ocr" / "tables.jsonl"
    if index_path.is_file():
        raw_tables = [json.loads(line) for line in index_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        for raw_table in raw_tables:
            table = table_by_id.get(raw_table["table_id"])
            if table and raw_table.get("status") == "success":
                raw_table.update({key: table[key] for key in ("cells", "grid", "cell_locations", "bbox_source",
                                 "bbox_padded", "crop_to_pdf", "width_px", "height_px") if key in table})
                _write_json(output / "table_ocr" / f"{raw_table['table_id']}.json", raw_table)
        _write_text(index_path, "".join(json.dumps(table, ensure_ascii=False, allow_nan=False) + "\n" for table in raw_tables))
    return summary


def _document_layout_stage(source_pdf, output_dir, identity, options, isolated):
    """并行模式下先在独立进程完成版面阶段，退出后释放检测模型的显存。"""
    if isolated:
        with ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("spawn")) as pool:
            return pool.submit(extract_pdf, source_pdf, output_dir, document_id=identity, **options).result()
    return extract_pdf(source_pdf, output_dir, document_id=identity, **options)


def run_pdf2doc(pdf_path, output_dir, *, document_id=None, config=None, layout_options=None,
                table_backend="vl", table_batch_size=1, table_ocr=True, ocr_parallel=True, table_workers=1):
    """统一执行步骤 1–6；输出目录必须全新或为空。"""
    output = Path(output_dir).expanduser().resolve()
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ValueError(f"统一流程输出目录必须是新目录或空目录：{output}")
    config = config or OCRConfig()
    if table_backend not in OCR_BACKENDS:
        raise ValueError(f"未知表格 OCR backend：{table_backend}")
    if isinstance(table_batch_size, bool) or not isinstance(table_batch_size, int) or table_batch_size < 1:
        raise ValueError("表格 OCR 提交批大小必须为正整数")
    if isinstance(table_workers, bool) or not isinstance(table_workers, int) or table_workers < 1:
        raise ValueError("table_workers 必须为正整数")
    options = dict(layout_options or {})
    for reserved in ("pdf_path", "output_dir", "document_id"):
        if reserved in options:
            raise ValueError(f"layout_options 不应包含 {reserved}")
    source = load_pdf(pdf_path)
    identity = document_name(Path(source["source_pdf"]), document_id)
    source["document_id"] = identity
    output.mkdir(parents=True, exist_ok=True)
    started = perf_counter()
    report = {"document_id": identity, "source_pdf": source["source_pdf"],
              "source_pdf_sha256": source["source_pdf_sha256"], "status": "running",
              "started_at_utc": _now(), "phases": []}
    def phase(name, function):
        print(f"pdf2doc stage={name}", flush=True)
        tick = perf_counter()
        try:
            return function()
        finally:
            report["phases"].append({"name": name, "seconds": perf_counter() - tick})
            _write_json(output / "run_summary.json", report)
    try:
        _write_json(output / "source_info.json", source)
        device = _resolve_device(config.device)
        options.setdefault("device", device)
        layout = phase("orientation_and_layout", lambda: _document_layout_stage(
            source["source_pdf"], output / "layout", identity, options, isolated=ocr_parallel))
        orientation_path = output / "layout" / "page_orientation.json"
        orientation = validate_preparation(Path(source["source_pdf"]), Path(layout["processing_pdf"]),
                                           orientation_path) if layout["orientation"]["manifest"] else None
        recognition = phase("recognition", lambda: recognize_content(
            layout, output / "layout", output, config=replace(config, device=device),
            table_backend=table_backend, table_batch_size=table_batch_size, table_ocr=table_ocr,
            parallel=ocr_parallel, table_workers=table_workers))
        document = phase("assembly", lambda: assemble_document(source, layout, recognition, orientation=orientation))
        summary = phase("export", lambda: export_document(document, output))
        report["status"] = summary["status"]
        return summary
    except BaseException as exc:
        report.update(status="interrupted" if isinstance(exc, KeyboardInterrupt) else "failed",
                      error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        report.update(finished_at_utc=_now(), wall_seconds=perf_counter() - started)
        _write_json(output / "run_summary.json", report)


def main(argv=None):
    """默认运行完整文档流程，独立阶段保留显式命令入口。"""
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in {"doctor", "locate", "_layout-worker"}:
        return layout_main(argv)
    if argv and argv[0] == "ocr":
        return page_ocr_main(argv[1:])
    if argv and argv[0] == "table-ocr":
        return table_ocr_main(argv[1:])
    if "--layout-only" in argv:
        argv.remove("--layout-only")
        return layout_crop_main(argv)
    parser = build_crop_parser()
    parser.description = "PDF → 方向校正 → 版面与裁图 → OCR → 结构组装 → JSON/Markdown/文本"
    parser.add_argument("--layout-only", action="store_true", help="只运行方向校正、版面分析和裁图")
    parser.add_argument("--backend", choices=OCR_BACKENDS, default="vl", help="整页 OCR 后端")
    parser.add_argument("--ocr-model-root", type=Path)
    parser.add_argument("--layout-model-dir", type=Path, help="整页 VL 的版面权重；默认沿用 --pp-model-dir")
    parser.add_argument("--vl-model-dir", type=Path)
    parser.add_argument("--max-new-tokens", type=int, default=4096)
    parser.add_argument("--max-side", type=int, default=2600)
    parser.add_argument("--batch-size", type=int, default=6)
    parser.add_argument("--text-workers", "--workers", dest="text_workers", type=int, default=1)
    parser.add_argument("--render-prefetch", type=int, default=0)
    parser.add_argument("--cpu-threads", type=int, default=4)
    parser.add_argument("--low-confidence", type=float, default=0.8)
    parser.add_argument("--table-backend", choices=OCR_BACKENDS, default="vl")
    parser.add_argument("--table-batch-size", type=int, default=1)
    parser.add_argument("--table-workers", type=int, default=1)
    parser.add_argument("--ocr-execution", choices=["parallel", "serial"], default="parallel",
                        help="正文与表格 OCR 默认分进程并行；显存受限时可选择 serial")
    parser.add_argument("--no-table-ocr", action="store_true", help="跳过裁图 OCR，保留整页 OCR 中已有的表格结构")
    values = vars(parser.parse_args(argv))
    try:
        values.pop("layout_only")
        table_backend = values.pop("table_backend")
        table_batch_size = values.pop("table_batch_size")
        table_workers = values.pop("table_workers")
        ocr_parallel = values.pop("ocr_execution") == "parallel"
        table_ocr = not values.pop("no_table_ocr")
        model_root, vl_model = values.pop("ocr_model_root"), values.pop("vl_model_dir")
        layout_model = values.pop("layout_model_dir") or values["pp_model_dir"]
        config = OCRConfig(
            device=values["device"], backend=values.pop("backend"),
            max_side=values.pop("max_side"), batch_size=values.pop("batch_size"),
            workers=values.pop("text_workers"), render_prefetch=values.pop("render_prefetch"),
            cpu_threads=values.pop("cpu_threads"), low_confidence=values.pop("low_confidence"),
            model_root=str(model_root) if model_root else None,
            layout_model_dir=str(layout_model) if layout_model else None,
            vl_model_dir=str(vl_model) if vl_model else None,
            max_new_tokens=values.pop("max_new_tokens"),
        )
        args = _crop_arguments(values)
        source, output, identity = args.pop("pdf_path"), args.pop("output_dir"), args.pop("document_id")
        summary = run_pdf2doc(source, output, document_id=identity, config=config, layout_options=args,
                              table_backend=table_backend, table_batch_size=table_batch_size, table_ocr=table_ocr,
                              ocr_parallel=ocr_parallel, table_workers=table_workers)
    except KeyboardInterrupt:
        print("PDF 处理已中断，已完成的阶段产物保留。", flush=True)
        return 130
    except (OSError, ValueError, RuntimeError) as exc:
        parser.exit(1, f"PDF 转结构化文档失败：{exc}\n")
    print(f"{summary['document_id']}：状态={summary['status']}，"
          f"页面 {summary['successful_pages']}/{summary['selected_pages']}，"
          f"表格 {summary['tables']}；产物：{output.resolve()}", flush=True)
    return int(summary["status"] != "complete")


if __name__ == "__main__":
    raise SystemExit(main())
