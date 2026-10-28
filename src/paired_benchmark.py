"""Reproduce a small VH/VL extraction comparison from saved public records.

This scores paired variable domains, never independently pooled heavy/light
chains. Public application/grant records retain their publication identities.
No benchmark labels or competitor records enter dataagent2's extraction input.
"""

from __future__ import annotations

import argparse
from collections import Counter
import csv
import gzip
import hashlib
import json
from pathlib import Path
import re
import subprocess


AA = set("ACDEFGHIKLMNPQRSTVWY")
QUESTION_BUDGET = 8
SAMPLES = [
    ("US20150183856A1", "US9598483B2", "DLL4", 66),
    ("US20100221263A1", "US7988970B2", "Sclerostin", 17),
    ("US20110262448A1", "US8268970B2", "Mesothelin", 25),
]
# Explicit publication aliases, not fuzzy name matching. Mutants/humanized
# constructs remain separate. The raw identifiers are retained in every row.
ALIASES = {
    "US20100221263A1": {"antibody86": "86", "antibody88": "88", "antibody89": "89"},
    # Retained for the source-audit tests and exploratory comparison.
    "WO2016073890A1": {"ebi029018020": "ebi029"},
    "WO2018068182A1": {
        "6f3murine": "6f3", "10b10murine": "10b10",
        "6f3humanizedfulllength": "6f3hu",
        "6f3humanizedvariable": "6f3hu",
        "10b10humanizedfulllength": "10b10hu",
        "10b10humanizedvariable": "10b10hu",
    },
}


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sequence(value):
    """Whitespace only. Never fix confusables, residues, case, or boundaries."""
    return re.sub(r"\s+", "", value or "")


def identity(doc, value):
    name = re.sub(r"[^a-z0-9]", "", value.casefold())
    return ALIASES.get(doc, {}).get(name, name)


def variable_pair(record):
    h, l = sequence(record.get("vh")), sequence(record.get("vl"))
    return bool(h and l and not (set(h + l) - AA)
                and 70 <= len(h) <= 180 and 65 <= len(l) <= 160)


def identity_keys(doc, record):
    return {identity(doc, name) for name in [record["name"], *record.get("aliases", [])] if name}


def compare(doc, gold, predictions):
    """Report identity-specific exact recovery, with ambiguity kept visible.

    The annotations are not an exhaustive inventory of the patent. Therefore
    unannotated predictions are unscored, not automatically false positives.
    A pair must exist in ONE predicted record; cross-record assembly is banned.
    """
    details = []
    all_variable_pairs = {(sequence(p["vh"]), sequence(p["vl"])) for p in predictions if variable_pair(p)}
    for g in gold:
        h, l = sequence(g["vh_sequence_aa"]), sequence(g["vl_sequence_aa"])
        eligible = bool(h and l and not (set(h + l) - AA))
        candidates = [p for p in predictions
                      if identity(doc, g["Antibody_Name"]) in identity_keys(doc, p)]
        pairs = {(sequence(p["vh"]), sequence(p["vl"]))
                 for p in candidates if variable_pair(p)}
        exact_exists = eligible and (h, l) in pairs
        details.append({
            "antibody": g["Antibody_Name"], "eligible_pair": eligible,
            "gold_invalid_characters": sorted(set(h + l) - AA),
            "candidate_count": len(candidates), "distinct_variable_pairs": len(pairs),
            "vh_exact_exists": bool(h and not (set(h) - AA)
                                    and any(sequence(p["vh"]) == h for p in candidates)),
            "vl_exact_exists": bool(l and not (set(l) - AA)
                                    and any(sequence(p["vl"]) == l for p in candidates)),
            "paired_exact_exists": exact_exists,
            "paired_exact_unambiguous": exact_exists and len(pairs) == 1,
            "sequence_pair_exists_ignoring_identity": eligible and (h, l) in all_variable_pairs,
            "candidate_records": [{
                "name": p["name"], "record_id": p.get("record_id"),
                "vh_length": len(sequence(p["vh"])), "vl_length": len(sequence(p["vl"])),
                "vh_exact": bool(h and sequence(p["vh"]) == h),
                "vl_exact": bool(l and sequence(p["vl"]) == l),
                "variable_pair": variable_pair(p),
                "vh_boundary_containment": bool(h and sequence(p["vh"]) != h
                                               and (h in sequence(p["vh"]) or sequence(p["vh"]) and sequence(p["vh"]) in h)),
                "vl_boundary_containment": bool(l and sequence(p["vl"]) != l
                                               and (l in sequence(p["vl"]) or sequence(p["vl"]) and sequence(p["vl"]) in l)),
            } for p in candidates],
        })
    eligible = sum(d["eligible_pair"] for d in details)
    hit = sum(d["paired_exact_unambiguous"] for d in details)
    names = {identity(doc, g["Antibody_Name"]) for g in gold}
    in_scope_pairs = {(name, sequence(p["vh"]), sequence(p["vl"]))
                      for p in predictions if variable_pair(p)
                      for name in identity_keys(doc, p) & names}
    expected = {(identity(doc, g["Antibody_Name"]), sequence(g["vh_sequence_aa"]), sequence(g["vl_sequence_aa"]))
                for g in gold if g["vh_sequence_aa"] and g["vl_sequence_aa"]
                and not (set(sequence(g["vh_sequence_aa"]) + sequence(g["vl_sequence_aa"])) - AA)}
    return {
        "gold_records": len(gold), "eligible_gold_pairs": eligible,
        "quarantined_gold_pairs": len(gold) - eligible,
        "prediction_records": len(predictions),
        "predicted_variable_pair_records": sum(variable_pair(p) for p in predictions),
        "paired_exact_exists": sum(d["paired_exact_exists"] for d in details),
        "paired_exact_unambiguous": hit,
        "paired_exact_unambiguous_recovery": hit / eligible if eligible else None,
        "sequence_pair_recovered_ignoring_identity": sum(d["sequence_pair_exists_ignoring_identity"] for d in details),
        "vh_exact_recovered": sum(d["vh_exact_exists"] for d in details),
        "vl_exact_recovered": sum(d["vl_exact_exists"] for d in details),
        "annotated_identities_with_variable_pair": sum(d["distinct_variable_pairs"] > 0 for d in details),
        "distinct_predicted_pairs_in_annotated_scope": len(in_scope_pairs),
        "exact_pair_precision_in_annotated_scope": len(in_scope_pairs & expected) / len(in_scope_pairs) if in_scope_pairs else None,
        "unannotated_prediction_records_unscored": sum(not identity_keys(doc, p) & names for p in predictions),
        "details": details,
    }


def prepare(root, output):
    sources = output / "sources"
    upstream = root / "vendor/ABDataBench"
    gold_path = output / "gold.source_verified.json"
    gold = read(gold_path) if gold_path.is_file() else {}
    manifest_path = sources / "poisk_manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError("Save the current official POISK data/manifest.json first")
    manifest = read(manifest_path)
    fields = manifest["fields"]
    with gzip.open(sources / "ABPairDB_antibodies.csv.gz", "rt") as stream:
        antibodies = list(csv.DictReader(stream))
    with gzip.open(sources / "ABPairDB_experiment.csv.gz", "rt") as stream:
        experiments = list(csv.DictReader(stream))
    abcurator = [record for part in sorted(sources.glob("ABDataCorpus_antibodies_part_*.json"))
                for record in read(part)]
    selected = []
    for doc, us, target, chunk in SAMPLES:
        us_number = re.sub(r"[AB]\d$", "", us)
        with gzip.open(sources / f"poisk_details_{chunk}.json.gz", "rt") as stream:
            detail = json.load(stream)
        poisk = [dict(zip(fields, row)) for row in detail["rows"]
                 if row[fields.index("patent_id")] == us_number[2:]]
        abpair = [r for r in antibodies if r["patent_id"] == us_number]
        exps = [r for r in experiments if r["patent_id"] == us_number]
        abc = [r for r in abcurator if r["source_paper"] == doc]
        directory = output / "baselines" / doc
        write(directory / "POISK.raw.json", poisk)
        write(directory / "ABPairDB.antibodies.raw.json", abpair)
        write(directory / "ABPairDB.experiments.raw.json", exps)
        write(directory / "ABCurator.raw.json", abc)
        write(directory / "ABCurator.pairs.json", [{
            "record_id": f"{doc}:{index}", "name": r["Antibody_Name"],
            "vh": r.get("vh_sequence_aa", ""), "vl": r.get("vl_sequence_aa", ""),
            "target": r.get("Target_Name", ""), "source_publication": doc,
            "field_sources": r.get("field_sources", {}),
        } for index, r in enumerate(abc)])
        write(directory / "POISK.pairs.json", [{
            "record_id": r["unique_record_id"], "name": r["antibody_identifier"],
            "vh": r["vh.sequence_from_patent"], "vl": r["vl.sequence_from_patent"],
            "target": r["target.protein_name"], "source_publication": us,
        } for r in poisk])
        write(directory / "ABPairDB.pairs.json", [{
            "record_id": r[""], "name": r["name"],
            "vh": r["heavy_sequence"], "vl": r["light_sequence"],
            "format": r["ab_type"], "source_publication": us,
        } for r in abpair])
        selected.append({
            "document_id": doc, "target": target,
            "gold_antibodies": len(gold.get(doc, {}).get("antibodies", [])),
            "gold_status": "assistant_source_audit_pending_human_review" if doc in gold else "pending_source_audit",
            "public_reference_publication": us,
            "publication_relation": "same US application, grant version; input publication differs",
            "POISK_records": len(poisk), "ABPairDB_records": len(abpair),
            "ABCurator_records": len(abc),
            "ABPairDB_experiments": len(exps),
            "input_pdf": str((sources / f"{doc}.pdf").resolve()),
            "family_evidence": f"https://patents.google.com/patent/{doc}/en",
            "ABCurator_predictions_status": "published ABDataCorpus extraction; not the README model leaderboard predictions",
        })
    write(output / "ground_truth.subset.json", {doc: gold[doc] for doc, *_ in SAMPLES if doc in gold})
    tracked = [p for p in sources.iterdir() if p.is_file()]
    tracked += [p for p in (output / "gold.source_verified.json", output / "build_source_reference.py",
                            output / "application_version_evidence.json") if p.is_file()]
    code = [root / "src/paired_benchmark.py", root / "src/paired_benchmark_replay.py",
            *sorted((root / "src/dataagent2").glob("*.py"))]
    commit = subprocess.check_output(["git", "-C", str(upstream), "rev-parse", "HEAD"], text=True).strip()
    write(output / "selection.json", {
        "date": "2026-10-28 Asia/Shanghai", "samples": selected,
        "ABDataBench_commit": commit, "POISK_dataset": manifest["dataset"],
        "ABCurator_corpus_commit": read(sources / "ABDataCorpus_repo_metadata.json").get("sha"),
        "scope": "dataagent2 and published ABCurator predictions on A1; POISK/ABPairDB on the same application grant. Verify shared sequence facts before interpreting differences.",
        "primary_metric": "identity-specific, same-record, unambiguous exact VH+VL recovery",
        "question_budget_per_document": QUESTION_BUDGET,
        "quality_replay": "Unchanged local pipeline, all Reader chunks and first 8 questions; exact saved request/response hashes; zero new model calls. This matches the existing standalone CLI question default. Extended physical collection logs/costs retained.",
        "sequence_policy": "Only whitespace removed; no residue fixes, case changes, or boundary trimming",
        "gold_policy": "Reference pairs read from original source sequences and explicit clone/chain mappings, not competitor predictions. Noncanonical pairs quarantined. No full-patent precision claimed.",
        "sha256": {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in tracked + code},
    })


def evaluate(root, output):
    ground_truth = read(output / "ground_truth.subset.json")
    scores = {}
    for doc, *_ in SAMPLES:
        rows = {}
        for name in ("POISK", "ABPairDB", "ABCurator"):
            pairs = read(output / "baselines" / doc / f"{name}.pairs.json")
            rows[name] = {"status": "same_publication_reference" if name == "ABCurator" else "same_application_grant_reference"}
            if doc in ground_truth:
                rows[name].update(compare(doc, ground_truth[doc]["antibodies"], pairs))
            else:
                rows[name].update(gold_status="pending_source_audit", prediction_records=len(pairs), score=None)
        result = output / "dataagent2" / doc / "results" / doc / "dataagent2_bounded8"
        native = result / "dataset.json"
        report = read(result / "report.json") if (result / "report.json").is_file() else {}
        if native.is_file() and report.get("benchmark_complete") and doc in ground_truth:
            data = read(native)
            pairs = [{"record_id": e["id"], "name": e["name"], "aliases": e.get("names", []),
                      "vh": e["sequences"]["VH"].get("value") if e["sequences"]["VH"]["status"] == "supported" else None,
                      "vl": e["sequences"]["VL"].get("value") if e["sequences"]["VL"]["status"] == "supported" else None,
                      "source_publication": doc,
                      "vh_status": e["sequences"]["VH"]["status"],
                      "vl_status": e["sequences"]["VL"]["status"],
                      "source_conflict": any(e["sequences"][c].get("source_conflict") for c in ("VH", "VL"))}
                     for e in data["entities"]]
            write(output / "predictions" / doc / "dataagent2.pairs.json", pairs)
            rows["dataagent2"] = {"status": "complete_extraction", "native_status": data["status"],
                                  "question_budget": QUESTION_BUDGET, "cache_only_assembly": True,
                                  "native_conflicts": len(data.get("conflicts", [])),
                                  **compare(doc, ground_truth[doc]["antibodies"], pairs)}
        else:
            rows["dataagent2"] = {"status": "not_complete", "score": None,
                                  "native_dataset_exists": native.is_file(),
                                  "reader_coverage": report.get("coverage")}
        scores[doc] = rows
    write(output / "paired_scores.json", scores)
    write_report(output, ground_truth, scores)
    return scores


def write_report(output, ground_truth, scores):
    lines = [
        "本地 dataagent2 与 POISK、ABCurator、ABPairDB 的 VH/VL 配对抽取试评",
        "日期：2026-10-28（Asia/Shanghai）", "",
        "口径：同一个抗体的一条预测记录必须同时给出正确 VH 和 VL。只移除空白，不修复残基，",
        "不自动裁剪序列边界，不把不同预测记录的正确单链重新拼成一对。原始输出全部保留。",
        "表中为已核验参考序列的无歧义精确成对恢复数，不是整个数据库的准确率。", "",
        "本地统一预算：完整 Reader + 每篇最多 8 个定向补全问题（现有独立 CLI 默认）。",
        "使用完全匹配原始请求哈希的已保存模型响应运行原组装流程；重放不调用模型，也不改序列。",
        "此前前两篇额外问答和第三篇采集过程均保留；额外采集开销不作为 8 问预算的成本。", "",
        "专利（靶点） | dataagent2 | POISK | ABCurator | ABPairDB",
    ]
    for doc, _, target, _ in SAMPLES:
        entries = []
        for name in ("dataagent2", "POISK", "ABCurator", "ABPairDB"):
            row = scores[doc][name]
            entries.append(f"{row['paired_exact_unambiguous']}/{row['eligible_gold_pairs']}"
                           if "paired_exact_unambiguous" in row else "未完成，不计分")
        lines.append(f"{doc}（{target}） | " + " | ".join(entries))
    totals = []
    for name in ("dataagent2", "POISK", "ABCurator", "ABPairDB"):
        rows = [scores[doc][name] for doc, *_ in SAMPLES]
        totals.append(f"{sum(r['paired_exact_unambiguous'] for r in rows)}/{sum(r['eligible_gold_pairs'] for r in rows)}"
                      if all("paired_exact_unambiguous" in r for r in rows) else "未全部完成，不汇总")
    lines.append("合计 | " + " | ".join(totals))
    for name in ("ABCurator", "dataagent2"):
        rows = [scores[doc][name] for doc, *_ in SAMPLES]
        if all("paired_exact_unambiguous" in r for r in rows):
            covered = sum(r["annotated_identities_with_variable_pair"] for r in rows)
            exact = sum(r["paired_exact_unambiguous"] for r in rows)
            total = sum(r["eligible_gold_pairs"] for r in rows)
            lines.append(f"{name}：参考身份中有完整 VH/VL 输出 {covered}/{total}；无歧义精确匹配 {exact}/{total}。")
    lines += ["", "选样和复用："]
    selection = read(output / "selection.json")
    for sample in selection["samples"]:
        doc = sample["document_id"]
        names = [g["Antibody_Name"] for g in ground_truth.get(doc, {}).get("antibodies", [])]
        lines += [f"{doc}：{'、'.join(names)}。授权版 {sample['public_reference_publication']}。",
                  f"  现成公开记录：POISK {sample['POISK_records']}；ABCurator {sample['ABCurator_records']}；ABPairDB {sample['ABPairDB_records']}。"]
    lines += ["", "本地已完成样本的诊断："]
    diagnostics = {}
    for doc, *_ in SAMPLES:
        if scores[doc]["dataagent2"]["status"] != "complete_extraction":
            continue
        native = output / "dataagent2" / doc / "results" / doc / "dataagent2_bounded8"
        data, report = read(native / "dataset.json"), read(native / "report.json")
        answers = read(native / "answers.json")
        errors = Counter(r["error"] for r in data["rejected_facts"])
        noncanonical = sum(n for error, n in errors.items() if "Noncanonical characters" in error)
        unknown_identity = sum(n for error, n in errors.items() if "Unknown or ambiguous item identity" in error)
        resolved = sum(bool(a.get("resolved")) for a in answers)
        root_report = read(output / "dataagent2" / doc / "run_summary.json")
        usage = root_report.get("token_usage", {}).get("totals", {})
        answer_statuses = Counter(a["status"] for a in answers)
        lines += [f"{doc}：导出 {data['summary']['variable_chains']} 条 VH/VL，{data['summary']['complete_pairs']} 对完整配对；",
                  f"  全文读取 {report['coverage']['completed_chunks']}/{report['coverage']['total_chunks']}，问答 {len(answers)} 次，实际解决 {resolved} 次。",
                  f"  拒绝候选事实：非标准序列字符 {noncanonical} 条，新实体编号/身份无法解析 {unknown_identity} 条。",
                  f"  历史物理采集耗时 {root_report.get('wall_seconds', 0) / 60:.1f} 分钟；已记录模型 token {usage.get('total_tokens', '未知')}（含额外问答，非8问预算成本）。"]
        if doc == "US20110262448A1" and (output / "budget_stop.json").is_file():
            lines.append("  原无限问答采集达到预算后主动中断，原 run_summary 保留 failed；正式8问缓存重放已完整结束。")
        if answer_statuses.get("no_evidence"):
            lines.append(f"  预算内 {answer_statuses['no_evidence']} 个问题无可用证据，未调用模型。")
        diagnostics[doc] = {"benchmark_complete": report["benchmark_complete"], "summary": data["summary"],
                            "questions_attempted": len(answers), "questions_resolved": resolved,
                            "answer_status_counts": dict(answer_statuses),
                            "unresolved_questions": report["unresolved_question_count"],
                            "rejection_counts": dict(errors), "wall_seconds": root_report.get("wall_seconds"),
                            "actual_model_usage": usage}
    write(output / "final_diagnostics.json", diagnostics)
    lines += [
        "", "解释结果时的边界：",
        "1. dataagent2 实际从原始 A1 PDF 运行完整的 OCR、共享表格分类/模板、Reader 和问答。",
        "   正式质量统计统一取完整 Reader 和原顺序前 8 个问题；以缓存重放相同流程完成，未按参考筛选问题。",
        "   原生 dataset 的 supported/conflict/missing 状态保留；处理成功不等于抽取正确。",
        "   OCR 的部分失败及接受部分结果的记录保留在原 run_summary；Reader 完整仅指所供 OCR 来源均已读完。",
        "2. ABCurator 为官方 Hugging Face 语料库内相同 A1 文档的历史输出，提取日期与具体模型配置未逐条披露；",
        "   它不是 README 模型榜单的逐篇预测。POISK 和 ABPairDB 使用同一申请的授权版 B2 记录，",
        "   因文本版本和工具配置不同，本表是公开结果的案例对照，不能当作同模型同输入的算法排名。",
        "3. 本次参考由助手核对原 PDF 图像、显式抗体—SEQ ID—链型关系及序列表后建立；",
        "   待独立人工复核，不是 ABDataBench 官方人工标注，也未把竞品预测当作真值。",
        "   助手核对前已接触竞品输出，此参考审计不是盲评。",
        "   DLL4 的 MLCK-2/MLCK-4 在图1与序列表中存在打印差异；本表固定使用序列表参考，",
        "   这些差异须单独复核，不能仅因未命中该参考就断言抽取有错。",
        "4. 参考仅覆盖上述具名抗体，不是穷尽的全篇专利标注；额外预测单独列为未评分，",
        "   不自动算作假阳性。精确配对恢复、边界差异、缺链和归属问题分开保留。",
        "5. 未完成的本地运行显示未完成，不能记为零分；不拿现成数据库记录数量推断运行成本或速度。",
        "6. 同时保留忽略抗体名称的整对序列命中数，用于区分序列未恢复与名称/实体归属不一致；",
        "   该诊断指标不替代具名抗体的一条记录内 VH/VL 配对指标。",
        "", "文件：",
        "selection.json：样本、版本、输入和源码校验和。",
        "gold.source_verified.json / build_source_reference.py：参考序列、编号、源 PDF 页码与图像哈希。",
        "baselines/<公开号>/：三家的原始记录及统一 VH/VL 适配结果。",
        "dataagent2/<公开号>/：本地完整流程的 OCR、原生提取、请求审计和实际用量。",
        "paired_scores.json：逐抗体、逐链及配对指标，含边界差异、缺失与歧义。",
        "paired_summary.csv：结果汇总。", "",
        "来源：",
        "https://github.com/GAIR-NLP/ABDataBench",
        "https://huggingface.co/datasets/GAIR/ABDataCorpus",
        "https://zenodo.org/records/21775623",
        "https://abpairdb.kaist.ac.kr/download/", "",
        "更新评测（不调用模型）：python src/paired_benchmark.py evaluate",
    ]
    (output / "REPORT.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    with (output / "paired_summary.csv").open("w", newline="", encoding="utf-8-sig") as stream:
        keys = ["document", "system", "status", "eligible_gold_pairs", "vh_exact_recovered", "vl_exact_recovered",
                "annotated_identities_with_variable_pair", "paired_exact_unambiguous", "paired_exact_unambiguous_recovery"]
        writer = csv.DictWriter(stream, fieldnames=keys)
        writer.writeheader()
        for doc, systems in scores.items():
            for system, row in systems.items():
                writer.writerow({"document": doc, "system": system,
                                 **{k: row.get(k) for k in keys if k not in ("document", "system")}})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "evaluate"))
    parser.add_argument("--output", type=Path, default=Path("runs/competitor_pilot_20261028"))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    output = args.output.resolve()
    if args.action == "prepare":
        prepare(root, output)
    else:
        scores = evaluate(root, output)
        for doc, systems in scores.items():
            print(doc, {name: (score["status"], score.get("paired_exact_unambiguous"),
                              score.get("eligible_gold_pairs")) for name, score in systems.items()})


if __name__ == "__main__":
    main()
