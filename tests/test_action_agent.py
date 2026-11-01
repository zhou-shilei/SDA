"""Complete-dataset consolidation must preserve evidence and account for every proposal."""

import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dataagent2.action_agent import (ACTION_INSTRUCTIONS, SCHEMA_ACTION, build_action_payload,
                                    run_action_agent, validate_action_result, encode_action_payload, decode_action_payload)
from dataagent2.contracts import empty_response
from dataagent2.contracts import check_shape
from dataagent2.action_record_refs import SCHEMA_ACTION_RECORD_REFS, expand_record_refs, record_reference_schema
from dataagent2.pipeline import run
from dataagent2.provider import Provider, digest, response_schema, write_json
from dataagent2.test_dataagent2 import SEQUENCE, action_response, fixture, response


class ActionAgentTests(unittest.TestCase):
    def referenced_example(self):
        data, chunks, completed, payload = self.example()
        canonical = action_response(payload)
        sequence = next(a for a in payload["reader_actions"] if a["kind"] == "sequences")
        # The complete flat counterpart uses the Reader's exact anchors;
        # Ledger's quote-normalized anchors represent the same source span.
        canonical["dataset"]["sequences"][0]["parts"] = copy.deepcopy(sequence["fact"]["parts"])
        wire = copy.deepcopy(canonical)
        wire["dataset"]["entities"][0] = {"seed_path": "/initial_dataset/entities/0"}
        wire["dataset"]["sequences"][0] = {"action_id": sequence["id"]}
        return data, chunks, completed, payload, canonical, wire

    def test_explicit_record_references_restore_the_complete_dataset_without_changing_indices(self):
        data, _, _, payload, canonical, wire = self.referenced_example()
        original = copy.deepcopy(wire)
        check_shape(wire, SCHEMA_ACTION_RECORD_REFS)
        expanded = expand_record_refs(wire, payload, data)
        self.assertEqual(expanded, canonical)
        self.assertEqual(wire, original)
        ledger = validate_action_result(data, payload, expanded)
        self.assertEqual(ledger.assemble(), validate_action_result(data, payload, canonical).assemble())
        self.assertEqual(response_schema("action", action_record_refs=True), SCHEMA_ACTION_RECORD_REFS)

    def test_reference_cannot_copy_a_fact_from_another_kind_or_an_unknown_action(self):
        data, _, _, payload, _, wire = self.referenced_example()
        wire["dataset"]["sequences"][0] = {"seed_path": "/initial_dataset/entities/0"}
        with self.assertRaisesRegex(ValueError, "same|kind"):
            expand_record_refs(wire, payload, data)
        wire["dataset"]["sequences"][0] = {"action_id": "unknown"}
        with self.assertRaisesRegex(ValueError, "known.*action"):
            expand_record_refs(wire, payload, data)

    def test_numeric_action_positions_expand_to_the_original_ids_without_changing_sources(self):
        data, _, _, payload, canonical, wire = self.referenced_example()
        positions = {a["id"]: i for i, a in enumerate(payload["reader_actions"])}
        wire["dataset"]["sequences"][0]["action_id"] = positions[wire["dataset"]["sequences"][0]["action_id"]]
        for decision in wire["action_decisions"]:
            decision["action_ids"] = [positions[a] for a in decision["action_ids"]]
        check_shape(wire, SCHEMA_ACTION_RECORD_REFS)
        self.assertEqual(expand_record_refs(wire, payload, data), canonical)
        wire["dataset"]["sequences"][0]["action_id"] = 99999
        with self.assertRaisesRegex(ValueError, "outside"):
            expand_record_refs(wire, payload, data)

    def test_complete_seed_collection_and_explicit_collection_targets_expand_exactly(self):
        data, _, _, payload, canonical, wire = self.referenced_example()
        wire["dataset"]["entities"] = [{"seed_collection": "all", "exclude": []}]
        wire["action_decisions"][0]["targets"] = ["/dataset/links"]
        check_shape(wire, record_reference_schema(payload))
        self.assertEqual(expand_record_refs(wire, payload, data), canonical)
        wire["dataset"]["entities"][0]["exclude"] = [999]
        with self.assertRaisesRegex(ValueError, "exclusion"):
            expand_record_refs(wire, payload, data)

    def test_request_specific_schema_disallows_nonexistent_seed_and_action_references(self):
        _, _, _, payload, _, wire = self.referenced_example()
        schema = record_reference_schema(payload)
        wire["dataset"]["entities"][0] = {"seed_path": "/initial_dataset/entities/2"}
        with self.assertRaises(ValueError):
            check_shape(wire, schema)
        wire["dataset"]["entities"][0] = {"seed_path": "/initial_dataset/entities/0"}
        wire["dataset"]["sequences"][0] = {"action_id": 999}
        with self.assertRaises(ValueError):
            check_shape(wire, schema)

    def test_seed_change_collection_targets_expand_like_decision_collection_targets(self):
        data, chunks, completed, _ = self.example()
        data["sources"][4] = {**data["sources"][1], "id": 4, "source_ref": "s4", "text": "0.5 nM donor1"}
        data["seed"]["observations"] = [{"item": "i1", "experiment": None, "metric": "IC50",
            "value": "0.5", "unit": "nM", "s": 4, "refs": [4]}]
        payload = build_action_payload(data, chunks, completed)
        canonical = action_response(payload)
        canonical["dataset"]["observations"][0]["donor"] = "donor1"
        canonical["seed_changes"] = [{"seed_paths": ["/initial_dataset/observations/0"],
            "outcome": "replace", "targets": ["/dataset/observations/0"], "refs": [4],
            "reason": "Fill the explicitly printed donor."}]
        wire = copy.deepcopy(canonical)
        wire["seed_changes"][0]["targets"] = ["/dataset/observations"]
        expanded = expand_record_refs(wire, payload, data)
        self.assertEqual(expanded, canonical)
        validate_action_result(data, payload, expanded)

    def test_original_v1_transport_remains_lossless_after_column_encoding_upgrade(self):
        _, _, _, payload = self.example()
        old = copy.deepcopy(payload)
        old["input_encoding"] = {"version": 1}
        for action in old["reader_actions"]:
            action.pop("chunk")
            if not action["source_scope_errors"]:
                action.pop("source_scope_errors")
        self.assertEqual(decode_action_payload(old), payload)

    def test_record_reference_mode_keeps_raw_model_output_separate_from_complete_result(self):
        data, chunks, completed, _, canonical, wire = self.referenced_example()

        class ReferencedProvider:
            action_record_refs = True

            def generate(self, stage, instructions, payload):
                assert "output_encoding" in payload
                return copy.deepcopy(wire), {}

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            _, metadata = run_action_agent(data, chunks, completed, ReferencedProvider(), out)
            self.assertEqual(json.loads((out / "action_agent.response.raw.1.json").read_text()), wire)
            self.assertEqual(json.loads((out / "action_agent.response.json").read_text()), canonical)
            self.assertEqual(metadata["response_encoding"], "explicit_record_references")

    def test_large_inputs_automatically_select_record_references_without_removing_sources(self):
        data, chunks, completed, _, _, wire = self.referenced_example()
        data["sources"][1]["text"] += " " * 250_000

        class AutomaticProvider:
            action_record_refs = None

            def generate(self, stage, instructions, payload):
                assert "output_encoding" in payload
                assert len(payload["sources"][0][6]) > 250_000
                return copy.deepcopy(wire), {}

        with tempfile.TemporaryDirectory() as tmp:
            _, metadata = run_action_agent(data, chunks, completed, AutomaticProvider(), Path(tmp))
        self.assertEqual(metadata["response_encoding"], "explicit_record_references")

    def test_transport_encoding_preserves_every_fact_source_and_nonempty_error(self):
        _, _, _, payload = self.example()
        payload["reader_actions"][0]["source_scope_errors"] = [999]
        original = copy.deepcopy(payload)
        wire = encode_action_payload(payload, threshold=0)
        self.assertEqual(payload, original)
        self.assertEqual(wire["initial_dataset"], payload["initial_dataset"])
        self.assertEqual(wire["sources"], payload["sources"])
        self.assertEqual(sum(len(t["rows"]) for t in wire["reader_actions"].values()), len(payload["reader_actions"]))
        self.assertEqual(wire["reader_action_scope_errors"][payload["reader_actions"][0]["id"]], [999])
        self.assertEqual(decode_action_payload(wire), payload)

    def example(self):
        data = fixture()
        chunks = [{"id": "c1", "core": [1], "context": []},
                  {"id": "c2", "core": [2, 3], "context": []}]
        first, second = empty_response(), empty_response()
        first["links"] = response()["links"][:1]
        second["sequences"] = response()["sequences"]
        completed = {"c1": {"response": first}, "c2": {"response": second}}
        payload = build_action_payload(data, chunks, completed)
        return data, chunks, completed, payload

    def test_cross_chunk_semantic_result_can_add_shared_chain_ownership(self):
        data, _, _, payload = self.example()
        result = action_response(payload)
        result["dataset"]["links"].append({"item": "i2", "region": "VH", "seq": 1, "name": None, "refs": [1]})
        review = next(r for r in result["domain_reviews"] if r["item"] == "i2" and r["region"] == "VH")
        review.update(status="supported", targets=[f'/dataset/links/{len(result["dataset"]["links"]) - 1}'],
                      missing_evidence=[])
        ledger = validate_action_result(data, payload, result)
        assembled = ledger.assemble()
        self.assertEqual(assembled["summary"]["variable_chains"], 2)
        self.assertEqual({e["sequences"]["VH"]["value"] for e in assembled["entities"]}, {SEQUENCE})
        self.assertEqual(assembled["action_agent"]["status"], "validated")

    def test_initial_entities_and_measurements_cannot_silently_disappear(self):
        data, chunks, completed, _ = self.example()
        data["sources"][4] = {**data["sources"][1], "id": 4, "source_ref": "v4", "text": "0.5 nM"}
        data["seed"]["observations"] = [{"item": "i1", "experiment": None, "metric": "IC50",
            "value": "0.5", "unit": "nM", "s": 4, "refs": [4]}]
        payload = build_action_payload(data, chunks, completed)
        result = action_response(payload)
        result["dataset"]["observations"] = []
        with self.assertRaisesRegex(ValueError, "silently removed"):
            validate_action_result(data, payload, result)
        result = action_response(payload)
        result["dataset"]["entities"].pop()
        with self.assertRaisesRegex(ValueError, "Initial entity"):
            validate_action_result(data, payload, result)

    def test_actions_require_complete_decisions_and_real_output_targets(self):
        data, _, _, payload = self.example()
        result = action_response(payload)
        result["action_decisions"].pop()
        with self.assertRaisesRegex(ValueError, "silently omitted"):
            validate_action_result(data, payload, result)
        result = action_response(payload)
        result["action_decisions"][0]["targets"] = ["/dataset/entities/999"]
        with self.assertRaisesRegex(ValueError, "does not exist"):
            validate_action_result(data, payload, result)
        result = action_response(payload)
        result["action_decisions"][0]["targets"] = ["/dataset/entities/0"]
        with self.assertRaisesRegex(ValueError, "of its kind"):
            validate_action_result(data, payload, result)

    def test_literal_same_entity_action_can_be_retained_as_one_entity_alias_set(self):
        data, chunks, completed, _ = self.example()
        data["sources"][1]["text"] += " Antibody A is also called Alias A."
        completed["c1"]["response"]["relations"] = [{"subject": "Antibody A", "relation": "same_entity",
            "object": "Alias A", "refs": [1]}]
        payload = build_action_payload(data, chunks, completed)
        result = action_response(payload)
        action = next(a for a in payload["reader_actions"] if a["kind"] == "relations")
        result["dataset"]["relations"] = []
        result["dataset"]["entities"][0]["names"] += ["Antibody A", "Alias A"]
        decision = next(d for d in result["action_decisions"] if action["id"] in d["action_ids"])
        decision.update(outcome="merge", targets=["/dataset/entities/0"], refs=[1])
        validate_action_result(data, payload, result)
        result["dataset"]["entities"][0]["names"].remove("Alias A")
        with self.assertRaisesRegex(ValueError, "of its kind"):
            validate_action_result(data, payload, result)

    def test_unchanged_canonical_seed_name_is_preserved_as_prior_context(self):
        data, chunks, completed, _ = self.example()
        data["seed"]["entities"][0]["name"] = "Canonical Antibody A"
        data["seed"]["entities"][0]["names"] = ["Canonical Antibody A"]
        payload = build_action_payload(data, chunks, completed)
        result = action_response(payload)
        ledger = validate_action_result(data, payload, result)
        self.assertEqual(ledger.entities["i1"]["name"], "Canonical Antibody A")

    def test_sequence_residues_and_source_scope_are_checked_after_integration(self):
        data, _, _, payload = self.example()
        result = action_response(payload)
        result["dataset"]["sequences"][0]["value"] = "A" + SEQUENCE[1:]
        with self.assertRaisesRegex(ValueError, "literal sequence"):
            validate_action_result(data, payload, result)
        result = action_response(payload)
        result["dataset"]["links"][0]["refs"] = [999]
        with self.assertRaisesRegex(ValueError, "absent from its request"):
            validate_action_result(data, payload, result)

    def test_feedback_reports_seed_loss_and_grounding_errors_together(self):
        data, chunks, completed, _ = self.example()
        data["seed"]["experiments"] = [{"id": "assay", "method": "ELISA", "refs": [1]}]
        payload = build_action_payload(data, chunks, completed)
        result = action_response(payload)
        result["dataset"]["experiments"] = []
        result["dataset"]["links"][0]["seq"] = 99
        with self.assertRaises(ValueError) as error:
            validate_action_result(data, payload, result)
        self.assertIn("silently removed", str(error.exception))
        self.assertIn("Claimed SEQ ID", str(error.exception))

    def test_rejected_reader_actions_remain_visible_in_the_audit(self):
        data, chunks, completed, _ = self.example()
        completed["c1"]["response"]["issues"].append({"kind": "other", "items": [], "seq": None, "refs": [999]})
        payload = build_action_payload(data, chunks, completed)
        self.assertEqual(payload["reader_actions"][-2]["kind"], "issues")
        bad = next(a for a in payload["reader_actions"] if 999 in a["source_scope_errors"])
        result = action_response(payload)
        ledger = validate_action_result(data, payload, result)
        self.assertEqual(len(ledger.rejections), 1)
        self.assertIn("999", json.dumps(ledger.rejections))
        self.assertTrue(any(d["outcome"] == "reject" and bad["id"] in d["action_ids"] for d in result["action_decisions"]))

    def test_feedback_includes_empty_group_evidence_and_invalid_facts_together(self):
        data, _, _, payload = self.example()
        result = action_response(payload)
        result["action_decisions"][0]["refs"] = []
        result["dataset"]["links"][0]["seq"] = 999
        with self.assertRaises(ValueError) as error:
            validate_action_result(data, payload, result)
        self.assertIn("Accepted action needs cited output evidence", str(error.exception))
        self.assertIn("Claimed SEQ ID", str(error.exception))

    def test_action_schema_is_used_for_requests_and_cached_answers(self):
        _, _, _, payload = self.example()
        result = action_response(payload)
        with tempfile.TemporaryDirectory() as tmp:
            provider = Provider(tmp)
            request = {"stage": "action", "instructions": ACTION_INSTRUCTIONS, "payload": payload,
                       "schema": SCHEMA_ACTION, "model": provider.model, "effort": provider.effort}
            key = digest(request)
            write_json(Path(tmp) / "requests" / key / "validated.json", result)
            with patch("dataagent2.provider.subprocess.Popen", side_effect=AssertionError("Cache miss")):
                actual, meta = provider.generate("action", ACTION_INSTRUCTIONS, payload)
        self.assertEqual(response_schema("action"), SCHEMA_ACTION)
        self.assertEqual(actual, result)
        self.assertTrue(meta["cache_hit"])

    def test_complete_dataset_generation_has_a_separate_timeout(self):
        from unittest.mock import MagicMock
        for stage, override, expected in [("reader", None, 600), ("action", None, 1800), ("action", 45, 45)]:
            with self.subTest(stage=stage, override=override), tempfile.TemporaryDirectory() as tmp:
                proc = MagicMock(returncode=1)
                proc.communicate.return_value = ("", "scripted failure")
                with patch("dataagent2.provider.subprocess.Popen", return_value=proc):
                    with self.assertRaisesRegex(RuntimeError, "scripted failure"):
                        Provider(tmp, action_timeout=override).generate(stage, "test", {})
                self.assertEqual(proc.communicate.call_args.kwargs["timeout"], expected)

    def test_action_reasoning_budget_does_not_change_reader_calls(self):
        from unittest.mock import MagicMock
        for stage, expected in [("reader", "xhigh"), ("action", "medium")]:
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as tmp:
                proc = MagicMock(returncode=1)
                proc.communicate.return_value = ("", "scripted failure")
                with patch("dataagent2.provider.subprocess.Popen", return_value=proc) as start:
                    with self.assertRaisesRegex(RuntimeError, "scripted failure"):
                        provider = Provider(tmp, effort="xhigh", action_effort="medium")
                        provider.generate(stage, "test", {})
                self.assertIn(f'model_reasoning_effort="{expected}"', start.call_args.args[0])
                self.assertEqual({c["effort"] for c in provider.calls}, {expected})

    def test_invalid_complete_output_gets_one_feedback_attempt(self):
        data, chunks, completed, _ = self.example()
        calls = []

        class Scripted:
            def generate(self, stage, instructions, payload):
                calls.append(copy.deepcopy(payload))
                result = action_response(payload)
                if len(calls) == 1:
                    result["dataset"]["entities"].pop()
                return result, {}

        with tempfile.TemporaryDirectory() as tmp:
            ledger, meta = run_action_agent(data, chunks, completed, Scripted(), Path(tmp))
            self.assertTrue((Path(tmp) / "action_agent.dataset.json").is_file())
        self.assertEqual(meta["attempt"], 2)
        self.assertIn("validation_errors", calls[1])
        self.assertEqual(len(ledger.entities), 2)

    def test_duplicate_audit_outcomes_get_a_small_llm_correction_without_rewriting_facts(self):
        data, chunks, completed, _ = self.example()
        calls, original = [], []

        class Scripted:
            def generate(self, stage, instructions, payload):
                calls.append(stage)
                if stage == "action":
                    result = action_response(payload)
                    original.append(copy.deepcopy(result["dataset"]))
                    result["action_decisions"].append({"action_ids": result["action_decisions"][0]["action_ids"],
                        "outcome": "reject", "targets": [], "refs": [1], "reason": "Contradictory duplicate disposition"})
                    return result, {}
                duplicate = payload["duplicates"][0]
                return {"resolutions": [{"action_id": duplicate["action"]["id"], "decision_index": 0,
                    "refs": [1], "reason": "The accepted output retains this source-supported mapping."}]}, {}

        with tempfile.TemporaryDirectory() as tmp:
            _, metadata = run_action_agent(data, chunks, completed, Scripted(), Path(tmp))
            result = json.loads((Path(tmp) / "action_agent.response.json").read_text())
        self.assertEqual(calls, ["action", "action_accounting"])
        self.assertEqual(result["dataset"], original[0])
        self.assertEqual(len(metadata["accounting_repair"]["duplicate_action_ids"]), 1)

    def test_host_invalid_cached_output_is_retained_for_audit_but_cannot_hit_again(self):
        data, chunks, completed, payload = self.example()

        class Cached(Provider):
            def generate(self, stage, instructions, supplied):
                if "validation_errors" in supplied:
                    return action_response(supplied), {}
                return super().generate(stage, instructions, supplied)

        with tempfile.TemporaryDirectory() as tmp:
            provider = Cached(tmp)
            key = digest({"stage": "action", "instructions": ACTION_INSTRUCTIONS, "payload": payload,
                "schema": SCHEMA_ACTION, "model": provider.model, "effort": provider.effort})
            invalid = action_response(payload)
            invalid["dataset"]["entities"].pop()
            folder = Path(tmp) / "requests" / key
            write_json(folder / "validated.json", invalid)
            with patch("dataagent2.provider.subprocess.Popen", side_effect=AssertionError("Unexpected model call")):
                _, meta = run_action_agent(data, chunks, completed, provider, Path(tmp))
            self.assertEqual(meta["status"], "validated")
            self.assertFalse((folder / "validated.json").exists())
            self.assertEqual(len(list(folder.glob("host-rejected-*.json"))), 1)

    def test_failed_action_agent_keeps_partial_data_and_skips_gap_answers(self):
        stages = []

        class Scripted:
            model, effort = "test", "test"

            def generate(self, stage, instructions, payload):
                stages.append(stage)
                if stage == "action":
                    raise RuntimeError("Action service unavailable")
                return response(), {}

            def usage(self):
                return {"total_tokens": 0}

        with tempfile.TemporaryDirectory() as tmp:
            report = run(fixture(), Scripted(), tmp, max_questions=2)
            saved = json.loads((Path(tmp) / "dataset.json").read_text())
        self.assertEqual(stages, ["reader", "action"])
        self.assertFalse(report["action_complete"])
        self.assertFalse(report["benchmark_complete"])
        self.assertEqual(saved["status"], "partial")
        self.assertEqual(saved["action_agent"]["status"], "failed")

    def test_incomplete_reader_batch_never_starts_action_model(self):
        data, chunks, completed, _ = self.example()
        completed.pop("c2")
        with tempfile.TemporaryDirectory() as tmp:
            provider = Provider(tmp)
            with patch.object(provider, "generate", side_effect=AssertionError("Must not integrate a partial batch")):
                with self.assertRaisesRegex(ValueError, "every Reader chunk"):
                    run_action_agent(data, chunks, completed, provider, Path(tmp))


if __name__ == "__main__":
    unittest.main()
