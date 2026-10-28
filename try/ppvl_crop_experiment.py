"""Compare archived table OCR, upright tables, sequence blocks and physical lines.

All image variants, raw PPVL responses, timings and scores stay beside this file.
References are assistant visual transcriptions of original raster pages, not an
independent human gold set. No historical repaired sequence is used as a target.
"""
from __future__ import annotations

import argparse
from collections import Counter
import copy
import difflib
import hashlib
import html
import importlib.metadata
import json
import math
import os
from pathlib import Path
import re
import time

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
RUN = ROOT / ".studio/patents/WO2024020591A9/runs/0bfd050356294c83a55dc1fc317c4732/document/structured"
OUT = HERE / "ppvl_crop_results"
AA = set("ACDEFGHIKLMNPQRSTVWY")
REFERENCES = {
    "AB-1": "QMQLVESGGGVVQPGRSLRLSCAASGFTFRTYGMHWVRQAPGKGLEWVAVVWYSGSYTHYADSVKGRFTITRDNSKNTLNLQMNSLRAEDTAVYYCSRSPQWEDIFEAMDIWGQGTMVTVSS",
    "AB-2": "QMQLVESGGGVVQPGRSLRLSCAASGFTFRTYGMHWVRQAPGKGLEWVAVVWYDGSYTHYADSVKGRFTITRDNSKNTLNLQMNSLRAEDTAVYYCARSPQWEEIFEAMDIWGQGTMVTVSS",
    "AB-5": "QMQLVESGGGVVQPGRSLRLSCAASGFTFRTYGMHWVRQAPGKGLEWVAVVWYDGYDTHYADSVKGRFTITRDNSKNTLNLQMNSLRAEDTAVYYCSRSPQWEDIFESMDIWGQGTMVTVSS",
}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    temp.replace(path)


def read(path):
    return json.loads(Path(path).read_text())


def normalize_transcription(text):
    """Remove formatting only; retain unexpected glyphs as visible failures."""
    text = re.sub(r"^\s*```[^\n]*\n|\n\s*```\s*$", "", text)
    text = re.sub(r"<[^>]*>", "", text)
    text = html.unescape(text).replace("\\n", "\n").replace("\\r", "\n")
    # PPVL sometimes represents printed underlines as nested LaTeX wrappers.
    # Unwrap only known formatting commands, retaining their entire contents.
    previous = None
    while previous != text:
        previous = text
        text = re.sub(r"\\(?:underline|text|mathrm|mathbf|mathit|textbf|textit)\{([^{}]*)\}", r"\1", text)
    if text.count("$") % 2 == 0:
        text = text.replace("$", "")
    return re.sub(r"\s+", "", text)


def bands(mask, minimum_width, merge_gap=10):
    import numpy as np
    active = np.flatnonzero(mask.sum(axis=1) > minimum_width)
    groups = []
    for y in active:
        if groups and y - groups[-1][1] <= merge_gap:
            groups[-1][1] = int(y) + 1
        else:
            groups.append([int(y), int(y) + 1])
    return groups


def prepare(out):
    from PIL import Image
    import numpy as np

    images = out / "images"
    images.mkdir(parents=True, exist_ok=True)
    refs = {"kind": "assistant_visual_transcription_from_original_raster",
            "independent_human_gold": False,
            "pages": [196, 229, 230], "sequences": REFERENCES,
            "note": "Frozen before new OCR. Subject to independent human verification; no biological motif correction."}
    write(out / "references.json", refs)
    jobs = read(ROOT / ".studio/jobs.json")
    geometry = read(Path(jobs["WO2024020591A9"]["result_dir"]) / "document_sources.json")
    records, cases = {}, []
    for page, index in [(196, 1), (229, 22), (230, 23)]:
        tid = f"p{page:04d}-r0001"
        archived = read(RUN / "table_ocr" / f"{tid}.json")
        source = ROOT / "report/images" / f"WO2024020591A9_table-{index:04d}-page-{page:04d}.png"
        if sha(source) != archived["image_sha256"]:
            raise ValueError(f"Report image changed: {source}")
        original = Image.open(source).convert("RGB")
        upright = original if page == 196 else original.transpose(Image.Transpose.ROTATE_270)
        target = images / f"page_{page}_upright.png"
        upright.save(target)
        records[page] = {"source": str(source), "source_sha256": sha(source), "size": original.size,
                         "rotation_clockwise": 0 if page == 196 else 90,
                         "upright": str(target), "upright_size": upright.size}
        cases.append({"id": f"original_table_{page}", "kind": "original_table", "page": page,
                      "prompt": "table", "path": str(source)})
        if page != 196:
            cases.append({"id": f"upright_table_{page}", "kind": "upright_table", "page": page,
                          "prompt": "table", "path": str(target)})
        if page == 196:
            for name in REFERENCES:
                number = int(name.split("-")[1])
                cell_ref = f"{tid}:R{number + 2}C2"
                cell = geometry[cell_ref]
                raw = cell.get("bbox_crop")
                if not raw or len(raw) != 4:
                    raise ValueError(f"Missing existing sequence cell geometry: {cell_ref}")
                x1, y1 = max(0, math.floor(raw[0]) - 4), max(0, math.floor(raw[1]) - 4)
                x2, y2 = min(upright.width, math.ceil(raw[2]) + 4), min(upright.height, math.ceil(raw[3]) + 4)
                crop = upright.crop((x1, y1, x2, y2))
                dest = images / f"{name}_p196_block.png"
                crop.save(dest)
                common = {"item": name, "page": page, "source_ref": cell_ref,
                          "crop_box_upright": [x1, y1, x2, y2], "geometry_precision": cell.get("precision")}
                for prompt in ["ocr", "table"]:
                    cases.append({**common, "id": f"{name}_block_{prompt}", "kind": "sequence_block",
                                  "prompt": prompt, "path": str(dest)})
                mask = np.asarray(crop.convert("L")) < 150
                rows = [r for r in bands(mask, crop.width * .02) if r[1] - r[0] > 15]
                if len(rows) != 3:
                    raise ValueError(f"Expected three physical sequence lines for {name}, got {rows}")
                for line, (a, b) in enumerate(rows, 1):
                    a, b = max(0, a - 4), min(crop.height, b + 4)
                    line_image = images / f"{name}_p196_line_{line}.png"
                    crop.crop((0, a, crop.width, b)).save(line_image)
                    cases.append({**common, "id": f"{name}_line_{line}", "kind": "sequence_line",
                                  "line": line, "prompt": "ocr", "path": str(line_image),
                                  "crop_box_upright": [x1, y1 + a, x2, y1 + b]})
        else:
            mask = np.asarray(upright.convert("L")) < 150
            rows = [r for r in bands(mask, upright.width * .035) if r[1] - r[0] >= 35]
            if len(rows) != 15:
                raise ValueError(f"Expected Reference plus fourteen antibody rows on p{page}, got {rows}")
            # Explicit, auditable geometric selection for this diagnostic sample.
            # x=342/359 starts before the first residue; x=2811 excludes the SEQ label on p230.
            x1, x2 = (342, upright.width) if page == 229 else (359, 2811)
            records[page]["figure_sequence_x_range"] = [x1, x2]
            for name in REFERENCES:
                number = int(name.split("-")[1])
                a, b = rows[number]
                a, b = max(0, a - 5), min(upright.height, b + 5)
                dest = images / f"{name}_p{page}_sequence.png"
                upright.crop((x1, a, x2, b)).save(dest)
                cases.append({"id": f"{name}_figure_{page}", "kind": "figure_sequence", "item": name,
                              "page": page, "prompt": "ocr", "path": str(dest),
                              "crop_box_upright": [x1, a, x2, b], "row_selection": "ink_projection_plus_reviewed_row_order"})
    # A bounded resolution ablation on identical crop pixels, same PPVL weights.
    crop_cases = list(cases)
    for c in crop_cases:
        if c["kind"] == "sequence_block" and c["prompt"] == "ocr":
            for pixels in [501760, 1003520]:
                cases.append({**c, "id": c["id"] + f"_px{pixels}", "min_pixels": pixels, "max_pixels": 1003520})
        elif c["kind"] in ["sequence_line", "figure_sequence"]:
            cases.append({**c, "id": c["id"] + "_px501760", "min_pixels": 501760, "max_pixels": 1003520})
    for c in cases:
        c["image_sha256"] = sha(c["path"])
    manifest = {"patent": "WO2024020591A9", "source_report": str(ROOT / "report/report.md"),
                "report_was_empty": not (ROOT / "report/report.md").read_text().strip(),
                "inputs": records, "cases": cases,
                "reference_sha256": sha(out / "references.json"),
                "scope": "Three visual-reference antibodies, not a whole-patent accuracy benchmark. No source PDF, OCR or dataset is changed."}
    write(out / "manifest.json", manifest)
    return manifest


def levenshtein(a, b):
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(current[-1] + 1, previous[j] + 1, previous[j - 1] + (ca != cb)))
        previous = current
    return previous[-1]


def extract_named_row(raw, item):
    from lxml import html as parser
    try:
        doc = parser.fromstring(raw)
    except Exception:
        return ""
    for row in doc.xpath("//tr"):
        cells = ["".join(c.itertext()) for c in row.xpath("./td|./th")]
        for i, name in enumerate(cells):
            if re.sub(r"\s+", "", name) == item:
                values = []
                for value in cells[i + 1:]:
                    value = re.split(r"\(\s*SEQ|ID\s*NO", value, maxsplit=1, flags=re.I)[0]
                    normalized = normalize_transcription(value)
                    # Short terminal fragments remain sequence; only formatting,
                    # identifier-only cells and non-sequence headings are skipped.
                    if re.fullmatch(r"[A-Z]{3,}", normalized):
                        values.append(normalized)
                return "".join(values)
    return ""


def summarize(out):
    manifest = read(out / "manifest.json")
    values, timings = {}, []
    for case in manifest["cases"]:
        path = out / "raw" / f"{case['id']}.json"
        if path.is_file():
            result = read(path)
            values[case["id"]] = result
            timings.append(result["seconds"])
    rows = []

    def score(method, item, value, complete=True):
        expected = REFERENCES[item]
        distance = levenshtein(expected, value)
        differences = [{"operation": op, "reference_start_1based": i + 1,
                        "reference_end_exclusive_0based": j,
                        "observed_start_1based": k + 1, "expected": expected[i:j], "observed": value[k:l]}
                       for op, i, j, k, l in difflib.SequenceMatcher(None, expected, value, autojunk=False).get_opcodes()
                       if op != "equal"]
        rows.append({"method": method, "item": item, "expected": expected, "observed": value,
                     "expected_length": len(expected), "observed_length": len(value),
                     "exact": complete and value == expected, "complete": complete,
                     "edit_distance": distance, "CER": distance / len(expected),
                     "noncanonical_characters": sorted(set(value) - AA), "differences": differences})

    archived = {page: read(RUN / "table_ocr" / f"p{page:04d}-r0001.json")["text"] for page in [196, 229, 230]}
    for item in REFERENCES:
        score("archived_table_p196", item, extract_named_row(archived[196], item))
        score("archived_figures_p229_230", item,
              extract_named_row(archived[229], item) + extract_named_row(archived[230], item))
        key = "original_table_196"
        if key in values:
            score("rerun_original_table_p196", item, extract_named_row(values[key]["text"], item))
        keys = ["original_table_229", "original_table_230"]
        if any(k in values for k in keys):
            score("rerun_original_figures_table_prompt", item,
                  "".join(extract_named_row(values[k]["text"], item) for k in keys if k in values),
                  all(k in values for k in keys))
        full_keys = ["upright_table_229", "upright_table_230"]
        if any(k in values for k in full_keys):
            score("upright_full_figures_table_prompt", item,
                  "".join(extract_named_row(values[k]["text"], item) for k in full_keys if k in values),
                  all(k in values for k in full_keys))
        for prompt in ["ocr", "table"]:
            key = f"{item}_block_{prompt}"
            if key in values:
                score(f"sequence_block_p196_{prompt}_prompt", item, normalize_transcription(values[key]["text"]))
        for pixels in [501760, 1003520]:
            key = f"{item}_block_ocr_px{pixels}"
            if key in values:
                score(f"sequence_block_p196_ocr_min{pixels}", item, normalize_transcription(values[key]["text"]))
        keys = [f"{item}_line_{n}" for n in [1, 2, 3]]
        if any(k in values for k in keys):
            score("physical_lines_p196_ocr_prompt", item,
                  "".join(normalize_transcription(values[k]["text"]) for k in keys if k in values),
                  all(k in values for k in keys))
        keys = [f"{item}_line_{n}_px501760" for n in [1, 2, 3]]
        if any(k in values for k in keys):
            score("physical_lines_p196_ocr_min501760", item,
                  "".join(normalize_transcription(values[k]["text"]) for k in keys if k in values),
                  all(k in values for k in keys))
        keys = [f"{item}_figure_{page}" for page in [229, 230]]
        if any(k in values for k in keys):
            score("sequence_crops_figures_ocr_prompt", item,
                  "".join(normalize_transcription(values[k]["text"]) for k in keys if k in values),
                  all(k in values for k in keys))
        keys = [f"{item}_figure_{page}_px501760" for page in [229, 230]]
        if any(k in values for k in keys):
            score("sequence_crops_figures_ocr_min501760", item,
                  "".join(normalize_transcription(values[k]["text"]) for k in keys if k in values),
                  all(k in values for k in keys))
    groups = []
    for method in dict.fromkeys(r["method"] for r in rows):
        selected = [r for r in rows if r["method"] == method]
        groups.append({"method": method, "exact": sum(r["exact"] for r in selected), "items": len(selected),
                       "complete_items": sum(r["complete"] for r in selected),
                       "edit_distance": sum(r["edit_distance"] for r in selected),
                       "reference_characters": sum(r["expected_length"] for r in selected)})
    summary = {"completed_calls": len(values), "planned_calls": len(manifest["cases"]),
               "inference_seconds_sum": round(sum(timings), 3), "groups": groups, "comparisons": rows,
               "reference_kind": "assistant visual transcription, not independent human gold",
               "note": "Archived rows are reused historical OCR, not new model calls. No Codex/LLM extraction calls are made."}
    write(out / "scores.json", summary)
    method_names = {
        "archived_table_p196": "旧缓存：第196页整表",
        "archived_figures_p229_230": "旧缓存：两幅侧向比对图整图拼接",
        "rerun_original_table_p196": "本次重跑：第196页原始整表",
        "rerun_original_figures_table_prompt": "本次重跑：两幅侧向比对图整图拼接",
        "upright_full_figures_table_prompt": "两幅图转正后整图识别、按名称拼接",
        "sequence_block_p196_ocr_prompt": "单条三行序列裁图，OCR提示，默认像素预算",
        "sequence_block_p196_table_prompt": "单条三行序列裁图，表格提示，默认像素预算",
        "sequence_block_p196_ocr_min501760": "单条序列裁图，OCR提示，最少501760像素",
        "sequence_block_p196_ocr_min1003520": "单条序列裁图，OCR提示，最少1003520像素",
        "physical_lines_p196_ocr_prompt": "三行分别识别再拼接，默认像素预算",
        "physical_lines_p196_ocr_min501760": "三行分别识别再拼接，最少501760像素",
        "sequence_crops_figures_ocr_prompt": "两幅图分别裁出同一序列再拼接，默认像素预算",
        "sequence_crops_figures_ocr_min501760": "两幅图分别裁出同一序列再拼接，最少501760像素",
    }
    lines = ["# PPVL 序列裁图识别实验", "", "样本：WO2024020591A9 的 AB-1、AB-2、AB-5，覆盖 PDF 第 196、229、230 页。",
             "", "参考序列为本次助手根据原始图片核读的暂定转录，尚非独立人工金标准。旧 OCR 和历史修复结果没有用作参考答案。",
             "", f"已完成 {len(values)}/{len(manifest['cases'])} 次本地 PPVL 调用；推理耗时累计 {sum(timings):.1f} 秒。",
             "", "## 当前观察", "", "在这三个诊断样本中，裁切能减少部分错误，但已测试条件均未得到整条完全一致的结果。单条三行裁图的默认识别在三条序列同一位置将 Q 识别成 O；提高视觉像素预算并未稳定改善。不能将本结果外推为整个专利的准确率。",
             "", "第196页原始整表及两幅侧向比对图已用同一现有权重重新识别，输出重现了原缓存中的问题。转正后的整幅 FIG.2B 还出现了抗体行名与 SEQ ID 的错配：原始输出中 AB-1 行附带 SEQ ID NO:3，因此单纯转正整图也不能保证关系正确。",
             "", "| 方式 | 整条完全一致 | 编辑距离合计 | CER | 已完成样本 |", "|---|---:|---:|---:|---:|"]
    for g in groups:
        lines.append(f"| {method_names.get(g['method'], g['method'])} | {g['exact']}/{g['items']} | {g['edit_distance']} | {g['edit_distance']/g['reference_characters']:.2%} | {g['complete_items']}/{g['items']} |")
    lines += ["", "CER = Levenshtein 编辑距离 / 参考序列字符数；每组参考共366个氨基酸。FR/RF 次序交换可计为两次编辑，编辑距离不等同于不同位置的数量。HTML、换行和已知 LaTeX 下划线排版标记会被确定性移除，但字符 O 不会被改成 Q。", ""]
    lines += ["", "## 逐条结果", ""]
    for r in rows:
        lines += [f"### {r['method']} / {r['item']}", "", f"完全一致：{r['exact']}；编辑距离：{r['edit_distance']}；长度：{r['observed_length']} / {r['expected_length']}。", "", "```text", r["observed"], "```", ""]
        for difference in r["differences"]:
            lines.append(f"- 参考位置 {difference['reference_start_1based']} 附近：`{difference['expected'] or '(空)'}` → `{difference['observed'] or '(空)'}`。")
        lines.append("")
    lines += ["## 裁图核查", "", "图像仅作裁剪、90°转正，像素未修复、未去下划线。p196 使用现有单元格定位；两幅图使用转正后的像素行投影和人工核对的行序。", ""]
    for c in manifest["cases"]:
        if c.get("item") == "AB-1" and c.get("kind") in ["sequence_block", "sequence_line", "figure_sequence"] and c["prompt"] == "ocr":
            lines += [f"{c['id']}：", "", f"![{c['id']}](images/{Path(c['path']).name})", ""]
    (out / "REPORT.md").write_text("\n".join(lines) + "\n")
    return summary


def run(out, model_dir, max_new_tokens):
    os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
    os.environ.setdefault("PADDLE_PDX_ENABLE_MKLDNN_BYDEFAULT", "false")
    os.environ.setdefault("FLAGS_use_mkldnn", "0")
    manifest = prepare(out)
    from paddleocr import PaddleOCRVL
    from paddlex.inference import load_pipeline_config

    config = copy.deepcopy(load_pipeline_config("PaddleOCR-VL-1.6"))
    config["batch_size"] = 1
    config["SubModules"]["VLRecognition"]["batch_size"] = 1
    settings = {"pipeline": "PaddleOCR-VL-1.6", "model_dir": str(model_dir.resolve()),
                "device": "gpu:0", "batch_size": 1, "max_new_tokens": max_new_tokens,
                "use_layout_detection": False, "use_doc_orientation_classify": False,
                "use_doc_unwarping": False,
                "model_config_sha256": sha(model_dir / "config.json"),
                "model_weights_sha256": sha(model_dir / "model.safetensors"),
                "packages": {name: importlib.metadata.version(name) for name in ["paddleocr", "paddlex", "paddlepaddle-gpu"]}}
    write(out / "settings.json", settings)
    start = time.perf_counter()
    engine = PaddleOCRVL(pipeline_version="v1.6", paddlex_config=config,
                        vl_rec_model_dir=str(model_dir), device="gpu:0", use_layout_detection=False,
                        use_doc_orientation_classify=False, use_doc_unwarping=False,
                        enable_mkldnn=False, cpu_threads=4)
    init_seconds = time.perf_counter() - start
    print(f"Model initialized in {init_seconds:.2f}s", flush=True)
    # Give the three example sequences results before the more costly full tables.
    ordered = sorted(manifest["cases"], key=lambda c: (c["kind"] in ["upright_table", "original_table"], c["page"], c.get("item", ""), c["id"]))
    for index, case in enumerate(ordered, 1):
        target = out / "raw" / f"{case['id']}.json"
        signature = hashlib.sha256(json.dumps({"case": case, "settings": settings}, sort_keys=True).encode()).hexdigest()
        if target.is_file() and read(target).get("signature") == signature:
            print(f"[{index}/{len(ordered)}] cached {case['id']}", flush=True)
            continue
        print(f"[{index}/{len(ordered)}] START {case['id']} prompt={case['prompt']}", flush=True)
        begun = time.perf_counter()
        pixel_settings = {k: case[k] for k in ["min_pixels", "max_pixels"] if k in case}
        results = list(engine.predict(input=str(case["path"]), prompt_label=case["prompt"],
                                      max_new_tokens=max_new_tokens, **pixel_settings))
        if len(results) != 1:
            raise ValueError("Expected one result for one image")
        raw = results[0].json["res"]
        text = "\n".join(b["block_content"] for b in raw.get("parsing_res_list", []) if b.get("block_content"))
        if not text.strip():
            raise ValueError("Model returned no text")
        seconds = time.perf_counter() - begun
        write(target, {"signature": signature, "case": case, "text": text, "seconds": round(seconds, 3), "raw": raw})
        write(out / "progress.json", {"phase": "running", "last_completed": case["id"], "index": index,
                                      "total": len(ordered), "initialization_seconds": round(init_seconds, 3)})
        summary = summarize(out)
        print(f"[{index}/{len(ordered)}] DONE {case['id']} seconds={seconds:.2f} text={text[:160]!r}", flush=True)
    summary = summarize(out)
    write(out / "progress.json", {"phase": "finished", "completed_calls": summary["completed_calls"],
                                  "total": len(ordered), "initialization_seconds": round(init_seconds, 3),
                                  "wall_seconds": round(time.perf_counter() - start, 3)})
    print(json.dumps({k: summary[k] for k in ["completed_calls", "planned_calls", "inference_seconds_sum", "groups"]}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "run", "summarize"])
    parser.add_argument("--output", type=Path, default=OUT)
    parser.add_argument("--model-dir", type=Path, default=Path("/root/.paddlex/official_models/PaddleOCR-VL-1.6"))
    parser.add_argument("--max-new-tokens", type=int, default=8192)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    if args.action == "prepare":
        result = prepare(args.output)
        print(f"Prepared {len(result['cases'])} cases in {args.output}")
    elif args.action == "summarize":
        result = summarize(args.output)
        print(json.dumps(result["groups"], ensure_ascii=False, indent=2))
    else:
        run(args.output, args.model_dir, args.max_new_tokens)
