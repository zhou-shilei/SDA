"""Source-addressed continuation candidates and verified OCR header inheritance.

Page geometry proposes links. OCR headers and matching column counts authorize
inheritance; physical crop images and their original text remain separate.
"""

import copy
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re

from lxml import html

from document.document_context import extract_document_blocks, link_table_contexts


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
            from document.sequence_styles import html_styles
            value, text_styles = html_styles(td)
            cell = {"text": value, "ref": f"{table_id}:R{ri}C{column}", "row": ri, "column": column}
            if text_styles:
                cell["text_styles"] = text_styles
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
