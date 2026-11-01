"""Record-level binding evidence triage for the pinned POISK v1.1 release.

This audits released fields and the release's model-generated explanations. It
does not independently verify 93,193 source patents or convert missing affinity
values / EC50 fitting failures into non-binding labels.
"""
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal
import csv
import hashlib
import json
from pathlib import Path
import re

from poisk_affinity_audit import EXPECTED_MD5, PLACEHOLDERS, VALUE, METHOD, normalize, source_hashes

ROOT = Path(__file__).resolve().parents[1]
SOURCE_RUN = ROOT / "runs/poisk_affinity_audit_20261030"
RUN = ROOT / "runs/poisk_binding_audit_20261030"

DIRECT_ASSAY = re.compile(r"\b(?:SPR|BLI|KinExA|Scatchard|MST|ITC|SET|binding)\b|surface plasmon|biacore|proteon|octet|biolayer|bio-layer|interferometry|thermophoresis|isothermal titration|equilibrium titration", re.I)
FUNCTIONAL = re.compile(r"neutraliz|inhibit|inhibition|cytotoxic|proliferat|viability|reporter|luciferase|signaling|signalling|activation|agonis|antagonis|phagocyt|ADCC|CDC|competition|competitive", re.I)
NEGATIVE_BINDING = re.compile(r"\b(?:no\s+(?:(?:detectable|measurable|specific|significant)\s+)?binding|(?:does|did|do|could)\s+not\s+bind|(?:doesn't|didn't|don't|couldn't|can't|cannot)\s+bind|(?:fails?|failed)\s+to\s+bind|no\s+longer\s+binds?|non[- ]binding)\b|\bbinding\s+(?:was\s+)?not\s+detect(?:ed|able)\b", re.I)
ND = re.compile(r"^\s*(?:not\s+detect(?:ed|able)|undetectable|N\.?D\.?)\s*[.!]?\s*$", re.I)
MIXED = re.compile(r"no\s*/\s*low|no\s+or\s+low|weak\s*/\s*no|no\s*/\s*weak", re.I)
VALUE_UNCERTAIN = re.compile(r"not (?:reported|measured|quantified|determined|calculated)|not explicitly|could not|cannot|not available|not provided|inferred|putative|hypothetical|predicted", re.I)
RAW_BINDING = re.compile(r"\b(?:specific\s+binding\s+(?:detected|observed)|qualitative\s+binding|(?:strong|weak|positive|high|low|detectable|measurable)\s+(?:specific\s+)?binding|(?:high|low|strong|weak)\s+affinity|binds?\s+(?:to\s+)?|positive\s+ELISA\s+signal)\b", re.I)
RAW_INVERSE_MOLAR = re.compile(r"\d\s*(?:[x*]\s*10\s*\^?\s*[+-]?\d+)?\s*(?:M\s*\^?\s*-\s*1|1\s*/\s*M)(?!\s*s)")
POSITIVE_CLAIM = re.compile(r"\bbinds?\s+(?:specifically\s+)?(?:to\s+)?|\bbound\s+(?:specifically\s+)?to\s+|\bbinding\s+(?:specifically\s+)?to\s+|\breacts?\s+with\s+|\breactive\s+with\s+|\brecogn[izs]+es?\s+|\bspecific\s+for\s+", re.I)
CLAIM_UNCERTAIN = re.compile(r"\b(?:not|no|never|cannot|unable|incapable|fails?|failed|without|lack|lacking|unverified|unconfirmed|unclear|uncertain|inferred|assumed|expected|may|might|could|would|potential|predicted|putative|hypothetical)\b|\b(?:doesn|didn|don|couldn|can)['’]t\b", re.I)
OTHER_SUBJECT = re.compile(r"\b(?:parental|parent|reference|control|donor|wild[- ]type|other antibody)\b", re.I)
CORE_FLAG = re.compile(r"(?:^|[\s,;])(?:target(?:[._\s]|$)|vh(?:[._\s]|$)|vl(?:[._\s]|$)|antibody_identifier|antibody_name|binding_affinity|affinity_value|binding_conditions|chain|pair|original construct-level)", re.I)

# These are reviews of the release's own fields/explanation, not original PDF
# verification. Explicit exceptions prevent known phrase-level false negatives.
CASE_REVIEWS = {
    "000010": ("reported_no_binding", "原审计解释明确说此重轻链组合不以可检测水平结合 EGFR（ELISA）；不是单凭 ND 缩写判阴性。"),
    "010123": ("uncertain", "原审计解释用回突变恢复亲和力及比较表缺少数据推断基本移植体不结合；未作为明确阴性。"),
    "051899": ("positive_statement", "原审计解释明确说明 FACS EC50 为 ND，但观察到弱结合，Fold Above Background = 7。"),
    "073696": ("positive_statement", "原审计解释明确说明 EC50 无法确定，但观察到结合，Fold Above Background = 26。"),
    "060213": ("positive_statement", "原审计解释明确区分 CD52 抗原结合保留与 FcγRIII 结合不可检测；当前靶点是 CD52。"),
    "050266": ("reported_no_binding", "原审计解释引用 huMAb1 did not bind；原审计等级 PLAUSIBLE，仍只列为待核验阴性线索。"),
    "050268": ("reported_no_binding", "原审计解释明确引用亲和表的 Binding not detected 脚注，支持对应条件下未检测到结合。"),
}


def present(text):
    return (text or "").strip().lower() not in PLACEHOLDERS


def identity_present(text):
    s = re.sub(r"[_/]+", " ", normalize(text or ""))
    return present(s) and re.search(r"\b(?:unknown|unidentified|unspecified|not reported|not specified|not available)\b", s, re.I) is None


def words(text):
    text = normalize(text).translate(str.maketrans({"α": "alpha", "β": "beta", "γ": "gamma", "κ": "kappa", "ω": "omega"}))
    return text


def alias_pattern(alias):
    parts = re.findall(r"[A-Za-z0-9]+", words(alias))
    if not parts or len("".join(parts)) < 3:
        return None
    return re.compile(r"(?<![A-Za-z0-9])" + r"[\s_-]*".join(map(re.escape, parts)) + r"(?![A-Za-z0-9])", re.I)


def target_patterns(row):
    name = row.get("target.protein_name", "")
    aliases = [name]
    aliases += re.split(r"[();,/]|\s+and\s+", name)
    genes = row.get("target.gene_names", "")
    aliases += re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", genes)
    aliases += [row.get("target.uniprot_id", "")]
    generic = {"human", "mouse", "protein", "antigen", "receptor", "alpha", "beta", "gamma", "unknown", "none", "not reported", "not applicable"}
    return [p for alias in aliases if present(alias) and alias.lower().strip() not in generic
            if (p := alias_pattern(alias)) is not None]


def explicit_audit_statement(row):
    patterns = target_patterns(row)
    if not patterns:
        return ""
    name = row.get("antibody_identifier", "")
    name_base = re.split(r"\s*\(", name)[0]
    names = [p for text in (name, name_base) if (p := alias_pattern(text)) is not None]
    audit = row.get("audit.reasoning", "").replace("\\n", "\n")
    for sentence in re.split(r"(?<=[.!?])\s+|\n", audit):
        sentence_norm = words(sentence).replace("**", "")
        if CLAIM_UNCERTAIN.search(sentence_norm) or NEGATIVE_BINDING.search(sentence_norm):
            continue
        for claim in POSITIVE_CLAIM.finditer(sentence_norm):
            before = sentence_norm[max(0, claim.start() - 200):claim.start()]
            after = sentence_norm[claim.end():claim.end() + 240]
            named = any(p.search(before) for p in names)
            subject = named or re.search(r"\b(?:antibod(?:y|ies)|VHH|scFv|Fab|clone)\b", before, re.I)
            if not subject or (OTHER_SUBJECT.search(before) and not named):
                continue
            if any(p.search(after) for p in patterns):
                return sentence.strip()
    return ""


def classify(row, numeric):
    raw = row.get(VALUE, "")
    method = row.get(METHOD, "")
    value = normalize(raw)
    method_norm = normalize(method)
    flags = row.get("audit.flags", "")
    trusted = row.get("audit.verdict") == "CONFIRMED" and not CORE_FLAG.search(flags)
    identity = identity_present(row.get("antibody_identifier", "")) and identity_present(row.get("target.protein_name", ""))
    trusted = bool(trusted and identity)
    labels = set(filter(None, numeric.get("explicit_metrics", "").split("|")))
    quantities = json.loads(numeric.get("quantities_json", "[]"))
    positive_quantity = any(Decimal(q["value"]) > 0 for q in quantities)
    direct = bool(DIRECT_ASSAY.search(method_norm)) and not FUNCTIONAL.search(method_norm)
    mixed = bool(MIXED.search(value))
    negative = bool(NEGATIVE_BINDING.search(value))
    nd = bool(ND.fullmatch(value))
    statement = explicit_audit_statement(row)
    detail = ""
    evidence_source = ""
    evidence = ""
    category = "uncertain"
    if row.get("unique_record_id") in CASE_REVIEWS:
        proposed, detail = CASE_REVIEWS[row["unique_record_id"]]
        category = proposed if proposed == "uncertain" or trusted else (
            "negative_clue_needs_review" if proposed == "reported_no_binding" else "positive_clue_needs_review")
        evidence_source = "audit.reasoning_case_review"
        evidence = row.get("audit.reasoning", "")
    elif mixed:
        detail = "No/Low 等混合表述，不能自动定为阳性或阴性。"
    elif negative:
        category = "reported_no_binding" if trusted else "negative_clue_needs_review"
        evidence_source, evidence = "affinity_value", raw
        detail = "记录报告在相应条件下无可检测结合；不是所有条件下绝对不结合。"
    elif nd:
        # ND alone may mean failure to determine EC50 rather than no binding.
        if statement:
            category = "positive_statement" if trusted else "positive_clue_needs_review"
            evidence_source, evidence = "audit.reasoning", statement
            detail = "数值未确定，但有针对当前靶点的正向结合陈述。"
        else:
            detail = "Not Detected/ND 未明确说明缺少结合还是无法确定数值，保持不确定。"
    else:
        uncertain_value = bool(VALUE_UNCERTAIN.search(value))
        bound = "inequality_or_bound" in numeric.get("screening_flags", "")
        relative = "relative_or_qualitative_language" in numeric.get("screening_flags", "")
        functional_metric_only = bool(labels & {"IC50", "Ki"}) and not bool(labels & {"KD", "KA"})
        quantitative = present(raw) and not uncertain_value and not bound and not relative and (
            (positive_quantity and "KD" in labels) or
            (positive_quantity and direct and not functional_metric_only) or
            ("KA" in labels and bool(RAW_INVERSE_MOLAR.search(value)) and direct))
        raw_qualitative = present(raw) and not uncertain_value and (
            bool(RAW_BINDING.search(value)) or
            (direct and re.search(r"\b(?:high|strong|weak|low)\b", value, re.I) is not None))
        if quantitative:
            category = "positive_direct" if trusted else "positive_clue_needs_review"
            evidence_source, evidence = "affinity_value_and_method", raw + " | " + method
            detail = "正数 KD/结合常数或直接结合实验浓度结果；不要求精确单点或完整序列。"
        elif raw_qualitative:
            category = "positive_qualitative" if trusted else "positive_clue_needs_review"
            evidence_source, evidence = "affinity_value_and_method", raw + " | " + method
            detail = "亲和字段明确描述结合或直接实验中的结合强弱。"
        elif statement:
            category = "positive_statement" if trusted else "positive_clue_needs_review"
            evidence_source, evidence = "audit.reasoning", statement
            detail = "原审计解释针对当前抗体和当前靶点明确陈述结合；不等于独立实验复核。"
        elif present(raw) and (FUNCTIONAL.search(method_norm) or labels & {"IC50", "EC50", "Ki"}):
            category = "functional_or_competition_only"
            evidence_source, evidence = "affinity_value_and_method", raw + " | " + method
            detail = "有功能/抑制/竞争结果，但本轮未找到足够直接的结合证据。"
        else:
            detail = "本轮保守规则未取得足够针对当前记录的正向或阴性结合证据。"
    return {
        "binding_evidence_category": category,
        "binding_supported_by_release": category in {"positive_direct", "positive_qualitative", "positive_statement"},
        "evidence_source": evidence_source, "evidence_excerpt": evidence,
        "decision_detail": detail, "has_binding_related_audit_flag": bool(CORE_FLAG.search(flags)),
        "has_affinity_value": present(raw),
    }


def main():
    RUN.mkdir(parents=True, exist_ok=True)
    source = SOURCE_RUN / "sources/poisk_dataset_v1.1.csv"
    hashes = source_hashes(source)
    if hashes["md5"] != EXPECTED_MD5:
        raise ValueError("POISK source checksum mismatch")
    numeric = {}
    with (SOURCE_RUN / "records_classified.csv").open(newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            numeric[row["unique_record_id"]] = {k: row[k] for k in ("explicit_metrics", "quantities_json", "screening_flags")}
    counts = Counter()
    tiers = defaultdict(Counter)
    supported_no_affinity = 0
    sample_candidates = defaultdict(list)
    seen = set()
    handles = []
    with source.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        original = reader.fieldnames
        derived = list(classify({}, {}))
        compact = ["unique_record_id", "patent_id", "antibody_identifier", "antibody_type",
                   "target.protein_name", "target.organism", VALUE, METHOD, "target.binding_conditions",
                   "audit.verdict", "audit.flags"] + derived
        writers = {}
        for filename, fields in [
            ("binding_record_labels.csv", compact),
            ("binding_supported_records.csv", original + derived),
            ("reported_no_binding_records.csv", original + derived),
            ("needs_review_records.csv", compact),
        ]:
            handle = (RUN / filename).open("w", newline="", encoding="utf-8-sig")
            handles.append(handle)
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writers[filename] = writer
        try:
            for row in reader:
                rid = row["unique_record_id"]
                if rid in seen:
                    raise ValueError("Duplicate record ID")
                seen.add(rid)
                result = classify(row, numeric[rid])
                combined = {**row, **result}
                category = result["binding_evidence_category"]
                counts[category] += 1
                tiers[category][row["audit.verdict"]] += 1
                writers["binding_record_labels.csv"].writerow(combined)
                if result["binding_supported_by_release"]:
                    writers["binding_supported_records.csv"].writerow(combined)
                    supported_no_affinity += not result["has_affinity_value"]
                elif category == "reported_no_binding":
                    writers["reported_no_binding_records.csv"].writerow(combined)
                else:
                    writers["needs_review_records.csv"].writerow(combined)
                if len(sample_candidates[category]) < 20:
                    sample_candidates[category].append({k: combined[k] for k in compact})
        finally:
            for handle in handles:
                handle.close()
    if len(seen) != 93193:
        raise ValueError("Unexpected release size")
    summary = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(), "records": len(seen),
        "categories": dict(counts), "audit_verdict_by_category": {k: dict(v) for k, v in tiers.items()},
        "binding_supported_by_released_evidence": sum(counts[k] for k in ("positive_direct", "positive_qualitative", "positive_statement")),
        "binding_supported_without_affinity_value": supported_no_affinity,
        "independent_source_patent_verification": "Not performed for the full dataset; support derives from released fields and original model audit text.",
        "source": str(source.relative_to(ROOT)), "source_md5": hashes["md5"], "source_sha256": hashes["sha256"],
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "case_reviews": CASE_REVIEWS,
    }
    (RUN / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
    (RUN / "examples.json").write_text(json.dumps(sample_candidates, indent=2, ensure_ascii=False) + "\n")
    write_report(summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def write_report(summary):
    c = summary["categories"]
    positive = summary["binding_supported_by_released_evidence"]
    unverified = c.get("positive_clue_needs_review", 0)
    negative = c.get("reported_no_binding", 0)
    negative_review = c.get("negative_clue_needs_review", 0)
    functional = c.get("functional_or_competition_only", 0)
    unknown = c.get("uncertain", 0)
    report = f"""POISK v1.1：结合证据的全量记录核查
日期：2026-10-30（Asia/Shanghai）

本轮能明确列出的支持集合
全量 93,193 条记录均已按同一规则检查。
从发布字段和原数据的模型审计中，保守列出 {positive:,} 条有明确正向结合支持的记录，占 {positive / summary['records']:.2%}。
这个数不是逐篇独立验证原始专利后得到的“真实阳性总数”，也不是该数据库所有实际结合者的上限。
本轮的证据来源是下载的 POISK 原始 CSV 及其 audit.reasoning，而不是重新检索 9.3 万条记录的源文献。

互斥分类（合计 93,193 条）
有明确正向结合支持：{positive:,}
  定量 KD/结合常数或直接结合测定的浓度结果：{c.get('positive_direct', 0):,}
  亲和力字段中的明确定性结合描述：{c.get('positive_qualitative', 0):,}
  本轮依据原审计解释中针对当前抗体/靶点的明确结合陈述：{c.get('positive_statement', 0):,}
有正向线索，但原审计等级或关键字段仍需核验：{unverified:,}
只有功能/抑制/竞争结果，本轮未取得足够直接结合证据：{functional:,}
发布记录报告未检测到/无显著结合，且模型审计通过、无关系关键字段标记：{negative:,}
阴性线索仍待核验：{negative_review:,}
当前字段与保守规则不足以判定：{unknown:,}

这次如何判定正向支持
1. 不要求有精确 KD，不要求完整 VH/VL/靶点序列，不以“能否训练 KD 回归”作为结合判定标准。
2. 定量支持允许范围和多个测量值，但不把只有右删失/界限或相对比较的数值直接当成观察到结合。
3. 正数 KD 或结合常数，或者 SPR/BLI/KinExA/Scatchard/SET 等结合测定的正浓度结果，可提供定量支持。
   明确名为 binding assay 的方法也可提供结合测定支持；只有泛称 ELISA/FACS 和浓度时，不自动推断。
4. 原始数值字段直接描述 specific binding detected、qualitative binding、high affinity 等，可提供定性支持。
5. 没有数值时，原审计解释明确陈述当前抗体 binds/binding to/recognizes/specific for 当前靶点，也可纳入陈述支持。
   必须匹配当前靶点名称、基因名或 UniProt 标识；仅“target is confirmed”不算结合陈述。
   否定、假设、失败、其他靶点或仅参考/亲本抗体的陈述不自动升级为当前记录的结合证据。
6. 以上支持集合还要求原数据 audit.verdict=CONFIRMED，且没有抗体身份、链归属、靶点或实验绑定等关键字段标记。
   普通发现方法、非关键表位标签等标记不会一律排除，避免重复之前训练子集过严的过滤。
7. 每条保留证据字段和解释片段，便于复核。原数据的 CONFIRMED 仍是模型审计标签，不是本次人工实验验证。

为什么不能把剩余记录当作不结合
支持集合中有 {summary['binding_supported_without_affinity_value']:,} 条亲和力字段为空，但有明确结合陈述。
其他记录可能同样真实结合，只是当前结构化字段未保留足够的实验或陈述信息；本轮规则也可能漏掉表达变体。
功能性 IC50、EC50、中和、细胞抑制等结果未被直接等同于抗体—当前抗原的物理结合。
正向线索待核验的记录也不等于“结合错误”；它们可供进一步原文复核。

“未检测到”必须看语境
000010：字段 Not Detectable，审计解释明确说该组合不以可检测水平结合 EGFR，列为条件限定的未检测到结合。
006189：undetectable off-rate 指解离速率无法检测，同时原字段描述 High，不是没有结合。
051899：EC50 为 ND，但审计解释报告弱结合、Fold Above Background=7，列为结合陈述支持。
073696：EC50 无法确定，但观察到结合、Fold Above Background=26，列为结合陈述支持。
060213：当前靶点 CD52 的结合保留，而 FcγRIII 结合不可检测；不能把另一个分子的阴性套到当前靶点。
010123：原解释用回突变恢复和缺少数据推断基本移植体不结合，本轮挂起为不确定，未作为明确阴性。
这些语境复核针对发布记录及其模型解释，本次未声称已经独立阅读这些记录的原始 PDF。
未检测到结合仍只适用于相应实验条件和检测能力，不是所有条件下绝对不结合。

统计单位与使用边界
按 unique_record_id 计数，保留全部 93,193 条，不是独立抗体数或去重后的序列对数。
一个记录可能概括一组相同属性的抗体或多条件结果；没有把组记录展开成额外独立样本。
这份集合不同于之前 362 个序列—精确 KD 的保守试训样本，不能互换口径。
本轮没有生成训练/测试划分，也没有声称这些结合支持记录都有可用的配对序列或精确亲和力。

文件
binding_record_labels.csv：全量逐条分类、证据来源、证据片段及原因。
binding_supported_records.csv：正向支持集合，保留原始 47 列及分类列。
reported_no_binding_records.csv：条件限定的未检测到结合记录，保留全部原列。
needs_review_records.csv：其余全部记录，分别保留功能线索、正负线索及信息不足状态。
summary.json：完整互斥计数与审计等级分布。
examples.json：各类示例，不是随机准确率估计。
record_text_spotcheck.json：首轮 38 项固定随机记录字段/证据片段检查，不是原文盲评或准确率估计。

可复算
python src/poisk_binding_audit.py
python -m unittest discover -s tests -p 'test_poisk_binding_audit.py' -v
八项测试覆盖直接结合、功能 IC50、无数值正向陈述、否定/其他靶点/亲本、ND/解离速率、关键标记、观察到结合但 EC50 未拟合，以及有明确不结合解释的 ND。

来源
https://zenodo.org/records/21775623
原始 CSV MD5：{summary['source_md5']}
原始 CSV SHA-256：{summary['source_sha256']}
POISK v1.1，Tupikina、Roncoli、Bujotzek、Abanades；CC BY 4.0。
"""
    (RUN / "REPORT.txt").write_text(report)
    (ROOT / "docs/poisk_binding_evidence_20261030.txt").write_text(report)
    outputs = []
    for path in sorted(RUN.iterdir()):
        if path.is_file() and path.name != "provenance.json":
            outputs.append({"path": path.name, "bytes": path.stat().st_size,
                            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    (RUN / "provenance.json").write_text(json.dumps({
        "source": summary["source"], "source_md5": summary["source_md5"],
        "source_sha256": summary["source_sha256"], "script": "src/poisk_binding_audit.py",
        "script_sha256": summary["script_sha256"], "generated_at_utc": summary["generated_at_utc"],
        "outputs": outputs, "scope": summary["independent_source_patent_verification"],
    }, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
