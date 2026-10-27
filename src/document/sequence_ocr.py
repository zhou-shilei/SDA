"""Refill sequence cells from addressed raster lines using PP-OCRv6 medium.

PPVL owns table structure. Its transcription is retained as vl_text; residue
characters come from new, auditable line recognition, never from page OCR.
"""
from __future__ import annotations

import copy
from functools import lru_cache
import hashlib
import html as html_text
import math
import os
from pathlib import Path
import re
from statistics import median
from time import perf_counter

from lxml import html

from .cell_geometry import crop_matrix, locate_grid, transform_box, valid_box

VERSION = "ppvl16-ppocrv6-sequence-v1"
REVISION = 5
AA = frozenset("ACDEFGHIKLMNPQRSTVWY")


def plain(text):
    text = html_text.unescape(str(text)).replace("\\n", "\n")
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    before = None
    while before != text:
        before = text
        text = re.sub(r"\\(?:underline|text|mathrm|mathbf)\{([^{}]*)\}", r"\1", text)
    return text.replace("$", "")


def sequence_body(text, min_length=12):
    """A lexical candidate, not a chain/boundary or biological identity decision."""
    text = plain(text)
    match = re.search(r"\(?\s*SEQ\s*ID\b|\(\s*SEQ\b|[（(]\s*(?:下[划劃]线|加粗|粗体|注[：:]|underlin|bold)", text, re.I)
    body, suffix = (text[:match.start()], text[match.start():]) if match else (text, "")
    value = re.sub(r"\s+", "", body)
    if (len(value) < min_length or not re.fullmatch(r"[A-Z0-9_]+", value)
            or sum(c in AA for c in value) / len(value) < .75):
        return None
    return value, suffix


def sequence_candidates(grid):
    """Short CDR/FR strings require an explicit sequence-column heading."""
    headers = {}
    for row in (grid or {}).get("physical", [])[:3]:
        for cell in row:
            if (len(cell["text"]) < 80 and not sequence_body(cell["text"])
                    and re.search(r"amino.?acid|氨基酸|\bsequence\b|\b[HL]?(?:CDR|FR)[1-4]?\b", cell["text"], re.I)
                    and not re.search(r"SEQ\s*ID", cell["text"], re.I)):
                headers[cell["column"]] = cell["row"]
    result = {}
    for row in (grid or {}).get("physical", []):
        for cell in row:
            minimum = 3 if cell["row"] > headers.get(cell["column"], math.inf) else 12
            body = sequence_body(cell["text"], min_length=minimum)
            if body:
                result[cell["ref"]] = body
    return result


LEADING_SEQ_ID = re.compile(r"^\s*SEQ\s*ID(?:\s*NO[.:：]?)?\s*[:：]?\s*\d+\s*", re.I)


def sequence_line(text):
    text = LEADING_SEQ_ID.sub("", plain(text))
    text = re.split(r"\(?\s*SEQ\s*ID|\(\s*SEQ|[（(]\s*(?:下[划劃]线|加粗|粗体|注[：:]|underlin|bold)",
                    plain(text), maxsplit=1, flags=re.I)[0]
    value = re.sub(r"\s+", "", text)
    return bool(re.fullmatch(r"[A-Z0-9]+", value) and sum(c in AA for c in value) / len(value) >= .7)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@lru_cache(maxsize=2)
def models(device, threads, root):
    from paddleocr import PaddleOCR, TextRecognition, DocImgOrientationClassification
    root = Path(root)
    common = {"device": device, "enable_mkldnn": False, "cpu_threads": threads}
    ocr = PaddleOCR(text_detection_model_name="PP-OCRv6_medium_det",
                    text_detection_model_dir=str(root / "PP-OCRv6_medium_det"),
                    text_recognition_model_name="PP-OCRv6_medium_rec",
                    text_recognition_model_dir=str(root / "PP-OCRv6_medium_rec"),
                    use_doc_orientation_classify=False, use_doc_unwarping=False,
                    use_textline_orientation=False, text_det_limit_side_len=3000,
                    text_det_limit_type="max", text_recognition_batch_size=1, **common)
    rec = TextRecognition(model_name="PP-OCRv6_medium_rec", model_dir=str(root / "PP-OCRv6_medium_rec"), **common)
    ori = DocImgOrientationClassification(model_name="PP-LCNet_x1_0_doc_ori",
                                          model_dir=str(root / "PP-LCNet_x1_0_doc_ori"), **common)
    return ocr, rec, ori


def rotation_to_source(angle, width, height):
    return {0: [1, 0, 0, 1, 0, 0], 90: [0, 1, -1, 0, width, 0],
            180: [-1, 0, 0, -1, width, height], 270: [0, -1, 1, 0, 0, height]}[angle]


def line_crop(image, box, *, min_left=None, max_right=None):
    """Expand before trimming; faint/isolated Q tails must survive line detection."""
    import numpy as np
    from PIL import ImageOps
    x1, y1, x2, y2 = box
    padding = max(8, min(12, math.ceil((y2 - y1) * .2)))
    bounds = [max(0, math.floor(x1) - padding), max(0, math.floor(y1) - padding),
              min(image.width, math.ceil(x2) + padding), min(image.height, math.ceil(y2) + padding)]
    if min_left is not None:
        bounds[0] = max(bounds[0], math.ceil(min_left))
    if max_right is not None:
        bounds[2] = min(bounds[2], math.floor(max_right))
    crop = image.crop(tuple(bounds))
    ink = np.asarray(crop.convert("L")) < 180
    ys, xs = np.where(ink)
    if not len(xs):
        raise ValueError("Sequence crop contains no visible ink")
    # Preserve all foreground, including glyph descenders, then add white margin.
    tight = [int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1]
    bounds = [bounds[0] + tight[0], bounds[1] + tight[1], bounds[0] + tight[2], bounds[1] + tight[3]]
    crop = image.crop(tuple(bounds))
    return ImageOps.expand(crop, border=8, fill="white"), bounds


def sequence_column_left(image, cell, grid, lines):
    """Find an observed column rule after the neighboring label, in pixels.

    No residue text is edited. The rule is required to continue through most of
    the cell's height, so a glyph stem cannot be mistaken for a table border.
    """
    import numpy as np
    peers = [c for row in grid["physical"] for c in row
             if c["column"] < cell["column"] and valid_box(c.get("bbox_pdf"))]
    if not peers:
        return None
    previous = max(c["column"] for c in peers)
    peers = [c for c in peers if c["column"] == previous]
    same_row = [c for c in peers if c["row"] == cell["row"]]
    left = max(c["bbox_pdf"][2] for c in (same_row or peers))
    height = median(l["bbox"][3] - l["bbox"][1] for l in lines)
    top = max(0, math.floor(min(l["bbox"][1] for l in lines) - height * .5))
    bottom = min(image.height, math.ceil(max(l["bbox"][3] for l in lines) + height * .5))
    start = max(0, math.floor(left - height * .15))
    stop = min(image.width, math.ceil(left + height * 1.25))
    if stop <= start or bottom <= top:
        return left + 1
    ink = np.asarray(image.convert("L"))[top:bottom, start:stop] < 180
    columns = ink.mean(axis=0) >= .9
    runs, begin = [], None
    for x, marked in enumerate([*columns, False]):
        if marked and begin is None:
            begin = x
        elif not marked and begin is not None:
            if x - begin <= max(6, height * .12):
                runs.append((start + begin, start + x))
            begin = None
    if len(runs) == 1:
        return runs[0][1] + 1
    # This bound excludes the literal neighboring label. Without a visible
    # rule it does not claim to identify a sequence glyph's exact boundary.
    return left + 1


def leading_label_boundary(image, box, text, left):
    """Separate a joined leading SEQ-ID by an observed rule or word gap."""
    import numpy as np
    match = LEADING_SEQ_ID.match(plain(text))
    if not match:
        return box
    if left is not None and box[0] < left < box[2]:
        return [left, *box[1:]]
    x1, y1, x2, y2 = map(int, box)
    ink = (np.asarray(image.convert("L"))[y1:y2, x1:x2] < 180).any(axis=0)
    expected = (x2 - x1) * len(re.sub(r"\s+", "", match.group())) / len(re.sub(r"\s+", "", text))
    gaps, begin = [], None
    for i, marked in enumerate([*ink, True]):
        if not marked and begin is None:
            begin = i
        elif marked and begin is not None:
            if (i - begin >= max(8, (y2 - y1) * .25)
                    and abs((begin + i) / 2 - expected) < (x2 - x1) * .12):
                gaps.append((begin, i))
            begin = None
    if not gaps:
        raise ValueError("Cannot separate a leading SEQ-ID label without cutting residues")
    gap = min(gaps, key=lambda g: abs((g[0] + g[1]) / 2 - expected))
    return [x1 + (gap[0] + gap[1]) / 2, y1, x2, y2]


def _annotation_boundary(image, box, text):
    """Use an observed word gap to exclude a trailing printed SEQ-ID label."""
    import numpy as np
    match = re.search(r"\(?\s*SEQ\s*ID|\(\s*SEQ", text, re.I)
    if not match:
        return box
    prefix = re.sub(r"\s+", "", text[:match.start()])
    if len(prefix) < 12:
        raise ValueError("No sequence precedes the identifier label")
    x1, y1, x2, y2 = map(int, box)
    mask = np.asarray(image.convert("L"))[max(0, y1):y2, max(0, x1):x2] < 180
    columns = mask.any(axis=0)
    gaps, begin = [], None
    for i, ink in enumerate(columns):
        if not ink and begin is None:
            begin = i
        elif ink and begin is not None:
            if i - begin >= max(8, (y2 - y1) * .25) and begin > 0:
                gaps.append((begin, i))
            begin = None
    expected = (x2 - x1) * len(prefix) / max(1, len(re.sub(r"\s+", "", text)))
    options = [g for g in gaps if abs((g[0] + g[1]) / 2 - expected) < (x2 - x1) * .12]
    if not options:
        raise ValueError("Cannot separate the identifier label without cutting residues")
    # The first word gap near the terminal SEQ label precedes the opening
    # parenthesis; later gaps inside "SEQ ID NO" are not sequence boundaries.
    gap = min(options, key=lambda g: g[0])
    return [x1, y1, x1 + (gap[0] + gap[1]) / 2, y2]


def addressed_row_lines(cell, grid, lines):
    """Anchor a figure row by its literal label, independently of residue OCR."""
    key = lambda t: re.sub(r"\s+", "", plain(t)).casefold()
    peers = [c for c in grid["physical"][cell["row"]] if c["ref"] != cell["ref"] and c["text"].strip()]
    anchors = []
    for peer in peers:
        hits = [line for line in lines if key(line["text"]) == key(peer["text"])]
        if len(hits) == 1:
            anchors.append((peer, hits[0]["bbox"]))
    if not anchors:
        return []
    centers = [(b[1] + b[3]) / 2 for _, b in anchors]
    height = max(b[3] - b[1] for _, b in anchors)
    if max(centers) - min(centers) > height:
        return []
    center = sum(centers) / len(centers)
    left = max((b[2] for p, b in anchors if p["column"] < cell["column"]), default=-math.inf)
    right = min((b[0] for p, b in anchors if p["column"] > cell["column"]), default=math.inf)
    rows = []
    for line in lines:
        box = line["bbox"]
        text = re.split(r"\(?\s*SEQ\s*ID|\(\s*SEQ", plain(line["text"]), maxsplit=1, flags=re.I)[0]
        value = re.sub(r"\s+", "", text)
        if (sequence_line(text) and left <= (box[0] + box[2]) / 2 <= right
                and abs((box[1] + box[3]) / 2 - center) <= height * .75):
            rows.append(line)
    return rows


def merge_lines(lines):
    groups = []
    for line in sorted(lines, key=lambda l: (l["bbox"][1], l["bbox"][0])):
        box = line["bbox"]
        same = next((g for g in groups if min(g["bbox"][3], box[3]) - max(g["bbox"][1], box[1])
                     >= .5 * min(g["bbox"][3] - g["bbox"][1], box[3] - box[1])), None)
        if same is None:
            groups.append({"bbox": list(box), "parts": [line]})
        else:
            same["bbox"] = [min(same["bbox"][0], box[0]), min(same["bbox"][1], box[1]),
                            max(same["bbox"][2], box[2]), max(same["bbox"][3], box[3])]
            same["parts"].append(line)
    return [{"bbox": g["bbox"], "text": " ".join(p["text"] for p in sorted(g["parts"], key=lambda p: p["bbox"][0]))} for g in groups]


def ruled_cell_boxes(image, grid):
    """Use complete visible rules only when they match the physical HTML grid.

    This also locates an unlabeled continuation row. Missing/extra rules or
    merged cells make the match ambiguous, so ordinary OCR localization stays
    the fallback. No row boundaries are interpolated from centered labels.
    """
    import numpy as np
    rows = grid.get("physical", [])
    if not rows or any(c.get("rowspan", 1) != 1 or c.get("colspan", 1) != 1 for r in rows for c in r):
        return {}
    columns = len(rows[0])
    if columns < 2 or any(len(r) != columns for r in rows):
        return {}
    ink = np.asarray(image.convert("L")) < 180

    def rules(mask):
        runs, start = [], None
        for i, marked in enumerate([*mask, False]):
            if marked and start is None:
                start = i
            elif not marked and start is not None:
                runs.append((start, i))
                start = None
        return runs

    xs = rules(ink.mean(axis=0) >= .8)
    if len(xs) != columns + 1:
        return {}
    # Underlined residues can span most of a wide sequence column. A row
    # divider must also cross the label column, where those underlines stop.
    ys = rules((ink[:, xs[0][0]:xs[-1][1]].mean(axis=1) >= .65)
               & (ink[:, xs[0][1]:xs[1][0]].mean(axis=1) >= .7))
    if len(ys) != len(rows) + 1:
        return {}
    return {c["ref"]: [xs[j][1] + 1, ys[i][1] + 1, xs[j + 1][0] - 1, ys[i + 1][0] - 1]
            for i, row in enumerate(rows) for j, c in enumerate(row)
            if xs[j][1] + 1 < xs[j + 1][0] - 1 and ys[i][1] + 1 < ys[i + 1][0] - 1}


def compare_page_text(evidence, metadata, page_lines):
    """Advisory comparison at the same physical lines; never change residues."""
    matrix = crop_matrix(metadata)
    if not matrix:
        return
    audits = {e["ref"]: e for e in evidence.get("sequence_ocr", {}).get("cells", [])}
    for cell in evidence.get("cells", []):
        audit = cell.get("sequence_ocr", {})
        if audit.get("status") != "success":
            continue
        parts, refs = [], []
        for line in audit["lines"]:
            box = transform_box(line["bbox_crop"], matrix)
            if not box:
                break
            hits = []
            for source in page_lines:
                b = source.get("bbox")
                if (valid_box(b) and box[0] - 3 <= (b[0] + b[2]) / 2 <= box[2] + 3
                        and box[1] - 3 <= (b[1] + b[3]) / 2 <= box[3] + 3
                        and sequence_line(source.get("text", ""))):
                    hits.append(source)
            hits.sort(key=lambda p: (p["bbox"][1], p["bbox"][0]))
            value = "".join(re.sub(r"\s+", "", plain(p["text"])) for p in hits)
            expected = re.sub(r"\s+", "", line["text"])
            if not hits or not .6 * len(expected) <= len(value) <= 1.5 * len(expected):
                break
            parts.append(value)
            refs.extend(p.get("source_ref") or "ocr:" + p.get("line_id", "") for p in hits)
        comparison = {"status": "uncompared"}
        if len(parts) == len(audit["lines"]):
            value = "".join(parts)
            comparison.update(status="matched" if value == audit["sequence"] else "mismatch",
                              table_text=audit["sequence"], page_text=value, page_refs=refs,
                              resolution="use_table_sequence_recognition")
        audit["page_comparison"] = comparison
        if cell.get("ref") in audits:
            audits[cell["ref"]]["page_comparison"] = copy.deepcopy(comparison)


def refine(evidence, *, device="gpu:0", threads=4, model_root=None, model_provider=None):
    """Return new evidence; all failures and original PPVL characters stay visible."""
    from .pdf2doc import parse_grid, _table_ocr_event
    grid = parse_grid(evidence["text"], evidence["table_id"])
    bodies = sequence_candidates(grid)
    candidates = [c for row in (grid or {}).get("physical", []) for c in row if c["ref"] in bodies]
    result = copy.deepcopy(evidence)
    audit = {"version": VERSION, "revision": REVISION, "policy": "table_sequence_recognition_authoritative_page_text_advisory",
             "model": "PP-OCRv6_medium_rec", "candidate_count": len(candidates), "cells": [],
             "image_sha256": evidence["image_sha256"]}
    result["sequence_ocr"] = audit
    def log(phase, **details):
        _table_ocr_event(phase, evidence=evidence, **details)

    if not candidates:
        audit["status"] = "not_applicable"
        log("sequence_skipped", candidates=0)
        return result
    result["vl_text"] = evidence["text"]
    started = perf_counter()
    log("sequence_start", candidates=len(candidates), model="PP-OCRv6_medium_rec", device=device)
    try:
        import numpy as np
        from PIL import Image
        original = Image.open(evidence["image_path"]).convert("RGB")
        root = Path(model_root or os.environ.get("SDA_OCR_MODEL_ROOT") or Path.home() / ".paddlex/official_models").expanduser().resolve()
        ocr, recognizer, orientation = (model_provider or models)(device, threads, str(root))
        log("sequence_models_ready", seconds=round(perf_counter() - started, 3))
        prediction = list(orientation.predict(input=np.asarray(original)[:, :, ::-1], batch_size=1))[0]
        angle = int(prediction["label_names"][0])
        if angle not in {0, 90, 180, 270}:
            raise ValueError("Invalid crop orientation")
        image = original.rotate(angle, expand=True)
        to_source = rotation_to_source(angle, original.width, original.height)
        audit.update(rotation_counterclockwise=angle, upright_to_crop=to_source,
                     orientation_confidence=float(prediction["scores"][0]))
        log("sequence_localization_start", rotation_counterclockwise=angle)
        raw = list(ocr.predict(input=np.asarray(image)[:, :, ::-1]))[0]
        texts, boxes = list(raw["rec_texts"]), list(raw["rec_boxes"])
        if len(texts) != len(boxes):
            raise ValueError("Text-line OCR identity mismatch")
        lines = [{"text": str(t), "bbox": [float(x) for x in b], "coordinate_space": "crop_pixels"}
                 for t, b in zip(texts, boxes)]
        audit["local_text_lines"] = lines
        log("sequence_localization_complete", lines=len(lines))
        geo = copy.deepcopy(grid)
        for row in geo["physical"]:
            for cell in row:
                body = bodies.get(cell["ref"])
                if body:
                    cell["text"] = body[0]
        locate_grid(geo, {"lines": lines, "table_id": evidence["table_id"]},
                    {"bbox": [0, 0, image.width, image.height], "bbox_padded": [0, 0, image.width, image.height],
                     "width_px": image.width, "height_px": image.height})
        by_ref = {c["ref"]: c for row in geo["physical"] for c in row}
        ruled = ruled_cell_boxes(image, grid)
        output_dir = Path(evidence["image_path"]).parent / "sequence_ocr" / (Path(evidence["image_path"]).stem + "-" + evidence["image_sha256"][:12])
        output_dir.mkdir(parents=True, exist_ok=True)
        tree = html.fromstring(evidence["text"])
        elements = tree.xpath(".//tr/td|.//tr/th")
        physical = [c for row in grid["physical"] for c in row]
        if len(elements) != len(physical):
            raise ValueError("Cannot preserve physical table cell addresses")
        dom = dict(zip((c["ref"] for c in physical), elements))
        updated = {}
        for index, cell in enumerate(candidates, 1):
            log("sequence_cell_start", ref=cell["ref"], current=index, total=len(candidates))
            entry = {"ref": cell["ref"], "vl_text": cell["text"], "status": "failed", "lines": []}
            audit["cells"].append(entry)
            try:
                rule_box = ruled.get(cell["ref"])
                box = rule_box or by_ref[cell["ref"]].get("bbox_pdf")
                selected = [line for line in lines if valid_box(box) and sequence_line(line["text"])
                            and box[0] - 4 <= (line["bbox"][0] + line["bbox"][2]) / 2 <= box[2] + 4
                            and box[1] - 4 <= (line["bbox"][1] + line["bbox"][3]) / 2 <= box[3] + 4]
                if not selected:
                    selected = addressed_row_lines(cell, grid, lines)
                if not selected:
                    raise ValueError("No sequence text lines inside the addressed cell")
                left = rule_box[0] if rule_box else sequence_column_left(image, cell, geo, selected)
                entry["localization"] = {"method": "visible_table_rules" if rule_box else by_ref[cell["ref"]].get("location_method", "row_label_fallback"),
                                         "detected_lines": len(selected), "column_left": left}
                value = ""
                for n, line in enumerate(merge_lines(selected), 1):
                    leading = leading_label_boundary(image, line["bbox"], line["text"], left)
                    residue_text = LEADING_SEQ_ID.sub("", plain(line["text"]))
                    clipped = _annotation_boundary(image, leading, residue_text)
                    crop, actual = line_crop(image, clipped,
                                             min_left=max(left or 0, leading[0]) if leading[0] > line["bbox"][0] else left,
                                             max_right=min(clipped[2], rule_box[2]) if rule_box else clipped[2] if clipped[2] < line["bbox"][2] else None)
                    path = output_dir / f"R{cell['row']}C{cell['column']}-line{n}.png"
                    crop.save(path)
                    pred = list(recognizer.predict(input=str(path), batch_size=1))[0]
                    text = str(pred["rec_text"])
                    residues = re.sub(r"\s+", "", text)
                    entry["lines"].append({"image_path": str(path), "image_sha256": digest(path),
                                           "bbox_upright": actual, "bbox_crop": transform_box(actual, to_source),
                                           "text": text, "confidence": float(pred["rec_score"])})
                    if not residues or set(residues) - AA:
                        raise ValueError("Re-recognized sequence has noncanonical or missing residues")
                    value += residues
                # A digit/label cell can never be borrowed to pad a missing sequence.
                if not value:
                    raise ValueError("Empty sequence recognition")
                expected, _ = bodies[cell["ref"]]
                if len(value) < len(expected) * .8:
                    raise ValueError("Re-recognized sequence is incomplete relative to the addressed cell")
                _, suffix = bodies[cell["ref"]]
                entry.update(status="success", sequence=value, text=value + (" " + suffix if suffix else ""))
                element = dom[cell["ref"]]
                # Preserve printed HTML formatting before replacing residue text.
                if cell.get("text_styles"):
                    entry["original_text_styles"] = copy.deepcopy(cell["text_styles"])
                for child in list(element):
                    element.remove(child)
                element.text = entry["text"]
                boxes_original = [l["bbox_crop"] for l in entry["lines"]]
                entry["bbox_crop"] = [min(b[0] for b in boxes_original), min(b[1] for b in boxes_original),
                                      max(b[2] for b in boxes_original), max(b[3] for b in boxes_original)]
            except (ValueError, KeyError, OSError) as exc:
                entry["error"] = str(exc)
            updated[cell["ref"]] = entry
            log("sequence_cell_result", ref=cell["ref"], current=index, total=len(candidates),
                status=entry["status"], lines=len(entry["lines"]), error=entry.get("error"))
        if any(e["status"] == "success" for e in audit["cells"]):
            result["text"] = html.tostring(tree, encoding="unicode")
            result["text_sha256"] = hashlib.sha256(result["text"].encode()).hexdigest()
        new_grid = parse_grid(result["text"], result["table_id"])
        for row in new_grid["physical"]:
            for cell in row:
                entry = updated.get(cell["ref"])
                if entry:
                    cell.update(sequence_ocr=copy.deepcopy(entry), vl_text=entry["vl_text"])
                    if entry["status"] == "success":
                        cell.update(bbox=entry["bbox_crop"], bbox_crop=entry["bbox_crop"],
                                    coordinate_space="crop_pixels", precision="ocr_lines", location_status="located",
                                    location_method="ppocrv6_sequence_lines")
        result.update(cells=[c for row in new_grid["physical"] for c in row], grid=new_grid["rows"])
    except Exception as exc:
        audit["error"] = f"{type(exc).__name__}: {exc}"
        audit["cells"] = [{"ref": c["ref"], "vl_text": c["text"], "status": "failed", "error": audit["error"], "lines": []} for c in candidates]
        failures = {e["ref"]: e for e in audit["cells"]}
        for cell in result["cells"]:
            if cell.get("ref") in failures:
                cell.update(sequence_ocr=failures[cell["ref"]], vl_text=cell["text"])
    audit.update(successful_cells=sum(e["status"] == "success" for e in audit["cells"]),
                 failed_cells=sum(e["status"] != "success" for e in audit["cells"]), seconds=round(perf_counter() - started, 3))
    audit["status"] = "complete" if not audit["failed_cells"] else "partial" if audit["successful_cells"] else "failed"
    log("sequence_complete", status=audit["status"], successful_cells=audit["successful_cells"],
        failed_cells=audit["failed_cells"], seconds=audit["seconds"], error=audit.get("error"))
    return result
