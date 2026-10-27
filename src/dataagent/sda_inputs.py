"""Adapt SDA's existing OCR evidence to the migrated dataagent graph.

This module never renders pages or runs OCR. HTML spans retain their physical
source address; missing cell coordinates and confidence remain unknown.
"""

from __future__ import annotations

import hashlib
import copy
import json
import math
import re
from pathlib import Path

from .document_context import link_table_contexts, _compact, _looks_like_caption, _sentences
from .llm_runtime import json_write


def _hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _records(path):
    path = Path(path).expanduser().resolve()
    if path.is_dir():
        preferred = path / "tables.jsonl"
        if preferred.is_file():
            return _records(preferred)
        rows = []
        for item in sorted(path.glob("*.json")):
            value = json.loads(item.read_text(encoding="utf-8"))
            if isinstance(value, dict) and "table_id" in value and (
                "table_text" in value or "text" in value or "table_evidence" in value
            ):
                rows.append((value, item))
        if not rows:
            raise ValueError(f"No SDA table OCR results found in {path}")
        return rows
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".jsonl":
        return [(json.loads(line), path) for line in text.splitlines() if line.strip()]
    value = json.loads(text)
    return [(item, path) for item in (value if isinstance(value, list) else [value])]


def _box(value):
    if not value:
        return []
    if (not isinstance(value, (list, tuple)) or len(value) != 4
            or any(isinstance(v, bool) or not isinstance(v, (float, int)) or not math.isfinite(v) for v in value)
            or value[2] < value[0] or value[3] < value[1]):
        raise ValueError(f"Invalid xyxy bounding box: {value!r}")
    return list(value)


def _rows(text, identity, evidence=None):
    # Reuse SDA's rowspan/colspan parser rather than infer structure again.
    from document.table_continuation import parse_grid

    grid = parse_grid(text, identity)
    if not grid:
        return []
    from document.cell_geometry import geometry_fields
    positions = {cell["ref"]: cell for cell in (evidence or {}).get("cells", [])}
    return [[{
        "row_index": ri, "col_index": ci, "text": cell["text"],
        "confidence": None, "bbox": [], "item_indices": [],
        **({"text_styles": copy.deepcopy(cell["text_styles"])} if cell.get("text_styles") else {}),
        "source_ref": cell.get("ref") or f"{identity}:R{ri}C{ci}",
        "origin_row": cell.get("row"), "origin_column": cell.get("column"),
        "rowspan": cell.get("rowspan", 1), "colspan": cell.get("colspan", 1),
        **geometry_fields(positions.get(cell.get("ref"), {})),
    } for ci, cell in enumerate(row)] for ri, row in enumerate(grid["rows"])]


def _page_dimensions(page):
    width = page.get("width_pt", page.get("page_width", page.get("width")))
    height = page.get("height_pt", page.get("page_height", page.get("height")))
    scale = page.get("pixel_to_page_scale")
    if scale and page.get("image_width") and page.get("image_height"):
        width, height = page["image_width"] * scale[0], page["image_height"] * scale[1]
    return {k: v for k, v in (("page_width", width), ("page_height", height)) if v is not None}


def _resolve_image(value, source, image_dir):
    if not value:
        return None
    path = Path(value).expanduser()
    if path.is_absolute():
        return path.resolve() if path.is_file() else None
    for base in [image_dir, source.parent]:
        if base and (Path(base) / path).is_file():
            return (Path(base) / path).resolve()
    return None


def read_prepared(path, document_id):
    prepared = json.loads(Path(path).read_text(encoding="utf-8"))
    if prepared.get("document_id") != document_id:
        raise ValueError("SDA inputs belong to a different document")
    return prepared


def prepare_sda_inputs(document_id, table_ocr, pdf_ocr, output_dir, *,
                       layout_manifest=None, image_dir=None, source_pdf=None):
    """Validate identity and preserve OCR before invoking semantic agents."""
    output_dir = Path(output_dir).resolve()
    pdf_ocr = Path(pdf_ocr).expanduser().resolve()
    warnings, pages, blocks, hashes, seen, line_ids = [], [], [], set(), set(), set()
    for page, source in _records(pdf_ocr):
        if page.get("document_id") != document_id or page.get("status") != "success":
            raise ValueError("PDF OCR must contain successful pages from this document")
        number = page.get("page_number")
        if type(number) is not int or number < 1 or number in seen:
            raise ValueError("Duplicate or invalid PDF OCR page number")
        if page.get("coordinate_space", "pdf_points") != "pdf_points":
            raise ValueError("PDF OCR coordinates must use pdf_points")
        seen.add(number)
        digest = page.get("pdf_sha256")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("Each PDF OCR page must identify its PDF with sha256")
        hashes.add(digest)
        lines = []
        for index, line in enumerate(page.get("lines", [])):
            text, identity = line.get("text"), line.get("line_id")
            if not isinstance(text, str) or not isinstance(identity, str) or not identity or identity in line_ids:
                raise ValueError("PDF OCR requires unique line_id and literal text")
            line_ids.add(identity)
            box = _box(line.get("bbox"))
            lines.append({**line, "bbox": box, "source_file": str(source), "source_ref": "ocr:" + identity})
            blocks.append({
                **line, "block_id": identity, "page": number, "bbox": box,
                **_page_dimensions(page),
                "normalized_text": _compact(text), "source": "page_ocr",
                "coordinate_space": "pdf_points", "source_file": str(source),
                "source_ref": "ocr:" + identity,
                "block_type": "table_caption" if _looks_like_caption(text) else "paragraph",
                "sentences": [{"sentence_id": f"{identity}-s{i}", "text": sentence, "bbox": box}
                              for i, sentence in enumerate(_sentences(text))],
            })
        if not lines:
            warnings.append({"code": "empty_ocr_page", "page": number})
        pages.append({"page": number, "text": "\n".join(line["text"] for line in lines),
                      "lines": lines, "source": "page_ocr", "coordinate_space": "pdf_points",
                      "quality": "ok" if lines else "uncertain", "confidence": None,
                      "source_file": str(source)})
    if not seen or sorted(seen) != list(range(1, max(seen) + 1)) or len(hashes) != 1:
        raise ValueError("PDF OCR must contain consecutive pages from one PDF, with its sha256")
    pdf_hash = next(iter(hashes))
    page_manifest = pdf_ocr.parent / "document.json"
    if page_manifest.is_file():
        manifest = json.loads(page_manifest.read_text())
        if manifest.get("source_pdf_sha256") != pdf_hash or manifest.get("total_pages") != len(seen):
            raise ValueError("PDF OCR does not match its page manifest")
    if source_pdf and _hash(source_pdf) != pdf_hash:
        raise ValueError("Source PDF must be the working PDF used by OCR (after orientation correction)")

    crops = {}
    if layout_manifest:
        layout_manifest = Path(layout_manifest).resolve()
        layout = json.loads(layout_manifest.read_text(encoding="utf-8"))
        if layout.get("document_id") != document_id or layout.get("processing_pdf_sha256") != pdf_hash:
            raise ValueError("Layout metadata and PDF OCR identify different working PDFs")
        image_dir = Path(image_dir).resolve() if image_dir else layout_manifest.parent
        for crop in layout.get("images", []):
            if crop.get("label") == "table":
                if crop["id"] in crops:
                    raise ValueError("Duplicate table ID in layout manifest")
                crops[crop["id"]] = crop

    tables, unstructured_tables, candidates, identities, geometry = [], [], [], set(), {}
    page_by_number = {page["page"]: page for page in pages}
    for raw, source in _records(table_ocr):
        evidence = raw.get("table_evidence") or raw
        for row in (raw, evidence):
            if row.get("document_id") != document_id:
                raise ValueError("Table OCR belongs to a different document")
            if row.get("error") or row.get("error_stage") or row.get("status", "success") != "success":
                raise ValueError("Failed/skipped table OCR cannot be used as successful evidence")
            if row.get("pdf_sha256", pdf_hash) != pdf_hash:
                raise ValueError("Table OCR and full-page OCR identify different PDFs")
        identity = evidence.get("table_id") or raw.get("table_id")
        if not isinstance(identity, str) or not identity or identity in identities:
            raise ValueError("Table OCR requires unique table IDs")
        identities.add(identity)
        if crops and identity not in crops:
            raise ValueError(f"Table {identity} is missing from the layout manifest")
        crop = crops.get(identity, {})
        number = evidence.get("page") or raw.get("page") or raw.get("layout", {}).get("page") or crop.get("page")
        if type(number) is not int or number not in seen or crop and crop.get("page") != number:
            raise ValueError(f"{identity}: table page does not match PDF OCR/layout")
        text = raw.get("table_text", evidence.get("text"))
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"{identity}: empty table OCR")
        if evidence.get("text") is not None and evidence["text"] != text:
            raise ValueError(f"{identity}: conflicting table_text and evidence text")
        text_hash = hashlib.sha256(text.encode()).hexdigest()
        if evidence.get("text_sha256") and evidence["text_sha256"] != text_hash:
            raise ValueError(f"{identity}: table text sha256 mismatch")
        image = _resolve_image(raw.get("image_path") or evidence.get("image_path") or crop.get("image"), source, image_dir)
        if image and evidence.get("image_sha256") and _hash(image) != evidence["image_sha256"]:
            raise ValueError(f"{identity}: table crop sha256 mismatch")
        if evidence.get("coordinate_space", "pdf_points") != "pdf_points":
            raise ValueError(f"{identity}: table region must use pdf_points")
        box = _box(evidence.get("bbox") or crop.get("bbox") or raw.get("layout", {}).get("bbox"))
        if not image:
            warnings.append({"code": "table_crop_unavailable", "table_id": identity})
        if not box:
            warnings.append({"code": "table_region_unavailable", "table_id": identity})
        width, height = 0, 0
        if image:
            from PIL import Image
            with Image.open(image) as opened:
                width, height = opened.size
        from document.pdf2doc import _ground_table_cells
        evidence = copy.deepcopy(evidence)
        evidence.update(text=text, table_id=identity)
        _ground_table_cells(evidence, {**crop, "bbox": box,
                            "width_px": width or evidence.get("width_px"),
                            "height_px": height or evidence.get("height_px")},
                            page_lines=page_by_number[number]["lines"])
        table = {"table_index": len(tables) + 1, "source_id": identity,
                 "image": image.name if image else "", "image_path": str(image) if image else "",
                 "page": number, "crop_confidence": crop.get("confidence"),
                 "width": width, "height": height, "backend": evidence.get("backend", "sda"),
                 "mode": "sda_html", "caption": raw.get("caption") or "", "bbox": box,
                 "bbox_padded": evidence.get("bbox_padded"), "crop_to_pdf": evidence.get("crop_to_pdf"),
                 "rows": _rows(text, identity, evidence), "ocr_items": [],
                 "horizontal_lines": [], "vertical_lines": [],
                 "sda_source": {"document_id": document_id, "source_file": str(source),
                                "table_id": identity, "pdf_sha256": pdf_hash,
                                "text": text, "text_sha256": text_hash,
                                "image_sha256": evidence.get("image_sha256"), "evidence": evidence}}
        if not table["rows"]:
            table["structure_status"] = "unavailable"
            unstructured_tables.append(table)
            warnings.append({"code": "table_structure_unavailable", "table_id": identity,
                             "page": number, "source_file": str(source),
                             "message": f"Table {identity} on page {number} has no parseable grid; raw OCR and crop are retained for review."})
            continue
        tables.append(table)
        geometry[identity] = {**(crop or raw.get("layout", {})), "id": identity, "document_id": document_id,
                              "page": number, "bbox": box, "image_path": table["image_path"]}
        candidates.append({"table_id": identity, "image": table["image"], "image_path": table["image_path"],
                           "page": number, "bbox": box, "crop_confidence": table["crop_confidence"],
                           "ocr_text": " ".join(c["text"] for row in table["rows"] for c in row)})
    if crops and identities != set(crops):
        raise ValueError("Table OCR does not cover every table in the layout manifest")
    # Missing geometry cannot support coordinate-based context associations.
    positioned = [b for b in blocks if len(b["bbox"]) == 4]
    contexts = link_table_contexts([c for c in candidates if c["bbox"]], positioned)
    indexed_contexts = {c["table_id"]: c for c in contexts}
    # Recheck SDA continuation candidates against the current table OCR and
    # PDF-point geometry. No PDF text or synthetic cell pixels are needed.
    from document.table_continuation import verified_context
    by_id = {table["source_id"]: table for table in tables}
    for table in tables:
        identity = table["source_id"]
        parents, seen_parents = [], {identity}
        current = geometry[identity]
        while current.get("table_context", {}).get("continuation_of"):
            parent_id = current["table_context"]["continuation_of"]
            if parent_id in seen_parents:
                raise ValueError("Cyclic continuation metadata")
            seen_parents.add(parent_id)
            if parent_id not in by_id:
                break
            current = geometry[parent_id]
            parents.append((current, by_id[parent_id]["sda_source"]["text"]))
        verified = verified_context(geometry[identity], table["sda_source"]["text"], parents)
        table["verified_context"] = verified
        if verified["support_tables"] and identity in indexed_contexts:
            context = indexed_contexts[identity]
            context["continuation_of"] = verified["support_tables"][0]["table_id"]
            context["logical_table_id"] = verified["logical_table_id"]
    for candidate in candidates:
        context = indexed_contexts.get(candidate["table_id"])
        if context:
            candidate.update(logical_table_id=context["logical_table_id"], table_number=context["table_number"])
    pages.sort(key=lambda p: p["page"])
    blocks.sort(key=lambda b: b["page"])
    index_path = output_dir / "sda_page_index.json"
    index = {"version": 1, "pdf_sha256": pdf_hash, "page_count": len(pages), "stage": "sda_existing_ocr",
             "pages": pages, "uncertain_pages": [p["page"] for p in pages if p["quality"] != "ok"],
             "performance": {"ocr_pages": 0, "reused_pages": len(pages)}}
    json_write(index_path, index)
    prepared = {"document_id": document_id, "pdf_sha256": pdf_hash, "tables": tables,
                "unstructured_tables": unstructured_tables,
                "candidates": candidates, "document_blocks": blocks, "table_contexts": contexts,
                "index_summary": {"status": "ready", "manifest": str(index_path),
                                  **{k: v for k, v in index.items() if k != "pages"}},
                "warnings": warnings, "inputs": {"table_ocr": str(Path(table_ocr).resolve()),
                    "pdf_ocr": str(pdf_ocr), "pdf_ocr_sha256": _hash(pdf_ocr),
                    "layout_manifest": str(layout_manifest) if layout_manifest else None}}
    path = output_dir / "sda_input.json"
    json_write(path, prepared)
    return path


def retrieve_existing_pages(config, tables, contexts):
    """Use the original retrieval planner, reading supplied OCR instead of OCRing again."""
    from .page_index import plan_page_reads

    prepared = read_prepared(config.sda_input_file, config.document_id)
    summary = prepared["index_summary"]
    index = json.loads(Path(summary["manifest"]).read_text(encoding="utf-8"))
    if index["pdf_sha256"] != prepared["pdf_sha256"]:
        raise ValueError("Prepared page index belongs to a different PDF")
    # All pages have already been read; no rendering/detail OCR budget is needed.
    plan = plan_page_reads(index, tables, limit=index["page_count"], contexts=contexts)
    methods = [{**p, "source_id": f"assay-method-page-{p['page']:04d}", "image": "",
                "mode": "assay_method_page"} for p in index["pages"] if p["page"] in plan["method_pages"]]
    warnings = [{**item, "severity": "medium", "message": item["code"],
                 "table_ids": [item["table_id"]] if item.get("table_id") else []}
                for item in prepared["warnings"]]
    plan.update(status="needs_review" if warnings else "ready", warnings=warnings, source="sda_existing_ocr",
                available_pages=len(index["pages"]), ocr_requests=0)
    path = Path(config.output_dir) / "page_retrieval.json"
    json_write(path, plan)
    return {"assay_method_pages": methods, "page_retrieval": plan,
            "artifacts": {"sda_input": config.sda_input_file, "page_index": summary["manifest"],
                          "page_retrieval": str(path)}}
