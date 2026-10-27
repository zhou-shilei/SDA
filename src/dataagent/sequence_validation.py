"""Audit sequence coverage and validate OCR sequence and assay candidates."""

from __future__ import annotations

import copy
import html
import re
from collections import Counter
from typing import Any

from .llm_io import build_evidence, cell_ref, contains_entity_name, normalize, table_id
from .sequence_evidence import SEQ_MARKER
from .sequence_resolver import (
    AA,
    SEQ_ID_VALUE_RE,
    _column_sequence_layouts,
    parse_sequence_definitions,
    resolve_sequences,
    sequence_scope,
)
from .sequence_resolver import (
    chain_from_text as _resolved_chain_from_text,
)

# Entity and chain coverage

def observed_entities(records: list[dict], template: dict | None) -> list[dict]:
    declared = {e["entity_id"]: copy.deepcopy(e) for e in (template or {}).get("entity_template", {}).get("entities", [])}
    entities: dict[str, dict] = {}
    for record in records:
        identity = str(record.get("canonical_entity_key") or record.get("antibody_id") or "")
        if not identity:
            continue
        entity = entities.setdefault(identity, declared.get(identity, {"entity_id": identity, "raw_names": []}))
        name = str(record.get("raw_antibody_id") or record.get("antibody_id") or "")
        # A declared entity's aliases cannot be expanded from an unvalidated
        # record label. A contradictory record belongs in identity review.
        if identity not in declared and name and name not in entity["raw_names"]:
            entity["raw_names"].append(name)
    return list(entities.values())


def explicit_sequence_links(tables: list[dict], entities: list[dict]) -> list[dict]:
    """Produce source addresses for conservative table-only mapping candidates."""
    links: list[dict] = []

    def add(entity: dict, chain: str, seq_id: int, scope: str, refs: list[str]) -> None:
        item = {"entity_id": entity["entity_id"], "chain": chain, "seq_id": seq_id,
                "scope": scope, "evidence_refs": list(dict.fromkeys(refs))}
        if item not in links:
            links.append(item)

    for table in tables:
        identity, rows = table_id(table), table.get("rows", [])
        for header, id_col, seq_col, label_col, header_chain in _column_sequence_layouts(table):
            if label_col is None:
                continue
            for ri in range(header + 1, len(rows)):
                row = rows[ri]
                if max(id_col, label_col, seq_col) >= len(row):
                    continue
                label = re.sub(r"<br\s*/?>", " ", str(row[label_col].get("text", "")), flags=re.I)
                id_text = re.sub(r"<br\s*/?>", " ", str(row[id_col].get("text", "")), flags=re.I).strip()
                match = SEQ_ID_VALUE_RE.fullmatch(id_text)
                chain = header_chain or _resolved_chain_from_text(label)
                if not match or chain not in {"VH", "VL"}:
                    continue
                # Scope is established from headers/labels, not invented by
                # treating a constant or CDR as a full variable domain.
                scope = sequence_scope(label, chain, "")
                if scope in {"constant_region", "heavy_cdr", "light_cdr"}:
                    continue
                refs = [cell_ref(identity, ri, label_col), cell_ref(identity, ri, id_col), cell_ref(identity, header, id_col)]
                if header_chain:
                    refs.append(cell_ref(identity, header, seq_col))
                for entity in entities:
                    owned = any(normalize(label) == normalize(name) if header_chain else contains_entity_name(label, name)
                                for name in entity["raw_names"])
                    if not owned:
                        continue
                    # "Dupilumab control" is a different observed entity from
                    # "Dupilumab". A longer literal variant name in this label
                    # cannot supply the shorter parent's sequence by substring.
                    more_specific_owner = any(other["entity_id"] != entity["entity_id"]
                        and contains_entity_name(label, other_name)
                        and any(len(normalize(other_name)) > len(normalize(name)) and contains_entity_name(other_name, name)
                                for name in entity["raw_names"])
                        for other in entities for other_name in other["raw_names"])
                    if not more_specific_owner:
                        add(entity, chain, int(match[1]), "full_chain" if scope.startswith("full_") or (
                            not header_chain and re.search(r"\b(?:heavy|light)\s+chain\b|抗体[重轻]链", label, re.I)
                            and not re.search(r"variable|可变|\bV[HL]\b", label, re.I)) else "variable", refs)

        # Index-only tables: Antibody | VH (SEQ ID NO) | VL (SEQ ID NO).
        # Require the SEQ semantics, literal same-row ownership and a chain
        # header aligned with the numeric identifier's own column.
        for hi, header_row in enumerate(rows[:3]):
            header_text = " ".join(str(c.get("text", "")) for c in header_row)
            if not re.search(r"SEQ\s*(?:<br>)?\s*ID|序列编号|序列号", header_text, re.I):
                continue
            for ci, header_cell in enumerate(header_row):
                label = re.sub(r"<br\s*/?>", " ", str(header_cell.get("text", "")), flags=re.I)
                chain = _resolved_chain_from_text(label)
                if not chain or re.search(r"constant|恒定|CDR", label, re.I):
                    continue
                for ri in range(hi + 1, len(rows)):
                    row = rows[ri]
                    match = SEQ_ID_VALUE_RE.fullmatch(str(row[ci].get("text", "")).strip()) if ci < len(row) else None
                    if not match:
                        continue
                    for entity in entities:
                        for ni, cell in enumerate(row):
                            if ni != ci and any(normalize(cell.get("text")) == normalize(name) for name in entity["raw_names"]):
                                add(entity, chain, int(match[1]), "variable", [cell_ref(identity, ri, ni), cell_ref(identity, ri, ci), cell_ref(identity, hi, ci)])
    return links


def audit_sequence_coverage(records: list[dict], tables: list[dict], template: dict | None = None,
                            passages: list[dict] | None = None) -> dict[str, Any]:
    if template is not None and "sequence_extraction" in template:
        return _audit_agent_values(records, tables, template)
    entities = observed_entities(records, template)
    evidence = build_evidence(tables, sequence_passages=passages)
    definitions = parse_sequence_definitions(tables, correct_ocr=False)
    candidates = explicit_sequence_links(tables, entities)
    declared = (template or {}).get("sequence_links", [])
    source_resolutions = resolve_sequences(tables, {"agent": {"mode": "llm"}, "entity_template": {"entities": entities},
        "sequence_links": [*candidates, *declared], "sequence_source_passages": passages or []})
    output: list[dict] = []
    repairs: list[dict] = []
    # Lexical discovery stays independent of executable layout parsers. A new
    # table layout can still be reported as an unresolved named reference.
    table_clues: dict[str, list[str]] = {}
    for table in tables:
        identity = table_id(table)
        text = " ".join(str(c.get("text", "")) for row in table.get("rows", []) for c in row)
        if re.search(r"SEQ\s*(?:<br>)?\s*ID|序列编号|序列号", text, re.I):
            table_clues[identity] = [ref for ref, source in evidence.items() if source.get("table_id") == identity and source.get("kind") == "cell"]
    for entity in entities:
        identity = entity["entity_id"]
        observations = [r for r in records if str(r.get("canonical_entity_key") or r.get("antibody_id") or "") == identity]
        owned_refs = [ref for refs in table_clues.values() for ref in refs
                      if any(contains_entity_name(evidence[ref]["text"], name) for name in entity["raw_names"])]
        owned_refs.extend(ref for ref, source in evidence.items() if source.get("kind") in {"method", "sequence_passage"}
                          and SEQ_MARKER.search(source.get("text", ""))
                          and any(contains_entity_name(source["text"], name) for name in entity["raw_names"]))
        item = {"entity_id": identity, "raw_names": entity["raw_names"],
                "record_ids": [str(r.get("record_id", "")) for r in observations], "chains": {}}
        for chain, field in (("VH", "vh"), ("VL", "vl")):
            choices = [link for link in candidates if link["entity_id"] == identity and link["chain"] == chain]
            plans = [link for link in declared if link["entity_id"] == identity and link["chain"] == chain]
            # Prefer explicit variable IDs over full-chain support, retaining
            # distinct conflicting IDs rather than silently choosing one.
            relevant = [link for link in [*choices, *plans] if link["scope"] == "variable"] or [*choices, *plans]
            ids = sorted({link["seq_id"] for link in relevant})
            refs = sorted({ref for link in relevant for ref in link.get("evidence_refs", [])} or set(owned_refs))
            missing = [str(r.get("record_id", "")) for r in observations if not r.get(field)]
            status, selected, source_sequence = "not_found", None, ""
            if len(ids) > 1:
                status = "mapping_conflict"
            elif ids:
                definition = definitions.get(ids[0])
                if not definition:
                    status = "definition_missing"
                elif definition.chain != chain or definition.sequence_scope in {"constant_region", "heavy_cdr", "light_cdr", "cdr"}:
                    status = "scope_conflict"
                else:
                    selected = next(link for link in relevant if link["seq_id"] == ids[0])
                    # Reuse only source residues, and check the resolver's
                    # length/type constraints before making an automatic task.
                    source_sequence = source_resolutions[identity].get(field, "")
                    if set(source_sequence) - AA:
                        source_sequence = ""
                    status = "linked" if source_sequence else "definition_invalid"
            elif owned_refs:
                status = "mapping_unresolved"
            present = [r.get(field, "") for r in observations if r.get(field)]
            if status == "not_found" and present:
                status = "source_unverified"
            conflicts = [str(r.get("record_id", "")) for r in observations if source_sequence and r.get(field) and r[field] != source_sequence]
            if source_sequence and any(value != source_sequence for value in present):
                status = "source_conflict"
            elif status == "linked" and missing:
                status = "link_missing"
                # Table-only, source-backed links are repairable without asking
                # a model to generate residues or invent a biological alias.
                if selected in choices:
                    repairs.append({**selected, "record_ids": missing})
            item["chains"][field] = {"status": status, "seq_ids": ids, "evidence_refs": refs,
                                     "missing_record_ids": missing, "conflicting_record_ids": conflicts,
                                     "present_count": len(present)}
        output.append(item)
    statuses = Counter(chain["status"] for item in output for chain in item["chains"].values())
    return {"schema_version": 1, "scope": "raw_source_sequence_coverage", "entities": output,
            "repair_candidates": repairs,
            "summary": {"entity_count": len(output), "record_count": len(records),
                        "complete_pair_count": sum(bool(r.get("vh") and r.get("vl")) for r in records),
                        "missing_chain_count": sum(not r.get(field) for r in records for field in ("vh", "vl")),
                        "source_backed_gap_count": sum(statuses[s] for s in ("link_missing", "definition_missing", "definition_invalid", "mapping_unresolved")),
                        "conflict_count": sum(statuses[s] for s in ("mapping_conflict", "scope_conflict", "source_conflict")),
                        "chain_status_counts": dict(statuses)}}


def _audit_agent_values(records: list[dict], tables: list[dict], template: dict) -> dict:
    """Recheck agent transcripts without letting old table patterns assign values."""
    entities = observed_entities(records, template)
    resolved = resolve_sequences(tables, template)
    output = []
    for entity in entities:
        identity = entity["entity_id"]
        observations = [r for r in records if str(r.get("canonical_entity_key") or r.get("antibody_id") or "") == identity]
        item = {"entity_id": identity, "raw_names": entity["raw_names"],
                "record_ids": [str(r.get("record_id", "")) for r in observations], "chains": {}}
        result = resolved.get(identity, {})
        for field in ("vh", "vl"):
            source_value = result.get(field, "")
            decision = result.get("sequence_resolution", {}).get(field, {})
            missing = [str(r.get("record_id", "")) for r in observations if not r.get(field)]
            conflicts = [str(r.get("record_id", "")) for r in observations
                         if r.get(field) and (not source_value or r[field] != source_value)]
            if source_value:
                status = "source_conflict" if conflicts else "link_missing" if missing else "linked"
            else:
                status = ("source_unverified" if conflicts else "mapping_conflict" if decision.get("status") == "ambiguous"
                          else "definition_invalid" if decision.get("status") == "source_invalid" else "mapping_unresolved")
            seq_id = result.get(field + "_seq_id")
            item["chains"][field] = {"status": status, "seq_ids": [seq_id] if seq_id is not None else [],
                "evidence_refs": decision.get("evidence_refs", []), "missing_record_ids": missing,
                "conflicting_record_ids": conflicts, "present_count": sum(bool(r.get(field)) for r in observations),
                "source_validated": bool(source_value), "agent_status": decision.get("status", "unresolved"),
                "rationale": decision.get("rationale", "No agent decision available")}
        output.append(item)
    statuses = Counter(chain["status"] for item in output for chain in item["chains"].values())
    return {"schema_version": 2, "scope": "agent_values_literal_source_coverage", "entities": output,
            "repair_candidates": [], "repair_policy": "Agent decisions require agent reanalysis; no rule-based sequence fill.",
            "summary": {"entity_count": len(output), "record_count": len(records),
                "complete_pair_count": sum(bool(r.get("vh") and r.get("vl")) for r in records),
                "missing_chain_count": sum(not r.get(field) for r in records for field in ("vh", "vl")),
                "source_backed_gap_count": sum(statuses[s] for s in ("link_missing", "definition_invalid", "mapping_unresolved")),
                "conflict_count": sum(statuses[s] for s in ("mapping_conflict", "source_conflict")),
                "chain_status_counts": dict(statuses)}}


def apply_sequence_links(records: list[dict], resolved: dict[str, dict], entity_id: str) -> list[dict]:
    """Fill only empty chains; preserve every assay observation and source ref."""
    output = copy.deepcopy(records)
    item = resolved[entity_id]
    for record in output:
        if str(record.get("canonical_entity_key") or record.get("antibody_id") or "") != entity_id:
            continue
        filled = [field for field in ("vh", "vl") if not record.get(field) and item.get(field)]
        if not filled:
            continue
        for field in filled:
            record[field] = item[field]
        evidence = record.setdefault("evidence", [])
        index = next((e for e in evidence if e.get("type") == "sequence_index"), None)
        if index is None:
            index = {"type": "sequence_index", "construct_key": entity_id, "rows": [], "resolution": {}}
            evidence.append(index)
        for field in filled:
            index[field + "_seq_id"] = item.get(field + "_seq_id")
            index.setdefault("resolution", {})[field] = item.get("sequence_resolution", {}).get(field, {})
            index.setdefault("locations", {}).pop(field, None)
            index["rows"] = [row for row in index.get("rows", []) if row.get("chain") != field.upper()] + [
                row for row in item.get("sequence_rows", []) if row.get("chain") == field.upper()]
        record["sequence_table_id"] = ",".join(item.get("sequence_table_ids", []))
        record["link_status"] = "review"
    return output


# OCR sequence and assay checks

# Keep the 20 canonical amino acids as the acceptance alphabet. The detector
# is intentionally more permissive than the validator: OCR mistakes such as
# ``O``, ``B`` or a digit must become a review item instead of disappearing.
AMINO_ACIDS = set("ACDEFGHIKLMNPQRSTVWY")
SEQUENCE_RE = re.compile(r"(?<![A-Z0-9])([A-Z0-9*?.-]{20,})(?![A-Z0-9])")
ASSAY_RE = re.compile(
    r"(?:IC\s*50|EC\s*50|K\s*D)"
    r"\s*(?:\([^)]*\))?\s*[:=]?\s*"
    r"(?P<operator><=|>=|<|>|~)?\s*"
    r"(?P<value>\d+(?:\.\d+)?)\s*"
    r"(?P<unit>pM|nM|uM|µM|mM)?",
    re.IGNORECASE,
)


def normalize_sequence(text: str) -> str:
    value = html.unescape(text).upper()
    value = re.sub(r"<BR\s*/?>", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\bSEQ\s+ID\s+NO\s*:?\s*\d+", "", value)
    return re.sub(r"[^A-Z]", "", value)


def chain_from_text(text: str) -> str:
    upper = text.upper()
    if re.search(r"\bV\s*H\b|HEAVY", upper):
        return "VH"
    if re.search(r"\bV\s*L\b|LIGHT", upper):
        return "VL"
    return "unknown"


def sequence_candidates(text: str) -> list[dict[str, Any]]:
    """Return candidate sequences without silently correcting characters."""

    raw = html.unescape(text)
    found: list[dict[str, Any]] = []
    for match in SEQUENCE_RE.finditer(raw.upper()):
        sequence = match.group(1)
        found.append(
            {
                "raw_text": raw,
                "normalized_sequence": sequence,
                "length": len(sequence),
                "valid_alphabet": set(sequence) <= AMINO_ACIDS,
                "invalid_characters": sorted(set(sequence) - AMINO_ACIDS),
                "chain": chain_from_text(raw),
            }
        )
    return found


def parse_assay_values(text: str) -> list[dict[str, Any]]:
    """Parse values while preserving comparison operators and units."""

    upper = text.upper()
    if not re.search(r"IC\s*50|EC\s*50|\bK\s*D\b", upper):
        return []
    values: list[dict[str, Any]] = []
    for match in ASSAY_RE.finditer(text):
        try:
            value = float(match.group("value"))
        except (TypeError, ValueError):
            continue
        values.append(
            {
                "raw_text": match.group(0),
                "operator": match.group("operator") or "=",
                "value": value,
                "unit": (match.group("unit") or "unknown").lower(),
            }
        )
    return values


def validate_ocr_results(
    document_id: str,
    ocr_results: list[dict[str, Any]],
    document_template: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    sequences: list[dict[str, Any]] = []
    assays: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    for table in ocr_results:
        table_id = str(table.get("source_id") or table.get("image") or "unknown")
        for row in table.get("rows", []):
            for cell in row if isinstance(row, list) else []:
                if not isinstance(cell, dict):
                    continue
                text = str(cell.get("text", ""))
                base = {
                    "document_id": document_id,
                    "table_id": table_id,
                    "image": table.get("image", ""),
                    "page": table.get("page"),
                    "row_index": cell.get("row_index", 0),
                    "col_index": cell.get("col_index", 0),
                    "bbox": cell.get("bbox", []),
                    "raw_text": text,
                    "confidence": cell.get("confidence"),
                }
                for candidate in sequence_candidates(text):
                    record = {**base, **candidate}
                    if record["length"] < 20 or not record["valid_alphabet"]:
                        record["validation_status"] = "review"
                        issues.append(
                            {
                                "kind": "sequence",
                                "severity": "high",
                                "table_id": table_id,
                                "message": "Sequence contains an invalid character or unexpected length.",
                                "raw_text": text,
                            }
                        )
                    else:
                        record["validation_status"] = "accepted"
                    sequences.append(record)
                for assay in parse_assay_values(text):
                    assays.append({**base, **assay, "link_status": "review"})
        # Most patent tables put the IC50 label in a header cell and values in
        # separate row/column cells. The cell-local parser above intentionally
        # stays conservative; this table-aware pass handles those grid layouts.
        from .dataset_linker import table_assay_rows

        seen = {
            (item.get("table_id"), item.get("row_index"), item.get("value"), item.get("entity"))
            for item in assays
        }
        for observation in table_assay_rows(
            table,
            "",
            document_template=document_template,
        ):
            key = (
                table_id,
                observation["row_index"],
                observation["ic50"],
                observation["entity"],
            )
            if key in seen:
                continue
            seen.add(key)
            assays.append(
                {
                    "document_id": document_id,
                    "table_id": table_id,
                    "image": table.get("image", ""),
                    "page": table.get("page"),
                    "row_index": observation["row_index"],
                    "col_index": observation.get("col_index"),
                    "bbox": [],
                    "raw_text": (
                        f"{observation['entity']} {observation['ic50']} "
                        f"{observation.get('ic50_unit', 'nM')}"
                    ),
                    "confidence": None,
                    "operator": "=",
                    "value": observation["ic50"],
                    "unit": str(observation.get("ic50_unit", "nM")).lower(),
                    "entity": observation["entity"],
                    "entity_key": observation.get("entity_key"),
                    "link_status": "review",
                }
            )
    return sequences, assays, issues
