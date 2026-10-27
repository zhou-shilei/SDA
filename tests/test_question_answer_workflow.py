import copy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dataagent.answer_agent import AnswerAgent, validate_answer
from dataagent.llm_runtime import CodexUnavailable
from dataagent.llm_sequence_agent import ground_decision, resolve_agent_sequences
from dataagent.question_workflow import collect_diagnostics, measurement_signature, run_question_workflow, stage_answer
from dataagent.sequence_evidence import build_document_structure


class QuestionAnswerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        # An actual local image is necessary for the byte/provenance validation.
        import pymupdf
        with pymupdf.open() as doc:
            page = doc.new_page()
            page.insert_text((30, 40), "Ab.L SEQ ID NO: 3 ACDEFGHIK")
            page.get_pixmap().save(str(self.root / "seq.png"))
        self.tables = [{"source_id": "seq", "page": 1, "image": "seq.png", "rows": [
            [{"text": "Ab.L SEQ ID NO: 3"}, {"text": "ACDE_FGHIK"}]]}]
        self.graph = build_document_structure("DOC", self.tables, methods=[{"page": 1, "text": "Ab.L ACDEFGHIK"}])
        self.original = {"entity_id": "ab", "chain": "VL", "status": "unresolved", "value": None,
            "seq_id": 3, "source_scope": "variable", "fragments": [], "evidence_refs": ["seq:R0C0", "seq:R0C1", "method:p1"],
            "rationale": "Invalid underscore in the VL OCR cell",
            "source_conflicts": [{"table_ref": "seq:R0C1", "text_ref": "method:p1", "table_quote": "ACDE_FGHIK",
                "text_quote": "ACDEFGHIK", "message": "Table/text mismatch"}]}
        self.extraction = {"document_id": "DOC", "decisions": [self.original], "sources": self.graph["sources"]}
        self.template = {"entity_template": {"entities": [{"entity_id": "ab", "canonical_name": "Ab",
            "raw_names": ["Ab"], "evidence_refs": ["seq:R0C0"]}]}, "sequence_extraction": self.extraction}
        self.state = {"config": {"document_id": "DOC", "input_dir": str(self.root), "output_dir": str(self.root / "out")},
            "document_template": self.template, "sequence_extraction": self.extraction,
            "document_structure": self.graph, "ocr_results": self.tables, "llm_runs": [],
            "dataset_records": [{"record_id": "r1", "antibody_id": "Ab", "vl": "", "vh": "", "ic50": 0.5, "ic50_unit": "nM"}],
            "record_verification": {"findings": []}}
        self.question = {**collect_diagnostics(self.state)[0], "question": "Ab 的 VL 原图是否为 ACDEFGHIK？"}

    def provider(self, *, uncertain=False, corrupt=False, unavailable=False, with_update=True):
        original = self.original

        class Provider:
            def __init__(self):
                self.calls = []

            def generate(self, task, instructions, payload, contract, **kwargs):
                self.calls.append({"task": task, "images": kwargs.get("images", [])})
                if unavailable:
                    raise CodexUnavailable("429 capacity")
                if task == "collection-questions-v1":
                    response = {"questions": [{"task_ids": [t["id"]], "question": "Ab 的 VL 原图是什么？",
                        "question_en": "What is the VL in the original Ab table image?", "kind": "sequence",
                        "answer_type": "text", "names": ["Ab"], "evidence_refs": t["evidence_refs"]} for t in payload["tasks"]]}
                else:
                    update = {**copy.deepcopy(original), "status": "resolved", "value": "ACDEFGHIK",
                        "fragments": [{"source_ref": payload["review_sources"]["seq:R0C1"]["reviewed_ref"],
                            "quote": "ACDEFGHIK", "occurrence": 0}], "rationale": "Original image shows an underline, not an extra residue"}
                    response = {"document_id": "DOC", "question_id": payload["question_id"],
                        "status": "uncertain" if uncertain else "answered", "answer": "原图为 ACDEFGHIK，保留复核证据。",
                        "evidence": [] if uncertain else [{"source_ref": "seq:R0C0", "quote": "made up" if corrupt else "Ab.L", "occurrence": 0}],
                        "source_reviews": [] if uncertain or not with_update else [{"source_ref": "seq:R0C1", "transcription": "ACDEFGHIK", "rationale": "Underline is formatting"}],
                        "sequence_updates": [] if uncertain or not with_update else [update], "assay_updates": []}
                response = contract.model_validate(response).model_dump()
                kwargs["validate"](response)
                return response, {"task": task}

        return Provider()

    def answer(self, provider=None):
        return AnswerAgent(provider or self.provider(), image_roots=[self.root]).run("DOC", [self.question],
            self.graph, self.tables, self.template)[0]

    def test_question_deduplicates_shared_seq_without_dropping_targets(self):
        state = copy.deepcopy(self.state)
        state["sequence_extraction"]["decisions"].append({**copy.deepcopy(self.original), "entity_id": "other"})
        tasks = collect_diagnostics(state)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(len(tasks[0]["targets"]), 2)

    def test_image_answer_stages_literal_sequence_and_preserves_raw_conflicts(self):
        provider = self.provider()
        answer = self.answer(provider)
        self.assertEqual(provider.calls[0]["images"], [self.root / "seq.png"])
        changes = stage_answer(self.state, self.question, answer)
        updated = changes["sequence_extraction"]["decisions"][0]
        self.assertEqual(updated["value"], "ACDEFGHIK")
        self.assertEqual(updated["source_conflicts"], self.original["source_conflicts"])
        self.assertEqual(self.graph["sources"]["seq:R0C1"]["text"], "ACDE_FGHIK")
        resolved = resolve_agent_sequences(self.tables, changes["document_template"])
        self.assertEqual(resolved["ab"]["vl"], "ACDEFGHIK")
        self.assertIsNone(self.original["value"])

    def test_changed_image_or_raw_ocr_invalidates_previously_accepted_transcription(self):
        changes = stage_answer(self.state, self.question, self.answer())
        tables = copy.deepcopy(self.tables)
        tables[0]["rows"][0][1]["text"] = "ACDEYGHIK"
        self.assertFalse(resolve_agent_sequences(tables, changes["document_template"])["ab"]["vl"])
        (self.root / "seq.png").write_bytes(b"changed")
        self.assertFalse(resolve_agent_sequences(self.tables, changes["document_template"])["ab"]["vl"])

    def test_invalid_quotes_and_provider_failure_leave_original_missing(self):
        for provider in (self.provider(corrupt=True), self.provider(unavailable=True)):
            self.assertEqual(self.answer(provider)["status"], "failed")
        self.assertEqual(self.answer(self.provider(uncertain=True))["answer"]["sequence_updates"], [])
        self.assertIsNone(self.original["value"])

    def test_out_of_scope_or_explicit_seq_replacement_rejected(self):
        result = self.answer()
        for field, value in (("entity_id", "someone_else"), ("seq_id", 99), ("source_conflicts", [])):
            answer = copy.deepcopy(result)
            answer["answer"]["sequence_updates"][0][field] = value
            with self.assertRaises(ValueError):
                stage_answer(self.state, self.question, answer)

    def test_no_image_does_not_authorize_a_transcription(self):
        (self.root / "seq.png").unlink()
        # A transcription against a missing manifest is not executable.
        with self.assertRaises(ValueError):
            from dataagent.answer_agent import answer_sources
            answer_sources({"source_reviews": [{"source_ref": "seq:R0C1", "transcription": "ACDEFGHIK", "rationale": "guess"}]},
                           self.graph["sources"], {}, question_id="q1")

    def test_assay_changes_require_scope_and_quoted_species_system_and_method(self):
        from dataagent.assays import materialize_profiles
        state = copy.deepcopy(self.state)
        state["document_structure"]["sources"]["method:p1"]["text"] = "ELISA measures human protein binding."
        region = {"region_id": "binding", "kind": "assay", "row_start": 0, "row_end": 0,
                  "metrics": [{"endpoint": "EC50"}]}
        state["table_classifications"] = [{"table_id": "seq", "regions": [region]}]
        profile = {"table_id": "seq", "region_id": "binding", "protocol_id": "ELISA1", "label": "Binding",
            "method": "ELISA", "system": "protein", "species": ["human"], "endpoints": ["EC50"],
            "summary_zh": "结合实验", "summary_en": "Binding assay", "evidence_refs": ["method:p1"]}
        state["document_template"]["assay_template"] = {"region_profiles": materialize_profiles([profile], state["document_structure"]["sources"], {})}
        result = {"answer": {"document_id": "DOC", "question_id": self.question["id"], "status": "answered",
            "answer": "人源蛋白的 ELISA 实验", "evidence": [{"source_ref": "method:p1",
                "quote": "ELISA measures human protein binding.", "occurrence": 0}],
            "source_reviews": [], "sequence_updates": [], "assay_updates": [profile]},
            "loaded_refs": list(state["document_structure"]["sources"]), "reviewed_sources": {}}
        candidate = stage_answer(state, self.question, result)
        self.assertEqual(candidate["document_template"]["assay_template"]["region_profiles"][0]["species"], ["human"])
        result["answer"]["assay_updates"][0]["species"] = ["mouse"]
        with self.assertRaisesRegex(ValueError, "terminology"):
            stage_answer(state, self.question, result)

    def test_workflow_asks_then_answers_relinks_and_commits_after_checks(self):
        def relink(state):
            value = state["sequence_extraction"]["decisions"][0]["value"]
            return {"dataset_records": [{**state["dataset_records"][0], "vl": value}]}
        with patch("dataagent.agent.graph.link_node", side_effect=relink), \
             patch("dataagent.agent.graph.construct_validation_node", return_value={"construct_validations": []}) as checks, \
             patch("dataagent.agent.graph.verify_records_node", return_value={"record_verification": {"findings": []}}):
            result = run_question_workflow(self.state, self.provider(), image_roots=[self.root])
        self.assertEqual(result["question_review"]["changes"][0]["status"], "applied")
        self.assertEqual(result["dataset_records"][0]["vl"], "ACDEFGHIK")
        self.assertEqual(measurement_signature(result["dataset_records"]), measurement_signature(self.state["dataset_records"]))
        self.assertEqual(checks.call_count, 1)
        self.assertTrue((self.root / "out/question_review/answers.json").is_file())

    def test_workflow_rejects_measurement_change_and_keeps_original_data(self):
        with patch("dataagent.agent.graph.link_node", return_value={"dataset_records": [{**self.state["dataset_records"][0], "ic50": 9}]}):
            result = run_question_workflow(self.state, self.provider(), image_roots=[self.root])
        self.assertEqual(result["question_review"]["changes"][0]["status"], "rejected")
        self.assertIsNone(result["sequence_extraction"]["decisions"][0]["value"])
        self.assertEqual(result["dataset_records"][0]["ic50"], 0.5)

    def test_empty_and_failed_question_planning_do_not_mutate_data(self):
        state = copy.deepcopy(self.state)
        state["sequence_extraction"]["decisions"] = []
        provider = self.provider()
        result = run_question_workflow(state, provider, image_roots=[self.root])
        self.assertEqual(provider.calls, [])
        self.assertEqual(result["question_review"]["diagnostic_count"], 0)
        result = run_question_workflow(self.state, self.provider(unavailable=True), image_roots=[self.root])
        self.assertEqual(result["question_review"]["status"], "needs_review")
        self.assertEqual(result["sequence_extraction"], self.state["sequence_extraction"])

    def test_answer_budget_preserves_the_remaining_question_queue(self):
        state = copy.deepcopy(self.state)
        state["sequence_extraction"]["decisions"].append({**copy.deepcopy(self.original), "chain": "VH", "seq_id": 5})
        tasks = collect_diagnostics(state)
        plan = {"questions": [{"task_ids": [t["id"]], "question": f"第 {i} 个序列问题是什么？",
            "question_en": f"What is sequence question {i}?", "kind": "sequence", "answer_type": "text",
            "names": ["Ab"], "evidence_refs": t["evidence_refs"]} for i, t in enumerate(tasks)], "agent": {}}
        with patch("dataagent.question_agent.QuestionAgent.run", return_value=plan), \
             patch("dataagent.answer_agent.AnswerAgent.run", return_value=[]) as answer:
            result = run_question_workflow(state, self.provider(), image_roots=[self.root], max_questions=1)
        self.assertEqual(len(result["question_review"]["questions"]), 2)
        self.assertEqual(len(answer.call_args.args[1]), 1)
        self.assertEqual(result["question_review"]["status"], "needs_review")


if __name__ == "__main__":
    unittest.main()
