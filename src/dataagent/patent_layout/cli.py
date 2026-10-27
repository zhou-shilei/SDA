"""Command line interface for patent PDF region localization."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from .engine import BACKEND_NAMES, PatentRegionLocator


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Locate layout/table regions in patent PDFs")
    subparsers = parser.add_subparsers(dest="command", required=True)

    locate = subparsers.add_parser("locate", help="run the configured layout detectors")
    locate.add_argument("--input", required=True, type=Path, help="input patent PDF")
    locate.add_argument("--output", required=True, type=Path, help="output JSON")
    locate.add_argument(
        "--backends",
        nargs="+",
        choices=BACKEND_NAMES,
        default=list(BACKEND_NAMES),
        help="one or more model backends",
    )
    locate.add_argument("--pp-model-dir", type=Path, help="local PP-DocLayoutV3 model directory")
    locate.add_argument("--yolo-model", type=Path, help="local DocLayout-YOLO .pt or model directory")
    locate.add_argument("--tatr-model-dir", type=Path, help="local TATR model directory")
    locate.add_argument("--dpi", type=int, default=150)
    locate.add_argument("--merge-iou", type=float, default=0.5)
    locate.add_argument("--device", default=None, help="cpu, cuda:0, gpu:0, etc., depending on backend")
    locate.add_argument("--pp-engine", default=None, help="paddle_static, transformers, or onnxruntime")
    locate.add_argument("--pp-threshold", type=float, default=0.2)
    locate.add_argument("--yolo-threshold", type=float, default=0.2)
    locate.add_argument("--tatr-threshold", type=float, default=0.7)
    locate.add_argument("--pages", nargs="+", type=int, help="1-based pages; default is all pages")
    locate.add_argument("--execution", choices=("auto", "serial", "parallel"), default="auto",
                        help="auto reads .studio/layout_runtime.json; serial bypasses runtime configuration")
    locate.add_argument("--torch-python", help="independent Python environment for YOLO/TATR")
    locate.add_argument("--torch-device", help="cpu or cuda:0 for YOLO/TATR")
    locate.add_argument("--pp-device", help="cpu or gpu:0 for Paddle layout")
    locate.add_argument("--cpu-workers", type=int, help="independent page shards per CPU backend (default 2)")
    locate.add_argument("--max-workers", type=int, help="maximum simultaneous inference processes (default 4)")
    locate.add_argument("--threads-per-worker", type=int, help="native CPU threads per process (default 4)")
    locate.add_argument(
        "--allow-missing-backends",
        action="store_true",
        help="continue and record errors if an optional backend is unavailable",
    )
    locate.add_argument("--print-json", action="store_true", help="also print the result to stdout")

    doctor = subparsers.add_parser("doctor", help="show optional dependency/model readiness")
    doctor.add_argument("--pp-model-dir", type=Path)
    doctor.add_argument("--yolo-model", type=Path)
    doctor.add_argument("--tatr-model-dir", type=Path)
    worker = subparsers.add_parser("_layout-worker", help=argparse.SUPPRESS)
    worker.add_argument("--job", required=True, type=Path)
    return parser


def _doctor(args: argparse.Namespace) -> int:
    import importlib.util

    modules = ["fitz", "paddleocr", "paddle", "doclayout_yolo", "transformers", "torch"]
    report = {module: bool(importlib.util.find_spec(module)) for module in modules}
    from .parallel import load_runtime
    report["runtime"] = load_runtime()
    report["paths"] = {
        "pp_model_dir": str(args.pp_model_dir) if args.pp_model_dir else None,
        "yolo_model": str(args.yolo_model) if args.yolo_model else None,
        "tatr_model_dir": str(args.tatr_model_dir) if args.tatr_model_dir else None,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "_layout-worker":
        from .parallel import run_worker
        return run_worker(args.job)
    if args.command == "doctor":
        return _doctor(args)
    model_dirs = {
        "pp_doclayoutv3": args.pp_model_dir,
        "doclayout_yolo": args.yolo_model,
        "tatr": args.tatr_model_dir,
    }
    locator = PatentRegionLocator(
        backends=args.backends,
        model_dirs={key: value for key, value in model_dirs.items() if value is not None},
        thresholds={
            "pp_doclayoutv3": args.pp_threshold,
            "doclayout_yolo": args.yolo_threshold,
            "tatr": args.tatr_threshold,
        },
        dpi=args.dpi,
        merge_iou=args.merge_iou,
        device=args.device,
        pp_engine=args.pp_engine,
        allow_missing_backends=args.allow_missing_backends,
        cpu_threads=args.threads_per_worker,
    )
    from .parallel import load_runtime, locate_parallel
    config = load_runtime() if args.execution != "serial" else {}
    execution = config.get("execution", "serial") if args.execution == "auto" else args.execution
    if execution == "parallel":
        pp_device = args.pp_device or args.device or config.get("pp_device", "cpu")
        pp_device = pp_device.replace("cuda:", "gpu:")
        result = locate_parallel(
            locator, args.input, output_path=args.output, selected_pages=args.pages,
            torch_python=args.torch_python or config.get("torch_python"),
            torch_device=args.torch_device or args.device or config.get("torch_device", "cpu"),
            pp_device=pp_device,
            cpu_workers=args.cpu_workers if args.cpu_workers is not None else int(config.get("cpu_workers", 2)),
            max_workers=args.max_workers if args.max_workers is not None else int(config.get("max_workers", 4)),
            threads_per_worker=args.threads_per_worker if args.threads_per_worker is not None else int(config.get("threads_per_worker", 4)),
        )
    else:
        result = locator.locate(args.input, pages=args.pages, output_path=args.output)
    print(f"wrote {args.output} ({sum(len(page['detections']) for page in result['pages'])} regions)")
    if args.print_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0
