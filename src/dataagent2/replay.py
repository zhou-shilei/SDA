"""Reassemble saved responses with current host rules; never call a model."""

import argparse
import copy
from pathlib import Path

from .contracts import ANSWER_INSTRUCTIONS, INSTRUCTIONS, SCHEMA, check_shape
from .inputs import discover, load_input, read, source_row
from .ledger import Ledger
from .pipeline import READER_INPUT_REVISION, compare_reference, initial_dataset, question_tasks, reader_payload
from .provider import Provider, digest, write_json
from .action_agent import (ACTION_INSTRUCTIONS, REVISION as ACTION_REVISION, build_action_payload,
                           business_dataset, validate_action_result)


def replay_followup(data, ledger, output, manifest):
    """Revalidate proposals, then replay the LLM's saved complete Dataset3 once."""
    from .followup import answer_payload, build_answer_action_payload, question_plan, validate_answer
    answers = read(output / "answers.json", [])
    planned = question_plan(ledger.assemble(), getattr(ledger, "domain_reviews", []))["questions"]
    if read(output / "questions.initial.json", []) != planned:
        raise ValueError("Saved question queue changed from Action domain reviews")
    questions = {q["id"]: q for q in planned}
    review_input = read(output / "action_agent.input.json", {}).get("domain_review_input", {})
    baseline_hash = digest(business_dataset(ledger))
    for answer in answers:
        if answer["status"] not in {"proposed", "no_change"}:
            continue
        key = answer["meta"]["key"]
        folder = output / "requests" / key
        request, response = read(folder / "input.json"), read(folder / "validated.json")
        if (not request or response is None or digest(request) != key or request["stage"] != "answer"
                or request["payload"].get("dataset2_hash") != baseline_hash
                or response != answer["response"]):
            raise ValueError("Saved focused Answer is missing or belongs to another Dataset2")
        rows = request["payload"]["sources"]
        expected_payload, _ = answer_payload(questions[answer["id"]], ledger, review_input, manifest["question_chars"])
        if digest(request["payload"]) != digest(expected_payload):
            raise ValueError("Saved focused Answer used different question context or retrieval")
        if any(row != source_row(ledger.sources[row[0]]) for row in rows):
            raise ValueError("Saved focused Answer used different original evidence")
        allowed = [row[0] for row in rows]
        facts = validate_answer(response, questions[answer["id"]], ledger, allowed)
        if facts != answer["facts"] or allowed != answer["allowed_sources"]:
            raise ValueError("Saved Answer proposals changed")
        if (answer["status"] == "proposed") != any(facts.values()):
            raise ValueError("Saved Answer status contradicts its proposals")
    status = read(output / "answer_action.json", {"status": "not_run"})
    expected = build_answer_action_payload(data, ledger, answers)
    if status["status"] == "not_needed" and expected["reader_actions"]:
        raise ValueError("Answer proposals were silently skipped")
    if status["status"] == "validated":
        payload = read(output / "answer_action/action_agent.input.json")
        response = read(output / "answer_action/action_agent.response.json")
        if (payload != expected or digest(payload) != status.get("input_hash")
                or response is None or digest(response) != status.get("response_hash")):
            raise ValueError("Saved Answer integration used different proposals or Dataset2")
        prior_conflicts = copy.deepcopy(getattr(ledger, "action_conflicts", []))
        prior_rejections = copy.deepcopy(ledger.rejections)
        ledger = validate_action_result(data, payload, response)
        ledger.action_conflicts = prior_conflicts + ledger.action_conflicts
        ledger.rejections = prior_rejections + ledger.rejections
        ledger.action_metadata = status
    return ledger, status


def replay(output, *, finalize=False):
    output = Path(output)
    manifest = read(output / "manifest.json")
    progress = read(output / "progress.json", {})
    if finalize and progress.get("phase") != "finished":
        raise ValueError("Finalize requires the live pipeline to have finished")
    try:
        _, reference = discover(Path(__file__).resolve().parents[2], manifest["document_id"])
    except (KeyError, ValueError, OSError):
        reference = None
    data = load_input(manifest["native_input"], reference)
    settings = {"input": data["signature"], "instructions": INSTRUCTIONS,
                        "answer_instructions": ANSWER_INSTRUCTIONS,
                        "chunk_chars": manifest["chunk_chars"], "model": manifest["model"],
                        "effort": manifest["effort"]}
    if "chunking_revision" in manifest:
        settings["chunking_revision"] = manifest["chunking_revision"]
    if "reader_input_revision" in manifest:
        if manifest["reader_input_revision"] != READER_INPUT_REVISION:
            raise ValueError("Reader input revision changed; replay would mix request formats")
        settings["reader_input_revision"] = READER_INPUT_REVISION
        settings["reader_initial_dataset_hash"] = digest(initial_dataset(data))
    if "action_agent_revision" in manifest:
        if manifest["action_agent_revision"] != ACTION_REVISION:
            raise ValueError("ActionAgent revision changed; replay would mix integration policies")
        settings.update(action_agent_revision=ACTION_REVISION, action_instructions=ACTION_INSTRUCTIONS)
    if "followup_revision" in manifest:
        from .followup import REVISION, ANSWER_INSTRUCTIONS as FOCUSED_ANSWER_INSTRUCTIONS, ANSWER_ACTION_INSTRUCTIONS
        if manifest["followup_revision"] != REVISION:
            raise ValueError("Followup workflow changed; replay would mix integration policies")
        settings.update(followup_revision=REVISION, focused_answer_instructions=FOCUSED_ANSWER_INSTRUCTIONS,
            answer_action_instructions=ANSWER_ACTION_INSTRUCTIONS, answer_effort=manifest["answer_effort"],
            question_chars=manifest["question_chars"], max_questions=manifest["max_questions"])
    signature = digest(settings)
    if signature != manifest["signature"]:
        raise ValueError("Original inputs/settings changed; replay would mix experiments")
    ledger, completed, completed_responses = Ledger(data), [], {}
    for chunk in manifest["chunks"]:
        fingerprint = {"stage": "reader", "instructions": INSTRUCTIONS,
            "payload": reader_payload(data, chunk), "schema": SCHEMA,
            "model": manifest["model"], "effort": manifest["effort"]}
        response = read(output / "requests" / digest(fingerprint) / "validated.json")
        if response is not None:
            check_shape(response)
            ledger.apply(response, chunk["context"] + chunk["core"], chunk["id"])
            completed.append(chunk)
            completed_responses[chunk["id"]] = {"response": response}
    action_status = {"status": "not_configured_legacy"}
    if "action_agent_revision" in manifest:
        action_status = read(output / "action_agent.json", {"status": "not_run"})
        if action_status["status"] == "validated":
            payload = read(output / "action_agent.input.json")
            response = read(output / "action_agent.response.json")
            if (not payload or response is None or digest(payload) != action_status.get("input_hash")
                    or digest(response) != action_status.get("response_hash")):
                raise ValueError("Saved ActionAgent input/response is missing or changed")
            # JSON source-ID dictionaries have string keys after loading.
            if digest(payload) != digest(build_action_payload(data, manifest["chunks"], completed_responses)):
                raise ValueError("Saved ActionAgent used different Reader actions or source evidence")
            ledger = validate_action_result(data, payload, response)
        ledger.action_metadata = action_status
    action_complete = action_status["status"] in {"validated", "not_needed", "not_configured_legacy"}
    first = ledger.assemble()
    first_facts = copy.deepcopy(ledger.facts)
    answer_replay = []
    if "followup_revision" in manifest and action_complete:
        ledger, second_status = replay_followup(data, ledger, output, manifest)
        action_complete = action_complete and second_status["status"] in {"validated", "not_needed"}
        answer_replay.append({"stage": "answer_action", **second_status})
    for answer in ([] if "followup_revision" in manifest else read(output / "answers.json", [])):
        if answer.get("status") != "applied":
            continue
        key = answer["meta"]["key"]
        folder = output / "requests" / key
        request, response = read(folder / "input.json"), read(folder / "validated.json")
        if not request or response is None or digest(request) != key or request["stage"] != "answer":
            raise ValueError("Saved applied answer has no intact request/response")
        rows = request["payload"]["sources"]
        if any(row != source_row(ledger.sources[row[0]]) for row in rows):
            raise ValueError("Saved answer used different source text")
        check_shape(response)
        current = ledger.assemble()
        candidate = copy.deepcopy(ledger)
        candidate.apply(response, [row[0] for row in rows], answer["id"])
        proposed = candidate.assemble()
        originals = {(e["id"], c): e["sequences"][c]["value"] for e in current["entities"] for c in ("VH", "VL")
                     if e["sequences"][c]["status"] == "supported"}
        incompatible = any(originals.get((e["id"], c)) and originals[e["id"], c] != e["sequences"][c]["value"]
                           for e in proposed["entities"] for c in ("VH", "VL"))
        if not incompatible:
            ledger = candidate
        answer_replay.append({"id": answer["id"], "status": "conflict_preserved" if incompatible else "replayed"})
    dataset = ledger.assemble()
    if "followup_revision" in manifest and hasattr(ledger, "domain_reviews"):
        from .followup import question_plan
        current_questions = question_plan(dataset, getattr(ledger, "domain_reviews", []))["questions"]
    else:
        current_questions = question_tasks(dataset, data)
    coverage = {"total_chunks": len(manifest["chunks"]), "completed_chunks": len(completed),
        "failed_chunks": {}, "core_sources_read": sum(len(c["core"]) for c in completed),
        "total_sources": len(ledger.sources), "scope": "Saved successful OCR reads; not semantic accuracy"}
    dataset["reader_coverage"] = coverage
    dataset["status"] = "partial" if not action_complete or len(completed) != len(manifest["chunks"]) or any(
        e["missing"] for e in dataset["entities"]) or dataset["conflicts"] else "source_supported"
    metadata = {"document": manifest["document_id"], "host_rules_revision": 2,
        "model_calls_made": 0, "coverage": coverage, "first_pass": first["summary"],
        "final": dataset["summary"], "answer_replay": answer_replay, "action_agent": action_status}
    write_json(output / "dataset.checkpoint.json", dataset)
    write_json(output / "checkpoint_progress.json", metadata)
    if finalize:
        report = read(output / "report.json")
        if not action_complete or len(completed) != len(manifest["chunks"]) or not report.get("benchmark_complete"):
            raise ValueError("Cannot finalize a partial document benchmark")
        backup = output / "report.before_reassembly.json"
        if not backup.is_file():
            write_json(backup, report)
        comparison = compare_reference(dataset, data["reference_for_scoring_only"])
        usage = Provider(output, model=manifest["model"], effort=manifest["effort"]).usage()
        total = report["reused_stage_historical_tokens"] + usage["total_tokens"]
        report.update(status=dataset["status"], first_pass=first["summary"], final=dataset["summary"],
            coverage=coverage, new_model_usage=usage, comparable_model_total_tokens=total,
            known_token_reduction_fraction=1 - total / report["historical_baseline_tokens"]
                if report["historical_baseline_tokens"] else None,
            questions_remaining=len(current_questions), reference_comparison=comparison["counts"],
            host_reassembly=metadata)
        write_json(output / "dataset.first_pass.json", first)
        write_json(output / "facts.first_pass.json", first_facts)
        write_json(output / "dataset.json", dataset)
        write_json(output / "facts.json", ledger.facts)
        write_json(output / "reference_comparison.json", comparison)
        write_json(output / "questions.current.json", current_questions)
        write_json(output / "report.json", report)
        write_json(output / "progress.json", {"phase": "finished", **report})
    return metadata


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="One document output directory")
    parser.add_argument("--finalize", action="store_true")
    args = parser.parse_args()
    print(replay(args.output, finalize=args.finalize))
