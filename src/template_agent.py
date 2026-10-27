"""Fixed schema, source-backed slots, deterministic build and optional tool loop.

Adapted from dataagent's TemplateAgent evidence planning contract. This agent does
not infer a new schema with an LLM. One observed assay row remains one observation;
construct names match exactly after trimming outer whitespace. VH/VL, full chains
and explicitly reported CDR/FR regions are never substituted for one another.

Slots carry value/status/required/provenance/candidates. Optional CDR/FR/full-chain
slots do not generate tasks unless a source explicitly reports an incomplete row.
Sequence completion updates source-backed slots, then refreshes missing fields.
An absent assay name stays an ``unnamed_assay`` issue on a separate anonymous
entity; the plan never invents a name or an unsupported alias-resolution task.
Explicit antigen/linker/nucleotide-only sequence entities retain their source
records without mandatory antibody-chain tasks when they have no assay rows.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from collections import Counter
from pathlib import Path
from typing import Any

from units.document_fields import get_document_id, normalize_region


SCHEMA_PATH = Path(__file__).resolve().parents[1] / "schemas" / "antibody.schema.json"
SCHEMA_VERSION = "antibody-dataset-2.0"
SLOT_STATUSES = {"missing", "present", "conflict", "unavailable", "not_applicable"}
CHAIN_FIELDS = ("VH", "VL", "H_CHAIN", "L_CHAIN")
OBSERVATION_FIELDS = ("metric", "value", "unit", "conditions", "experiment_type", "target")
REGION_PATHS = {
    "VH": "VH.sequence", "VL": "VL.sequence",
    "H_chain": "H_CHAIN.sequence", "L_chain": "L_CHAIN.sequence",
    **{f"HCDR{i}": f"VH.CDRH{i}" for i in range(1, 4)},
    **{f"LCDR{i}": f"VL.CDRL{i}" for i in range(1, 4)},
    **{f"HFR{i}": f"VH.FRH{i}" for i in range(1, 5)},
    **{f"LFR{i}": f"VL.FRL{i}" for i in range(1, 5)},
}
REGION_PATHS.update({f"CDRH{i}": f"VH.CDRH{i}" for i in range(1, 4)})
REGION_PATHS.update({f"CDRL{i}": f"VL.CDRL{i}" for i in range(1, 4)})


def _empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _stable_id(prefix: str, *parts: Any) -> str:
    raw = json.dumps(parts, ensure_ascii=False, separators=(",", ":"))
    return prefix + "-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _slot(value: Any = None, *, required: bool = False, source: dict | None = None) -> dict:
    present = not _empty(value)
    return {"value": copy.deepcopy(value) if present else None,
            "status": "present" if present else "missing", "required": required,
            "provenance": [copy.deepcopy(source)] if present and source else [],
            "candidates": []}


def _add_unique(items: list, value: Any) -> None:
    if value not in items:
        items.append(copy.deepcopy(value))


def _observe(slot: dict, value: Any, source: dict) -> None:
    """Retain incompatible observed values; never silently pick an OCR candidate."""
    if _empty(value):
        return
    candidate = {"value": copy.deepcopy(value), "provenance": [copy.deepcopy(source)]}
    if slot["status"] == "conflict":
        _add_unique(slot["candidates"], candidate)
    elif slot["status"] == "present" and slot["value"] != value:
        slot["candidates"] = [{"value": slot["value"], "provenance": slot["provenance"]}, candidate]
        slot.update(value=None, status="conflict")
    else:
        slot.update(value=copy.deepcopy(value), status="present")
        _add_unique(slot["provenance"], source)


def _name(value: Any) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def _validate_value(path: str, value: Any) -> None:
    """Check raw/filled field types before empty containers can become null."""
    if value is None or value == "":
        return
    if path.endswith("seq_id"):
        valid = isinstance(value, int) and not isinstance(value, bool) and value > 0
    elif path == "METRIC.value":
        valid = (not isinstance(value, bool) and isinstance(value, (str, int, float))
                 and not (isinstance(value, str) and not value.strip())
                 and not (isinstance(value, float) and not math.isfinite(value)))
    elif path == "METRIC.conditions":
        valid = isinstance(value, (str, list, dict)) and not (isinstance(value, str) and not value.strip())
    else:
        valid = isinstance(value, str) and bool(value.strip())
    if not valid:
        raise ValueError(f"Invalid value type/content for {path}: {type(value).__name__}")


def _explicit_non_antibody(row: dict) -> bool:
    """Use declared source classification only; clone names provide no type proof."""
    molecule = str(row.get("molecule_type") or "").strip().casefold()
    sequence_type = str(row.get("sequence_type") or "").strip().casefold()
    role = str(row.get("domain_role") or "").strip().casefold()
    return (molecule in {"antigen", "linker", "nucleotide", "dna", "rna", "抗原", "接头", "连接肽"}
            or sequence_type in {"nucleotide", "dna", "rna", "核酸"}
            or role in {"linker", "接头", "连接肽"})


def _seq_id(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"\s*(?:SEQ\s*ID\s*NO\s*[:：.]?\s*)?(\d+)\s*", value, re.I)
    return int(match.group(1)) if match and int(match.group(1)) > 0 else None


def _entity(document_id: str, name: str | None, anonymous_key: Any = None) -> dict:
    result = {"entity_id": _stable_id("entity", document_id, name, anonymous_key),
              "name": name, "name_provenance": [], "METRIC": [], "other_sequences": [],
              "sequence_links": []}
    for field in CHAIN_FIELDS:
        result[field] = {"sequence": _slot(required=field in {"VH", "VL"}), "seq_id": _slot()}
    for chain, letter in (("VH", "H"), ("VL", "L")):
        result[chain].update({f"CDR{letter}{i}": _slot() for i in range(1, 4)})
        result[chain].update({f"FR{letter}{i}": _slot() for i in range(1, 5)})
    return result


def _source(table: dict, kind: str, index: int, document_id: str, table_index: int) -> dict:
    value = {key: table[key] for key in
             ("table_id", "image_path", "result_file", "page", "example", "caption")
             if key in table and table[key] is not None}
    value.update(document_id=document_id, source_type=table.get("source_type", "table"), record_type=kind,
                 table_index=table_index, record_index=index)
    row = table.get(kind, [])[index]
    for key in ("value_ref", "cell_refs", "measurement_name", "exp_id"):
        if row.get(key) is not None:
            value[key] = copy.deepcopy(row[key])
    return value


def _all_slots(entity: dict):
    for chain in CHAIN_FIELDS:
        for field, slot in entity[chain].items():
            yield f"{chain}.{field}", None, slot
    for observation in entity["METRIC"]:
        for field in OBSERVATION_FIELDS:
            yield f"METRIC.{field}", observation["observation_id"], observation[field]


def refresh_missing(template: dict) -> dict:
    """Rebuild the executable required-slot plan, without changing existing data."""
    tasks = []
    for entity in template["entities"]:
        occurrences = Counter()
        for observation in sorted(entity["METRIC"], key=lambda item: item["order"]):
            endpoint = observation["metric"]["value"]
            endpoint_key = json.dumps(endpoint, ensure_ascii=False, sort_keys=True)
            occurrences[endpoint_key] += 1
            observation["label"] = f"{endpoint if not _empty(endpoint) else 'UNKNOWN'}-{occurrences[endpoint_key]}"
        for chain in ("VH", "VL"):
            if entity[chain]["sequence"]["status"] == "present":
                entity[chain]["seq_id"]["required"] = False
        for path, observation_id, slot in _all_slots(entity):
            if not slot["required"] or slot["status"] != "missing":
                continue
            observation = next((item for item in entity["METRIC"]
                                if item["observation_id"] == observation_id), None)
            source = observation.get("source", {}) if observation else {}
            endpoint = observation["metric"]["value"] if observation else None
            query = " ".join(str(part) for part in (
                entity["name"], endpoint, path, source.get("table_id"),
                source.get("example"), source.get("caption")) if part is not None)
            tasks.append({"task_id": _stable_id("task", entity["entity_id"], observation_id, path),
                          "entity_id": entity["entity_id"], "observation_id": observation_id,
                          "field_path": path, "query": query,
                          "kind": "assay" if observation_id else "sequence"})
    template["missing"] = tasks
    status_counts = Counter(slot["status"] for entity in template["entities"]
                            for _, _, slot in _all_slots(entity))
    template["summary"] = {"entities": len(template["entities"]),
                           "observations": sum(len(item["METRIC"]) for item in template["entities"]),
                           "missing_required": len(tasks), "slot_status": dict(status_counts),
                           "issues": len(template.get("issues", []))}
    return template


def _catalog_index(template: dict, catalog: dict) -> dict:
    if not isinstance(catalog, dict) or catalog.get("document_id") != template["document_id"]:
        raise ValueError("Sequence catalog must explicitly belong to this document")
    index = {}
    for row in catalog.get("records", []):
        number = _seq_id(row.get("seq_id"))
        if number is None or number in index:
            raise ValueError("Invalid or duplicate catalog SEQ ID")
        sequence = row.get("sequence")
        if not isinstance(sequence, str) or not sequence:
            raise ValueError("Catalog sequence must be a nonempty string")
        if row.get("length") is not None and row["length"] != len(sequence):
            raise ValueError(f"Catalog sequence length mismatch: {number}")
        index[number] = row
    return index


def resolve_catalog(template: dict, catalog: dict) -> dict:
    """Resolve only explicit construct/chain/SEQ-ID slots in the same document.

    Original WIPO residues take precedence over OCR residue transcription under
    that explicit mapping. The prior OCR value remains a candidate and an issue;
    conflicting ownership/ID slots are never auto-resolved. Catalog membership or
    matching residue strings alone cannot establish an entity/region mapping.
    """
    index = _catalog_index(template, catalog)
    for entity in template["entities"]:
        mappings = [(f"{chain}.sequence", entity[chain]["seq_id"]) for chain in CHAIN_FIELDS]
        mappings.extend((item["field_path"], item["seq_id"]) for item in entity.get("sequence_links", []))
        for field_path, id_slot in mappings:
            chain, field = field_path.split(".")
            slot = entity[chain][field]
            if id_slot["status"] != "present" or not id_slot["provenance"]:
                continue
            number = _seq_id(id_slot["value"])
            row = index.get(number)
            if row is None:
                continue
            if str(row.get("molecule_type", "AA")).upper() not in {"AA", "PROTEIN", "AMINO_ACID"}:
                _add_unique(template["issues"], {"kind": "catalog_molecule_type_mismatch", "entity_id": entity["entity_id"], "field_path": field_path, "seq_id": number})
                continue
            if slot["status"] == "conflict":
                continue
            if any(p.get("document_id", template["document_id"]) != template["document_id"] for p in id_slot["provenance"]):
                continue
            sequence = row["sequence"]
            provenance = {"document_id": template["document_id"], "source_type": catalog.get("source_type", "WIPO_sequence_listing"),
                          "source_file": row.get("source_file", catalog.get("source_xml")), "seq_id": number,
                          "mapping_provenance": copy.deepcopy(id_slot["provenance"])}
            if slot["status"] == "present" and slot["value"] != sequence:
                _add_unique(slot["candidates"], {"value": slot["value"], "provenance": slot["provenance"]})
                _add_unique(template["issues"], {"kind": "ocr_catalog_sequence_mismatch", "entity_id": entity["entity_id"],
                                               "field_path": field_path, "seq_id": number,
                                               "resolution": "original_WIPO_sequence_listing"})
            slot.update(value=sequence, status="present")
            _add_unique(slot["provenance"], provenance)
    return refresh_missing(template)


def export_dataset(template: dict) -> list[dict]:
    """Export one legacy six-field row per observation, in original global order."""
    ordered = [(observation["order"], entity, observation)
               for entity in template["entities"] for observation in entity["METRIC"]]
    def value(slot):
        return copy.deepcopy(slot["value"]) if slot["status"] == "present" else None
    return [{"NAME": observation.get("source", {}).get("measurement_name", entity["name"]), "VH": value(entity["VH"]["sequence"]),
             "VL": value(entity["VL"]["sequence"]), "METRIC": value(observation["metric"]),
             "VALUE": value(observation["value"]), "UNIT": value(observation["unit"])}
            for _, entity, observation in sorted(ordered, key=lambda item: item[0])]


def validate_template(template: dict) -> None:
    """Validate inexpensive invariants without making jsonschema a runtime dependency."""
    if not isinstance(template.get("document_id"), str) or not template["document_id"]:
        raise ValueError("Template requires document_id")
    entity_ids, observation_ids = set(), set()
    experiments = template.get("experiments", [])
    exp_ids = [e.get("exp_id") for e in experiments]
    if len(exp_ids) != len(set(exp_ids)) or any(not isinstance(e, str) or not e for e in exp_ids):
        raise ValueError("Invalid or duplicate experiment ID")
    for entity in template["entities"]:
        if entity.get("name") is not None and (not isinstance(entity["name"], str) or not entity["name"].strip()):
            raise ValueError("Entity name must be a nonempty string or null")
        if entity["entity_id"] in entity_ids:
            raise ValueError("Duplicate entity_id")
        entity_ids.add(entity["entity_id"])
        for observation in entity["METRIC"]:
            if observation["observation_id"] in observation_ids:
                raise ValueError("Duplicate observation_id")
            observation_ids.add(observation["observation_id"])
            if observation.get("exp_id") is not None and observation["exp_id"] not in exp_ids:
                raise ValueError("Observation refers to an unknown experiment")
        slots = [(path, slot) for path, _, slot in _all_slots(entity)]
        slots.extend((f"{link['field_path']}.seq_id", link["seq_id"])
                     for link in entity.get("sequence_links", []))
        for path, slot in slots:
            if slot.get("status") not in SLOT_STATUSES or not isinstance(slot.get("required"), bool):
                raise ValueError(f"Invalid schema slot: {path}")
            if slot["status"] == "present" and _empty(slot.get("value")):
                raise ValueError(f"Present slot lacks value: {path}")
            if slot["status"] in {"missing", "conflict", "unavailable", "not_applicable"} and slot.get("value") is not None:
                raise ValueError(f"Unresolved slot has value: {path}")
            if slot["status"] == "present":
                _validate_value(path, slot["value"])
            for candidate in slot.get("candidates", []):
                _validate_value(path, candidate.get("value"))
    definitions = template.get("sequence_definitions", [])
    definition_ids, components = {}, {}
    for definition in definitions:
        identity = definition.get("definition_id")
        number = definition.get("seq_id")
        if not isinstance(identity, str) or not identity or identity in definition_ids:
            raise ValueError("Invalid or duplicate sequence definition ID")
        if number is not None and (isinstance(number, bool) or not isinstance(number, int) or number < 1):
            raise ValueError("Invalid sequence definition SEQ ID")
        if definition.get("status") not in {"unresolved", "definition", "linked", "reference", "non_antibody", "ambiguous"}:
            raise ValueError("Invalid sequence definition status")
        if not definition.get("source_refs") or not isinstance(definition.get("assignments"), list):
            raise ValueError("Sequence definition requires source addresses and assignments")
        definition_ids[identity] = definition
    for entity in template["entities"]:
        for component in entity.get("sequence_components", []):
            identity = component.get("component_id")
            definition = definition_ids.get(component.get("definition_id"))
            if not isinstance(identity, str) or identity in components or definition is None:
                raise ValueError("Invalid component ID or unknown sequence definition")
            if component.get("seq_id") != definition["seq_id"]:
                raise ValueError("Component SEQ ID differs from its definition")
            if component.get("role") not in {"molecule", "IgG", "scFv", "linker", "other"}:
                raise ValueError("Invalid sequence component role")
            if component.get("parent_chain") not in {None, "H_CHAIN", "L_CHAIN"}:
                raise ValueError("Invalid component parent chain")
            order = component.get("order")
            if order is not None and (isinstance(order, bool) or not isinstance(order, int) or order < 1):
                raise ValueError("Invalid component order")
            if not component.get("provenance") or any(p.get("document_id") != template["document_id"] for p in component["provenance"]):
                raise ValueError("Component requires same-document provenance")
            sequence = component.get("sequence")
            if sequence is not None and (not isinstance(sequence, str) or not re.fullmatch(r"[A-Za-z*.-]+", sequence)):
                raise ValueError("Invalid component sequence characters")
            if identity not in definition["assignments"] or definition["status"] != "linked":
                raise ValueError("Component is absent from its definition assignments")
            components[identity] = component
    for definition in definitions:
        if any(c not in components for c in definition["assignments"]):
            raise ValueError("Sequence definition refers to an unknown component")
        if bool(definition["assignments"]) != (definition["status"] == "linked"):
            raise ValueError("Linked sequence definition requires an actual component")


class TemplateAgent:
    """Build the fixed schema and optionally retrieve/verify missing sequence links."""

    def __init__(self, schema_path: str | Path | None = None):
        self.schema_path = Path(schema_path) if schema_path is not None else SCHEMA_PATH
        self.schema = json.loads(self.schema_path.read_text(encoding="utf-8"))
        if self.schema.get("x-schema-version") != SCHEMA_VERSION:
            raise ValueError("Unsupported antibody schema version")

    async def complete(self, template, document, llm, output_dir, *, sequence_catalog=None,
                       max_calls=12, max_context_chars=24000, timing=None):
        """Run the bounded, evidence-backed sequence planning/tool loop.

        ``run`` stays deterministic. Completion uses only this document's local
        evidence; every proposed change is checked before modifying the schema.
        """
        from template_sequence_agent import complete_template_sequences
        return await complete_template_sequences(
            template, document, llm, output_dir, catalog=sequence_catalog,
            max_calls=max_calls, max_context_chars=max_context_chars, timing=timing)

    def run(self, document_id: str, tables: list[dict], *, sequence_catalog: dict | None = None,
            experiments: list[dict] | None = None) -> dict:
        if not isinstance(document_id, str) or not document_id.strip():
            raise ValueError("document_id must be a nonempty string")
        if not isinstance(tables, list) or any(not isinstance(item, dict) for item in tables):
            raise ValueError("tables must be a list of objects")
        template = {"document_id": document_id, "schema_version": SCHEMA_VERSION,
                    "agent": {"name": "TemplateAgent", "mode": "fixed_schema_evidence_plan"},
                    "entities": [], "missing": [], "summary": {}, "issues": [],
                    "experiments": copy.deepcopy(experiments or []), "relations": []}
        entities, order = {}, 0
        sequence_classifications = {}
        for table_index, table in enumerate(tables):
            table_document = get_document_id(table, document_id)
            if table_document != document_id:
                raise ValueError("Template tables must belong to one explicit document")
            if table.get("error") or table.get("error_stage"):
                template["issues"].append({"kind": "skipped_failed_table", "table_id": table.get("table_id"), "error": table.get("error")})
                continue
            for reason in table.get("extraction_context", {}).get("issues", []):
                template["issues"].append({"kind": "continuation_unresolved",
                    "table_id": table.get("table_id"), "reason": reason})
            if table.get("layout", {}).get("orientation_review_required"):
                template["issues"].append({"kind": "page_orientation_needs_review",
                    "table_id": table.get("table_id"), "page": table["layout"].get("page")})
            for kind in ("sequence_records", "assay_records"):
                rows = table.get(kind, [])
                if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                    raise ValueError(f"{kind} must be a list of objects")
                for index, row in enumerate(rows):
                    if row.get("name") is not None and not isinstance(row["name"], str):
                        raise ValueError("Record name must be a string or null")
                    source = _source(table, kind, index, document_id, table_index)
                    if row.get("evidence"):
                        source["extraction_evidence"] = copy.deepcopy(row["evidence"])
                    name = _name(row.get("canonical_name", row.get("name")))
                    if name is None and kind == "sequence_records":
                        template["issues"].append({"kind": "unassigned_sequence", "record": copy.deepcopy(row), "source": source})
                        continue
                    identity = row.get("entity_id")
                    key = ("explicit", identity) if identity is not None else (
                        name if name is not None else ("anonymous", table_index, index))
                    if key not in entities:
                        entities[key] = _entity(document_id, name, key if name is None or identity is not None else None)
                    entity = entities[key]
                    if identity is not None:
                        entity["original_entity_id"] = identity
                    for extra in ("aliases", "construct_type", "notes"):
                        if row.get(extra) is not None:
                            entity[extra] = copy.deepcopy(row[extra])
                    if name is not None:
                        _add_unique(entity["name_provenance"], source)
                    if row.get("identity_only"):
                        continue
                    if kind == "assay_records":
                        for field in OBSERVATION_FIELDS:
                            _validate_value(f"METRIC.{field}", row.get(field))
                        endpoint = row.get("metric")
                        occurrence = 1 + sum(item["metric"]["value"] == endpoint for item in entity["METRIC"])
                        observation = {"observation_id": _stable_id("observation", document_id, table_index, table.get("table_id"), index),
                                       "label": f"{endpoint if not _empty(endpoint) else 'UNKNOWN'}-{occurrence}",
                                       "source": source, "order": order}
                        for extra in ("exp_id", "method", "keywords"):
                            if row.get(extra) is not None:
                                observation[extra] = copy.deepcopy(row[extra])
                        for field in OBSERVATION_FIELDS:
                            observation[field] = _slot(row.get(field), required=True, source=source)
                        entity["METRIC"].append(observation)
                        if name is None:
                            template["issues"].append({"kind": "unnamed_assay", "entity_id": entity["entity_id"], "observation_id": observation["observation_id"], "source": source})
                        order += 1
                        continue
                    for field in ("region", "sequence"):
                        _validate_value(field, row.get(field))
                    raw_id = row.get("seq_id")
                    if raw_id is not None and (isinstance(raw_id, bool) or not isinstance(raw_id, (str, int))):
                        raise ValueError("Sequence SEQ ID must be a string, positive integer or null")
                    if isinstance(raw_id, int) and raw_id <= 0:
                        raise ValueError("Sequence SEQ ID must be positive")
                    non_antibody = _explicit_non_antibody(row)
                    sequence_classifications.setdefault(entity["entity_id"], []).append(non_antibody)
                    if non_antibody:
                        entity["other_sequences"].append({"record": copy.deepcopy(row), "source": source})
                        continue
                    region = normalize_region(row.get("region"))
                    path = REGION_PATHS.get(region)
                    if path is None:
                        entity["other_sequences"].append({"record": copy.deepcopy(row), "source": source})
                        if str(region).casefold() not in {"igg_hc", "igg_lc", "scfv_vh", "scfv_vl", "linker", "scfv_linker", "scfv"}:
                            template["issues"].append({"kind": "unsupported_sequence_region", "entity_id": entity["entity_id"], "region": region, "source": source})
                        continue
                    chain, field = path.split(".")
                    slot = entity[chain][field]
                    slot["required"] = True
                    _observe(slot, row.get("sequence"), source)
                    if field == "sequence":
                        raw_id = row.get("seq_id")
                        number = _seq_id(raw_id)
                        if not _empty(raw_id) and number is None:
                            template["issues"].append({"kind": "invalid_seq_id", "entity_id": entity["entity_id"], "raw": raw_id, "source": source})
                        _observe(entity[chain]["seq_id"], number, source)
                    else:
                        number = _seq_id(row.get("seq_id"))
                        if number is not None:
                            link = next((item for item in entity["sequence_links"] if item["field_path"] == path), None)
                            if link is None:
                                link = {"field_path": path, "seq_id": _slot()}
                                entity["sequence_links"].append(link)
                            _observe(link["seq_id"], number, source)
        template["entities"] = list(entities.values())
        for entity in template["entities"]:
            if re.search(r"bispecific|multispecific|双特异|多特异", str(entity.get("construct_type", "")), re.I):
                # A molecule with multiple binding arms has no single overall
                # VH/VL pair. Its explicitly named components remain separate.
                for chain in ("VH", "VL"):
                    entity[chain]["sequence"]["required"] = False
            classifications = sequence_classifications.get(entity["entity_id"], [])
            non_antibody_only = bool(classifications) and all(classifications) and not entity["METRIC"]
            if non_antibody_only:
                for chain in ("VH", "VL"):
                    entity[chain]["sequence"]["required"] = False
            for path, observation_id, slot in _all_slots(entity):
                if slot["status"] == "conflict":
                    template["issues"].append({"kind": "conflicting_observed_values", "entity_id": entity["entity_id"], "field_path": path, "observation_id": observation_id})
            for link in entity["sequence_links"]:
                if link["seq_id"]["status"] == "conflict":
                    template["issues"].append({"kind": "conflicting_sequence_mapping", "entity_id": entity["entity_id"], "field_path": link["field_path"]})
            if sequence_catalog is not None:
                for chain in ("VH", "VL"):
                    entity[chain]["seq_id"]["required"] = (
                        entity[chain]["sequence"]["required"]
                        and entity[chain]["sequence"]["status"] == "missing")
        if sequence_catalog is not None:
            resolve_catalog(template, sequence_catalog)
        refresh_missing(template)
        validate_template(template)
        return template
