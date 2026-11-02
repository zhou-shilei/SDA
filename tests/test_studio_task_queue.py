"""Exercise persistence and real competing processes without OCR/model calls."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from time import monotonic, sleep
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from studio.sda_data import Workspace
from studio.task_queue import TaskQueue, process_token
from studio.queue_worker import QueueWorker


class TaskQueueTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace = Workspace(self.root)
        self.queue = TaskQueue(self.workspace)

    def submit(self, identity, *, run_id="first", operation="pipeline"):
        self.workspace.update_job(identity, status="queued", stage="queued", run_id=run_id,
                                  operation=operation, queued_at="2026-11-02T00:00:00+00:00", started_at=None)
        return self.queue.submit(identity, config={"ocr_device": "cpu"})

    def test_fifo_survives_reopen_and_ignores_last_updated_order(self):
        self.submit("doc-a")
        self.submit("doc-b", operation="analyze")
        self.submit("doc-c", operation="compare")
        self.workspace.update_job("doc-a", filename="changed-after-submission.pdf")
        reopened = TaskQueue(Workspace(self.root))
        self.assertEqual(reopened.snapshot()["waiting"], 3)
        claimed = []
        for expected in ("doc-a", "doc-b", "doc-c"):
            task = reopened.claim()
            self.assertEqual(task["document_id"], expected)
            claimed.append(task["job"]["operation"])
            self.assertEqual(reopened.snapshot()["running"], 1)
            self.assertIsNone(reopened.claim())
            self.workspace.update_job(expected, status="completed")
            reopened.finish(task)
        self.assertEqual(claimed, ["pipeline", "analyze", "compare"])
        self.assertEqual(reopened.snapshot()["waiting"], 0)

    def test_dedup_and_retry_gets_a_new_place_at_the_end(self):
        first = self.submit("doc-a")
        self.assertEqual(first, self.queue.submit("doc-a", config={}))
        self.submit("doc-b")
        task = self.queue.claim()
        self.workspace.update_job("doc-a", status="failed")
        self.queue.finish(task)
        self.submit("doc-a", run_id="retry")
        positions = self.queue.snapshot()["positions"]
        self.assertEqual(positions["doc-b"]["queue_position"], 1)
        self.assertEqual(positions["doc-a"]["queue_position"], 2)

    def test_public_execution_order_uses_queue_ids_and_excludes_private_fields(self):
        for identity, name in (("doc-a", "张三"), ("doc-b", "李四"), ("doc-c", "王五")):
            self.submit(identity)
            self.workspace.update_job(identity, filename=f"{identity}.pdf", submitted_by=name,
                                      token="private", execution_config={"secret": "private"})
        task = self.queue.claim()
        self.workspace.update_job("doc-a", stage="reader", progress=76)
        # An old document being retried still belongs at the end, regardless
        # of its original creation time or subsequent status writes.
        self.workspace.update_job("doc-b", created_at="2099-01-01", queued_at="2099-01-01")
        snapshot = self.queue.snapshot()
        self.assertEqual([t["document_id"] for t in snapshot["tasks"]], ["doc-a", "doc-b", "doc-c"])
        self.assertEqual([t["position"] for t in snapshot["tasks"]], [1, 2, 3])
        self.assertEqual([t["submitted_by"] for t in snapshot["tasks"]], ["张三", "李四", "王五"])
        self.assertEqual(snapshot["tasks"][0]["progress"], 76)
        self.assertEqual(snapshot["tasks"][1]["queue_position"], 1)
        self.assertEqual(snapshot["tasks"][1]["queue_ahead"], 1)
        self.assertNotIn("private", json.dumps(snapshot))
        self.workspace.update_job("doc-a", status="failed")
        self.queue.finish(task)
        self.submit("doc-a", run_id="retry")
        self.assertEqual([t["document_id"] for t in self.queue.snapshot()["tasks"]], ["doc-b", "doc-c", "doc-a"])

    def test_failed_task_releases_slot_and_next_task_runs(self):
        self.submit("doc-a")
        self.submit("doc-b")
        calls = []
        workspace = self.workspace
        class Executor:
            def run_pipeline(self, identity, run, operation):
                calls.append(identity)
                if identity == "doc-a":
                    raise RuntimeError("simulated model failure")
                workspace.update_job(identity, status="completed")
        worker = QueueWorker(workspace, executor_factory=lambda *_a, **_k: Executor())
        self.assertTrue(worker.run_one())
        self.assertTrue(worker.run_one())
        self.assertEqual(calls, ["doc-a", "doc-b"])
        self.assertEqual(workspace.jobs()["doc-a"]["status"], "failed")
        self.assertIn("simulated model failure", workspace.jobs()["doc-a"]["error"])
        self.assertEqual(workspace.jobs()["doc-b"]["status"], "completed")

    def test_migrate_old_waiting_jobs_and_do_not_replay_completed_jobs(self):
        self.workspace.update_job("old-doc", status="queued", run_id="legacy", started_at="2026-11-01T00:00:00+00:00")
        self.queue.recover(config={"ocr_device": "cpu", "table_workers": 1})
        task = self.queue.claim()
        self.assertEqual(task["config"]["ocr_device"], "cpu")
        self.workspace.update_job("old-doc", status="completed")
        self.queue.recover()
        self.assertIsNone(self.queue.claim())

    def test_dead_runner_with_live_child_blocks_the_next_gpu_task(self):
        self.submit("doc-a")
        self.submit("doc-b")
        self.queue.claim()
        self.workspace.update_job("doc-a", runner_pid=999999999, worker_pid=os.getpid(),
                                  worker_pid_token=process_token(os.getpid()))
        self.queue.recover()
        self.assertEqual(self.workspace.jobs()["doc-a"]["status"], "processing")
        self.assertIsNone(self.queue.claim())
        self.assertEqual(self.queue.snapshot()["positions"]["doc-b"]["queue_ahead"], 1)
        self.workspace.update_job("doc-a", worker_pid=999999998)
        self.queue.recover()
        self.assertEqual(self.workspace.jobs()["doc-a"]["status"], "failed")
        self.assertEqual(self.queue.claim()["document_id"], "doc-b")

    def test_pid_reuse_does_not_hold_the_queue_forever(self):
        self.submit("doc-a")
        self.queue.claim()
        self.workspace.update_job("doc-a", runner_pid_token="wrong-start-time")
        self.queue.recover()
        self.assertEqual(self.workspace.jobs()["doc-a"]["status"], "failed")

    def test_wait_time_is_excluded_from_processing_start(self):
        self.submit("doc-a")
        self.assertIsNone(self.workspace.jobs()["doc-a"]["started_at"])
        self.queue.claim()
        job = self.workspace.jobs()["doc-a"]
        self.assertGreater(job["started_at"], job["queued_at"])

    def test_separate_processes_cannot_execute_two_tasks_at_once(self):
        for i in range(5):
            self.submit(f"doc-{i}")
        script = '''
import sys, time
from pathlib import Path
from studio.sda_data import Workspace
from studio.queue_worker import QueueWorker
workspace = Workspace(Path(sys.argv[1]))
class Executor:
    def run_pipeline(self, identity, run, operation):
        with (workspace.root / "events").open("a") as f:
            f.write("start " + identity + "\\n")
        time.sleep(0.08)
        with (workspace.root / "events").open("a") as f:
            f.write("end " + identity + "\\n")
        workspace.update_job(identity, status="completed")
worker = QueueWorker(workspace, executor_factory=lambda *_a, **_k: Executor())
deadline = time.monotonic() + 10
while time.monotonic() < deadline:
    worker.run_one()
    if all(j["status"] == "completed" for j in workspace.jobs().values()):
        break
    time.sleep(0.01)
else:
    raise RuntimeError("Queue did not drain")
'''
        processes = [subprocess.Popen([sys.executable, "-c", script, str(self.root)], cwd=ROOT,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(3)]
        try:
            for process in processes:
                stdout, stderr = process.communicate(timeout=20)
                self.assertEqual(process.returncode, 0, stdout + stderr)
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.wait()
        expected = [f"{event} doc-{i}" for i in range(5) for event in ("start", "end")]
        self.assertEqual((self.root / "events").read_text().splitlines(), expected)

    def test_detached_worker_survives_http_shutdown_and_has_single_owner(self):
        # Empty queue: this exercises the real detached process without OCR.
        server = subprocess.Popen([sys.executable, str(ROOT / "studio/server.py"), "--root", str(self.root), "--port", "0"],
                                  cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        pid_path = self.workspace.state / "queue-worker.pid"
        worker_pid = None
        try:
            deadline = monotonic() + 10
            while not pid_path.exists() and monotonic() < deadline:
                sleep(0.02)
            self.assertTrue(pid_path.exists())
            worker_pid = int(pid_path.read_text())
            from studio.task_queue import ensure_worker
            ensure_worker(self.workspace)
            self.assertEqual(int(pid_path.read_text()), worker_pid)
            server.terminate()
            server.communicate(timeout=10)
            self.assertIsNotNone(process_token(worker_pid))
            self.assertTrue(self.queue.snapshot()["worker_online"])
        finally:
            if server.poll() is None:
                server.kill()
                server.communicate()
            if worker_pid and process_token(worker_pid):
                import signal
                os.kill(worker_pid, signal.SIGTERM)
                deadline = monotonic() + 5
                while process_token(worker_pid) and monotonic() < deadline:
                    sleep(0.02)

    def test_concurrent_http_uploads_drain_through_detached_worker_after_restart(self):
        from concurrent.futures import ThreadPoolExecutor
        from urllib.request import Request, urlopen
        import pymupdf
        source = self.root / "src/document/run_document_pdf.py"
        source.parent.mkdir(parents=True)
        source.write_text('''
import json, sys, time
from pathlib import Path
identity = sys.argv[sys.argv.index("--document-id") + 1]
run = Path(sys.argv[sys.argv.index("--run-dir") + 1])
run.mkdir(parents=True, exist_ok=True)
with Path("events").open("a") as f:
    f.write("start " + identity + "\\n")
time.sleep(0.3)
(run / "run_summary.json").write_text(json.dumps({"status":"complete"}))
with Path("events").open("a") as f:
    f.write("end " + identity + "\\n")
''')
        with pymupdf.open() as pdf:
            pdf.new_page()
            data = pdf.tobytes()
        servers = []
        def start_server():
            process = subprocess.Popen([sys.executable, str(ROOT / "studio/server.py"), "--root", str(self.root),
                                        "--port", "0", "--ocr-device", "cpu"],
                                       cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            servers.append(process)
            return process, process.stdout.readline().strip().split()[-1].removesuffix("/studio/")
        worker_pid = None
        try:
            server, base = start_server()
            def upload(index):
                boundary = "queue-integration"
                body = (f'--{boundary}\r\nContent-Disposition: form-data; name="document_id"\r\n\r\ndoc-{index}\r\n'
                        f'--{boundary}\r\nContent-Disposition: form-data; name="pdf"; filename="doc-{index}.pdf"\r\n'
                        f'Content-Type: application/pdf\r\n\r\n').encode() + data + f"\r\n--{boundary}--\r\n".encode()
                with urlopen(Request(base + "/api/patents/import", data=body,
                    headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}), timeout=10) as response:
                    self.assertEqual(response.status, 202)
                    return json.load(response)["patent"]
            with ThreadPoolExecutor(max_workers=3) as clients:
                uploaded = list(clients.map(upload, range(3)))
            self.assertEqual(len({row["document_id"] for row in uploaded}), 3)
            expected_order = [row[0] for row in self.workspace.connection().execute(
                "SELECT document_id FROM task_queue ORDER BY id").fetchall()]
            deadline = monotonic() + 10
            events = self.root / "events"
            while not events.exists() and monotonic() < deadline:
                sleep(0.02)
            self.assertTrue(events.exists())
            worker_pid = int((self.workspace.state / "queue-worker.pid").read_text())
            server.terminate()
            server.communicate(timeout=10)
            _, base = start_server()
            deadline = monotonic() + 15
            while not all(job["status"] == "completed" for job in self.workspace.jobs().values()) and monotonic() < deadline:
                sleep(0.02)
            self.assertTrue(all(job["status"] == "completed" for job in self.workspace.jobs().values()), self.workspace.jobs())
            self.assertEqual(events.read_text().splitlines(), [f"{event} {identity}" for identity in expected_order for event in ("start", "end")])
            self.assertEqual(int((self.workspace.state / "queue-worker.pid").read_text()), worker_pid)
            with urlopen(base + "/api/queue", timeout=5) as response:
                snapshot = json.load(response)
            self.assertEqual((snapshot["concurrency"], snapshot["running"], snapshot["waiting"]), (1, 0, 0))
        finally:
            for server in servers:
                if server.poll() is None:
                    server.terminate()
                    server.communicate(timeout=10)
            if worker_pid and process_token(worker_pid):
                import signal
                os.kill(worker_pid, signal.SIGTERM)
                deadline = monotonic() + 5
                while process_token(worker_pid) and monotonic() < deadline:
                    sleep(0.02)

    def test_legacy_upgrade_waits_for_idle_and_backs_up_before_restart(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import threading
        self.workspace.update_job("legacy-doc", status="processing", run_id="old")
        restarted = self.root / "restarted"
        root = self.root
        class Health(BaseHTTPRequestHandler):
            def do_GET(self):
                payload = json.dumps({"persistent_queue": restarted.exists()}).encode()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(payload)
            def log_message(self, *_args):
                pass
        health = ThreadingHTTPServer(("127.0.0.1", 0), Health)
        thread = threading.Thread(target=health.serve_forever, daemon=True)
        thread.start()
        script = root / "studio/studio.sh"
        script.parent.mkdir()
        script.write_text('touch "$(dirname "$0")/../restarted"\n')
        process = subprocess.Popen([sys.executable, str(ROOT / "studio/upgrade_when_idle.py"),
            "--root", str(root), "--port", str(health.server_port), "--interval", "0.05"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            status = self.workspace.state / "queue-upgrade.json"
            deadline = monotonic() + 5
            while not status.exists() and monotonic() < deadline:
                sleep(0.01)
            self.assertEqual(json.loads(status.read_text())["state"], "waiting_for_legacy_queue")
            self.assertFalse(restarted.exists())
            self.workspace.update_job("legacy-doc", status="completed")
            stdout, stderr = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 0, stdout + stderr)
            self.assertEqual(json.loads(status.read_text())["state"], "complete")
            backups = list((self.workspace.state / "backups").glob("before-gpu-queue-*"))
            self.assertEqual(len(backups), 1)
            self.assertTrue((backups[0] / "dataset.sqlite").is_file())
            self.assertEqual(json.loads((backups[0] / "jobs.json").read_text())["legacy-doc"]["status"], "completed")
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()
            health.shutdown()
            health.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
