import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dataagent.sda_inputs import prepare_sda_inputs, read_prepared
from dataagent.run_sda import main, run_from_ocr


class DataagentMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.table_path = self.root / "tables.jsonl"
        self.pages_path = self.root / "pages.jsonl"
        self.html = ("<table><tr><th>Antibody</th><th>IC50 (nM)</th></tr>"
                     "<tr><td>Ab1</td><td>0.5</td></tr><tr><td>Ab2</td><td>1.2</td></tr></table>")
        self.table = {"document_id": "DOC", "table_id": "table-1", "page": 1,
                      "status": "success", "backend": "vl", "text": self.html,
                      "text_sha256": hashlib.sha256(self.html.encode()).hexdigest(),
                      "bbox": [40, 200, 550, 500], "coordinate_space": "pdf_points"}
        self.page = {"document_id": "DOC", "page_number": 1, "status": "success", "pdf_sha256": "a" * 64,
                     "width": 600, "height": 800, "coordinate_space": "pdf_points",
                     "lines": [{"line_id": "p0001-l0001", "text": "Table 1. Binding inhibition IC50 by ELISA",
                                "bbox": [40, 160, 550, 190], "confidence": 0.99},
                               {"line_id": "p0001-l0002", "text": "Example 1. Ab1 and Ab2 inhibit human antigen binding.",
                                "bbox": [40, 80, 550, 110], "confidence": 0.98}]}
        self.write_inputs()

    def write_inputs(self):
        self.table_path.write_text(json.dumps(self.table) + "\n")
        self.pages_path.write_text(json.dumps(self.page) + "\n")

    def prepare(self, name="prepared"):
        path = prepare_sda_inputs("DOC", self.table_path, self.pages_path, self.root / name)
        return read_prepared(path, "DOC")

    def test_literal_values_sources_and_unknown_coordinates_survive(self):
        prepared = self.prepare()
        table = prepared["tables"][0]
        self.assertEqual(table["rows"][1][1]["text"], "0.5")
        self.assertEqual(table["rows"][1][1]["source_ref"], "table-1:R1C1")
        self.assertEqual(table["rows"][1][1]["bbox"], [])
        self.assertIsNone(table["rows"][1][1]["confidence"])
        self.assertEqual(table["sda_source"]["text"], self.html)
        self.assertEqual(prepared["document_blocks"][0]["source_ref"], "ocr:p0001-l0001")
        self.assertEqual(prepared["document_blocks"][0]["page_height"], 800)
        self.assertEqual(prepared["table_contexts"][0]["table_number"], "1")
        self.assertEqual(prepared["index_summary"]["performance"]["ocr_pages"], 0)

    def test_spans_keep_physical_cell_address(self):
        self.table["text"] = ("<table><tr><th rowspan='2'>Antibody</th><th colspan='2'>KD</th></tr>"
                              "<tr><th>Human</th><th>Mouse</th></tr></table>")
        self.table.pop("text_sha256")
        self.write_inputs()
        rows = self.prepare()["tables"][0]["rows"]
        self.assertEqual(rows[1][0]["source_ref"], "table-1:R0C0")
        self.assertEqual(rows[0][2]["source_ref"], "table-1:R0C1")

    def test_unstructured_table_keeps_evidence_without_blocking_valid_tables(self):
        bad = {**self.table, "table_id": "table-2", "text": "1-30",
               "text_sha256": hashlib.sha256(b"1-30").hexdigest()}
        self.table_path.write_text(json.dumps(self.table) + "\n" + json.dumps(bad) + "\n")
        prepared = self.prepare()
        self.assertEqual([t["source_id"] for t in prepared["tables"]], ["table-1"])
        rejected = prepared["unstructured_tables"][0]
        self.assertEqual(rejected["source_id"], "table-2")
        self.assertEqual(rejected["sda_source"]["text"], "1-30")
        self.assertEqual(rejected["rows"], [])
        self.assertEqual(prepared["warnings"][-1]["code"], "table_structure_unavailable")
        bad["text_sha256"] = "0" * 64
        self.table_path.write_text(json.dumps(self.table) + "\n" + json.dumps(bad) + "\n")
        with self.assertRaisesRegex(ValueError, "sha256 mismatch"):
            self.prepare("tampered-unstructured")

    def test_structured_directory_cli_checks_layout_and_reuses_crops(self):
        from PIL import Image

        structured = self.root / "structured"
        layout_dir = structured / "layout"
        table_dir, page_dir = structured / "table_ocr", structured / "page_ocr"
        for directory in (layout_dir, table_dir, page_dir):
            directory.mkdir(parents=True)
        image = layout_dir / "table-1.png"
        Image.new("RGB", (300, 200), "white").save(image)
        self.table["image_sha256"] = hashlib.sha256(image.read_bytes()).hexdigest()
        self.table_path, self.pages_path = table_dir / "tables.jsonl", page_dir / "pages.jsonl"
        self.write_inputs()
        (structured / "document.json").write_text(json.dumps({"document_id": "DOC"}))
        (page_dir / "document.json").write_text(json.dumps({"source_pdf_sha256": "a" * 64, "total_pages": 1}))
        manifest = {"document_id": "DOC", "processing_pdf_sha256": "a" * 64, "images": [{
            "id": "table-1", "label": "table", "page": 1, "bbox": self.table["bbox"], "image": image.name}]}
        layout_path = layout_dir / "DOC_manifest.json"
        layout_path.write_text(json.dumps(manifest))
        self.assertEqual(main(["--structured-dir", str(structured), "--output-dir", str(self.root / "cli"),
                               "--semantic-provider", "rules"]), 0)
        prepared = read_prepared(self.root / "cli/sda_input.json", "DOC")
        self.assertEqual(prepared["tables"][0]["width"], 300)
        self.assertEqual(prepared["warnings"], [])
        self.assertEqual(prepared["tables"][0]["image_path"], str(image))
        manifest["processing_pdf_sha256"] = "b" * 64
        layout_path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, "different working PDFs"):
            prepare_sda_inputs("DOC", self.table_path, self.pages_path, self.root / "wrong-layout",
                               layout_manifest=layout_path)

    def test_nested_sda_table_results_are_supported(self):
        self.table_path = self.root / "sda-result.json"
        wrapped = {"document_id": "DOC", "table_id": "table-1.png", "table_text": self.html,
                   "table_evidence": self.table}
        self.table_path.write_text(json.dumps(wrapped))
        prepared = self.prepare()
        self.assertEqual(prepared["tables"][0]["source_id"], "table-1")
        wrapped["table_text"] += "different"
        self.table_path.write_text(json.dumps(wrapped))
        with self.assertRaisesRegex(ValueError, "conflicting table_text"):
            self.prepare("wrong-text")

    def test_wrong_document_page_hash_or_failed_ocr_rejected(self):
        baseline = copy.deepcopy(self.table)
        changes = [{"document_id": "OTHER"}, {"page": 2}, {"text_sha256": "bad"}, {"status": "failed"}]
        for index, change in enumerate(changes):
            with self.subTest(change=change):
                self.table = {**baseline, **change}
                self.write_inputs()
                with self.assertRaises(ValueError):
                    self.prepare(f"bad-{index}")

    def test_mixed_pdf_and_duplicate_line_ids_rejected(self):
        other = {**self.page, "page_number": 2, "pdf_sha256": "b" * 64,
                 "lines": [{"line_id": "p0002-l0001", "text": "other", "bbox": []}]}
        self.pages_path.write_text(json.dumps(self.page) + "\n" + json.dumps(other))
        with self.assertRaises(ValueError):
            self.prepare("mixed")
        self.page["lines"].append(self.page["lines"][0])
        self.write_inputs()
        with self.assertRaises(ValueError):
            self.prepare("duplicate")

    def test_full_graph_reuses_both_ocr_inputs_without_running_ocr(self):
        with patch("dataagent.page_index.build_page_index", side_effect=AssertionError("Unexpected page OCR")), \
             patch("dataagent.agent.graph.extract_assay_method_pages", side_effect=AssertionError("Unexpected method OCR")), \
             patch("dataagent.document_context.extract_document_blocks", side_effect=AssertionError("Unexpected PDF read")):
            result = run_from_ocr("DOC", self.table_path, self.pages_path, self.root / "run",
                                  semantic_provider="rules", max_repair_rounds=0)
        self.assertEqual(result["document_id"], "DOC")
        self.assertEqual(result["status"], "partial")
        state = json.loads((self.root / "run/agent_state.json").read_text())
        self.assertEqual(state["selected_table_ids"], ["table-1"])
        self.assertEqual(len(state["document_blocks"]), 2)
        self.assertEqual(state["page_retrieval"]["ocr_requests"], 0)
        self.assertEqual(len(state["assay_method_pages"]), 1)
        self.assertTrue((self.root / "run/dataset_records.json").is_file())
        self.assertTrue((self.root / "run/agent_run.sqlite").is_file())
        self.assertGreater(len(result["records"]), 0)
        with self.assertRaisesRegex(ValueError, "new output directory"):
            run_from_ocr("DOC", self.table_path, self.pages_path, self.root / "run", semantic_provider="rules")

    def test_empty_table_set_supports_document_only_input(self):
        self.table_path.write_text("")
        result = run_from_ocr("DOC", self.table_path, self.pages_path, self.root / "empty",
                              semantic_provider="rules", max_repair_rounds=0)
        self.assertEqual(result["records"], [])
        self.assertEqual(result["status"], "partial")

    def test_semantic_graph_with_scripted_provider_checks_real_contracts(self):
        from PIL import Image

        calls = []
        root = self.root
        crop_dir = root / "crops"
        crop_dir.mkdir()
        image = crop_dir / "table-1.png"
        Image.new("RGB", (300, 200), "white").save(image)
        self.table["image_path"] = str(image)
        self.table["image_sha256"] = hashlib.sha256(image.read_bytes()).hexdigest()
        sequences = {"VH": "EVQLVESGGGLVQPGGSLRLSCAASGFTFSSYAMSWVRQAPGKGLEWVSAISGSGGSTYYADSVKGRFTISRDNSKNTLYLQMNSLRAEDTAVYYCARWGQGTLVTVSS",
                     "VL": "DIQMTQSPSSLSASVGDRVTITCRASQSIYSSLAWYQQKPGKAPKLLIYAASSLQSGVPSRFSGSGSGTDFTLTISSLQPEDFATYYCQQYNSYPWTFGQGTKVEIK"}
        for index, (chain, sequence) in enumerate(sequences.items(), 3):
            self.page["lines"].append({"line_id": f"p0001-l000{index}",
                "text": f"Ab1 {chain} variable region amino acid sequence: {sequence}",
                "bbox": [40, 510 + index * 25, 550, 530 + index * 25]})
        self.write_inputs()

        class ScriptedProvider:
            model, effort, profile = "fixture", "low", None

            def __init__(self, output_dir, **kwargs):
                self.audit_path = Path(output_dir) / "llm_calls.jsonl"
                self.cache_dir = root / "fixture_cache"
                self.calls = []

            def generate(self, task, instructions, payload, contract, **kwargs):
                self_test.assertTrue(instructions.strip())
                calls.append(task)
                if contract.__name__ == "ClassificationBatch":
                    self_test.assertEqual(kwargs["images"], [image])
                    response = {"tables": [{"table_id": "table-1", "primary_label": "assay_ic50",
                        "labels": ["assay_ic50"], "confidence": 0.99, "entity_axis": "rows",
                        "measurement_axis": "columns", "decision": "extract", "detected_endpoints": ["IC50"],
                        "evidence_refs": ["table-1:R0C1"], "explanation": "Explicit IC50 header",
                        "regions": [{"kind": "assay", "row_start": 1, "row_end": 2, "header_rows": [0],
                            "entity_column": 0, "condition_columns": [], "entity_groups": [],
                            "metrics": [{"endpoint": "IC50", "value_column": 1, "value_row": None,
                                "header_refs": ["table-1:R0C1"], "unit": "nM", "unit_refs": ["table-1:R0C1"]}],
                            "excluded_columns": [], "sequence_id_column": None, "sequence_column": None,
                            "chain_column": None, "name_column": None, "sequence_scope": "unknown",
                            "comparisons": []}], "issues": []}]}
                elif contract.__name__ == "DocumentPlan":
                    self_test.assertTrue(any("ELISA" in p["text"] for p in payload["passages"]))
                    region_id = payload["tables"][0]["classification"]["regions"][0]["region_id"]
                    response = {"document_id": "DOC", "entities": [{"entity_id": name,
                        "canonical_name": name, "raw_names": [name], "role": "antibody",
                        "parent_entity_id": None, "variant": None,
                        "evidence_refs": [f"table-1:R{index}C0"], "alias_evidence_refs": []}
                        for index, name in enumerate(("Ab1", "Ab2"), 1)],
                        "sequence_links": [], "assay_profiles": [{"table_id": "table-1", "region_id": region_id,
                            "protocol_id": "ELISA-1", "label": "Binding inhibition", "method": "ELISA", "system": None,
                            "species": ["human"], "endpoints": ["IC50"], "summary_zh": "结合抑制实验",
                            "summary_en": "Binding inhibition assay", "evidence_refs": ["method:p1"]}],
                        "unresolved": []}
                elif contract.__name__ == "SequenceExtractionStep":
                    decisions = []
                    for entity in payload["entities"]:
                        for index, (chain, sequence) in enumerate(sequences.items(), 3):
                            resolved = entity["entity_id"] == "Ab1"
                            ref = f"block:p0001-l000{index}"
                            if resolved:
                                self_test.assertIn(sequence, payload["loaded_sources"][ref]["text"])
                            decisions.append({"entity_id": entity["entity_id"], "chain": chain,
                                "status": "resolved" if resolved else "unresolved", "value": sequence if resolved else None,
                                "seq_id": None, "source_scope": "variable" if resolved else "unknown",
                                "fragments": [{"source_ref": ref, "quote": sequence, "occurrence": 0}] if resolved else [],
                                "evidence_refs": [ref] if resolved else [],
                                "rationale": "Explicit OCR definition" if resolved else "No sequence in supplied OCR"})
                    response = {"document_id": "DOC", "request_refs": [], "search_queries": [],
                        "complete": True, "decisions": decisions}
                elif contract.__name__ == "QuestionPlan":
                    response = {"questions": [{"task_ids": [t["id"]], "question": f"Ab2 的 {t['targets'][0]['chain']} 是否有明确原文定义？",
                        "question_en": "Does the source explicitly define this Ab2 chain?", "kind": "sequence",
                        "answer_type": "text", "names": ["Ab2"], "evidence_refs": t["evidence_refs"]}
                        for t in payload["tasks"]]}
                elif contract.__name__ == "QuestionAnswer":
                    response = {"document_id": "DOC", "question_id": payload["question_id"], "status": "uncertain",
                        "answer": "来源未定义 Ab2 对应序列，保留缺失。", "evidence": [],
                        "source_reviews": [], "sequence_updates": [], "assay_updates": []}
                else:
                    raise AssertionError(f"Unexpected task {task}")
                response = contract.model_validate(response).model_dump()
                kwargs["validate"](response)
                meta = {"task": task, "model": "fixture", "status": "validated", "cache_hit": False}
                self.calls.append(meta)
                return response, meta

        self_test = self
        with patch("dataagent.llm_runtime.provider_from_env", ScriptedProvider), \
             patch("dataagent.page_index.build_page_index", side_effect=AssertionError("Unexpected OCR")):
            result = run_from_ocr("DOC", self.table_path, self.pages_path, self.root / "semantic",
                                  semantic_provider="codex", classification_triage=False, max_repair_rounds=0,
                                  evaluation_enabled=False)
        self.assertEqual(len(result["measurements"]), 2)
        self.assertEqual({m["value"] for m in result["measurements"]}, {0.5, 1.2})
        self.assertEqual({m["value_ref"] for m in result["measurements"]}, {"table-1:R1C1", "table-1:R2C1"})
        self.assertEqual(len(result["records"]), 2)
        by_name = {r["antibody_id"]: r for r in result["records"]}
        self.assertEqual(by_name["Ab1"]["vh"], sequences["VH"])
        self.assertEqual(by_name["Ab1"]["vl"], sequences["VL"])
        self.assertFalse(by_name["Ab2"]["vh"])
        self.assertFalse(by_name["Ab2"]["vl"])
        self.assertEqual(sum(task.startswith(("table-classification", "document-template", "sequence-extraction")) for task in calls), 3)
        self.assertIn("collection-questions-v1", calls)
        self.assertIn("answer-questions-v1", calls)
        self.assertEqual(result["question_review"]["applied_chain_count"], 0)
        self.assertTrue(any(task.startswith("sequence-extraction") for task in calls))
        from dataagent.main_pipeline import export_sda_result
        from units.token_usage import TokenUsageRecorder
        exported = export_sda_result("DOC", self.root / "semantic", self.root / "sda", self.root,
                                     TokenUsageRecorder().report())
        self.assertEqual(exported["schema_version"], "antibody-dataset-2.0")
        rows = {r["NAME"]: r for r in exported["records"]}
        self.assertEqual(rows["Ab1"]["VH"], sequences["VH"])
        self.assertEqual(rows["Ab1"]["VL"], sequences["VL"])
        self.assertIsNone(rows["Ab2"]["VH"])
        self.assertEqual(exported["coverage"]["covered_result_cells"], 2)
        self.assertEqual(len(exported["experiments"]), 1)
        self.assertEqual(exported["experiments"][0]["METHOD"], "ELISA")
        self.assertTrue(exported["entities"][0]["METRIC"][0]["exp_id"])


if __name__ == "__main__":
    unittest.main()
