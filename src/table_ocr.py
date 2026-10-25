"""默认用 PaddleOCR-VL-1.6 返回表格 HTML；可选普通 OCR 按行返回文字。"""

import os
import copy
import hashlib
import importlib.metadata
from functools import lru_cache
from pathlib import Path
from time import perf_counter

# CPU 回退时关闭 oneDNN，避免 Paddle 推理兼容性问题。
os.environ.setdefault("PADDLE_PDX_ENABLE_MKLDNN_BYDEFAULT", "false")
os.environ.setdefault("FLAGS_use_mkldnn", "0")

# VL 固定使用指定设备；普通 OCR 保留 GPU 失败后回退 CPU 的行为。
_ocr_device = "gpu:0"
_cpu_threads = 12
_ocr_backend = "vl"
_ocr_batch_size = 1
OCR_BACKENDS = ("vl", "paddle")


def configure_ocr_worker(device="gpu:0", cpu_threads=4, backend="vl", batch_size=1):
    """在 spawn 创建的独立进程中设置参数，模型在第一次识别时初始化。"""
    global _ocr_device, _cpu_threads, _ocr_backend, _ocr_batch_size
    if backend not in OCR_BACKENDS:
        raise ValueError(f"未知 OCR backend：{backend}；可选 {OCR_BACKENDS}")
    if not isinstance(batch_size, int) or isinstance(batch_size, bool) or batch_size < 1:
        raise ValueError("表格 OCR 提交批大小必须为正整数")
    _ocr_device = device
    _cpu_threads = cpu_threads
    _ocr_backend = backend
    _ocr_batch_size = batch_size
    get_ocr.cache_clear()


def _create_ocr(device):
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
        return _create_ocr(_ocr_device)

    try:
        if _ocr_device.startswith("gpu:"):
            device_index = int(_ocr_device.split(":", 1)[1])
            if device_index >= paddle.device.cuda.device_count():
                raise RuntimeError(f"设备 {_ocr_device} 不存在")
            return _create_ocr(_ocr_device)
    except Exception as exc:
        print(f"GPU 不可用，改用 CPU：{exc}")

    _ocr_device = "cpu"
    return _create_ocr("cpu")


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
    ocr = get_ocr()
    initialized = perf_counter()
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
    if len(outputs) != len(paths):
        raise ValueError("OCR batch returned fewer results than input images")
    runtime = {**ocr_runtime(ocr), "submission_size": len(paths),
               "initialization_seconds": initialized - started,
               "inference_wall_seconds": perf_counter() - initialized}
    for output in outputs.values():
        output["batch_runtime"] = runtime
    return [outputs[p] for p in paths]


def structure_table_evidence(text, image_path, metadata=None, *, blocks=None, lines=None):
    """HTML cell addresses are structural; never invent cell bounding boxes."""
    from table_continuation import parse_grid

    metadata = metadata or {}
    path = Path(image_path).resolve()
    table_id = metadata.get("id") or path.stem
    grid = parse_grid(text, table_id)
    try:
        version = importlib.metadata.version("paddleocr")
    except importlib.metadata.PackageNotFoundError:
        version = None
    return {"version": 1, "document_id": metadata.get("document_id"),
            "table_id": table_id, "page": metadata.get("page"),
            "bbox": metadata.get("bbox"), "coordinate_space": "pdf_points",
            "image_path": str(path), "image_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "backend": _ocr_backend, "backend_version": version, "device": _ocr_device,
            "text": text, "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "cells": [cell for row in grid["physical"] for cell in row] if grid else [],
            "grid": grid["rows"] if grid else [], "blocks": blocks or [], "lines": lines or []}


def extract_table_text(image_path):
    """Compatibility API: return the original HTML/plain text without rewriting."""
    return extract_table_evidence(image_path)["text"]


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image_path", type=Path, nargs="?",
                        default=Path(__file__).resolve().parents[1] / "data" / "table.png")
    parser.add_argument("--backend", choices=OCR_BACKENDS, default="vl")
    parser.add_argument("--device", default="gpu:0")
    parser.add_argument("--cpu-threads", type=int, default=4)
    args = parser.parse_args()
    configure_ocr_worker(args.device, args.cpu_threads, args.backend)
    table_text = extract_table_text(args.image_path)
    print("\n识别出的表格文字：")
    print(table_text)
