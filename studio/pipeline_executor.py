"""Run one Studio task without owning the HTTP service or its lifetime."""

import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
from time import perf_counter

from studio.sda_data import now, read_json
from studio.document_input import prepare_document_input
from studio.ocr_settings import table_worker_settings


class PipelineExecutor:
    def __init__(self, workspace, *, python=None, ocr_device="gpu:0", ocr_backend="vl",
                 text_backend="paddle", text_workers=1, table_workers=None, ocr_parallel=True):
        self.workspace = workspace
        self.python = python or sys.executable
        self.ocr_device, self.ocr_backend = ocr_device, ocr_backend
        self.text_backend, self.text_workers = text_backend, text_workers
        self.table_workers, self.ocr_parallel = table_workers, ocr_parallel
        self.processes = {}
        self.process_lock = threading.Lock()
        self.closing = threading.Event()

    def table_ocr_source(self, identity, result_dir):
        """Find persisted OCR even when extraction failed before table export."""
        for path in result_dir.glob("*.json"):
            row = read_json(path, {})
            if isinstance(row, dict) and row.get("table_id") and isinstance(row.get("table_text"), str) and row["table_text"].strip():
                return result_dir
        checkpoint = read_json(result_dir / "dataagent_checkpoint.json", {}) or read_json(result_dir / "dataagent2_checkpoint.json", {})
        job = self.workspace.jobs().get(identity, {})
        candidates = [checkpoint.get("inputs", {}).get("table_ocr")]
        if job.get("structured_dir"):
            candidates.append(str(Path(job["structured_dir"]) / "table_ocr/tables.jsonl"))
        for value in candidates:
            if value:
                path = Path(value).resolve()
                if path.is_relative_to(self.workspace.root) and (path.is_file() or path.is_dir() and any(path.glob("*.json"))):
                    return path
        raise ValueError("没有可复用的表格 OCR，请先解析 PDF")

    def run_pipeline(self, identity, run, operation="pipeline"):
        from studio.server import EXTRACTION_ENGINES, extraction_engine

        workspace = self.workspace
        started = perf_counter()
        started_at_utc = now()
        try:
            run.mkdir(parents=True, exist_ok=True)
            source = workspace.state / "patents" / identity / "source.pdf"
            job = workspace.jobs()[identity]
            engine = extraction_engine(job.get("extraction_engine", "dataagent1"))
            mode = EXTRACTION_ENGINES[engine]
            evidence_id = job.get("evidence_document_id") or identity
            inputs = Path(job["input_dir"])
            context = Path(job["text_context_dir"])
            previous_inputs = Path(job["previous_input_dir"])
            workspace.update_job(identity, status="processing", stage="layout" if operation == "pipeline" else "extraction", progress=2)
            with (run / "pipeline.log").open("w", encoding="utf-8") as log:
                if operation == "pipeline":
                    work = run / "document"
                    result_dir = work / "results" / identity
                    input_dir = work / "data" / identity
                    ocr_settings = table_worker_settings(self.ocr_device, self.ocr_backend, self.table_workers,
                        page_workers=self.text_workers if self.ocr_parallel else 1)
                    log.write("table-ocr-configuration " + json.dumps(ocr_settings, ensure_ascii=False) + "\n")
                    log.flush()
                    workspace.update_job(identity, input_dir=str(input_dir), text_context_dir=str(input_dir),
                                         result_dir=str(result_dir), structured_dir=str(work / "structured"),
                                         ocr_branches={}, evidence_document_id=identity, ocr_configuration=ocr_settings)
                    command = [self.python, str(workspace.root / "src/document/run_document_pdf.py"), str(source),
                               "--document-id", identity, "--run-dir", str(work), "--device", self.ocr_device,
                               "--text-backend", self.text_backend, "--table-backend", self.ocr_backend,
                               "--text-workers", str(self.text_workers), "--table-ocr-workers", str(ocr_settings["workers"]),
                               "--ocr-execution", "parallel" if self.ocr_parallel else "serial"]
                    command.extend(["--extraction-mode", mode])
                    catalog = previous_inputs / "wipo/sequences.json"
                    if catalog.is_file():
                        command.extend(["--catalog", str(catalog)])
                    code = self.command(identity, command, log)
                    report = read_json(work / "run_summary.json", {})
                    if code or report.get("status") not in {"complete", "partial"}:
                        extraction_summary = read_json(work / "results/_batch_summary.json", {})
                        detail = extraction_summary.get("failure_details", {}).get(identity)
                        if detail:
                            workspace.update_job(identity, failure_details=detail)
                        if self.closing.is_set():
                            reason = "服务停止或重启，解析任务已中断；已有产物保留，可重新解析"
                        elif code < 0:
                            reason = f"解析进程被信号 {signal.Signals(-code).name} 终止；已有产物保留"
                        else:
                            reason = (extraction_summary.get("errors", {}).get(identity) or report.get("error")
                                      or f"解析进程退出码 {code}，未生成有效运行汇总")
                        raise RuntimeError(f"文档提取失败：{reason}；日志：{run / 'pipeline.log'}")
                    workspace.update_job(identity, status="completed", stage="completed", progress=100,
                                         semantic_status=report.get("status"), preprocessing_status=report.get("preprocessing_status"),
                                         finished_at=now(), worker_pid=None)
                    return
                log.write("复用已保存的原始表格 OCR，重新抽取；不运行版面检测与表格 OCR。\n")
                previous_bundle = workspace._build(identity, Path(job["previous_result_dir"]), previous_inputs)
                evidence_pdf = previous_bundle["pdf"] or source
                manifest = read_json(previous_inputs / f"{evidence_id}_manifest.json", {})
                prepare_document_input(context, evidence_id,
                                       evidence_pdf if evidence_pdf.is_file() else None,
                                       existing=previous_inputs, extract_text=False)
                workspace.update_job(identity, stage="ocr" if operation == "pipeline" else "extraction", progress=30)
                extraction = [self.python, str(workspace.root / "src/graph.py"),
                              "--image-dir", str(inputs), "--output-dir", str(run / "results"),
                              "--text-context-dir", str(context),
                              "--ocr-device", self.ocr_device, "--ocr-backend", self.ocr_backend]
                if operation == "analyze":
                    previous_result = Path(job["previous_result_dir"])
                    extraction.extend(["--reuse-table-ocr-from", str(self.table_ocr_source(identity, previous_result))])
                    checkpoint = read_json(previous_result / "dataagent_checkpoint.json", {}) or read_json(previous_result / "dataagent2_checkpoint.json", {})
                    structured_dir = job.get("structured_dir") or previous_bundle.get("run", {}).get("structured_dir")
                    candidates = [checkpoint.get("inputs", {}).get("text_pages_jsonl"),
                                  str(Path(structured_dir) / "page_ocr/pages.jsonl") if structured_dir else None,
                                  str(previous_result.parent.parent / "text_ocr/pages.jsonl")]
                    pages = next((path for value in candidates if value and (path := workspace.local_path(value))), None)
                    if pages is None:
                        raise RuntimeError("缺少已保存的全文 OCR；请先运行完整 PDF 解析，再重新提取")
                    extraction.extend(["--extraction-mode", mode, "--text-pages-jsonl", str(pages),
                                       "--document-id", evidence_id])
                    if structured_dir:
                        extraction.extend(["--structured-dir", str(structured_dir)])
                code = self.command(identity, extraction, log)
                summary = read_json(run / "results/_batch_summary.json", {})
                if code or not summary or summary.get("status") == "failed":
                    detail = summary.get("failure_details", {}).get(evidence_id)
                    if detail:
                        workspace.update_job(identity, failure_details=detail)
                    reason = summary.get("errors", {}).get(evidence_id) or summary.get("errors", {}).get(identity)
                    if self.closing.is_set():
                        reason = "服务停止或重启，提取任务已中断"
                    elif code < 0:
                        reason = f"提取进程被信号 {signal.Signals(-code).name} 终止"
                    reason = reason or "未生成有效提取汇总"
                    raise RuntimeError(f"SDA 提取未全部成功（退出码 {code}）：{reason}；"
                                       f"已保存的产物可继续审阅；日志：{run / 'pipeline.log'}")
                result_dir = run / "results" / evidence_id
                workspace.update_job(identity, result_dir=str(result_dir))
                dataset = read_json(result_dir / "dataset.json", {})
                structured_dir = job.get("structured_dir") or previous_bundle.get("run", {}).get("structured_dir")
                structured_summary = read_json(Path(structured_dir) / "summary.json", {}) if structured_dir else {}
                preprocessing_status = previous_bundle.get("run", {}).get("preprocessing_status") or structured_summary.get("status")
                if preprocessing_status == "partial" and dataset.get("status") == "complete":
                    dataset["status"] = summary["status"] = "partial"
                    dataset["preprocessing"] = {"status": "partial", "document_file": str(Path(structured_dir) / "document.json") if structured_dir else None}
                    (result_dir / "dataset.json").write_text(json.dumps(dataset, ensure_ascii=False, indent=2), encoding="utf-8")
                report = {"document_id": identity, "run_kind": "ocr_reanalysis", "status": summary["status"],
                          "extraction_mode": mode, "extraction_engine": engine, "started_at_utc": started_at_utc,
                          "preprocessing_status": preprocessing_status,
                          "wall_seconds": perf_counter() - started, "token_usage": summary["token_usage"],
                          "result_dir": str(result_dir), "run_dir": str(run), "finished_at_utc": now(),
                          "total_pages": previous_bundle["pages"]["page_count"],
                          "table_count": structured_summary.get("tables", len(previous_bundle["ocr"])),
                          "cache_policy": "复用表格 OCR 的重新提取；不是完整 PDF 首次解析耗时。",
                          "records": len(dataset.get("records", [])), "phases": [],
                          "experiments": len(dataset.get("experiments", [])), "relations": len(dataset.get("relations", []))}
                if structured_dir:
                    report["structured_dir"] = structured_dir
                    report["structured_document"] = str(Path(structured_dir) / "document.json")
                (result_dir / "run_summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                (run / "run_summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                workspace.update_job(identity, status="completed", stage="completed", progress=100,
                                     preprocessing_status=manifest.get("status"), worker_pid=None,
                                     semantic_status=summary.get("status"), finished_at=now())
        except Exception as exc:
            branches = dict(workspace.jobs().get(identity, {}).get("ocr_branches", {}))
            branches = {branch: "failed" if status == "processing" else status
                        for branch, status in branches.items()}
            workspace.update_job(identity, status="failed", stage="failed", worker_pid=None,
                                 error=str(exc), ocr_branches=branches, finished_at=now())
            with (run / "pipeline.log").open("a", encoding="utf-8") as log:
                log.write(f"\nSDA pipeline failed: {exc}\n")

    def command(self, identity, command, log):
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        with self.process_lock:
            if self.closing.is_set():
                raise RuntimeError("服务停止，解析任务已中断；已有产物保留")
            process = subprocess.Popen(command, cwd=self.workspace.root, env=env,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                       start_new_session=True)
            self.processes[identity] = process
        with process:
            from studio.task_queue import process_token
            self.workspace.update_job(identity, worker_pid=process.pid, worker_pid_token=process_token(process.pid))
            log.write("$ " + json.dumps(command, ensure_ascii=False) + "\n")
            log.flush()
            try:
                for line in process.stdout:
                    log.write(line)
                    log.flush()
                    self.update_progress(identity, line)
                code = process.wait()
                log.write(f"\nSDA process exited: returncode={code}\n")
                log.flush()
                return code
            finally:
                with self.process_lock:
                    self.processes.pop(identity, None)
                self.workspace.update_job(identity, worker_pid=None)

    def update_progress(self, identity, line):
        changes = None
        if line.startswith("sda-dataagent2-progress "):
            event = json.loads(line.removeprefix("sda-dataagent2-progress "))
            current, total = int(event["completed"]), int(event["total"])
            stage, base, span = {"reader": ("reader", 76, 12), "action": ("action", 88, 2), "questions": ("tst", 90, 6),
                                 "answer_action": ("action", 96, 2), "export": ("build", 98, 0)}[event["phase"]]
            changes = {"stage": stage, "stage_current": current, "stage_total": total,
                       "progress": base + round(span * min(current, total) / max(1, total))}
        elif line.startswith("PDF stage="):
            stage = line.strip().split("=", 1)[1]
            progress = {"structured_document": 5, "document_extraction": 65,
                        "layout": 5, "text_ocr": 20, "table_ocr_and_extraction": 40}.get(stage, 50)
            changes = {"stage": {"structured_document": "layout", "document_extraction": "extraction",
                                  "text_ocr": "ocr", "table_ocr_and_extraction": "extraction"}.get(stage, stage), "progress": progress}
        elif line.startswith("pdf2doc stage="):
            stage = line.strip().split("=", 1)[1]
            state, progress = {"orientation_and_layout": ("layout", 5), "recognition": ("ocr", 20),
                               "assembly": ("assembly", 55), "export": ("export", 60)}.get(stage, ("ocr", 20))
            changes = {"stage": state, "progress": progress, "stage_current": None, "stage_total": None}
        elif match := re.match(r"llm-(classification|sequence-progress) (\d+)/(\d+)", line):
            kind, current, total = match.groups()
            current, total = int(current), int(total)
            stage, base, span = ("classify", 65, 7) if kind == "classification" else ("sequence_extract", 76, 12)
            changes = {"stage": stage, "stage_current": current, "stage_total": total,
                       "progress": base + round(span * min(current, total) / max(1, total))}
        elif line.startswith("llm-template-start"):
            changes = {"stage": "template", "progress": 74, "stage_current": None, "stage_total": None}
        elif line.startswith("llm-questions:") or line.startswith("llm-answer:"):
            match = re.search(r"question (\d+)/(\d+)", line)
            changes = {"stage": "tst", "progress": 90,
                       "stage_current": int(match[1]) if match else None,
                       "stage_total": int(match[2]) if match else None}
        elif line.startswith("sda-ocr-progress "):
            event = json.loads(line.removeprefix("sda-ocr-progress "))
            if event.get("branch") in {"text", "table"}:
                branches = dict(self.workspace.jobs().get(identity, {}).get("ocr_branches", {}))
                branches[event["branch"]] = event["status"]
                finished = sum(status in {"completed", "partial", "failed"} for status in branches.values())
                changes = {"stage": "ocr", "ocr_branches": branches, "progress": 20 + finished * 12}
                if event["branch"] == "table" and "total" in event:
                    changes.update(stage_current=event.get("completed", 0), stage_total=event["total"])
        elif line.startswith("sda-progress "):
            event = json.loads(line.removeprefix("sda-progress "))
            if event.get("stage") == "tst":
                changes = {"stage": "tst", "progress": 90, "stage_current": event["request_index"],
                           "stage_total": None, "stage_phase": event.get("phase")}
        elif "orientation-page " in line or "layout-page " in line:
            changes = {"stage": "layout", "progress": 5}
        elif "进入固定 schema" in line:
            changes = {"stage": "template", "progress": 87, "stage_current": None, "stage_total": None}
        elif "进入序列与实验数据拼接" in line or "进入最终数据构建" in line:
            changes = {"stage": "construct", "progress": 96, "stage_current": None, "stage_total": None}
        else:
            match = re.search(r"\[(\d+)/(\d+)\]", line)
            if match:
                current, total = map(int, match.groups())
                changes = {"stage": "extraction", "stage_current": current, "stage_total": total,
                           "progress": 30 + round(55 * current / max(1, total))}
        if changes:
            current = self.workspace.jobs().get(identity, {}).get("progress", 0)
            changes["progress"] = max(current, changes["progress"])
            self.workspace.update_job(identity, **changes)

