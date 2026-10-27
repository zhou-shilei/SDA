"""Layout detection, cropping, reports, ensembles, benchmarks, and model downloads."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Sequence

from .patent_layout.cli import main as locate_main
from .patent_layout.cropper import crop_table_regions
from .patent_layout.engine import PatentRegionLocator
from .patent_layout.schema import RawDetection, cluster_detections, fused_detection

PROJECT_ROOT = Path(__file__).resolve().parents[2]


# Markdown layout reports

def _cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _bbox(value: Any, digits: int = 2) -> str:
    if not isinstance(value, (list, tuple)):
        return _cell(value)
    return "[" + ", ".join(f"{float(x):.{digits}f}" for x in value) + "]"


def _provenance(value: Any) -> str:
    if not isinstance(value, list):
        return _cell(value)
    compact = []
    for item in value:
        if not isinstance(item, dict):
            compact.append(item)
            continue
        compact.append(
            {
                key: item.get(key)
                for key in ("model", "model_id", "backend", "source", "raw_label")
                if item.get(key) is not None
            }
        )
    return _cell(json.dumps(compact, ensure_ascii=False, separators=(",", ":")))


def render_report(
    data: dict[str, Any],
    report_title: str,
) -> str:
    document = data.get("document", {})
    settings = data.get("settings", {})
    models = data.get("models", [])
    pages = data.get("pages", [])
    detections = [d for page in pages for d in page.get("detections", [])]
    labels: dict[str, int] = {}
    for detection in detections:
        label = str(detection.get("label", "unknown"))
        labels[label] = labels.get(label, 0) + 1

    lines = [
        f"# {report_title}",
        "",
        "## Summary",
        "",
        f"- Source PDF: `{_cell(document.get('filename', ''))}`",
        f"- SHA256: `{_cell(document.get('sha256', ''))}`",
        f"- Pages: {len(pages)}",
        f"- Regions: {len(detections)}",
        f"- Coordinate space: `{_cell(settings.get('coordinate_space', 'pdf_points'))}`",
        f"- Render DPI: {settings.get('dpi', '')}",
        f"- Label counts: `{_cell(json.dumps(labels, ensure_ascii=False, sort_keys=True))}`",
        "",
        "## Models",
        "",
        "| Model | Backend | Status | Threshold | Provenance source |",
        "|---|---|---:|---:|---|",
    ]
    for model in models:
        lines.append(
            "| "
            + " | ".join(
                [
                    _cell(model.get("model", "")),
                    _cell(model.get("backend", "")),
                    _cell(model.get("status", "")),
                    _cell(model.get("threshold", "")),
                    _cell(model.get("model_source", model.get("source", ""))),
                ]
            )
            + " |"
        )

    if data.get("errors"):
        lines.extend(
            [
                "",
                "## Errors",
                "",
                "```json",
                json.dumps(data["errors"], ensure_ascii=False, indent=2),
                "```",
                "",
            ]
        )

    lines.extend(["", "## Regions", ""])
    for page in pages:
        page_number = page.get("page", "?")
        page_detections = page.get("detections", [])
        lines.extend(
            [
                f"### Page {page_number} ({len(page_detections)} regions)",
                "",
                "| ID | Label | Confidence | Bbox (PDF points) | Bbox (pixels) | Provenance |",
                "|---|---|---:|---|---|---|",
            ]
        )
        for detection in page_detections:
            lines.append(
                "| "
                + " | ".join(
                    [
                        _cell(detection.get("id", "")),
                        _cell(detection.get("label", "")),
                        _cell(detection.get("confidence", "")),
                        _bbox(detection.get("bbox", [])),
                        _bbox(detection.get("bbox_px", [])),
                        _provenance(detection.get("provenance", [])),
                    ]
                )
                + " |"
            )
        lines.append("")
    return "\n".join(lines)


def report_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Convert layout JSON to Markdown")
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--title", default=None)
    args = parser.parse_args(argv)
    data = json.loads(args.input.read_text(encoding="utf-8"))
    title = args.title or data.get("document", {}).get("filename", args.input.stem)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render_report(data, title), encoding="utf-8")
    return 0


# Crop table regions

def crop_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render table boxes from the original PDF")
    parser.add_argument("--input", required=True, type=Path, help="locator JSON")
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--pdf", type=Path, help="override the PDF path stored in JSON")
    parser.add_argument("--dpi", type=int, default=400)
    parser.add_argument("--padding-pt", type=float, default=6.0)
    parser.add_argument("--min-confidence", type=float, default=0.8)
    args = parser.parse_args(argv)
    payload = json.loads(args.input.read_text(encoding="utf-8"))
    crops = crop_table_regions(
        payload,
        args.output_dir,
        pdf_path=args.pdf,
        dpi=args.dpi,
        padding_pt=args.padding_pt,
        min_confidence=args.min_confidence,
    )
    print(f"wrote {len(crops)} table crops to {args.output_dir}")
    return 0


# Batch cropped-image reports


def render_crop_report(report_title: str, crops: list[dict[str, Any]], *, image_prefix: str) -> str:
    """Render table crops in document order with links relative to the report."""
    lines = [f"# {report_title}", ""]
    for crop in crops:
        image = (Path(image_prefix) / crop["image"]).as_posix()
        lines.extend([
            f"## Table {crop['index']} — page {crop['page']}",
            "",
            f"![Table {crop['index']}]({image})",
            "",
        ])
    return "\n".join(lines)


def _safe_component(name: str) -> str:
    name = re.sub(r"[^0-9A-Za-z一-龥._-]+", "_", name).strip("._")
    return name or "patent"


def render_reports_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render ordered table-image reports")
    parser.add_argument("--json-root", type=Path, default=PROJECT_ROOT / "benchmark/test/raw_json")
    parser.add_argument("--output-root", type=Path, default=PROJECT_ROOT / "benchmark/test")
    parser.add_argument("--models", nargs="+", default=None, help="JSON subdirectories to process")
    parser.add_argument("--dpi", type=int, default=400)
    parser.add_argument("--padding-pt", type=float, default=6.0)
    parser.add_argument("--min-confidence", type=float, default=0.8)
    parser.add_argument(
        "--reuse-existing",
        action="store_true",
        help="reuse table_crops.json instead of rendering images again",
    )
    args = parser.parse_args(argv)

    model_dirs = sorted(
        path for path in args.json_root.iterdir() if path.is_dir()
    ) if args.models is None else [args.json_root / name for name in args.models]
    for model_dir in model_dirs:
        if not model_dir.is_dir():
            raise SystemExit(f"model JSON directory not found: {model_dir}")
        output_dir = args.output_root / model_dir.name
        for json_path in sorted(model_dir.glob("*.json")):
            payload = json.loads(json_path.read_text(encoding="utf-8"))
            stem = json_path.stem
            crop_dir = output_dir / f"{_safe_component(stem)}_tables"
            metadata_path = crop_dir / "table_crops.json"
            if args.reuse_existing and metadata_path.is_file():
                crops = json.loads(metadata_path.read_text(encoding="utf-8"))
            else:
                crops = crop_table_regions(
                    payload,
                    crop_dir,
                    dpi=args.dpi,
                    padding_pt=args.padding_pt,
                    min_confidence=args.min_confidence,
                )
            markdown_path = output_dir / f"{stem}.md"
            markdown_path.parent.mkdir(parents=True, exist_ok=True)
            markdown_path.write_text(
                render_crop_report(
                    f"{model_dir.name} — {stem}",
                    crops,
                    image_prefix=crop_dir.name,
                ),
                encoding="utf-8",
            )
            print(f"{model_dir.name}/{stem}: {len(crops)} table crops")
    return 0


# Fuse saved model predictions

MODEL_DIRS = {
    "pp_doclayoutv3": "PP-DocLayoutV3",
    "doclayout_yolo": "DocLayout-YOLO",
    "tatr": "Table-Transformer-TATR",
}


def build_ensemble(reports: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not reports:
        raise ValueError("no model reports supplied")
    base = reports[0]
    by_page: Dict[int, List[RawDetection]] = {}
    for report in reports:
        model_entries = report.get("models", [])
        model_entry = model_entries[0] if model_entries else {}
        model = model_entry.get("model", "unknown")
        model_id = model_entry.get("model_id", "unknown")
        backend = model_entry.get("backend", "unknown")
        for page in report.get("pages", []):
            page_number = int(page["page"])
            for region in page.get("detections", []):
                by_page.setdefault(page_number, []).append(
                    RawDetection(
                        label=region["label"],
                        score=float(region["confidence"]),
                        bbox_px=region["bbox_px"],
                        model=model,
                        model_id=model_id,
                        backend=backend,
                        extra={"source_provenance": region.get("provenance", [])},
                    )
                )

    pages = []
    for page in base.get("pages", []):
        page_number = int(page["page"])
        regions = []
        for index, cluster in enumerate(cluster_detections(by_page.get(page_number, [])), start=1):
            fused = fused_detection(cluster)
            fused["id"] = f"p{page_number:04d}-r{index:04d}"
            fused["page"] = page_number
            width_px, height_px = page["width_px"], page["height_px"]
            width_pt, height_pt = page["width_pt"], page["height_pt"]
            scale_x, scale_y = width_pt / width_px, height_pt / height_px
            fused["bbox"] = [
                round(fused["bbox_px"][0] * scale_x, 3),
                round(fused["bbox_px"][1] * scale_y, 3),
                round(fused["bbox_px"][2] * scale_x, 3),
                round(fused["bbox_px"][3] * scale_y, 3),
            ]
            fused["bbox_format"] = "xyxy"
            fused["coordinate_space"] = "pdf_points"
            fused["bbox_px_format"] = "xyxy"
            fused["coordinate_space_px"] = "rendered_image_pixels"
            regions.append(fused)
        regions.sort(key=lambda item: (item["bbox"][1], item["bbox"][0], item["label"]))
        pages.append({**page, "detections": regions})

    return {
        "schema_version": "patent-layout-regions/v1",
        "document": base["document"],
        "settings": {**base.get("settings", {}), "backends": list(MODEL_DIRS)},
        "models": [
            {**report.get("models", [{}])[0], "ensemble_source": MODEL_DIRS[backend]}
            for backend, report in zip(MODEL_DIRS, reports)
        ],
        "errors": [error for report in reports for error in report.get("errors", [])],
        "pages": pages,
    }


def ensemble_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build Ensemble JSON/Markdown from model reports")
    parser.add_argument("--root", type=Path, default=PROJECT_ROOT / "benchmark/test")
    parser.add_argument("--crop-dpi", type=int, default=400)
    parser.add_argument("--crop-padding-pt", type=float, default=6.0)
    parser.add_argument("--min-table-confidence", type=float, default=0.8)
    args = parser.parse_args(argv)
    json_root = args.root / "raw_json"
    output_root = args.root / "Ensemble"
    ensemble_json_root = json_root / "Ensemble"
    names = sorted((json_root / MODEL_DIRS["pp_doclayoutv3"]).glob("*.json"))
    for pp_path in names:
        stem = pp_path.stem
        paths = [
            pp_path,
            json_root / MODEL_DIRS["doclayout_yolo"] / f"{stem}.json",
            json_root / MODEL_DIRS["tatr"] / f"{stem}.json",
        ]
        if not all(path.is_file() for path in paths):
            raise SystemExit(f"Missing one or more model reports for {stem}")
        reports = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
        ensemble = build_ensemble(reports)
        json_path = ensemble_json_root / f"{stem}.json"
        md_path = output_root / f"{stem}.md"
        crop_dir = output_root / f"{_safe_component(stem)}_tables"
        json_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(json.dumps(ensemble, ensure_ascii=False, indent=2), encoding="utf-8")
        crops = crop_table_regions(
            ensemble,
            crop_dir,
            dpi=args.crop_dpi,
            padding_pt=args.crop_padding_pt,
            min_confidence=args.min_table_confidence,
        )
        md_path.write_text(
            render_crop_report(f"Ensemble — {stem}", crops, image_prefix=crop_dir.name),
            encoding="utf-8",
        )
        print(
            f"{stem}: {sum(len(page['detections']) for page in ensemble['pages'])} regions, "
            f"{len(crops)} table crops"
        )
    return 0


# Benchmark layout models

MODEL_CONFIGS = {
    "PP-DocLayoutV3": {
        "backends": ["pp_doclayoutv3"],
        "dirs": {"pp_doclayoutv3": "pp_doclayoutv3"},
        "thresholds": {"pp_doclayoutv3": 0.2},
    },
    "DocLayout-YOLO": {
        "backends": ["doclayout_yolo"],
        "dirs": {"doclayout_yolo": "doclayout_yolo"},
        "thresholds": {"doclayout_yolo": 0.2},
    },
    "Table-Transformer-TATR": {
        "backends": ["tatr"],
        "dirs": {"tatr": "tatr"},
        "thresholds": {"tatr": 0.7},
    },
    "Ensemble": {
        "backends": ["pp_doclayoutv3", "doclayout_yolo", "tatr"],
        "dirs": {
            "pp_doclayoutv3": "pp_doclayoutv3",
            "doclayout_yolo": "doclayout_yolo",
            "tatr": "tatr",
        },
        "thresholds": {"pp_doclayoutv3": 0.2, "doclayout_yolo": 0.2, "tatr": 0.7},
    },
}


def _safe_filename(name: str) -> str:
    name = re.sub(r"[\\/:*?\"<>|]", "_", name).strip()
    return name or "patent"


def _pdfs(root: Path) -> Sequence[Path]:
    return sorted(root.rglob("*.pdf"))


def benchmark_main(argv: list[str] | None = None) -> int:
    project_root = PROJECT_ROOT
    parser = argparse.ArgumentParser(description="Run deployed patent layout models")
    parser.add_argument("--input-root", type=Path, default=project_root / "benchmark" / "识别")
    parser.add_argument("--output-root", type=Path, default=project_root / "benchmark" / "test")
    parser.add_argument("--models", nargs="+", choices=sorted(MODEL_CONFIGS), default=list(MODEL_CONFIGS))
    parser.add_argument("--dpi", type=int, default=120)
    parser.add_argument("--crop-dpi", type=int, default=400, help="DPI for final PDF table crops")
    parser.add_argument("--crop-padding-pt", type=float, default=6.0)
    parser.add_argument("--min-table-confidence", type=float, default=0.8)
    parser.add_argument("--allow-missing-backends", action="store_true")
    parser.add_argument("--device", default=None)
    args = parser.parse_args(argv)

    model_root = project_root / "vendor" / "models"
    pdfs = _pdfs(args.input_root)
    if not pdfs:
        raise SystemExit(f"No PDF files found under {args.input_root}")

    for model_name in args.models:
        config = MODEL_CONFIGS[model_name]
        locator = PatentRegionLocator(
            config["backends"],
            model_dirs={key: model_root / value for key, value in config["dirs"].items()},
            thresholds=config["thresholds"],
            dpi=args.dpi,
            device=args.device,
            pp_engine="paddle_static",
            allow_missing_backends=args.allow_missing_backends,
        )
        for number, pdf_path in enumerate(pdfs, start=1):
            stem = _safe_filename(pdf_path.stem)
            raw_path = args.output_root / "raw_json" / model_name / f"{stem}.json"
            md_path = args.output_root / model_name / f"{stem}.md"
            crop_dir = args.output_root / model_name / f"{_safe_component(stem)}_tables"
            print(f"[{model_name}] {number}/{len(pdfs)} {pdf_path.name}", flush=True)
            result = locator.locate(pdf_path, output_path=raw_path)
            md_path.parent.mkdir(parents=True, exist_ok=True)
            crops = crop_table_regions(
                result,
                crop_dir,
                pdf_path=pdf_path,
                dpi=args.crop_dpi,
                padding_pt=args.crop_padding_pt,
                min_confidence=args.min_table_confidence,
            )
            md_path.write_text(
                render_crop_report(
                    f"{model_name} — {pdf_path.stem}",
                    crops,
                    image_prefix=crop_dir.name,
                ),
                encoding="utf-8",
            )
            counts = sum(len(page["detections"]) for page in result["pages"])
            print(f"  -> {counts} regions, {len(crops)} table crops; {md_path}", flush=True)
    return 0


# Download model checkpoints

VENDOR_ROOT = PROJECT_ROOT / "vendor"
MODEL_ROOT = VENDOR_ROOT / "models"
MODEL_REPOS = {
    "pp_doclayoutv3": "PaddlePaddle/PP-DocLayoutV3",
    "doclayout_yolo": "juliozhao/DocLayout-YOLO-DocStructBench",
    "tatr": "microsoft/table-transformer-detection",
}


def download_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Download patent-layout model checkpoints")
    parser.add_argument("models", nargs="*", metavar="MODEL", help="model names; defaults to all layout models")
    parser.add_argument("--root", type=Path, default=MODEL_ROOT)
    args = parser.parse_args(argv)
    args.models = args.models or sorted(MODEL_REPOS)
    unknown = set(args.models) - MODEL_REPOS.keys()
    if unknown:
        parser.error(f"unknown models: {', '.join(sorted(unknown))}; choose from {', '.join(sorted(MODEL_REPOS))}")
    try:
        from huggingface_hub import snapshot_download
    except ImportError as exc:
        raise SystemExit("Install huggingface-hub from requirements-models.txt first") from exc

    args.root.mkdir(parents=True, exist_ok=True)
    for name in args.models:
        target = args.root / name
        print(f"downloading {MODEL_REPOS[name]} -> {target}", flush=True)
        snapshot_download(repo_id=MODEL_REPOS[name], local_dir=str(target))
    return 0


def main(argv: list[str] | None = None) -> int:
    """Dispatch layout tasks while keeping each task's arguments together."""
    commands = {
        "crop": crop_main,
        "report": report_main,
        "render-reports": render_reports_main,
        "ensemble": ensemble_main,
        "benchmark": benchmark_main,
        "download-models": download_main,
    }
    layout_commands = ("locate", "doctor", "_layout-worker")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=[*layout_commands, *commands])
    parser.add_argument("args", nargs=argparse.REMAINDER, help="arguments for the selected task")
    args = parser.parse_args(argv)
    if args.command in layout_commands:
        return locate_main([args.command, *args.args])
    return commands[args.command](args.args)


if __name__ == "__main__":
    raise SystemExit(main())
