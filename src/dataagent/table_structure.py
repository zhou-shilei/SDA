"""Recover OCR token placement and build evidence-preserving table views."""

from __future__ import annotations

import copy
import hashlib
import re
from collections import Counter
from typing import Any

# OCR token recovery

def interval_index(value: float, boundaries: list[float]) -> int | None:
    for index, (left, right) in enumerate(zip(boundaries, boundaries[1:])):
        if left <= value < right or (index == len(boundaries) - 2 and value == right):
            return index
    return None


def token_audit(table: dict) -> dict:
    tokens = {item.get("item_index", i): item for i, item in enumerate(table.get("ocr_items", []))}
    counts = Counter(i for row in table.get("rows", []) for cell in row for i in cell.get("item_indices", []))
    return {"token_count": len(tokens), "assigned_count": sum(i in counts for i in tokens),
            "unassigned_indices": sorted(set(tokens) - counts.keys()),
            "duplicate_indices": sorted(i for i, n in counts.items() if n > 1),
            "unknown_indices": sorted(counts.keys() - tokens.keys())}


def recover_grid_tokens(raw: dict) -> dict:
    table = copy.deepcopy(raw)
    rows = table.get("rows", [])
    horizontal, vertical = table.get("horizontal_lines", []), table.get("vertical_lines", [])
    audit = token_audit(table)
    repairs = []
    if table.get("mode") == "grid" and len(horizontal) == len(rows) + 1 and all(len(r) + 1 == len(vertical) for r in rows):
        for index, token in enumerate(table.get("ocr_items", [])):
            index = token.get("item_index", index)
            if index not in audit["unassigned_indices"] or len(token.get("bbox", [])) != 4:
                continue
            x0, y0, x1, y1 = token["bbox"]
            r = interval_index((y0 + y1) / 2, horizontal)
            c = interval_index((x0 + x1) / 2, vertical)
            if r is None or c is None:
                continue
            cell = rows[r][c]
            # Never overwrite an existing cell or guess how independent text joins.
            if cell.get("item_indices") or str(cell.get("text", "")).strip():
                continue
            cell.update(text=token["text"], confidence=token.get("score", 0), bbox=list(token["bbox"]),
                        item_indices=[index], row_index=r, col_index=c,
                        source_token_refs=[f"{table.get('source_id') or table.get('image')}:token:{index}"])
            repairs.append({"kind": "recover_unassigned_token", "item_index": index, "row": r, "column": c})
    if repairs:
        previous = table.get("structure_repair", {})
        table["structure_repair"] = {**previous, "version": 2, "repairs": [*previous.get("repairs", []), *repairs]}
    table["token_audit"] = token_audit(table)
    # Coverage describes text geometry, not an asserted merged-cell boundary.
    for row in rows:
        for cell in row:
            if len(cell.get("bbox", [])) != 4 or not cell.get("text"):
                continue
            x0, y0, x1, y1 = cell["bbox"]
            cell["text_coverage"] = {
                "columns": [c for c, (a, b) in enumerate(zip(vertical, vertical[1:]))
                            if min(x1, b) - max(x0, a) > .2 * max(1, x1 - x0)],
                "rows": [r for r, (a, b) in enumerate(zip(horizontal, horizontal[1:]))
                         if min(y1, b) - max(y0, a) > .2 * max(1, y1 - y0)],
            }
    return table


def apply_structure_plan(raw: dict, plan: dict) -> dict:
    """Validate every source/geometry claim before applying a model proposal."""
    table = copy.deepcopy(raw)
    identity = str(table.get("source_id") or table.get("image"))
    if plan.get("table_id") != identity:
        raise ValueError("Structure plan changed table identity")
    rows = table["rows"]
    tokens = {t.get("item_index", i): t for i, t in enumerate(table.get("ocr_items", []))}

    def cell_at(r, c):
        if not 0 <= r < len(rows) or not 0 <= c < len(rows[r]):
            raise ValueError("Structure address outside table")
        return rows[r][c]

    for move in plan.get("placements", []):
        index, r, c = move["token_index"], move["row"], move["column"]
        cell = cell_at(r, c)
        if index not in token_audit(table)["unassigned_indices"] or cell.get("text") or cell.get("item_indices"):
            raise ValueError("Placement must recover an unassigned token into an empty cell")
        token = tokens[index]
        x0, y0, x1, y1 = token["bbox"]
        if (interval_index((x0 + x1) / 2, table.get("vertical_lines", [])) != c
                or interval_index((y0 + y1) / 2, table.get("horizontal_lines", [])) != r):
            raise ValueError("Placement contradicts token geometry")
        cell.update(text=token["text"], bbox=token["bbox"], confidence=token.get("score", 0), item_indices=[index])
    spans = []
    for span in plan.get("header_spans", []):
        r0, c0, r1, c1 = (span[k] for k in ("row", "column", "row_end", "column_end"))
        cell_at(r0, c0)
        cell_at(r1, c1)
        match = re.fullmatch(re.escape(identity) + r":R(\d+)C(\d+)", span["name_ref"])
        if not match or r0 > r1 or c0 > c1:
            raise ValueError("Invalid header span")
        r, c = map(int, match.groups())
        header = cell_at(r, c)
        if not r0 <= r <= r1 or not c0 <= c <= c1 or not header.get("text"):
            raise ValueError("Header source must be inside its span")
        if any(cell_at(rr, cc).get("text") for rr in range(r0, r1 + 1) for cc in range(c0, c1 + 1) if (rr, cc) != (r, c)):
            raise ValueError("Header span overlaps other nonempty cells")
        rectangle = {(rr, cc) for rr in range(r0, r1 + 1) for cc in range(c0, c1 + 1)}
        if any(rectangle & previous for previous in spans):
            raise ValueError("Header spans overlap")
        spans.append(rectangle)
        header["header_span"] = {k: span[k] for k in ("row", "column", "row_end", "column_end")}
    for join in plan.get("numeric_joins", []):
        cell = cell_at(join["row"], join["column"])
        indices = join["token_indices"]
        if len(indices) < 2 or len(indices) != len(set(indices)) or set(indices) != set(cell.get("item_indices", [])):
            raise ValueError("Numeric join must preserve every original cell token exactly once")
        parts = [tokens[i] for i in indices]
        if any(len(p.get("bbox", [])) != 4 for p in parts):
            raise ValueError("Numeric join lacks token coordinates")
        for upper, lower in zip(parts, parts[1:]):
            a, b = upper["bbox"], lower["bbox"]
            if b[1] < a[3] - .15 * (a[3] - a[1]) or min(a[2], b[2]) <= max(a[0], b[0]):
                raise ValueError("Numeric join is not a vertically wrapped number")
        original = re.sub(r"\s+|<br\s*/?>", "", str(cell["text"]))
        value = "".join(p["text"].strip() for p in parts)
        if value != original or not re.fullmatch(r"[<>≤≥~]?[+-]?(?:\d+\.\d+|\d+)(?:[eE][+-]?\d+)?", value):
            raise ValueError("Numeric join changed source characters")
        cell["numeric_text"] = value
        cell["numeric_join"] = {"token_indices": indices, "source": "image_confirmed_wrap"}
    table["structure_plan"] = copy.deepcopy(plan)
    table["token_audit"] = token_audit(table)
    return table


# Semantic table views

METRIC_HEADER = re.compile(r"^(?:IC\s*\d+|EC\s*\d+|K[ad]|K_on|K_off|kon|koff)\s*[（(]", re.I)
ENTITY_HEADER = re.compile(r"^(?:抗体(?:名称)?|分子|名称|antibody|molecule|clone|name|construct)$", re.I)
NUMERIC_CELL = re.compile(r"^(?:NA|N/A|ND|[-–—]|[<>≤≥~]?\s*[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)$", re.I)


def _compact_assay_columns(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Remove phantom columns only when every row proves a two-column assay."""
    rows = raw.get("rows", [])
    if len(rows) < 2 or max(map(len, rows), default=0) <= 2:
        return None
    pairs = [[(i, cell) for i, cell in enumerate(row) if str(cell.get("text", "")).strip()] for row in rows]
    if any(len(pair) != 2 for pair in pairs):
        return None
    if not ENTITY_HEADER.fullmatch(str(pairs[0][0][1]["text"]).strip()) or not METRIC_HEADER.match(str(pairs[0][1][1]["text"]).strip()):
        return None
    if any(not NUMERIC_CELL.fullmatch(str(pair[1][1]["text"]).strip()) for pair in pairs[1:]):
        return None
    if any(len(cell.get("bbox", [])) != 4 for pair in pairs for _, cell in pair):
        return None
    # A shared gap separates all names from all values, independent of the
    # erroneous OpenCV line positions and OCR string lengths.
    left_edge = max(pair[0][1]["bbox"][2] for pair in pairs)
    right_edge = min(pair[1][1]["bbox"][0] for pair in pairs)
    if right_edge - left_edge < 0.04 * float(raw.get("width") or right_edge):
        return None
    if any(min(a["bbox"][3], b["bbox"][3]) <= max(a["bbox"][1], b["bbox"][1]) for pair in pairs for a, b in [(pair[0][1], pair[1][1])]):
        return None
    identity = str(raw.get("source_id") or raw.get("image"))
    recovered = [[{**copy.deepcopy(cell), "row_index": r, "col_index": c,
                   "source_ref": f"{identity}:R{r}C{original}"}
                  for c, (original, cell) in enumerate(pair)] for r, pair in enumerate(pairs)]
    return {**raw, "rows": recovered, "structure_repair": {"version": 1, "repairs": [
        {"kind": "remove_phantom_assay_columns", "rows": list(range(len(rows))),
         "original_width": max(map(len, rows)), "recovered_width": 2}]}}


def _semantic_geometry(raw: dict[str, Any]) -> dict[str, Any]:


    raw = recover_grid_tokens(raw)
    if any(r.get("kind") != "recover_unassigned_token" for r in raw.get("structure_repair", {}).get("repairs", [])):
        return raw
    compacted = _compact_assay_columns(raw)
    if compacted:
        return compacted
    rows = raw.get("rows", [])
    tokens = {item.get("item_index", i): {**item, "item_index": item.get("item_index", i)}
              for i, item in enumerate(raw.get("ocr_items", []))}
    repairs = []
    output = copy.deepcopy(rows)
    identity = str(raw.get("source_id") or raw.get("image"))
    for header_index, row in enumerate(rows):
        for column, cell in enumerate(row):
            parts = [tokens[index] for index in cell.get("item_indices", []) if index in tokens]
            parts.sort(key=lambda item: item.get("bbox", [0])[0])
            if len(parts) < 2 or not all(METRIC_HEADER.match(item.get("text", "").strip()) for item in parts):
                continue
            if any(len(item.get("bbox", [])) != 4 for item in parts):
                continue
            # Independently recognized, horizontally separated metric headers.
            if any(right["bbox"][0] - left["bbox"][2] < 0.7 * max(left["bbox"][3] - left["bbox"][1], 1)
                   for left, right in zip(parts, parts[1:])):
                continue
            boundaries = [(left["bbox"][2] + right["bbox"][0]) / 2 for left, right in zip(parts, parts[1:])]
            recovered = []
            for row_index in range(header_index, len(rows)):
                source_row = rows[row_index]
                if len(source_row) != len(row):
                    break
                source = source_row[column]
                items = [tokens[index] for index in source.get("item_indices", []) if index in tokens]
                items.sort(key=lambda item: item.get("bbox", [0])[0])
                if len(items) != len(parts) or any(len(item.get("bbox", [])) != 4 for item in items):
                    break
                if row_index > header_index and any(METRIC_HEADER.match(item.get("text", "")) for item in items):
                    break
                if any(sum((item["bbox"][0] + item["bbox"][2]) / 2 > boundary for boundary in boundaries) != i
                       for i, item in enumerate(items)):
                    break
                # Every original scientific token must survive verbatim.
                compact = lambda text: re.sub(r"\s+|<br\s*/?>", "", text)
                if compact(str(source.get("text", ""))) != compact(" ".join(item["text"] for item in items)):
                    break
                split = [{"text": item["text"], "bbox": item["bbox"],
                          "confidence": item.get("score"), "item_indices": [item["item_index"]],
                          "source_ref": f"{identity}:R{row_index}C{column}"} for item in items]
                output[row_index] = copy.deepcopy(source_row[:column]) + split + copy.deepcopy(source_row[column + 1:])
                for index, value in enumerate(output[row_index]):
                    original_column = index if index < column else column if index < column + len(parts) else index - len(parts) + 1
                    value.setdefault("source_ref", f"{identity}:R{row_index}C{original_column}")
                    value.update(row_index=row_index, col_index=index)
                recovered.append(row_index)
            if len(recovered) > 1:
                repairs.append({"kind": "split_merged_metric_columns", "header_row": header_index,
                                "rows": recovered, "original_column": column,
                                "headers": [item["text"] for item in parts]})
            else:
                output[header_index] = copy.deepcopy(rows[header_index])
    if not repairs:
        return raw
    return {**raw, "rows": output, "structure_repair": {"version": 1, "repairs": repairs}}


def semantic_table(raw: dict[str, Any]) -> dict[str, Any]:


    result = recover_grid_tokens(_semantic_geometry(raw))
    plan = raw.get("structure_plan") or raw.get("table_classification", {}).get("structure_plan")
    if plan and result.get("structure_plan") != plan:
        result = apply_structure_plan(result, plan)
    return result


def verified_header_links(tables: list[dict[str, Any]], contexts: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Allow inherited headers only for explicit, geometrically aligned continuations."""
    by_id = {str(t.get("source_id") or t.get("image")): t for t in tables}
    by_context = {str(c.get("table_id")): c for c in contexts}
    links = {}
    for identity, table in by_id.items():
        if not table.get("sda_source"):
            continue
        for support in table.get("verified_context", {}).get("support_tables", []):
            parent_id = support["table_id"]
            parent = by_id.get(parent_id, {})
            original = parent.get("sda_source", {}).get("text")
            if (original and parent.get("page", 0) < table.get("page", 0)
                    and support.get("verification") == "adjacent_page_bbox_and_ocr_columns"
                    and support.get("source_ocr_sha256") == hashlib.sha256(original.encode()).hexdigest()):
                links.setdefault(identity, []).append(parent_id)
    for identity, context in by_context.items():
        if identity in links:
            continue
        parent_id = context.get("continuation_of")
        child, parent = by_id.get(identity), by_id.get(parent_id)
        if not child or not parent or child.get("page") != (parent.get("page") or 0) + 1:
            continue
        headers, body = parent.get("rows", []), child.get("rows", [])
        if not headers or len(headers) > 5 or not body:
            continue
        if not any(METRIC_HEADER.match(str(cell.get("text", "")).strip()) for row in headers for cell in row):
            continue
        width = max(map(len, headers))
        if any(len(row) != width for row in body[:min(3, len(body))]):
            continue
        left, right = parent.get("vertical_lines", []), child.get("vertical_lines", [])
        if len(left) != width + 1 or len(right) != len(left) or not parent.get("width") or not child.get("width"):
            continue
        if any(abs(a / parent["width"] - b / child["width"]) > 0.035 for a, b in zip(left, right)):
            continue
        parent_box, child_box = by_context.get(parent_id, {}).get("bbox", []), context.get("bbox", [])
        if len(parent_box) != 4 or len(child_box) != 4 or any(abs(parent_box[i] - child_box[i]) > 10 for i in (0, 2)):
            continue
        links[identity] = [parent_id]
    return links
