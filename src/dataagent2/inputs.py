"""Read raw OCR and the ORIGINAL template stage, never repaired sequence answers."""

from __future__ import annotations

from collections import defaultdict
import json
from pathlib import Path
import re

from .provider import compact, digest

CHUNKING_REVISION = 2


def split_text(text, max_chars=8000):
    """Lossless spans, preferring paragraph/line/word boundaries near the limit."""
    start = 0
    while len(text) - start > max_chars:
        stop = start + max_chars
        floor = start + max_chars // 2
        for pattern in (r"\n\s*\n", r"\n", r"\s+"):
            matches = list(re.finditer(pattern, text[floor:stop]))
            if matches:
                stop = floor + matches[-1].end()
                break
        yield start, text[start:stop]
        start = stop
    if start < len(text):
        yield start, text[start:]


def read(path, default=None):
    path = Path(path)
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else default


def discover(root, document_id):
    root = Path(root).resolve()
    jobs = read(root / ".studio/jobs.json", {})
    result = Path(jobs[document_id]["result_dir"])
    checkpoint = read(result / "dataagent_checkpoint.json", {})
    attempts = checkpoint.get("attempts", [])
    # The earliest native run is before the later sequence/question recoveries.
    original = next((Path(x["directory"]) for x in attempts
                     if (Path(x["directory"]) / "sda_input.json").is_file()), None)
    if original is None:
        raise ValueError(f"No original dataagent input/template for {document_id}")
    return original, result


def load_input(native_dir, reference_result=None):
    native = Path(native_dir).resolve()
    prepared = read(native / "sda_input.json")
    template = read(native / "document_template.json")
    if not prepared or not template or prepared["document_id"] != template["document_id"]:
        raise ValueError("Matching original OCR and document template are required")
    document_id = prepared["document_id"]
    sources, by_ref = {}, defaultdict(list)

    def add(ref, text, page, kind, table=None, row=None, col=None, metadata=None):
        if not isinstance(text, str) or not text.strip():
            return
        # Preserve every character of long OCR blocks, with reversible offsets.
        segments = split_text(text)
        for start, value in segments:
            sid = len(sources) + 1
            sources[sid] = {"id": sid, "source_ref": ref, "text": value, "page": page,
                            "kind": kind, "table_id": table, "row": row, "column": col,
                            "source_offset": start, **(metadata or {})}
            by_ref[ref].append(sid)

    for b in prepared.get("document_blocks", []):
        add(b.get("source_ref", "ocr:" + b["block_id"]), b["text"], b.get("page"), "text",
            metadata={"bbox": b.get("bbox", []), "source_file": b.get("source_file")})
    for table in prepared.get("tables", []):
        identity = str(table.get("source_id") or table.get("table_id"))
        seen_cells = set()
        for ri, row in enumerate(table.get("rows", [])):
            for ci, cell in enumerate(row):
                ref = cell.get("source_ref") or f"{identity}:R{ri}C{ci}"
                if ref in seen_cells:
                    continue
                seen_cells.add(ref)
                add(ref, cell.get("text", ""), table.get("page"), "cell",
                    identity, cell.get("origin_row", ri), cell.get("origin_column", ci),
                    {"bbox": cell.get("bbox_pdf", cell.get("bbox", [])),
                     **{k: cell[k] for k in ("sequence_ocr", "vl_text", "text_styles") if k in cell}})
    for table in prepared.get("unstructured_tables", []):
        raw = table.get("sda_source", {})
        text = raw.get("text") or raw.get("table_text") or ""
        add(str(table["source_id"]) + ":unstructured", text, table.get("page"), "unstructured",
            str(table["source_id"]))

    style_definitions = []
    for table in prepared.get("tables", []):
        for source in table.get("sequence_style_sources", []):
            from document.sequence_styles import validate_style_source
            originals = {s["source_ref"]: s for s in sources.values()}
            validate_style_source(source, originals)
            add(source["source_ref"], source["text"], source["page"], "cell", table["source_id"],
                source["row_index"], source["col_index"],
                {k: source[k] for k in ("sequence_style", "bbox_crop", "bbox_pdf", "precision")})
            domain = source["sequence_style"]["domain"]
            if not domain:
                continue
            row = table["rows"][source["row_index"]]
            labels = [(c, re.findall(r"SEQ\s*ID(?:\s*NO[.:]?)?\s*[:：]?\s*(\d+)", c["text"], re.I)) for c in row]
            numbered = [(c, int(ids[0])) for c, ids in labels if len(ids) == 1]
            if len(numbered) != 1:
                continue
            label, seq = numbered[0]
            sid = by_ref[source["source_ref"]][0]
            value = source["text"][domain["start"]:domain["end"]]
            style_definitions.append({"seq": seq, "region": domain["chain"], "name": None,
                "parts": [{"s": sid, "start": value[:20], "end": value[-20:]}],
                "refs": [sid, *by_ref.get(label["source_ref"], []),
                         *by_ref.get(source["sequence_style"]["original_ref"], [])]})

    def refs(values):
        found = []
        for ref in values:
            found.extend(by_ref.get(ref, []))
        return list(dict.fromkeys(found))

    entities = []
    native_to_item = {}
    for n, e in enumerate(template.get("entity_template", {}).get("entities", []), 1):
        identity = f"i{n}"
        native_to_item[e["entity_id"]] = identity
        entities.append({"id": identity, "native_id": e["entity_id"],
                         "name": e["canonical_name"], "names": e.get("raw_names", [e["canonical_name"]]),
                         "role": e.get("role", "unknown"), "refs": refs(e.get("evidence_refs", []))})
    links = []
    for link in template.get("sequence_links", []):
        if link["entity_id"] not in native_to_item:
            continue
        region = link["chain"]
        if link["scope"] == "full_chain":
            region = "H_CHAIN" if region == "VH" else "L_CHAIN"
        links.append({"item": native_to_item[link["entity_id"]], "region": region,
                      "seq": link["seq_id"], "name": None, "refs": refs(link.get("evidence_refs", [])),
                      "original_refs": link.get("evidence_refs", []), "origin": "template_seed"})
    experiments = []
    for profile in template.get("assay_template", {}).get("region_profiles", []):
        experiments.append({"id": profile.get("experiment_id") or profile["protocol_id"],
            "table": profile["table_id"], "region_id": profile.get("region_id"),
            "method": profile.get("method"), "target": None,
            "species": ",".join(profile.get("species", [])), "system": profile.get("system"),
            "conditions": None, "refs": refs(profile.get("evidence_refs", [])), "origin": "template_seed"})
    observations = []
    measurements = read(native / "assay_measurements.json", [])
    name_index = defaultdict(set)
    for e in entities:
        for name in [e["name"], *e["names"]]:
            name_index[name.casefold().strip()].add(e["id"])
    for m in measurements:
        source_ids = refs([m.get("value_ref")])
        ids = name_index.get(m.get("antibody_id", "").casefold().strip(), set())
        if not source_ids or len(ids) != 1:
            continue
        observations.append({"item": next(iter(ids)), "experiment": None,
            "metric": m["metric"], "value": str(m.get("raw_text") or m["value"]),
            "unit": m.get("unit"), "s": source_ids[0],
            "refs": list(dict.fromkeys(source_ids + refs(m.get("header_refs", [])))),
            "table": m.get("source_table"), "origin": "table_seed"})
    # Reference outputs are attached for scoring ONLY; never put into model payloads.
    reference = read(Path(reference_result) / "template.json", {}) if reference_result else {}
    baseline_report = read(Path(reference_result) / "run_summary.json", {}) if reference_result else {}
    usage = baseline_report.get("token_usage", {})
    stages = usage.get("by_stage", [])
    shared = [r for r in stages if str(r.get("stage", "")).startswith(("table-classification-", "document-template-"))]
    baseline = {"total_tokens": usage.get("totals", {}).get("total_tokens"),
        "unknown_requests": usage.get("totals", {}).get("requests_without_usage", 0),
        "shared_stage_tokens": sum(r.get("total_tokens") or 0 for r in shared),
        "shared_stages": [{"stage": r["stage"], "total_tokens": r.get("total_tokens")} for r in shared],
        "current_reference_variable_chains": sum(e.get(c, {}).get("sequence", {}).get("status") == "present"
            for e in reference.get("entities", []) for c in ("VH", "VL")),
        "current_reference_entities": len(reference.get("entities", [])),
        "note": "Historical model total excludes local OCR; later repaired reference is not ground truth. Shared stages are reused, not rerun."}
    seed = {"entities": entities, "links": links, "experiments": experiments, "observations": observations}
    signature = digest({"document_id": document_id, "sources": sources, "seed": seed})
    return {"document_id": document_id, "pdf_sha256": prepared.get("pdf_sha256"),
            "sources": sources, "seed": seed, "signature": signature,
            "native_dir": str(native), "baseline": baseline,
            "reference_for_scoring_only": reference,
            "input_warnings": prepared.get("warnings", []),
            "unstructured_table_count": len(prepared.get("unstructured_tables", [])),
            "style_definitions": style_definitions}


def source_row(s):
    return [s["id"], s["page"], s["kind"], s["table_id"], s["row"], s["column"], s["text"]]


def source_ocr_status(sources, ids):
    return {sid: {k: sources[sid]["sequence_ocr"][k] for k in ("status", "error")
                  if k in sources[sid]["sequence_ocr"]}
            for sid in ids if sources[sid].get("sequence_ocr")}


def make_chunks(data, max_chars=24000, overlap_chars=700):
    if max_chars < 3000:
        raise ValueError("Chunk budget must be at least 3000 characters")
    if overlap_chars < 0:
        raise ValueError("Overlap budget must be nonnegative")
    sources = data["sources"]
    ordered = sorted(sources, key=lambda k: (sources[k]["page"] or 0,
        sources[k]["kind"] != "text", sources[k]["table_id"] or "",
        sources[k]["row"] if sources[k]["row"] is not None else -1,
        sources[k]["column"] if sources[k]["column"] is not None else -1, k))
    sizes = {sid: len(compact(source_row(sources[sid]))) + 1 for sid in ordered}
    context_budget = min(overlap_chars + 2000, max_chars // 4) if overlap_chars else 0
    # One extra character accounts for the source array's brackets/commas.
    source_budget = max_chars - 1
    core_budget = source_budget - context_budget

    # Keep a small table together. For larger tables, keep physical rows
    # together; only a row larger than the full budget may split between cells.
    units = []
    for sid in ordered:
        source = sources[sid]
        key = (source["page"], source["table_id"]) if source["table_id"] else ("text", sid)
        if units and units[-1][0] == key:
            units[-1][1].append(sid)
        else:
            units.append((key, [sid]))
    groups = []
    for _, ids in units:
        if sum(sizes[s] for s in ids) <= core_budget or not sources[ids[0]]["table_id"]:
            groups.append(ids)
            continue
        rows = []
        for sid in ids:
            row = sources[sid]["row"]
            if rows and row is not None and sources[rows[-1][0]]["row"] == row:
                rows[-1].append(sid)
            else:
                rows.append([sid])
        for row in rows:
            groups.extend([[sid] for sid in row] if sum(sizes[s] for s in row) > source_budget else [row])

    chunks, current, size = [], [], 0
    for group in groups:
        n = sum(sizes[s] for s in group)
        first = sources[group[0]]
        new_page = current and sources[current[-1]]["page"] != first["page"]
        heading = first["kind"] == "text" and bool(re.match(
            r"^\s*(?:(?i:examples?\s+\d+\b)|实施例\s*\d+|[A-Z][A-Z \d-]{4,70}$)", first["text"]))
        if current and (size + n > core_budget or ((new_page or heading) and size >= core_budget * .65)):
            chunks.append({"id": f"c{len(chunks) + 1}", "core": current})
            current, size = [], 0
        current.extend(group)
        size += n
    if current:
        chunks.append({"id": f"c{len(chunks) + 1}", "core": current})

    positions = {sid: i for i, sid in enumerate(ordered)}
    headers = defaultdict(list)
    for sid in ordered:
        source = sources[sid]
        if source["table_id"] and source["row"] in (0, 1):
            headers[source["table_id"]].append(sid)
    for chunk in chunks:
        core = set(chunk["core"])
        remaining = min(context_budget, max(0, source_budget - sum(sizes[s] for s in core)))
        context = []

        def include(sid, budget):
            nonlocal remaining
            if sid in core or sid in context:
                return 0
            if sizes[sid] <= min(remaining, budget):
                context.append(sid)
                remaining -= sizes[sid]
                return sizes[sid]
            return 0

        # Headers take priority over prose overlap for a continuation table.
        for table in dict.fromkeys(sources[s]["table_id"] for s in chunk["core"] if sources[s]["table_id"]):
            for sid in headers[table]:
                include(sid, remaining)
        # Supply both sides of a boundary without requiring another chunk's
        # model response. Every source still has exactly one owning core.
        for neighbors in (reversed(ordered[max(0, positions[chunk["core"][0]] - 12):positions[chunk["core"][0]]]),
                          iter(ordered[positions[chunk["core"][-1]] + 1:positions[chunk["core"][-1]] + 13])):
            used = 0
            for sid in neighbors:
                used += include(sid, max(0, overlap_chars // 2 - used))
        chunk["context"] = sorted(context, key=positions.__getitem__)
        chunk["source_characters"] = 1 + sum(sizes[s] for s in chunk["core"] + context)
        # An indivisible source may exceed a deliberately small user budget.
        # Preserve it and report it, rather than silently truncate its text.
        chunk["oversized_source_ids"] = [s for s in chunk["core"] if sizes[s] > source_budget]
    assert sorted(k for c in chunks for k in c["core"]) == sorted(sources)
    return chunks
