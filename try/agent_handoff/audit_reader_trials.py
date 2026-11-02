"""Read-only comparisons of completed Reader trials; no model and no gold labels."""

from collections import Counter, defaultdict
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from dataagent2.inputs import read
from dataagent2.ledger import normalized
from dataagent2.provider import write_json


def compare_named(before, after, seed_ids):
    old = {e["id"]: e for e in before["entities"]}
    new = {e["id"]: e for e in after["entities"]}
    by_name = defaultdict(list)
    for entity in after["entities"]:
        by_name[normalized(entity["name"])].append(entity["id"])
    matches, unmatched, used = {}, [], set()
    for item, entity in old.items():
        if item in seed_ids and item in new:
            target = item
        else:
            candidates = by_name[normalized(entity["name"])]
            target = candidates[0] if len(candidates) == 1 else None
        if target is None or target in used:
            unmatched.append({"item": item, "name": entity["name"]})
        else:
            matches[item] = target
            used.add(target)
    added, lost, changed = [], [], []
    for item, entity in old.items():
        target = new.get(matches.get(item))
        for region in ("VH", "VL"):
            old_slot = entity["sequences"][region]
            new_slot = target["sequences"][region] if target else {}
            a = old_slot.get("value") if old_slot["status"] == "supported" else None
            b = new_slot.get("value") if new_slot.get("status") == "supported" else None
            row = {"name": entity["name"], "before_item": item, "after_item": target["id"] if target else None,
                   "region": region, "before_seq": old_slot.get("seq_id"), "after_seq": new_slot.get("seq_id")}
            if a and not b:
                lost.append(row)
            elif b and not a:
                added.append(row)
            elif a and b and a != b:
                changed.append(row)
    for item in new.keys() - used:
        for region in ("VH", "VL"):
            slot = new[item]["sequences"][region]
            if slot["status"] == "supported":
                added.append({"name": new[item]["name"], "before_item": None, "after_item": item,
                              "region": region, "before_seq": None, "after_seq": slot.get("seq_id")})

    def observations(dataset, numeric=False):
        names = {e["id"]: normalized(e["name"]) for e in dataset["entities"]}
        return Counter((names[o["item"]], o["s"], normalized(o["metric"]), o["value"], o.get("unit"),
                        o.get("condition"), o.get("donor"), o.get("replicate"))
                       for o in dataset["observations"] if not numeric or re.search(r"\d", o["value"]))
    ob, oa = observations(before), observations(after)
    nb, na = observations(before, True), observations(after, True)
    return {"matching_policy": "Stable initial IDs; otherwise unambiguous exact canonical names after case/whitespace normalization. No fuzzy identity inference.",
        "matched_entities": len(matches), "unmatched_historical_entities": unmatched,
        "new_entity_names": [new[i]["name"] for i in new.keys() - used],
        "added": added, "lost": lost, "changed_residues": changed,
        "measurements_lost": sum((ob - oa).values()), "measurements_added": sum((oa - ob).values()),
        "numeric_measurements_lost": sum((nb - na).values()), "numeric_measurements_added": sum((na - nb).values()),
        "old_only_measurement_rows": [list(v) for v in (ob - oa).elements()],
        "new_only_measurement_rows": [list(v) for v in (oa - ob).elements()]}


def audit(baseline, folder):
    before, after = read(baseline / "dataset.first_pass.json"), read(folder / "action/dataset.json")
    a, b = read(baseline / "facts.first_pass.json"), read(folder / "action/facts.json")
    seed_ids = {e["id"] for e in read(baseline / "seed.json")["entities"]}
    result = compare_named(before, after, seed_ids)
    key = lambda d: (d["seq"], d["region"], normalized(d.get("name") or "") if d["seq"] is None else None, d["value"])
    old = {key(d) for d in a["sequences"]}
    new = {key(d) for d in b["sequences"]}
    result["definitions"] = {"historical_records": len(a["sequences"]), "new_records": len(b["sequences"]),
        "old_only_count": len(old - new), "new_only_count": len(new - old),
        "old_only_ids": [d[0] for d in sorted(old - new, key=str)],
        "old_only_records": [list(d) for d in sorted(old - new, key=str)],
        "new_only_records": [list(d) for d in sorted(new - old, key=str)]}
    aa_key = lambda d: (d["seq"], d["value"])
    aa_before = {aa_key(d) for d in a["sequences"]}
    aa_after = {aa_key(d) for d in b["sequences"]}
    result["definitions"]["literal_id_value_pairs_lost"] = [list(d) for d in sorted(aa_before - aa_after, key=str)]
    result["definitions"]["named_labels_missing"] = [
        {"seq": d["seq"], "region": d["region"], "name": d["name"]}
        for d in a["sequences"] if d.get("name") and not any(
            aa_key(n) == aa_key(d) and normalized(n.get("name") or "") == normalized(d["name"])
            for n in b["sequences"])]
    result["native_summary"] = after["summary"]
    result["computed_score_candidates"] = [o for o in after["observations"]
        if re.search(r"potts|cbo|model.score", o["metric"], re.I)]
    result["scope"] = "Literal/structural comparison to a prior model output, not independent accuracy validation. Added pairs/measurements are not automatically improvements."
    write_json(folder / "comparison.audit.json", result)
    return result


if __name__ == "__main__":
    root = Path(sys.argv[1])
    baseline = Path(read(root / "work_state.json")["baseline"])
    results = {}
    for name, mode in [("full_large_original", "full"), ("full_adaptive_native", "catalogue_native"), ("full_adaptive_full", "full")]:
        folder = root / name / mode
        if (folder / "action/dataset.json").is_file() and read(folder / "action/experiment.json", {}).get("status") == "complete":
            result = audit(baseline, folder)
            results[name] = {k: result[k] for k in ("added", "lost", "changed_residues", "unmatched_historical_entities",
                "measurements_lost", "numeric_measurements_lost", "measurements_added", "native_summary")}
            results[name]["definition_ids_lost"] = result["definitions"]["old_only_ids"]
            results[name]["computed_score_candidates"] = len(result["computed_score_candidates"])
    print(json.dumps(results, ensure_ascii=False, indent=2))
