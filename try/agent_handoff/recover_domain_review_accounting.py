"""Validate a real saved Action response, then let the LLM resolve duplicate audit IDs."""

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from dataagent2.action_accounting import repair_accounting
from dataagent2.action_agent import DuplicateActionDecisions, validate_action_result
from dataagent2.action_record_refs import expand_record_refs
from dataagent2.inputs import load_input
from dataagent2.provider import Provider, digest, write_json
from dataagent2.quality import audit_dataset
from rerun_action_domain_review import metrics, compare, read


def main(trial, output):
    trial, output = trial.resolve(), output.resolve()
    context = read(trial / "experiment.json")
    baseline = Path(context["baseline"])
    data = load_input(baseline.parent / "shared_seed")
    payload = read(trial / "action_agent.input.json")
    call = [json.loads(s) for s in (trial / "calls.jsonl").read_text().splitlines() if s][0]
    request = read(trial / "requests" / call["key"] / "input.json")
    if digest(request) != call["key"]:
        raise ValueError("Saved model request changed")
    raw = read(trial / "action_agent.response.raw.1.json")
    response = expand_record_refs(raw, payload, data)
    try:
        validate_action_result(data, payload, response)
    except DuplicateActionDecisions as error:
        duplicate_ids = error.identities
    else:
        raise ValueError("This recovery requires a duplicate-only audit failure")
    output.mkdir(parents=True, exist_ok=True)
    provider = Provider(output, model=context["model"], effort="xhigh", action_effort=context["action_effort"])
    write_json(output / "action_agent.input.json", payload)
    write_json(output / "action_agent.response.before_accounting.json", response)
    write_json(output / "action_agent.response.raw.original.json", raw)
    response, repair_meta = repair_accounting(payload, response, duplicate_ids, provider, output)
    ledger = validate_action_result(data, payload, response)
    dataset = ledger.assemble()
    quality = audit_dataset(dataset, data["sources"])
    if quality["literal_errors"]:
        raise ValueError("Literal provenance audit failed")
    metadata = {**ledger.action_metadata, "input_hash": digest(payload), "response_hash": digest(response),
        "original_action_key": call["key"], "original_trial": str(trial), "accounting_repair": repair_meta}
    write_json(output / "action_agent.json", metadata)
    write_json(output / "action_agent.response.json", response)
    write_json(output / "action_agent.dataset.json", response["dataset"])
    write_json(output / "dataset.first_pass.json", dataset)
    write_json(output / "dataset.json", dataset)
    write_json(output / "facts.json", ledger.facts)
    write_json(output / "sources.json", data["sources"])
    write_json(output / "quality_audit.json", quality)
    write_json(output / "domain_review.json", {"summary": metadata["domain_review"],
        "input": payload["domain_review_input"], "reviews": response["domain_reviews"]})
    result = {"status": "complete", "scope": "Action rerun plus an explicit LLM-only duplicate audit correction; no gap answers enter the model.",
        "baseline": str(baseline), "original_trial": str(trial), "original_action_call": call,
        "input_evidence_equal": context["input_evidence_equal"], "reused_readers": context["reader_count"],
        "baseline_action": metrics(read(baseline / "dataset.first_pass.json")),
        "baseline_after_gap_answers": metrics(read(baseline / "dataset.json")), "new_action": metrics(dataset),
        "versus_old_action": compare(read(baseline / "dataset.first_pass.json"), dataset),
        "versus_old_final": compare(read(baseline / "dataset.json"), dataset),
        "literal_errors": quality["literal_errors"],
        "measurements_with_complete_pair": quality["measurements_with_complete_variable_pair"],
        "accounting_call_usage": provider.usage(), "action_agent": metadata, "code_hashes": {}}
    for name in ["action_agent.py", "action_accounting.py", "action_record_refs.py", "domain_review.py", "provider.py"]:
        source = ROOT / "src/dataagent2" / name
        dest = output / "source/src/dataagent2" / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, dest)
        result["code_hashes"][name] = hashlib.sha256(source.read_bytes()).hexdigest()
    write_json(output / "experiment.json", result)
    print(json.dumps({k: result[k] for k in ["baseline_action", "baseline_after_gap_answers", "new_action", "versus_old_action", "versus_old_final"]}, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trial", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    main(args.trial, args.output)
