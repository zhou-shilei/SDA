"""兼容入口；table_ocr 的实现已集中到 document.pdf2doc。"""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from document.pdf2doc import (
    _ocr_device,
    _cpu_threads,
    _ocr_backend,
    _ocr_batch_size,
    OCR_BACKENDS,
    configure_ocr_worker,
    _create_table_ocr as _create_ocr,
    get_ocr,
    extract_table_evidence,
    ocr_runtime,
    extract_table_evidence_batch,
    structure_table_evidence,
    extract_table_text,
    table_ocr_main,
)


if __name__ == "__main__":
    raise SystemExit(table_ocr_main())
