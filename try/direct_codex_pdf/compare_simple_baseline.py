"""Compare extraction outputs without treating either output as ground truth."""

import argparse
from collections import Counter, defaultdict
import csv
import hashlib
import json
from pathlib import Path
import re


VERIFIED_PDF = "cd553828376ec75b4e585b5358e5107c9c7f3d79d19f5de89171d5ce3e6f7723"
# Read directly from the original PDF, physical page 198, Table 3 LCDR1.
# This is a targeted check of disagreements, not a random accuracy sample.
VERIFIED_CDR = {59: "NLGSKS", 60: "NIGSKS", 62: "NLGSYS", 63: "NIGSKN",
                64: "NIGSFS", 65: "NIGRFS", 66: "NLGSFS"}


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def fingerprint(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def entity_name(name):
    # Explicit alias stated on PDF page 13. Keep every a/b suffix distinct.
    return "Reference" if name in {"Reference", "Reference Antibody"} else name


def species(value):
    value = str(value or "").lower()
    return "cyno" if "cyno" in value else "human" if "human" in value else None


def donor(value):
    return re.sub(r"[^0-9]", "", value or "") or None


def unit(value):
    return (value or "").replace("µ", "μ").replace(" ", "")


def dump_csv(path, rows):
    if not rows:
        return
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("agent_document", type=Path, help="Run's document directory")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    baseline = read(args.baseline / "dataset.json")
    baseline_manifest = read(args.baseline / "manifest.json")
    result_dir = args.agent_document / "results/49-Sructure/dataagent2"
    agent = read(result_dir / "dataset.json")
    sources = read(result_dir / "sources.json")
    run = read(args.agent_document / "run_summary.json")
    usage = read(args.agent_document / "results/_token_usage.json")
    agent_report = read(result_dir / "report.json")
    if baseline_manifest["source_sha256"] != run["source_pdf_sha256"]:
        raise ValueError("The runs used different original PDFs")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)

    tables = {}
    for source in sources.values():
        if source.get("table_id") and source.get("row") is None:
            match = re.match(r"Table\s+(\d+)\b", source["text"])
            if match:
                tables[source["table_id"]] = int(match[1])
    names = {e["id"]: entity_name(e["name"]) for e in agent["entities"]}
    agent_cells, baseline_cells = defaultdict(list), defaultdict(list)
    for observation in agent["observations"]:
        table = tables[observation["table"]]
        source = sources[str(observation["s"])]
        column = source["column"]
        antigen = None
        if table in {7, 8}:
            if column not in {1, 2}:
                raise ValueError("Unknown species column")
            antigen = "human" if column == 1 else "cyno"
        elif table == 9:
            if column not in range(1, 7):
                raise ValueError("Unknown KinExA species column")
            antigen = "human" if column <= 3 else "cyno"
        elif table in {10, 11}:
            antigen = "human"
        elif table in {12, 13}:
            antigen = species(observation.get("condition"))
        elif table != 6:
            raise ValueError(f"Unexpected measured table {table}")
        if table != 6 and antigen is None:
            raise ValueError("Unresolved comparison dimension")
        key = (table, names[observation["item"]], observation["metric"],
               antigen, donor(observation.get("donor")))
        agent_cells[key].append({"id": observation["id"], "value": observation["value"],
                                 "unit": observation["unit"], "source": source["source_ref"]})
    for observation in baseline["measurements"]:
        match = re.fullmatch(r"Table (\d+)", observation["sources"][0]["location"])
        if not match or not 6 <= int(match[1]) <= 13 or observation["status"] != "reported":
            continue
        table = int(match[1])
        antigen = species(observation["target_species"]) if table != 6 else None
        metric = {"ka": "kon", "kd": "koff"}.get(observation["metric"], observation["metric"])
        key = (table, entity_name(observation["antibody_id"]), metric,
               antigen, donor(observation.get("donor_id")))
        baseline_cells[key].append({"id": observation["measurement_id"],
                                    "value": observation.get("cell_raw", observation["value_raw"]),
                                    "unit": observation["unit_raw"]})
    shared = agent_cells.keys() & baseline_cells.keys()
    paired = []
    for key in sorted(shared, key=str):
        if len(agent_cells[key]) != 1 or len(baseline_cells[key]) != 1:
            raise ValueError(f"Ambiguous comparison key {key}")
        a, b = agent_cells[key][0], baseline_cells[key][0]
        paired.append(dict(zip(["table", "entity", "metric", "antigen_species", "donor"], key)) |
                      {"dataagent2_value": a["value"], "codex_value": b["value"],
                       "value_equal": a["value"] == b["value"], "dataagent2_unit": a["unit"],
                       "codex_unit": b["unit"], "unit_equal": unit(a["unit"]) == unit(b["unit"]),
                       "dataagent2_source": a["source"], "codex_measurement": b["id"]})

    numbered = {s["seq"]: s for s in agent["sequences"] if s.get("seq") is not None}
    baseline_numbered = {s["seq_id_no"]: s for s in baseline["sequences"]}
    common_numbers = numbered.keys() & baseline_numbered.keys()
    sequence_differences = []
    verified = VERIFIED_CDR if run["source_pdf_sha256"] == VERIFIED_PDF else {}
    for number in sorted(common_numbers):
        a, b = numbered[number].get("value"), baseline_numbered[number].get("sequence")
        if a != b:
            sequence_differences.append({"seq_id": number, "dataagent2": a, "codex": b,
                                         "original_pdf_verified_value": verified.get(number),
                                         "verified_pdf_page": 198 if number in verified else None})
    core = {f"AB-{i}{suffix}" for i in range(1, 15) for suffix in ["", "a", "b"]} | {"Reference"}
    agent_domains, baseline_domains = {}, {}
    for entity in agent["entities"]:
        name = entity_name(entity["name"])
        if name in core:
            for region in ["VH", "VL"]:
                definition = entity["sequences"][region]
                if definition.get("status") == "supported" and definition.get("value"):
                    agent_domains[name, region] = definition["value"]
    for link in baseline["sequence_assignments"]:
        name = entity_name(link["entity_id"])
        if name in core and link["region"] in {"VH", "VL"}:
            number = int(link["sequence_id"].removeprefix("SEQ_"))
            baseline_domains[name, link["region"]] = baseline_numbered[number]["sequence"]

    totals = usage["totals"]
    baseline_usage = baseline_manifest["usage"]
    def summed(key):
        values = [v.get(key) for v in baseline_usage]
        return sum(values) if values and all(type(v) is int for v in values) else None
    bt = {k: summed(k) for k in ["input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens"]}
    bt["uncached_input_tokens"] = bt["input_tokens"] - bt["cached_input_tokens"]
    bt["total_tokens"] = bt["input_tokens"] + bt["output_tokens"]
    business_unchanged = read(result_dir / "dataset2.business.json") == read(result_dir / "dataset3.business.json")
    report = {
        "inputs": {"baseline": str(args.baseline.resolve()), "dataagent2": str(result_dir.resolve()),
                   "original_pdf_sha256": run["source_pdf_sha256"],
                   "baseline_dataset_sha256": fingerprint(args.baseline / "dataset.json"),
                   "agent_dataset_sha256": fingerprint(result_dir / "dataset.json")},
        "tokens": {"codex": bt, "dataagent2": totals, "dataagent2_stages": usage["by_stage"],
                   "dataagent2_reduction_fraction": 1 - totals["total_tokens"] / bt["total_tokens"],
                   "codex_input_cache_fraction": bt["cached_input_tokens"] / bt["input_tokens"],
                   "dataagent2_input_cache_fraction": totals["cached_input_tokens"] / totals["input_tokens"]},
        "wall_seconds": {"codex": baseline_manifest["elapsed_seconds"], "dataagent2": run["wall_seconds"]},
        "measurements": {"codex_all_rows": len(baseline["measurements"]),
                         "codex_statuses": dict(Counter(v["status"] for v in baseline["measurements"])),
                         "codex_reported_potts_scores": sum(v["metric"] == "potts_model_score" for v in baseline["measurements"]),
                         "codex_cell_calibration": sum(v["metric"] == "hTSLP stimulation EC50" for v in baseline["measurements"]),
                         "comparable_keys": len(shared), "values_equal": sum(v["value_equal"] for v in paired),
                         "units_equal": sum(v["unit_equal"] for v in paired),
                         "only_dataagent2": sorted(agent_cells.keys() - baseline_cells.keys(), key=str),
                         "only_codex": sorted(baseline_cells.keys() - agent_cells.keys(), key=str)},
        "sequences": {"codex_numbered": len(baseline_numbered), "dataagent2_numbered": len(numbered),
                      "dataagent2_unnumbered": sum(v.get("seq") is None for v in agent["sequences"]),
                      "common_numbered": len(common_numbers), "literal_equal": len(common_numbers) - len(sequence_differences),
                      "differences": sequence_differences,
                      "only_codex_numbered": sorted(baseline_numbered.keys() - numbered.keys()),
                      "core_antibody_count": len(core), "codex_core_VH_VL_slots": len(baseline_domains),
                      "dataagent2_core_VH_VL_slots": len(agent_domains),
                      "codex_core_pairs": sum(all((name, region) in baseline_domains for region in ["VH", "VL"]) for name in core),
                      "dataagent2_core_pairs": sum(all((name, region) in agent_domains for region in ["VH", "VL"]) for name in core),
                      "shared_domain_values_equal": sum(value == baseline_domains.get(key) for key, value in agent_domains.items()),
                      "only_codex_domain_assignments": sorted(baseline_domains.keys() - agent_domains.keys())},
        "followup": {"questions": agent_report["questions_attempted"],
                     "questions_remaining": agent_report["questions_remaining"],
                     "validated_answer_actions": len(read(result_dir / "answer_actions.json")),
                     "business_dataset_unchanged": business_unchanged},
        "limitations": [
            "This compares two outputs, not an independently labeled full dataset; agreement is not accuracy.",
            "Measurements are paired by table, antibody, metric, antigen species and donor; original cell strings include confidence intervals.",
            "For dataagent2 Tables 7-9 the species dimension is recovered from source cell columns. This is not a test that each observation explicitly stores species.",
            "Only the seven selected CDR disagreements were independently checked against original PDF page 198. Do not extrapolate this to overall sequence accuracy.",
            "The 13 extra Codex a-variant VL assignments are unverified ownership claims, not established improvements.",
            "Codex used downloaded same-publication patent text; dataagent2 used this run's OCR. Baseline is not restricted to local PDF evidence.",
            "Both use gpt-6.1-sol; Codex and Readers use xhigh; dataagent2 Action and Answer use medium.",
            "Token totals include cached input once; reasoning is a subset of output. Local OCR compute is excluded from model token totals.",
        ],
    }
    (output / "comparison.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    dump_csv(output / "measurements.csv", paired)
    dump_csv(output / "sequence_differences.csv", sequence_differences)
    print(json.dumps({"output": str(output), "measurements": report["measurements"],
                      "sequences": report["sequences"], "followup": report["followup"],
                      "token_reduction": report["tokens"]["dataagent2_reduction_fraction"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
