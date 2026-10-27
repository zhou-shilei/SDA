"""QuestionAgent -> AnswerAgent -> validated modifications, bounded per run."""
from __future__ import annotations

import copy
import hashlib
import json
from collections import Counter
from pathlib import Path

from .llm_runtime import CodexProviderError, json_write


def collect_diagnostics(state: dict) -> list[dict]:
    """One shared source defect is one diagnostic, retaining every affected chain."""
    sources = state.get("document_structure", {}).get("sources", {})
    extraction = state.get("sequence_extraction", {}) or state.get("document_template", {}).get("sequence_extraction", {})
    names = {e["entity_id"]: e.get("canonical_name", e["entity_id"])
             for e in state.get("document_template", {}).get("entity_template", {}).get("entities", [])}
    groups = {}

    def add(key, category, code, message, refs=(), targets=(), record_ids=(), table_ids=(), priority=2):
        identity = hashlib.sha256(json.dumps(key, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        task = groups.setdefault(identity, {"id": "diagnostic-" + identity[:20], "fingerprint": identity,
            "category": category, "page": None, "table_ids": [], "record_ids": [],
            "codes": [], "messages": [], "evidence_refs": [], "targets": [], "priority": priority})
        for field, values in (("codes", [code]), ("messages", [message]), ("evidence_refs", refs),
                              ("targets", targets), ("record_ids", record_ids), ("table_ids", table_ids)):
            for value in values:
                if value not in task[field]:
                    task[field].append(value)
        for ref in refs:
            source = sources.get(ref, {})
            if source.get("table_id") and source["table_id"] not in task["table_ids"]:
                task["table_ids"].append(source["table_id"])
            if task["page"] is None:
                task["page"] = source.get("page")

    for decision in extraction.get("decisions", []):
        target = {"entity_id": decision["entity_id"], "chain": decision["chain"],
                  "name": names.get(decision["entity_id"], decision["entity_id"])}
        if decision["status"] != "resolved":
            # Shared SEQ definitions are independent of the affected assay-row names.
            scope = decision["seq_id"] if decision["seq_id"] is not None else decision["entity_id"]
            add(("sequence", scope, decision["chain"]), "sequence", "sequence_" + decision["status"],
                decision["rationale"], decision["evidence_refs"], [target], priority=0)
        # A resolved table sequence remains the selected value. Page OCR
        # disagreements are display-only and must not consume question budget.
    for finding in state.get("record_verification", {}).get("findings", []):
        if finding["code"] == "table_text_mismatch" or finding["code"].startswith("sequence_"):
            continue  # Already represented by the original sequence decision and its source defect.
        add((finding["code"], finding.get("table_id"), finding["message"]), "assay", finding["code"],
            finding["message"], finding.get("evidence_refs", []), record_ids=[finding["record_id"]],
            table_ids=[finding["table_id"]] if finding.get("table_id") else [])
    for warning in state.get("template_validation", {}).get("warnings", []):
        if warning.get("code", "").startswith(("sequence_agent_", "sequence_identity_")) or warning.get("code") == "table_text_mismatch":
            continue
        refs = warning.get("evidence_refs", [])
        if refs:
            add(("template", warning.get("code"), warning.get("message")), "source",
                warning.get("code", "template_gap"), warning.get("message", "Source evidence needs review"),
                refs, table_ids=warning.get("table_ids", []), priority=3)
    return sorted(groups.values(), key=lambda t: (t["priority"], t["id"]))


def measurement_signature(records: list[dict]) -> Counter:
    fields = ("record_id", "document_id", "antibody_id", "raw_antibody_id", "canonical_entity_key",
              "assay_table_id", "measurement_id", "ic50", "ic50_unit", "ic50_operator", "assay_endpoint")
    return Counter(json.dumps({key: r.get(key) for key in fields}, sort_keys=True, ensure_ascii=False)
                   for r in records)


def stage_answer(state: dict, question: dict, result: dict) -> dict:
    """Build an isolated candidate. The caller must relink and verify before committing."""
    from .answer_agent import validate_answer
    from .assays import materialize_profiles, table_profile_summaries
    from .llm_sequence_agent import decision_refs, ground_decision

    template = copy.deepcopy(state["document_template"])
    extraction = copy.deepcopy(state.get("sequence_extraction") or template.get("sequence_extraction", {}))
    sources = state["document_structure"]["sources"]
    regions = {c["table_id"]: c.get("regions", []) for c in state.get("table_classifications", [])}
    reviewed = result.get("reviewed_sources", {})
    manifests = {s["image_review"]["original_ref"]: {
        "reviewed_ref": ref, "image_path": s["image_review"]["image_path"],
        "image_sha256": s["image_review"]["image_sha256"]} for ref, s in reviewed.items()}
    loaded = {ref: sources[ref] for ref in result.get("loaded_refs", []) if ref in sources}
    ledger = validate_answer(result["answer"], state["config"]["document_id"], question, loaded,
                             manifests, extraction.get("decisions", []), regions)
    if {ref: ledger[ref] for ref in reviewed} != reviewed:
        raise ValueError("Reviewed-source audit changed after the answer was validated")
    replacements = {}
    for update in result["answer"]["sequence_updates"]:
        item = copy.deepcopy(update)
        item["rationale"] = f"AnswerAgent 原始证据复核：{item['rationale']}"
        item["grounded_fragments"] = ground_decision(update, ledger)
        replacements[item["entity_id"], item["chain"]] = item
        # A derived source retains the original cell too, so provenance can always be revalidated.
        refs = set(decision_refs(update))
        refs.update(ledger[r]["image_review"]["original_ref"] for r in list(refs)
                    if ledger[r].get("image_review"))
        extraction.setdefault("sources", {}).update({r: ledger[r] for r in refs})
    extraction["decisions"] = [replacements.get((d["entity_id"], d["chain"]), d) for d in extraction.get("decisions", [])]
    extraction["status"] = "validated" if all(d["status"] == "resolved" and not d.get("source_conflicts")
                                               for d in extraction["decisions"]) else "needs_review"
    template["sequence_extraction"] = extraction
    profiles = result["answer"]["assay_updates"]
    if profiles:
        revised = materialize_profiles(profiles, sources, result.get("llm", {}))
        keys = {(p["table_id"], p.get("region_id"), tuple(sorted(p["endpoints"]))) for p in revised}
        existing = template.get("assay_template", {}).get("region_profiles", [])
        old_keys = {(p["table_id"], p.get("region_id"), tuple(sorted(p["endpoints"]))) for p in existing}
        if not keys <= old_keys:
            raise ValueError("Assay answers may only revise existing region profiles")
        assay = template.setdefault("assay_template", {})
        assay["region_profiles"] = [p for p in existing if
            (p["table_id"], p.get("region_id"), tuple(sorted(p["endpoints"]))) not in keys] + revised
        assay["table_profiles"] = table_profile_summaries(assay["region_profiles"])
    validation = copy.deepcopy(state.get("template_validation", {}))
    validation["sequence_extraction_status"] = extraction["status"]
    validation["warnings"] = [w for w in validation.get("warnings", [])
                               if not w.get("code", "").startswith("sequence_agent_")]
    validation["warnings"].extend({"code": "sequence_agent_" + d["status"], "severity": "medium",
        "message": f"{d['entity_id']} {d['chain']}: {d['rationale']}", "evidence_refs": d["evidence_refs"],
        "table_ids": []} for d in extraction["decisions"] if d["status"] != "resolved")
    return {"sequence_extraction": extraction, "document_template": template, "template_validation": validation}


def run_question_workflow(state: dict, provider, *, image_roots: list[Path], max_questions=12) -> dict:
    from .answer_agent import AnswerAgent
    from .question_agent import QuestionAgent, question_tasks
    from .agent.graph import link_node, construct_validation_node, verify_records_node

    root = Path(state["config"]["output_dir"]) / "question_review"
    diagnostics = collect_diagnostics(state)
    report = {"schema_version": 1, "document_id": state["config"]["document_id"], "rounds": 1,
        "status": "completed", "diagnostic_count": len(diagnostics), "questions": [], "answers": [],
        "changes": [], "deferred_diagnostics": diagnostics[64:], "applied_chain_count": 0}
    # Build a question queue beyond the automatic-answer budget. Very large documents
    # keep remaining diagnostics explicitly pending instead of unbounded model calls.
    selected = diagnostics[:64]
    print(f"llm-questions: diagnostics={len(diagnostics)} selected={len(selected)}", flush=True)
    json_write(root / "diagnostics.json", diagnostics)
    try:
        if selected:
            plan = QuestionAgent(provider, audience="answer_agent").run(report["document_id"], selected)
            # Never silently exceed the bounded AnswerAgent budget, even if a diagnosis has several questions.
            questions = question_tasks(plan["questions"], selected)
            by_diagnostic = {d["id"]: d for d in selected}
            for q in questions:
                q["targets"] = []
                for task_id in q["source_task_ids"]:
                    for target in by_diagnostic[task_id].get("targets", []):
                        if target not in q["targets"]:
                            q["targets"].append(target)
            report.update(questions=questions, question_agent=plan["agent"])
    except (CodexProviderError, ValueError) as exc:
        report.update(status="needs_review", error=str(exc), deferred_diagnostics=diagnostics)
    json_write(root / "questions.json", report["questions"])
    graph = {**state.get("document_structure", {}), "assay_regions": {
        c["table_id"]: c.get("regions", []) for c in state.get("table_classifications", [])}}
    report["answers"] = AnswerAgent(provider, image_roots=image_roots).run(report["document_id"],
        report["questions"][:max_questions], graph, state.get("ocr_results", []), state.get("document_template", {}))
    json_write(root / "answers.json", report["answers"])
    working = copy.deepcopy(state)
    json_write(root / "changes.json", [])
    for question, answer in zip(report["questions"], report["answers"]):
        decision = {"question_id": question["id"], "status": "needs_review"}
        if answer["status"] == "validated" and answer["answer"]["status"] == "answered":
            value = answer["answer"]
            if not value["sequence_updates"] and not value["assay_updates"]:
                decision["status"] = "answered_without_change" if question.get("codes") == ["table_text_mismatch"] else "needs_review"
                if decision["status"] == "needs_review":
                    decision["reason"] = "回答尚未形成可校验的修改，原问题保留。"
            else:
                try:
                    candidate = {**working, **stage_answer(working, question, answer)}
                    candidate.update(link_node(candidate))
                    if measurement_signature(candidate["dataset_records"]) != measurement_signature(working["dataset_records"]):
                        raise ValueError("Answer would change measurement values, names, units, endpoints or observation count")
                    # Previously resolved sequences must survive the complete relink unchanged.
                    before = {d["record_id"]: d for d in working.get("dataset_records", [])}
                    if any(before[r["record_id"]].get(field) and before[r["record_id"]][field] != r.get(field)
                           for r in candidate["dataset_records"] for field in ("vh", "vl")):
                        raise ValueError("Answer would change an already resolved sequence")
                    candidate["review_tasks"] = [t for t in candidate.get("review_tasks", []) if t.get("kind") != "construct_pair"]
                    candidate.update(construct_validation_node(candidate))
                    candidate.update(verify_records_node(candidate))
                    errors = lambda s: {(f.get("record_id"), f["code"]) for f in s.get("record_verification", {}).get("findings", []) if f["severity"] == "error"}
                    if not errors(candidate) <= errors(working):
                        raise ValueError("Answer introduced a new source-validation error")
                    target_ids = {u["entity_id"] for u in value["sequence_updates"]}
                    if any(v.get("construct_key") in target_ids and
                           (v.get("agent_review", {}).get("status") == "error" or v.get("anarci_review", {}).get("status") == "error")
                           for v in candidate.get("construct_validations", [])):
                        raise ValueError("Answer failed construct/chain validation")
                    working = candidate
                    decision.update(status="applied", sequence_updates=[{"entity_id": u["entity_id"], "chain": u["chain"]}
                        for u in value["sequence_updates"]], assay_profile_count=len(value["assay_updates"]))
                    report["applied_chain_count"] += len(value["sequence_updates"])
                except (ValueError, KeyError) as exc:
                    decision.update(status="rejected", reason=str(exc))
        report["changes"].append(decision)
        json_write(root / "changes.json", report["changes"])
    if (report.get("error") or report["deferred_diagnostics"] or len(report["questions"]) > len(report["answers"])
            or any(c["status"] in {"needs_review", "rejected"} for c in report["changes"])):
        report["status"] = "needs_review"
    json_write(root / "report.json", report)
    fields = ("sequence_extraction", "document_template", "template_validation", "dataset_records", "resolved_sequences",
              "construct_validations", "record_verification", "repair_plan", "repair_state", "review_tasks")
    return {**{field: working[field] for field in fields if field in working}, "question_review": report,
        "artifacts": {**state.get("artifacts", {}), "question_review": str(root / "report.json"),
                      "questions": str(root / "questions.json"), "answers": str(root / "answers.json"),
                      "answer_changes": str(root / "changes.json")},
        "llm_runs": [*state.get("llm_runs", []), *provider.calls]}
