"""Benchmark the production table OCR branch on identical, isolated raster crops."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
if os.environ.get("SDA_OCR_BENCH_SOURCE"):
    sys.path.insert(0, os.environ["SDA_OCR_BENCH_SOURCE"])


def read(path):
    return json.loads(Path(path).read_text())


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def table_signature(row):
    return {"table_id": row["table_id"], "text": row.get("text"),
            "cells": [{k: c.get(k) for k in ("ref", "text", "row", "column", "rowspan", "colspan")}
                      for c in row.get("cells", [])],
            "sequence_status": {k: row.get("sequence_ocr", {}).get(k)
                                for k in ("status", "successful_cells", "failed_cells")}}


def worker(args):
    from document.pdf2doc import OCRConfig, _recognize_table_content, recognize_content
    trial = args.output.resolve()
    layout = read(trial / "layout.json")
    started = time.monotonic()
    if args.with_page_ocr:
        result = recognize_content(layout, trial / "crops", trial / "result",
            config=OCRConfig(device="gpu:0", cpu_threads=4, backend="paddle", workers=1, render_prefetch=4),
            table_backend="vl", table_batch_size=1, table_workers=args.workers, parallel=True)
    else:
        result = _recognize_table_content(layout, trial / "crops", trial / "result",
            OCRConfig(device="gpu:0", cpu_threads=4), "vl", 1, True, args.workers)
    rows = result.pop("tables")
    save(trial / "result.json", {"seconds": time.monotonic() - started, "runtime": result["runtime"],
        "errors": result["errors"], "page_summary": result.get("page_summary"),
        "successful_tables": sum(r["status"] == "success" for r in rows),
        "table_signatures": [table_signature(r) for r in rows]})


def benchmark(layout_path, output, workers, *, max_seconds=1200, memory_limit_mib=79000, with_page_ocr=False):
    output = output.resolve()
    if output.exists():
        raise ValueError("Use a fresh trial directory")
    output.mkdir(parents=True)
    # All spawned predictors import the exact source snapshot for this trial.
    # This permits fixing the implementation between trials without mixing code
    # inside a running process pool.
    code_hashes = {}
    for src in (ROOT / "src/document").glob("*.py"):
        target = output / "source/document" / src.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, target)
        code_hashes["document/" + src.name] = hashlib.sha256(src.read_bytes()).hexdigest()
    layout = read(layout_path)
    crops = [c for c in layout["images"] if c["label"] == "table"]
    original_hashes = {}
    for c in crops:
        src = layout_path.parent / c["image"]
        raw = src.read_bytes()
        checksum = hashlib.sha256(raw).hexdigest()
        if checksum != c["sha256"]:
            raise ValueError("Saved crop identity changed: " + c["id"])
        target = output / "crops" / c["image"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        original_hashes[c["id"]] = checksum
    save(output / "layout.json", {**layout, "images": crops})
    save(output / "configuration.json", {"workers": workers, "batch_size": 1, "device": "gpu:0",
        "source_layout": str(layout_path.resolve()), "source_hashes": original_hashes,
        "scope": "Production parallel page+table OCR" if with_page_ocr else "Production table OCR including PP-OCRv6 sequence refinement; no PDF rendering/text OCR/LLM calls",
        "with_page_ocr": with_page_ocr,
        "page_ocr_settings": {"backend": "paddle", "workers": 1, "render_prefetch": 4,
                              "max_side": 2600, "batch_size": 6} if with_page_ocr else None,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "python": sys.executable, "max_seconds": max_seconds, "memory_limit_mib": memory_limit_mib})
    save(output / "code_hashes.json", code_hashes)
    command = [sys.executable, str(Path(__file__).resolve()), "--worker", "--workers", str(workers),
               "--output", str(output)]
    if with_page_ocr:
        command.append("--with-page-ocr")
    samples = []
    log = output / "worker.log"
    started = time.monotonic()
    termination = None
    with log.open("w") as stream:
        proc = subprocess.Popen(command, cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT,
                                start_new_session=True, env={**os.environ,
                                    "SDA_OCR_BENCH_SOURCE": str(output / "source")})
        save(output / "process.json", {"pid": proc.pid, "command": command})
        while proc.poll() is None:
            if time.monotonic() - started > max_seconds:
                termination = "benchmark_timeout"
            tail = log.read_bytes()[-20000:].decode(errors="replace")
            if "ResourceExhaustedError" in tail or "Out of memory error on GPU" in tail:
                termination = "CUDA_out_of_memory"
            try:
                raw = subprocess.check_output(["nvidia-smi", "--query-gpu=memory.used,utilization.gpu",
                    "--format=csv,noheader,nounits", "--id=0"], text=True, timeout=5)
                memory, utilization = [int(v.strip()) for v in raw.strip().split(",")]
                sample = {"seconds": round(time.monotonic() - started, 3),
                          "memory_mib": memory, "gpu_percent": utilization}
                samples.append(sample)
                with (output / "gpu.jsonl").open("a") as f:
                    f.write(json.dumps(sample) + "\n")
                if memory > memory_limit_mib:
                    termination = "GPU_memory_limit"
            except (OSError, ValueError, subprocess.SubprocessError):
                pass
            if termination:
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait(timeout=15)
                break
            time.sleep(.5)
    elapsed = time.monotonic() - started
    result = read(output / "result.json") if (output / "result.json").exists() else {}
    summary = {"workers": workers, "seconds_including_process_startup": elapsed,
        "table_branch_seconds": result.get("seconds") if not with_page_ocr else (
            result["runtime"]["table"]["finished"] - result["runtime"]["table"]["started"] if result else None),
        "ocr_wall_seconds": result.get("seconds"), "returncode": proc.returncode,
        "termination": termination, "peak_gpu_mib": max((s["memory_mib"] for s in samples), default=None),
        "mean_gpu_percent": sum(s["gpu_percent"] for s in samples) / len(samples) if samples else None,
        "gpu_samples": len(samples), "successful_tables": result.get("successful_tables"),
        "expected_tables": len(crops), "errors": result.get("errors", []), "trial": str(output)}
    summary["with_page_ocr"] = with_page_ocr
    if with_page_ocr:
        summary["page_summary"] = result.get("page_summary")
        summary["page_branch_seconds"] = (result["runtime"]["text"]["finished"] - result["runtime"]["text"]["started"]
                                          if result else None)
    events = []
    for line in log.read_text(errors="replace").splitlines():
        if line.startswith("table-ocr "):
            try:
                events.append(json.loads(line.split(" ", 1)[1]))
            except ValueError:
                pass
    summary["completed_inference_batches"] = sum(e["phase"] == "batch_complete" for e in events)
    summary["failed_inference_batches"] = sum(e["phase"] == "batch_failed" for e in events)
    # Compare actual text/physical cells and sequence-refinement outcomes,
    # excluding relocated paths and page-OCR-only geometry enrichment.
    baseline_path = layout_path.parent.parent / "table_ocr/tables.jsonl"
    if result and baseline_path.exists():
        original = {r["table_id"]: table_signature(r) for line in baseline_path.read_text().splitlines()
                    if (r := json.loads(line)).get("status") == "success"}
        diffs = []
        for row in result["table_signatures"]:
            prior = original.get(row["table_id"])
            if row != prior:
                diffs.append({"table_id": row["table_id"], "changed_fields": [k for k in row if prior is None or row[k] != prior.get(k)]})
        summary["versus_saved_1_worker"] = {"matching_tables": len(result["table_signatures"]) - len(diffs), "differences": diffs}
    save(output / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return summary


def summarize_trials(directory, destination, selection):
    """Publish a compact report; large crops, native logs and OCR stay local."""
    directory = Path(directory).resolve()
    trials = []
    for folder in sorted(directory.iterdir()):
        if not (folder / "summary.json").is_file():
            continue
        summary = read(folder / "summary.json")
        config = read(folder / "configuration.json")
        summary["trial"] = str(folder.relative_to(ROOT))
        summary["profile"] = folder.name
        summary["idle_cache_release"] = "cache" in folder.name or "page" in folder.name
        summary["gpu_memory_guard_mib"] = config["memory_limit_mib"]
        summary["valid_full_run"] = (summary["returncode"] == 0 and not summary["termination"]
            and summary["successful_tables"] == summary["expected_tables"] and not summary["errors"]
            and summary.get("versus_saved_1_worker", {}).get("matching_tables") == summary["expected_tables"])
        if summary["termination"]:
            summary["successful_tables"] = None  # An interrupted full result is unknown, not zero accuracy.
        trials.append(summary)
    config = read(next(directory.glob("*/configuration.json")))
    layout = Path(config["source_layout"])
    original = read(layout.parent.parent / "recognition_summary.json")["runtime"]["table"]
    version = subprocess.check_output([sys.executable, "-c",
        "import importlib.metadata,json; print(json.dumps({p:importlib.metadata.version(p) for p in ['paddleocr','paddlex','paddlepaddle-gpu']}))"], text=True)
    report = {"date": "2026-11-02", "document": "49-Sructure", "table_count": len(config["source_hashes"]),
        "gpu": "NVIDIA A100-SXM4-80GB", "model": "PaddleOCR-VL-1.6 + PP-OCRv6 medium sequence refinement",
        "package_versions": json.loads(version), "batch_size": 1, "cpu_threads_per_worker": 4,
        "input_crop_sha256": config["source_hashes"],
        "historical_baseline": {"workers": 1, "table_branch_seconds": original["finished"] - original["started"],
            "scope": "Previously saved same 29 crops, parallel page OCR; not a new one-worker measurement"},
        "trials": trials, "studio_selection": selection,
        "notes": ["Fresh models/process pools in every trial; includes initialization, sequence refinement and shutdown.",
            "All crops are byte-verified independent copies; original patent results are unchanged.",
            "No image size, precision, generation or sequence-recognition change.",
            "GPU memory sampled about every 0.5 seconds; guard termination is not a measured CUDA OOM.",
            "Table-only and parallel full-page+table timings have different scopes; no LLM calls.",
            "Exact OCR equality checks transcription/structure stability, not independent recognition accuracy."]}
    save(destination, report)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--layout", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, required=True)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--max-seconds", type=int, default=1200)
    parser.add_argument("--with-page-ocr", action="store_true", help="Also validate the production parallel text OCR branch")
    args = parser.parse_args()
    if args.workers < 1:
        parser.error("workers must be positive")
    if args.worker:
        worker(args)
    else:
        if not args.layout:
            parser.error("--layout is required for a benchmark")
        benchmark(args.layout.resolve(), args.output, args.workers, max_seconds=args.max_seconds,
                  with_page_ocr=args.with_page_ocr)
