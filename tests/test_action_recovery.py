"""Recovery must reuse exact evidence and never reread a document on a cache miss."""

import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dataagent2.inputs import make_chunks
from dataagent2.pipeline import reader_request
from dataagent2.provider import Provider, digest, write_json
from dataagent2.recover import RecoveryProvider, prepare_recovery, reader_fingerprint, recover, restore_validated_action
from dataagent2.test_dataagent2 import action_response, fixture, response


class ActionRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.source = Path(temporary.name) / "failed"
        self.output = Path(temporary.name) / "recovery"
        self.data = fixture()
        self.manifest = {"document_id": "test", "pdf_sha256": "test", "native_input": "unused",
                         "model": "gpt-6.1-sol", "effort": "xhigh", "chunk_chars": 24000,
                         "max_questions": 0, "chunks": make_chunks(self.data, 24000)}
        write_json(self.source / "manifest.json", self.manifest)
        write_json(self.source / "action_agent.json", {"status": "failed", "error": "invalid literal sequence"})
        self.folders = []
        for chunk in self.manifest["chunks"]:
            payload, instructions = reader_request(self.data, chunk)
            fingerprint = reader_fingerprint(instructions, payload, self.manifest)
            folder = self.source / "requests" / digest(fingerprint)
            write_json(folder / "input.json", fingerprint)
            write_json(folder / "validated.json", response())
            self.folders.append(folder)
        self.addCleanup(patch.stopall)
        patch("dataagent2.recover.load_input", side_effect=lambda *_: copy.deepcopy(self.data)).start()

    def test_dry_run_verifies_all_readers_and_preserves_original_artifacts(self):
        originals = {p: p.read_bytes() for p in self.source.rglob("*.json")}
        with patch("dataagent2.provider.subprocess.Popen", side_effect=AssertionError("Unexpected model call")):
            plan = recover(self.source, self.output, dry_run=True)
        self.assertEqual(plan["reused_reader_count"], len(self.manifest["chunks"]))
        self.assertIn("invalid literal sequence", plan["feedback"])
        self.assertEqual({p: p.read_bytes() for p in originals}, originals)
        self.assertTrue((self.output / "recovery.json").is_file())
        self.assertFalse((self.output / "calls.jsonl").exists())

    def test_missing_corrupt_or_changed_reader_fails_before_a_model_call(self):
        for fault in ("missing", "fingerprint", "response", "document", "evidence"):
            with self.subTest(fault=fault):
                folder = self.folders[0]
                originals = {p: p.read_bytes() for p in folder.glob("*.json")}
                data = copy.deepcopy(self.data)
                try:
                    if fault == "missing":
                        (folder / "validated.json").unlink()
                    elif fault == "fingerprint":
                        write_json(folder / "input.json", {"unrelated": True})
                    elif fault == "response":
                        write_json(folder / "validated.json", {"invalid": True})
                    elif fault == "document":
                        self.data["pdf_sha256"] = "another-pdf"
                    else:
                        self.data["sources"][1]["text"] += " Changed evidence."
                    with patch("dataagent2.provider.subprocess.Popen", side_effect=AssertionError("Unexpected model call")):
                        with self.assertRaises(ValueError):
                            recover(self.source, self.output)
                    self.assertFalse(self.output.exists())
                finally:
                    self.data = data
                    for path, content in originals.items():
                        path.write_bytes(content)

    def test_cache_changed_after_preflight_cannot_trigger_a_new_reader_call(self):
        _, manifest, plan = prepare_recovery(self.source)
        provider = RecoveryProvider(self.output, self.source, manifest, plan)
        (self.folders[0] / "validated.json").unlink()
        payload, instructions = reader_request(self.data, self.manifest["chunks"][0])
        with patch("dataagent2.provider.subprocess.Popen", side_effect=AssertionError("Unexpected model call")):
            with self.assertRaisesRegex(ValueError, "cache changed after preflight"):
                provider.generate("reader", instructions, payload)

    def test_recovery_transport_failure_is_not_silently_retried(self):
        _, manifest, plan = prepare_recovery(self.source)
        provider = RecoveryProvider(self.output, self.source, manifest, plan)
        with patch("dataagent2.provider.subprocess.Popen", side_effect=OSError("scripted service failure")) as call:
            with self.assertRaisesRegex(RuntimeError, "scripted service failure"):
                provider.generate("answer", "test", {})
        self.assertEqual(call.call_count, 1)
        command = call.call_args.args[0]
        self.assertIn('model_provider="sda_http"', command)
        self.assertIn('model_providers.sda_http.supports_websockets=false', command)
        self.assertEqual(provider.usage()["unknown_requests"], 1)

    def test_only_consolidation_is_generated_and_full_validation_still_runs(self):
        original_generate = Provider.generate
        new_calls = []

        def scripted(provider, stage, instructions, payload):
            if stage == "reader":
                return original_generate(provider, stage, instructions, payload)
            new_calls.append(stage)
            self.assertIn("invalid literal sequence", payload["validation_errors"][0])
            return action_response(payload), {}

        with patch.object(Provider, "generate", new=scripted):
            report = recover(self.source, self.output)
        self.assertTrue(report["benchmark_complete"])
        self.assertEqual(new_calls, ["action"])
        self.assertEqual(report["new_model_usage"]["reused_reader_requests"], len(self.manifest["chunks"]))

    def test_existing_or_overlapping_output_is_rejected(self):
        self.output.mkdir()
        for output in (self.source, self.source / "child", self.source.parent, self.output):
            with self.subTest(output=output), self.assertRaises(ValueError):
                recover(self.source, output, dry_run=True)

    def test_http_resume_reuses_a_fully_validated_action_and_preserves_its_archive(self):
        from dataagent2.action_agent import ACTION_INSTRUCTIONS, SCHEMA_ACTION, build_action_payload
        from dataagent2.inputs import read
        plan = recover(self.source, self.output, dry_run=True)
        plan.update(status="failed", error="Previous followup service failure")
        write_json(self.output / "recovery.json", plan)
        completed = {chunk["id"]: {"response": response()} for chunk in self.manifest["chunks"]}
        payload = build_action_payload(self.data, self.manifest["chunks"], completed)
        canonical = action_response(payload)
        request = {"stage": "action", "instructions": ACTION_INSTRUCTIONS,
                   "payload": {**payload, "validation_errors": [plan["feedback"]]},
                   "schema": SCHEMA_ACTION, "model": self.manifest["model"], "effort": "high"}
        key = digest(request)
        write_json(self.output / "requests" / key / "input.json", request)
        write_json(self.output / "action_agent.input.json", payload)
        write_json(self.output / "action_agent.response.json", canonical)
        write_json(self.output / "action_agent.json", {"status": "validated", "input_hash": digest(payload),
                   "response_hash": digest(canonical), "provider": {"key": key}})
        with patch("dataagent2.provider.subprocess.Popen", side_effect=AssertionError("Unexpected model call")):
            report = recover(self.source, self.output, resume=True)
        self.assertTrue(report["benchmark_complete"])
        archive = next((self.output / "resume_archive").iterdir())
        self.assertEqual(read(archive / "action_agent.response.json"), canonical)
        audit = read(self.output / "requests" / key / "completed_action_cache.json")
        self.assertTrue(audit["full_validation"])
        self.assertEqual(audit["response_hash"], digest(canonical))
        # A matching hash cannot certify a modified sequence: full validation still runs.
        bad = copy.deepcopy(canonical)
        bad["dataset"]["sequences"][0]["value"] = "A"
        write_json(self.output / "action_agent.response.json", bad)
        meta = read(self.output / "action_agent.json")
        meta["response_hash"] = digest(bad)
        write_json(self.output / "action_agent.json", meta)
        with self.assertRaisesRegex(ValueError, "changed literal sequence"):
            restore_validated_action(self.output, self.data)

    def test_invalid_new_sequence_still_fails_complete_validation(self):
        original_generate = Provider.generate
        calls = []

        def scripted(provider, stage, instructions, payload):
            if stage == "reader":
                return original_generate(provider, stage, instructions, payload)
            calls.append(stage)
            if stage == "action_repair":
                raise ValueError("No source-supported correction returned")
            result = action_response(payload)
            result["dataset"]["sequences"][0]["value"] = "A"
            return result, {}

        with patch.object(Provider, "generate", new=scripted):
            report = recover(self.source, self.output)
        self.assertTrue(report["reader_complete"])
        self.assertFalse(report["action_complete"])
        self.assertFalse(report["benchmark_complete"])
        self.assertEqual(calls.count("action"), 2)
        self.assertIn("changed literal sequence characters", report["action_agent"]["error"])


if __name__ == "__main__":
    unittest.main()
