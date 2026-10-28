"""Decode explicitly printed amino-acid notation, retaining original offsets."""

import re

from .provider import digest

AA = set("ACDEFGHIKLMNPQRSTVWY")
THREE_LETTER = dict(zip(
    ("Ala", "Arg", "Asn", "Asp", "Cys", "Gln", "Glu", "Gly", "His", "Ile",
     "Leu", "Lys", "Met", "Phe", "Pro", "Ser", "Thr", "Trp", "Tyr", "Val"),
    "ARNDCQEGHILKMFPSTWYV"))
THREE_LETTER = {name.casefold(): residue for name, residue in THREE_LETTER.items()}
NOTATION_VERSION = "standard_amino_acids_v1"
INVALID = "Noncanonical characters in literal sequence; no OCR correction allowed"
TOKEN = re.compile(r"<br\s*/?>|\s+|[A-Za-z]+|.", re.I | re.S)


def residues(text):
    """Legacy one-letter whitespace removal; never interpret or repair characters."""
    return re.sub(r"\s+", "", re.sub(r"<br\s*/?>", "", text, flags=re.I))


def decode(text, encoding=None):
    """Return residues, encoding and source spans (one span per residue).

    Uppercase single-letter text keeps its existing interpretation. Automatic
    three-letter detection requires at least one lowercase letter; uppercase
    ALA GLY is ambiguous and must not silently become AG. Explicit encoding is
    used only to reconstruct or slice an already grounded three-letter fragment.
    """
    literal = residues(text)
    if encoding in (None, "one_letter") and literal and not set(literal) - AA:
        positions = [(m.start() + i, m.start() + i + 1)
                     for m in TOKEN.finditer(text) if m.group().isalpha()
                     for i in range(len(m.group()))]
        return literal, "one_letter", positions
    if encoding == "one_letter" or encoding not in (None, "three_letter"):
        raise ValueError(INVALID)
    tokens, positions = [], []
    previous_residue, pending_hyphen = False, False
    for match in TOKEN.finditer(text):
        token = match.group()
        if token.isspace() or re.fullmatch(r"<br\s*/?>", token, re.I):
            continue
        if token == "-":
            if not previous_residue or pending_hyphen:
                raise ValueError(INVALID)
            pending_hyphen, previous_residue = True, False
            continue
        if token.casefold() not in THREE_LETTER:
            raise ValueError(INVALID)
        tokens.append(token)
        positions.append((match.start(), match.end()))
        previous_residue, pending_hyphen = True, False
    if not tokens or pending_hyphen or (encoding is None and not any(t != t.upper() for t in tokens)):
        raise ValueError(INVALID)
    return "".join(THREE_LETTER[t.casefold()] for t in tokens), "three_letter", positions


def fragment(sid, text, start, end, encoding=None):
    if not 0 <= start < end <= len(text):
        raise ValueError("Invalid sequence fragment interval")
    quote = text[start:end]
    value, encoding, positions = decode(quote, encoding)
    result = {"s": sid, "start": start, "end": end, "quote": quote,
              "sequence": value, "source_sha256": digest(text)}
    if encoding == "three_letter":
        result["notation"] = {"encoding": encoding, "rule": NOTATION_VERSION,
                              "residue_spans": [[start + a, start + b] for a, b in positions]}
    return result


def validate_fragment(value, source):
    """Recompute conversion and provenance, including every recorded residue span."""
    notation = value.get("notation")
    encoding = notation.get("encoding") if isinstance(notation, dict) else None
    expected = fragment(value["s"], source["text"], value["start"], value["end"], encoding)
    if any(value.get(key) != expected.get(key) for key in (*expected, "notation")):
        raise ValueError("Sequence fragment or conversion provenance changed")
    _, _, positions = decode(expected["quote"], encoding)
    return expected["sequence"], positions
