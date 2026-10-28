"""Publish a separate Studio run using saved facts plus reviewed image styles.

No reader/answer calls, OCR overwrites, or inferred parent-to-mutant inheritance.
"""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import shutil
import sys
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from studio.sda_data import Workspace, now
from dataagent2.inputs import load_input
from dataagent2.ledger import Ledger
from dataagent2.provider import digest, write_json
from dataagent2.studio_pipeline import export_result
from dataagent2.export_rows import export as export_rows
from dataagent2.pipeline import question_tasks
from dataagent.main_pipeline import collect_usage
from units.token_usage import write_usage_report


def publish(source_id, reviewed, identity):
    started = perf_counter()
    workspace = Workspace(ROOT)
    source_job = workspace.jobs()[source_id]
    source = Path(source_job["result_dir"])
    parent = workspace.state / "patents" / identity
    if parent.exists():
        raise ValueError("Use a new Studio identity to preserve previous results")
    run = parent / "runs" / "printed_styles_20261027"
    evidence_id = source_job.get("evidence_document_id") or source_id
    output = run / "results" / evidence_id
    seed = output / "shared_seed"
    native = output / "dataagent2"
    output.mkdir(parents=True)
    seed.mkdir()
    native.mkdir()
    for name in ("sda_input.json", "agent_state.json", "document_template.json", "assay_measurements.json"):
        shutil.copy2(source / "shared_seed" / name, seed / name)
    prepared = json.loads((reviewed / "sda_input.json").read_text())
    report = json.loads((reviewed / "sequence_styles.json").read_text())
    if not report["summary"]["explicit_domains"]:
        raise ValueError("No image-backed explicit domains were recovered")
    write_json(seed / "sda_input.json", prepared)
    # Preserve a self-contained original-input index for the Studio viewer.
    inputs = parent / "tables"
    inputs.mkdir()
    original_inputs = Path(source_job["input_dir"])
    for path in original_inputs.iterdir():
        if path.is_file() and (path.suffix == ".png" or path.name.endswith("_manifest.json")):
            shutil.copy2(path, inputs / path.name)
    pdf = workspace.source_pdf(source_id, original_inputs, {})
    if pdf:
        shutil.copy2(pdf, parent / "source.pdf")
    data = load_input(seed)
    before = json.loads((source / "dataagent2/dataset.json").read_text())
    old_sources = json.loads((source / "dataagent2/sources.json").read_text())
    for sid, original in old_sources.items():
        current = data["sources"][int(sid)]
        if current["source_ref"] != original["source_ref"] or current["text"] != original["text"]:
            raise ValueError("The saved reader facts refer to different original OCR")
    ledger = Ledger(data)
    ledger.entities = {e["id"]: {k: copy.deepcopy(v) for k, v in e.items()
                               if k not in ("sequences", "missing", "relations")} for e in before["entities"]}
    facts = json.loads((source / "dataagent2/facts.json").read_text())
    for kind, values in facts.items():
        for fact in values:
            if kind == "sequences":
                for fragment in fact["fragments"]:
                    text = data["sources"][fragment["s"]]["text"]
                    if (text[fragment["start"]:fragment["end"]] != fragment["quote"]
                            or digest(text) != fragment["source_sha256"]):
                        raise ValueError("Saved sequence fragment changed")
            ledger.add(kind, copy.deepcopy(fact))
    ledger.rejections.extend(before["rejected_facts"])
    final = ledger.assemble()
    final["reader_coverage"] = {**before["reader_coverage"],
        "image_reviewed_sources": report["summary"]["reviewed_cells"],
        "scope": "Original completed reader facts reused unchanged; additional printed-image-style evidence reviewed separately"}
    final["status"] = "partial" if any(e["missing"] for e in final["entities"]) or final["conflicts"] else "source_supported"
    # Existing supported domains cannot disappear in a style repair.
    by_id = {e["id"]: e for e in final["entities"]}
    for e in before["entities"]:
        for chain in ("VH", "VL"):
            old = e["sequences"][chain]
            if old["status"] == "supported" and by_id[e["id"]]["sequences"][chain]["value"] != old["value"]:
                raise ValueError("Style repair would overwrite a supported explicit domain")
    for name, value in (("dataset.json", final), ("facts.json", ledger.facts), ("sources.json", data["sources"]),
                        ("sequence_styles.json", report), ("questions.current.json", question_tasks(final, data))):
        write_json(native / name, value)
    exported = export_result(data, final, seed, output)
    export_rows(native / "dataset.json", native / "review.formal")
    usage = collect_usage([{"directory": str(reviewed)}], evidence_id)
    baseline_usage = json.loads((source / "_token_usage.json").read_text())
    changes = []
    old_by_id = {e["id"]: e for e in before["entities"]}
    for e in final["entities"]:
        for chain in ("VH", "VL"):
            sequence = e["sequences"][chain]
            if sequence["status"] == "supported" and old_by_id[e["id"]]["sequences"][chain]["status"] != "supported":
                changes.append({"item": e["id"], "name": e["name"], "chain": chain, "length": len(sequence["value"]),
                                "source_seq_id": sequence["seq_id"], "seq_id_scope": sequence.get("seq_id_scope"),
                                "boundary_method": sequence.get("boundary_method")})
    comparison = {"before": before["summary"], "after": final["summary"], "added": changes,
                  "new_model_usage": usage, "reused_baseline_usage": baseline_usage["totals"],
                  "note": "Only printed style recovery was run; original reader, answer and measurements reused unchanged. Not an independent accuracy evaluation."}
    write_json(output / "sequence_style_comparison.json", comparison)
    write_json(native / "report.json", comparison)
    write_json(native / "repair_manifest.json", {"source_result": str(source), "review_directory": str(reviewed),
        "source_dataset_sha256": hashlib.sha256((source / "dataagent2/dataset.json").read_bytes()).hexdigest(),
        "reader_calls_made": 0, "answer_calls_made": 0, "review_summary": report["summary"]})
    write_usage_report(output, usage)
    finished = now()
    image_seconds = sum(row.get("request", {}).get("total_elapsed_seconds", 0) for row in report["tables"])
    run_report = {"document_id": identity, "evidence_document_id": evidence_id, "run_kind": "printed_style_repair",
        "extraction_engine": "dataagent2", "extraction_mode": "dataagent2", "status": exported["status"],
        "structured_dir": source_job["structured_dir"], "result_dir": str(output), "run_dir": str(run),
        "token_usage": usage, "reused_model_tokens": baseline_usage["totals"].get("total_tokens"),
        "records": len(exported["records"]), "wall_seconds": image_seconds + perf_counter() - started,
        "phases": [{"name": "printed_sequence_styles", "seconds": image_seconds}],
        "source_run_id": source_job["run_id"], "source_document_id": source_id, "finished_at_utc": finished,
        "sequence_style_comparison": comparison,
        "cache_policy": "仅新增图片样式复核的实际用量；已完成的全文读取与测量结果直接复用，历史用量单列。"}
    write_json(output / "run_summary.json", run_report)
    write_json(run / "run_summary.json", run_report)
    write_json(output / "dataagent2_checkpoint.json", {"document_id": evidence_id,
        "inputs": json.loads((source / "dataagent2_checkpoint.json").read_text())["inputs"], "style_repair_source": str(source)})
    (run / "pipeline.log").write_text("Printed sequence styles recovered from original images.\n" + json.dumps(comparison, ensure_ascii=False, indent=2))
    workspace.update_job(identity, **{**source_job, "filename": "WO2026086808 · DataAgent 2 · 下划线边界修复",
        "status": "completed", "stage": "completed", "progress": 100, "operation": "printed_style_repair",
        "comparison_source_id": source_id, "comparison_source_run_id": source_job["run_id"],
        "evidence_document_id": evidence_id, "input_dir": str(inputs), "text_context_dir": str(inputs),
        "result_dir": str(output), "run_id": run.name, "log": str(run / "pipeline.log"),
        "created_at": finished, "started_at": finished, "finished_at": finished,
        "semantic_status": exported["status"], "error": None, "worker_pid": None, "runner_pid": None})
    return {"document_id": identity, "result_dir": str(output), **comparison}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--reviewed", type=Path, required=True)
    parser.add_argument("--identity", required=True)
    args = parser.parse_args()
    print(json.dumps(publish(args.source, args.reviewed.resolve(), args.identity), ensure_ascii=False, indent=2))
