"""Ground HTML cells in existing OCR rectangles, without inferring a grid of boxes.

``bbox`` stays in crop pixels for legacy consumers. Explicit ``bbox_pdf`` and
``bbox_source`` prevent callers from confusing crop, working and source frames.
Only complete literal OCR text is matched; substrings never get invented boxes.
"""

from collections import Counter, defaultdict
from difflib import SequenceMatcher
import copy
import math
import re


LOCATION_FIELDS = (
    "bbox_crop", "bbox_pdf", "bbox_source", "coordinate_space", "precision",
    "location_status", "location_method", "geometry_source_refs", "confidence",
    "ocr_lines", "geometry_alignment",
)


def valid_box(box):
    return (isinstance(box, (list, tuple)) and len(box) == 4
            and all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in box)
            and box[0] < box[2] and box[1] < box[3])


def transform_box(box, matrix):
    if not valid_box(box) or not isinstance(matrix, (list, tuple)) or len(matrix) != 6:
        return []
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in matrix):
        return []
    a, b, c, d, e, f = matrix
    points = [(a * x + c * y + e, b * x + d * y + f)
              for x, y in ((box[0], box[1]), (box[2], box[1]), (box[2], box[3]), (box[0], box[3]))]
    result = [min(p[0] for p in points), min(p[1] for p in points),
              max(p[0] for p in points), max(p[1] for p in points)]
    return result if valid_box(result) else []


def _inverse(matrix):
    a, b, c, d, e, f = matrix
    determinant = a * d - b * c
    if not determinant:
        return None
    return [d / determinant, -b / determinant, -c / determinant, a / determinant,
            (c * f - d * e) / determinant, (b * e - a * f) / determinant]


def crop_matrix(metadata):
    matrix = metadata.get("crop_to_pdf")
    if transform_box([0, 0, 1, 1], matrix) and _inverse(matrix):
        return list(matrix)
    box = metadata.get("bbox_padded")
    width, height = metadata.get("width_px"), metadata.get("height_px")
    if (valid_box(box) and all(isinstance(v, (int, float)) and not isinstance(v, bool)
                             and math.isfinite(v) and v > 0 for v in (width, height))):
        return [(box[2] - box[0]) / width, 0, 0, (box[3] - box[1]) / height, box[0], box[1]]
    return None


def _literal(text):
    # Preserve case, punctuation and residue characters. Ignore OCR whitespace.
    return re.sub(r"\s+", "", str(text or ""))


def _overlap(a, b, axis):
    return max(0, min(a[axis + 2], b[axis + 2]) - max(a[axis], b[axis])) / min(
        a[axis + 2] - a[axis], b[axis + 2] - b[axis])


def _wrapped_cells(cells, tokens):
    """Use observed column anchors and row labels to locate wrapped text.

    Approximate text alignment is used only inside an explicitly addressed row,
    and is labelled ocr_row. It never changes the source or exported residues.
    """
    def store(cell, group, precision, score=1.0):
        boxes = [t["pdf"] for t in group]
        cell.update(bbox_pdf=[min(b[0] for b in boxes), min(b[1] for b in boxes),
                              max(b[2] for b in boxes), max(b[3] for b in boxes)],
                    location_status="located", precision=precision,
                    location_method="row_anchored_ocr" if precision == "ocr_row" else "literal_wrapped_ocr",
                    geometry_source_refs=[t["ref"] for t in group],
                    geometry_alignment={"text_verified": precision != "ocr_row", "similarity": score},
                    ocr_lines=[{"text": t["raw_text"], "bbox_pdf": t["pdf"], "bbox_crop": t["crop"],
                                "source_ref": t["ref"]} for t in group])
        scores = [t["confidence"] for t in group if isinstance(t["confidence"], (int, float))]
        cell["confidence"] = min(scores) if len(scores) == len(group) else None

    ordered = sorted((t for t in tokens if t["pdf"]), key=lambda t: (t["pdf"][1], t["pdf"][0]))
    counts = Counter(_literal(cell["text"]) for cell in cells)
    for cell in cells:
        target = _literal(cell["text"])
        if cell.get("bbox_pdf") or not target or counts[target] != 1 or cell.get("colspan", 1) != 1:
            continue
        matches = []
        for start, first in enumerate(ordered):
            if not target.startswith(first["text"]):
                continue
            group, joined = [], ""
            for token in ordered[start:]:
                if _overlap(first["pdf"], token["pdf"], 0) < 0.5:
                    continue
                if group and token["pdf"][1] - group[-1]["pdf"][3] > 3 * (group[-1]["pdf"][3] - group[-1]["pdf"][1]):
                    break
                group.append(token)
                joined += token["text"]
                if joined == target:
                    matches.append(group)
                    break
                if not target.startswith(joined):
                    break
        if len(matches) == 1:
            store(cell, matches[0], "ocr_lines")

    positioned = [c for c in cells if c.get("bbox_pdf") and c.get("colspan", 1) == 1]
    columns = sorted({c["column"] for c in cells})
    if columns != list(range(len(columns))) or len(columns) < 2:
        return
    cuts = []
    for column in columns[:-1]:
        boxes = [c["bbox_pdf"] for c in positioned if c["column"] == column]
        if not boxes:
            return
        cuts.append(max(b[2] for b in boxes))
    if cuts != sorted(cuts):
        return
    streams = defaultdict(list)
    for token in ordered:
        # Detection rectangles can include a neighboring rule/SEQ-ID edge.
        # A long residue line still belongs to the column containing its body.
        body = re.sub(r"^(?:SEQID(?:NO[.:：]?)?|NO[.:：]?)\d+", "", token["text"], flags=re.I)
        x = ((token["pdf"][0] + token["pdf"][2]) / 2
             if re.fullmatch(r"[A-Z0-9]{20,}", body) else token["pdf"][0])
        column = sum(x >= cut for cut in cuts)
        streams[column].append(token)

    # Labels and SEQ IDs are often two OCR lines in a single physical cell.
    for cell in cells:
        if cell.get("bbox_pdf") or cell.get("colspan", 1) != 1:
            continue
        target, stream = _literal(cell["text"]), streams[cell["column"]]
        if not target:
            continue
        matches = []
        for start in range(len(stream)):
            joined = ""
            for end in range(start, len(stream)):
                if end > start and stream[end]["pdf"][1] - stream[end - 1]["pdf"][3] > 3 * (
                        stream[end - 1]["pdf"][3] - stream[end - 1]["pdf"][1]):
                    break
                joined += stream[end]["text"]
                if joined == target:
                    matches.append(stream[start:end + 1])
                    break
                if not target.startswith(joined):
                    break
        if len(matches) == 1:
            # Repeated cells require a different column's addressed row anchor.
            peers = [c for c in cells if c["row"] == cell["row"] and c["column"] != cell["column"] and c.get("bbox_pdf")]
            if sum(_literal(c["text"]) == target for c in cells) > 1 and not peers:
                continue
            group = matches[0]
            low, high = group[0]["pdf"][1], group[-1]["pdf"][3]
            if peers and not any(low <= (c["bbox_pdf"][1] + c["bbox_pdf"][3]) / 2 <= high for c in peers):
                continue
            store(cell, group, "ocr_lines")

    for cell in cells:
        target, stream = _literal(cell["text"]), streams[cell["column"]]
        target = _literal(re.sub(r"[（(][^()（）]*[)）]", "", cell["text"])).replace("_", "")
        stream = list(stream)
        if cell.get("bbox_pdf") or cell.get("colspan", 1) != 1 or not re.fullmatch(r"[A-Z]{20,}", target):
            continue
        # Joined labels keep their actual whole-line box, never an invented
        # sequence-only rectangle. This path always reports approximate rows.
        for token in ordered:
            tail = re.sub(r"^(?:SEQID(?:NO[.:：]?)?|NO[.:：]?)\d+", "", token["text"], flags=re.I)
            if tail != token["text"] and re.fullmatch(r"[A-Z]{20,}", tail) and token not in stream:
                stream.append(token)
        stream.sort(key=lambda t: (t["pdf"][1], t["pdf"][0]))
        peers = [c for c in cells if c["row"] == cell["row"] and c["column"] != cell["column"]
                 and c.get("bbox_pdf") and c.get("rowspan", 1) == 1]
        if not peers:
            continue
        centers = [(c["bbox_pdf"][1] + c["bbox_pdf"][3]) / 2 for c in peers]
        other_centers = [(c["bbox_pdf"][1] + c["bbox_pdf"][3]) / 2 for c in cells
                         if c["column"] != cell["column"] and c["row"] != cell["row"]
                         and c.get("bbox_pdf") and c.get("rowspan", 1) == 1]
        lower = max((y for y in other_centers if y < min(centers)), default=-math.inf)
        upper = min((y for y in other_centers if y > max(centers)), default=math.inf)
        stream = [t for t in stream if t["pdf"][1] > lower and t["pdf"][3] < upper]
        options = []
        for start in range(len(stream)):
            joined = ""
            for end in range(start, len(stream)):
                line = stream[end]
                line_text = re.sub(r"^(?:SEQID(?:NO[.:：]?)?|NO[.:：]?)\d+", "", line["text"], flags=re.I)
                if not re.fullmatch(r"[A-Z0-9₀-₉]+", line_text):
                    break
                if end > start and line["pdf"][1] - stream[end - 1]["pdf"][3] > 3 * (
                        stream[end - 1]["pdf"][3] - stream[end - 1]["pdf"][1]):
                    break
                joined += line_text
                if len(joined) > len(target) * 1.35:
                    break
                low, high = stream[start]["pdf"][1], line["pdf"][3]
                if len(joined) < len(target) * 0.7 or not all(low <= center <= high for center in centers):
                    continue
                if any(low <= center <= high for center in other_centers):
                    continue
                score = SequenceMatcher(None, target, joined, autojunk=False).ratio()
                if score >= 0.8:
                    options.append((score, stream[start:end + 1]))
        options.sort(key=lambda pair: -pair[0])
        if options and (len(options) == 1 or options[0][0] - options[1][0] >= 0.015):
            store(cell, options[0][1], "ocr_row", options[0][0])


def locate_grid(grid, evidence, metadata, *, page_lines=(), working_to_source=None):
    """Mutate a freshly parsed grid, retaining physical addresses through spans.

    Repeated text requires a located peer in its row plus column/reading-order
    constraints. Ties and conflicting OCR layouts remain unlocated. No model,
    rendering, native PDF extraction, or extra OCR is performed here.
    """
    if not grid:
        return
    cells = [cell for row in grid["physical"] for cell in row]
    matrix = crop_matrix(metadata)
    inverse = _inverse(matrix) if matrix else None
    region = metadata.get("bbox")
    previous = defaultdict(list)
    for cell in copy.deepcopy(evidence.get("cells", [])):
        previous[cell.get("ref") or cell.get("source_ref")].append(cell)

    tokens, seen = [], set()
    def admit(item, default_space, ref):
        text = item.get("text", item.get("block_content", ""))
        if not isinstance(text, str) or not _literal(text) or re.search(r"<\s*(?:table|tr|td|th)\b", text, re.I):
            return
        # A prose/layout block does not imply an individual text-line rectangle.
        if item.get("granularity") == "block" or item.get("block_label") in {"table", "figure"}:
            return
        box = item.get("bbox", item.get("block_bbox"))
        space = item.get("coordinate_space", default_space)
        if not valid_box(box):
            return
        pdf = list(box) if space == "pdf_points" else transform_box(box, matrix) if space == "crop_pixels" else []
        if valid_box(region) and pdf:
            # Exclude captions, footnotes and neighbors outside the detected table.
            if not (region[0] <= pdf[0] and region[1] <= pdf[1] and pdf[2] <= region[2] and pdf[3] <= region[3]):
                return
        crop = list(box) if space == "crop_pixels" else transform_box(box, inverse)
        # Match in one common frame even when no crop transform is available.
        key = (_literal(text), tuple(round(v, 4) for v in (pdf or crop)), bool(pdf))
        if key in seen or not (pdf or crop):
            return
        seen.add(key)
        tokens.append({"text": _literal(text), "raw_text": text, "box": pdf or crop, "pdf": pdf, "crop": crop,
                       "frame": "pdf" if pdf else "crop", "ref": ref, "confidence": item.get("confidence")})

    for index, item in enumerate(evidence.get("lines", [])):
        admit(item, "crop_pixels", item.get("source_ref") or f"{evidence.get('table_id')}:ocr:{index}")
    for index, item in enumerate(evidence.get("blocks", [])):
        # Only explicitly line/token-grained blocks may serve as text anchors.
        if item.get("granularity") in {"line", "token"}:
            admit(item, "crop_pixels", f"{evidence.get('table_id')}:ocr-block:{index}")
    for item in page_lines:
        if valid_box(region):
            admit(item, "pdf_points", item.get("source_ref") or "ocr:" + str(item.get("line_id", "")))

    counts = Counter(_literal(cell["text"]) for cell in cells)
    candidates = {cell["ref"]: [i for i, token in enumerate(tokens) if token["text"] == _literal(cell["text"])]
                  for cell in cells}
    matched, used = {}, set()
    for cell in cells:
        for name in ("bbox", *LOCATION_FIELDS):
            cell.pop(name, None)
        cell.update(bbox=[], bbox_crop=[], bbox_pdf=[], precision="unlocated", location_status="unlocated")
        existing = previous[cell["ref"]]
        if len(existing) == 1 and _literal(existing[0].get("text")) == _literal(cell["text"]):
            old = existing[0]
            crop = old.get("bbox_crop") or (old.get("bbox") if old.get("coordinate_space") == "crop_pixels" else [])
            pdf = old.get("bbox_pdf") or (old.get("bbox") if old.get("coordinate_space") == "pdf_points" else [])
            crop, pdf = (list(crop) if valid_box(crop) else []), (list(pdf) if valid_box(pdf) else [])
            if crop and pdf and matrix:
                projected = transform_box(crop, matrix)
                if not projected or any(abs(a - b) > 0.01 for a, b in zip(projected, pdf)):
                    continue
            pdf = pdf or transform_box(crop, matrix)
            if valid_box(region) and pdf and not (region[0] <= pdf[0] and region[1] <= pdf[1]
                                                  and pdf[2] <= region[2] and pdf[3] <= region[3]):
                continue
            if crop or pdf:
                cell.update({key: copy.deepcopy(old[key]) for key in LOCATION_FIELDS if key in old})
                cell.update(bbox_crop=crop or transform_box(pdf, inverse), bbox_pdf=pdf or transform_box(crop, matrix),
                            precision=old.get("precision") if old.get("precision") not in {None, "unlocated"} else "ocr_cell",
                            location_status="located",
                            location_method=old.get("location_method", "explicit_cell"))
    def compatible(cell, token, anchors):
        row_anchor = False
        for peer, anchor in anchors:
            if anchor["frame"] != token["frame"]:
                return False, False
            box, other = token["box"], anchor["box"]
            if cell["row"] == peer["row"] and cell.get("rowspan", 1) == peer.get("rowspan", 1) == 1:
                if _overlap(box, other, 1) < 0.5:
                    return False, False
                row_anchor = True
                if cell["column"] < peer["column"] and (box[0] + box[2]) >= (other[0] + other[2]):
                    return False, False
                if cell["column"] > peer["column"] and (box[0] + box[2]) <= (other[0] + other[2]):
                    return False, False
            elif cell["row"] + cell.get("rowspan", 1) <= peer["row"]:
                if (box[1] + box[3]) >= (other[1] + other[3]):
                    return False, False
            elif peer["row"] + peer.get("rowspan", 1) <= cell["row"]:
                if (box[1] + box[3]) <= (other[1] + other[3]):
                    return False, False
            if cell["column"] == peer["column"] and cell.get("colspan", 1) == peer.get("colspan", 1) == 1:
                if _overlap(box, other, 0) < 0.25:
                    return False, False
        return True, row_anchor

    fixed = [(cell, {"box": cell.get("bbox_pdf") or cell["bbox_crop"],
                     "frame": "pdf" if cell.get("bbox_pdf") else "crop"})
             for cell in cells if cell.get("bbox_pdf") or cell.get("bbox_crop")]
    seeds = [(cell, tokens[candidates[cell["ref"]][0]]) for cell in cells
             if not cell.get("bbox_pdf") and not cell.get("bbox_crop") and _literal(cell["text"])
             and counts[_literal(cell["text"])] == 1 and len(candidates[cell["ref"]]) == 1]
    # Assess all anchors together so conflicting rows cannot depend on loop order.
    for cell, token in seeds:
        if compatible(cell, token, fixed + [(p, t) for p, t in seeds if p is not cell])[0]:
            matched[cell["ref"]] = token
            used.add(candidates[cell["ref"]][0])
    while True:
        anchors = fixed + [(cell, matched[cell["ref"]]) for cell in cells if cell["ref"] in matched]
        proposals = []
        for cell in cells:
            if cell["ref"] in matched or cell.get("bbox_pdf") or cell.get("bbox_crop") or not _literal(cell["text"]):
                continue
            options = [i for i in candidates[cell["ref"]] if i not in used
                       and compatible(cell, tokens[i], anchors) == (True, True)]
            if len(options) == 1:
                proposals.append((cell, options[0]))
        claims = Counter(index for _, index in proposals)
        accepted = [(cell, index) for cell, index in proposals if claims[index] == 1
                    and compatible(cell, tokens[index], [(p, tokens[i]) for p, i in proposals if p is not cell])[0]]
        if not accepted:
            break
        for cell, index in accepted:
            matched[cell["ref"]] = tokens[index]
            used.add(index)
    for cell in cells:
        token = matched.get(cell["ref"])
        if token:
            cell.update(bbox_crop=token["crop"], bbox_pdf=token["pdf"], precision="ocr_text",
                        location_status="located", location_method="literal_ocr_with_layout",
                        geometry_source_refs=[token["ref"]], confidence=token["confidence"])
    _wrapped_cells(cells, tokens)
    for cell in cells:
        if cell["bbox_pdf"] and not cell["bbox_crop"]:
            cell["bbox_crop"] = transform_box(cell["bbox_pdf"], inverse)
        cell["bbox"] = cell["bbox_crop"]
        cell["coordinate_space"] = "crop_pixels"
        if working_to_source is not None:
            cell.pop("bbox_source", None)
            source_box = transform_box(cell["bbox_pdf"], working_to_source)
            if source_box:
                cell["bbox_source"] = source_box


def geometry_fields(cell):
    """Fields carried through adapters without changing the literal cell text."""
    return {key: copy.deepcopy(cell[key]) for key in ("bbox", *LOCATION_FIELDS, "sequence_ocr", "vl_text") if key in cell}
