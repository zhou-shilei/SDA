"""Resolve construct names to VH/VL sequences through patent SEQ IDs.

Patent sequence tables are usually not keyed by the assay row.  They contain
an index table (construct -> SEQ ID NO) followed by sequence definitions,
often wrapped over several OCR rows and pages.  This module keeps that
structure explicit instead of trying to join a short OCR fragment directly
to an IC50 value.
"""

from __future__ import annotations

import html
import re
from collections import Counter
from dataclasses import dataclass, field as dataclass_field
from typing import Any

from .assays import load_semantic_template
from .template_agent import expand_entity_names


AA = set("ACDEFGHIKLMNPQRSTVWY")
SEMANTIC_TEMPLATE = load_semantic_template()
SEQUENCE_TEMPLATE = SEMANTIC_TEMPLATE["sequence_definition"]
MAPPING_TEMPLATE = SEMANTIC_TEMPLATE["construct_mapping"]
SEQ_ID_RE = re.compile(SEQUENCE_TEMPLATE["seq_id_pattern"], re.I)
SEQ_ID_VALUE_RE = re.compile(MAPPING_TEMPLATE["seq_id_value_pattern"], re.I)
FASTA_CHAIN_RES = [
    re.compile(pattern, re.I)
    for pattern in SEQUENCE_TEMPLATE.get("label_patterns", [])
]
SCOPE_RES = {
    scope: [re.compile(pattern, re.I) for pattern in patterns]
    for scope, patterns in SEQUENCE_TEMPLATE.get("scope_patterns", {}).items()
}
VARIANT_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:H\d+(?:L\d+)+|[A-Za-z]{1,12}\d+(?:-\d+)+|(?:AMG|hu|ch)\d+)(?![A-Za-z0-9])",
    re.I,
)
LABELED_CHAIN_RE = re.compile(
    r"(?P<name>(?:[A-Za-z]{1,12}\d+(?:-\d+)+|(?:AMG|hu|ch)\d+))"
    r"[^|\n]{0,36}?(?P<chain>重链|轻链|heavy\s*chain|light\s*chain)",
    re.I,
)
CDR_LABEL_RE = re.compile(
    r"\b(?P<prefix>[HL])\s*CDR\s*(?P<number>[123])(?P<suffix>[^|<]*)",
    re.I,
)

# Full antibody chains in the source patents include their constant regions.
# The deliberately conservative lower bounds keep variable-only and
# constant-only definitions out of the construct resolver.  They are also
# used to distinguish a previous record's page-break tail from the prefix of
# the next record.
FULL_LENGTH = {"VL": (190, 260), "VH": (420, 520)}
CONSTANT_MOTIFS = {"VH": ("ASTKGPS",), "VL": ("GQPKAAP", "QANKAT", "RTVAAP")}
NAMED_SEQ_ID_RE = re.compile(r"^(?:(?:SEQ\s*ID\s*)?NO\s*:?[\s]*)?(\d+)$", re.I)
TEZEPELUMAB_EXPERIMENT_RE = re.compile(
    r"\b(?:tezepelumab|tezepumab)\s+(?P<variant>\d+(?:-\d+)+)\b",
    re.I,
)
VARIABLE_LENGTH = {"VH": (80, 160), "VL": (80, 140)}


def clean_text(value: Any) -> str:
    return html.unescape(str(value or "")).replace("\u00a0", " ").strip()


def construct_keys(value: str) -> list[str]:
    """Return normalized construct keys found in an OCR label.

    The base antibody is ``tezepumab``; mutation labels are represented by
    their experiment names (``h0l10``, ``s3-5``, ``3-3``).  This deliberately
    ignores mutation notation such as ``G23S``.
    """

    text = re.sub(r"<br\s*/?>", " ", clean_text(value), flags=re.I).lower().replace("tezepulumab", "tezepelumab")
    numbered = re.findall(r"\b(?:antibody|construct)\s+\d+[a-z]?(?![a-z0-9])", text, re.I)
    if numbered:
        return list(dict.fromkeys(re.sub(r"\s+", "", name) for name in numbered))
    experiment = TEZEPELUMAB_EXPERIMENT_RE.search(text)
    if experiment:
        return [experiment.group("variant").lower()]
    keys = [token.lower() for token in VARIANT_RE.findall(text)]
    if keys:
        return list(dict.fromkeys(keys))
    if "tezepelumab" in text or "tezepumab" in text:
        return ["tezepumab"]
    if "csj117" in text:
        return ["csj117"]
    return []


def row_text(row: list[dict[str, Any]]) -> str:
    return " | ".join(clean_text(cell.get("text")) for cell in row if isinstance(cell, dict))


def sequence_ids(text: str) -> list[int]:
    return [int(value) for value in SEQ_ID_RE.findall(text)]


def chain_from_text(text: str) -> str | None:
    upper = clean_text(text).upper()
    if re.search(r"(?:\.H\b|\bVH\b|\bHCDR\s*[123]\b|\bHEAVY\s+CHAIN\b|重链)", upper):
        return "VH"
    if re.search(r"(?:\.L\b|\bVL\b|\bLCDR\s*[123]\b|\bLIGHT\s+CHAIN\b|轻链)", upper):
        return "VL"
    return None


def amino_acid_chunks(text: str, *, min_length: int = 20, correct_ocr: bool = True) -> list[str]:
    """Extract long amino-acid-like runs, retaining OCR provenance upstream."""

    text = clean_text(text).upper()
    text = SEQ_ID_RE.sub(" ", text)
    chunks: list[str] = []
    for run in re.findall(rf"[A-Z]{{{max(5, min_length)},}}", text):
        canonical = sum(char in AA for char in run)
        if len(run) >= min_length and canonical / max(1, len(run)) >= 0.75:
            # O is not a standard antibody amino acid and is a common OCR
            # confusion with Q in this document (e.g. DIWGOGT -> DIWGQGT).
            chunks.append(run.replace("O", "Q") if correct_ocr else run)
    return chunks


def sequence_scope(label: str, chain: str, sequence: str = "") -> str:
    """Classify sequence scope independently from its VH/VL chain type."""

    normalized = clean_text(label)
    for scope in (
        "constant_region",
        "heavy_cdr",
        "light_cdr",
        "variable_heavy",
        "variable_light",
        "full_heavy_chain",
        "full_light_chain",
    ):
        if any(pattern.search(normalized) for pattern in SCOPE_RES.get(scope, [])):
            return scope

    upper = sequence.upper()
    if any(upper.startswith(motif) for motif in CONSTANT_MOTIFS.get(chain, ())):
        return "constant_region"
    if any(upper.find(motif) > 20 for motif in CONSTANT_MOTIFS.get(chain, ())):
        return "full_heavy_chain" if chain == "VH" else "full_light_chain"
    return "variable_heavy" if chain == "VH" else "variable_light"


def variable_region(full_sequence: str, chain: str) -> str:
    sequence = full_sequence.upper()
    motifs = CONSTANT_MOTIFS.get(chain, ())
    positions = [sequence.find(motif) for motif in motifs if sequence.find(motif) > 20]
    return sequence[: min(positions)] if positions else sequence


def _chain_from_full_sequence(sequence: str) -> str | None:
    """Infer a chain only from well-known variable/constant-region evidence."""

    upper = sequence.upper()
    if any(upper.find(motif) > 20 for motif in CONSTANT_MOTIFS["VH"]):
        return "VH"
    if any(upper.find(motif) > 20 for motif in CONSTANT_MOTIFS["VL"]):
        return "VL"
    if upper.startswith(("EVQL", "QVQL", "QMQL", "DVQL", "VQLV", "QLQL")):
        return "VH"
    if upper.startswith(("DIQ", "DIV", "EIV", "QSV", "QAV", "SYE", "SYV", "AIQ", "ETV")):
        return "VL"
    return None


def _named_sequence_tables(
    ocr_results: list[dict[str, Any]],
) -> list[tuple[dict[str, Any], int, int, int, int]]:
    tables: list[tuple[dict[str, Any], int, int, int, int]] = []
    for table in sorted(ocr_results, key=lambda item: item.get("table_index", 0)):
        rows = table.get("rows", [])
        for header_index, row in enumerate(rows[:3]):
            cells = [clean_text(cell.get("text")) for cell in row if isinstance(cell, dict)]
            compact = [re.sub(r"\s+", "", cell).lower() for cell in cells]
            name_col = next((i for i, cell in enumerate(compact) if cell in {"name", "名称"}), None)
            seq_col = next((i for i, cell in enumerate(compact) if "seqid" in cell or "序列编号" in cell), None)
            amino_col = next((i for i, cell in enumerate(compact) if "aminoacidsequence" in cell or "氨基酸序列" in cell), None)
            if name_col is not None and seq_col is not None and amino_col is not None:
                tables.append((table, header_index, name_col, seq_col, amino_col))
                break
    return tables


def parse_named_sequence_chains(
    ocr_results: list[dict[str, Any]],
    document_template: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Read ``Name | SEQ ID | Amino Acid Sequence`` document templates.

    Unlike the legacy resolver, this layout can name a full heavy-chain
    variant (AB-2a/AB-2b) separately from a shared light chain (AB-2).  The
    document template supplies the canonical parent key while raw labels and
    full-chain variants remain in the evidence.
    """

    if not document_template:
        return []
    definitions: list[dict[str, Any]] = []
    active_group: list[dict[str, Any]] = []
    for table, header_index, name_col, seq_col, amino_col in _named_sequence_tables(ocr_results):
        table_id = str(table.get("source_id") or table.get("image") or "unknown")
        rows = table.get("rows", [])
        for row_index in range(header_index + 1, len(rows)):
            cells = [clean_text(cell.get("text")) for cell in rows[row_index] if isinstance(cell, dict)]
            sequence_text = cells[amino_col] if amino_col < len(cells) else ""
            chunks = amino_acid_chunks(sequence_text, min_length=10)
            sequence = "".join(chunks)
            raw_name = cells[name_col] if name_col < len(cells) else ""
            raw_seq_id = cells[seq_col] if seq_col < len(cells) else ""

            # A continuation row at the top of a new page can complete the
            # preceding named definition without repeating its name/SEQ ID.
            if not raw_name and not raw_seq_id and sequence and active_group:
                for prior in active_group:
                    low, _ = FULL_LENGTH.get(prior["chain"], (0, 0))
                    if len(prior["full_sequence"]) >= low:
                        continue
                    prior["full_sequence"] += sequence
                    prior["variable_sequence"] = variable_region(
                        prior["full_sequence"], prior["chain"]
                    )
                    prior["table_ids"].append(table_id)
                    prior["rows"].append(row_index)
                continue

            entities = expand_entity_names(raw_name, document_template)
            seq_match = NAMED_SEQ_ID_RE.fullmatch(raw_seq_id)
            chain = _chain_from_full_sequence(sequence)
            if not entities or not seq_match or not chain or not sequence:
                active_group = []
                continue
            seq_id = int(seq_match.group(1))
            active_group = []
            for entity in entities:
                definition = {
                    "construct_key": entity["canonical_key"],
                    "raw_name": entity["raw_name"],
                    "canonical_name": entity["canonical_name"],
                    "variant_suffix": entity.get("variant_suffix"),
                    "entity_role": entity.get("entity_role", "antibody"),
                    "seq_id": seq_id,
                    "chain": chain,
                    "full_sequence": sequence,
                    "variable_sequence": variable_region(sequence, chain),
                    "sequence_scope": sequence_scope(raw_name, chain, sequence),
                    "table_ids": [table_id],
                    "rows": [row_index],
                }
                definitions.append(definition)
                active_group.append(definition)
    return definitions


def resolve_named_sequences(
    ocr_results: list[dict[str, Any]],
    document_template: dict[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    definitions = parse_named_sequence_chains(ocr_results, document_template)
    grouped: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for definition in definitions:
        grouped.setdefault(definition["construct_key"], {"VH": [], "VL": []})[
            definition["chain"]
        ].append(definition)

    resolved: dict[str, dict[str, Any]] = {}
    for key, chains in grouped.items():
        result: dict[str, Any] = {
            "construct_key": key,
            "vh": "",
            "vl": "",
            "vh_seq_id": None,
            "vl_seq_id": None,
            "sequence_table_ids": [],
            "sequence_rows": [],
            "sequence_resolution": {},
            "supporting_sequence_table_ids": [],
            "supporting_sequence_rows": [],
            "full_heavy_seq_id": None,
            "full_light_seq_id": None,
            "constant_region_seq_ids": {"VH": [], "VL": []},
            "cdr_seq_ids": {},
            "mapping_evidence": [],
            "named_sequence_variants": [],
            "ambiguities": [],
            "status": "review",
        }
        for chain, field, seq_id_field in (
            ("VH", "vh", "vh_seq_id"),
            ("VL", "vl", "vl_seq_id"),
        ):
            items = chains[chain]
            if not items:
                continue
            counts = Counter(item["variable_sequence"] for item in items)
            selected_sequence, selected_count = counts.most_common(1)[0]
            selected = sorted(
                (item for item in items if item["variable_sequence"] == selected_sequence),
                key=lambda item: (
                    item.get("variant_suffix") not in {None, "a"},
                    item.get("variant_suffix") is not None,
                    item["seq_id"],
                ),
            )[0]
            result[field] = selected_sequence
            result[seq_id_field] = selected["seq_id"]
            result["sequence_resolution"][field] = {
                "source_scope": selected["sequence_scope"],
                "seq_id": selected["seq_id"],
                "resolution": "document_template_named_sequence",
                "support_count": selected_count,
            }
            result["sequence_table_ids"].extend(selected["table_ids"])
            result["sequence_rows"].extend({
                "table_id": table_id,
                "row_index": row,
                "seq_id": selected["seq_id"],
                "chain": chain,
                "sequence_scope": selected["sequence_scope"],
                "evidence_role": "primary",
                "raw_name": selected["raw_name"],
            } for table_id, row in zip(selected["table_ids"], selected["rows"]))
            result["mapping_evidence"].append({
                "construct_key": key,
                "assignments": {chain: selected["seq_id"]},
                "table_id": selected["table_ids"][0],
                "row_index": selected["rows"][0],
                "raw_cells": [selected["raw_name"], f"NO:{selected['seq_id']}"] ,
                "header_cells": ["Name", "SEQ ID", "Amino Acid Sequence"],
                "header_table_id": selected["table_ids"][0],
                "header_mapping_status": "document_template_direct_name",
                "confidence": 0.95 if len(counts) == 1 else 0.75,
            })
            if len(counts) > 1:
                result["ambiguities"].append({
                    "chain": chain,
                    "unique_variable_sequences": len(counts),
                    "selected_support_count": selected_count,
                    "seq_ids": [item["seq_id"] for item in items],
                })
            for item in items:
                result["named_sequence_variants"].append({
                    "raw_name": item["raw_name"],
                    "variant_suffix": item.get("variant_suffix"),
                    "seq_id": item["seq_id"],
                    "chain": chain,
                    "full_sequence": item["full_sequence"],
                    "variable_sequence": item["variable_sequence"],
                    "table_ids": item["table_ids"],
                    "rows": item["rows"],
                })
                if item is selected:
                    continue
                result["supporting_sequence_table_ids"].extend(item["table_ids"])
                result["supporting_sequence_rows"].extend({
                    "table_id": table_id,
                    "row_index": row,
                    "seq_id": item["seq_id"],
                    "chain": chain,
                    "sequence_scope": item["sequence_scope"],
                    "evidence_role": "variant_support",
                    "raw_name": item["raw_name"],
                } for table_id, row in zip(item["table_ids"], item["rows"]))
        result["sequence_table_ids"] = list(dict.fromkeys(result["sequence_table_ids"]))
        result["supporting_sequence_table_ids"] = list(dict.fromkeys(
            result["supporting_sequence_table_ids"]
        ))
        result["status"] = (
            "linked" if result["vh"] and result["vl"] and not result["ambiguities"]
            else "review"
        )
        resolved[key] = result
    return resolved


@dataclass
class SequenceDefinition:
    seq_id: int
    chain: str
    full_sequence: str
    variable_sequence: str
    sequence_scope: str
    table_ids: list[str] = dataclass_field(default_factory=list)
    rows: list[int] = dataclass_field(default_factory=list)
    label: str = ""
    label_table_id: str | None = None
    label_row_index: int | None = None


def _column_sequence_layouts(table: dict[str, Any]) -> list[tuple[int, int, int, int | None, str | None]]:
    """Read explicit definition columns, including paired VH/VL sequence tables.

    Each layout is (header row, ID column, residues column, label column, chain).
    Bare integers acquire SEQ-ID semantics only beneath an explicit ID header.
    """
    for ri, row in enumerate(table.get("rows", [])[:3]):
        cells = [re.sub(r"\s+", "", re.sub(r"<br\s*/?>", " ", clean_text(c.get("text")), flags=re.I)).lower() for c in row]
        ids = [i for i, text in enumerate(cells) if re.fullmatch(r"(?:seq(?:uence)?id(?:no[.:：]?)?|序列编号|序列号)", text)]
        residues = [i for i, text in enumerate(cells) if text in {"sequence", "aminoacidsequence", "氨基酸序列"}]
        labels = [i for i, text in enumerate(cells) if text in {"description", "name", "名称", "描述", "antibody", "construct", "抗体"}]
        if len(ids) == len(residues) == 1 and ids[0] != residues[0]:
            return [(ri, ids[0], residues[0], labels[0] if labels else None, None)]
        # Antibody | SEQ ID NO | VH | SEQ ID NO | VL. Keep each
        # identifier attached to its own adjacent chain, not integer order.
        paired = [(ri, ci - 1, ci, labels[0], cells[ci].upper())
                  for ci in range(1, len(cells))
                  if labels and cells[ci] in {"vh", "vl"} and ci - 1 in ids]
        if paired:
            return paired
    return []


def _residue_cell(value: Any) -> str:
    value = re.sub(r"\s+", "", re.sub(r"<br\s*/?>", "", clean_text(value), flags=re.I))
    # Preserve every residue, including invalid OCR characters for later
    # validation. Never discard short wrapped tails or silently replace O/Q.
    if re.fullmatch(r"[A-Z*]{5,}", value) and sum(c in AA for c in value) / len(value) >= 0.9:
        return value
    return ""


def _parse_column_sequence_definitions(ocr_results: list[dict[str, Any]]) -> tuple[dict[int, SequenceDefinition], set[int]]:
    candidates: list[SequenceDefinition] = []
    active: dict[tuple[int, int], SequenceDefinition] = {}
    previous_page: int | None = None
    for table in sorted(ocr_results, key=lambda item: item.get("table_index", 0)):
        layouts = _column_sequence_layouts(table)
        page = table.get("page")
        if not layouts or page is None or previous_page is None or page not in {previous_page, previous_page + 1}:
            active = {}
        identity = str(table.get("source_id") or table.get("image") or "unknown")
        for header, id_col, seq_col, label_col, header_chain in layouts:
            key = (id_col, seq_col)
            for ri in range(header + 1, len(table.get("rows", []))):
                row = table["rows"][ri]
                if max(id_col, seq_col, label_col or 0) >= len(row):
                    active.pop(key, None)
                    continue
                sequence = _residue_cell(row[seq_col].get("text"))
                id_text = re.sub(r"<br\s*/?>", " ", clean_text(row[id_col].get("text")), flags=re.I)
                label = re.sub(r"<br\s*/?>", " ", clean_text(row[label_col].get("text")), flags=re.I) if label_col is not None else ""
                match = SEQ_ID_VALUE_RE.fullmatch(id_text)
                chain = header_chain or chain_from_text(label)
                if match and sequence and chain:
                    item = SequenceDefinition(int(match[1]), chain, sequence, "", sequence_scope(label, chain, sequence),
                                              [identity], [ri], label, identity, ri)
                    candidates.append(item)
                    active[key] = item
                elif not id_text and not label and sequence and key in active:
                    item = active[key]
                    item.full_sequence += sequence
                    item.table_ids.append(identity)
                    item.rows.append(ri)
                else:
                    active.pop(key, None)
        previous_page = page
    definitions: dict[int, SequenceDefinition] = {}
    conflicts: set[int] = set()
    for item in candidates:
        item.variable_sequence = variable_region(item.full_sequence, item.chain)
        prior = definitions.get(item.seq_id)
        if prior and (prior.full_sequence != item.full_sequence or prior.chain != item.chain):
            conflicts.add(item.seq_id)
        elif prior:
            prior.table_ids.extend(item.table_ids)
            prior.rows.extend(item.rows)
        else:
            definitions[item.seq_id] = item
    return {key: item for key, item in definitions.items() if key not in conflicts}, conflicts


def _expected_complete(sequence: SequenceDefinition | None) -> bool:
    if not sequence:
        return False
    low, high = FULL_LENGTH.get(sequence.chain, (0, 0))
    return low <= len(sequence.full_sequence) <= high


def _can_accept_tail(sequence: SequenceDefinition | None) -> bool:
    """Whether unlabeled rows can safely be appended to this definition.

    A constant-region-only definition may be shorter than a full chain, but
    appending the next page's variable prefix would make its length appear
    valid.  Constant motifs at the beginning make that case explicit.
    """

    if not sequence or _expected_complete(sequence):
        return False
    upper = sequence.full_sequence.upper()
    return not any(upper.startswith(motif) for motif in CONSTANT_MOTIFS.get(sequence.chain, ()))


def _is_sequence_definition_table(table: dict[str, Any]) -> bool:
    if any(layout[4] is None for layout in _column_sequence_layouts(table)):
        return True
    header = row_text(table.get("rows", [[]])[0] if table.get("rows") else [])
    normalized = re.sub(r"\s+", "", header).lower()
    return bool(
        ("序列编号" in normalized and "氨基酸序列" in normalized)
        or ("sequenceid" in normalized and "aminoacidsequence" in normalized)
    )


def _parse_labeled_sequence_definitions(
    ocr_results: list[dict[str, Any]],
    *, correct_ocr: bool = True,
) -> dict[int, SequenceDefinition]:
    """Parse prose-style ``construct + chain`` listings terminated by a SEQ ID.

    Chinese patent sequence listings often place the construct label before
    the sequence and the SEQ ID after it, sometimes across a page break. They
    are not grid tables, so the header-based parser intentionally misses them.
    """

    definitions: dict[int, SequenceDefinition] = {}
    label = ""
    chain: str | None = None
    chunks: list[str] = []
    table_ids: list[str] = []
    rows: list[int] = []

    def finish(seq_id: int) -> None:
        nonlocal label, chain, chunks, table_ids, rows
        if not label or not chain or not chunks:
            return
        full_sequence = "".join(chunks)
        definition = SequenceDefinition(
            seq_id=seq_id,
            chain=chain,
            full_sequence=full_sequence,
            variable_sequence=variable_region(full_sequence, chain),
            sequence_scope=sequence_scope(label, chain, full_sequence),
            table_ids=list(table_ids),
            rows=list(rows),
            label=label,
            label_table_id=table_ids[0] if table_ids else None,
            label_row_index=rows[0] if rows else None,
        )
        variable_bounds = SEQUENCE_TEMPLATE.get("variable_length", {})
        configured = variable_bounds.get(chain, [80, 520] if chain == "VH" else [70, 260])
        low, high = int(configured[0]), max(int(configured[1]), FULL_LENGTH[chain][1])
        if low <= len(full_sequence) <= high:
            definitions[seq_id] = definition
        label = ""
        chain = None
        chunks = []
        table_ids = []
        rows = []

    def fasta_label(row: list[dict[str, Any]], text: str) -> re.Match[str] | None:
        values = [
            clean_text(cell.get("text"))
            for cell in row
            if isinstance(cell, dict) and clean_text(cell.get("text"))
        ]
        values.append(text)
        for value in values:
            for pattern in FASTA_CHAIN_RES:
                match = pattern.search(value)
                if match:
                    return match
        return None

    for table in sorted(ocr_results, key=lambda item: item.get("table_index", 0)):
        table_id = str(table.get("source_id") or table.get("image") or "unknown")
        for row_index, row in enumerate(table.get("rows", [])):
            text = row_text(row)
            label_match = LABELED_CHAIN_RE.search(text)
            fasta_match = fasta_label(row, text)
            constant_label = (
                text
                if text.lstrip().startswith(">")
                and any(
                    pattern.search(text)
                    for pattern in SCOPE_RES.get("constant_region", [])
                )
                and chain_from_text(text)
                else None
            )
            started_label = bool(label_match or fasta_match or constant_label)
            if started_label:
                # A new explicit label starts a new definition. An unfinished
                # prior label is left unresolved rather than joined silently.
                if fasta_match:
                    label = fasta_match.group(0)
                    chain = fasta_match.group("chain").upper()
                elif constant_label:
                    label = constant_label
                    chain = chain_from_text(constant_label)
                else:
                    assert label_match is not None
                    label = label_match.group(0)
                    chain = "VH" if re.search(r"重链|heavy", label_match.group("chain"), re.I) else "VL"
                chunks = []
                table_ids = []
                rows = []

            if label and chain and not started_label:
                # Explicit sequence blocks can end with a short 5-19 aa tail.
                # The relaxed threshold stays scoped to a recognized label.
                for chunk in amino_acid_chunks(text, min_length=5, correct_ocr=correct_ocr):
                    chunks.append(chunk)
                    table_ids.append(table_id)
                    rows.append(row_index)

                ids = sequence_ids(text)
                if ids and chunks:
                    finish(ids[0])

    return definitions


def parse_sequence_definitions(ocr_results: list[dict[str, Any]], *, correct_ocr: bool = True) -> dict[int, SequenceDefinition]:
    """Parse full-chain definitions across wrapped OCR rows and pages.

    Definition tables are recognized by their headers instead of by a
    patent-specific SEQ-ID range.  Short CDR and constant-region definitions
    are excluded later by the full-chain length check.
    """

    definitions: dict[int, SequenceDefinition] = {}
    current: SequenceDefinition | None = None
    pending_chunks: list[str] = []
    pending_table_ids: list[str] = []
    pending_rows: list[int] = []

    tables = [
        table
        for table in sorted(ocr_results, key=lambda item: item.get("table_index", 0))
        if _is_sequence_definition_table(table)
    ]
    for table in tables:
        table_id = str(table.get("source_id") or table.get("image") or "unknown")
        for row_index, row in enumerate(table.get("rows", [])):
            text = row_text(row)
            ids = sequence_ids(text)
            chunks = amino_acid_chunks(text, correct_ocr=correct_ocr)
            if not chunks:
                continue

            if ids:
                seq_id = ids[0]
                chain = chain_from_text(text)
                if chain is None:
                    chain = "VH" if seq_id in {2, 5, 6, 8, 10, 11, 12, 13, 14, 15, 17, 19, 21} else "VL"

                # Blank OCR rows before a label are often the variable-region
                # prefix of the new sequence (IDs 1 and 2).  Blank rows after
                # an incomplete definition are the tail of the old sequence
                # (IDs 5, 15).  Full-length bounds distinguish the two cases.
                if current and pending_chunks and _can_accept_tail(current):
                    current.full_sequence += "".join(pending_chunks)
                    current.table_ids.extend(pending_table_ids)
                    current.rows.extend(pending_rows)
                    current.variable_sequence = variable_region(current.full_sequence, current.chain)
                    pending_chunks = []
                    pending_table_ids = []
                    pending_rows = []
                prefix = "".join(pending_chunks)
                prefix_tables = list(pending_table_ids)
                prefix_rows = list(pending_rows)
                pending_chunks = []
                pending_table_ids = []
                pending_rows = []

                current = SequenceDefinition(
                    seq_id=seq_id,
                    chain=chain,
                    full_sequence=prefix + "".join(chunks),
                    variable_sequence="",
                    sequence_scope="variable_heavy" if chain == "VH" else "variable_light",
                    table_ids=prefix_tables + [table_id],
                    rows=prefix_rows + [row_index],
                    label=text[:180],
                    label_table_id=table_id,
                    label_row_index=row_index,
                )
                current.variable_sequence = variable_region(current.full_sequence, current.chain)
                current.sequence_scope = sequence_scope(
                    current.label, current.chain, current.full_sequence
                )
                definitions[seq_id] = current
            else:
                # Delay assignment until the next explicit SEQ ID so that a
                # page-break prefix can be assigned to the correct sequence.
                pending_chunks.extend(chunks)
                pending_table_ids.append(table_id)
                pending_rows.append(row_index)

    if current and pending_chunks and not _expected_complete(current):
        current.full_sequence += "".join(pending_chunks)
        current.table_ids.extend(pending_table_ids)
        current.rows.extend(pending_rows)
        current.variable_sequence = variable_region(current.full_sequence, current.chain)
        current.sequence_scope = sequence_scope(
            current.label, current.chain, current.full_sequence
        )
        definitions[current.seq_id] = current

    labeled_definitions = _parse_labeled_sequence_definitions(ocr_results, correct_ocr=correct_ocr)
    definitions.update(labeled_definitions)

    # Keep only definitions that look like complete full-chain records.  The
    # caller will mark any missing ID as review rather than hallucinating it.
    complete = {
        seq_id: definition
        for seq_id, definition in definitions.items()
        if _expected_complete(definition) or seq_id in labeled_definitions
    }
    column_definitions, conflicts = _parse_column_sequence_definitions(ocr_results)
    complete.update(column_definitions)
    return {key: item for key, item in complete.items() if key not in conflicts}


def _is_cdr_table(table: dict[str, Any]) -> bool:
    text = " ".join(row_text(row) for row in table.get("rows", []))
    return bool(re.search(r"\bH\s*CDR\s*[123]\b|\bL\s*CDR\s*[123]\b", text, re.I))


def _cdr_sequence(value: str) -> str:
    """Return the short amino-acid fragment preceding a CDR SEQ ID."""

    text = clean_text(value)
    match = SEQ_ID_RE.search(text)
    if match:
        text = text[: match.start()]
    text = re.sub(r"<br\s*/?>", "", text, flags=re.I).upper()
    sequence = re.sub(r"[^ACDEFGHIKLMNPQRSTVWYX]", "", text)
    return sequence if len(sequence) >= 3 else ""


def parse_cdr_evidence(ocr_results: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Parse construct-specific HCDR/LCDR rows as supporting evidence.

    CDR SEQ IDs describe short fragments, not VH/VL definitions.  They are
    retained with exact row provenance but deliberately excluded from the
    construct-to-variable-chain index.
    """

    by_construct: dict[str, list[dict[str, Any]]] = {}
    for table in sorted(ocr_results, key=lambda item: item.get("table_index", 0)):
        if not _is_cdr_table(table):
            continue
        table_id = str(table.get("source_id") or table.get("image") or "unknown")
        active_construct: str | None = None
        for row_index, row in enumerate(table.get("rows", [])):
            cells = [clean_text(cell.get("text")) for cell in row if isinstance(cell, dict)]
            if not cells:
                continue
            first_cell_keys = construct_keys(cells[0]) if not CDR_LABEL_RE.search(cells[0]) else []
            if first_cell_keys:
                active_construct = first_cell_keys[0]
            if not active_construct:
                continue
            for col_index, label_text in enumerate(cells):
                label_match = CDR_LABEL_RE.search(label_text)
                if not label_match or col_index + 1 >= len(cells):
                    continue
                value_text = cells[col_index + 1]
                ids = sequence_ids(value_text)
                sequence = _cdr_sequence(value_text)
                if not ids or not sequence:
                    continue
                prefix = label_match.group("prefix").upper()
                cdr_number = int(label_match.group("number"))
                cdr_name = re.sub(r"\s+", "", label_match.group(0)).upper()
                by_construct.setdefault(active_construct, []).append(
                    {
                        "table_id": table_id,
                        "row_index": row_index,
                        "col_index": col_index + 1,
                        "seq_id": ids[0],
                        "chain": "VH" if prefix == "H" else "VL",
                        "sequence_scope": "heavy_cdr" if prefix == "H" else "light_cdr",
                        "evidence_role": "supporting",
                        "cdr_name": cdr_name,
                        "cdr_number": cdr_number,
                        "sequence": sequence,
                    }
                )
    return by_construct


def _index_header(row: list[dict[str, Any]]) -> tuple[list[str], list[str]] | None:
    chains: list[str] = []
    raw_cells: list[str] = []
    for cell in row:
        text = clean_text(cell.get("text")) if isinstance(cell, dict) else ""
        raw_cells.append(text)
        compact = re.sub(r"\s+", "", text).lower()
        matched_chain = None
        for candidate in ("VL", "VH"):
            aliases = MAPPING_TEMPLATE.get("headers", {}).get(candidate, [])
            if any(re.sub(r"\s+", "", alias).lower() in compact for alias in aliases):
                matched_chain = candidate
                break
        if matched_chain:
            chains.append(matched_chain)
    header_text = " ".join(raw_cells)
    has_sequence_index_semantics = bool(
        re.search(r"\bVH\b|\bVL\b|序列号|SEQ\s*ID", header_text, re.I)
    )
    has_construct_semantics = bool(
        re.search(r"抗体|突变号|ANTIBODY|CONSTRUCT", header_text, re.I)
    )
    return (
        (chains, raw_cells)
        if "VL" in chains
        and "VH" in chains
        and (has_sequence_index_semantics or has_construct_semantics)
        else None
    )


def _row_sequence_ids(row: list[dict[str, Any]]) -> list[int]:
    """Read explicit markers or bare integer cells under a VH/VL header."""

    explicit = sequence_ids(row_text(row))
    if explicit:
        return explicit
    values: list[int] = []
    for cell in row:
        if not isinstance(cell, dict):
            continue
        match = SEQ_ID_VALUE_RE.fullmatch(clean_text(cell.get("text")))
        if match:
            values.append(int(match.group(1)))
    return values


def parse_construct_mappings(ocr_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Read construct mappings with header and source-row provenance."""

    mappings: list[dict[str, Any]] = []
    active_header: tuple[list[str], list[str]] | None = None
    header_table_id: str | None = None
    previous_page: int | None = None
    for table in sorted(ocr_results, key=lambda item: item.get("table_index", 0)):
        page = table.get("page")
        if page is not None and previous_page is not None and page not in {previous_page, previous_page + 1}:
            active_header = None
            header_table_id = None
        previous_page = page
        if _is_sequence_definition_table(table) or _is_cdr_table(table):
            active_header = None
            header_table_id = None
            continue
        table_id = str(table.get("source_id") or table.get("image") or "unknown")
        rows = table.get("rows", [])
        detected_header: tuple[list[str], list[str]] | None = None
        header_row_index: int | None = None
        for row_index, row in enumerate(rows[:3]):
            detected_header = _index_header(row)
            if detected_header:
                header_row_index = row_index
                active_header = detected_header
                header_table_id = table_id
                break
        if active_header and not detected_header:
            # A continuation preserves column geometry and begins with data.
            # A later combination/assay table must not inherit an old chain
            # header and reinterpret unrelated numbers as SEQ identifiers.
            first = next((row for row in rows if any(clean_text(c.get("text")) for c in row)), [])
            if len(first) != len(active_header[1]) or not construct_keys(row_text(first)) or not _row_sequence_ids(first):
                active_header = None
                header_table_id = None
        if not active_header:
            continue
        ordered_chains, header_cells = active_header
        for row_index, row in enumerate(rows):
            if row_index == header_row_index:
                continue
            text = row_text(row)
            keys = construct_keys(text)
            if not keys:
                continue
            ids = _row_sequence_ids(row)
            if not ids or len(ids) > 2:
                continue
            explicit_chains = [chain_from_text(cell.get("text")) for cell in row if isinstance(cell, dict)]
            assignments: dict[str, int] = {}
            if len(ids) == 2:
                assignments = {
                    chain: seq_id
                    for chain, seq_id in zip(ordered_chains[:2], ids)
                }
            elif len(ids) == 1:
                chain = next((value for value in explicit_chains if value), None)
                if chain:
                    assignments[chain] = ids[0]
            if not assignments:
                continue
            for key in keys:
                mappings.append(
                    {
                        "construct_key": key,
                        "assignments": assignments,
                        "table_id": table_id,
                        "row_index": row_index,
                        "raw_cells": [clean_text(cell.get("text")) for cell in row if isinstance(cell, dict)],
                        "header_cells": header_cells,
                        "header_table_id": header_table_id,
                        "header_mapping_status": "explicit" if detected_header else "inherited",
                        "confidence": 0.98 if detected_header else 0.9,
                    }
                )
    return mappings


def parse_construct_index(ocr_results: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """Compatibility view of the provenance-rich construct mappings."""

    index: dict[str, dict[str, int]] = {}
    for mapping in parse_construct_mappings(ocr_results):
        index.setdefault(mapping["construct_key"], {}).update(mapping["assignments"])
    return index


def resolve_sequences(
    ocr_results: list[dict[str, Any]],
    document_template: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Return one VH/VL resolution per construct key with evidence."""

    if document_template and "sequence_extraction" in document_template:
        from .llm_sequence_agent import resolve_agent_sequences

        return resolve_agent_sequences(ocr_results, document_template)

    if document_template and document_template.get("agent", {}).get("mode") == "llm":
        return _resolve_llm_sequences(ocr_results, document_template)

    definitions = parse_sequence_definitions(ocr_results)
    _, definition_conflicts = _parse_column_sequence_definitions(ocr_results)
    mappings = parse_construct_mappings(ocr_results)
    cdr_evidence = parse_cdr_evidence(ocr_results)
    construct_index: dict[str, dict[str, int]] = {}
    mapping_evidence: dict[str, list[dict[str, Any]]] = {}
    mapping_values: dict[tuple[str, str], set[int]] = {}
    for mapping in mappings:
        key = mapping["construct_key"]
        construct_index.setdefault(key, {})
        for chain, seq_id in mapping["assignments"].items():
            mapping_values.setdefault((key, chain), set()).add(seq_id)
        mapping_evidence.setdefault(key, []).append(mapping)
    for (key, chain), values in mapping_values.items():
        if len(values) == 1:
            construct_index[key][chain] = next(iter(values))

    support_by_construct: dict[str, list[SequenceDefinition]] = {}
    constant_definitions: dict[str, list[SequenceDefinition]] = {"VH": [], "VL": []}
    for definition in definitions.values():
        if definition.sequence_scope == "constant_region":
            constant_definitions[definition.chain].append(definition)
        elif definition.sequence_scope in {"full_heavy_chain", "full_light_chain"}:
            for key in construct_keys(definition.label):
                support_by_construct.setdefault(key, []).append(definition)

    # Direct variable-region labels fill gaps in an imperfect index OCR.
    # Full chains and constant regions remain supporting evidence and must
    # never overwrite the VH/VL identifiers from a construct index.
    for seq_id, definition in definitions.items():
        if definition.sequence_scope not in {"variable_heavy", "variable_light"}:
            continue
        for key in construct_keys(definition.label):
            # A family name followed only by residue substitutions identifies
            # a mutant chain, not necessarily the parental construct.  It may
            # be linked through the explicit construct index, but must not
            # overwrite or conflict with the base pair (for example CSJ117
            # (T60V,A64D), whose H5L0/H5L1 names live in another table).
            if key in {"tezepumab", "csj117"} and re.search(
                r"\([A-Z]\d+[A-Z](?:\s*,\s*[A-Z]\d+[A-Z])*\)",
                definition.label,
                re.I,
            ):
                continue
            if key == "tezepumab" and definition.label.lower().endswith(".l"):
                chain = "VL"
            elif key == "tezepumab" and definition.label.lower().endswith(".h"):
                chain = "VH"
            else:
                chain = definition.chain
            if chain:
                if len(mapping_values.get((key, chain), set())) > 1:
                    continue
                existing_seq_id = construct_index.setdefault(key, {}).get(chain)
                if existing_seq_id not in definitions:
                    construct_index[key][chain] = seq_id
                mapping_evidence.setdefault(key, []).append(
                    {
                        "construct_key": key,
                        "assignments": {chain: seq_id},
                        "table_id": definition.label_table_id,
                        "row_index": definition.label_row_index,
                        "raw_cells": [definition.label],
                        "header_cells": [],
                        "header_table_id": None,
                        "header_mapping_status": "direct_sequence_label",
                        "sequence_scope": definition.sequence_scope,
                        "confidence": 0.85,
                    }
                )

    # Mutation-family notation carries an additional invariant: H0 means the
    # parental heavy chain and L0 means the parental light chain.  Some
    # patents list the parental chains only in the sequence-definition table,
    # not as a separate row in the construct index.  Recover a missing base
    # assignment only when every zero-component row for that named family
    # agrees on the same SEQ ID.
    base_keys = [key for key in construct_index if key in {"tezepumab", "csj117"}]
    for base_key in base_keys:
        family_mappings = [
            mapping
            for mapping in mappings
            if any(base_key in clean_text(cell).lower() for cell in mapping.get("raw_cells", []))
        ]
        zero_candidates = {
            "VH": {
                mapping["assignments"]["VH"]
                for mapping in family_mappings
                if re.match(r"^h0l\d+(?:l\d+)*$", mapping["construct_key"])
                and "VH" in mapping["assignments"]
            },
            "VL": {
                mapping["assignments"]["VL"]
                for mapping in family_mappings
                if re.match(r"^h\d+l0$", mapping["construct_key"])
                and "VL" in mapping["assignments"]
            },
        }
        for chain, candidates in zero_candidates.items():
            if len(candidates) != 1 or chain in construct_index[base_key]:
                continue
            seq_id = next(iter(candidates))
            construct_index[base_key][chain] = seq_id
            mapping_evidence.setdefault(base_key, []).append(
                {
                    "construct_key": base_key,
                    "assignments": {chain: seq_id},
                    "table_id": None,
                    "row_index": None,
                    "raw_cells": [f"{chain[1]}0 family invariant"],
                    "header_cells": [],
                    "header_table_id": None,
                    "header_mapping_status": "family_zero_component",
                    "confidence": 0.9,
                }
            )

    resolved: dict[str, dict[str, Any]] = {}
    for key, mapping in construct_index.items():
        result: dict[str, Any] = {
            "construct_key": key,
            "vh": "",
            "vl": "",
            "vh_seq_id": mapping.get("VH"),
            "vl_seq_id": mapping.get("VL"),
            "sequence_table_ids": [],
            "sequence_rows": [],
            "sequence_resolution": {},
            "supporting_sequence_table_ids": [],
            "supporting_sequence_rows": [],
            "full_heavy_seq_id": None,
            "full_light_seq_id": None,
            "constant_region_seq_ids": {"VH": [], "VL": []},
            "cdr_seq_ids": {},
            "mapping_evidence": mapping_evidence.get(key, []),
            "ambiguities": [{"chain": chain, "reason": "sequence_mapping_conflict", "seq_ids": sorted(values)}
                            for (entity, chain), values in mapping_values.items() if entity == key and len(values) > 1],
            "status": "review",
        }
        for chain, field in (("VH", "vh"), ("VL", "vl")):
            definition = definitions.get(mapping.get(chain, -1))
            if (definition and definition.chain == chain
                    and definition.sequence_scope not in {"constant_region", "heavy_cdr", "light_cdr", "cdr"}
                    and VARIABLE_LENGTH[chain][0] <= len(definition.variable_sequence) <= VARIABLE_LENGTH[chain][1]):
                result[field] = definition.variable_sequence
                result["sequence_resolution"][field] = {
                    "source_scope": definition.sequence_scope,
                    "seq_id": definition.seq_id,
                }
                result["sequence_table_ids"].extend(definition.table_ids)
                result["sequence_rows"].extend(
                    {
                        "table_id": table_id,
                        "row_index": row,
                        "seq_id": definition.seq_id,
                        "chain": chain,
                        "sequence_scope": definition.sequence_scope,
                        "evidence_role": "primary",
                    }
                    for table_id, row in zip(definition.table_ids, definition.rows)
                )
        supporting = list(support_by_construct.get(key, []))
        # Some patents provide an explicit construct -> variable-region SEQ ID
        # index, but OCR recovers only the later, construct-labelled full-chain
        # definitions. When exactly one full-chain variable sequence supports
        # the same construct and chain, use its variable prefix as a traceable
        # fallback. Keep the index SEQ ID authoritative in ``vh_seq_id`` /
        # ``vl_seq_id`` and record the full-chain SEQ ID as the derivation
        # source so the two identifiers are never silently conflated.
        for chain, field, id_field in (
            ("VH", "vh", "vh_seq_id"),
            ("VL", "vl", "vl_seq_id"),
        ):
            if result[field]:
                continue
            if len(mapping_values.get((key, chain), set())) > 1 or mapping.get(chain) in definition_conflicts:
                continue
            mapped_definition = definitions.get(mapping.get(chain))
            if mapped_definition and (mapped_definition.chain != chain or mapped_definition.sequence_scope in {
                "constant_region", "heavy_cdr", "light_cdr", "cdr"
            }):
                continue
            expected_scope = "full_heavy_chain" if chain == "VH" else "full_light_chain"
            candidates = [
                definition
                for definition in supporting
                if definition.chain == chain
                and definition.sequence_scope == expected_scope
                and VARIABLE_LENGTH[chain][0]
                <= len(definition.variable_sequence)
                <= VARIABLE_LENGTH[chain][1]
            ]
            unique_sequences = {
                definition.variable_sequence for definition in candidates
            }
            if len(unique_sequences) != 1:
                continue
            selected = min(candidates, key=lambda definition: definition.seq_id)
            mapped_seq_id = mapping.get(chain)
            if mapped_seq_id is None:
                # A uniquely named full chain can also supply a missing side
                # of the construct index (for example ``Tezepelumab.H`` when
                # only ``Tezepelumab.L`` was recovered). Make that provenance
                # explicit so construct validation sees a complete, auditable
                # mapping rather than treating the derived chain as guessed.
                mapped_seq_id = selected.seq_id
                result[id_field] = selected.seq_id
                result["mapping_evidence"].append({
                    "construct_key": key,
                    "assignments": {chain: selected.seq_id},
                    "table_id": selected.label_table_id,
                    "row_index": selected.label_row_index,
                    "raw_cells": [selected.label],
                    "header_cells": [],
                    "header_table_id": None,
                    "header_mapping_status": "construct_named_full_chain_fallback",
                    "sequence_scope": selected.sequence_scope,
                    "confidence": 0.9,
                })
            result[field] = selected.variable_sequence
            result["sequence_resolution"][field] = {
                "source_scope": selected.sequence_scope,
                "seq_id": selected.seq_id,
                "mapped_seq_id": mapped_seq_id,
                "resolution": "construct_named_full_chain_fallback",
            }
            result["sequence_table_ids"].extend(selected.table_ids)
            result["sequence_rows"].extend(
                {
                    "table_id": table_id,
                    "row_index": row,
                    "seq_id": selected.seq_id,
                    "mapped_seq_id": mapped_seq_id,
                    "chain": chain,
                    "sequence_scope": selected.sequence_scope,
                    "evidence_role": "derived_variable_region",
                }
                for table_id, row in zip(selected.table_ids, selected.rows)
            )
        if supporting:
            for chain in ("VH", "VL"):
                # A single patent-level constant definition per chain is an
                # unambiguous component of the displayed complete chains.
                if len(constant_definitions[chain]) == 1:
                    supporting.extend(constant_definitions[chain])
        seen_support: set[tuple[int, str]] = set()
        for definition in supporting:
            support_key = (definition.seq_id, definition.sequence_scope)
            if support_key in seen_support:
                continue
            seen_support.add(support_key)
            if definition.sequence_scope == "full_heavy_chain":
                result["full_heavy_seq_id"] = definition.seq_id
            elif definition.sequence_scope == "full_light_chain":
                result["full_light_seq_id"] = definition.seq_id
            elif definition.sequence_scope == "constant_region":
                result["constant_region_seq_ids"][definition.chain].append(definition.seq_id)
            result["supporting_sequence_table_ids"].extend(definition.table_ids)
            result["supporting_sequence_rows"].extend(
                {
                    "table_id": table_id,
                    "row_index": row,
                    "seq_id": definition.seq_id,
                    "chain": definition.chain,
                    "sequence_scope": definition.sequence_scope,
                    "evidence_role": "supporting",
                }
                for table_id, row in zip(definition.table_ids, definition.rows)
            )
        seen_cdr: set[tuple[str, int, str, int]] = set()
        for item in cdr_evidence.get(key, []):
            cdr_key = (
                str(item.get("table_id")),
                int(item.get("row_index", -1)),
                str(item.get("cdr_name")),
                int(item.get("seq_id", -1)),
            )
            if cdr_key in seen_cdr:
                continue
            seen_cdr.add(cdr_key)
            cdr_name = str(item.get("cdr_name"))
            result["cdr_seq_ids"].setdefault(cdr_name, []).append(item.get("seq_id"))
            result["supporting_sequence_table_ids"].append(str(item.get("table_id")))
            result["supporting_sequence_rows"].append(item)
        result["cdr_seq_ids"] = {
            name: list(dict.fromkeys(ids))
            for name, ids in result["cdr_seq_ids"].items()
        }
        result["sequence_table_ids"] = list(dict.fromkeys(result["sequence_table_ids"]))
        result["supporting_sequence_table_ids"] = list(
            dict.fromkeys(result["supporting_sequence_table_ids"])
        )
        result["status"] = "linked" if result["vh"] and result["vl"] and not result["ambiguities"] else "review"
        resolved[key] = result
    # Merge the adaptive ``Name | SEQ ID | Amino Acid Sequence`` layout after
    # the legacy resolver. Existing explicit VH/VL mappings remain
    # authoritative; the document template fills otherwise unsupported names.
    for key, item in resolve_named_sequences(ocr_results, document_template).items():
        current = resolved.get(key)
        if current is None:
            resolved[key] = item
            continue
        for chain, field, id_field in (
            ("VH", "vh", "vh_seq_id"),
            ("VL", "vl", "vl_seq_id"),
        ):
            if current.get(field) or not item.get(field):
                continue
            current[field] = item[field]
            current[id_field] = item[id_field]
            current.setdefault("sequence_resolution", {})[field] = item[
                "sequence_resolution"
            ][field]
        for list_field in (
            "sequence_table_ids",
            "sequence_rows",
            "supporting_sequence_table_ids",
            "supporting_sequence_rows",
            "mapping_evidence",
            "named_sequence_variants",
            "ambiguities",
        ):
            current.setdefault(list_field, []).extend(item.get(list_field, []))
        current["status"] = (
            "linked"
            if current.get("vh") and current.get("vl") and not current.get("ambiguities")
            else "review"
        )
    return resolved


def _explicit_sequence_blocks(ocr_results: list[dict[str, Any]], template: dict[str, Any]) -> tuple[dict[int, SequenceDefinition], set[int]]:
    """Recover a named chain block through its closing SEQ ID, then stop."""
    pages: dict[int, list[tuple[str, int, str]]] = {}
    for table in ocr_results:
        identity = str(table.get("source_id") or "")
        if not identity.startswith("sequence-page-") or not table.get("page"):
            continue
        rows = table.get("rows", [])
        lines = []
        for index, row in enumerate(rows):
            text = " ".join(str(cell.get("text", "")) for cell in row)
            if index == len(rows) - 1 and re.fullmatch(r"\s*\d{1,3}\s*", text):
                continue
            if index == 0 and re.match(r"\s*(?:WO\s*\d|PCT[/／])", text, re.I):
                continue
            lines.append((identity, index, text))
        pages[int(table["page"])] = lines
    for source in template.get("sequence_source_passages", []):
        page = source.get("page")
        if not page or int(page) in pages or source.get("ref"):
            continue
        lines = source.get("text", "").splitlines()
        pages[int(page)] = [(f"method:p{page}", i, line) for i, line in enumerate(lines)
                            if not re.match(r"\s*(?:WO\s*\d|PCT[/／])", line, re.I)
                            and not (i == len(lines) - 1 and re.fullmatch(r"\s*\d{1,3}\s*", line))]
    pieces, spans = [], []
    offset, previous = 0, None
    for page, lines in sorted(pages.items()):
        if previous is not None and page != previous + 1:
            pieces.append("|")
            offset += 1
        for identity, index, text in lines:
            value = re.sub(r"\s+|<br\s*/?>", "", text)
            pieces.append(value)
            spans.append((offset, offset + len(value), identity, index))
            offset += len(value)
        previous = page
    text = "".join(pieces)
    pattern = re.compile(
        r"(?P<chain>重链|轻链|heavychain|lightchain|VH|VL)"
        r"(?P<label>(?:(?!重链|轻链|SEQID|序列为|sequenceis)[^|]){0,70})"
        r"(?:序列为|sequenceis)[:：]?(?P<sequence>[ACDEFGHIKLMNPQRSTVWYBOXZUJ*]{5,})"
        r"[（(]?SEQID(?:NO\.?)?[:：]?(?P<seq_id>\d+)(?!\d)", re.I)
    definitions: dict[int, SequenceDefinition] = {}
    conflicts: set[int] = set()
    for match in pattern.finditer(text):
        seq_id = int(match["seq_id"])
        chain = "VH" if re.search(r"重|heavy|VH", match["chain"], re.I) else "VL"
        sequence = match["sequence"].upper()
        label = match["chain"] + match["label"]
        scope = sequence_scope(label, chain, sequence)
        refs = [(identity, index) for start, end, identity, index in spans
                if start < match.end("sequence") and end > match.start("sequence")]
        if seq_id in definitions and definitions[seq_id].full_sequence != sequence:
            conflicts.add(seq_id)
            continue
        definitions[seq_id] = SequenceDefinition(seq_id, chain, sequence, variable_region(sequence, chain), scope,
                                               table_ids=[item[0] for item in refs], rows=[item[1] for item in refs], label=label)
    return definitions, conflicts


def _resolve_llm_sequences(ocr_results: list[dict[str, Any]], template: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Resolve only validated model links; sequence characters stay source-backed."""

    from .llm_io import build_evidence

    evidence = build_evidence(ocr_results, method_pages=template.get("sequence_source_passages", []))
    definitions = parse_sequence_definitions(ocr_results, correct_ocr=False)
    _, conflicting_ids = _parse_column_sequence_definitions(ocr_results)
    # Execute sequence column plans for variable-only and unfamiliar named
    # definition layouts that the older full-chain parser does not recognize.
    current: SequenceDefinition | None = None
    for table in ocr_results:
        identity = str(table.get("source_id") or table.get("image") or "unknown")
        rows = table.get("rows", [])
        regions = table.get("table_classification", {}).get("regions", [])
        for region in regions:
            if region.get("kind") != "sequence_definition" or region.get("sequence_column") is None:
                continue
            # Free-text blocks put their identifier AFTER the residues. They
            # are parsed with explicit boundaries below, never carried into
            # the next chain's rows by a persistent current definition.
            if region.get("sequence_id_column") is None or region.get("sequence_id_column") == region["sequence_column"]:
                continue
            current = None
            for row_index in range(region["row_start"], region["row_end"] + 1):
                if row_index >= len(rows):
                    continue
                row = rows[row_index]
                column = region["sequence_column"]
                if column >= len(row):
                    continue
                text = str(row[column].get("text", ""))
                chunks = amino_acid_chunks(text, min_length=5, correct_ocr=False)
                if not chunks:
                    continue
                id_column = region.get("sequence_id_column")
                ids: list[int] = []
                if id_column is not None and id_column < len(row):
                    id_text = re.sub(r"<br\s*/?>", " ", str(row[id_column].get("text", "")), flags=re.I)
                    ids = sequence_ids(id_text)
                    if not ids:
                        match = re.fullmatch(r"\s*(?:NO[.:：]?\s*)?(\d+)\s*", id_text, re.I)
                        ids = [int(match.group(1))] if match else []
                if ids:
                    scope = region.get("sequence_scope", "unknown")
                    label_chain = chain_from_text(row_text(row).replace("<br>", ""))
                    if scope == "unknown" and label_chain:
                        scope = sequence_scope(row_text(row).replace("<br>", ""), label_chain, "".join(chunks))
                    chain = "VH" if scope in {"variable_heavy", "full_heavy_chain"} else "VL" if scope in {"variable_light", "full_light_chain"} else "unknown"
                    current = SequenceDefinition(ids[0], chain, "", "", scope,
                                                 label=row_text(row), label_table_id=identity, label_row_index=row_index)
                    # Do not overwrite a conflicting definition silently.
                    existing = definitions.get(ids[0])
                    sequence = "".join(chunks)
                    if existing:
                        # Preserve the complete source-backed definition;
                        # visiting its first row again is not a new sequence.
                        if existing.full_sequence != sequence and not existing.full_sequence.startswith(sequence):
                            conflicting_ids.add(ids[0])
                        current = None
                        continue
                    definitions[ids[0]] = current
                if current:
                    current.full_sequence += "".join(chunks)
                    current.variable_sequence = current.full_sequence
                    current.table_ids.append(identity)
                    current.rows.append(row_index)

    blocks, block_conflicts = _explicit_sequence_blocks(ocr_results, template)
    conflicting_ids.update(block_conflicts)
    for seq_id, definition in blocks.items():
        if seq_id in definitions and definitions[seq_id].full_sequence != definition.full_sequence:
            conflicting_ids.add(seq_id)
        else:
            definitions[seq_id] = definition

    resolved: dict[str, dict[str, Any]] = {}
    for entity in template.get("entity_template", {}).get("entities", []):
        key = entity["entity_id"]
        result: dict[str, Any] = {
            "construct_key": key, "family": None,
            "vh": "", "vl": "", "vh_seq_id": None, "vl_seq_id": None,
            "sequence_resolution": {}, "sequence_table_ids": [], "sequence_rows": [],
            "supporting_sequence_table_ids": [], "supporting_sequence_rows": [],
            "constant_region_seq_ids": {"VH": [], "VL": []}, "cdr_seq_ids": {},
            "mapping_evidence": [], "ambiguities": [], "status": "review",
        }
        links = [link for link in template.get("sequence_links", []) if link["entity_id"] == key]
        for chain, field in (("VH", "vh"), ("VL", "vl")):
            candidates = [link for link in links if link["chain"] == chain]
            candidates.sort(key=lambda link: link["scope"] != "variable")
            if not candidates:
                continue
            link = candidates[0]
            seq_id = link["seq_id"]
            result[f"{field}_seq_id"] = seq_id
            refs = [evidence[ref] for ref in link["evidence_refs"] if ref in evidence]
            cells = [ref for ref in refs if ref.get("kind") == "cell"]
            result["mapping_evidence"].append({
                "construct_key": key, "assignments": {chain: seq_id},
                "table_id": cells[0].get("table_id") if cells else None,
                "row_index": max((ref["row_index"] for ref in cells), default=0),
                "raw_cells": [ref["text"] for ref in cells],
                "header_cells": [ref["text"] for ref in cells],
                "header_mapping_status": "llm_validated_evidence",
                "evidence_refs": link["evidence_refs"],
                "passages": [ref for ref in refs if ref.get("kind") in {"method", "sequence_passage"}],
            })
            definition = definitions.get(seq_id)
            if seq_id in conflicting_ids:
                result["ambiguities"].append({"chain": chain, "seq_id": seq_id, "reason": "sequence_definition_conflict"})
                continue
            if not definition:
                result["ambiguities"].append({"chain": chain, "seq_id": seq_id, "reason": "sequence_definition_missing"})
                continue
            if definition.sequence_scope in {"constant_region", "cdr", "heavy_cdr", "light_cdr"}:
                result["ambiguities"].append({"chain": chain, "seq_id": seq_id, "reason": "non_variable_sequence"})
                continue
            if definition.chain in {"VH", "VL"} and definition.chain != chain:
                result["ambiguities"].append({"chain": chain, "seq_id": seq_id, "reason": "sequence_chain_mismatch"})
                continue
            variable = variable_region(definition.full_sequence, chain)
            low, high = VARIABLE_LENGTH[chain]
            if not low <= len(variable) <= high:
                result["ambiguities"].append({"chain": chain, "seq_id": seq_id, "reason": "variable_sequence_length", "length": len(variable)})
                continue
            result[field] = variable
            result["sequence_resolution"][field] = {"source_scope": definition.sequence_scope, "seq_id": seq_id, "source": "llm_validated_link"}
            result["sequence_table_ids"].extend(definition.table_ids)
            result["sequence_rows"].extend({"table_id": tid, "row_index": index, "seq_id": seq_id,
                                           "chain": chain, "sequence_scope": definition.sequence_scope,
                                           "evidence_role": "primary"}
                                          for tid, index in zip(definition.table_ids, definition.rows))
        result["sequence_table_ids"] = list(dict.fromkeys(result["sequence_table_ids"]))
        result["status"] = "linked" if result["vh"] and result["vl"] and not result["ambiguities"] else "review"
        resolved[key] = result
    return resolved
