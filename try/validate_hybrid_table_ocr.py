"""Run the production Table OCR entry on the three diagnostic source images."""
from pathlib import Path
import json
import shutil
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from document.pdf2doc import configure_ocr_worker, extract_table_evidence_batch, _ground_table_cells, parse_grid
from document.sequence_ocr import sequence_body


def main():
    original = ROOT / ".studio/patents/WO2024020591A9/runs/0bfd050356294c83a55dc1fc317c4732/document/structured"
    out = ROOT / "try/hybrid_table_ocr/live"
    inputs = out / "inputs"
    inputs.mkdir(parents=True, exist_ok=True)
    layout = json.loads((original / "layout/WO2024020591A9_manifest.json").read_text())
    metadata, paths = [], []
    for page in [196, 229, 230]:
        row = next(c for c in layout["images"] if c["id"] == f"p{page:04d}-r0001")
        target = inputs / row["image"]
        shutil.copy2(ROOT / "report/images" / row["image"], target)
        metadata.append({**row, "document_id": "WO2024020591A9", "image_path": str(target)})
        paths.append(target)
    configure_ocr_worker(device="gpu:0", backend="vl", batch_size=1)
    evidence = extract_table_evidence_batch(paths, metadata)
    by_page = {}
    for item, meta in zip(evidence, metadata):
        page = json.loads((original / "page_ocr/pages" / f"page-{item['page']:04d}.json").read_text())
        _ground_table_cells(item, meta, page_lines=page.get("lines", []))
        (out / f"{item['table_id']}.json").write_text(json.dumps(item, ensure_ascii=False, indent=2))
        by_page[item["page"]] = item
    refs = json.loads((ROOT / "try/ppvl_crop_results/references.json").read_text())["sequences"]
    def sequence(page, name):
        table = by_page[page]
        grid = parse_grid(table["text"], table["table_id"])
        by_ref = {c["ref"]: c for c in table["cells"]}
        rows = [r for r in grid["physical"] if any(c["text"] == name for c in r)]
        if len(rows) != 1:
            return None
        values = [by_ref[c["ref"]]["sequence_ocr"]["sequence"] for c in rows[0]
                  if by_ref[c["ref"]].get("sequence_ocr", {}).get("status") == "success"]
        return "".join(values) if values else None
    checks = []
    for name, expected in refs.items():
        value = sequence(196, name)
        prefix, suffix = sequence(229, name), sequence(230, name)
        checks.append({"name": name, "table_matches": value == expected,
                       "figure_matches": prefix is not None and suffix is not None and prefix + suffix == expected,
                       "table_sequence": value, "figure_sequence": (prefix or "") + (suffix or "")})
    summary = {"implementation": "document.pdf2doc.extract_table_evidence_batch",
               "scope": "Three source table images, automatic orientation and addressed row/cell crops; no manual crop coordinates",
               "reference": "Prior frozen assistant visual transcription; not independent human gold",
               "checks": checks,
               "tables": [{"page": t["page"], "successful_cells": t["sequence_ocr"]["successful_cells"],
                           "failed_cells": t["sequence_ocr"]["failed_cells"],
                           "failed": [{"ref": c["ref"], "error": c.get("error")} for c in t["sequence_ocr"]["cells"] if c["status"] != "success"],
                           "comparisons": {s: sum(c.get("sequence_ocr", {}).get("page_comparison", {}).get("status") == s for c in t["cells"]) for s in ["matched", "mismatch", "uncompared"]}}
                          for t in evidence]}
    (out / "validation.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if not all(c["table_matches"] and c["figure_matches"] for c in checks):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
