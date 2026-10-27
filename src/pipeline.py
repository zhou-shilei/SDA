"""Batch table OCR, document-level extraction, sequence verification and dataset export."""

import asyncio
import copy
import hashlib
import json
import math
import multiprocessing
from itertools import islice
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path
from time import perf_counter

from agent import codexllm
from document.table_ocr import (OCR_BACKENDS, configure_ocr_worker, extract_table_evidence,
                               extract_table_evidence_batch, structure_table_evidence)
from document.table_continuation import load_crop_metadata, verified_context
from units.document_fields import document_folder, get_document_id, group_tables_by_document
from units.timing import TimingRecorder, write_timing_report
from schema_pipeline import build_document_dataset


@dataclass(frozen=True)
class PipelineConfig:
    extraction_mode: str = "document"
    document_repair_rounds: int = 2
    document_template_agent: bool = True
    template_agent_max_calls: int = 12
    template_agent_context_chars: int = 24000
    document_resume: str | None = None
    ocr_workers: int = 6
    ocr_batch_size: int = 1
    ocr_device: str = "gpu:0"
    ocr_cpu_threads: int = 4
    ocr_backend: str = "vl"
    reuse_table_ocr_from: str | None = None
    text_context_dir: str | None = None
    text_pages_jsonl: str | None = None
    retries: int = 2
    retry_delay: float = 1.0
    limit: int | None = None

    def __post_init__(self):
        if self.extraction_mode != "document":
            raise ValueError("仅支持文档级提取：extraction_mode=document")
        if not self.text_pages_jsonl:
            raise ValueError("文档模式需要 --text-pages-jsonl 提供完整 OCR")
        if type(self.document_repair_rounds) is not int or self.document_repair_rounds < 0:
            raise ValueError("document_repair_rounds 必须为非负整数")
        if (type(self.template_agent_max_calls) is not int or self.template_agent_max_calls < 0
                or type(self.template_agent_context_chars) is not int or self.template_agent_context_chars < 4000):
            raise ValueError("TemplateAgent 调用预算须非负，上下文至少 4000 字符")
        for name in ("ocr_workers", "ocr_batch_size", "ocr_cpu_threads"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                raise ValueError(f"{name} 必须为正整数")
        if type(self.retries) is not int or self.retries < 0 or self.retry_delay < 0 or not math.isfinite(self.retry_delay):
            raise ValueError("重试次数和间隔必须为非负有限数")
        if self.limit is not None and (type(self.limit) is not int or self.limit < 1):
            raise ValueError("limit 必须为正整数")
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
        self.llm = llm or codexllm(timeout=600,
            artifact_dir=self.output_dir / "requests" / ("resume_" + str(int(perf_counter())))
            if config.document_resume else self.output_dir / "requests")
        if llm is None:
            from document.document_extraction import response_schema
            self.llm.output_schema = response_schema()
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
            if output.name in {"data_construction.json", "_batch_summary.json"}:
                self.outputs[image] = output.with_suffix(".table.json")
        # A requested reuse run is all-or-nothing. Validate the entire batch
        # before workers can call a model or initialize a GPU process.
        self.reused_ocr = self._load_reusable_ocr() if config.reuse_table_ocr_from is not None else {}
        self.metrics = {"ocr": {"requests": 0, "failed_requests": 0, "seconds": 0.0,
            "tables_succeeded": 0, "tables_failed": 0, "cache_hits": 0}}
        self.succeeded = 0
        self.failed = 0
        self.started = 0.0
        self.timing = TimingRecorder()
        if self.config.document_resume:
            previous = self.output_dir / "_token_usage.json"
            if previous.is_file():
                from units.token_usage import TokenUsageRecorder
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
                if evidence.get("backend") == "vl":
                    from document.sequence_ocr import VERSION, REVISION, sequence_candidates
                    from document.pdf2doc import parse_grid
                    if (sequence_candidates(parse_grid(text, evidence.get("table_id", relative)))
                            and (evidence.get("sequence_ocr", {}).get("version"),
                                 evidence.get("sequence_ocr", {}).get("revision")) != (VERSION, REVISION)):
                        raise ValueError(f"旧表格 OCR 缓存缺少 PP-OCRv6 序列复识别，需重新运行 Table OCR：{source}")
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
        state["extraction_method"] = "document_pending"
        self._save(state)

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
        finally:
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
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
                tasks = [asyncio.create_task(self._produce_ocr(iter(self.images), pool))]
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
        # 等待全部 OCR worker 完成，再将同文档的表格和全文一起提取。
        with self.timing.measure("load_table_results", kind="phase"):
            tables = [
                {**json.loads(path.read_text(encoding="utf-8")), "result_file": str(path)}
                for path in self.outputs.values()
            ]
        return await self._run_documents(tables)

    async def _run_documents(self, tables):
        """Raw OCR -> one document extraction -> existing template/export path."""
        from document.document_extraction import extract_document
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
                    from document.document_extraction import response_schema
                    self.llm.output_schema = response_schema()
                catalog = load_sequence_catalog(directory, identity)
                result = await extract_document(identity, document_tables, self.config.text_pages_jsonl,
                    output, self.llm, timing=self.timing, repair_rounds=self.config.document_repair_rounds, catalog=catalog,
                    resume_payload=self.config.document_resume)
                if isinstance(self.llm, codexllm):
                    self.llm.output_schema = None
                completed = await build_document_dataset(identity, document_tables, directory, output,
                    llm=self.llm, complete_text=self.config.document_template_agent,
                    initial_template=result["template"], text_pages_jsonl=self.config.text_pages_jsonl,
                    timing=self.timing, sequence_agent=self.config.document_template_agent,
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
