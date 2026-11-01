"""Isolated Action experiment with identical, hash-verified saved Reader results."""

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from dataagent2.action_agent import ACTION_INSTRUCTIONS, REVISION, build_action_payload, run_action_agent
from dataagent2.contracts import INSTRUCTIONS, SCHEMA, check_shape
from dataagent2.inputs import load_input
from dataagent2.pipeline import question_tasks, reader_payload
from dataagent2.provider import Provider, digest, write_json
from dataagent2.quality import audit_dataset


def read(path):
    return json.loads(path.read_text())


def metrics(dataset):
    counts = Counter(c for e in dataset["entities"] for c in ("VH", "VL")
                     if e["sequences"][c]["status"] == "supported")
    return {"entities": len(dataset["entities"]), "VH": counts["VH"], "VL": counts["VL"],
        "domains": counts["VH"] + counts["VL"],
        "pairs": sum(all(e["sequences"][c]["status"] == "supported" for c in ("VH", "VL"))
                     for e in dataset["entities"]), "measurements": len(dataset["observations"])}


def compare(before, after):
    old = {e["id"]: e for e in before["entities"]}
    added, lost, changed = [], [], []
    for e in after["entities"]:
        if e["id"] not in old:
            continue
        for region in ("VH", "VL"):
            a, b = old[e["id"]]["sequences"][region], e["sequences"][region]
            av = a.get("value") if a["status"] == "supported" else None
            bv = b.get("value") if b["status"] == "supported" else None
            row = {"item": e["id"], "name": e["name"], "region": region,
                   "before_seq": a.get("seq_id"), "after_seq": b.get("seq_id"), "length": len(bv or "")}
            if bv and not av:
                added.append(row)
            elif av and not bv:
                lost.append(row)
            elif av and bv and av != bv:
                changed.append(row)
    def observations(d):
        names = {e["id"]: e["name"] for e in d["entities"]}
        return Counter((names[o["item"]], o["s"], o["metric"], o["value"], o.get("unit"),
                        o.get("condition"), o.get("donor"), o.get("replicate")) for o in d["observations"])
    ob, oa = observations(before), observations(after)
    return {"added": added, "lost": lost, "changed_residues": changed,
            "measurements_lost": sum((ob - oa).values()), "measurements_added": sum((oa - ob).values())}


def main(baseline, output, feedback_file=None):
    baseline, output = baseline.resolve(), output.resolve()
    if output == baseline:
        raise ValueError("The baseline must remain immutable")
    output.mkdir(parents=True, exist_ok=True)
    manifest = read(baseline / "manifest.json")
    data = load_input(manifest["native_input"])
    completed, reused = {}, []
    for chunk in manifest["chunks"]:
        fingerprint = {"stage": "reader", "instructions": INSTRUCTIONS,
            "payload": reader_payload(data, chunk), "schema": SCHEMA,
            "model": manifest["model"], "effort": manifest["effort"]}
        key = digest(fingerprint)
        folder = baseline / "requests" / key
        if digest(read(folder / "input.json")) != key:
            raise ValueError("Saved Reader input changed: " + chunk["id"])
        response = read(folder / "validated.json")
        check_shape(response)
        completed[chunk["id"]] = {"response": response, "meta": {"key": key, "cache_hit": True}}
        reused.append({"chunk": chunk["id"], "key": key, "response_hash": digest(response), "folder": str(folder)})
    payload = build_action_payload(data, manifest["chunks"], completed)
    baseline_input = read(baseline / "action_agent.input.json")
    if digest({k: v for k, v in payload.items() if k != "domain_review_input"}) != digest(baseline_input):
        raise ValueError("New Action experiment would change the underlying evidence/actions/initial dataset")
    started = time.monotonic()
    report = {"status": "running", "started_at": datetime.now(timezone.utc).isoformat(),
        "scope": "Action-only rerun; identical original OCR, Template seed and all saved Reader outputs. No gap answers or previous Action output enter the model.",
        "baseline": str(baseline), "output": str(output), "action_revision": REVISION,
        "reused_readers": reused, "reader_count": len(reused), "ocr_calls": 0, "reader_calls": 0,
        "model": manifest["model"], "action_effort": manifest["action_effort"],
        "old_input_hash": digest(baseline_input), "new_input_hash": digest(payload),
        "input_evidence_equal": True, "candidate_count": len(payload["domain_review_input"]["candidates"]),
        "baseline_action": metrics(read(baseline / "dataset.first_pass.json")),
        "baseline_after_gap_answers": metrics(read(baseline / "dataset.json")), "code_hashes": {}}
    for name in ["src/dataagent2/action_agent.py", "src/dataagent2/domain_review.py", "src/dataagent2/action_record_refs.py",
                 "src/dataagent2/action_accounting.py", "src/dataagent2/provider.py"]:
        source = ROOT / name
        target = output / "source" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        report["code_hashes"][name] = hashlib.sha256(source.read_bytes()).hexdigest()
    write_json(output / "experiment.json", report)
    write_json(output / "sources.json", data["sources"])
    write_json(output / "reader_provenance.json", reused)
    (output / "instructions.txt").write_text(ACTION_INSTRUCTIONS)
    provider = Provider(output, model=manifest["model"], effort=manifest["effort"],
        action_effort=manifest["action_effort"], action_timeout=1800, action_record_refs=True)
    if feedback_file:
        provider.action_feedback = feedback_file.read_text()
        report["initial_feedback_file"] = str(feedback_file.resolve())
        report["initial_feedback_sha256"] = hashlib.sha256(feedback_file.read_bytes()).hexdigest()
        write_json(output / "experiment.json", report)
    print(f"Reusing {len(reused)} Readers; exact candidates={report['candidate_count']}; fresh Action revision={REVISION}", flush=True)
    try:
        ledger, metadata = run_action_agent(data, manifest["chunks"], completed, provider, output)
        dataset = ledger.assemble()
        write_json(output / "dataset.first_pass.json", dataset)
        write_json(output / "dataset.json", dataset)
        write_json(output / "facts.json", ledger.facts)
        audit = audit_dataset(dataset, data["sources"])
        write_json(output / "quality_audit.json", audit)
        write_json(output / "questions.json", question_tasks(dataset, data))
        report.update(status="complete", action_agent=metadata, new_action=metrics(dataset),
            versus_old_action=compare(read(baseline / "dataset.first_pass.json"), dataset),
            versus_old_final=compare(read(baseline / "dataset.json"), dataset),
            literal_errors=audit["literal_errors"],
            measurements_with_complete_pair=audit["measurements_with_complete_variable_pair"])
        if audit["literal_errors"]:
            raise ValueError("Literal provenance audit failed")
        print(json.dumps({k: report[k] for k in ["baseline_action", "baseline_after_gap_answers", "new_action", "versus_old_action", "versus_old_final"]}, ensure_ascii=False, indent=2), flush=True)
    except BaseException as error:
        report.update(status="failed", error=f"{type(error).__name__}: {error}")
        action_state = read(output / "action_agent.json") if (output / "action_agent.json").exists() else {}
        write_json(output / "action_agent.json", {**action_state, "status": "failed", "error": report["error"]})
        raise
    finally:
        report.update(finished_at=datetime.now(timezone.utc).isoformat(),
                      elapsed_seconds=round(time.monotonic() - started, 3), usage=provider.usage())
        write_json(output / "experiment.json", report)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--feedback", type=Path, help="Actual failed validation findings; never a repaired dataset")
    args = parser.parse_args()
    main(args.baseline, args.output, args.feedback)
