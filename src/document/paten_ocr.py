"""兼容入口；paten_ocr 的实现已集中到 document.pdf2doc。"""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from document.pdf2doc import (
    OCR_VERSION as VERSION,
    DETECTOR,
    RECOGNIZER,
    MODEL_FILES,
    OCRConfig,
    _now,
    _digest,
    _file_hash,
    _write_text,
    _write_json,
    parse_ocr_pages as parse_pages,
    _runtime_versions,
    _resolve_device,
    _model_signature,
    _create_page_ocr as _create_ocr,
    _normalise_result,
    _predict_page,
    _render,
    _render_pdf_page,
    _WORKER_ENGINE,
    _WORKER_INIT_ERROR,
    _ocr_page_job,
    _parallel_ocr_pages,
    _prefetched_pages,
    _cached_page,
    _output_lock,
    _export,
    ocr_pdf,
    page_ocr_main as main,
)


if __name__ == "__main__":
    raise SystemExit(main())
