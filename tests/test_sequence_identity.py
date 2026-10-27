import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dataagent.llm_sequence_agent import SequenceExtractionAgent
from dataagent.llm_runtime import CodexUnavailable
from dataagent.sequence_evidence import build_document_structure
from dataagent.sequence_identity import identity_questions, review_missing_variable_regions


class IdentityReviewTests(unittest.TestCase):
    def setUp(self):
        tables = [{"source_id": "seq", "page": 1, "rows": [
            [{"text": "Ab S3-6 VH SEQ ID NO: 1"}, {"text": "ACDEFGHIK"}],
            [{"text": "Ab S3-6 VL SEQ ID NO: 2"}, {"text": "LMNPQRSTV"}]]},
            {"source_id": "assay", "page": 2, "rows": [[{"text": "Antibody"}],
                [{"text": "Ab S3-6-hIgG2"}]]}]
        self.graph = build_document_structure("IDENTITY_TEST", tables,
            methods=[{"page": 1, "text": "Ab S3-6 VH ACDEYGHIK"}])
        self.entities = [{"entity_id": "base", "canonical_name": "Ab S3-6", "raw_names": ["Ab S3-6"],
                          "evidence_refs": ["seq:R0C0", "seq:R1C0"]},
                         {"entity_id": "format", "canonical_name": "Ab S3-6-hIgG2", "raw_names": ["Ab S3-6-hIgG2"],
                          "evidence_refs": ["assay:R1C0"]}]
        decisions = []
        for row, (chain, value) in enumerate((("VH", "ACDEFGHIK"), ("VL", "LMNPQRSTV"))):
            decisions.append({"entity_id": "base", "chain": chain, "status": "resolved", "value": value,
                "seq_id": row + 1, "source_scope": "variable", "rationale": "Explicit variable-domain table cell",
                "evidence_refs": [f"seq:R{row}C0"],
                "fragments": [{"source_ref": f"seq:R{row}C1", "quote": value, "occurrence": 0}],
                "source_conflicts": [{"table_ref": "seq:R0C1", "text_ref": "method:p1",
                    "table_quote": "ACDEFGHIK", "text_quote": "ACDEYGHIK", "message": "F/Y discrepancy"}] if row == 0 else []})
        decisions += [{"entity_id": "format", "chain": chain, "status": "unresolved", "value": None,
                       "seq_id": None, "source_scope": "unknown", "rationale": "Ownership is missing",
                       "evidence_refs": ["assay:R1C0"], "fragments": [], "source_conflicts": []}
                      for chain in ("VH", "VL")]
        self.bundle = {"document_id": "IDENTITY_TEST", "decisions": decisions, "sources": self.graph["sources"]}
        self.template = {"entity_template": {"entities": self.entities}}

    def provider(self, verdicts=None, corrupt=False, unavailable=False):
        original = self.bundle

        class Provider:
            cache_enabled = False

            def __init__(self):
                self.calls = []

            def generate(self, task, instructions, payload, contract, **kwargs):
                self.calls.append(task)
                if unavailable:
                    raise CodexUnavailable("429 capacity")
                if contract.__name__ == "SequenceExtractionStep":
                    return {"document_id": "IDENTITY_TEST", "complete": True, "request_refs": [],
                            "search_queries": [], "decisions": original["decisions"]}, {}
                result = {"document_id": "IDENTITY_TEST", "decisions": [{**q,
                    "verdict": (verdicts or {}).get(q["chain"], "same"), "basis": "isotype_format",
                    "rationale": "The exact S3-6 base is retained; hIgG2 identifies constant-region format",
                    "evidence": [{"source_ref": "seq:R0C0", "quote": "Ab S3-6", "occurrence": 0},
                                 {"source_ref": "assay:R1C0", "quote": "invented" if corrupt else "Ab S3-6-hIgG2", "occurrence": 0}]}
                    for q in payload["questions"]]}
                kwargs["validate"](result)
                return result, {"task": task, "cache_hit": False}

        return Provider()

    def run_review(self, provider):
        return review_missing_variable_regions(provider, self.graph, self.template, self.bundle)

    def test_same_chains_inherit_literal_values_with_conflicts_and_audit(self):
        provider = self.provider()
        result = self.run_review(provider)
        self.assertEqual(len(provider.calls), 1)
        children = result["decisions"][2:]
        self.assertEqual([d["value"] for d in children], ["ACDEFGHIK", "LMNPQRSTV"])
        self.assertEqual(children[0]["source_conflicts"], self.bundle["decisions"][0]["source_conflicts"])
        self.assertEqual(result["sequence_inheritance"][0]["basis"], "codex_evidence")
        self.assertEqual(result["identity_review"]["applied_chain_count"], 2)
        self.assertIsNone(self.bundle["decisions"][2]["value"])

    def test_different_or_uncertain_chain_is_not_filled(self):
        for verdict in ("different", "uncertain"):
            with self.subTest(verdict=verdict):
                result = self.run_review(self.provider({"VH": "same", "VL": verdict}))
                self.assertEqual(result["decisions"][2]["value"], "ACDEFGHIK")
                self.assertIsNone(result["decisions"][3]["value"])
                self.assertEqual(result["identity_review"]["applied_chain_count"], 1)
                if verdict == "uncertain":
                    self.assertEqual(result["identity_review"]["status"], "needs_review")

    def test_invalid_quotes_and_service_failure_preserve_missing_status(self):
        for provider in (self.provider(corrupt=True), self.provider(unavailable=True)):
            result = self.run_review(provider)
            self.assertEqual(result["decisions"], self.bundle["decisions"])
            self.assertEqual(result["identity_review"]["status"], "needs_review")
            self.assertEqual(result["identity_review"]["applied_chain_count"], 0)

    def test_candidate_generation_preserves_mutation_designation(self):
        entities = copy.deepcopy(self.entities)
        entities[0].update(canonical_name="Ab S3-7", raw_names=["Ab S3-7"])
        self.assertEqual(identity_questions(self.bundle, entities), [])
        self.assertEqual(len(identity_questions(self.bundle, self.entities)), 2)

    def test_explicit_damaged_sequence_assignment_is_not_bypassed(self):
        bundle = copy.deepcopy(self.bundle)
        bundle["decisions"][2]["seq_id"] = 99
        self.assertEqual([q["chain"] for q in identity_questions(bundle, self.entities)], ["VL"])

    def test_completed_entity_does_not_trigger_model_call(self):
        self.bundle["decisions"][2:]=[{**copy.deepcopy(d), "entity_id": "format"}
                                      for d in self.bundle["decisions"][:2]]
        provider = self.provider()
        self.assertIs(self.run_review(provider), self.bundle)
        self.assertEqual(provider.calls, [])

    def test_sequence_agent_automatically_triggers_identity_review_after_extraction(self):
        provider = self.provider()
        result = SequenceExtractionAgent(provider).run(self.graph, self.template)
        self.assertEqual(provider.calls[-1], "sequence-identity-v1")
        self.assertEqual(result["identity_review"]["applied_chain_count"], 2)
        self.assertEqual([d["status"] for d in result["decisions"]], ["resolved"] * 4)


if __name__ == "__main__":
    unittest.main()
