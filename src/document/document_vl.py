"""兼容入口；document_vl 的实现已集中到 document.pdf2doc。"""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from document.pdf2doc import (
    LAYOUT_MODEL,
    VL_MODEL,
    model_paths,
    create_engine,
    normalise_result,
    plain_text,
    rebase_markdown,
)
