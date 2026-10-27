"""Revalidate saved model plans after fixing endpoint typography handling."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

from .assays import identified_regions
from .llm_io import TableDecision, build_evidence, metric_endpoints, table_id
from .llm_table_agents import validate_classifications
from .table_structure import semantic_table, verified_header_links


def recover_metric_plans(state: dict, request_dir: Path) -> tuple[dict, list[dict]]:
    """Accept only complete, source-checked plans; never synthesize metrics."""
    result = copy.deepcopy(state)
    tables = [semantic_table(t) for t in state["ocr_results"]]
    by_id = {table_id(t): t for t in tables}
    evidence = build_evidence(tables, state.get("table_contexts", []))
    links = verified_header_links(tables, state.get("table_contexts", []))
    pending = {c["table_id"]: c for c in state["table_classifications"]
               if c.get("decision") == "needs_review"}
    recovered, audit = {}, []
    files = sorted(Path(request_dir).rglob("answer.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in files:
        try:
            response = json.loads(path.read_text())
        except (ValueError, OSError):
            continue
        if not isinstance(response, dict):
            continue
        proposals = response.get("tables", [])
        if not isinstance(proposals, list):
            continue
        for proposal in proposals:
            if not isinstance(proposal, dict):
                continue
            identity = proposal.get("table_id")
            if identity not in pending or identity in recovered or identity not in by_id:
                continue
            if proposal.get("decision") != "extract":
                continue
            try:
                decision = TableDecision.model_validate(proposal).model_dump()
                validate_classifications({"tables": [decision]}, [by_id[identity]], evidence, links)
            except ValueError:
                continue
            if not any(r["metrics"] or any(g["metrics"] for g in r["entity_groups"])
                       for r in decision["regions"] if r["kind"] == "assay"):
                continue
            metrics = [m for r in decision["regions"] for m in
                       [*r["metrics"], *(m for g in r["entity_groups"] for m in g["metrics"])]]
            if any(m["endpoint"] not in metric_endpoints(" ".join(evidence[ref]["text"] for ref in m["header_refs"]))
                   for m in metrics):
                continue
            origin = {"response_file": str(path.resolve()),
                      "response_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                      "validation": "schema, source references, endpoint headers, units and row/column bounds"}
            recovered[identity] = {**pending[identity], **decision,
                "regions": identified_regions(identity, [{**r, "execution_status": "ready"} for r in decision["regions"]]),
                "retain_for_pipeline": True, "review_required": bool(decision["issues"]),
                "model_decision": "extract", "understanding_status": "identified", "extraction_status": "ready",
                "evidence": [{"ref": ref, **evidence[ref]} for ref in decision["evidence_refs"]],
                "classifier": {**pending[identity].get("classifier", {}), "mode": "llm", "metric_recovery": origin}}
            audit.append({"table_id": identity, **origin})
    result["table_classifications"] = [recovered.get(c["table_id"], c) for c in state["table_classifications"]]
    return result, audit
