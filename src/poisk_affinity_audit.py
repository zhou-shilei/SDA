"""Audit metric labels in the pinned POISK v1.1 CSV without inferring KD from SPR.

Counts are record counts, never independent experimental measurement counts.
Audit-reasoning mentions are exported separately and cannot promote a record to
an explicitly labelled KD/IC50 record. Numeric parsing is a screening aid.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
import unicodedata

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUN = ROOT / "runs/poisk_affinity_audit_20261030"
EXPECTED_MD5 = "f0c1f098abf1756e4b9891aa4dc028ee"
VALUE = "target.binding_affinity.affinity_value"
METHOD = "target.binding_affinity.method"
METRICS = ("KD", "IC50", "EC50", "KA", "Ki")
PLACEHOLDERS = {"", ".", "[]", "n.a.", "n/a", "na", "nan", "none", "none found",
                "not applicable", "not available", "not determined", "not disclosed",
                "not found", "not provided", "not reported", "not specified", "null",
                "unavailable", "unknown", "unspecified", "{}", "—"}


def normalize(text):
    s = unicodedata.normalize("NFKC", text or "")
    s = s.translate(str.maketrans({"−": "-", "–": "-", "—": "-", "μ": "u", "µ": "u", "×": "x"}))
    return re.sub(r"10\s*\^\s*[({]\s*([+-]?\d+)\s*[)}]", r"10^\1", s)


PATTERNS = {
    "KD": re.compile(r"(?<![A-Za-z0-9])K\s*(?:[_-]\s*)?(?:D|\(D\))(?:_?app)?(?:_?\d+)?(?![A-Za-z0-9])|\b(?:equilibrium\s+)?dissociation\s+constant\b", re.I),
    "IC50": re.compile(r"(?<![A-Za-z0-9])IC\s*(?:[_-]\s*)?(?:50|\(50\))(?![A-Za-z0-9])", re.I),
    "EC50": re.compile(r"(?<![A-Za-z0-9])EC\s*(?:[_-]\s*)?(?:50|\(50\))(?![A-Za-z0-9])", re.I),
    "KA": re.compile(r"(?<![A-Za-z0-9])K\s*[_-]?\s*A(?![A-Za-z0-9])|\bassociation\s+constant\b", re.I),
    "Ki": re.compile(r"(?<![A-Za-z0-9])K\s*[_-]?\s*I(?![A-Za-z0-9])|\binhibition\s+constant\b", re.I),
}
RATE = re.compile(r"(?:/\s*(?:s|sec)(?![A-Za-z])|(?:s|sec)\s*(?:\^\s*)?-\s*1\b)", re.I)
NUM = r"(?:\d+(?:\.\d*)?|\.\d+)(?:\s*[eE]\s*[+-]?\d+)?"
SCIENTIFIC = rf"(?:{NUM}\s*[x*]\s*10\s*(?:\^\s*)?[+-]?\d+|10\s*\^\s*[+-]?\d+|{NUM})"
UNIT = r"(?:[fpnum]?M|(?:[fpnum]?mol\s*/\s*[lL])|(?:[fpnum]?g\s*/\s*(?:m[lL]|[lL])))"
QUANTITY = re.compile(rf"(?<![\w.])(?P<number>{SCIENTIFIC})\s*(?P<unit>{UNIT})(?![A-Za-z0-9]|\s*(?:\^\s*)?-\s*1)")
RANGE = re.compile(r"\d\s*(?:-|to|through|and)\s*\d|\brange\b|\bbetween\b", re.I)
CENSOR = re.compile(r"[<>≤≥]|\b(?:less than|more than|greater than|lower than|higher than|at least|at most|up to|or less|or more|or greater|below|above|no more than|not more than)\b", re.I)
RELATIVE = re.compile(r"\b(?:relative|fold|ratio|compared|improvement|increase|decrease|specific activity|titer|titre|comparable|equivalent|same as|not detectable|not detected|no binding)\b", re.I)
MOLAR_SCALE = {"M": Decimal(1), "mM": Decimal("1e-3"), "uM": Decimal("1e-6"),
               "nM": Decimal("1e-9"), "pM": Decimal("1e-12"), "fM": Decimal("1e-15")}


def decimal_number(text):
    s = re.sub(r"\s+", "", text)
    if "x" in s or "*" in s:
        base, exp = re.split(r"[x*]10\^?", s)
        return Decimal(base) * Decimal(10) ** int(exp)
    if s.startswith("10^"):
        return Decimal(10) ** int(s[3:])
    return Decimal(s)


def quantities(text):
    out = []
    s = normalize(text)
    for m in QUANTITY.finditer(s):
        try:
            number = decimal_number(m["number"])
        except (InvalidOperation, ValueError, OverflowError):
            continue
        start = m.start()
        if start and s[start - 1] == "-" and (start < 2 or s[start - 2] not in "0123456789."):
            number = -number
            start -= 1
        unit = re.sub(r"\s+", "", m["unit"])
        scale = MOLAR_SCALE.get(unit)
        if unit.endswith("mol/L") or unit.endswith("mol/l"):
            scale = MOLAR_SCALE.get(unit[:-5] + "M")
        out.append({"text": s[start:m.end()], "value": str(number), "unit": unit,
                    "value_M": str(number * scale) if scale is not None else None,
                    "start": start, "end": m.end()})
    return out


def metric_labels(text):
    s = normalize(text)
    labels = set()
    kinetic_kd = False
    for metric, pattern in PATTERNS.items():
        for match in pattern.finditer(s):
            if metric == "KD":
                # Only an explicit inverse-time unit can reclassify lower-case kd.
                # Limit to the local punctuation-delimited clause so another
                # measurement's rate unit cannot erase a genuine KD.
                before = max(s.rfind(";", 0, match.start()), s.rfind(",", 0, match.start())) + 1
                endings = [p for p in (s.find(";", match.end()), s.find(",", match.end())) if p >= 0]
                after = min(endings) if endings else len(s)
                clause = s[before:after]
                if RATE.search(clause) and not quantities(clause):
                    kinetic_kd = True
                    continue
            labels.add(metric)
    return labels, kinetic_kd


def classify(row):
    raw = row.get(VALUE, "")
    value_labels, rate_value = metric_labels(raw)
    method_labels, rate_method = metric_labels(row.get(METHOD, ""))
    audit_labels, _ = metric_labels(row.get("audit.reasoning", ""))
    present = raw.strip().lower() not in PLACEHOLDERS
    # A method label without an affinity value is not a measured-label record.
    explicit = value_labels | method_labels if present else set()
    q = quantities(raw) if present else []
    norm = normalize(raw)
    flags = []
    if len(explicit) > 1:
        flags.append("multiple_metric_types")
    if rate_value or rate_method:
        flags.append("kinetic_kd_inverse_time")
    if CENSOR.search(norm):
        flags.append("inequality_or_bound")
    if RANGE.search(norm):
        # Scientific exponents such as 10^-9 are not measurement ranges.
        stripped = re.sub(r"10\s*(?:\^\s*)?[+-]\d+|[eE]\s*[+-]\d+", "EXP", norm)
        if RANGE.search(stripped):
            flags.append("range_or_interval")
    if RELATIVE.search(norm):
        flags.append("relative_or_qualitative_language")
    if "~" in norm or re.search(r"\b(?:about|approximately|approx)\b", norm, re.I):
        flags.append("approximate")
    if len(q) > 1:
        flags.append("multiple_quantities")
    if "±" in norm or "+/-" in norm:
        flags.append("uncertainty_expression")
    residual = norm
    for item in reversed(q):
        residual = residual[:item["start"]] + " " + residual[item["end"]:]
    for pattern in PATTERNS.values():
        residual = pattern.sub(" ", residual)
    if q and re.search(rf"(?<![A-Za-z0-9.]){NUM}(?![A-Za-z0-9.])", residual):
        flags.append("additional_numbers_require_review")
    if any(x["value_M"] is None for x in q):
        flags.append("mass_concentration_not_molar")
    if any(Decimal(x["value"]) <= 0 for x in q):
        flags.append("nonpositive_concentration")
    if present and not q:
        flags.append("no_absolute_concentration_parsed")
    hint = next(iter(audit_labels)) if present and not explicit and len(audit_labels) == 1 else ""
    if hint:
        flags.append("audit_reasoning_only_not_confirmed_metric")
    if len(audit_labels) > 1 and not explicit:
        flags.append("multiple_metric_mentions_in_audit")
    point = ""
    blockers = {"multiple_metric_types", "inequality_or_bound", "range_or_interval",
                "relative_or_qualitative_language", "multiple_quantities", "kinetic_kd_inverse_time",
                "uncertainty_expression", "additional_numbers_require_review"}
    if len(explicit) == 1 and len(q) == 1 and q[0]["value_M"] is not None and not (set(flags) & blockers):
        if Decimal(q[0]["value_M"]) > 0:
            point = q[0]["value_M"]
    return {
        "affinity_present": present,
        "value_metrics": "|".join(sorted(value_labels)),
        "method_metrics": "|".join(sorted(method_labels)),
        "explicit_metrics": "|".join(sorted(explicit)),
        "audit_metric_mentions": "|".join(sorted(audit_labels)),
        "audit_only_single_metric_hint": hint,
        "quantity_count": len(q), "quantities_json": json.dumps(q, ensure_ascii=False),
        "single_molar_point_candidate_M": point,
        "screening_flags": "|".join(flags),
    }


COMPACT = ["unique_record_id", "patent_id", "antibody_identifier", "antibody_type",
           "target.protein_name", "target.uniprot_id", "target.organism", VALUE, METHOD,
           "target.binding_conditions", "audit.verdict", "audit.flags"]


def checksum(path, kind):
    h = hashlib.new(kind)
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def source_hashes(path):
    hashes = {name: hashlib.new(name) for name in ("md5", "sha256")}
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            for h in hashes.values():
                h.update(block)
    return {k: h.hexdigest() for k, h in hashes.items()}


def audit(run):
    source = run / "sources/poisk_dataset_v1.1.csv"
    hashes = source_hashes(source)
    actual_md5 = hashes["md5"]
    if actual_md5 != EXPECTED_MD5:
        raise ValueError(f"Source CSV checksum mismatch: {actual_md5}")
    stats = Counter()
    explicit_stats = {metric: Counter() for metric in METRICS}
    hint_stats = {metric: Counter() for metric in METRICS}
    units = {metric: Counter() for metric in METRICS}
    examples = defaultdict(list)
    ids = set()
    patents = set()
    handles = []
    try:
        with source.open(newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            original_fields = reader.fieldnames
            if len(original_fields) != 47:
                raise ValueError("Unexpected POISK schema")
            derived_fields = list(classify({}))
            writers = {}
            def writer(name, fields):
                handle = (run / name).open("w", newline="", encoding="utf-8-sig")
                handles.append(handle)
                obj = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
                obj.writeheader()
                return obj
            compact = writer("records_classified.csv", COMPACT + derived_fields)
            untyped = writer("untyped_affinity_records.csv", COMPACT + derived_fields)
            mixed = writer("multiple_metric_records.csv", COMPACT + derived_fields)
            both = writer("both_KD_IC50_records.csv", original_fields + derived_fields)
            rate = writer("kinetic_kd_records.csv", COMPACT + derived_fields)
            for metric in ("KD", "IC50"):
                writers[metric] = writer(f"{metric}_explicit_records.csv", original_fields + derived_fields)
                writers[metric + "_hint"] = writer(f"{metric}_audit_only_candidates.csv", original_fields + derived_fields)
            for row in reader:
                record_id = row["unique_record_id"]
                if record_id in ids:
                    raise ValueError(f"Duplicate record ID: {record_id}")
                ids.add(record_id)
                patents.add(row["patent_id"])
                result = classify(row)
                combined = {**row, **result}
                compact.writerow(combined)
                stats["records"] += 1
                if not result["affinity_present"]:
                    stats["without_affinity"] += 1
                    continue
                stats["with_affinity"] += 1
                labels = set(filter(None, result["explicit_metrics"].split("|")))
                value_labels = set(filter(None, result["value_metrics"].split("|")))
                if labels:
                    stats["explicit_metric_any"] += 1
                else:
                    stats["untyped_affinity"] += 1
                    untyped.writerow(combined)
                if len(labels) > 1:
                    stats["multiple_metric_types"] += 1
                    mixed.writerow(combined)
                if {"KD", "IC50"} <= labels:
                    stats["both_KD_and_IC50"] += 1
                    both.writerow(combined)
                if "kinetic_kd_inverse_time" in result["screening_flags"]:
                    stats["kinetic_kd_records"] += 1
                    rate.writerow(combined)
                q = json.loads(result["quantities_json"])
                for metric in labels:
                    count = explicit_stats[metric]
                    count["records"] += 1
                    count["value_field_mentions"] += metric in value_labels
                    count["method_only_label"] += metric not in value_labels
                    count["with_concentration_quantity_candidates"] += bool(q)
                    count["with_molar_quantity_candidates"] += any(x["value_M"] is not None for x in q)
                    count["single_molar_point_candidates"] += bool(result["single_molar_point_candidate_M"])
                    count["audit_" + row["audit.verdict"]] += 1
                    if result["single_molar_point_candidate_M"]:
                        count["single_molar_point_audit_" + row["audit.verdict"]] += 1
                    for flag in filter(None, result["screening_flags"].split("|")):
                        count["flag_" + flag] += 1
                    for item in q:
                        units[metric][item["unit"]] += 1
                    if metric in writers:
                        writers[metric].writerow(combined)
                    if len(examples[metric]) < 12:
                        examples[metric].append({k: combined[k] for k in COMPACT + derived_fields})
                hint = result["audit_only_single_metric_hint"]
                if hint:
                    count = hint_stats[hint]
                    count["records"] += 1
                    count["with_concentration_quantity_candidates"] += bool(q)
                    count["audit_" + row["audit.verdict"]] += 1
                    if hint + "_hint" in writers:
                        writers[hint + "_hint"].writerow(combined)
            if stats["records"] != 93193 or len(patents) != 10010:
                raise ValueError("Dataset counts differ from pinned v1.1")
    finally:
        for handle in handles:
            handle.close()
    summary = {
        "dataset": "POISK v1.1", "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_url": "https://zenodo.org/records/21775623/files/poisk_dataset.csv?download=1",
        "source_bytes": source.stat().st_size, "source_md5": actual_md5,
        "source_sha256": hashes["sha256"],
        "script_sha256": checksum(__file__, "sha256"),
        "counts": dict(stats), "patents": len(patents),
        "explicit_metrics": {k: dict(v) for k, v in explicit_stats.items()},
        "audit_only_candidates": {k: dict(v) for k, v in hint_stats.items()},
        "quantity_unit_occurrences_not_measurement_counts": {k: dict(v) for k, v in units.items()},
        "rules": [
            "Explicit metric means named in affinity_value or method with a nonmissing affinity_value.",
            "kd with explicit inverse-time units is a rate constant, not equilibrium KD.",
            "KD/IC50/EC50 are never inferred from nM, SPR, ELISA or assay platform alone.",
            "Audit-only hints are model-text candidates, not confirmed metric assignments; mentions may be negated or disputed.",
            "A row mentioning multiple metrics counts in each metric; overlap is reported separately.",
            "Quantities are text-parsing candidates, not necessarily attributable independent measurements.",
            "Single molar point candidate excludes bounds, ranges, relative language, multiple metrics/quantities, uncertainty, additional numeric context and kinetic kd.",
            "Approximate values are retained with an explicit flag; all original records and audit labels are preserved.",
            "No manual source-patent verification or train/test construction is implied.",
        ],
    }
    (run / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    (run / "examples.json").write_text(json.dumps(examples, ensure_ascii=False, indent=2) + "\n")
    csv_rows = [{"metric": k, **dict(v)} for k, v in explicit_stats.items()]
    fields = ["metric"] + sorted({key for row in csv_rows for key in row if key != "metric"})
    with (run / "metric_summary.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(csv_rows)
    write_report(run, summary)
    print(json.dumps({"counts": summary["counts"], "explicit": summary["explicit_metrics"],
                      "audit_only": summary["audit_only_candidates"]}, ensure_ascii=False, indent=2))


def write_report(run, summary):
    c = summary["counts"]
    e = summary["explicit_metrics"]
    h = summary["audit_only_candidates"]
    rows = []
    for metric in ("KD", "IC50", "EC50", "KA", "Ki"):
        x = e[metric]
        rows.append(f"{metric} | {x.get('records', 0):,} | {x.get('value_field_mentions', 0):,} | "
                    f"{x.get('method_only_label', 0):,} | {x.get('single_molar_point_candidates', 0):,} | "
                    f"{x.get('audit_CONFIRMED', 0):,}")
    report = f"""POISK v1.1：KD / IC50 分类与数值表达整理
核查日期：2026-10-30（Asia/Shanghai）

结果
从 Zenodo 下载完整 poisk_dataset.csv：{summary['source_bytes']:,} 字节（约 234 MB），
{c['records']:,} 条记录、47 列、{summary['patents']:,} 个专利号。
文件 MD5：{summary['source_md5']}，与官方 v1.1 发布值一致。
文件 SHA-256：{summary['source_sha256']}。
其中亲和力字段有内容 {c['with_affinity']:,} 条，无内容 {c['without_affinity']:,} 条。

明确指标的记录数
指标 | 亲和力值或方法字段明确标注 | 亲和力值字段标注 | 仅方法字段补充类型 | 单一正摩尔数值候选 | 模型审计 CONFIRMED
{chr(10).join(rows)}

主计数为不同 unique_record_id 的数量；含两个指标的同一行分别计入对应指标。
同时明确标注 KD 和 IC50 的记录有 {c['both_KD_and_IC50']:,} 条，已单独导出。
这些数量不是独立抗体数，也不是独立实验测量数。
例如同一行的 KD(IL1a) 和 KD(IL1b)、同一抗体对多个物种的数值，均仍只算一条 KD 记录。

严格计数规则
仅在 target.binding_affinity.affinity_value 或 target.binding_affinity.method 中明确写出指标名称，
且 affinity_value 非占位/空值时，才计入明确指标记录。
识别 KD、Kd、K_D、KDapp、KD1/KD2、IC50、IC_50、IC₅₀ 等写法，保留来源和原文。
明确带 /s、1/s、s^-1、sec^-1 等单位的 kd 按解离速率处理，不归入平衡解离常数 KD。
共 {c['kinetic_kd_records']:,} 条记录带此类速率表达；部分还同时包含其他指标，不与 KD 计数简单相减。
不因为有 nM/M 单位或采用 SPR/BLI/KinExA/ELISA 方法，就自动认定测量的是 KD 或 IC50。
没有恢复原始专利全文，也没有调用模型为字段补写指标类型。

审计解释中的额外候选
在数值/方法字段未明确归类、但 audit.reasoning 仅提到本次五类指标中的一种时，另存为候选：
KD：{h['KD'].get('records', 0):,} 条；IC50：{h['IC50'].get('records', 0):,} 条。
这些候选不计入上表；解释可能是在否定、质疑或比较该指标，不能直接把提及当成测量标签。
审计文本由原数据集的模型生成，不是独立人工核验。
按本次五类规则仍未明确归类的带亲和信息记录有 {c['untyped_affinity']:,} 条，
包括只写浓度、定性/相对结果、速率或其他指标的记录；它们不是“没有亲和信息”。
因此，明确标注数量不能被解释为整个数据集中实际 KD/IC50 实验的完整总量。

数值与单位整理
quantities_json 保留识别到的浓度表达、数值、单位、原始文本位置和可确定的 value_M。
M、mM、uM/μM、nM、pM、fM 统一为 M；质量浓度保留原单位，不在未知分子量时换算为 M。
单一正摩尔数值候选只保留：指标唯一、浓度表达唯一、数值为正，且没有范围、不等号、
相对描述、多数值、误差项或速率混杂；近似值可保留，但有 approximate 标记。
候选数量只是格式筛选结果，没有证明数值属于正确抗体/抗原、实验条件充分或序列正确。
数值解析器是保守的规则筛选，未识别到数值不意味着原文没有数值。
不能把区间/上限当精确点值，不能把 IC50/EC50 当 KD，不能把未填字段当不结合负例。

文件
sources/poisk_dataset_v1.1.csv：未经改写的原始完整 CSV。
KD_explicit_records.csv / IC50_explicit_records.csv：明确标注的各类记录，保留原始 47 列及分类列。
both_KD_IC50_records.csv：同时含 KD、IC50 的原始记录。
KD_audit_only_candidates.csv / IC50_audit_only_candidates.csv：仅审计解释提示的候选，保留全部原列。
records_classified.csv：全量记录的分类索引与原始数值/方法/条件/审计字段。
untyped_affinity_records.csv：未归入五类明确指标的亲和信息，保留原文和待核验提示。
kinetic_kd_records.csv：包含逆时间 kd 表达的记录。
multiple_metric_records.csv：同时出现多个明确指标的记录。
metric_summary.csv / summary.json：完整统计、单位分布、审计分层与口径。
examples.json：每类前 12 条示例，便于检查，不是随机质量审计。
provenance.json：输入/输出哈希与统计脚本信息。

复算
python src/poisk_affinity_audit.py
python -m unittest discover -s tests -p 'test_poisk_affinity_audit.py' -v
六项测试覆盖指标/方法字段、Unicode 和科学计数、kd 速率混淆、质量浓度、
范围/不等号/相对/误差/多指标，以及审计解释不能直接升级为明确标签。

来源与许可
https://zenodo.org/records/21775623
https://zenodo.org/records/21775623/files/poisk_dataset.csv?download=1
POISK v1.1，Tupikina、Roncoli、Bujotzek、Abanades；CC BY 4.0。
"""
    (run / "REPORT.txt").write_text(report)
    outputs = [{"path": p.name, "bytes": p.stat().st_size, "sha256": checksum(p, "sha256")}
               for p in sorted(run.iterdir()) if p.is_file() and p.name != "provenance.json"]
    provenance = {k: summary[k] for k in ("dataset", "generated_at_utc", "source_url", "source_bytes",
                                         "source_md5", "source_sha256", "script_sha256")}
    provenance.update({"script": "src/poisk_affinity_audit.py", "license": "CC BY 4.0", "outputs": outputs,
                       "validation": "Source MD5, v1.1 row/patent/schema counts, unique record IDs; six focused unit tests."})
    (run / "provenance.json").write_text(json.dumps(provenance, indent=2, ensure_ascii=False) + "\n")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run-dir", type=Path, default=DEFAULT_RUN)
    args = p.parse_args()
    args.run_dir.mkdir(parents=True, exist_ok=True)
    audit(args.run_dir)


if __name__ == "__main__":
    main()
