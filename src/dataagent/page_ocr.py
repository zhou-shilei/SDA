"""Read targeted patent pages for assay methods and sequence definitions."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, Sequence

from .assays import load_semantic_template
from .sequence_resolver import parse_construct_mappings
from .table_ocr import Cell, OCRItem, TableResult, _cluster_rows, _make_cell

# Assay method pages

TEMPLATE = load_semantic_template()
ASSAY_TEMPLATE = TEMPLATE.get("assay", {})
PROTOCOL_OCR = ASSAY_TEMPLATE.get("protocol_ocr", {})
ENDPOINT_HINT_RE = re.compile(
    r"IC\s*(?:50|so|s0)|EC\s*(?:50|so|s0)|\bK[Dd]\b|binding affin",
    re.I,
)
ENTITY_HEADER_RE = re.compile(
    r"^(?:clone|antibody|construct|protein|name|抗体|克隆|名称)$",
    re.I,
)


def _method_result_payload(result: Any) -> dict[str, Any]:
    return result.as_dict() if hasattr(result, "as_dict") else dict(result)


def _row_texts(payload: dict[str, Any]) -> list[list[str]]:
    return [
        [str(cell.get("text", "")).strip() for cell in row if isinstance(cell, dict)]
        for row in payload.get("rows", [])
        if isinstance(row, list)
    ]


def candidate_assay_method_pages(
    results: Sequence[Any],
    page_count: int,
    *,
    before: int | None = None,
    after: int | None = None,
) -> list[int]:
    """Return a bounded page window anchored by genuine endpoint tables."""

    anchors: set[int] = set()
    for result in results:
        payload = _method_result_payload(result)
        page = payload.get("page")
        if not isinstance(page, int):
            continue
        rows = _row_texts(payload)
        text = " ".join(cell for row in rows for cell in row)
        has_entity_header = any(
            ENTITY_HEADER_RE.fullmatch(cell)
            for row in rows[:4]
            for cell in row
        )
        has_transposed_entities = bool(
            rows
            and re.search(r"^(?:IC|EC)(?:\s|\(|$)", rows[0][0] if rows[0] else "", re.I)
            and len(rows[0]) >= 3
        )
        if ENDPOINT_HINT_RE.search(text) and (has_entity_header or has_transposed_entities):
            anchors.add(page)
    if not anchors:
        return []

    before = int(PROTOCOL_OCR.get("page_scan_before", 36) if before is None else before)
    after = int(PROTOCOL_OCR.get("page_scan_after", 0) if after is None else after)
    first = max(1, min(anchors) - before)
    last = min(page_count, max(anchors) + after)
    return list(range(first, last + 1))


def _native_lines(page: Any) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    for block in page.get_text("blocks"):
        if len(block) < 5:
            continue
        x1, y1, x2, y2, raw = block[:5]
        values = [re.sub(r"\s+", " ", value).strip() for value in str(raw).splitlines()]
        values = [value for value in values if value]
        if not values:
            continue
        height = max(1.0, float(y2) - float(y1))
        line_height = height / len(values)
        for index, text in enumerate(values):
            top = float(y1) + line_height * index
            lines.append({
                "text": text,
                "bbox": [float(x1), top, float(x2), top + line_height],
                "confidence": 1.0,
            })
    return sorted(lines, key=lambda item: (item["bbox"][1], item["bbox"][0]))


def _ocr_lines(items: list[Any]) -> list[dict[str, Any]]:
    from .table_ocr import _cluster_rows

    output: list[dict[str, Any]] = []
    for row_items in _cluster_rows(items):
        ordered = sorted(row_items, key=lambda item: item.x1)
        output.append({
            "text": " ".join(item.text for item in ordered).strip(),
            "bbox": [
                min(item.x1 for item in ordered),
                min(item.y1 for item in ordered),
                max(item.x2 for item in ordered),
                max(item.y2 for item in ordered),
            ],
            "confidence": round(
                sum(float(item.score) for item in ordered) / max(1, len(ordered)),
                6,
            ),
        })
    return output


def extract_assay_method_pages(
    source_pdf: Path,
    artifact_dir: Path,
    table_results: Sequence[Any],
    *,
    backend_name: str | None = None,
    lang: str = "en",
    device: str | None = None,
    max_side: int | None = None,
) -> list[dict[str, Any]]:
    """Extract method/caption pages near endpoint result tables with caching."""

    try:
        import pymupdf
    except ImportError:  # pragma: no cover - compatibility with older PyMuPDF
        import fitz as pymupdf  # type: ignore

    artifact_dir.mkdir(parents=True, exist_ok=True)
    image_dir = artifact_dir / "assay_method_pages"
    image_dir.mkdir(parents=True, exist_ok=True)
    # Keep the extraction cache separate from the published plain-list
    # artifact written by agent_store.
    artifact = artifact_dir / ".assay_method_pages_cache.json"
    document = pymupdf.open(source_pdf)
    requested_backend = backend_name or str(PROTOCOL_OCR.get("preferred_backend", "tesseract"))
    max_side = int(max_side or PROTOCOL_OCR.get("max_side", 1900))
    pages = candidate_assay_method_pages(table_results, len(document))
    cache_key = {
        "schema_version": 1,
        "template_version": TEMPLATE.get("version", 1),
        "source_pdf_mtime_ns": source_pdf.stat().st_mtime_ns,
        "pages": pages,
        "backend": requested_backend,
        "max_side": max_side,
    }
    try:
        if artifact.is_file():
            try:
                cached = json.loads(artifact.read_text(encoding="utf-8"))
                if cached.get("cache_key") == cache_key:
                    return list(cached.get("pages", []))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                pass

        backend: Any | None = None
        output: list[dict[str, Any]] = []
        for index, page_number in enumerate(pages, start=1):
            print(f"ocr-assay-method-page {index}/{len(pages)}: page {page_number}", flush=True)
            page = document[page_number - 1]
            native = _native_lines(page)
            source = "pdf_text"
            width = float(page.rect.width)
            height = float(page.rect.height)
            lines = native
            image_name = ""
            if len(" ".join(item["text"] for item in native)) < 120:
                if backend is None:
                    from .table_ocr import make_backend

                    backend = make_backend(requested_backend, lang, device, max_side)
                scale = max(1.0, float(max_side) / max(width, height))
                image_path = image_dir / f"assay-method-page-{page_number:04d}.png"
                page.get_pixmap(
                    matrix=pymupdf.Matrix(scale, scale), alpha=False
                ).save(str(image_path))
                items = backend.recognize(image_path)
                for item_index, item in enumerate(items):
                    item.item_index = item_index
                lines = _ocr_lines(items)
                source = "page_ocr"
                image_name = image_path.name
                render_width = int(round(width * scale))
                render_height = int(round(height * scale))
            else:
                render_width = int(round(width))
                render_height = int(round(height))
            output.append({
                "source_id": f"assay-method-page-{page_number:04d}",
                "page": page_number,
                "source": source,
                "image": image_name,
                "width": render_width,
                "height": render_height,
                "pdf_width": width,
                "pdf_height": height,
                "text": "\n".join(item["text"] for item in lines),
                "lines": lines,
                "mode": "assay_method_page",
            })
    finally:
        document.close()

    artifact.write_text(
        json.dumps({"cache_key": cache_key, "pages": output}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return output


# Sequence definition pages

SEQUENCE_TEMPLATE = TEMPLATE["sequence_definition"]
FASTA_HINTS = [re.compile(pattern, re.I | re.M) for pattern in SEQUENCE_TEMPLATE.get("label_patterns", [])]
SEQ_ID_HINT = re.compile(SEQUENCE_TEMPLATE["seq_id_pattern"], re.I)
AA_HINT = re.compile(r"[ACDEFGHIKLMNPQRSTVWYO]{20,}", re.I)


def _sequence_result_payload(result: Any) -> dict[str, Any]:
    return result.as_dict() if hasattr(result, "as_dict") else result


def _result_text(result: Any) -> str:
    payload = _sequence_result_payload(result)
    raw = [str(item.get("text", "")) for item in payload.get("ocr_items", [])]
    if not raw:
        raw = [
            str(cell.get("text", ""))
            for row in payload.get("rows", [])
            for cell in row
            if isinstance(cell, dict)
        ]
    return "\n".join(raw)


def candidate_sequence_pages(results: Sequence[Any], page_count: int) -> list[int]:
    """Select pages near explicit VH/VL-to-SEQ-ID indexes."""

    payloads = [_sequence_result_payload(result) for result in results]
    mappings = parse_construct_mappings(payloads)
    mapping_header_ids = {
        str(mapping.get("header_table_id"))
        for mapping in mappings
        if mapping.get("header_table_id")
    }
    anchors: set[int] = set()
    assay_pages: set[int] = set()
    for result in results:
        payload = _sequence_result_payload(result)
        page = payload.get("page")
        if not isinstance(page, int):
            continue
        compact = re.sub(r"\s+", "", _result_text(result)).lower()
        has_chain_index = (
            ("vh" in compact and "vl" in compact and "seqid" in compact)
            or ("重链" in compact and "轻链" in compact and ("序列" in compact or "seqid" in compact))
        )
        table_id = str(payload.get("source_id") or payload.get("image"))
        if table_id in mapping_header_ids or (not mapping_header_ids and has_chain_index):
            anchors.add(page)
        if re.search(r"IC\s*50|IC₅₀", compact, re.I):
            assay_pages.add(page)
    if anchors and assay_pages:
        nearest_distance = min(abs(anchor - assay) for anchor in anchors for assay in assay_pages)
        anchors = {
            anchor
            for anchor in anchors
            if min(abs(anchor - assay) for assay in assay_pages) <= nearest_distance + 2
        }
    before = int(SEQUENCE_TEMPLATE.get("page_scan_before", 4))
    after = int(SEQUENCE_TEMPLATE.get("page_scan_after", 1))
    pages = {
        page
        for anchor in anchors
        for page in range(max(1, anchor - before), min(page_count, anchor + after) + 1)
    }
    return sorted(pages)


def _is_sequence_page(text: str) -> bool:
    has_label = any(pattern.search(text) for pattern in FASTA_HINTS)
    has_seq_id = bool(SEQ_ID_HINT.search(text))
    has_amino_acids = bool(AA_HINT.search(re.sub(r"\s+", "", text)))
    return has_amino_acids and (has_label or has_seq_id)


def _restore_result(raw: dict[str, Any]) -> TableResult:
    return TableResult(
        table_index=int(raw["table_index"]),
        image=str(raw["image"]),
        page=int(raw["page"]) if raw.get("page") is not None else None,
        source_id=str(raw["source_id"]) if raw.get("source_id") else None,
        crop_confidence=None,
        width=int(raw["width"]),
        height=int(raw["height"]),
        backend=str(raw["backend"]),
        caption=str(raw.get("caption", "")),
        items=[
            OCRItem(
                str(item.get("text", "")),
                float(item.get("score", 0.0)),
                [float(value) for value in item.get("bbox", [])],
                str(item.get("backend", raw["backend"])),
                int(item.get("item_index", -1)),
            )
            for item in raw.get("ocr_items", [])
        ],
        rows=[
            [
                Cell(
                    row_index=int(cell.get("row_index", row_index)),
                    col_index=int(cell.get("col_index", col_index)),
                    text=str(cell.get("text", "")),
                    confidence=float(cell.get("confidence", 0.0)),
                    bbox=[float(value) for value in cell.get("bbox", [])],
                    item_indices=[int(value) for value in cell.get("item_indices", [])],
                )
                for col_index, cell in enumerate(row)
            ]
            for row_index, row in enumerate(raw.get("rows", []))
        ],
        horizontal_lines=[],
        vertical_lines=[],
        mode="sequence_page",
    )


def extract_sequence_page_results(
    source_pdf: Path,
    image_root: Path,
    artifact_dir: Path,
    backend: Any,
    table_results: Sequence[Any],
    *,
    max_side: int = 2600,
    backend_factory: Callable[[], Any] | None = None,
) -> list[TableResult]:
    """OCR selected full pages and return only pages containing sequences."""

    try:
        import pymupdf
    except ImportError:  # pragma: no cover - compatibility with older PyMuPDF
        import fitz as pymupdf  # type: ignore

    document = pymupdf.open(source_pdf)
    image_root.mkdir(parents=True, exist_ok=True)
    artifact = artifact_dir / "sequence_page_ocr.json"
    results: list[TableResult] = []
    try:
        pages = candidate_sequence_pages(table_results, len(document))
        # Selecting no sequence pages does not require any OCR model. This
        # commonly occurs when reparsing an already preserved table dataset.
        if not pages:
            artifact.write_text(json.dumps({"cache_key": {"schema_version": 1, "template_version": TEMPLATE.get("version", 1),
                "source_pdf_mtime_ns": source_pdf.stat().st_mtime_ns, "pages": [],
                "backend": getattr(backend, "name", "not_required"), "max_side": max_side}, "results": []}, indent=2), encoding="utf-8")
            return []
        if backend is None:
            if backend_factory is None:
                raise ValueError("An OCR backend is required for selected sequence pages")
            backend = backend_factory()
        cache_key = {
            "schema_version": 1,
            "template_version": TEMPLATE.get("version", 1),
            "source_pdf_mtime_ns": source_pdf.stat().st_mtime_ns,
            "pages": pages,
            "backend": backend.name,
            "max_side": max_side,
        }
        if artifact.is_file():
            try:
                cached = json.loads(artifact.read_text(encoding="utf-8"))
                if cached.get("cache_key") == cache_key:
                    return [_restore_result(raw) for raw in cached.get("results", [])]
            except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
                pass
        for page_index, page_number in enumerate(pages, start=1):
            print(
                f"ocr-sequence-page {page_index}/{len(pages)}: page {page_number}",
                flush=True,
            )
            page = document[page_number - 1]
            scale = max(1.0, float(max_side) / max(float(page.rect.width), float(page.rect.height)))
            image_path = image_root / f"sequence-page-{page_number:04d}.png"
            page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False).save(str(image_path))
            items = backend.recognize(image_path)
            for index, item in enumerate(items):
                item.item_index = index
            text = "\n".join(item.text for item in items)
            if not _is_sequence_page(text):
                continue
            visual_rows = _cluster_rows(items)
            rows: list[list[Cell]] = []
            for row_index, row_items in enumerate(visual_rows):
                bbox = [
                    min(item.x1 for item in row_items),
                    min(item.y1 for item in row_items),
                    max(item.x2 for item in row_items),
                    max(item.y2 for item in row_items),
                ]
                rows.append([_make_cell(row_index, 0, row_items, bbox)])
            pix_width = int(round(float(page.rect.width) * scale))
            pix_height = int(round(float(page.rect.height) * scale))
            results.append(
                TableResult(
                    table_index=100_000 + page_number,
                    image=image_path.name,
                    page=page_number,
                    source_id=f"sequence-page-{page_number:04d}",
                    crop_confidence=None,
                    width=pix_width,
                    height=pix_height,
                    backend=backend.name,
                    caption="",
                    items=items,
                    rows=rows,
                    horizontal_lines=[],
                    vertical_lines=[],
                    mode="sequence_page",
                )
            )
    finally:
        document.close()

    artifact.write_text(
        json.dumps(
            {"cache_key": cache_key, "results": [result.as_dict() for result in results]},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return results
