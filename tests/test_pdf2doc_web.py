"""Process concurrency and HTTP upload-to-structured-document integration."""

import asyncio
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
from time import monotonic, sleep
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen

import pymupdf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from document import pdf2doc
from document import run_document_pdf as runner
from pipeline import BatchPipeline, PipelineConfig
from studio.sda_data import Workspace
from studio.server import StudioServer
import test_pdf2doc_document as fixture


def _meet_other_branch(output_dir, branch, peer):
    started = monotonic()
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    (output / f"{branch}.started").write_text(str(os.getpid()))
    deadline = monotonic() + 8
    while not (output / f"{peer}.started").exists():
        if monotonic() > deadline:
            raise RuntimeError("OCR branches did not start concurrently")
        sleep(0.01)
    sleep(0.03)
    return {"pid": os.getpid(), "started": started, "finished": monotonic(), "workers": 1}


def parallel_page_probe(layout, output_dir, config):
    runtime = _meet_other_branch(output_dir, "text", "table")
    return {"page_summary": {"status": "complete", "successful_pages": 1}, "pages": [],
            "errors": [], "runtime": runtime}


def parallel_table_probe(layout, layout_dir, output_dir, config, backend, batch_size, enabled, workers):
    runtime = _meet_other_branch(output_dir, "table", "text")
    return {"tables": [], "errors": [], "runtime": runtime}


class OCRProcessTests(unittest.TestCase):
    def test_isolated_layout_returns_crops_without_loading_models_in_parent(self):
        case = fixture.PDFDocumentTests()
        case.setUp()
        try:
            output = case.root / "isolated-layout"
            result = pdf2doc._document_layout_stage(case.source, output, "demo", {
                "regions_json": case.regions, "orientation": "off", "crop_dpi": 72,
            }, isolated=True)
            self.assertEqual(result["image_count"], 2)
            self.assertTrue(all((output / row["image"]).is_file() for row in result["images"]))
        finally:
            case.tearDown()

    def test_real_spawn_processes_overlap_before_join(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.pdf"
            with pymupdf.open() as pdf:
                pdf.new_page()
                pdf.save(source)
            layout = {"processing_pdf": str(source), "processing_pdf_sha256": pdf2doc.file_digest(source),
                      "images": [{"id": "probe", "label": "table"}]}
            with patch.object(pdf2doc, "_recognize_page_content", parallel_page_probe), \
                    patch.object(pdf2doc, "_recognize_table_content", parallel_table_probe):
                result = pdf2doc.recognize_content(layout, root, root / "output", config=pdf2doc.OCRConfig(device="cpu"))
            text, table = result["runtime"]["text"], result["runtime"]["table"]
            self.assertEqual(result["execution"], "parallel")
            self.assertNotEqual(text["pid"], table["pid"])
            self.assertNotIn(os.getpid(), (text["pid"], table["pid"]))
            self.assertLess(max(text["started"], table["started"]), min(text["finished"], table["finished"]))


class WebFailureReportingTests(unittest.TestCase):
    def test_table_counts_survive_parallel_text_branch_completion(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Workspace(Path(temporary))
            server = StudioServer(("127.0.0.1", 0), workspace)
            try:
                for event in (
                        {"branch": "table", "status": "processing", "completed": 0, "total": 5},
                        {"branch": "table", "status": "processing", "completed": 2, "total": 5},
                        {"branch": "text", "status": "completed", "completed": 63, "total": 63}):
                    server.update_progress("demo", "sda-ocr-progress " + json.dumps(event))
                job = workspace.jobs()["demo"]
                self.assertEqual((job["stage_current"], job["stage_total"]), (2, 5))
                self.assertEqual(job["ocr_branches"], {"table": "processing", "text": "completed"})
                self.assertEqual(job["stage"], "ocr")
                server.update_progress("demo", "sda-ocr-progress " + json.dumps(
                    {"branch": "table", "status": "partial", "completed": 5, "total": 5}))
                job = workspace.jobs()["demo"]
                self.assertEqual((job["stage_current"], job["stage_total"]), (5, 5))
                self.assertEqual(job["ocr_branches"]["table"], "partial")
            finally:
                server.server_close()

    def test_semantic_logs_update_stage_and_batch_counts(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Workspace(Path(temporary))
            server = StudioServer(("127.0.0.1", 0), workspace)
            try:
                for line, stage, current, total in (
                        ("llm-classification 20/28", "classify", 20, 28),
                        ("llm-template-start", "template", None, None),
                        ("llm-sequence-progress 8/9", "sequence_extract", 8, 9),
                        ("llm-questions: diagnostics=12 selected=12", "tst", None, None),
                        ("llm-answer: question 2/12 question-test", "tst", 2, 12)):
                    server.update_progress("demo", line)
                    job = workspace.jobs()["demo"]
                    self.assertEqual((job["stage"], job["stage_current"], job["stage_total"]), (stage, current, total))
            finally:
                server.server_close()

    def test_restart_does_not_stop_server_with_active_job(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "studio").mkdir()
            (root / ".studio").mkdir()
            script = root / "studio/studio.sh"
            script.write_bytes((ROOT / "studio/studio.sh").read_bytes())
            (root / ".studio/studio.pid").write_text(str(os.getpid()))
            (root / ".studio/jobs.json").write_text(json.dumps({"demo": {"status": "processing"}}))
            result = subprocess.run(["bash", str(script), "restart"], capture_output=True, text=True,
                                    env={**os.environ, "SDA_STUDIO_PYTHON": sys.executable}, timeout=5)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("解析任务仍在运行（demo）", result.stderr)

    def test_interrupted_and_incomplete_runs_preserve_actionable_errors(self):
        for code, closing, report, expected in (
                (-signal.SIGTERM, True, {}, "服务停止或重启"),
                (-signal.SIGTERM, False, {}, "SIGTERM"),
                (1, False, {"status": "failed", "error": "表格识别异常"}, "表格识别异常"),
                (0, False, {}, "未生成有效运行汇总")):
            with self.subTest(code=code, closing=closing, report=report), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                workspace = Workspace(root)
                run = workspace.state / "patents/demo/runs/test"
                workspace.update_job("demo", input_dir=str(run / "data/demo"),
                                     text_context_dir=str(run / "data/demo"),
                                     previous_input_dir=str(root / "previous"))
                server = StudioServer(("127.0.0.1", 0), workspace)
                def command(identity, args, log):
                    workspace.update_job(identity, ocr_branches={"text": "completed", "table": "processing"})
                    if report:
                        runner.save(run / "document/run_summary.json", report)
                    if closing:
                        server.closing.set()
                    return code
                try:
                    with patch.object(server, "command", command):
                        server.run_pipeline("demo", run)
                    job = workspace.jobs()["demo"]
                    self.assertEqual(job["status"], "failed")
                    self.assertIn(expected, job["error"])
                    self.assertEqual(job["ocr_branches"], {"text": "completed", "table": "failed"})
                    self.assertIn(job["error"], (run / "pipeline.log").read_text())
                finally:
                    server.server_close()


class WebDocumentFlowTests(unittest.TestCase):
    def setUp(self):
        self.case = fixture.PDFDocumentTests()
        self.case.setUp()
        self.root = self.case.root
        self.commands, self.cache_counts = [], []
        self.review_orientation = False
        self.popen = patch.object(runner.subprocess, "Popen", self.fake_process)
        self.popen.start()

    def tearDown(self):
        self.popen.stop()
        self.case.tearDown()

    def fake_process(self, command, **kwargs):
        command = list(map(str, command))
        self.commands.append(command)
        output = io.StringIO()
        with redirect_stdout(output):
            if command[1].endswith("/document/pdf2doc.py"):
                args = command[2:]
                # 替换模型边界；仍执行真实 CLI、校正、裁图、组装与导出。
                args[args.index("--ocr-execution") + 1] = "serial"
                args[args.index("--render-prefetch") + 1] = "0"
                args[args.index("--pp-model-dir") + 1] = str(Path(self.case.config.model_root) / pdf2doc.LAYOUT_MODEL)
                args += ["--ocr-model-root", self.case.config.model_root, "--max-side", "400"]
                def detect(backend, image):
                    index = int(Path(image).stem.split("-")[-1]) - 1
                    pixmap = pymupdf.Pixmap(image)
                    box = [v * (pixmap.width / 300 if i % 2 == 0 else pixmap.height / 400)
                           for i, v in enumerate(self.case.boxes[index])]
                    return [pdf2doc.RawDetection("table", 0.95, box, backend.model, backend.model_id, backend.name)]
                decision = {"clockwise": 0, "status": "needs_review" if self.review_orientation else "upright", "probes": []}
                with patch.object(pdf2doc.PPDocLayoutV3Backend, "predict", detect), \
                        patch.object(pdf2doc.OrientationDetector, "__call__", return_value=decision):
                    code = pdf2doc.main(args)
            elif command[1].endswith("/graph.py"):
                def argument(name):
                    return command[command.index(name) + 1]
                config = PipelineConfig(text_pages_jsonl=argument("--text-pages-jsonl"),
                    text_context_dir=argument("--text-context-dir"),
                    reuse_table_ocr_from=argument("--reuse-table-ocr-from"),
                    ocr_workers=1, document_template_agent=False)
                pipeline = BatchPipeline(argument("--image-dir"), argument("--output-dir"), config)
                with patch.object(pdf2doc, "get_ocr", side_effect=AssertionError("OCR repeated during extraction")):
                    asyncio.run(pipeline._produce_ocr(iter(pipeline.images), None))
                self.cache_counts.append(pipeline.metrics["ocr"])
                target = Path(argument("--output-dir"))
                identity = next(iter(pipeline.document_ids.values()))
                runner.save(target / identity / "dataset.json", {"document_id": identity, "status": "complete",
                    "records": [], "entities": [], "experiments": [], "relations": []})
                runner.save(target / "_batch_summary.json", {"status": "complete", "token_usage": {"totals": {}}})
                code = 0
            else:
                raise AssertionError(command)
        class Process:
            stdout = io.StringIO(output.getvalue())
            def wait(self):
                return code
            def terminate(self):
                pass
        return Process()

    def run_pdf(self, run_dir=None):
        return runner.run_pdf(self.case.source, run_dir or self.root / "run", document_id="demo",
                              device="cpu", text_backend="vl", text_workers=1, table_ocr_workers=1,
                              template_agent=False)

    def test_web_runner_uses_six_steps_and_reuses_every_table_without_ocr(self):
        report = self.run_pdf()
        self.assertEqual(report["status"], "complete")
        self.assertEqual([p["name"] for p in report["phases"]], ["structured_document", "document_extraction"])
        preprocess, extraction = self.commands
        self.assertEqual(preprocess[preprocess.index("--orientation") + 1], "auto")
        self.assertEqual(preprocess[preprocess.index("--ocr-execution") + 1], "parallel")
        self.assertIn("--reuse-table-ocr-from", extraction)
        self.assertEqual(self.cache_counts[0]["cache_hits"], 2)
        self.assertEqual(self.cache_counts[0]["requests"], 0)
        self.assertTrue(Path(report["structured_document"]).is_file())
        self.assertTrue((self.root / "run/text_ocr/pages.jsonl").is_file())
        self.assertTrue((self.root / "run/data/demo/demo_manifest.json").is_file())
        html = (Path(report["result_dir"]) / "report.html").read_text()
        self.assertIn("structured_artifacts", html)
        self.assertIn("../../structured/document.json", html)

    def test_orientation_review_is_preserved_as_partial_after_semantic_extraction(self):
        self.review_orientation = True
        report = self.run_pdf()
        self.assertEqual(report["status"], "partial")
        self.assertTrue(report["phases"][0]["accepted_partial"])
        dataset = json.loads((Path(report["result_dir"]) / "dataset.json").read_text())
        self.assertEqual(dataset["status"], "partial")
        self.assertEqual(dataset["preprocessing"]["status"], "partial")

    def test_preprocessing_failure_does_not_invoke_semantic_model(self):
        self.case.engine.predict.side_effect = RuntimeError("page OCR failed")
        with self.assertRaisesRegex(RuntimeError, "全文 OCR 不完整"):
            self.run_pdf()
        self.assertEqual(len(self.commands), 1)
        self.assertEqual(json.loads((self.root / "run/structured/document.json").read_text())["status"], "partial")
        # 表格虽成功，但全文不完整；graph 的输入完整性检查必须阻止后续请求。

    def test_raw_table_tampering_is_rejected_before_cache_commit(self):
        self.run_pdf()
        structured = self.root / "run/structured"
        path = structured / "table_ocr/tables.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        rows[0]["text_sha256"] = "0" * 64
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        cache = self.root / "bad-cache"
        with self.assertRaisesRegex(ValueError, "哈希"):
            runner.prepare_semantic_ocr_cache(structured, structured / "layout", cache)
        self.assertFalse(cache.exists())

    def test_finished_table_evidence_survives_later_interruption(self):
        layout_dir = self.root / "partial-layout"
        layout = pdf2doc.extract_pdf(self.case.source, layout_dir, document_id="demo",
                                     regions_json=self.case.regions, orientation="off", crop_dpi=72)
        def predict(paths, metadata):
            if metadata[0]["page"] == 2:
                raise KeyboardInterrupt()
            return self.case.predict_tables(paths, metadata)
        output = self.root / "partial-recognition"
        with patch.object(pdf2doc, "extract_table_evidence_batch", predict), self.assertRaises(KeyboardInterrupt):
            pdf2doc._recognize_table_content(layout, layout_dir, output, self.case.config, "vl", 1, True, 1)
        self.assertEqual(json.loads((output / "table_ocr/table-1.json").read_text())["status"], "success")
        self.assertFalse((output / "table_ocr/table-2.json").exists())

    def test_http_upload_automatically_runs_pipeline_and_serves_structured_files(self):
        (self.root / "web").mkdir()
        workspace = Workspace(self.root / "web")
        server = StudioServer(("127.0.0.1", 0), workspace, python=sys.executable,
                              ocr_device="cpu", text_backend="vl")
        def command(identity, args, log):
            self.assertTrue(args[1].endswith("/src/document/run_document_pdf.py"))
            self.assertIn("--ocr-execution", args)
            report = runner.run_pdf(args[2], args[args.index("--run-dir") + 1], document_id=identity,
                                    device="cpu", text_backend="vl", template_agent=False)
            return int(report["status"] == "failed")
        server.command = command
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_port}"
            boundary = "sda-test-boundary"
            body = (f'--{boundary}\r\nContent-Disposition: form-data; name="document_id"\r\n\r\ndemo\r\n'
                    f'--{boundary}\r\nContent-Disposition: form-data; name="pdf"; filename="demo.pdf"\r\n'
                    'Content-Type: application/pdf\r\n\r\n').encode()
            body += self.case.source.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
            request = Request(base + "/api/patents/import", data=body,
                              headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
            with urlopen(request, timeout=10) as response:
                self.assertEqual(response.status, 202)
            deadline = monotonic() + 10
            while workspace.jobs()["demo"]["status"] in {"queued", "processing"} and monotonic() < deadline:
                sleep(0.02)
            self.assertEqual(workspace.jobs()["demo"]["status"], "completed", workspace.jobs()["demo"])
            with urlopen(base + "/api/patents/demo/structured.json") as response:
                document = json.load(response)
            self.assertEqual(document["producer"], "SDA/pdf2doc")
            self.assertEqual(len(document["tables"]), 2)
            for suffix in ("structured.md", "structured.txt", "structured-summary.json"):
                with urlopen(base + "/api/patents/demo/" + suffix) as response:
                    self.assertEqual(response.status, 200)
            with urlopen(base + "/api/patents") as response:
                patent = json.load(response)["patents"][0]
            self.assertIn("structured_json", patent["artifacts"])
            server.update_progress("demo", 'pdf2doc stage=recognition\n')
            for branch in ("text", "table"):
                server.update_progress("demo", 'sda-ocr-progress ' + json.dumps({"branch": branch, "status": "processing"}))
            self.assertEqual(workspace.jobs()["demo"]["ocr_branches"], {"text": "processing", "table": "processing"})
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
