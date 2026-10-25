"""PDF 整页文档解析，默认本地 PaddleOCR-VL-1.6；--backend paddle 可选普通 OCR。

示例（默认识别全部页面，重复执行自动复用成功页）：
    python src/paten_ocr.py --input data/WO2025108394A1/source.pdf \
        --output-dir results/pdf_ocr/WO2025108394A1 --device auto

输出 document.json、pages/page-XXXX.json、pages.jsonl、document.txt、
document.md、summary.json 和页面图片。VL 另存原始 JSON、逐页 Markdown 和区域图片，
保留版面类别、阅读顺序及表格 HTML；兼容 lines 是区块投影，不提供虚构置信度。
不读取 PDF 文字层，不调用远程模型服务，不做序列/实验语义提取。
"""

from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import importlib.metadata
import json
import math
import multiprocessing
import os
from pathlib import Path
import re
import tempfile
from time import perf_counter


VERSION = 2
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
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


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


def parse_pages(values, total_pages):
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
        from document_vl import LAYOUT_MODEL, VL_MODEL, model_paths

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


def _create_ocr(config, device, model_root):
    os.environ.setdefault("PADDLE_PDX_ENABLE_MKLDNN_BYDEFAULT", "false")
    os.environ.setdefault("FLAGS_use_mkldnn", "0")
    os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "true")
    import paddle

    if device.startswith("gpu:"):
        index = int(device.split(":")[1])
        if not paddle.is_compiled_with_cuda() or index >= paddle.device.cuda.device_count():
            raise RuntimeError(f"当前 Paddle 环境无法使用 {device}；可指定 --device cpu 或使用 GPU 环境")
    if config.backend == "vl":
        from document_vl import create_engine

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
    from document_vl import normalise_result, rebase_markdown

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
                _WORKER_ENGINE = _create_ocr(config, device, Path(model_root))
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
            from document_vl import plain_text

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
        from document_vl import LAYOUT_MODEL, VL_MODEL, model_paths

        layout, vl = model_paths(config, model_root)
        settings.update(layout_model=LAYOUT_MODEL, vl_model=VL_MODEL, pipeline_version="v1.6",
                        layout_model_dir=str(layout), vl_model_dir=str(vl),
                        use_layout_detection=True, use_chart_recognition=False,
                        use_queues=False, format_block_content=False)
    else:
        settings.update(text_detector=DETECTOR, text_recognizer=RECOGNIZER, text_rec_score_thresh=0.0)
    key = _digest({"version": VERSION, "pdf_sha256": source_hash,
                   "document_id": document_id, "settings": {k: v for k, v in settings.items()
                                                               if k not in {"model_root", "layout_model_dir", "vl_model_dir", "render_prefetch", "workers"}},
                   "models": model_hashes, "runtime": versions})
    with pymupdf.open(pdf_path) as pdf:
        if pdf.needs_pass or not len(pdf):
            raise ValueError("PDF 需要密码或没有页面")
        selected = parse_pages(pages, len(pdf))
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
            manifest = {"schema_version": VERSION, "producer": "SDA/paten_ocr", "document_id": document_id,
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
                cached = None if force else _cached_page(output_dir / "pages" / f"page-{number:04d}.json", key, document_id, number)
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
                                        engine = _create_ocr(config, device, model_root)
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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
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
    output = args.output_dir or Path(__file__).resolve().parents[1] / "results/pdf_ocr" / identity
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


if __name__ == "__main__":
    raise SystemExit(main())
