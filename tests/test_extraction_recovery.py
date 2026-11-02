"""Recoveries retain exact evidence and expose the actual failed stage."""

import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dataagent2.contracts import INSTRUCTIONS, SCHEMA, empty_response
from dataagent2.diagnostics import failure_details
from dataagent2.provider import Provider, digest, write_json


class ReaderReuseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.old, self.new = Path(temporary.name) / "old", Path(temporary.name) / "new"
        self.old.mkdir()
        self.payload = {"document": "DOC", "sources": [[1, "original OCR"]],
                        "source_sequence_ocr": {1: {"status": "success"}}}
        self.fingerprint = {"stage": "reader", "instructions": INSTRUCTIONS, "payload": self.payload,
                            "schema": SCHEMA, "model": "gpt-6.1-sol", "effort": "xhigh"}
        self.key = digest(self.fingerprint)
        self.folder = self.old / "requests" / self.key
        write_json(self.folder / "input.json", self.fingerprint)
        write_json(self.folder / "validated.json", empty_response())

    def test_exact_reader_is_reused_without_new_usage_and_retains_provenance(self):
        provider = Provider(self.new, reuse_readers_from=self.old)
        original = (self.folder / "validated.json").read_bytes()
        with patch("dataagent2.provider.subprocess.Popen", side_effect=AssertionError("Unexpected model call")):
            result, meta = provider.generate("reader", INSTRUCTIONS, self.payload)
            provider.generate("reader", INSTRUCTIONS, self.payload)
        self.assertEqual(result, empty_response())
        self.assertEqual(meta["reused_from"], str(self.folder))
        self.assertEqual(provider.usage()["requests"], 0)
        self.assertEqual(provider.usage()["reused_reader_requests"], 1)
        self.assertEqual((self.folder / "validated.json").read_bytes(), original)
        resumed = Provider(self.new)
        self.assertEqual(resumed.usage()["reused_reader_requests"], 1)
        copied = json.loads((self.new / "requests" / self.key / "input.json").read_text())
        self.assertEqual(digest(copied), digest(self.fingerprint))

    def test_changed_evidence_model_effort_or_instructions_cannot_reuse_old_reader(self):
        changed = copy.deepcopy(self.payload)
        changed["sources"][0][1] = "different OCR"
        for options, prompt, payload in [({}, INSTRUCTIONS, changed), ({"model": "other-model"}, INSTRUCTIONS, self.payload),
                                         ({"effort": "medium"}, INSTRUCTIONS, self.payload), ({}, "changed prompt", self.payload)]:
            with self.subTest(options=options, prompt=prompt[:15]):
                provider = Provider(self.new, reuse_readers_from=self.old, **options)
                with patch.object(provider, "admit", side_effect=RuntimeError("New request required")):
                    with self.assertRaisesRegex(RuntimeError, "New request required"):
                        provider.generate("reader", prompt, payload)
                self.assertEqual(provider.usage()["reused_reader_requests"], 0)

    def test_corrupt_request_or_response_cannot_be_imported(self):
        for name, value in [("input.json", {"different": True}), ("validated.json", {"invalid": True})]:
            with self.subTest(name=name):
                write_json(self.folder / "input.json", self.fingerprint)
                write_json(self.folder / "validated.json", empty_response())
                write_json(self.folder / name, value)
                with patch("dataagent2.provider.subprocess.Popen", side_effect=AssertionError("Unexpected model call")):
                    with self.assertRaises(ValueError):
                        Provider(self.new, reuse_readers_from=self.old).generate("reader", INSTRUCTIONS, self.payload)
                self.assertFalse((self.new / "requests" / self.key / "validated.json").exists())

    def test_recovery_usage_cannot_claim_a_full_run_token_reduction(self):
        from dataagent2.pipeline import run
        from dataagent2.test_dataagent2 import action_response, fixture, response

        class RecoveryProvider:
            model, effort = "test", "test"

            def generate(self, stage, instructions, payload):
                return (action_response(payload) if stage == "action" else response()), {}

            def usage(self):
                return {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120,
                        "requests": 1, "unknown_requests": 0, "reused_reader_requests": 1}

        report = run(fixture(), RecoveryProvider(), self.new, max_questions=0)
        self.assertTrue(report["benchmark_complete"])
        self.assertEqual(report["new_model_usage"]["total_tokens"], 120)
        self.assertIsNone(report["comparable_model_total_tokens"])
        self.assertIsNone(report["known_token_reduction_fraction"])


class FailureDiagnosticsTests(unittest.TestCase):
    def test_action_failure_explains_real_validation_errors_and_preserves_paths(self):
        result = failure_details({"reader_complete": True, "action_complete": False,
            "coverage": {"completed_chunks": 11, "total_chunks": 11},
            "action_agent": {"error": "Sequence anchors absent or ambiguous\nDomain completeness review errors",
                             "validation_paths": ["/dataset/sequences/47", "/domain_reviews/34"]}})
        self.assertEqual(result["stage"], "action")
        self.assertIn("序列片段无法唯一定位原文", result["message"])
        self.assertIn("域复核引用或组装结果不一致", result["message"])
        self.assertEqual(result["reader_completed_chunks"], 11)
        self.assertEqual(len(result["validation_paths"]), 2)

    def test_reader_answer_and_completed_results_are_distinguished(self):
        result = failure_details({"reader_complete": False,
            "coverage": {"completed_chunks": 2, "total_chunks": 3, "failed_chunks": {"c3": "timeout"}}})
        self.assertEqual(result["stage"], "reader")
        self.assertIn("2/3", result["message"])
        self.assertIn("c3: timeout", result["error"])
        self.assertEqual(failure_details({"reader_complete": True, "action_complete": True,
                                         "question_api_failures": 1})["stage"], "questions")
        self.assertEqual(failure_details({"reader_complete": True, "action_complete": True,
            "answer_action": {"error": "integration rejected"}})["stage"], "answer_action")
        self.assertIsNone(failure_details({"benchmark_complete": True}))


if __name__ == "__main__":
    unittest.main()
