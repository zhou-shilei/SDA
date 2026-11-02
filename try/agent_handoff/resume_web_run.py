"""Resume a failed Studio extraction using its saved OCR and an exact verified Template request."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

from dataagent2.provider import Provider, write_json


def read(path):
    return json.loads(Path(path).read_text())


def save_reports(run_dir, report, usage):
    """Persist the core outcome before rendering optional human-readable reports."""
    from dataagent2.timing import extraction_stages
    from document.run_document_pdf import write_html_report
    from units.token_usage import write_usage_report
    from units.timing import write_timing_report

    document = Path(report["result_dir"])
    identity = report["document_id"]

    def core():
        write_json(run_dir / "run_summary.json", report)
        write_json(document / "run_summary.json", report)
        write_json(document.parent / "_batch_summary.json", {"status": report["status"], "token_usage": usage,
            "extraction_mode": "dataagent2", "errors": {identity: report["error"]} if report.get("error") else {}})

    core()
    timing = {"wall_seconds": report["wall_seconds"], "token_usage": usage,
        "phase_ranking": extraction_stages(document), "stage_ranking": [], "slowest_requests": [], "events": [],
        "run_mode": "resumed_same_ocr", "note": report.get("cache_policy", "")}
    warnings = []
    for target in (document, document.parent):
        for writer in (write_usage_report, write_timing_report):
            try:
                writer(target, usage if writer is write_usage_report else timing)
            except Exception as exc:
                warnings.append({"report": writer.__name__, "directory": str(target), "error": str(exc)})
    if report["status"] != "failed":
        try:
            write_html_report(run_dir, report)
        except Exception as exc:
            warnings.append({"report": "write_html_report", "error": str(exc)})
    report["report_warnings"] = warnings
    core()
    return warnings


def finalize_saved(run_dir, *, update_web=True):
    """Finish reporting from validated saved exports without invoking any model."""
    from dataagent2.quality import audit_dataset
    from dataagent2.studio_pipeline import collect_usage

    run_dir = Path(run_dir)
    report = read(run_dir / "run_summary.json")
    workspace = None
    if update_web:
        from studio.sda_data import Workspace
        workspace = Workspace(ROOT)
        job = workspace.jobs()[report["document_id"]]
        if job["run_id"] != run_dir.parent.name or job.get("status") in {"queued", "processing"}:
            raise ValueError("Web job changed or is still active")
    document = Path(report["result_dir"])
    native_dir = document / "dataagent2"
    native_report = read(native_dir / "report.json")
    if (not all(native_report.get(k) is True for k in ("benchmark_complete", "reader_complete", "action_complete"))
            or native_report.get("question_api_failures") != 0):
        raise ValueError("Cannot finalize incomplete model processing")
    if hashlib.sha256((run_dir / "source.pdf").read_bytes()).hexdigest() != report["source_pdf_sha256"]:
        raise ValueError("PDF identity changed")
    native = read(native_dir / "dataset.json")
    sources = {int(k): v for k, v in read(native_dir / "sources.json").items()}
    quality = audit_dataset(native, sources)
    if quality["literal_errors"]:
        raise ValueError("Saved dataset failed literal provenance validation")
    dataset_path = document / "dataset.json"
    dataset_hash = hashlib.sha256(dataset_path.read_bytes()).hexdigest()
    dataset = read(dataset_path)
    if len(dataset["records"]) != len(native["observations"]) or dataset["status"] == "failed":
        raise ValueError("Business export is missing or inconsistent")
    metadata_dir = run_dir / "recovery"
    metadata_dir.mkdir(parents=True, exist_ok=True)
    backup = metadata_dir / "run_summary.before-finalize.json"
    if not backup.exists():
        write_json(backup, report)
    timing_path = document / "_timings.json"
    saved_seconds = read(timing_path).get("wall_seconds") if timing_path.exists() else None
    if saved_seconds is None or saved_seconds < report.get("wall_seconds", 0):
        raise ValueError("Missing measured cumulative time for report-only finalization")
    previous_seconds = report.get("wall_seconds", 0)
    if not any(p["name"] == "document_resume" for p in report["phases"]):
        report["phases"].append({"name": "document_resume", "seconds": saved_seconds - previous_seconds,
            "returncode": 1, "accepted_partial": True, "extraction_complete": True,
            "failure_scope": "report_generation", "error": "KeyError: slowest_requests",
            "timing_basis": "previously persisted measured cumulative runtime"})
    preprocessing = dataset.get("preprocessing", {})
    finalized_at = datetime.now(timezone.utc).isoformat()
    report.update(status="partial" if preprocessing.get("status") != "complete" or dataset["status"] != "complete" else "complete",
        resumed=True, wall_seconds=saved_seconds, dataset_status=dataset["status"],
        preprocessing_status=preprocessing.get("status"), records=len(dataset["records"]),
        entities=len(dataset["entities"]), experiments=len(dataset["experiments"]), relations=len(dataset["relations"]),
        recovery_benchmark_complete=True, finished_at_utc=finalized_at,
        processing_completed_at_utc=datetime.fromtimestamp(dataset_path.stat().st_mtime, timezone.utc).isoformat(),
        wall_time_scope="Sum of actual automated processing intervals; developer pause and report-only finalization excluded.",
        cache_policy="Same saved OCR; exact historical Template request revalidated; new Reader/Action/gap processing; failed attempts retained.",
        report_finalization={"status": "repaired", "completed_at": finalized_at,
                             "model_calls_made": 0, "business_dataset_sha256": dataset_hash})
    report.pop("error", None)
    usage = collect_usage(document / "shared_seed", native_dir, report["document_id"])
    report["token_usage"] = usage
    save_reports(run_dir, report, usage)
    if hashlib.sha256(dataset_path.read_bytes()).hexdigest() != dataset_hash:
        raise ValueError("Report-only finalization changed the business dataset")
    if workspace is not None:
        job = workspace.jobs()[report["document_id"]]
        if job["run_id"] != run_dir.parent.name or job.get("status") in {"queued", "processing"}:
            raise ValueError("Web job changed or is still active")
        workspace.update_job(report["document_id"], status="completed", stage="completed", progress=100,
            worker_pid=None, error=None, semantic_status=report["status"],
            preprocessing_status=report["preprocessing_status"], finished_at=finalized_at,
            report_finalization=report["report_finalization"])
    write_json(metadata_dir / "finalization.json", {"status": "repaired", "model_calls_made": 0,
        "dataset_sha256_before": dataset_hash, "dataset_sha256_after": hashlib.sha256(dataset_path.read_bytes()).hexdigest(),
        "reader_complete": True, "action_complete": True, "question_api_failures": 0,
        "native_summary": native["summary"], "report_warnings": report["report_warnings"]})
    print(json.dumps({"status": "completed", "business_status": report["status"],
        "records": report["records"], "native_summary": native["summary"], "model_calls_made": 0}, ensure_ascii=False), flush=True)
    return report


def restore_seed(document, checkpoint, source_request):
    from dataagent.agent.graph import build_seed_graph, initial_state
    from dataagent.agent.schemas import AgentConfig
    from dataagent.llm_runtime import CodexProvider
    from dataagent.records import export_measurements

    seed = document / "shared_seed"
    saved = read(source_request)
    attempt = next(p for p in source_request.parent.glob("attempt-*")
                   if (p / "answer.json").exists() and (p / "events.jsonl").exists())
    events = [json.loads(l) for l in (attempt / "events.jsonl").read_text().splitlines() if l.strip()]
    completed = next((e for e in reversed(events) if e.get("type") == "turn.completed"), None)
    if not completed or any(e.get("type") == "turn.failed" for e in events):
        raise ValueError("Template origin has no successful terminal completion")
    old_invoke = CodexProvider._invoke_cached

    def invoke(provider, request, schema, images, fingerprint, key):
        if not request["task"].startswith("document-template-"):
            return old_invoke(provider, request, schema, images, fingerprint, key)
        if fingerprint != saved:
            raise ValueError("Template request changed; refusing to reuse a different response")
        result, recovered = provider._validate_result(read(attempt / "answer.json"), request)
        audit = seed / "requests" / f"{key}-exact-recovery"
        audit.mkdir(parents=True, exist_ok=True)
        write_json(audit / "input.json", fingerprint)
        write_json(audit / "validated.json", result)
        shutil.copyfile(attempt / "answer.json", audit / "original_answer.json")
        shutil.copyfile(attempt / "events.jsonl", audit / "original_events.jsonl")
        metadata = {"provider": "codex_cli", "task": request["task"], "model": provider.model,
            "reasoning_effort": provider.effort, "input_hash": key, "status": "validated",
            "cache_hit": True, "response_source": "exact_saved_request_revalidated",
            "usage": completed.get("usage", {}), "recovered": recovered,
            "cache_origin": str(source_request), "audit_dir": str(audit),
            "completed_at": datetime.now(timezone.utc).isoformat(), "total_elapsed_seconds": 0,
            "retry_count": 0, "retry_seconds": 0}
        write_json(audit / "recovery_metadata.json", metadata)
        provider._record(metadata)
        print("recovery: exact Template request and current schema/source validation passed; no new Template call", flush=True)
        return result, metadata

    config = AgentConfig(document_id=checkpoint["document_id"], input_dir=checkpoint["inputs"]["image_dir"],
        output_dir=str(seed), sda_input_file=str(seed / "sda_input.json"), selection_mode="all",
        run_ocr=False, run_sequence_page_ocr=False, copy_images=False,
        semantic_provider="codex", llm_model=saved["model"], llm_effort=saved["effort"],
        llm_timeout=600, llm_workers=0, sequence_workers=1)
    CodexProvider._invoke_cached = invoke
    try:
        state = build_seed_graph().invoke(initial_state(config), {"recursion_limit": 100})
    finally:
        CodexProvider._invoke_cached = old_invoke
    measurements = export_measurements(config.document_id, state["ocr_results"],
        state["table_classifications"], state["document_template"], [])
    for name, value in [("agent_state.json", state), ("document_template.json", state["document_template"]),
                        ("assay_measurements.json", measurements["measurements"])]:
        write_json(seed / name, value)
    return state


def worker(run_dir, source_request):
    from dataagent2.inputs import load_input
    from dataagent2.pipeline import run
    from dataagent2.studio_pipeline import collect_usage, export_result

    started = time.monotonic()
    report = read(run_dir / "run_summary.json")
    identity = report["document_id"]
    document = Path(report["result_dir"])
    checkpoint = read(document / "dataagent2_checkpoint.json")
    if hashlib.sha256((run_dir / "source.pdf").read_bytes()).hexdigest() != report["source_pdf_sha256"]:
        raise ValueError("PDF identity changed")
    if not read(Path(checkpoint["inputs"]["structured_dir"]) / "page_ocr/summary.json")["full_document_ocr_complete"]:
        raise ValueError("Saved page OCR is incomplete")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    write_json(run_dir / f"run_summary.before-resume-{stamp}.json", report)
    perf = document / "shared_seed/pipeline_performance.json"
    if perf.exists():
        shutil.copyfile(perf, perf.with_name(f"pipeline_performance.before-resume-{stamp}.json"))
    report.update(status="running", resumed=True)
    report.pop("error", None)
    write_json(run_dir / "run_summary.json", report)
    error = None
    try:
        restore_seed(document, checkpoint, source_request)
        data = load_input(document / "shared_seed")
        fingerprint = read(source_request)
        provider = Provider(document / "dataagent2", model=fingerprint["model"], effort=fingerprint["effort"],
                            timeout=600, action_timeout=1800, action_effort="medium")
        native = run(data, provider, document / "dataagent2", workers=0, chunk_chars=24000, max_questions=None)
        final = read(document / "dataagent2/dataset.json")
        dataset = export_result(data, final, document / "shared_seed", document)
        preprocessing = read(Path(checkpoint["inputs"]["structured_dir"]) / "summary.json")
        dataset["preprocessing"] = {"status": preprocessing["status"], "document_file": report["structured_document"]}
        if not native["benchmark_complete"]:
            raise RuntimeError("Extraction incomplete; partial results and request errors are preserved")
        report.update(status="partial" if preprocessing["status"] != "complete" or dataset["status"] != "complete" else "complete",
            dataset_status=dataset["status"], preprocessing_status=preprocessing["status"],
            records=len(dataset["records"]), entities=len(dataset["entities"]),
            experiments=len(dataset["experiments"]), relations=len(dataset["relations"]),
            total_pages=preprocessing["total_pages"], table_count=preprocessing["tables"],
            recovery_benchmark_complete=True)
        write_json(document / "dataset.json", dataset)
    except BaseException as exc:
        error = f"{type(exc).__name__}: {exc}"
        report.update(status="failed", error=error)
        raise
    finally:
        seconds = time.monotonic() - started
        report["phases"].append({"name": "document_resume", "seconds": seconds, "returncode": int(error is not None),
            "mode": "reuse_saved_ocr_and_exact_validated_template"})
        report.update(wall_seconds=report.get("wall_seconds", 0) + seconds,
            wall_time_scope="Sum of actual automated processing intervals; developer pause excluded.",
            finished_at_utc=datetime.now(timezone.utc).isoformat(),
            cache_policy="Same saved OCR; exact historical Template request revalidated; new Reader/Action/gap processing; failed attempts retained.")
        usage = collect_usage(document / "shared_seed", document / "dataagent2", identity)
        report["token_usage"] = usage
        save_reports(run_dir, report, usage)


def supervise(run_dir, source_request):
    from studio.sda_data import Workspace, now
    from studio.server import StudioServer

    workspace = Workspace(ROOT)
    identity = read(run_dir / "run_summary.json")["document_id"]
    job = workspace.jobs()[identity]
    if job["run_id"] != run_dir.parent.name or job.get("status") in {"processing", "queued"}:
        raise ValueError("Web job changed or is already running")
    recovery_dir = run_dir / "recovery"
    recovery_dir.mkdir(exist_ok=True)
    write_json(recovery_dir / "job.before_resume.json", job)
    workspace.update_job(identity, status="processing", stage="template", progress=74,
        stage_current=None, stage_total=None, error=None, finished_at=None,
        previous_started_at=job.get("started_at"), started_at=now(),
        recovery_started_at=now(), recovery_mode="same_ocr_exact_template_revalidation")
    target = SimpleNamespace(workspace=workspace, closing=threading.Event(), process_lock=threading.Lock(), processes={})
    target.update_progress = lambda document_id, line: StudioServer.update_progress(target, document_id, line)
    command = [sys.executable, str(Path(__file__).resolve()), "--run-dir", str(run_dir),
               "--template-source", str(source_request), "--worker"]
    code = 1
    try:
        with (run_dir.parent / "pipeline.log").open("a") as log:
            log.write("\nRecovery: reuse existing OCR; verify exact saved Template request; continue extraction.\n")
            code = StudioServer.command(target, identity, command, log)
    finally:
        report = read(run_dir / "run_summary.json")
        success = code == 0 and report.get("status") in {"partial", "complete"}
        workspace.update_job(identity, status="completed" if success else "failed",
            stage="completed" if success else "failed", progress=100 if success else workspace.jobs()[identity].get("progress", 74),
            worker_pid=None, error=None if success else report.get("error", "Recovery subprocess failed"),
            semantic_status=report.get("status"), preprocessing_status=report.get("preprocessing_status"), finished_at=now())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--template-source", type=Path)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--finalize-only", action="store_true", help="Repair reports from completed saved processing; no model calls")
    args = parser.parse_args()
    if args.finalize_only:
        finalize_saved(args.run_dir.resolve())
    else:
        if args.template_source is None:
            parser.error("--template-source is required unless using --finalize-only")
        (worker if args.worker else supervise)(args.run_dir.resolve(), args.template_source.resolve())
