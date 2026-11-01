"""Missing cross-chunk domain links require explicit, grounded model review."""

import copy
import unittest

from dataagent2.action_agent import build_action_payload, validate_action_result
from dataagent2.contracts import empty_response
from dataagent2.domain_review import build_domain_review_input
from dataagent2.test_dataagent2 import SEQUENCE, action_response, fixture


class DomainReviewTests(unittest.TestCase):
    def example(self):
        data = fixture()
        tail = "ASTKGPSVFPLAPSSKSTSGGTAALGCLVKDYFPEPVTVSWNSGALTSGVHTFPAVLQSSGLYSLSSVVTVPSSSLGTQTYICNVNHKPSNTKVDKRV"
        for sid, text in [(4, SEQUENCE + tail), (5, tail), (6, "Antibody A full heavy chain is SEQ ID 3; CH is SEQ ID 2.")]:
            data["sources"][sid] = {**data["sources"][1], "id": sid, "source_ref": f"s{sid}", "text": text}
        proposal = empty_response()
        proposal["links"] = [{"item": "i1", "region": "H_CHAIN", "seq": 3, "name": None, "refs": [6]}]
        proposal["sequences"] = [
            {"seq": seq, "name": None, "region": region, "parts": [{"s": sid, "start": "", "end": ""}], "refs": [sid, label]}
            for seq, region, sid, label in [(1, "VH", 3, 2), (2, "CH", 5, 6), (3, "H_CHAIN", 4, 6)]]
        chunks = [{"id": "c1", "core": list(data["sources"]), "context": []}]
        completed = {"c1": {"response": proposal}}
        payload = build_action_payload(data, chunks, completed)
        return data, payload

    def test_exact_candidate_does_not_automatically_create_domain_ownership(self):
        data, payload = self.example()
        original = copy.deepcopy(payload)
        self.assertEqual(len(payload["domain_review_input"]["candidates"]), 1)
        result = action_response(payload)
        self.assertFalse(any(l["item"] == "i1" and l["region"] == "VH" for l in result["dataset"]["links"]))
        ledger = validate_action_result(data, payload, result)
        self.assertEqual(ledger.assemble()["summary"]["variable_chains"], 0)
        self.assertEqual(payload, original)

    def test_prefix_or_unexplained_residue_and_failed_ocr_are_not_exact_candidates(self):
        data, payload = self.example()
        data["sources"][4]["text"] += "A"
        self.assertEqual(build_domain_review_input(data, payload)["candidates"], [])
        data["sources"][4]["text"] = data["sources"][4]["text"][:-1]
        data["sources"][3]["sequence_ocr"] = {"status": "failed"}
        self.assertEqual(build_domain_review_input(data, payload)["candidates"], [])

    def test_equally_exact_alternatives_are_preserved_for_model_judgment(self):
        data, payload = self.example()
        duplicate = copy.deepcopy(next(a for a in payload["reader_actions"] if a["kind"] == "sequences" and a["fact"]["region"] == "VH"))
        duplicate["id"] = "c1:sequences:99"
        duplicate["fact"]["seq"] = 99
        duplicate["fact"]["refs"].append(6)
        data["sources"][6]["text"] += " Alternative complete VH is SEQ ID 99."
        payload["reader_actions"].append(duplicate)
        candidates = build_domain_review_input(data, payload)["candidates"]
        self.assertEqual(len(candidates), 2)
        self.assertEqual({c["alternative_count"] for c in candidates}, {2})

    def test_numbered_definition_names_do_not_hide_an_exact_candidate(self):
        data, payload = self.example()
        for action in payload["reader_actions"]:
            if action["kind"] == "sequences":
                action["fact"]["name"] = "Printed domain description"
        self.assertEqual(len(build_domain_review_input(data, payload)["candidates"]), 1)

    def test_missing_review_candidate_or_specific_gap_is_rejected(self):
        data, payload = self.example()
        for modification, error in [("row", "Missing per-entity"), ("candidate", "omitted or invented"),
                                    ("gap", "specific evidence gap"), ("generic", "generic evidence gap"),
                                    ("refs", "lacks compared evidence")]:
            with self.subTest(modification=modification):
                result = action_response(payload)
                review = next(r for r in result["domain_reviews"] if r["item"] == "i1" and r["region"] == "VH")
                if modification == "row":
                    result["domain_reviews"].remove(review)
                elif modification == "candidate":
                    review["candidate_ids"] = []
                elif modification == "gap":
                    review["missing_evidence"] = []
                elif modification == "generic":
                    review["missing_evidence"] = ["missing_mapping"]
                else:
                    review["refs"] = [6]
                with self.assertRaisesRegex(ValueError, error):
                    validate_action_result(data, payload, result)

    def test_supported_review_requires_an_actual_supported_link(self):
        data, payload = self.example()
        result = action_response(payload)
        review = next(r for r in result["domain_reviews"] if r["item"] == "i1" and r["region"] == "VH")
        review.update(status="supported", missing_evidence=[])
        with self.assertRaisesRegex(ValueError, "contradicts"):
            validate_action_result(data, payload, result)
        result["dataset"]["links"].append({"item": "i1", "region": "VH", "seq": 1, "name": None, "refs": [1, 2, 6]})
        with self.assertRaisesRegex(ValueError, "corresponding link"):
            validate_action_result(data, payload, result)
        review["targets"] = [f'/dataset/links/{len(result["dataset"]["links"]) - 1}']
        self.assertEqual(validate_action_result(data, payload, result).assemble()["summary"]["variable_chains"], 1)


if __name__ == "__main__":
    unittest.main()
