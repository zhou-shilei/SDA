"""Reversible source grouping; business seed, OCR text and IDs are unchanged."""

import copy

REVISION = 1
FORMATS = ("rows", "grouped")
ENCODING = "grouped_sources_v1"
OLD_DESCRIPTION = "Source rows are [source_integer,PDF_page,kind,table_id_or_null,row_or_null,column_or_null,literal_text]."
DESCRIPTION = """sources use grouped_sources_v1. Each consecutive group shares page, kind and an optional table (absent = null).
lines contains [original_source_integer,literal_text] pairs, whose physical row/column are both null.
rows contains [physical_row,[[physical_column,original_source_integer,literal_text],...]] entries.
Explicit null coordinates, sparse columns and repeated physical positions are retained; do not shift cells.
Groups preserve original order. Cite original INTEGER source IDs, never row/column positions.
context_only_sources and OCR/style metadata still refer to those same source IDs. Text is literal and untruncated."""


def instructions(base, source_format="rows"):
    if source_format not in FORMATS:
        raise ValueError("Unknown Reader source format")
    if source_format == "rows":
        return base
    if base.count(OLD_DESCRIPTION) != 1:
        raise ValueError("Cannot replace an unknown source-format instruction")
    return base.replace(OLD_DESCRIPTION, DESCRIPTION)


def _coordinate(value, *, positive=False):
    return value is None or (type(value) is int and value >= int(positive))


def _check_row(row):
    if (not isinstance(row, list) or len(row) != 7 or type(row[0]) is not int or row[0] < 1
            or not _coordinate(row[1], positive=True) or not isinstance(row[2], str)
            or (row[3] is not None and not isinstance(row[3], str))
            or not _coordinate(row[4]) or not _coordinate(row[5]) or not isinstance(row[6], str)):
        raise ValueError("Invalid canonical source row")


def pack_sources(rows):
    groups, seen = [], set()
    previous = None
    for original in rows:
        _check_row(original)
        sid, page, kind, table, row, column, text = original
        if sid in seen:
            raise ValueError("Repeated source ID")
        seen.add(sid)
        shape = "lines" if row is None and column is None else "rows"
        key = page, kind, table, shape
        if key != previous:
            group = {"page": page, "kind": kind}
            if table is not None:
                group["table"] = table
            group[shape] = []
            groups.append(group)
            previous = key
        if shape == "lines":
            groups[-1][shape].append([sid, text])
        else:
            entries = groups[-1][shape]
            if not entries or entries[-1][0] != row:
                entries.append([row, []])
            entries[-1][1].append([column, sid, text])
    return groups


def unpack_sources(groups):
    if not isinstance(groups, list):
        raise ValueError("Source groups must be an array")
    result, seen = [], set()
    for group in groups:
        if not isinstance(group, dict):
            raise ValueError("Invalid source group")
        fields = set(group) - {"page", "kind", "table"}
        if fields not in ({"lines"}, {"rows"}) or not {"page", "kind"} <= set(group):
            raise ValueError("Unknown or ambiguous source-group fields")
        shape = next(iter(fields))
        entries = group[shape]
        if not isinstance(entries, list) or not entries:
            raise ValueError("Empty or invalid source group")
        flat = []
        if shape == "lines":
            for line in entries:
                if not isinstance(line, list) or len(line) != 2:
                    raise ValueError("Invalid source line")
                flat.append([line[0], None, None, line[1]])
        else:
            for entry in entries:
                if (not isinstance(entry, list) or len(entry) != 2
                        or not isinstance(entry[1], list) or not entry[1]):
                    raise ValueError("Invalid physical table row")
                for cell in entry[1]:
                    if not isinstance(cell, list) or len(cell) != 3:
                        raise ValueError("Invalid source cell")
                    flat.append([cell[1], entry[0], cell[0], cell[2]])
        for sid, row, column, text in flat:
            restored = [sid, group["page"], group["kind"], group.get("table"), row, column, text]
            _check_row(restored)
            if sid in seen:
                raise ValueError("Repeated source ID")
            seen.add(sid)
            result.append(restored)
    return result


def encode_payload(payload, source_format="grouped"):
    if source_format not in FORMATS:
        raise ValueError("Unknown Reader source format")
    if "source_encoding" in payload:
        raise ValueError("Reader sources are already encoded")
    result = copy.deepcopy(payload)
    if source_format == "grouped":
        result["sources"] = pack_sources(result["sources"])
        result["source_encoding"] = ENCODING
        if decode_payload(result) != payload:
            raise ValueError("Source grouping changed the original payload")
    return result


def decode_payload(payload):
    result = copy.deepcopy(payload)
    if "source_encoding" not in result:
        return result
    if result.pop("source_encoding") != ENCODING:
        raise ValueError("Unknown source encoding")
    result["sources"] = unpack_sources(result["sources"])
    return result
