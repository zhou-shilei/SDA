"""Structured LLM contracts, evidence references, and bounded input payloads."""

from __future__ import annotations

import hashlib
import html
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from document.cell_geometry import geometry_fields

# Structured output contracts

class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


Label = Literal[
    "assay_ic50", "assay_ec50", "binding_kd", "sequence_mapping",
    "sequence_definition", "construct_variant", "formulation",
    "other_biological_table", "non_target_table", "not_a_table", "ambiguous",
]
Endpoint = Literal["IC50", "IC90", "EC50", "EC90", "KD", "kon", "koff", "other"]


class Finding(Contract):
    code: str
    message: str
    evidence_refs: list[str]


class MetricPlan(Contract):
    endpoint: Endpoint
    value_column: int | None
    value_row: int | None
    header_refs: list[str]
    unit: str | None
    unit_refs: list[str]


class EntityGroup(Contract):
    raw_name: str
    name_ref: str
    value_column: int | None
    metrics: list[MetricPlan]


class ComparisonPlan(Contract):
    endpoint: Endpoint
    value_column: int
    header_refs: list[str]
    numerator_ref: str
    denominator_ref: str


class HeaderSpan(Contract):
    row: int
    column: int
    row_end: int
    column_end: int
    name_ref: str


class NumericJoin(Contract):
    row: int
    column: int
    token_indices: list[int]


class TokenPlacement(Contract):
    token_index: int
    row: int
    column: int


class TableStructurePlan(Contract):
    table_id: str
    placements: list[TokenPlacement]
    header_spans: list[HeaderSpan]
    numeric_joins: list[NumericJoin]
    unresolved: list[Finding]


class TableRegion(Contract):
    kind: Literal["assay", "sequence_mapping", "sequence_definition", "other"]
    row_start: int
    row_end: int
    header_rows: list[int]
    entity_column: int | None
    condition_columns: list[int]
    entity_groups: list[EntityGroup]
    metrics: list[MetricPlan]
    excluded_columns: list[int]
    sequence_id_column: int | None
    sequence_column: int | None
    chain_column: int | None
    name_column: int | None
    sequence_scope: Literal["variable_heavy", "variable_light", "full_heavy_chain", "full_light_chain", "constant_region", "cdr", "unknown"]
    comparisons: list[ComparisonPlan] = Field(default_factory=list)


class TableDecision(Contract):
    table_id: str
    primary_label: Label
    labels: list[Label] = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)
    entity_axis: Literal["rows", "columns", "mixed", "unknown"]
    measurement_axis: Literal["rows", "columns", "mixed", "unknown"]
    decision: Literal["extract", "context_only", "exclude", "needs_review"]
    detected_endpoints: list[Endpoint]
    evidence_refs: list[str] = Field(min_length=1)
    explanation: str
    regions: list[TableRegion]
    issues: list[Finding]


class ClassificationBatch(Contract):
    tables: list[TableDecision]


class TableRoute(Contract):
    table_id: str
    route: Literal["detail", "not_a_table", "non_target_table", "uncertain"]
    evidence_refs: list[str] = Field(min_length=1)
    explanation: str


class TableRoutingBatch(Contract):
    tables: list[TableRoute]


class DocumentEntity(Contract):
    entity_id: str
    canonical_name: str
    raw_names: list[str] = Field(min_length=1)
    role: Literal["antibody", "control", "unknown"]
    parent_entity_id: str | None
    variant: str | None
    evidence_refs: list[str] = Field(min_length=1)
    alias_evidence_refs: list[str]


class SequenceLink(Contract):
    entity_id: str
    chain: Literal["VH", "VL"]
    seq_id: int = Field(gt=0)
    scope: Literal["variable", "full_chain"]
    evidence_refs: list[str] = Field(min_length=1)


class AssayProfile(Contract):
    table_id: str
    region_id: str | None = None
    protocol_id: str = Field(min_length=1)
    label: str
    method: str | None
    system: str | None
    species: list[str]
    endpoints: list[Endpoint] = Field(min_length=1)
    summary_zh: str
    summary_en: str
    evidence_refs: list[str] = Field(min_length=1)


class DocumentPlan(Contract):
    document_id: str
    entities: list[DocumentEntity]
    sequence_links: list[SequenceLink]
    assay_profiles: list[AssayProfile]
    unresolved: list[Finding]


class AssayBindingPlan(Contract):
    assay_profiles: list[AssayProfile]
    unresolved: list[Finding]


class ConnectivityResult(Contract):
    status: Literal["ok"]


class SourceQuote(Contract):
    source_ref: str
    quote: str = Field(min_length=1)
    occurrence: int = Field(ge=0, description="Zero-based occurrence of the exact quote in this source")


class SequenceSourceConflict(Contract):
    table_ref: str
    text_ref: str
    table_quote: str = Field(min_length=1)
    text_quote: str = Field(min_length=1)
    message: str = Field(min_length=1)


class SequenceDecision(Contract):
    entity_id: str
    chain: Literal["VH", "VL"]
    status: Literal["resolved", "unresolved", "ambiguous"]
    value: str | None
    seq_id: int | None = Field(gt=0)
    source_scope: Literal["variable", "full_chain", "unknown"]
    fragments: list[SourceQuote]
    evidence_refs: list[str]
    rationale: str = Field(min_length=1)
    source_conflicts: list[SequenceSourceConflict] = Field(default_factory=list)


class SequenceExtractionStep(Contract):
    document_id: str
    request_refs: list[str]
    search_queries: list[str]
    complete: bool
    decisions: list[SequenceDecision]


class VariableRegionIdentityDecision(Contract):
    entity_id: str
    parent_entity_id: str
    chain: Literal["VH", "VL"]
    verdict: Literal["same", "different", "uncertain"]
    basis: Literal["isotype_format", "shared_variable_region", "different_variable_region", "insufficient_evidence"]
    evidence: list[SourceQuote]
    rationale: str = Field(min_length=1)


class VariableRegionIdentityBatch(Contract):
    document_id: str
    decisions: list[VariableRegionIdentityDecision]


# Evidence references and model inputs

def table_id(table: dict[str, Any]) -> str:
    return str(table.get("source_id") or table.get("image") or "unknown")


def cell_ref(identity: str, row: int, column: int) -> str:
    return f"{identity}:R{row}C{column}"


def _entity_text(value: Any) -> str:
    return re.sub(r"<br\s*/?>", " ", html.unescape(str(value or "")), flags=re.I)


def normalize(value: Any) -> str:
    return re.sub(r"\s+", "", _entity_text(value)).casefold()


def metric_endpoints(value: Any) -> set[str]:
    """Read explicit endpoint typography without altering the source OCR."""
    text = _entity_text(value).translate(str.maketrans("₀₅₉", "059"))
    text = re.sub(r"</?(?:sub|sup)\b[^>]*>", "", text, flags=re.I)
    text = re.sub(r"\\(?:mathrm|text|operatorname|mathsf)\s*\{([^{}]*)\}", r"\1", text)
    text = re.sub(r"[$_{}]", "", text)
    values = {a.upper() + n for a, n in re.findall(r"(IC|EC)\s*(50|90)(?!\d)", text, re.I)}
    rate = bool(re.search(r"1\s*/\s*s|/\s*s|s\s*\^?\s*-1", text, re.I))
    if re.search(r"(?<![A-Za-z0-9])K\s*[dD](?![A-Za-z0-9])", text) and not rate:
        values.add("KD")
    if re.search(r"\bkon\b|(?<![A-Za-z0-9])k\s*a(?![A-Za-z0-9])", text, re.I):
        values.add("kon")
    if re.search(r"\bkoff\b", text, re.I) or rate and re.search(r"(?<![A-Za-z0-9])k\s*d(?![A-Za-z0-9])", text, re.I):
        values.add("koff")
    return values


def contains_entity_name(text: str, name: str) -> bool:
    """Allow whitespace/OCR wrapping while retaining numeric name boundaries."""
    value = _entity_text(text)
    name = _entity_text(name)
    parts = re.split(r"\s+", name.strip())
    if not name.strip():
        return False
    pattern = r"(?<![A-Za-z0-9])" + r"\s*".join(re.escape(part) for part in parts) + r"(?![A-Za-z0-9])"
    return bool(re.search(pattern, value, re.I))


def build_evidence(
    tables: list[dict[str, Any]],
    contexts: list[dict[str, Any]] | None = None,
    method_pages: list[dict[str, Any]] | None = None,
    sequence_passages: list[dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    evidence: dict[str, dict[str, Any]] = {}
    for table in tables:
        identity = table_id(table)
        for source in table.get("sequence_style_sources", []):
            from document.sequence_styles import validate_style_source
            validate_style_source(source, {c["source_ref"]: c for row in table["rows"] for c in row})
            evidence[source["source_ref"]] = dict(source)
        evidence[identity] = {"text": str(table.get("caption", "")), "table_id": identity, "kind": "table", "page": table.get("page")}
        evidence[f"{identity}:image"] = {"text": "Table crop image", "table_id": identity, "kind": "image", "page": table.get("page")}
        for index, token in enumerate(table.get("ocr_items", [])):
            ref = f"{identity}:token:{token.get('item_index', index)}"
            evidence[ref] = {**token, "table_id": identity, "kind": "ocr_token", "page": table.get("page"), "source_ref": ref}
        for row_index, row in enumerate(table.get("rows", [])):
            for col_index, cell in enumerate(row):
                evidence[cell_ref(identity, row_index, col_index)] = {
                    "text": str(cell.get("text", "")), "table_id": identity,
                    "row_index": row_index, "col_index": col_index, "kind": "cell",
                    "page": table.get("page"),
                    "source_ref": cell.get("source_ref", cell_ref(identity, row_index, col_index)),
                    "item_indices": cell.get("item_indices", []),
                    "bbox": cell.get("bbox", []),
                    "text_coverage": cell.get("text_coverage", {}),
                    "numeric_text": cell.get("numeric_text"),
                    "header_span": cell.get("header_span"),
                    **({"text_styles": cell["text_styles"]} if cell.get("text_styles") else {}),
                    **geometry_fields(cell),
                }
    for context in contexts or []:
        identity = str(context.get("table_id", ""))
        if identity not in evidence:
            continue
        items = [context.get("caption"), *context.get("nearby_context", []), *context.get("referring_sentences", []), *context.get("footnotes", [])]
        for index, item in enumerate(items):
            if not item or not item.get("text"):
                continue
            ref = f"{identity}:context:{index}"
            evidence[ref] = {**item, "table_id": identity, "kind": "context"}
    for page in [*(method_pages or []), *(sequence_passages or [])]:
        number = page.get("page")
        if page.get("ref"):
            evidence[page["ref"]] = {**page, "kind": "sequence_passage"}
            continue
        if page.get("text"):
            evidence[f"method:p{number}"] = {"text": str(page["text"]), "page": number, "kind": "method"}
            text = str(page["text"])
            headings = list(re.finditer(r"(?m)^\s*(\d{1,2}\.\d{1,2})(?![\d.])\s+(?=[A-Za-z\u4e00-\u9fff])", text))
            for index, heading in enumerate(headings):
                end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
                ref = f"method:p{number}:section:{heading[1]}:{heading.start()}"
                evidence[ref] = {"text": text[heading.start():end], "page": number, "kind": "method",
                                 "section_id": heading[1], "char_start": heading.start(), "char_end": end,
                                 "source_ref": f"method:p{number}"}
        for index, line in enumerate(page.get("lines", [])):
            if line.get("text"):
                evidence[f"method:p{number}:L{index}"] = {**line, "page": number, "kind": "method"}
    return evidence


def clipped_text(value: str, limit: int = 500) -> str:
    if len(value) <= limit:
        return value
    return f"{value[:limit // 2]} [middle omitted; {len(value)} characters] {value[-limit // 2:]}"


def pack_table(table: dict[str, Any], *, cell_limit: int = 500) -> dict[str, Any]:
    identity = table_id(table)
    return {
        "table_id": identity, "page": table.get("page"),
        "image_ref": f"{identity}:image", "mode": table.get("mode"),
        "caption": table.get("caption", ""),
        "row_count": len(table.get("rows", [])),
        "column_count": max((len(row) for row in table.get("rows", [])), default=0),
        "cells": [
            {"ref": cell_ref(identity, row_index, col_index), "row": row_index,
             "column": col_index, "text": clipped_text(str(cell.get("text", "")), cell_limit),
             "confidence": cell.get("confidence"), "bbox": cell.get("bbox", []),
             "item_indices": cell.get("item_indices", []), "text_coverage": cell.get("text_coverage", {}),
             "numeric_text": cell.get("numeric_text"), "header_span": cell.get("header_span")}
            for row_index, row in enumerate(table.get("rows", []))
            for col_index, cell in enumerate(row)
            if str(cell.get("text", "")).strip()
        ],
        "structure_repair": table.get("structure_repair"),
        "token_audit": table.get("token_audit", {}),
        "unassigned_tokens": [{**token, "ref": f"{identity}:token:{token.get('item_index', index)}"}
                              for index, token in enumerate(table.get("ocr_items", []))
                              if token.get("item_index", index) in table.get("token_audit", {}).get("unassigned_indices", [])],
    }


def find_image(table: dict[str, Any], roots: list[Path]) -> Path | None:
    if table.get("sda_source"):
        # Prefer the validated exact crop, including when basenames repeat.
        value = table.get("image_path")
        if not value:
            return None
        path = Path(value).resolve()
        return path if path.is_file() and any(path.is_relative_to(root.resolve()) for root in roots) else None
    filename = str(table.get("image", ""))
    if not filename:
        return None
    for root in roots:
        path = (root / filename).resolve()
        # Only attach evidence beneath explicit input/output image roots.
        if path.is_relative_to(root.resolve()) and path.is_file():
            return path
    return None


def require_refs(refs: list[str], evidence: dict[str, dict[str, Any]]) -> None:
    missing = [ref for ref in refs if ref not in evidence]
    if missing:
        raise ValueError(f"Unknown evidence references: {missing[:8]}")


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()
