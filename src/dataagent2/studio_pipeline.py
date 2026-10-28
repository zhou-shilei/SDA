"""Bridge fresh SDA OCR, shared template stages and dataagent2 to Studio exports.

The standalone benchmark remains independent. This adapter reuses SDA's evidence
and export contracts; it never runs dataagent1 sequence extraction or repair.
"""

from __future__ import annotations

import copy
from collections import defaultdict
import json
from pathlib import Path
import re
from time import perf_counter

from .inputs import load_input, read
from .pipeline import run
from .provider import Provider, write_json


def collect_usage(seed_dir, reader_dir, document_id):
    from dataagent.main_pipeline import collect_usage as seed_usage
    from units.token_usage import TokenUsageRecorder

    shared = seed_usage([{"directory": str(seed_dir)}], document_id)
    recorder = TokenUsageRecorder(shared["events"])
    seen = set()
    calls = Path(reader_dir) / "calls.jsonl"
    for line in calls.read_text().splitlines() if calls.is_file() else []:
        call = json.loads(line)
        if call["attempt_id"] in seen:
            continue
        seen.add(call["attempt_id"])
        recorder.record(call.get("usage"), stage="dataagent2-" + call["stage"],
            document_id=document_id, item_id=document_id, attempt=call.get("attempt", 1),
            status="ok" if call["status"] == "ok" else "failed", request_id=call["attempt_id"])
    return recorder.report(document_id=document_id)


def export_result(data, final, seed_dir, output):
    from dataagent.main_pipeline import _sources_and_coverage, stable_id
    from document.document_extraction import template_from_payload, coverage_audit, provenance
    from schema_pipeline import structured_entities
    from template_agent import export_dataset, refresh_missing, validate_template

    state, prepared = read(seed_dir / "agent_state.json"), read(seed_dir / "sda_input.json")
    sources, model_input, grids = _sources_and_coverage(state, prepared)
    addressed = {}
    for sid, source in data["sources"].items():
        ref = source["source_ref"]
        if ref not in sources or sources[ref].get("text") != source["text"]:
            original = ref
            ref = f"dataagent2:source:{sid}"
            sources[ref] = {**copy.deepcopy(sources.get(original, {})), **copy.deepcopy(source),
                "ref": ref, "source_id": ref, "source_ref": original, "original_ref": original,
                "text": source["text"], "quote": source["text"], "document_id": data["document_id"]}
        addressed[sid] = ref

    def refs(ids):
        return list(dict.fromkeys(addressed[s] for s in ids if s in addressed))

    def fragment_refs(sequence):
        result = []
        for fragment in sequence.get("fragments", []):
            sid = fragment["s"]
            ref = f"dataagent2:fragment:{sid}:{fragment['start']}:{fragment['end']}"
            sources[ref] = {**copy.deepcopy(sources[addressed[sid]]),
                "ref": ref, "source_id": ref, "source_ref": data["sources"][sid]["source_ref"], "original_ref": addressed[sid],
                "source_char_start": fragment["start"], "source_char_end": fragment["end"],
                "text": fragment["quote"], "quote": fragment["quote"]}
            if fragment.get("notation"):
                sources[ref].update(sequence_notation=copy.deepcopy(fragment["notation"]),
                                    normalized_sequence=fragment["sequence"])
            result.append(ref)
        return result

    entities = []
    for item in final["entities"]:
        entity = {"id": item["id"], "NAME": item["name"],
            "ALIASES": list(dict.fromkeys(n for n in item["names"] if n != item["name"])),
            "CONSTRUCT_TYPE": None, "NOTES": None, "VH": None, "VL": None,
            "VH_SEQ_ID": None, "VL_SEQ_ID": None, "SEQUENCE_LINKS": [],
            "EVIDENCE": {"NAME": refs(item.get("refs", [])), "VH": [], "VL": []}}
        for chain in ("VH", "VL", "H_CHAIN", "L_CHAIN"):
            sequence = item["sequences"][chain]
            if sequence["status"] != "supported":
                continue
            citations = fragment_refs(sequence)
            if chain in ("VH", "VL"):
                entity[chain] = sequence["value"]
                entity[chain + "_SEQ_ID"] = sequence.get("seq_id") if sequence.get("seq_id_scope") != "full_chain" else None
                entity["EVIDENCE"][chain] = list(dict.fromkeys(citations + refs(sequence.get("mapping_refs", []))))
            else:
                entity["SEQUENCE_LINKS"].append({"region": "H_chain" if chain == "H_CHAIN" else "L_chain",
                    "seq_id": sequence.get("seq_id"), "SOURCE_REFS": citations})
        entities.append(entity)
    # Several chunks can describe the same experiment. Keep its stable identity
    # and all variants; conflicting metadata must not overwrite another source.
    experiment_groups = defaultdict(list)
    for experiment in final["experiments"]:
        experiment_groups[experiment["id"]].append(experiment)
    experiments, experiment_issues = [], []
    for identity, variants in experiment_groups.items():
        experiment = {"id": identity, "NAME": identity, "KEYWORDS": [], "TAGS": [],
            "EVIDENCE": refs([sid for e in variants for sid in e.get("refs", [])]),
            "SOURCE_VARIANTS": copy.deepcopy(variants)}
        ambiguous = []
        for field in ("method", "target", "species", "system", "conditions"):
            values = list(dict.fromkeys(e[field] for e in variants if e.get(field)))
            experiment[field.upper()] = values[0] if len(values) == 1 else None
            if len(values) > 1:
                ambiguous.append(field)
        if ambiguous:
            experiment_issues.append({"kind": "dataagent2_experiment_variants", "experiment_id": identity,
                "fields": ambiguous, "message": "同一实验存在多条来源描述，差异字段保留待复核；完整描述见 SOURCE_VARIANTS。"})
        experiments.append(experiment)
    names = {e["id"]: e["NAME"] for e in entities}
    records = [{"ENTITY_ID": o["item"], "NAME": names[o["item"]], "METRIC": o["metric"],
        "VALUE": o["value"], "UNIT": o.get("unit"), "TARGET": None, "METHOD": None,
        "CONDITIONS": None, "EXPERIMENT_ID": o.get("experiment"),
        "CELL_REFS": refs([o["s"]]) if data["sources"][o["s"]]["kind"] == "cell" else [],
        "EVIDENCE": {"MEASUREMENT": refs([o["s"]]), "CONTEXT": refs(o.get("refs", []))}, "NOTES": None}
        for o in final["observations"]]
    relations = [{"subject_id": r["subject"], "object_id": r["object"], "type": r["relation"],
        "DETAILS": None, "EVIDENCE": refs(r.get("refs", []))} for r in final["relations"]]
    dispositions = []
    for c in state.get("table_classifications", []):
        metrics = [m for region in c.get("regions", []) for m in region.get("metrics", []) +
                   [m for group in region.get("entity_groups", []) for m in group.get("metrics", [])]]
        dispositions.append({"source_id": "table:" + c["table_id"],
            "status": "ambiguous" if c.get("decision") == "needs_review" else "measurements_extracted" if metrics else "no_measurements",
            "measurement_columns": sorted({m["value_column"] for m in metrics if m.get("value_column") is not None}),
            "header_rows": sorted({r for region in c.get("regions", []) for r in region.get("header_rows", [])}),
            "reason": c.get("explanation", "")})
    payload = {"entities": entities, "experiments": experiments, "records": records,
               "relations": relations, "table_coverage": dispositions, "limitations": []}
    template = template_from_payload(data["document_id"], payload, sources)
    template["agent"] = {"name": "dataagent2", "mode": "existing_ocr", "native_directory": str(output / "dataagent2")}
    template["issues"].extend(experiment_issues)
    template["issues"].extend({"kind": "dataagent2_conflict", **c} for c in final["conflicts"])
    template["issues"].extend({"kind": "dataagent2_issue", **i} for i in final["issues"])
    native_items = {item["id"]: item for item in final["entities"]}
    for exported in template["entities"]:
        item = native_items[exported["original_entity_id"]]
        for chain in ("CH", "CL"):
            sequence = item["sequences"].get(chain, {})
            if sequence.get("status") != "supported":
                continue
            citations = fragment_refs(sequence)
            exported["other_sequences"].append({"record": {"region": chain,
                "seq_id": sequence.get("seq_id"), "sequence": sequence["value"],
                "evidence": provenance(list(dict.fromkeys(citations + refs(sequence.get("mapping_refs", [])))), sources)},
                "source": {"document_id": data["document_id"], "source_type": "dataagent2",
                           "record_type": "sequence_records", "region": chain}})
        for chain in ("VH", "VL"):
            sequence = item["sequences"][chain]
            if sequence.get("failure_kind") == "ocr_failure":
                template["issues"].append({"kind": "table_sequence_ocr_failed", "entity_id": exported["entity_id"],
                    "chain": chain, "seq_ids": sequence.get("seq_ids", []),
                    "evidence_refs": refs(sequence["failed_source_ids"]),
                    "recovery_action": "rerun_table_sequence_ocr",
                    "message": "表格序列复识别失败，需要重新识别原图；编号映射已保留。"})
            if sequence.get("boundary_method") == "printed_image_style" and sequence["status"] == "supported":
                style = next(data["sources"][f["s"]]["sequence_style"] for f in sequence["fragments"]
                             if data["sources"][f["s"]].get("sequence_style"))
                template["issues"].append({"kind": "sequence_image_review", "entity_id": exported["entity_id"],
                    "chain": chain, "source_scope": "full_chain", "source_seq_id": sequence.get("seq_id"),
                    "boundary_method": "printed_image_style",
                    "annotation_quote": style["annotation_quote"], "original_ref": style["original_ref"],
                    "sequence_rationale": f"原图“{style['annotation_quote']}”标明 {chain} 范围（{len(sequence['value'])} aa）；"
                                          f"对应完整链 SEQ ID NO:{sequence.get('seq_id')}。",
                    "evidence_refs": list(entity_ref for p in exported[chain]["sequence"].get("provenance", [])
                                          for entity_ref in [e.get("ref") for e in p.get("extraction_evidence", [])] if entity_ref)})
    if final["rejected_facts"]:
        template["issues"].append({"kind": "dataagent2_rejected_facts", "count": len(final["rejected_facts"]),
                                  "message": "部分候选事实未通过来源校验；详见 dataagent2/dataset.json。"})
    refresh_missing(template)
    validate_template(template)
    coverage = coverage_audit(payload, model_input, sources, grids)
    status = "partial" if final["status"] != "source_supported" or template["missing"] or template["issues"] or coverage["status"] == "partial" else "complete"
    dataset = {"document_id": data["document_id"], "schema_version": template["schema_version"],
        "status": status, "extraction_mode": "dataagent2", "extraction_engine": "dataagent2",
        "entities": structured_entities(template), "records": export_dataset(template),
        "experiments": template["experiments"], "relations": template["relations"],
        "summary": template["summary"], "issues": template["issues"], "coverage": coverage,
        "reader_coverage": final["reader_coverage"], "warnings": data["input_warnings"],
        "limitations": ["原文字符校验不等于归属、边界或生物学验证；未解决项目保留待复核。"],
        "provenance_file": "template.completed.json", "document_report_file": "document_report.json"}
    for table in [*prepared["tables"], *prepared.get("unstructured_tables", [])]:
        identity, origin = table["source_id"], table["sda_source"]
        classification = next((c for c in state["table_classifications"] if c["table_id"] == identity), {})
        write_json(output / (stable_id("table-", identity) + ".json"), {
            "document_id": data["document_id"], "table_id": identity, "table_text": origin["text"],
            "table_evidence": origin["evidence"], "image_path": table["image_path"],
            "layout": {"page": table["page"], "bbox": table["bbox"],
                       "bbox_padded": table.get("bbox_padded"), "crop_to_pdf": table.get("crop_to_pdf"),
                       "width_px": table["width"], "height_px": table["height"]},
            "classification": {"category": classification.get("primary_label"), "reason": classification.get("explanation", "")}})
    for name, value in {"dataset.json": dataset, "template.json": template, "template.completed.json": template,
        "document_extraction.json": payload, "document_sources.json": sources, "coverage.json": coverage,
        "document_report.json": {"status": status, "extraction_mode": "dataagent2", "coverage": coverage,
                                 "reader_coverage": final["reader_coverage"], "issues": template["issues"],
                                 "limitations": dataset["limitations"]},
        "data_construction.json": {"records": dataset["records"]},
        "missing_fields.json": {"document_id": data["document_id"], "missing": template["missing"],
                                "summary": template["summary"], "issues": template["issues"]}}.items():
        write_json(output / name, value)
    return dataset


def refresh_table_sequences(table_ocr, output, *, image_dir=None, retry_failed=True):
    """Upgrade stale OCR and retry failed tables once, in isolated new assets."""
    import shutil
    from dataagent.sda_inputs import _records, _resolve_image
    from document.pdf2doc import _ground_table_cells, parse_grid
    from document.sequence_ocr import VERSION, REVISION, sequence_candidates, refine
    rows, changed = [], False
    for raw, source in _records(table_ocr):
        evidence = raw.get("table_evidence") or raw
        audit = evidence.get("sequence_ocr", {})
        text = evidence.get("vl_text") or evidence.get("text") or raw.get("table_text", "")
        stale = (audit.get("version"), audit.get("revision")) != (VERSION, REVISION)
        failed = any(c.get("status") == "failed" for c in audit.get("cells", []))
        if evidence.get("backend") == "vl" and (stale or retry_failed and failed) and sequence_candidates(parse_grid(text, evidence["table_id"])):
            evidence = copy.deepcopy(evidence)
            image = _resolve_image(evidence.get("image_path"), source, image_dir)
            if image is None:
                raise ValueError(f"{evidence['table_id']}: 旧序列 OCR 需要重识别，但原始表格裁图不可用")
            # refine saves crops beside the image. Never write them into an old run.
            assets = Path(output).parent / "sequence_ocr_inputs"
            assets.mkdir(parents=True, exist_ok=True)
            copied = assets / (evidence["table_id"] + "-" + image.name)
            if image.is_file():
                shutil.copy2(image, copied)
                image = copied
            prior_cells = {c["ref"]: c for c in evidence.get("cells", [])
                           if not stale and c.get("sequence_ocr", {}).get("status") == "success"}
            prior_audit = copy.deepcopy(audit)
            evidence.update(image_path=str(image), text=text)
            evidence = refine(evidence)
            # A failed retry cannot erase a previously recognized cell. Keep
            # the prior evidence and its own crop hashes rather than the old VL text.
            from lxml import html
            tree = html.fromstring(evidence["text"])
            physical = [c for row in parse_grid(evidence["text"], evidence["table_id"])["physical"] for c in row]
            elements = dict(zip((c["ref"] for c in physical), tree.xpath(".//tr/td|.//tr/th")))
            restored = []
            for cell in evidence.get("cells", []):
                old = prior_cells.get(cell["ref"])
                if old and cell.get("sequence_ocr", {}).get("status") == "failed":
                    cell.update(copy.deepcopy(old))
                    node = elements[cell["ref"]]
                    for child in list(node):
                        node.remove(child)
                    node.text = old["text"]
                    restored.append(cell["ref"])
            if restored:
                import hashlib
                evidence["text"] = html.tostring(tree, encoding="unicode")
                evidence["text_sha256"] = hashlib.sha256(evidence["text"].encode()).hexdigest()
                fresh = evidence["sequence_ocr"]
                fresh["cells"] = [copy.deepcopy(c["sequence_ocr"]) for c in evidence["cells"] if c.get("sequence_ocr")]
                fresh["successful_cells"] = sum(c["status"] == "success" for c in fresh["cells"])
                fresh["failed_cells"] = sum(c["status"] == "failed" for c in fresh["cells"])
                fresh["status"] = "partial" if fresh["failed_cells"] else "complete"
                fresh["preserved_prior_cells"] = restored
            evidence["sequence_ocr"]["previous_attempt"] = {k: prior_audit.get(k) for k in ("version", "revision", "status", "failed_cells")}
            _ground_table_cells(evidence, evidence)
            changed = True
            if "table_evidence" in raw:
                raw = {**raw, "table_evidence": evidence, "table_text": evidence["text"]}
            else:
                raw = evidence
        rows.append(raw)
    if not changed:
        return Path(table_ocr)
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    return output


def run_studio_pipeline(image_dir, output_root, text_pages_jsonl, *, table_ocr=None,
                        structured_dir=None, document_id=None, data_dir=None, resume=False,
                        semantic_provider=None, extract_sequences=True, **options):
    from dataagent.run_sda import run_from_ocr
    from units.token_usage import write_usage_report
    from units.timing import write_timing_report

    if resume:
        raise ValueError("dataagent2 网页重解析请使用新运行目录并复用 OCR；不支持 dataagent1 续跑检查点")
    if semantic_provider not in {None, "codex", "rules"}:
        raise ValueError("Unsupported semantic provider")
    started = perf_counter()
    image_dir, output_root = Path(image_dir).resolve(), Path(output_root).resolve()
    pages = Path(text_pages_jsonl).resolve()
    identity = document_id or json.loads(next(l for l in pages.read_text().splitlines() if l.strip()))["document_id"]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{2,79}", identity):
        raise ValueError("无效 document_id")
    structured = Path(structured_dir).resolve() if structured_dir else None
    table_ocr = Path(table_ocr).resolve() if table_ocr else structured / "table_ocr/tables.jsonl" if structured else None
    if table_ocr is None:
        raise ValueError("dataagent2 需要已保存的表格 OCR")
    output = output_root / identity
    if output.exists() and any(output.iterdir()):
        raise ValueError("dataagent2 需要全新输出目录，以保留之前的运行结果")
    seed_dir, reader_dir = output / "shared_seed", output / "dataagent2"
    layout = (structured / "layout" if structured else image_dir) / f"{identity}_manifest.json"
    output.mkdir(parents=True, exist_ok=True)
    write_json(output / "dataagent2_checkpoint.json", {"document_id": identity, "inputs": {
        "image_dir": str(image_dir), "table_ocr": str(table_ocr), "text_pages_jsonl": str(pages),
        "structured_dir": str(structured) if structured else None}})
    error, dataset = None, {}
    try:
        table_ocr = refresh_table_sequences(table_ocr, output / "table_ocr.refreshed.jsonl", image_dir=image_dir)
        seed_options = {k: v for k, v in options.items() if k in ("llm_model", "llm_effort", "llm_workers")}
        seed_state = run_from_ocr(identity, table_ocr, pages, seed_dir, image_dir=image_dir,
            layout_manifest=layout if layout.is_file() else None, seed_only=True,
            semantic_provider=semantic_provider or "codex", **seed_options)
        data = load_input(seed_dir)
        config = seed_state["config"]
        provider = Provider(reader_dir, model=config["llm_model"], effort=config["llm_effort"],
                            timeout=config["llm_timeout"])
        # rules is exclusively an offline integration check, matching dataagent1.
        if semantic_provider == "rules":
            from .ledger import Ledger
            final = Ledger(data).assemble()
            final.update(status="partial", reader_coverage={"total_chunks": 0, "completed_chunks": 0,
                "total_sources": len(data["sources"]), "core_sources_read": 0, "scope": "Offline rules check; reader not run"})
            write_json(reader_dir / "dataset.json", final)
        else:
            report = run(data, provider, reader_dir, workers=options.get("sequence_workers") or 1,
                         max_questions=options.get("max_answer_questions") if extract_sequences else 0)
            final = read(reader_dir / "dataset.json")
            if not report["benchmark_complete"]:
                error = "dataagent2 全文读取或问答请求未完成；已保存部分结果及原始请求记录"
        dataset = export_result(data, final, seed_dir, output)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        if not (output / "dataset.json").is_file():
            write_json(output / "dataset.json", {"document_id": identity, "status": "failed",
                "extraction_mode": "dataagent2", "entities": [], "records": [], "error": error})
    finally:
        usage = collect_usage(seed_dir, reader_dir, identity)
        elapsed = perf_counter() - started
        timing = {"wall_seconds": elapsed, "run_mode": "dataagent2_existing_ocr", "token_usage": usage,
            "phase_ranking": [], "stage_ranking": [], "slowest_requests": [], "events": [],
            "note": "dataagent2：包含本次共享表格分类/模板及分块提取、有限问答；复用已保存 OCR。"}
        for target in (output, output_root):
            write_usage_report(target, usage)
            write_timing_report(target, timing)
        summary = {"status": "failed" if error else dataset.get("status", "failed"),
            "extraction_mode": "dataagent2", "wall_seconds": elapsed, "token_usage": usage,
            "errors": {identity: error} if error else {},
            "documents": {identity: {"status": dataset.get("status", "failed"), "records": len(dataset.get("records", []))}}}
        write_json(output_root / "_batch_summary.json", summary)
    if error:
        print(f"dataagent2 提取失败 {identity}: {error}", flush=True)
    return summary
