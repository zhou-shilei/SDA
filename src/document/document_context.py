"""兼容入口；document_context 的实现已集中到 document.pdf2doc。"""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from document.pdf2doc import (
    TABLE_REF_RE,
    IMPLICIT_TABLE_RE,
    FOOTNOTE_RE,
    CONTINUATION_RE,
    _fitz,
    _clean_text,
    _compact,
    _bbox,
    _union_bbox,
    _area,
    _intersection_area,
    _horizontal_overlap,
    _overlap_fraction,
    _is_running_header_or_footer,
    _sentences,
    table_references,
    _table_mentions,
    _looks_like_caption,
    _caption_excerpt,
    extract_document_blocks,
    _evidence,
    _vertical_distance,
    _caption_score,
    _number_parts,
    _between_number,
    _infer_missing_table_numbers,
    _block_overlaps_a_table,
    _nearby_blocks,
    _nearest_table_after,
    _assign_logical_tables,
    link_table_contexts,
    render_context_images,
    context_text,
)
