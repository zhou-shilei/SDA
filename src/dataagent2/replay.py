"""Reassemble saved responses with current host rules; never call a model."""

import argparse
import copy
from pathlib import Path

from .contracts import INSTRUCTIONS, SCHEMA, check_shape
from .inputs import discover, load_input, read, source_row
from .ledger import Ledger
from .pipeline import compare_reference, question_tasks, reader_payload
from .provider import Provider, digest, write_json


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
                        "chunk_chars": manifest["chunk_chars"], "model": manifest["model"],
                        "effort": manifest["effort"]}
    if "chunking_revision" in manifest:
        settings["chunking_revision"] = manifest["chunking_revision"]
    signature = digest(settings)
    if signature != manifest["signature"]:
        raise ValueError("Original inputs/settings changed; replay would mix experiments")
    ledger, completed = Ledger(data), []
    for chunk in manifest["chunks"]:
        fingerprint = {"stage": "reader", "instructions": INSTRUCTIONS,
            "payload": reader_payload(data, chunk), "schema": SCHEMA,
            "model": manifest["model"], "effort": manifest["effort"]}
        response = read(output / "requests" / digest(fingerprint) / "validated.json")
        if response is not None:
            check_shape(response)
            ledger.apply(response, chunk["context"] + chunk["core"], chunk["id"])
            completed.append(chunk)
    first = ledger.assemble()
    first_facts = copy.deepcopy(ledger.facts)
    answer_replay = []
    for answer in read(output / "answers.json", []):
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
    coverage = {"total_chunks": len(manifest["chunks"]), "completed_chunks": len(completed),
        "failed_chunks": {}, "core_sources_read": sum(len(c["core"]) for c in completed),
        "total_sources": len(ledger.sources), "scope": "Saved successful OCR reads; not semantic accuracy"}
    dataset["reader_coverage"] = coverage
    dataset["status"] = "partial" if len(completed) != len(manifest["chunks"]) or any(
        e["missing"] for e in dataset["entities"]) or dataset["conflicts"] else "source_supported"
    metadata = {"document": manifest["document_id"], "host_rules_revision": 2,
        "model_calls_made": 0, "coverage": coverage, "first_pass": first["summary"],
        "final": dataset["summary"], "answer_replay": answer_replay}
    write_json(output / "dataset.checkpoint.json", dataset)
    write_json(output / "checkpoint_progress.json", metadata)
    if finalize:
        report = read(output / "report.json")
        if len(completed) != len(manifest["chunks"]) or not report.get("benchmark_complete"):
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
            questions_remaining=len(question_tasks(dataset, data)), reference_comparison=comparison["counts"],
            host_reassembly=metadata)
        write_json(output / "dataset.first_pass.json", first)
        write_json(output / "facts.first_pass.json", first_facts)
        write_json(output / "dataset.json", dataset)
        write_json(output / "facts.json", ledger.facts)
        write_json(output / "reference_comparison.json", comparison)
        write_json(output / "questions.current.json", question_tasks(dataset, data))
        write_json(output / "report.json", report)
        write_json(output / "progress.json", {"phase": "finished", **report})
    return metadata


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="One document output directory")
    parser.add_argument("--finalize", action="store_true")
    args = parser.parse_args()
    print(replay(args.output, finalize=args.finalize))
