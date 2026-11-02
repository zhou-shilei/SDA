"""Compare independent Readers with Readers forked from one seed-only setup.

All child Readers fork the SAME parent, never another Reader. Setup has no chunk
sources and its response must be empty. Its real model usage is included. Only
previously measured, exact baseline probe calls may be retained as controls.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path
import shutil
import sys

from benchmark_reader_cache import (INSTRUCTIONS, ROOT, SAMPLE, SCHEMA, SHARED_KEYS,
    aggregate, compact, compare, compare_named, digest, layout, load_input,
    incremental_usage, one_request, read, reader_payload, signatures, write_json)


def main(args):
    baseline, probe, output = args.baseline.resolve(), args.probe.resolve(), args.output.resolve()
    for previous in (baseline, probe):
        if output == previous or output in previous.parents or previous in output.parents:
            raise ValueError("Use an isolated output directory")
    if args.workers < 1 or args.timeout < 1:
        raise ValueError("Workers and timeout must be positive")
    manifest = read(baseline / "manifest.json")
    data = load_input(manifest["native_input"])
    if digest(read(baseline / "sources.json")) != digest(data["sources"]):
        raise ValueError("Original OCR sources changed")
    by_id = {c["id"]: c for c in manifest["chunks"]}
    if not args.chunks or len(set(args.chunks)) != len(args.chunks) or set(args.chunks) - by_id.keys():
        raise ValueError("Unknown or repeated chunks")
    chunks = [by_id[c] for c in args.chunks]
    payloads = {c["id"]: reader_payload(data, c) for c in chunks}
    shared = {k: payloads[chunks[0]["id"]][k] for k in SHARED_KEYS}
    audits = []
    for chunk in manifest["chunks"]:
        p = reader_payload(data, chunk)
        if {k: p[k] for k in SHARED_KEYS} != shared:
            raise ValueError("The complete initial dataset differs between chunks")
        local = {k: v for k, v in p.items() if k not in shared}
        if {**shared, **local} != p:
            raise ValueError("Split omitted request fields")
        audits.append({"chunk": chunk["id"], "payload_hash": digest(p), "seed_hash": digest(shared["initial_dataset"])})
    model, effort = manifest["model"], manifest["effort"]
    protocol = {"revision": 1, "baseline": str(baseline), "probe": str(probe),
        "model": model, "effort": effort, "chunks": args.chunks, "workers_per_arm": args.workers,
        "timeout": args.timeout, "payload_hashes": {k: digest(v) for k, v in payloads.items()},
        "scope": "Six-chunk Reader screening; full seed and original sources. No OCR, Template, Action or Answer calls.",
        "method": "One seed-only setup with empty response; every Reader forks that same persisted thread. No sibling Reader facts are inherited.",
        "limitations": ["No independent human gold and no full-document/downstream accuracy guarantee.",
            "Exact measured baseline probe calls are retained with their original usage/timing; remaining calls are fresh.",
            "Control and fork arms run concurrently; external model load/routing and output variation affect time.",
            "Setup usage is included; CLI does not separately report cache-write billing, so no dollar saving is claimed."]}
    if prior := read(output / "protocol.json"):
        if prior != protocol:
            raise ValueError("Protocol changed; use a fresh output directory")
    output.mkdir(parents=True, exist_ok=True)
    write_json(output / "protocol.json", protocol)
    write_json(output / "lossless_audit.json", {"roundtrip_equal": True, "chunks_checked": len(audits), "chunks": audits})
    for arm in ("control", "shared_fork"):
        folder = output / arm
        folder.mkdir(exist_ok=True)
        write_json(folder / "schema.json", SCHEMA)
    retained = []
    for chunk in chunks:
        cid = chunk["id"]
        source = probe / "baseline/requests" / cid
        target = output / "control/requests" / cid
        if source.is_dir() and not target.exists():
            if read(source / "input.json") != json.loads(compact(payloads[cid])):
                raise ValueError("Prior control used a different payload")
            row = read(source / "result.json")
            if not row or row["status"] != "ok":
                continue
            # one_request also checks model, effort, schema and exact wire hash
            # before allowing these retained calls into the measured control.
            target.parent.mkdir(exist_ok=True)
            shutil.copytree(source, target)
        if target.is_dir() and source.is_dir():
            retained.append({"chunk": cid, "source": str(source), "target": str(target)})
    write_json(output / "retained_controls.json", retained)
    for name in ("benchmark_reader_cache.py", "benchmark_reader_fork.py", "benchmark_source_groups.py", "audit_reader_trials.py"):
        for directory in ("source", "reporting_source"):
            target = output / directory / name
            target.parent.mkdir(exist_ok=True)
            if directory == "reporting_source" or not target.exists():
                shutil.copyfile(Path(__file__).with_name(name), target)

    def execute_arm(arm):
        folder = output / arm
        parent, setup = None, None
        if arm == "shared_fork":
            setup_payload = {**shared, "chunk": "setup", "sources": [],
                "context_only_sources": [], "existing_observation_sources": []}
            setup, response = one_request(setup_payload, "shared_fork", folder, model, effort,
                args.timeout, bootstrap=True)
            if setup["status"] != "ok" or not setup.get("thread_id"):
                raise RuntimeError("Setup failed; no child Reader calls sent")
            if any(response.values()):
                raise ValueError("Setup is not an empty response")
            parent = setup["thread_id"]
        completed, rows = {}, []
        def accept(chunk, result):
            row, response = result
            rows.append(row)
            if row["status"] == "ok":
                completed[chunk["id"]] = {"response": response, "meta": {"key": row["key"]}}
            write_json(folder / "progress.json", {"finished": len(rows), "total": len(chunks), "usage": aggregate(rows)})
            write_json(folder / "completed.json", completed)
        # Observe the first child serially, then sample parallel Reader behavior.
        start = 0
        if arm == "shared_fork":
            c = chunks[0]
            accept(c, one_request(payloads[c["id"]], "shared_fork", folder, model, effort,
                args.timeout, parent=parent, parent_usage=setup["usage"]))
            start = 1
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            pending = {pool.submit(one_request, payloads[c["id"]],
                "baseline" if arm == "control" else "shared_fork", folder,
                model, effort, args.timeout, parent=parent,
                parent_usage=setup["usage"] if setup else None): c for c in chunks[start:]}
            for future in as_completed(pending):
                accept(pending[future], future.result())
        sigs, ledger = signatures(data, chunks, completed)
        seed = signatures(data, [], {})[0]
        dataset = ledger.assemble()
        report = {"status": "complete" if len(completed) == len(chunks) else "failed",
            "reader_usage": aggregate(rows), "usage_including_setup": aggregate(([setup] if setup else []) + rows),
            "setup": setup, "requests": sorted(rows, key=lambda r: args.chunks.index(r["chunk"])),
            "accepted_new_fact_signatures": {k: len(sigs[k] - seed[k]) for k in sigs},
            "grounding_rejections": len(ledger.rejections),
            "same_parent_for_all_readers": all(r.get("parent") == parent for r in rows) if parent else None}
        write_json(folder / "report.json", report)
        write_json(folder / "grounding_rejections.json", ledger.rejections)
        write_json(folder / "facts.json", ledger.facts)
        write_json(folder / "dataset.provisional.json", dataset)
        return report, completed, sigs, dataset

    results = {}
    with ThreadPoolExecutor(max_workers=2) as arms:
        pending = {arms.submit(execute_arm, arm): arm for arm in ("control", "shared_fork")}
        for future in as_completed(pending):
            arm = pending[future]
            try:
                results[arm] = future.result()
            except Exception as exc:
                write_json(output / (arm + ".failure.json"), {"error": str(exc)})
                raise
    control, candidate = results["control"], results["shared_fork"]
    common = [c for c in chunks if c["id"] in control[1] and c["id"] in candidate[1]]
    differences = {"common_chunks": [c["id"] for c in common],
        "facts": compare(signatures(data, common, control[1])[0], signatures(data, common, candidate[1])[0]),
        "by_chunk": {c["id"]: compare(signatures(data, [c], control[1])[0],
            signatures(data, [c], candidate[1])[0]) for c in common}}
    if common:
        differences["provisional_dataset_literal_audit"] = compare_named(
            signatures(data, common, control[1])[1].assemble(),
            signatures(data, common, candidate[1])[1].assemble(),
            {e["id"] for e in data["seed"]["entities"]})
    common_ids = {c["id"] for c in common}
    matched_usage = {arm: aggregate([r for r in value[0]["requests"] if r["chunk"] in common_ids])
        for arm, value in results.items()}
    matched_usage["shared_fork_including_setup"] = aggregate([candidate[0]["setup"],
        *[r for r in candidate[0]["requests"] if r["chunk"] in common_ids]])
    accounting = {"method": "Subtract setup snapshot from each fork terminal total; count setup once",
        "raw_terminal_usage_preserved": True}
    calibration = read(output / "accounting_calibration/report.json")
    if calibration:
        setup_usage = candidate[0]["setup"]["usage"]
        delta = incremental_usage(calibration["terminal_usage"], setup_usage)
        last = calibration["rollout_usage"]["last_token_usage"]
        if any(delta[k] != last[k] for k in delta):
            raise ValueError("CLI accounting calibration disagrees with per-turn rollout usage")
        accounting.update(calibration_verified=True, calibration_incremental_usage=delta,
            calibration_source="accounting_calibration/report.json",
            calibration_included_in_runtime_comparison=False)
    write_json(output / "report.json", {"protocol": protocol, "accounting": accounting,
        "results": {k: v[0] for k, v in results.items()}, "matched_successful_pairs_usage": matched_usage,
        "comparison": differences})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--probe", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--chunks", nargs="+", default=list(SAMPLE))
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--timeout", type=int, default=600)
    main(parser.parse_args())
