"""Ground compact model facts in immutable OCR; assemble without model re-reading."""

from __future__ import annotations

from collections import defaultdict
import copy
import re

from .provider import compact, digest

AA = set("ACDEFGHIKLMNPQRSTVWY")
CHAIN_REGIONS = ("VH", "VL", "H_CHAIN", "L_CHAIN", "CH", "CL")


def fact_region(fact):
    """Read legacy 'other' constant labels without changing their provenance."""
    region = fact["region"]
    if region != "other":
        return region
    name = fact.get("name") or ""
    for label, chinese in (("CL", "轻链恒定区"), ("CH", "重链恒定区")):
        if chinese in name or re.search(r"(?<![A-Za-z])" + label + r"(?![A-Za-z0-9])", name):
            return label
    return region


def normalized(text):
    return re.sub(r"\s+", "", str(text)).casefold()


def metric_key(metric):
    value = metric.strip()
    for endpoint in ("IC50", "EC50"):
        if re.search(r"(?:^|_)" + endpoint + r"(?:$|_)", value, re.I):
            return endpoint
    # KD (affinity) and kd (dissociation rate) are different endpoints.
    return value if value in ("KD", "kd") else value.casefold()


def residues(text):
    return re.sub(r"\s+", "", re.sub(r"<br\s*/?>", "", text, flags=re.I))


def ground_sequence(parts, sources):
    fragments, covered = [], defaultdict(list)
    for part in parts:
        sid, begin, end = part["s"], part["start"], part["end"]
        if sources[sid].get("sequence_style"):
            from document.sequence_styles import validate_style_source
            validate_style_source(sources[sid], {s["source_ref"]: s for s in sources.values()})
        if sources[sid].get("sequence_ocr", {}).get("status") not in {None, "success"}:
            raise ValueError("Table sequence re-recognition failed; old PPVL transcription is audit-only")
        text = sources[sid]["text"]
        if not begin and not end:
            start, stop = 0, len(text)
        else:
            if not begin or not end:
                raise ValueError("Both literal anchors are required")
            starts = [m.start() for m in re.finditer(re.escape(begin), text)]
            ends = [m.end() for m in re.finditer(re.escape(end), text)]
            candidates = [(a, b) for a in starts for b in ends if b >= a + len(begin)]
            if len(candidates) != 1:
                raise ValueError("Sequence anchors absent or ambiguous")
            start, stop = candidates[0]
        if any(start < b and a < stop for a, b in covered[sid]):
            raise ValueError("Overlapping sequence fragments")
        quote = text[start:stop]
        value = residues(quote)
        if not value or set(value) - AA:
            raise ValueError("Noncanonical characters in literal sequence; no OCR correction allowed")
        covered[sid].append((start, stop))
        fragments.append({"s": sid, "start": start, "end": stop,
                          "quote": quote, "sequence": value,
                          "source_sha256": digest(text)})
    if not fragments:
        raise ValueError("Sequence definition requires literal fragments")
    return "".join(f["sequence"] for f in fragments), fragments


class Ledger:
    def __init__(self, data):
        self.data, self.sources = data, data["sources"]
        self.entities = {e["id"]: copy.deepcopy(e) for e in data["seed"]["entities"]}
        self.facts = {k: [] for k in ("mentions", "links", "sequences", "experiments", "observations", "relations", "issues")}
        self.keys = {k: set() for k in self.facts}
        self.rejections = []
        for k in ("links", "experiments", "observations"):
            for fact in data["seed"][k]:
                self.add(k, copy.deepcopy(fact))
        if data.get("style_definitions"):
            from .contracts import empty_response
            response = empty_response()
            response["sequences"] = data["style_definitions"]
            self.apply(response, list(self.sources), "style-review")

    def add(self, kind, value):
        semantic = {k: v for k, v in value.items() if k not in ("chunk", "origin", "fragments", "original_refs")}
        key = digest(semantic)
        if key not in self.keys[kind]:
            self.keys[kind].add(key)
            self.facts[kind].append(value)

    def item_id(self, name):
        if name in self.entities:
            return name
        candidates = [e["id"] for e in self.entities.values()
                      if normalized(name) in {normalized(n) for n in [e["name"], *e["names"]]}]
        if len(candidates) != 1:
            raise ValueError("Unknown or ambiguous item identity: " + name)
        return candidates[0]

    def apply(self, response, allowed, chunk):
        allowed = set(allowed)

        def require(refs):
            if not refs or any(type(r) is not int or r not in allowed for r in refs):
                raise ValueError("Facts require source IDs from this request")

        for new in response["new_items"]:
            try:
                require(new["refs"])
                if not new["name"].strip() or not any(normalized(new["name"]) in normalized(self.sources[r]["text"]) for r in new["refs"]):
                    raise ValueError("New item name is absent from cited text")
                try:
                    self.item_id(new["name"])
                except ValueError:
                    identity = "n" + digest(new["name"])[:12]
                    self.entities.setdefault(identity, {"id": identity, "native_id": None,
                        "name": new["name"], "names": [new["name"]], "role": "unknown", "refs": new["refs"]})
            except (ValueError, KeyError) as exc:
                self.rejections.append({"chunk": chunk, "kind": "new_items", "error": str(exc), "fact": new})
        for kind in self.facts:
            for raw in response[kind]:
                fact = copy.deepcopy(raw)
                try:
                    refs = fact.get("refs", [fact["s"]] if "s" in fact else [])
                    require(refs)
                    if "items" in fact:
                        fact["items"] = [self.item_id(x) for x in fact["items"]]
                    if "item" in fact:
                        fact["item"] = self.item_id(fact["item"])
                    if kind == "relations":
                        fact["subject"] = self.item_id(fact["subject"])
                        if fact["relation"] != "tested_in":
                            fact["object"] = self.item_id(fact["object"])
                        if fact["subject"] == fact["object"]:
                            raise ValueError("Self relationship")
                    if kind == "sequences":
                        require([p["s"] for p in fact["parts"]])
                        sequence, fragments = ground_sequence(fact.pop("parts"), self.sources)
                        fact.update(value=sequence, fragments=fragments)
                        styled = [self.sources[f["s"]].get("sequence_style") for f in fragments]
                        if styled and all(styled):
                            fact.update(boundary_method="printed_image_style", seq_id_scope="full_chain")
                        fact["refs"] = list(dict.fromkeys(refs + [f["s"] for f in fragments]))
                        if fact["region"] in ("VH", "VL"):
                            low, high = (70, 180) if fact["region"] == "VH" else (65, 160)
                            if not low <= len(sequence) <= high:
                                raise ValueError("Claimed VH/VL has implausible length; boundary needs review")
                    if kind in ("links", "sequences") and fact.get("seq") is not None:
                        if fact["seq"] < 1 or not any(re.search(rf"(?<!\d){fact['seq']}(?!\d)", self.sources[r]["text"]) for r in refs):
                            raise ValueError("Claimed SEQ ID is absent from cited sources")
                    if kind == "links" and fact["seq"] is None and not fact.get("name"):
                        raise ValueError("Unnumbered mapping requires a definition name")
                    if kind == "observations":
                        require([fact["s"]])
                        if not fact["value"] or fact["value"] not in self.sources[fact["s"]]["text"]:
                            raise ValueError("Measurement must be copied verbatim from its value source")
                        if fact["unit"] and not any(fact["unit"] in self.sources[r]["text"] for r in refs):
                            raise ValueError("Measurement unit lacks literal source support")
                        fact["table"] = self.sources[fact["s"]]["table_id"]
                    if kind == "experiments" and fact.get("table"):
                        if fact["table"] not in {s["table_id"] for s in self.sources.values()}:
                            raise ValueError("Unknown result table")
                    fact.update(chunk=chunk, origin="reader" if chunk.startswith("c") else "answer")
                    self.add(kind, fact)
                except (ValueError, KeyError) as exc:
                    self.rejections.append({"chunk": chunk, "kind": kind, "error": str(exc), "fact": raw})

    def assemble(self):
        # Explicit equivalences enable reuse, while all entity identities stay visible.
        parents = {k: k for k in self.entities}

        def find(x):
            while parents[x] != x:
                x = parents[x]
            return x

        for r in self.facts["relations"]:
            if r["relation"] == "same_entity":
                parents[find(r["subject"])] = find(r["object"])
        families = defaultdict(set)
        for k in parents:
            families[find(k)].add(k)
        definitions = self.facts["sequences"]
        failed_rows = defaultdict(list)
        for sid, source in self.sources.items():
            if source.get("sequence_ocr", {}).get("status") not in {None, "success"}:
                failed_rows[(source.get("table_id"), source.get("row"))].append(sid)
        failed_numbers = defaultdict(set)
        for source in self.sources.values():
            if source.get("table_id"):
                for number in re.findall(r"SEQ\s*ID(?:\s*NO[.:：]?)?\s*[:：]?\s*(\d+)", source["text"], re.I):
                    failed_numbers[int(number)].update(failed_rows.get((source["table_id"], source.get("row")), []))
        output, conflicts = [], []
        for key, entity in self.entities.items():
            item = copy.deepcopy(entity)
            item["sequences"] = {}
            relatives = families[find(key)]
            links = [l for l in self.facts["links"] if l["item"] in relatives]
            for chain in CHAIN_REGIONS:
                matching = [l for l in links if fact_region(l) == chain]
                # A variable-domain SEQ ID and a full-chain SEQ ID are separate
                # namespaces of scope, even when both belong to one antibody.
                # Full-chain ownership is a fallback only when no domain mapping exists.
                if not matching and chain in ("VH", "VL"):
                    matching = [l for l in links if l["region"] == ("H_CHAIN" if chain == "VH" else "L_CHAIN")]
                candidates = []
                for link in matching:
                    for d in definitions:
                        identity_matches = (d["seq"] == link["seq"] if link["seq"] is not None else
                            d["seq"] is None and d.get("name") and normalized(d["name"]) == normalized(link["name"]))
                        if identity_matches and fact_region(d) == chain:
                            candidates.append((link, d))
                # Raw table characters prevail over a contradictory plain page transcription.
                table_candidates = [(l, d) for l, d in candidates if all(self.sources[f["s"]]["kind"] == "cell" for f in d["fragments"])]
                selected = table_candidates or candidates
                all_values = {d["value"] for _, d in candidates}
                values = {d["value"] for _, d in selected}
                numbers = {l["seq"] for l in matching if l["seq"] is not None}
                if len(all_values) > 1:
                    conflicts.append({"item": key, "chain": chain, "kind": "sequence_conflict",
                        "values": [{"seq": d["seq"], "sha256": digest(d["value"]), "refs": d["refs"]} for _, d in candidates]})
                if len(values) == 1 and len(numbers) <= 1:
                    link, definition = selected[0]
                    item["sequences"][chain] = {"status": "supported", "value": definition["value"],
                        "seq_id": definition["seq"], "definition_refs": definition["refs"],
                        "mapping_refs": link["refs"], "fragments": definition["fragments"],
                        "source_conflict": len(all_values) > 1,
                        **{k: definition[k] for k in ("boundary_method", "seq_id_scope") if k in definition},
                        "validation": "literal characters checked; ownership and boundaries are model judgments"}
                else:
                    item["sequences"][chain] = {"status": "conflict" if len(values) > 1 or len(numbers) > 1 else "missing",
                        "value": None, "seq_ids": sorted(numbers),
                        "reason": "conflicting mappings/definitions" if len(values) > 1 or len(numbers) > 1 else
                                  "definition/boundary missing" if matching else "mapping missing"}
                    failed_sources = sorted({p["s"] for rejected in self.rejections
                        if rejected["kind"] == "sequences"
                        and fact_region(rejected["fact"]) == chain
                        and any((l["seq"] == rejected["fact"].get("seq") if l["seq"] is not None else
                                 l.get("name") == rejected["fact"].get("name")) for l in matching)
                        for p in rejected["fact"].get("parts", [])
                        if p["s"] in self.sources
                        and self.sources[p["s"]].get("sequence_ocr", {}).get("status") not in {None, "success"}})
                    failed_sources = sorted(set(failed_sources) | {sid for link in matching for ref in link["refs"]
                        for sid in failed_rows.get((self.sources[ref].get("table_id"), self.sources[ref].get("row")), [])
                        if self.sources[ref].get("table_id")})
                    # Mapping tables and the actual definition can be on
                    # different pages. Diagnose the numbered full chain too.
                    if chain in ("VH", "VL"):
                        full_region = "H_CHAIN" if chain == "VH" else "L_CHAIN"
                        if not any(d["seq"] in numbers and fact_region(d) == full_region for d in definitions):
                            failed_sources = sorted(set(failed_sources) | {sid for n in numbers for sid in failed_numbers[n]})
                    if failed_sources:
                        item["sequences"][chain].update(reason="table sequence OCR failed", failure_kind="ocr_failure",
                            failed_source_ids=failed_sources, recovery_action="rerun_table_sequence_ocr")
            item["missing"] = [c for c in ("VH", "VL") if item["sequences"][c]["status"] != "supported"]
            item["relations"] = [r for r in self.facts["relations"] if key in (r["subject"], r["object"])]
            output.append(item)
        # A physical value cell / endpoint is one observation; duplicate reads are merged.
        observations, measurement_conflicts = {}, []
        for raw in self.facts["observations"]:
            key = (raw["s"], metric_key(raw["metric"]), raw["value"])
            if key in observations and observations[key]["item"] != raw["item"]:
                measurement_conflicts.append({"kind": "measurement_owner_conflict", "candidates": [observations[key], raw]})
            elif key not in observations:
                observations[key] = copy.deepcopy(raw)
            else:
                observations[key]["refs"] = sorted(set(observations[key]["refs"] + raw["refs"]))
                if not observations[key].get("experiment"):
                    observations[key]["experiment"] = raw.get("experiment")
        for conflict in measurement_conflicts:
            r = conflict["candidates"][0]
            observations.pop((r["s"], metric_key(r["metric"]), r["value"]), None)
        experiments = self.facts["experiments"]
        for o in observations.values():
            if not o.get("experiment"):
                matches = {e["id"] for e in experiments if e.get("table") and e["table"] == o.get("table")}
                if len(matches) == 1:
                    o["experiment"] = next(iter(matches))
        experiment_ids = {e["id"] for e in experiments}
        # Page OCR often repeats a physical table value. Merge only when one
        # table cell matches the same entity, explicit experiment, endpoint,
        # literal value, unit and page. Separate table cells/replicates remain.
        def semantic_key(o):
            if o.get("experiment") not in experiment_ids:
                return None
            return (find(o["item"]), o["experiment"], metric_key(o["metric"]), o["value"].strip(),
                    normalized(o.get("unit") or ""), self.sources[o["s"]]["page"])

        cells = defaultdict(list)
        for key, o in observations.items():
            if self.sources[o["s"]]["kind"] == "cell" and semantic_key(o) is not None:
                cells[semantic_key(o)].append(key)
        merged_sources = 0
        for key, o in list(observations.items()):
            matches = cells.get(semantic_key(o), [])
            if self.sources[o["s"]]["kind"] == "text" and len(matches) == 1:
                target = observations[matches[0]]
                target["refs"] = sorted(set(target["refs"] + o["refs"] + [o["s"]]))
                target.setdefault("corroborating_sources", []).append(o["s"])
                if target["metric"] != o["metric"]:
                    target.setdefault("metric_aliases", []).append(o["metric"])
                observations.pop(key)
                merged_sources += 1
        for item in output:
            item["observation_ids"] = [digest(k)[:16] for k, o in observations.items() if o["item"] == item["id"]]
        records = [{"id": digest(k)[:16], **v, "experiment_status": "supported" if v.get("experiment") in experiment_ids else "missing"}
                   for k, v in observations.items()]
        return {"document_id": self.data["document_id"], "entities": output,
                "sequences": definitions, "experiments": experiments, "observations": records,
                "relations": self.facts["relations"], "conflicts": conflicts + measurement_conflicts,
                "issues": self.facts["issues"], "rejected_facts": self.rejections,
                "summary": {"entities": len(output), "seed_entities": len(self.data["seed"]["entities"]),
                    "variable_chains": sum(e["sequences"][c]["status"] == "supported" for e in output for c in ("VH", "VL")),
                    "complete_pairs": sum(not e["missing"] for e in output),
                    "seed_variable_chains": sum(e["sequences"][c]["status"] == "supported" for e in output
                                                if e["id"].startswith("i") for c in ("VH", "VL")),
                    "seed_complete_pairs": sum(not e["missing"] for e in output if e["id"].startswith("i")),
                    "full_chain_slots": sum(e["sequences"][c]["status"] == "supported" for e in output for c in ("H_CHAIN", "L_CHAIN")),
                    "unique_sequence_values": len({d["value"] for d in definitions}),
                    "unique_variable_sequence_values": len({d["value"] for d in definitions if d["region"] in ("VH", "VL")}),
                    "observations": len(records), "experiments": len({e["id"] for e in experiments}),
                    "merged_text_observation_sources": merged_sources,
                    "missing_experiment_observations": sum(o["experiment_status"] == "missing" for o in records),
                    "sequence_definitions": len(definitions), "conflicts": len(conflicts) + len(measurement_conflicts),
                    "rejected_facts": len(self.rejections)}}
