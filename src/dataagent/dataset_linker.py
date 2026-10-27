#!/usr/bin/env python3
"""Link antibody sequence rows and assay rows into a dataset table.

The OCR table database preserves cells. This module adds the semantic layer:
one record per antibody/assay observation with VH, VL, IC50 and provenance.
Repeated extraction of the same observation is collapsed into one record with
multiple evidence items; measurements from different experiments stay apart.
OCR-only links remain ``review``; an existing manually verified CSV may be
provided as a traceable reference seed for benchmarking this linker.
"""

from __future__ import annotations

import csv
import hashlib
import html
import math
import re
from pathlib import Path
from typing import Any

from .assays import load_semantic_template
from .sequence_resolver import construct_keys, resolve_sequences
from .template_agent import assay_metadata_for_observation, match_entity


NUMBER_RE = re.compile(r"(?<![A-Za-z0-9])(?:<|<=|>|>=|~)?\s*(\d+\.\d+|\d+)(?![A-Za-z0-9])")
ENTITY_RE = re.compile(r"(?:tezepumab|tezepelumab|csj117)[^|;<br>\n]*", re.IGNORECASE)
CHAIN_RE = re.compile(r"(?:\bVH\b|\bVL\b|\.H\b|\.L\b|\(VH\)|\(VL\))", re.IGNORECASE)
GENERIC_ENTITY_RE = re.compile(
    r"\b(?:H\d+(?:L\d+)+|[A-Za-z]{1,12}\d+(?:-\d+)+|(?:AMG|hu|ch)\d+)\b",
    re.IGNORECASE,
)
CONSTANT_MOTIFS = (
    "ASTKGPS",  # heavy-chain constant region
    "QANKAT",   # light-chain constant region
)
SEMANTIC_TEMPLATE = load_semantic_template()
ASSAY_TEMPLATE = SEMANTIC_TEMPLATE["assay"]
ENDPOINT_RE = re.compile(
    "|".join(
        r"IC\s*50" if endpoint.upper() == "IC50" else re.escape(endpoint)
        for endpoint in ASSAY_TEMPLATE.get("endpoints", ["IC50"])
    ),
    re.I,
)
ANY_ENDPOINT_RE = re.compile(r"\b(?:IC|EC)\s*(?:50|90)\b|(?:IC|EC)[₅₉][₀]", re.I)
ENTITY_HEADER_RE = re.compile(
    r"^(?:抗体|突变号|克隆|名称|name|clone|antibody|construct|protein)$",
    re.I,
)
ASSAY_AXIS_RE = re.compile(
    r"^(?:assay|method|test|endpoint|实验|试验|检测(?:方法|项目)?)$",
    re.I,
)
RATIO_RE = re.compile(r"\bratio\b|比值|比例", re.I)
UNIT_RE = re.compile(r"(?:nM|pM|µg/mL|μg/mL|ug/mL|mg/mL)", re.I)
TRANSPOSED_IC_HEADER_RE = re.compile(r"^IC(?:\s|\(|$)", re.I)


def normalize_entity(value: str) -> str:
    value = html.unescape(value).lower()
    value = re.sub(r"\([^)]*\)", "", value)
    value = re.sub(r"\b(seq\s*id\s*no?\.?|id)\s*:?\s*\d+", "", value)
    value = re.sub(r"[^a-z0-9]+", "", value)
    value = value.replace("tezepelumab", "tezepumab").replace("tezepulumab", "tezepumab")
    value = re.sub(r"higg\d*$", "", value)
    return re.sub(r"[hl]$", "", value)


def scope_entity_key(
    value: str,
    document_template: dict[str, Any] | None = None,
) -> str:
    """Map OCR labels and reference labels to the same construct key."""

    inferred = match_entity(value, document_template)
    if inferred:
        return str(inferred["canonical_key"])

    # Preserve the experiment-name punctuation (S3-5, 3-3) used by the
    # sequence index.  The older alphanumeric normalization turns S3-5 into
    # S35 and therefore cannot join it to the patent's SEQ-ID map.
    sequence_keys = construct_keys(value)
    if sequence_keys:
        return sequence_keys[0]

    normalized = normalize_entity(value)
    variant = normalized.replace("tezepumab", "")
    if re.fullmatch(r"(?:h\d+l\d+|[a-z]?\d+)", variant):
        return variant
    if "tezepumab" in normalized:
        return "tezepumab"
    return normalized


def _text(value: Any) -> str:
    return re.sub(r"\s+", " ", html.unescape(str(value or "")).replace("<br>", " ")).strip()


def _entity_info(
    text: str,
    document_template: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    inferred = match_entity(text, document_template)
    if inferred:
        return inferred
    if document_template and document_template.get("agent", {}).get("mode") == "llm":
        return None
    parts = [_text(part).strip(" :,-") for part in text.split("|")]
    hits = [part for part in parts if re.search(r"tezep|csj117|higg|\bH\d+L\d+", part, re.I)]
    if hits:
        # Keep the mutation/construct suffix when it is in a neighbouring
        # cell, e.g. ``CSJ117 | H2L1L2-hIgG1``.
        value = " ".join(hit for hit in hits if len(hit) < 180)
        return {
            "raw_name": value,
            "display_name": value,
            "canonical_key": scope_entity_key(value),
            "canonical_name": value,
            "variant_suffix": None,
            "entity_role": "antibody",
            "pattern_kind": "legacy",
        }
    match = ENTITY_RE.search(text)
    if match:
        value = _text(match.group(0)).strip(" :,-")
        return {
            "raw_name": value,
            "display_name": value,
            "canonical_key": scope_entity_key(value),
            "canonical_name": value,
            "variant_suffix": None,
            "entity_role": "antibody",
            "pattern_kind": "legacy",
        }
    # Mutation names such as H0L10 or S3-16 are useful identifiers even when
    # OCR drops the leading antibody family name.
    match = GENERIC_ENTITY_RE.search(text)
    if match:
        value = match.group(0)
        return {
            "raw_name": value,
            "display_name": value,
            "canonical_key": scope_entity_key(value),
            "canonical_name": value,
            "variant_suffix": None,
            "entity_role": "antibody",
            "pattern_kind": "legacy",
        }
    # When TemplateAgent cannot prove a stable grammar, keep an explicit
    # entity-column label as reviewable raw data instead of dropping the IC50
    # observation. Downstream quality warnings make the uncertainty visible.
    value = _text(text).strip(" :,-")
    if (
        document_template
        and value
        and len(value) <= 80
        and re.search(r"[A-Za-z\u4e00-\u9fff]", value)
        and not ENTITY_HEADER_RE.fullmatch(value)
        and not ENDPOINT_RE.search(value)
        and not re.search(r"^(?:donor|table|seq\s*id)\b", value, re.I)
    ):
        return {
            "raw_name": value,
            "display_name": value,
            "canonical_key": normalize_entity(value),
            "canonical_name": value,
            "variant_suffix": None,
            "entity_role": "unknown",
            "pattern_kind": "unvalidated_raw",
        }
    return None


def _entity(
    text: str,
    document_template: dict[str, Any] | None = None,
) -> str:
    info = _entity_info(text, document_template)
    return str(info["display_name"]) if info else ""


def _cell_value(text: str) -> float | None:
    """Return one standalone numeric result from a value cell."""

    cleaned = _text(text)
    if not cleaned or cleaned.upper() in {"N/A", "NA", "—", "-"}:
        return None
    match = NUMBER_RE.search(cleaned)
    if not match:
        return None
    return float(match.group(1))


def table_assay_rows(
    table: dict[str, Any],
    experiment_group: str,
    experiment_id: str | None = None,
    document_template: dict[str, Any] | None = None,
    assay_profile: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Read both row-pair and transposed IC50 table layouts."""

    table_id = str(table.get("source_id") or table.get("image") or "unknown")
    rows = [
        [_text(cell.get("text")) for cell in row if isinstance(cell, dict)]
        for row in table.get("rows", [])
    ]
    llm_plan = table.get("table_classification", {}).get("classifier", {}).get("mode") == "llm"
    if not rows or (not llm_plan and not any(ENDPOINT_RE.search(cell) for row in rows for cell in row)):
        return []

    observations: list[dict[str, Any]] = []
    assay_profile = assay_profile or {}
    seen: set[tuple[str, float, int, int | None, str]] = set()

    def slug(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")[:80]

    def unit(value: str) -> str:
        match = UNIT_RE.search(value)
        return match.group(0) if match else ASSAY_TEMPLATE.get("default_unit", "nM")

    def add(
        entity: dict[str, Any] | None,
        value: float | None,
        row_index: int,
        *,
        col_index: int | None = None,
        measurement_label: str = "",
        condition_label: str = "",
        multiple_measurements: bool = False,
        exact_unit: str | None = None,
        operator: str = "=",
        value_ref: str | None = None,
        header_refs: list[str] | None = None,
        unit_refs: list[str] | None = None,
        region_id: str | None = None,
        entity_ref: str | None = None,
        condition_refs: list[str] | None = None,
    ) -> None:
        if not entity or value is None:
            return
        label = " · ".join(
            item for item in (condition_label, measurement_label) if item
        )
        from .assays import select_assay_profile

        selected_profile = select_assay_profile(
            document_template, table_id, region_id, regions=table.get("table_classification", {}).get("regions", []),
        ) if llm_plan else assay_profile
        base_id = (selected_profile.get("protocol_id") or table_id) if llm_plan else (experiment_id or experiment_group or table_id)
        region_suffix = f":{region_id}" if region_id else ""
        measurement_id = (
            f"{base_id}{region_suffix}:{slug(label) or f'col-{col_index}'}"
            if multiple_measurements
            else base_id
        )
        if llm_plan:
            # ASCII slugs erase Chinese conditions and can merge two equal
            # IC50 values from different systems or metric columns.
            metric_identity = "|".join([label, str(col_index), *(header_refs or [])])
            measurement_id += ":" + hashlib.sha256(metric_identity.encode()).hexdigest()[:12]
        protocol_matched = bool(
            selected_profile.get("status") == "matched"
            and selected_profile.get("protocol_id")
        )
        observation_id = base_id if protocol_matched else measurement_id
        observation_group = (selected_profile.get("label") or experiment_group) if protocol_matched else (
            f"{experiment_group} · {label}".strip(" ·")
            if label and multiple_measurements
            else experiment_group
        )
        key = (
            str(entity["canonical_key"]), value, row_index, col_index, measurement_id,
        )
        if key in seen:
            return
        seen.add(key)
        assay_metadata = assay_metadata_for_observation(
            table_id,
            measurement_label,
            condition_label,
            document_template,
            profile_override=selected_profile if llm_plan else None,
        )
        observations.append(
            {
                "entity": entity["display_name"],
                "raw_entity": entity["raw_name"],
                "entity_key": entity["canonical_key"],
                "canonical_entity": entity["canonical_name"],
                "entity_variant": entity.get("variant_suffix"),
                "entity_role": entity.get("entity_role", "antibody"),
                "ic50": value,
                "ic50_unit": exact_unit if exact_unit is not None else unit(measurement_label),
                "ic50_operator": operator,
                "value_ref": value_ref,
                "header_refs": header_refs or [],
                "unit_refs": unit_refs or [],
                "entity_ref": entity_ref,
                "condition_refs": condition_refs or [],
                "region_id": region_id,
                "assay_binding_status": selected_profile.get("status", "unmatched"),
                "table_id": table_id,
                "image": table.get("image", ""),
                "row_index": row_index,
                "col_index": col_index,
                "measurement_label": measurement_label,
                "condition_label": condition_label,
                "experiment_group": observation_group,
                "experiment_id": observation_id,
                "measurement_id": measurement_id,
                **assay_metadata,
            }
        )

    classification = table.get("table_classification", {})
    if classification.get("classifier", {}).get("mode") == "llm":
        # Execute only the validated declarative plan. Other endpoint plans
        # remain in the classification artifact; never put KD/IC90 into IC50.
        numeric = re.compile(
            r"^\s*(<=|>=|<|>|~|≤|≥)?\s*"
            r"([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)"
            r"\s*(?:nM|pM|[µμu]M|M|[µμu]g/mL|mg/mL)?\s*$", re.I,
        )

        def read_metric(entity: dict[str, Any] | None, metric: dict[str, Any], row_index: int, region: dict[str, Any], group: dict[str, Any] | None = None) -> None:
            if metric.get("endpoint") != "IC50":
                return
            value_column = metric.get("value_column")
            if value_column is None and group:
                value_column = group.get("value_column")
            actual_row = metric.get("value_row")
            actual_row = row_index if actual_row is None else actual_row
            if value_column is None or not 0 <= actual_row < len(rows) or not 0 <= value_column < len(rows[actual_row]):
                return
            match = numeric.fullmatch(rows[actual_row][value_column])
            if not match:
                return
            numeric_value = float(match.group(2))
            if not math.isfinite(numeric_value):
                return
            condition = " · ".join(rows[actual_row][column] for column in region.get("condition_columns", []) if column < len(rows[actual_row]) and rows[actual_row][column])
            name = str((group or {}).get("raw_name") or "")
            label = " · ".join(value for value in ("IC50", metric.get("unit"), name) if value)
            add(
                entity, numeric_value, actual_row,
                col_index=value_column, measurement_label=label,
                condition_label=condition, multiple_measurements=True,
                exact_unit=metric.get("unit") or "",
                operator={"≤": "<=", "≥": ">="}.get(match.group(1), match.group(1) or "="),
                value_ref=f"{table_id}:R{actual_row}C{value_column}",
                header_refs=metric.get("header_refs", []), unit_refs=metric.get("unit_refs", []),
                region_id=region.get("region_id"),
                entity_ref=(group or {}).get("name_ref") or f"{table_id}:R{actual_row}C{region.get('entity_column')}",
                condition_refs=[f"{table_id}:R{actual_row}C{column}" for column in region.get("condition_columns", [])],
            )

        from .assays import identified_regions
        for region in identified_regions(table_id, classification.get("regions", [])):
            if region.get("kind") != "assay" or region.get("execution_status", "ready") != "ready":
                continue
            for row_index in range(region["row_start"], region["row_end"] + 1):
                if not 0 <= row_index < len(rows):
                    continue
                entity_column = region.get("entity_column")
                if entity_column is not None and entity_column < len(rows[row_index]):
                    entity = _entity_info(rows[row_index][entity_column], document_template)
                    for metric in region.get("metrics", []):
                        read_metric(entity, metric, row_index, region)
                for group in region.get("entity_groups", []):
                    entity = _entity_info(group["raw_name"], document_template)
                    for metric in group.get("metrics", []):
                        # Fixed transposed result rows must be visited once.
                        if metric.get("value_row") is not None and row_index != region["row_start"]:
                            continue
                        read_metric(entity, metric, row_index, region, group)
        return observations

    handled_entity_headers = False

    # Classifier-guided column-oriented layout. Some assay tables put assay
    # names down the rows and repeat endpoint pairs for each antibody across
    # the columns, for example ``Assay | IC50 | IC90 | IC50 | IC90``. The
    # legacy pair parser treats the cell left of IC50 as an entity and thus
    # mistakes assay names for antibodies. Use the classifier's orientation
    # decision to recover antibody names from the header rows instead.
    classification = table.get("table_classification", {})
    if classification.get("entity_axis") == "columns":
        for assay_header_index, assay_header in enumerate(rows[:4]):
            assay_axis_columns = [
                index
                for index, cell in enumerate(assay_header)
                if ASSAY_AXIS_RE.fullmatch(cell)
            ]
            endpoint_columns = [
                index
                for index, cell in enumerate(assay_header)
                if ANY_ENDPOINT_RE.search(cell)
            ]
            target_columns = [
                index
                for index, cell in enumerate(assay_header)
                if ENDPOINT_RE.search(cell)
            ]
            if not assay_axis_columns or not endpoint_columns or not target_columns:
                continue

            handled_entity_headers = True
            assay_axis_column = assay_axis_columns[0]
            max_columns = max((len(row) for row in rows), default=0)
            for target_position, value_column in enumerate(target_columns):
                next_target = (
                    target_columns[target_position + 1]
                    if target_position + 1 < len(target_columns)
                    else max_columns
                )
                group_columns = list(range(value_column, next_target))
                entity: dict[str, Any] | None = None
                for header_row in reversed(rows[:assay_header_index]):
                    for column in reversed(group_columns):
                        if column >= len(header_row):
                            continue
                        header_value = header_row[column]
                        if not header_value or RATIO_RE.search(header_value):
                            continue
                        entity = _entity_info(header_value, document_template)
                        if entity:
                            break
                    if entity:
                        break
                if not entity:
                    continue

                measurement_label = " · ".join(
                    value
                    for value in (
                        assay_header[value_column],
                        str(entity.get("display_name") or ""),
                    )
                    if value
                )
                for row_index in range(assay_header_index + 1, len(rows)):
                    row = rows[row_index]
                    if value_column >= len(row):
                        continue
                    condition = (
                        row[assay_axis_column]
                        if assay_axis_column < len(row)
                        else ""
                    )
                    add(
                        entity,
                        _cell_value(row[value_column]),
                        row_index,
                        col_index=value_column,
                        measurement_label=measurement_label,
                        condition_label=condition,
                        multiple_measurements=True,
                    )
            break

    # Row-oriented tables. Supports both a direct header
    # ``clone | hTSLP IC50 | cynoTSLP IC50`` and stacked headers such as an
    # IC50 super-header above ``clone | Donor 1 | ... | Donor 5``.
    for header_index, header in enumerate(rows[:4]):
        entity_columns = [
            index for index, cell in enumerate(header)
            if ENTITY_HEADER_RE.fullmatch(cell)
        ]
        if not entity_columns:
            continue
        entity_column = entity_columns[0]
        header_rows = rows[: header_index + 1]
        endpoint_cells = [
            (row_index, col_index)
            for row_index, row in enumerate(header_rows)
            for col_index, cell in enumerate(row)
            if ENDPOINT_RE.search(cell)
        ]
        if not endpoint_cells:
            continue
        max_columns = max((len(row) for row in rows), default=0)
        entity_groups: list[tuple[int, list[int]]] = []
        if len(entity_columns) == 1:
            entity_column = entity_columns[0]
            value_columns = sorted({
                col_index
                for _, col_index in endpoint_cells
                if col_index != entity_column
            })
            if len(endpoint_cells) == 1 and header_index > endpoint_cells[0][0]:
                # A single centred super-header applies to every labelled
                # value column in the next header row.
                value_columns = [
                    index for index, cell in enumerate(header)
                    if index != entity_column and cell
                ]
            entity_groups.append((entity_column, value_columns))
        else:
            # Repeated horizontal groups are common in compact patent tables:
            # ``antibody | IC50 | antibody | IC50``. Associate each entity
            # column only with endpoint columns before the next entity column;
            # otherwise every value is incorrectly assigned to the first
            # antibody and later collapsed as duplicate evidence.
            ordered_entities = sorted(entity_columns)
            for position, entity_column in enumerate(ordered_entities):
                next_entity_column = (
                    ordered_entities[position + 1]
                    if position + 1 < len(ordered_entities)
                    else max_columns
                )
                value_columns = sorted({
                    col_index
                    for _, col_index in endpoint_cells
                    if entity_column < col_index < next_entity_column
                })
                entity_groups.append((entity_column, value_columns))

        entity_groups = [
            (entity_column, [
                index for index in value_columns if index < max_columns
            ])
            for entity_column, value_columns in entity_groups
            if value_columns
        ]
        if not entity_groups:
            continue
        handled_entity_headers = True
        measurement_count = sum(len(value_columns) for _, value_columns in entity_groups)
        multiple = measurement_count > 1
        for row_index in range(header_index + 1, len(rows)):
            row = rows[row_index]
            for entity_column, value_columns in entity_groups:
                if entity_column >= len(row):
                    continue
                entity = _entity_info(row[entity_column], document_template)
                if not entity:
                    continue
                for value_column in value_columns:
                    if value_column >= len(row):
                        continue
                    header_parts = [
                        header_row[value_column]
                        for header_row in header_rows
                        if value_column < len(header_row) and header_row[value_column]
                    ]
                    if not any(ENDPOINT_RE.search(part) for part in header_parts):
                        header_parts.extend(
                            cell for endpoint_row, endpoint_col in endpoint_cells
                            for cell in [header_rows[endpoint_row][endpoint_col]]
                            if cell and cell not in header_parts
                        )
                    add(
                        entity,
                        _cell_value(row[value_column]),
                        row_index,
                        col_index=value_column,
                        measurement_label=" · ".join(header_parts),
                        multiple_measurements=multiple,
                    )

    # Transposed layout: entity names are column headers and IC50 is a row
    # label. Intermediate rows can provide condition labels (human/cyno).
    for header_index, header in enumerate(rows[:3]):
        if not header or not TRANSPOSED_IC_HEADER_RE.search(header[0]):
            continue
        entities = {
            column: _entity_info(cell, document_template)
            for column, cell in enumerate(header[1:], start=1)
        }
        entities = {column: entity for column, entity in entities.items() if entity}
        if not entities:
            continue
        for assay_row_index in range(header_index + 1, len(rows)):
            assay_row = rows[assay_row_index]
            if not assay_row or not ENDPOINT_RE.search(assay_row[0]):
                continue
            condition = ""
            for prior_index in range(assay_row_index - 1, header_index, -1):
                prior = rows[prior_index]
                if prior and not ENDPOINT_RE.search(" ".join(prior)):
                    condition = " ".join(cell for cell in prior if cell)
                    break
            multiple = len(entities) > 1 or sum(
                bool(row and ENDPOINT_RE.search(row[0]))
                for row in rows[header_index + 1:]
            ) > 1
            for column, entity in entities.items():
                if column >= len(assay_row):
                    continue
                add(
                    entity,
                    _cell_value(assay_row[column]),
                    assay_row_index,
                    col_index=column,
                    measurement_label=f"{header[0]} · {assay_row[0]} · {header[column]}",
                    condition_label=condition,
                    multiple_measurements=multiple,
                )

    # Sparse transposed layout: the entity marker and entity names occupy one
    # row, while the endpoint label and aligned values occupy the next row.
    # OCR commonly preserves their visual columns with blank cells, e.g.
    # ``| 抗体 | | AMG157 | | AB-2`` followed by
    # ``| | IC50 | 0.2 | | 0.3``.
    for header_index, header in enumerate(rows[:3]):
        if not any(ENTITY_HEADER_RE.fullmatch(cell) for cell in header):
            continue
        entities = {
            column: _entity_info(cell, document_template)
            for column, cell in enumerate(header)
        }
        entities = {column: entity for column, entity in entities.items() if entity}
        if not entities:
            continue
        for assay_row_index in range(header_index + 1, min(len(rows), header_index + 4)):
            assay_row = rows[assay_row_index]
            endpoint_labels = [cell for cell in assay_row if ENDPOINT_RE.search(cell)]
            if not endpoint_labels:
                continue
            measurement_label = " · ".join(endpoint_labels)
            multiple = len(entities) > 1
            for column, entity in entities.items():
                if column >= len(assay_row):
                    continue
                add(
                    entity,
                    _cell_value(assay_row[column]),
                    assay_row_index,
                    col_index=column,
                    measurement_label=measurement_label,
                    multiple_measurements=multiple,
                )

    # Legacy repeated pairs: 抗体 | IC50 | 抗体 | IC50. Keep this fallback for
    # older patents whose entity header is not explicit.
    if not handled_entity_headers:
        for header_index, header in enumerate(rows):
            value_columns = [index for index, cell in enumerate(header) if ENDPOINT_RE.search(cell)]
            pairs = [(index - 1, index) for index in value_columns if index > 0]
            if not pairs:
                continue
            for row_index in range(header_index + 1, len(rows)):
                row = rows[row_index]
                for entity_column, value_column in pairs:
                    if max(entity_column, value_column) >= len(row):
                        continue
                    add(
                        _entity_info(row[entity_column], document_template),
                        _cell_value(row[value_column]),
                        row_index,
                        col_index=value_column,
                        measurement_label=header[value_column],
                        multiple_measurements=len(pairs) > 1,
                    )

    return observations


def _assay_profile(
    document_id: str,
    table: dict[str, Any],
    ordinal: int,
    document_template: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a stable assay identity and a readable label for one table.

    ``experiment_id`` is the semantic identity used for de-duplication.  It is
    deliberately separate from the user-facing label.  A parser or a patent
    template may supply either field directly; otherwise the source table is
    used so two biologically different experiments are never merged merely
    because their numeric values happen to be equal.
    """

    table_id = str(table.get("source_id") or table.get("image") or f"table-{ordinal}")
    adaptive = (
        document_template.get("assay_template", {}).get("table_profiles", {}).get(table_id, {})
        if document_template
        else {}
    )
    document_profiles = ASSAY_TEMPLATE.get("document_profiles", {})
    configured = document_profiles.get(document_id, {}).get(table_id, {})
    explicit_id = _text(table.get("experiment_id") or table.get("assay_id"))
    explicit_label = _text(table.get("experiment_group") or table.get("assay_name"))
    caption = _text(table.get("caption"))
    return {
        **adaptive,
        "experiment_id": (
            _text(adaptive.get("experiment_id") or adaptive.get("protocol_id"))
            or _text(configured.get("experiment_id"))
            or explicit_id
            or table_id
        ),
        "label": (
            _text(adaptive.get("label"))
            or _text(adaptive.get("caption"))
            or _text(configured.get("label"))
            or explicit_label
            or caption
            or f"experiment_{ordinal}"
        ),
    }


def _observation_key(record: dict[str, Any]) -> tuple[str, str, str, str, str, str]:
    """Identity of one measured result, excluding its evidence location."""

    return (
        str(record.get("document_id", "")),
        str(
            record.get("canonical_entity_key")
            or scope_entity_key(str(record.get("antibody_id", "")))
        ),
        str(
            record.get("measurement_id")
            or record.get("experiment_id")
            or record.get("experiment_group", "")
        ).casefold(),
        str(record.get("assay_endpoint", "IC50")).upper(),
        str(record.get("ic50_operator") or record.get("operator") or "="),
        f"{record.get('ic50')}|{str(record.get('ic50_unit', '')).casefold()}",
    )


def _merge_assay_evidence(target: dict[str, Any], source: dict[str, Any]) -> None:
    existing = target.setdefault("evidence", [])
    keys = {
        (
            str(item.get("type", "")),
            str(item.get("table_id", "")),
            item.get("row_index"),
            str(item.get("value", "")),
        )
        for item in existing
        if isinstance(item, dict)
    }
    for item in source.get("evidence", []):
        if not isinstance(item, dict) or item.get("type") != "assay_measurement":
            continue
        key = (
            str(item.get("type", "")),
            str(item.get("table_id", "")),
            item.get("row_index"),
            str(item.get("value", "")),
        )
        if key not in keys:
            existing.append(item)
            keys.add(key)

    table_ids = [
        value.strip()
        for value in str(target.get("assay_table_id") or "").split(",")
        if value.strip()
    ]
    source_table_id = str(source.get("assay_table_id") or "").strip()
    if source_table_id and source_table_id not in table_ids:
        table_ids.append(source_table_id)
    target["assay_table_id"] = ", ".join(table_ids) or None
    target["assay_evidence_count"] = sum(
        item.get("type") == "assay_measurement"
        for item in existing
        if isinstance(item, dict)
    )


def consolidate_duplicate_observations(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse exact duplicate observations while retaining every source box.

    Different experiment IDs or different values remain separate rows.  This
    prevents a repeated crop/page continuation from duplicating exported data
    without confusing genuinely different assays with duplicate evidence.
    """

    consolidated: list[dict[str, Any]] = []
    by_key: dict[tuple[str, str, str, str, str, str], dict[str, Any]] = {}
    for record in records:
        key = _observation_key(record)
        current = by_key.get(key)
        if current is None:
            record["assay_evidence_count"] = sum(
                item.get("type") == "assay_measurement"
                for item in record.get("evidence", [])
                if isinstance(item, dict)
            )
            by_key[key] = record
            consolidated.append(record)
            continue
        _merge_assay_evidence(current, record)
    return consolidated


def _sequence(text: str) -> str:
    """Join OCR lines from the sequence cell without silently correcting them."""

    # Table OCR separates columns with ``|`` and wrapped sequence lines with
    # ``<br>``. The final cell is the sequence cell for these patent tables;
    # taking it as a whole avoids returning only one 40-aa OCR fragment.
    if "|" in text:
        text = text.split("|")[-1]
    candidates: list[str] = []
    for part in re.split(r"<br>|\n", text, flags=re.IGNORECASE):
        compact = re.sub(r"[^A-Z0-9*?.-]", "", part.upper())
        if len(compact) < 20:
            continue
        canonical = sum(char in set("ACDEFGHIKLMNPQRSTVWY") for char in compact)
        if canonical / max(1, len(compact)) >= 0.65:
            candidates.append(compact)
    return "".join(candidates)


def _chain(text: str) -> str | None:
    upper = text.upper()
    if re.search(r"\bVH\b|\.H\b|\(VH\)", upper):
        return "VH"
    if re.search(r"\bVL\b|\.L\b|\(VL\)", upper):
        return "VL"
    return None


def _variable_region(sequence: str) -> str:
    upper = sequence.upper()
    positions = [upper.find(motif) for motif in CONSTANT_MOTIFS if upper.find(motif) > 20]
    return sequence[: min(positions)] if positions else sequence


def _plausible_chain(sequence: str, chain: str) -> bool:
    if not sequence:
        return False
    if chain == "VH":
        return 70 <= len(sequence) <= 160
    if chain == "VL":
        return 70 <= len(sequence) <= 140
    return False


def _base_record(document_id: str, entity: str, source: str, **extra: Any) -> dict[str, Any]:
    return {
        "document_id": document_id,
        "record_id": f"{normalize_entity(entity)}-{len(extra)}-{abs(hash(source)) % 100000}",
        "antibody_id": entity,
        "experiment_group": "",
        "vh": "",
        "vl": "",
        "ic50": None,
        "ic50_unit": ASSAY_TEMPLATE.get("default_unit", "nM"),
        "link_status": "review",
        "source": source,
        "sequence_table_id": None,
        "assay_table_id": None,
        "evidence": [],
        **extra,
    }


def _reference_records(document_id: str, path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for index, row in enumerate(csv.DictReader(handle), start=1):
            entity = _text(row.get("突变号") or row.get("antibody_id"))
            if not entity:
                continue
            group = _text(row.get("实验组") or row.get("experiment_group"))
            vh = _text(row.get("VH") or row.get("vh"))
            vl = _text(row.get("VL") or row.get("vl"))
            assay_columns = [
                (key, value)
                for key, value in row.items()
                if "ic50" in key.lower() or "ic50" in key.lower().replace("_", "")
            ]
            if not assay_columns:
                assay_columns = [("IC50", row.get("IC50", ""))]
            for assay_name, raw_value in assay_columns:
                value_match = NUMBER_RE.search(str(raw_value or ""))
                if not value_match:
                    continue
                record = _base_record(
                    document_id,
                    entity,
                    "reference_csv",
                    record_id=f"reference-{index}-{assay_name}",
                    experiment_group=group,
                    vh=vh,
                    vl=vl,
                    ic50=float(value_match.group(1)),
                    ic50_unit="nM",
                    link_status="reference",
                    evidence=[{"source_file": str(path), "row": index, "column": assay_name}],
                )
                records.append(record)
    return records


def _construct_keywords(
    sequence: dict[str, Any],
    document_template: dict[str, Any] | None,
    entity_name: str = "",
) -> list[dict[str, Any]]:
    """Return supported construct/chain labels without guessing a format."""

    detected = {
        str(item.get("key")): item
        for item in (document_template or {}).get("construct_template", {}).get("detected_types", [])
    }
    values: list[dict[str, Any]] = []

    def add(key: str, label_zh: str, label_en: str, confidence: float = 0.95) -> None:
        if any(item.get("key") == key for item in values):
            return
        values.append({
            "category": "construct",
            "key": key,
            "label_zh": label_zh,
            "label_en": label_en,
            "confidence": confidence,
        })

    if sequence.get("vh"):
        add("variable_heavy", "VH", "VH")
    if sequence.get("vl"):
        add("variable_light", "VL", "VL")
    if sequence.get("full_heavy_seq_id"):
        add("full_heavy_chain", "完整重链", "Full heavy chain")
    if sequence.get("full_light_seq_id"):
        add("full_light_chain", "完整轻链", "Full light chain")
    if (
        "full_length_antibody" in detected
        or (sequence.get("full_heavy_seq_id") and sequence.get("full_light_seq_id"))
        or re.search(r"(?:^|[-_\s])h?IgG[1-4]?(?:$|[-_\s])", entity_name, re.I)
    ):
        item = detected.get("full_length_antibody", {})
        add(
            "full_length_antibody",
            str(item.get("label_zh", "完整抗体")),
            str(item.get("label_en", "Full-length antibody")),
            float(item.get("confidence", 0.9)),
        )
    # Bispecific/Fab/scFv mentions can describe alternative embodiments in a
    # patent and must not be projected onto every measured construct. They stay
    # available in the document template until entity-level evidence exists.
    return values


def _ocr_records(
    document_id: str,
    ocr_results: list[dict[str, Any]],
    document_template: dict[str, Any] | None = None,
    sequence_sources: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    # Resolve the patent's explicit construct -> SEQ ID -> full sequence
    # index before reading assay rows.  This is the important distinction from
    # joining an IC50 row to whichever short OCR fragment happens to contain
    # the same antibody name.
    resolved_sequences = resolve_sequences(sequence_sources if sequence_sources is not None else ocr_results, document_template)
    assay_rows: list[dict[str, Any]] = []
    assay_table_number = 0
    for table in ocr_results:
        table_text = " ".join(
            _text(cell.get("text"))
            for row in table.get("rows", [])
            for cell in row
            if isinstance(cell, dict)
        )
        if not ENDPOINT_RE.search(table_text) and table.get("table_classification", {}).get("classifier", {}).get("mode") != "llm":
            continue
        # Preserve the established WO2026086808A1 dataset definition: its
        # target endpoint is TSLP/TSLPR blocking, not the neighbouring
        # IL7Rα/TSLPR control table. Other patents retain every IC50 table and
        # expose the table as a separate experiment group for review.
        if document_id == "WO2026086808A1" and not (
            re.search(r"tslp", table_text, re.I)
            and re.search(r"tslpr", table_text, re.I)
            and not re.search(r"il\s*7\s*r|il7r", table_text, re.I)
        ):
            continue
        assay_table_number += 1
        profile = _assay_profile(
            document_id, table, assay_table_number, document_template
        )
        assay_rows.extend(
            table_assay_rows(
                table,
                profile["label"],
                profile["experiment_id"],
                document_template,
                profile,
            )
        )
    records: list[dict[str, Any]] = []
    for record_index, row in enumerate(assay_rows):
        entity_key = str(row.get("entity_key") or scope_entity_key(
            row["entity"], document_template
        ))
        seq = resolved_sequences.get(entity_key, {})
        sequence_table_ids = seq.get("sequence_table_ids", [])
        supporting_sequence_table_ids = seq.get("supporting_sequence_table_ids", [])
        sequence_evidence = seq.get("sequence_rows", [])
        supporting_sequence_evidence = seq.get("supporting_sequence_rows", [])
        record_id = f"ocr-{record_index}-{row['table_id']}-{row['row_index']}-{row['ic50']}"
        if document_template and document_template.get("agent", {}).get("mode") == "llm":
            identity = "|".join(str(value) for value in (
                document_id, entity_key, row.get("value_ref"), row.get("condition_label"),
                row.get("ic50"), row.get("ic50_unit"), row.get("ic50_operator", "="),
            ))
            record_id = "llm-" + hashlib.sha256(identity.encode()).hexdigest()[:24]
        records.append(_base_record(
            document_id,
            row["entity"],
            "ocr",
            record_id=record_id,
            raw_antibody_id=row.get("raw_entity", row["entity"]),
            canonical_entity_key=entity_key,
            canonical_antibody_id=row.get("canonical_entity", row["entity"]),
            antibody_variant=row.get("entity_variant"),
            entity_role=row.get("entity_role", "antibody"),
            vh=seq.get("vh", ""),
            vl=seq.get("vl", ""),
            ic50=row["ic50"],
            ic50_operator=row.get("ic50_operator", "="),
            ic50_unit=row.get("ic50_unit", ASSAY_TEMPLATE.get("default_unit", "nM")),
            assay_table_id=row["table_id"],
            assay_region_id=row.get("region_id"),
            assay_binding_status=row.get("assay_binding_status", "unmatched"),
            assay_endpoint="IC50",
            assay_condition=row.get("condition_label", ""),
            assay_measurement_label=row.get("measurement_label", ""),
            assay_method=row.get("assay_method"),
            assay_system=row.get("assay_system"),
            species=row.get("species", []),
            donor_id=row.get("donor_id"),
            assay_keywords=row.get("assay_keywords", []),
            sequence_source_conflicts=seq.get("source_conflicts", []),
            antigen_target=row.get("antigen_target"),
            assay_keyword_status=row.get("assay_keyword_status"),
            assay_keyword_library=row.get("assay_keyword_library"),
            assay_keyword_unmatched=row.get("assay_keyword_unmatched", []),
            antibody_keywords=_construct_keywords(seq, document_template, row["entity"]),
            assay_protocol_id=row.get("assay_protocol_id"),
            assay_protocol_ids=row.get("assay_protocol_ids", []),
            assay_protocol_summary_zh=row.get("assay_protocol_summary_zh", ""),
            assay_protocol_summary_en=row.get("assay_protocol_summary_en", ""),
            assay_protocol_pages=row.get("assay_protocol_pages", []),
            assay_protocol_confidence=row.get("assay_protocol_confidence"),
            assay_protocol_match_reason=row.get("assay_protocol_match_reason"),
            sequence_table_id=", ".join(sequence_table_ids or supporting_sequence_table_ids) or None,
            experiment_group=row.get("experiment_group", ""),
            experiment_id=row.get("experiment_id", ""),
            measurement_id=row.get("measurement_id", ""),
            link_status=(
                "linked"
                if _plausible_chain(seq.get("vh", ""), "VH")
                and _plausible_chain(seq.get("vl", ""), "VL")
                else "review"
            ),
            evidence=[
                {
                    "type": "assay_measurement",
                    "endpoint": "IC50",
                    "value": row["ic50"],
                    "unit": row.get("ic50_unit", ASSAY_TEMPLATE.get("default_unit", "nM")),
                    "experiment_id": row.get("experiment_id", ""),
                    "measurement_id": row.get("measurement_id", ""),
                    "experiment_group": row.get("experiment_group", ""),
                    "donor_id": row.get("donor_id"),
                    "species": row.get("species", []),
                    "assay_method": row.get("assay_method"),
                    "assay_system": row.get("assay_system"),
                    "image": row["image"],
                    "row_index": row["row_index"],
                    "table_id": row["table_id"],
                    "col_index": row.get("col_index"),
                    "value_ref": row.get("value_ref"),
                    "header_refs": row.get("header_refs", []),
                    "unit_refs": row.get("unit_refs", []),
                    "entity_ref": row.get("entity_ref"),
                    "raw_entity": row.get("raw_entity", row["entity"]),
                    "region_id": row.get("region_id"),
                    "condition_refs": row.get("condition_refs", []),
                    "condition_label": row.get("condition_label", ""),
                    "operator": row.get("ic50_operator", "="),
                },
                {"type": "sequence_index", "construct_key": entity_key,
                 "vh_seq_id": seq.get("vh_seq_id"), "vl_seq_id": seq.get("vl_seq_id"),
                 "resolution": seq.get("sequence_resolution", {}),
                 "rows": sequence_evidence},
                {"type": "sequence_support", "construct_key": entity_key,
                 "full_heavy_seq_id": seq.get("full_heavy_seq_id"),
                 "full_light_seq_id": seq.get("full_light_seq_id"),
                 "constant_region_seq_ids": seq.get("constant_region_seq_ids", {}),
                 "cdr_seq_ids": seq.get("cdr_seq_ids", {}),
                 "rows": supporting_sequence_evidence},
                {
                    "type": "assay_protocol",
                    "protocol_id": row.get("assay_protocol_id"),
                    "pages": row.get("assay_protocol_pages", []),
                    "confidence": row.get("assay_protocol_confidence"),
                    "match_reason": row.get("assay_protocol_match_reason"),
                    "summary_zh": row.get("assay_protocol_summary_zh", ""),
                    "summary_en": row.get("assay_protocol_summary_en", ""),
                    "evidence": row.get("assay_protocol_evidence", []),
                },
            ],
        ))
    return consolidate_duplicate_observations(records)


def link_dataset(
    document_id: str,
    ocr_results: list[dict[str, Any]],
    reference_csv: str | None = None,
    reference_entities_only: bool = False,
    document_template: dict[str, Any] | None = None,
    *,
    sequence_sources: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    records = _ocr_records(document_id, ocr_results, document_template, sequence_sources)
    if reference_csv and Path(reference_csv).is_file():
        reference_records = _reference_records(document_id, Path(reference_csv))
        if reference_entities_only:
            allowed = {
                scope_entity_key(record["antibody_id"], document_template)
                for record in reference_records
            }
            records = [
                record
                for record in records
                if scope_entity_key(record["antibody_id"], document_template) in allowed
            ]
        else:
            records.extend(reference_records)
    return records
