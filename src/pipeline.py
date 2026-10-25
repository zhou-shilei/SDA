"""独立进程 OCR、统一表格提取及批次结束后的跨表拼接；分类路由可选。"""

import asyncio
import copy
import hashlib
import json
import math
import multiprocessing
from itertools import islice
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path
from time import perf_counter

from agent import (
    AssayExtractionAgent,
    DataConstructionAgent,
    SequenceExtractionAgent,
    TextSequenceExtractionAgent,
    TableClassificationAgent,
    UnifiedTableExtractionAgent,
    codexllm,
)
from table_ocr import (OCR_BACKENDS, configure_ocr_worker, extract_table_evidence,
                       extract_table_evidence_batch, structure_table_evidence)
from costructure import construct_by_document, document_folder, get_document_id, group_tables_by_document
from text_sequences import TextSequenceProcessor, load_document, write_review_issues
from timing import TimingRecorder, await_with_progress, write_timing_report
from schema_pipeline import build_document_dataset
from template_agent import template_to_tables
from table_continuation import load_crop_metadata, verified_context, extract_continuation_assays


@dataclass(frozen=True)
class PipelineConfig:
    extraction_mode: str = "table"
    document_repair_rounds: int = 2
    document_sequence_mapping: bool = False
    document_template_agent: bool = True
    template_agent_max_calls: int = 12
    template_agent_context_chars: int = 24000
    document_resume: str | None = None
    ocr_workers: int = 6  # 本机 A100 80GB 同图测试采用 6 进程；每个进程独立加载模型。
    ocr_batch_size: int = 1  # 提交的图片数；与后端实际模型 batch 分别记录。
    ocr_device: str = "gpu:0"
    ocr_cpu_threads: int = 4
    ocr_backend: str = "vl"
    reuse_table_ocr_from: str | None = None
    classify_tables: bool = False
    classification_batch_size: int = 5
    classification_max_chars: int = 20000
    classification_flush_seconds: float = 1.0
    classification_concurrency: int = 4
    extraction_concurrency: int = 8
    construction_concurrency: int = 4
    text_sequence_max_chars: int = 12000
    text_sequence_overlap_chars: int = 600
    text_sequence_concurrency: int = 0  # 0：同一专利全部文本块同时处理。
    text_sequences: bool = True
    text_context_dir: str | None = None
    template_completion: bool = True
    tst_concurrency: int = 4
    tst_max_tasks: int = 200
    text_pages_jsonl: str | None = None
    page_evidence: bool = True
    page_ocr_max_pages: int = 8
    sequence_mapping_max_calls: int = 8
    retries: int = 2
    retry_delay: float = 1.0
    limit: int | None = None

    def __post_init__(self):
        if self.extraction_mode not in {"table", "document"}:
            raise ValueError("extraction_mode 必须为 table 或 document")
        if self.document_repair_rounds < 0:
            raise ValueError("document_repair_rounds 必须非负")
        if self.template_agent_max_calls < 0 or self.template_agent_context_chars < 4000:
            raise ValueError("TemplateAgent 调用预算须非负，上下文预算至少 4000 字符")
        if self.extraction_mode == "document" and not self.text_pages_jsonl:
            raise ValueError("文档模式需要 --text-pages-jsonl 提供完整 OCR")
        for name in (
            "ocr_workers", "ocr_batch_size", "ocr_cpu_threads", "classification_batch_size",
            "classification_max_chars", "classification_concurrency",
            "extraction_concurrency", "construction_concurrency",
            "tst_concurrency", "tst_max_tasks",
            "sequence_mapping_max_calls",
        ):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} 必须大于 0")
        if self.text_sequence_concurrency < 0:
            raise ValueError("text_sequence_concurrency必须非负，0表示全部文本块并发")
        if self.page_ocr_max_pages < 0:
            raise ValueError("page_ocr_max_pages 必须非负；0 禁用额外页面 OCR")
        if self.text_sequence_max_chars < 1000:
            raise ValueError("text_sequence_max_chars至少1000")
        if not 0 <= self.text_sequence_overlap_chars <= self.text_sequence_max_chars // 4:
            raise ValueError("text_sequence_overlap_chars不能超过文本预算的1/4")
        if self.retries < 0 or self.retry_delay < 0 or not math.isfinite(self.retry_delay):
            raise ValueError("重试次数和间隔必须为非负有限数")
        if self.classification_flush_seconds <= 0 or not math.isfinite(self.classification_flush_seconds):
            raise ValueError("分类等待时间必须为正有限数")
        if self.limit is not None and self.limit < 1:
            raise ValueError("limit 必须大于 0")
        if self.ocr_device != "cpu" and not (
            self.ocr_device.startswith("gpu:") and self.ocr_device.split(":", 1)[1].isdigit()
        ):
            raise ValueError("ocr_device 应为 cpu 或 gpu:设备编号")
        if self.ocr_backend not in OCR_BACKENDS:
            raise ValueError(f"ocr_backend 应为 {OCR_BACKENDS}")


def _write_json(path, value):
    """在同一目录写临时文件后替换，避免留下写了一半的结果。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


class BatchPipeline:
    def __init__(self, image_dir, output_dir, config, llm=None):
        self.image_dir = Path(image_dir).resolve()
        self.output_dir = Path(output_dir).resolve()
        if not self.image_dir.is_dir():
            raise ValueError(f"图片目录不存在：{self.image_dir}")
        self.config = config
        self.llm = llm or codexllm(timeout=600 if config.extraction_mode == "document" else None,
                                   artifact_dir=(self.output_dir / "requests" / ("resume_" + str(int(perf_counter())))
                                                 if config.document_resume else self.output_dir / "requests")
                                   if config.extraction_mode == "document" else None)
        if llm is None and config.extraction_mode == "document":
            from document_extraction import response_schema
            self.llm.output_schema = response_schema()
        self.schema_results = {}
        self.schema_errors = {}
        self.images = sorted(path for path in self.image_dir.rglob("*.png") if path.is_file())
        self.crop_metadata = load_crop_metadata(self.image_dir)
        if self.crop_metadata:
            # Dependencies must be scheduled before their continuation crops,
            # including with a single OCR worker and shuffled filenames.
            self.images.sort(key=lambda p: (self.crop_metadata.get(str(p), {}).get("document_id") or
                get_document_id({"image_path": str(p)}) or "",
                self.crop_metadata.get(str(p), {}).get("page", 0),
                self.crop_metadata.get(str(p), {}).get("bbox", [0, 0])[1], p.name))
        if config.limit is not None:
            self.images = self.images[:config.limit]
        self.document_ids = {str(path): self.crop_metadata.get(str(path), {}).get("document_id") or
                             get_document_id({"image_path": str(path)}) for path in self.images}
        self._ocr_states = {}
        self._ocr_ready = {str(p): asyncio.Event() for p in self.images}
        self._region_images = {(self.document_ids[str(p)], self.crop_metadata[str(p)]["id"]): str(p)
                               for p in self.images if str(p) in self.crop_metadata}
        self.outputs = {}
        for path in self.images:
            folder = document_folder(self.document_ids[str(path)])
            relative = path.relative_to(self.image_dir).with_suffix(".json")
            if len(relative.parts) > 1 and relative.parts[0] == folder:
                relative = Path(*relative.parts[1:])
            self.outputs[str(path)] = self.output_dir / folder / relative
        # 避免同名输入图片覆盖汇总文件。
        for image, output in self.outputs.items():
            if output.name in {"costructure.json", "data_construction.json", "_batch_summary.json"}:
                self.outputs[image] = output.with_suffix(".table.json")
        # A requested reuse run is all-or-nothing. Validate the entire batch
        # before workers can call a model or initialize a GPU process.
        self.reused_ocr = self._load_reusable_ocr() if config.reuse_table_ocr_from is not None else {}
        self.classifier = TableClassificationAgent(llm or codexllm())
        self.assay = AssayExtractionAgent(llm or codexllm())
        self.sequence = SequenceExtractionAgent(llm or codexllm())
        self.unified = UnifiedTableExtractionAgent(llm or codexllm())
        self.constructor = DataConstructionAgent(llm or codexllm())
        self.text_sequence = TextSequenceExtractionAgent(llm or codexllm())
        # 队列和 worker 数量都有限制，下游繁忙时上游会等待。
        self.ocr_queue = asyncio.Queue(maxsize=(config.classification_batch_size * config.classification_concurrency * 2
                                               if config.classify_tables else config.extraction_concurrency * 2))
        self.batch_queue = asyncio.Queue(maxsize=config.classification_concurrency * 2)
        self.extraction_queue = asyncio.Queue(maxsize=config.extraction_concurrency * 2)
        self.metrics = {
            stage: {"requests": 0, "failed_requests": 0, "seconds": 0.0,
                    "tables_succeeded": 0, "tables_failed": 0}
            for stage in ("ocr", "classification", "extraction")
        }
        self.metrics["construction"] = {
            "requests": 0, "failed_requests": 0, "seconds": 0.0,
            "documents_succeeded": 0, "documents_failed": 0,
        }
        self.metrics["ocr"]["cache_hits"] = 0
        self.succeeded = 0
        self.failed = 0
        self.started = 0.0
        self.timing = TimingRecorder()
        if self.config.document_resume:
            previous = self.output_dir / "_token_usage.json"
            if previous.is_file():
                from token_usage import TokenUsageRecorder
                self.timing.usage_recorder = TokenUsageRecorder(json.loads(previous.read_text()).get("events", []))

    def _load_reusable_ocr(self):
        """Match original evidence by exact image/document/table identity.

        Only original OCR fields are reused. Old classifications, extraction
        results, errors, and continuation decisions never enter the new run.
        """
        directory = Path(self.config.reuse_table_ocr_from).expanduser().resolve()
        if not directory.is_dir():
            raise ValueError(f"OCR 复用目录不存在：{directory}")
        candidates = {str(image): [] for image in self.images}
        for path in sorted(directory.rglob("*.json")):
            try:
                row = json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, OSError, UnicodeError):
                # A malformed intended table will be reported as missing below;
                # unrelated summaries are not themselves table evidence.
                continue
            if not isinstance(row, dict) or not ({"table_text", "table_evidence"} & row.keys()):
                continue
            image_path = row.get("image_path")
            if not isinstance(image_path, str) or not Path(image_path).is_absolute():
                continue
            image = str(Path(image_path).resolve())
            if image in candidates:
                candidates[image].append((path, row))
        reused = {}
        for image in self.images:
            matches = candidates[str(image)]
            if not matches:
                raise ValueError(f"OCR 复用缺少有效原始表格结果：{image}；不会自动重新 OCR")
            if len(matches) != 1:
                raise ValueError(f"OCR 复用结果重复，无法唯一匹配：{image}：{[str(p) for p, _ in matches]}")
            source, row = matches[0]
            document_id = self.document_ids[str(image)]
            relative = image.relative_to(self.image_dir).as_posix()
            table_ids = {relative}
            if document_id and not relative.startswith(document_id + "/"):
                table_ids.add(f"{document_id}/{relative}")
            if row.get("document_id") != document_id or row.get("table_id") not in table_ids:
                raise ValueError(f"OCR 复用文档/表格身份不匹配：{source}；当前 {document_id}/{relative}")
            metadata = self.crop_metadata.get(str(image)) or {"id": relative, "document_id": document_id}
            evidence = row.get("table_evidence")
            text = row.get("table_text")
            if evidence is not None and not isinstance(evidence, dict):
                raise ValueError(f"OCR 复用 table_evidence 无效：{source}")
            if text is None and evidence is not None:
                text = evidence.get("text")
            if not isinstance(text, str) or not text.strip():
                raise ValueError(f"OCR 复用 table_text 为空或无效：{source}")
            if evidence:
                evidence_ids = {metadata["id"]} if str(image) in self.crop_metadata else table_ids
                if evidence.get("text") != text:
                    raise ValueError(f"OCR 复用 table_text 与 table_evidence 不一致：{source}")
                if "document_id" in evidence and evidence["document_id"] != document_id:
                    raise ValueError(f"OCR 复用证据文档身份不匹配：{source}")
                if "table_id" in evidence and evidence["table_id"] not in evidence_ids:
                    raise ValueError(f"OCR 复用证据表格身份不匹配：{source}")
                if "image_path" in evidence and str(Path(evidence["image_path"]).resolve()) != str(image):
                    raise ValueError(f"OCR 复用证据图片身份不匹配：{source}")
                for key, actual in (
                    ("image_sha256", hashlib.sha256(image.read_bytes()).hexdigest()),
                    ("text_sha256", hashlib.sha256(text.encode()).hexdigest()),
                ):
                    if key in evidence and evidence[key] != actual:
                        raise ValueError(f"OCR 复用证据 {key} 已失效：{source}")
                evidence = copy.deepcopy(evidence)
            else:
                evidence = structure_table_evidence(text, image, metadata)
            reused[str(image)] = {"evidence": evidence, "source_file": str(source)}
        return reused

    def _save(self, state, stage=None, error=None):
        if error is not None:
            state["error_stage"] = stage
            state["error"] = f"{type(error).__name__}: {error}"
        # 保存错误要向上传播，不能误报为已完成。
        path = self.outputs[state["image_path"]]
        with self.timing.measure("result_io", item_id=state["table_id"]):
            _write_json(path, state)
        if error is None:
            self.succeeded += 1
        else:
            self.failed += 1
        completed = self.succeeded + self.failed
        status = "成功" if error is None else f"失败（{stage}）：{error}"
        print(f"[{completed}/{len(self.images)}] {state['table_id']} {status}，已保存：{path}", flush=True)

    async def _request(self, stage, operation, *, item_id=None, attempt=1):
        metric = self.metrics[stage]
        metric["requests"] += 1
        started = perf_counter()
        try:
            with self.timing.measure(stage, kind="request", item_id=item_id, attempt=attempt):
                if stage == "construction":
                    print(f"{item_id} 最终数据构建请求开始（第 {attempt} 次尝试）", flush=True)
                    return await await_with_progress(operation(), lambda elapsed:
                        f"{item_id} 最终数据构建仍在等待模型：本次请求已等待 {elapsed:.0f} 秒")
                return await operation()
        except Exception:
            metric["failed_requests"] += 1
            raise
        finally:
            metric["seconds"] += perf_counter() - started

    async def _retry(self, stage, table_id, operation):
        for attempt in range(self.config.retries + 1):
            try:
                return await self._request(stage, operation, item_id=table_id, attempt=attempt + 1)
            except Exception:
                if attempt == self.config.retries:
                    raise
                print(f"{stage} 重试 {attempt + 1}/{self.config.retries}：{table_id}", flush=True)
                with self.timing.measure(stage, kind="retry_sleep", item_id=table_id):
                    await asyncio.sleep(self.config.retry_delay * 2 ** attempt)

    def _new_ocr_state(self, image):
        state = {"document_id": self.document_ids[str(image)],
                 "table_id": image.relative_to(self.image_dir).as_posix(),
                 "image_path": str(image), "assay_records": [], "sequence_records": []}
        metadata = self.crop_metadata.get(str(image))
        if metadata:
            state["layout"] = metadata
        return state

    def _publish_ocr_state(self, state, evidence):
        if isinstance(evidence, str):
            evidence = structure_table_evidence(evidence, state["image_path"], state.get("layout") or
                {"id": state["table_id"], "document_id": state["document_id"]})
        state["table_evidence"] = evidence
        state["table_text"] = evidence["text"]
        self.metrics["ocr"]["tables_succeeded"] += 1
        self._ocr_states[state["image_path"]] = state
        self._ocr_ready[state["image_path"]].set()

    def _fail_ocr_state(self, state, exc):
        self._ocr_states[state["image_path"]] = {"error": str(exc)}
        self._ocr_ready[state["image_path"]].set()
        self.metrics["ocr"]["tables_failed"] += 1
        self._save(state, "ocr", exc)

    async def _forward_ocr_state(self, state):
        if state.get("layout"):
            state["extraction_context"] = await self._continuation_context(state["layout"], state["table_text"])
        if self.config.extraction_mode == "document":
            state["extraction_method"] = "document_pending"
            self._save(state)
            return
        state["_classification_ready" if self.config.classify_tables else "_extraction_ready"] = perf_counter()
        await self.ocr_queue.put(state)

    async def _ocr_worker(self, images, pool):
        loop = asyncio.get_running_loop()
        for image in images:
            state = self._new_ocr_state(image)
            metadata = self.crop_metadata.get(str(image))
            if metadata:
                state["layout"] = metadata
            ocr_metadata = metadata or {"id": state["table_id"], "document_id": state["document_id"]}
            try:
                if self.config.reuse_table_ocr_from is not None:
                    with self.timing.measure("ocr_cache", item_id=state["table_id"]):
                        cached = self.reused_ocr[str(image)]
                        evidence = copy.deepcopy(cached["evidence"])
                        state["ocr_source"] = {"mode": "reused", "result_file": cached["source_file"]}
                        self.metrics["ocr"]["cache_hits"] += 1
                else:
                    evidence = await self._retry(
                        "ocr", state["table_id"],
                        lambda: loop.run_in_executor(pool, extract_table_evidence, str(image), ocr_metadata),
                    )
                self._publish_ocr_state(state, evidence)
            except Exception as exc:
                self._fail_ocr_state(state, exc)
                continue
            await self._forward_ocr_state(state)

    async def _ocr_worker_batched(self, images, pool):
        loop = asyncio.get_running_loop()
        while batch := list(islice(images, self.config.ocr_batch_size)):
            states = [self._new_ocr_state(image) for image in batch]
            metadata = [s.get("layout") or {"id": s["table_id"], "document_id": s["document_id"]} for s in states]
            try:
                outputs = await self._retry("ocr", "batch:" + states[0]["table_id"],
                    lambda: loop.run_in_executor(pool, extract_table_evidence_batch,
                                                 [str(image) for image in batch], metadata))
                if len(outputs) != len(states) or any(not isinstance(e, dict) or not isinstance(e.get("text"), str)
                                                       or not e["text"] or Path(e.get("image_path", "")).resolve() != image.resolve()
                                                       for image, e in zip(batch, outputs)):
                    raise ValueError("Batch OCR result count or image identity mismatch")
                # Publish every raw result before awaiting continuation headers:
                # a later table in this same batch may depend on an earlier one.
                for state, evidence in zip(states, outputs):
                    self._publish_ocr_state(state, evidence)
            except Exception as exc:
                for state in states:
                    self._fail_ocr_state(state, exc)
                continue
            for state in states:
                await self._forward_ocr_state(state)

    async def _continuation_context(self, metadata, text):
        parents, seen, current = [], {metadata["id"]}, metadata
        while current.get("table_context", {}).get("continuation_of"):
            parent_id = current["table_context"]["continuation_of"]
            if parent_id in seen:
                raise ValueError("Cyclic continuation metadata")
            seen.add(parent_id)
            image = self._region_images.get((metadata.get("document_id"), parent_id))
            if image is None:
                context = verified_context(metadata, text, parents)
                context["issues"].append("continuation_parent_not_in_batch")
                return context
            parent_metadata = self.crop_metadata[image]
            if current["page"] != parent_metadata["page"] + 1:
                context = verified_context(metadata, text, parents)
                context["issues"].append("invalid_continuation_page_order")
                return context
            await self._ocr_ready[image].wait()
            parent = self._ocr_states[image]
            if parent.get("error"):
                context = verified_context(metadata, text, parents)
                context["issues"].append("continuation_parent_ocr_failed")
                return context
            current = parent_metadata
            parents.append((current, parent["table_text"]))
        return verified_context(metadata, text, parents)

    async def _produce_ocr(self, images, pool):
        # 同一事件循环中共享迭代器，每张图片只会被一个 worker 领取。
        worker = (self._ocr_worker_batched if self.config.ocr_batch_size > 1
                  and self.config.ocr_backend == "vl" and self.config.reuse_table_ocr_from is None
                  else self._ocr_worker)
        workers = [asyncio.create_task(worker(images, pool))
                   for _ in range(min(self.config.ocr_workers, len(self.images)))]
        try:
            await asyncio.gather(*workers)
            await self.ocr_queue.put(None)
        finally:
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)

    async def _batcher(self):
        batch = []
        chars = 0
        deadline = 0.0
        while True:
            try:
                if batch:
                    state = await asyncio.wait_for(
                        self.ocr_queue.get(), max(0.0, deadline - perf_counter())
                    )
                else:
                    state = await self.ocr_queue.get()
            except asyncio.TimeoutError:
                await self.batch_queue.put(batch)
                batch, chars = [], 0
                continue
            if state is None:
                if batch:
                    await self.batch_queue.put(batch)
                for _ in range(self.config.classification_concurrency):
                    await self.batch_queue.put(None)
                return
            size = len(state["table_text"])
            if batch and chars + size > self.config.classification_max_chars:
                await self.batch_queue.put(batch)
                batch, chars = [], 0
            if not batch:
                deadline = perf_counter() + self.config.classification_flush_seconds
            batch.append(state)
            chars += size
            # 超长单表独立提交，保留原文，不截断。
            if len(batch) >= self.config.classification_batch_size or chars >= self.config.classification_max_chars:
                await self.batch_queue.put(batch)
                batch, chars = [], 0

    async def _classify(self, batch):
        started = perf_counter()
        for state in batch:
            ready = state.pop("_classification_ready", None)
            if ready is not None:
                self.timing.add("classification", ready, started, kind="queue_wait", item_id=state["table_id"])
        tables, deterministic = [], {}
        for state in batch:
            context = state.get("extraction_context")
            records = extract_continuation_assays(state["table_text"], context)
            if records is not None:
                deterministic[state["table_id"]] = {"category": "assay",
                    "reason": "Verified continuation header and executable metric columns"}
                state["_continuation_assays"] = records
                continue
            table = {"table_id": state["table_id"], "table_text": state["table_text"]}
            if state.get("extraction_context", {}).get("support_tables"):
                table["extraction_context"] = state["extraction_context"]
            tables.append(table)
        if not tables:
            return deterministic, {}
        try:
            results, errors = await self._request(
                "classification", lambda: self.classifier.run_batch(tables),
                item_id=" | ".join(state["table_id"] for state in batch),
                attempt=max((state.get("_classification_attempt", 1) for state in batch), default=1),
            )
            if errors:
                self.metrics["classification"]["failed_requests"] += 1
            return {**deterministic, **results}, errors
        except Exception as exc:
            return deterministic, {table["table_id"]: f"{type(exc).__name__}: {exc}" for table in tables}

    async def _route(self, state, classification):
        state["classification"] = classification
        self.metrics["classification"]["tables_succeeded"] += 1
        if classification["category"] == "other":
            self._save(state)
        else:
            state["_extraction_ready"] = perf_counter()
            await self.extraction_queue.put(state)

    async def _classification_worker(self):
        while True:
            batch = await self.batch_queue.get()
            if batch is None:
                return
            results, errors = await self._classify(batch)
            for state in batch:
                if state["table_id"] in results:
                    await self._route(state, results[state["table_id"]])
            # 已成功的表格继续流向下游，只对异常项逐表重试。
            for state in batch:
                table_id = state["table_id"]
                if table_id not in errors:
                    continue
                error = errors[table_id]
                for attempt in range(self.config.retries):
                    print(f"classification 单表重试 {attempt + 1}/{self.config.retries}：{table_id}", flush=True)
                    with self.timing.measure("classification", kind="retry_sleep", item_id=table_id):
                        await asyncio.sleep(self.config.retry_delay * 2 ** attempt)
                    state["_classification_attempt"] = attempt + 2
                    retried, invalid = await self._classify([state])
                    if table_id in retried:
                        await self._route(state, retried[table_id])
                        break
                    error = invalid[table_id]
                else:
                    self.metrics["classification"]["tables_failed"] += 1
                    self._save(state, "classification", ValueError(error))

    async def _classify_all(self):
        workers = [asyncio.create_task(self._classification_worker())
                   for _ in range(self.config.classification_concurrency)]
        try:
            await asyncio.gather(*workers)
            for _ in range(self.config.extraction_concurrency):
                await self.extraction_queue.put(None)
        finally:
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)

    async def _forward_to_extraction(self):
        """关闭分类时每张 OCR 结果都进入统一提取，不按表格类型筛选。"""
        while True:
            state = await self.ocr_queue.get()
            if state is None:
                for _ in range(self.config.extraction_concurrency):
                    await self.extraction_queue.put(None)
                return
            await self.extraction_queue.put(state)

    async def _extraction_worker(self):
        while True:
            state = await self.extraction_queue.get()
            if state is None:
                return
            ready = state.pop("_extraction_ready", None)
            if ready is not None:
                self.timing.add("extraction", ready, perf_counter(), kind="queue_wait", item_id=state["table_id"])
            try:
                context = state.get("extraction_context")
                if not self.config.classify_tables:
                    # 即使表格有可执行的测量列，也让统一提取保留其中可能存在的序列。
                    extracted = await self._retry("extraction", state["table_id"],
                        lambda: self.unified.run(state["table_text"], table_context=context))
                    state["assay_records"] = extracted["assay_records"]
                    state["sequence_records"] = extracted["sequence_records"]
                    state["extraction_method"] = "unified_agent"
                else:
                    sequence = state["classification"]["category"] == "sequence"
                    agent = self.sequence if sequence else self.assay
                    with self.timing.measure("continuation_extract", item_id=state["table_id"]):
                        records = state.pop("_continuation_assays", None)
                        if records is None and not sequence:
                            records = extract_continuation_assays(state["table_text"], context)
                    if records is not None:
                        state["extraction_method"] = "verified_continuation_grid"
                    else:
                        operation = (lambda: agent.run(state["table_text"], table_context=context)) if context and context.get("support_tables") else (
                            lambda: agent.run(state["table_text"]))
                        records = await self._retry("extraction", state["table_id"], operation)
                    state["sequence_records" if sequence else "assay_records"] = records
            except Exception as exc:
                self.metrics["extraction"]["tables_failed"] += 1
                self._save(state, "extraction", exc)
                continue
            self.metrics["extraction"]["tables_succeeded"] += 1
            self._save(state)

    async def _construct_data(self, tables):
        """等待提取结束后按文档调用 Agent，文档间使用独立并发限制。"""
        groups = group_tables_by_document(tables)
        semaphore = asyncio.Semaphore(self.config.construction_concurrency)

        async def process(folder, document_tables):
            path = self.output_dir / folder / "data_construction.json"
            valid_tables = [table for table in document_tables
                            if not table.get("error") and not table.get("error_stage")]
            queued = perf_counter()
            async with semaphore:
                self.timing.add("construction", queued, perf_counter(), kind="queue_wait", item_id=folder)
                try:
                    if not valid_tables:
                        raise ValueError("此文档没有成功提取的表格，无法构建最终数据")
                    if self.config.template_completion and folder in self.schema_errors:
                        raise ValueError(self.schema_errors[folder])
                    if self.config.template_completion and folder in self.schema_results:
                        # 固定 schema 已校验来源和槽位，按原实验顺序导出，不再让模型重写。
                        records = self.schema_results[folder]["records"]
                    else:
                        records = await self._retry(
                            "construction", folder, lambda: self.constructor.run(valid_tables)
                        )
                except Exception as exc:
                    error = f"{type(exc).__name__}: {exc}"
                    # 覆盖旧结果并明确标记失败，避免旧数据被误认为本批产物。
                    with self.timing.measure("result_io", item_id=str(path)):
                        _write_json(path, {"status": "failed", "records": [], "error": error})
                    self.metrics["construction"]["documents_failed"] += 1
                    print(f"{folder} 数据构建失败：{error}", flush=True)
                    return folder, path, 0, error
                with self.timing.measure("result_io", item_id=str(path)):
                    _write_json(path, {"records": records})
                self.metrics["construction"]["documents_succeeded"] += 1
                print(f"{folder} 数据构建完成：{len(records)} 条，已保存：{path}", flush=True)
                return folder, path, len(records), None

        tasks = [asyncio.create_task(process(folder, group)) for folder, group in groups.items()]
        try:
            results = await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        return {
            "succeeded": sum(error is None for _, _, _, error in results),
            "failed": sum(error is not None for _, _, _, error in results),
            "records": sum(count for _, _, count, _ in results),
            "files": {folder: str(path) for folder, path, _, _ in results},
            "errors": {folder: error for folder, _, _, error in results if error is not None},
        }

    async def _complete_templates(self, tables):
        """每篇专利建立一次 schema，再按缺口完成 TST 检索与动态实例提取。"""
        summary = {"enabled": self.config.text_sequences, "mode": "schema_tst",
                   "succeeded": 0, "partial": 0, "failed": 0, "skipped": 0,
                   "files": {}, "documents": {}, "errors": {}, "warnings": {}}
        root = Path(self.config.text_context_dir).resolve() if self.config.text_context_dir else self.image_dir
        augmented = []
        for folder, document_tables in group_tables_by_document(tables).items():
            if folder == "_unassigned":
                augmented.extend(document_tables)
                summary["skipped"] += 1
                continue
            directory = root if root.name == folder else root / folder
            output = self.output_dir / folder
            try:
                with self.timing.measure("template_completion", kind="request", item_id=folder):
                    result = await build_document_dataset(
                        folder, document_tables, directory, output, llm=self.llm,
                        complete_text=self.config.text_sequences,
                        text_pages_jsonl=self.config.text_pages_jsonl,
                        page_evidence=self.config.page_evidence,
                        page_ocr_max_pages=self.config.page_ocr_max_pages,
                        page_ocr_device=self.config.ocr_device,
                        page_ocr_cpu_threads=self.config.ocr_cpu_threads,
                        sequence_mapping_max_calls=self.config.sequence_mapping_max_calls,
                        concurrency=self.config.tst_concurrency, max_tasks=self.config.tst_max_tasks,
                        timing=self.timing)
                self.schema_results[folder] = result
                report = result["report"]
                if report["status"] == "failed":
                    raise ValueError(f"TST 正文补全失败：{report.get('errors', [])}")
                augmented.extend(template_to_tables(result["template"], document_tables))
                summary["files"][folder] = str(output / "dataset.json")
                missing = result["template"]["missing"]
                summary["documents"][folder] = {"status": report["status"],
                    "remaining_missing_fields": len(missing), "records": len(result["records"]),
                    "wall_seconds": report["wall_seconds"], "mode": "schema_tst"}
                if missing or report["status"] == "partial" or result["template"].get("issues"):
                    summary["partial"] += 1
                    issues = [{"severity": "warning", "reason": "字段仍缺少证据", **task} for task in missing]
                    issues.extend({"severity": "warning", **issue} for issue in result["template"].get("issues", []))
                    issues.extend({"severity": "warning", "kind": "new_text_observation", "candidate": candidate}
                                  for candidate in report.get("candidates", []))
                    issues.extend({"severity": "warning", "kind": "tst_request_error", **error}
                                  for error in report.get("errors", []))
                    issues.extend({"severity": "warning", **issue}
                                  for issue in report.get("page_evidence_issues", []))
                    issues.extend({"severity": "warning", "kind": "rejected_sequence_mapping", **issue}
                                  for issue in report.get("sequence_mapping", {}).get("rejected", []))
                    issues.extend({"severity": "warning", "kind": "rejected_completion", "reason": item.get("reason"),
                                   "task_id": item.get("task", {}).get("task_id")}
                                  for item in report.get("rejected", []))
                    summary["warnings"][folder] = issues
                    _write_json(output / "review_issues.json", {"document_id": folder, "issues": issues})
                    (output / "review_issues.txt").write_text(
                        "\n".join([*(f"{task['entity_id']} {task.get('observation_id') or ''} {task['field_path']}：仍缺少明确证据"
                                    for task in missing),
                                   *(f"{issue.get('kind', 'review')}：{issue.get('entity_id', '')} {issue.get('field_path', '')} {issue.get('reason', '')}"
                                     for issue in issues if issue.get("kind"))]) + "\n", encoding="utf-8")
                else:
                    summary["succeeded"] += 1
                    _write_json(output / "review_issues.json", {"document_id": folder, "issues": []})
                    (output / "review_issues.txt").write_text("无未解决的必需字段。\n", encoding="utf-8")
                print(f"{folder} schema/TST 完成：{len(result['records'])} 条实验，"
                      f"仍缺 {len(missing)} 个字段；产物：{output / 'dataset.json'}", flush=True)
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                self.schema_errors[folder] = error
                summary["failed"] += 1
                summary["errors"][folder] = error
                augmented.extend(document_tables)
                _write_json(output / "dataset.json", {"document_id": folder, "status": "failed", "records": [], "error": error})
                _write_json(output / "review_issues.json", {"document_id": folder,
                    "issues": [{"severity": "error", "kind": "template_completion_failed", "reason": error}]})
                (output / "review_issues.txt").write_text(error + "\n", encoding="utf-8")
                print(f"{folder} schema/TST 失败：{error}", flush=True)
        return augmented, summary

    async def _extract_text_sequences(self, tables):
        """按专利执行一次正文提取；缓存按正文、提示词和文本块指纹隔离。"""
        summary = {"enabled": self.config.text_sequences, "succeeded": 0, "partial": 0, "failed": 0,
                   "skipped": 0, "files": {}, "documents": {}, "errors": {}, "warnings": {}}
        if not self.config.text_sequences:
            return tables, summary
        root = Path(self.config.text_context_dir).resolve() if self.config.text_context_dir else self.image_dir
        groups = group_tables_by_document(tables)
        # 无图片时仍能处理只有正文的新专利，序列实体不依赖实验记录存在。
        if not self.images:
            for name in ("patent_context.json", "patent_text.zh.txt", "patent_text.txt"):
                for path in sorted(root.rglob(name)):
                    document_id = get_document_id({"image_path": str(path)})
                    if document_id:
                        groups.setdefault(document_folder(document_id), [])
        processor = TextSequenceProcessor(
            self.text_sequence, max_chars=self.config.text_sequence_max_chars,
            overlap_chars=self.config.text_sequence_overlap_chars,
            concurrency=self.config.text_sequence_concurrency,
            retries=self.config.retries, retry_delay=self.config.retry_delay,
            timing=self.timing,
        )
        augmented = list(tables)
        for folder, document_tables in groups.items():
            if folder == "_unassigned":
                summary["skipped"] += 1
                continue
            directory = root if root.name == folder else root / folder
            try:
                with self.timing.measure("text_load", item_id=folder):
                    document = load_document(directory, folder)
                if document is None:
                    summary["skipped"] += 1
                    print(f"{folder} 无正文文件，跳过正文提取：{directory}", flush=True)
                    continue
                result = await processor.run(document, document_tables, self.output_dir / folder)
                augmented.append(result["synthetic_input"])
                summary["files"][folder] = str(self.output_dir / folder / "text_sequences.json")
                summary["documents"][folder] = {"status": result["status"], **result["summary"],
                                               "issues_file": result["issues_file"],
                                               "timings": result["timings"]}
                if any(issue["severity"] == "error" for issue in result["issues"]):
                    summary["failed"] += 1
                    summary["errors"][folder] = result["errors"]
                elif result["status"] == "partial":
                    summary["partial"] += 1
                    summary["warnings"][folder] = result["issues"]
                    print(f"{folder} 正文部分完成，问题已备注：{result['issues_file']}", flush=True)
                else:
                    summary["succeeded"] += 1
                print(f"{folder} 正文序列提取：{result['summary']}，状态：{result['status']}", flush=True)
            except Exception as exc:
                summary["failed"] += 1
                summary["errors"][folder] = f"{type(exc).__name__}: {exc}"
                # 覆盖旧的正文产物，避免将旧结果误认为本批提取成功。
                failed_result = {
                    "document_id": folder, "status": "failed", "records": [],
                    "error": summary["errors"][folder]}
                write_review_issues(self.output_dir / folder, failed_result)
                _write_json(self.output_dir / folder / "text_sequences.json", failed_result)
                print(f"{folder} 正文序列提取失败：{summary['errors'][folder]}", flush=True)
        return augmented, summary

    async def run(self, ocr_executor=None):
        """ocr_executor 可由调用方提供；默认创建独立的 spawn 进程池。"""
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.started = perf_counter()
        if not self.config.document_resume:
            self.timing = TimingRecorder()
        table_started = perf_counter()
        owned_pool = (ocr_executor is None and bool(self.images)
                      and self.config.reuse_table_ocr_from is None)
        pool = ocr_executor
        tasks = []
        print(f"批处理开始：{len(self.images)} 张，配置：{asdict(self.config)}", flush=True)
        try:
            if self.images:
                if owned_pool:
                    pool = ProcessPoolExecutor(
                        max_workers=self.config.ocr_workers,
                        mp_context=multiprocessing.get_context("spawn"),
                        initializer=configure_ocr_worker,
                        initargs=(self.config.ocr_device, self.config.ocr_cpu_threads,
                                  self.config.ocr_backend, self.config.ocr_batch_size),
                    )
                tasks = [asyncio.create_task(self._produce_ocr(iter(self.images), pool))] if self.config.extraction_mode == "document" else [
                    asyncio.create_task(self._produce_ocr(iter(self.images), pool)),
                    *([asyncio.create_task(self._batcher()), asyncio.create_task(self._classify_all())]
                      if self.config.classify_tables else [asyncio.create_task(self._forward_to_extraction())]),
                    *[asyncio.create_task(self._extraction_worker())
                      for _ in range(self.config.extraction_concurrency)],
                ]
                await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if owned_pool and pool is not None:
                print("图片任务已结束，正在关闭 OCR 进程池……", flush=True)
                await asyncio.to_thread(pool.shutdown, wait=True, cancel_futures=True)
            self.timing.add("table_processing", table_started, perf_counter(), kind="phase")
        print(f"图片阶段结束：成功 {self.succeeded}，失败 {self.failed}；正在汇总表格结果。", flush=True)
        # 只汇总本批输入对应的结果，避免混入输出目录中旧批次的数据。
        # 必须等待所有提取 worker 完成，序列表和实验表才能互相关联。
        with self.timing.measure("load_table_results", kind="phase"):
            tables = [
                {**json.loads(path.read_text(encoding="utf-8")), "result_file": str(path)}
                for path in self.outputs.values()
            ]
        if self.config.extraction_mode == "document":
            return await self._run_documents(tables)
        # 没有表格锚点时保留正文发现入口；存在表格则由缺失槽驱动检索。
        use_template = self.config.template_completion and bool(tables)
        with self.timing.measure("template_tst" if use_template else "text_sequences", kind="phase"):
            if use_template:
                print("进入固定 schema、缺失检查与 TST 补全阶段……", flush=True)
                tables, text_sequence_summary = await self._complete_templates(tables)
            else:
                print("进入原全文序列扫描阶段……" if self.config.text_sequences else "正文序列阶段已关闭。", flush=True)
                tables, text_sequence_summary = await self._extract_text_sequences(tables)
        print("进入序列与实验数据拼接阶段……", flush=True)
        construction_started = perf_counter()
        with self.timing.measure("costructure"):
            constructions = construct_by_document(tables)
        construction_files = {}
        construction_summary = Counter()
        for folder, construction in constructions.items():
            path = self.output_dir / folder / "costructure.json"
            with self.timing.measure("result_io", item_id=str(path)):
                _write_json(path, construction)
            construction_files[folder] = str(path)
            construction_summary.update(construction["summary"])
            print(f"{folder} 拼接完成：{construction['summary']}，已保存：{path}", flush=True)
        self.timing.add("costructure", construction_started, perf_counter(), kind="phase")
        with self.timing.measure("data_construction", kind="phase"):
            print("进入最终数据构建阶段……", flush=True)
            data_construction = await self._construct_data(tables)
        timing_report = self.timing.report()
        timing_report["run_mode"] = "batch_pipeline"
        timing_report["cache_counts"] = {"table_ocr_cached": self.metrics["ocr"]["cache_hits"],
                                       "text_chunks_cached": sum(
            doc.get("cached", 0) for doc in text_sequence_summary["documents"].values())}
        timing_path = write_timing_report(self.output_dir, timing_report)
        summary = {
            "status": "failed" if self.failed or data_construction["failed"] or text_sequence_summary["failed"]
                      else "partial" if text_sequence_summary["partial"] else "complete",
            "total": len(self.images), "succeeded": self.succeeded, "failed": self.failed,
            "wall_seconds": round(perf_counter() - self.started, 3),
            "config": asdict(self.config), "stages": self.metrics,
            "construction": dict(construction_summary),
            "construction_files": construction_files,
            "data_construction": data_construction,
            "text_sequences": text_sequence_summary,
            "template_completion": {"enabled": self.config.template_completion,
                                    "files": {folder: str(self.output_dir / folder / "dataset.json")
                                              for folder in self.schema_results}},
            "timings": {key: timing_report[key] for key in ("phase_ranking", "stage_ranking", "slowest_requests")},
            "timing_file": str(timing_path),
            "token_usage": timing_report["token_usage"],
            "token_usage_file": str(self.output_dir / "_token_usage.json"),
        }
        _write_json(self.output_dir / "_batch_summary.json", summary)
        print(f"批处理完成：单表成功 {self.succeeded}，失败 {self.failed}；"
              f"数据构建成功 {data_construction['succeeded']} 篇，失败 {data_construction['failed']} 篇；"
              f"正文完整完成 {text_sequence_summary['succeeded']} 篇，"
              f"部分完成（有备注）{text_sequence_summary['partial']} 篇，"
              f"失败 {text_sequence_summary['failed']} 篇，跳过 {text_sequence_summary['skipped']} 篇；"
              f"耗时 {summary['wall_seconds']} 秒。", flush=True)
        print(f"各阶段请求耗时累计（含重试，并发时不可相加为总耗时）：{self.metrics}", flush=True)
        if timing_report["phase_ranking"]:
            slowest = timing_report["phase_ranking"][0]
            print(f"实际耗时最多的阶段：{slowest['stage']}，{slowest['seconds']:.3f} 秒；详情：{timing_path}", flush=True)
        if timing_report["stage_ranking"]:
            slowest = timing_report["stage_ranking"][0]
            print(f"请求/操作覆盖时间最多：{slowest['stage']}，{slowest['active_wall_seconds']:.3f} 秒", flush=True)
        return summary

    async def _run_documents(self, tables):
        """Raw OCR -> one document extraction -> existing template/export path."""
        from document_extraction import extract_document
        from schema_pipeline import load_sequence_catalog
        documents, errors, files = {}, {}, {}
        groups = group_tables_by_document(tables)
        if not groups:
            first = json.loads(Path(self.config.text_pages_jsonl).read_text().splitlines()[0])
            groups[first["document_id"]] = []
        root = Path(self.config.text_context_dir).resolve() if self.config.text_context_dir else self.image_dir
        for identity, document_tables in groups.items():
            output = self.output_dir / identity
            directory = root if root.name == identity else root / identity
            output.mkdir(parents=True, exist_ok=True)
            try:
                if isinstance(self.llm, codexllm):
                    from document_extraction import response_schema
                    self.llm.output_schema = response_schema()
                catalog = load_sequence_catalog(directory, identity)
                result = await extract_document(identity, document_tables, self.config.text_pages_jsonl,
                    output, self.llm, timing=self.timing, repair_rounds=self.config.document_repair_rounds, catalog=catalog,
                    resume_payload=self.config.document_resume)
                if isinstance(self.llm, codexllm):
                    self.llm.output_schema = None
                completed = await build_document_dataset(identity, document_tables, directory, output,
                    llm=self.llm, complete_text=self.config.text_sequences,
                    initial_template=result["template"], text_pages_jsonl=self.config.text_pages_jsonl,
                    page_evidence=self.config.page_evidence and self.config.document_sequence_mapping, page_ocr_max_pages=0,
                    sequence_mapping_max_calls=self.config.sequence_mapping_max_calls,
                    run_tst=False, timing=self.timing,
                    sequence_agent=self.config.document_template_agent and self.config.template_completion,
                    template_agent_max_calls=self.config.template_agent_max_calls,
                    template_agent_context_chars=self.config.template_agent_context_chars)
                dataset = completed["dataset"]
                if result["report"]["status"] == "partial":
                    dataset["status"] = "partial"
                dataset.update(extraction_mode="document", coverage=result["report"]["coverage"],
                               limitations=result["report"]["limitations"], document_report_file="document_report.json")
                _write_json(output / "dataset.json", dataset)
                _write_json(output / "data_construction.json", {"records": completed["records"]})
                documents[identity] = {"status": dataset["status"], "records": len(completed["records"]),
                                       "experiments": len(dataset.get("experiments", [])),
                                       "remaining_missing_fields": len(completed["template"]["missing"])}
                files[identity] = str(output / "data_construction.json")
                self.schema_results[identity] = completed
            except Exception as exc:
                errors[identity] = f"{type(exc).__name__}: {exc}"
                _write_json(output / "dataset.json", {"document_id": identity, "status": "failed", "records": [], "error": errors[identity]})
                print(f"文档提取失败 {identity}: {errors[identity]}", flush=True)
        report = self.timing.report()
        report["run_mode"] = "document_pipeline"
        path = write_timing_report(self.output_dir, report)
        partial = sum(d["status"] == "partial" for d in documents.values())
        summary = {"status": "failed" if errors or self.failed else "partial" if partial else "complete",
                   "wall_seconds": perf_counter() - self.started, "total": len(self.images),
                   "succeeded": self.succeeded, "failed": self.failed, "config": asdict(self.config),
                   "documents": documents, "stages": self.metrics, "errors": errors,
                   "data_construction": {"succeeded": len(documents), "failed": len(errors),
                                          "records": sum(d["records"] for d in documents.values()), "files": files, "errors": errors},
                   "text_sequences": {"succeeded": len(documents) - partial, "partial": partial, "failed": len(errors),
                                      "skipped": 0, "warnings": {}, "errors": errors},
                   "token_usage": report["token_usage"], "timing_file": str(path)}
        _write_json(self.output_dir / "_batch_summary.json", summary)
        for identity in documents:
            _write_json(self.output_dir / identity / "_timings.json", self.timing.report(item_prefix=identity))
        print(f"文档模式完成：{summary['status']}，{summary['wall_seconds']:.3f} 秒，{summary['data_construction']['records']} 条记录", flush=True)
        return summary


async def run_pipeline(image_dir, output_dir, config=None):
    return await BatchPipeline(image_dir, output_dir, config or PipelineConfig()).run()
