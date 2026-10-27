"""Validate VH/VL construct pairs and revalidate saved dataset records."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from .agent_store import _write_dataset_csv
from eval.anarci import pending as pending_anarci
from .sequence_resolver import clean_text, construct_keys

# Construct pair validation

AA = set("ACDEFGHIKLMNPQRSTVWY")
VARIABLE_LENGTH = {"VH": (80, 160), "VL": (80, 140)}
FAMILY_KEY_RE = re.compile(r"^h(\d+)((?:l\d+)+)$", re.I)


def _pending_esm2_review(message: str) -> dict[str, Any]:
    return {
        "status": "manual_review",
        "model": "esm2_t33_650M_UR50D",
        "method": "masked_pseudo_log_likelihood",
        "chains": {},
        "message": message,
    }


def _rule_chain_type(sequence: str) -> str | None:
    """Conservative prefix heuristic used only when ANARCI is unavailable."""

    upper = sequence.upper()
    heavy_prefixes = ("EVQL", "QVQL", "QMQL", "DVQL", "VQLV", "QLQL")
    light_prefixes = ("DIQ", "DIV", "EIV", "QSV", "QAV", "SYE", "SYV", "AIQ", "ETV")
    if upper.startswith(heavy_prefixes):
        return "VH"
    if upper.startswith(light_prefixes):
        return "VL"
    return None


def assess_chain(sequence: str, declared_chain: str) -> dict[str, Any]:
    upper = clean_text(sequence).upper()
    invalid = sorted(set(upper) - AA)
    low, high = VARIABLE_LENGTH[declared_chain]
    length_valid = low <= len(upper) <= high
    rule_type = _rule_chain_type(upper) if upper else None
    anarci = pending_anarci("ANARCI runs once on final sequences after evidence repairs.")
    predicted = (
        "VH" if anarci.get("chain_type") == "H"
        else "VL" if anarci.get("chain_type") in {"K", "L"}
        else rule_type
    )
    issues: list[str] = []
    if not upper:
        issues.append(f"Missing {declared_chain} sequence.")
    if invalid:
        issues.append(f"{declared_chain} contains invalid characters: {', '.join(invalid)}.")
    if upper and not length_valid:
        issues.append(f"{declared_chain} length {len(upper)} is outside {low}-{high} aa.")
    if predicted and predicted != declared_chain:
        issues.append(f"{declared_chain} field was classified as {predicted}.")
    if anarci["status"] == "error":
        issues.append(anarci["message"])

    if not upper or invalid or not length_valid:
        status = "invalid"
    elif predicted and predicted != declared_chain:
        status = "conflict"
    elif anarci["status"] == "matched":
        status = "consistent"
    else:
        status = "review"
    return {
        "declared_chain": declared_chain,
        "sequence_sha256": hashlib.sha256(upper.encode()).hexdigest(),
        "length": len(upper),
        "length_range": [low, high],
        "length_valid": length_valid,
        "valid_alphabet": not invalid,
        "invalid_characters": invalid,
        "rule_chain_type": rule_type,
        "predicted_chain": predicted,
        "anarci": anarci,
        "status": status,
        "issues": issues,
    }


def _family_from_evidence(item: dict[str, Any]) -> str | None:
    text = " ".join(
        clean_text(cell)
        for evidence in item.get("mapping_evidence", [])
        for cell in evidence.get("raw_cells", [])
    ).lower()
    if "csj117" in text or item.get("construct_key") == "csj117":
        return "csj117"
    if "tezep" in text or item.get("construct_key") == "tezepumab":
        return "tezepumab"
    return None


def _mapping_check(item: dict[str, Any]) -> dict[str, Any]:
    evidence = item.get("mapping_evidence", [])
    assignments = {
        chain: sorted({
            value
            for row in evidence
            for key, value in row.get("assignments", {}).items()
            if key == chain and value is not None
        })
        for chain in ("VH", "VL")
    }
    conflicts = {chain: values for chain, values in assignments.items() if len(values) > 1}
    grounded_chains = {chain for row in evidence for chain, status in row.get("chain_decisions", {}).items()
                       if status == "source_grounded"}
    missing_chains = [chain for chain, values in assignments.items() if not values and chain not in grounded_chains]
    resolved_mismatches: dict[str, dict[str, Any]] = {}
    for chain, field in (("VH", "vh_seq_id"), ("VL", "vl_seq_id")):
        resolved_id = item.get(field)
        if resolved_id is not None and assignments[chain] and resolved_id not in assignments[chain]:
            resolved_mismatches[chain] = {
                "resolved_seq_id": resolved_id,
                "evidence_seq_ids": assignments[chain],
            }
    sources = list(dict.fromkeys(
        row.get("header_mapping_status", "unknown") for row in evidence
    ))
    return {
        "status": (
            "conflict" if conflicts or resolved_mismatches
            else "missing" if not evidence
            else "incomplete" if missing_chains
            else "consistent"
        ),
        "assignments": assignments,
        "conflicts": conflicts,
        "missing_chains": missing_chains,
        "resolved_mismatches": resolved_mismatches,
        "evidence_types": sources,
        "evidence": evidence,
    }


def _assay_names(records: list[dict[str, Any]]) -> dict[str, list[str]]:
    names: dict[str, list[str]] = {}
    for record in records:
        if record.get("source") != "ocr":
            continue
        raw_name = clean_text(record.get("antibody_id"))
        keys = (
            [str(record["canonical_entity_key"])]
            if record.get("canonical_entity_key")
            else construct_keys(raw_name)
        )
        for key in keys:
            names.setdefault(key, []).append(raw_name)
    return {key: list(dict.fromkeys(values)) for key, values in names.items()}


def _agent_review(
    vh: dict[str, Any],
    vl: dict[str, Any],
    mapping: dict[str, Any],
    family: dict[str, Any],
) -> dict[str, Any]:
    errors: list[str] = []
    pending: list[str] = []
    for check in (vh, vl):
        chain = check["declared_chain"]
        if not check["valid_alphabet"]:
            errors.append(f"{chain} contains invalid amino-acid characters.")
        if not check["length_valid"]:
            errors.append(f"{chain} length is outside the accepted variable-domain range.")
        rule_type = check.get("rule_chain_type")
        if rule_type and rule_type != chain:
            errors.append(f"{chain} was classified as {rule_type} by Agent rules.")
        elif not rule_type:
            pending.append(f"{chain} could not be typed confidently by Agent rules.")
    if mapping["status"] == "conflict":
        errors.append("Construct-to-sequence mapping is conflicting.")
    elif mapping["status"] in {"missing", "incomplete"}:
        pending.append("Construct-to-sequence mapping requires confirmation.")
    if family["status"] == "conflict":
        errors.append("Mutation-family sequence evidence is conflicting.")

    status = "error" if errors else "manual_review" if pending else "passed"
    messages = errors or pending or ["Agent sequence and construct checks passed."]
    return {"status": status, "messages": messages}


def _anarci_review(vh: dict[str, Any], vl: dict[str, Any]) -> dict[str, Any]:
    chain_statuses = {
        "VH": vh.get("anarci", {}).get("status", "not_run"),
        "VL": vl.get("anarci", {}).get("status", "not_run"),
    }
    statuses = set(chain_statuses.values())
    if statuses == {"matched"}:
        status = "passed"
        message = "ANARCI classified both VH and VL with the declared chain types."
    elif statuses & {"conflict", "error"}:
        status = "error"
        message = "ANARCI reported an execution or chain-type error."
    else:
        status = "manual_review"
        message = "ANARCI did not validate both chains; manual review is required."
    return {
        "status": status,
        "chain_statuses": chain_statuses,
        "message": message,
    }


def _family_checks(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    checks: dict[str, dict[str, Any]] = {}
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for item in items:
        match = FAMILY_KEY_RE.match(item.get("construct_key", ""))
        family = item.get("family")
        if not match or not family:
            continue
        heavy_component = f"h{match.group(1)}"
        light_component = match.group(2).lower()
        grouped.setdefault((family, "VH", heavy_component), []).append(item)
        grouped.setdefault((family, "VL", light_component), []).append(item)

    for item in items:
        key = item.get("construct_key", "")
        checks[key] = {"status": "not_applicable", "components": [], "conflicts": []}
        match = FAMILY_KEY_RE.match(key)
        family = item.get("family")
        if not match or not family:
            continue
        components = [("VH", f"h{match.group(1)}"), ("VL", match.group(2).lower())]
        conflicts: list[dict[str, Any]] = []
        component_results: list[dict[str, Any]] = []
        for chain, component in components:
            peers = grouped.get((family, chain, component), [])
            sequences = {peer.get(chain.lower(), "") for peer in peers if peer.get(chain.lower())}
            peer_keys = [peer.get("construct_key") for peer in peers]
            consistent = len(sequences) <= 1
            result = {
                "chain": chain,
                "component": component,
                "peer_constructs": peer_keys,
                "unique_sequence_count": len(sequences),
                "status": "consistent" if consistent else "conflict",
            }
            component_results.append(result)
            if not consistent:
                conflicts.append(result)
        checks[key] = {
            "status": "conflict" if conflicts else "consistent",
            "components": component_results,
            "conflicts": conflicts,
        }
    return checks


def validate_construct_pairs(
    resolved_sequences: list[dict[str, Any]] | dict[str, dict[str, Any]],
    dataset_records: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Validate document identity and biological plausibility for each pair."""

    raw_items = (
        list(resolved_sequences.values())
        if isinstance(resolved_sequences, dict)
        else list(resolved_sequences)
    )
    assay_names = _assay_names(dataset_records or [])
    prepared: list[dict[str, Any]] = []
    for raw in raw_items:
        item = dict(raw)
        item["family"] = _family_from_evidence(item)
        prepared.append(item)
    family_checks = _family_checks(prepared)
    esm2_reviews = {}  # GPU scoring runs once after all evidence repairs, in src/eval.

    validations: list[dict[str, Any]] = []
    for item in prepared:
        key = item.get("construct_key", "")
        vh = assess_chain(item.get("vh", ""), "VH")
        vl = assess_chain(item.get("vl", ""), "VL")
        mapping = _mapping_check(item)
        family = family_checks.get(key, {"status": "not_applicable", "components": [], "conflicts": []})
        names = assay_names.get(key, [])
        issues = list(vh["issues"]) + list(vl["issues"])
        if mapping["status"] == "missing":
            issues.append("No construct-to-SEQ-ID evidence was found.")
        elif mapping["status"] == "incomplete":
            issues.append(f"Construct-index evidence is missing chains: {mapping['missing_chains']}.")
        elif mapping["status"] == "conflict":
            issues.append(
                "Conflicting construct-index assignments: "
                f"{mapping['conflicts'] or mapping['resolved_mismatches']}."
            )
        if family["status"] == "conflict":
            issues.append("Constructs sharing the same H/L mutation component do not share a sequence.")

        if mapping["status"] == "conflict" or family["status"] == "conflict" or "conflict" in {vh["status"], vl["status"]}:
            pair_status = "conflict"
        elif "invalid" in {vh["status"], vl["status"]}:
            pair_status = "incomplete" if not item.get("vh") or not item.get("vl") else "review"
        elif "review" in {vh["status"], vl["status"]} or mapping["status"] in {"missing", "incomplete"}:
            pair_status = "review"
        else:
            pair_status = "consistent"

        agent_review = _agent_review(vh, vl, mapping, family)
        anarci_review = _anarci_review(vh, vl)
        esm2_review = esm2_reviews.get(
            key,
            _pending_esm2_review("Final ESM-2 evaluation has not been run."),
        )
        if esm2_review["status"] == "error":
            issues.append(esm2_review.get("message", "ESM-2 sequence review failed."))
        review_statuses = {
            agent_review["status"],
            anarci_review["status"],
            esm2_review["status"],
        }
        overall_review_status = (
            "passed"
            if review_statuses == {"passed"}
            else "error"
            if "error" in review_statuses
            else "manual_review"
        )

        validations.append({
            "construct_key": key,
            "family": item.get("family"),
            "vh_seq_id": item.get("vh_seq_id"),
            "vl_seq_id": item.get("vl_seq_id"),
            "pair_status": pair_status,
            "validation_level": "anarci" if vh["anarci"]["status"] == "matched" and vl["anarci"]["status"] == "matched" else "rule_based",
            "agent_review": agent_review,
            "anarci_review": anarci_review,
            "esm2_review": esm2_review,
            "overall_review_status": overall_review_status,
            "vh_check": vh,
            "vl_check": vl,
            "mapping_check": mapping,
            "family_check": family,
            "assay_name_check": {
                "status": "normalized_exact" if names else "not_observed",
                "raw_names": names,
            },
            "issues": issues,
        })
    return validations


def apply_construct_validations(
    records: list[dict[str, Any]], validations: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Propagate pair validation to OCR dataset rows and their evidence."""

    by_key = {item["construct_key"]: item for item in validations}
    updated: list[dict[str, Any]] = []
    for source in records:
        record = dict(source)
        if record.get("source") != "ocr":
            updated.append(record)
            continue
        keys = (
            [str(record["canonical_entity_key"])]
            if record.get("canonical_entity_key")
            else construct_keys(clean_text(record.get("antibody_id")))
        )
        validation = by_key.get(keys[0]) if keys else None
        if validation:
            record["construct_validation_status"] = validation["pair_status"]
            agent_review = validation.get("agent_review", {"status": "manual_review"})
            anarci_review = validation.get("anarci_review", {"status": "manual_review"})
            esm2_review = validation.get(
                "esm2_review",
                _pending_esm2_review("ESM-2 sequence review is missing."),
            )
            record["agent_review_status"] = agent_review["status"]
            record["anarci_review_status"] = anarci_review["status"]
            record["esm2_review_status"] = esm2_review["status"]
            record["agent_review"] = agent_review
            record["anarci_review"] = anarci_review
            record["esm2_review"] = esm2_review
            record["link_status"] = (
                "linked"
                if {
                    agent_review["status"],
                    anarci_review["status"],
                    esm2_review["status"],
                } == {"passed"} and record.get("record_verification_status", "passed") == "passed"
                else "review"
            )
            prior_evidence = [
                item for item in record.get("evidence", [])
                if item.get("type") != "construct_validation"
            ]
            record["evidence"] = prior_evidence + [{
                "type": "construct_validation",
                "construct_key": validation["construct_key"],
                "pair_status": validation["pair_status"],
                "validation_level": validation["validation_level"],
                "agent_review": agent_review,
                "anarci_review": anarci_review,
                "esm2_review": esm2_review,
                "issues": validation["issues"],
            }]
        else:
            agent_review = {
                "status": "manual_review",
                "messages": ["No construct validation could be matched to this dataset record."],
            }
            anarci_review = {
                "status": "manual_review",
                "chain_statuses": {"VH": "not_run", "VL": "not_run"},
                "message": "ANARCI review is pending because the construct mapping is unresolved.",
            }
            esm2_review = _pending_esm2_review(
                "ESM-2 review is pending because the construct mapping is unresolved."
            )
            record["agent_review_status"] = "manual_review"
            record["anarci_review_status"] = "manual_review"
            record["esm2_review_status"] = "manual_review"
            record["agent_review"] = agent_review
            record["anarci_review"] = anarci_review
            record["esm2_review"] = esm2_review
            record["link_status"] = "review"
        updated.append(record)
    return updated


# Revalidate saved records

def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json_atomic(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _update_database(
    path: Path,
    run_id: str,
    state: dict[str, Any],
    validations: list[dict[str, Any]],
    records: list[dict[str, Any]],
) -> None:
    if not path.is_file() or not run_id:
        return
    with sqlite3.connect(path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(dataset_records)")}
        for column in ("agent_review_status", "anarci_review_status", "esm2_review_status"):
            if column not in columns:
                connection.execute(f"ALTER TABLE dataset_records ADD COLUMN {column} TEXT")
        connection.execute(
            "UPDATE runs SET state_json = ? WHERE run_id = ?",
            (json.dumps(state, ensure_ascii=False), run_id),
        )
        connection.execute("DELETE FROM construct_validations WHERE run_id = ?", (run_id,))
        for validation in validations:
            connection.execute(
                """INSERT INTO construct_validations
                   (run_id, construct_key, family, pair_status,
                    validation_level, validation_json)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    validation.get("construct_key", ""),
                    validation.get("family"),
                    validation.get("pair_status", "review"),
                    validation.get("validation_level", "rule_based"),
                    json.dumps(validation, ensure_ascii=False),
                ),
            )
        for record in records:
            connection.execute(
                """UPDATE dataset_records
                   SET agent_review_status = ?, anarci_review_status = ?, esm2_review_status = ?,
                       link_status = ?, evidence_json = ?
                   WHERE run_id = ? AND record_id = ?""",
                (
                    record.get("agent_review_status", "manual_review"),
                    record.get("anarci_review_status", "manual_review"),
                    record.get("esm2_review_status", "manual_review"),
                    record.get("link_status", "review"),
                    json.dumps(record.get("evidence", []), ensure_ascii=False),
                    run_id,
                    record.get("record_id", ""),
                ),
            )


def revalidate(output_dir: Path) -> dict[str, Any]:
    resolved_path = output_dir / "resolved_sequences.json"
    records_path = output_dir / "dataset_records.json"
    if not resolved_path.is_file() or not records_path.is_file():
        raise FileNotFoundError(f"{output_dir}: missing resolved_sequences.json or dataset_records.json")

    resolved = _read_json(resolved_path)
    records = _read_json(records_path)
    validations = validate_construct_pairs(resolved, records)
    from eval.pipeline import evaluate_sequences, merge_evaluations
    report = evaluate_sequences(resolved, output_dir / "eval")
    validations = merge_evaluations(validations, report)
    updated_records = apply_construct_validations(records, validations)

    state_path = output_dir / "agent_state.json"
    state = _read_json(state_path) if state_path.is_file() else {}
    if not isinstance(state, dict):
        state = {}
    state["resolved_sequences"] = resolved
    state["construct_validations"] = validations
    state["sequence_evaluation"] = report
    state["dataset_records"] = updated_records

    _write_json_atomic(output_dir / "construct_validations.json", validations)
    _write_json_atomic(records_path, updated_records)
    _write_dataset_csv(output_dir / "dataset_records.csv", updated_records)
    if state_path.is_file():
        _write_json_atomic(state_path, state)
    _update_database(
        output_dir / "agent_run.sqlite",
        str(state.get("run_id", "")),
        state,
        validations,
        updated_records,
    )

    validation_counts: dict[str, int] = {}
    for validation in validations:
        status = validation.get("overall_review_status", "manual_review")
        validation_counts[status] = validation_counts.get(status, 0) + 1
    record_counts: dict[str, int] = {}
    for record in updated_records:
        status = record.get("anarci_review_status", "manual_review")
        record_counts[status] = record_counts.get(status, 0) + 1
    return {
        "output_dir": str(output_dir),
        "construct_count": len(validations),
        "record_count": len(updated_records),
        "construct_review_counts": validation_counts,
        "record_anarci_counts": record_counts,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Re-run ANARCI review for published agent runs")
    parser.add_argument("output_dirs", nargs="+", type=Path)
    args = parser.parse_args()
    summaries = [revalidate(path.resolve()) for path in args.output_dirs]
    print(json.dumps(summaries, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
