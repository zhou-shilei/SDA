"""Evidence-grounded antibody-domain adaptation of TST (ACL 2025).

The paper's schema traversal, type-aware ordered retrieval, dynamic Counter and
ordinal Extractor are implemented here. This does not reuse its CPL-trained
weights or claim reproduction of its dense-retrieval experiments. Recognizable
antibody/assay types are supplemented by lexical scope retrieval. New text-only
measurements are review candidates, never silently merged into observed rows.
"""

import asyncio
import copy
import json
import math
import re
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from time import perf_counter
from token_usage import TokenUsageRecorder, get_usage_context, usage_scope, write_usage_report


TYPE_PATTERNS = {
    "sequence": re.compile(r"SEQ\s*ID|氨基酸|核苷酸|可变区|重链|轻链|CDR|FR[HL]?\d|[A-Z]{20,}|variable\s+(?:region|domain)|heavy\s+chain|light\s+chain", re.I),
    "metric": re.compile(r"IC\s*50|EC\s*50|K\s*[Dd]|抑制|解离|亲和|半数|\d\s*(?:nM|pM|μM|uM|ng/mL|ug/mL|mg/mL)|affinity|inhibit", re.I),
    "conditions": re.compile(r"SPR|ELISA|BLI|FACS|flow\s+cytometry|surface\s+plasmon|细胞|孵育|实验|方法|温度|浓度|缓冲|cell|incubat|assay|method|buffer|\d\s*(?:°C|℃|hours?|min|小时|分钟)", re.I),
}
FIELD_ALIASES = {
    "experiment_type": {"SPR": ("SPR", "surface plasmon resonance", "表面等离子共振"),
                        "ELISA": ("ELISA", "酶联免疫"), "BLI": ("BLI", "生物层干涉"),
                        "FACS": ("FACS", "flow cytometry", "流式")},
}


@lru_cache(maxsize=1)
def load_domain_knowledge():
    """Read only reusable terminology, never units, profiles or length rules."""
    path = Path(__file__).resolve().parents[1] / "schemas" / "antibody_knowledge.json"
    if not path.is_file():
        return {"template_id": None, "groups": {}, "scope_patterns": {}}
    raw = json.loads(path.read_text(encoding="utf-8"))
    groups = {}
    groups["constructs"] = raw.get("antibody_construct_knowledge", {}).get("types", [])
    for key in ("methods", "systems", "species"):
        groups[key] = raw.get("assay", {}).get(key, [])
    # The allowlist deliberately excludes assay.default_unit, document_profiles,
    # variable_length and OCR scan heuristics from the migrated source.
    fields = {"key", "label_zh", "label_en", "patterns", "endpoint", "system", "minimum_score", "summary_zh"}
    return {"template_id": raw.get("template_id"), "version": raw.get("version"),
            "groups": {group: [{k: copy.deepcopy(v) for k, v in row.items() if k in fields}
                               for row in rows] for group, rows in groups.items()},
            "scope_patterns": raw.get("sequence_definition", {}).get("scope_patterns", {})}


def _knowledge_matches(text, *, limit=10):
    result = []
    for group, rows in load_domain_knowledge()["groups"].items():
        for row in rows:
            hits = sum(bool(re.search(pattern, text, re.I)) for pattern in row.get("patterns", []))
            threshold = max(1, row.get("minimum_score", 1))
            if hits >= threshold:
                result.append((hits, group, row))
    result.sort(key=lambda entry: -entry[0])
    return [{"group": group, "key": row.get("key"), "label_zh": row.get("label_zh"),
             "label_en": row.get("label_en"), "endpoint": row.get("endpoint"),
             "system": row.get("system"), "definition": row.get("summary_zh"), "rule_hits": hits}
            for hits, group, row in result[:limit]]


def _domain_vocabulary(context):
    return {"template_id": load_domain_knowledge().get("template_id"),
            "source": "schemas/antibody_knowledge.json",
            "usage": "Schema terminology only; not document facts, units or sequence boundaries.",
            "matches": _knowledge_matches("\n".join(p["text"] for p in context))}


def _compact(value):
    return re.sub(r"\s+", "", value)


def _slot_value(value):
    return value.get("value") if isinstance(value, dict) and "status" in value else value


def _entity(template, identity):
    return next((e for e in template.get("entities", []) if e.get("entity_id") == identity), None)


def _observation(entity, identity):
    return next((o for o in entity.get("METRIC", []) if o.get("observation_id") == identity), None)


def _source_terms(source):
    terms = []
    for key in ("table_id", "caption", "example"):
        value = source.get(key)
        if value is not None:
            terms.append(str(value))
    return terms


def retrieve_evidence(document, entity, tasks, observations=(), *, max_paragraphs=16, max_chars=18000):
    """Typed rules + lexical/cross-reference recall, returned in original order.

    The score selects evidence, never validates a relationship. Exact citations
    and a separate semantic verifier gate all accepted slot mutations.
    """
    fields = " ".join(t["field_path"] for t in tasks)
    groups = {"sequence"} if any(x in fields for x in ("VH", "VL", "CHAIN", "CDR", "FR")) else {"metric", "conditions"}
    name = _slot_value(entity.get("name"))
    names = [str(name)] if name else []
    names += [str(_slot_value(a)) for a in entity.get("aliases", []) if _slot_value(a)]
    metrics = [str(_slot_value(o.get("metric"))) for o in observations if _slot_value(o.get("metric"))]
    references = [s for o in observations for s in _source_terms(o.get("source") or {})]
    query = " ".join(str(t.get("query", "")) for t in tasks)
    tokens = set(re.findall(r"[A-Za-z][A-Za-z0-9_.-]{2,}|[\u4e00-\u9fff]{2,6}", query))
    values = [_slot_value(o.get("value")) for o in observations if _slot_value(o.get("value")) is not None]
    units = [str(_slot_value(o.get("unit"))) for o in observations if _slot_value(o.get("unit"))]
    table_ids = {o.get("source", {}).get("table_id") for o in observations if o.get("source", {}).get("table_id")}
    pages = set()
    for o in observations:
        source = o.get("source") or {}
        if isinstance(source.get("page"), int):
            pages.add(source["page"])
        for key in ("table_id", "image_path"):
            match = re.search(r"page-(\d+)", str(source.get(key, "")), re.I)
            if match:
                pages.add(int(match.group(1)))
    ranked, reasons, anchors = {}, defaultdict(list), set()
    paragraphs = document.get("paragraphs", [])

    def name_in(text):
        return any(re.search(r"(?<![A-Za-z0-9_-])" + re.escape(_compact(n)) + r"(?![A-Za-z0-9_-])",
                             _compact(text), re.I) for n in names)

    def value_in(value, text):
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return _numeric_supported(value, text)
        return isinstance(value, str) and _compact(value).casefold() in _compact(text).casefold()

    def same_page_neighbor(left, right):
        return (left.get("source_type") == right.get("source_type") == "pdf_ocr"
                and left.get("pdf_page") is not None and left.get("pdf_page") == right.get("pdf_page"))

    def boost(index, score, reason):
        ranked[index] = max(ranked.get(index, 0), score)
        if reason not in reasons[index]:
            reasons[index].append(reason)

    for index, p in enumerate(paragraphs):
        # A numeric match somewhere in another full table does not establish
        # that its row belongs to the current observation. Sequence-only
        # retrieval has no observation table scope and can still inspect all.
        if table_ids and p.get("source_type") == "table_context" and p.get("table_id") not in table_ids:
            continue
        text = p["text"]
        folded = _compact(text).casefold()
        typed = sum(bool(TYPE_PATTERNS[k].search(text)) for k in groups)
        vocabulary_hits = _knowledge_matches(text, limit=3)
        knowledge_score = min(3, len([m for m in vocabulary_hits if
            ("sequence" in groups and m["group"] == "constructs") or
            ("metric" in groups and m["group"] in {"methods", "systems"})]))
        if "sequence" in groups:
            knowledge_score += int(any(re.search(pattern, text, re.I)
                for patterns in load_domain_knowledge()["scope_patterns"].values() for pattern in patterns))
        name_hits = int(name_in(text))
        metric_hits = sum(_compact(m).casefold() in folded for m in metrics)
        reference_hits = sum(_compact(s).casefold() in folded or str(p.get("example")) == s for s in references)
        lexical = sum(token.casefold() in text.casefold() for token in tokens)
        numeric_hits = sum(value_in(value, text) for value in values)
        unit_hits = sum(_compact(unit).casefold() in folded for unit in units)
        page_hit = p.get("pdf_page") in pages
        score = typed * 2 + knowledge_score + name_hits * 8 + metric_hits * 3 + reference_hits * 5 + min(lexical, 4)
        score += min(numeric_hits, 2) * 5 + int(bool(unit_hits)) * 2 + int(page_hit) * 2
        if score:
            boost(index, score, "typed_lexical_scope")
        if p.get("source_type") == "table_context" and p.get("table_id") in table_ids:
            boost(index, 120, "exact_source_table_context")
            anchors.add(index)
        if numeric_hits:
            nearby_name = name_hits or any(name_in(paragraphs[j]["text"]) and same_page_neighbor(p, paragraphs[j])
                for j in range(max(0, index - 2), min(len(paragraphs), index + 3)))
            if nearby_name and (page_hit or metric_hits or unit_hits):
                boost(index, 115, "known_observation_value_and_named_row")
                anchors.add(index)
    # OCR neighbors expose the original row/header/caption; they are retrieval
    # context only, never a proof that a protocol belongs to the measurement.
    for index in list(anchors):
        for j in range(max(0, index - 4), min(len(paragraphs), index + 5)):
            if same_page_neighbor(paragraphs[index], paragraphs[j]):
                boost(j, 105 - abs(j - index), "bounded_same_page_row_neighbors")

    def table_references(text):
        return set(re.findall(r"(?:表|\bTable\s*)\s*(\d+(?:[-－]\d+)?)", text, re.I))

    def method_references(text):
        return set(re.findall(r"(?:方法\s*(?:同|参照|参考|见)|(?:method|protocol)\s*(?:as|in|of|see)\s*(?:section\s*)?)[：:\s]*(\d+(?:\.\d+){1,3})", text, re.I))

    def heading(text):
        return re.match(r"^\s*\d+(?:\.\d+){1,3}(?![\d.])\s*(?:[\u4e00-\u9fffA-Za-z])", text) is not None

    # Cross-reference closure is driven only by literal references already
    # recalled. Physical page numbers never imply a logical table number.
    seeds = set(anchors)
    seeds.update(i for i, _ in sorted(ranked.items(), key=lambda item: (-item[1], item[0]))[:max(2, max_paragraphs // 3)])
    seeds.update(i for i in ranked if "bounded_same_page_row_neighbors" in reasons[i])
    logical_tables = set().union(*(table_references(paragraphs[i]["text"]) for i in seeds)) if seeds else set()
    sections = set().union(*(method_references(paragraphs[i]["text"]) for i in seeds)) if seeds else set()
    linked_indices = set()
    for index, p in enumerate(paragraphs):
        if table_ids and p.get("source_type") == "table_context" and p.get("table_id") not in table_ids:
            continue
        if table_references(p["text"]) & logical_tables:
            boost(index, 110 if TYPE_PATTERNS["conditions"].search(p["text"]) else 100,
                  "explicit_logical_table_reference")
            linked_indices.add(index)
    for index in list(linked_indices):
        sections.update(method_references(paragraphs[index]["text"]))
    for section in sorted(sections):
        matches = [i for i, p in enumerate(paragraphs)
                   if re.match(r"^\s*" + re.escape(section) + r"(?![\d.])\s*[\u4e00-\u9fffA-Za-z]", p["text"])]
        for index in matches[:2]:
            boost(index, 112, "explicit_method_section_heading")
            source_type = paragraphs[index].get("source_type", "text")
            for j in range(index + 1, min(len(paragraphs), index + (5 if source_type == "pdf_ocr" else 3))):
                if paragraphs[j].get("source_type", "text") != source_type or heading(paragraphs[j]["text"]):
                    break
                if source_type == "pdf_ocr" and not same_page_neighbor(paragraphs[index], paragraphs[j]):
                    break
                boost(j, 111, "explicit_method_section_protocol")
    selected = set()
    consumed = 0
    truncated = []
    for index, score in sorted(ranked.items(), key=lambda item: (-item[1], item[0])):
        p = paragraphs[index]
        size = len(json.dumps(p, ensure_ascii=False))
        if len(selected) >= max_paragraphs or consumed + size > max_chars:
            truncated.append(p["id"])
            continue
        selected.add(index)
        consumed += size
    result = [copy.deepcopy(paragraphs[i]) for i in sorted(selected)]
    return result, {"retriever": "antibody_type_rules+value_scope+explicit_cross_references", "dense_retrieval": False,
                    "knowledge_template_id": load_domain_knowledge().get("template_id"),
                    "selection_reasons": {paragraphs[i]["id"]: reasons[i] for i in sorted(selected)},
                    "physical_page_hints": sorted(pages), "logical_table_references": sorted(logical_tables),
                    "method_section_references": sorted(sections),
                    "paragraphs_total": len(paragraphs), "retrieved_ids": [p["id"] for p in result],
                    "omitted_matching_ids": truncated, "context_chars": consumed,
                    "complete_document_scan": not truncated and len(selected) == len(paragraphs)}


def _check_evidence(items, context, document):
    if not isinstance(items, list) or not items:
        raise ValueError("proposal lacks evidence")
    by_id = {p["id"]: p for p in context}
    result = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("evidence must be an object")
        p = by_id.get(item.get("paragraph_id"))
        whole = item.get("whole_paragraph") is True
        if "whole_paragraph" in item and not isinstance(item["whole_paragraph"], bool):
            raise ValueError("whole_paragraph must be a boolean")
        if whole and "quote" in item:
            raise ValueError("whole_paragraph citation must omit quote entirely")
        quote = p["text"] if whole and p is not None else item.get("quote")
        if p is None or not isinstance(quote, str) or not quote.strip() or quote not in p["text"]:
            raise ValueError("evidence is not an exact substring of the cited source paragraph")
        if item.get("document_id", document["document_id"]) != document["document_id"]:
            raise ValueError("cross-document evidence is forbidden")
        start = p["text"].index(quote)
        result.append({"document_id": document["document_id"], "paragraph_id": p["id"],
                       "quote": quote, "start": start, "end": start + len(quote),
                       "citation_scope": "whole_paragraph" if whole else "exact_quote",
                       "source_file": p.get("source_file") or document.get("source_file"),
                       "source_sha256": p.get("source_sha256") or document.get("source_sha256"),
                       "source_sha256_scope": "paragraph_source_file" if p.get("source_sha256") else
                           document.get("source_sha256_scope", "combined_document_index" if document.get("source_files") else "document_source_file"),
                       "document_index_sha256": document.get("source_sha256"),
                       "pdf_page": p.get("pdf_page"), "example": p.get("example"),
                       "table_id": p.get("table_id"), "source_type": p.get("source_type", "text"),
                       "origin": "tst_text_completion"})
    return result


def _numeric_supported(value, text):
    if isinstance(value, bool) or not math.isfinite(value):
        return False
    try:
        expected = Decimal(str(value))
        for match in re.finditer(r"(?<![\w.])[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?(?![\w.])", text):
            prefix = text[max(0, match.start() - 12):match.start()]
            suffix = text[match.end():match.end() + 8]
            if re.search(r"[<>≤≥~≈]\s*$|(?:less than|greater than|below|above|小于|大于|低于|高于|约)\s*$", prefix, re.I):
                continue
            if re.search(r"\d\s*(?:[-–~至到]|to\b)\s*$", prefix, re.I):
                continue
            if re.match(r"\s*(?:[-–~至到]|to\b)\s*\d", suffix, re.I):
                continue
            if Decimal(match.group()) == expected:
                return True
        return False
    except InvalidOperation:
        return False


def _supported_value(value, field_path, evidence):
    text = "\n".join(p["quote"] for p in evidence)
    field = field_path.rsplit(".", 1)[-1]
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, (int, float)):
        if field == "seq_id":
            return value > 0 and isinstance(value, int) and any(int(n) == value for n in re.findall(
                r"SEQ\s*ID\s*(?:NO\s*)?[.:：]?\s*(\d+)", text, re.I))
        return _numeric_supported(value, text)
    if isinstance(value, dict):
        return bool(value) and all(_supported_value(v, field_path, evidence) for v in value.values())
    if isinstance(value, list):
        return bool(value) and all(_supported_value(v, field_path, evidence) for v in value)
    if not isinstance(value, str) or not value.strip():
        return False
    if field == "sequence" or re.fullmatch(r"(?:CDR|FR)[HL]\d", field):
        return bool(re.fullmatch(r"[A-Z*\s]+", value)) and _compact(value) in _compact(text)
    if field == "seq_id":
        number = re.search(r"\d+", value)
        return bool(number) and any(int(n) == int(number.group()) for n in re.findall(
            r"SEQ\s*ID\s*(?:NO\s*)?[.:：]?\s*(\d+)", text, re.I))
    if field == "metric" and value in {"ka", "kd", "KD", "Ka", "Kd"}:
        # Off-rate kd and equilibrium-affinity KD must not be conflated by
        # generic case folding; unit/context entailment is also verified later.
        return re.search(r"(?<![A-Za-z])" + re.escape(value) + r"(?![A-Za-z])", text) is not None
    if _compact(value).casefold() in _compact(text).casefold():
        return True
    aliases = FIELD_ALIASES.get(field, {}).get(value, ())
    return any(alias.casefold() in text.casefold() for alias in aliases)


def _entity_context(entity):
    """Share sequence availability/IDs, not long residues and provenance per assay."""
    result = {k: copy.deepcopy(entity[k]) for k in ("entity_id", "name", "aliases") if k in entity}
    for chain in ("VH", "VL", "H_CHAIN", "L_CHAIN"):
        result[chain] = {}
        for field, slot in entity.get(chain, {}).items():
            item = {"status": slot["status"], "required": slot["required"]}
            if field == "seq_id":
                item["value"] = copy.deepcopy(slot["value"])
            elif isinstance(slot.get("value"), str):
                item.update(length=len(slot["value"]), value_stored_locally=True)
            result[chain][field] = item
    return result


class TSTCompletionAgent:
    """Bounded TST count/ordinal completion; run returns {template, report}."""

    def __init__(self, llm, *, max_concurrency=4, max_tasks=32, max_instances=5,
                 max_paragraphs=16, max_context_chars=18000, max_calls=96,
                 request_timeout=180, timing=None, concurrency=None, max_evidence_chars=None):
        if concurrency is not None:
            max_concurrency = concurrency
        if max_evidence_chars is not None:
            max_context_chars = max_evidence_chars
        for name, value in {"max_concurrency": max_concurrency, "max_tasks": max_tasks,
                            "max_instances": max_instances, "max_paragraphs": max_paragraphs,
                            "max_context_chars": max_context_chars, "max_calls": max_calls}.items():
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        self.llm = llm
        self.max_concurrency, self.max_tasks, self.max_instances = max_concurrency, max_tasks, max_instances
        self.max_paragraphs, self.max_context_chars, self.max_calls = max_paragraphs, max_context_chars, max_calls
        self.request_timeout, self.timing = request_timeout, timing
        self._run_lock = asyncio.Lock()
        prompt_dir = Path(__file__).resolve().parents[1] / "prompt"
        self.prompts = {stage: (prompt_dir / f"tst_{stage}.txt").read_text(encoding="utf-8")
                        for stage in ("count", "extract", "verify")}

    async def _request(self, stage, payload, key):
        queued = perf_counter()
        async with self._semaphore:
            start = perf_counter()
            if self._calls >= self.max_calls:
                raise RuntimeError("TST request budget exhausted")
            self._calls += 1
            print("sda-progress " + json.dumps({"stage": "tst", "phase": stage,
                                                "request_index": self._calls}), flush=True)
            entry = {"stage": stage, "item_id": key, "queue_seconds": start - queued}
            self._report["stages"].append(entry)
            try:
                with usage_scope(recorder=self._usage_recorder, stage=f"tst_{stage}",
                                 item_id=f"{payload.get('document_id')}/{key}",
                                 document_id=payload.get("document_id")):
                    response = await asyncio.wait_for(self.llm.chat(system=self.prompts[stage],
                        user=json.dumps({"stage": stage, **payload}, ensure_ascii=False)), self.request_timeout)
                parsed = response if isinstance(response, dict) else json.loads(response)
                if not isinstance(parsed, dict):
                    raise ValueError("TST response must be an object")
                entry["status"] = "ok"
                entry["response"] = parsed
                return parsed
            except Exception as exc:
                entry.update(status="failed", error_type=type(exc).__name__, error=str(exc)[:500])
                raise
            finally:
                entry["seconds"] = perf_counter() - start
                if self.timing is not None:
                    self.timing.add(f"tst_{stage}", start, perf_counter(), kind="request", item_id=f"{payload.get('document_id')}/{key}",
                                    status=entry.get("status", "cancelled"))

    async def _extract(self, template, document, entity, tasks, observations, context, instance, total):
        from template_agent import fill_slot, get_slot
        observation_id = instance.get("existing_observation_id")
        key = f"{entity['entity_id']}/{observation_id or 'unbound'}/{instance.get('key', 'static')}"
        applicable = [t for t in tasks if t.get("observation_id") == observation_id]
        entity_context = _entity_context(entity)
        payload = {"document_id": document["document_id"], "entity": entity_context,
                   "observations": observations, "tasks": applicable, "context": context,
                   "domain_vocabulary": _domain_vocabulary(context),
                   "instance": instance, "ordinal": instance.get("ordinal", 1), "total_instances": total}
        response = await self._request("extract", payload, key)
        if not isinstance(response.get("fills", []), list) or not isinstance(response.get("new_observations", []), list):
            raise ValueError("extract fills/new_observations must be arrays")
        by_task = {t["task_id"]: t for t in applicable}
        proposals = []
        seen = set()
        for row in response.get("fills", []):
            try:
                if not isinstance(row, dict) or row.get("task_id") not in by_task or row["task_id"] in seen:
                    raise ValueError("unknown or duplicate task_id")
                seen.add(row["task_id"])
                task = by_task[row["task_id"]]
                status = row.get("status")
                if status == "unknown":
                    self._report["unresolved"].append({"task_id": task["task_id"], "reason": "unknown_in_retrieved_context"})
                    continue
                if status not in {"found", "not_applicable"}:
                    raise ValueError("unsupported extraction status")
                evidence = _check_evidence(row.get("evidence"), context, document)
                if status == "not_applicable" and not re.search(
                    r"not\s+applicable|does\s+not\s+apply|不适用|不涉及|无需", "\n".join(e["quote"] for e in evidence), re.I):
                    raise ValueError("not_applicable requires an explicit source assertion")
                if status == "found" and not _supported_value(row.get("value"), task["field_path"], evidence):
                    raise ValueError("value not literally supported by the cited evidence")
                value = row.get("value")
                if status == "found" and (task["field_path"].endswith(".sequence") or re.search(r"\.(?:CDR|FR)[HL]\d$", task["field_path"])):
                    value = _compact(value)
                slot = get_slot(template, task)
                if slot is None or slot.get("status") != "missing" or slot.get("value") is not None:
                    raise ValueError("slot is already observed, conflicted or unavailable")
                proposals.append({"proposal_id": task["task_id"], "task": task,
                                  "status": status, "value": value, "evidence": evidence})
            except (ValueError, TypeError, KeyError) as exc:
                self._report["rejected"].append({"item_id": key, "proposal": row, "reason": str(exc)})
        self._report["unresolved"].extend({"task_id": identity, "reason": "extractor_omitted_task"}
            for identity in by_task.keys() - seen)
        # A new measurement is never projected onto an existing condition/row.
        for index, row in enumerate(response.get("new_observations", [])):
            try:
                if observation_id is not None:
                    raise ValueError("existing-instance extraction cannot create another observation")
                if not isinstance(row, dict) or index > 0:
                    raise ValueError("one ordinal permits at most one candidate observation")
                evidence = _check_evidence(row.get("evidence"), context, document)
                for field in ("metric", "value"):
                    if not _supported_value(row.get(field), "METRIC." + field, evidence):
                        raise ValueError(f"new observation lacks explicit {field}")
                for field in ("unit", "conditions", "experiment_type", "target"):
                    if row.get(field) is not None and not _supported_value(row[field], "METRIC." + field, evidence):
                        raise ValueError(f"new observation has unsupported {field}")
                proposals.append({"proposal_id": key + f"/candidate-{index + 1}",
                                  "candidate": row, "evidence": evidence, "status": "candidate"})
            except (ValueError, TypeError, KeyError) as exc:
                self._report["rejected"].append({"item_id": key, "proposal": row, "reason": str(exc)})
        if not proposals:
            return
        checked = await self._request("verify", {**payload, "proposals": proposals}, key)
        decisions = checked.get("decisions")
        if not isinstance(decisions, list):
            raise ValueError("semantic verification requires decisions array")
        decision_map = {}
        for row in decisions:
            if not isinstance(row, dict) or row.get("proposal_id") in decision_map:
                raise ValueError("invalid or duplicate semantic decision")
            decision_map[row.get("proposal_id")] = row
        for proposal in proposals:
            decision = decision_map.get(proposal["proposal_id"], {})
            if decision.get("supported") is not True or not isinstance(decision.get("reason"), str) or not decision["reason"].strip():
                self._report["rejected"].append({**proposal, "reason": decision.get("reason", "semantic binding not verified")})
                continue
            provenance = [{**item, "semantic_verification": decision["reason"],
                           "tst_instance_key": instance.get("key"), "tst_ordinal": instance.get("ordinal", 1)}
                          for item in proposal["evidence"]]
            if "candidate" in proposal:
                candidate = {**proposal["candidate"], "entity_id": entity["entity_id"],
                             "provenance": provenance, "status": "requires_review"}
                self._report["candidates"].append(candidate)
            elif proposal["status"] == "not_applicable":
                slot = get_slot(template, proposal["task"])
                if slot.get("status") == "missing" and slot.get("value") is None:
                    slot.update(status="not_applicable", provenance=provenance)
                    self._report["accepted"].append({**proposal, "provenance": provenance})
            elif fill_slot(template, proposal["task"], proposal["value"], provenance):
                self._report["accepted"].append({**proposal, "provenance": provenance})
            else:
                self._report["rejected"].append({**proposal, "reason": "schema refused fill or slot was already filled"})

    async def _group(self, template, document, entity, tasks, observations, dynamic):
        key = entity["entity_id"] + "/" + (str(_slot_value(observations[0].get("metric"))) if dynamic else "sequence")
        if dynamic:
            key += "/" + str(observations[0].get("source", {}).get("table_id") or observations[0]["observation_id"])
        context, retrieval = retrieve_evidence(document, entity, tasks, observations,
            max_paragraphs=self.max_paragraphs, max_chars=self.max_context_chars)
        self._report["retrieval"].append({"item_id": key, **retrieval})
        if not context:
            self._report["unresolved"].extend({"task_id": t["task_id"], "reason": "no_retrieved_evidence"} for t in tasks)
            return
        if not dynamic:
            await self._extract(template, document, entity, tasks, [], context,
                                {"key": "static", "ordinal": 1, "existing_observation_id": None}, 1)
            return
        entity_context = _entity_context(entity)
        counted = await self._request("count", {"document_id": document["document_id"], "entity": entity_context,
            "observations": observations, "tasks": tasks, "context": context,
            "domain_vocabulary": _domain_vocabulary(context), "max_instances": self.max_instances}, key)
        count, instances = counted.get("count"), counted.get("instances")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0 or not isinstance(instances, list) or count != len(instances) or count > self.max_instances:
            raise ValueError("invalid dynamic instance count")
        allowed_ids = {o["observation_id"] for o in observations}
        seen_keys, seen_observations = set(), set()
        validated, previous_position = [], None
        context_order = {p["id"]: i for i, p in enumerate(context)}
        for ordinal, instance in enumerate(instances, 1):
            if not isinstance(instance, dict) or instance.get("ordinal") != ordinal or not isinstance(instance.get("key"), str) or not instance["key"] or instance["key"] in seen_keys:
                raise ValueError("instances need unique keys and consecutive source ordinals")
            identity = instance.get("existing_observation_id")
            if identity is not None and (identity not in allowed_ids or identity in seen_observations):
                raise ValueError("unknown or multiply-bound existing observation")
            if identity:
                seen_observations.add(identity)
            seen_keys.add(instance["key"])
            evidence = _check_evidence(instance.get("evidence"), context, document)
            position = min((context_order[e["paragraph_id"]], e["start"]) for e in evidence)
            if previous_position is not None and position < previous_position:
                raise ValueError("dynamic ordinals violate evidence source order")
            previous_position = position
            validated.append({**instance, "evidence": evidence})
        self._report["counts"].append({"item_id": key, "count": count,
            "instances": validated, "context_limited": True, "truncated": bool(counted.get("truncated")),
            "total_count": counted.get("total_count", count)})
        for instance in validated:
            await self._extract(template, document, entity, tasks, observations, context, instance, count)
        bound = {x.get("existing_observation_id") for x in validated}
        self._report["unresolved"].extend({"task_id": t["task_id"], "reason": "observation_not_bound_in_retrieved_context"}
            for t in tasks if t.get("observation_id") not in bound)

    async def run(self, template, document, output_dir=None):
        # A caller may share one agent across patents; keep per-run audit state isolated.
        async with self._run_lock:
            return await self._run(template, document, output_dir)

    async def _run(self, template, document, output_dir=None):
        from template_agent import refresh_missing
        started = perf_counter()
        self._usage_recorder = get_usage_context().get("recorder") or (
            self.timing.usage_recorder if self.timing is not None else TokenUsageRecorder())
        if not isinstance(template, dict) or not template.get("document_id"):
            raise ValueError("template requires document_id")
        if document is not None and template["document_id"] != document.get("document_id"):
            raise ValueError("template and text must be from the same document")
        updated = copy.deepcopy(template)
        self._report = {"document_id": template["document_id"], "method": "TST antibody-domain adaptation",
            "paper": "https://aclanthology.org/2025.acl-long.829/", "status": "ok", "accepted": [],
            "rejected": [], "unresolved": [], "candidates": [], "counts": [], "retrieval": [], "stages": [], "errors": []}
        self._calls, self._semaphore = 0, asyncio.Semaphore(self.max_concurrency)
        tasks = updated.get("missing", [])
        if not document or not document.get("paragraphs"):
            self._report["status"] = "skipped_no_text"
        else:
            ids = set()
            for p in document["paragraphs"]:
                if not isinstance(p.get("id"), str) or p["id"] in ids or not isinstance(p.get("text"), str):
                    raise ValueError("text paragraphs require unique string IDs and text")
                if p.get("document_id", document["document_id"]) != document["document_id"]:
                    raise ValueError("cross-document paragraph is forbidden")
                ids.add(p["id"])
            selected, deferred = tasks[:self.max_tasks], tasks[self.max_tasks:]
            self._report["unresolved"].extend({"task_id": t["task_id"], "reason": "task_budget_deferred"} for t in deferred)
            grouped = defaultdict(list)
            for task in selected:
                entity = _entity(updated, task.get("entity_id"))
                if entity is None:
                    self._report["errors"].append({"task_id": task.get("task_id"), "error": "unknown entity"})
                    continue
                observation = _observation(entity, task.get("observation_id")) if task.get("observation_id") else None
                if task.get("observation_id") and observation is None:
                    self._report["errors"].append({"task_id": task.get("task_id"), "error": "unknown observation"})
                    continue
                metric = str(_slot_value(observation.get("metric"))) if observation else None
                source = (observation or {}).get("source") or {}
                scope = (source.get("table_id"), source.get("example")) if observation else (None, None)
                # Unknown provenance never groups unrelated observations together.
                if observation and not any(scope):
                    scope = (observation["observation_id"], None)
                grouped[(entity["entity_id"], metric, scope)].append(task)
            async def process(identity, metric, scope, group_tasks):
                try:
                    entity = _entity(updated, identity)
                    task_observations = {t.get("observation_id") for t in group_tasks}
                    observations = [o for o in entity.get("METRIC", []) if o["observation_id"] in task_observations] if metric is not None else []
                    await self._group(updated, document, entity, group_tasks, observations, metric is not None)
                except Exception as exc:
                    self._report["errors"].append({"entity_id": identity, "metric": metric,
                                                  "error_type": type(exc).__name__, "error": str(exc)[:500]})
            await asyncio.gather(*(process(identity, metric, scope, group_tasks)
                                   for (identity, metric, scope), group_tasks in grouped.items()))
            refresh_missing(updated)
            if self._report["errors"]:
                self._report["status"] = "partial" if self._report["accepted"] else "failed"
            elif (updated.get("missing") or self._report["rejected"] or self._report["candidates"]
                  or updated.get("summary", {}).get("slot_status", {}).get("conflict", 0)
                  or any(c["truncated"] for c in self._report["counts"])):
                self._report["status"] = "partial"
        self._report.update(requests=self._calls, wall_seconds=perf_counter() - started,
                            remaining_tasks=len(updated.get("missing", [])),
                            budget={"max_tasks": self.max_tasks, "max_instances": self.max_instances,
                                    "max_calls": self.max_calls, "max_concurrency": self.max_concurrency})
        self._report["token_usage"] = self._usage_recorder.report(
            document_id=template["document_id"], since=started)
        if output_dir is not None:
            root = Path(output_dir)
            root.mkdir(parents=True, exist_ok=True)
            for filename, payload in (("template_completed.json", updated), ("tst_report.json", self._report)):
                path = root / filename
                temp = path.with_suffix(path.suffix + ".tmp")
                temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                temp.replace(path)
            write_usage_report(root, self._report["token_usage"])
        return {"template": updated, "report": self._report}
