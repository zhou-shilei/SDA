"""Evaluate focused parallel Answers and Action2 on one hash-verified saved Dataset2."""

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

from dataagent2.action_agent import business_dataset, validate_action_result
from dataagent2.followup import (REVISION, ANSWER_INSTRUCTIONS, ANSWER_ACTION_INSTRUCTIONS,
                                answer_payload, question_plan, run_followup)
from dataagent2.inputs import load_input, read
from dataagent2.provider import Provider, compact, digest, write_json
from dataagent2.quality import audit_dataset
from dataagent2.timing import PhaseTimings
from rerun_action_domain_review import compare, metrics


def main(baseline, output, *, plan_only=False, answer_effort="medium", workers=4):
    baseline, output = baseline.resolve(), output.resolve()
    if baseline == output or baseline in output.parents or output in baseline.parents:
        raise ValueError("Use an isolated output directory; the saved web run must stay unchanged")
    output.mkdir(parents=True, exist_ok=True)
    manifest = read(baseline / "manifest.json")
    data = load_input(manifest["native_input"])
    source = read(baseline / "sources.json")
    if digest(source) != digest(data["sources"]):
        raise ValueError("Original OCR evidence changed")
    payload = read(baseline / "action_agent.input.json")
    result = read(baseline / "action_agent.response.json")
    status = read(baseline / "action_agent.json")
    if digest(payload) != status["input_hash"] or digest(result) != status["response_hash"]:
        raise ValueError("Saved Action1 request/response changed")
    ledger = validate_action_result(data, payload, result)
    fixed = business_dataset(ledger)
    config = {"baseline": str(baseline), "dataset2_hash": digest(fixed), "revision": REVISION,
              "model": manifest["model"], "answer_effort": answer_effort, "action_effort": "medium",
              "answer_workers": workers, "question_chars": 8000,
              "answer_instructions_hash": digest(ANSWER_INSTRUCTIONS),
              "answer_action_instructions_hash": digest(ANSWER_ACTION_INSTRUCTIONS)}
    previous = read(output / "configuration.json")
    if previous and previous != config:
        raise ValueError("Experiment settings changed; use a fresh output directory")
    write_json(output / "configuration.json", config)
    write_json(output / "domain_review.json", read(baseline / "domain_review.json"))
    write_json(output / "dataset2.business.json", fixed)
    write_json(output / "dataset.first_pass.json", ledger.assemble())
    write_json(output / "sources.json", data["sources"])
    (output / "answer_prompt.txt").write_text(ANSWER_INSTRUCTIONS)
    (output / "answer_action_prompt.txt").write_text(ANSWER_ACTION_INSTRUCTIONS)
    plan = question_plan(ledger.assemble(), ledger.domain_reviews)
    write_json(output / "question_plan.json", plan)
    sizes = []
    for q in plan["questions"]:
        if q["kind"] == "ocr_failure":
            continue
        p, _ = answer_payload(q, ledger, payload["domain_review_input"])
        write_json(output / "planned_answers" / q["id"] / "input.json", p)
        sizes.append({"question": q["id"], "input_characters": len(ANSWER_INSTRUCTIONS + compact(p)),
                      "sources": len(p["sources"]), **p["retrieval"]})
    write_json(output / "input_sizes.json", sizes)
    print(compact({"questions": len(plan["questions"]), "answer_sizes": sizes}), flush=True)
    if plan_only:
        return
    for name in ["action_agent.py", "followup.py", "domain_review.py", "provider.py", "action_record_refs.py", "pipeline.py"]:
        target = output / "source" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / "src/dataagent2" / name, target)
    provider = Provider(output, model=manifest["model"], effort=manifest["effort"],
                        answer_effort=answer_effort, action_effort="medium", action_timeout=1800)
    started = time.monotonic()
    report = {"status": "running", "started_at": datetime.now(timezone.utc).isoformat(),
        "scope": "Followup-only experiment on saved Dataset2; no new OCR, Template, Reader or Action1 calls.",
        "configuration": config, "baseline_first": metrics(ledger.assemble()),
        "baseline_final": metrics(read(baseline / "dataset.json")),
        "baseline_answer_usage": read(baseline / "token_usage.json")["by_stage"]["answer"],
        "baseline_question_count": len(read(baseline / "questions.initial.json")),
        "baseline_answer_seconds": sum(e["seconds"] for e in read(baseline / "phase_timings.json")["events"]
                                         if e["stage"] == "dataagent2_questions"),
        "reused_dataset2_hash": digest(fixed),
        "code_hashes": {f.name: hashlib.sha256(f.read_bytes()).hexdigest() for f in (output / "source").iterdir()}}
    write_json(output / "experiment.json", report)

    def emit(phase, current, total):
        value = {"phase": phase, "completed": current, "total": total, "usage": provider.usage(),
                 "elapsed_seconds": round(time.monotonic() - started, 3)}
        write_json(output / "progress.json", value)
        print(compact({k: v for k, v in value.items() if k != "usage"}), flush=True)

    try:
        updated, followup = run_followup(data, ledger, provider, output, workers=workers,
            timings=PhaseTimings(output / "phase_timings.json"), emit=emit)
        final = updated.assemble()
        audit = audit_dataset(final, data["sources"])
        write_json(output / "dataset.json", final)
        write_json(output / "facts.json", updated.facts)
        write_json(output / "quality_audit.json", audit)
        old_definitions = read(baseline / "facts.json")["sequences"]
        definition_key = lambda d: (d["seq"], d["region"], d["value"])
        old_keys = {definition_key(d) for d in old_definitions}
        new_keys = {definition_key(d) for d in updated.facts["sequences"]}
        final_business = business_dataset(updated)
        write_json(output / "comparison.audit.json", {
            "dataset2_hash": digest(fixed), "dataset3_hash": digest(final_business),
            "business_dataset_unchanged": fixed == final_business,
            "business_collection_counts": {k: len(v) for k, v in final_business.items() if isinstance(v, list)},
            "formal_measurements": len(final["observations"]),
            "pending_measurement_candidates": len(final_business["observations"]) - len(final["observations"]),
            "old_answer_unique_sequence_definitions_lost": len(old_keys - new_keys),
            "old_answer_extra_duplicate_definition_records": len(old_definitions) - len(updated.facts["sequences"])})
        report.update(status="complete" if followup["integration"]["status"] in {"validated", "not_needed"}
                      and not any(a["status"] == "failed" for a in followup["answers"]) else "failed",
            final=metrics(final), versus_dataset2=compare(read(baseline / "dataset.first_pass.json"), final),
            versus_old_final=compare(read(baseline / "dataset.json"), final),
            questions=len(followup["questions"]), remaining=len(followup["remaining"]),
            answer_statuses={s: sum(a["status"] == s for a in followup["answers"])
                             for s in sorted({a["status"] for a in followup["answers"]})},
            answer_action=followup["integration"], literal_errors=audit["literal_errors"],
            measurements_with_complete_pair=audit["measurements_with_complete_variable_pair"])
    except Exception as exc:
        report.update(status="failed", error=str(exc))
        raise
    finally:
        report.update(seconds=round(time.monotonic() - started, 3), usage=provider.usage(),
                      finished_at=datetime.now(timezone.utc).isoformat())
        write_json(output / "experiment.json", report)
        print(compact(report), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--plan-only", action="store_true")
    parser.add_argument("--answer-effort", default="medium")
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    main(args.baseline, args.output, plan_only=args.plan_only, answer_effort=args.answer_effort, workers=args.workers)
