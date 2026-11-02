"""Paired Reader input experiment on immutable original OCR/Template inputs.

No repaired dataset or later Action output enters any Reader prompt. Historical
and contemporaneous controls are labelled separately; token usage is actual.
"""

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from dataagent2.action_agent import run_action_agent
from dataagent2.contracts import INSTRUCTIONS, SCHEMA
from dataagent2.inputs import load_input, make_chunks, read
from dataagent2.ledger import Ledger, normalized
from dataagent2.pipeline import reader_payload
from dataagent2.provider import Provider, compact, digest, write_json
from dataagent2.quality import audit_dataset
from dataagent2.reader_transport import REVISION, MODES, adaptive_chunks, decode, experimental_input, pack_sources
from rerun_action_domain_review import metrics, compare


SAMPLE = ["c4", "c6", "c7", "c26", "c29", "c44", "c45", "c46", "c47", "c51", "c54"]
SAMPLE_REASONS = {
    "c4": "Prose experiments and cross-entity relations", "c6": "No named relevant facts in historical response",
    "c7": "New literal antigen entities and definitions", "c26": "New antibody/control names and aliases",
    "c29": "Prose sequences and multiple mappings", "c44": "Text measurements and experiment associations",
    "c45": "Table mapping and domain definitions", "c46": "Dense sequence table; shared numbered definitions",
    "c47": "Dense chain/domain table continuation", "c51": "Mixed tables, measurements and sequences",
    "c54": "Cross-table sequence relationships and unresolved mappings",
}


class SourceGroupedActionProvider(Provider):
    """Experimental, reversible source formatting when an Action hits CLI limits."""

    def generate(self, stage, instructions, payload):
        if stage != "action" or len(instructions + compact(payload)) < 950_000:
            return super().generate(stage, instructions, payload)
        grouped = pack_sources(payload["sources"])
        restored = decode({"transport": {"version": 1}, "shared_strings": [], "sources": grouped})["sources"]
        if restored != payload["sources"]:
            raise ValueError("Action source grouping changed literal evidence")
        request = copy.deepcopy(payload)
        request["sources"] = grouped
        request["source_encoding"] = {"version": 1, "kind": "consecutive_source_groups"}
        prompt = instructions + """
SOURCE TRANSPORT: sources are consecutive groups {page,kind,table_id,columns,rows}.
Zip columns with each row and combine with the group's page/kind/table_id. id is
the original INTEGER source ID. Missing row/column mean null. This overrides ONLY
the seven-column source-row wire format. All literal source text and IDs remain
present and untruncated; use the original IDs for refs. No evidence was filtered.
"""
        audit = {"original_payload_hash": digest(payload), "wire_payload_hash": digest(request),
            "source_rows": len(restored), "canonical_source_hash": digest(restored),
            "original_prompt_chars": len(instructions + "\nINPUT_JSON:\n" + compact(payload)),
            "wire_prompt_chars": len(prompt + "\nINPUT_JSON:\n" + compact(request)),
            "lossless": True}
        write_json(self.output / "source_transport" / (digest(payload) + ".json"), audit)
        response, meta = super().generate(stage, prompt, request)
        return response, {**meta, "source_transport": audit}


def saved_readers(data, manifest, baseline):
    completed = {}
    for chunk in manifest["chunks"]:
        payload = reader_payload(data, chunk)
        key = digest({"stage": "reader", "instructions": INSTRUCTIONS, "payload": payload,
                      "schema": SCHEMA, "model": manifest["model"], "effort": manifest["effort"]})
        folder = baseline / "requests" / key
        if digest(read(folder / "input.json")) != key:
            raise ValueError("Historical Reader input changed: " + chunk["id"])
        completed[chunk["id"]] = {"response": read(folder / "validated.json"), "meta": {"key": key}}
    return completed


def evidence_facts(data, chunks, completed):
    """Stable literal fact signatures, not an independent semantic gold standard."""
    ledger = Ledger(data)
    for chunk in chunks:
        if chunk["id"] in completed:
            ledger.apply(completed[chunk["id"]]["response"], chunk["core"] + chunk["context"], chunk["id"])
    names = {e["id"]: normalized(e["name"]) for e in ledger.entities.values()}
    expts = {e["id"]: e for e in ledger.facts["experiments"]}
    signatures = {k: set() for k in ("entities", "links", "sequences", "observations", "annotations", "relations", "experiments")}
    def add(kind, value):
        signatures[kind].add(compact(value))
    for e in ledger.entities.values():
        add("entities", names[e["id"]])
    for fact in ledger.facts["links"]:
        add("links", [names[fact["item"]], fact["region"], fact["seq"], fact.get("name") if fact["seq"] is None else None])
    for fact in ledger.facts["sequences"]:
        add("sequences", [fact["seq"], fact["region"], fact.get("name") if fact["seq"] is None else None, fact["value"]])
    for fact in ledger.facts["observations"]:
        add("observations", [names[fact["item"]], fact["s"], normalized(fact["metric"]), fact["value"],
                              fact.get("unit"), fact.get("condition"), fact.get("donor"), fact.get("replicate")])
    for fact in ledger.facts["annotations"]:
        subject = names.get(fact["subject"], fact["subject"])
        if fact["subject_type"] == "experiment":
            e = expts.get(fact["subject"], {})
            subject = [e.get("table"), sorted(e.get("refs", []))]
        add("annotations", [fact["subject_type"], subject, fact["category"], normalized(fact["value"])])
    for fact in ledger.facts["relations"]:
        if fact["relation"] == "tested_in":
            e = expts.get(fact["object"], {})
            target = [e.get("table"), sorted(e.get("refs", []))]
        else:
            target = names.get(fact["object"], fact["object"])
        add("relations", [names.get(fact["subject"], fact["subject"]), fact["relation"], target])
    for fact in expts.values():
        add("experiments", [fact.get(k) for k in ("table", "region_id", "method", "target", "species", "system", "conditions")])
    return signatures, ledger


def fact_comparison(reference, candidate):
    return {k: {"reference": len(reference[k]), "candidate": len(candidate[k]),
                "shared": len(reference[k] & candidate[k]), "reference_only": len(reference[k] - candidate[k]),
                "candidate_only": len(candidate[k] - reference[k]),
                "reference_only_examples": [json.loads(x) for x in sorted(reference[k] - candidate[k])[:20]],
                "candidate_only_examples": [json.loads(x) for x in sorted(candidate[k] - reference[k])[:20]]}
            for k in reference}


def execute(data, manifest, selected, mode, folder, workers):
    folder.mkdir(parents=True, exist_ok=True)
    config = {"input_hash": data["signature"], "seed_hash": digest(reader_payload(data, selected[0])["initial_dataset"]),
        "mode": mode, "transport_revision": REVISION, "chunks": [c["id"] for c in selected],
        "model": manifest["model"], "effort": manifest["effort"], "workers": workers,
        "requests": {c["id"]: digest(experimental_input(reader_payload(data, c), mode)) for c in selected}}
    previous_config = read(folder / "configuration.json")
    if previous_config and previous_config != config:
        raise ValueError("Reader experiment configuration changed; choose a fresh output directory")
    write_json(folder / "configuration.json", config)
    saved = read(folder / "reader_report.json", {})
    if saved.get("status") == "complete":
        completed = read(folder / "completed.json")
        print(f"{mode}: using completed experiment; retain original measured wall time", flush=True)
        return saved, completed
    provider = Provider(folder, model=manifest["model"], effort=manifest["effort"], timeout=600)
    completed, failed = {}, {}
    prior_seconds = saved.get("seconds", 0)
    started = time.monotonic()
    report = {"status": "running", "configuration": config, "started_at": datetime.now(timezone.utc).isoformat()}
    write_json(folder / "reader_report.json", report)

    def one(chunk):
        canonical = reader_payload(data, chunk)
        payload, instructions = experimental_input(canonical, mode)
        write_json(folder / "inputs" / (chunk["id"] + ".json"), payload)
        response, meta = provider.generate("reader", instructions, payload)
        return {"response": response, "meta": meta}

    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(one, c): c for c in selected}
            for future in as_completed(futures):
                chunk = futures[future]
                try:
                    completed[chunk["id"]] = future.result()
                except Exception as exc:
                    failed[chunk["id"]] = str(exc)
                write_json(folder / "completed.json", completed)
                write_json(folder / "progress.json", {"mode": mode, "completed": len(completed), "failed": failed,
                    "total": len(selected), "seconds": round(time.monotonic() - started, 3), "usage": provider.usage()})
                print(f"{mode}: {len(completed)}/{len(selected)} failed={len(failed)} tokens={provider.usage()['total_tokens']}", flush=True)
    finally:
        signatures, ledger = evidence_facts(data, selected, completed)
        report.update(status="complete" if len(completed) == len(selected) else "failed", failures=failed,
            seconds=round(prior_seconds + time.monotonic() - started, 3), usage=provider.usage(),
            facts={k: len(v) for k, v in signatures.items()}, provisional=metrics(ledger.assemble()),
            grounded_rejections=len(ledger.rejections), finished_at=datetime.now(timezone.utc).isoformat())
        write_json(folder / "reader_report.json", report)
        write_json(folder / "provisional_dataset.json", ledger.assemble())
        write_json(folder / "grounding_rejections.json", ledger.rejections)
    return report, completed


def consolidate(data, manifest, completed, folder, baseline):
    folder.mkdir(parents=True, exist_ok=True)
    report = read(folder / "experiment.json", {})
    if report.get("status") == "complete":
        return report
    provider = SourceGroupedActionProvider(folder, model=manifest["model"], effort=manifest["effort"],
                        action_effort=manifest.get("action_effort", "medium"), action_timeout=1800)
    started = time.monotonic()
    report = {"status": "running", "scope": "Action-only consolidation of this mode's fresh Readers; no old Reader/Action/Answer facts added"}
    write_json(folder / "experiment.json", report)
    try:
        ledger, status = run_action_agent(data, manifest["chunks"], completed, provider, folder)
        dataset = ledger.assemble()
        audit = audit_dataset(dataset, data["sources"])
        write_json(folder / "dataset.json", dataset)
        write_json(folder / "facts.json", ledger.facts)
        write_json(folder / "quality_audit.json", audit)
        report.update(status="complete" if not audit["literal_errors"] else "failed", action_status=status,
            metrics=metrics(dataset), comparison_to_historical_action=compare(read(baseline / "dataset.first_pass.json"), dataset),
            quality={k: v for k, v in audit.items() if not isinstance(v, list) or len(v) < 10})
    except Exception as exc:
        report.update(status="failed", error=str(exc))
        print(f"Action failed: {exc}", flush=True)
    finally:
        report.update(seconds=round(time.monotonic() - started, 3), usage=provider.usage())
        write_json(folder / "experiment.json", report)
    return report


def main(args):
    baseline, output = args.baseline.resolve(), args.output.resolve()
    if output == baseline or baseline in output.parents or output in baseline.parents:
        raise ValueError("Output must be isolated from the saved web run")
    manifest = read(baseline / "manifest.json")
    data = load_input(manifest["native_input"])
    if digest(read(baseline / "sources.json")) != digest(data["sources"]):
        raise ValueError("Saved OCR source identity changed")
    historical = saved_readers(data, manifest, baseline)
    original_chunks = manifest["chunks"]
    if args.chunk_chars or args.adaptive:
        if args.phase != "full" or args.chunks:
            raise ValueError("Different chunk sizes require complete all-source evaluation")
        if args.chunk_chars and args.adaptive:
            raise ValueError("Choose fixed large chunks OR adaptive prose pairs")
        source_hash = digest(data["sources"])
        manifest = copy.deepcopy(manifest)
        manifest["chunks"] = adaptive_chunks(data, original_chunks) if args.adaptive else make_chunks(data, args.chunk_chars)
        manifest["chunk_chars"] = args.chunk_chars or manifest["chunk_chars"]
        manifest["chunk_layout"] = "adaptive_prose_pairs" if args.adaptive else "fixed_character_budget"
        if digest(data["sources"]) != source_hash:
            raise ValueError("Chunk-size experiment changed original source identities")
        before = [s for c in original_chunks for s in c["core"]]
        after = [s for c in manifest["chunks"] for s in c["core"]]
        if set(before) != set(after) or len(after) != len(set(after)):
            raise ValueError("Different chunk sizes lost or repeated core sources")
    ids = args.chunks or (SAMPLE if args.phase == "screen" else [c["id"] for c in manifest["chunks"]])
    selected = [c for c in manifest["chunks"] if c["id"] in ids]
    if len(selected) != len(set(ids)):
        raise ValueError("Unknown or duplicate chunk IDs")
    output.mkdir(parents=True, exist_ok=True)
    write_json(output / "protocol.json", {"baseline": str(baseline), "source_input_hash": data["signature"],
        "initial_dataset_hash": digest(reader_payload(data, selected[0])["initial_dataset"]),
        "reader_schema_hash": digest(SCHEMA), "phase": args.phase, "chunks": ids,
        "strata": SAMPLE_REASONS if args.phase == "screen" else {},
        "selection_scope": "Purposefully representative sample, not random or a patent recall estimate" if args.phase == "screen" else "Every saved source, all 62 original chunks",
        "scope": "Immutable original OCR and Template Dataset1 only; later Action/Answer outputs are scoring references, never Reader inputs",
        "model": manifest["model"], "effort": manifest["effort"], "workers": args.workers,
        "chunk_chars": manifest["chunk_chars"], "chunk_count": len(manifest["chunks"]),
        "core_sources": sum(len(c["core"]) for c in selected), "chunk_layout": manifest.get("chunk_layout", "original"),
        "chunk_plan": selected})
    source_folder = output / "source"
    source_folder.mkdir(exist_ok=True)
    for name in ["src/dataagent2/reader_transport.py", "src/dataagent2/provider.py", "src/dataagent2/contracts.py", "src/dataagent2/pipeline.py"]:
        shutil.copyfile(ROOT / name, source_folder / Path(name).name)
    reference_chunks = original_chunks if args.chunk_chars or args.adaptive else selected
    reference, _ = evidence_facts(data, reference_chunks, historical)
    results, completed_by_mode = {}, {}
    if args.control:
        control = read(args.control / "configuration.json")
        expected_requests = {c["id"]: digest(experimental_input(reader_payload(data, c), "full")) for c in selected}
        if (control.get("mode") != "full" or control["requests"] != expected_requests
                or control["model"] != manifest["model"] or control["effort"] != manifest["effort"]):
            raise ValueError("Fresh control used different chunks, inputs or model settings")
        completed_by_mode["full"] = read(args.control / "completed.json")
    for mode in args.modes:
        report, completed = execute(data, manifest, selected, mode, output / mode, args.workers)
        actual, _ = evidence_facts(data, selected, completed)
        report["versus_historical_reader_facts"] = fact_comparison(reference, actual)
        write_json(output / mode / "reader_report.json", report)
        results[mode], completed_by_mode[mode] = report, completed
        # Contemporaneous full control is the primary screening comparison.
        if "full" in completed_by_mode:
            control, _ = evidence_facts(data, selected, completed_by_mode["full"])
            for key, value in completed_by_mode.items():
                if key not in results:
                    continue
                candidate, _ = evidence_facts(data, selected, value)
                results[key]["versus_fresh_full_facts"] = fact_comparison(control, candidate)
                write_json(output / key / "reader_report.json", results[key])
        write_json(output / "comparison.json", results)
        if args.consolidate:
            if len(completed) != len(manifest["chunks"]):
                raise ValueError("Only a complete all-chunk run can be consolidated")
            results[mode]["action"] = consolidate(data, manifest, completed, output / mode / "action", baseline)
            write_json(output / "comparison.json", results)
    print(compact({k: {f: v.get(f) for f in ("status", "seconds", "usage", "provisional", "grounded_rejections")}
                   for k, v in results.items()}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--phase", choices=["screen", "full"], default="screen")
    parser.add_argument("--modes", choices=MODES, nargs="+", default=list(MODES))
    parser.add_argument("--chunks", nargs="+")
    parser.add_argument("--chunk-chars", type=int, help="Full-coverage chunk-size ablation; never changes OCR source IDs")
    parser.add_argument("--adaptive", action="store_true", help="Merge prose pairs, preserve original table/sequence-dense chunks")
    parser.add_argument("--workers", type=int, default=11)
    parser.add_argument("--consolidate", action="store_true")
    parser.add_argument("--control", type=Path, help="Hash-verified contemporaneous full-format control; evaluation only")
    args = parser.parse_args()
    if args.workers < 1:
        parser.error("workers must be positive")
    main(args)
