"""Local full-page VL parsing helpers; preserve model text and table markup."""

import math
import os
from pathlib import Path
import re


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
