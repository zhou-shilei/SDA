#!/usr/bin/env python3
"""Local SDA service for the migrated DataAgent interface; no old repo imports."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from email import policy
from email.parser import BytesParser
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import threading
from time import perf_counter
from urllib.parse import parse_qs, unquote, urlsplit
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from studio.sda_data import Workspace, now, read_json
from studio.document_input import prepare_document_input

MAX_UPLOAD = 300 * 1024 * 1024
IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{2,79}\Z")
STATIC = {"index.html", "styles.css", "app.js", "collection_review.js", "job_eta.js",
          "patent_identity.js", "table_columns.js", "run.html"}


class StudioServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, workspace, *, python=None, ocr_device="gpu:0", ocr_backend="vl"):
        self.workspace = workspace
        self.python = python or sys.executable
        self.ocr_device, self.ocr_backend = ocr_device, ocr_backend
        self.workers = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sda-import")
        self.render_lock = threading.Lock()
        self.processes = {}
        self.process_lock = threading.Lock()
        self.closing = threading.Event()
        super().__init__(address, StudioHandler)

    def enqueue(self, identity, *, operation="pipeline"):
        workspace = self.workspace
        with workspace.lock:
            if self.closing.is_set():
                raise ValueError("服务正在停止，请稍后重试")
            job = workspace.jobs().get(identity)
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
            if operation == "analyze" and not any(
                    isinstance(read_json(p, {}), dict) and read_json(p, {}).get("table_id")
                    and isinstance(read_json(p, {}).get("table_text"), str)
                    and read_json(p, {})["table_text"].strip() for p in previous_results.glob("*.json")):
                raise ValueError("没有可复用的表格 OCR，请先解析 PDF")
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
            workspace.update_job(identity, status="queued", stage="queued", progress=0, error=None,
                                 input_dir=str(input_dir), text_context_dir=str(run / "data" / identity),
                                 result_dir=str(run / "results" / identity), operation=operation,
                                 previous_result_dir=str(previous_results), previous_input_dir=str(previous_inputs),
                                 run_id=run.name, worker_pid=None, finished_at=None,
                                 started_at=now(), log=str(run / "pipeline.log"))
            self.workers.submit(self.run_pipeline, identity, run, operation)
        return next(p for p in workspace.patents() if p["document_id"] == identity)

    def run_pipeline(self, identity, run, operation="pipeline"):
        workspace = self.workspace
        started = perf_counter()
        try:
            run.mkdir(parents=True, exist_ok=True)
            source = workspace.state / "patents" / identity / "source.pdf"
            job = workspace.jobs()[identity]
            inputs = Path(job["input_dir"])
            context = Path(job["text_context_dir"])
            previous_inputs = Path(job["previous_input_dir"])
            workspace.update_job(identity, status="processing", stage="layout" if operation == "pipeline" else "extraction", progress=2)
            with (run / "pipeline.log").open("w", encoding="utf-8") as log:
                if operation == "pipeline":
                    work = run / "document"
                    result_dir = work / "results" / identity
                    input_dir = work / "data" / identity
                    workspace.update_job(identity, input_dir=str(input_dir), text_context_dir=str(input_dir),
                                         result_dir=str(result_dir))
                    command = [self.python, str(workspace.root / "src/run_document_pdf.py"), str(source),
                               "--document-id", identity, "--run-dir", str(work), "--device", self.ocr_device]
                    catalog = previous_inputs / "wipo/sequences.json"
                    if catalog.is_file():
                        command.extend(["--catalog", str(catalog)])
                    code = self.command(identity, command, log)
                    report = read_json(work / "run_summary.json", {})
                    if code or report.get("status") == "failed":
                        raise RuntimeError(f"文档提取失败：{report.get('error', '查看运行日志')}")
                    workspace.update_job(identity, status="completed", stage="completed", progress=100,
                                         semantic_status=report.get("status"), finished_at=now(), worker_pid=None)
                    return
                log.write("复用已保存的原始表格 OCR，重新抽取；不运行版面检测与表格 OCR。\n")
                previous_bundle = workspace._build(identity, Path(job["previous_result_dir"]), previous_inputs)
                evidence_pdf = previous_bundle["pdf"] or source
                manifest = read_json(previous_inputs / f"{identity}_manifest.json", {})
                prepare_document_input(context, identity,
                                       evidence_pdf if evidence_pdf.is_file() else None,
                                       existing=previous_inputs)
                workspace.update_job(identity, stage="ocr" if operation == "pipeline" else "extraction", progress=30)
                extraction = [self.python, str(workspace.root / "src/graph.py"),
                              "--image-dir", str(inputs), "--output-dir", str(run / "results"),
                              "--text-context-dir", str(context),
                              "--ocr-device", self.ocr_device, "--ocr-backend", self.ocr_backend]
                if operation == "analyze":
                    extraction.extend(["--reuse-table-ocr-from", job["previous_result_dir"]])
                    pages = Path(job["previous_result_dir"]).parent.parent / "text_ocr/pages.jsonl"
                    if not pages.is_file():
                        text_dir = run / "text_ocr"
                        code = self.command(identity, [self.python, str(workspace.root / "src/paten_ocr.py"),
                            str(evidence_pdf), "--document-id", identity, "--output-dir", str(text_dir),
                            "--backend", "paddle", "--device", self.ocr_device,
                            "--workers", "6", "--batch-size", "6", "--render-prefetch", "4"], log)
                        if code:
                            raise RuntimeError("全文 OCR 失败，不能运行文档提取")
                        pages = text_dir / "pages.jsonl"
                    extraction.extend(["--extraction-mode", "document", "--text-pages-jsonl", str(pages)])
                code = self.command(identity, extraction, log)
                summary = read_json(run / "results/_batch_summary.json", {})
                if code or not summary or summary.get("status") == "failed":
                    raise RuntimeError(f"SDA 提取未全部成功（退出码 {code}），已保存的产物可继续审阅")
                result_dir = run / "results" / identity
                dataset = read_json(result_dir / "dataset.json", {})
                report = {"document_id": identity, "run_kind": "ocr_reanalysis", "status": summary["status"],
                          "wall_seconds": perf_counter() - started, "token_usage": summary["token_usage"],
                          "result_dir": str(result_dir), "run_dir": str(run), "finished_at_utc": now(),
                          "total_pages": previous_bundle["pages"]["page_count"],
                          "table_count": len(previous_bundle["ocr"]),
                          "cache_policy": "复用表格 OCR 的重新提取；不是完整 PDF 首次解析耗时。",
                          "records": len(dataset.get("records", [])), "phases": [],
                          "experiments": len(dataset.get("experiments", [])), "relations": len(dataset.get("relations", []))}
                (result_dir / "run_summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                workspace.update_job(identity, status="completed", stage="completed", progress=100,
                                     preprocessing_status=manifest.get("status"), worker_pid=None,
                                     semantic_status=summary.get("status"), finished_at=now())
        except Exception as exc:
            workspace.update_job(identity, status="failed", stage="failed", worker_pid=None,
                                 error=str(exc), finished_at=now())

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
            self.workspace.update_job(identity, worker_pid=process.pid)
            log.write("$ " + json.dumps(command, ensure_ascii=False) + "\n")
            log.flush()
            try:
                for line in process.stdout:
                    log.write(line)
                    log.flush()
                    self.update_progress(identity, line)
                return process.wait()
            finally:
                with self.process_lock:
                    self.processes.pop(identity, None)
                self.workspace.update_job(identity, worker_pid=None)

    def update_progress(self, identity, line):
        changes = None
        if line.startswith("PDF stage="):
            stage = line.strip().split("=", 1)[1]
            progress = {"layout": 5, "text_ocr": 20, "table_ocr_and_extraction": 40}.get(stage, 50)
            changes = {"stage": {"text_ocr": "ocr", "table_ocr_and_extraction": "extraction"}.get(stage, stage), "progress": progress}
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
            self.workspace.update_job(identity, **changes)

    def server_close(self):
        self.closing.set()
        super().server_close()
        self.workers.shutdown(wait=False, cancel_futures=True)
        with self.process_lock:
            for process in self.processes.values():
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass


class StudioHandler(BaseHTTPRequestHandler):
    server_version = "SDAStudio/1.0"

    @property
    def workspace(self):
        return self.server.workspace

    def send_bytes(self, data, content_type, *, status=200, cache="no-store"):
        self.send_response(status)
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
            return self.json({"patents": self.workspace.patents()})
        if path == "/api/dataset/folders":
            return self.json({"folders": self.workspace.folders()})
        if path == "/api/dataset/records":
            return self.json({"records": self.workspace.published(query.get("dataset_id", [None])[0]),
                              "folders": self.workspace.folders()})
        if path == "/api/reviews/human":
            return self.json({"reviews": self.workspace.reviews()})
        if path == "/api/health":
            return self.json({"status": "ok", "service": "SDA Studio"})
        match = re.fullmatch(r"/api/patents/([^/]+)/(.+)", path)
        if not match:
            return self.json({"error": "接口不存在"}, 404)
        identity, artifact = match.groups()
        bundle = self.workspace.bundle(identity)
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
        page = re.fullmatch(r"pages/(\d{4})\.png", artifact)
        if page:
            return self.render_page(bundle, int(page[1]))
        return self.json({"error": "文件不存在"}, 404)

    def render_page(self, bundle, number):
        if not bundle["pdf"] or not 1 <= number <= bundle["pages"]["page_count"]:
            return self.json({"error": "页码不存在"}, 404)
        pdf = bundle["pdf"]
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
        if path == "/api/dataset/folders":
            folder = self.workspace.create_folder(payload.get("name"))
            return self.json({"folder": folder, "folders": self.workspace.folders()}, 201)
        if path == "/api/reviews/human":
            return self.json({"review": self.workspace.save_review(payload)})
        if path == "/api/patents/publish-records":
            return self.json(self.workspace.publish(payload.get("document_id"), payload.get("record_ids"), payload.get("dataset_id", "SDA")))
        if path == "/api/patents/retry":
            return self.json({"patent": self.server.enqueue(payload.get("document_id"))}, 202)
        if path == "/api/patents/analyze":
            return self.json({"patent": self.server.enqueue(payload.get("document_id"), operation="analyze")}, 202)
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
        with self.workspace.lock:
            if identity in self.workspace.locations():
                raise ValueError("专利已存在")
            parent = self.workspace.state / "patents" / identity
            parent.mkdir(parents=True, exist_ok=False)
            (parent / "source.pdf").write_bytes(data)
            self.workspace.update_job(identity, filename=Path(pdf_part.get_filename() or f"{identity}.pdf").name,
                                      status="uploaded", created_at=now())
            patent = self.server.enqueue(identity)
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
            trash = workspace.state / "trash" / uuid.uuid4().hex
            trash.mkdir(parents=True)
            moved = []
            try:
                with workspace.connection() as db:
                    archive = {"jobs": {identity: jobs[identity] for identity in identities}, "publications": [], "reviews": []}
                    for identity in identities:
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
    args = parser.parse_args()
    workspace = Workspace(args.root)
    for identity, job in workspace.jobs().items():
        if job.get("status") in {"queued", "processing"}:
            workspace.update_job(identity, status="failed", stage="failed", error="服务曾中断；已有结果保留，可点击重试")
    server = StudioServer((args.host, args.port), workspace, python=args.python,
                          ocr_device=args.ocr_device, ocr_backend=args.ocr_backend)
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
