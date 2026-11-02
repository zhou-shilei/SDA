"""Replay a frozen rejected Action response; only the local repair calls are live."""

import argparse
from collections import Counter
import copy
import hashlib
import json
from pathlib import Path
import sys
import time
import shutil

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from dataagent2.action_agent import run_action_payload
from dataagent2.action_repair import records
from dataagent2.provider import Provider, digest, write_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("result", type=Path, help="Historical results/<document-id> directory")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--relocate", nargs=2, metavar=("OLD_PREFIX", "NEW_PREFIX"),
                        help="Read archived image bytes without restoring or modifying historical files")
    args = parser.parse_args()
    historical = args.result.resolve()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    original = historical / "dataagent2"
    payload = json.loads((original / "action_agent.input.json").read_text())
    rejected = json.loads((original / "action_agent.response.1.json").read_text())
    reference = json.loads((original / "dataset.json").read_text())
    original_hash = digest(rejected)
    data = {"document_id": payload["document"],
            "sources": {int(k): v for k, v in json.loads((original / "sources.json").read_text()).items()},
            "seed": copy.deepcopy(payload["initial_dataset"]), "style_definitions": []}
    relocations = {}
    for source in data["sources"].values():
        style = source.get("sequence_style")
        if not style:
            continue
        for key in ("image_path", "original_image_path"):
            if not style.get(key):
                continue
            path = Path(style[key])
            if not path.exists() and args.relocate:
                relative = path.relative_to(Path(args.relocate[0]))
                path = Path(args.relocate[1]) / relative
            expected = style["image_sha256" if key == "image_path" else "original_image_sha256"]
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise ValueError("Archived image bytes differ from the reviewed evidence")
            target = output / "evidence" / (expected + path.suffix)
            target.parent.mkdir(exist_ok=True)
            if not target.exists():
                shutil.copyfile(path, target)
            relocations[style[key]] = str(target)
            style[key] = str(target)
        payload.setdefault("source_image_styles", {})[str(source["id"])] = copy.deepcopy(style)
    historical_calls = [json.loads(line) for line in (original / "calls.jsonl").read_text().splitlines()]
    full_retries = [c for c in historical_calls if c["stage"] == "action"]

    class Replay(Provider):
        replayed = False

        def generate(self, stage, instructions, request):
            if stage == "action":
                if self.replayed:
                    raise RuntimeError("Local repair failed; production would perform a full retry. "
                                       "This replay intentionally does not launch a new full generation.")
                self.replayed = True
                return copy.deepcopy(rejected), {"historical_replay": True, "cache_hit": True}
            return super().generate(stage, instructions, request)

    provider = Replay(output, model="gpt-6.1-sol", effort="xhigh", action_effort="medium",
                      action_record_refs=False, timeout=600)
    started = time.monotonic()
    report = {"historical_result": str(historical), "replayed_response_hash": original_hash,
              "historical_first_generation_excluded": True, "full_regeneration_enabled": False,
              "evidence_path_relocations": relocations,
              "status": "running"}
    write_json(output / "replay_report.json", report)
    print(json.dumps({"status": "running", "output": str(output)}, ensure_ascii=False), flush=True)
    try:
        ledger, metadata = run_action_payload(data, payload, provider, output)
        complete = json.loads((output / "action_agent.response.json").read_text())
        before, after = dict(records(rejected)), dict(records(complete))
        changed = [path for path in before.keys() | after.keys() if before.get(path) != after.get(path)]
        permitted = set(metadata.get("local_repair", {}).get("changed_paths", []))
        if metadata.get("accounting_repair"):
            permitted.update(path for path in before if path.startswith("/action_decisions/"))
        unexpected = sorted(set(changed) - permitted)
        if unexpected:
            raise ValueError("Local correction changed unlisted records: " + str(unexpected))
        assembled = ledger.assemble()
        write_json(output / "dataset.json", assembled)
        def measurements(dataset):
            return Counter((o["item"], o["s"], o["metric"], o["value"], o["unit"])
                           for o in dataset["observations"])
        reference_values, new_values = measurements(reference), measurements(assembled)
        missing = list((reference_values - new_values).elements())
        report.update(status="validated", action_metadata=metadata,
                      changed_paths=changed, unchanged_records=sum(before[p] == after.get(p) for p in before),
                      unexpected_changed_paths=unexpected, summary=assembled["summary"],
                      reference_measurements=len(reference["observations"]),
                      preserved_reference_measurements=sum((reference_values & new_values).values()),
                      missing_reference_measurements=missing)
        if missing:
            report["status"] = "quality_difference"
    except Exception as error:
        report.update(status="failed", error=f"{type(error).__name__}: {error}")
    report.update(elapsed_seconds=round(time.monotonic() - started, 3), usage=provider.usage(),
                  historical_full_retry_seconds=full_retries[1]["seconds"] if len(full_retries) > 1 else None,
                  historical_full_retry_tokens=full_retries[1]["usage"] if len(full_retries) > 1 else None,
                  historical_response_unchanged=digest(json.loads((original / "action_agent.response.1.json").read_text())) == original_hash)
    write_json(output / "replay_report.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return 0 if report["status"] == "validated" else 1


if __name__ == "__main__":
    raise SystemExit(main())
