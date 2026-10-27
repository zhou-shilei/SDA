"""从 PDF 定位表格/插图并高清裁图；默认使用 PP-DocLayoutV3。

检测、框融合、串行/并行推理和裁图统一放在本文件。
可选 YOLO、TATR 后端；传入 --regions-json 可复用检测结果，跳过模型推理。
示例：python src/pdf2fig.py --input patent.pdf --output-dir data/pdf2fig/demo
"""


from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import queue
import re
import subprocess
import sys
import tempfile
import time
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from time import perf_counter
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
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

    try:
        import pymupdf as fitz  # type: ignore
    except ImportError:
        try:
            import fitz  # type: ignore
        except ImportError as exc:  # pragma: no cover（取决于运行环境）
            raise RuntimeError(
                "PDF rendering requires PyMuPDF; install SDA/environment.yml."
            ) from exc

    if isinstance(dpi, bool) or not isinstance(dpi, int) or dpi <= 0:
        raise ValueError("dpi must be a positive integer")
    pdf_path = Path(pdf_path).expanduser().resolve()
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    requested = set(pages) if pages is not None else None
    scale = float(dpi) / 72.0
    document = fitz.open(pdf_path)
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
            pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
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

def _sha256(path: Path, chunk_size: int = 1024 * 1024) -> str:
    """分块计算文件哈希，用于核对源 PDF 与输出图片。"""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


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
    """延迟导入 PyMuPDF，兼容 pymupdf 和 fitz 两种模块名称。"""
    try:
        import pymupdf as fitz  # type: ignore
    except ImportError:
        try:
            import fitz  # type: ignore
        except ImportError as exc:  # pragma: no cover（取决于运行环境）
            raise RuntimeError(
                "Table cropping requires PyMuPDF. Install "
                "SDA/environment.yml"
            ) from exc
    return fitz


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
    command = [job['python'], str(ROOT / 'src/pdf2fig.py'), '_layout-worker', '--job', str(job_path)]
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
                from document.pdf_orientation import validate_preparation
                preparation = validate_preparation(pdf, prepared, orientation_path)
                if _sha256(prepared) != payload["document"]["sha256"]:
                    raise ValueError("Saved regions do not belong to the prepared PDF")
                import shutil
                out.mkdir(parents=True, exist_ok=True)
                processing_pdf = out / "prepared.pdf"
                shutil.copyfile(prepared, processing_pdf)
                save_json(out / "page_orientation.json", preparation)
        payload = _validate_layout(payload, processing_pdf, pages, _sha256(processing_pdf), total_pages)
        reused = {"file": str(regions_path), "sha256": _sha256(regions_path)}
    else:
        # 3. 先校正页面方向，再用低分辨率整页图检测表格或插图。
        if orientation == "auto":
            from document.pdf_orientation import prepare_pdf
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
    from tables.table_continuation import build_table_contexts
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


def main(argv=None):
    """解析裁图参数并执行主流程；保留辅助命令和内部推理入口。"""
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in {"doctor", "locate", "_layout-worker"}:
        return layout_main(argv)
    parser = argparse.ArgumentParser(description=__doc__)
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
    args = vars(parser.parse_args(argv))
    args["model_dirs"] = {backend: args.pop(option) for backend, option in [
        ("pp_doclayoutv3", "pp_model_dir"), ("doclayout_yolo", "yolo_model"), ("tatr", "tatr_model_dir")]}
    args["model_dirs"] = {k: v for k, v in args["model_dirs"].items() if v is not None}
    args["thresholds"] = {backend: args.pop(option) for backend, option in [
        ("pp_doclayoutv3", "pp_threshold"), ("doclayout_yolo", "yolo_threshold"), ("tatr", "tatr_threshold")]}
    try:
        args["pages"] = parse_pages(args["pages"])
        result = extract_pdf(**args)
    except (OSError, ValueError, RuntimeError) as exc:
        parser.exit(1, f"PDF 裁图失败：{exc}\n")
    print(f"{result['document_id']}：共 {result['image_count']} 张裁图，状态={result['status']}，"
          f"耗时 {result['wall_seconds']:.3f} 秒；保存至 {args['output_dir'].resolve()}")
    return int(result["status"] != "complete")


if __name__ == "__main__":
    raise SystemExit(main())
