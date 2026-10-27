"""Local material-size estimates; these tokenizers are not billing authorities."""

import argparse
import json
import os
from pathlib import Path


def count(root):
    import tiktoken

    root = Path(root)
    os.environ["TIKTOKEN_CACHE_DIR"] = str(root / "encoding_cache")
    encoders = {name: tiktoken.get_encoding(name) for name in ("o200k_base", "cl100k_base")}
    result = {"note": "Local proxy tokenizer estimates, not verified model billing. Raw material excludes prompts, source addresses and repeated context.", "documents": []}
    for folder in sorted(root.iterdir()):
        if not (folder / "sources.json").is_file():
            continue
        sources = json.loads((folder / "sources.json").read_text())
        manifest = json.loads((folder / "manifest.json").read_text())
        usage = json.loads((folder / "token_usage.json").read_text())
        pdf = "\n".join(s["text"] for s in sources.values() if s["kind"] == "text")
        tables = "\n".join(s["text"] for s in sources.values() if s["kind"] != "text")
        material = {"pdf_ocr": pdf, "table_ocr": tables, "pdf_plus_table_ocr": pdf + "\n" + tables,
                    "full_original_seed_json": (folder / "seed.json").read_text()}
        csv_path = folder / "review" / "items.csv"
        if csv_path.is_file():
            material["current_review_items_csv"] = csv_path.read_text(encoding="utf-8-sig")
        calls = [json.loads(line) for line in (folder / "calls.jsonl").read_text().splitlines() if line.strip()]
        completed = len({c["key"] for c in calls if c["stage"] == "reader" and c["status"] == "ok"})
        reader = usage.get("by_stage", {}).get("reader", {})
        row = {"document": folder.name, "local_estimates": {name: {key: len(encoder.encode(text, disallowed_special=()))
            for key, text in material.items()} for name, encoder in encoders.items()},
            "reader_completed_chunks": completed, "reader_total_chunks": len(manifest["chunks"]),
            "actual_known_reader_usage": reader,
            "full_reader_projection_from_observed_mean": {key: round(reader.get(key, 0) * len(manifest["chunks"]) / completed)
                for key in ("input_tokens", "output_tokens", "total_tokens")} if completed else None}
        result["documents"].append(row)
    (root / "material_token_estimates.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("benchmark", type=Path)
    print(json.dumps(count(parser.parse_args().benchmark), ensure_ascii=False, indent=2))
