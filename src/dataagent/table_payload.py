"""Row-organized semantic input; physical evidence stays in the host source map."""

from __future__ import annotations

import re
from typing import Any

from .llm_io import cell_ref, metric_endpoints, table_id


_HEADER = re.compile(
    r"\b(?:name|clone|antibody|construct|assay|method|sequence|SEQ\s*ID|VH|VL|"
    r"heavy|light|chain|species|human|cyno\w*|mouse|concentration|unit|score|"
    r"IC\s*(?:50|90)|EC\s*(?:50|90)|KD|kon|koff)\b|"
    r"名称|抗体|序列|编号|重链|轻链|浓度|单位|物种", re.I)
_SCALAR = re.compile(r"[<>≤≥~]?\s*[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?(?:\s*%)?")
_SEQUENCE = re.compile(r"[ACDEFGHIKLMNPQRSTVWY]{20,}", re.I)
_UNIT = re.compile(r"[（(]?(?:[pnuµμm]?M|mol/L|ng/mL|µg/mL|%)[）)]?", re.I)


def _header_rows(table: dict[str, Any]) -> tuple[list[int], str]:
    rows = table.get("rows", [])
    # Classified header indices include internal/repeated headers, not only row 0.
    regions = table.get("table_classification", {}).get("regions", [])
    explicit = table.get("header_rows")
    if explicit is None and regions:
        explicit = [r for region in regions for r in region.get("header_rows", [])]
    if explicit is not None:
        return sorted({r for r in explicit if type(r) is int and 0 <= r < len(rows)}), "declared"

    headers = []
    for r, row in enumerate(rows):
        values = [str(c.get("text", "")).strip() for c in row if str(c.get("text", "")).strip()]
        if not values:
            continue
        # A number or residue body is data, including headerless continuation rows.
        if any(_SCALAR.fullmatch(v) or _SEQUENCE.fullmatch(re.sub(r"\s+|<br\s*/?>", "", v)) for v in values):
            break
        # A text-only first row is not automatically a header. Headerless
        # continuations often begin with entity/SEQ-ID/sequence values; keep
        # those cells in the data rows unless their content or merged-cell
        # metadata provides positive header evidence.
        if all(_HEADER.search(v) or metric_endpoints(v) or _UNIT.fullmatch(v) for v in values) or any(
            c.get("header_span") or (c.get("origin_row", r) in headers and c.get("rowspan", 1) > 1)
            for c in row
        ):
            headers.append(r)
        else:
            break
        # Without declared header metadata, do not turn an arbitrarily long
        # text-only body into a supposed header band and defeat one-row sampling.
        if len(headers) >= 5:
            break
    return headers, "leading_rows_heuristic" if headers else "no_header_detected"


def pack_semantic_table(table: dict[str, Any], *, sample_data_row: bool = False,
                       inherited_header: dict[str, Any] | None = None) -> dict[str, Any]:
    """Keep original strings/indices; Label gets <=1 nonempty data row, Template all rows."""
    identity = table_id(table)
    rows = table.get("rows", [])
    headers, selection = _header_rows(table)
    header_set = set(headers)
    data_rows = [r for r, row in enumerate(rows) if r not in header_set]
    nonempty_data = [r for r in data_rows if any(str(c.get("text", "")).strip() for c in rows[r])]
    selected = nonempty_data[:1] if sample_data_row else data_rows
    supplied = set(headers + selected)

    def encode(r: int) -> list[list[str]]:
        # Empty cells are explicit, so a missing value never shifts the next column.
        return [[cell_ref(identity, r, c), str(cell.get("text", ""))] for c, cell in enumerate(rows[r])]

    item = {
        "table_id": identity, "page": table.get("page"), "caption": table.get("caption", ""),
        "cell_encoding": "[source_id, text]", "source_id_type": "original OCR cell reference string",
        "row_count": len(rows), "column_count": max(map(len, rows), default=0),
        "header_row_indices": headers, "header_selection": selection,
        "header_status": "inherited" if inherited_header else ("local" if headers else "missing"),
        "headers": [cell for r in headers for cell in encode(r)],
        "row_indices": selected, "rows": [encode(r) for r in selected],
        "input_scope": "headers_and_one_data_row" if sample_data_row else "all_rows",
        "omitted_data_row_count": len(data_rows) - len(selected),
    }
    if inherited_header:
        item["inherited_headers"] = inherited_header
    # Only source-backed merged-cell / reviewed numeric views need extra metadata.
    views = {}
    for r in sorted(supplied):
        for c, cell in enumerate(rows[r]):
            view = {k: cell[k] for k in ("numeric_text", "header_span") if cell.get(k) is not None}
            for k in ("rowspan", "colspan"):
                if cell.get(k, 1) > 1:
                    view[k] = cell[k]
            if view:
                views[cell_ref(identity, r, c)] = view
    if views:
        item["cell_views"] = views
    if sample_data_row:
        item["sample_row_index"] = selected[0] if selected else None
        item["data_row_bounds"] = [nonempty_data[0], nonempty_data[-1]] if nonempty_data else None
        item["sampling_notice"] = (
            "Only candidate headers and at most one data row are supplied. Row counts/bounds are host metadata, "
            "not evidence that omitted rows have uniform meaning. Do not cite or describe unseen cells. "
            "Header selection is heuristic unless declared; mark uncertain structure needs_review."
        )
    return item


def semantic_cell_refs(item: dict[str, Any]) -> set[str]:
    refs = {ref for ref, _ in item["headers"]} | {ref for row in item["rows"] for ref, _ in row}
    for header_row in item.get("inherited_headers", {}).get("rows", []):
        refs.update(ref for ref, _ in header_row.get("cells", []))
    return refs


def inherited_header_view(parent: dict[str, Any], parent_id: str) -> dict[str, Any] | None:
    """Expose actual parent header cells and references without relabeling child rows."""
    rows = parent.get("rows", [])
    header_rows, selection = _header_rows(parent)
    if not header_rows:
        return None
    return {
        "status": "inherited",
        "source_table_id": parent_id,
        "source_header_row_indices": header_rows,
        "source_header_selection": selection,
        "rows": [{
            "row_index": r,
            "cells": [[cell_ref(parent_id, r, c), str(cell.get("text", ""))]
                      for c, cell in enumerate(rows[r])],
        } for r in header_rows],
    }
