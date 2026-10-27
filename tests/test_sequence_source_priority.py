import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dataagent.llm_sequence_agent import (SequenceExtractionAgent, ground_decision, resolve_agent_sequences,
                                        apply_confirmed_sequence_inheritance, validate_decisions_individually)
from dataagent.sequence_evidence import build_document_structure
from dataagent.sequence_recovery import recover_sequence_decisions


class SequenceSourcePriorityTests(unittest.TestCase):
    def setUp(self):
        self.tables = [{"source_id": "seq", "page": 1, "rows": [
            [{"text": "Ab VH SEQ ID NO: 1"}, {"text": "ACDEFGHIK"}],
            [{"text": "Ab VL SEQ ID NO: 2"}, {"text": "LMNPQRSTV"}],
        ]}]
        self.graph = build_document_structure("DOC", self.tables, methods=[
            {"page": 1, "text": "Ab VH SEQ ID NO: 1 ACDEYGHIK"}])
        self.conflict = {"table_ref": "seq:R0C1", "text_ref": "method:p1",
            "table_quote": "ACDEFGHIK", "text_quote": "ACDEYGHIK", "message": "F/Y discrepancy"}
        self.decisions = [{"entity_id": "ab", "chain": chain, "status": "resolved", "value": value,
            "seq_id": row + 1, "source_scope": "variable",
            "fragments": [{"source_ref": f"seq:R{row}C1", "quote": value, "occurrence": 0}],
            "evidence_refs": [f"seq:R{row}C0"], "rationale": "Explicit table definition",
            "source_conflicts": [self.conflict] if row == 0 else []}
            for row, (chain, value) in enumerate((("VH", "ACDEFGHIK"), ("VL", "LMNPQRSTV")))]

    def test_conflict_preserves_table_value_and_both_sources_through_resolution(self):
        decisions = self.decisions

        class Provider:
            cache_enabled = False

            def generate(self, task, instructions, payload, contract, **kwargs):
                result = {"document_id": "DOC", "request_refs": [], "search_queries": [],
                          "complete": True, "decisions": decisions}
                kwargs["validate"](result)
                return result, {}

        template = {"entity_template": {"entities": [{"entity_id": "ab"}]}}
        bundle = SequenceExtractionAgent(Provider()).run(self.graph, template,
                                                       initial_source_refs=list(self.graph["sources"]))
        self.assertEqual(bundle["status"], "needs_review")
        self.assertIn("method:p1", bundle["sources"])
        template["sequence_extraction"] = bundle
        resolved = resolve_agent_sequences(self.tables, template)["ab"]
        self.assertEqual(resolved["vh"], "ACDEFGHIK")
        self.assertEqual(resolved["vl"], "LMNPQRSTV")
        self.assertEqual(resolved["source_conflicts"], [{**self.conflict, "chain": "VH"}])
        self.assertEqual(resolved["sequence_resolution"]["vh"]["status"], "resolved")

    def test_conflicting_page_value_cannot_replace_table_value(self):
        decision = copy.deepcopy(self.decisions[0])
        decision["value"] = "ACDEYGHIK"
        decision["fragments"] = [{"source_ref": "method:p1", "quote": "ACDEYGHIK", "occurrence": 0}]
        with self.assertRaisesRegex(ValueError, "gold-standard table cell"):
            ground_decision(decision, self.graph["sources"])

    def test_corroborating_full_chain_table_does_not_require_duplicate_residues(self):
        sources = copy.deepcopy(self.graph["sources"])
        sources["full:R0C1"] = {"kind": "cell", "table_id": "full", "text": "ACDEFGHIKLMNPQRSTV"}
        decision = copy.deepcopy(self.decisions[0])
        decision["source_conflicts"].append({**self.conflict, "table_ref": "full:R0C1"})
        segments = ground_decision(decision, sources)
        self.assertEqual("".join(s["text"] for s in segments), "ACDEFGHIK")
        self.assertEqual(len(segments), 1)
        decision["value"] = "ACDEYGHIK"
        decision["fragments"] = [{"source_ref": "method:p1", "quote": "ACDEYGHIK", "occurrence": 0}]
        with self.assertRaisesRegex(ValueError, "gold-standard"):
            ground_decision(decision, sources)

    def test_one_invalid_chain_does_not_discard_supported_chain_in_batch(self):
        decisions = copy.deepcopy(self.decisions)
        decisions[1]["value"] = "AAAAAAAAA"
        class Provider:
            cache_enabled = False
            def generate(self, task, instructions, payload, contract, **kwargs):
                result = {"document_id": "DOC", "request_refs": [], "search_queries": [],
                          "complete": True, "decisions": decisions}
                kwargs["validate"](result)
                return result, {}
        bundle = SequenceExtractionAgent(Provider()).run(self.graph,
            {"entity_template": {"entities": [{"entity_id": "ab"}]}},
            initial_source_refs=list(self.graph["sources"]))
        self.assertEqual(bundle["decisions"][0]["value"], "ACDEFGHIK")
        self.assertEqual(bundle["decisions"][1]["status"], "unresolved")
        self.assertEqual(bundle["batches"][0]["status"], "partial")
        malformed = {"document_id": "OTHER", "request_refs": [], "search_queries": [],
                     "complete": True, "decisions": decisions}
        with self.assertRaisesRegex(ValueError, "Wrong document"):
            validate_decisions_individually(malformed, "DOC", [{"entity_id": "ab"}], self.graph["sources"], set(self.graph["sources"]))

    def test_saved_response_recovery_respects_request_scope_and_source_bytes(self):
        entities = [{"entity_id": "ab"}]
        state = {"config": {"document_id": "DOC"}, "document_structure": self.graph,
                 "document_template": {"entity_template": {"entities": entities}},
                 "sequence_extraction": {"sources": {}, "decisions": [
                     {**copy.deepcopy(d), "status": "unresolved", "value": None,
                      "fragments": [], "source_conflicts": []} for d in self.decisions]}}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            attempt = root / "request/attempt-one"
            attempt.mkdir(parents=True)
            (attempt.parent / "input.json").write_text(json.dumps({"task": "sequence-extraction-v4", "payload": {
                "document_id": "DOC", "entities": entities, "loaded_sources": self.graph["sources"]}}))
            (attempt / "answer.json").write_text(json.dumps({"document_id": "DOC", "request_refs": [],
                "search_queries": [], "complete": True, "decisions": self.decisions}))
            (attempt / "events.jsonl").write_text(json.dumps({"type": "turn.completed"}))
            recovered, audit = recover_sequence_decisions(state, root)
            self.assertEqual(len(audit), 2)
            self.assertEqual(recovered['sequence_extraction']['decisions'][0]['value'], 'ACDEFGHIK')
            self.assertIsNone(state['sequence_extraction']['decisions'][0]['value'])
            changed = copy.deepcopy(state)
            changed['document_structure']['sources']['seq:R0C1']['text'] = 'ACDEYGHIK'
            _, audit = recover_sequence_decisions(changed, root)
            self.assertEqual([a['chain'] for a in audit], ['VL'])

    def test_conflict_requires_literal_quotes_and_different_sources(self):
        for change in ({"text_quote": "invented"}, {"text_ref": "seq:R0C1"}):
            decision = copy.deepcopy(self.decisions[0])
            decision["source_conflicts"][0].update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                ground_decision(decision, self.graph["sources"])

    def test_text_only_definition_remains_supported(self):
        decision = copy.deepcopy(self.decisions[0])
        decision.update(value="ACDEYGHIK", source_conflicts=[],
                        fragments=[{"source_ref": "method:p1", "quote": "ACDEYGHIK", "occurrence": 0}])
        decision["evidence_refs"] = ["method:p1"]
        sources = {"method:p1": self.graph["sources"]["method:p1"]}
        self.assertEqual(ground_decision(decision, sources)[0]["text"], "ACDEYGHIK")

    def test_confirmed_isotype_form_inherits_only_variable_domains_and_keeps_conflicts(self):
        sources = copy.deepcopy(self.graph["sources"])
        sources["assay:R1C0"] = {"kind": "cell", "table_id": "assay", "row_index": 1,
                                   "col_index": 0, "text": "Ab-hIgG2"}
        entities = [{"entity_id": "ab", "canonical_name": "Ab", "evidence_refs": ["seq:R0C0"]},
                    {"entity_id": "ab_higg2", "canonical_name": "Ab-hIgG2", "evidence_refs": ["assay:R1C0"]}]
        children = [{**copy.deepcopy(d), "entity_id": "ab_higg2", "status": "unresolved",
                     "value": None, "fragments": [], "source_conflicts": []} for d in self.decisions]
        bundle = {"decisions": self.decisions + children, "sources": {}}
        assignment = {"entity_id": "ab_higg2", "parent_entity_id": "ab", "chains": ["VH", "VL"],
                      "scope": "variable_regions", "basis": "user_confirmation", "note": "Same VH/VL confirmed"}
        result = apply_confirmed_sequence_inheritance(bundle, [assignment], sources, entities)
        for child, parent in zip(result["decisions"][2:], self.decisions):
            self.assertEqual(child["value"], parent["value"])
            self.assertEqual(child["fragments"], parent["fragments"])
            self.assertEqual(child["source_conflicts"], parent["source_conflicts"])
            self.assertIn("assay:R1C0", child["evidence_refs"])
        self.assertIsNone(bundle["decisions"][2]["value"])
        self.assertEqual(result["sequence_inheritance"][0]["scope"], "variable_regions")
        with self.assertRaisesRegex(ValueError, "user confirmation"):
            apply_confirmed_sequence_inheritance(bundle, [{**assignment, "basis": "name_suffix"}], sources, entities)


if __name__ == "__main__":
    unittest.main()
