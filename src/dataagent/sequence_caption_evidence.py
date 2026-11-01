"""Validate a cited sequence-table title together with its named identifier row."""

import re

from .llm_io import contains_entity_name, normalize


def _named_row(text: str, names: list[str]) -> bool:
    text = text.strip()
    if any(contains_entity_name(text, name) for name in names):
        return True
    # A comma list spells out membership, not identity or suffix inheritance.
    group = re.fullmatch(r"\s*([A-Za-z][A-Za-z0-9]*-)(\d+[A-Za-z]*)(\s*,\s*\d+[A-Za-z]*)+\s*", text)
    if group is None:
        return False
    prefix = group[1]
    suffixes = re.findall(r"\d+[A-Za-z]*", text[len(prefix):])
    members = {normalize(prefix + suffix) for suffix in suffixes}
    return any(normalize(name) in members for name in names)


def _caption_scope(text: str):
    text = re.sub(r"</?sub\b[^>]*>", "", text, flags=re.I)
    text = re.sub(r"\bV\s*_\s*([HL])\b", r"V\1", text, flags=re.I)
    if re.search(r"\bCDR\b|HCDR|LCDR|constant|恒定", text, re.I):
        return None
    chains = set()
    for chain, pattern in (("VH", r"\bVH\b|\bheavy\s+chain\b|重链"),
                           ("VL", r"\bVL\b|\blight\s+chain\b|轻链")):
        if re.search(pattern, text, re.I):
            chains.add(chain)
    if len(chains) != 1:
        return None
    chain = chains.pop()
    variable = re.search(r"\bV[HL]\b|variable|可变", text, re.I)
    return chain, "variable" if variable else "full_chain"


def caption_sequence_relation(link: dict, entity: dict, sources: list[dict]) -> bool:
    """Require the caption, identifier and name to support one scoped relation.

    Only the sources cited by this link are considered. A title from another
    table, a CDR title or a label from another row cannot prove this mapping.
    """
    seq = str(link['seq_id'])
    for source in sources:
        if source.get('kind') != 'cell':
            continue
        value = normalize(source.get('text', ''))
        explicit = re.fullmatch(rf"(?:seq(?:uence)?id(?:no[.:]?)?[.:]?|no[.:]?){seq}", value, re.I)
        if not explicit and value != seq:
            continue
        table = source.get('table_id')
        row = source.get('row_index')
        col = source.get('col_index')
        if table is None or row is None or col is None:
            continue
        if not explicit and not any(
            s.get('kind') == 'cell' and s.get('table_id') == table
            and s.get('col_index') == col and s.get('row_index', row) < row
            and re.fullmatch(r"seq(?:uence)?id(?:no[.:]?)?|序列编号|序列号", normalize(s.get('text', '')), re.I)
            for s in sources
        ):
            continue
        caption = any(s.get('kind') == 'table' and s.get('table_id') == table
                      and _caption_scope(s.get('text', '')) == (link['chain'], link['scope'])
                      for s in sources)
        named = any(s.get('kind') == 'cell' and s.get('table_id') == table
                    and s.get('row_index') == row
                    and _named_row(s.get('text', ''), entity['raw_names']) for s in sources)
        if caption and named:
            return True
    return False


def complete_caption_refs(link: dict, entity: dict, evidence: dict) -> dict:
    """Attach a supplied same-table caption only when it proves the typed row.

    This completes the source citations for an existing model proposal; it
    never creates a mapping, infers a missing chain or uses another table's title.
    """
    refs = link['evidence_refs']
    if any(ref not in evidence for ref in refs):
        return link
    sources = [evidence[ref] for ref in refs]
    tables = {s.get('table_id') for s in sources if s.get('kind') == 'cell'}
    for identity in sorted(t for t in tables if isinstance(t, str)):
        title = evidence.get(identity)
        if identity in refs or not title or title.get('kind') != 'table':
            continue
        if caption_sequence_relation(link, entity, [*sources, title]):
            return {**link, 'evidence_refs': [*refs, identity]}
    return link
