"""Export one review row per item, plus separately linked measurements."""

import argparse
import csv
import json
from pathlib import Path


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
               "observation_count": len(observations)}
        for chain in ("VH", "VL", "CH", "CL"):
            sequence = entity["sequences"].get(chain, {"status": "missing"})
            row.update({chain: sequence.get("value") or "", chain + "_status": sequence["status"],
                chain + "_seq_id": sequence.get("seq_id"),
                chain + "_seq_id_scope": sequence.get("seq_id_scope", "as_recorded"),
                chain + "_boundary_method": sequence.get("boundary_method", ""),
                chain + "_refs": json.dumps(sorted(set(sequence.get("definition_refs", []) + sequence.get("mapping_refs", []))))})
        row["missing_values"] = json.dumps([c for c in ("VH", "VL") if not row[c]] + ([] if experiments else ["experiment"]))
        row["missing_explicit_domains"] = json.dumps(entity.get("missing_explicit_domains", entity["missing"]))
        items.append(row)
    fields = ["item", "name", "role"]
    for chain in ("VH", "VL", "CH", "CL"):
        fields += [chain, chain + "_status", chain + "_seq_id", chain + "_seq_id_scope", chain + "_boundary_method", chain + "_refs"]
    fields += ["experiment_ids", "experiment_missing", "observation_count", "missing_values", "missing_explicit_domains"]
    write_csv(output / "items.csv", items, fields)
    write_csv(output / "observations.csv", measurements,
              ["id", "item", "item_name", "experiment", "experiment_status", "metric", "value", "unit", "s", "refs"])
    experiments = [{**e, "refs": json.dumps(e["refs"])} for e in dataset["experiments"]]
    write_csv(output / "experiments.csv", experiments,
              ["id", "table", "method", "target", "species", "system", "conditions", "refs"])
    return {"items": len(items), "observations": len(measurements), "experiment_profiles": len(experiments),
            "output": str(output.resolve())}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    print(json.dumps(export(args.dataset, args.output), ensure_ascii=False))
