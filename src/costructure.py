"""按文档和分子名称关联单表结果；不推断别名、不拼装氨基酸序列。

运行：python src/costructure.py --input-dir results
默认在各文档目录生成 costructure.json；--output 可显式指定合并输出文件。
records 每条实验测量一行；entities 保留包括抗原在内的所有有名序列分组。
sequences 保存任意区域，常见区域另展开为顶层字段，便于后续导出宽表。
matched 仅表示同文档同名关联成功，has_vh_vl 单独表示是否有明确 VH/VL。
"""

import argparse
import json
import re
from collections import Counter
from pathlib import Path


REGION_FIELDS = (
    "VH", "VL", "H_chain", "L_chain",
    *(f"HCDR{i}" for i in range(1, 4)),
    *(f"LCDR{i}" for i in range(1, 4)),
    *(f"HFR{i}" for i in range(1, 5)),
    *(f"LFR{i}" for i in range(1, 5)),
    "CH1", "CH2", "CH3", "CH4", "CL", "Fc",
)
REGION_ALIASES = {field.upper(): field for field in REGION_FIELDS}
REGION_ALIASES.update({
    "重链可变区": "VH", "轻链可变区": "VL",
    "HEAVYCHAINVARIABLEREGION": "VH", "LIGHTCHAINVARIABLEREGION": "VL",
    "重链": "H_chain", "重链全长": "H_chain", "完整重链": "H_chain",
    "HEAVYCHAIN": "H_chain", "FULLHEAVYCHAIN": "H_chain", "HC": "H_chain",
    "轻链": "L_chain", "轻链全长": "L_chain", "完整轻链": "L_chain",
    "LIGHTCHAIN": "L_chain", "FULLLIGHTCHAIN": "L_chain", "LC": "L_chain",
    **{f"FRH{i}": f"HFR{i}" for i in range(1, 5)},
    **{f"FRL{i}": f"LFR{i}" for i in range(1, 5)},
})
PATENT_ID = re.compile(r"(?<![A-Z0-9])(?:WO|US|EP|CN|JP|KR)\d{6,}[A-Z]\d?(?![A-Z0-9])", re.I)


def _text(value):
    if isinstance(value, str):
        return value.strip() or None
    return None


def normalize_region(region):
    """只统一明确的区域标签；未指明链的 FR1 等标签保持原样。"""
    region = _text(region)
    if region is None:
        return None
    return REGION_ALIASES.get(re.sub(r"\s+", "", region).upper(), region)


def get_document_id(table, default_document_id=None):
    explicit = _text(table.get("document_id")) or _text(table.get("patent_id"))
    if explicit:
        return explicit
    for field in ("table_id", "image_path", "result_file"):
        match = PATENT_ID.search(str(table.get(field) or ""))
        if match:
            return match.group(0).upper()
    return _text(default_document_id)


def document_folder(document_id):
    """文档 ID 直接作为目录名；未知归属单独保存。"""
    name = document_id or "_unassigned"
    if name in {".", ".."} or any(char in name for char in ("/", "\\", "\0")):
        raise ValueError(f"文档 ID 不能作为目录名：{name!r}")
    return name


def _source(table, kind, index):
    return {"table_id": table.get("table_id"), "image_path": table.get("image_path"),
            "result_file": table.get("result_file"), "record_type": kind,
            "record_index": index}


def construct_records(table_results, *, default_document_id=None):
    """精确匹配文档 ID + 去首尾空白的名称；不同变体和大小写保持独立。

    同一区域的不同非空序列视为冲突，宽表值为 null，候选及 SEQ ID 全部保留。
    只提供 CDR 或全长链时，不自动推导 VH/VL；不自动将抗原连接为实验靶点。
    """
    entities = {}
    assay_rows = []
    unassigned = []
    skipped_tables = []
    for table in table_results:
        if table.get("error_stage") or table.get("error"):
            skipped_tables.append({"table_id": table.get("table_id"),
                                   "result_file": table.get("result_file"),
                                   "error_stage": table.get("error_stage"), "error": table.get("error")})
            continue
        document_id = get_document_id(table, default_document_id)
        for kind in ("sequence_records", "assay_records"):
            rows = table.get(kind, [])
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise ValueError(f"{table.get('table_id')}: {kind} 必须是对象数组")
            for index, row in enumerate(rows):
                name = _text(row.get("name"))
                source = _source(table, kind, index)
                if kind == "assay_records":
                    assay_rows.append((document_id, name, row, source))
                    continue
                entry = {**row, "source": source}
                if document_id is None or name is None:
                    unassigned.append({**entry, "document_id": document_id,
                                       "issues": [field for field, value in
                                                  (("missing_document_id", document_id), ("missing_name", name))
                                                  if value is None]})
                    continue
                entity = entities.setdefault((document_id, name), {
                    "document_id": document_id, "name": name, "sequence_records": [],
                })
                entity["sequence_records"].append(entry)

    for entity in entities.values():
        candidates = {}
        issues = []
        for row in entity["sequence_records"]:
            region = normalize_region(row.get("region"))
            if region is None:
                issues.append("missing_region")
                continue
            values = candidates.setdefault(region, [])
            # 不修正 OCR 字符、大小写或内部空格，原值保存在 sequence_records。
            sequence = _text(row.get("sequence"))
            if sequence is not None and sequence not in values:
                values.append(sequence)
        sequences = {region: values[0] if len(values) == 1 else None
                     for region, values in candidates.items()}
        conflicts = {region: values for region, values in candidates.items() if len(values) > 1}
        issues.extend(f"sequence_conflict:{region}" for region in conflicts)
        issues.extend(f"missing_sequence:{region}" for region, values in candidates.items() if not values)
        entity.update(sequences=sequences, sequence_conflicts=conflicts, issues=sorted(set(issues)),
                      has_vh_vl=bool(sequences.get("VH") and sequences.get("VL")))
        entity.update({region: sequences.get(region) for region in REGION_FIELDS})

    records = []
    for document_id, name, assay, source in assay_rows:
        entity = entities.get((document_id, name))
        issues = list(entity["issues"]) if entity else []
        if document_id is None:
            issues.append("missing_document_id")
        if name is None:
            issues.append("missing_name")
        if entity is None:
            issues.append("no_sequence_match")
        issues.extend(f"missing_{field}" for field in ("metric", "value", "unit")
                      if assay.get(field) is None or assay.get(field) == "")
        sequences = entity["sequences"] if entity else {}
        status = "unmatched" if entity is None else (
            "ambiguous" if entity["sequence_conflicts"] else "matched")
        records.append({
            **assay, "document_id": document_id, "name": name,
            **{region: sequences.get(region) for region in REGION_FIELDS},
            "sequences": dict(sequences),
            "sequence_records": entity["sequence_records"] if entity else [],
            "sequence_conflicts": entity["sequence_conflicts"] if entity else {},
            "assay_source": source, "link_status": status,
            "has_vh_vl": bool(sequences.get("VH") and sequences.get("VL")),
            "issues": sorted(set(issues)),
        })
    counts = Counter(record["link_status"] for record in records)
    return {
        "records": records, "entities": list(entities.values()),
        "unassigned_sequence_records": unassigned, "skipped_tables": skipped_tables,
        "summary": {"assay_records": len(records), "sequence_entities": len(entities),
                    "matched": counts["matched"], "ambiguous": counts["ambiguous"],
                    "unmatched": counts["unmatched"],
                    "with_vh_vl": sum(row["has_vh_vl"] for row in records),
                    "unassigned_sequence_records": len(unassigned), "skipped_tables": len(skipped_tables)},
    }


def load_table_results(input_dir):
    """读取单表 JSON，忽略汇总及本模块输出，支持子目录。"""
    input_dir = Path(input_dir)
    if not input_dir.is_dir():
        raise ValueError(f"结果目录不存在：{input_dir}")
    tables = []
    for path in sorted(input_dir.rglob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict) and any(key in payload for key in ("assay_records", "sequence_records")):
            tables.append({**payload, "result_file": str(path.resolve())})
    return tables


def construct_from_directory(input_dir, *, default_document_id=None):
    return construct_records(load_table_results(input_dir), default_document_id=default_document_id)


def group_tables_by_document(table_results, *, default_document_id=None):
    groups = {}
    for table in table_results:
        document_id = get_document_id(table, default_document_id)
        groups.setdefault(document_folder(document_id), []).append(table)
    return dict(sorted(groups.items()))


def construct_by_document(table_results, *, default_document_id=None):
    groups = group_tables_by_document(table_results, default_document_id=default_document_id)
    return {folder: construct_records(tables, default_document_id=default_document_id)
            for folder, tables in groups.items()}


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="按文档和分子名称关联实验与所有序列区域")
    parser.add_argument("--input-dir", type=Path, default=root / "results")
    parser.add_argument("--output", type=Path, help="可选：将所有文档合并输出到指定 JSON 文件")
    parser.add_argument("--document-id", help="仅为无法从元数据或路径识别文档的输入补充 ID")
    args = parser.parse_args()
    try:
        tables = load_table_results(args.input_dir)
        if args.output:
            outputs = {args.output: construct_records(tables, default_document_id=args.document_id)}
        else:
            groups = construct_by_document(tables, default_document_id=args.document_id)
            # 输入本身就是专利目录时，直接写到该目录，避免重复嵌套。
            outputs = {
                (args.input_dir if args.input_dir.name == folder else args.input_dir / folder)
                / "costructure.json": result for folder, result in groups.items()
            }
        for path in outputs:
            if path.is_file():
                previous = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(previous, dict) and any(key in previous for key in ("assay_records", "sequence_records")):
                    raise ValueError("输出路径不能覆盖输入单表结果")
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    for path, result in outputs.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
        print(json.dumps(result["summary"], ensure_ascii=False))
        print(f"已保存：{path}")


if __name__ == "__main__":
    main()
