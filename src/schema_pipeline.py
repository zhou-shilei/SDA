"""固定抗体 schema → 缺失字段 → TST 正文补全；可复用已有表格结果。"""

import argparse
import asyncio
import copy
import hashlib
import json
from pathlib import Path
from time import perf_counter

from costructure import get_document_id, load_table_results
from template_agent import TemplateAgent, export_dataset, refresh_missing, resolve_catalog, validate_template
from text_sequences import load_document
from token_usage import TokenUsageRecorder, write_usage_report, get_usage_context, usage_scope


def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def structured_entities(template):
    """数据集用自然值；槽位状态、冲突候选和逐字段来源保存在 completed 模板。"""
    def value(slot):
        return copy.deepcopy(slot["value"]) if slot["status"] == "present" else None
    entities = []
    for entity in template["entities"]:
        item = {"entity_id": entity["entity_id"], "NAME": entity["name"]}
        for key in ("original_entity_id", "aliases", "construct_type", "notes", "other_sequences", "sequence_links", "sequence_components"):
            if key in entity:
                item[key] = copy.deepcopy(entity[key])
        for chain in ("VH", "VL", "H_CHAIN", "L_CHAIN"):
            item[chain] = {field: value(slot) for field, slot in entity[chain].items()}
        item["METRIC"] = [{"observation_id": observation["observation_id"], "label": observation["label"],
                           **{field: value(observation[field]) for field in
                              ("metric", "value", "unit", "conditions", "experiment_type", "target")},
                           "source": copy.deepcopy(observation["source"]),
                           **{k: copy.deepcopy(observation[k]) for k in ("exp_id", "method", "keywords") if k in observation}}
                          for observation in entity["METRIC"]]
        entities.append(item)
    return entities


def load_sequence_catalog(data_dir, document_id):
    """只读取本专利序列表；原始 XML 可直接转为经过长度校验的目录。"""
    directory = Path(data_dir)
    candidates = [directory / "wipo/sequences.json", directory / "sequences.json"]
    for path in candidates:
        if path.is_file():
            catalog = json.loads(path.read_text(encoding="utf-8"))
            if catalog.get("document_id") != document_id:
                raise ValueError(f"WIPO 序列表文档号不一致：{path}")
            return catalog
    xml_files = sorted((directory / "wipo").glob("*.xml"))
    if len(xml_files) > 1:
        raise ValueError("发现多个序列表 XML，请先转换并指定本专利的 sequences.json")
    if xml_files:
        from convert_st26 import parse_listing
        return parse_listing(xml_files[0], document_id)
    return None


def load_completion_document(data_dir, document_id, tables, text_pages_jsonl=None):
    """统一正文段落、显式指定的 OCR 行与表格上下文，保留可追溯地址。"""
    document = load_document(data_dir, document_id)
    if document is None:
        document = {"document_id": document_id, "source_file": None, "paragraphs": []}
    document = copy.deepcopy(document)
    source_files = [document["source_file"]] if document.get("source_file") else []
    paragraphs = document["paragraphs"]
    seen = {row["id"] for row in paragraphs}
    if text_pages_jsonl:
        path = Path(text_pages_jsonl)
        source_files.append(str(path.resolve()))
        for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not raw.strip():
                continue
            page = json.loads(raw)
            if page.get("document_id") != document_id:
                raise ValueError(f"OCR 正文文档号不一致：{path}:{number}")
            if page.get("status") not in {None, "success", "complete"}:
                document.setdefault("issues", []).append({"type": "ocr_page_failed", "page": page.get("page_number")})
                continue
            for line in page.get("lines", []):
                identity, text = line.get("line_id"), line.get("text")
                if not isinstance(identity, str) or not isinstance(text, str):
                    raise ValueError(f"OCR 行缺少 line_id/text：{path}:{number}")
                identity = "ocr:" + identity
                if identity in seen:
                    raise ValueError(f"正文地址重复：{identity}")
                seen.add(identity)
                if text.strip():
                    paragraphs.append({"id": identity, "text": text,
                                       "pdf_page": page.get("page_number"),
                                       "bbox": line.get("bbox"), "source_type": "pdf_ocr",
                                       "source_file": str(path.resolve()),
                                       "ocr_backend": page.get("backend", "paddle"),
                                       **{k: line[k] for k in ("block_id", "block_label", "block_order", "granularity") if k in line}})
    for index, table in enumerate(tables):
        if get_document_id(table) != document_id:
            raise ValueError("禁止跨专利拼接表格与正文")
        text = table.get("table_text")
        if not isinstance(text, str) or not text.strip() or table.get("error") or table.get("error_stage"):
            continue
        identity = f"table:{index}:{table.get('table_id', index)}"
        if identity in seen:
            raise ValueError(f"正文地址重复：{identity}")
        seen.add(identity)
        paragraphs.append({"id": identity, "text": text, "table_id": table.get("table_id"),
                           "source_type": "table_context", "source_file": table.get("result_file")})
    document["source_files"] = source_files
    document["source_sha256"] = hashlib.sha256(json.dumps(
        paragraphs, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    document["source_sha256_scope"] = "combined_document_index"
    return document


async def build_document_dataset(document_id, tables, data_dir, output_dir, *, llm=None,
                                 complete_text=True, text_pages_jsonl=None,
                                 concurrency=4, max_tasks=200, initial_template=None, timing=None,
                                 page_evidence=True, page_ocr_max_pages=8, page_ocr_device="cpu",
                                 page_ocr_cpu_threads=4, sequence_mapping_max_calls=8, run_tst=True,
                                 sequence_agent=False, template_agent_max_calls=12,
                                 template_agent_context_chars=24000):
    """表格是记录来源，补全过程只修改缺失槽；所有阶段产物独立保存。"""
    from tst_completion import TSTCompletionAgent

    started = perf_counter()
    output_dir = Path(output_dir)
    catalog = load_sequence_catalog(data_dir, document_id)
    template = copy.deepcopy(initial_template) if initial_template is not None else TemplateAgent().run(
        document_id, tables, sequence_catalog=catalog)
    if template.get("document_id") != document_id:
        raise ValueError("模板与正文文档号不一致")
    save_json(output_dir / "template.json", template)
    save_json(output_dir / "missing_fields.before.json", {"document_id": document_id,
                                                          "missing": template["missing"]})
    mapping_report = {"status": "skipped", "accepted": [], "errors": [], "page_issues": []}
    recorder = get_usage_context().get("recorder") or (
        timing.usage_recorder if timing is not None else TokenUsageRecorder())
    sequence_report = {"status": "skipped", "reason": "sequence_agent_disabled"}
    if complete_text and sequence_agent:
        from template_sequence_agent import document_from_sources
        source_path = output_dir / "document_sources.json"
        if initial_template is not None and source_path.is_file():
            document = document_from_sources(document_id, json.loads(source_path.read_text()))
        else:
            document = load_completion_document(data_dir, document_id, tables, text_pages_jsonl)
        if llm is None:
            from agent import codexllm
            llm = codexllm()
        with usage_scope(recorder=recorder, document_id=document_id):
            sequence_result = await TemplateAgent().complete(template, document, llm, output_dir,
                sequence_catalog=catalog, max_calls=template_agent_max_calls,
                max_context_chars=template_agent_context_chars, timing=timing)
        template, sequence_report = sequence_result["template"], sequence_result["report"]
    if complete_text and page_evidence:
        from page_evidence import PageEvidenceStore
        from sequence_mapping import complete_sequence_mappings
        store = await asyncio.to_thread(PageEvidenceStore, document_id, data_dir, output_dir,
            pages_jsonl=text_pages_jsonl, device=page_ocr_device, cpu_threads=page_ocr_cpu_threads,
            max_ocr_pages=page_ocr_max_pages)
        if store.pages:
            if llm is None:
                from agent import codexllm
                llm = codexllm()
            with usage_scope(recorder=recorder, document_id=document_id):
                mapping_report = await complete_sequence_mappings(template, store, catalog, llm,
                    output_dir, max_calls=sequence_mapping_max_calls, timing=timing)
        else:
            mapping_report["status"] = "skipped_no_pages"
        save_json(output_dir / "sequence_mapping.json", mapping_report)
    for table in tables:
        evidence = table.get("table_evidence")
        if evidence:
            identity = hashlib.sha256(str(table.get("table_id")).encode()).hexdigest()[:20]
            save_json(output_dir / "evidence" / "tables" / f"{identity}.json", evidence)
    if complete_text and run_tst:
        document = load_completion_document(data_dir, document_id, tables, text_pages_jsonl)
        if llm is None:
            from agent import codexllm
            llm = codexllm()
        with usage_scope(recorder=recorder, document_id=document_id):
            result = await TSTCompletionAgent(llm, concurrency=concurrency, max_tasks=max_tasks, timing=timing).run(
                template, document, output_dir=output_dir)
        template, report = result["template"], result["report"]
    else:
        report = {"status": "partial" if complete_text and template["missing"] else "skipped",
                  "reason": "mapping_only" if complete_text else "text_completion_disabled",
                  "accepted": [], "rejected": [], "candidates": [],
                  "token_usage": TokenUsageRecorder().report()}
    report["sequence_mapping"] = mapping_report
    report["template_agent"] = sequence_report
    if sequence_report["status"] == "partial" and report["status"] != "failed":
        report["status"] = "partial"
    report["token_usage"] = recorder.report(document_id=document_id, since=started)
    report.setdefault("errors", []).extend({"stage": "sequence_mapping", **e} for e in mapping_report["errors"])
    report["page_evidence_issues"] = mapping_report.get("page_issues", [])
    if report["status"] == "failed" and mapping_report["accepted"]:
        report["status"] = "partial"
    if mapping_report["errors"] or report["page_evidence_issues"]:
        report["status"] = "partial" if report["status"] != "failed" else "failed"
    if catalog:
        resolve_catalog(template, catalog)
    refresh_missing(template)
    validate_template(template)
    records = export_dataset(template)
    report["wall_seconds"] = perf_counter() - started
    report["remaining_missing_fields"] = len(template["missing"])
    report["review_issues"] = copy.deepcopy(template.get("issues", []))
    if report["status"] == "ok" and template.get("summary", {}).get("slot_status", {}).get("conflict", 0):
        report["status"] = "partial"
    save_json(output_dir / "tst_completion.json", report)
    write_usage_report(output_dir, report["token_usage"])
    save_json(output_dir / "missing_fields.json", {"document_id": document_id,
                                                   "missing": template["missing"],
                                                   "summary": template.get("summary", {}),
                                                   "issues": template.get("issues", [])})
    dataset_status = "failed" if report["status"] == "failed" else (
        "partial" if template["missing"] or template.get("issues") or report["status"] == "partial" else "complete")
    dataset = {"document_id": document_id, "schema_version": template["schema_version"],
               "status": dataset_status, "text_completion_status": report["status"],
               "entities": structured_entities(template), "records": records,
               "experiments": copy.deepcopy(template.get("experiments", [])),
               "relations": copy.deepcopy(template.get("relations", [])),
               "provenance_file": "template.completed.json",
               "summary": template.get("summary", {}), "issues": template.get("issues", [])}
    for key in ("sequence_definitions", "sequence_audit"):
        if key in template:
            dataset[key] = copy.deepcopy(template[key])
    save_json(output_dir / "dataset.json", dataset)
    save_json(output_dir / "template.completed.json", template)
    return {"template": template, "report": report, "records": records, "dataset": dataset}


def tables_from_extraction(path, document_id):
    """适配已有 OCR→Codex 六字段结果，保留其编号、条件和引用，不重新识别。"""
    path = Path(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("document_id") not in {None, document_id}:
        raise ValueError("提取结果文档号不一致")
    tables, sequence_rows = [], []
    entities = {row.get("id"): row for row in payload.get("entities", []) if row.get("id") is not None}
    for index, row in enumerate(payload.get("records", [])):
        metric = row.get("METRIC") or ("IC50" if "IC50" in row else None)
        assay = {"name": row.get("NAME"), "metric": metric,
                 "value": row.get("VALUE", row.get("IC50")), "unit": row.get("UNIT"),
                 "experiment_type": row.get("METHOD"), "target": row.get("TARGET"),
                 "conditions": row.get("CONDITIONS"), "evidence": row.get("EVIDENCE", {})}
        entity = entities.get(row.get("ENTITY_ID"))
        if entity:
            assay.update(entity_id=entity["id"], canonical_name=entity.get("NAME"),
                         aliases=entity.get("ALIASES", []), measurement_name=row.get("NAME"))
        tables.append({"document_id": document_id, "table_id": f"extraction:{index}:{row.get('TABLE')}",
                       "caption": row.get("TABLE"),
                       "result_file": str(path.resolve()), "assay_records": [assay], "sequence_records": []})
    for row in [*payload.get("records", []), *payload.get("entities", [])]:
        for chain in ("VH", "VL"):
            if row.get(chain) is not None or row.get(chain + "_SEQ_ID") is not None:
                sequence_rows.append({"name": row.get("NAME"), "region": chain,
                                      "sequence": row.get(chain), "seq_id": row.get(chain + "_SEQ_ID"),
                                      "evidence": row.get("EVIDENCE", {}),
                                      **({"entity_id": row.get("id"), "aliases": row.get("ALIASES", [])}
                                         if row.get("id") is not None else
                                         {"entity_id": row["ENTITY_ID"]} if row.get("ENTITY_ID") in entities else {})})
    tables.append({"document_id": document_id, "table_id": "extraction:sequences",
                   "result_file": str(path.resolve()), "assay_records": [], "sequence_records": sequence_rows})
    return tables


async def main_async(args):
    tables = tables_from_extraction(args.source_extraction, args.document_id) if args.source_extraction else [
        table for table in load_table_results(args.input_dir) if get_document_id(table) == args.document_id]
    if not tables:
        raise ValueError("未找到此专利的表格提取结果")
    result = await build_document_dataset(args.document_id, tables, args.data_dir, args.output_dir,
                                          complete_text=not args.no_retrieval,
                                          text_pages_jsonl=args.text_pages_jsonl,
                                          concurrency=args.concurrency, max_tasks=args.max_tasks,
                                          page_evidence=not args.no_page_evidence,
                                          page_ocr_max_pages=args.page_ocr_max_pages,
                                          sequence_mapping_max_calls=args.sequence_mapping_max_calls,
                                          page_ocr_device=args.page_ocr_device, run_tst=not args.mapping_only)
    print(json.dumps({"document_id": args.document_id, "status": result["dataset"]["status"],
                      "text_completion_status": result["report"]["status"],
                      "records": len(result["records"]), "summary": result["template"].get("summary"),
                      "output_dir": str(args.output_dir.resolve())}, ensure_ascii=False, indent=2))
    return int(result["report"]["status"] == "failed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--document-id", required=True)
    parser.add_argument("--input-dir", type=Path, default=Path("results"))
    parser.add_argument("--source-extraction", type=Path, help="可选：已有 OCR→Codex extraction.json")
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--text-pages-jsonl", type=Path, help="可选：同篇专利逐页 OCR 行")
    parser.add_argument("--no-retrieval", action="store_true", help="仅构建 schema、映射序列表和检查缺失，不调用模型")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--max-tasks", type=int, default=200)
    parser.add_argument("--no-page-evidence", action="store_true", help="关闭额外的 PDF 页面证据与序列映射")
    parser.add_argument("--page-ocr-max-pages", type=int, default=8, help="新增页面 OCR 上限；0 仅复用文字层/指定 OCR")
    parser.add_argument("--sequence-mapping-max-calls", type=int, default=8)
    parser.add_argument("--page-ocr-device", default="cpu")
    parser.add_argument("--mapping-only", action="store_true", help="仅运行页面序列映射，跳过通用 TST 请求")
    args = parser.parse_args()
    if args.concurrency < 1 or args.max_tasks < 1:
        parser.error("concurrency 和 max-tasks 必须为正数")
    if args.page_ocr_max_pages < 0 or args.sequence_mapping_max_calls < 1:
        parser.error("page-ocr-max-pages 必须非负，sequence-mapping-max-calls 必须为正")
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
