"""Run official PP-OCRv6 medium on the same, frozen PPVL line crops."""
from __future__ import annotations

import argparse
import difflib
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import time

from ppvl_crop_experiment import AA, REFERENCES, HERE, levenshtein, read, sha, write


def evaluate(ppvl_dir, out, predictions):
    reference = read(ppvl_dir / "references.json")
    if reference["sequences"] != REFERENCES:
        raise ValueError("References differ from the frozen PPVL experiment")
    comparisons = []
    for item, expected in reference["sequences"].items():
        methods = {
            "physical_lines_p196": [f"{item}_line_{n}" for n in [1, 2, 3]],
            "sequence_crops_figures": [f"{item}_figure_{page}" for page in [229, 230]],
        }
        for method, keys in methods.items():
            if not all(k in predictions for k in keys):
                continue
            observed = "".join(re.sub(r"\s+", "", predictions[k]["text"]) for k in keys)
            differences = [{"operation": op, "reference_position_1based": i + 1,
                            "expected": expected[i:j], "observed": observed[k:l]}
                           for op, i, j, k, l in difflib.SequenceMatcher(None, expected, observed, autojunk=False).get_opcodes()
                           if op != "equal"]
            distance = levenshtein(expected, observed)
            comparisons.append({"method": method, "item": item, "expected": expected, "observed": observed,
                                "exact": expected == observed, "edit_distance": distance,
                                "reference_characters": len(expected), "CER": distance / len(expected),
                                "noncanonical_characters": sorted(set(observed) - AA), "differences": differences,
                                "source_cases": keys})
    groups = []
    for method in ["physical_lines_p196", "sequence_crops_figures"]:
        rows = [c for c in comparisons if c["method"] == method]
        if rows:
            groups.append({"method": method, "items": len(rows), "exact": sum(c["exact"] for c in rows),
                           "edit_distance": sum(c["edit_distance"] for c in rows),
                           "reference_characters": sum(c["reference_characters"] for c in rows),
                           "CER": sum(c["edit_distance"] for c in rows) / sum(c["reference_characters"] for c in rows)})
    summary = {"model": "PP-OCRv6_medium_rec", "reference_kind": reference["kind"],
               "independent_human_gold": False, "reference_sha256": sha(ppvl_dir / "references.json"),
               "images": len(predictions), "inference_seconds_sum": round(sum(p["seconds"] for p in predictions.values()), 3),
               "groups": groups, "comparisons": comparisons,
               "note": "Same crop files and reference as PPVL. Whitespace only is removed; no residue correction, motif inference or historical repair values."}
    write(out / "scores.json", summary)
    old = read(ppvl_dir / "scores.json")
    methods = {
        "physical_lines_p196": ("序列表逐行识别后拼接", "physical_lines_p196_ocr_prompt"),
        "sequence_crops_figures": ("两幅图的序列裁图识别后拼接", "sequence_crops_figures_ocr_prompt"),
    }
    lines = ["# PP-OCRv6 medium 序列裁图实测", "",
             "测试 AB-1、AB-2、AB-5。直接复用 PPVL 实验的9张序列表文字行裁图和6张比对图序列裁图，不新增裁剪、不更换参考。",
             "", "使用官方 PP-OCRv6_medium_rec 的单行 TextRecognition 模块；batch_size=1，不额外运行文字检测或 LLM。模型原始输出只去排版空白，不替换氨基酸。",
             "", "参考是本次助手对原始图片的核读转录，尚非独立人工金标准。以下为三个诊断样本的观察，不能外推全篇准确率。",
             "", f"本次已识别 {len(predictions)}/15 张图片，识别调用耗时合计 {summary['inference_seconds_sum']:.3f} 秒（不含加载模型）。",
             "", "| 相同裁图方式 | 模型 | 整条完全一致 | 编辑距离合计 | CER |", "|---|---|---:|---:|---:|"]
    for g in groups:
        title, previous_method = methods[g["method"]]
        previous = next(x for x in old["groups"] if x["method"] == previous_method)
        lines.append(f"| {title} | PPVL 默认 OCR 提示 | {previous['exact']}/{previous['items']} | {previous['edit_distance']} | {previous['edit_distance']/previous['reference_characters']:.2%} |")
        lines.append(f"| {title} | PP-OCRv6 medium | {g['exact']}/{g['items']} | {g['edit_distance']} | {g['CER']:.2%} |")
    lines += ["", "## 逐条输出与差异", ""]
    for row in comparisons:
        lines += [f"### {row['method']} / {row['item']}", "", f"完全一致：{row['exact']}；编辑距离：{row['edit_distance']}。", "", "```text", row["observed"], "```", ""]
        for difference in row["differences"]:
            lines.append(f"- 参考位置 {difference['reference_position_1based']} 附近：`{difference['expected'] or '(空)'}` → `{difference['observed'] or '(空)'}`。")
        lines.append("")
    lines += ["## 原始结果", "", "逐图转录、模型识别分数、输入哈希与模型原始结果在 `raw/`；模型版本、配置和权重哈希在 `settings.json`。模型分数并不代表序列正确概率。", ""]
    (out / "REPORT.md").write_text("\n".join(lines))
    return summary


def run(ppvl_dir, out, model_dir):
    os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
    os.environ.setdefault("PADDLE_PDX_ENABLE_MKLDNN_BYDEFAULT", "false")
    os.environ.setdefault("FLAGS_use_mkldnn", "0")
    out.mkdir(parents=True, exist_ok=True)
    source = read(ppvl_dir / "manifest.json")
    cases = [c for c in source["cases"] if c["kind"] in ["sequence_line", "figure_sequence"] and "min_pixels" not in c]
    if len(cases) != 15 or len({c["id"] for c in cases}) != 15:
        raise ValueError("Expected the same fifteen line crops as PPVL")
    for case in cases:
        if sha(case["path"]) != case["image_sha256"]:
            raise ValueError(f"Input crop changed: {case['path']}")
    config = {"model": "PP-OCRv6_medium_rec", "model_dir": str(model_dir.resolve()),
              "weights_sha256": sha(model_dir / "inference.pdiparams"),
              "config_sha256": sha(model_dir / "inference.yml"), "device": "gpu:0", "batch_size": 1,
              "packages": {name: importlib.metadata.version(name) for name in ["paddleocr", "paddlex", "paddlepaddle-gpu"]},
              "ppvl_reference_sha256": source["reference_sha256"]}
    write(out / "settings.json", config)
    from paddleocr import TextRecognition

    start = time.perf_counter()
    model = TextRecognition(model_name=config["model"], model_dir=str(model_dir), device="gpu:0",
                            enable_mkldnn=False, cpu_threads=4)
    initialized = time.perf_counter() - start
    predictions = {}
    for index, case in enumerate(cases, 1):
        path = out / "raw" / f"{case['id']}.json"
        signature = hashlib.sha256(json.dumps({"case": case, "settings": config}, sort_keys=True).encode()).hexdigest()
        if path.is_file() and read(path).get("signature") == signature:
            result = read(path)
        else:
            begun = time.perf_counter()
            output = list(model.predict(input=case["path"], batch_size=1))
            if len(output) != 1:
                raise ValueError("One image must yield one recognition result")
            result = {"signature": signature, "case": case, "text": str(output[0]["rec_text"]),
                      "confidence": float(output[0]["rec_score"]), "raw": output[0].json,
                      "seconds": round(time.perf_counter() - begun, 4)}
            write(path, result)
        predictions[case["id"]] = result
        print(f"[{index}/{len(cases)}] {case['id']} {result['seconds']:.3f}s {result['text']!r}", flush=True)
        write(out / "progress.json", {"phase": "running", "completed": index, "total": len(cases)})
    summary = evaluate(ppvl_dir, out, predictions)
    write(out / "progress.json", {"phase": "finished", "completed": len(cases), "total": len(cases),
                                  "initialization_seconds": round(initialized, 3),
                                  "wall_seconds": round(time.perf_counter() - start, 3)})
    print(json.dumps({k: summary[k] for k in ["images", "inference_seconds_sum", "groups"]}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ppvl-dir", type=Path, default=HERE / "ppvl_crop_results")
    parser.add_argument("--output", type=Path, default=HERE / "ppocrv6_medium_results")
    parser.add_argument("--model-dir", type=Path, default=Path("/root/.paddlex/official_models/PP-OCRv6_medium_rec"))
    args = parser.parse_args()
    run(args.ppvl_dir, args.output, args.model_dir)
