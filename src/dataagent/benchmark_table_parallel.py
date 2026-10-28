"""Measure fresh image review and classification over an existing OCR snapshot."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import time

from .llm_runtime import CodexProvider, json_write
from .llm_table_agents import LLMTableClassificationAgent
from document.sequence_styles import review_prepared, validate_style_source


def run(source, output, *, workers=0, model=None, effort=None):
    source, output = Path(source).resolve(), Path(output).resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError("Use a fresh benchmark output directory")
    if not 0 <= workers <= 8:
        raise ValueError("workers must be 0 (all tables) or 1–8")
    output.mkdir(parents=True, exist_ok=True)
    prepared = json.loads((source / "sda_input.json").read_text())
    previous_styles = json.loads((source / "sequence_styles.json").read_text())
    previous_classes = json.loads((source / "classification_progress.json").read_text())
    previous_timing = json.loads((source / "classification_performance.json").read_text())
    settings = next((t["request"] for t in previous_styles["tables"]), {})
    model, effort = model or settings.get("model", "gpt-6.1-sol"), effort or settings.get("reasoning_effort", "xhigh")
    # Keep the exact original OCR, images and contexts, but no model answers.
    for table in prepared["tables"]:
        table.pop("sequence_style_sources", None)
    prepared.pop("sequence_style_review", None)
    json_write(output / "sda_input.original.json", prepared)
    provider = CodexProvider(output / "cache", model=model, effort=effort,
                             audit_path=output / "llm_calls.jsonl")
    provider.cache_enabled = False
    summary = {
        "source": str(source), "output": str(output), "document_id": prepared["document_id"],
        "started_at_utc": datetime.now(timezone.utc).isoformat(), "status": "running",
        "workers": workers, "model": model, "effort": effort, "phases": [],
        "scope": "Fresh original-image review and table classification; reuses exact saved OCR. Excludes PDF preprocessing, template and Reader.",
        "baseline": {
            "style_seconds": previous_styles.get("performance", {}).get("elapsed_seconds",
                round(sum(t["request"].get("total_elapsed_seconds", 0) for t in previous_styles["tables"]), 3)),
            "style_timing_basis": "wall time" if "performance" in previous_styles else "historical serial request sum",
            "classification_seconds": previous_timing["elapsed_seconds"],
            "classification_workers": previous_timing["workers"],
            "style_confirmed_cells": previous_styles["summary"]["reviewed_cells"],
            "style_review_findings": previous_styles["summary"]["failures"],
            "classified_tables": len(previous_classes),
        },
    }
    started = time.monotonic()
    json_write(output / "benchmark.json", summary)
    try:
        tick = time.monotonic()
        styles = review_prepared(prepared, provider, output, max_workers=workers)
        summary["phases"].append({"name": "image_review", "seconds": round(time.monotonic() - tick, 3)})
        originals = {c["source_ref"]: c for t in prepared["tables"] for row in t["rows"] for c in row}
        for s in styles["sources"]:
            validate_style_source(s, originals)
        json_write(output / "sda_input.json", prepared)
        json_write(output / "benchmark.json", summary)
        tick = time.monotonic()
        roots = sorted({Path(t["image_path"]).parent for t in prepared["tables"] if t.get("image_path")})
        classifications = LLMTableClassificationAgent(
            provider, image_roots=roots, max_workers=workers, coarse_triage=True,
        ).run(prepared["tables"], prepared.get("table_contexts", []))
        summary["phases"].append({"name": "classification", "seconds": round(time.monotonic() - tick, 3)})
        json_write(output / "classifications.json", classifications)
        old = {c["table_id"]: c for c in previous_classes}
        summary["quality"] = {
            "input_tables": len(prepared["tables"]), "classified_tables": len(classifications),
            "missing_tables": sorted({t["source_id"] for t in prepared["tables"]} - {c["table_id"] for c in classifications}),
            "style_confirmed_cells": len(styles["sources"]),
            "style_review_findings": len(styles["failures"]),
            "style_request_failures": [f for f in styles["failures"] if "table_id" in f],
            "classification_request_failures": [c["table_id"] for c in classifications if c.get("classifier", {}).get("mode") == "error"],
            "tables_needing_review": [c["table_id"] for c in classifications if c.get("review_required")],
            "changed_labels_or_decisions": [{"table_id": c["table_id"],
                "before": {k: old[c["table_id"]].get(k) for k in ("primary_label", "decision")},
                "after": {k: c.get(k) for k in ("primary_label", "decision")}}
                for c in classifications if c["table_id"] in old and any(
                    c.get(k) != old[c["table_id"]].get(k) for k in ("primary_label", "decision"))],
            "note": "Source validation is retained. Differences from a stochastic historical run are not an accuracy score.",
        }
        summary["status"] = "completed" if not any(summary["quality"][k] for k in (
            "missing_tables", "style_request_failures", "classification_request_failures")) else "partial"
    except Exception as exc:
        summary.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        summary["wall_seconds"] = round(time.monotonic() - started, 3)
        summary["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        summary["model_requests"] = len(provider.calls)
        summary["cache_hits"] = sum(bool(c.get("cache_hit")) for c in provider.calls)
        json_write(output / "benchmark.json", summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="Existing shared_seed directory")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=0, help="0 means one worker per table")
    args = parser.parse_args()
    result = run(args.source, args.output, workers=args.workers)
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0 if result["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
