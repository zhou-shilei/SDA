"""Local edits cannot bypass full validation or modify retained business records."""

import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from dataagent2.action_agent import ActionValidationError, run_action_agent, validate_action_result
from dataagent2.action_repair import (apply_replacements, build_repair_request, record_at,
                                    repair_schema)
from dataagent2.provider import Provider, response_schema
from dataagent2.test_dataagent2 import action_response
import test_action_agent as fixtures


class LocalActionRepairTests(unittest.TestCase):
    def setUp(self):
        self.data, self.chunks, self.completed, self.payload = fixtures.ActionAgentTests().example()
        self.good = action_response(self.payload)
        self.bad = copy.deepcopy(self.good)
        self.bad["dataset"]["links"][0]["seq"] = 99

    def request(self, response=None):
        response = self.bad if response is None else response
        with self.assertRaises(ActionValidationError) as error:
            validate_action_result(self.data, self.payload, response)
        return build_repair_request(self.data, self.payload, response, error.exception)

    def correction(self, request):
        replacements = []
        for row in request["editable_records"]:
            container, index = record_at(self.good, row["path"])
            if row["record"] != container[index]:
                replacements.append({"path": row["path"], "original_hash": row["original_hash"],
                    "record": copy.deepcopy(container[index]), "reason": "Restore the source-supported original fact."})
        return {"base_response_hash": request["base_response_hash"], "needs_full_regeneration": False,
                "reason": "Only the rejected records need correction.", "replacements": replacements, "appends": []}

    def test_corrected_record_is_merged_and_every_other_record_is_preserved(self):
        request = self.request()
        self.assertIn("/dataset/links/0", [r["path"] for r in request["editable_records"]])
        self.assertIn("/dataset/sequences/0", [r["path"] for r in request["readonly_related_records"]])
        self.assertTrue(all(row in self.payload["sources"] for row in request["sources"]))
        original = copy.deepcopy(self.bad)
        merged, changed = apply_replacements(self.bad, request, self.correction(request))
        self.assertEqual(self.bad, original)
        self.assertEqual(merged, self.good)
        self.assertEqual(changed, ["/dataset/links/0"])
        validate_action_result(self.data, self.payload, merged)

    def test_pipeline_uses_one_local_request_instead_of_a_second_complete_generation(self):
        calls, rejected = [], []
        testcase = self

        class Scripted:
            def generate(self, stage, instructions, payload):
                calls.append(stage)
                return (copy.deepcopy(testcase.bad) if stage == "action" else testcase.correction(payload)), {"key": stage}

            def reject_cached_response(self, key, error):
                rejected.append(key)

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            ledger, metadata = run_action_agent(self.data, self.chunks, self.completed, Scripted(), out)
            self.assertEqual(json.loads((out / "action_agent.response.json").read_text()), self.good)
            self.assertEqual(json.loads((out / "action_agent.response.before_repair.1.json").read_text()), self.bad)
            self.assertTrue(json.loads((out / "action_repair.1.validation.json").read_text())["full_validation"])
            self.assertEqual(ledger.action_metadata["status"], "validated")
        self.assertEqual(calls, ["action", "action_repair"])
        self.assertEqual(rejected, ["action"])
        self.assertEqual(metadata["attempt"], 1)
        self.assertEqual(metadata["local_repair"]["changed_paths"], ["/dataset/links/0"])

    def test_readonly_record_edit_stale_hash_duplicate_path_and_new_evidence_are_rejected(self):
        request = self.request()
        for fault in ["readonly", "response_hash", "record_hash", "duplicate", "evidence", "wrong_kind"]:
            with self.subTest(fault=fault):
                answer = self.correction(request)
                patch = answer["replacements"][0]
                if fault == "readonly":
                    patch["path"] = "/dataset/entities/0"
                elif fault == "response_hash":
                    answer["base_response_hash"] = "stale"
                elif fault == "record_hash":
                    patch["original_hash"] = "stale"
                elif fault == "duplicate":
                    answer["replacements"].append(copy.deepcopy(patch))
                elif fault == "evidence":
                    patch["record"]["refs"] = [999]
                else:
                    patch["record"] = copy.deepcopy(self.good["domain_reviews"][0])
                with self.assertRaises(ValueError):
                    apply_replacements(self.bad, request, answer)

    def test_full_retry_with_only_local_defects_gets_a_bounded_repair(self):
        calls = []
        testcase = self

        class Scripted:
            def generate(self, stage, instructions, payload):
                calls.append(stage)
                if stage == "action_repair":
                    return testcase.correction(payload), {}
                result = copy.deepcopy(testcase.bad)
                if calls.count("action") == 1:
                    result["dataset"]["entities"].pop()
                return result, {}

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            _, metadata = run_action_agent(self.data, self.chunks, self.completed, Scripted(), out)
            self.assertEqual(json.loads((out / "action_agent.response.json").read_text()), self.good)
            self.assertTrue(json.loads((out / "action_repair.2.validation.json").read_text())["full_validation"])
        self.assertEqual(calls, ["action", "action", "action_repair"])
        self.assertEqual(metadata["attempt"], 2)
        self.assertEqual(metadata["local_repair"]["changed_paths"], ["/dataset/links/0"])

    def test_plausible_but_still_ungrounded_patch_does_not_bypass_complete_validation(self):
        calls, rejected = [], []
        testcase = self

        class Scripted:
            def generate(self, stage, instructions, payload):
                calls.append(stage)
                if stage == "action":
                    return copy.deepcopy(testcase.bad if calls.count("action") == 1 else testcase.good), {"key": stage}
                result = testcase.correction(payload)
                result["replacements"][0]["record"]["seq"] = 98
                return result, {"key": "bad_patch"}

            def reject_cached_response(self, key, error):
                rejected.append(key)

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            _, metadata = run_action_agent(self.data, self.chunks, self.completed, Scripted(), out)
            self.assertEqual(json.loads((out / "action_agent.response.json").read_text()), self.good)
            self.assertEqual(json.loads((out / "action_repair.1.validation.json").read_text())["status"], "rejected")
        self.assertEqual(calls, ["action", "action_repair", "action"])
        self.assertIn("bad_patch", rejected)
        self.assertEqual(metadata["attempt"], 2)
        self.assertNotIn("local_repair", metadata)

    def test_global_seed_loss_uses_existing_full_retry_without_a_local_call(self):
        calls = []
        testcase = self

        class Scripted:
            def generate(self, stage, instructions, payload):
                calls.append(stage)
                result = copy.deepcopy(testcase.good)
                if len(calls) == 1:
                    result["dataset"]["entities"].pop()
                return result, {}

        with tempfile.TemporaryDirectory() as tmp:
            _, metadata = run_action_agent(self.data, self.chunks, self.completed, Scripted(), Path(tmp))
        self.assertEqual(calls, ["action", "action"])
        self.assertEqual(metadata["attempt"], 2)

    def test_model_can_decline_an_unsafe_local_fix_and_request_full_regeneration(self):
        calls = []
        testcase = self

        class Scripted:
            def generate(self, stage, instructions, payload):
                calls.append(stage)
                if stage == "action":
                    return copy.deepcopy(testcase.bad if calls.count("action") == 1 else testcase.good), {}
                return {"base_response_hash": payload["base_response_hash"], "needs_full_regeneration": True,
                        "reason": "This fix requires a new record and global review.", "replacements": [], "appends": []}, {}

        with tempfile.TemporaryDirectory() as tmp:
            _, metadata = run_action_agent(self.data, self.chunks, self.completed, Scripted(), Path(tmp))
        self.assertEqual(calls, ["action", "action_repair", "action"])
        self.assertEqual(metadata["attempt"], 2)

    def test_no_invalid_result_is_published_after_both_paths_fail(self):
        testcase = self
        calls = []

        class Scripted:
            def generate(self, stage, instructions, payload):
                calls.append(stage)
                if stage == "action":
                    return copy.deepcopy(testcase.bad), {}
                result = testcase.correction(payload)
                result["replacements"][0]["record"]["seq"] = 98
                return result, {}

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            with self.assertRaisesRegex(ValueError, "failed complete-dataset validation"):
                run_action_agent(self.data, self.chunks, self.completed, Scripted(), out)
            self.assertFalse((out / "action_agent.response.json").exists())
            self.assertFalse((out / "action_agent.dataset.json").exists())
        self.assertEqual(calls, ["action", "action_repair", "action", "action_repair"])

    def test_service_failure_during_local_repair_is_not_hidden_by_an_expensive_retry(self):
        calls = []
        testcase = self

        class Scripted:
            def generate(self, stage, instructions, payload):
                calls.append(stage)
                if stage == "action":
                    return copy.deepcopy(testcase.bad), {}
                raise RuntimeError("Model service circuit open")

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(RuntimeError, "service circuit open"):
                run_action_agent(self.data, self.chunks, self.completed, Scripted(), Path(tmp))
        self.assertEqual(calls, ["action", "action_repair"])

    def test_repair_schema_is_selected_for_the_actual_editable_paths(self):
        request = self.request()
        self.assertEqual(response_schema("action_repair", payload=request), repair_schema(request))
        schema = repair_schema(request)
        paths = schema["properties"]["replacements"]["items"]["properties"]["path"]["enum"]
        self.assertEqual(set(paths), {r["path"] for r in request["editable_records"]})

    def test_measurements_are_unchanged_when_repairing_sequence_links(self):
        self.data["sources"][4] = {**self.data["sources"][1], "id": 4, "text": "0.5 nM"}
        self.data["seed"]["observations"] = [{"item": "i1", "experiment": None, "metric": "IC50",
            "value": "0.5", "unit": "nM", "s": 4, "refs": [4]}]
        from dataagent2.action_agent import build_action_payload
        self.payload = build_action_payload(self.data, self.chunks, self.completed)
        self.good = action_response(self.payload)
        self.bad = copy.deepcopy(self.good)
        self.bad["dataset"]["links"][0]["seq"] = 99
        request = self.request()
        merged, _ = apply_replacements(self.bad, request, self.correction(request))
        self.assertEqual(merged["dataset"]["observations"], self.bad["dataset"]["observations"])
        validate_action_result(self.data, self.payload, merged)

    def test_valid_measurement_fields_stay_frozen_when_only_condition_is_invalid(self):
        self.data["sources"][4] = {**self.data["sources"][1], "id": 4, "text": "0.5 nM donor1"}
        self.data["seed"]["observations"] = [{"item": "i1", "experiment": None, "metric": "IC50",
            "value": "0.5", "unit": "nM", "s": 4, "refs": [4]}]
        from dataagent2.action_agent import build_action_payload
        self.payload = build_action_payload(self.data, self.chunks, self.completed)
        self.good = action_response(self.payload)
        self.bad = copy.deepcopy(self.good)
        self.bad["dataset"]["observations"][0]["condition"] = "invented"
        request = self.request()
        row = next(r for r in request["editable_records"] if r["path"] == "/dataset/observations/0")
        self.assertIn("value", row["frozen_fields"])
        self.assertIn("unit", row["frozen_fields"])
        self.assertNotIn("condition", row["frozen_fields"])
        answer = self.correction(request)
        merged, _ = apply_replacements(self.bad, request, answer)
        validate_action_result(self.data, self.payload, merged)
        # "0" is also a literal substring of 0.5 and would pass the old literal
        # checker, but changing a previously valid value is forbidden in a patch.
        answer["replacements"][0]["record"]["value"] = "0"
        with self.assertRaisesRegex(ValueError, "already passed"):
            apply_replacements(self.bad, request, answer)

    def test_fixing_sequence_number_evidence_cannot_change_grounded_residues(self):
        bad = copy.deepcopy(self.good)
        bad["dataset"]["sequences"][0]["refs"] = [3]
        request = self.request(bad)
        row = next(r for r in request["editable_records"] if r["path"] == "/dataset/sequences/0")
        self.assertEqual(row["frozen_fields"], ["parts", "value"])

    def test_printed_table_labels_have_the_original_canonical_table_registry_and_caption(self):
        self.data["sources"][4] = {**self.data["sources"][2], "id": 4,
            "source_ref": "t:caption", "kind": "text", "row": None, "column": None,
            "text": "Table 1. Binding by ELISA"}
        self.chunks[1]["core"].append(4)
        self.completed["c2"]["response"]["experiments"] = [{"id": "assay", "table": "t", "method": "ELISA",
            "target": None, "species": None, "system": None, "conditions": None, "refs": [4],
            "label": None, "protocol": None, "region_id": None}]
        from dataagent2.action_agent import build_action_payload
        self.payload = build_action_payload(self.data, self.chunks, self.completed)
        self.good = action_response(self.payload)
        self.bad = copy.deepcopy(self.good)
        self.bad["dataset"]["experiments"][0]["table"] = "Table 1"
        request = self.request()
        self.assertIn({"table_id": "t", "caption_source_ids": [4]}, request["result_tables"])
        self.assertIn(4, [s[0] for s in request["sources"]])
        merged, _ = apply_replacements(self.bad, request, self.correction(request))
        validate_action_result(self.data, self.payload, merged)

    def test_experiment_issue_scope_can_be_retained_in_a_bounded_explanatory_append(self):
        self.data["sources"][1]["text"] += " ELISA results need review."
        self.data["seed"]["experiments"] = [{"id": "e1", "table": None, "method": "ELISA", "refs": [1]}]
        from dataagent2.action_agent import build_action_payload
        self.payload = build_action_payload(self.data, self.chunks, self.completed)
        self.good = action_response(self.payload)
        self.bad = copy.deepcopy(self.good)
        issue = {"kind": "experiment", "items": ["e1"], "seq": None, "refs": [1]}
        self.bad["dataset"]["issues"].append(issue)
        request = self.request()
        row = next(r for r in request["editable_records"] if r["path"] == "/dataset/issues/0")
        answer = {"base_response_hash": request["base_response_hash"], "needs_full_regeneration": False,
            "reason": "Preserve the experiment-specific issue in a valid annotation.",
            "replacements": [{"path": row["path"], "original_hash": row["original_hash"],
                              "record": {**issue, "items": []}, "reason": "items is entity-only."}],
            "appends": [{"collection": "annotations", "original_length": len(self.bad["dataset"]["annotations"]),
                "record": {"subject_type": "experiment", "subject": "e1", "category": "other",
                           "value": "ELISA results need review.", "refs": [1]},
                "reason": "Keep the affected experiment and its review scope."}]}
        merged, changed = apply_replacements(self.bad, request, answer)
        validate_action_result(self.data, self.payload, merged)
        self.assertIn("/dataset/annotations/0", changed)
        self.assertEqual(merged["dataset"]["observations"], self.bad["dataset"]["observations"])
        self.assertEqual(merged["dataset"]["sequences"], self.bad["dataset"]["sequences"])

    def test_appends_cannot_add_measurements_or_unrelated_ownership_claims(self):
        request = self.request()
        for fault in ["measurement", "unrelated", "wrong_length", "unknown_sequence"]:
            with self.subTest(fault=fault):
                answer = self.correction(request)
                addition = {"collection": "annotations", "original_length": 0,
                    "record": {"subject_type": "item", "subject": "i2", "category": "other",
                               "value": "unrelated", "refs": [1]}, "reason": "Test"}
                if fault == "measurement":
                    addition["collection"] = "observations"
                elif fault == "wrong_length":
                    addition["original_length"] = 99
                elif fault == "unknown_sequence":
                    addition.update(collection="issues", record={"kind": "other", "items": [], "seq": 999, "refs": [1]})
                answer["appends"] = [addition]
                with self.assertRaises(ValueError):
                    apply_replacements(self.bad, request, answer)

    def test_repair_uses_action_effort_and_records_its_parent_phase(self):
        request = self.request()
        request["parent_stage"] = "answer_action"
        with tempfile.TemporaryDirectory() as tmp:
            proc = MagicMock(returncode=1)
            proc.communicate.return_value = ("", "scripted failure")
            with patch("dataagent2.provider.subprocess.Popen", return_value=proc) as start:
                provider = Provider(tmp, effort="xhigh", action_effort="medium")
                with self.assertRaisesRegex(RuntimeError, "scripted failure"):
                    provider.generate("action_repair", "test", request)
            self.assertIn('model_reasoning_effort="medium"', start.call_args.args[0])
            self.assertTrue(all(c["parent_stage"] == "answer_action" for c in provider.calls))
            self.assertIn("action_repair", provider.usage()["by_stage"])

    def test_historical_request_timing_includes_repair_in_the_parent_phase(self):
        from dataagent2.timing import request_coverage
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            calls = [{"attempt_id": "action", "stage": "action", "started_at_unix": 100, "seconds": 10},
                     {"attempt_id": "repair", "stage": "action_repair", "parent_stage": "action",
                      "started_at_unix": 111, "seconds": 2}]
            (root / "calls.jsonl").write_text("\n".join(json.dumps(c) for c in calls) + "\n")
            rows = request_coverage(root)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["stage"], "dataagent2_action")
        self.assertEqual(rows[0]["seconds"], 12)

    def test_oversized_context_skips_the_extra_call(self):
        self.data["sources"][1]["text"] += " " * 400_000
        from dataagent2.action_agent import build_action_payload
        self.payload = build_action_payload(self.data, self.chunks, self.completed)
        with self.assertRaisesRegex(ValueError, "context is too broad"):
            self.request()


if __name__ == "__main__":
    unittest.main()
