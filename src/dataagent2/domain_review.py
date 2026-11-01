"""Literal domain candidates and explicit model review; never assign ownership here."""

from collections import Counter, defaultdict
import hashlib
import json
import re

from .contracts import REFS, S, array, obj
from .ledger import fact_region, ground_sequence, normalized


REVIEW_SCHEMA = obj({
    "item": S, "region": {"type": "string", "enum": ["VH", "VL"]},
    "status": {"type": "string", "enum": ["supported", "unresolved", "not_applicable"]},
    "candidate_ids": array(S), "targets": array(S), "refs": REFS,
    "reason": S, "missing_evidence": array(S),
})

REVIEW_INSTRUCTIONS = """
DOMAIN COMPLETENESS REVIEW (required before returning the complete dataset):
domain_review_input is a host-computed literal comparison index, NOT accepted biological facts.
Its definitions are grounded in original source characters; proposed region labels and construct
ownership still require your judgment against the cited sources. No fuzzy matches, inferred
boundaries, residue corrections, or name/suffix-based inheritance were used to make this index.
Each candidate is a FULL equality: full_chain == variable + constant, with separately proposed
domain definitions. Inspect their printed labels, full-chain ownership, any documented shared-chain
rules and alternative decompositions. When that evidence establishes the domain, ADD the domain
link to the complete dataset even if no Reader proposed it; reuse the separately numbered domain.
An exact, unique complete decomposition with confirmed domain labels and full-chain ownership can
establish the constant component's presence in that chain; do not demand an additional sentence
repeating that same constant ownership. The generic existence of a constant region alone, a prefix
match, conflicting labels, ambiguous alternatives, or a similar construct name is insufficient.
Return domain_reviews with exactly one row for EACH output entity and EACH of VH and VL.
Include EVERY supplied candidate ID for that entity/region, even when rejecting it. Review new
entities too. status=supported requires a supported domain in the actual dataset, a concrete
/dataset/links/N target, source refs, and a concise evidence-based explanation. status=unresolved
requires specific missing_evidence statements identifying what is absent, conflicting, or ambiguous;
"missing mapping", "insufficient evidence", or a generic need for domain boundaries is not an
adequate explanation for a supplied exact decomposition. Cite and discuss the actual candidate
definitions and construct mapping. status=not_applicable needs evidence that the entity is not a
relevant antibody domain owner. Do not invent VH/VL for antigens or other non-antibody molecules.
For unresolved candidates cite the full-chain mapping and all three compared definitions. Supported
and not_applicable rows have empty missing_evidence. The host checks review coverage, candidate
accounting, source scope, and consistency with the assembled dataset; it never chooses ownership.
"""


def _refs(fact):
    return set(fact.get("refs", [])) | {p["s"] for p in fact.get("parts", [])}


def build_domain_review_input(data, payload):
    """Compare only supplied, grounded complete definitions; retain alternative matches."""
    allowed = {row[0] for row in payload["sources"]}
    records = [(f"seed:{kind}:{i}", kind, fact)
               for kind in ("links", "sequences")
               for i, fact in enumerate(payload["initial_dataset"][kind])]
    records += [(a["id"], a["kind"], a["fact"]) for a in payload["reader_actions"]
                if a["kind"] in {"links", "sequences"}]
    entities = {e["id"]: {k: e.get(k) for k in ("id", "name", "names", "role")}
                for e in payload["initial_dataset"]["entities"]}
    names = defaultdict(set)
    for item, entity in entities.items():
        for name in [entity["name"], *entity["names"]]:
            names[normalized(name)].add(item)

    def item_id(value):
        if value in entities:
            return value
        matches = names.get(normalized(value), set())
        return next(iter(matches)) if len(matches) == 1 else value

    grouped, links = {}, defaultdict(set)
    for origin, kind, fact in records:
        refs = _refs(fact)
        if not refs or not refs <= allowed:
            continue
        region = fact_region(fact)
        key = (fact.get("seq"), None if fact.get("seq") is not None else fact.get("name"))
        if kind == "links":
            links[item_id(fact["item"]), region, key].update(refs)
            continue
        if region not in {"VH", "VL", "CH", "CL", "H_CHAIN", "L_CHAIN"}:
            continue
        if key[0] is not None and not any(re.search(rf"(?<!\d){key[0]}(?!\d)", data["sources"][sid]["text"])
                                          for sid in refs):
            continue
        try:
            value, _ = ground_sequence(fact["parts"], data["sources"])
        except (ValueError, KeyError, TypeError):
            continue
        if not value or (fact.get("value") is not None and fact["value"] != value):
            continue
        signature = (region, *key, value)
        definition = grouped.setdefault(signature, {"region": region, "seq": key[0], "name": key[1],
            "value": value, "length": len(value), "refs": set(), "proposals": []})
        definition["refs"].update(refs)
        definition["proposals"].append(origin)
    definitions, by_region = [], defaultdict(list)
    for definition in grouped.values():
        definition.update(id=f"d{len(definitions) + 1}", refs=sorted(definition["refs"]))
        definitions.append(definition)
        by_region[definition["region"]].append(definition)
    candidates = []
    for (item, chain, key), mapping_refs in sorted(links.items(), key=lambda kv: str(kv[0])):
        if chain not in {"H_CHAIN", "L_CHAIN"}:
            continue
        variable, constant = ("VH", "CH") if chain == "H_CHAIN" else ("VL", "CL")
        fulls = [d for d in by_region[chain] if (d["seq"], d["name"]) == key]
        for full in fulls:
            for domain in by_region[variable]:
                if not full["value"].startswith(domain["value"]):
                    continue
                for tail in by_region[constant]:
                    if full["value"] != domain["value"] + tail["value"]:
                        continue
                    identity = json.dumps([item, variable, full["id"], domain["id"], tail["id"]])
                    candidates.append({"id": "dc_" + hashlib.sha256(identity.encode()).hexdigest()[:12],
                        "item": item, "region": variable, "full_chain": full["id"],
                        "variable": domain["id"], "constant": tail["id"],
                        "mapping_refs": sorted(mapping_refs), "exact_concatenation": True,
                        "constant_mapping_refs": sorted(links.get((item, constant, (tail["seq"], tail["name"])), set()))})
    alternatives = Counter((c["item"], c["region"], c["full_chain"]) for c in candidates)
    for candidate in candidates:
        candidate["alternative_count"] = alternatives[candidate["item"], candidate["region"], candidate["full_chain"]]
    used = {c[k] for c in candidates for k in ("full_chain", "variable", "constant")}
    return {"revision": 1, "scope": "Literal candidates only; the model must verify labels and ownership.",
        "entities": list(entities.values()), "definitions": [d for d in definitions if d["id"] in used],
        "candidates": candidates}


def validate_domain_reviews(payload, response, assembled):
    """Enforce explicit coverage and evidence accounting, without accepting a candidate."""
    entities = {e["id"]: e for e in assembled["entities"]}
    expected = {(item, region) for item in entities for region in ("VH", "VL")}
    index = payload["domain_review_input"]
    definitions = {d["id"]: d for d in index["definitions"]}
    candidates = defaultdict(list)
    for candidate in index["candidates"]:
        item = candidate["item"]
        if item not in entities:
            matches = [e["id"] for e in entities.values()
                       if normalized(item) in {normalized(n) for n in [e["name"], *e["names"]]}]
            if len(matches) != 1:
                raise ValueError("Domain candidate has no unambiguous retained owner: " + item)
            item = matches[0]
        candidates[item, candidate["region"]].append(candidate)
    seen, errors = set(), []
    for review in response["domain_reviews"]:
        pair = review["item"], review["region"]
        if pair not in expected or pair in seen:
            errors.append("Unknown or repeated domain review: " + str(pair))
            continue
        seen.add(pair)
        required = {c["id"] for c in candidates[pair]}
        if set(review["candidate_ids"]) != required or len(review["candidate_ids"]) != len(required):
            errors.append("Domain review omitted or invented candidates: " + str(pair))
        if not review["reason"].strip() or not review["refs"]:
            errors.append("Domain review needs an explanation and evidence: " + str(pair))
        supported = entities[pair[0]]["sequences"][pair[1]]["status"] == "supported"
        if (review["status"] == "supported") != supported:
            errors.append("Domain review contradicts the assembled domain: " + str(pair))
        if review["status"] == "unresolved":
            if not review["missing_evidence"] or any(not s.strip() for s in review["missing_evidence"]):
                errors.append("Unresolved domain needs a specific evidence gap: " + str(pair))
            generic = {"missing mapping", "missing_mapping", "insufficient evidence", "missing evidence",
                       "boundary missing", "missing definition", "unknown", "unresolved"}
            if review["missing_evidence"] and all(s.strip().casefold().rstrip(".") in generic
                                                 for s in review["missing_evidence"]):
                errors.append("Unresolved domain has only a generic evidence gap: " + str(pair))
        elif review["missing_evidence"]:
            errors.append("Resolved or inapplicable domain cannot have missing_evidence: " + str(pair))
        targets = []
        for path in review["targets"]:
            pieces = path.split("/")
            if len(pieces) != 4 or pieces[1] != "dataset" or pieces[2] not in {"links", "issues"} or not pieces[3].isdigit():
                errors.append("Domain review needs a concrete link/issue target: " + path)
                continue
            try:
                targets.append(response["dataset"][pieces[2]][int(pieces[3])])
            except IndexError:
                errors.append("Domain review target does not exist: " + path)
        if supported and not any(t.get("item") == pair[0] and t.get("region") == pair[1] for t in targets):
            errors.append("Supported domain review has no corresponding link target: " + str(pair))
        for candidate in candidates[pair]:
            # A supported direct mapping may use other evidence. Unresolved exact candidates
            # must show that each actual component and the owner mapping were inspected.
            if review["status"] != "unresolved":
                continue
            groups = [candidate["mapping_refs"], *[definitions[candidate[k]]["refs"]
                       for k in ("full_chain", "variable", "constant")]]
            if any(not set(group) & set(review["refs"]) for group in groups):
                errors.append("Unresolved candidate review lacks compared evidence: " + candidate["id"])
    if seen != expected:
        errors.append("Missing per-entity domain reviews: " + str(sorted(expected - seen)))
    if errors:
        raise ValueError("Domain completeness review errors:\n" + "\n".join(errors))
    return {"reviewed_domains": len(seen), "candidates": len(index["candidates"]),
            "statuses": dict(Counter(r["status"] for r in response["domain_reviews"]))}
