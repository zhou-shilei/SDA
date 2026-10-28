"""Separate clipped glyph tails, right whitespace and padding on the same Q/O rows."""
from pathlib import Path
import hashlib
import json
import os
import re
import time

from ppvl_crop_experiment import HERE, ROOT, read, sha, write, levenshtein


def run():
    from PIL import Image, ImageOps
    import numpy as np

    out = HERE / "crop_boundary_results"
    (out / "images").mkdir(parents=True, exist_ok=True)
    source = ROOT / "report/images/WO2024020591A9_table-0001-page-0196.png"
    page = Image.open(source).convert("RGB")
    expected = "MDIWGQGTMVTVSS"
    cases, diagnostics = [], []
    for item in ["AB-1", "AB-2", "AB-5"]:
        prior = read(HERE / "ppocrv6_medium_results/raw" / f"{item}_line_3.json")
        original = Image.open(prior["case"]["path"]).convert("RGB")
        assert sha(prior["case"]["path"]) == prior["case"]["image_sha256"]
        box = prior["case"]["crop_box_upright"]
        x1, y1, x2, y2 = box
        mask = np.asarray(original.convert("L")) < 160
        ys, xs = np.where(mask)
        right = min(original.width, int(xs.max()) + 1 + 8)
        restored = page.crop((x1, y1, x2, y2 + 8))
        removed = np.asarray(page.convert("L"))[y2:y2 + 8, x1:x1 + right] < 160
        diagnostics.append({"item": item, "original_box": box, "original_size": original.size,
                            "ink_bbox_in_original": [int(xs.min()), int(ys.min()), int(xs.max() + 1), int(ys.max() + 1)],
                            "black_pixels_below_old_boundary": int(removed.sum()), "restored_pixels_rows": 8})
        variants = {
            "original": original,
            "trim_right_only": original.crop((0, 0, right, original.height)),
            "white_bottom_padding_only": ImageOps.expand(original, border=(0, 0, 0, 8), fill="white"),
            "restore_bottom_only": restored,
            "restore_bottom_trim_right": restored.crop((0, 0, right, restored.height)),
            "restore_bottom_trim_right_pad8": ImageOps.expand(restored.crop((0, 0, right, restored.height)), border=8, fill="white"),
        }
        for variant, image in variants.items():
            dest = out / "images" / f"{item}_{variant}.png"
            image.save(dest)
            cases.append({"id": f"{item}_{variant}", "item": item, "variant": variant,
                          "path": str(dest), "image_sha256": sha(dest), "size": image.size})
    manifest = {"source": str(source), "source_sha256": sha(source), "diagnostics": diagnostics,
                "expected": expected, "reference_kind": "assistant visual transcription, not independent human gold",
                "cases": cases, "note": "Restored pixels come directly from original raster; white padding is a separate control. No glyph repair or O-to-Q substitution."}
    write(out / "manifest.json", manifest)
    os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
    os.environ.setdefault("PADDLE_PDX_ENABLE_MKLDNN_BYDEFAULT", "false")
    os.environ.setdefault("FLAGS_use_mkldnn", "0")
    from paddleocr import TextRecognition

    model_dir = Path("/root/.paddlex/official_models/PP-OCRv6_medium_rec")
    settings = {"model": "PP-OCRv6_medium_rec", "weights_sha256": sha(model_dir / "inference.pdiparams"),
                "device": "gpu:0", "batch_size": 1}
    write(out / "settings.json", settings)
    start = time.perf_counter()
    model = TextRecognition(model_name=settings["model"], model_dir=str(model_dir), device="gpu:0",
                            enable_mkldnn=False, cpu_threads=4)
    rows = []
    for i, case in enumerate(cases, 1):
        signature = hashlib.sha256(json.dumps({"case": case, "settings": settings}, sort_keys=True).encode()).hexdigest()
        dest = out / "raw" / f"{case['id']}.json"
        if dest.is_file() and read(dest).get("signature") == signature:
            result = read(dest)
        else:
            begun = time.perf_counter()
            prediction = list(model.predict(input=case["path"], batch_size=1))[0]
            text = str(prediction["rec_text"])
            value = re.sub(r"\s+", "", text)
            result = {"signature": signature, "case": case, "text": text, "value": value,
                      "exact": value == expected, "edit_distance": levenshtein(expected, value),
                      "confidence": float(prediction["rec_score"]), "raw": prediction.json,
                      "seconds": round(time.perf_counter() - begun, 4)}
            write(dest, result)
        rows.append(result)
        print(f"[{i}/{len(cases)}] {case['id']} {result['text']!r} exact={result['exact']}", flush=True)
    groups = [{"variant": variant, "exact": sum(r["exact"] for r in rows if r["case"]["variant"] == variant),
               "count": sum(r["case"]["variant"] == variant for r in rows)}
              for variant in variants]
    full_sequences = []
    frozen = read(HERE / "ppvl_crop_results/references.json")["sequences"]
    for row in rows:
        item = row["case"]["item"]
        first_two = [read(HERE / "ppocrv6_medium_results/raw" / f"{item}_line_{n}.json") for n in [1, 2]]
        for result in first_two:
            assert sha(result["case"]["path"]) == result["case"]["image_sha256"]
        value = "".join(re.sub(r"\s+", "", p["text"]) for p in first_two) + row["value"]
        full_sequences.append({"item": item, "variant": row["case"]["variant"], "value": value,
                               "exact": value == frozen[item], "edit_distance": levenshtein(frozen[item], value),
                               "note": "First two lines reuse unchanged PP-OCRv6 predictions; tail is the new model output, not a manual correction."})
    write(out / "scores.json", {"groups": groups, "results": rows,
                                 "reassembled_full_sequences": full_sequences,
                                 "wall_seconds": round(time.perf_counter() - start, 3),
                                 "inference_seconds": round(sum(r["seconds"] for r in rows), 3)})
    names = {"original": "原裁图", "trim_right_only": "只去右侧空白，保留原来的底部边界",
             "white_bottom_padding_only": "只补8像素白边，不恢复原始笔画",
             "restore_bottom_only": "从原图恢复底部8行像素，保留右侧空白",
             "restore_bottom_trim_right": "恢复底部8行像素，再去右侧空白",
             "restore_bottom_trim_right_pad8": "恢复底部、去右侧空白，再四周补8像素白边"}
    lines = ["# Q/O 裁剪边界对照", "", "同一 PP-OCRv6 medium，batch_size=1，三张尾行，参考为 MDIWGQGTMVTVSS。",
             "", "恢复底部使用原始图片中的真实像素；不对残基作替换。三条图片来自三个抗体，但该尾部文字相同，不能视为三种不同序列的广泛验证。",
             "", "| 裁图条件 | 完全一致 |", "|---|---:|"]
    for g in groups:
        lines.append(f"| {names[g['variant']]} | {g['exact']}/{g['count']} |")
    repaired = [r for r in full_sequences if r["variant"] == "restore_bottom_trim_right"]
    lines += ["", f"将修正裁图识别出的尾行，与原先未改变的前两行识别输出拼接：完整序列有 {sum(r['exact'] for r in repaired)}/{len(repaired)} 条与冻结参考完全一致。没有人工将 O 替换成 Q。", ""]
    lines += ["", "## 边界诊断", "", "```json", json.dumps(diagnostics, ensure_ascii=False, indent=2), "```", "",
              "## AB-1 对照图片与输出", ""]
    for r in rows:
        if r["case"]["item"] == "AB-1":
            c = r["case"]
            lines += [f"### {names[c['variant']]}", "", f"输入尺寸：{c['size']}；模型输出：`{r['text']}`。", "",
                      f"![{c['id']}](images/{Path(c['path']).name})", ""]
    (out / "REPORT.md").write_text("\n".join(lines))
    print(json.dumps(groups, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    run()
