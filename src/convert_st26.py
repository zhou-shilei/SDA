"""将 WIPO ST.26 序列表 XML 转成 JSON 和 FASTA，仅使用 Python 标准库。

示例：
python src/convert_st26.py data/WO2025108394A1/wipo/PCTCN2024133628-seql.xml \
    --document-id WO2025108394A1 --output-dir /tmp/wipo-converted
"""

import argparse
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET


def parse_listing(xml_path, document_id):
    """读取 XML 中的原始字段，校验编号、条数和序列长度。"""
    xml_path = Path(xml_path)
    source = xml_path.read_bytes()
    root = ET.fromstring(source)
    if root.tag != "ST26SequenceListing":
        raise ValueError("输入必须是 ST26SequenceListing 格式的 XML")

    records = []
    for row in root.findall("SequenceData"):
        # 编号在 XML 属性里；其他字段在 INSDSeq 子节点里。
        seq_id = int(row.attrib["sequenceIDNumber"])
        length = int(row.findtext("INSDSeq/INSDSeq_length"))
        molecule_type = row.findtext("INSDSeq/INSDSeq_moltype")
        raw_sequence = row.findtext("INSDSeq/INSDSeq_sequence")
        if raw_sequence is None or not molecule_type:
            raise ValueError(f"SEQ ID {seq_id} 缺少序列或类型字段")

        # 只移除排版空白，不改大小写、不纠错、不补字符。
        sequence = "".join(raw_sequence.split())
        if length < 0 or len(sequence) != length:
            raise ValueError(
                f"SEQ ID {seq_id} 长度不一致：声明 {length}，实际 {len(sequence)}"
            )
        records.append({
            "seq_id": seq_id,
            "molecule_type": molecule_type,
            "length": length,
            "sequence": sequence,
            "source_file": xml_path.name,
        })

    declared_count = int(root.findtext("SequenceTotalQuantity"))
    if len(records) != declared_count:
        raise ValueError(f"序列条数不一致：声明 {declared_count}，实际 {len(records)}")
    ids = [row["seq_id"] for row in records]
    if sorted(ids) != list(range(1, declared_count + 1)):
        raise ValueError("SEQ ID 必须从 1 开始连续编号，不能重复")

    # 文档号由命令行显式提供，不从序列文字中猜测。
    return {
        "document_id": document_id,
        "source_type": "WIPO_ST26_sequence_listing",
        "source_xml": xml_path.name,
        "source_xml_sha256": hashlib.sha256(source).hexdigest(),
        "listing_attributes": dict(root.attrib),
        "applicant": root.findtext("ApplicantName"),
        "title": root.findtext("InventionTitle"),
        "declared_sequence_count": declared_count,
        "sequence_count": len(records),
        "validation": {
            "counts_match": True,
            "unique_contiguous_seq_ids": True,
            "all_declared_lengths_match": True,
            "dtd_validation_performed": False,
        },
        "records": records,
    }


def write_outputs(payload, output_dir):
    """将同一份解析结果分别写成 JSON 和 FASTA。"""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "sequences.json"
    fasta_path = output_dir / "sequences.fasta"

    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    with fasta_path.open("w", encoding="utf-8") as handle:
        for row in payload["records"]:
            handle.write(
                f">{payload['document_id']}|SEQ_ID_NO_{row['seq_id']}"
                f"|{row['molecule_type']}|length={row['length']}\n"
            )
            sequence = row["sequence"]
            for start in range(0, len(sequence), 80):
                handle.write(sequence[start:start + 80] + "\n")
    return json_path, fasta_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("xml_path", type=Path)
    parser.add_argument("--document-id", required=True, help="例如 WO2025108394A1")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if not args.document_id or any(char.isspace() for char in args.document_id):
        parser.error("--document-id 必须为非空且不含空白的文档号")

    payload = parse_listing(args.xml_path, args.document_id)
    paths = write_outputs(payload, args.output_dir)
    print(f"已转换 {payload['sequence_count']} 条序列，编号、条数及长度校验通过。")
    for path in paths:
        print(path.resolve())


if __name__ == "__main__":
    main()
