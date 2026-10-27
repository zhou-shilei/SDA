"""Create a separate strategy run over an immutable copy of the same OCR."""

import hashlib
import os
from pathlib import Path
import shutil
from time import perf_counter
import uuid

from studio.sda_data import now, read_json


def create_comparison(server, identity, engine):
    from studio.server import extraction_engine, IDENTITY

    engine = extraction_engine(engine)
    if not isinstance(identity, str):
        raise ValueError("请选择源专利")
    workspace = server.workspace
    with workspace.lock:
        if server.closing.is_set():
            raise ValueError("服务正在停止，请稍后重试")
        if identity not in workspace.locations():
            raise ValueError("专利不存在")
        source_job = workspace.jobs().get(identity, {})
        bundle = workspace.bundle(identity)
        structured_value = source_job.get("structured_dir") or bundle["run"].get("structured_dir")
        if not structured_value:
            raise ValueError("缺少已完成的结构化 OCR，请先解析 PDF")
        structured = Path(structured_value).resolve()
        if not structured.is_relative_to(workspace.root):
            raise ValueError("OCR 路径不在当前工作区")
        document = read_json(structured / "document.json", {})
        evidence_id = document.get("document_id")
        if not isinstance(evidence_id, str) or not IDENTITY.fullmatch(evidence_id):
            raise ValueError("结构化 OCR 的专利编号无效")
        if evidence_id != (source_job.get("evidence_document_id") or identity):
            raise ValueError("结构化 OCR 与源专利不匹配")
        page_summary = read_json(structured / "page_ocr/summary.json", {})
        if not evidence_id or page_summary.get("status") != "complete" or not page_summary.get("full_document_ocr_complete"):
            raise ValueError("全文 OCR 尚未完成，请等待 OCR 完成后运行策略对比")
        if not (structured / "table_ocr/tables.jsonl").is_file() or not bundle["pdf"]:
            raise ValueError("缺少表格 OCR 或源 PDF，无法创建对比任务")
        for other_id, other in workspace.jobs().items():
            if (other.get("comparison_source_id") == identity and other.get("comparison_source_run_id") == source_job.get("run_id")
                    and other.get("extraction_engine") == engine and other.get("status") in {"queued", "processing"}):
                return next(p for p in workspace.patents() if p["document_id"] == other_id)
        comparison_id = f"{identity[:53]}-{engine}-{uuid.uuid4().hex[:8]}"
        parent = workspace.state / "patents" / comparison_id
        run = parent / "runs" / uuid.uuid4().hex
        snapshot = run / "structured"
        source_pdf = parent / "source.pdf"
        try:
            parent.mkdir(parents=True, exist_ok=False)
            original_pdf = workspace.local_path(workspace.state / "patents" / identity / "source.pdf") or bundle["pdf"]
            shutil.copyfile(original_pdf, source_pdf)
            shutil.copytree(structured, snapshot)
        except BaseException:
            shutil.rmtree(parent, ignore_errors=True)
            raise
        workspace.update_job(comparison_id, filename=source_job.get("filename") or f"{identity}.pdf",
            status="queued", stage="queued", progress=0, operation="compare", extraction_engine=engine,
            comparison_source_id=identity, comparison_source_run_id=source_job.get("run_id"),
            evidence_document_id=evidence_id, source_pdf_sha256=hashlib.sha256(source_pdf.read_bytes()).hexdigest(),
            input_dir=str(snapshot / "layout"), text_context_dir=str(snapshot / "layout"),
            structured_dir=str(snapshot), result_dir=str(run / "results" / evidence_id),
            run_id=run.name, started_at=now(), created_at=now(), finished_at=None,
            worker_pid=None, runner_pid=os.getpid(), error=None, log=str(run / "pipeline.log"),
            stage_current=None, stage_total=None, ocr_branches={"text": "completed", "table": "completed"})
        server.workers.submit(run_comparison, server, comparison_id, run)
    return next(p for p in workspace.patents() if p["document_id"] == comparison_id)


def run_comparison(server, identity, run):
    from studio.server import EXTRACTION_ENGINES
    from dataagent2.provider import write_json

    workspace = server.workspace
    started, started_at = perf_counter(), now()
    job = workspace.jobs()[identity]
    evidence_id = job["evidence_document_id"]
    structured = Path(job["structured_dir"])
    result_dir = Path(job["result_dir"])
    mode = EXTRACTION_ENGINES[job["extraction_engine"]]
    summary, error = {}, None
    workspace.update_job(identity, status="processing", stage="extraction", progress=65)
    try:
        with (run / "pipeline.log").open("w", encoding="utf-8") as log:
            log.write(f"Agent 策略对比：{job['comparison_source_id']} → {job['extraction_engine']}；复用同一批 OCR，不重新识别 PDF。\n")
            command = [server.python, str(workspace.root / "src/graph.py"),
                "--image-dir", str(structured / "layout"), "--output-dir", str(run / "results"),
                "--text-context-dir", str(structured / "layout"),
                "--text-pages-jsonl", str(structured / "page_ocr/pages.jsonl"),
                "--reuse-table-ocr-from", str(structured / "table_ocr/tables.jsonl"),
                "--structured-dir", str(structured), "--document-id", evidence_id, "--extraction-mode", mode]
            code = server.command(identity, command, log)
            summary = read_json(run / "results/_batch_summary.json", {})
            if code or not summary or summary.get("status") == "failed":
                reason = summary.get("errors", {}).get(evidence_id) or f"退出码 {code}，缺少有效运行汇总"
                raise RuntimeError(f"策略对比未完成：{reason}；已保存的部分结果可以审阅")
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        with (run / "pipeline.log").open("a", encoding="utf-8") as log:
            log.write(error + "\n")
    dataset = read_json(result_dir / "dataset.json", {})
    preprocessing = read_json(structured / "summary.json", {})
    if preprocessing.get("status") == "partial" and dataset.get("status") == "complete":
        dataset["status"] = "partial"
        write_json(result_dir / "dataset.json", dataset)
    report = {"document_id": identity, "evidence_document_id": evidence_id, "run_kind": "ocr_reanalysis",
        "comparison_source_id": job["comparison_source_id"], "comparison_source_run_id": job["comparison_source_run_id"],
        "extraction_mode": mode, "extraction_engine": job["extraction_engine"],
        "status": "failed" if error else dataset.get("status", summary.get("status", "partial")),
        "preprocessing_status": preprocessing.get("status"), "source_pdf_sha256": job["source_pdf_sha256"],
        "started_at_utc": started_at, "finished_at_utc": now(), "wall_seconds": perf_counter() - started,
        "token_usage": summary.get("token_usage") or read_json(result_dir / "_token_usage.json", {}),
        "records": len(dataset.get("records", [])), "experiments": len(dataset.get("experiments", [])),
        "relations": len(dataset.get("relations", [])), "total_pages": preprocessing.get("total_pages"),
        "table_count": preprocessing.get("tables"), "structured_dir": str(structured),
        "structured_document": str(structured / "document.json"), "result_dir": str(result_dir),
        "run_dir": str(run), "phases": [], "error": error,
        "cache_policy": "策略对比复用同一批原始 OCR；实际请求用量仅包含本次 Agent 阶段，不计历史 OCR 或历史模型用量。"}
    write_json(run / "run_summary.json", report)
    write_json(result_dir / "run_summary.json", report)
    workspace.update_job(identity, status="failed" if error else "completed", stage="failed" if error else "completed",
        progress=workspace.jobs()[identity].get("progress", 65) if error else 100,
        semantic_status=dataset.get("status"), preprocessing_status=preprocessing.get("status"),
        worker_pid=None, runner_pid=None, error=error, finished_at=now())
