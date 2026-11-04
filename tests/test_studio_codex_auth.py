"""Credential isolation, upload boundaries and task handoff, without model calls."""

import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from studio.codex_auth import CodexAuthStore, validate_auth
from studio.pipeline_executor import PipelineExecutor
from studio.server import StudioServer
from studio.sda_data import Workspace
from studio.task_queue import TaskQueue


AUTH = {"OPENAI_API_KEY": None, "tokens": {"access_token": "test-access-secret",
        "refresh_token": "test-refresh-secret", "id_token": "test-id-secret", "account_id": "test-account"}}


class CodexAuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = Workspace(self.root)
        self.store = CodexAuthStore(self.workspace.state)

    def test_private_separate_files_and_default_environment(self):
        first, second = self.store.save(AUTH), self.store.save(AUTH)
        one, two = self.store.home(first), self.store.home(second)
        self.assertNotEqual(one, two)
        self.assertNotEqual(one.name, first)
        self.assertEqual(json.loads((one / "auth.json").read_text()), AUTH)
        for path, mode in ((self.store.root, 0o700), (one, 0o700), (one / "auth.json", 0o600)):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), mode)
        base = {"CODEX_HOME": "/default", "OPENAI_API_KEY": "default-secret", "CODEX_API_KEY": "other",
                "CODEX_ACCESS_TOKEN": "other", "OPENAI_BASE_URL": "https://other.invalid", "PATH": "/bin"}
        custom = self.store.environment(first, base)
        self.assertEqual(custom, {"CODEX_HOME": str(one), "PATH": "/bin"})
        self.assertEqual(self.store.environment(None, base), base)
        self.assertEqual(base["CODEX_HOME"], "/default")

    def test_reject_invalid_content_and_references_without_echoing_secrets(self):
        for value in (None, [], {}, {"tokens": {}}, {"OPENAI_API_KEY": "secret\r\ninjected"},
                      {"tokens": {"access_token": "secret"}}, {"OPENAI_API_KEY": "x" * 65536}):
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaises(ValueError) as caught:
                    self.store.save(value)
                self.assertNotIn("injected", str(caught.exception))
        for ref in ("../../auth.json", "a" * 64, [], {}):
            with self.assertRaises(ValueError):
                self.store.home(ref)
        self.assertFalse(self.store.root.exists())
        self.assertIn("OPENAI_API_KEY", json.loads(validate_auth({"OPENAI_API_KEY": "test-key"})))

    def test_actual_subprocess_reads_and_refreshes_only_selected_login(self):
        reference = self.store.save(AUTH)
        other = self.store.save(AUTH)
        self.workspace.update_job("DOC", _codex_auth_id=reference, status="processing")
        executor = PipelineExecutor(self.workspace)
        script = ("import os,json,pathlib; p=pathlib.Path(os.environ['CODEX_HOME'])/'auth.json'; "
                  "d=json.loads(p.read_text()); assert d['tokens']['access_token']=='test-access-secret'; "
                  "assert 'OPENAI_API_KEY' not in os.environ; "
                  "d['tokens']['access_token']='test-refreshed'; p.write_text(json.dumps(d)); print('isolated-ok')")
        log = io.StringIO()
        with patch.dict(os.environ, {"OPENAI_API_KEY": "default-secret", "CODEX_HOME": "/default"}):
            self.assertEqual(executor.command("DOC", [sys.executable, "-c", script], log), 0)
            self.assertEqual(os.environ["CODEX_HOME"], "/default")
        self.assertIn("isolated-ok", log.getvalue())
        self.assertEqual(json.loads((self.store.home(reference) / "auth.json").read_text())["tokens"]["access_token"], "test-refreshed")
        self.assertEqual(json.loads((self.store.home(other) / "auth.json").read_text()), AUTH)
        with patch("studio.pipeline_executor.subprocess.Popen") as popen:
            self.workspace.update_job("DOC", _codex_auth_id="a" * 64)
            with self.assertRaises(ValueError):
                executor.command("DOC", [sys.executable, "-c", "pass"], io.StringIO())
            popen.assert_not_called()


class CodexAuthHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = Workspace(self.root)
        self.server = StudioServer(("127.0.0.1", 0), self.workspace)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.temp.cleanup()

    def post(self, path, payload, **headers):
        req = Request(self.url + path, data=json.dumps(payload).encode(),
                      headers={"Content-Type": "application/json", **headers})
        with urlopen(req) as response:
            return json.load(response)

    def save(self):
        return self.post("/api/codex/auth", {"reviewer_name": "测试用户", "auth": AUTH})["credential_id"]

    def import_pdf(self, reference):
        import pymupdf
        with pymupdf.open() as pdf:
            pdf.new_page()
            content = pdf.tobytes()
        boundary = "sda-auth-test"
        parts = []
        for name, value in (("document_id", "DOC-AUTH"), ("submitted_by", "测试用户"), ("codex_auth_id", reference)):
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="pdf"; filename="DOC-AUTH.pdf"\r\nContent-Type: application/pdf\r\n\r\n'.encode() + content + b'\r\n')
        parts.append(f'--{boundary}--\r\n'.encode())
        with urlopen(Request(self.url + "/api/patents/import", data=b''.join(parts),
                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})) as response:
            return json.load(response)

    def test_save_import_queue_restart_and_public_interfaces(self):
        reference = self.save()
        response = self.import_pdf(reference)
        self.assertEqual(response["patent"]["submitted_by"], "测试用户")
        self.assertEqual(self.workspace.jobs()["DOC-AUTH"]["_codex_auth_id"], reference)
        for path in ("/api/patents", "/api/queue", "/api/reviews/queue"):
            with urlopen(self.url + path) as result:
                public = result.read().decode()
            self.assertNotIn(reference, public)
            self.assertNotIn("_codex_auth_id", public)
            self.assertNotIn("test-access-secret", public)
        task = TaskQueue(Workspace(self.root)).claim()
        self.assertEqual(task["job"]["_codex_auth_id"], reference)
        self.assertEqual(task["job"]["status"], "queued")
        self.assertNotIn(reference, json.dumps(response))

    def test_retry_can_select_other_login_or_explicit_default(self):
        first, second = self.save(), self.save()
        self.import_pdf(first)
        for reference in (second, ""):
            self.workspace.update_job("DOC-AUTH", status="failed")
            response = self.post("/api/patents/retry", {"document_id": "DOC-AUTH", "codex_auth_id": reference})
            self.assertEqual(self.workspace.jobs()["DOC-AUTH"]["_codex_auth_id"], reference or None)
            self.assertNotIn("_codex_auth_id", response["patent"])

    def test_invalid_upload_origin_size_and_private_paths(self):
        for payload in ({"reviewer_name": "测试用户", "auth": {}}, {"reviewer_name": "A", "auth": AUTH}):
            with self.assertRaises(HTTPError) as caught:
                self.post("/api/codex/auth", payload)
            self.assertEqual(caught.exception.code, 400)
            self.assertNotIn(b"test-access-secret", caught.exception.read())
        with self.assertRaises(HTTPError) as caught:
            self.post("/api/codex/auth", {"reviewer_name": "测试用户", "auth": AUTH}, Origin="https://unrelated.invalid")
        self.assertEqual(caught.exception.code, 403)
        with self.assertRaises(HTTPError) as caught:
            self.post("/api/codex/auth", {"reviewer_name": "测试用户", "auth": {"OPENAI_API_KEY": "x" * 70000}})
        self.assertEqual(caught.exception.code, 413)
        reference = self.save()
        for path in ("/auth.json", "/api/codex/auth", "/studio/codex-auth/" + reference + "/auth.json"):
            with self.assertRaises(HTTPError) as caught:
                urlopen(self.url + path)
            self.assertEqual(caught.exception.code, 404)
        with self.assertRaises(HTTPError) as caught:
            self.import_pdf("a" * 64)
        self.assertEqual(caught.exception.code, 400)
        self.assertNotIn("DOC-AUTH", self.workspace.jobs())


if __name__ == "__main__":
    unittest.main()
