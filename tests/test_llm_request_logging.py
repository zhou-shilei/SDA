"""Request logs expose waits and failures without changing inference results."""

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dataagent import llm_runtime as runtime
from dataagent.llm_io import ConnectivityResult
from dataagent.llm_table_agents import LLMTemplateAgent


class RequestLoggingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = patch.dict(os.environ, {"SDA_CACHE_ENABLED": "0"})
        environment.start()
        self.addCleanup(environment.stop)
        self.provider = runtime.CodexProvider(self.root / "cache", audit_path=self.root / "llm_calls.jsonl")

    def process(self, command, **kwargs):
        output = Path(command[command.index("--output-last-message") + 1])
        class Process:
            returncode = 0
            def communicate(self, prompt, timeout=None):
                output.write_text('{"status":"ok"}')
                return json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10}}), ""
        return Process()

    def request(self):
        return self.provider.generate("document-template-test", "Return ok", {"private": "source text"}, ConnectivityResult)

    def events(self, output):
        return [json.loads(line.removeprefix("llm-request ")) for line in output.getvalue().splitlines()
                if line.startswith("llm-request ")]

    def test_waiting_is_visible_and_stops_after_response(self):
        waiting = threading.Event()
        original_log = runtime.log_llm_event
        def log(event, **details):
            original_log(event, **details)
            if event == "waiting":
                waiting.set()
        def process(command, **kwargs):
            base = self.process(command, **kwargs)
            communicate = base.communicate
            def delayed(prompt, timeout=None):
                self.assertTrue(waiting.wait(2), "No heartbeat while request was blocked")
                return communicate(prompt, timeout)
            base.communicate = delayed
            return base
        output = io.StringIO()
        with redirect_stdout(output), patch.object(runtime, "HEARTBEAT_SECONDS", .01), \
                patch.object(runtime, "log_llm_event", side_effect=log), \
                patch.object(runtime.subprocess, "Popen", process):
            result, metadata = self.request()
        events = self.events(output)
        self.assertEqual(result, {"status": "ok"})
        self.assertEqual(metadata["usage"]["input_tokens"], 10)
        self.assertEqual(events[0]["event"], "request_start")
        self.assertIn("waiting", [e["event"] for e in events])
        self.assertEqual(events[-1]["event"], "request_complete")
        self.assertEqual(len({e["input_hash"] for e in events}), 1)
        self.assertNotIn("source text", output.getvalue())
        self.assertFalse(any(t.name == "llm-request-heartbeat" for t in threading.enumerate()))

    def test_capacity_retry_logs_reason_and_backoff_before_success(self):
        calls = []
        def process(command, **kwargs):
            calls.append(command)
            if len(calls) == 1:
                failed = Mock(returncode=1)
                failed.communicate.return_value = (json.dumps({"type": "turn.failed", "error": {
                    "message": "429 Too Many Requests Bearer private-token"}}), "")
                return failed
            return self.process(command, **kwargs)
        output = io.StringIO()
        with redirect_stdout(output), patch.object(runtime.subprocess, "Popen", process), \
                patch.object(runtime.time, "sleep") as sleep:
            _, metadata = self.request()
        events = self.events(output)
        retry = next(e for e in events if e["event"] == "retry_scheduled")
        self.assertEqual((retry["error_kind"], retry["delay_seconds"], retry["next_attempt"]), ("capacity", 5, 2))
        sleep.assert_called_once_with(5)
        self.assertEqual(metadata["retry_count"], 1)
        self.assertNotIn("private-token", output.getvalue())
        self.assertEqual(events[-1]["event"], "request_complete")

    def test_access_failure_is_logged_and_propagated(self):
        process = Mock(returncode=1)
        process.communicate.return_value = (json.dumps({"type": "turn.failed", "error": {"message": "401 unauthorized"}}), "")
        output = io.StringIO()
        with redirect_stdout(output), patch.object(runtime.subprocess, "Popen", return_value=process):
            with self.assertRaises(runtime.CodexUnavailable):
                self.request()
        last = self.events(output)[-1]
        self.assertEqual((last["event"], last["error_kind"]), ("request_failed", "access"))
        self.assertFalse(any(t.name == "llm-request-heartbeat" for t in threading.enumerate()))

    def test_timeout_stops_heartbeat_and_preserves_timeout_failure(self):
        process = Mock(pid=123456, returncode=-9)
        process.communicate.side_effect = [subprocess.TimeoutExpired("codex", 600), ("", "")] * 2
        output = io.StringIO()
        with redirect_stdout(output), patch.object(runtime.subprocess, "Popen", return_value=process), \
                patch.object(runtime.os, "killpg") as kill, patch.object(runtime.time, "sleep"):
            with self.assertRaises(runtime.CodexUnavailable):
                self.request()
        self.assertEqual(kill.call_count, 2)
        self.assertEqual(self.events(output)[-1]["error_kind"], "timeout")
        self.assertFalse(any(t.name == "llm-request-heartbeat" for t in threading.enumerate()))

    def test_template_logs_preparation_counts_and_final_result(self):
        provider = Mock(audit_path=None)
        def generate(task, instructions, payload, contract, **kwargs):
            result = {"document_id": "DOC", "entities": [], "sequence_links": [], "assay_profiles": [], "unresolved": []}
            kwargs["validate"](result)
            return result, {"status": "validated", "cache_hit": False}
        provider.generate.side_effect = generate
        output = io.StringIO()
        with redirect_stdout(output):
            template, validation = LLMTemplateAgent(provider).run("DOC", [])
        events = self.events(output)
        self.assertEqual([e["event"] for e in events], ["template_prepare_start", "template_input_ready", "template_complete"])
        self.assertEqual(events[1]["table_count"], 0)
        self.assertEqual(events[-1]["entity_count"], validation["entity_count"])
        self.assertEqual(events[-1]["status"], template["status"])
        self.assertLess(output.getvalue().index("llm-template-start"), output.getvalue().index("template_prepare_start"))


if __name__ == "__main__":
    unittest.main()
