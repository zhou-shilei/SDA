import asyncio
from concurrent.futures import ThreadPoolExecutor
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from document_extraction import (load_input, template_from_payload, coverage_audit, value_matches,
                                 validate_payload, merge_repair, extract_document, guard_entity_assignments)
from document_extraction import normalize_references
from document_extraction import deduplicate_ocr_measurements, complete_sequence_citations
from document_extraction import seed_explicit_variable_rows
from document_extraction import validate_shape
from template_agent import export_dataset, validate_template
from pipeline import PipelineConfig


def entity(identity, name="A"):
    return {"id": identity, "NAME": name, "ALIASES": [], "VH": None, "VL": None,
            "VH_SEQ_ID": None, "VL_SEQ_ID": None, "CONSTRUCT_TYPE": "antibody",
            "SEQUENCE_LINKS": [], "EVIDENCE": {"NAME": ["ocr:p0001-l0001"], "VH": [], "VL": []}, "NOTES": None}


def experiment(identity="exp1"):
    return {"id": identity, "NAME": "binding", "KEYWORDS": ["SPR"], "TAGS": ["spr_kd"],
            "METHOD": "SPR", "TARGET": "human T", "SPECIES": "human", "SYSTEM": "protein",
            "CONDITIONS": None, "EVIDENCE": ["ocr:p0001-l0001"]}


def record(identity="e1", ref="c1r1c1", value=1):
    return {"ENTITY_ID": identity, "NAME": "A alias", "METRIC": "IC50", "VALUE": value, "UNIT": "nM",
            "TARGET": None, "METHOD": "SPR", "CONDITIONS": None, "EXPERIMENT_ID": "exp1", "CELL_REFS": [ref],
            "EVIDENCE": {"MEASUREMENT": ["table:T-page-0001"], "CONTEXT": ["ocr:p0001-l0001"]}, "NOTES": None}


def payload():
    return {"entities": [entity("e1"), entity("e2")], "experiments": [experiment()],
            "records": [record()], "relations": [{"subject_id": "e2", "object_id": "e1",
             "type": "humanized_from", "DETAILS": "explicit source", "EVIDENCE": ["ocr:p0001-l0001"]}],
            "table_coverage": [{"source_id": "table:T-page-0001", "status": "measurements_extracted",
             "measurement_columns": [1], "header_rows": [0], "reason": "explicit header"}], "limitations": []}


class DocumentExtractionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.pages = self.root / "pages.jsonl"
        self.pages.write_text(json.dumps({"document_id": "DOC", "page_number": 1, "status": "success",
             "pdf_sha256": "hash", "lines": [{"line_id": "p0001-l0001", "text": "A humanized SPR human T"}]}) + "\n")
        self.tables = [{"document_id": "DOC", "table_id": "T-page-0001", "table_text":
            '<table><tr><td>Antibody</td><td>IC50 (nM)</td></tr><tr><td>A</td><td>1</td></tr><tr><td>B</td><td>0</td></tr></table>'}]
        self.inputs, self.sources, self.grids = load_input("DOC", self.pages, self.tables)

    def tearDown(self):
        self.tmp.cleanup()

    def test_schema_rejects_boolean_sequence_id(self):
        with self.assertRaisesRegex(ValueError, "Invalid JSON type"):
            validate_shape(True, {"type": ["integer", "null"]})

    def test_usage_does_not_double_count_cached_or_reasoning_tokens(self):
        from token_usage import parse_codex_events
        result = parse_codex_events(json.dumps({"type": "turn.completed", "usage": {
            "input_tokens": 100, "cached_input_tokens": 40, "output_tokens": 20, "reasoning_output_tokens": 5}}))
        self.assertEqual(result["usage"]["total_tokens"], 120)
        self.assertEqual(result["usage"]["uncached_input_tokens"], 60)

    def test_identical_names_keep_distinct_ids_aliases_relations_and_experiment_fields(self):
        data = payload()
        data["entities"][0]["ALIASES"] = ["A alias"]
        data["records"].append(record("e2", "c1r2c1", 0))
        template = template_from_payload("DOC", data, self.sources)
        self.assertEqual(len(template["entities"]), 2)
        self.assertNotEqual(template["entities"][0]["entity_id"], template["entities"][1]["entity_id"])
        self.assertEqual(template["entities"][0]["aliases"], ["A alias"])
        self.assertEqual(template["entities"][0]["METRIC"][0]["method"], "SPR")
        self.assertEqual(template["entities"][0]["METRIC"][0]["exp_id"], "exp1")
        self.assertEqual(template["relations"][0]["object_id"], template["entities"][0]["entity_id"])
        self.assertEqual([r["NAME"] for r in export_dataset(template)], ["A alias", "A alias"])
        self.assertEqual([r["VALUE"] for r in export_dataset(template)], [1, 0])
        validate_template(template)

    def test_coverage_finds_missing_cell_despite_table_complete_claim(self):
        report = coverage_audit(payload(), self.inputs, self.sources, self.grids)
        self.assertEqual(report["expected_result_cells"], 2)
        self.assertEqual(report["covered_result_cells"], 1)
        self.assertEqual(report["uncovered_cells"][0]["source_id"], "c1r2c1")
        self.assertEqual(report["uncovered_cells"][0]["raw_value"], "0")

    def test_zero_qualified_and_scientific_values_are_not_silently_coerced(self):
        self.assertTrue(value_matches(0, "0.00"))
        self.assertFalse(value_matches(0, ""))
        self.assertTrue(value_matches(1e-9, "1.00E-09"))
        self.assertFalse(value_matches(1, ">1"))
        self.assertTrue(value_matches(">1", "> 1"))

    def test_unknown_reference_fails_and_wrong_value_is_not_counted_covered(self):
        data = payload()
        data["records"][0]["CELL_REFS"] = ["unknown"]
        with self.assertRaises(ValueError):
            validate_payload(data, self.sources)
        data["records"][0]["CELL_REFS"] = ["c1r1c1"]
        data["records"][0]["VALUE"] = 999
        self.assertTrue(validate_payload(data, self.sources))
        self.assertEqual(coverage_audit(data, self.inputs, self.sources, self.grids)["covered_result_cells"], 0)

    def test_foreign_document_missing_pages_and_failed_ocr_are_rejected(self):
        with self.assertRaises(ValueError):
            load_input("OTHER", self.pages, self.tables)
        page = json.loads(self.pages.read_text())
        page["page_number"] = 2
        self.pages.write_text(json.dumps(page))
        with self.assertRaises(ValueError):
            load_input("DOC", self.pages, self.tables)

    def test_repair_is_bounded_and_does_not_repeat_global_read_or_existing_record(self):
        original = payload()
        patch = copy.deepcopy(original)
        patch["entities"], patch["experiments"], patch["relations"] = [], [], []
        patch["records"] = [record("e2", "c1r2c1", 0)]
        llm = type("LLM", (), {})()
        llm.chat = AsyncMock(side_effect=[json.dumps(original), json.dumps(patch)])
        result = asyncio.run(extract_document("DOC", self.tables, self.pages, self.root / "out", llm, repair_rounds=2))
        self.assertEqual(llm.chat.await_count, 2)
        self.assertEqual(len(result["records"]), 2)
        self.assertEqual(result["report"]["coverage"]["uncovered_cells"], [])
        self.assertEqual(len(merge_repair(merge_repair(original, patch), patch)["records"]), 2)

    def test_no_repair_progress_stops_early_and_preserves_gap(self):
        data = payload()
        llm = type("LLM", (), {})()
        llm.chat = AsyncMock(return_value=json.dumps(data))
        result = asyncio.run(extract_document("DOC", self.tables, self.pages, self.root / "out", llm, repair_rounds=3))
        self.assertEqual(llm.chat.await_count, 2)
        self.assertEqual(result["report"]["status"], "partial")

    def test_document_mode_requires_full_text_input(self):
        with self.assertRaises(ValueError):
            PipelineConfig(extraction_mode="document")
        with self.assertRaises(ValueError):
            PipelineConfig(extraction_mode="unknown")

    def test_batch_pipeline_document_mode_skips_per_table_model_and_exports_existing_template(self):
        from pipeline import BatchPipeline
        inputs = self.root / "inputs"
        inputs.mkdir()
        image = inputs / "T-page-0001.png"
        image.write_bytes(b"image fixture")
        (inputs / "DOC_manifest.json").write_text(json.dumps({"document_id": "DOC", "source_pdf": "source.pdf",
            "images": [{"id": image.name, "image": image.name, "page": 1, "bbox": [0, 0, 10, 10]}]}))
        expected = payload()
        expected["records"][0]["EVIDENCE"]["MEASUREMENT"] = ["table:" + image.name]
        expected["records"].append(record("e2", "c1r2c1", 0))
        expected["records"][1]["EVIDENCE"]["MEASUREMENT"] = ["table:" + image.name]
        expected["table_coverage"][0]["source_id"] = "table:" + image.name
        llm = type("LLM", (), {})()
        llm.chat = AsyncMock(return_value=json.dumps(expected))
        config = PipelineConfig(extraction_mode="document", text_pages_jsonl=str(self.pages),
                                text_sequences=False, page_evidence=False, document_repair_rounds=0)
        pipeline = BatchPipeline(inputs, self.root / "out", config, llm)
        evidence = {"text": self.tables[0]["table_text"], "page": 1}
        with patch("pipeline.extract_table_evidence", return_value=evidence), ThreadPoolExecutor(1) as pool:
            report = asyncio.run(pipeline.run(ocr_executor=pool))
        self.assertEqual(report["data_construction"]["records"], 2)
        self.assertEqual(report["data_construction"]["failed"], 0, report)
        self.assertEqual(llm.chat.await_count, 1)
        exported = json.loads((self.root / "out/DOC/dataset.json").read_text())
        self.assertEqual(len(exported["experiments"]), 1)
        self.assertEqual(len(exported["relations"]), 1)
        self.assertEqual([r["VALUE"] for r in exported["records"]], [1, 0])

    def test_studio_exposes_experiment_keywords_and_runtime_without_inventing_old_timings(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from studio.sda_data import Workspace
        result = self.root / "results/DOC"
        result.mkdir(parents=True)
        template = template_from_payload("DOC", payload(), self.sources)
        (result / "template.completed.json").write_text(json.dumps(template))
        (result / "dataset.json").write_text(json.dumps({"document_id": "DOC", "status": "partial", "records": export_dataset(template)}))
        workspace = Workspace(self.root)
        data = workspace.bundle("DOC")
        self.assertEqual(data["records"][0]["experiment_group"], "binding")
        self.assertEqual(data["records"][0]["assay_method"], "SPR")
        self.assertEqual(data["records"][0]["antibody_id"], "A alias")
        self.assertEqual(data["records"][0]["canonical_antibody_id"], "A")
        self.assertEqual(data["records"][0]["experiment_id"], "exp1")
        self.assertTrue(data["records"][0]["assay_keywords"])
        self.assertEqual(data["run"], {})

    def test_wrong_entity_assignment_cannot_borrow_another_molecules_sequence(self):
        data = payload()
        data["entities"][0]["VH"] = "ABC"
        row = record("e1", "c1r2c1", 0)
        row["NAME"] = "B"
        data["records"] = [row]
        guarded, issues = guard_entity_assignments(data, self.sources, self.grids)
        assigned = next(e for e in guarded["entities"] if e["id"] == guarded["records"][0]["ENTITY_ID"])
        self.assertEqual(assigned["NAME"], "B")
        self.assertIsNone(assigned["VH"])
        self.assertIsNone(assigned["VL"])
        self.assertEqual(issues[0]["kind"], "entity_assignment_conflict")
        self.assertEqual(data["records"][0]["ENTITY_ID"], "e1")

    def test_later_header_in_a_mixed_table_does_not_hide_earlier_result_cells(self):
        data = payload()
        data["table_coverage"][0]["header_rows"] = [0, 9, 10]
        audit = coverage_audit(data, self.inputs, self.sources, self.grids)
        self.assertEqual(audit["expected_result_cells"], 2)

    def test_qualified_cell_refs_are_resolved_only_for_the_matching_table(self):
        data = payload()
        data["records"][0]["CELL_REFS"] = ["table:T-page-0001#c1r1c1"]
        normalized, changes = normalize_references(data, self.sources)
        self.assertEqual(normalized["records"][0]["CELL_REFS"], ["c1r1c1"])
        self.assertEqual(len(changes), 1)
        validate_payload(normalized, self.sources)
        data["records"][0]["CELL_REFS"] = ["table:T-page-0001:c1r1c1"]
        normalized, _ = normalize_references(data, self.sources)
        self.assertEqual(normalized["records"][0]["CELL_REFS"], ["c1r1c1"])
        data["records"][0]["CELL_REFS"] = ["table:FOREIGN#c1r1c1"]
        normalized, _ = normalize_references(data, self.sources)
        with self.assertRaises(ValueError):
            validate_payload(normalized, self.sources)

    def test_resume_reuses_exact_input_checkpoint_and_rejects_changed_input(self):
        first = payload()
        first["records"].append(record("e2", "c1r2c1", 0))
        out = self.root / "out"
        out.mkdir()
        (out / "document_input.json").write_text(json.dumps(self.inputs))
        (out / "document_extraction.raw.json").write_text(json.dumps(first))
        llm = type("LLM", (), {})()
        llm.chat = AsyncMock(side_effect=AssertionError("checkpoint should not re-read full document"))
        result = asyncio.run(extract_document("DOC", self.tables, self.pages, out, llm,
                            resume_payload=out / "document_extraction.raw.json"))
        self.assertEqual(len(result["records"]), 2)
        llm.chat.assert_not_called()
        changed = json.loads(self.pages.read_text())
        changed["lines"][0]["text"] += " modified"
        self.pages.write_text(json.dumps(changed))
        with self.assertRaisesRegex(ValueError, "不完全一致"):
            asyncio.run(extract_document("DOC", self.tables, self.pages, out, llm,
                        resume_payload=out / "document_extraction.raw.json"))

    def test_cell_coverage_cannot_shrink_after_a_model_changes_its_column_claim(self):
        first = coverage_audit(payload(), self.inputs, self.sources, self.grids)
        data = payload()
        data["table_coverage"][0]["header_rows"] = [0, 1, 2]
        second = coverage_audit(data, self.inputs, self.sources, self.grids, required_cells=first["result_cells"])
        self.assertEqual(second["expected_result_cells"], 2)
        self.assertEqual(len(second["uncovered_cells"]), 1)

    def test_duplicate_line_requires_literal_value_and_table_geometry(self):
        data = payload()
        duplicate = copy.deepcopy(data["records"][0])
        duplicate["CELL_REFS"] = []
        duplicate["EVIDENCE"]["MEASUREMENT"] = ["ocr:p0001-l0001"]
        data["records"].append(duplicate)
        sources = copy.deepcopy(self.sources)
        sources['table:T-page-0001']['bbox'] = [0, 0, 100, 100]
        sources['ocr:p0001-l0001'].update(text='1', bbox=[10, 10, 20, 20])
        result, changes = deduplicate_ocr_measurements(data, sources)
        self.assertEqual(len(result['records']), 1)
        self.assertEqual(len(changes), 1)
        sources['ocr:p0001-l0001']['bbox'] = [200, 200, 210, 210]
        self.assertEqual(len(deduplicate_ocr_measurements(data, sources)[0]['records']), 2)
        sources['ocr:p0001-l0001'].update(text='999', bbox=[10, 10, 20, 20])
        self.assertEqual(len(deduplicate_ocr_measurements(data, sources)[0]['records']), 2)

    def test_sequence_mapping_can_cite_explicit_chinese_chain_and_number_cells(self):
        table = copy.deepcopy(self.tables)
        table[0]['table_text']='<table><tr><td>A</td><td>重链可变区</td><td>ABC</td><td>SEQ ID NO: 9</td></tr></table>'
        _,sources,grids=load_input('DOC',self.pages,table)
        data=payload()
        data['entities'][0].update(VH='ABC',VH_SEQ_ID=9)
        data['entities'][0]['EVIDENCE']['VH']=['c1r0c2']
        result,changes=complete_sequence_citations(data,sources,grids)
        self.assertIn('c1r0c3',result['entities'][0]['EVIDENCE']['VH'])
        self.assertIn('c1r0c1',result['entities'][0]['EVIDENCE']['VH'])
        self.assertTrue(changes)

    def test_resume_initializes_usage_with_prior_actual_events(self):
        from pipeline import BatchPipeline
        from token_usage import TokenUsageRecorder
        out=self.root/'out';out.mkdir()
        usage=TokenUsageRecorder()
        usage.record({'input_tokens':100,'output_tokens':20,'cached_input_tokens':30},stage='document_extraction',item_id='DOC')
        (out/'_token_usage.json').write_text(json.dumps(usage.report()))
        inputs=self.root/'inputs';inputs.mkdir()
        config=PipelineConfig(extraction_mode='document',document_resume='checkpoint.json',text_pages_jsonl=str(self.pages))
        pipeline=BatchPipeline(inputs,out,config,llm=object())
        totals=pipeline.timing.usage_recorder.report()['totals']
        self.assertEqual(totals['total_tokens'],120)
        self.assertEqual(totals['cached_input_tokens'],30)
        self.assertEqual(totals['requests'],1)

    def test_document_llm_passes_native_output_schema_to_codex_and_records_usage(self):
        from agent import codexllm
        from token_usage import TokenUsageRecorder
        usage=TokenUsageRecorder()
        spec={'type':'object','properties':{'links':{'type':'array','items':{'type':'string'}}},'required':['links'],'additionalProperties':False}
        events=[{'type':'item.completed','item':{'type':'agent_message','text':'{"links": []}'}},
                {'type':'turn.completed','usage':{'input_tokens':20,'output_tokens':5,'cached_input_tokens':0}}]
        process=type('Process',(),{'returncode':0})()
        process.communicate=AsyncMock(return_value=('\n'.join(json.dumps(x) for x in events).encode(),b''))
        create=AsyncMock(return_value=process)
        llm=codexllm(usage,artifact_dir=self.root/'requests',output_schema=spec,
                     model='gpt-6.1-sol',effort='xhigh',transport='http')
        with patch('agent.asyncio.create_subprocess_exec',create):
            result=asyncio.run(llm.chat(system='extract',user='source'))
        args=create.call_args.args
        self.assertEqual(args[args.index('--model')+1],'gpt-6.1-sol')
        self.assertIn('model_reasoning_effort=xhigh',args)
        self.assertIn('model_providers.sda-http.supports_websockets=false',args)
        self.assertEqual(llm.cache_identity['model'],'gpt-6.1-sol')
        self.assertEqual(usage.events[0]['model'],'gpt-6.1-sol')
        self.assertEqual(usage.events[0]['effort'],'xhigh')
        self.assertIn('--output-schema',args)
        schema_path=Path(args[args.index('--output-schema')+1])
        self.assertEqual(json.loads(schema_path.read_text()),spec)
        self.assertEqual(json.loads(result),{'links':[]})
        self.assertEqual(usage.report()['totals']['total_tokens'],25)

    def test_explicit_variable_table_seeds_dot_separated_clone_names(self):
        table=copy.deepcopy(self.tables)
        sequence='QVQLVQSGAEVKKPGASVKVSCKAS'
        table[0]['table_text']=f'<table><tr><td>1.34.4</td><td>重链可变区</td><td>{sequence}</td><td>SEQ ID NO: 9</td></tr></table>'
        _,sources,grids=load_input('DOC',self.pages,table)
        result=seed_explicit_variable_rows(payload(),sources,grids)
        entity=next(e for e in result['entities'] if e['NAME']=='1.34.4')
        self.assertEqual(entity['VH_SEQ_ID'],9)
        self.assertEqual(entity['VH'],sequence)
        self.assertIn('c1r0c3',entity['EVIDENCE']['VH'])

    def test_duplicate_partial_variable_link_does_not_conflict_with_complete_chain(self):
        data=payload();sequence='QVQLVQSGAEVKKPGASVKVSCKAS'
        data['entities'][0].update(VH=sequence,VH_SEQ_ID=9)
        data['entities'][0]['SEQUENCE_LINKS']=[{'region':'VH','seq_id':9,'SOURCE_REFS':['ocr:p0001-l0001']}]
        sources=copy.deepcopy(self.sources);sources['ocr:p0001-l0001']['text']='QVQLVQSGAEVKKP'
        template=template_from_payload('DOC',data,sources)
        self.assertEqual(template['entities'][0]['VH']['sequence']['status'],'present')
        self.assertEqual(template['entities'][0]['VH']['sequence']['value'],sequence)

    def test_alias_cannot_shadow_an_explicitly_named_other_construct(self):
        data=payload()
        data['entities'][0].update(NAME='A-鼠源',ALIASES=['A'])
        data['records'][0].update(NAME='A',ENTITY_ID='e1')
        result,issues=guard_entity_assignments(data,self.sources,self.grids)
        self.assertEqual(result['records'][0]['ENTITY_ID'],'e2')
        self.assertEqual(issues[0]['resolution'],'unique_exact_name_with_explicit_name_evidence_on_both_sides')


if __name__ == "__main__":
    unittest.main()
