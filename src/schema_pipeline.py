"""Document template, verified sequence completion and dataset export."""

import copy
import hashlib
import json
from pathlib import Path
from time import perf_counter

from units.document_fields import get_document_id
from template_agent import TemplateAgent, export_dataset, refresh_missing, resolve_catalog, validate_template
from units.token_usage import TokenUsageRecorder, write_usage_report, get_usage_context, usage_scope


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
    """只读取已转换、且明确属于本专利的 JSON 序列表。"""
    directory = Path(data_dir)
    candidates = [directory / "wipo/sequences.json", directory / "sequences.json"]
    for path in candidates:
        if path.is_file():
            catalog = json.loads(path.read_text(encoding="utf-8"))
            if catalog.get("document_id") != document_id:
                raise ValueError(f"WIPO 序列表文档号不一致：{path}")
            return catalog
    if any((directory / "wipo").glob("*.xml")):
        raise ValueError("请先将本专利序列表 XML 转换为 sequences.json")
    return None


def load_completion_document(data_dir, document_id, tables, text_pages_jsonl=None):
    """统一正文段落、显式指定的 OCR 行与表格上下文，保留可追溯地址。"""
    document = {"document_id": document_id, "source_file": None, "paragraphs": []}
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
                                 initial_template=None, timing=None,
                                 sequence_agent=False, template_agent_max_calls=12,
                                 template_agent_context_chars=24000):
    """保留文档提取模板，按配置核验序列，再导出带来源的 dataset。"""
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
    for table in tables:
        evidence = table.get("table_evidence")
        if evidence:
            identity = hashlib.sha256(str(table.get("table_id")).encode()).hexdigest()[:20]
            save_json(output_dir / "evidence" / "tables" / f"{identity}.json", evidence)
    report = {"status": "partial" if complete_text and template["missing"] else "skipped",
              "reason": "sequence_completion" if complete_text else "text_completion_disabled",
              "accepted": [], "rejected": [], "candidates": [], "template_agent": sequence_report,
              "errors": sequence_report.get("errors", []),
              "token_usage": recorder.report(document_id=document_id, since=started)}
    if sequence_report["status"] == "partial":
        report["status"] = "partial"
    if catalog:
        resolve_catalog(template, catalog)
    refresh_missing(template)
    validate_template(template)
    records = export_dataset(template)
    report["wall_seconds"] = perf_counter() - started
    report["remaining_missing_fields"] = len(template["missing"])
    report["review_issues"] = copy.deepcopy(template.get("issues", []))
    if template.get("summary", {}).get("slot_status", {}).get("conflict", 0):
        report["status"] = "partial"
    save_json(output_dir / "completion_report.json", report)
    write_usage_report(output_dir, report["token_usage"])
    save_json(output_dir / "missing_fields.json", {"document_id": document_id,
                                                   "missing": template["missing"],
                                                   "summary": template.get("summary", {}),
                                                   "issues": template.get("issues", [])})
    dataset_status = "partial" if template["missing"] or template.get("issues") or report["status"] == "partial" else "complete"
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
