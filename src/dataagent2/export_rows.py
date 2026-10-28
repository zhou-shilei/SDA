"""Export one review row per item, plus separately linked measurements."""

import argparse
import csv
import json
from pathlib import Path

from .structure import CHAIN_REGIONS, normalize_region


def write_csv(path, rows, fields):
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def export(dataset_path, output):
    dataset = json.loads(Path(dataset_path).read_text())
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    experiment_ids = {e["id"] for e in dataset["experiments"]}
    names = {e["id"]: e["name"] for e in dataset["entities"]}
    measurements, items = [], []
    for observation in dataset["observations"]:
        measurements.append({**observation, "item_name": names[observation["item"]],
                             "refs": json.dumps(observation["refs"])})
    for entity in dataset["entities"]:
        observations = [o for o in dataset["observations"] if o["item"] == entity["id"]]
        experiments = {o["experiment"] for o in observations if o.get("experiment") in experiment_ids}
        experiments.update(r["object"] for r in entity.get("relations", []) if
                           r["relation"] == "tested_in" and r["subject"] == entity["id"] and r["object"] in experiment_ids)
        row = {"item": entity["id"], "name": entity["name"], "role": entity["role"],
               "experiment_ids": json.dumps(sorted(experiments)), "experiment_missing": not experiments,
               "observation_count": len(observations), "annotations": json.dumps(entity.get("annotations", []), ensure_ascii=False)}
        for chain in CHAIN_REGIONS:
            sequence = entity["sequences"].get(chain, {"status": "missing"})
            ids = sequence.get("mapping_seq_ids", sequence.get("seq_ids", []))
            mapping_refs = [r for link in entity.get("sequence_links", []) if normalize_region(link["region"]) == chain for r in link.get("refs", [])]
            row.update({chain: sequence.get("value") or "", chain + "_status": sequence["status"],
                chain + "_seq_id": sequence.get("seq_id", ids[0] if len(ids) == 1 else None),
                chain + "_seq_ids": json.dumps(ids),
                chain + "_source_chain_seq_ids": json.dumps(sequence.get("source_chain_seq_ids", [])),
                chain + "_seq_id_scope": sequence.get("seq_id_scope", "as_recorded"),
                chain + "_boundary_method": sequence.get("boundary_method", ""),
                chain + "_refs": json.dumps(sorted(set(sequence.get("definition_refs", []) + sequence.get("mapping_refs", []) + mapping_refs)))})
        row["missing_values"] = json.dumps([c for c in ("VH", "VL") if not row[c]] + ([] if experiments else ["experiment"]))
        row["missing_explicit_domains"] = json.dumps(entity.get("missing_explicit_domains", entity["missing"]))
        items.append(row)
    fields = ["item", "name", "role"]
    for chain in CHAIN_REGIONS:
        fields += [chain, chain + "_status", chain + "_seq_id", chain + "_seq_ids", chain + "_source_chain_seq_ids",
                   chain + "_seq_id_scope", chain + "_boundary_method", chain + "_refs"]
    fields += ["experiment_ids", "experiment_missing", "observation_count", "missing_values", "missing_explicit_domains", "annotations"]
    write_csv(output / "items.csv", items, fields)
    write_csv(output / "observations.csv", measurements,
              ["id", "item", "item_name", "experiment", "experiment_status", "metric", "value", "unit", "condition", "donor", "replicate", "s", "refs"])
    experiments = [{**e, "refs": json.dumps(e["refs"]), "annotations": json.dumps(e.get("annotations", []), ensure_ascii=False)} for e in dataset["experiments"]]
    write_csv(output / "experiments.csv", experiments,
              ["id", "label", "protocol", "region_id", "table", "method", "target", "species", "system", "conditions", "annotations", "refs"])
    return {"items": len(items), "observations": len(measurements), "experiment_profiles": len(experiments),
            "output": str(output.resolve())}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    print(json.dumps(export(args.dataset, args.output), ensure_ascii=False))
