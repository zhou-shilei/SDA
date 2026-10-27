"""PDF 裁图的兼容导入及命令行入口；实现已迁入 document.pdf2doc。

原有参数保持可用：
python src/document/pdf2fig.py --input source.pdf --output-dir output
"""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from document.pdf2doc import (
    ROOT,
    CONFIG_PATH,
    WORKER_EVENT,
    BACKEND_NAMES,
    PATENT_ID,
    MODEL_SOURCE,
    RawDetection,
    canonical_label,
    clamp_bbox,
    iou,
    weighted_box,
    cluster_detections,
    fused_detection,
    DetectorBackend,
    PPDocLayoutV3Backend,
    DocLayoutYOLOBackend,
    TableTransformerBackend,
    RenderedPage,
    render_pdf,
    _sha256,
    _convert_to_pdf_points,
    fuse_page,
    PatentRegionLocator,
    _fitz,
    _as_xyxy,
    _padded_rect,
    _table_regions,
    crop_table_regions,
    report_progress,
    isolated_env,
    load_runtime,
    run_worker,
    plan_jobs,
    supervise,
    merge_results,
    locate_parallel,
    build_layout_parser,
    _doctor,
    layout_main,
    save_json,
    parse_pages,
    document_name,
    _validate_layout,
    extract_pdf,
    _model_default,
    layout_crop_main as main,
)


if __name__ == "__main__":
    raise SystemExit(main())
