"""Focused answers must not commit proposals or silently lose prior facts."""

import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from dataagent2.action_agent import build_action_payload, business_dataset, validate_action_result
from dataagent2.contracts import check_shape
from dataagent2.followup import (ANSWER_SCHEMA, answer_payload, question_plan, run_followup, validate_answer)
from dataagent2.provider import digest, response_schema, write_json
from dataagent2.test_dataagent2 import SEQUENCE, action_response, empty_answer, fixture, response


def example():
    data = fixture()
    for sid, text in [(4, "Antibody A VL is SEQ ID 2; antibody B VL is SEQ ID 2."),
                      (5, "VL SEQ ID 2"), (6, SEQUENCE)]:
        data["sources"][sid] = {**data["sources"][1], "id": sid, "source_ref": f"ocr:{sid}", "text": text, "page": 3}
    chunks = [{"id": "c1", "core": list(data["sources"]), "context": []}]
    payload = build_action_payload(data, chunks, {"c1": {"response": response()}})
    result = action_response(payload)
    ledger = validate_action_result(data, payload, result)
    return data, ledger, {"input": payload["domain_review_input"], "reviews": result["domain_reviews"]}


def resolved_answer(payload):
    q = payload["question"]
    return {"question_id": q["id"], "status": "resolved", "answer": "Explicit VL mapping and complete definition.",
        "refs": [4, 5, 6], "missing_evidence": [], "actions": [
            *[{"type": "add_link", "fact": {"item": t["item"], "region": "VL", "seq": 2, "name": None, "refs": [4, 5]}}
              for t in q["targets"]],
            {"type": "add_sequence_definition", "fact": {"seq": 2, "name": None, "region": "VL",
                "parts": [{"s": 6, "start": "", "end": ""}], "refs": [5, 6]}}]}


class FollowupTests(unittest.TestCase):
    def test_questions_come_from_reviews_and_exclude_not_applicable(self):
        _, ledger, review = example()
        row = next(r for r in review["reviews"] if r["item"] == "i2" and r["region"] == "VL")
        row.update(status="not_applicable", missing_evidence=[])
        plan = question_plan(ledger.assemble(), review["reviews"])
        self.assertEqual(len(plan["questions"]), 1)
        self.assertEqual(plan["questions"][0]["review_context"][0]["missing_evidence"],
                         ["Test fixture does not supply confirmed domain ownership."])
        with self.assertRaisesRegex(ValueError, "complete"):
            question_plan(ledger.assemble(), review["reviews"][:-1])

    def test_required_original_evidence_stays_whole_when_budget_is_small(self):
        _, ledger, review = example()
        # Numbered definitions can have descriptive names while their links
        # omit the name; the exact SEQ ID still locates the same definition.
        ledger.facts["sequences"][0]["name"] = "Printed variable domain"
        q = question_plan(ledger.assemble(), review["reviews"])["questions"][0]
        payload, allowed = answer_payload(q, ledger, review["input"], max_chars=1)
        self.assertTrue(payload["retrieval"]["required_budget_exceeded"])
        self.assertIn(3, allowed)
        self.assertEqual(next(row[6] for row in payload["sources"] if row[0] == 3), SEQUENCE)
        self.assertTrue(any(d["seq"] == 1 for d in payload["known_definitions"]))

    def test_answer_schema_is_narrow_and_legacy_requests_remain_readable(self):
        self.assertEqual(response_schema("answer", payload={"workflow_revision": 1}), ANSWER_SCHEMA)
        from dataagent2.contracts import SCHEMA
        self.assertEqual(response_schema("answer", payload={}), SCHEMA)
        with self.assertRaises(ValueError):
            check_shape({**empty_answer({"question": {"id": "q1"}, "sources": []}), "observations": []}, ANSWER_SCHEMA)

    def test_unavailable_sources_and_false_resolution_are_rejected_without_mutation(self):
        _, ledger, review = example()
        q = question_plan(ledger.assemble(), review["reviews"])["questions"][0]
        payload, allowed = answer_payload(q, ledger, review["input"], 20000)
        initial = business_dataset(ledger)
        answer = resolved_answer(payload)
        answer["refs"].append(999)
        with self.assertRaisesRegex(ValueError, "unavailable"):
            validate_answer(answer, q, ledger, allowed)
        answer = resolved_answer(payload)
        answer["actions"] = []
        with self.assertRaisesRegex(ValueError, "concrete actions"):
            validate_answer(answer, q, ledger, allowed)
        self.assertEqual(initial, business_dataset(ledger))

    def test_parallel_answers_use_fixed_dataset_then_one_llm_commits_complete_dataset(self):
        data, ledger, review = example()
        ledger.rejections.append({"chunk": "old", "kind": "issues", "error": "Saved prior rejection", "fact": {}})
        ledger.action_conflicts = [{"kind": "action_conflict", "code": "old", "message": "Prior conflict", "refs": [1], "action_ids": []}]
        initial = business_dataset(ledger)
        barrier, stages, hashes = threading.Barrier(2), [], []

        class Provider:
            def generate(self, stage, instructions, payload):
                stages.append(stage)
                if stage == "answer":
                    hashes.append(payload["dataset2_hash"])
                    barrier.wait(timeout=5)
                    self_check.assertEqual(business_dataset(ledger), initial)
                    return resolved_answer(payload), {}
                self_check.assertEqual(stage, "answer_action")
                self_check.assertEqual(payload["initial_dataset"], initial)
                self_check.assertEqual(business_dataset(ledger), initial)
                return action_response(payload), {}

        self_check = self
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            write_json(output / "domain_review.json", review)
            updated, result = run_followup(data, ledger, Provider(), output, workers=2, question_chars=20000)
            self.assertEqual(json.loads((output / "dataset2.business.json").read_text()), initial)
            self.assertEqual(json.loads((output / "dataset3.business.json").read_text()), business_dataset(updated))
        self.assertEqual(stages.count("answer"), 2)
        self.assertEqual(stages.count("answer_action"), 1)
        self.assertEqual(len(set(hashes)), 1)
        self.assertEqual(result["integration"]["status"], "validated")
        self.assertTrue(all(a["resolved"] for a in result["answers"]))
        self.assertEqual(updated.assemble()["summary"]["complete_pairs"], 2)
        self.assertEqual(updated.rejections, ledger.rejections)
        self.assertEqual(updated.action_conflicts, ledger.action_conflicts)
        self.assertEqual(business_dataset(ledger), initial)

    def test_no_new_facts_skips_second_action_and_keeps_missing_questions(self):
        data, ledger, review = example()

        class Provider:
            def generate(self, stage, instructions, payload):
                self_check.assertEqual(stage, "answer")
                return empty_answer(payload), {}

        self_check = self
        with tempfile.TemporaryDirectory() as tmp:
            write_json(Path(tmp) / "domain_review.json", review)
            updated, result = run_followup(data, ledger, Provider(), tmp)
        self.assertIs(updated, ledger)
        self.assertEqual(result["integration"]["status"], "not_needed")
        self.assertEqual(len(result["remaining"]), 2)
        self.assertTrue(all(a["status"] == "no_change" and not a["resolved"] for a in result["answers"]))

    def test_invalid_second_action_keeps_dataset2_and_reports_failure(self):
        data, ledger, review = example()
        initial = business_dataset(ledger)

        class Provider:
            def generate(self, stage, instructions, payload):
                if stage == "answer":
                    return resolved_answer(payload), {}
                result = action_response(payload)
                result["dataset"]["entities"] = []
                return result, {}

        with tempfile.TemporaryDirectory() as tmp:
            write_json(Path(tmp) / "domain_review.json", review)
            updated, result = run_followup(data, ledger, Provider(), tmp, question_chars=20000)
        self.assertEqual(result["integration"]["status"], "failed")
        self.assertIs(updated, ledger)
        self.assertEqual(business_dataset(updated), initial)
        self.assertTrue(all(not a["resolved"] for a in result["answers"]))

    def test_question_budget_defers_explicitly_without_losing_queue(self):
        data, ledger, review = example()
        class Provider:
            def generate(self, *args):
                raise AssertionError("Zero budget must not call a model")
        with tempfile.TemporaryDirectory() as tmp:
            write_json(Path(tmp) / "domain_review.json", review)
            _, result = run_followup(data, ledger, Provider(), tmp, max_questions=0)
        self.assertEqual(len(result["remaining"]), 2)
        self.assertTrue(all(a["status"] == "deferred_budget" for a in result["answers"]))

    def test_full_pipeline_replays_saved_second_action_without_applying_answers(self):
        from dataagent2.pipeline import run
        from dataagent2.replay import replay
        data, _, _ = example()

        class Provider:
            model, effort, answer_effort = "test", "test", "test"

            def __init__(self, output):
                self.output = output

            def usage(self):
                return {"total_tokens": 0}

            def generate(self, stage, instructions, payload):
                result = (response() if stage == "reader" else resolved_answer(payload)
                          if stage == "answer" else action_response(payload))
                fingerprint = {"stage": stage, "instructions": instructions, "payload": payload,
                    "schema": response_schema(stage, payload=payload), "model": self.model, "effort": self.effort}
                key = digest(fingerprint)
                write_json(self.output / "requests" / key / "input.json", fingerprint)
                write_json(self.output / "requests" / key / "validated.json", result)
                return result, {"key": key}

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            report = run(data, Provider(output), output, max_questions=None, question_chars=20000)
            self.assertTrue(report["benchmark_complete"])
            expected = json.loads((output / "dataset.json").read_text())
            with patch("dataagent2.replay.load_input", return_value=data), \
                    patch("dataagent2.provider.subprocess.Popen", side_effect=AssertionError("Replay called a model")):
                result = replay(output)
            actual = json.loads((output / "dataset.checkpoint.json").read_text())
            self.assertEqual(result["model_calls_made"], 0)
            for key in ("entities", "observations", "definitions", "conflicts"):
                self.assertEqual(actual.get(key), expected.get(key))
            # The replay must reject a changed saved proposal even if the full
            # second Action file was left intact.
            answers = json.loads((output / "answers.json").read_text())
            answers[0]["facts"]["links"][0]["seq"] = 999
            write_json(output / "answers.json", answers)
            with patch("dataagent2.replay.load_input", return_value=data), self.assertRaisesRegex(ValueError, "proposals changed"):
                replay(output)


if __name__ == "__main__":
    unittest.main()
