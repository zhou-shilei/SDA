#!/usr/bin/env python3
"""Coordinate-aware table captions, nearby prose, and reference linking.

The module intentionally starts with deterministic PDF evidence.  It keeps
the original text and bounding boxes so a later model-assisted semantic pass
can be audited against the source document rather than replacing it.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import Any, Iterable


TABLE_REF_RE = re.compile(r"(?:表|TABLE)([0-9]+(?:[-‐-―][0-9]+)?|[IVXLC]+)", re.I)
IMPLICIT_TABLE_RE = re.compile(
    r"(?:如下表|见下表|下表所示|结果列于下表|FOLLOWINGTABLE|TABLEBELOW)", re.I
)
FOOTNOTE_RE = re.compile(r"^(?:注|NOTE|NOTES|\*|#)", re.I)
CONTINUATION_RE = re.compile(r"(?:续表|表续|CONTINUED|CONTINUATION)", re.I)


def _fitz() -> Any:
    try:
        import pymupdf

        return pymupdf
    except ImportError:
        import fitz

        return fitz


def _clean_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    return re.sub(r"\s+", " ", text).strip()


def _compact(value: Any) -> str:
    return re.sub(r"\s+", "", _clean_text(value)).upper()


def _bbox(values: Iterable[Any]) -> list[float]:
    result = [float(value) for value in values]
    if len(result) != 4:
        return []
    return [round(value, 3) for value in result]


def _union_bbox(boxes: Iterable[Iterable[Any]]) -> list[float]:
    valid = [_bbox(box) for box in boxes]
    valid = [box for box in valid if len(box) == 4]
    if not valid:
        return []
    return [
        min(box[0] for box in valid),
        min(box[1] for box in valid),
        max(box[2] for box in valid),
        max(box[3] for box in valid),
    ]


def _area(box: list[float]) -> float:
    return max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1]) if len(box) == 4 else 0.0


def _intersection_area(left: list[float], right: list[float]) -> float:
    if len(left) != 4 or len(right) != 4:
        return 0.0
    return max(0.0, min(left[2], right[2]) - max(left[0], right[0])) * max(
        0.0, min(left[3], right[3]) - max(left[1], right[1])
    )


def _horizontal_overlap(left: list[float], right: list[float]) -> float:
    if len(left) != 4 or len(right) != 4:
        return 0.0
    overlap = max(0.0, min(left[2], right[2]) - max(left[0], right[0]))
    denominator = max(1.0, min(left[2] - left[0], right[2] - right[0]))
    return min(1.0, overlap / denominator)


def _overlap_fraction(block_box: list[float], table_box: list[float]) -> float:
    return _intersection_area(block_box, table_box) / max(1.0, _area(block_box))


def _is_running_header_or_footer(text: str, box: list[float], page_height: float) -> bool:
    compact = _compact(text)
    at_edge = box[1] < 75 or box[3] > page_height - 45
    if not at_edge:
        return False
    return bool(
        re.fullmatch(r"\d+", compact)
        or re.search(r"^(?:WO\d|PCT/|CN\d|US\d|EP\d)", compact, re.I)
    )


def _sentences(text: str) -> list[str]:
    """Split bilingual prose conservatively while retaining source wording."""

    parts = re.split(r"(?<=[。！？!?；;])\s*|(?<=\.)\s+(?=[A-Z])", _clean_text(text))
    return [part.strip() for part in parts if part.strip()]


def table_references(text: str) -> list[str]:
    """Return normalized table identifiers such as ``7`` or ``9-1``."""

    compact = _compact(text).replace("‐", "-").replace("‑", "-")
    compact = compact.replace("‒", "-").replace("–", "-").replace("—", "-")
    return list(dict.fromkeys(match.group(1).upper() for match in TABLE_REF_RE.finditer(compact)))


def _table_mentions(text: str) -> list[str]:
    compact = _compact(text).replace("‐", "-").replace("‑", "-")
    compact = compact.replace("‒", "-").replace("–", "-").replace("—", "-")
    return [match.group(1).upper() for match in TABLE_REF_RE.finditer(compact)]


def _looks_like_caption(text: str) -> bool:
    return bool(re.match(r"^(?:表|TABLE)(?:[0-9]+|[IVXLC]+)", _compact(text), re.I))


def _caption_excerpt(text: str, limit: int = 240) -> str:
    value = _clean_text(text)
    matches = list(
        re.finditer(
            r"(?:表\s*|TABLE\s*)(?:[0-9]+(?:\s*[-‐-―]\s*[0-9]+)?|[IVXLC]+)",
            value,
            re.I,
        )
    )
    if matches:
        # A PDF text block may contain the explanatory sentence followed by
        # the caption.  In that common layout the last marker is the caption.
        match = matches[0] if _looks_like_caption(value) else matches[-1]
        value = value[match.start() :]
    sentence = _sentences(value)
    if sentence:
        value = sentence[0]
    header = re.search(
        r"\s+(?:抗体\s+链|突变号(?:\s|$)|实验组(?:\s|$)|SEQ\s*ID\s*NO|序列编号(?:\s|$))",
        value,
        re.I,
    )
    if header:
        value = value[: header.start()]
    return value[:limit].rstrip()


def extract_document_blocks(pdf_path: str | Path) -> list[dict[str, Any]]:
    """Extract readable PDF text blocks with page coordinates and sentences."""

    fitz = _fitz()
    path = Path(pdf_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    document = fitz.open(path)
    output: list[dict[str, Any]] = []
    try:
        for page_index, page in enumerate(document):
            page_number = page_index + 1
            page_width = float(page.rect.width)
            page_height = float(page.rect.height)
            payload = page.get_text("dict")
            block_number = 0
            for raw_block in payload.get("blocks", []):
                if raw_block.get("type", 0) != 0:
                    continue
                kept_lines: list[tuple[str, list[float]]] = []
                for line in raw_block.get("lines", []):
                    spans = line.get("spans", [])
                    text = _clean_text("".join(str(span.get("text", "")) for span in spans))
                    box = _bbox(line.get("bbox", []))
                    if len(box) == 4 and page.rotation:
                        box = list(fitz.Rect(box) * page.rotation_matrix)
                    if not text or len(box) != 4:
                        continue
                    if _is_running_header_or_footer(text, box, page_height):
                        continue
                    kept_lines.append((text, box))
                if not kept_lines:
                    continue
                text = _clean_text(" ".join(line_text for line_text, _ in kept_lines))
                box = _union_bbox(line_box for _, line_box in kept_lines)
                if not text or len(box) != 4:
                    continue
                block_number += 1
                sentence_values = _sentences(text)
                block_id = f"p{page_number:04d}-b{block_number:04d}"
                output.append(
                    {
                        "block_id": block_id,
                        "page": page_number,
                        "page_width": round(page_width, 3),
                        "page_height": round(page_height, 3),
                        "bbox": box,
                        "text": text,
                        "normalized_text": _compact(text),
                        "block_type": "table_caption" if _looks_like_caption(text) else "paragraph",
                        "source": "pdf_text",
                        "sentences": [
                            {
                                "sentence_id": f"{block_id}-s{index:03d}",
                                "text": sentence,
                                # A sentence can span several PDF lines.  The containing
                                # block is the smallest reliable bbox without inventing
                                # character coordinates.
                                "bbox": box,
                            }
                            for index, sentence in enumerate(sentence_values, start=1)
                        ],
                    }
                )
    finally:
        document.close()
    return output


def _evidence(
    block: dict[str, Any],
    *,
    relation: str,
    confidence: float,
    text: str | None = None,
    evidence_id: str | None = None,
) -> dict[str, Any]:
    return {
        "evidence_id": evidence_id or block.get("block_id", ""),
        "page": block.get("page"),
        "bbox": block.get("bbox", []),
        "text": text if text is not None else block.get("text", ""),
        "source": block.get("source", "pdf_text"),
        "relation": relation,
        "confidence": round(float(confidence), 4),
    }


def _vertical_distance(block_box: list[float], table_box: list[float]) -> float:
    if _intersection_area(block_box, table_box) > 0:
        return 0.0
    if block_box[3] <= table_box[1]:
        return table_box[1] - block_box[3]
    if block_box[1] >= table_box[3]:
        return block_box[1] - table_box[3]
    return 0.0


def _caption_score(block: dict[str, Any], candidate: dict[str, Any]) -> float:
    block_box = _bbox(block.get("bbox", []))
    table_box = _bbox(candidate.get("bbox", []))
    if block.get("page") != candidate.get("page") or len(block_box) != 4 or len(table_box) != 4:
        return 0.0
    references = table_references(block.get("text", ""))
    if not references:
        return 0.0
    distance = _vertical_distance(block_box, table_box)
    if distance > 150:
        return 0.0
    caption_at_start = _looks_like_caption(block.get("text", ""))
    # Embedded captions occur when PyMuPDF combines the preceding prose and
    # the table title into one block.  Geometry keeps ordinary remote table
    # references from being promoted to captions.
    if not caption_at_start:
        mentions = _table_mentions(block.get("text", ""))
        # A combined prose+caption block repeats the table number: once in
        # "results are shown in Table N" and once at the title.  Requiring
        # that repetition rejects ordinary in-text references near a table.
        repeated = any(mentions.count(value) >= 2 for value in set(mentions))
        if distance > 15 or not repeated:
            return 0.0
    score = (0.35 if caption_at_start else 0.15) + 0.20
    score += 0.20 * max(0.0, 1.0 - distance / 150.0)
    score += 0.15 * _horizontal_overlap(block_box, table_box)
    if block_box[1] <= table_box[1] + 30:
        score += 0.10
    return min(1.0, score)


def _number_parts(value: Any) -> tuple[int, ...] | None:
    text = str(value or "")
    if not re.fullmatch(r"\d+(?:-\d+)?", text):
        return None
    return tuple(int(part) for part in text.split("-"))


def _between_number(left: Any, right: Any) -> str | None:
    before = _number_parts(left)
    after = _number_parts(right)
    if before is None or after is None or len(before) != len(after):
        return None
    if len(before) == 1 and after[0] == before[0] + 2:
        return str(before[0] + 1)
    if len(before) == 2 and before[0] == after[0] and after[1] == before[1] + 2:
        return f"{before[0]}-{before[1] + 1}"
    return None


def _infer_missing_table_numbers(
    contexts: list[dict[str, Any]], blocks: list[dict[str, Any]]
) -> None:
    """Conservatively fill one missing ordinal between two known captions."""

    ordered = sorted(contexts, key=lambda item: (item.get("page") or 0, item.get("bbox", [0, 0])[1]))
    groups: list[list[dict[str, Any]]] = []
    for context in ordered:
        if not groups or groups[-1][0].get("logical_table_id") != context.get("logical_table_id"):
            groups.append([context])
        else:
            groups[-1].append(context)
    for index in range(1, len(groups) - 1):
        group = groups[index]
        if any(item.get("table_number") for item in group):
            continue
        inferred = _between_number(
            groups[index - 1][0].get("table_number"),
            groups[index + 1][0].get("table_number"),
        )
        if not inferred:
            continue
        logical_id = f"table-{inferred.lower()}"
        for item in group:
            item["table_number"] = inferred
            item["logical_table_id"] = logical_id
            item["context_status"] = "review"
        first = group[0]
        page = int(first.get("page") or 0)
        table_box = _bbox(first.get("bbox", []))
        nearby = []
        for block in blocks:
            if block.get("page") != page:
                continue
            box = _bbox(block.get("bbox", []))
            if len(box) == 4 and box[3] <= table_box[1] + 4:
                distance = max(0.0, table_box[1] - box[3])
                if distance <= 45 and len(_clean_text(block.get("text", ""))) <= 300:
                    nearby.append((distance, block))
        if nearby and not first.get("caption"):
            _, block = min(nearby, key=lambda pair: pair[0])
            first["caption"] = _evidence(
                block,
                relation="caption",
                confidence=0.55,
                text=_caption_excerpt(block.get("text", "")),
            )


def _block_overlaps_a_table(
    block: dict[str, Any], candidates_by_page: dict[int, list[dict[str, Any]]]
) -> bool:
    block_box = _bbox(block.get("bbox", []))
    return any(
        _overlap_fraction(block_box, _bbox(candidate.get("bbox", []))) >= 0.18
        for candidate in candidates_by_page.get(int(block.get("page", 0)), [])
    )


def _nearby_blocks(
    candidate: dict[str, Any],
    blocks: list[dict[str, Any]],
    candidates_by_page: dict[int, list[dict[str, Any]]],
    caption_block_id: str | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    page = int(candidate.get("page") or 0)
    table_box = _bbox(candidate.get("bbox", []))
    if not page or len(table_box) != 4:
        return [], []
    before: list[tuple[float, dict[str, Any]]] = []
    after: list[tuple[float, dict[str, Any]]] = []
    notes: list[tuple[float, dict[str, Any]]] = []
    for block in blocks:
        if block.get("page") != page or block.get("block_id") == caption_block_id:
            continue
        if block.get("block_type") == "table_caption" or _block_overlaps_a_table(block, candidates_by_page):
            continue
        box = _bbox(block.get("bbox", []))
        if len(box) != 4:
            continue
        if box[3] <= table_box[1] + 3:
            distance = max(0.0, table_box[1] - box[3])
            if distance <= 180:
                before.append((distance, block))
        elif box[1] >= table_box[3] - 3:
            distance = max(0.0, box[1] - table_box[3])
            if distance <= 140:
                if FOOTNOTE_RE.match(_clean_text(block.get("text", ""))):
                    notes.append((distance, block))
                else:
                    after.append((distance, block))
    selected_before = [item for _, item in sorted(before, key=lambda pair: pair[0])[:2]]
    selected_after = [item for _, item in sorted(after, key=lambda pair: pair[0])[:2]]
    context = []
    for block in selected_before:
        distance = max(0.0, table_box[1] - _bbox(block["bbox"])[3])
        context.append(
            _evidence(block, relation="preceding_paragraph", confidence=max(0.55, 0.9 - distance / 400.0))
        )
    for block in selected_after:
        distance = max(0.0, _bbox(block["bbox"])[1] - table_box[3])
        context.append(
            _evidence(block, relation="following_paragraph", confidence=max(0.5, 0.85 - distance / 400.0))
        )
    footnotes = [
        _evidence(block, relation="footnote", confidence=max(0.6, 0.9 - distance / 300.0))
        for distance, block in sorted(notes, key=lambda pair: pair[0])[:3]
    ]
    return context, footnotes


def _nearest_table_after(
    block: dict[str, Any], contexts: list[dict[str, Any]]
) -> dict[str, Any] | None:
    page = int(block.get("page") or 0)
    box = _bbox(block.get("bbox", []))
    options: list[tuple[float, dict[str, Any]]] = []
    for context in contexts:
        table_page = int(context.get("page") or 0)
        table_box = _bbox(context.get("bbox", []))
        if table_page not in {page, page + 1} or len(table_box) != 4:
            continue
        if table_page == page and table_box[1] < box[3]:
            continue
        distance = (table_page - page) * 1000.0 + max(0.0, table_box[1] - box[3])
        options.append((distance, context))
    return min(options, key=lambda pair: pair[0])[1] if options else None


def _assign_logical_tables(contexts: list[dict[str, Any]], blocks: list[dict[str, Any]]) -> None:
    page_heights = {int(block["page"]): float(block.get("page_height", 842.0)) for block in blocks}
    ordered = sorted(contexts, key=lambda item: (item.get("page") or 0, item.get("bbox", [0, 0])[1]))
    for context in ordered:
        number = context.get("table_number")
        context["logical_table_id"] = f"table-{str(number).lower()}" if number else context["table_id"]
        context["continuation_of"] = None
        context["continuation_confidence"] = None
    for index, context in enumerate(ordered):
        if index == 0:
            continue
        previous = ordered[index - 1]
        same_number = bool(
            context.get("table_number")
            and context.get("table_number") == previous.get("table_number")
        )
        caption_text = (context.get("caption") or {}).get("text", "")
        explicit_continuation = bool(CONTINUATION_RE.search(_compact(caption_text)))
        page = int(context.get("page") or 0)
        previous_page = int(previous.get("page") or 0)
        table_box = _bbox(context.get("bbox", []))
        previous_box = _bbox(previous.get("bbox", []))
        page_height = page_heights.get(page, 842.0)
        previous_height = page_heights.get(previous_page, 842.0)
        geometric_continuation = bool(
            not context.get("caption")
            and page == previous_page + 1
            and len(table_box) == 4
            and len(previous_box) == 4
            and table_box[1] <= page_height * 0.22
            and previous_box[3] >= previous_height * 0.80
        )
        if same_number or explicit_continuation or geometric_continuation:
            context["logical_table_id"] = previous["logical_table_id"]
            context["continuation_of"] = previous["table_id"]
            context["continuation_confidence"] = 0.95 if same_number or explicit_continuation else 0.65


def link_table_contexts(
    candidates: list[dict[str, Any]], blocks: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Link captions, adjacent prose, references, and continuations to tables."""

    candidates_by_page: dict[int, list[dict[str, Any]]] = {}
    for candidate in candidates:
        page = int(candidate.get("page") or 0)
        if page:
            candidates_by_page.setdefault(page, []).append(candidate)

    caption_blocks = [block for block in blocks if table_references(block.get("text", ""))]
    pair_scores: list[tuple[float, str, str, dict[str, Any], dict[str, Any]]] = []
    for block in caption_blocks:
        for candidate in candidates_by_page.get(int(block.get("page") or 0), []):
            score = _caption_score(block, candidate)
            if score >= 0.55:
                pair_scores.append(
                    (score, str(candidate.get("table_id")), str(block.get("block_id")), candidate, block)
                )
    assigned_tables: dict[str, tuple[float, dict[str, Any]]] = {}
    assigned_blocks: set[str] = set()
    for score, table_id, block_id, _, block in sorted(pair_scores, key=lambda item: item[0], reverse=True):
        if table_id in assigned_tables or block_id in assigned_blocks:
            continue
        assigned_tables[table_id] = (score, block)
        assigned_blocks.add(block_id)

    contexts: list[dict[str, Any]] = []
    for candidate in candidates:
        table_id = str(candidate.get("table_id", ""))
        caption_pair = assigned_tables.get(table_id)
        caption: dict[str, Any] | None = None
        table_number: str | None = None
        caption_block_id: str | None = None
        if caption_pair:
            score, block = caption_pair
            caption_block_id = str(block.get("block_id"))
            references = table_references(block.get("text", ""))
            table_number = references[0] if references else None
            caption = _evidence(
                block,
                relation="caption",
                confidence=score,
                text=_caption_excerpt(block.get("text", "")),
            )
        nearby, footnotes = _nearby_blocks(
            candidate, blocks, candidates_by_page, caption_block_id
        )
        contexts.append(
            {
                "table_id": table_id,
                "image": candidate.get("image", ""),
                "page": candidate.get("page"),
                "bbox": candidate.get("bbox", []),
                "table_number": table_number,
                "logical_table_id": table_id,
                "caption": caption,
                "nearby_context": nearby,
                "referring_sentences": [],
                "footnotes": footnotes,
                "continuation_of": None,
                "continuation_confidence": None,
                "context_image": None,
                "context_status": "linked" if caption else ("review" if nearby or footnotes else "missing"),
            }
        )

    _assign_logical_tables(contexts, blocks)
    _infer_missing_table_numbers(contexts, blocks)

    by_number: dict[str, list[dict[str, Any]]] = {}
    for context in contexts:
        if context.get("table_number"):
            by_number.setdefault(str(context["table_number"]), []).append(context)

    caption_evidence_ids = {
        str(context["caption"].get("evidence_id"))
        for context in contexts
        if context.get("caption")
    }
    for block in blocks:
        if _block_overlaps_a_table(block, candidates_by_page):
            continue
        for sentence in block.get("sentences", []):
            sentence_text = str(sentence.get("text", ""))
            references = table_references(sentence_text)
            sentence_id = str(sentence.get("sentence_id") or block.get("block_id"))
            if (
                str(block.get("block_id")) in caption_evidence_ids
                and _looks_like_caption(sentence_text)
            ):
                continue
            for reference in references:
                options = by_number.get(reference, [])
                if not options:
                    continue
                target = min(
                    options,
                    key=lambda item: (
                        abs(int(item.get("page") or 0) - int(block.get("page") or 0)),
                        abs(_bbox(item.get("bbox", [0, 0, 0, 0]))[1] - _bbox(block.get("bbox", [0, 0, 0, 0]))[3]),
                    ),
                )
                evidence = _evidence(
                    block,
                    relation="explicit_reference",
                    confidence=0.98,
                    text=sentence_text,
                    evidence_id=sentence_id,
                )
                if evidence["evidence_id"] != ((target.get("caption") or {}).get("evidence_id")):
                    target["referring_sentences"].append(evidence)
                    target["context_status"] = "linked"
            if not references and IMPLICIT_TABLE_RE.search(_compact(sentence_text)):
                target = _nearest_table_after(block, contexts)
                if target:
                    target["referring_sentences"].append(
                        _evidence(
                            block,
                            relation="implicit_reference",
                            confidence=0.72,
                            text=sentence_text,
                            evidence_id=sentence_id,
                        )
                    )
                    if target["context_status"] == "missing":
                        target["context_status"] = "review"

    for context in contexts:
        deduplicated: dict[tuple[str, str], dict[str, Any]] = {}
        for evidence in context["referring_sentences"]:
            deduplicated[(str(evidence.get("evidence_id")), str(evidence.get("text")))] = evidence
        context["referring_sentences"] = list(deduplicated.values())
        if context.get("continuation_confidence") and context["continuation_confidence"] < 0.75:
            context["context_status"] = "review"
    return contexts


def render_context_images(
    pdf_path: str | Path,
    contexts: list[dict[str, Any]],
    output_dir: str | Path,
    *,
    dpi: int = 180,
    padding_pt: float = 12.0,
) -> dict[str, str]:
    """Render one auditable same-page context image per physical table."""

    fitz = _fitz()
    pdf_path = Path(pdf_path).expanduser().resolve()
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    document = fitz.open(pdf_path)
    written: dict[str, str] = {}
    try:
        for context in contexts:
            page_number = int(context.get("page") or 0)
            table_box = _bbox(context.get("bbox", []))
            if not page_number or page_number > len(document) or len(table_box) != 4:
                continue
            boxes = [table_box]
            for key in ("nearby_context", "footnotes"):
                boxes.extend(
                    _bbox(item.get("bbox", []))
                    for item in context.get(key, [])
                    if item.get("page") == page_number
                )
            caption = context.get("caption")
            if caption and caption.get("page") == page_number:
                boxes.append(_bbox(caption.get("bbox", [])))
            clip_box = _union_bbox(boxes)
            page = document.load_page(page_number - 1)
            clip = fitz.Rect(
                max(page.rect.x0, clip_box[0] - padding_pt),
                max(page.rect.y0, clip_box[1] - padding_pt),
                min(page.rect.x1, clip_box[2] + padding_pt),
                min(page.rect.y1, clip_box[3] + padding_pt),
            )
            pixmap = page.get_pixmap(matrix=fitz.Matrix(dpi / 72.0, dpi / 72.0), clip=clip, alpha=False)
            safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(context.get("table_id", "table")))
            destination = output_dir / f"context-{safe_id}.png"
            pixmap.save(destination)
            context["context_image"] = str(destination)
            written[str(context.get("table_id"))] = str(destination)
    finally:
        document.close()
    return written


def context_text(context: dict[str, Any]) -> str:
    """Flatten only linked prose for deterministic table triage."""

    values: list[str] = []
    if context.get("caption"):
        values.append(str(context["caption"].get("text", "")))
    for key in ("nearby_context", "referring_sentences", "footnotes"):
        values.extend(str(item.get("text", "")) for item in context.get(key, []))
    return " ".join(value for value in values if value)
