"""Export a conservative sequence-input / scalar-KD pilot from the POISK audit.

This does not certify source-patent truth or exhaust the KD labels recoverable
from POISK. It also does not create a train/test split.
"""
from collections import Counter, defaultdict
import csv
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import re

from poisk_affinity_audit import PLACEHOLDERS

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / "runs/poisk_affinity_audit_20261030"


def sequence(raw):
    """Normalize case/whitespace only; never repair residues or trim domains."""
    s = re.sub(r"\s+", "", raw or "").upper()
    return s if s and re.fullmatch("[ACDEFGHIKLMNPQRSTVWY]+", s) else ""


def rejection(row):
    if not row["single_molar_point_candidate_M"]:
        return "not_single_molar_point"
    if row["audit.verdict"] != "CONFIRMED":
        return "audit_not_confirmed"
    if row["audit.flags"].strip():
        return "audit_flags_present"
    if row["screening_flags"].strip():
        return "screening_flags_present_including_approximate"
    if row["antibody_type"] not in {"Conventional", "VHH"}:
        return "unsupported_antibody_architecture"
    if row["target.target_type"] != "Protein":
        return "not_single_protein_target_type"
    if not sequence(row["vh.sequence_from_patent"]) or not sequence(row["target.sequence"]):
        return "invalid_or_missing_vh_or_target_sequence"
    if row["vh.is_full_heavy_chain"] != "False":
        return "vh_not_explicitly_variable_region"
    if row["antibody_type"] == "VHH":
        if row["vl.sequence_from_patent"].strip().lower() not in PLACEHOLDERS:
            return "vhh_has_unexpected_light_chain"
    elif not sequence(row["vl.sequence_from_patent"]) or row["vl.is_full_light_chain"] != "False":
        return "invalid_missing_or_full_length_light_chain"
    return ""


def input_key(row):
    return (sequence(row["vh.sequence_from_patent"]),
            sequence(row["vl.sequence_from_patent"]), sequence(row["target.sequence"]))


def group_candidates(rows):
    groups = defaultdict(list)
    rejected = []
    for row in rows:
        reason = rejection(row)
        if reason:
            rejected.append((row, reason))
        else:
            groups[input_key(row)].append(row)
    accepted, conflicts = {}, {}
    for key, items in groups.items():
        values = {Decimal(x["single_molar_point_candidate_M"]) for x in items}
        (accepted if len(values) == 1 else conflicts)[key] = items
    return accepted, conflicts, rejected


def sample_id(key):
    return hashlib.sha256("\n".join(key).encode()).hexdigest()[:24]


def write_csv(path, rows, fields):
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def main():
    source = RUN / "KD_explicit_records.csv"
    with source.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        fields = reader.fieldnames
        rows = list(reader)
    accepted, conflicts, rejected = group_candidates(rows)
    samples, source_rows, decisions = [], [], []
    for row, reason in rejected:
        decisions.append({"unique_record_id": row["unique_record_id"], "decision": reason, "sample_id": ""})
    for key, items in conflicts.items():
        for row in items:
            decisions.append({"unique_record_id": row["unique_record_id"],
                              "decision": "multiple_kd_values_for_same_sequence_inputs", "sample_id": sample_id(key)})
    for key, items in accepted.items():
        kd = Decimal(items[0]["single_molar_point_candidate_M"])
        sid = sample_id(key)
        samples.append({
            "sample_id": sid, "vh_sequence": key[0], "vl_sequence": key[1], "target_sequence": key[2],
            "KD_M": str(kd.normalize()), "pKD": -math.log10(float(kd)),
            "antibody_types_json": json.dumps(sorted({r["antibody_type"] for r in items})),
            "source_record_count": len(items),
            "source_record_ids_json": json.dumps([r["unique_record_id"] for r in items]),
            "patent_ids_json": json.dumps(sorted({r["patent_id"] for r in items})),
            "methods_json": json.dumps(sorted({r["target.binding_affinity.method"] for r in items})),
            "conditions_json": json.dumps(sorted({r["target.binding_conditions"] for r in items}), ensure_ascii=False),
            "antibody_names_json": json.dumps(sorted({r["antibody_identifier"] for r in items}), ensure_ascii=False),
            "target_names_json": json.dumps(sorted({r["target.protein_name"] for r in items}), ensure_ascii=False),
        })
        for row in items:
            source_rows.append({"sample_id": sid, **row})
            decisions.append({"unique_record_id": row["unique_record_id"], "decision": "included_source_record", "sample_id": sid})
    write_csv(RUN / "KD_training_candidates.csv", samples, list(samples[0]))
    write_csv(RUN / "KD_training_source_records.csv", source_rows, ["sample_id"] + fields)
    write_csv(RUN / "KD_training_decisions.csv", sorted(decisions, key=lambda x: x["unique_record_id"]),
              ["unique_record_id", "decision", "sample_id"])
    conflict_rows = [{"sample_id": sample_id(key), **row} for key, items in conflicts.items() for row in items]
    write_csv(RUN / "KD_training_conflicting_labels.csv", conflict_rows, ["sample_id"] + fields)
    summary = {
        "explicit_kd_records": len(rows),
        "single_molar_point_records": sum(bool(r["single_molar_point_candidate_M"]) for r in rows),
        "model_confirmed_point_records": sum(bool(r["single_molar_point_candidate_M"]) and r["audit.verdict"] == "CONFIRMED" for r in rows),
        "eligible_source_records_before_label_conflicts": sum(map(len, accepted.values())) + sum(map(len, conflicts.values())),
        "unique_sequence_input_groups_before_label_conflicts": len(accepted) + len(conflicts),
        "multi_label_input_groups_excluded": len(conflicts), "multi_label_source_records_excluded": len(conflict_rows),
        "included_source_records": len(source_rows), "deduplicated_training_candidates": len(samples),
        "first_rejection_counts": dict(Counter(reason for _, reason in rejected)),
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "deduplication_key": "VH sequence + VL sequence (empty for VHH) + target.sequence; whitespace/case normalized only",
        "label_conflict_policy": "Exclude every input group with more than one distinct numeric KD; do not average. Different conditions may legitimately explain the values.",
        "limitation": "Conservative pilot subset of explicitly typed records; model CONFIRMED is not independent manual validation; no generalization evaluation split is supplied.",
    }
    (RUN / "KD_training_summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
    report = f"""POISK：当前可组装的保守 KD 训练候选
日期：2026-10-30（Asia/Shanghai）

当前导出 {len(samples)} 个去重的（VH、VL、靶点序列）—KD 候选样本，来自 {len(source_rows)} 条原始记录。
每条有 KD_M、pKD = -log10(KD_M)、输入序列、原始记录号、专利号、方法和条件。

计数过程
明确 KD 记录 {len(rows)} -> 单一正摩尔数值候选 {summary['single_molar_point_records']} ->
原数据模型审计 CONFIRMED {summary['model_confirmed_point_records']} ->
无审计/数值筛选标记、普通抗体或 VHH、单蛋白靶点、标准氨基酸序列齐全、
VH/VL 被明确标为可变区的记录 {summary['eligible_source_records_before_label_conflicts']} ->
不同输入序列组 {summary['unique_sequence_input_groups_before_label_conflicts']} ->
排除同一输入对应多个不同 KD 的 {len(conflicts)} 组（{len(conflict_rows)} 条来源记录） -> {len(samples)} 个样本。

未自动截取完整重轻链；未修复残基；VHH 不要求 VL，但不得有异常的非空 VL。
近似值、范围、不等号、多指标、多数值、误差、速率、质量浓度及审计标记都没有进入此保守子集。
相同序列输入和相同数值 KD 跨记录合并，所有来源和方法/条件保留。
同一序列输入出现不同 KD 可能由条件或测量差异造成，另表保留，不判为原文错误，也不取均值。

这个数是当前可以组装并用于训练试验的保守候选数，不是 POISK 全部可用 KD 的上限。
未标明指标的记录、仅审计解释提示 KD 的记录和需要进一步处理完整链的记录尚未纳入。
原数据的 CONFIRMED 是模型标签；本次未对这些样本逐篇独立核验原专利，也未生成防泄漏训练/测试划分。

文件
KD_training_candidates.csv：去重后的序列输入、KD_M、pKD 及来源清单。
KD_training_source_records.csv：保留全部原始列的入选来源记录。
KD_training_conflicting_labels.csv：相同序列输入的不同 KD 和原始条件。
KD_training_decisions.csv：全部明确 KD 记录的纳入/排除原因。
KD_training_summary.json：计数、去重规则及文件校验信息。

复算：python src/poisk_kd_training_candidates.py
"""
    (RUN / "KD_TRAINING_REPORT.txt").write_text(report)
    (ROOT / "docs/poisk_kd_training_candidates_20261030.txt").write_text(report)
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
