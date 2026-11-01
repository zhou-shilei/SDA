"""Compact, source-addressed graph projections; never repeat stored residues or values."""

import copy
import re

from .structure import normalize_region

REVISION = 1


def build_index(data, *, entities=None, facts=None):
    seed = data["seed"]
    entities = entities if entities is not None else seed["entities"]
    facts = facts if facts is not None else seed
    links = [{k: copy.deepcopy(link[k]) for k in ("item", "region", "original_region", "seq", "name", "refs", "origin", "chunk") if k in link}
             for link in facts.get("links", [])]
    for link in links:
        link["region"] = normalize_region(link["region"])
    definitions = [{"seq": s.get("seq"), "name": s.get("name"), "region": normalize_region(s["region"]),
                    "length": len(s["value"]) if s.get("value") else None,
                    "refs": s.get("refs", []), "status": "defined" if s.get("value") else "candidate"}
                   for s in facts.get("sequences", data.get("style_definitions", []))]
    items = []
    for entity in entities:
        item = {k: copy.deepcopy(entity[k]) for k in ("id", "name", "names", "role", "refs", "identity_status", "table_id") if k in entity}
        regions = {}
        for link in links:
            if link["item"] != entity["id"]:
                continue
            region = regions.setdefault(link["region"], {"seq_ids": [], "mapping_status": "supported", "definition_status": "missing"})
            if link.get("seq") is not None and link["seq"] not in region["seq_ids"]:
                region["seq_ids"].append(link["seq"])
            if len(region["seq_ids"]) > 1:
                region["mapping_status"] = "conflict"
            matches = [d for d in definitions if d["region"] == link["region"] and
                       (d["seq"] == link.get("seq") if link.get("seq") is not None else d.get("name") == link.get("name"))]
            if matches:
                region["definition_status"] = "defined" if any(d["status"] == "defined" for d in matches) else "candidate"
        for region, slot in entity.get("sequences", {}).items():
            if region in regions or slot.get("status") == "supported" or slot.get("source_chain_seq_ids"):
                regions[region] = {k: copy.deepcopy(slot[k]) for k in (
                    "status", "mapping_status", "mapping_seq_ids", "seq_id", "seq_ids", "seq_id_scope", "source_chain_seq_ids", "evidence_role") if k in slot}
        item["regions"] = regions
        items.append(item)
    return {"revision": REVISION, "document": data["document_id"],
        "items": items,
        "links": links, "definitions": definitions,
        "experiments": copy.deepcopy(facts.get("experiments", [])),
        "observations": [{k: copy.deepcopy(o[k]) for k in ("item", "experiment", "metric", "s", "refs", "table", "region_id", "condition", "donor", "replicate") if k in o}
                         for o in facts.get("observations", [])],
        "relations": copy.deepcopy(facts.get("relations", [])),
        "annotations": copy.deepcopy(facts.get("annotations", []))}


def chunk_index(data, chunk):
    """All names remain in the outer catalogue; detail follows names, IDs and evidence."""
    index = build_index(data)
    ids = set(chunk["core"] + chunk["context"])
    texts = [data["sources"][sid]["text"] for sid in ids]
    normalized = "\n".join(re.sub(r"\s+", "", t).casefold() for t in texts)
    numbers = {int(n) for text in texts for n in re.findall(r"SEQ\s*ID(?:\s*NO[.:：]?)?\s*[:：]?\s*(\d+)", text, re.I)}
    numbers.update(int(t.strip()) for t in texts if t.strip().isdigit())
    selected = {e["id"] for e in index["items"] if set(e.get("refs", [])) & ids or any(
        re.sub(r"\s+", "", name).casefold() in normalized
        for name in [e["name"], *e.get("names", [])] if len(name.strip()) >= 2)}
    selected.update(l["item"] for l in index["links"] if l.get("seq") in numbers or set(l.get("refs", [])) & ids)
    index["items"] = [e for e in index["items"] if e["id"] in selected]
    index["links"] = [l for l in index["links"] if l["item"] in selected]
    numbers.update(l["seq"] for l in index["links"] if l.get("seq") is not None)
    index["definitions"] = [d for d in index["definitions"] if d.get("seq") in numbers or set(d["refs"]) & ids]
    index["observations"] = [o for o in index["observations"] if o["s"] in ids]
    # Keep the experiment catalogue: a prose-only method chunk may refer to a
    # distant result table, and two column groups of one table may be different assays.
    index["annotations"] = [a for a in index["annotations"] if a.get("subject_type") == "experiment" or a.get("subject") in selected]
    return index
