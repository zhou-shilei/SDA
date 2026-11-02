"""Experimental Reader inputs. Production reader_payload remains unchanged.

compact_full is reversible representation only. catalogue is a separately
labelled retrieval experiment; selection never establishes biological identity.
"""

import copy
import re
from collections import Counter

from .contracts import INSTRUCTIONS
from .provider import digest

REVISION = 1
MODES = ("full", "compact_full", "catalogue", "catalogue_typed", "catalogue_native")

TRANSPORT_INSTRUCTIONS = """
INPUT REPRESENTATION (overrides only the source-row/initial_dataset wire format above):
transport.version=1 is lossless table encoding. initial_dataset and dataset_catalogue
collections with {tables:[...],size:N} decode to ordinary arrays. Each table has columns,
positions and rows: zip columns with each row; place that record at its zero-based
position. Empty collections decode to []. Every supplied record/field/value is retained.
Within dataset records, {"$s":N} means the exact string shared_strings[N]; literal
objects using this reserved shape are wrapped as {"$literal":...}. Expand these before
reading facts. Always OUTPUT the original string (especially experiment IDs), never a pool reference.
sources are consecutive groups {page,kind,table_id,columns,rows}. Zip columns with each
row; combine with the group's page/kind/table_id. Text groups omit row/column, both null.
The resulting source identity is id. Cite these original INTEGER IDs, not table positions.
Text is literal, untruncated. All ordinary grounding, identity and completeness rules apply.
"""

CATALOGUE_CONTEXT = """Reader requests contain dataset_catalogue, a GLOBAL directory of ALL initial
entities, chain links and experiments, plus observation addresses. initial_dataset contains
the complete existing records retrieved for this chunk. This is a candidate retrieval subset,
NOT the complete patent dataset. Use the global directory to reuse IDs and notice other
constructs; it does not establish that retrieved names, variants or shared sequences are identical.
Retrieval follows explicit names, SEQ IDs and source addresses, never inferred ownership.
Omitted detail is UNKNOWN, not evidence that facts/entities/relations do not exist.
Read EVERY supplied source, including unnamed passages and sources with no directory hits.
Extract new entities, definitions and relationships from original evidence even when no seed
record was retrieved. Explicit cross-construct/shared-chain statements need all cited evidence
sides. Never transfer ownership by suffix, proximity or catalogue similarity. Retain specific
context gaps as issues rather than guessing. ActionAgent later receives the FULL initial dataset
and all Readers' proposals, but cannot recover source evidence that nobody extracted.
The initial dataset and directory are prior context, not new evidence. Cite only IDs present
in this request's sources for new facts, even if prior records cite distant sources.
The host retains original seed evidence and consolidates accepted additions without silently
dropping existing facts.
"""


def pack_records(records):
    tables = {}
    for position, record in enumerate(records):
        keys = tuple(sorted(record))
        table = tables.setdefault(keys, {"columns": list(keys), "positions": [], "rows": []})
        table["positions"].append(position)
        table["rows"].append([copy.deepcopy(record[k]) for k in keys])
    return {"size": len(records), "tables": list(tables.values())}


def unpack_records(value):
    records = [None] * value["size"]
    for table in value["tables"]:
        for position, row in zip(table["positions"], table["rows"]):
            if records[position] is not None or len(row) != len(table["columns"]):
                raise ValueError("Repeated position or inconsistent transport row")
            records[position] = dict(zip(table["columns"], copy.deepcopy(row)))
    if any(r is None for r in records):
        raise ValueError("Transport omitted a record")
    return records


def pack_seed(seed):
    return {k: pack_records(v) if isinstance(v, list) and all(isinstance(r, dict) for r in v)
            else copy.deepcopy(v) for k, v in seed.items()}


def pack_sources(rows):
    groups = []
    for sid, page, kind, table, row, column, text in rows:
        columns = ["id", "text"] if row is None and column is None else ["id", "row", "column", "text"]
        if (not groups or any(groups[-1][k] != v for k, v in (
                ("page", page), ("kind", kind), ("table_id", table), ("columns", columns)))):
            groups.append({"page": page, "kind": kind, "table_id": table, "columns": columns, "rows": []})
        groups[-1]["rows"].append([sid, text] if len(columns) == 2 else [sid, row, column, text])
    return groups


def encode(payload):
    result = copy.deepcopy(payload)
    for key in ("initial_dataset", "dataset_catalogue"):
        if key in result:
            result[key] = pack_seed(result[key])
    counts = Counter()
    def count_strings(value):
        if isinstance(value, str):
            counts[value] += 1
        elif isinstance(value, list):
            for child in value:
                count_strings(child)
        elif isinstance(value, dict):
            for child in value.values():
                count_strings(child)
    for key in ("initial_dataset", "dataset_catalogue"):
        if key in result:
            count_strings(result[key])
    pool = [s for s, count in counts.items() if count >= 2 and len(s) >= 10]
    indices = {s: i for i, s in enumerate(pool)}
    def share(value):
        if isinstance(value, str) and value in indices:
            return {"$s": indices[value]}
        if isinstance(value, list):
            return [share(child) for child in value]
        if isinstance(value, dict):
            if set(value) in ({"$s"}, {"$literal"}):
                return {"$literal": copy.deepcopy(value)}
            return {k: share(child) for k, child in value.items()}
        return value
    for key in ("initial_dataset", "dataset_catalogue"):
        if key in result:
            result[key] = share(result[key])
    result["shared_strings"] = pool
    result["sources"] = pack_sources(result["sources"])
    result["transport"] = {"version": REVISION}
    return result


def decode(payload):
    result = copy.deepcopy(payload)
    if result.pop("transport")["version"] != REVISION:
        raise ValueError("Unknown Reader transport")
    pool = result.pop("shared_strings")
    def expand(value):
        if isinstance(value, dict):
            if set(value) == {"$s"}:
                return pool[value["$s"]]
            if set(value) == {"$literal"}:
                return value["$literal"]
            return {k: expand(child) for k, child in value.items()}
        if isinstance(value, list):
            return [expand(child) for child in value]
        return value
    for key in ("initial_dataset", "dataset_catalogue"):
        if key in result:
            result[key] = expand(result[key])
            result[key] = {k: unpack_records(v) if isinstance(v, dict) and set(v) == {"size", "tables"}
                           else v for k, v in result[key].items()}
    rows = []
    for group in result["sources"]:
        for row in group["rows"]:
            fact = dict(zip(group["columns"], row))
            rows.append([fact["id"], group["page"], group["kind"], group["table_id"],
                         fact.get("row"), fact.get("column"), fact["text"]])
    result["sources"] = rows
    return result


def catalogue_payload(payload, *, typed=False):
    seed = payload["initial_dataset"]
    ids = {row[0] for row in payload["sources"]}
    text = "\n".join(row[6] for row in payload["sources"])
    numbers = {int(n) for n in re.findall(r"SEQ\s*ID(?:\s*NO[.:：]?)?\s*[:：]?\s*(\d+)", text, re.I)}
    sequence_tables = {r[3] for r in payload["sources"] if r[3] and re.search(r"SEQ|sequence|序列", r[6], re.I)}
    numbers.update(int(r[6].strip()) for r in payload["sources"]
                   if r[3] in sequence_tables and r[6].strip().isdigit())
    selected, reasons = set(), {}
    for entity in seed["entities"]:
        why = []
        if set(entity.get("refs", [])) & ids:
            why.append("source_address")
        for name in dict.fromkeys([entity["name"], *entity.get("names", [])]):
            pattern = r"(?<![A-Za-z0-9])" + re.escape(name).replace(r"\ ", r"\s+") + r"(?![A-Za-z0-9])"
            if name.strip() and re.search(pattern, text, re.I):
                why.append("literal_name:" + name)
        if why:
            selected.add(entity["id"])
            reasons[entity["id"]] = why
    for link in seed.get("links", []):
        if link.get("seq") in numbers or set(link.get("refs", [])) & ids:
            selected.add(link["item"])
            reasons.setdefault(link["item"], []).append("number_or_link_source")
    def relevant_observation(observation):
        if observation["s"] in ids:
            return True
        if observation["item"] not in selected:
            return False
        if not typed:
            return True
        metric = observation.get("metric", "").strip()
        return bool(metric and re.search(r"(?<![A-Za-z0-9])" + re.escape(metric) + r"(?![A-Za-z0-9])", text, re.I))
    # All existing names and global links/experiment identities remain visible.
    catalogue = {
        "entities": [{k: copy.deepcopy(e[k]) for k in ("id", "name", "names", "identity_status", "table_id") if k in e}
                     for e in seed["entities"]],
        "links": [{k: copy.deepcopy(l[k]) for k in ("item", "region", "seq", "name", "refs") if k in l}
                  for l in seed.get("links", []) if l["item"] not in selected],
        "experiments": [{k: copy.deepcopy(e[k]) for k in ("id", "table", "label", "method", "region_id") if k in e}
                        for e in seed.get("experiments", [])],
        "observation_addresses": [{k: copy.deepcopy(o[k]) for k in ("s", "item", "experiment", "metric") if k in o}
                                  for o in seed.get("observations", []) if not relevant_observation(o)],
    }
    related_numbers = numbers | {l["seq"] for l in seed.get("links", [])
                                if l["item"] in selected and l.get("seq") is not None}
    subset = {k: copy.deepcopy(v) for k, v in seed.items()}
    subset["entities"] = [e for e in subset["entities"] if e["id"] in selected]
    subset["links"] = [l for l in subset.get("links", []) if l["item"] in selected]
    subset["observations"] = [o for o in subset.get("observations", []) if relevant_observation(o)]
    subset["annotations"] = [a for a in subset.get("annotations", [])
                             if a.get("subject_type") == "experiment" or a.get("subject") in selected]
    if "sequences" in subset:
        subset["sequences"] = [d for d in subset["sequences"] if d.get("seq") in related_numbers or set(d.get("refs", [])) & ids]
    result = copy.deepcopy(payload)
    result.update(initial_dataset=subset, dataset_catalogue=catalogue,
        retrieval={"policy": "Candidate context only; no ownership inference", "selected_items": sorted(selected),
                   **({"observation_policy": "Local addressed values plus selected-entity metrics literally present"} if typed else {}),
                   "reasons": reasons, "full_initial_hash": digest(seed),
                   "counts": {k: {"full": len(v), "retrieved": len(subset[k])} for k, v in seed.items() if isinstance(v, list)}})
    return result


def experimental_input(payload, mode):
    if mode not in MODES:
        raise ValueError("Unknown experimental Reader input mode")
    if mode == "full":
        return copy.deepcopy(payload), INSTRUCTIONS
    canonical = catalogue_payload(payload, typed=mode in {"catalogue_typed", "catalogue_native"}) if mode.startswith("catalogue") else copy.deepcopy(payload)
    if mode == "catalogue_native":
        counts = Counter(o["item"] for o in payload["initial_dataset"].get("observations", []))
        canonical["dataset_catalogue"].pop("observation_addresses")
        canonical["dataset_catalogue"]["observation_counts"] = [{"item": item, "count": count} for item, count in sorted(counts.items())]
        canonical["dataset_catalogue"]["experiments"] = []  # Complete records already retained in initial_dataset.
        canonical["retrieval"]["directory_policy"] = "All names; nonlocal observation counts instead of repeated measurement addresses; all experiments remain complete"
        context = CATALOGUE_CONTEXT.replace("plus observation addresses", "plus per-entity observation counts") + (
            "All initial experiment records remain in initial_dataset. Observation counts only describe stored prior data, "
            "not measurements to extract. Details outside the retrieved subset remain unavailable in this chunk.\n")
        start = INSTRUCTIONS.index("Reader requests contain initial_dataset:")
        stop = INSTRUCTIONS.index("Do not output existing item cards", start)
        return canonical, INSTRUCTIONS[:start] + context + INSTRUCTIONS[stop:]
    packed = encode(canonical)
    if decode(packed) != canonical:
        raise ValueError("Reader transport lost source characters or dataset fields")
    instructions = INSTRUCTIONS
    if mode.startswith("catalogue"):
        start = instructions.index("Reader requests contain initial_dataset:")
        stop = instructions.index("Do not output existing item cards", start)
        instructions = instructions[:start] + CATALOGUE_CONTEXT + instructions[stop:]
    return packed, instructions + TRANSPORT_INSTRUCTIONS


def adaptive_chunks(data, chunks):
    """Merge adjacent prose pairs; preserve table/sequence-dense original chunks.

    This is an experimental context budget policy, never a relevance filter.
    Every original core source remains exactly once, with stable source IDs.
    """
    from .inputs import source_row
    from .provider import compact
    amino = r"[ACDEFGHIKLMNPQRSTVWYX]{10,}"
    triplet = r"\b(?:Ala|Arg|Asn|Asp|Cys|Gln|Glu|Gly|His|Ile|Leu|Lys|Met|Phe|Pro|Ser|Thr|Trp|Tyr|Val)\b"
    protected = {}
    before = [s for c in chunks for s in c["core"]]
    order = {s: i for i, s in enumerate(before)}
    for chunk in chunks:
        rows = [data["sources"][s] for s in chunk["core"]]
        text = "\n".join(s["text"] for s in rows)
        reasons = []
        if any(s["kind"] == "cell" for s in rows):
            reasons.append("table_cells")
        if len(re.findall(amino, text)) >= 6:
            reasons.append("dense_sequence_letters")
        if len(re.findall(triplet, text, re.I)) >= 50:
            reasons.append("dense_three_letter_sequence")
        if reasons:
            protected[chunk["id"]] = reasons
    result, position = [], 0
    while position < len(chunks):
        group = [chunks[position]]
        if (position + 1 < len(chunks) and chunks[position]["id"] not in protected
                and chunks[position + 1]["id"] not in protected):
            group.append(chunks[position + 1])
        core = [s for c in group for s in c["core"]]
        context = sorted({s for c in group for s in c["context"]} - set(core), key=order.__getitem__)
        result.append({"id": f"c{len(result) + 1}", "core": core, "context": context,
            "source_characters": len(compact([source_row(data["sources"][s]) for s in context + core])),
            "original_chunks": [c["id"] for c in group],
            "preservation_reasons": protected.get(group[0]["id"], [])})
        position += len(group)
    after = [s for c in result for s in c["core"]]
    if before != after or len(after) != len(set(after)):
        raise ValueError("Adaptive chunking lost, reordered or repeated original core sources")
    return result
