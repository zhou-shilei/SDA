#!/usr/bin/env python3
"""Extract OCR text and table rows from PP-DocLayoutV3 table crops.

The layout model and the cropper deliberately stay separate from this module.
This module consumes the deterministic PNG crops and produces four auditable
artifacts:

* a Markdown report with the source PNG embedded for every table;
* a SQLite database containing tables, rows, cells and OCR boxes;
* CSV exports of rows and cells;
* the unmodified OCR boxes and the inferred grid in JSON.

PaddleOCR is preferred when it is installed.  Tesseract is a dependency-light
fallback that is useful on machines where PaddleOCR model files are not yet
available.  Table structure is inferred locally from grid lines and OCR box
alignment, so this stage does not run a second layout detector over an already
cropped table.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median
from typing import Any, Sequence
from urllib.parse import quote
from .ocr_runtime import find_tesseract, tesseract_env


try:
    import cv2  # type: ignore
    import numpy as np  # type: ignore
except ImportError as exc:  # pragma: no cover - exercised by the CLI doctor
    raise SystemExit(
        "table_ocr.py requires opencv-python and numpy; install requirements-models.txt"
    ) from exc


IMAGE_RE = re.compile(r"table-(?P<index>\d+)-page-(?P<page>\d+)", re.IGNORECASE)


@dataclass
class OCRItem:
    """One text line returned by an OCR backend."""

    text: str
    score: float
    bbox: list[float]
    backend: str
    item_index: int = -1

    @property
    def x1(self) -> float:
        return self.bbox[0]

    @property
    def y1(self) -> float:
        return self.bbox[1]

    @property
    def x2(self) -> float:
        return self.bbox[2]

    @property
    def y2(self) -> float:
        return self.bbox[3]

    @property
    def cx(self) -> float:
        return (self.x1 + self.x2) / 2.0

    @property
    def cy(self) -> float:
        return (self.y1 + self.y2) / 2.0

    @property
    def height(self) -> float:
        return max(1.0, self.y2 - self.y1)

    def as_dict(self) -> dict[str, Any]:
        return {
            "item_index": self.item_index,
            "text": self.text,
            "score": round(float(self.score), 6),
            "bbox": [round(float(x), 2) for x in self.bbox],
            "backend": self.backend,
        }


@dataclass
class Cell:
    row_index: int
    col_index: int
    text: str = ""
    confidence: float = 0.0
    bbox: list[float] = field(default_factory=list)
    item_indices: list[int] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "row_index": self.row_index,
            "col_index": self.col_index,
            "text": self.text,
            "confidence": round(float(self.confidence), 6),
            "bbox": [round(float(x), 2) for x in self.bbox],
            "item_indices": self.item_indices,
        }


@dataclass
class TableResult:
    table_index: int
    image: str
    page: int | None
    source_id: str | None
    crop_confidence: float | None
    width: int
    height: int
    backend: str
    caption: str
    items: list[OCRItem]
    rows: list[list[Cell]]
    horizontal_lines: list[float]
    vertical_lines: list[float]
    mode: str

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "TableResult":
        """Restore persisted evidence identically for checkpoints and workers."""
        return cls(
            table_index=int(raw["table_index"]), image=str(raw["image"]),
            page=int(raw["page"]) if raw.get("page") is not None else None,
            source_id=str(raw["source_id"]) if raw.get("source_id") is not None else None,
            crop_confidence=float(raw["crop_confidence"]) if raw.get("crop_confidence") is not None else None,
            width=int(raw["width"]), height=int(raw["height"]), backend=str(raw["backend"]),
            caption=str(raw.get("caption", "")),
            items=[OCRItem(text=str(item.get("text", "")), score=float(item.get("score", 0)),
                           bbox=[float(v) for v in item.get("bbox", [])],
                           backend=str(item.get("backend", raw["backend"])),
                           item_index=int(item.get("item_index", -1))) for item in raw.get("ocr_items", [])],
            rows=[[Cell(row_index=int(cell.get("row_index", r)), col_index=int(cell.get("col_index", c)),
                        text=str(cell.get("text", "")), confidence=float(cell.get("confidence", 0)),
                        bbox=[float(v) for v in cell.get("bbox", [])],
                        item_indices=[int(v) for v in cell.get("item_indices", [])])
                   for c, cell in enumerate(row)] for r, row in enumerate(raw.get("rows", []))],
            horizontal_lines=[float(v) for v in raw.get("horizontal_lines", [])],
            vertical_lines=[float(v) for v in raw.get("vertical_lines", [])], mode=str(raw.get("mode", "alignment")),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "table_index": self.table_index,
            "image": self.image,
            "page": self.page,
            "source_id": self.source_id,
            "crop_confidence": self.crop_confidence,
            "width": self.width,
            "height": self.height,
            "backend": self.backend,
            "mode": self.mode,
            "caption": self.caption,
            "horizontal_lines": [round(float(x), 2) for x in self.horizontal_lines],
            "vertical_lines": [round(float(x), 2) for x in self.vertical_lines],
            "ocr_items": [item.as_dict() for item in self.items],
            "rows": [[cell.as_dict() for cell in row] for row in self.rows],
        }


def _clean_text(value: Any) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\r", " ").replace("\n", " ")
    return re.sub(r"[ \t]+", " ", text).strip()


def _bbox_from_polygon(poly: Any) -> list[float] | None:
    try:
        points = np.asarray(poly, dtype=float).reshape(-1, 2)
    except (TypeError, ValueError):
        return None
    if len(points) == 0:
        return None
    x1, y1 = points.min(axis=0)
    x2, y2 = points.max(axis=0)
    return [float(x1), float(y1), float(x2), float(y2)]


def _result_value(result: Any, key: str, default: Any = None) -> Any:
    try:
        return result[key]
    except (KeyError, TypeError, IndexError):
        return getattr(result, key, default)


class PaddleOCRBackend:
    """PaddleOCR text detector/recognizer for an already-cropped table."""

    name = "paddleocr"

    def __init__(self, lang: str, device: str | None, max_side: int, rec_batch_size: int | None = None) -> None:
        if rec_batch_size is not None and rec_batch_size < 1:
            raise ValueError("OCR recognition batch size must be positive")
        # PaddleX reads these flags during import.  Without this setting some
        # Paddle 3.x CPU builds select oneDNN and fail on OCRv6 PIR attributes.
        os.environ.setdefault("PADDLE_PDX_ENABLE_MKLDNN_BYDEFAULT", "false")
        os.environ.setdefault("FLAGS_use_mkldnn", "0")
        os.environ.setdefault("PADDLE_PDX_CPU_NUM_THREADS", "4")
        from paddleocr import PaddleOCR  # type: ignore

        kwargs: dict[str, Any] = {
            "lang": lang,
            "use_doc_orientation_classify": False,
            "use_doc_unwarping": False,
            "use_textline_orientation": False,
            "text_det_limit_side_len": max_side,
            "text_det_limit_type": "max",
        }
        # Preserve the installed pipeline's default unless explicitly tuned.
        # Recognition padding changes with batch composition and can affect text.
        if rec_batch_size is not None:
            kwargs["text_recognition_batch_size"] = rec_batch_size
        if device:
            kwargs["device"] = device
        if os.environ.get("DATASETAGENT_OCR_CPU_THREADS"):
            kwargs["cpu_threads"] = int(os.environ["DATASETAGENT_OCR_CPU_THREADS"])
        self.engine = PaddleOCR(**kwargs)

    def recognize(self, image_path: Path) -> list[OCRItem]:
        results = self.engine.predict(str(image_path))
        if not results:
            return []
        result = results[0]
        raw_texts = _result_value(result, "rec_texts", [])
        raw_scores = _result_value(result, "rec_scores", [])
        texts = list(raw_texts) if raw_texts is not None else []
        scores = list(raw_scores) if raw_scores is not None else []
        raw_polygons = _result_value(result, "rec_polys", None)
        polygons = list(raw_polygons) if raw_polygons is not None else []
        if len(polygons) == 0:
            raw_boxes = _result_value(result, "rec_boxes", [])
            polygons = list(raw_boxes) if raw_boxes is not None else []
        items: list[OCRItem] = []
        for index, text in enumerate(texts):
            cleaned = _clean_text(text)
            if not cleaned or index >= len(polygons):
                continue
            bbox = _bbox_from_polygon(polygons[index])
            if bbox is None:
                continue
            score = float(scores[index]) if index < len(scores) else 0.0
            items.append(OCRItem(cleaned, score, bbox, self.name, len(items)))
        return items


class TesseractBackend:
    """Tesseract TSV fallback, intentionally invoked as an external binary."""

    name = "tesseract"

    def __init__(self, lang: str, psm: int) -> None:
        self.lang = lang
        self.psm = psm
        self.executable = find_tesseract()
        if self.executable is None:
            raise RuntimeError("tesseract executable was not found on PATH")
        self.env = tesseract_env(self.executable, lang)
        try:
            probe = subprocess.run([self.executable, "--list-langs"], capture_output=True, text=True, env=self.env, timeout=10)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"Tesseract cannot start: {exc}") from exc
        languages = set(probe.stdout.splitlines())
        if probe.returncode or not set(lang.split("+")) <= languages:
            raise RuntimeError(f"Tesseract language initialization failed ({lang}): {probe.stderr.strip() or probe.stdout.strip()}")

    def recognize(self, image_path: Path) -> list[OCRItem]:
        command = [
            self.executable,
            str(image_path),
            "stdout",
            "--psm",
            str(self.psm),
            "-l",
            self.lang,
            "tsv",
        ]
        # Normalize raster encoding and suppress only long table rules.
        # Dimensions stay identical so all returned boxes remain in the
        # original crop's coordinate system; the source image is untouched.
        with tempfile.TemporaryDirectory(prefix="dataagent-tesseract-") as temporary:
            gray = cv2.imread(str(image_path), cv2.IMREAD_GRAYSCALE)
            if gray is None:
                raise RuntimeError(f"Cannot read OCR image: {image_path}")
            height, width = gray.shape
            binary = cv2.threshold(gray, 210, 255, cv2.THRESH_BINARY_INV)[1]
            horizontal = cv2.morphologyEx(binary, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (max(80, width // 5), 1)))
            vertical = cv2.morphologyEx(binary, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(80, height // 4))))
            gray[cv2.bitwise_or(horizontal, vertical) > 0] = 255
            normalized = Path(temporary) / "normalized.png"
            if not cv2.imwrite(str(normalized), gray):
                raise RuntimeError("Could not prepare the Tesseract input image")
            command[1] = str(normalized)
            completed = subprocess.run(command, capture_output=True, text=True, check=False, env=self.env)
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or "tesseract failed")
        items: list[OCRItem] = []
        for row in csv.DictReader(completed.stdout.splitlines(), delimiter="\t"):
            text = _clean_text(row.get("text"))
            if not text:
                continue
            try:
                score = max(0.0, float(row.get("conf", -1.0))) / 100.0
                x1 = float(row["left"])
                y1 = float(row["top"])
                x2 = x1 + float(row["width"])
                y2 = y1 + float(row["height"])
            except (KeyError, TypeError, ValueError):
                continue
            items.append(OCRItem(text, score, [x1, y1, x2, y2], self.name, len(items)))
        return items


def make_backend(
    backend: str,
    lang: str = "en",
    device: str | None = None,
    max_side: int = 2200,
    rec_batch_size: int | None = None,
) -> Any:
    """Create an OCR backend, with deterministic fallback for ``auto``."""

    if backend == "tesseract":
        return TesseractBackend("eng", 6)
    if backend == "paddleocr":
        return PaddleOCRBackend(lang, device, max_side, rec_batch_size)
    try:
        return PaddleOCRBackend(lang, device, max_side, rec_batch_size)
    except Exception as paddle_error:
        if device and device.startswith(("gpu", "cuda")):
            raise RuntimeError(f"Requested GPU OCR failed: {paddle_error}") from paddle_error
        try:
            return TesseractBackend("eng", 6)
        except Exception as tesseract_error:
            raise RuntimeError(
                "Neither PaddleOCR nor Tesseract is available: "
                f"PaddleOCR={paddle_error}; Tesseract={tesseract_error}"
            ) from tesseract_error


def _line_centers(mask: np.ndarray, axis_length: int, min_fraction: float) -> list[float]:
    """Return centers of long connected line bands in a binary mask."""

    projection = (mask > 0).sum(axis=1 if mask.shape[1] == axis_length else 0)
    threshold = max(8, int(axis_length * min_fraction))
    indexes = np.flatnonzero(projection >= threshold)
    if len(indexes) == 0:
        return []
    groups: list[list[int]] = [[int(indexes[0])]]
    for value in indexes[1:]:
        value = int(value)
        if value <= groups[-1][-1] + 1:
            groups[-1].append(value)
        else:
            groups.append([value])
    return [float(sum(group) / len(group)) for group in groups]


def detect_grid(image: np.ndarray) -> tuple[list[float], list[float]]:
    """Detect long horizontal/vertical rules; works for most patent tables."""

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    binary = cv2.threshold(gray, 210, 255, cv2.THRESH_BINARY_INV)[1]
    height, width = gray.shape[:2]
    horizontal_kernel = cv2.getStructuringElement(
        cv2.MORPH_RECT, (max(20, width // 25), 1)
    )
    vertical_kernel = cv2.getStructuringElement(
        cv2.MORPH_RECT, (1, max(20, height // 25))
    )
    horizontal = cv2.morphologyEx(binary, cv2.MORPH_OPEN, horizontal_kernel)
    vertical = cv2.morphologyEx(binary, cv2.MORPH_OPEN, vertical_kernel)
    # A 25% span threshold tolerates short gaps in rasterized rules while
    # filtering text strokes and sequence characters.
    hs = _line_centers(horizontal, width, 0.25)
    vs = _line_centers(vertical, height, 0.25)
    return hs, vs


def complete_vertical_boundaries(
    vertical: Sequence[float], width: float, items: Sequence[OCRItem]
) -> list[float]:
    """Add missing outer rules when OCR proves that edge columns exist.

    Patent table crops frequently retain the two internal column separators
    while the left and right border is clipped or too faint for morphology.
    Treating the two separators as the complete grid silently discards both
    edge columns.  OCR item centres provide direct evidence for those columns,
    so add a crop-edge boundary only on the side where such evidence exists.
    """

    boundaries = sorted({float(value) for value in vertical})
    if len(boundaries) < 2 or not items:
        return boundaries
    padding = max(2.0, float(width) * 0.005)
    if any(item.cx < boundaries[0] for item in items):
        left = max(0.0, min(item.x1 for item in items) - padding)
        if left < boundaries[0]:
            boundaries.insert(0, left)
    if any(item.cx > boundaries[-1] for item in items):
        right = min(float(width), max(item.x2 for item in items) + padding)
        if right > boundaries[-1]:
            boundaries.append(right)
    return boundaries


def _vertical_overlap(a: OCRItem, y1: float, y2: float) -> float:
    return max(0.0, min(a.y2, y2) - max(a.y1, y1))


def _cluster_rows(items: Sequence[OCRItem]) -> list[list[OCRItem]]:
    """Cluster OCR lines into visual rows without assuming a table grid."""

    if not items:
        return []
    ordered = sorted(items, key=lambda item: (item.cy, item.x1))
    typical_height = median(item.height for item in ordered)
    rows: list[list[OCRItem]] = []
    bounds: list[list[float]] = []
    for item in ordered:
        best = -1
        best_overlap = 0.0
        for index, (y1, y2) in enumerate(bounds):
            overlap = _vertical_overlap(item, y1, y2)
            close = abs(item.cy - (y1 + y2) / 2.0) <= max(typical_height * 0.65, 5.0)
            if overlap >= min(item.height, y2 - y1) * 0.25 or close:
                if overlap > best_overlap:
                    best, best_overlap = index, overlap
        if best < 0:
            rows.append([item])
            bounds.append([item.y1, item.y2])
        else:
            rows[best].append(item)
            bounds[best][0] = min(bounds[best][0], item.y1)
            bounds[best][1] = max(bounds[best][1], item.y2)
    paired = sorted(zip(bounds, rows), key=lambda pair: pair[0][0])
    return [sorted(row, key=lambda item: item.x1) for _, row in paired]


def _join_items(items: Sequence[OCRItem]) -> tuple[str, float, list[float]]:
    if not items:
        return "", 0.0, []
    ordered = sorted(items, key=lambda item: (item.y1, item.x1))
    lines: list[str] = []
    current: list[str] = []
    current_y: float | None = None
    typical_height = median(item.height for item in ordered)
    for item in ordered:
        if current_y is None or abs(item.cy - current_y) <= max(typical_height * 0.7, 4.0):
            current.append(item.text)
            current_y = item.cy if current_y is None else (current_y + item.cy) / 2.0
        else:
            lines.append(" ".join(current))
            current = [item.text]
            current_y = item.cy
    if current:
        lines.append(" ".join(current))
    x1 = min(item.x1 for item in ordered)
    y1 = min(item.y1 for item in ordered)
    x2 = max(item.x2 for item in ordered)
    y2 = max(item.y2 for item in ordered)
    return "<br>".join(_clean_text(line) for line in lines), sum(
        item.score for item in ordered
    ) / len(ordered), [x1, y1, x2, y2]


def _make_cell(row: int, col: int, items: Sequence[OCRItem], bbox: list[float]) -> Cell:
    text, confidence, text_bbox = _join_items(items)
    return Cell(
        row_index=row,
        col_index=col,
        text=text,
        confidence=confidence,
        bbox=text_bbox or bbox,
        item_indices=[item.item_index for item in items],
    )


def _grid_cells(
    items: Sequence[OCRItem], horizontal: Sequence[float], vertical: Sequence[float]
) -> tuple[list[list[Cell]], list[OCRItem], str]:
    """Assign OCR boxes to cells between detected rules."""

    rows: list[list[Cell]] = []
    used: set[int] = set()
    for row_index, (top, bottom) in enumerate(zip(horizontal, horizontal[1:])):
        row: list[Cell] = []
        for col_index, (left, right) in enumerate(zip(vertical, vertical[1:])):
            selected = [
                item
                for item in items
                if (left <= item.cx < right or (col_index == len(vertical) - 2 and item.cx == right))
                and (top <= item.cy < bottom or (row_index == len(horizontal) - 2 and item.cy == bottom))
            ]
            used.update(item.item_index for item in selected)
            row.append(_make_cell(row_index, col_index, selected, [left, top, right, bottom]))
        rows.append(row)
    outside = [item for item in items if item.item_index not in used]
    return rows, outside, "grid"


def _alignment_cells(items: Sequence[OCRItem]) -> list[list[Cell]]:
    """Best-effort structure for wireless/no-rule tables.

    Repeated x-start positions are treated as column anchors.  If the page has
    no stable anchors, a one-column reading-order table is safer than inventing
    cell boundaries.
    """

    visual_rows = _cluster_rows(items)
    if not visual_rows:
        return []
    width = max((item.x2 for item in items), default=1.0)
    typical_height = median(item.height for item in items)
    tolerance = max(18.0, width * 0.025, typical_height * 2.5)
    starts = sorted(item.x1 for item in items)
    clusters: list[list[float]] = []
    for start in starts:
        if not clusters or start - median(clusters[-1]) > tolerance:
            clusters.append([start])
        else:
            clusters[-1].append(start)
    # A real column usually has at least two observations.  Keep the leftmost
    # anchor for short tables so that two-cell headers are not discarded.
    anchors = [median(cluster) for cluster in clusters if len(cluster) >= 2]
    if len(anchors) < 2:
        anchors = [median(cluster) for cluster in clusters]
    if len(anchors) > 12:
        anchors = anchors[:12]
    if len(anchors) <= 1:
        return [
            [_make_cell(row_index, 0, row, [min(i.x1 for i in row), min(i.y1 for i in row), max(i.x2 for i in row), max(i.y2 for i in row)])]
            for row_index, row in enumerate(visual_rows)
        ]
    boundaries = [0.0]
    boundaries.extend((left + right) / 2.0 for left, right in zip(anchors, anchors[1:]))
    boundaries.append(max(width, max(item.x2 for item in items)))
    result: list[list[Cell]] = []
    for row_index, visual_row in enumerate(visual_rows):
        row: list[Cell] = []
        for col_index in range(len(boundaries) - 1):
            left, right = boundaries[col_index : col_index + 2]
            selected = [item for item in visual_row if left <= item.cx < right]
            bbox = [left, min(i.y1 for i in visual_row), right, max(i.y2 for i in visual_row)]
            row.append(_make_cell(row_index, col_index, selected, bbox))
        result.append(row)
    return result


def _outside_caption(items: Sequence[OCRItem], horizontal: Sequence[float]) -> str:
    if not items:
        return ""
    first_line = horizontal[0] if horizontal else float("inf")
    caption_items = [item for item in items if item.cy < first_line]
    if not caption_items:
        return ""
    return " ".join(item.text for item in sorted(caption_items, key=lambda i: (i.cy, i.x1)))


def _crop_metadata(input_dir: Path) -> dict[str, dict[str, Any]]:
    metadata_path = input_dir / "table_crops.json"
    if not metadata_path.is_file():
        return {}
    try:
        values = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {
        str(value.get("image")): value
        for value in values
        if isinstance(value, dict) and value.get("image")
    }


def _image_order(path: Path, metadata: dict[str, dict[str, Any]]) -> tuple[int, int, str]:
    item = metadata.get(path.name, {})
    match = IMAGE_RE.search(path.stem)
    index = int(item.get("index", match.group("index") if match else 10**9))
    page = int(item.get("page", match.group("page") if match else 10**9))
    return index, page, path.name


def extract_one(
    image_path: Path,
    metadata: dict[str, dict[str, Any]],
    backend: Any,
    table_number: int,
) -> TableResult:
    image = cv2.imread(str(image_path))
    if image is None:
        raise RuntimeError(f"unable to read image: {image_path}")
    height, width = image.shape[:2]
    items = backend.recognize(image_path)
    for index, item in enumerate(items):
        item.item_index = index
    horizontal, vertical = detect_grid(image)
    vertical = complete_vertical_boundaries(vertical, width, items)
    # Need at least one data interval. Two rules in each direction can be a
    # one-row/one-column table; shorter detections fall back to alignment.
    if len(horizontal) >= 2 and len(vertical) >= 2:
        rows, outside, mode = _grid_cells(items, horizontal, vertical)
        caption = _outside_caption(outside, horizontal)
    else:
        rows = _alignment_cells(items)
        outside = []
        caption = ""
        mode = "alignment"
    info = metadata.get(image_path.name, {})
    match = IMAGE_RE.search(image_path.stem)
    page_value = info.get("page", match.group("page") if match else None)
    page = int(page_value) if page_value is not None else None
    crop_confidence = info.get("confidence")
    return TableResult(
        table_index=int(info.get("index", table_number)),
        image=image_path.name,
        page=page,
        source_id=info.get("id"),
        crop_confidence=float(crop_confidence) if crop_confidence is not None else None,
        width=width,
        height=height,
        backend=backend.name,
        caption=caption,
        items=items,
        rows=rows,
        horizontal_lines=horizontal,
        vertical_lines=vertical,
        mode=mode,
    )


def _md_cell(value: str) -> str:
    return value.replace("|", r"\|").replace("\n", "<br>")


def _md_image_path(markdown_path: Path, image_path: Path) -> str:
    relative = os.path.relpath(image_path, markdown_path.parent)
    return quote(Path(relative).as_posix(), safe="/_-.()")


def render_markdown(
    results: Sequence[TableResult],
    markdown_path: Path,
    title: str,
    image_root: Path,
    backend_name: str,
    source_pdf: Path | None,
) -> str:
    lines = [f"# {title}", "", "## OCR table extraction", ""]
    lines.append(f"- OCR backend: `{backend_name}`")
    lines.append("- Layout/crop model: `PP-DocLayoutV3` + PyMuPDF crop metadata")
    if source_pdf:
        lines.append(f"- Source PDF: `{source_pdf}`")
    lines.extend(
        [
            "- Each image below is the original high-resolution table crop.",
            "- The companion SQLite database preserves OCR boxes and inferred cells.",
            "",
        ]
    )
    for number, result in enumerate(results, start=1):
        image_path = image_root / result.image
        page = f"page {result.page}" if result.page is not None else "page unknown"
        lines.extend(
            [
                f"## Table {number} — {page}",
                "",
                f"![{result.image}]({_md_image_path(markdown_path, image_path)})",
                "",
                f"- Crop: `{result.image}`",
                f"- Structure mode: `{result.mode}`; rows: {len(result.rows)}; columns: {max((len(row) for row in result.rows), default=0)}",
            ]
        )
        if result.caption:
            lines.append(f"- Caption/OCR outside grid: {_md_cell(result.caption)}")
        lines.append("")
        if result.rows:
            column_count = max(len(row) for row in result.rows)
            padded = [row + [Cell(len(row), col) for col in range(len(row), column_count)] for row in result.rows]
            header = padded[0]
            lines.append("| " + " | ".join(_md_cell(cell.text or f"Column {i + 1}") for i, cell in enumerate(header)) + " |")
            lines.append("| " + " | ".join("---" for _ in range(column_count)) + " |")
            for row in padded[1:]:
                lines.append("| " + " | ".join(_md_cell(cell.text) for cell in row) + " |")
        else:
            lines.append("_No OCR text was returned for this crop._")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _write_csvs(results: Sequence[TableResult], output_dir: Path) -> None:
    rows_path = output_dir / "extracted_rows.csv"
    cells_path = output_dir / "extracted_cells.csv"
    with rows_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["table_index", "page", "image", "row_index", "is_header", "values_json"])
        for result in results:
            for row_index, row in enumerate(result.rows):
                writer.writerow(
                    [
                        result.table_index,
                        result.page or "",
                        result.image,
                        row_index,
                        row_index == 0,
                        json.dumps([cell.text for cell in row], ensure_ascii=False),
                    ]
                )
    with cells_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow([
            "table_index",
            "page",
            "image",
            "row_index",
            "col_index",
            "text",
            "confidence",
            "bbox_json",
        ])
        for result in results:
            for row in result.rows:
                for cell in row:
                    writer.writerow([
                        result.table_index,
                        result.page or "",
                        result.image,
                        cell.row_index,
                        cell.col_index,
                        cell.text,
                        f"{cell.confidence:.6f}",
                        json.dumps(cell.bbox),
                    ])


def _write_sqlite(results: Sequence[TableResult], output_dir: Path, metadata: dict[str, Any]) -> None:
    db_path = output_dir / "tables.sqlite"
    # This database is a derived artifact. Recreate it so a resumed OCR run is
    # idempotent even if the previous process stopped midway through publishing.
    db_path.unlink(missing_ok=True)
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            PRAGMA foreign_keys = ON;
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE tables (
                table_id INTEGER PRIMARY KEY,
                table_index INTEGER NOT NULL,
                image TEXT NOT NULL,
                page INTEGER,
                source_id TEXT,
                crop_confidence REAL,
                width INTEGER NOT NULL,
                height INTEGER NOT NULL,
                backend TEXT NOT NULL,
                structure_mode TEXT NOT NULL,
                caption TEXT
            );
            CREATE TABLE rows (
                row_id INTEGER PRIMARY KEY,
                table_id INTEGER NOT NULL REFERENCES tables(table_id),
                row_index INTEGER NOT NULL,
                is_header INTEGER NOT NULL,
                UNIQUE(table_id, row_index)
            );
            CREATE TABLE cells (
                cell_id INTEGER PRIMARY KEY,
                row_id INTEGER NOT NULL REFERENCES rows(row_id),
                col_index INTEGER NOT NULL,
                text TEXT NOT NULL,
                confidence REAL NOT NULL,
                bbox_json TEXT NOT NULL,
                item_indices_json TEXT NOT NULL,
                UNIQUE(row_id, col_index)
            );
            CREATE TABLE ocr_items (
                item_id INTEGER PRIMARY KEY,
                table_id INTEGER NOT NULL REFERENCES tables(table_id),
                item_index INTEGER NOT NULL,
                text TEXT NOT NULL,
                score REAL NOT NULL,
                bbox_json TEXT NOT NULL,
                backend TEXT NOT NULL,
                UNIQUE(table_id, item_index)
            );
            """
        )
        connection.executemany(
            "INSERT INTO metadata(key, value) VALUES (?, ?)",
            [(str(key), json.dumps(value, ensure_ascii=False)) for key, value in metadata.items()],
        )
        for table_number, result in enumerate(results, start=1):
            cursor = connection.execute(
                """INSERT INTO tables(table_index, image, page, source_id,
                   crop_confidence, width, height, backend, structure_mode, caption)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    result.table_index,
                    result.image,
                    result.page,
                    result.source_id,
                    result.crop_confidence,
                    result.width,
                    result.height,
                    result.backend,
                    result.mode,
                    result.caption,
                ),
            )
            table_id = cursor.lastrowid
            for item in result.items:
                connection.execute(
                    """INSERT INTO ocr_items(table_id, item_index, text, score,
                       bbox_json, backend) VALUES (?, ?, ?, ?, ?, ?)""",
                    (table_id, item.item_index, item.text, item.score, json.dumps(item.bbox), item.backend),
                )
            for row_index, row in enumerate(result.rows):
                cursor = connection.execute(
                    "INSERT INTO rows(table_id, row_index, is_header) VALUES (?, ?, ?)",
                    (table_id, row_index, int(row_index == 0)),
                )
                row_id = cursor.lastrowid
                for cell in row:
                    connection.execute(
                        """INSERT INTO cells(row_id, col_index, text, confidence,
                           bbox_json, item_indices_json) VALUES (?, ?, ?, ?, ?, ?)""",
                        (
                            row_id,
                            cell.col_index,
                            cell.text,
                            cell.confidence,
                            json.dumps(cell.bbox),
                            json.dumps(cell.item_indices),
                        ),
                    )
        connection.commit()


def write_artifacts(
    results: Sequence[TableResult],
    output_dir: Path,
    *,
    title: str,
    markdown_name: str = "tables_ocr.md",
    input_dir: Path,
    image_root: Path,
    backend_name: str,
    source_pdf: Path | None = None,
) -> dict[str, str]:
    """Write the same Markdown/CSV/JSON/SQLite bundle used by the CLI."""

    output_dir.mkdir(parents=True, exist_ok=True)
    ordered = sorted(results, key=lambda result: (result.table_index, result.page or 10**9, result.image))
    markdown_path = output_dir / markdown_name
    markdown_path.write_text(
        render_markdown(ordered, markdown_path, title, image_root, backend_name, source_pdf),
        encoding="utf-8",
    )
    json_path = output_dir / "ocr_results.json"
    json_path.write_text(
        json.dumps([result.as_dict() for result in ordered], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _write_csvs(ordered, output_dir)
    _write_sqlite(
        ordered,
        output_dir,
        {
            "title": title,
            "ocr_backend": backend_name,
            "input_dir": str(input_dir),
            "source_pdf": str(source_pdf) if source_pdf else None,
            "table_count": len(ordered),
            "schema_version": 1,
        },
    )
    return {
        "markdown": str(markdown_path),
        "ocr_results": str(json_path),
        "rows_csv": str(output_dir / "extracted_rows.csv"),
        "cells_csv": str(output_dir / "extracted_cells.csv"),
        "tables_database": str(output_dir / "tables.sqlite"),
    }


def _make_backend(args: argparse.Namespace) -> Any:
    if args.backend == "tesseract":
        return TesseractBackend(args.tesseract_lang, args.tesseract_psm)
    if args.backend == "paddleocr":
        return PaddleOCRBackend(args.lang, args.device, args.max_side, args.rec_batch_size)
    try:
        return PaddleOCRBackend(args.lang, args.device, args.max_side, args.rec_batch_size)
    except Exception as paddle_error:
        if args.device and args.device.startswith(("gpu", "cuda")):
            raise RuntimeError(f"Requested GPU OCR failed: {paddle_error}") from paddle_error
        print(f"warning: PaddleOCR unavailable, falling back to Tesseract: {paddle_error}", file=sys.stderr)
        try:
            return TesseractBackend(args.tesseract_lang, args.tesseract_psm)
        except Exception as tesseract_error:
            raise RuntimeError(
                f"neither PaddleOCR nor Tesseract is usable: {tesseract_error}"
            ) from paddle_error


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="OCR PP-DocLayoutV3 table crops")
    parser.add_argument("--input-dir", required=True, type=Path, help="directory containing table-*.png")
    parser.add_argument("--output-dir", required=True, type=Path, help="directory for Markdown/database artifacts")
    parser.add_argument("--title", default=None, help="Markdown document title")
    parser.add_argument("--markdown-name", default="tables_ocr.md")
    parser.add_argument("--source-pdf", type=Path)
    parser.add_argument("--backend", choices=("auto", "paddleocr", "tesseract"), default="auto")
    parser.add_argument("--lang", default="en", help="PaddleOCR language/model hint")
    parser.add_argument("--device", default=None, help="Paddle device, e.g. cpu or gpu:0")
    parser.add_argument("--max-side", type=int, default=3000, help="PaddleOCR max input side")
    parser.add_argument("--rec-batch-size", type=int, help="text lines per recognition batch; omitted preserves model default")
    parser.add_argument("--tesseract-lang", default="eng")
    parser.add_argument("--tesseract-psm", type=int, default=6)
    parser.add_argument("--no-copy-images", action="store_true", help="link to source PNGs instead of copying them")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    input_dir = args.input_dir.resolve()
    output_dir = args.output_dir.resolve()
    if not input_dir.is_dir():
        raise SystemExit(f"input directory not found: {input_dir}")
    images = sorted(input_dir.glob("*.png"), key=lambda path: _image_order(path, _crop_metadata(input_dir)))
    if not images:
        raise SystemExit(f"no PNG table crops found under {input_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    metadata = _crop_metadata(input_dir)
    backend = _make_backend(args)
    print(f"OCR backend: {backend.name}; crops: {len(images)}", flush=True)
    image_root = output_dir / "tables"
    if args.no_copy_images:
        image_root = input_dir
    else:
        image_root.mkdir(parents=True, exist_ok=True)
    results: list[TableResult] = []
    for number, image_path in enumerate(images, start=1):
        print(f"[{number}/{len(images)}] {image_path.name}", flush=True)
        if not args.no_copy_images:
            shutil.copy2(image_path, image_root / image_path.name)
        result = extract_one(image_path, metadata, backend, number)
        results.append(result)
        print(
            f"  -> {len(result.items)} OCR items, {len(result.rows)} rows, "
            f"{max((len(row) for row in result.rows), default=0)} columns, {result.mode}",
            flush=True,
        )
    results.sort(key=lambda result: (result.table_index, result.page or 10**9, result.image))
    markdown_path = output_dir / args.markdown_name
    title = args.title or input_dir.name.removesuffix("_tables")
    source_pdf = args.source_pdf.resolve() if args.source_pdf else None
    markdown_path.write_text(
        render_markdown(results, markdown_path, title, image_root, backend.name, source_pdf),
        encoding="utf-8",
    )
    json_path = output_dir / "ocr_results.json"
    json_path.write_text(
        json.dumps([result.as_dict() for result in results], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _write_csvs(results, output_dir)
    _write_sqlite(
        results,
        output_dir,
        {
            "title": title,
            "ocr_backend": backend.name,
            "input_dir": str(input_dir),
            "source_pdf": str(source_pdf) if source_pdf else None,
            "table_count": len(results),
            "schema_version": 1,
        },
    )
    print(f"wrote {markdown_path}")
    print(f"wrote {output_dir / 'tables.sqlite'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
