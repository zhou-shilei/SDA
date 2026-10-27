"""Verify record evidence, export measurements, evaluate predictions, and plan repairs."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from collections import Counter
from decimal import Decimal, InvalidOperation
from typing import Any

from .assays import identified_regions, region_endpoints, select_assay_profile
from .llm_io import build_evidence, metric_endpoints, normalize, table_id
from .sequence_validation import audit_sequence_coverage
from .table_structure import semantic_table

# Record evidence verification

NUMBER = re.compile(r"^\s*(<=|>=|[<>≤≥~≈])?\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s*(pM|nM|[µμu]M|mM|M|[µμu]g/mL|mg/mL)?\s*$")
UNITS = re.compile(r"(?<![A-Za-z])(pM|nM|[µμu]M|mM|M|[µμu]g/mL|mg/mL)(?![A-Za-z])")


def parse_measurement(text: str) -> tuple[float, str, str] | None:
    match = NUMBER.fullmatch(text)
    if not match or not math.isfinite(float(match[2])):
        return None
    return float(match[2]), {"≤": "<=", "≥": ">=", "≈": "~"}.get(match[1], match[1] or "="), match[3] or ""


def normalized_unit(unit: str) -> str:
    return str(unit).replace("µ", "u").replace("μ", "u").strip()


def endpoints_in(text: str) -> set[str]:
    compact = re.sub(r"\s+|_", "", text).translate(str.maketrans("₅₀₉", "509"))
    values = metric_endpoints(text)
    # Some method OCR spells IC50 as ICS0; this is only a protocol clue.
    if re.search(r"IC[Ss]0", compact):
        values.add("IC50")
    return values


def method_kinds(text: str) -> set[str]:
    patterns = {"spr": r"\bSPR\b|Biacore|surface\s+plasmon|表面\s*等\s*离子",
                "reporter": r"reporter|报告\s*基因", "facs": r"\bFACS\b|flow\s+cytometr|流\s*式",
                "elisa": r"\bELISA\b|enzyme.linked\s+immunosorbent", "proliferation": r"proliferation|增殖"}
    return {kind for kind, pattern in patterns.items() if re.search(pattern, text, re.I)}


def _key(record: dict, index: int) -> str:
    return str(record.get("record_id") or f"record-{index}")


def verify_records(
    records: list[dict], tables: list[dict], template: dict | None = None,
    contexts: list[dict] | None = None, method_pages: list[dict] | None = None,
    classifications: list[dict] | None = None,
    sequence_passages: list[dict] | None = None,
) -> tuple[list[dict], dict[str, Any]]:
    raw_sequence_sources = tables
    classifications_by_id = {str(c["table_id"]): c for c in classifications or []}
    tables = [semantic_table({**t, "table_classification": classifications_by_id.get(table_id(t), t.get("table_classification", {}))}) for t in tables]
    by_id = {table_id(t): t for t in tables}
    passages = [*(sequence_passages or []), *(template or {}).get("sequence_source_passages", []), *(method_pages or [])]
    evidence = build_evidence(tables, contexts, method_pages, passages)
    sequence_coverage = audit_sequence_coverage(records,
        raw_sequence_sources if "sequence_extraction" in (template or {}) else tables, template, passages)
    sequence_entities = {entity["entity_id"]: entity for entity in sequence_coverage["entities"]}
    output = copy.deepcopy(records)
    findings: list[dict] = []
    validations = []
    claims: dict[tuple, set[int]] = {}
    for index, record in enumerate(output):
        for source in record.get("evidence", []):
            if source.get("type") == "assay_measurement" and source.get("value_ref"):
                anchor = (source.get("table_id"), source["value_ref"], source.get("entity_ref"), record.get("assay_endpoint", "IC50"))
                claims.setdefault(anchor, set()).add(index)

    def add(record_id: str | None, identity: str, code: str, message: str,
            refs: list[str] | None = None, *, error: bool = False, action: str | None = None,
            entity_id: str | None = None, field: str | None = None) -> None:
        refs = sorted(set(ref for ref in refs or [] if ref))
        fingerprint = "|".join([str(record_id), identity, code, *refs])
        if entity_id or field:
            fingerprint += "|" + str(entity_id) + "|" + str(field)
        finding = {"finding_id": hashlib.sha256(fingerprint.encode()).hexdigest()[:20],
                   "record_id": record_id, "table_id": identity, "code": code,
                   "severity": "error" if error else "warning", "message": message,
                   "evidence_refs": refs, "repair_action": action}
        if entity_id:
            finding.update(entity_id=entity_id, field=field)
        if not any(f["finding_id"] == finding["finding_id"] for f in findings):
            findings.append(finding)

    for index, record in enumerate(output):
        record_id = _key(record, index)
        start = len(findings)
        for conflict in record.get("sequence_source_conflicts", []):
            table_source = evidence.get(conflict["table_ref"], {})
            add(record_id, str(table_source.get("table_id", "")), "table_text_mismatch",
                f"{conflict.get('chain', '')} 表格和文字不一致；以表格单元格为金标准。 {conflict['message']}",
                [conflict["table_ref"], conflict["text_ref"]])
        measurements = [e for e in record.get("evidence", []) if e.get("type") == "assay_measurement"]
        if not measurements:
            add(record_id, str(record.get("assay_table_id", "")), "measurement_evidence_missing", "No source measurement was supplied.")
        for source in measurements:
            identity = str(source.get("table_id") or "")
            table = by_id.get(identity)
            value_ref = source.get("value_ref")
            name_ref = source.get("entity_ref")
            anchor = (source.get("table_id"), value_ref, name_ref, record.get("assay_endpoint", "IC50"))
            if value_ref and len(claims.get(anchor, set())) > 1:
                add(record_id, identity, "duplicate_measurement_claim", "Several observations claim the same entity/value source cell.", [value_ref, name_ref], error=True, action="reclassify_table")
            if not table:
                add(record_id, identity, "source_table_missing", "The source table is unavailable.")
                continue
            row = source.get("row_index")
            rows = table.get("rows", [])
            if not isinstance(row, int) or not 0 <= row < len(rows):
                add(record_id, identity, "source_row_invalid", "Measurement row is outside the source table.", error=True, action="reclassify_table")
                continue
            if not value_ref or not name_ref:
                texts = [str(c.get("text", "")) for c in rows[row]]
                merged = any(re.search(r"<br\s*/?>|\n", text, re.I) for text in texts)
                add(record_id, identity, "ambiguous_merged_row" if merged else "legacy_cell_refs_missing",
                    "Merged rows cannot prove an entity/value pairing." if merged else "Cell-level entity/value references are required.",
                    [f"{identity}:R{row}C{c}" for c in range(len(texts)) if texts[c]], error=merged, action="reclassify_table")
            else:
                value_source, name_source = evidence.get(value_ref), evidence.get(name_ref)
                if not value_source or not name_source or any(s.get("table_id") != identity or s.get("kind") != "cell" for s in (value_source, name_source) if s):
                    add(record_id, identity, "measurement_refs_invalid", "Entity/value refs must resolve to source-table cells.", [value_ref, name_ref], error=True, action="reclassify_table")
                else:
                    parsed = parse_measurement(value_source["text"])
                    if not parsed:
                        add(record_id, identity, "measurement_not_scalar", "The cited value cell is not one unambiguous measurement.", [value_ref], error=True, action="reclassify_table")
                    else:
                        try:
                            numeric_ok = not isinstance(record.get("ic50"), bool) and math.isclose(parsed[0], float(record.get("ic50")), rel_tol=1e-9, abs_tol=1e-12)
                        except (TypeError, ValueError):
                            numeric_ok = False
                        if not numeric_ok or parsed[1] != (record.get("ic50_operator") or "="):
                            add(record_id, identity, "measurement_value_conflict", "Value or inequality operator differs from the original cell.", [value_ref], error=True, action="reclassify_table")
                    raw_name = record.get("raw_antibody_id") or record.get("antibody_id") or ""
                    if normalize(name_source["text"]) != normalize(raw_name) or re.search(r"<br\s*/?>|\n", name_source["text"], re.I):
                        add(record_id, identity, "entity_source_conflict", "The observed entity name is not a single literal source cell.", [name_ref], error=True, action="reclassify_table")
                    if (template or {}).get("agent", {}).get("mode") == "llm":
                        entities = (template or {}).get("entity_template", {}).get("entities", [])
                        declared = [e for e in entities if e.get("entity_id") == record.get("canonical_entity_key")
                                    and any(normalize(n) == normalize(raw_name) for n in e.get("raw_names", []))]
                        if len(declared) != 1:
                            add(record_id, identity, "entity_plan_conflict", "The observation's construct identity is absent from the explicit entity plan.", [name_ref], error=True)
                    scopes = identified_regions(identity, table.get("table_classification", {}).get("regions", []))
                    scope = next((r for r in scopes if r["region_id"] == source.get("region_id")), None)
                    if not scope or scope.get("execution_status", "ready") != "ready" or scope.get("kind") != "assay":
                        add(record_id, identity, "assay_region_missing", "The measurement has no executable assay-region plan.", [value_ref], action="reclassify_table")
                    else:
                        column = value_source["col_index"]
                        metrics = list(scope.get("metrics", []))
                        groups = scope.get("entity_groups", [])
                        metrics.extend({**m, "value_column": m.get("value_column") if m.get("value_column") is not None else g.get("value_column")}
                                       for g in groups if g.get("name_ref") == name_ref for m in g.get("metrics", []))
                        selected = [m for m in metrics if m.get("endpoint") == record.get("assay_endpoint", "IC50") and m.get("value_column") == column
                                    and (m.get("value_row") is None or m["value_row"] == row)]
                        row_name = scope.get("entity_column") is not None and name_ref == f"{identity}:R{row}C{scope['entity_column']}"
                        group_name = any(g.get("name_ref") == name_ref for g in groups)
                        if not (scope["row_start"] <= row <= scope["row_end"] and selected and (row_name or group_name) and value_source["row_index"] == row):
                            add(record_id, identity, "entity_value_alignment_conflict", "Entity/value cells do not belong to the selected region and metric.", [name_ref, value_ref], error=True, action="reclassify_table")
            headers = source.get("header_refs", [])
            units = source.get("unit_refs", [])
            endpoint = record.get("assay_endpoint", "IC50")
            refs = [*headers, *units]
            if not headers or any(ref not in evidence for ref in refs):
                add(record_id, identity, "metric_evidence_missing", "Endpoint/unit source references are incomplete.", refs, action="reclassify_table")
            else:
                allowed = {identity, *table.get("table_classification", {}).get("allowed_header_table_ids", [])}
                if any(evidence[ref].get("table_id") not in allowed or evidence[ref].get("kind") != "cell" for ref in refs):
                    add(record_id, identity, "metric_source_invalid", "Headers must come from this table or a verified continuation.", refs, error=True, action="reclassify_table")
                if endpoint not in endpoints_in(" ".join(evidence[ref]["text"] for ref in headers)):
                    add(record_id, identity, "endpoint_source_conflict", "The selected endpoint is absent from the cited headers.", headers, error=True, action="reclassify_table")
                source_units = {normalized_unit(u) for ref in units for u in UNITS.findall(evidence[ref]["text"])}
                inline = parse_measurement(evidence.get(value_ref, {}).get("text", ""))
                if inline and inline[2]:
                    source_units.add(normalized_unit(inline[2]))
                if not record.get("ic50_unit") or not source_units:
                    add(record_id, identity, "unit_not_supported", "The measurement unit is missing or lacks source support.", units, action="reclassify_table")
                elif source_units != {normalized_unit(record["ic50_unit"])}:
                    add(record_id, identity, "unit_source_conflict", "The unit differs from the source or the source units are ambiguous.", units, error=True, action="reclassify_table")
            profile = select_assay_profile(template, identity, source.get("region_id"), endpoint,
                regions=table.get("table_classification", {}).get("regions", []), record_id=record_id)
            if profile.get("status") != "matched" or not record.get("assay_protocol_id"):
                add(record_id, identity, "assay_binding_unresolved", "No unique region/endpoint protocol binding is supported.", headers, action="rebind_assay")
            elif profile.get("protocol_id") != record.get("assay_protocol_id"):
                add(record_id, identity, "assay_binding_conflict", "Exported protocol differs from the region binding.", headers, error=True, action="rebind_assay")
            elif any(normalize(profile.get(key)) != normalize(record.get(key)) for key in ("assay_method", "assay_system")):
                add(record_id, identity, "protocol_metadata_conflict", "Exported method/system differs from the selected evidence plan.", headers, error=True, action="rebind_assay")
            protocol_sources = [e for e in record.get("evidence", []) if e.get("type") == "assay_protocol"]
            # Check cached protocol quotations against the current source. Cached
            # quotations alone are never accepted as an independent source.
            protocol_refs = [item.get("evidence_ref") for p in protocol_sources for item in p.get("evidence", []) if item.get("evidence_ref")]
            if not protocol_refs or any(ref not in evidence for ref in protocol_refs):
                add(record_id, identity, "protocol_evidence_missing", "Protocol quotations lack resolvable original evidence.", protocol_refs, action="rebind_assay")
            stale_refs = [item["evidence_ref"] for p in protocol_sources for item in p.get("evidence", [])
                          if item.get("text") and item.get("evidence_ref") in evidence
                          and normalize(item["text"]) != normalize(evidence[item["evidence_ref"]]["text"])]
            if stale_refs:
                add(record_id, identity, "protocol_evidence_stale", "Cached protocol quotations differ from the original source at those refs.", stale_refs, error=True, action="rebind_assay")
            cell_endpoints = set().union(*(endpoints_in(evidence[ref]["text"]) for ref in protocol_refs if ref in evidence and evidence[ref].get("kind") == "cell"))
            if cell_endpoints and endpoint not in cell_endpoints:
                add(record_id, identity, "protocol_endpoint_conflict", "Protocol references point to another endpoint's table region.", protocol_refs, error=True, action="rebind_assay")
            protocol_id = str(record.get("assay_protocol_id") or "")
            section_tokens: set[str] = set()
            method_passages = []
            for ref in protocol_refs:
                original = evidence.get(ref, {})
                if original.get("kind") not in {"method", "context"}:
                    continue
                text = original["text"]
                if original.get("section_id") == protocol_id:
                    section_tokens.update(endpoints_in(text))
                    method_passages.append(text)
                elif re.fullmatch(r"\d+\.\d+", protocol_id):
                    section = re.search(rf"(?<![\d.]){re.escape(protocol_id)}(?![\d.])\s+", text)
                    if section:
                        following = text[section.end():]
                        next_section = re.search(r"(?<![\d.])\d{1,2}\.\d{1,2}(?![\d.])\s+(?=[A-Za-z\u4e00-\u9fff])", following)
                        passage = following[:next_section.start()] if next_section else following
                        section_tokens.update(endpoints_in(passage))
                        method_passages.append(passage)
                else:
                    method_passages.append(text)
            if section_tokens and endpoint not in section_tokens:
                add(record_id, identity, "protocol_section_conflict", "The selected method subsection reports a different endpoint.", protocol_refs, error=True, action="rebind_assay")
            declared_methods = method_kinds(str(record.get("assay_method") or ""))
            supported_methods = method_kinds(" ".join(method_passages))
            if declared_methods and supported_methods and not declared_methods <= supported_methods:
                add(record_id, identity, "protocol_method_conflict", "The selected protocol passage describes a different assay method.", protocol_refs, error=True, action="rebind_assay")
            condition_refs = source.get("condition_refs", [])
            if any(ref not in evidence or evidence[ref].get("table_id") != identity or evidence[ref].get("row_index") != row for ref in condition_refs):
                add(record_id, identity, "condition_refs_invalid", "Assay conditions must cite cells from the measurement row.", condition_refs, error=True, action="reclassify_table")
            else:
                condition_methods = method_kinds(" ".join(evidence[ref]["text"] for ref in condition_refs))
                if declared_methods and condition_methods and not condition_methods <= declared_methods:
                    add(record_id, identity, "condition_method_conflict", "This region combines rows with different assay methods; split its plan.", condition_refs, error=True, action="reclassify_table")
        entity_id = str(record.get("canonical_entity_key") or record.get("antibody_id") or "")
        sequence_entity = sequence_entities.get(entity_id, {})
        sequence_states = {}
        for field, chain_check in sequence_entity.get("chains", {}).items():
            state = chain_check["status"]
            sequence_states[field] = state if not record.get(field) or state in {"mapping_conflict", "scope_conflict"} else (
                "source_conflict" if record_id in chain_check["conflicting_record_ids"] else "supported"
                if chain_check["seq_ids"] or chain_check.get("source_validated") else "source_unverified")
            if state == "link_missing" and record_id not in chain_check["missing_record_ids"]:
                continue
            if state == "source_conflict" and record_id not in chain_check["conflicting_record_ids"]:
                continue
            messages = {"link_missing": "原文已有明确的抗体—SEQ 映射和序列，但该记录尚未关联。",
                        "definition_missing": "已找到 SEQ 索引，但尚未解析到对应的氨基酸序列定义。",
                        "definition_invalid": "SEQ 定义不完整，或序列字符、长度需要核对。",
                        "mapping_unresolved": "原文存在该抗体的序列线索，但链与 SEQ 的对应关系尚未确认。",
                        "mapping_conflict": "同一抗体的同一条链对应多个 SEQ 编号，需要核对变体或别名。",
                        "scope_conflict": "SEQ 的链类型或范围不匹配，恒定区/CDR 不能作为 VH/VL。",
                        "source_unverified": "记录已有序列，但尚无可核验的抗体—SEQ 来源映射。",
                        "source_conflict": "记录中的序列与原文支持的序列不一致。"}
            if state not in messages:
                continue
            repairable = any(c["entity_id"] == entity_id and c["chain"].lower() == field for c in sequence_coverage["repair_candidates"])
            add(record_id, str(record.get("assay_table_id") or ""), "sequence_" + state, messages[state],
                chain_check["evidence_refs"], error=state in {"mapping_conflict", "scope_conflict", "source_conflict"},
                action="relink_sequences" if repairable and state == "link_missing" else None, entity_id=entity_id, field=field)
        record["sequence_coverage_status"] = "complete" if record.get("vh") and record.get("vl") and all(
            value == "supported" for value in sequence_states.values()) else "review_required"
        local = findings[start:]
        status = "error" if any(f["severity"] == "error" for f in local) else "manual_review" if local else "passed"
        field_states = {field: ("present" if record.get(field) not in (None, "", []) else "not_found")
                        for field in ("antibody_id", "vh", "vl", "ic50", "ic50_unit", "assay_protocol_id")}
        field_states.update(sequence_states)
        conflict_fields = {"measurement_value_conflict": "ic50", "measurement_not_scalar": "ic50",
            "unit_source_conflict": "ic50_unit", "entity_source_conflict": "antibody_id", "entity_plan_conflict": "antibody_id",
            "assay_binding_conflict": "assay_protocol_id", "protocol_endpoint_conflict": "assay_protocol_id",
            "protocol_section_conflict": "assay_protocol_id", "protocol_metadata_conflict": "assay_protocol_id",
            "protocol_method_conflict": "assay_protocol_id",
            "condition_method_conflict": "assay_protocol_id",
            "protocol_evidence_stale": "assay_protocol_id"}
        for finding in local:
            if finding["code"] in conflict_fields:
                field_states[conflict_fields[finding["code"]]] = "conflict"
        for chain in ("vh", "vl"):
            if record.get(chain) and any("unavailable" in str(record.get(f"{tool}_review", {})).lower() for tool in ("anarci", "esm2")):
                field_states[f"{chain}_biological_check"] = "runtime_unavailable"
        record["record_verification_status"] = status
        record["record_verification"] = {"version": 1, "status": status, "finding_ids": [f["finding_id"] for f in local],
            "findings": [{key: f[key] for key in ("code", "message", "evidence_refs")} for f in local], "field_states": field_states}
        if status != "passed":
            record["link_status"] = "review"
        validations.append({"record_id": record_id, **record["record_verification"]})

    # Coverage checks are distinct from record validation. Only explicit IC50
    # evidence creates a task; KD-only tables are outside the current export.
    represented: dict[str, set[str]] = {}
    for record in output:
        for source in record.get("evidence", []):
            if source.get("type") == "assay_measurement":
                represented.setdefault(str(source.get("table_id")), set()).add(str(source.get("value_ref")))
    for identity, table in by_id.items():
        scopes = identified_regions(identity, table.get("table_classification", {}).get("regions", []))
        expected, unparsed = set(), set()
        for scope in scopes:
            if scope.get("kind") != "assay" or "IC50" not in region_endpoints(scope):
                continue
            metrics = [*scope.get("metrics", []), *({**m, "value_column": m.get("value_column") if m.get("value_column") is not None else g.get("value_column")}
                        for g in scope.get("entity_groups", []) for m in g.get("metrics", []))]
            for row in range(scope["row_start"], scope["row_end"] + 1):
                for metric in metrics:
                    col = metric.get("value_column")
                    actual_row = metric.get("value_row") if metric.get("value_row") is not None else row
                    ref = f"{identity}:R{actual_row}C{col}"
                    if metric.get("endpoint") == "IC50" and parse_measurement(evidence.get(ref, {}).get("text", "")):
                        expected.add(ref)
                    elif metric.get("endpoint") == "IC50" and re.search(r"\d", evidence.get(ref, {}).get("text", "")):
                        unparsed.add(ref)
        # A second coverage route reads literal IC50 headers independently of
        # the model's regions. A bad reclassification cannot hide a gap merely
        # by pointing its metric at blank cells or excluding the assay region.
        for header_row, cells in enumerate(table.get("rows", [])):
            for column, cell in enumerate(cells):
                header_text = str(cell.get("text", ""))
                if "IC50" not in endpoints_in(header_text) or re.search(r"ratio|比值|比率", header_text, re.I):
                    continue
                for row in range(header_row + 1, len(table.get("rows", []))):
                    values = table["rows"][row]
                    row_text = " ".join(str(c.get("text", "")) for c in values)
                    row_endpoints = endpoints_in(row_text)
                    if row_endpoints and "IC50" not in row_endpoints:
                        break
                    if column < len(values):
                        ref = f"{identity}:R{row}C{column}"
                        if parse_measurement(str(values[column].get("text", ""))):
                            expected.add(ref)
        missing = expected - represented.get(identity, set())
        text = " ".join(str(c.get("text", "")) for row in table.get("rows", []) for c in row)
        if unparsed:
            add(None, identity, "ic50_cells_unparsed", "IC50 candidate cells contain unparsed or merged measurements.", sorted(unparsed), action="reclassify_table")
        if missing or ("IC50" in endpoints_in(text) and identity not in represented and not any("IC50" in region_endpoints(s) for s in scopes)
                       and any(parse_measurement(part) for row in table.get("rows", []) for c in row
                               for part in re.split(r"<br\s*/?>|\n", str(c.get("text", "")), flags=re.I))):
            add(None, identity, "ic50_coverage_gap", "Explicit IC50 candidate cells have no exported observation; inspect layout/entity routing.", sorted(missing) or [identity], action="reclassify_table")
    counts = Counter(v["status"] for v in validations)
    report = {"schema_version": 2, "scope": "consistency_with_supplied_evidence", "records": validations,
              "sequence_coverage": sequence_coverage,
              "findings": findings, "summary": {"record_count": len(output), **{s: counts[s] for s in ("passed", "error", "manual_review")},
              "finding_count": len(findings), "coverage_gap_count": sum(f["code"] == "ic50_coverage_gap" for f in findings),
              "sequence_gap_count": sequence_coverage["summary"]["source_backed_gap_count"],
              "missing_sequence_chain_count": sequence_coverage["summary"]["missing_chain_count"]}}
    return output, report


# Measurement export

def _numeric(source: dict):
    text = source.get("numeric_text") or re.sub(r"<br\s*/?>", " ", source.get("text", ""))
    return parse_measurement(text)


def _rounding_interval(source: dict):
    text = (source.get("numeric_text") or source.get("text", "")).strip()
    try:
        value = Decimal(text)
        delta = Decimal(5).scaleb(value.as_tuple().exponent - 1)
        return value - delta, value + delta
    except InvalidOperation:
        return None


def export_measurements(document_id: str, tables: list[dict], classifications: list[dict],
                        template: dict | None = None, legacy_records: list[dict] | None = None) -> dict:
    by_class = {c["table_id"]: c for c in classifications}
    tables = [semantic_table({**t, "table_classification": by_class.get(table_id(t), {})}) for t in tables]
    evidence = build_evidence(tables)
    measurements, comparisons, issues = [], [], []
    seen, ratio_seen = {}, {}
    legacy = {(e.get("value_ref"), e.get("table_id")): r for r in legacy_records or []
              for e in r.get("evidence", []) if e.get("type") == "assay_measurement"}

    def issue(identity, ref, reason):
        issues.append({"table_id": identity, "source_ref": ref, "reason": reason})

    for table in tables:
        identity = table_id(table)
        classification = by_class.get(identity, {})
        if classification.get("classifier", {}).get("mode") != "llm" or classification.get("decision") == "exclude":
            continue
        regions = identified_regions(identity, classification.get("regions", []))
        for region in regions:
            if region.get("kind") != "assay" or region.get("execution_status", "ready") != "ready":
                continue
            for row in range(region["row_start"], region["row_end"] + 1):
                condition_refs = [f"{identity}:R{row}C{c}" for c in region.get("condition_columns", [])]
                condition = " · ".join(evidence[ref]["text"].replace("<br>", " ") for ref in condition_refs if ref in evidence)
                groups = list(region.get("entity_groups", []))
                if region.get("entity_column") is not None:
                    ref = f"{identity}:R{row}C{region['entity_column']}"
                    groups.append({"name_ref": ref, "raw_name": evidence.get(ref, {}).get("text", ""), "metrics": region.get("metrics", [])})
                for group in groups:
                    name_ref = group["name_ref"]
                    if not group["raw_name"] or normalize(group["raw_name"]) not in normalize(evidence.get(name_ref, {}).get("text", "")):
                        issue(identity, name_ref, "entity_evidence_missing")
                        continue
                    for metric in group["metrics"]:
                        if metric.get("value_row") is not None and row != region["row_start"]:
                            continue
                        value_row = row if metric.get("value_row") is None else metric["value_row"]
                        col = metric.get("value_column")
                        col = group.get("value_column") if col is None else col
                        ref = f"{identity}:R{value_row}C{col}"
                        source = evidence.get(ref, {})
                        parsed = _numeric(source)
                        if set(source.get("item_indices", [])) & set(table.get("token_audit", {}).get("duplicate_indices", [])):
                            issue(identity, ref, "duplicate_token_assignment")
                            continue
                        headers = " ".join(evidence.get(r, {}).get("text", "") for r in metric["header_refs"])
                        if metric["endpoint"] not in endpoints_in(headers) or col in region.get("excluded_columns", []):
                            issue(identity, ref, "endpoint_or_column_conflict")
                            continue
                        if not parsed:
                            if re.search(r"\d", source.get("text", "")):
                                issue(identity, ref, "numeric_text_needs_review")
                            continue
                        value, operator, inline_unit = parsed
                        unit = metric.get("unit")
                        unit_text = " ".join(evidence.get(r, {}).get("text", "") for r in metric["unit_refs"])
                        if (unit and normalized_unit(unit) not in normalized_unit(unit_text)) or (inline_unit and normalized_unit(inline_unit) != normalized_unit(unit or "")):
                            issue(identity, ref, "unit_evidence_conflict")
                            continue
                        key = (ref, metric["endpoint"])
                        if key in seen:
                            if seen[key]["entity_ref"] != name_ref:
                                seen[key]["status"] = "needs_review"
                                issue(identity, ref, "multiple_entity_claims")
                            continue
                        profile = select_assay_profile(template, identity, region["region_id"], metric["endpoint"], regions=regions)
                        old = legacy.get((ref, identity), {})
                        item = {"measurement_id": "measurement-" + hashlib.sha256(f"{document_id}|{ref}|{metric['endpoint']}|{name_ref}".encode()).hexdigest()[:24],
                                "document_id": document_id, "antibody_id": group["raw_name"], "entity_ref": name_ref,
                                "metric": metric["endpoint"], "value": value, "operator": operator, "unit": unit,
                                "assay_condition": condition, "assay_method": profile.get("assay_method"),
                                "species": profile.get("species", []), "protocol_status": profile.get("status", "unmatched"),
                                "vh": old.get("vh", ""), "vl": old.get("vl", ""), "legacy_record_id": old.get("record_id"),
                                "source_table": identity, "page": table.get("page"), "region_id": region["region_id"],
                                "value_ref": ref, "raw_text": source.get("text", ""), "numeric_text": source.get("numeric_text"),
                                "bbox": source.get("bbox", []), "item_indices": source.get("item_indices", []),
                                "header_refs": metric["header_refs"], "unit_refs": metric["unit_refs"], "condition_refs": condition_refs,
                                "status": "source_checked"}
                        seen[key] = item
                        measurements.append(item)
                for comparison in region.get("comparisons", []):
                    ref = f"{identity}:R{row}C{comparison['value_column']}"
                    parsed = _numeric(evidence.get(ref, {}))
                    if ref in ratio_seen:
                        if ratio_seen[ref] != comparison:
                            issue(identity, ref, "comparison_plan_conflict")
                            for existing in comparisons:
                                if existing["value_ref"] == ref:
                                    existing["check"] = "conflicting_plan"
                        continue
                    ratio_seen[ref] = comparison
                    if not parsed or parsed[1] != "=" or parsed[2]:
                        issue(identity, ref, "comparison_value_needs_review")
                        continue
                    comparisons.append({"document_id": document_id, "source_table": identity, "row": row,
                                        "metric": comparison["endpoint"], "value": parsed[0], "unit": None,
                                        "numerator_ref": comparison["numerator_ref"], "denominator_ref": comparison["denominator_ref"],
                                        "numerator": evidence[comparison["numerator_ref"]]["text"],
                                        "denominator": evidence[comparison["denominator_ref"]]["text"],
                                        "value_ref": ref, "raw_text": evidence[ref]["text"], "header_refs": comparison["header_refs"],
                                        "assay_condition": condition, "check": "unresolved"})
    for item in comparisons:
        if item["check"] == "conflicting_plan":
            continue
        linked = []
        for field in ("numerator_ref", "denominator_ref"):
            candidates = [m for m in measurements if m["source_table"] == item["source_table"] and m["entity_ref"] == item[field]
                          and m["metric"] == item["metric"] and m["value_ref"].startswith(f"{item['source_table']}:R{item['row']}C")
                          and m["operator"] == "=" and m["status"] == "source_checked"]
            linked.append(candidates[0] if len(candidates) == 1 else None)
        numerator, denominator = linked
        if numerator and denominator and denominator["value"] and normalized_unit(numerator["unit"] or "") == normalized_unit(denominator["unit"] or ""):
            item["measurement_refs"] = [numerator["measurement_id"], denominator["measurement_id"]]
            item["computed_ratio"] = numerator["value"] / denominator["value"]
            a, b, c = (_rounding_interval(evidence[ref]) for ref in (numerator["value_ref"], denominator["value_ref"], item["value_ref"]))
            if a and b and c and a[0] >= 0 and b[0] > 0:
                item["check"] = "consistent_with_rounding" if a[0] / b[1] <= c[1] and a[1] / b[0] >= c[0] else "mismatch"
    return {"measurements": measurements, "comparisons": comparisons, "issues": issues,
            "summary": {"measurement_count": len(measurements), "comparison_count": len(comparisons), "issue_count": len(issues)}}


# Prediction evaluation

FIELDS = ("ic50", "ic50_unit", "ic50_operator", "vh", "vl", "assay_method", "assay_system", "assay_condition", "species", "assay_protocol_id")
SCALES = {"M": ("molar", 1.0), "mM": ("molar", 1e-3), "uM": ("molar", 1e-6),
          "nM": ("molar", 1e-9), "pM": ("molar", 1e-12),
          "mg/mL": ("mass", 1.0), "ug/mL": ("mass", 1e-3)}


def _text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def _quantity(record: dict) -> tuple[str, float] | None:
    if isinstance(record.get("ic50"), bool):
        return None
    scale = SCALES.get(normalized_unit(record.get("ic50_unit", "")))
    try:
        value = float(record.get("ic50"))
    except (ValueError, TypeError):
        return None
    return (scale[0], value * scale[1]) if scale and math.isfinite(value) else None


def _equal(field: str, gold: dict, pred: dict, rtol: float) -> bool:
    left, right = gold.get(field), pred.get(field)
    if field == "ic50":
        a, b = _quantity(gold), _quantity(pred)
        return bool(a and b and a[0] == b[0] and math.isclose(a[1], b[1], rel_tol=rtol, abs_tol=0.0))
    if field == "ic50_unit":
        a, b = _quantity(gold), _quantity(pred)
        return bool(a and b and a[0] == b[0])
    if field == "ic50_operator":
        return (left or "=") == (right or "=")
    if field in {"vh", "vl"}:
        return re.sub(r"\s+", "", str(left or "")).upper() == re.sub(r"\s+", "", str(right or "")).upper()
    if isinstance(left, list):
        return isinstance(right, list) and {_text(x) for x in left} == {_text(x) for x in right}
    return _text(left) == _text(right)


def maximum_assignment(weights: list[list[int]]) -> list[tuple[int, int]]:
    """Rectangular Hungarian matching with one unmatched dummy per gold row."""
    if not weights:
        return []
    n, real_columns = len(weights), len(weights[0])
    m = real_columns + n
    matrix = [row + [0] * n for row in weights]
    u, v, p, way = [0] * (n + 1), [0] * (m + 1), [0] * (m + 1), [0] * (m + 1)
    for i in range(1, n + 1):
        p[0], j0 = i, 0
        minimum, used = [float("inf")] * (m + 1), [False] * (m + 1)
        while True:
            used[j0] = True
            i0, delta, j1 = p[j0], float("inf"), 0
            for j in range(1, m + 1):
                if used[j]:
                    continue
                cost = -matrix[i0 - 1][j - 1] - u[i0] - v[j]
                if cost < minimum[j]:
                    minimum[j], way[j] = cost, j0
                if minimum[j] < delta:
                    delta, j1 = minimum[j], j
            for j in range(m + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minimum[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while j0:
            previous = way[j0]
            p[j0] = p[previous]
            j0 = previous
    return [(p[j] - 1, j - 1) for j in range(1, real_columns + 1) if p[j] and weights[p[j] - 1][j - 1] > 0]


def evaluate_observations(gold: list[dict], predictions: list[dict], *, rtol: float = 1e-3) -> dict:
    if not 0 <= rtol < 1:
        raise ValueError("rtol must be in [0, 1)")
    for record in [*gold, *predictions]:
        if not record.get("document_id") or not record.get("antibody_id"):
            raise ValueError("Every observation needs document_id and antibody_id")
    for record in gold:
        if record.get("ic50") is None or not record.get("ic50_unit"):
            raise ValueError("Gold observations need an explicitly annotated IC50 and unit")
        if _quantity(record) is None:
            raise ValueError("Gold IC50 must be finite and use a supported concentration unit")
        if not isinstance(record.get("match_on", []), list) or any(key not in record for key in record.get("match_on", [])):
            raise ValueError("match_on must list explicitly annotated gold fields")
    gold = [{**g, "ic50_operator": g.get("ic50_operator") or "="} for g in gold]
    scored_fields = [[field for field in FIELDS if field in g and g[field] is not None] for g in gold]

    def eligible(g: dict, p: dict) -> bool:
        aliases = {_text(g["antibody_id"]), *(_text(a) for a in g.get("aliases", []))}
        if g["document_id"] != p["document_id"] or _text(p["antibody_id"]) not in aliases:
            return False
        if g.get("assay_endpoint", "IC50") != p.get("assay_endpoint", "IC50"):
            return False
        # Optional independently annotated anchors prevent swapping two assays
        # of the same antibody just because their numeric values match better.
        return all(g[key] == p.get(key) for key in g.get("match_on", []) if key in g)

    weights = [[(1000 + sum(_equal(f, g, p, rtol) for f in scored_fields[i])) if eligible(g, p) else -100000
                for p in predictions] for i, g in enumerate(gold)]
    aligned = maximum_assignment(weights)
    matched = dict(aligned)
    fields: dict[str, Counter] = {}
    errors = []
    true_positives = 0
    for i, g in enumerate(gold):
        p = predictions[matched[i]] if i in matched else None
        wrong = []
        for field in scored_fields[i]:
            correct = p is not None and _equal(field, g, p, rtol)
            fields.setdefault(field, Counter())["total"] += 1
            fields[field]["correct"] += int(correct)
            if not correct:
                wrong.append(field)
        true_positives += int(p is not None and not wrong)
        if p is None or wrong:
            errors.append({"gold_index": i, "prediction_index": matched.get(i), "document_id": g["document_id"],
                           "antibody_id": g["antibody_id"], "mismatched_fields": wrong, "missing": p is None})
    precision = true_positives / len(predictions) if predictions else 0.0
    recall = true_positives / len(gold) if gold else 0.0
    return {"schema_version": 1, "alignment": "maximum_weight_entity_endpoint_matching",
            "rtol": rtol, "gold_count": len(gold), "prediction_count": len(predictions), "aligned_count": len(aligned),
            "strict_observation": {"true_positive": true_positives, "false_positive": len(predictions) - true_positives,
                "false_negative": len(gold) - true_positives, "precision": precision, "recall": recall,
                "f1": 2 * precision * recall / (precision + recall) if precision + recall else 0.0},
            "fields": {field: {**count, "accuracy": count["correct"] / count["total"]} for field, count in fields.items()},
            "errors": errors, "extra_prediction_indices": sorted(set(range(len(predictions))) - {j for _, j in aligned})}


# Bounded repair planning

def plan_repairs(report: dict, history: list[dict], *, enabled: bool, rounds: int,
                 max_rounds: int, actions_used: int, max_actions: int, stopped: bool = False) -> dict:
    if not report.get("findings"):
        return {"tasks": [], "stop_reason": "verified"}
    if stopped:
        return {"tasks": [], "stop_reason": "previous_attempt_stopped"}
    if not enabled:
        return {"tasks": [], "stop_reason": "automatic_repair_disabled"}
    if rounds >= max_rounds or actions_used >= max_actions:
        return {"tasks": [], "stop_reason": "budget_exhausted"}
    groups: dict[str, list[dict]] = {}
    for finding in report.get("findings", []):
        if finding.get("repair_action") == "relink_sequences" and finding.get("entity_id"):
            groups.setdefault("sequence:" + finding["entity_id"], []).append(finding)
            continue
        if finding.get("repair_action") and finding.get("table_id"):
            groups.setdefault(finding["table_id"], []).append(finding)
    attempted = {item.get("task_fingerprint") for item in history}
    tasks = []
    for identity, findings in groups.items():
        action = "relink_sequences" if identity.startswith("sequence:") else "reclassify_table" if any(f["repair_action"] == "reclassify_table" for f in findings) else "rebind_assay"
        ids = sorted(f["finding_id"] for f in findings)
        fingerprint = hashlib.sha256(json.dumps([identity, action, ids]).encode()).hexdigest()[:24]
        # A smaller set of the same findings is not new evidence. Avoid spending
        # another round repeating an action that already considered these refs.
        def signature(finding: dict) -> tuple:
            return (finding.get("code"), tuple(sorted(finding.get("evidence_refs", []))))

        current = {signature(f) for f in findings}
        repeated = any(
            item.get("action") == action
            and (item.get("entity_id") == findings[0].get("entity_id") if action == "relink_sequences"
                 else item.get("table_id") == identity)
            and current <= {signature(f) for f in item.get("findings", [])}
            for item in history
        )
        if repeated:
            continue
        if fingerprint not in attempted:
            tasks.append({"table_id": findings[0]["table_id"] if action == "relink_sequences" else identity,
                          **({"entity_id": findings[0]["entity_id"]} if action == "relink_sequences" else {}),
                          "action": action, "finding_ids": ids,
                          "task_fingerprint": fingerprint, "findings": findings})
    tasks.sort(key=lambda t: (t["action"] != "relink_sequences", -sum(f["severity"] == "error" for f in t["findings"]), t["table_id"]))
    # One staged table action per graph round keeps budgets and attribution clear.
    return {"tasks": tasks[:1], "stop_reason": None if tasks else "no_new_action"}


def _observation(record: dict) -> tuple:
    return (record.get("document_id"), record.get("raw_antibody_id") or record.get("antibody_id"),
            record.get("ic50"), record.get("ic50_operator", "="), record.get("ic50_unit"),
            record.get("assay_protocol_id"), record.get("measurement_id"), record.get("assay_condition"),
            record.get("assay_method"), record.get("assay_system"), tuple(sorted(record.get("species") or [])),
            record.get("vh"), record.get("vl"))


def assess_candidate(before: list[dict], after: list[dict], old_report: dict, new_report: dict, target_ids: set[str]) -> dict:
    def counts(report: dict) -> tuple[int, int]:
        findings = report.get("findings", [])
        return sum(f["severity"] == "error" for f in findings), len(findings)

    def sources(record: dict) -> set[str]:
        return {str(e.get("table_id")) for e in record.get("evidence", []) if e.get("type") == "assay_measurement"}

    def multiset(records: list[dict], protected: bool) -> Counter:
        return Counter(_observation(r) for r in records if not sources(r) & target_ids or (protected and r.get("record_verification_status") == "passed"))

    def anchors(records: list[dict]) -> set[tuple]:
        return {(str(e.get("table_id")), str(e["value_ref"])) for r in records for e in r.get("evidence", [])
                if e.get("type") == "assay_measurement" and e.get("value_ref")}

    protected_before = multiset(before, True)
    available_after = Counter(_observation(r) for r in after)
    previously_passed = Counter(_observation(r) for r in before if r.get("record_verification_status") == "passed")
    still_passed = Counter(_observation(r) for r in after if r.get("record_verification_status") == "passed")
    reason = None
    if protected_before - available_after:
        reason = "protected_observation_changed"
    elif previously_passed - still_passed:
        reason = "protected_validation_regressed"
    elif multiset(before, False) != multiset(after, False):
        reason = "unrelated_observation_changed"
    elif len(after) < len(before):
        reason = "observation_loss"
    elif anchors(before) - anchors(after):
        reason = "measurement_source_loss"
    elif counts(new_report)[0] > counts(old_report)[0]:
        reason = "new_source_conflicts"
    elif counts(new_report)[1] >= counts(old_report)[1]:
        reason = "no_progress"
    old_unrelated = {f["finding_id"] for f in old_report.get("findings", []) if f.get("table_id") not in target_ids}
    new_unrelated = {f["finding_id"] for f in new_report.get("findings", []) if f.get("table_id") not in target_ids}
    if new_unrelated - old_unrelated:
        reason = "unrelated_validation_regressed"
    def source_errors(report: dict) -> set[tuple]:
        return {(f.get("table_id"), f["code"], tuple(f.get("evidence_refs", [])))
                for f in report.get("findings", []) if f["severity"] == "error"}
    if source_errors(new_report) - source_errors(old_report):
        reason = "new_source_conflicts"
    return {"accepted": reason is None, "reason": reason or "fewer_findings_without_regression",
            "before": {"record_count": len(before), "errors": counts(old_report)[0], "findings": counts(old_report)[1]},
            "after": {"record_count": len(after), "errors": counts(new_report)[0], "findings": counts(new_report)[1]}}


def assess_sequence_candidate(before: list[dict], after: list[dict], old_report: dict, new_report: dict, entity_id: str) -> dict:
    """Accept only fills of missing chains, with immutable assays and no regression."""
    old = {r["record_id"]: r for r in before}
    new = {r["record_id"]: r for r in after}
    reason = None
    permitted = {"vh", "vl", "sequence_table_id", "link_status", "construct_validation_status",
                 "agent_review", "anarci_review", "esm2_review", "agent_review_status", "anarci_review_status", "esm2_review_status",
                 "record_verification", "record_verification_status", "sequence_coverage_status", "evidence"}
    sequence_types = {"sequence_index", "sequence_support", "construct_validation"}
    for record_id in old.keys() & new.keys():
        previous, candidate = old[record_id], new[record_id]
        target = str(previous.get("canonical_entity_key") or previous.get("antibody_id") or "") == entity_id
        if {k: v for k, v in previous.items() if k not in permitted} != {k: v for k, v in candidate.items() if k not in permitted}:
            reason = "scientific_observation_changed"
        if [e for e in previous.get("evidence", []) if e.get("type") not in sequence_types] != [e for e in candidate.get("evidence", []) if e.get("type") not in sequence_types]:
            reason = "measurement_source_changed"
        if not target and [e for e in previous.get("evidence", []) if e.get("type") in sequence_types] != [e for e in candidate.get("evidence", []) if e.get("type") in sequence_types]:
            reason = "unrelated_sequence_evidence_changed"
        for field in ("vh", "vl"):
            if previous.get(field) and previous[field] != candidate.get(field):
                reason = "existing_sequence_changed"
            if not target and previous.get(field) != candidate.get(field):
                reason = "unrelated_sequence_changed"
        if previous.get("record_verification_status") == "passed" and candidate.get("record_verification_status") != "passed":
            reason = "protected_validation_regressed"
    old_findings = {f["finding_id"] for f in old_report.get("findings", [])}
    new_findings = {f["finding_id"] for f in new_report.get("findings", [])}
    if new_findings - old_findings:
        reason = "new_validation_findings"
    if len(new_findings) >= len(old_findings):
        reason = reason or "no_progress"
    if len(old) != len(before) or len(new) != len(after) or old.keys() != new.keys():
        reason = "observation_identity_changed"
    return {"accepted": reason is None, "reason": reason or "source_backed_missing_chains_filled",
            "before": {"record_count": len(before), "findings": len(old_findings)},
            "after": {"record_count": len(after), "findings": len(new_findings)}}
