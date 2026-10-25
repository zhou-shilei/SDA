"""按专利分块提取正文序列，保留证据并与表格中的明确SEQ ID关联。

单独处理已有表格结果：
python src/text_sequences.py --data-dir data/WO2025108394A1 \
    --result-dir results/WO2025108394A1 --output-dir validation_results/text_sequences
"""

import argparse
import asyncio
import copy
import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path
from time import perf_counter

from costructure import construct_records, get_document_id, load_table_results, normalize_region
from timing import TimingRecorder, await_with_progress, write_timing_report


SEQ_ID = re.compile(r"SEQ\s*ID\s*(?:NO\s*)?[.:：]?\s*(\d+)", re.I)
TYPES = {"antibody", "antigen", "linker", "unknown"}


def compact(text):
    return re.sub(r"\s+", "", text)


def seq_number(value):
    if isinstance(value, str) and re.fullmatch(r"\d+", value.strip()):
        return str(int(value.strip()))
    match = SEQ_ID.fullmatch(value.strip()) if isinstance(value, str) else None
    return str(int(match.group(1))) if match else None


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def load_document(data_dir, document_id):
    """优先结构化正文，其次带段落编号的TXT；本阶段不使用未核对的PDF OCR层。"""
    data_dir = Path(data_dir)
    path = data_dir / "patent_context.json"
    if path.is_file():
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("document_id") != document_id:
            raise ValueError(f"正文文档号不一致：{path}")
        if "paragraphs" in payload:
            rows = payload["paragraphs"]
        else:
            rows = []
            if payload.get("abstract"):
                rows.append({"id": "abstract", "text": payload["abstract"]})
            rows.extend(payload.get("description", []))
            rows.extend({**row, "section": "权利要求"} for row in payload.get("claims", []))
    else:
        path = next((data_dir / name for name in ("patent_text.zh.txt", "patent_text.txt")
                     if (data_dir / name).is_file()), None)
        if path is None:
            return None
        raw = path.read_text(encoding="utf-8")
        # 独立的原文段落作为单位；编号不参与模型引文。
        rows = []
        current = None
        for block in re.split(r"\n\s*\n", raw):
            match = re.match(r"\[([^\]\n]+)\]\s*([\s\S]*)", block)
            if match:
                current, text = match.groups()
            else:
                current, text = f"txt-{len(rows) + 1:05d}", block
            rows.append({"id": current, "text": text})
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("正文段落必须是对象数组")
    paragraphs, seen = [], set()
    for i, row in enumerate(rows):
        text = row.get("text")
        if not isinstance(text, str):
            raise ValueError("正文段落缺少字符串text")
        if not text.strip():
            continue
        identity = row.get("id") or f"paragraph-{i + 1:05d}"
        if not isinstance(identity, str) or identity in seen:
            raise ValueError(f"正文段落ID无效或重复：{identity}")
        seen.add(identity)
        paragraphs.append({"id": identity, "text": text, "section": row.get("section"),
                           "example": row.get("example"), "pdf_page": row.get("pdf_page"),
                           "missing_image_count": row.get("missing_image_count", 0)})
    if not paragraphs:
        raise ValueError("正文为空，无法执行序列提取")
    pdf = data_dir / "source.pdf"
    return {"document_id": document_id, "source_file": str(path.resolve()),
            "pdf_file": str(pdf.resolve()) if pdf.is_file() else None,
            "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "paragraphs": paragraphs}


def chunk_document(document, max_chars=12000, overlap_chars=600):
    """按段落打包，序列字符不从中间截开；不能容纳的原子片段明确进入待处理清单。"""
    if max_chars < 1000 or overlap_chars < 0 or overlap_chars > max_chars // 4:
        raise ValueError("max_chars至少1000，overlap_chars不超过预算的1/4")
    # 预留JSON元数据和标题空间；长段落在句子边界优先拆分。
    limit = max_chars - 500
    units, pending = [], []
    for paragraph in document["paragraphs"]:
        text, offset, part = paragraph["text"], 0, 0
        while offset < len(text):
            end = min(offset + limit, len(text))
            if end < len(text):
                # 保护连续序列及其排版空白；避免将半条序列误标为完整VH/VL。
                spans = list(re.finditer(r"[A-Z](?:[A-Z\s]*[A-Z])?|[a-z]{20,}(?:\s+[a-z]{20,})*", text[offset:]))
                protected = next((m for m in spans if m.start() < end - offset < m.end()
                                  and len(compact(m.group())) >= 20), None)
                if protected:
                    end = offset + protected.start()
                    if end == offset:
                        end = offset + protected.end()
                        pending.append({"type": "sequence_span_exceeds_budget", "paragraph_id": paragraph["id"],
                                        "start": offset, "end": end, "source_file": document["source_file"]})
                        offset = end
                        continue
                else:
                    boundaries = list(re.finditer(r"[。；;\n]|\.(?=\s)", text[offset:end]))
                    if boundaries and boundaries[-1].end() > limit // 2:
                        end = offset + boundaries[-1].end()
            value = {**paragraph, "text": text[offset:end], "origin_id": paragraph["id"],
                     "start": offset, "end": end}
            if offset or end < len(text):
                value["id"] = f"{paragraph['id']}::part-{part}"
            units.append(value)
            offset, part = end, part + 1
    chunks, batch = [], []

    def size(rows):
        return len(json.dumps({"document_id": document["document_id"], "paragraphs": rows}, ensure_ascii=False))

    def emit(rows):
        chunks.append({"document_id": document["document_id"],
                       "chunk_id": f"chunk-{len(chunks) + 1:05d}", "paragraphs": rows})

    for unit in units:
        if size([unit]) > max_chars:
            pending.append({"type": "paragraph_metadata_exceeds_budget", "paragraph_id": unit["id"]})
            continue
        if batch and (size([*batch, unit]) > max_chars or
                      (unit.get("example") != batch[-1].get("example") and unit.get("example") is not None)):
            emit(batch)
            overlap, chars = [], 0
            for previous in reversed(batch):
                chars += len(previous["text"])
                if chars > overlap_chars:
                    break
                overlap.insert(0, previous)
            batch = overlap if size([*overlap, unit]) <= max_chars else []
        batch.append(unit)
    if batch:
        emit(batch)
    for paragraph in document["paragraphs"]:
        if paragraph["missing_image_count"] or "[网页缺少" in paragraph["text"]:
            pending.append({"type": "missing_image_requires_pdf", "paragraph_id": paragraph["id"],
                            "pdf_page": paragraph.get("pdf_page"), "source_file": document["source_file"],
                            "pdf_file": document.get("pdf_file"),
                            "text": paragraph["text"]})
    return chunks, pending


def validate_extraction(payload, chunk):
    """逐字证据校验；允许排版空白差异，不能修改字符或大小写。"""
    if not isinstance(payload, dict) or not isinstance(payload.get("records"), list):
        raise ValueError("正文提取响应必须包含records数组")
    paragraphs = {row["id"]: row for row in chunk["paragraphs"]}
    records = []
    for row in payload["records"]:
        if not isinstance(row, dict) or row.get("molecule_type") not in TYPES:
            raise ValueError("正文序列记录缺少有效molecule_type")
        if row.get("sequence_type") not in {"amino_acid", "nucleotide", "unknown"}:
            raise ValueError("正文序列记录缺少有效sequence_type")
        record = {}
        for field in ("name", "region", "sequence", "seq_id", "domain_role"):
            value = row.get(field)
            if isinstance(value, str) and not value.strip():
                value = None
            if field == "seq_id" and isinstance(value, int) and not isinstance(value, bool) and value > 0:
                value = str(value)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{field}必须为非空字符串或null")
            record[field] = value
        evidence = row.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            raise ValueError("序列记录必须包含原文证据")
        quotes, verified = [], []
        for item in evidence:
            if not isinstance(item, dict):
                raise ValueError("原文证据必须为对象")
            paragraph = paragraphs.get(item.get("paragraph_id"))
            quote = item.get("quote")
            if paragraph is None or not isinstance(quote, str) or not quote.strip():
                raise ValueError("证据引用未知段落或为空")
            recovered = False
            if compact(quote) not in compact(paragraph["text"]):
                # 模型可能写错相邻段落ID或句末标点；只允许逐字短语在本块唯一命中后纠正。
                # 不替换引文内部字符，不对名称、编号、残基做模糊匹配。
                core = compact(quote.strip().strip("。；;，,.：:！？!?"))
                matches = [p for p in paragraphs.values() if core and core in compact(p["text"])]
                if len(matches) == 1:
                    paragraph, recovered = matches[0], True
                    quote = paragraph["text"]
                else:
                    raise ValueError(f"段落{item['paragraph_id']}的证据引文不在输入中：{quote[:200]}。"
                                     "请复制逐字短引文，保留标点；多个字段可分别用短引文证明，不用省略号改写。")
            quotes.append(quote)
            verified.append({**item, "paragraph_id": paragraph["id"], "quote": quote,
                             **({"submitted_paragraph_id": item["paragraph_id"],
                                 "quote_recovered_from_source": True} if recovered else {}),
                             "origin_id": paragraph.get("origin_id", paragraph["id"]),
                             "pdf_page": paragraph.get("pdf_page")})
        combined = "\n".join(quotes)
        # 元数据必须来自文字说明，不能因为氨基酸串恰好含N、VH等字符而通过。
        metadata_text = re.sub(r"[A-Z]{20,}", "", SEQ_ID.sub("", compact(combined)))
        if record["sequence"]:
            metadata_text = metadata_text.replace(compact(record["sequence"]), "")
        for field in ("name", "region", "domain_role"):
            value = record[field]
            supported = value is None or compact(value) in metadata_text
            if field == "name" and value is not None:
                supported = re.search(r"(?<![A-Za-z0-9_-])" + re.escape(compact(value))
                                      + r"(?![A-Za-z0-9_-])", metadata_text) is not None
            if field == "domain_role" and value in {"IgG-scFv", "IgG/scFv"}:
                # 接头位于两个明确命名域之间；组合域仅存入分域序列库，不能填平面VH/VL。
                supported = all(label in metadata_text for label in ("IgG", "scFv")) and any(
                    word in metadata_text for word in ("连接", "linked", "linker"))
            if not supported:
                # region允许VH/VL与原文中文明确区域名称等价。
                labels = {"VH": ("VH", "重链可变区", "heavy chain variable region"),
                          "VL": ("VL", "轻链可变区", "light chain variable region"),
                          "H_chain": ("重链", "heavy chain"), "HC": ("重链", "heavy chain"),
                          "L_chain": ("轻链", "light chain"), "LC": ("轻链", "light chain"),
                          "CH": ("重链恒定区", "heavy chain constant region"),
                          "CL": ("轻链恒定区", "light chain constant region"),
                          "linker": ("接头", "Linker"), "接头": ("Linker", "linker")}
                if field != "region" or not any(compact(label).lower() in metadata_text.lower()
                                                for label in labels.get(normalize_region(value), ())):
                    raise ValueError(f"{field}={value!r}没有逐字证据；请使用原文名称/区域，"
                                     f"并引用支持它的句子。当前引文：{combined[:400]}")
        if record["seq_id"] is not None:
            number = seq_number(record["seq_id"])
            supported = {str(int(m.group(1))) for m in SEQ_ID.finditer(combined)}
            if number is None or number not in supported:
                raise ValueError(f"SEQ ID编号{record['seq_id']!r}没有引文证据；引用中的编号为{sorted(supported)}")
        if record["sequence"] is not None:
            sequence = compact(record["sequence"])
            if not re.fullmatch(r"[A-Za-z*]+", sequence):
                raise ValueError("序列只能包含原文字符及终止标记")
            if not any(sequence in compact(quote) for quote in quotes):
                raise ValueError("实际序列无法在单条原文引文中逐字找到")
            record["sequence"] = sequence
        if not any(record.get(k) for k in ("sequence", "seq_id")) and not (record["name"] and record["region"]):
            raise ValueError("正文序列记录至少需要实际序列、SEQ ID或明确的名称及区域")
        records.append({**record, "molecule_type": row["molecule_type"], "evidence": verified,
                        "sequence_type": row["sequence_type"],
                        "chunk_id": chunk["chunk_id"]})
    return {"records": records}


def merge_sequences(tables, text_records, document):
    """保留各来源，按本篇SEQ ID恢复明确归属；不同序列不强行选择。"""
    document_id = document["document_id"]
    candidates = []
    for table in tables:
        if table.get("error") or table.get("error_stage"):
            continue
        if get_document_id(table) != document_id:
            raise ValueError("禁止跨文档合并序列")
        for index, row in enumerate(table.get("sequence_records", [])):
            candidates.append({**row, "origin": "table", "evidence": [
                {"table_id": table.get("table_id"), "record_index": index,
                 "result_file": table.get("result_file")}], "domain_role": row.get("domain_role"),
                "molecule_type": row.get("molecule_type", "unknown")})
    candidates.extend({**row, "origin": "text", "source_file": document["source_file"]} for row in text_records)
    existing = {(row["name"].strip(), normalize_region(row["region"]), compact(row["sequence"]),
                 row.get("domain_role")) for row in candidates if row["origin"] == "table"
                and row.get("name") and row.get("region") and row.get("sequence")}
    by_number = defaultdict(list)
    for row in candidates:
        if row.get("sequence") and seq_number(row.get("seq_id")):
            by_number[seq_number(row["seq_id"])].append(row)
    resolved, unresolved, conflicts = [], [], []
    for row in candidates:
        row = copy.deepcopy(row)
        number = seq_number(row.get("seq_id"))
        pool = by_number.get(number, [])
        values = {compact(r["sequence"]) for r in pool}
        if len(values) > 1:
            conflicts.append({"type": "seq_id_conflict", "seq_id": number,
                              "sequences": sorted(values), "evidence": row["evidence"]})
        if not row.get("sequence") and number:
            if len(values) == 1:
                compatible = [r for r in pool if r.get("region") is None or row.get("region") is None
                              or normalize_region(r["region"]) == normalize_region(row["region"])]
                if compatible:
                    row["sequence"] = next(iter(values))
                    row["evidence"] += [e for r in compatible for e in r["evidence"]]
                    row["resolution"] = "same_document_seq_id"
                    if row.get("sequence_type", "unknown") == "unknown":
                        types = {r.get("sequence_type", "unknown") for r in compatible}
                        if len(types) == 1:
                            row["sequence_type"] = next(iter(types))
            elif len(values) > 1:
                row["resolution"] = "conflicting_seq_id"
        if row.get("sequence"):
            row["sequence"] = compact(row["sequence"])
        # 原表保留不改写；只有正文明确的完整归属才生成补充输入。
        if row.get("sequence") and row.get("name") and row.get("region"):
            resolved.append(row)
        else:
            unresolved.append(row)
    # 相同分子、区域、序列、域归属去重，同时累积引文；不合并分子变体。
    merged = {}
    for row in resolved:
        key = (row["name"].strip(), normalize_region(row["region"]), row["sequence"], row.get("domain_role"),
               row.get("sequence_type", "unknown"))
        if key in merged:
            merged[key]["evidence"].extend(e for e in row["evidence"] if e not in merged[key]["evidence"])
            merged[key]["origins"] = sorted(set(merged[key]["origins"] + [row["origin"]]))
        else:
            merged[key] = {**row, "origins": [row["origin"]]}
    records = list(merged.values())
    supplementary = []
    for row in records:
        if "text" not in row["origins"] and row.get("resolution") != "same_document_seq_id":
            continue
        if row.get("domain_role"):
            # 双抗域保留在序列库，不进入平面的VH/VL字段。
            continue
        if row["origin"] == "text":
            if row.get("molecule_type") != "antibody" or row.get("sequence_type") != "amino_acid":
                continue
            if not re.fullmatch(r"[ACDEFGHIKLMNPQRSTVWY]+", row["sequence"]):
                continue
        if normalize_region(row["region"]) in {"VH", "VL"}:
            if row.get("sequence_type") not in {None, "amino_acid"}:
                continue
            if not re.fullmatch(r"[ACDEFGHIKLMNPQRSTVWY]+", row["sequence"]):
                continue
        if (row["name"].strip(), normalize_region(row["region"]), row["sequence"], row.get("domain_role")) in existing:
            # 已有同名序列只在序列库累积来源，不再注入排版不同的重复值造成假冲突。
            continue
        supplementary.append({key: row.get(key) for key in (
            "name", "region", "sequence", "seq_id", "evidence", "molecule_type", "sequence_type", "domain_role")})
    synthetic = {"document_id": document_id, "table_id": f"{document_id}/_text_sequences",
                 "source_type": "validated_text_sequences", "sequence_records": supplementary,
                 "assay_records": [], "table_text": "",
                 "text_source": {"file": document["source_file"], "sha256": document["source_sha256"]}}
    construction = construct_records([*tables, synthetic])
    for entity in construction["entities"]:
        for region, values in entity["sequence_conflicts"].items():
            conflicts.append({"type": "molecule_region_conflict", "name": entity["name"],
                              "region": region, "sequences": values})
    return {"records": records, "unresolved_records": unresolved, "conflicts": conflicts,
            "synthetic_input": synthetic, "entities": construction["entities"]}


def write_review_issues(output_dir, result):
    """把未通过校验的块记为待核对；请求故障和完全无覆盖仍视为错误。"""
    chunks = {chunk["chunk_id"]: chunk for chunk in result.get("chunks", [])}
    has_coverage = result.get("summary", {}).get("succeeded", 0) > 0
    source = {key: result.get(key) for key in ("document_id", "source_file", "source_sha256")}
    issues = []
    for chunk_id, reason in sorted(result.get("errors", {}).items()):
        validation = reason.startswith("ValueError:")
        recoverable = validation and has_coverage
        chunk = chunks.get(chunk_id, {})
        issues.append({**source, "type": "text_chunk_validation_failed" if validation else "text_chunk_request_failed",
                       "severity": "warning" if recoverable else "error", "status": "pending_review",
                       "chunk_id": chunk_id, "paragraph_ids": chunk.get("paragraph_ids", []),
                       "reason": reason, "attempts": len(chunk.get("timings", {}).get("attempts", [])),
                       "impact": "本块未纳入正文补充，相关序列或归属信息可能遗漏；已通过校验的结果保留。",
                       "suggested_action": "核对原文及引文，重新提取本块；已有成功块可复用缓存。"})
    for task in result.get("pending_tasks", []):
        if task["type"] == "missing_image_requires_pdf":
            continue  # 图片来源限制已单独保存在 pending_tasks。
        issues.append({**source, "type": task["type"], "severity": "warning", "status": "pending_review",
                       "reason": "正文片段超出处理预算，详见原始待处理任务。", "task": task,
                       "impact": "该片段未纳入正文补充，提取覆盖不完整。",
                       "suggested_action": "核对原文，调整文本预算或单独处理该片段。"})
    if result.get("status") == "failed":
        issues.append({**source, "type": "text_processing_failed", "severity": "error", "status": "pending_review",
                       "reason": result.get("error", "正文处理失败"),
                       "impact": "本篇正文未能完成处理。", "suggested_action": "修复错误后重新运行正文处理。"})
    result["issues"] = issues
    result.setdefault("summary", {})["review_issues"] = len(issues)
    output_dir = Path(output_dir)
    path = output_dir / "review_issues.json"
    result["issues_file"] = str(path.resolve())
    save_json(path, {**source, "text_sequence_status": result["status"], "issues": issues})
    lines = [f"{result['document_id']} 正文待核对问题", f"正文状态：{result['status']}，问题数：{len(issues)}",
             "问题备注不代表信息已补全；未通过校验的块不会用于补充序列。"]
    for issue in issues:
        lines.extend(["", f"[{issue['severity']}] {issue.get('chunk_id', issue['type'])}",
                      "来源段落：" + ", ".join(issue.get("paragraph_ids", [])),
                      f"原因：{issue['reason']}", f"影响：{issue['impact']}",
                      f"建议：{issue['suggested_action']}"])
    (output_dir / "review_issues.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return issues


class TextSequenceProcessor:
    def __init__(self, agent, *, max_chars=12000, concurrency=0, overlap_chars=600,
                 retries=2, retry_delay=1.0, timing=None):
        if concurrency < 0 or retries < 0 or retry_delay < 0:
            raise ValueError("正文提取并发必须非负（0表示全部块并发），重试和间隔不能为负")
        self.agent = agent
        self.max_chars, self.overlap_chars = max_chars, overlap_chars
        self.concurrency, self.retries, self.retry_delay = concurrency, retries, retry_delay
        self.timing = timing

    async def run(self, document, tables, output_dir):
        timing = self.timing or TimingRecorder()
        stage_started = perf_counter()
        prefix = document["document_id"] + "/"
        with timing.measure("text_split", item_id=prefix + "split"):
            chunks, pending = chunk_document(document, self.max_chars, self.overlap_chars)
        output_dir = Path(output_dir)
        results, errors, cached = {}, {}, []
        chunk_timings = {}
        inputs = iter(chunks)
        ready = perf_counter()
        worker_count = min(self.concurrency, len(chunks)) if self.concurrency else len(chunks)
        print(f"{document['document_id']} 正文扫描开始：{len(chunks)} 块，并发 {worker_count}；"
              f"以下进度为文本块，与图片进度分别计数。", flush=True)

        async def worker():
            for chunk in inputs:
                started = perf_counter()
                item_id = prefix + chunk["chunk_id"]
                timing.add("text_request", ready, started, kind="queue_wait", item_id=item_id)
                chunk_timing = {"queue_wait_seconds": started - ready, "attempts": [],
                                "cache_hit": False, "retry_sleep_seconds": 0.0}
                key = digest({"source": document["source_sha256"], "chunk": chunk,
                              "prompt": self.agent.system_prompt, "schema": 1})
                path = output_dir / "_text_sequence_chunks" / f"{key}.json"
                try:
                    cache_started = perf_counter()
                    with timing.measure("text_cache", item_id=item_id):
                        if path.is_file():
                            try:
                                result = validate_extraction(json.loads(path.read_text(encoding="utf-8")), chunk)
                                cached.append(chunk["chunk_id"])
                                chunk_timing["cache_hit"] = True
                            except (ValueError, KeyError, TypeError):
                                result = None
                        else:
                            result = None
                    chunk_timing["cache_seconds"] = perf_counter() - cache_started
                    if result is None:
                        feedback = None
                        for attempt in range(self.retries + 1):
                            attempt_started, attempt_status = perf_counter(), "ok"
                            attempt_seconds = None
                            try:
                                request = {**chunk, "validation_feedback": feedback} if feedback else chunk
                                with timing.measure("text_request", kind="request", item_id=item_id, attempt=attempt + 1):
                                    result = await self.agent.run(request)
                                with timing.measure("text_validate", item_id=item_id):
                                    result = validate_extraction(result, chunk)
                                with timing.measure("text_cache_write", item_id=item_id):
                                    save_json(path, result)
                                break
                            except Exception as exc:
                                attempt_status = "failed"
                                attempt_seconds = perf_counter() - attempt_started
                                if attempt == self.retries:
                                    raise
                                feedback = str(exc)
                                print(f"{item_id} 正文重试 {attempt + 1}/{self.retries}："
                                      f"{type(exc).__name__}: {str(exc)[:240]}", flush=True)
                                sleep_started = perf_counter()
                                with timing.measure("text_request", kind="retry_sleep", item_id=item_id):
                                    await asyncio.sleep(self.retry_delay * 2 ** attempt)
                                chunk_timing["retry_sleep_seconds"] += perf_counter() - sleep_started
                            finally:
                                chunk_timing["attempts"].append({"attempt": attempt + 1, "status": attempt_status,
                                                                "seconds": attempt_seconds if attempt_seconds is not None
                                                                else perf_counter() - attempt_started})
                    results[chunk["chunk_id"]] = result
                except Exception as exc:
                    errors[chunk["chunk_id"]] = f"{type(exc).__name__}: {exc}"
                finally:
                    chunk_timing["wall_seconds"] = perf_counter() - started
                    chunk_timings[chunk["chunk_id"]] = chunk_timing
                    if chunk["chunk_id"] in errors:
                        state = f"失败：{errors[chunk['chunk_id']][:240]}"
                    elif chunk["chunk_id"] in results:
                        state = "缓存命中" if chunk_timing["cache_hit"] else "成功"
                    else:
                        state = "已取消"
                    print(f"[正文 {len(results) + len(errors)}/{len(chunks)}] {item_id} {state}，"
                          f"耗时 {chunk_timing['wall_seconds']:.1f} 秒", flush=True)
        workers = [asyncio.create_task(worker()) for _ in range(worker_count)]
        try:
            await await_with_progress(asyncio.gather(*workers), lambda elapsed:
                f"{document['document_id']} 正文扫描仍在运行："
                f"完成 {len(results) + len(errors)}/{len(chunks)}，成功 {len(results)}，"
                f"失败 {len(errors)}，缓存 {len(cached)}，已等待 {elapsed:.0f} 秒")
        finally:
            for task in workers:
                task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
        records = [row for chunk in chunks for row in results.get(chunk["chunk_id"], {}).get("records", [])]
        with timing.measure("text_merge", item_id=prefix + "merge"):
            merged = merge_sequences(tables, records, document)
        status = "partial" if errors or any(t["type"] != "missing_image_requires_pdf" for t in pending) else "complete"
        result = {"document_id": document["document_id"], "status": status,
                  "source_coverage": "text_scanned_images_pending" if any(
                      t["type"] == "missing_image_requires_pdf" for t in pending) else "text_only",
                  "source_file": document["source_file"], "source_sha256": document["source_sha256"],
                  "input_tables_fingerprint": digest(tables),
                  "summary": {"chunks": len(chunks), "succeeded": len(results), "failed": len(errors),
                              "cached": len(cached), "extracted_records": len(records),
                              "merged_records": len(merged["records"]),
                              "supplementary_records": len(merged["synthetic_input"]["sequence_records"]),
                              "pending_tasks": len(pending)},
                  "errors": errors, "pending_tasks": pending,
                  "timings": {key: value for key, value in timing.report(
                      item_prefix=prefix, since=stage_started,
                      wall_seconds=perf_counter() - stage_started).items() if key != "events"},
                  "chunks": [{"chunk_id": chunk["chunk_id"],
                              "paragraph_ids": [p["id"] for p in chunk["paragraphs"]],
                              "status": "failed" if chunk["chunk_id"] in errors else "succeeded",
                              "timings": chunk_timings[chunk["chunk_id"]]}
                             for chunk in chunks], **merged}
        result["synthetic_input"]["result_file"] = str((output_dir / "text_sequences.json").resolve())
        with timing.measure("text_result_io", item_id=prefix + "result"):
            write_review_issues(output_dir, result)
            save_json(output_dir / "text_sequences.json", result)
        write_timing_report(output_dir, timing.report(item_prefix=prefix, since=stage_started,
                                                     wall_seconds=perf_counter() - stage_started))
        return result


async def main_async(args):
    from agent import TextSequenceExtractionAgent, codexllm

    timing = TimingRecorder()
    document_id = args.document_id or args.data_dir.name
    with timing.measure("load_inputs", kind="phase"):
        document = load_document(args.data_dir, document_id)
        tables = load_table_results(args.result_dir)
    if document is None:
        raise ValueError("未找到patent_context.json或patent_text(.zh).txt")
    processor = TextSequenceProcessor(TextSequenceExtractionAgent(codexllm()), max_chars=args.max_chars,
                                      concurrency=args.concurrency, overlap_chars=args.overlap_chars,
                                      retries=args.retries, timing=timing)
    with timing.measure("text_sequences", kind="phase"):
        result = await processor.run(document, tables, args.output_dir)
    # 单独命令只更新指定输出目录，可复用已完成的表格OCR。
    with timing.measure("costructure", kind="phase"):
        construction = construct_records([*tables, result["synthetic_input"]])
        save_json(args.output_dir / "costructure.json", construction)
    if args.construct_data:
        from agent import DataConstructionAgent
        with timing.measure("data_construction", kind="phase"):
            with timing.measure("construction", kind="request", item_id=document_id):
                records = await DataConstructionAgent(codexllm()).run([*tables, result["synthetic_input"]])
            save_json(args.output_dir / "data_construction.json", {"records": records})
    report = timing.report()
    report["run_mode"] = "text_sequences_and_construction" if args.construct_data else "text_sequences_only"
    report["cache_counts"] = {"text_chunks_cached": result["summary"]["cached"]}
    timing_path = write_timing_report(args.output_dir, report)
    print(json.dumps({"status": result["status"], **result["summary"]}, ensure_ascii=False, indent=2))
    print(f"耗时报告：{timing_path}")
    if result["issues"]:
        print(f"待核对问题：{args.output_dir / 'review_issues.txt'}")
    return 1 if any(issue["severity"] == "error" for issue in result["issues"]) else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--result-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--document-id")
    parser.add_argument("--max-chars", type=int, default=12000)
    parser.add_argument("--overlap-chars", type=int, default=600)
    parser.add_argument("--concurrency", type=int, default=0, help="正文请求并发数；0表示全部文本块并发（默认）")
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--construct-data", action="store_true", help="同时运行最终六字段拼接Agent")
    return asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
