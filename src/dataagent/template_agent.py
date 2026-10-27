#!/usr/bin/env python3
"""Infer and validate a document-specific semantic template from OCR evidence.

The template agent is deliberately split into two responsibilities:

* inference proposes a small, declarative entity grammar from repeated table
  labels; and
* validation measures coverage and rejects unsafe or unsupported grammars.

It never emits executable Python.  Downstream parsers consume the validated
fields and retain raw names alongside canonical keys.
"""

from __future__ import annotations

import html
import re
from collections import Counter, defaultdict
from typing import Any

from .assays import load_semantic_template


AGENT_VERSION = 2
SEMANTIC_TEMPLATE = load_semantic_template()
ASSAY_KNOWLEDGE = SEMANTIC_TEMPLATE.get("assay", {})
CONSTRUCT_KNOWLEDGE = SEMANTIC_TEMPLATE.get("antibody_construct_knowledge", {})
ENTITY_HEADER_RE = re.compile(
    r"^(?:抗体|突变号|克隆|名称|name|clone|antibody|construct|protein)$",
    re.I,
)
ENDPOINT_RE = re.compile(r"\b(?:IC|EC)\s*50\b|IC₅₀|EC₅₀", re.I)
TRANSPOSED_ENDPOINT_HEADER_RE = re.compile(r"^(?:IC|EC)(?:\s|\(|$)", re.I)
REFERENCE_RE = re.compile(
    r"^(?:reference(?:\s+antibody)?|control(?:\s+antibody)?|vehicle|blank|"
    r"human\s+isotype(?:\s+igg\d+)?|igg\d+)$",
    re.I,
)
FAMILY_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9])(?P<prefix>[A-Za-z][A-Za-z0-9]*[-_])"
    r"(?P<number>\d+)(?P<suffix>[A-Za-z]?)(?![A-Za-z0-9])"
)
BLOCKED_EXAMPLES = (
    "Donor 1",
    "Table 14",
    "SEQ ID NO: 91",
    "IC50",
    "EC50",
    "Human TSLP",
    "Cyno TSLP",
)
EXAMPLE_RE = re.compile(
    r"(?:\bExamp(?:le|1e)|实施例|实验例)\s*(?P<number>\d+(?:\.\d+)*)"
    r"\s*[.:：、]?\s*(?P<title>[^\n]{0,180})",
    re.I,
)
NUMBERED_ASSAY_SECTION_RE = re.compile(
    r"(?m)^[ \t]*(?P<number>\d+(?:\.\d+)+)\s*[.、:：]?[ \t]*"
    r"(?P<title>[^\n]*(?:ELISA|Biacore|KinExA|SPR|BLI|FACS|流式|细胞|"
    r"亲\s*和\s*力|阻\s*断|结\s*合)[^\n]*)",
    re.I,
)
TABLE_REFERENCE_RE = re.compile(
    r"(?:\bTable\s*|表\s*)(?P<number>\d+(?:[-.]\d+)?[A-Za-z]?)(?!\d)",
    re.I,
)
TABLE_CAPTION_RE = re.compile(
    r"(?:\bTable\s*|表\s*)(?P<number>\d+(?:[-.]\d+)?[A-Za-z]?)"
    r"\s*[.:：、]?\s*(?P<title>.*)$",
    re.I,
)
DONOR_RE = re.compile(r"\bDonor\s*(?P<number>\d+)\b", re.I)


def clean_text(value: Any) -> str:
    return re.sub(
        r"\s+",
        " ",
        html.unescape(str(value or "")).replace("<br>", " ").replace("<br/>", " "),
    ).strip(" |,;:")


def _table_rows(table: dict[str, Any]) -> list[list[str]]:
    return [
        [clean_text(cell.get("text")) for cell in row if isinstance(cell, dict)]
        for row in table.get("rows", [])
        if isinstance(row, list)
    ]


def _tokens(value: str) -> list[str]:
    """Return conservative antibody-like labels from one entity cell."""

    text = clean_text(value)
    if not text:
        return []
    if REFERENCE_RE.fullmatch(text):
        return [text]
    return [match.group(0) for match in FAMILY_TOKEN_RE.finditer(text)]


def collect_entity_evidence(ocr_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collect names only from columns/rows with explicit entity semantics."""

    evidence: list[dict[str, Any]] = []
    seen: set[tuple[str, str, int, int]] = set()
    for table in ocr_results:
        rows = _table_rows(table)
        table_id = str(table.get("source_id") or table.get("image") or "unknown")
        page = table.get("page")

        # Row-oriented tables: Name/clone/antibody column followed by records.
        for header_index, row in enumerate(rows[:4]):
            entity_columns = [
                index for index, value in enumerate(row)
                if ENTITY_HEADER_RE.fullmatch(clean_text(value))
            ]
            for entity_column in entity_columns:
                for row_index in range(header_index + 1, len(rows)):
                    if entity_column >= len(rows[row_index]):
                        continue
                    raw = rows[row_index][entity_column]
                    for token in _tokens(raw):
                        key = (table_id, token.casefold(), row_index, entity_column)
                        if key in seen:
                            continue
                        seen.add(key)
                        evidence.append({
                            "raw_name": token,
                            "source_text": raw,
                            "table_id": table_id,
                            "page": page,
                            "row_index": row_index,
                            "col_index": entity_column,
                            "source": "entity_column",
                        })

        # Transposed assay tables: endpoint is in the first header cell and
        # entity names occupy the remaining header cells.
        for row_index, row in enumerate(rows[:3]):
            if not row or not TRANSPOSED_ENDPOINT_HEADER_RE.search(row[0]):
                continue
            for col_index, raw in enumerate(row[1:], start=1):
                for token in _tokens(raw):
                    key = (table_id, token.casefold(), row_index, col_index)
                    if key in seen:
                        continue
                    seen.add(key)
                    evidence.append({
                        "raw_name": token,
                        "source_text": raw,
                        "table_id": table_id,
                        "page": page,
                        "row_index": row_index,
                        "col_index": col_index,
                        "source": "transposed_header",
                    })
    return evidence


def _result_text(result: dict[str, Any]) -> str:
    value = clean_text(result.get("text"))
    if value:
        return str(result.get("text", ""))
    items = [str(item.get("text", "")) for item in result.get("ocr_items", [])]
    if items:
        return "\n".join(item for item in items if item)
    return "\n".join(
        " ".join(str(cell.get("text", "")) for cell in row if isinstance(cell, dict))
        for row in result.get("rows", [])
        if isinstance(row, list)
    )


def _matches(text: str, patterns: list[str]) -> list[str]:
    matched: list[str] = []
    for pattern in patterns:
        try:
            if re.search(pattern, text, re.I):
                matched.append(pattern)
        except re.error:
            continue
    return matched


def _definition_by_key(definitions: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(item.get("key")): item for item in definitions if item.get("key")}


def _species_for_text(text: str) -> list[str]:
    return [
        str(item["key"])
        for item in ASSAY_KNOWLEDGE.get("species", [])
        if _matches(text, list(item.get("patterns", [])))
    ]


def _method_for_text(text: str) -> tuple[dict[str, Any] | None, int, list[str]]:
    ranked: list[tuple[int, int, dict[str, Any], list[str]]] = []
    for order, method in enumerate(ASSAY_KNOWLEDGE.get("methods", [])):
        matched = _matches(text, list(method.get("patterns", [])))
        score = len(matched)
        if score >= int(method.get("minimum_score", 2)):
            ranked.append((score, -order, method, matched))
    if not ranked:
        return None, 0, []
    score, _, method, matched = max(ranked, key=lambda item: (item[0], item[1]))
    return method, score, matched


def _keyword(
    category: str,
    key: str,
    label_zh: str,
    label_en: str,
    confidence: float,
) -> dict[str, Any]:
    return {
        "category": category,
        "key": key,
        "label_zh": label_zh,
        "label_en": label_en,
        "confidence": round(confidence, 4),
    }


def _keywords_for_classification(
    species: list[str],
    method: dict[str, Any] | None,
    *,
    donor_dimension: bool = False,
    confidence: float = 0.9,
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    species_defs = _definition_by_key(ASSAY_KNOWLEDGE.get("species", []))
    system_defs = _definition_by_key(ASSAY_KNOWLEDGE.get("systems", []))
    for key in species:
        definition = species_defs.get(key)
        if definition:
            output.append(_keyword(
                "species",
                key,
                str(definition.get("label_zh", key)),
                str(definition.get("label_en", key)),
                confidence,
            ))
    if method:
        system_key = str(method.get("system", ""))
        system = system_defs.get(system_key)
        if system:
            output.append(_keyword(
                "system",
                system_key,
                str(system.get("label_zh", system_key)),
                str(system.get("label_en", system_key)),
                confidence,
            ))
        output.append(_keyword(
            "method",
            str(method.get("key", "unknown")),
            str(method.get("label_zh", method.get("key", ""))),
            str(method.get("label_en", method.get("key", ""))),
            confidence,
        ))
    if donor_dimension:
        output.append(_keyword("sample", "donor", "供者", "Donor", confidence))
    return output


def _deduplicate_keywords(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for value in values:
        key = (str(value.get("category", "")), str(value.get("key", "")))
        if key in seen:
            continue
        seen.add(key)
        output.append(value)
    return output


def infer_construct_template(
    ocr_results: list[dict[str, Any]],
    method_pages: list[dict[str, Any]],
) -> dict[str, Any]:
    """Detect only explicitly supported antibody/chain forms."""

    sources = [
        (str(item.get("source_id", "unknown")), item.get("page"), _result_text(item))
        for item in [*method_pages, *ocr_results]
    ]
    detected: list[dict[str, Any]] = []
    for definition in CONSTRUCT_KNOWLEDGE.get("types", []):
        evidence = []
        for source_id, page, text in sources:
            matched = _matches(text, list(definition.get("patterns", [])))
            if not matched:
                continue
            evidence.append({
                "source_id": source_id,
                "page": page,
                "matched_patterns": matched,
            })
        if evidence:
            detected.append({
                "key": definition.get("key"),
                "label_zh": definition.get("label_zh"),
                "label_en": definition.get("label_en"),
                "confidence": round(min(1.0, 0.72 + 0.07 * len(evidence)), 4),
                "evidence": evidence[:12],
            })
    return {
        "knowledge_version": SEMANTIC_TEMPLATE.get("version", 1),
        "known_types": [
            {
                "key": item.get("key"),
                "label_zh": item.get("label_zh"),
                "label_en": item.get("label_en"),
            }
            for item in CONSTRUCT_KNOWLEDGE.get("types", [])
        ],
        "detected_types": detected,
    }


def _protocol_sections(method_pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sections: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for page in sorted(method_pages, key=lambda item: int(item.get("page") or 0)):
        text = str(page.get("text") or _result_text(page))
        matches = sorted(
            [*EXAMPLE_RE.finditer(text), *NUMBERED_ASSAY_SECTION_RE.finditer(text)],
            key=lambda match: match.start(),
        )
        looks_like_table_appendix = bool(
            not matches
            and any(
                TABLE_CAPTION_RE.search(line)
                for line in text[:700].splitlines()
            )
            and not re.search(r"\[\s*0?\d{3,5}\s*\]", text)
        )
        if current and looks_like_table_appendix:
            sections.append(current)
            current = None
            continue
        cursor = 0
        if matches and current and matches[0].start() > 0:
            current["parts"].append(text[:matches[0].start()])
            current["pages"].add(int(page.get("page") or 0))
        elif not matches and current:
            current["parts"].append(text)
            current["pages"].add(int(page.get("page") or 0))
            continue
        for index, match in enumerate(matches):
            if current:
                sections.append(current)
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            title = clean_text(match.group("title")).strip(" .:-")
            current = {
                "example_number": match.group("number"),
                "title": title,
                "parts": [text[match.start():end]],
                "pages": {int(page.get("page") or 0)},
            }
            cursor = end
        if not matches and not current:
            cursor = len(text)
    if current:
        sections.append(current)
    return sections


def _specific_process_summary(
    method: dict[str, Any],
    text: str,
) -> tuple[str, str]:
    key = str(method.get("key", ""))
    if key == "primary_cell_cytokine_ic50" and re.search(r"PBMC|mDC|dendritic", text, re.I):
        donor_match = re.search(r"(?:from\s+)?(\d+)\s+independent donors?", text, re.I)
        donors = donor_match.group(1) if donor_match else "multiple"
        zh_donors = f"{donors} 名独立供者" if donors.isdigit() else "多名独立供者"
        return (
            f"从新鲜全血的{zh_donors}分离 PBMC 并富集 mDC；抗体与 hTSLP 预孵育后加入 mDC 培养，收集培养液，用 MSD 检测 CCL17（TARC）并通过非线性曲线拟合 IC50。",
            f"PBMCs were isolated from fresh whole blood from {donors} independent donors and mDCs were enriched. Antibody was pre-incubated with hTSLP before mDC culture; supernatant CCL17 (TARC) was measured by MSD and IC50 was fitted nonlinearly.",
        )
    return (
        str(method.get("summary_zh", "")),
        str(method.get("summary_en", "")),
    )


def _explicit_table_numbers(text: str) -> list[str]:
    """Return table references used in method/result prose, not table captions."""

    values: list[str] = []
    relation = re.compile(
        r"shown|displayed|reported|determined|obtained|calculated|results?|values?|"
        r"curve|fitting|compare|see|summari[sz]ed|结\s*果|数\s*值|曲\s*线|"
        r"拟\s*合|计\s*算|如|见|所\s*示|列\s*于|总\s*结|显\s*示",
        re.I,
    )
    for match in TABLE_REFERENCE_RE.finditer(text):
        window = text[max(0, match.start() - 180): min(len(text), match.end() + 90)]
        if relation.search(window):
            values.append(match.group("number"))
    return list(dict.fromkeys(values))


def collect_assay_protocols(method_pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    protocols: list[dict[str, Any]] = []
    for section in _protocol_sections(method_pages):
        text = "\n".join(section["parts"])
        method, score, matched = _method_for_text(text)
        if not method:
            continue
        species = _species_for_text(text)
        donor_dimension = bool(DONOR_RE.search(text) or re.search(r"independent donors?", text, re.I))
        example_number = str(section["example_number"])
        method_key = str(method.get("key", "unknown"))
        summary_zh, summary_en = _specific_process_summary(method, text)
        table_numbers = _explicit_table_numbers(text)
        confidence = min(0.99, 0.68 + 0.05 * score + (0.08 if table_numbers else 0.0))
        title = str(section.get("title") or method.get("label_en") or method_key)
        if method_key == "primary_cell_cytokine_ic50":
            title_match = re.search(
                r"Myeloid Dendritic Cell\s*\(mDC\)\s*TSLP Stimulation Assay",
                text,
                re.I,
            )
            if title_match:
                title = title_match.group(0)
        protocols.append({
            "protocol_id": f"example_{example_number}_{method_key}",
            "example_number": example_number,
            "title": title,
            "source_pages": sorted(page for page in section["pages"] if page),
            "table_numbers": table_numbers,
            "endpoint": method.get("endpoint"),
            "assay_system": method.get("system"),
            "assay_method": method_key,
            "assay_method_label_zh": method.get("label_zh"),
            "assay_method_label_en": method.get("label_en"),
            "species": species,
            "donor_dimension": donor_dimension,
            "summary_zh": summary_zh,
            "summary_en": summary_en,
            "keywords": _keywords_for_classification(
                species, method, donor_dimension=donor_dimension, confidence=confidence
            ),
            "confidence": round(confidence, 4),
            "matched_patterns": matched,
            "source_excerpt": clean_text(text)[:3000],
        })
    return protocols


def infer_table_captions(
    ocr_results: list[dict[str, Any]],
    table_contexts: list[dict[str, Any]],
    method_pages: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Use full-page OCR coordinates to attach the nearest preceding caption."""

    pages = {int(item.get("page")): item for item in method_pages if item.get("page")}
    contexts = {
        str(item.get("table_id")): item
        for item in table_contexts
        if item.get("table_id")
    }
    output: dict[str, dict[str, Any]] = {}
    for table in ocr_results:
        table_id = str(table.get("source_id") or table.get("image") or "")
        page_number = table.get("page")
        context = contexts.get(table_id, {})
        bbox = context.get("bbox", [])
        page = pages.get(int(page_number)) if isinstance(page_number, int) else None
        candidates: list[tuple[float, re.Match[str], dict[str, Any]]] = []
        if page and len(bbox) == 4:
            scale_y = float(page.get("pdf_height") or 1.0) / max(1.0, float(page.get("height") or 1.0))
            table_top = float(bbox[1])
            for line in page.get("lines", []):
                match = TABLE_CAPTION_RE.search(str(line.get("text", "")))
                line_box = line.get("bbox", [])
                if not match or len(line_box) != 4:
                    continue
                line_bottom = float(line_box[3]) * scale_y
                distance = table_top - line_bottom
                if -8 <= distance <= 130:
                    candidates.append((abs(distance), match, line))
        if candidates:
            _, match, line = min(candidates, key=lambda item: item[0])
            output[table_id] = {
                "table_number": match.group("number"),
                "caption": clean_text(line.get("text")),
                "caption_title": clean_text(match.group("title")),
                "caption_page": page_number,
                "caption_bbox": line.get("bbox", []),
                "caption_confidence": line.get("confidence", 0.0),
                "source": page.get("source") if page else "page_ocr",
            }
        else:
            existing = context.get("caption") or {}
            number = context.get("table_number")
            if number or existing:
                text = clean_text(existing.get("text") if isinstance(existing, dict) else existing)
                output[table_id] = {
                    "table_number": str(number or ""),
                    "caption": text,
                    "caption_title": text,
                    "caption_page": page_number,
                    "caption_confidence": existing.get("confidence", 1.0) if isinstance(existing, dict) else 1.0,
                    "source": "pdf_text",
                }
    return output


def _table_text(table: dict[str, Any]) -> str:
    return " ".join(cell for row in _table_rows(table) for cell in row)


def _profile_from_protocols(
    table_id: str,
    table: dict[str, Any],
    caption: dict[str, Any],
    protocols: list[dict[str, Any]],
    configured: dict[str, Any] | None = None,
) -> dict[str, Any]:
    configured = configured or {}
    table_number = str(caption.get("table_number") or "")
    matches = [
        protocol for protocol in protocols
        if table_number and table_number in {str(value) for value in protocol.get("table_numbers", [])}
    ]
    reason = "explicit_table_reference" if matches else ""
    confidence = 0.97 if len(matches) == 1 else 0.92 if matches else 0.0
    text = _table_text(table)
    if not matches and DONOR_RE.search(text):
        matches = [
            protocol for protocol in protocols
            if protocol.get("donor_dimension")
            and protocol.get("assay_system") == "cell"
        ]
        if len(matches) == 1:
            reason = "donor_primary_cell_signature"
            confidence = 0.86
        else:
            matches = []
    if not matches and configured.get("assay_method"):
        methods = _definition_by_key(ASSAY_KNOWLEDGE.get("methods", []))
        method = methods.get(str(configured.get("assay_method")))
        if method:
            species = list(dict.fromkeys(str(value) for value in configured.get("species", [])))
            donor_dimension = bool(configured.get("donor_dimension"))
            confidence = float(configured.get("confidence", 0.9))
            protocol_id = str(
                configured.get("protocol_id")
                or configured.get("experiment_id")
                or f"profile_{table_id}_{method.get('key')}"
            )
            source_pages = sorted({
                int(page) for page in configured.get("protocol_pages", []) if str(page).isdigit()
            })
            summary_zh, summary_en = _specific_process_summary(method, text)
            evidence_text = str(
                configured.get("evidence_text")
                or configured.get("label")
                or method.get("label_zh")
                or ""
            )
            return {
                "table_id": table_id,
                **caption,
                "table_number": str(
                    caption.get("table_number") or configured.get("table_number") or ""
                ),
                "status": "matched",
                "protocol_id": protocol_id,
                "protocol_ids": [protocol_id],
                "experiment_id": str(configured.get("experiment_id") or protocol_id),
                "label": str(configured.get("label") or caption.get("caption") or protocol_id),
                "match_confidence": round(confidence, 4),
                "match_reason": "document_profile_prior",
                "species": species,
                "assay_system": method.get("system"),
                "assay_method": method.get("key"),
                "endpoint": configured.get("endpoint") or method.get("endpoint"),
                "donor_dimension": donor_dimension,
                "keywords": _keywords_for_classification(
                    species,
                    method,
                    donor_dimension=donor_dimension,
                    confidence=confidence,
                ),
                "summary_zh": str(configured.get("summary_zh") or summary_zh),
                "summary_en": str(configured.get("summary_en") or summary_en),
                "source_pages": source_pages,
                "protocol_evidence": [
                    {
                        "page": page,
                        "text": evidence_text,
                        "relation": "document_profile_prior",
                        "confidence": round(confidence, 4),
                        "source": "semantic_template",
                    }
                    for page in source_pages[:1]
                ],
            }
    if not matches:
        method, score, _ = _method_for_text(" ".join([caption.get("caption", ""), text]))
        species = _species_for_text(text)
        return {
            "table_id": table_id,
            **caption,
            "status": "unmatched",
            "match_confidence": round(min(0.7, 0.4 + 0.05 * score), 4) if method else 0.0,
            "match_reason": "table_content_only" if method else "no_protocol_match",
            "species": species,
            "assay_system": method.get("system") if method else None,
            "assay_method": method.get("key") if method else None,
            "endpoint": method.get("endpoint") if method else "IC50",
            "keywords": _keywords_for_classification(species, method, confidence=0.65),
        }

    methods = list(dict.fromkeys(str(item.get("assay_method")) for item in matches))
    systems = list(dict.fromkeys(str(item.get("assay_system")) for item in matches))
    endpoints = list(dict.fromkeys(str(item.get("endpoint")) for item in matches))
    species = list(dict.fromkeys(
        species_key for item in matches for species_key in item.get("species", [])
    ))
    keywords = _deduplicate_keywords([
        keyword for item in matches for keyword in item.get("keywords", [])
    ])
    if len(matches) == 1:
        protocol_id = str(matches[0]["protocol_id"])
        label = f"Section {matches[0]['example_number']} · {matches[0]['title']}"
    else:
        protocol_id = f"table_{table_number or table_id}_" + "_".join(methods)
        label = caption.get("caption") or f"Table {table_number}"
    return {
        "table_id": table_id,
        **caption,
        "status": "matched",
        "protocol_id": protocol_id,
        "protocol_ids": [str(item["protocol_id"]) for item in matches],
        "experiment_id": str(configured.get("experiment_id") or protocol_id),
        "label": str(configured.get("label") or label),
        "match_confidence": confidence,
        "match_reason": reason,
        "species": species,
        "assay_system": systems[0] if len(systems) == 1 else "mixed",
        "assay_method": methods[0] if len(methods) == 1 else "mixed",
        "endpoint": endpoints[0] if len(endpoints) == 1 else "mixed",
        "donor_dimension": any(item.get("donor_dimension") for item in matches),
        "keywords": keywords,
        "summary_zh": " ".join(str(item.get("summary_zh", "")) for item in matches).strip(),
        "summary_en": " ".join(str(item.get("summary_en", "")) for item in matches).strip(),
        "source_pages": sorted({
            page for item in matches for page in item.get("source_pages", [])
        }),
        "protocol_evidence": [
            {
                "page": page,
                "text": item.get("source_excerpt", ""),
                "relation": "explicit_reference" if reason == "explicit_table_reference" else "implicit_reference",
                "confidence": confidence,
                "source": "page_ocr",
            }
            for item in matches
            for page in item.get("source_pages", [])[:1]
        ],
    }


def infer_assay_template(
    ocr_results: list[dict[str, Any]],
    method_pages: list[dict[str, Any]],
    table_contexts: list[dict[str, Any]],
    document_id: str = "",
) -> dict[str, Any]:
    protocols = collect_assay_protocols(method_pages)
    captions = infer_table_captions(ocr_results, table_contexts, method_pages)
    configured_profiles = (
        ASSAY_KNOWLEDGE.get("document_profiles", {}).get(document_id, {})
        if document_id
        else {}
    )
    table_profiles: dict[str, dict[str, Any]] = {}
    endpoint_tables = 0
    for table in ocr_results:
        table_id = str(table.get("source_id") or table.get("image") or "unknown")
        configured = configured_profiles.get(table_id, {})
        text = _table_text(table)
        if (
            not configured
            and not ENDPOINT_RE.search(text)
            and not re.search(r"\bK[Dd]\b|EC\s*50", text, re.I)
        ):
            continue
        rows = _table_rows(table)
        has_entity_header = any(
            ENTITY_HEADER_RE.fullmatch(cell)
            for row in rows[:4]
            for cell in row
        )
        has_transposed_entities = bool(
            rows
            and rows[0]
            and re.search(r"^(?:IC|EC)(?:\s|\(|$)", rows[0][0], re.I)
            and len(rows[0]) >= 3
        )
        if not configured and not has_entity_header and not has_transposed_entities:
            continue
        endpoint_tables += 1
        table_profiles[table_id] = _profile_from_protocols(
            table_id,
            table,
            captions.get(table_id, {}),
            protocols,
            configured,
        )
    matched = sum(profile.get("status") == "matched" for profile in table_profiles.values())
    status = (
        "unavailable"
        if not method_pages
        else "validated"
        if endpoint_tables and matched == endpoint_tables
        else "needs_review"
        if endpoint_tables
        else "unavailable"
    )
    return {
        "status": status,
        "knowledge_version": SEMANTIC_TEMPLATE.get("version", 1),
        "protocols": protocols,
        "method_page_count": len(method_pages),
        "table_profiles": table_profiles,
        "endpoint_table_count": endpoint_tables,
        "matched_table_count": matched,
        "configured_profile_count": sum(
            profile.get("match_reason") == "document_profile_prior"
            for profile in table_profiles.values()
        ),
        "coverage": round(matched / endpoint_tables, 4) if endpoint_tables else 0.0,
    }


def assay_metadata_for_observation(
    table_id: str,
    measurement_label: str,
    condition_label: str,
    template: dict[str, Any] | None,
    *, profile_override: dict[str, Any] | None = None, endpoint: str = "IC50",
) -> dict[str, Any]:
    """Return observation-level protocol metadata and localized keywords."""

    profile = profile_override if profile_override is not None else (
        (template or {}).get("assay_template", {}).get("table_profiles", {}).get(table_id, {})
    )
    observation_text = " ".join([condition_label, measurement_label])
    from .assays import annotate_assay, keyword_context

    context = (template or {}).get("assay_keyword_context")
    if context is None:
        context = keyword_context()
    annotation = annotate_assay(profile, measurement_label, condition_label, context, table_id=table_id, endpoint=endpoint)
    # Vocabulary gaps must not erase species facts already extracted upstream.
    species = annotation.pop("species") or _species_for_text(observation_text) or list(profile.get("species", []))
    donor = DONOR_RE.search(observation_text)
    donor_id = f"Donor {donor.group('number')}" if donor else None
    return {
        "assay_protocol_id": profile.get("protocol_id"),
        "assay_protocol_ids": profile.get("protocol_ids", []),
        "assay_method": profile.get("assay_method"),
        "assay_system": profile.get("assay_system"),
        "donor_id": donor_id,
        "species": species,
        **annotation,
        "assay_protocol_summary_zh": profile.get("summary_zh", ""),
        "assay_protocol_summary_en": profile.get("summary_en", ""),
        "assay_protocol_pages": profile.get("source_pages", []),
        "assay_protocol_confidence": profile.get("match_confidence"),
        "assay_protocol_match_reason": profile.get("match_reason"),
        "assay_protocol_evidence": profile.get("protocol_evidence", []),
    }


def enrich_table_contexts_with_assay_template(
    table_contexts: list[dict[str, Any]],
    template: dict[str, Any],
) -> list[dict[str, Any]]:
    """Expose protocol summaries/evidence through the existing review UI API."""

    profiles = template.get("assay_template", {}).get("table_profiles", {})
    output: list[dict[str, Any]] = []
    for raw in table_contexts:
        context = dict(raw)
        profile = profiles.get(str(context.get("table_id")), {})
        scoped = [p for p in template.get("assay_template", {}).get("region_profiles", []) if p["table_id"] == context.get("table_id")]
        if scoped:
            context["experiment_summaries"] = [{"region_id": p.get("region_id"), "method": p.get("assay_method"),
                "metric": ", ".join(p.get("endpoints", [])), "summary_zh": p.get("summary_zh", ""),
                "summary_en": p.get("summary_en", "")} for p in scoped]
        if profile.get("status") == "mixed":
            output.append(context)
            continue
        if profile.get("status") == "matched":
            evidence = list(profile.get("protocol_evidence", []))
            context["table_number"] = context.get("table_number") or profile.get("table_number")
            if not context.get("caption") and profile.get("caption"):
                context["caption"] = {
                    "evidence_id": f"protocol-caption-{context.get('table_id')}",
                    "page": profile.get("caption_page") or context.get("page"),
                    "bbox": profile.get("caption_bbox", []),
                    "text": profile.get("caption"),
                    "source": profile.get("source", "page_ocr"),
                    "relation": "caption",
                    "confidence": profile.get("caption_confidence", 0.8),
                }
            context["referring_sentences"] = [
                *context.get("referring_sentences", []),
                *[
                    {
                        "evidence_id": f"protocol-{context.get('table_id')}-{index}",
                        "page": item.get("page"),
                        "bbox": item.get("bbox", []),
                        "text": item.get("text", ""),
                        "source": item.get("source", "page_ocr"),
                        "relation": item.get("relation", "explicit_reference"),
                        "confidence": item.get("confidence", 0.8),
                    }
                    for index, item in enumerate(evidence)
                ],
            ]
            # Reanalysis must not append the same protocol evidence again;
            # stable evidence also keeps subsequent LLM cache keys stable.
            unique_references = {}
            for item in context["referring_sentences"]:
                key = (item.get("page"), item.get("text"), item.get("relation"))
                unique_references.setdefault(key, item)
            context["referring_sentences"] = list(unique_references.values())
            context["experiment_summaries"] = [{
                "method": profile.get("assay_method"),
                "metric": profile.get("endpoint", ""),
                "summary_zh": profile.get("summary_zh", ""),
                "summary_en": profile.get("summary_en", ""),
            }]
            context["assay_keywords"] = profile.get("keywords", [])
            context["context_status"] = "linked"
        output.append(context)
    return output


def _pattern_regex(prefix: str, allow_variant_suffix: bool) -> str:
    suffix = r"(?P<variant>[A-Za-z])?" if allow_variant_suffix else ""
    return rf"^{re.escape(prefix)}(?P<number>\d+){suffix}$"


def infer_document_template(
    document_id: str,
    ocr_results: list[dict[str, Any]],
    method_pages: list[dict[str, Any]] | None = None,
    table_contexts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Propose a declarative entity template backed by OCR coordinates."""

    method_pages = method_pages or []
    table_contexts = table_contexts or []
    evidence = collect_entity_evidence(ocr_results)
    references = sorted({
        item["raw_name"] for item in evidence
        if REFERENCE_RE.fullmatch(item["raw_name"])
    }, key=str.casefold)
    families: dict[str, list[dict[str, Any]]] = defaultdict(list)
    prefix_spelling: dict[str, Counter[str]] = defaultdict(Counter)
    for item in evidence:
        match = FAMILY_TOKEN_RE.fullmatch(item["raw_name"])
        if not match:
            continue
        key = match.group("prefix").casefold()
        families[key].append({**item, **match.groupdict()})
        prefix_spelling[key][match.group("prefix")] += 1

    patterns: list[dict[str, Any]] = []
    for key, items in sorted(families.items(), key=lambda pair: -len(pair[1])):
        unique_numbers = sorted({int(item["number"]) for item in items})
        if len(unique_numbers) < 2:
            continue
        prefix = prefix_spelling[key].most_common(1)[0][0]
        suffixes = sorted({item["suffix"].lower() for item in items if item["suffix"]})
        allow_suffix = bool(suffixes)
        examples = list(dict.fromkeys(item["raw_name"] for item in items))[:20]
        patterns.append({
            "kind": "numeric_family",
            "prefix": prefix,
            "regex": _pattern_regex(prefix, allow_suffix),
            "canonical_format": f"{prefix}{{number}}",
            "allow_variant_suffix": allow_suffix,
            "observed_variant_suffixes": suffixes,
            "matched_examples": examples,
            "evidence_count": len(items),
            "distinct_number_count": len(unique_numbers),
            "table_ids": list(dict.fromkeys(item["table_id"] for item in items)),
        })

    template: dict[str, Any] = {
        "template_id": f"{document_id}-document-template-v1",
        "version": 1,
        "document_id": document_id,
        "agent": {
            "name": "TemplateAgent",
            "version": AGENT_VERSION,
            "mode": "deterministic_evidence_inference",
        },
        "status": "proposed",
        "entity_template": {
            "status": "proposed" if patterns else "no_stable_pattern",
            "header_aliases": [
                "抗体", "突变号", "克隆", "名称", "name", "clone",
                "antibody", "construct", "protein",
            ],
            "patterns": patterns,
            "reference_names": references,
            "candidate_count": len(evidence),
            "candidate_examples": list(dict.fromkeys(
                item["raw_name"] for item in evidence
            ))[:40],
        },
        "construct_template": infer_construct_template(ocr_results, method_pages),
        "assay_template": infer_assay_template(
            ocr_results,
            method_pages,
            table_contexts,
            document_id,
        ),
        "evidence": evidence[:250],
        "warnings": [],
    }
    return template


def match_entity(value: Any, template: dict[str, Any] | None) -> dict[str, Any] | None:
    """Match one raw label against a validated document template."""

    raw = clean_text(value)
    if not raw or not template:
        return None
    entity_template = template.get("entity_template", {})
    if template.get("agent", {}).get("mode") == "llm":
        normalized = re.sub(r"\s+", "", raw).casefold()
        for entity in entity_template.get("entities", []):
            if any(re.sub(r"\s+", "", clean_text(name)).casefold() == normalized for name in entity.get("raw_names", [])):
                return {
                    "raw_name": raw, "display_name": raw,
                    "canonical_key": entity["entity_id"],
                    "canonical_name": entity.get("canonical_name", raw),
                    "variant_suffix": entity.get("variant"),
                    "entity_role": entity.get("role", "unknown"),
                    "pattern_kind": "llm_evidence_entity",
                }
        return None
    for pattern in entity_template.get("patterns", []):
        try:
            match = re.fullmatch(str(pattern.get("regex", "")), raw, re.I)
        except re.error:
            continue
        if not match:
            continue
        groups = match.groupdict()
        number = groups.get("number", "")
        variant = groups.get("variant") or ""
        prefix = str(pattern.get("prefix", ""))
        canonical = f"{prefix}{number}"
        return {
            "raw_name": raw,
            "display_name": raw,
            "canonical_key": canonical.casefold(),
            "canonical_name": canonical,
            "variant_suffix": variant.lower() or None,
            "entity_role": "antibody",
            "pattern_kind": pattern.get("kind", "unknown"),
        }
    reference_names = {
        clean_text(name).casefold() for name in entity_template.get("reference_names", [])
    }
    if raw.casefold() in reference_names or REFERENCE_RE.fullmatch(raw):
        canonical = "reference" if "reference" in raw.casefold() else re.sub(
            r"[^a-z0-9]+", "", raw.casefold()
        )
        return {
            "raw_name": raw,
            "display_name": raw,
            "canonical_key": canonical,
            "canonical_name": raw,
            "variant_suffix": None,
            "entity_role": "control",
            "pattern_kind": "reference",
        }
    return None


def expand_entity_names(value: Any, template: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Expand compact labels such as ``AB-1, 3, 8, 9`` safely."""

    direct = match_entity(value, template)
    if direct:
        return [direct]
    raw = clean_text(value)
    if not raw or not template:
        return []
    for pattern in template.get("entity_template", {}).get("patterns", []):
        prefix = str(pattern.get("prefix", ""))
        first = re.match(
            rf"^{re.escape(prefix)}(?P<number>\d+)(?P<variant>[A-Za-z]?)",
            raw,
            re.I,
        )
        if not first:
            continue
        values = [first.group("number") + (first.group("variant") or "")]
        tail = raw[first.end():]
        if tail and not re.fullmatch(r"(?:\s*[,/&]\s*\d+[A-Za-z]?)+", tail, re.I):
            continue
        values.extend(re.findall(r"\d+[A-Za-z]?", tail, re.I))
        expanded = [match_entity(f"{prefix}{item}", template) for item in values]
        return [item for item in expanded if item]
    return []


def validate_document_template(
    template: dict[str, Any],
    ocr_results: list[dict[str, Any]],
) -> dict[str, Any]:
    """Validate coverage and safety without trusting the inferred regexes."""

    evidence = collect_entity_evidence(ocr_results)
    non_reference = [
        item for item in evidence
        if not REFERENCE_RE.fullmatch(item["raw_name"])
    ]
    matched = [item for item in non_reference if match_entity(item["raw_name"], template)]
    unmatched = [item for item in non_reference if not match_entity(item["raw_name"], template)]
    coverage = len(matched) / len(non_reference) if non_reference else 0.0
    distinct_matched = sorted({item["raw_name"] for item in matched}, key=str.casefold)
    matched_tables = {item["table_id"] for item in matched}

    unsafe_matches: list[str] = []
    for example in BLOCKED_EXAMPLES:
        if match_entity(example, template):
            unsafe_matches.append(example)

    canonical_groups: dict[str, set[str]] = defaultdict(set)
    for item in matched:
        entity = match_entity(item["raw_name"], template)
        if entity:
            canonical_groups[entity["canonical_key"]].add(entity["raw_name"])
    variant_groups = {
        key: sorted(values, key=str.casefold)
        for key, values in canonical_groups.items()
        if len(values) > 1
    }

    patterns = template.get("entity_template", {}).get("patterns", [])
    if not patterns or len(distinct_matched) < 2:
        status = "no_stable_pattern"
    elif unsafe_matches or coverage < 0.8:
        status = "needs_review"
    else:
        status = "validated"
    entity_status = status
    confidence = round(min(
        1.0,
        coverage * 0.7
        + min(0.15, len(distinct_matched) / 40)
        + min(0.15, len(matched_tables) / 20),
    ), 4)

    warnings: list[dict[str, Any]] = []
    if status == "no_stable_pattern":
        warnings.append({
            "code": "no_stable_entity_naming_pattern",
            "severity": "high",
            "message": "No stable antibody naming pattern could be inferred from OCR evidence.",
        })
    if unmatched:
        warnings.append({
            "code": "unmatched_entity_names",
            "severity": "medium",
            "message": f"{len(unmatched)} entity-name observations were not covered by the template.",
            "examples": list(dict.fromkeys(item["raw_name"] for item in unmatched))[:20],
        })
    if unsafe_matches:
        warnings.append({
            "code": "unsafe_entity_pattern",
            "severity": "high",
            "message": "The inferred pattern matches known non-entity labels.",
            "examples": unsafe_matches,
        })

    assay_template = template.get("assay_template", {})
    endpoint_table_count = int(assay_template.get("endpoint_table_count", 0))
    matched_table_count = int(assay_template.get("matched_table_count", 0))
    unmatched_profiles = [
        table_id
        for table_id, profile in assay_template.get("table_profiles", {}).items()
        if profile.get("status") != "matched"
    ]
    if (
        endpoint_table_count
        and assay_template.get("method_page_count")
        and not assay_template.get("protocols")
        and matched_table_count < endpoint_table_count
    ):
        warnings.append({
            "code": "no_assay_protocol_detected",
            "severity": "high",
            "message": "Endpoint tables were detected, but no experiment-method protocol was recovered from document text/OCR.",
            "table_ids": unmatched_profiles,
        })
    elif assay_template.get("method_page_count") and unmatched_profiles:
        warnings.append({
            "code": "unmatched_assay_protocols",
            "severity": "medium",
            "message": f"{len(unmatched_profiles)} endpoint table(s) could not be linked to a supported experiment method.",
            "table_ids": unmatched_profiles,
        })
    if (
        status == "validated"
        and assay_template.get("method_page_count")
        and endpoint_table_count
        and matched_table_count < endpoint_table_count
    ):
        status = "needs_review"

    validation = {
        "status": status,
        "confidence": confidence,
        "coverage": round(coverage, 4),
        "candidate_count": len(non_reference),
        "matched_count": len(matched),
        "distinct_matched_count": len(distinct_matched),
        "matched_table_count": len(matched_tables),
        "matched_examples": distinct_matched[:30],
        "unmatched_examples": list(dict.fromkeys(
            item["raw_name"] for item in unmatched
        ))[:30],
        "variant_groups": variant_groups,
        "unsafe_matches": unsafe_matches,
        "assay_protocol_count": len(assay_template.get("protocols", [])),
        "assay_endpoint_table_count": endpoint_table_count,
        "assay_matched_table_count": matched_table_count,
        "assay_coverage": assay_template.get("coverage", 0.0),
        "warnings": warnings,
    }
    template["status"] = status
    template["entity_template"]["status"] = entity_status
    template["entity_template"]["confidence"] = confidence
    template["entity_template"]["coverage"] = round(coverage, 4)
    template["warnings"] = warnings
    return validation


class TemplateAgent:
    """Evidence-driven document-template agent used by the LangGraph node."""

    def run(
        self,
        document_id: str,
        ocr_results: list[dict[str, Any]],
        method_pages: list[dict[str, Any]] | None = None,
        table_contexts: list[dict[str, Any]] | None = None,
        *, antigen_target: str | None = None, keyword_library_dir: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if not ocr_results:
            from .assays import keyword_context

            template = {
                "template_id": f"{document_id}-document-template-v1",
                "version": 1,
                "document_id": document_id,
                "assay_keyword_context": keyword_context(antigen_target, keyword_library_dir),
                "agent": {
                    "name": "TemplateAgent",
                    "version": AGENT_VERSION,
                    "mode": "deterministic_evidence_inference",
                },
                "status": "unavailable",
                "entity_template": {
                    "status": "unavailable",
                    "patterns": [],
                    "reference_names": [],
                    "candidate_count": 0,
                },
                "construct_template": {
                    "knowledge_version": SEMANTIC_TEMPLATE.get("version", 1),
                    "known_types": [],
                    "detected_types": [],
                },
                "assay_template": {
                    "status": "unavailable",
                    "protocols": [],
                    "table_profiles": {},
                    "endpoint_table_count": 0,
                    "matched_table_count": 0,
                    "coverage": 0.0,
                },
                "evidence": [],
                "warnings": [],
            }
            return template, {
                "status": "unavailable",
                "confidence": 0.0,
                "coverage": 0.0,
                "candidate_count": 0,
                "matched_count": 0,
                "warnings": [],
            }
        template = infer_document_template(
            document_id,
            ocr_results,
            method_pages,
            table_contexts,
        )
        validation = validate_document_template(template, ocr_results)
        from .assays import keyword_context

        template["assay_keyword_context"] = keyword_context(
            antigen_target, keyword_library_dir,
            texts=[*(_table_text(table) for table in ocr_results),
                   *(_result_text(page) for page in method_pages or []),
                   *(str(profile.get("label", "")) for profile in template.get("assay_template", {}).get("table_profiles", {}).values()),
                   *(str(evidence.get("text", "")) for profile in template.get("assay_template", {}).get("table_profiles", {}).values()
                     for evidence in profile.get("protocol_evidence", []))],
        )
        return template, validation
