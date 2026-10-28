"""Compare PPVL and PP-OCRv6 on identical crops after restoring clipped pixels."""
from pathlib import Path
import copy
import hashlib
import json
import os
import re
import time

from ppvl_crop_experiment import HERE, ROOT, REFERENCES, read, write, sha, normalize_transcription, levenshtein


def main():
    from PIL import Image
    import numpy as np

    out = HERE / "corrected_crop_comparison"
    (out / "images").mkdir(parents=True, exist_ok=True)
    old_manifest = read(HERE / "ppvl_crop_results/manifest.json")
    cases = [c for c in old_manifest["cases"] if c["kind"] in ["sequence_line", "figure_sequence"] and "min_pixels" not in c]
    source = ROOT / "report/images/WO2024020591A9_table-0001-page-0196.png"
    page = Image.open(source).convert("RGB")
    corrected = []
    for case in cases:
        if sha(case["path"]) != case["image_sha256"]:
            raise ValueError("Frozen input changed")
        c = copy.deepcopy(case)
        if c["kind"] == "sequence_line":
            x1, y1, x2, y2 = c["crop_box_upright"]
            image = page.crop((x1, y1, x2, y2 + 8))
            mask = np.asarray(image.convert("L")) < 160
            ys, xs = np.where(mask)
            right = min(image.width, int(xs.max()) + 1 + 8)
            image = image.crop((0, 0, right, image.height))
            c.update(crop_box_upright=[x1, y1, x1 + right, y2 + 8],
                     correction="restore eight original pixel rows below old boundary, trim right whitespace")
        else:
            image = Image.open(c["path"]).convert("RGB")
            c["correction"] = "unchanged figure crop"
        target = out / "images" / f"{c['id']}.png"
        image.save(target)
        c.update(path=str(target), image_sha256=sha(target), size=image.size)
        corrected.append(c)
    write(out / "manifest.json", {"cases": corrected, "source_sha256": sha(source),
                                   "reference_sha256": sha(HERE / "ppvl_crop_results/references.json"),
                                   "reference_kind": "assistant visual transcription; not independent human gold",
                                   "note": "Both models receive exactly the same fifteen images. No amino-acid substitution or motif correction."})
    os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
    os.environ.setdefault("PADDLE_PDX_ENABLE_MKLDNN_BYDEFAULT", "false")
    os.environ.setdefault("FLAGS_use_mkldnn", "0")
    from paddleocr import PaddleOCRVL, TextRecognition
    from paddlex.inference import load_pipeline_config

    vl_dir = Path("/root/.paddlex/official_models/PaddleOCR-VL-1.6")
    rec_dir = Path("/root/.paddlex/official_models/PP-OCRv6_medium_rec")
    settings = {"ppvl_model": "PaddleOCR-VL-1.6", "ppvl_weights_sha256": sha(vl_dir / "model.safetensors"),
                "rec_model": "PP-OCRv6_medium_rec", "rec_weights_sha256": sha(rec_dir / "inference.pdiparams"),
                "device": "gpu:0", "batch_size": 1, "ppvl_prompt": "ocr", "max_new_tokens": 8192}
    write(out / "settings.json", settings)
    cfg = copy.deepcopy(load_pipeline_config("PaddleOCR-VL-1.6"))
    cfg["batch_size"] = cfg["SubModules"]["VLRecognition"]["batch_size"] = 1
    vl = PaddleOCRVL(pipeline_version="v1.6", paddlex_config=cfg, vl_rec_model_dir=str(vl_dir), device="gpu:0",
                     use_layout_detection=False, use_doc_orientation_classify=False, use_doc_unwarping=False,
                     enable_mkldnn=False, cpu_threads=4)
    rec = TextRecognition(model_name=settings["rec_model"], model_dir=str(rec_dir), device="gpu:0",
                          enable_mkldnn=False, cpu_threads=4)
    predictions = {}
    for name, model in [("PPVL", vl), ("PP-OCRv6", rec)]:
        predictions[name] = {}
        for i, case in enumerate(corrected, 1):
            target = out / "raw" / name / f"{case['id']}.json"
            sig = hashlib.sha256(json.dumps({"case": case, "settings": settings, "model": name}, sort_keys=True).encode()).hexdigest()
            if target.is_file() and read(target).get("signature") == sig:
                result = read(target)
            else:
                begun = time.perf_counter()
                if name == "PPVL":
                    raw = list(model.predict(input=case["path"], prompt_label="ocr", max_new_tokens=8192))[0].json["res"]
                    text = "\n".join(b["block_content"] for b in raw["parsing_res_list"] if b.get("block_content"))
                    value = normalize_transcription(text)
                else:
                    output = list(model.predict(input=case["path"], batch_size=1))[0]
                    raw, text = output.json, str(output["rec_text"])
                    value = re.sub(r"\s+", "", text)
                result = {"signature": sig, "case": case, "model": name, "raw": raw, "text": text,
                          "value": value, "seconds": round(time.perf_counter() - begun, 4)}
                write(target, result)
            predictions[name][case["id"]] = result
            print(f"{name} [{i}/15] {case['id']} {result['text']!r}", flush=True)
    comparisons = []
    for name, values in predictions.items():
        for item, expected in REFERENCES.items():
            for method, keys in {
                "corrected_table_lines": [f"{item}_line_{n}" for n in [1, 2, 3]],
                "figure_sequence_crops": [f"{item}_figure_{p}" for p in [229, 230]],
            }.items():
                value = "".join(values[k]["value"] for k in keys)
                comparisons.append({"model": name, "method": method, "item": item, "value": value,
                                    "expected": expected, "exact": value == expected,
                                    "edit_distance": levenshtein(expected, value)})
    groups = []
    for name in predictions:
        for method in ["corrected_table_lines", "figure_sequence_crops"]:
            rows = [x for x in comparisons if x["model"] == name and x["method"] == method]
            groups.append({"model": name, "method": method, "exact": sum(r["exact"] for r in rows),
                           "items": len(rows), "edit_distance": sum(r["edit_distance"] for r in rows)})
    write(out / "scores.json", {"groups": groups, "comparisons": comparisons,
                                 "inference_seconds": {name: round(sum(v["seconds"] for v in values.values()), 3) for name, values in predictions.items()}})
    lines = ["# 修正裁图后：PPVL 与 PP-OCRv6 medium 同图对照", "",
             "AB-1、AB-2、AB-5；两模型读取相同15张图片。序列表行裁图恢复底部原始像素并裁去右侧空白；图中序列裁图保持原样。参考仍为先前冻结的助手目视转录，未经独立人工核验。",
             "", "| 输入 | 模型 | 整条完全一致 | 编辑距离合计 |", "|---|---|---:|---:|"]
    for g in groups:
        label = "修正后的序列表行裁图" if g["method"] == "corrected_table_lines" else "两幅图的序列裁图"
        lines.append(f"| {label} | {g['model']} | {g['exact']}/{g['items']} | {g['edit_distance']} |")
    for row in comparisons:
        lines += ["", f"## {row['model']} / {row['method']} / {row['item']}", "",
                  f"完全一致：{row['exact']}；编辑距离：{row['edit_distance']}。", "", "```text", row["value"], "```"]
    lines += ["", "全部原始模型输出在 raw/；完整输入与像素哈希在 manifest.json；模型设置及权重哈希在 settings.json。", ""]
    (out / "REPORT.md").write_text("\n".join(lines))
    print(json.dumps(groups, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
