"""Assay protocol binding, semantic templates, and antigen keyword annotation."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

# Region and endpoint protocol binding

def identified_regions(table_id: str, regions: list[dict]) -> list[dict]:
    return [
        {**region, "region_id": region.get("region_id") or
         f"{table_id}:r{region['row_start']}-{region['row_end']}:{index}"}
        for index, region in enumerate(regions)
    ]


def region_endpoints(region: dict) -> set[str]:
    metrics = [*region.get("metrics", []),
               *(metric for group in region.get("entity_groups", []) for metric in group.get("metrics", []))]
    return {metric["endpoint"] for metric in metrics}


def select_assay_profile(
    template: dict | None, table_id: str, region_id: str | None = None,
    endpoint: str = "IC50", *, regions: list[dict] | None = None, record_id: str | None = None,
) -> dict[str, Any]:
    assay = (template or {}).get("assay_template", {})
    record_profile = assay.get("record_profiles", {}).get(record_id)
    if record_profile and record_profile.get("table_id") == table_id and record_profile.get("region_id") == region_id and endpoint in record_profile.get("endpoints", []):
        return record_profile
    if "region_profiles" in assay:
        matches = [profile for profile in assay["region_profiles"]
                   if profile.get("table_id") == table_id
                   and profile.get("region_id") == region_id
                   and endpoint in profile.get("endpoints", [])]
        if len(matches) == 1:
            return matches[0]
        return {"status": "unmatched", "binding_reason": "ambiguous_binding" if matches else "missing_binding"}
    # Historical profiles cannot safely cross mixed regions or endpoints.
    scopes = [r for r in regions or [] if r.get("kind") == "assay"]
    if len(scopes) > 1 or any(len(region_endpoints(r)) > 1 for r in scopes):
        return {"status": "unmatched", "binding_reason": "legacy_mixed_scope"}
    profile = assay.get("table_profiles", {}).get(table_id, {})
    declared = profile.get("endpoints") or [profile.get("endpoint")]
    if any(declared) and endpoint not in declared:
        return {"status": "unmatched", "binding_reason": "endpoint_mismatch"}
    return profile


def materialize_profiles(profiles: list[dict], evidence: dict, meta: dict) -> list[dict]:
    result = []
    for profile in profiles:
        refs = profile["evidence_refs"]
        prose = [ref for ref in refs if evidence[ref].get("kind") in {"method", "context"}]
        result.append({
            **profile, "status": "matched" if prose else "unmatched",
            "protocol_ids": [profile["protocol_id"]], "experiment_id": profile["protocol_id"],
            "assay_method": profile["method"], "assay_system": profile["system"],
            "endpoint": profile["endpoints"][0] if len(profile["endpoints"]) == 1 else "mixed",
            "summary_zh": profile["summary_zh"] if prose else "",
            "summary_en": profile["summary_en"] if prose else "",
            "match_confidence": None, "match_reason": "llm_region_evidence_plan",
            "source_pages": sorted({evidence[ref]["page"] for ref in refs if evidence[ref].get("page") is not None}),
            "protocol_evidence": [{**evidence[ref], "evidence_ref": ref, "relation": "explicit_reference"} for ref in refs],
            "llm": meta,
        })
    return result


def table_profile_summaries(profiles: list[dict]) -> dict[str, dict]:
    """Only expose a table-wide method when all regions agree on a protocol."""
    grouped: dict[str, list[dict]] = {}
    for profile in profiles:
        grouped.setdefault(profile["table_id"], []).append(profile)
    return {identity: (items[0] if len({p["protocol_id"] for p in items}) == 1 else {
        "table_id": identity, "status": "mixed", "region_profiles": items,
        "label": "Multiple assay protocols; inspect region_profiles",
    }) for identity, items in grouped.items()}


# Editable semantic template

DEFAULT_TEMPLATE_PATH = (
    Path(__file__).resolve().parent
    / "templates"
    / "antibody_sequence_assay_v2.json"
)


@lru_cache(maxsize=4)
def load_semantic_template(path: str | Path | None = None) -> dict[str, Any]:
    template_path = Path(path).expanduser().resolve() if path else DEFAULT_TEMPLATE_PATH
    with template_path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict) or not value.get("template_id"):
        raise ValueError(f"Invalid semantic template: {template_path}")
    return value


# Antigen keyword annotation

DEFAULT_LIBRARY_DIR = Path(__file__).parent / "templates" / "assay_keywords"
CATEGORIES = {"species", "system", "method", "sample", "endpoint", "cell_model", "readout"}


def _normalize(value: Any) -> str:
    return re.sub(r"[\s_·-]+", "", str(value or "")).casefold()


def _matches(patterns: list[str], text: str) -> bool:
    return any(re.search(pattern, text, re.I) for pattern in patterns)


def load_keyword_libraries(directory: str | Path | None = None) -> list[dict]:
    """Read editable libraries afresh; reject duplicate identities and bad rules."""
    root = Path(directory) if directory else DEFAULT_LIBRARY_DIR
    paths = sorted(root.glob("*.json"))
    if not paths:
        raise ValueError(f"No assay keyword libraries found in {root}")
    libraries, targets, aliases = [], set(), set()
    for path in paths:
        library = json.loads(path.read_text(encoding="utf-8"))
        target = library.get("target_id")
        if not isinstance(target, str) or not target.strip() or not isinstance(library.get("version"), int) or library["version"] < 1:
            raise ValueError(f"Invalid target/version in {path}")
        if target in targets:
            raise ValueError(f"Duplicate antigen target: {target}")
        targets.add(target)
        names = {_normalize(name) for name in [target, *library.get("aliases", [])]}
        if names & aliases:
            raise ValueError(f"Ambiguous antigen alias in {path}")
        aliases.update(names)
        identities = set()
        if not library.get("labels"):
            raise ValueError(f"Empty assay keyword library: {path}")
        for label in library["labels"]:
            identity = (label.get("category"), label.get("key"))
            if identity[0] not in CATEGORIES or not identity[1] or identity in identities:
                raise ValueError(f"Invalid/duplicate label {identity} in {path}")
            identities.add(identity)
            if not label.get("label_zh") or not label.get("label_en"):
                raise ValueError(f"Missing bilingual label: {identity} in {path}")
            for pattern in [*label.get("patterns", []), *(p for group in label.get("requires", []) for p in group)]:
                re.compile(pattern)
        for pattern in library.get("target_patterns", []):
            re.compile(pattern)
        digest = hashlib.sha256(json.dumps(library, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        libraries.append({**library, "library_id": f"assay-keywords:{target}", "digest": digest})
    return libraries


def keyword_context(
    target: str | None = None, directory: str | Path | None = None,
    *, texts: list[str] | None = None,
) -> dict:
    libraries = load_keyword_libraries(directory)
    resolved = [item["target_id"] for item in libraries
                if _normalize(target) in {_normalize(v) for v in [item["target_id"], *item.get("aliases", [])]}] if target else []
    if target and not resolved:
        raise ValueError(f"No assay keyword library for antigen target {target!r}")
    if not target:
        resolved = list(dict.fromkeys(item["target_id"] for item in libraries
                                     if any(_matches(item.get("target_patterns", []), text) for text in texts or [])))
    return {"target_id": resolved[0] if len(resolved) == 1 else None,
            "target_source": "configured" if target else "document_evidence",
            "libraries": libraries}


def _exact(label: dict, value: str) -> bool:
    return bool(value) and _normalize(value) in {
        _normalize(item) for item in [label["key"], label["label_zh"], label["label_en"], *label.get("aliases", [])]
    }


def retag_records(records: list[dict], template: dict, tables: list[dict], context: dict) -> list[dict]:
    """Refresh keyword labels while retaining observation values and evidence."""

    from .llm_io import build_evidence
    from .table_structure import semantic_table

    evidence = build_evidence([semantic_table(table) for table in tables])
    result = copy.deepcopy(records)
    for record in result:
        measurements = [item for item in record.get("evidence", []) if item.get("type") == "assay_measurement"]
        observation = measurements[0] if measurements else {}
        table_id = str(observation.get("table_id") or record.get("assay_table_id") or "")
        endpoint = record.get("assay_endpoint") or observation.get("endpoint") or "IC50"
        profile = select_assay_profile(template, table_id, record.get("assay_region_id") or observation.get("region_id"), endpoint,
                                       record_id=record.get("record_id"))
        label = record.get("assay_measurement_label") or " · ".join(
            str(evidence.get(ref, {}).get("text", "")) for ref in observation.get("header_refs", []))
        condition = record.get("assay_condition") or observation.get("condition_label", "")
        annotation = annotate_assay(profile, label, condition, context, endpoint=endpoint, table_id=table_id)
        # Species is an extracted value, not a keyword-label refresh target.
        annotation.pop("species")
        record.update(annotation)
    return result


def annotate_assay(
    profile: dict, measurement_label: str, condition_label: str,
    context: dict, *, endpoint: str = "IC50", table_id: str = "",
) -> dict:
    """Map one region/endpoint observation, without scanning unrelated method pages."""
    libraries = context.get("libraries", [])
    agent_facts = profile.get("experiment_facts")
    observation = " · ".join(filter(None, [condition_label, measurement_label]))
    # A measurement's explicit target takes precedence over the document default.
    # Only bound method/system names are used as fallback, never a whole PDF page.
    method = str(profile.get("assay_method") or "")
    system = str(profile.get("assay_system") or "")
    profile_text = " · ".join(filter(None, [method, system, profile.get("label", "")]))
    target_ids = [item["target_id"] for item in libraries if _matches(item.get("target_patterns", []), observation)]
    profile_targets = [item["target_id"] for item in libraries if _matches(item.get("target_patterns", []), profile_text)]
    if agent_facts is not None:
        profile_targets = [item["target_id"] for item in libraries if _normalize(profile.get("antigen_target")) in
                           {_normalize(v) for v in [item["target_id"], *item.get("aliases", [])]}]
    target_conflict = bool(target_ids and profile_targets and not set(target_ids) & set(profile_targets))
    target_source = "observation"
    if agent_facts is not None or not target_ids:
        target_ids = profile_targets
        target_source = "protocol"
    if agent_facts is None and not target_ids and context.get("target_id"):
        target_ids = [context["target_id"]]
        target_source = context.get("target_source", "configured")
    result = {"antigen_target": target_ids[0] if len(target_ids) == 1 else None,
              "assay_keyword_status": "needs_review", "assay_keyword_library": None,
              "assay_keywords": [], "assay_keyword_unmatched": [], "species": []}
    unresolved = result["assay_keyword_unmatched"]
    for issue in profile.get("experiment_unresolved", []):
        unresolved.append({"category": issue["field"], "value": "", **{k: v for k, v in issue.items() if k != "field"}})
    if target_conflict:
        unresolved.append({"category": "target", "value": profile_targets, "reason": "protocol_target_conflict"})
        if agent_facts is not None:
            # A disagreement between explicit agent facts and result headers
            # needs review before either target's labels can be assigned.
            result["antigen_target"] = None
            return result
        # Keep local evidence, but never borrow another target's method/species.
        profile = {"region_id": profile.get("region_id")}
        method = system = ""
    if len(target_ids) != 1:
        unresolved.append({"category": "target", "value": target_ids, "reason": "ambiguous_target" if target_ids else "missing_target"})
        return result
    library = next((item for item in libraries if item["target_id"] == target_ids[0]), None)
    if library is None:
        unresolved.append({"category": "target", "value": target_ids[0], "reason": "missing_library"})
        return result
    result["assay_keyword_library"] = {key: library[key] for key in ("library_id", "target_id", "version", "digest")}
    result["assay_keyword_library"]["target_source"] = target_source
    definitions = library["labels"]
    provenance = {"table_id": table_id, "region_id": profile.get("region_id"),
                  "protocol_id": profile.get("protocol_id"),
                  "evidence_refs": profile.get("evidence_refs", []), "source_pages": profile.get("source_pages", [])}

    def emit(label: dict, source: str, text: str) -> None:
        fact_field = {"species": "antigen_species"}.get(label["category"], label["category"])
        quotes = [c for f in agent_facts or [] if f["field"] == fact_field and _exact(label, f["value"]) for c in f["evidence"]]
        result["assay_keywords"].append({
            **{key: label[key] for key in ("category", "key", "label_zh", "label_en")},
            "target_id": library["target_id"], "library_id": library["library_id"],
            "library_version": library["version"], "library_digest": library["digest"],
            "confidence": profile.get("match_confidence"),
            "evidence": {**provenance, "source": source, "text": text, **({"quotes": quotes} if quotes else {})},
        })
        if agent_facts is not None and label["category"] == "species":
            result["assay_keywords"][-1].update(label_zh=f"靶蛋白：{label['label_zh']}", label_en=f"Antigen: {label['label_en']}")

    def choose(category: str, candidates: list[dict], value: Any, source: str) -> dict | None:
        if len(candidates) == 1:
            emit(candidates[0], source, str(value))
            return candidates[0]
        unresolved.append({"category": category, "value": value,
                           "reason": "ambiguous_label" if candidates else "unknown_label" if value else "missing_evidence"})
        return None

    species_defs = [d for d in definitions if d["category"] == "species"]
    species = [d for d in species_defs if _matches(d.get("patterns", []), observation)] if agent_facts is None else []
    species_value, species_source = observation, "observation"
    if not species:
        species_value, species_source = profile.get("species", []), "protocol.species"
        species = [d for d in species_defs if any(_exact(d, v) for v in species_value)]
        unknown_species = [v for v in species_value if not any(_exact(d, v) for d in species_defs)]
        if unknown_species:
            unresolved.append({"category": "species", "value": unknown_species, "reason": "unknown_label"})
    selected_species = choose("species", species, species_value, species_source)
    if selected_species:
        result["species"] = [selected_species["key"]]

    choose("endpoint", [d for d in definitions if d["category"] == "endpoint" and _exact(d, endpoint)], endpoint, "observation.endpoint")
    method_defs = [d for d in definitions if d["category"] == "method" and d.get("endpoint") == endpoint]
    method_text = " · ".join(filter(None, [method, system, observation, endpoint]))
    methods = [d for d in method_defs if _exact(d, method)]
    if not methods and agent_facts is None:
        methods = [d for d in method_defs if d.get("requires") and all(_matches(group, method_text) for group in d["requires"])]
    selected_method = choose("method", methods, method or observation, "protocol.method" if method else "observation")
    system_defs = [d for d in definitions if d["category"] == "system"]
    systems = [d for d in system_defs if _exact(d, system)]
    if not systems and selected_method and agent_facts is None:
        systems = [d for d in system_defs if d["key"] == selected_method.get("system")]
    if selected_method and systems and systems[0]["key"] != selected_method.get("system"):
        unresolved.append({"category": "system", "value": system, "reason": "method_system_conflict"})
    else:
        choose("system", systems, system, "protocol.system" if system else "method_definition")
    # These dimensions are optional and may legitimately contain several labels.
    for definition in definitions:
        if definition["category"] not in {"sample", "cell_model", "readout"}:
            continue
        if agent_facts is not None:
            matched = any(f["field"] == definition["category"] and _exact(definition, f["value"]) for f in agent_facts)
        else:
            matched = _matches(definition.get("patterns", []), method_text)
        if matched:
            emit(definition, "observation_and_protocol", method_text)
    if agent_facts is not None:
        for fact in agent_facts:
            if fact["field"] == "cell_host_species":
                label = next((d for d in species_defs if _exact(d, fact["value"])), None)
                if label:
                    emit({**label, "category": "cell_host_species", "label_zh": f"细胞来源：{label['label_zh']}",
                          "label_en": f"Cell host: {label['label_en']}"}, "experiment.cell_host_species", fact["value"])
                else:
                    unresolved.append({"category": "cell_host_species", "value": fact["value"], "reason": "unknown_label"})
            if fact["field"] in {"cell_model", "sample", "readout"} and not any(
                    d["category"] == fact["field"] and _exact(d, fact["value"]) for d in definitions):
                unresolved.append({"category": fact["field"], "value": fact["value"], "reason": "unknown_label"})
    result["assay_keyword_status"] = "needs_review" if unresolved else "matched"
    return result
