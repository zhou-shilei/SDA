"""Bounded Codex adjudication of missing VH/VL associations for named isotype forms."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from collections import Counter

from .llm_io import VariableRegionIdentityBatch, require_refs
from .sequence_evidence import prompt_source


FORMAT_SUFFIX = re.compile(r"[\s_\-–—]+(?:h?IgG[1-4]|h?Fc)$", re.I)


def normalized_name(value: str) -> str:
    return re.sub(r"\s+", " ", str(value)).strip().casefold()


def names(entity: dict) -> list[str]:
    return list(dict.fromkeys(n for n in [entity.get("canonical_name"), *entity.get("raw_names", [])] if n))


def identity_questions(bundle: dict, entities: list[dict]) -> list[dict]:
    decisions = {(d["entity_id"], d["chain"]): d for d in bundle["decisions"]}
    names_to_ids = {}
    for entity in entities:
        for name in names(entity):
            names_to_ids.setdefault(normalized_name(name), set()).add(entity["entity_id"])
    questions = []
    for entity in entities:
        bases = {normalized_name(FORMAT_SUFFIX.sub("", name)) for name in names(entity)
                 if FORMAT_SUFFIX.search(name)}
        parents = set().union(*(names_to_ids.get(base, set()) for base in bases)) if bases else set()
        parents.discard(entity["entity_id"])
        if len(parents) != 1:
            continue
        parent = next(iter(parents))
        for chain in ("VH", "VL"):
            target, source = decisions.get((entity["entity_id"], chain)), decisions.get((parent, chain))
            # A concrete but damaged/conflicting SEQ assignment must not be bypassed.
            if (target and source and target["status"] == "unresolved" and target["seq_id"] is None
                    and source["status"] == "resolved"):
                questions.append({"entity_id": entity["entity_id"], "parent_entity_id": parent, "chain": chain})
    return questions


def validate_identity(response: dict, document_id: str, questions: list[dict],
                      entities: dict, sources: dict) -> None:
    response = VariableRegionIdentityBatch.model_validate(response).model_dump()
    if response["document_id"] != document_id:
        raise ValueError("Wrong identity-review document_id")
    keys = ("entity_id", "parent_entity_id", "chain")
    if Counter(tuple(d[k] for k in keys) for d in response["decisions"]) != Counter(
            tuple(q[k] for k in keys) for q in questions):
        raise ValueError("Identity review must answer each supplied chain question exactly once")
    for item in response["decisions"]:
        for citation in item["evidence"]:
            ref, quote = citation["source_ref"], citation["quote"]
            require_refs([ref], sources)
            if citation["occurrence"] >= len(list(re.finditer(re.escape(quote), sources[ref].get("text", "")))):
                raise ValueError("Identity review quote must occur verbatim")
        if item["verdict"] != "same":
            continue
        if item["basis"] not in {"isotype_format", "shared_variable_region"}:
            raise ValueError("A same-chain decision requires an affirmative identity basis")
        for role in ("entity_id", "parent_entity_id"):
            entity = entities[item[role]]
            refs = set(entity.get("evidence_refs", []))
            if not any(c["source_ref"] in refs and any(
                    normalized_name(name) in normalized_name(c["quote"]) for name in names(entity))
                    for c in item["evidence"]):
                raise ValueError("Same-chain identity requires quoted name evidence for both entities")


def review_missing_variable_regions(provider, graph: dict, template: dict, bundle: dict,
                                    *, batch_size: int = 8, max_questions: int = 32,
                                    source_budget: int = 96_000) -> dict:
    from .llm_runtime import CodexProviderError, json_write
    from .llm_sequence_agent import decision_refs, ground_decision

    entities = template.get("entity_template", {}).get("entities", [])
    by_id = {e["entity_id"]: e for e in entities}
    questions = identity_questions(bundle, entities)
    if not questions:
        return bundle
    instructions = (Path(__file__).parent / "prompt" / "sequence_identity.txt").read_text(encoding="utf-8")
    result = copy.deepcopy(bundle)
    originals = {(d["entity_id"], d["chain"]): d for d in bundle["decisions"]}
    replacements, batches = {}, []
    limited = questions[:max_questions]
    for start in range(0, len(limited), batch_size):
        batch = limited[start:start + batch_size]
        required = []
        for q in batch:
            for role in ("entity_id", "parent_entity_id"):
                required.extend(by_id[q[role]].get("evidence_refs", []))
            required.extend(decision_refs(originals[q["parent_entity_id"], q["chain"]]))
        loaded, omitted, size = {}, [], 0
        # Prefer directly addressed cells; every included quotation stays complete.
        required = sorted(set(required), key=lambda ref: (graph["sources"].get(ref, {}).get("kind") != "cell", ref))
        for ref in required:
            source = graph["sources"].get(ref)
            if source is None:
                continue
            packed = prompt_source(source)
            length = len(ref) + len(json.dumps(packed, ensure_ascii=False))
            if size + length > source_budget:
                omitted.append(ref)
            else:
                loaded[ref] = packed
                size += length
        payload = {"document_id": graph["document_id"], "questions": batch,
                   "entities": [by_id[key] for key in sorted({q[role] for q in batch
                                 for role in ("entity_id", "parent_entity_id")})],
                   "loaded_sources": loaded, "omitted_evidence_refs": omitted}
        print(f"llm-sequence-identity: questions {start + 1}-{start + len(batch)}/{len(limited)}", flush=True)
        try:
            response, meta = provider.generate("sequence-identity-v1", instructions, payload,
                VariableRegionIdentityBatch,
                validate=lambda value: validate_identity(value, graph["document_id"], batch, by_id, loaded))
            response = VariableRegionIdentityBatch.model_validate(response).model_dump()
            validate_identity(response, graph["document_id"], batch, by_id, loaded)
            batches.append({"status": "completed", "questions": batch, "decisions": response["decisions"], "llm": meta})
        except (CodexProviderError, ValueError) as exc:
            batches.append({"status": "failed", "questions": batch, "error": str(exc)})
            continue
        for decision in response["decisions"]:
            if decision["verdict"] != "same":
                continue
            key = decision["entity_id"], decision["chain"]
            parent = originals[decision["parent_entity_id"], decision["chain"]]
            item = copy.deepcopy(parent)
            item["entity_id"] = decision["entity_id"]
            item["evidence_refs"] = list(dict.fromkeys(item["evidence_refs"] +
                [c["source_ref"] for c in decision["evidence"]]))
            item["rationale"] = f"Codex 核验两者 {decision['chain']} 相同：{decision['rationale']} {parent['rationale']}"
            item["grounded_fragments"] = ground_decision(
                {k: v for k, v in item.items() if k != "grounded_fragments"}, graph["sources"])
            result["sources"].update({ref: graph["sources"][ref] for ref in decision_refs(item)})
            replacements[key] = item
            result.setdefault("sequence_inheritance", []).append({
                "entity_id": decision["entity_id"], "parent_entity_id": decision["parent_entity_id"],
                "chains": [decision["chain"]], "scope": "variable_regions", "basis": "codex_evidence",
                "identity_basis": decision["basis"], "evidence": decision["evidence"], "note": decision["rationale"]})
    result["decisions"] = [replacements.get((d["entity_id"], d["chain"]), d) for d in result["decisions"]]
    result["identity_review"] = {"status": "needs_review" if any(b["status"] == "failed" or any(
                                 d["verdict"] == "uncertain" for d in b.get("decisions", [])) for b in batches)
                                 or len(limited) < len(questions) else "completed",
                                 "question_count": len(questions), "applied_chain_count": len(replacements),
                                 "deferred_questions": questions[max_questions:], "batches": batches}
    result["status"] = "validated" if all(d["status"] == "resolved" and not d.get("source_conflicts")
                                         for d in result["decisions"]) else "needs_review"
    audit_path = getattr(provider, "audit_path", None)
    if audit_path:
        json_write(Path(audit_path).parent / "sequence_identity_review.json", result["identity_review"])
    return result
