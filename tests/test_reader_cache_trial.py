"""Cache experiments must keep literal evidence and unknown usage intact."""

import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "try" / "agent_handoff"
sys.path.insert(0, str(SCRIPT_DIR))
import benchmark_reader_cache as trial
from dataagent2.provider import event_usage
from dataagent2.test_dataagent2 import empty_response


class ReaderCacheTrialTests(unittest.TestCase):
    def payload(self):
        return {"document": "专利", "chunk": "c1", "initial_dataset": {
                "entities": [{"id": "i2", "name": "AB-2"}, {"id": "i1", "name": "AB-1"}],
                "observations": [{"value": "0.0010", "unit": None, "refs": [9, 1]}]},
            "tag_categories": ["b", "a"], "tag_vocabulary": {"x": ["z", "y"]},
            "sources": [[9, 2, "cell", "t", 0, 3, "AA\n BB <br> CC"],
                        [1, 1, "text", None, None, None, " 中文 "]],
            "context_only_sources": [9],
            "source_sequence_ocr": {9: {"status": "failed", "error": "Incomplete crop"}},
            "source_image_styles": {1: {"spans": [{"start": 1, "end": 3}]}},
            "future_metadata": {"empty": [], "null": None}}

    def test_layout_preserves_every_literal_field_and_array_order_without_mutation(self):
        payload = self.payload()
        before = copy.deepcopy(payload)
        for mode in trial.MODES:
            with self.subTest(mode=mode):
                developer, prompt = trial.layout(payload, mode)
                if developer:
                    shared = json.loads(developer.split("\nSHARED_INPUT_JSON:\n", 1)[1])
                    local = json.loads(prompt.split("INPUT_JSON:\n", 1)[1])
                    self.assertFalse(shared.keys() & local.keys())
                    decoded = {**shared, **local}
                else:
                    decoded = json.loads(prompt.split("\nINPUT_JSON:\n", 1)[1])
                self.assertEqual(decoded, json.loads(trial.compact(before)))
                self.assertEqual(payload, before)

    def test_different_chunks_keep_identical_shared_developer_message(self):
        a = self.payload()
        b = copy.deepcopy(a)
        b.update(chunk="c2", sources=[[55, 3, "text", None, None, None, "Other evidence"]])
        da, ua = trial.layout(a, "shared_developer")
        db, ub = trial.layout(b, "shared_developer")
        self.assertEqual(da, db)
        self.assertNotEqual(ua, ub)
        a["initial_dataset"]["observations"][0]["value"] = "0.0020"
        self.assertNotEqual(trial.layout(a, "shared_developer")[0], da)

    def test_missing_cache_measurement_stays_unknown(self):
        rows = [{"status": "ok", "seconds": 1, "usage": event_usage(json.dumps({
            "type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 20}}))}]
        usage = trial.aggregate(rows)
        self.assertEqual(usage["input_tokens"], 100)
        self.assertIsNone(usage["cached_input_tokens"])
        self.assertIsNone(usage["cache_read_fraction"])
        self.assertIsNone(usage["non_read_input_tokens"])
        self.assertEqual(usage["unknown_cache_requests"], 1)
        rows.append({"status": "failed", "seconds": 2, "usage": event_usage("")})
        self.assertIsNone(trial.aggregate(rows)["input_tokens"])
        self.assertEqual(trial.aggregate(rows)["unknown_usage_requests"], 1)

    def test_fork_usage_subtracts_parent_once_and_preserves_unknowns(self):
        parent = {"input_tokens": 30000, "cached_input_tokens": 0,
            "output_tokens": 100, "reasoning_output_tokens": 50, "total_tokens": 30100}
        terminal = {"input_tokens": 70000, "cached_input_tokens": 33000,
            "output_tokens": 2100, "reasoning_output_tokens": 1050, "total_tokens": 72100}
        row = {"usage": terminal}
        result = trial.account_fork(row, parent)
        self.assertEqual(result["usage"]["input_tokens"], 40000)
        self.assertEqual(result["usage"]["output_tokens"], 2000)
        self.assertEqual(result["usage"]["cached_input_tokens"], 33000)
        self.assertEqual(result["usage"]["total_tokens"], 42000)
        self.assertEqual(trial.account_fork(result, parent), result)
        unknown = trial.incremental_usage({**terminal, "cached_input_tokens": None}, parent)
        self.assertIsNone(unknown["cached_input_tokens"])
        with self.assertRaisesRegex(ValueError, "below the parent"):
            trial.incremental_usage({**terminal, "input_tokens": 1}, parent)

    def test_extension_retains_original_inputs_and_rejects_changed_settings(self):
        old = {"model": "same", "chunks": ["c1"], "payload_hashes": {"c1": "hash1"}}
        new = {"model": "same", "chunks": ["c1", "c2"],
               "payload_hashes": {"c1": "hash1", "c2": "hash2"}}
        with self.assertRaisesRegex(ValueError, "Protocol changed"):
            trial.check_protocol(old, new)
        trial.check_protocol(old, new, extend=True)
        for changed in [{**new, "model": "different"},
                        {**new, "chunks": ["c2", "c1"]},
                        {**new, "payload_hashes": {"c1": "changed", "c2": "hash2"}}]:
            with self.subTest(changed=changed), self.assertRaisesRegex(ValueError, "Protocol changed"):
                trial.check_protocol(old, changed, extend=True)

    def test_live_call_has_audit_and_restart_retains_original_usage(self):
        events = json.dumps({"type": "turn.completed", "usage": {
            "input_tokens": 100, "cached_input_tokens": 80, "output_tokens": 20,
            "reasoning_output_tokens": 5}})
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            trial.write_json(folder / "schema.json", trial.SCHEMA)
            proc = MagicMock(returncode=0)
            def communicate(prompt, timeout):
                trial.write_json(folder / "requests/c1/answer.json", empty_response())
                return events, ""
            proc.communicate.side_effect = communicate
            with patch.object(trial.subprocess, "Popen", return_value=proc) as popen:
                row, response = trial.one_request(self.payload(), "shared_developer", folder,
                    "gpt-6.1-sol", "xhigh", 60)
                args = popen.call_args.args[0]
                self.assertIn("--ignore-user-config", args)
                self.assertIn("multi_agent", args)
                self.assertEqual(args[args.index("--cd") + 1], str(folder / "workspace"))
                self.assertTrue(any(v.startswith("developer_instructions=") for v in args))
                self.assertEqual(row["non_read_input_tokens"], 20)
                self.assertEqual(response, empty_response())
                self.assertTrue((folder / "requests/c1/events.jsonl").is_file())
                again, _ = trial.one_request(self.payload(), "shared_developer", folder,
                    "gpt-6.1-sol", "xhigh", 60)
                self.assertEqual(again, row)
                self.assertEqual(popen.call_count, 1)
                changed = self.payload()
                changed["sources"][0][-1] = "Changed evidence"
                with self.assertRaisesRegex(ValueError, "request changed"):
                    trial.one_request(changed, "shared_developer", folder, "gpt-6.1-sol", "xhigh", 60)

    def test_fork_keeps_parent_persistent_and_reconstructs_original_payload(self):
        events = '\n'.join([json.dumps({"type": "thread.started", "thread_id": "parent-id"}),
            json.dumps({"type": "turn.completed", "usage": {"input_tokens": 100,
                "cached_input_tokens": 80, "output_tokens": 20, "reasoning_output_tokens": 5}})])
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            trial.write_json(folder / "schema.json", trial.SCHEMA)
            proc = MagicMock(returncode=0)
            def communicate(prompt, timeout):
                command = popen.call_args.args[0]
                trial.write_json(Path(command[command.index("--output-last-message") + 1]), empty_response())
                if "fork" in command:
                    return '\n'.join([json.dumps({"type": "thread.started", "thread_id": "child-id"}),
                        json.dumps({"type": "turn.completed", "usage": {"input_tokens": 150,
                            "cached_input_tokens": 80, "output_tokens": 40, "reasoning_output_tokens": 20}})]), ""
                return events, ""
            proc.communicate.side_effect = communicate
            with patch.object(trial.subprocess, "Popen", return_value=proc) as popen:
                p = self.payload()
                setup = {**{k: p[k] for k in trial.SHARED_KEYS}, "chunk": "setup", "sources": []}
                row, _ = trial.one_request(setup, "shared_fork", folder, "gpt-6.1-sol", "xhigh", 60,
                    bootstrap=True)
                self.assertEqual(row["thread_id"], "parent-id")
                self.assertNotIn("--ephemeral", popen.call_args.args[0])
                child, _ = trial.one_request(p, "shared_fork", folder, "gpt-6.1-sol", "xhigh", 60,
                    parent=row["thread_id"], parent_usage=row["usage"])
                self.assertEqual(child["usage"]["input_tokens"], 50)
                self.assertEqual(child["usage"]["output_tokens"], 20)
                command = popen.call_args.args[0]
                self.assertEqual(command[-3:], ["fork", "parent-id", "-"])
                self.assertIn("--ephemeral", command)
                shared = json.loads((folder / "requests/setup/prompt.txt").read_text().split("\nSHARED_INPUT_JSON:\n", 1)[1])
                local = json.loads((folder / "requests/c1/prompt.txt").read_text().split("\nINPUT_JSON:\n", 1)[1])
                self.assertEqual({**shared, **local}, json.loads(trial.compact(p)))
                self.assertEqual(popen.call_count, 2)


if __name__ == "__main__":
    unittest.main()
