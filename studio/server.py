#!/usr/bin/env python3
"""Local SDA service for the migrated DataAgent interface; no old repo imports."""

from __future__ import annotations

import argparse
from email import policy
from email.parser import BytesParser
import hashlib
import gzip
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import re
import shutil
import signal
import sys
import threading
from urllib.parse import parse_qs, unquote, urlsplit
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from studio.sda_data import Workspace, now, read_json, review_payload
from studio.pipeline_executor import PipelineExecutor
from studio.task_queue import TaskQueue, ensure_worker
from studio.review_queue import ReviewQueue, QueueConflict
from units.cache_policy import cache_enabled

MAX_UPLOAD = 300 * 1024 * 1024
IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{2,79}\Z")
STATIC = {"index.html", "styles.css", "app.js", "collection_review.js", "job_eta.js",
          "patent_identity.js", "table_columns.js", "intake_metrics.js", "database_columns.js", "review_queue.js", "processing_queue.js", "run.html"}
EXTRACTION_ENGINES = {"dataagent1": "dataagent", "dataagent2": "dataagent2"}


def extraction_engine(value):
    if not isinstance(value, str) or value not in EXTRACTION_ENGINES:
        raise ValueError("解析引擎必须为 dataagent1 或 dataagent2")
    return value


class StudioServer(PipelineExecutor, ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, workspace, *, python=None, ocr_device="gpu:0", ocr_backend="vl",
                 text_backend="paddle", text_workers=1, table_workers=None, ocr_parallel=True):
        if table_workers is not None and (type(table_workers) is not int or table_workers < 1):
            raise ValueError("table_workers 必须为正整数")
        PipelineExecutor.__init__(self, workspace, python=python, ocr_device=ocr_device,
                                  ocr_backend=ocr_backend, text_backend=text_backend,
                                  text_workers=text_workers, table_workers=table_workers,
                                  ocr_parallel=ocr_parallel)
        self.queue = TaskQueue(workspace)
        self.review_queue = ReviewQueue(workspace.root)
        self.render_lock = threading.Lock()
        ThreadingHTTPServer.__init__(self, address, StudioHandler)

    def submit_task(self, identity):
        return self.queue.submit(identity, config=self.execution_config())

    def execution_config(self):
        return {key: getattr(self, key) for key in (
            "python", "ocr_device", "ocr_backend", "text_backend", "text_workers",
            "table_workers", "ocr_parallel")}

    def enqueue(self, identity, *, operation="pipeline", engine=None):
        workspace = self.workspace
        with workspace.lock:
            if self.closing.is_set():
                raise ValueError("服务正在停止，请稍后重试")
            job = workspace.jobs().get(identity)
            engine = extraction_engine(engine if engine is not None else (job or {}).get("extraction_engine", "dataagent1"))
            location = workspace.locations().get(identity)
            if location is None:
                raise ValueError("专利不存在")
            if job and job.get("status") in {"queued", "processing"}:
                raise ValueError("该专利已有运行中的任务")
            if job and job.get("worker_pid"):
                try:
                    os.kill(int(job["worker_pid"]), 0)
                except ProcessLookupError:
                    pass
                else:
                    raise ValueError("此前的解析进程仍在运行，请等待结束后重试")
            bundle = workspace.bundle(identity)
            previous_results, previous_inputs = location
            if operation == "analyze":
                self.table_ocr_source(identity, previous_results)
            if operation not in {"pipeline", "analyze"}:
                raise ValueError("未知解析操作")
            if operation == "pipeline" and not bundle["pdf"]:
                raise ValueError("缺少源 PDF，无法重新解析")
            parent = workspace.state / "patents" / identity
            parent.mkdir(parents=True, exist_ok=True)
            source = parent / "source.pdf"
            if not source.exists() and bundle["pdf"]:
                shutil.copyfile(bundle["pdf"], source)
            run = parent / "runs" / uuid.uuid4().hex
            input_dir = previous_inputs if operation == "analyze" else run / "data" / identity
            workspace.update_job(identity, status="queued", stage="queued", progress=0, error=None, failure_details=None,
                                 input_dir=str(input_dir), text_context_dir=str(run / "data" / identity),
                                 result_dir=str(run / "results" / identity), operation=operation, extraction_engine=engine,
                                 previous_result_dir=str(previous_results), previous_input_dir=str(previous_inputs),
                                 run_id=run.name, worker_pid=None, finished_at=None,
                                 runner_pid=None, runner_pid_token=None, worker_pid_token=None,
                                 stage_current=None, stage_total=None,
                                 execution_config=self.execution_config(),
                                 queued_at=now(), started_at=None, log=str(run / "pipeline.log"))
            self.submit_task(identity)
        return next(p for p in workspace.patents() if p["document_id"] == identity)

    def compare(self, identity, *, engine):
        from studio.comparison import create_comparison
        return create_comparison(self, identity, engine)

    def server_close(self):
        self.closing.set()
        ThreadingHTTPServer.server_close(self)


class StudioHandler(BaseHTTPRequestHandler):
    server_version = "SDAStudio/1.0"

    @property
    def workspace(self):
        return self.server.workspace

    def send_bytes(self, data, content_type, *, status=200, cache="no-store"):
        if not cache_enabled():
            cache = "no-store"
        compress = (content_type.startswith("application/json") and len(data) >= 1024
                    and any(part.split(";")[0].strip() == "gzip"
                            and not re.search(r";\s*q=0(?:\.0*)?(?:\s|$)", part)
                            for part in self.headers.get("Accept-Encoding", "").split(",")))
        if compress:
            data = gzip.compress(data, compresslevel=1)
        self.send_response(status)
        if content_type.startswith("application/json"):
            self.send_header("Vary", "Accept-Encoding")
        if compress:
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def json(self, value, status=200):
        self.send_bytes(json.dumps(value, ensure_ascii=False).encode(), "application/json; charset=utf-8", status=status)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        try:
            self.get()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except KeyError as exc:
            self.json({"error": str(exc)}, 404)
        except (ValueError, FileNotFoundError) as exc:
            self.json({"error": str(exc)}, 400)
        except Exception as exc:
            self.log_error("%s", exc)
            self.json({"error": str(exc)}, 500)

    def get(self):
        url = urlsplit(self.path)
        path = unquote(url.path)
        query = parse_qs(url.query)
        if path == "/favicon.ico":
            return self.send_bytes(b"", "image/x-icon", status=204)
        if path in {"/", "/studio"}:
            self.send_response(302)
            self.send_header("Location", "studio/" if path == "/" else "studio/")
            self.end_headers()
            return
        if path.startswith("/studio/"):
            name = path.removeprefix("/studio/") or "index.html"
            if name not in STATIC:
                return self.json({"error": "文件不存在"}, 404)
            file = Path(__file__).parent / name
            return self.send_bytes(file.read_bytes(), mimetypes.guess_type(file)[0] or "application/octet-stream")
        if path == "/api/patents":
            queue = self.server.queue.snapshot()
            return self.json({"patents": self.workspace.patents(),
                              "queue": {key: value for key, value in queue.items() if key != "positions"}})
        if path == "/api/queue":
            return self.json(self.server.queue.snapshot())
        if path == "/api/reviews/queue":
            with self.workspace.lock:
                tasks = self.server.review_queue.list(self.workspace.patents(), query.get("document_id", [None])[0])
                processing_queue = self.server.queue.snapshot()
            return self.json({"tasks": tasks, "processing_queue": processing_queue})
        if path == "/api/dataset/folders":
            return self.json({"folders": self.workspace.folders()})
        if path == "/api/dataset/records":
            return self.json({"records": self.workspace.published(query.get("dataset_id", [None])[0]),
                              "folders": self.workspace.folders()})
        if path == "/api/reviews/human":
            return self.json({"reviews": self.workspace.reviews()})
        if path == "/api/health":
            return self.json({"status": "ok", "service": "SDA Studio", "extraction_engines": list(EXTRACTION_ENGINES),
                              "agent_comparison": True, "persistent_queue": True, "team_review_queue": True})
        match = re.fullmatch(r"/api/patents/([^/]+)/(.+)", path)
        if not match:
            return self.json({"error": "接口不存在"}, 404)
        identity, artifact = match.groups()
        page = re.fullmatch(r"pages/(\d{4})\.png", artifact)
        if page:
            return self.render_page(self.workspace.page_bundle(identity), int(page[1]))
        bundle = self.workspace.bundle(identity)
        if artifact == "review.json":
            return self.json(review_payload(bundle))
        structured_names = {"structured.json": "json", "structured.md": "markdown",
                            "structured.txt": "text", "structured-summary.json": "summary"}
        if artifact in structured_names:
            file = bundle.get("structured_files", {}).get(structured_names[artifact])
            if not file:
                return self.json({"error": "结构化文档尚未生成"}, 404)
            return self.send_bytes(file.read_bytes(), "application/json; charset=utf-8" if artifact.endswith(".json")
                                   else "text/plain; charset=utf-8")
        if artifact.endswith(".json") and artifact[:-5] in {
                "records", "contexts", "candidates", "classifications", "ocr", "template", "runtime", "pages", "missing", "tst", "dataset",
                "run", "coverage", "document_report"}:
            return self.json(bundle[artifact[:-5]])
        if artifact == "log":
            log = self.workspace.local_path(self.workspace.jobs().get(identity, {}).get("log"))
            return self.send_bytes(log.read_bytes()[-200000:] if log else b"", "text/plain; charset=utf-8")
        crop = re.fullmatch(r"crops/(\d+)\.png", artifact)
        if crop and crop[1] in bundle["crops"]:
            return self.send_bytes(bundle["crops"][crop[1]].read_bytes(), "image/png", cache="private, max-age=300")
        return self.json({"error": "文件不存在"}, 404)

    def render_page(self, bundle, number):
        if not bundle["pdf"] or not 1 <= number <= bundle["pages"]["page_count"]:
            return self.json({"error": "页码不存在"}, 404)
        pdf = bundle["pdf"]
        if not cache_enabled():
            import pymupdf
            with self.server.render_lock, pymupdf.open(pdf) as document:
                page = document[number - 1]
                data = page.get_pixmap(matrix=pymupdf.Matrix(1280 / page.rect.width, 1280 / page.rect.width), alpha=False).tobytes("png")
            return self.send_bytes(data, "image/png")
        identity = hashlib.sha256(f"{pdf}:{pdf.stat().st_mtime_ns}:{pdf.stat().st_size}:1280".encode()).hexdigest()[:24]
        path = self.workspace.state / "pages" / identity / f"{number:04d}.png"
        with self.server.render_lock:
            if not path.exists():
                import pymupdf
                path.parent.mkdir(parents=True, exist_ok=True)
                with pymupdf.open(pdf) as document:
                    page = document[number - 1]
                    page.get_pixmap(matrix=pymupdf.Matrix(1280 / page.rect.width, 1280 / page.rect.width), alpha=False).save(path)
        self.send_bytes(path.read_bytes(), "image/png", cache="private, max-age=300")

    def do_POST(self):
        try:
            self.post()
        except QueueConflict as exc:
            self.json({"error": str(exc)}, 409)
        except KeyError as exc:
            self.json({"error": str(exc)}, 404)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self.json({"error": str(exc)}, 400)
        except Exception as exc:
            self.log_error("%s", exc)
            self.json({"error": str(exc)}, 500)

    def post(self):
        # Writes originate in this local interface; do not accept cross-origin form submissions.
        origin = self.headers.get("Origin")
        # code-server may rewrite Host while preserving the browser's Origin.
        forwarded_host = self.headers.get("X-Forwarded-Host", "").split(",")[0].strip()
        if (origin and urlsplit(origin).netloc not in {self.headers.get("Host"), forwarded_host}
                and self.headers.get("Sec-Fetch-Site") != "same-origin"):
            return self.json({"error": "请求来源不匹配"}, 403)
        path = urlsplit(self.path).path
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > (MAX_UPLOAD + 65536 if path == "/api/patents/import" else 2 * 1024 * 1024):
            return self.json({"error": "请求大小无效"}, 413)
        content_type = self.headers.get("Content-Type", "")
        body = self.rfile.read(length)
        if path == "/api/patents/import":
            return self.import_pdf(content_type, body)
        if not content_type.startswith("application/json"):
            raise ValueError("需要 JSON 请求")
        payload = json.loads(body)
        if not isinstance(payload, dict):
            raise ValueError("请求必须是 JSON 对象")
        if path == "/api/reviews/queue":
            with self.workspace.lock:
                task = self.server.review_queue.act(self.workspace.patents(), payload)
            return self.json({"task": task})
        if path == "/api/dataset/folders":
            folder = self.workspace.create_folder(payload.get("name"))
            return self.json({"folder": folder, "folders": self.workspace.folders()}, 201)
        if path == "/api/reviews/human":
            with self.workspace.lock:
                self.server.review_queue.authorize(payload.get("document_id"), payload, require_claim=True)
                return self.json({"review": self.workspace.save_review(payload)})
        if path == "/api/patents/publish-records":
            with self.workspace.lock:
                self.server.review_queue.authorize(payload.get("document_id"), payload)
                return self.json(self.workspace.publish(payload.get("document_id"), payload.get("record_ids"), payload.get("dataset_id", "SDA")))
        if path == "/api/patents/retry":
            with self.workspace.lock:
                self.server.review_queue.authorize(payload.get("document_id"), payload)
                return self.json({"patent": self.server.enqueue(payload.get("document_id"), engine=payload.get("extraction_engine"))}, 202)
        if path == "/api/patents/compare":
            return self.json({"patent": self.server.compare(payload.get("document_id"), engine=payload.get("extraction_engine"))}, 202)
        if path == "/api/patents/analyze":
            with self.workspace.lock:
                self.server.review_queue.authorize(payload.get("document_id"), payload)
                return self.json({"patent": self.server.enqueue(payload.get("document_id"), operation="analyze", engine=payload.get("extraction_engine"))}, 202)
        if path == "/api/patents/check-text":
            identity = payload.get("document_id")
            self.workspace.bundle(identity)
            return self.json({"patent_text": self.workspace.patent_text(identity)})
        if path == "/api/patents/delete":
            return self.delete_patents(payload.get("document_ids"))
        return self.json({"error": "此操作尚未接入 SDA"}, 501)

    def import_pdf(self, content_type, body):
        if not content_type.startswith("multipart/form-data"):
            raise ValueError("需要 PDF 上传表单")
        message = BytesParser(policy=policy.default).parsebytes(
            f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode() + body)
        parts = {part.get_param("name", header="content-disposition"): part for part in message.iter_parts()}
        engine = extraction_engine(parts["extraction_engine"].get_content().strip() if "extraction_engine" in parts else "dataagent2")
        identity = parts["document_id"].get_content().strip() if "document_id" in parts else ""
        if not IDENTITY.fullmatch(identity) or identity in {".", ".."}:
            raise ValueError("专利编号无效")
        pdf_part = parts.get("pdf")
        if pdf_part is None:
            raise ValueError("请选择 PDF")
        data = pdf_part.get_payload(decode=True)
        if not data or len(data) > MAX_UPLOAD or b"%PDF-" not in data[:1024]:
            raise ValueError("PDF 文件无效或超过 300 MB")
        import pymupdf
        with pymupdf.open(stream=data, filetype="pdf") as document:
            if document.needs_pass or not len(document):
                raise ValueError("PDF 为空或需要密码")
        # Browser FormData text fields usually omit a charset. MIME's ASCII
        # default would turn Chinese submitter names into replacement characters.
        submitter_part = parts.get("submitted_by")
        submitted_by = (submitter_part.get_payload(decode=True).decode(
            submitter_part.get_content_charset() or "utf-8").strip()[:40] if submitter_part else "")
        with self.workspace.lock:
            if identity in self.workspace.locations():
                raise ValueError("专利已存在")
            parent = self.workspace.state / "patents" / identity
            parent.mkdir(parents=True, exist_ok=False)
            (parent / "source.pdf").write_bytes(data)
            self.workspace.update_job(identity, filename=Path(pdf_part.get_filename() or f"{identity}.pdf").name,
                                      submitted_by=submitted_by,
                                      status="uploaded", created_at=now())
            patent = self.server.enqueue(identity, engine=engine)
        return self.json({"patent": patent}, 202)

    def delete_patents(self, identities):
        if not isinstance(identities, list) or not identities or len(identities) > 100 or any(not isinstance(x, str) for x in identities):
            raise ValueError("请选择专利")
        workspace = self.workspace
        with workspace.lock:
            jobs = workspace.jobs()
            identities = list(dict.fromkeys(identities))
            for identity in identities:
                if self.workspace.is_builtin(identity) or identity not in jobs or jobs[identity].get("status") in {"queued", "processing"}:
                    raise ValueError("示例专利或处理中的专利不能删除")
            with workspace.connection() as db:
                for identity in identities:
                    if db.execute("SELECT 1 FROM review_tasks WHERE document_id=? AND owner != ''", (identity,)).fetchone():
                        raise QueueConflict("文件正在标注，请先退回队列再删除")
            trash = workspace.state / "trash" / uuid.uuid4().hex
            trash.mkdir(parents=True)
            moved = []
            try:
                with workspace.connection() as db:
                    archive = {"jobs": {identity: jobs[identity] for identity in identities}, "publications": [], "reviews": [], "review_tasks": [], "review_task_events": []}
                    for identity in identities:
                        for table in ("review_tasks", "review_task_events"):
                            archive[table].extend(db.execute(f"SELECT * FROM {table} WHERE document_id=?", (identity,)).fetchall())
                            db.execute(f"DELETE FROM {table} WHERE document_id=?", (identity,))
                        archive["publications"].extend(db.execute("SELECT * FROM publications WHERE document_id=?", (identity,)).fetchall())
                        archive["reviews"].extend(db.execute("SELECT * FROM reviews WHERE document_id=?", (identity,)).fetchall())
                        shutil.move(str(workspace.state / "patents" / identity), str(trash / identity))
                        moved.append(identity)
                        db.execute("DELETE FROM publications WHERE document_id=?", (identity,))
                        db.execute("DELETE FROM reviews WHERE document_id=?", (identity,))
                    (trash / "manifest.json").write_text(json.dumps(archive, ensure_ascii=False, indent=2), encoding="utf-8")
                    remaining = {key: value for key, value in jobs.items() if key not in identities}
                    temporary = workspace.state / "jobs.json.tmp"
                    temporary.write_text(json.dumps(remaining, ensure_ascii=False), encoding="utf-8")
                    temporary.replace(workspace.state / "jobs.json")
            except Exception:
                for identity in moved:
                    shutil.move(str(trash / identity), str(workspace.state / "patents" / identity))
                (workspace.state / "jobs.json").write_text(json.dumps(jobs, ensure_ascii=False), encoding="utf-8")
                raise
        return self.json({"deleted_document_ids": identities, "deleted_count": len(identities), "patents": workspace.patents()})


def main():
    parser = argparse.ArgumentParser(description="SDA Studio — migrated DataAgent web interface")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4182)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--python", default=os.environ.get("SDA_STUDIO_PYTHON", sys.executable))
    parser.add_argument("--ocr-device", default=os.environ.get("SDA_STUDIO_OCR_DEVICE", "gpu:0"))
    parser.add_argument("--ocr-backend", choices=("vl", "paddle"), default=os.environ.get("SDA_STUDIO_OCR_BACKEND", "vl"))
    parser.add_argument("--text-backend", choices=("vl", "paddle"), default=os.environ.get("SDA_STUDIO_TEXT_OCR_BACKEND", "paddle"))
    parser.add_argument("--text-workers", type=int, default=int(os.environ.get("SDA_STUDIO_TEXT_WORKERS", "1")))
    parser.add_argument("--table-workers", type=int, default=os.environ.get("SDA_STUDIO_TABLE_WORKERS"),
                        help="表格 OCR 并发；默认按空闲显存自动选择，正整数可覆盖")
    parser.add_argument("--ocr-execution", choices=("parallel", "serial"), default=os.environ.get("SDA_STUDIO_OCR_EXECUTION", "parallel"))
    args = parser.parse_args()
    if args.table_workers is not None and args.table_workers < 1:
        parser.error("--table-workers 必须为正整数；省略时自动选择")
    workspace = Workspace(args.root)
    server = StudioServer((args.host, args.port), workspace, python=args.python,
                          ocr_device=args.ocr_device, ocr_backend=args.ocr_backend,
                          text_backend=args.text_backend, text_workers=args.text_workers,
                          table_workers=args.table_workers, ocr_parallel=args.ocr_execution == "parallel")
    server.queue.recover(config=server.execution_config())
    # The detached worker survives HTTP restarts. Its file lock enforces one GPU task.
    def supervise_worker():
        while not server.closing.is_set():
            try:
                ensure_worker(workspace, python=args.python)
            except OSError as exc:
                print(f"Queue worker unavailable: {exc}", file=sys.stderr, flush=True)
            server.closing.wait(5)
    threading.Thread(target=supervise_worker, daemon=True, name="sda-queue-supervisor").start()
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown, daemon=True).start())
    print(f"SDA Studio: http://{args.host}:{server.server_port}/studio/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
