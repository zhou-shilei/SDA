"""Continue this run's saved fresh OCR after its launcher was terminated.

Does not change model evidence or synthesize any model answer. A surviving
Template call can be reused only with the exact request and a completion event.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from dataagent2.provider import Provider, write_json
from dataagent2.inputs import load_input
from dataagent2.pipeline import run
from dataagent2.studio_pipeline import collect_usage, export_result
from document.run_document_pdf import write_html_report
from units.token_usage import write_usage_report
from units.timing import write_timing_report
from dataagent2.timing import extraction_stages


def recover_seed(run_dir, document, context):
    from dataagent.agent.schemas import AgentConfig
    from dataagent.agent.graph import build_seed_graph, initial_state
    from dataagent.llm_runtime import CodexProvider
    from dataagent.records import export_measurements

    seed = document / "shared_seed"
    if (seed / "agent_state.json").exists():
        return json.loads((seed / "agent_state.json").read_text())
    saved = next(seed.glob("requests/94fcd*/input.json"))
    fingerprint = json.loads(saved.read_text())
    old_invoke = CodexProvider._invoke_cached

    def invoke(provider, request, schema, images, current, key):
        if current != fingerprint:
            return old_invoke(provider, request, schema, images, current, key)
        attempt = next(saved.parent.glob("attempt-*"))
        deadline = time.monotonic() + 600
        events = []
        while time.monotonic() < deadline:
            try:
                events = [json.loads(line) for line in (attempt / "events.recovered.jsonl").read_text().splitlines() if line]
            except (OSError, ValueError):
                pass
            completed = next((e for e in reversed(events) if e.get("type") == "turn.completed"), None)
            if completed and (attempt / "answer.json").exists():
                break
            if any(e.get("type") == "turn.failed" for e in events):
                break
            print("recovery: waiting for the surviving original Template call", flush=True)
            time.sleep(15)
        completed = next((e for e in reversed(events) if e.get("type") == "turn.completed"), None)
        if completed and not any(e.get("type") == "turn.failed" for e in events):
            response = json.loads((attempt / "answer.json").read_text())
            try:
                result, recovered = provider._validate_result(response, request)
            except Exception as error:
                write_json(attempt / "recovery_validation.json", {"status": "rejected", "error": str(error)})
            else:
                shutil.copyfile(attempt / "events.recovered.jsonl", attempt / "events.jsonl")
                metadata = {"provider": "codex_cli", "model": provider.model, "reasoning_effort": provider.effort,
                    "task": request["task"], "input_hash": key, "cache_hit": False, "cache_enabled": False,
                    "status": "validated", "response_source": "surviving_original_attempt",
                    "recovered": recovered, "launcher_recovered": True,
                    "completed_at": datetime.now(timezone.utc).isoformat(), "usage": completed.get("usage", {}),
                    "audit_dir": str(attempt), "request_event_files": [str(attempt / "events.jsonl")],
                    "duration_note": "Launcher terminated; terminal completion and full usage recovered from the original child stdout."}
                provider._record(metadata)
                print("recovery: original Template answer passed current source/schema checks", flush=True)
                return result, metadata
        print("recovery: original Template answer unavailable/invalid; making a recorded fresh request", flush=True)
        return old_invoke(provider, request, schema, images, current, key)

    CodexProvider._invoke_cached = invoke
    config = AgentConfig(document_id="49-Sructure", input_dir=str((run_dir / "structured/layout").resolve()),
        output_dir=str(seed), sda_input_file=str(seed / "sda_input.json"),
        selection_mode="all", run_ocr=False, run_sequence_page_ocr=False, copy_images=False,
        semantic_provider="codex", llm_model=context["model"], llm_effort=context["effort"],
        llm_workers=0, sequence_workers=1)
    try:
        state = build_seed_graph().invoke(initial_state(config), {"recursion_limit": 100})
    finally:
        CodexProvider._invoke_cached = old_invoke
    measurements = export_measurements("49-Sructure", state["ocr_results"], state["table_classifications"],
                                       state["document_template"], [])
    for name, value in (("agent_state.json", state), ("document_template.json", state["document_template"]),
                        ("assay_measurements.json", measurements["measurements"])):
        write_json(seed / name, value)
    return state


def main(run_dir):
    run_dir = run_dir.resolve()
    context = json.loads((run_dir / "comparison_context.json").read_text())
    report = json.loads((run_dir / "run_summary.json").read_text())
    document = run_dir / "results/49-Sructure"
    started = datetime.fromisoformat(report["started_at_utc"])
    prepared = run_dir / "structured/document.json"
    evidence = json.loads(prepared.read_text())
    if hashlib.sha256((run_dir / "source.pdf").read_bytes()).hexdigest() != report["source_pdf_sha256"]:
        raise ValueError("Original PDF identity changed")
    if evidence["source_pdf_sha256"] != report["source_pdf_sha256"]:
        raise ValueError("Structured OCR belongs to another PDF")
    page_summary = json.loads((run_dir / "structured/page_ocr/summary.json").read_text())
    if not page_summary["full_document_ocr_complete"]:
        raise ValueError("Cannot continue incomplete PDF OCR")
    recovery = {"started_at_utc": datetime.now(timezone.utc).isoformat(),
        "reason": "Continue interrupted or incomplete extraction; previous failures remain in native report/request logs.",
        "mode": "Continue saved prepared seed, then normal Reader/ActionAgent/gap pipeline.",
        "fresh_ocr_preserved": True, "prepared_document_sha256": hashlib.sha256(prepared.read_bytes()).hexdigest()}
    report.setdefault("recoveries", []).append(recovery)
    report.update(status="running")
    report.pop("error", None)
    write_json(run_dir / "run_summary.json", report)
    error = None
    try:
        recover_seed(run_dir, document, context)
        data = load_input(document / "shared_seed")
        provider = Provider(document / "dataagent2", model=context["model"], effort=context["effort"],
                            timeout=600, action_timeout=context.get("action_timeout_seconds", 1800),
                            action_record_refs=context.get("action_record_refs", False),
                            action_effort=context.get("action_effort"))
        provider.action_feedback = context.get("action_prior_validation_error")
        native_report = run(data, provider, document / "dataagent2", workers=0, chunk_chars=24000, max_questions=None)
        final = json.loads((document / "dataagent2/dataset.json").read_text())
        dataset = export_result(data, final, document / "shared_seed", document)
        summary = json.loads((run_dir / "structured/summary.json").read_text())
        dataset["preprocessing"] = {"status": summary["status"], "document_file": str(prepared)}
        write_json(document / "dataset.json", dataset)
        report.update(status="partial" if summary["status"] != "complete" or dataset["status"] != "complete" else "complete",
            dataset_status=dataset["status"], records=len(dataset["records"]), entities=len(dataset["entities"]),
            experiments=len(dataset["experiments"]), relations=len(dataset["relations"]), total_pages=263,
            table_count=len(evidence["tables"]), recovery_benchmark_complete=native_report["benchmark_complete"])
        if not native_report["benchmark_complete"]:
            raise RuntimeError("Reader/ActionAgent/gap calls incomplete; partial results saved")
    except BaseException as exc:
        error = f"{type(exc).__name__}: {exc}"
        report.update(status="failed", error=error)
        raise
    finally:
        usage = collect_usage(document / "shared_seed", document / "dataagent2", "49-Sructure")
        report.update(finished_at_utc=datetime.now(timezone.utc).isoformat(),
            wall_seconds=(datetime.now(timezone.utc) - started).total_seconds(), token_usage=usage)
        recovery.update(finished_at_utc=report["finished_at_utc"], error=error)
        timing = {"wall_seconds": report["wall_seconds"], "token_usage": usage,
                  "run_mode": "fresh_pdf_with_launcher_recovery", "phase_ranking": extraction_stages(document),
                  "stage_ranking": [], "slowest_requests": [], "events": [],
                  "note": "Includes the original fresh PDF run and checkpoint recovery. Interrupted phase timing may be incomplete; model usage comes from preserved actual request logs."}
        for target in (document, document.parent):
            write_usage_report(target, usage)
            write_timing_report(target, timing)
        write_json(document.parent / "_batch_summary.json", {"status": report["status"], "extraction_mode": "dataagent2",
            "token_usage": usage, "errors": {"49-Sructure": error} if error else {}})
        write_json(run_dir / "run_summary.json", report)
        write_json(document / "run_summary.json", report)
        if not error:
            write_html_report(run_dir, report)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True, type=Path)
    main(parser.parse_args().run)
