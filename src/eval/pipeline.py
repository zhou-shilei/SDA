"""Evaluate final variable domains in batches and retain independent tool results."""
from __future__ import annotations

import copy
import csv
import hashlib
import json
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .anarci import pending
from .runtime import EvaluationConfig, run_worker


def normalize(sequence) -> str:
    return "".join(str(sequence or "").split()).upper()


def chain_id(sequence: str, chain: str) -> str:
    return chain + ":" + hashlib.sha256(sequence.encode()).hexdigest()


def aggregate_anarci(chains: dict) -> dict:
    statuses = {chain: value["status"] for chain, value in chains.items()}
    values = set(statuses.values())
    failures = {"conflict", "invalid", "unclassified", "partial", "multiple_domains"}
    status = "passed" if values == {"matched"} else "error" if values & failures else "manual_review"
    return {"status": status, "scheme": "imgt", "chain_statuses": statuses, "chains": chains,
        "message": "Both variable domains match their declared ANARCI chain types." if status == "passed" else
                   "ANARCI sequence screening failed: " + "; ".join(c["message"] for c in chains.values() if c["status"] in failures) if status == "error" else
                   "ANARCI evaluation needs review: " + "; ".join(c["message"] for c in chains.values() if c["status"] != "matched")}


def aggregate_esm2(chains: dict) -> dict:
    statuses = {c["status"] for c in chains.values()}
    status = "error" if "error" in statuses else "passed" if statuses == {"passed"} else "manual_review"
    return {"status": status, "method": "masked_pseudo_log_likelihood",
        "model": next((c.get("model") for c in chains.values() if c.get("model")), "esm2_t33_650M_UR50D"),
        "chains": chains, "message": "; ".join(c["message"] for c in chains.values())}


def evaluate_sequences(items: list[dict], output_dir: Path, *, config: EvaluationConfig | None = None) -> dict:
    config = config or EvaluationConfig.from_env()
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    keys = [item["construct_key"] for item in items]
    if len(keys) != len(set(keys)):
        raise ValueError("Evaluation construct keys must be unique")
    unique = {}
    for item in items:
        for chain in ("VH", "VL"):
            seq = normalize(item.get(chain.lower()))
            identity = chain_id(seq, chain)
            unique.setdefault(identity, {"id": identity, "chain": chain, "sequence": seq})
    payload = list(unique.values())
    anarci, anarci_runtime = run_worker("anarci", payload, config, output_dir)
    esm2, esm2_runtime = run_worker("esm2", payload, config, output_dir)
    results = []
    for item in items:
        ac, ec, hashes = {}, {}, {}
        for chain in ("VH", "VL"):
            seq = normalize(item.get(chain.lower()))
            identity = chain_id(seq, chain)
            hashes[chain] = hashlib.sha256(seq.encode()).hexdigest()
            ac[chain] = copy.deepcopy(anarci.get(identity, pending(anarci_runtime.get("reason", "ANARCI worker did not return a result"))))
            ec[chain] = copy.deepcopy(esm2.get(identity, {"status": "manual_review", "metric_status": "not_run",
                "length": len(seq), "message": esm2_runtime.get("reason", "ESM-2 worker did not return a result")}))
        results.append({"construct_key": item["construct_key"], "sequence_sha256": hashes,
                        "anarci_review": aggregate_anarci(ac), "esm2_review": aggregate_esm2(ec)})
    summary = {tool: dict(Counter(r[tool + "_review"]["status"] for r in results)) for tool in ("anarci", "esm2")}
    summary.update(construct_count=len(items), chain_count=2 * len(items), unique_chain_count=len(unique),
        unique_sequence_count=len({p["sequence"] for p in payload if p["sequence"]}),
        esm2_scored_chain_count=sum(c.get("metric_status") == "scored" for r in results for c in r["esm2_review"]["chains"].values()))
    summary["esm2_flagged_chain_count"] = sum(c.get("site_screening", {}).get("risk_detected", False)
        for r in results for c in r["esm2_review"]["chains"].values())
    report = {"schema_version": 1, "evaluated_at": datetime.now(timezone.utc).isoformat(),
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "status": "completed" if not items or all(m["status"] == "completed" for m in (anarci_runtime, esm2_runtime)) else "partial",
        "summary": summary, "runtime": {"anarci": anarci_runtime, "esm2": esm2_runtime}, "results": results}
    for name, value in (("sequence_evaluation", report), ("anarci_results", {"runtime": anarci_runtime, "results": anarci}),
                        ("esm2_results", {"runtime": esm2_runtime, "results": esm2})):
        (output_dir / (name + ".json")).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    write_evaluation_csv(report, output_dir)
    return report


def write_evaluation_csv(report: dict, output_dir: Path) -> None:
    with (output_dir / "sequence_evaluation.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["construct_key", "chain", "sequence_sha256", "anarci_status",
            "anarci_chain_type", "esm2_status", "esm2_mean_pll", "esm2_pseudo_perplexity",
            "esm2_max_site_pseudo_perplexity", "esm2_flagged_position_count", "esm2_flagged_positions"])
        writer.writeheader()
        for result in report["results"]:
            for chain in ("VH", "VL"):
                ar, er = result["anarci_review"]["chains"][chain], result["esm2_review"]["chains"][chain]
                writer.writerow({"construct_key": result["construct_key"], "chain": chain,
                    "sequence_sha256": result["sequence_sha256"][chain], "anarci_status": ar["status"],
                    "anarci_chain_type": ar.get("chain_type"), "esm2_status": er["status"],
                    "esm2_mean_pll": er.get("mean_pll"), "esm2_pseudo_perplexity": er.get("pseudo_perplexity"),
                    "esm2_max_site_pseudo_perplexity": er.get("max_site_pseudo_perplexity"),
                    "esm2_flagged_position_count": er.get("flagged_position_count"),
                    "esm2_flagged_positions": "; ".join(f"{p['position']}{p['residue']}" for p in er.get("flagged_positions", []))})


def merge_evaluations(validations: list[dict], report: dict) -> list[dict]:
    """Join model results to the exact VH/VL bytes assessed by construct checks."""
    results = {r["construct_key"]: r for r in report["results"]}
    if set(results) != {v["construct_key"] for v in validations} or len(results) != len(report["results"]):
        raise ValueError("Evaluation construct set differs from the validated constructs")
    output = copy.deepcopy(validations)
    for validation in output:
        result = results.get(validation["construct_key"])
        if result is None:
            raise ValueError("Evaluation result is missing a construct")
        for chain in ("VH", "VL"):
            if validation[chain.lower() + "_check"].get("sequence_sha256") != result["sequence_sha256"][chain]:
                raise ValueError("Evaluation results belong to a different VH/VL sequence")
        validation.update(copy.deepcopy(result))
        for chain in ("VH", "VL"):
            check = validation[chain.lower() + "_check"]
            check["anarci"] = copy.deepcopy(result["anarci_review"]["chains"][chain])
            predicted = check["anarci"].get("chain_type")
            if predicted:
                check["predicted_chain"] = "VH" if predicted == "H" else "VL" if predicted in {"K", "L"} else None
            if check["anarci"]["status"] == "matched" and check["valid_alphabet"] and check["length_valid"]:
                check["status"] = "consistent"
            elif check["anarci"]["status"] == "conflict":
                check["status"] = "conflict"
        validation["validation_level"] = "anarci" if result["anarci_review"]["status"] == "passed" else "rule_based"
        statuses = {validation[tool + "_review"]["status"] for tool in ("agent", "anarci", "esm2")}
        validation["overall_review_status"] = "error" if "error" in statuses else "passed" if statuses == {"passed"} else "manual_review"
        if any(c["status"] == "conflict" for c in result["anarci_review"]["chains"].values()):
            validation["pair_status"] = "conflict"
        elif result["anarci_review"]["status"] == "error" and validation["pair_status"] not in {"conflict", "incomplete"}:
            validation["pair_status"] = "review"
        elif (result["anarci_review"]["status"] == "passed" and validation["agent_review"]["status"] == "passed"
              and validation["pair_status"] == "review"):
            validation["pair_status"] = "consistent"
        for tool in ("anarci", "esm2"):
            if result[tool + "_review"]["status"] == "error":
                validation["issues"].append(result[tool + "_review"]["message"])
    return output
