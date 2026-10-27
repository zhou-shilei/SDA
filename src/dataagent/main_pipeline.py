"""Connect dataagent's existing-OCR workflow to SDA datasets and reports."""

from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from time import perf_counter

from document.document_extraction import coverage_audit, template_from_payload
from document.table_continuation import parse_grid
from document.cell_geometry import geometry_fields
from schema_pipeline import structured_entities, load_sequence_catalog, save_json
from template_agent import export_dataset, refresh_missing, validate_template
from units.token_usage import TokenUsageRecorder, parse_codex_events, write_usage_report
from units.timing import write_timing_report

from .run_sda import run_from_ocr


def read(path, default=None):
    path = Path(path)
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else copy.deepcopy(default)


def stable_id(prefix, *values):
    return prefix + hashlib.sha256(json.dumps(values, ensure_ascii=False).encode()).hexdigest()[:20]


def collect_usage(attempts, document_id):
    """Count each live provider attempt once, including failed/schema retries."""
    recorder, seen = TokenUsageRecorder(), set()
    for attempt in attempts:
        directory = Path(attempt["directory"])
        logs = sorted(directory.rglob("llm_calls.jsonl"))
        calls = []
        for path in logs:
            for line in path.read_text().splitlines():
                if line.strip():
                    try:
                        calls.append(json.loads(line))
                    except ValueError:
                        continue
        if not logs:
            calls = read(directory / "agent_state.json", {}).get("llm_runs", [])
        for index, call in enumerate(calls):
            if call.get("cache_hit"):
                continue
            files = call.get("request_event_files") or ([str(Path(call["audit_dir"]) / "events.jsonl")]
                                                        if call.get("audit_dir") else [])
            if not files:
                recorder.record(call.get("usage"), stage=call.get("task"), document_id=document_id,
                    item_id=document_id, status="failed" if call.get("status") == "error" else "ok",
                    request_id=f"{directory}:{index}")
            for number, filename in enumerate(files, 1):
                path = Path(filename).resolve()
                if str(path) in seen:
                    continue
                seen.add(str(path))
                parsed = parse_codex_events(path.read_text() if path.is_file() else "")
                failed = parsed["failed_events"] or (path.parent / "validation_error.json").is_file()
                recorder.record(parsed["usage"], stage=call.get("task"), document_id=document_id,
                    item_id=document_id, attempt=number, status="failed" if failed else "ok", event_file=str(path))
    return recorder.report(document_id=document_id)


def _sources_and_coverage(state, prepared):
    sources = copy.deepcopy(state.get("document_structure", {}).get("sources", {}))
    from .answer_agent import validate_reviewed_source
    for ref, source in state.get("sequence_extraction", {}).get("sources", {}).items():
        if source.get("image_review"):
            validate_reviewed_source(source, sources)
            sources[ref] = copy.deepcopy(source)
    for block in prepared["document_blocks"]:
        sources.setdefault("block:" + block["block_id"], {**block, "kind": "document_block"})
        sources.setdefault(block["source_ref"], {**block, "kind": "text"})
    grids, tables = {}, []
    from document.pdf2doc import _ground_table_cells
    from .sda_inputs import _rows
    layout = read(prepared.get("inputs", {}).get("layout_manifest"), {}) if prepared.get("inputs", {}).get("layout_manifest") else {}
    crops = {crop["id"]: crop for crop in layout.get("images", []) if crop.get("label") == "table"}
    page_lines = {}
    for block in prepared["document_blocks"]:
        page_lines.setdefault(block["page"], []).append({**block, "line_id": block["block_id"]})
    for table in prepared["tables"]:
        identity = table["source_id"]
        origin = table["sda_source"]
        evidence = origin["evidence"]
        evidence.update(table_id=identity, text=origin["text"])
        _ground_table_cells(evidence, {**crops.get(identity, {}), "bbox": table.get("bbox"),
                            "width_px": table.get("width"), "height_px": table.get("height")},
                            page_lines=page_lines.get(table["page"], []))
        table["rows"] = _rows(origin["text"], identity, evidence)
        for field in ("bbox_padded", "crop_to_pdf"):
            table[field] = evidence.get(field)
        sid = "table:" + identity
        grid = parse_grid(origin["text"], identity)
        cell_positions = {cell["ref"]: cell for cell in origin["evidence"].get("cells", [])}
        for row in grid["physical"]:
            for cell in row:
                cell.update(geometry_fields(cell_positions.get(cell["ref"], {})))
                ref = cell["ref"]
                cell["source_id"] = ref
                sources[ref] = {**sources.get(ref, {}), **cell, "source_id": ref, "kind": "cell",
                    "table_id": identity, "table_source_id": sid, "page": table["page"],
                    "source_file": origin["source_file"], "quote": cell["text"]}
        grids[sid] = grid
        tables.append({"source_id": sid, "pdf_page": table["page"], "text": origin["text"],
                       "verified_context": table.get("verified_context", {})})
    for table in prepared.get("unstructured_tables", []):
        origin = table["sda_source"]
        sid = "table:" + table["source_id"]
        sources[sid] = {"kind": "unstructured_table", "table_id": table["source_id"],
                        "page": table["page"], "text": origin["text"], "source_file": origin["source_file"],
                        "image_path": table["image_path"], "structure_status": "unavailable"}
        tables.append({"source_id": sid, "pdf_page": table["page"], "text": origin["text"]})
    page_files = {block["page"]: block["source_file"] for block in prepared["document_blocks"]}
    for ref, source in sources.items():
        source.update(source_id=ref, quote=source.get("text", source.get("quote", "")),
                      document_id=prepared["document_id"])
        source.setdefault("ref", ref)
        if not source.get("source_file") and source.get("page") in page_files:
            source["source_file"] = page_files[source["page"]]
    for source in sources.values():
        if source.get("image_review"):
            validate_reviewed_source(source, sources)
            source.update(geometry_fields(sources[source["image_review"]["original_ref"]]))
    return sources, {"tables": tables}, grids


def _payload(state, native, sources):
    """Translate accepted decisions, retaining explicit identities and all endpoints."""
    document_template = state.get("document_template", {})
    entities, by_id, issues = [], {}, []
    resolved = {row["construct_key"]: row for row in state.get("resolved_sequences", [])}

    def refs(values):
        return list(dict.fromkeys(ref for ref in values or [] if ref in sources))

    def entity(identity, name, aliases=(), evidence=()):
        if identity not in by_id:
            item = {"id": identity, "NAME": name, "ALIASES": list(aliases), "CONSTRUCT_TYPE": None,
                    "VH": None, "VL": None, "VH_SEQ_ID": None, "VL_SEQ_ID": None,
                    "SEQUENCE_LINKS": [], "EVIDENCE": {"NAME": refs(evidence), "VH": [], "VL": []}, "NOTES": None}
            entities.append(item)
            by_id[identity] = item
        return by_id[identity]

    for item in document_template.get("entity_template", {}).get("entities", []):
        identity, name = item["entity_id"], item.get("canonical_name")
        aliases = [n for n in item.get("raw_names", []) if n != name]
        entity(identity, name, aliases, item.get("evidence_refs", []))
    for record in native["records"]:
        identity = record.get("canonical_entity_key") or record.get("antibody_id")
        if identity:
            entity(identity, record.get("canonical_antibody_id") or record.get("antibody_id"))
    names = {}
    for item in entities:
        for name in [item["NAME"], *item["ALIASES"]]:
            names.setdefault(name, []).append(item["id"])
    for identity, item in by_id.items():
        sequence = resolved.get(identity, {})
        for chain, field, maximum in (("VH", "vh", 160), ("VL", "vl", 140)):
            resolution = sequence.get("sequence_resolution", {}).get(field, {})
            citations = refs(resolution.get("evidence_refs", []))
            citations = refs(citations + [s["source_ref"] for s in resolution.get("segments", [])])
            value = sequence.get(field)
            if value and len(value) <= maximum and citations:
                item[chain], item["EVIDENCE"][chain] = value, citations
                # A whole-chain identifier must not replace a variable-region slice with full-chain residues.
                if resolution.get("source_scope") == "variable":
                    item[chain + "_SEQ_ID"] = resolution.get("seq_id")
            elif value:
                issues.append({"kind": "unexported_variable_sequence", "entity_id": identity, "chain": chain,
                               "reason": "No addressed source or sequence exceeds variable-region length", "candidate": value})
    for link in document_template.get("sequence_links", []):
        target = by_id.get(link["entity_id"])
        citations = refs(link["evidence_refs"])
        if target and citations:
            region = link["chain"] if link["scope"] == "variable" else {"VH": "H_chain", "VL": "L_chain"}[link["chain"]]
            target["SEQUENCE_LINKS"].append({"region": region, "seq_id": link["seq_id"], "SOURCE_REFS": citations})

    profiles = document_template.get("assay_template", {}).get("region_profiles", [])
    experiments, experiment_ids = [], {}
    for profile in profiles:
        key = (profile["table_id"], profile.get("region_id"), profile["protocol_id"])
        if key in experiment_ids:
            continue
        identity = stable_id("experiment-", *key)
        experiment_ids[key] = identity
        experiments.append({"id": identity, "NAME": profile.get("label"),
            "KEYWORDS": list(profile.get("species", [])) + ([profile["system"]] if profile.get("system") else []),
            "TAGS": list(profile.get("endpoints", [])), "METHOD": profile.get("method"),
            "TARGET": profile.get("antigen_target"), "SPECIES": ", ".join(profile.get("species", [])) or None,
            "SYSTEM": profile.get("system"), "CONDITIONS": profile.get("assay_condition"),
            "EVIDENCE": refs(profile.get("evidence_refs", []))})

    legacy = {record["record_id"]: record for record in native["records"]}
    measurements = copy.deepcopy(native["measurements"])
    known = {(m["value_ref"], m["metric"]) for m in measurements}
    for record in native["records"]:
        for evidence in record.get("evidence", []):
            if evidence.get("type") != "assay_measurement":
                continue
            ref = evidence.get("value_ref")
            if not ref and evidence.get("row_index") is not None and evidence.get("col_index") is not None:
                ref = f"{evidence['table_id']}:R{evidence['row_index']}C{evidence['col_index']}"
            metric = evidence.get("endpoint") or record.get("assay_endpoint") or "IC50"
            if ref in sources and (ref, metric) not in known:
                measurements.append({"antibody_id": record.get("raw_antibody_id") or record["antibody_id"],
                    "metric": metric, "value": record.get("ic50"), "unit": record.get("ic50_unit"),
                    "operator": record.get("ic50_operator", "="), "value_ref": ref,
                    "source_table": evidence["table_id"], "region_id": record.get("assay_region_id"),
                    "legacy_record_id": record["record_id"], "assay_condition": record.get("assay_condition")})
                known.add((ref, metric))
    records = []
    for measurement in measurements:
        record = legacy.get(measurement.get("legacy_record_id"), {})
        name, ref = measurement["antibody_id"], measurement["value_ref"]
        identity = record.get("canonical_entity_key")
        if identity not in by_id or name not in [by_id[identity]["NAME"], *by_id[identity]["ALIASES"]]:
            matches = names.get(name, [])
            identity = matches[0] if len(matches) == 1 else stable_id("unresolved-", name)
            if identity not in by_id:
                entity(identity, name, evidence=[ref])
        matching = [p for p in profiles if p["table_id"] == measurement["source_table"]
                    and p.get("region_id") == measurement.get("region_id")
                    and measurement["metric"] in p.get("endpoints", [])]
        profile = matching[0] if len(matching) == 1 else {}
        exp_id = experiment_ids.get((profile.get("table_id"), profile.get("region_id"), profile.get("protocol_id")))
        raw = sources.get(ref, {}).get("text", "").strip()
        value = measurement["value"]
        if raw and (measurement.get("operator", "=") != "=" or re.search(r"[eE<>≤≥±~]", raw)):
            value = raw
        records.append({"ENTITY_ID": identity, "NAME": name, "METRIC": measurement["metric"], "VALUE": value,
            "UNIT": measurement.get("unit"), "TARGET": record.get("antigen_target") or profile.get("antigen_target"),
            "METHOD": measurement.get("assay_method") or record.get("assay_method") or profile.get("method"),
            "CONDITIONS": measurement.get("assay_condition") or record.get("assay_condition"),
            "EXPERIMENT_ID": exp_id, "CELL_REFS": refs([ref]),
            "EVIDENCE": {"MEASUREMENT": refs([ref]), "CONTEXT": refs(
                measurement.get("header_refs", []) + measurement.get("unit_refs", [])
                + measurement.get("condition_refs", []) + profile.get("evidence_refs", []))}, "NOTES": None})
    table_coverage = []
    for classification in state.get("table_classifications", []):
        columns, headers = set(), set()
        for region in classification.get("regions", []):
            headers.update(region.get("header_rows", []))
            metrics = region.get("metrics", []) + [m for g in region.get("entity_groups", []) for m in g.get("metrics", [])]
            columns.update(m["value_column"] for m in metrics if m.get("value_column") is not None)
        table_coverage.append({"source_id": "table:" + classification["table_id"],
            "status": "ambiguous" if classification.get("decision") == "needs_review" else
                      "measurements_extracted" if columns or any(r["CELL_REFS"] and sources[r["CELL_REFS"][0]].get("table_id") == classification["table_id"] for r in records) else "no_measurements",
            "measurement_columns": sorted(columns), "header_rows": sorted(headers), "reason": classification.get("explanation", "")})
    return {"entities": entities, "records": records, "experiments": experiments, "relations": [],
            "table_coverage": table_coverage, "limitations": []}, issues


def export_sda_result(document_id, native_dir, output_dir, data_dir, usage):
    native_dir, output_dir = Path(native_dir), Path(output_dir)
    native = read(native_dir / "dataset.json")
    state, prepared = read(native_dir / "agent_state.json"), read(native_dir / "sda_input.json")
    sources, model_input, grids = _sources_and_coverage(state, prepared)
    from .sequence_evidence import attach_sequence_locations
    resolved_map = {row["construct_key"]: row for row in state.get("resolved_sequences", [])}
    attach_sequence_locations(resolved_map, [], prepared["tables"], state.get("assay_method_pages", []),
                              prepared.get("candidates", []), sources=sources)
    payload, issues = _payload(state, native, sources)
    template = template_from_payload(document_id, payload, sources, catalog=load_sequence_catalog(data_dir, document_id))
    template["agent"] = {"name": "dataagent", "mode": "existing_ocr", "native_directory": str(native_dir)}
    template["issues"].extend(issues)
    template["issues"].extend({"kind": warning["code"], **warning}
                              for warning in native["warnings"] if warning.get("code") == "table_structure_unavailable")
    entity_ids = {e.get("original_entity_id"): e["entity_id"] for e in template["entities"]}
    validations = {v["construct_key"]: v for v in state.get("construct_validations", [])}
    resolved = {r["construct_key"]: r for r in state.get("resolved_sequences", [])}
    for entity in template["entities"]:
        key = entity.get("original_entity_id")
        validation, sequence = validations.get(key), resolved.get(key, {})
        entity["sequence_locations"] = {
            field: copy.deepcopy(location) for field, location in sequence.get("sequence_locations", {}).items()
            if entity.get(field.upper(), {}).get("sequence", {}).get("value") == sequence.get(field)}
        if validation and all(entity.get(chain, {}).get("sequence", {}).get("value") == (sequence.get(chain.lower()) or None)
                              for chain in ("VH", "VL")):
            evaluation = {k: copy.deepcopy(validation[k]) for k in ("agent_review", "anarci_review", "esm2_review",
                "overall_review_status", "pair_status", "sequence_sha256") if k in validation}
            # Full numbering remains in eval/ JSON; the UI uses regions and scores.
            for chain in evaluation.get("anarci_review", {}).get("chains", {}).values():
                for domain in chain.get("domains", []):
                    domain.pop("numbering", None)
            for chain in evaluation.get("esm2_review", {}).get("chains", {}).values():
                chain.pop("position_scores", None)  # Full profiles remain in eval/; show flagged sites in Studio.
            entity["sequence_evaluation"] = evaluation
    for decision in state.get("sequence_extraction", {}).get("decisions", []):
        for conflict in decision.get("source_conflicts", []):
            template["issues"].append({"kind": "table_text_mismatch",
                "entity_id": entity_ids.get(decision["entity_id"], decision["entity_id"]),
                "chain": decision["chain"], **conflict,
                "sequence_status": decision["status"], "sequence_rationale": decision["rationale"],
                "resolution": "table_cells_gold_standard"})
    for decision in state.get("sequence_extraction", {}).get("decisions", []):
        reviewed = [state["sequence_extraction"]["sources"][f["source_ref"]]
            for f in decision.get("fragments", [])
            if state["sequence_extraction"]["sources"].get(f["source_ref"], {}).get("image_review")]
        if reviewed:
            template["issues"].append({"kind": "sequence_image_review",
                "entity_id": entity_ids.get(decision["entity_id"], decision["entity_id"]),
                "chain": decision["chain"], "sequence_status": decision["status"],
                "sequence_rationale": decision["rationale"], "resolution": "original_table_image",
                "message": "原图复核已修正 OCR，原始识别结果和修正依据已保留。",
                "reviews": [s["image_review"] for s in reviewed]})
    template["issues"].extend({"kind": "dataagent_review", "task_id": task.get("task_id"),
        "message": task.get("message"), "severity": task.get("severity"), "table_id": task.get("table_id")}
        for task in native.get("review_tasks", []))
    refresh_missing(template)
    validate_template(template)
    coverage = coverage_audit(payload, model_input, sources, grids)
    limitations = [warning.get("message") or ": ".join(str(warning.get(key)) for key in ("code", "table_id", "page")
                   if warning.get(key) is not None) for warning in native["warnings"]]
    status = "partial" if native["status"] != "complete" or template["missing"] or template["issues"] or coverage["status"] == "partial" else "complete"
    dataset = {"document_id": document_id, "schema_version": template["schema_version"], "status": status,
        "extraction_mode": "dataagent", "text_completion_status": state.get("sequence_extraction", {}).get("status", "skipped"),
        "entities": structured_entities(template), "records": export_dataset(template),
        "experiments": template.get("experiments", []), "relations": template.get("relations", []),
        "summary": template["summary"], "issues": template["issues"], "coverage": coverage,
        "limitations": limitations, "warnings": native["warnings"], "provenance_file": "template.completed.json",
        "document_report_file": "document_report.json", "dataagent_directory": str(native_dir)}
    dataset["review_tasks"] = native.get("review_tasks", [])
    dataset["question_review"] = state.get("question_review", {})
    dataset["sequence_evaluation"] = state.get("sequence_evaluation", {})
    evaluations = {e["entity_id"]: e.get("sequence_evaluation") for e in template["entities"]}
    for entity in dataset["entities"]:
        origin = next((e for e in template["entities"] if e["entity_id"] == entity["entity_id"]), {})
        entity["sequence_locations"] = copy.deepcopy(origin.get("sequence_locations", {}))
        if evaluations.get(entity["entity_id"]):
            entity["sequence_evaluation"] = evaluations[entity["entity_id"]]
    contexts = {context["table_id"]: context for context in state.get("table_contexts", [])}
    for table in [*prepared["tables"], *prepared.get("unstructured_tables", [])]:
        identity, origin = table["source_id"], table["sda_source"]
        classification = next((c for c in state["table_classifications"] if c["table_id"] == identity), {})
        save_json(output_dir / (stable_id("table-", identity) + ".json"), {
            "document_id": document_id, "table_id": identity, "table_text": origin["text"],
            "table_evidence": origin["evidence"], "image_path": table["image_path"],
            "layout": {"page": table["page"], "bbox": table["bbox"],
                       "bbox_padded": table.get("bbox_padded"), "crop_to_pdf": table.get("crop_to_pdf"),
                       "width_px": table["width"], "height_px": table["height"],
                       "table_context": contexts.get(identity, {})},
            "classification": {"category": classification.get("primary_label"), "reason": classification.get("explanation", "")}})
    report = {"status": status, "extraction_mode": "dataagent", "coverage": coverage,
        "records": len(dataset["records"]), "entities": len(dataset["entities"]),
        "experiments": len(dataset["experiments"]), "relations": len(dataset["relations"]),
        "issues": template["issues"], "limitations": limitations, "native_directory": str(native_dir),
        "verification_scope": "OCR source grounding and result-cell coverage; semantic and biological review remain separate."}
    for name, value in {"dataset.json": dataset, "template.json": template, "template.completed.json": template,
        "document_extraction.json": payload, "document_sources.json": sources, "coverage.json": coverage,
        "document_report.json": report, "data_construction.json": {"records": dataset["records"]},
        "missing_fields.json": {"document_id": document_id, "missing": template["missing"],
                                "summary": template["summary"], "issues": template["issues"]},
        "completion_report.json": {"status": status, "reason": "dataagent_sequence_extraction",
                                   "remaining_missing_fields": len(template["missing"]), "token_usage": usage}}.items():
        save_json(output_dir / name, value)
    return dataset


def run_dataagent_pipeline(image_dir, output_root, text_pages_jsonl, *, table_ocr=None,
                           structured_dir=None, document_id=None, data_dir=None, resume=False,
                           semantic_provider=None, repair_rounds=2, extract_sequences=True, **options):
    """Main-process entry after OCR; all evidence is read from persisted OCR."""
    started = perf_counter()
    image_dir, output_root, pages = Path(image_dir).resolve(), Path(output_root).resolve(), Path(text_pages_jsonl).resolve()
    first = next(json.loads(line) for line in pages.read_text().splitlines() if line.strip())
    identity = document_id or first["document_id"]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{2,79}", identity):
        raise ValueError("无效 document_id")
    structured = Path(structured_dir).resolve() if structured_dir else None
    table_ocr = Path(table_ocr).resolve() if table_ocr else structured / "table_ocr/tables.jsonl" if structured else None
    if table_ocr is None:
        raise ValueError("dataagent 模式需要已有表格 OCR：--reuse-table-ocr-from 或 --structured-dir")
    layout = structured / "layout" / f"{identity}_manifest.json" if structured else image_dir / f"{identity}_manifest.json"
    layout = layout if layout.is_file() else None
    output = output_root / identity
    data_dir = Path(data_dir or image_dir).resolve()
    if data_dir.name != identity and (data_dir / identity).is_dir():
        data_dir = data_dir / identity
    checkpoint_path = output / "dataagent_checkpoint.json"
    checkpoint = read(checkpoint_path, {"document_id": identity, "attempts": []})
    if checkpoint["attempts"] and not resume:
        raise ValueError("已有 dataagent 结果；请使用新目录或 --dataagent-resume")
    if resume and not checkpoint["attempts"]:
        raise ValueError("没有可续跑的 dataagent 输入检查点")
    previous = next((Path(a["directory"]) / "sda_input.json" for a in checkpoint["attempts"]
                     if (Path(a["directory"]) / "sda_input.json").is_file()), None)
    if resume and previous is None:
        raise ValueError("缺少可校验的 dataagent OCR 输入快照，请使用新目录")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    native_dir = output / "dataagent" / stamp
    attempt = {"directory": str(native_dir), "status": "running", "started_at": datetime.now(timezone.utc).isoformat()}
    checkpoint["attempts"].append(attempt)
    checkpoint["inputs"] = {"image_dir": str(image_dir), "table_ocr": str(table_ocr), "text_pages_jsonl": str(pages),
                            "structured_dir": str(structured) if structured else None,
                            "data_dir": str(data_dir)}
    checkpoint["options"] = {"semantic_provider": semantic_provider, "repair_rounds": repair_rounds,
                             "extract_sequences": extract_sequences, **options}
    save_json(checkpoint_path, checkpoint)
    dataset, error, interruption = None, None, None
    try:
        kwargs = {**options, "max_repair_rounds": repair_rounds, "extract_sequences": extract_sequences}
        if semantic_provider:
            kwargs["semantic_provider"] = semantic_provider
        run_from_ocr(identity, table_ocr, pages, native_dir, image_dir=image_dir,
                     layout_manifest=layout, expected_input=previous if resume else None, **kwargs)
        actual_config = read(native_dir / "agent_state.json", {}).get("config", {})
        checkpoint["options"].update({key: actual_config[key] for key in
            ("semantic_provider", "llm_model", "llm_effort", "llm_workers", "sequence_workers") if key in actual_config})
        usage = collect_usage(checkpoint["attempts"], identity)
        dataset = export_sda_result(identity, native_dir, output, data_dir, usage)
        attempt["status"] = dataset["status"]
    except BaseException as exc:
        error = f"{type(exc).__name__}: {exc}"
        if not isinstance(exc, Exception):
            interruption = exc
        attempt.update(status="failed", error=error)
        if not (output / "dataset.json").is_file():
            save_json(output / "dataset.json", {"document_id": identity, "status": "failed", "records": [], "entities": [], "error": error})
    finally:
        attempt["wall_seconds"] = perf_counter() - started
        save_json(checkpoint_path, checkpoint)
        usage = collect_usage(checkpoint["attempts"], identity)
        write_usage_report(output, usage)
        write_usage_report(output_root, usage)
        stages = read(native_dir / "pipeline_performance.json", {}).get("stages", [])
        timing = {"wall_seconds": sum(a.get("wall_seconds", 0) for a in checkpoint["attempts"]),
            "run_mode": "dataagent_existing_ocr", "token_usage": usage,
            "phase_ranking": [{"stage": s["stage"], "seconds": s["elapsed_seconds"], "status": s["status"]} for s in stages],
            "stage_ranking": [], "slowest_requests": [], "events": [],
            "note": "dataagent阶段耗时可并行重叠；续跑总用时累计自动处理区间，全文与表格均复用已有OCR。"}
        write_timing_report(output, timing)
        write_timing_report(output_root, timing)
        summary = {"status": "failed" if error else dataset["status"], "extraction_mode": "dataagent",
            "wall_seconds": timing["wall_seconds"], "token_usage": usage, "errors": {identity: error} if error else {},
            "documents": {identity: {"status": attempt["status"], "records": len((dataset or {}).get("records", []))}},
            "data_construction": {"records": len((dataset or {}).get("records", []))}, "timing_file": str(output / "_timings.json")}
        save_json(output_root / "_batch_summary.json", summary)
    if error:
        print(f"dataagent 提取失败 {identity}: {error}", flush=True)
    if interruption:
        raise interruption
    return summary
