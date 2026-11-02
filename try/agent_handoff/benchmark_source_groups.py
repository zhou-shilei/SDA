"""Paired sources-only trial: fixed seed, chunks, schema, model and reasoning effort."""

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
import shutil
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from dataagent2.contracts import INSTRUCTIONS, SCHEMA
from dataagent2.inputs import load_input, read, source_row
from dataagent2.ledger import Ledger, metric_key, normalized
from dataagent2.pipeline import reader_payload, reader_request
from dataagent2.provider import Provider, compact, digest, write_json
from dataagent2.source_transport import REVISION, decode_payload

SAMPLE = {"c7": "New antigen identities", "c26": "New antibody/control names and aliases",
          "c44": "Text measurements and experiment association", "c46": "Dense sequence table",
          "c47": "Sequence table continuation", "c51": "Mixed table measurements and sequences"}


def signatures(data, chunks, completed):
    ledger = Ledger(data)
    for chunk in chunks:
        if chunk["id"] in completed:
            ledger.apply(completed[chunk["id"]]["response"], chunk["context"] + chunk["core"], chunk["id"])
    names = {k: normalized(e["name"]) for k, e in ledger.entities.items()}
    result = {k: set() for k in ("entities", "links", "sequences", "observations", "relations", "annotations", "experiments")}
    for e in ledger.entities.values():
        result["entities"].add(compact(normalized(e["name"])))
    for f in ledger.facts["links"]:
        result["links"].add(compact([names[f["item"]], f["region"], f["seq"], f.get("name") if f["seq"] is None else None]))
    for f in ledger.facts["sequences"]:
        result["sequences"].add(compact([f["seq"], f["region"], f.get("name") if f["seq"] is None else None, f["value"]]))
    for f in ledger.facts["observations"]:
        result["observations"].add(compact([names[f["item"]], f["s"], metric_key(f["metric"]), f["value"],
                                          f.get("unit"), f.get("condition"), f.get("donor"), f.get("replicate")]))
    experiments = {f["id"]: f for f in ledger.facts["experiments"]}
    def exp(identity):
        f = experiments.get(identity, {})
        return [f.get(k) for k in ("table", "region_id", "method", "target", "species", "system", "conditions")]
    for f in ledger.facts["experiments"]:
        result["experiments"].add(compact(exp(f["id"])))
    for f in ledger.facts["relations"]:
        target = exp(f["object"]) if f["relation"] == "tested_in" else names.get(f["object"], f["object"])
        result["relations"].add(compact([names.get(f["subject"], f["subject"]), f["relation"], target]))
    for f in ledger.facts["annotations"]:
        subject = exp(f["subject"]) if f["subject_type"] == "experiment" else names.get(f["subject"], f["subject"])
        result["annotations"].add(compact([f["subject_type"], subject, f["category"], normalized(f["value"])]))
    return result, ledger


def compare(a, b):
    import json
    return {k: {"rows": len(a[k]), "grouped": len(b[k]), "shared": len(a[k] & b[k]),
                "rows_only": len(a[k] - b[k]), "grouped_only": len(b[k] - a[k]),
                "rows_only_examples": [json.loads(v) for v in sorted(a[k] - b[k])],
                "grouped_only_examples": [json.loads(v) for v in sorted(b[k] - a[k])]}
            for k in a}


def run_mode(data, manifest, chunks, mode, folder, workers):
    folder.mkdir(parents=True, exist_ok=True)
    configuration = {"source_revision": REVISION, "mode": mode, "model": manifest["model"],
        "effort": manifest["effort"], "workers": workers,
        "requests": {c["id"]: digest(reader_request(data, c, mode)) for c in chunks}}
    if previous := read(folder / "configuration.json"):
        if previous != configuration:
            raise ValueError("Trial input changed; use a new output directory")
    write_json(folder / "configuration.json", configuration)
    if read(folder / "report.json", {}).get("status") == "complete":
        return read(folder / "report.json"), read(folder / "completed.json")
    provider = Provider(folder, model=manifest["model"], effort=manifest["effort"], timeout=600)
    completed, failed = {}, {}
    started = time.monotonic()
    def one(chunk):
        payload, prompt = reader_request(data, chunk, mode)
        write_json(folder / "inputs" / (chunk["id"] + ".json"), payload)
        response, meta = provider.generate("reader", prompt, payload)
        return {"response": response, "meta": meta}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        pending = {pool.submit(one, c): c for c in chunks}
        for future in as_completed(pending):
            cid = pending[future]["id"]
            try:
                completed[cid] = future.result()
            except Exception as exc:
                failed[cid] = str(exc)
            write_json(folder / "completed.json", completed)
            write_json(folder / "progress.json", {"completed": len(completed), "failed": failed,
                "total": len(chunks), "usage": provider.usage(), "seconds": time.monotonic() - started})
            print(f"{mode}: {len(completed)}/{len(chunks)} failed={len(failed)}", flush=True)
    sigs, ledger = signatures(data, chunks, completed)
    report = {"status": "complete" if len(completed) == len(chunks) else "failed", "mode": mode,
        "seconds": round(time.monotonic() - started, 3), "usage": provider.usage(), "failures": failed,
        "grounding_rejections": len(ledger.rejections), "facts": {k: len(v) for k, v in sigs.items()}}
    write_json(folder / "report.json", report)
    write_json(folder / "grounding_rejections.json", ledger.rejections)
    write_json(folder / "facts.json", ledger.facts)
    write_json(folder / "fact_signatures.json", {k: sorted(v) for k, v in sigs.items()})
    return report, completed


def main(baseline, output, *, plan_only=False, selected=None):
    baseline, output = baseline.resolve(), output.resolve()
    if baseline == output or baseline in output.parents or output in baseline.parents:
        raise ValueError("Use an isolated output directory")
    manifest = read(baseline / "manifest.json")
    data = load_input(manifest["native_input"])
    if digest(data["sources"]) != digest(read(baseline / "sources.json")):
        raise ValueError("Original OCR sources changed")
    output.mkdir(parents=True, exist_ok=True)
    selected = selected or list(SAMPLE)
    chunks = [c for c in manifest["chunks"] if c["id"] in selected]
    if {c["id"] for c in chunks} != set(selected):
        raise ValueError("Unknown or repeated sample chunk")
    protocol = {"baseline": str(baseline), "started_at": datetime.now(timezone.utc).isoformat(),
        "scope": "Sources-only format change. Identical full Dataset1, source text/IDs, chunk membership, schema, model and effort. No OCR/Template/Action/Answer calls.",
        "source_revision": REVISION, "source_hash": digest(data["sources"]), "chunks": [c["id"] for c in chunks],
        "sample_reasons": {c["id"]: SAMPLE.get(c["id"], "Explicitly selected") for c in chunks},
        "model": manifest["model"], "effort": manifest["effort"]}
    previous = read(output / "protocol.json")
    if previous and {k: v for k, v in previous.items() if k != "started_at"} != {k: v for k, v in protocol.items() if k != "started_at"}:
        raise ValueError("Trial settings changed; use a new directory")
    write_json(output / "protocol.json", previous or protocol)
    import tiktoken
    enc = tiktoken.get_encoding("o200k_base")
    size = lambda value: len(enc.encode(value, disallowed_special=()))
    sizes, styles, failures = [], 0, 0
    for chunk in manifest["chunks"]:
        canonical = reader_payload(data, chunk)
        wire, prompt = reader_request(data, chunk, "grouped")
        if decode_payload(wire) != canonical:
            raise ValueError("Sources-only roundtrip changed data")
        # Verify against the exact saved production request, rather than
        # silently benchmarking newly prepared source text.
        key = digest({"stage": "reader", "instructions": INSTRUCTIONS, "payload": canonical, "schema": SCHEMA,
                      "model": manifest["model"], "effort": manifest["effort"]})
        if digest(read(baseline / "requests" / key / "input.json")) != key:
            raise ValueError("Historical Reader request differs: " + chunk["id"])
        styles += len(canonical.get("source_image_styles", {}))
        failures += sum(s.get("status") != "success" for s in canonical.get("source_sequence_ocr", {}).values())
        sizes.append({"chunk": chunk["id"], "sources": len(canonical["sources"]),
            "source_tokens_rows": size(compact(canonical["sources"])),
            "source_tokens_grouped": size(compact(wire["sources"])),
            "prompt_tokens_rows": size(INSTRUCTIONS + "\nINPUT_JSON:\n" + compact(canonical)),
            "prompt_tokens_grouped": size(prompt + "\nINPUT_JSON:\n" + compact(wire))})
    audit = {"chunks_checked": len(sizes), "source_transmissions_checked": sum(s["sources"] for s in sizes),
        "unique_sources": len(data["sources"]), "source_image_style_entries_checked": styles,
        "failed_or_stale_ocr_entries_checked": failures, "roundtrip_equal": True,
        "same_initial_dataset": True, "same_chunk_membership": True, "local_tokenizer": "o200k_base (not verified model billing)",
        "local_totals": {k: sum(s[k] for s in sizes) for k in sizes[0] if k not in {"chunk", "sources"}}, "sizes": sizes}
    write_json(output / "transport_audit.json", audit)
    print(compact({k: v for k, v in audit.items() if k != "sizes"}), flush=True)
    if plan_only:
        return
    for name in ["source_transport.py", "pipeline.py", "contracts.py", "provider.py"]:
        target = output / "source" / name
        target.parent.mkdir(exist_ok=True)
        shutil.copyfile(ROOT / "src/dataagent2" / name, target)
    results, outputs = {}, {}
    for mode in ["rows", "grouped"]:
        results[mode], outputs[mode] = run_mode(data, manifest, chunks, mode, output / mode, workers=min(6, len(chunks)))
    signatures_by_mode = {m: signatures(data, chunks, outputs[m])[0] for m in outputs}
    differences = compare(signatures_by_mode["rows"], signatures_by_mode["grouped"])
    write_json(output / "fact_comparison.json", differences)
    by_chunk = {}
    for chunk in chunks:
        c = [chunk]
        by_chunk[chunk["id"]] = compare(signatures(data, c, outputs["rows"])[0], signatures(data, c, outputs["grouped"])[0])
    write_json(output / "by_chunk_comparison.json", by_chunk)
    write_json(output / "report.json", {"scope": protocol["scope"], "sample": protocol["chunks"],
        "results": results, "comparison": differences,
        "limitations": ["Purposefully selected sample, not a full-document accuracy guarantee.",
                        "Rows and grouped calls run sequentially; load/caching can affect timing.",
                        "Fresh model outputs are references, not human ground truth; wording differences need review.",
                        "No downstream Action/Answer run; no claim that a complete Dataset2 is preserved."]})
    print(compact({"results": results, "differences": {k: {x: v[x] for x in ["rows", "grouped", "shared", "rows_only", "grouped_only"]} for k, v in differences.items()}}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--plan-only", action="store_true")
    parser.add_argument("--chunks", nargs="+")
    args = parser.parse_args()
    main(args.baseline, args.output, plan_only=args.plan_only, selected=args.chunks)
