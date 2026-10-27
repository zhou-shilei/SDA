"""Deterministic high-resolution table cropping from source PDFs.

Detection models operate on a rendered page image, but the final crop is
rendered again from the original PDF. The locator JSON stores table boxes in
PDF points, so this module deliberately does not crop the detector input
image.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence


def _fitz() -> Any:
    try:
        import pymupdf as fitz  # type: ignore
    except ImportError:
        try:
            import fitz  # type: ignore
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise RuntimeError(
                "Table cropping requires PyMuPDF. Install "
                "DataAgent/vendor/requirements-pdf.txt"
            ) from exc
    return fitz


def _as_xyxy(value: Sequence[float]) -> List[float]:
    if len(value) != 4:
        raise ValueError(f"bbox must contain four values, got {value!r}")
    x1, y1, x2, y2 = (float(item) for item in value)
    if x2 <= x1 or y2 <= y1:
        raise ValueError(f"degenerate bbox {value!r}")
    return [x1, y1, x2, y2]


def _padded_rect(fitz: Any, page: Any, bbox: Sequence[float], padding_pt: float) -> Any:
    page_rect = page.rect
    x1, y1, x2, y2 = _as_xyxy(bbox)
    if padding_pt < 0:
        raise ValueError("padding_pt must be non-negative")
    # PyMuPDF uses the same top-left page coordinate convention as the
    # locator's PDF-point boxes. Intersecting with page.rect prevents padding
    # from requesting pixels outside the page.
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
    """Render detected table regions from the original PDF.

    Returned records contain image paths relative to ``output_dir``. Regions
    are sorted in reading order (page, y, x), which is also the order used by
    the generated article-style Markdown.
    """

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
            filename = f"table-{index:04d}-page-{page_number:04d}.png"
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
