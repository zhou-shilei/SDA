"""Document identity, sequence-region labels and source grouping for the PDF pipeline."""

import re

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

def group_tables_by_document(table_results, *, default_document_id=None):
    groups = {}
    for table in table_results:
        document_id = get_document_id(table, default_document_id)
        groups.setdefault(document_folder(document_id), []).append(table)
    return dict(sorted(groups.items()))

SEQ_ID = re.compile(r"SEQ\s*ID\s*(?:NO\s*)?[.:：]?\s*(\d+)", re.I)
