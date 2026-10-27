"""Recover independently grounded chains from completed, saved model turns."""

import copy
import hashlib
import json
from pathlib import Path

from .llm_sequence_agent import decision_refs, ground_decision, validate_decisions_individually


def recover_sequence_decisions(state: dict, request_dir: Path) -> tuple[dict, list[dict]]:
    result = copy.deepcopy(state)
    bundle = result["sequence_extraction"]
    document_id = state["config"]["document_id"]
    sources = {**state["document_structure"]["sources"], **bundle.get("sources", {})}
    known_entities = {e["entity_id"] for e in state["document_template"]["entity_template"]["entities"]}
    pending = {(d["entity_id"], d["chain"]) for d in bundle["decisions"] if d["status"] != "resolved"}
    replacements, audit = {}, []
    files = sorted(Path(request_dir).rglob("answer.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in files:
        input_path = path.parent.parent / "input.json"
        if not input_path.is_file():
            continue
        try:
            request = json.loads(input_path.read_text())
            if not request.get("task", "").startswith("sequence-extraction-v"):
                continue
            payload = request["payload"]
            if payload["document_id"] != document_id or any(e["entity_id"] not in known_entities for e in payload["entities"]):
                continue
            events = [json.loads(l) for l in (path.parent / "events.jsonl").read_text().splitlines() if l.strip()]
            if not any(e.get("type") == "turn.completed" for e in events) or any(e.get("type") == "turn.failed" for e in events):
                continue
            # Reuse only evidence actually exposed in this request and still
            # byte-identical to the saved prompt. Do not add unseen references.
            scoped = {ref: sources[ref] for ref, saved in payload["loaded_sources"].items()
                      if ref in sources and saved.get("text") == sources[ref].get("text")}
            decisions = validate_decisions_individually(json.loads(path.read_text()), document_id,
                                                       payload["entities"], scoped, set(sources))
        except (OSError, ValueError, KeyError, TypeError):
            continue
        for item in decisions:
            key = item["entity_id"], item["chain"]
            if key not in pending or key in replacements or item["status"] != "resolved":
                continue
            item["grounded_fragments"] = ground_decision(item, scoped)
            replacements[key] = item
            bundle.setdefault("sources", {}).update({ref: scoped[ref] for ref in decision_refs(item)})
            audit.append({"entity_id": key[0], "chain": key[1], "seq_id": item["seq_id"],
                          "response_file": str(path.resolve()), "response_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                          "input_file": str(input_path.resolve()), "validation": "request-scoped literal grounding; table priority retained"})
    bundle["decisions"] = [replacements.get((d["entity_id"], d["chain"]), d) for d in bundle["decisions"]]
    bundle["status"] = "validated" if all(d["status"] == "resolved" and not d.get("source_conflicts") for d in bundle["decisions"]) else "needs_review"
    bundle["recovered_decisions"] = audit
    result["document_template"]["sequence_extraction"] = copy.deepcopy(bundle)
    return result, audit
