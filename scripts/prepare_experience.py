"""Package existing, source-backed cases without invoking OCR or model calls."""
import argparse
import gzip
import hashlib
import json
from pathlib import Path
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from studio.sda_data import Workspace, table_record

CASES = [
    ("US20150183856A1", "入门 · 小型专利案例", "从少量记录开始，查看序列、实验数值及对应原文。", "Starter · Compact patent", "Start with a smaller set of sequences, measurements and source evidence."),
    ("WO2026086808A1", "标准 · 抗 TSLP 抗体", "体验 VH/VL 序列、实验标签与 PDF 证据之间的联查。", "Standard · Anti-TSLP antibodies", "Explore paired chains, assay labels and linked PDF evidence."),
    ("WO2025108394A1", "标准 · 序列与实验联查", "按实验筛选记录，核对序列来源并导出所需数据。", "Standard · Sequences and assays", "Filter measurements, inspect sequence sources and export records."),
    ("WO2024020591A9", "进阶 · 长篇专利", "体验长篇原文、多表证据和待复核信息的审阅流程。", "Advanced · Long patent", "Review a longer document with multiple tables and unresolved evidence."),
]


def portable(value, source):
    if isinstance(value, dict):
        return {key: portable(item, source) for key, item in value.items()
                if key not in {"extraction_evidence", "mapping_provenance", "_codex_auth_id"}}
    if isinstance(value, list):
        return [portable(item, source) for item in value]
    if isinstance(value, str) and value.startswith(str(source) + "/"):
        return "source/" + str(Path(value).relative_to(source))
    return value


def package(source, output):
    if output.exists():
        raise ValueError("Output already exists; use a new directory to preserve existing cases")
    output.mkdir(parents=True)
    workspace = Workspace(source)
    manifest = {"package_id": "developer-preview-20261104-v1", "version": "0.1.0-preview.1",
                "cases": [], "note": "真实专利提取结果；自动提取的缺失、冲突和待复核状态均保留。"}
    for identity, title, description, title_en, description_en in CASES:
        print(f"Preparing {identity}", flush=True)
        bundle = workspace.bundle(identity)
        directory = output / "cases" / identity
        directory.mkdir(parents=True)
        records = []
        for record in bundle["records"]:
            row = table_record(record)
            row["sda_fields"] = record.get("sda_fields", {})
            row["sda_sources"] = record.get("sda_sources", [])
            records.append(portable(row, source))
        compact = {key: portable(bundle.get(key, {}), source) for key in (
            "contexts", "candidates", "classifications", "ocr", "runtime", "pages", "template",
            "pdf_form", "coverage", "missing", "tst", "document_report")}
        compact["records"] = records
        compact["dataset"] = {key: portable(bundle["dataset"].get(key), source) for key in (
            "document_id", "schema_version", "status", "extraction_mode", "summary", "issues")}
        compact["dataset"]["records"] = [{"record_id": row["record_id"]} for row in records]
        run_keys = {"document_id", "status", "run_kind", "extraction_engine", "extraction_mode", "wall_seconds",
                    "started_at_utc", "finished_at_utc", "records", "experiments", "phases", "token_usage", "resumed"}
        compact["run"] = {key: portable(value, source) for key, value in bundle["run"].items() if key in run_keys}
        compact["crops"] = {}
        shutil.copyfile(bundle["pdf"], directory / "source.pdf")
        for key, path in bundle["crops"].items():
            name = f"assets/crop-{key}{path.suffix}"
            destination = directory / name
            destination.parent.mkdir(exist_ok=True)
            shutil.copyfile(path, destination)
            compact["crops"][key] = name
        (directory / "dataset.json").write_text(json.dumps(compact["dataset"], ensure_ascii=False), encoding="utf-8")
        (directory / "run_summary.json").write_text(json.dumps(compact["run"], ensure_ascii=False), encoding="utf-8")
        compressed = gzip.compress(json.dumps(compact, ensure_ascii=False, separators=(",", ":")).encode(), mtime=0)
        (directory / "bundle.json.gz").write_bytes(compressed)
        case = {"document_id": identity, "filename": identity + ".pdf", "title": title, "description": description,
                "title_en": title_en, "description_en": description_en,
                "record_count": len(records), "page_count": bundle["pages"]["page_count"],
                "paired_count": sum(bool(row.get("vh") and row.get("vl")) for row in records),
                "experiment_count": len({row.get("experiment_id") for row in records} - {None, ""}),
                "status": compact["dataset"]["status"],
                "files": {str(path.relative_to(directory)): hashlib.sha256(path.read_bytes()).hexdigest()
                          for path in sorted(directory.rglob("*")) if path.is_file()}}
        manifest["cases"].append(case)
        print(f"Packaged {identity}: {len(records)} records, {case['page_count']} pages, {len(compressed)} snapshot bytes", flush=True)
        del bundle, compact, records
        workspace.cache.clear()
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[1] / "demo")
    args = parser.parse_args()
    package(args.source_root.resolve(), args.output.resolve())
