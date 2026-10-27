"""兼容入口；table_continuation 的实现已集中到 document.pdf2doc。"""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from document.pdf2doc import (
    ENDPOINT,
    MISSING,
    build_table_contexts,
    load_crop_metadata,
    parse_grid,
    _scalar,
    _numeric_data,
    _header_band,
    _header_html,
    _width,
    verified_context,
    _metrics,
    extract_continuation_assays,
)
