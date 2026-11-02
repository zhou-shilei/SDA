"""Attach independent sequence checks to the final SDA template."""
from __future__ import annotations

import copy
import hashlib

from .pipeline import evaluate_sequences, normalize


def evaluate_template(template, output_dir):
    items = [{"construct_key": entity["entity_id"], **{
        chain.lower(): entity.get(chain, {}).get("sequence", {}).get("value")
        for chain in ("VH", "VL")}} for entity in template["entities"]]
    report = evaluate_sequences(items, output_dir)
    results = {r["construct_key"]: r for r in report["results"]}
    for entity in template["entities"]:
        result = copy.deepcopy(results[entity["entity_id"]])
        for chain in ("VH", "VL"):
            sequence = normalize(entity.get(chain, {}).get("sequence", {}).get("value"))
            if hashlib.sha256(sequence.encode()).hexdigest() != result["sequence_sha256"][chain]:
                raise ValueError("Evaluation belongs to a different exported sequence")
        # Source support is separate from model plausibility and human review.
        slots = [entity.get(chain, {}).get("sequence", {}) for chain in ("VH", "VL")]
        source_status = "error" if any(s.get("status") == "conflict" for s in slots) else (
            "passed" if all(s.get("status") == "present" and s.get("value") and s.get("provenance") for s in slots)
            else "manual_review")
        result["agent_review"] = {"status": source_status, "scope": "source_anchored_variable_sequences",
            "messages": ["检查最终 VH/VL 是否均有已接受的原文来源；不代表人工复核或模型通过。"]}
        statuses = {result[tool + "_review"]["status"] for tool in ("agent", "anarci", "esm2", "ophiuchus")}
        result["overall_review_status"] = "error" if "error" in statuses else "passed" if statuses == {"passed"} else "manual_review"
        result["pair_status"] = "conflict" if result["anarci_review"]["status"] == "error" else (
            "consistent" if result["overall_review_status"] == "passed" else "review")
        for chain in result["anarci_review"]["chains"].values():
            for domain in chain.get("domains", []):
                domain.pop("numbering", None)
        for tool in ("esm2", "ophiuchus"):
            for chain in result[tool + "_review"]["chains"].values():
                chain.pop("position_scores", None)
                chain.pop("source_hashes", None)
        entity["sequence_evaluation"] = result
    return report
