"""Re-run pixel recognition for CSJ117's printed domain table, keeping history."""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from document.pdf2doc import _ground_table_cells
from document.sequence_ocr import refine


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "try/csj117_recovery/live")
    args = parser.parse_args()
    source = ROOT / ".studio/patents/WO2026086808-dataagent2-bfc155b3/runs/2f3c915cef194930b4210d8937dcb922/structured/table_ocr/tables.jsonl"
    original = next(r for r in map(json.loads, source.open()) if r["table_id"] == "p0025-r0001")
    output = args.output.resolve()
    if (output / "table.json").exists():
        raise ValueError("Choose a fresh output directory")
    images = output / "inputs"
    images.mkdir(parents=True, exist_ok=True)
    image = images / Path(original["image_path"]).name
    shutil.copy2(original["image_path"], image)
    evidence = copy.deepcopy(original)
    evidence.update(image_path=str(image), text=original["vl_text"])
    result = refine(evidence)
    _ground_table_cells(result, result)
    (output / "table.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    cells = {c["ref"]: c for c in result["cells"]}
    checks = []
    for row, number, chain, count in [(6, 65, "VL", 3), (7, 66, "VH", 3), (8, 67, "CL", 3), (9, 68, "CH", 8)]:
        cell = cells[f"p0025-r0001:R{row}C2"]
        audit = cell["sequence_ocr"]
        checks.append({"seq_id": number, "region": chain, "status": audit["status"],
                       "expected_lines": count, "recognized_lines": len(audit["lines"]),
                       "length": len(audit.get("sequence", "")), "sequence": audit.get("sequence"),
                       "error": audit.get("error"), "localization": audit.get("localization")})
    report = {"source": str(source), "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
              "scope": "Saved PPVL structure; fresh local detection, pixel crops and PP-OCRv6 recognition. No LLM calls or historical writes.",
              "checks": checks}
    (output / "validation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not all(c["status"] == "success" and c["expected_lines"] == c["recognized_lines"] for c in checks):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
