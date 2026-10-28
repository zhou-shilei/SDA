"""Ground the four saved CSJ117 proposals against freshly recognized pixels.

All other accepted facts and image-style domains are retained after checking
their literal source hashes. Writes only to a new review directory; no LLM.
"""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from lxml import html
from dataagent.sda_inputs import _rows
from dataagent2.contracts import empty_response
from dataagent2.inputs import load_input, read
from dataagent2.ledger import Ledger
from dataagent2.pipeline import question_tasks
from dataagent2.provider import digest, write_json
from dataagent2.studio_pipeline import export_result
from dataagent2.export_rows import export
from document.pdf2doc import parse_grid, _ground_table_cells


def rebuild(source, recognized, output):
    if output.exists():
        raise ValueError("Choose a fresh review directory")
    seed, native = output / "shared_seed", output / "dataagent2"
    seed.mkdir(parents=True)
    native.mkdir()
    for name in ("sda_input.json", "document_template.json", "agent_state.json", "assay_measurements.json"):
        shutil.copy2(source / "shared_seed" / name, seed / name)
    prepared = read(seed / "sda_input.json")
    recognized_table = read(recognized)
    target_refs = {f"p0025-r0001:R{r}C2" for r in (6, 7, 8, 9)}
    recognized_cells = {c["ref"]: c for c in recognized_table["cells"]}
    for ref in target_refs:
        if recognized_cells[ref]["sequence_ocr"]["status"] != "success":
            raise ValueError("Unresolved pixel recognition: " + ref)
    table = next(t for t in prepared["tables"] if t["source_id"] == "p0025-r0001")
    evidence = table["sda_source"]["evidence"]
    grid = parse_grid(evidence["text"], table["source_id"])
    tree = html.fromstring(evidence["text"])
    elements = tree.xpath(".//tr/td|.//tr/th")
    physical = [c for row in grid["physical"] for c in row]
    if len(elements) != len(physical):
        raise ValueError("Physical cell addresses changed")
    for c, element in zip(physical, elements):
        if c["ref"] in target_refs:
            for child in list(element):
                element.remove(child)
            element.text = recognized_cells[c["ref"]]["text"]
    evidence["text"] = html.tostring(tree, encoding="unicode")
    evidence["text_sha256"] = hashlib.sha256(evidence["text"].encode()).hexdigest()
    evidence["cells"] = [copy.deepcopy(recognized_cells[c["ref"]]) if c["ref"] in target_refs else c
                         for c in evidence["cells"]]
    audit = evidence["sequence_ocr"]
    audit["cells"] = [copy.deepcopy(recognized_cells[c["ref"]]["sequence_ocr"]) if c["ref"] in target_refs else c
                      for c in audit["cells"]]
    audit["cell_recovery"] = {"revision": recognized_table["sequence_ocr"]["revision"], "refs": sorted(target_refs)}
    audit["successful_cells"] = sum(c["status"] == "success" for c in audit["cells"])
    audit["failed_cells"] = len(audit["cells"]) - audit["successful_cells"]
    audit["status"] = "partial" if audit["failed_cells"] else "complete"
    page_lines = [b for b in prepared["document_blocks"] if b["page"] == table["page"]]
    _ground_table_cells(evidence, evidence, page_lines=page_lines)
    table["sda_source"].update(text=evidence["text"], text_sha256=evidence["text_sha256"])
    table["rows"] = _rows(evidence["text"], table["source_id"], evidence)
    write_json(seed / "sda_input.json", prepared)
    data = load_input(seed)
    old_sources = read(source / "dataagent2/sources.json")
    changed = set()
    for sid, old in old_sources.items():
        sid = int(sid)
        current = data["sources"][sid]
        if current["source_ref"] != old["source_ref"]:
            raise ValueError("Source address changed")
        if current["text"] != old["text"] or current.get("sequence_ocr") != old.get("sequence_ocr"):
            if current["source_ref"] not in target_refs:
                raise ValueError("An unrelated source changed")
            changed.add(sid)
    before = read(source / "dataagent2/dataset.json")
    ledger = Ledger(data)
    ledger.entities = {e["id"]: {k: copy.deepcopy(v) for k, v in e.items()
                               if k not in ("sequences", "missing", "relations")} for e in before["entities"]}
    for kind, facts in read(source / "dataagent2/facts.json").items():
        for fact in facts:
            if kind == "sequences":
                if any(f["s"] in changed for f in fact["fragments"]):
                    continue
                for f in fact["fragments"]:
                    text = data["sources"][f["s"]]["text"]
                    if digest(text) != f["source_sha256"] or text[f["start"]:f["end"]] != f["quote"]:
                        raise ValueError("An unchanged sequence failed literal source validation")
            ledger.add(kind, copy.deepcopy(fact))
    request_root = read(source / "dataagent2/repair_manifest.json", {}).get("source_result", str(source))
    response = empty_response()
    for request in (Path(request_root) / "dataagent2/requests").glob("*/validated.json"):
        for fact in read(request).get("sequences", []):
            if fact.get("seq") in {65, 66, 67, 68} and any(p["s"] in changed for p in fact["parts"]):
                response["sequences"].append(fact)
    ledger.apply(response, list(data["sources"]), "pixel-recovery")
    if ledger.rejections:
        raise ValueError("A recovered definition failed source grounding")
    ledger.rejections.extend(r for r in before["rejected_facts"]
                             if not any(p["s"] in changed for p in r["fact"].get("parts", [])))
    final = ledger.assemble()
    final.update(status="partial", reader_coverage={**before["reader_coverage"],
        "pixel_recovered_sources": sorted(changed), "scope": "Saved accepted facts with four separately re-recognized table cells"})
    by_id = {e["id"]: e for e in final["entities"]}
    for old in before["entities"]:
        for chain in ("VH", "VL", "H_CHAIN", "L_CHAIN"):
            slot = old["sequences"][chain]
            if slot["status"] == "supported" and by_id[old["id"]]["sequences"][chain]["value"] != slot["value"]:
                raise ValueError("Recovery changed an existing supported chain")
    csj = next(e for e in final["entities"] if e["name"] == "CSJ117")
    if any(csj["sequences"][c]["status"] != "supported" for c in ("VH", "VL", "CH", "CL")):
        raise ValueError("CSJ117 still has an unresolved target region")
    for name, value in (("dataset.json", final), ("facts.json", ledger.facts), ("sources.json", data["sources"]),
                        ("questions.json", question_tasks(final, data))):
        write_json(native / name, value)
    dataset = export_result(data, final, seed, output)
    export(native / "dataset.json", native / "review.formal")
    report = {"source_result": str(source), "recognized_table": str(recognized), "new_model_calls": 0,
              "changed_source_ids": sorted(changed), "previous_summary": before["summary"], "summary": final["summary"],
              "CSJ117": {c: {"status": csj["sequences"][c]["status"], "seq_id": csj["sequences"][c]["seq_id"],
                             "length": len(csj["sequences"][c]["value"])} for c in ("VH", "VL", "CH", "CL")},
              "exported_entities": len(dataset["entities"])}
    write_json(output / "recovery_report.json", report)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=ROOT / ".studio/patents/WO2026086808-dataagent2-styles-20261027/runs/printed_styles_20261027/results/WO2026086808")
    parser.add_argument("--recognized", type=Path, default=ROOT / "try/csj117_recovery/live/table.json")
    parser.add_argument("--output", type=Path, default=ROOT / "try/csj117_recovery/rebuilt")
    args = parser.parse_args()
    print(json.dumps(rebuild(args.source.resolve(), args.recognized.resolve(), args.output.resolve()), ensure_ascii=False, indent=2))
