"""Monitor a fresh patent run and compare saved native datasets without model calls."""

import argparse
from collections import Counter
from datetime import datetime
from pathlib import Path
import json
import re
import sys
import time
import unicodedata
from zoneinfo import ZoneInfo


def read(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def save(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def name_key(value):
    return re.sub(r"\s+", "", str(value)).casefold()


def entities_by_name(dataset):
    index = {}
    for entity in dataset.get("entities", []):
        for key in {name_key(n) for n in [entity["name"], *entity.get("names", [])]}:
            index.setdefault(key, []).append(entity)
    return index


def chain_checks(dataset):
    index = entities_by_name(dataset)
    light = [20, 21, 20, 22, 23, 24, 25, 20, 20, 26, 27, 28, 29, 30]
    result = []
    for number in range(1, 15):
        matches = index.get(name_key(f"AB-{number}"), [])
        entity = matches[0] if len(matches) == 1 else {}
        for chain, expected in [("VH", number + 3), ("VL", light[number - 1])]:
            slot = entity.get("sequences", {}).get(chain, {})
            ids = slot.get("mapping_seq_ids", slot.get("seq_ids", []))
            result.append({"name": f"AB-{number}", "chain": chain, "expected_seq_id": expected,
                "actual_seq_ids": ids, "mapping_correct": set(ids) == {expected}
                    and slot.get("mapping_status") == "supported" and slot.get("seq_id_scope") != "full_chain",
                "sequence_supported": slot.get("status") == "supported", "sequence_length": len(slot.get("value") or ""),
                "status": slot.get("status", "entity_missing"), "entity_matches": len(matches)})
    return result


def compare_chains(before, after):
    old, new = entities_by_name(before), entities_by_name(after)
    rows, seen = [], set()
    for name in sorted(set(old) & set(new)):
        if len(old[name]) != 1 or len(new[name]) != 1:
            continue
        a, b = old[name][0], new[name][0]
        pair = (a["id"], b["id"])
        if pair in seen:
            continue
        seen.add(pair)
        for chain in ("VH", "VL"):
            x, y = a["sequences"][chain], b["sequences"][chain]
            supported_x, supported_y = x["status"] == "supported", y["status"] == "supported"
            status = ("same_sequence" if supported_x and supported_y and x["value"] == y["value"] else
                      "different_sequence" if supported_x and supported_y else "newly_supported" if supported_y else
                      "lost_support" if supported_x else "still_unresolved")
            rows.append({"name": b["name"], "chain": chain, "comparison": status,
                "old_seq_ids": x.get("seq_ids", []), "new_seq_ids": y.get("seq_ids", []),
                "old_length": len(x.get("value") or ""), "new_length": len(y.get("value") or "")})
    return {"counts": dict(Counter(row["comparison"] for row in rows)), "rows": rows,
            "scope": "Unique exact names/aliases only; changed residue strings require source review."}


def measurement_key(row, entities):
    metric = re.sub(r"[\s_${}\\]", "", row.get("metric", ""))
    metric = metric if metric in ("KD", "kd") else metric.upper()
    return (name_key(entities.get(row["item"], row["item"])), metric,
            re.sub(r"\s+", "", row["value"]), re.sub(r"\s+", "", unicodedata.normalize("NFKC", row.get("unit") or "")))


def compare_measurements(before, after):
    def values(dataset):
        names = {e["id"]: e["name"] for e in dataset["entities"]}
        return Counter(measurement_key(row, names) for row in dataset["observations"])
    a, b = values(before), values(after)
    return {"common_count": sum((a & b).values()), "old_only_count": sum((a - b).values()),
        "new_only_count": sum((b - a).values()),
        "old_only_examples": [list(key) + [count] for key, count in (a - b).most_common(15)],
        "new_only_examples": [list(key) + [count] for key, count in (b - a).most_common(15)],
        "scope": "Literal-name/endpoint/value/unit multiset overlap only; does not prove correct experiment/condition assignment."}


def compare_model_reference(dataset, reference):
    """Compare only explicit variable-domain numbers and same-scope residue strings."""
    index = entities_by_name(dataset)
    ref_entities = {e["id"]: e for e in reference["entities"]}
    expected, sequence_rows = {}, []
    for link in reference["sequence_links"]:
        if link["region_type"] not in ("VH", "VL"):
            continue
        name = ref_entities[link["entity_id"]]["name"]
        match = re.fullmatch(r"SEQ-(\d+)", link["sequence_id"])
        if match:
            expected.setdefault((name, link["region_type"]), set()).add(int(match[1]))
    mappings = []
    for (name, chain), ids in sorted(expected.items()):
        matches = index.get(name_key(name), [])
        slot = matches[0].get("sequences", {}).get(chain, {}) if len(matches) == 1 else {}
        actual = set(slot.get("mapping_seq_ids", slot.get("seq_ids", [])))
        mappings.append({"name": name, "chain": chain, "reference_seq_ids": sorted(ids),
            "actual_seq_ids": sorted(actual), "entity_matches": len(matches),
            "agreement": actual == ids and slot.get("mapping_status") == "supported"
                         and slot.get("seq_id_scope") != "full_chain"})
    regions = {"VH": "VH", "VL": "VL", "H_CHAIN": "full_heavy_chain", "L_CHAIN": "full_light_chain",
        "CH": "heavy_chain_constant_domain", "CL": "light_chain_constant_domain",
        **{f"CDR{chain}{i}": f"{chain}CDR{i}" for chain in "HL" for i in (1, 2, 3)}}
    native = {}
    for seq in dataset.get("sequences", []):
        if seq.get("seq_id_scope") == "full_chain" and seq.get("region") in ("VH", "VL"):
            continue
        if seq.get("evidence_role") == "derived_variable" or seq.get("boundary_method") == "printed_image_style":
            continue
        if seq.get("seq") is not None and seq.get("region") in regions:
            native.setdefault((seq["seq"], regions[seq["region"]]), set()).add(seq["value"])
    for seq in reference["sequences"]:
        values = native.get((seq["seq_id_no"], seq["region_type"]), set())
        if seq["region_type"] not in regions.values():
            continue
        sequence_rows.append({"seq_id": seq["seq_id_no"], "region": seq["region_type"],
            "comparison": "same" if values == {seq["sequence"]} else "contains_reference_and_variants"
                          if seq["sequence"] in values else "different" if values else "not_extracted_at_same_scope",
            "native_lengths": sorted({len(v) for v in values}), "reference_length": len(seq["sequence"])})
    return {"scope": "Previous full-PDF model output, not human ground truth. Exact names and printed SEQ IDs/scopes only.",
        "variable_mapping_total": len(mappings), "variable_mapping_agreements": sum(m["agreement"] for m in mappings),
        "variable_mapping_rows": mappings,
        "sequence_counts": dict(Counter(r["comparison"] for r in sequence_rows)), "sequence_rows": sequence_rows}


def measured_pair_coverage(dataset):
    if not dataset:
        return None
    entities = {e["id"]: e for e in dataset["entities"]}
    return {"measurements": len(dataset["observations"]),
        "with_complete_pair": sum(all(entities[o["item"]]["sequences"][chain]["status"] == "supported"
                                      for chain in ("VH", "VL")) for o in dataset["observations"])}


def compare_physical_measurements(before, after, old_sources, new_sources):
    """Match literal values under their original OCR source address, including multiplicity."""
    def records(dataset, sources):
        names = {e["id"]: e["name"] for e in dataset["entities"]}
        result = Counter()
        for row in dataset["observations"]:
            source = sources[str(row["s"])]
            box = source.get("bbox")
            location = ((source["page"], tuple(round(float(v), 3) for v in box))
                        if isinstance(box, list) and len(box) == 4 else source["source_ref"])
            result[(*measurement_key(row, names), location)] += 1
        return result
    old, new = records(before, old_sources), records(after, new_sources)
    return {"retained": sum((old & new).values()), "old_only": sum((old - new).values()),
            "new_only": sum((new - old).values()),
            "old_only_records": [list(key) + [count] for key, count in (old - new).items()],
            "new_only_records": [list(key) + [count] for key, count in (new - old).items()],
            "scope": "Same name, endpoint, literal value, normalized unit and physical PDF page/bbox (0.001pt), with original source_ref fallback. Micro-sign typography is normalized for comparison only. Added caption/header rows may renumber R/C references. Experiment associations require separate review."}


def unresolved_literal_candidates(dataset):
    """Diagnostics only: exact string partitions never write entity ownership."""
    result = []
    for entity in dataset.get("entities", []):
        for chain, whole, constant in [("VH", "H_CHAIN", "CH"), ("VL", "L_CHAIN", "CL")]:
            slot = entity["sequences"][whole]
            if entity["sequences"][chain]["status"] == "supported" or slot["status"] != "supported":
                continue
            seen = set()
            for variable in dataset.get("sequences", []):
                if variable["region"] != chain or variable.get("seq_id_scope") == "full_chain":
                    continue
                for tail in dataset.get("sequences", []):
                    key = (variable.get("seq"), tail.get("seq"))
                    if tail["region"] != constant or key in seen or variable["value"] + tail["value"] != slot["value"]:
                        continue
                    seen.add(key)
                    result.append({"name": entity["name"], "chain": chain,
                        "full_chain_seq_ids": slot.get("seq_ids", [slot.get("seq_id")]),
                        "variable_candidate_seq_id": variable.get("seq"), "constant_seq_id": tail.get("seq"),
                        "variable_length": len(variable["value"]), "constant_length": len(tail["value"]),
                        "refs": sorted(set(slot.get("mapping_refs", []) + slot.get("definition_refs", []) + variable["refs"] + tail["refs"])),
                        "status": "literal_candidate_only_not_an_accepted_dataset_mapping"})
    return result


def update(run, snapshot):
    context = read(run / "comparison_context.json")
    old_dir = Path(context["baseline_full"])
    old = read(old_dir / "dataset.json", {})
    reference = read(context["full_pdf_model_reference"], {})
    report = read(run / "run_summary.json", {})
    native = run / "results/49-Sructure/dataagent2"
    progress = read(native / "progress.json", {})
    new_report = read(native / "report.json", {})
    final = read(native / "dataset.json")
    first = read(native / "dataset.first_pass.json")
    draft = read(native / "dataset.partial.json")
    checkpoint = read(native / "dataset.checkpoint.json")
    root_log = Path(str(run) + ".log")
    try:
        with root_log.open('rb') as stream:
            stream.seek(max(0, root_log.stat().st_size - 90000))
            log = stream.read().decode('utf-8', errors='replace')
    except OSError:
        log = ''
    orientation = re.findall(r"orientation-page (\d+)/(\d+)", log)
    events = re.findall(r'^sda-dataagent2-progress (\{.*\})$', log, re.M)
    last_event = json.loads(events[-1]) if events else {}
    stage = progress.get("phase") if progress.get("phase") == "finished" else last_event.get("phase", progress.get("phase"))
    if stage == "finished" and report.get("status") == "running":
        stage = "export"
    page_ocr = run / "structured/page_ocr/pages.jsonl"
    table_ocr = run / "structured/table_ocr/tables.jsonl"
    def line_count(path):
        try:
            with path.open('rb') as stream:
                return sum(bool(line.strip()) for line in stream)
        except OSError:
            return 0
    counts = {"page_ocr": len(list((page_ocr.parent / "pages").glob("page-*.json"))),
              "table_ocr": len(list(table_ocr.parent.glob("p*-r*.json")))}
    counts["page_ocr"] = max(counts["page_ocr"], line_count(page_ocr))
    counts["table_ocr"] = max(counts["table_ocr"], line_count(table_ocr))
    if not stage:
        active = read(native.parent / "phase_timings.json", {}).get("active", [])
        stage = (active[-1]["stage"] if active else "template" if (native.parent / "shared_seed").exists() else
                 "ocr" if page_ocr.parent.exists() else "layout" if (run / "structured/layout/49-Sructure_manifest.json").exists() else "orientation_layout")
    action = read(native / "action_agent.json", {})
    if stage == "questions" and report.get("status") == "running" and action.get("status") == "validated":
        applied = [a["id"] for a in read(native / "answers.json", []) if a.get("status") == "applied"]
        replayed = [a["id"] for a in read(native / "checkpoint_progress.json", {}).get("answer_replay", [])]
        if applied != replayed:
            # Official replay reconstructs a review checkpoint from saved, validated
            # responses. It makes no model call and never replaces the live dataset.
            sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
            from dataagent2.replay import replay
            try:
                replay(native)
                checkpoint = read(native / "dataset.checkpoint.json")
            except (OSError, ValueError, KeyError) as error:
                save(run / "checkpoint_refresh_error.json", {"error": str(error)})
    prior = read(run / "comparison.json", {})
    value = {"updated_at_beijing": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        "status": report.get("status", "running"), "stage": stage, "run_dir": str(run),
        "model": context["model"], "effort": context["effort"],
        "action_effort": context.get("action_effort", context["effort"]), "ocr_counts": counts,
        "orientation": [int(v) for v in orientation[-1]] if orientation else prior.get("orientation"),
        "reader_progress": {key: progress.get("coverage", progress).get(key) for key in ("completed_chunks", "total_chunks", "failed_chunks", "skipped_chunks")},
        "phase_progress": last_event,
        "action": action, "old_summary": old.get("summary", {}),
        "draft_summary": (draft or {}).get("summary"),
        "first_pass_summary": (first or {}).get("summary") if action.get("status") in {"validated", "not_needed"} else None,
        "new_summary": (final or {}).get("summary") if new_report.get("benchmark_complete") else None,
        "checkpoint_summary": (checkpoint or {}).get("summary"),
        "benchmark_complete": new_report.get("benchmark_complete"),
        "reader_usage": read(native / "token_usage.json", {}),
        "old_reader_usage": read(old_dir / "report.json", {}).get("new_model_usage", {}),
        "old_known_mapping_checks": chain_checks(old),
        "known_mapping_checks": chain_checks(final if new_report.get("benchmark_complete") else checkpoint or first or draft) if final or first or draft else None,
        "new_quality": read(native / "quality_audit.json") if new_report.get("benchmark_complete") else None,
        "old_quality": read(old_dir / "quality_audit.json"),
        "measured_pair_coverage": {"old": measured_pair_coverage(old), "draft": measured_pair_coverage(draft),
                                  "after_action": measured_pair_coverage(first) if action.get("status") == "validated" else None,
                                  "checkpoint": measured_pair_coverage(checkpoint),
                                  "final": measured_pair_coverage(final) if new_report.get("benchmark_complete") else None},
        "runtime_fixes": context.get("runtime_fixes", []),
        "input_contract": read(run / "input_contract_audit.json"),
        "old_model_reference_comparison": compare_model_reference(old, reference),
        "error": report.get("error") or progress.get("action_agent", {}).get("error"),
        "methodology": ["Fresh PDF/OCR plus new extraction pipeline; this is not an isolated ActionAgent ablation.",
            "AB-1 through AB-14 SEQ IDs are a previously inspected table subset, not full-document human ground truth.",
            "The previous full-PDF model output is a non-human reference; counts alone do not establish accuracy.",
            "Previous full run used the same model and Reader effort with no question-count limit; this run uses medium effort for ActionAgent."]}
    if final and new_report.get("benchmark_complete"):
        value["chain_comparison"] = compare_chains(old, final)
        value["measurement_comparison"] = compare_measurements(old, final)
        value["physical_measurement_comparison"] = compare_physical_measurements(old, final,
            read(old_dir / "sources.json"), read(native / "sources.json"))
        value["model_reference_comparison"] = compare_model_reference(final, reference)
        value["unresolved_literal_candidates"] = unresolved_literal_candidates(final)
        save(run / "unresolved_literal_candidates.json", {"scope": "Exact string partitions only. Ownership and domain evidence still require review; no dataset changes made.",
            "candidates": value["unresolved_literal_candidates"]})
    if draft and first and action.get("status") == "validated":
        value["action_chain_comparison"] = compare_chains(draft, first)
        value["action_measurement_comparison"] = compare_measurements(draft, first)
        response = read(native / "action_agent.response.json", {})
        value["action_decision_counts"] = dict(Counter(decision["outcome"] for decision in response.get("action_decisions", [])
                                                       for _ in decision["action_ids"]))
    case = read(run / "ab2b_verification_case.json")
    if case:
        checked_dataset = final if new_report.get("benchmark_complete") else checkpoint or first if action.get("status") == "validated" else None
        matches = entities_by_name(checked_dataset or {}).get(name_key("AB-2b"), [])
        entity = matches[0] if len(matches) == 1 else None
        slot = (entity or {}).get("sequences", {}).get("VH", {})
        value["ab2b_case"] = {"exact_concatenation": case["exact_concatenation"], "lengths": case["lengths"],
            "old_unpaired_measurements": case["old_measurements_missing_vh"], "new_vh_status": slot.get("status"),
            "new_vh_matches_checked_literal": slot.get("status") == "supported" and slot.get("value") == case["variable_value"],
            "evaluation_stage": "final" if new_report.get("benchmark_complete") else "after_action" if checked_dataset else "waiting",
            "evidence": case["evidence"], "scope": case["scope"]}
    save(run / "comparison.json", value)
    save(snapshot / "rerun-comparison.json", value)
    return value


HTML = r'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>49-Sructure · 从头重跑与效果对照</title><link rel="icon" href="data:,"><style>
:root{font:15px/1.8 system-ui,"PingFang SC","Microsoft YaHei",sans-serif;color:#23332f;background:#f5f6f2;--line:#dce2dc;--muted:#6c7973;--green:#176b52}*{box-sizing:border-box}body{margin:0}main{max-width:1160px;padding:28px 24px 60px;margin:auto}a{color:var(--green)}h1{font-size:29px;line-height:1.4}h2{font-size:20px}section{border:1px solid var(--line);border-radius:8px;background:white;padding:22px;margin:20px 0}.muted{font-size:12px;color:var(--muted)}.status{padding:14px 18px;background:#edf6ef;border-left:3px solid var(--green);font-size:14px}.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}.grid div{padding:14px;background:#edf5ef;border-radius:6px}.grid strong{font-size:24px;display:block}.grid span{font-size:12px;color:var(--muted)}.scroll{overflow:auto;max-height:470px}table{width:100%;border-collapse:collapse;text-align:left;font-size:13px}th,td{padding:10px 13px;border-bottom:1px solid var(--line)}th{background:#edf3eb;position:sticky;top:0;white-space:nowrap}.good{color:#176b52}.bad{color:#a15f2c}code{font-size:11px;overflow-wrap:anywhere}pre{font-size:11px;background:#f1f3ed;padding:15px;overflow:auto;max-height:350px}details{margin-top:16px}summary{cursor:pointer;color:var(--green)}@media(max-width:650px){main{padding:20px 16px}.grid{grid-template-columns:1fr 1fr}section{padding:17px}h1{font-size:24px}}
</style></head><body><main><a href="index.html">← 返回整体流程</a><h1>49-Sructure：从原始 PDF 重跑，再看哪些信息变好了</h1><p><a href="../49-Sructure-action-final-20261101/index.html">查看本轮真实输入、完整输出及调用快照 →</a> · <a href="../49-Sructure-action-final-20261101/artifacts/final_dataset.json" download>下载最终原生 dataset</a></p><p class="muted">原 PDF → 新定位与截图 → 新 OCR → 新 Template → 全量 Seed / 并行 Reader → LLM ActionAgent → 缺口补全</p><div class="status" id="status">正在读取状态……</div><p class="muted" id="updated"></p><section><h2>本次进度</h2><div class="grid"><div><strong id="orient">—</strong><span>方向检测页数</span></div><div><strong id="ocr">—</strong><span>已保存的正文 OCR 页</span></div><div><strong id="tables">—</strong><span>已保存的表格 OCR</span></div><div><strong id="chunks">—</strong><span>Reader 完成块数</span></div></div><p class="muted">页面每 10 秒读取一次本地状态。下面的新结果只有在文件实际生成后才显示；临时结果与最终结果分开。</p></section><section><h2>本轮实际输入与已知限制</h2><p id="input-contract"></p><p id="caption-limitation" class="bad"></p></section><details><summary>查看本轮实际截图：Table 6 的标题与表格</summary><p class="muted">来源：本轮第 205 页裁图。这里展示输入截图，标题是否被 OCR 和 Template 接收仍需检查对应产物。</p><img src="rerun-data/table6-crop.png" alt="本轮 Table 6 截图，包含 Self-association Properties 标题及完整表格" style="display:block;max-width:100%;max-height:650px"></details><section><h2>完整结果对照</h2><div class="scroll"><table><thead><tr><th>指标</th><th>旧版完整结果</th><th>本次 Reader 临时结果</th><th>本次 Action 后首轮结果</th><th>已补全的检查点</th><th>本次最终结果</th></tr></thead><tbody id="metrics"></tbody></table></div><p class="muted">拒绝条目的口径不同：本轮包括 1,085 条索引提及及 326 条被整合的局部缺口，不能与旧程序拒绝数直接比较。数量变化不等于准确率变化。此次同时更新了截图、OCR、Template 和整合流程，不能把差异全部归因于 ActionAgent。</p></section><section><h2>预先核对的 28 条 VH/VL 编号关系</h2><p id="subset"></p><div class="scroll"><table><thead><tr><th>抗体</th><th>链</th><th>表格支持的编号</th><th>旧结果</th><th>新结果</th><th>新序列状态</th></tr></thead><tbody id="mapping"></tbody></table></div><p class="muted">这里只检查 AB-1～AB-14 的编号关系；不代表已人工核对全部残基或全篇准确率。</p></section><section><h2>重点检查：AB-2b 的 8 条测量</h2><p>旧结果已找到 AB-2b 的完整重链 SEQ ID 106，但这 8 条测量缺少已支持的 VH。</p><p><strong>已提取字符的逐一比对：</strong>重链 106（452 aa）＝ VH 5（122 aa）＋常量区 85（330 aa）。原文第 84 页将“AB-[]b”关联到常量区 85，第 99 页将 AB-2b 关联到重链 106；Table 1 将序列 5 标为 VH。</p><p id="ab2b">等待本轮整合结果。</p><p class="muted">这是针对一个缺口的来源核对，字符依赖 OCR；不代表已人工转录整份 PDF。</p><details><summary>查看对应原文及单元格引用</summary><pre id="ab2b-evidence"></pre></details></section><section><h2>已有结果保留情况与运行成本</h2><p id="actioneffect">等待 ActionAgent 实际整合结果。</p><p id="overlap">等待本次最终结果。</p><p id="quality"></p><p id="reference"></p><p id="usage"></p><details><summary>查看原始对照 JSON、范围和状态</summary><pre id="json"></pre></details><p class="muted">旧完整 PDF 模型提取结果作为非人工参考保留。序列变化和实验归属差异需要继续核对原文。</p></section><section id="candidate-section" hidden><h2>仍未确认的字面候选</h2><p class="muted">这些等式只说明残基字符串完全相同，尚未获得模型的关系确认，保持为待复核项，没有写入 dataset。</p><div class="scroll"><table><thead><tr><th>实体</th><th>链</th><th>完整链与已披露域的字面等式</th><th>长度</th></tr></thead><tbody id="candidate-rows"></tbody></table></div></section><p class="muted">本次目录：<code id="path"></code></p></main><script>
const $=id=>document.getElementById(id),n=x=>typeof x==='number'?x.toLocaleString():'—';
const stageNames={finished:'结束（含待复核项）',orientation_layout:'方向与版面',layout:'版面定位',ocr:'全文及表格 OCR',reader:'并行 Reader',action:'LLM ActionAgent 整合',questions:'定向缺口补全',finished:'结果导出',export:'结果导出',sequence_ocr_refresh:'序列 OCR 校验',template:'Template 整理表格草稿',dataagent2_input:'组织 Reader 输入'};
const fields=[['entities','实体'],['variable_chains','已支持 VH/VL 槽位'],['complete_pairs','完整 VH/VL 配对'],['sequence_definitions','序列定义'],['observations','测量记录'],['conflicts','冲突'],['rejected_facts','拒绝条目（口径不同）']];
function cell(row,text,cls){const c=document.createElement('td');c.textContent=text;if(cls)c.className=cls;row.append(c)}
async function refresh(){try{const r=await fetch('rerun-comparison.json?t='+Date.now(),{cache:'no-store'});if(!r.ok)throw Error('状态文件尚未就绪');const d=await r.json();$('status').textContent=(d.status==='running'?'正在运行：':d.benchmark_complete?'模型流程已完整结束：':'当前状态：')+(stageNames[d.stage]||d.stage)+' · '+d.model+' / '+(d.stage==='action'?(d.action_effort||d.effort):d.effort)+(d.error?' · '+d.error:'');$('updated').textContent='更新时间：'+d.updated_at_beijing;$('orient').textContent=d.orientation?d.orientation.join(' / '):'已完成或等待';$('ocr').textContent=n(d.ocr_counts.page_ocr);$('tables').textContent=n(d.ocr_counts.table_ocr);$('chunks').textContent=n(d.reader_progress.completed_chunks)+' / '+n(d.reader_progress.total_chunks);$('path').textContent=d.run_dir;$('metrics').replaceChildren();for(const[k,label]of fields){const tr=document.createElement('tr');cell(tr,label);for(const s of [d.old_summary,d.draft_summary,d.first_pass_summary,d.checkpoint_summary,d.new_summary])cell(tr,n(s?.[k]));$('metrics').append(tr)}const old=d.old_known_mapping_checks,cur=d.known_mapping_checks||[];$('subset').textContent=d.known_mapping_checks?'编号对应正确：旧结果 '+old.filter(x=>x.mapping_correct).length+'/28；当前已生成结果 '+cur.filter(x=>x.mapping_correct).length+'/28。':'旧结果 '+old.filter(x=>x.mapping_correct).length+'/28；本次尚未生成可核对的 dataset。';$('mapping').replaceChildren();cur.forEach((x,i)=>{const tr=document.createElement('tr');cell(tr,x.name);cell(tr,x.chain);cell(tr,'SEQ ID '+x.expected_seq_id);cell(tr,old[i].mapping_correct?'对应':'未确认',old[i].mapping_correct?'good':'bad');cell(tr,x.mapping_correct?'对应 '+x.actual_seq_ids.join(','):x.actual_seq_ids.join(',')||'未确认',x.mapping_correct?'good':'bad');cell(tr,x.sequence_supported?'已支持 · '+x.sequence_length+' aa':x.status);$('mapping').append(tr)});if(d.chain_comparison){const c=d.chain_comparison.counts,m=d.measurement_comparison;$('overlap').textContent='共同且唯一名称下：新增支持 '+(c.newly_supported||0)+' 个域；失去支持 '+(c.lost_support||0)+' 个域；序列内容变化 '+(c.different_sequence||0)+' 个域。测量粗匹配重合 '+m.common_count+' 条、旧独有 '+m.old_only_count+' 条、新独有 '+m.new_only_count+' 条。';}if(d.action_chain_comparison){const ac=d.action_chain_comparison.counts,cov=d.measured_pair_coverage;$('actioneffect').textContent='只看本轮整合前后：新增支持 '+(ac.newly_supported||0)+' 个 VH/VL 域，失去支持 '+(ac.lost_support||0)+' 个域，序列变化 '+(ac.different_sequence||0)+' 个域；可关联完整配对的测量从 '+cov.draft.with_complete_pair+'/'+cov.draft.measurements+' 变为 '+cov.after_action.with_complete_pair+'/'+cov.after_action.measurements+'。';}if(d.measured_pair_coverage.checkpoint){const c=d.measured_pair_coverage.checkpoint;$('actioneffect').textContent+=' 已完成补全的检查点：'+c.with_complete_pair+'/'+c.measurements+' 条测量已配齐 VH/VL。'+(d.benchmark_complete?'缺口检查已结束。':'其余问答仍在运行。');}const q=d.new_quality,oq=d.old_quality;if(q)$('quality').textContent='有完整 VH/VL 的测量记录：旧 '+oq.measurements_with_complete_variable_pair+'/'+oq.measurements+'，新 '+q.measurements_with_complete_variable_pair+'/'+q.measurements+'；新结果字符来源校验错误 '+q.literal_errors.length+' 条。该校验不替代 PDF 原图复核。';const input=d.input_contract;if(input){$('input-contract').textContent='Template 实收 '+input.template.tables+' 张表、'+input.template.titles+' 个非空标题，未传 PDF 正文；62 个 Reader 都使用同一份完整业务 seed。';$('caption-limitation').textContent=input.observed_caption_ocr_issue?'仍有 OCR 限制：Table 1 的 VH 被读成 Ví，初始 Template 因而未生成 VH 关系；本轮由 Reader 和 ActionAgent 补回，不能把后续提升当成 Template 单独准确率。':'';}const ref=d.model_reference_comparison,oref=d.old_model_reference_comparison;$('reference').textContent='与完整 PDF 模型参考的显式 VH/VL 编号对应一致：旧 '+oref.variable_mapping_agreements+'/'+oref.variable_mapping_total+(ref?'，新 '+ref.variable_mapping_agreements+'/'+ref.variable_mapping_total:'；本轮尚待结果')+'。参考不是人工标准；名称与范围不一致的项目不会强行匹配。';$('usage').textContent='Reader / 整合 / 问答的累计实际用量：'+n(d.reader_usage.input_tokens)+' input tokens，'+n(d.reader_usage.output_tokens)+' output tokens。不含本轮共享 Template 阶段；完整汇总在运行报告中。'+(d.reader_usage.unknown_requests?' 有 '+d.reader_usage.unknown_requests+' 次调用缺少用量统计，当前合计为已知下限。':'');if(d.ab2b_case){const a=d.ab2b_case;$('ab2b').textContent=a.evaluation_stage==='waiting'?'本轮还在等待 ActionAgent 返回，尚不能判定是否补上。':(a.evaluation_stage==='final'?'最终结果：':'Action 整合后：')+(a.new_vh_matches_checked_literal?'VH 字符与已核对的 122 aa 候选一致；仍需结合引用判断所有权证据。':'VH 当前状态为 '+a.new_vh_status+'，尚未确认补上。');$('ab2b-evidence').textContent=a.evidence.map(e=>'PDF '+e.page+' 页 · '+e.source_ref+'\n'+e.text).join('\n\n');}const candidates=d.unresolved_literal_candidates||[];$('candidate-section').hidden=!candidates.length;$('candidate-rows').replaceChildren();for(const c of candidates){const tr=document.createElement('tr');cell(tr,c.name);cell(tr,c.chain);cell(tr,'完整 SEQ '+c.full_chain_seq_ids.join('/')+' = '+c.chain+' SEQ '+(c.variable_candidate_seq_id??'未编号')+' + 恒定区 SEQ '+(c.constant_seq_id??'未编号'));cell(tr,c.variable_length+' + '+c.constant_length+' aa');$('candidate-rows').append(tr);}$('json').textContent=JSON.stringify(d,null,2)}catch(e){$('status').textContent=e.message}}
refresh();setInterval(refresh,10000);
</script></body></html>'''


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--watch", action="store_true")
    args = parser.parse_args()
    (args.snapshot / "rerun-comparison.html").write_text(HTML)
    previous = None
    while True:
        value = update(args.run, args.snapshot)
        marker = (value["status"], value["stage"], value["orientation"], value["reader_progress"])
        if marker != previous:
            print(json.dumps({"status": value["status"], "stage": value["stage"], "orientation": value["orientation"],
                              "reader_progress": value["reader_progress"]}, ensure_ascii=False), flush=True)
            previous = marker
        if not args.watch or value["status"] in {"complete", "partial", "failed"}:
            break
        time.sleep(10)
