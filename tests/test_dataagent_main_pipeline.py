"""Exercise the main entry, schema/UI bridge, OCR reuse and resume accounting."""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import graph
from dataagent.main_pipeline import collect_usage, export_sda_result, run_dataagent_pipeline
from dataagent.run_sda import run_from_ocr
from document import run_document_pdf as runner
from studio.sda_data import Workspace, review_payload
from studio.server import StudioServer
from units.token_usage import TokenUsageRecorder
import test_dataagent_migration as fixtures
import test_pdf2doc_web as web_fixtures


class DataagentMainTests(unittest.TestCase):
    def setUp(self):
        self.case = fixtures.DataagentMigrationTests()
        self.case.setUp()
        self.addCleanup(self.case.temporary.cleanup)
        self.root = self.case.root
        self.output = self.root / "results" / "DOC"

    def run_main(self, **options):
        return run_dataagent_pipeline(self.root, self.root / "results", self.case.pages_path,
            table_ocr=self.case.table_path, semantic_provider="rules", repair_rounds=0, **options)

    def test_default_graph_entry_uses_dataagent_and_exports_browsable_sda(self):
        with patch("dataagent.page_index.build_page_index", side_effect=AssertionError("Unexpected OCR")), \
             patch("dataagent.agent.graph.extract_document_blocks", side_effect=AssertionError("Unexpected PDF text")), \
             patch("document.document_extraction.extract_document", side_effect=AssertionError("Old semantic path used")):
            code = graph.main(["--image-dir", str(self.root), "--output-dir", str(self.root / "results"),
                "--text-pages-jsonl", str(self.case.pages_path), "--reuse-table-ocr-from", str(self.case.table_path),
                "--semantic-provider", "rules", "--document-repair-rounds", "0"])
        self.assertEqual(code, 0)
        dataset = json.loads((self.output / "dataset.json").read_text())
        self.assertEqual(dataset["schema_version"], "antibody-dataset-2.0")
        self.assertEqual(dataset["extraction_mode"], "dataagent")
        self.assertEqual(len(dataset["records"]), 2)
        self.assertEqual(dataset["coverage"]["covered_result_cells"], 2)
        self.assertTrue((self.output / "template.completed.json").is_file())
        self.assertTrue((self.output / "data_construction.json").is_file())
        bundle = Workspace(self.root)._build("DOC", self.output, self.root)
        self.assertEqual(len(bundle["records"]), 2)
        self.assertEqual(bundle["records"][0]["assay_endpoint"], "IC50")
        self.assertEqual(len(bundle["ocr"]), 1)
        self.assertEqual(bundle["records"][0]["evidence"][0]["row_index"], 1)

    def test_resume_reuses_snapshot_and_rejects_changed_ocr_repeatedly(self):
        self.assertNotEqual(self.run_main()["status"], "failed")
        original = (self.output / "dataset.json").read_bytes()
        self.assertNotEqual(self.run_main(resume=True)["status"], "failed")
        checkpoint = json.loads((self.output / "dataagent_checkpoint.json").read_text())
        self.assertEqual(len(checkpoint["attempts"]), 2)
        self.case.page["lines"][0]["text"] += " changed"
        self.case.write_inputs()
        for _ in range(2):
            with patch("dataagent.agent.graph.build_graph", side_effect=AssertionError("Model invoked before input check")):
                result = self.run_main(resume=True)
            self.assertEqual(result["status"], "failed")
            self.assertIn("原输入不一致", result["errors"]["DOC"])
        dataset = json.loads((self.output / "dataset.json").read_text())
        self.assertEqual(len(dataset["records"]), 2)
        self.assertEqual(json.loads(original)["records"], dataset["records"])

    def test_page_cell_geometry_reaches_result_citations_and_browser(self):
        entries = [("Antibody", [50, 210, 110, 220]), ("IC50 (nM)", [200, 210, 260, 220]),
                   ("Ab1", [50, 250, 70, 260]), ("0.5", [210, 250, 230, 260]),
                   ("Ab2", [50, 280, 70, 290]), ("1.2", [210, 280, 230, 290])]
        self.case.page["lines"].extend({"line_id": f"cell-{i}", "text": text, "bbox": box, "confidence": 0.99}
                                        for i, (text, box) in enumerate(entries))
        self.case.write_inputs()
        self.assertNotEqual(self.run_main()["status"], "failed")
        sources = json.loads((self.output / "document_sources.json").read_text())
        self.assertEqual(sources["table-1:R1C1"]["bbox_pdf"], [210, 250, 230, 260])
        bundle = Workspace(self.root)._build("DOC", self.output, self.root)
        cell = bundle["ocr"][0]["rows"][1][1]
        self.assertEqual(cell["bbox_pdf"], [210, 250, 230, 260])
        self.assertEqual(cell["precision"], "ocr_text")
        self.assertEqual(bundle["records"][0]["evidence"][0]["row_index"], 1)

    def test_unstructured_table_preserves_other_results_and_partial_coverage(self):
        bad = {**self.case.table, "table_id": "table-2", "text": "1-30",
               "text_sha256": hashlib.sha256(b"1-30").hexdigest()}
        self.case.table_path.write_text(json.dumps(self.case.table) + "\n" + json.dumps(bad) + "\n")
        self.assertEqual(self.run_main()["status"], "partial")
        dataset = json.loads((self.output / "dataset.json").read_text())
        self.assertEqual(len(dataset["records"]), 2)
        self.assertIn({"source_id": "table:table-2", "reason": "unparsed_table_grid"}, dataset["coverage"]["ambiguous_tables"])
        self.assertTrue(any(i["kind"] == "table_structure_unavailable" for i in dataset["issues"]))
        sources = json.loads((self.output / "document_sources.json").read_text())
        self.assertEqual(sources["table:table-2"]["text"], "1-30")
        self.assertTrue(any(json.loads(p.read_text()).get("table_id") == "table-2" for p in self.output.glob("table-*.json")))
        self.assertNotEqual(self.run_main(resume=True)["status"], "failed")
        bad["text"] = "changed"
        bad["text_sha256"] = hashlib.sha256(bad["text"].encode()).hexdigest()
        self.case.table_path.write_text(json.dumps(self.case.table) + "\n" + json.dumps(bad) + "\n")
        self.assertEqual(self.run_main(resume=True)["status"], "failed")

    def test_review_payload_keeps_displayed_evidence_without_duplicate_extraction_trees(self):
        self.run_main()
        bundle = Workspace(self.root)._build("DOC", self.output, self.root)
        slot = bundle["records"][0]["sda_fields"]["value"]
        slot["provenance"] = [{"table_id": "table-1", "quote": "0.5", "value_ref": "table-1:R1C1",
                               "extraction_evidence": {"duplicate": "x" * 10000}}]
        result = review_payload(bundle)
        self.assertEqual(len(result["records"]), 2)
        self.assertEqual(result["records"][0]["sda_fields"]["value"]["provenance"],
                         [{"table_id": "table-1", "quote": "0.5", "value_ref": "table-1:R1C1"}])
        self.assertIn("extraction_evidence", slot["provenance"][0])
        self.assertEqual(set(result["template"]), {"agent"})

    def test_pdf_page_loading_does_not_rebuild_review_dataset(self):
        self.run_main()
        workspace = Workspace(self.root)
        source = workspace.state / "patents/DOC/source.pdf"
        source.parent.mkdir(parents=True)
        import pymupdf
        with pymupdf.open() as document:
            document.new_page()
            document.save(source)
        with patch.object(workspace, "_build", side_effect=AssertionError("Dataset rebuilt for a PDF page")):
            bundle = workspace.page_bundle("DOC")
        self.assertEqual(bundle["pdf"], source)
        self.assertEqual(bundle["pages"]["page_count"], 1)

    def test_sequence_source_conflicts_survive_export_and_studio_entity_remapping(self):
        self.run_main()
        dataset = json.loads((self.output / "dataset.json").read_text())
        native_dir = Path(dataset["dataagent_directory"])
        state = json.loads((native_dir / "agent_state.json").read_text())
        identity = json.loads((native_dir / "dataset.json").read_text())["records"][0]["canonical_entity_key"]
        conflict = {"table_ref": "table-1:R1C0", "text_ref": "method:p1",
                    "table_quote": "Ab1", "text_quote": "Ab2", "message": "Different transcriptions"}
        state["sequence_extraction"] = {"decisions": [{"entity_id": identity, "chain": "VH",
                                                       "status": "unresolved", "rationale": "Conflicting transcriptions",
                                                       "source_conflicts": [conflict]}]}
        (native_dir / "agent_state.json").write_text(json.dumps(state))
        export_sda_result("DOC", native_dir, self.output, self.root, TokenUsageRecorder().report())
        records = Workspace(self.root)._build("DOC", self.output, self.root)["records"]
        matched = [r for r in records if r["sequence_source_conflicts"]]
        self.assertEqual(len(matched), 1)
        self.assertEqual(matched[0]["sequence_source_conflicts"][0]["table_quote"], "Ab1")
        self.assertEqual(matched[0]["sequence_source_conflicts"][0]["text_ref"], "method:p1")
        self.assertEqual(matched[0]["sequence_source_conflicts"][0]["sequence_status"], "unresolved")
        self.assertEqual(matched[0]["experiment_group"], "IC50")

    def test_sequence_locations_survive_sda_export_and_browser_bridge(self):
        from dataagent.sequence_evidence import source_digest
        self.run_main()
        native_dir = Path(json.loads((self.output / "dataset.json").read_text())["dataagent_directory"])
        state = json.loads((native_dir / "agent_state.json").read_text())
        identity = json.loads((native_dir / "dataset.json").read_text())["records"][0]["canonical_entity_key"]
        value, prefix = "ACDEFGHIKLMNPQRSTVWY", "Ab1 VH sequence: "
        source = {"text": prefix + value, "page": 1, "bbox": [40, 100, 400, 120],
                  "coordinate_space": "pdf_points", "kind": "document_block"}
        state.setdefault("document_structure", {}).setdefault("sources", {})["block:sequence-vh"] = source
        resolved = {"construct_key": identity}
        state["resolved_sequences"] = [r for r in state["resolved_sequences"] if r["construct_key"] != identity] + [resolved]
        resolved.update(vh=value, sequence_resolution={"vh": {"source_scope": "variable", "segments": [{
            "source_ref": "block:sequence-vh", "char_start": len(prefix), "char_end": len(prefix) + len(value),
            "text": value, "quote": value, "source_sha256": source_digest(source)}]}})
        (native_dir / "agent_state.json").write_text(json.dumps(state))
        export_sda_result("DOC", native_dir, self.output, self.root, TokenUsageRecorder().report())
        dataset = json.loads((self.output / "dataset.json").read_text())
        entity = next(e for e in dataset["entities"] if e["original_entity_id"] == identity)
        self.assertEqual(entity["sequence_locations"]["vh"]["status"], "located")
        records = Workspace(self.root)._build("DOC", self.output, self.root)["records"]
        record = next(r for r in records if r["vh"] == value)
        location = next(e for e in record["evidence"] if e["type"] == "sequence_index")["locations"]["vh"]
        self.assertEqual(location["segments"][0]["bbox"], [40, 100, 400, 120])
        self.assertEqual(location["segments"][0]["source_ref"], "block:sequence-vh")
        from dataagent.refresh_locations import refresh_result_locations
        before = json.loads((self.output / "dataset.json").read_text())
        report = refresh_result_locations(self.output)
        after = json.loads((self.output / "dataset.json").read_text())
        self.assertEqual(after["records"], before["records"])
        self.assertEqual(after["entities"][0]["VH"], before["entities"][0]["VH"])
        self.assertTrue((Path(report["backup_dir"]) / "dataset.json").is_file())
        self.assertEqual(report["statuses"].get("located"), 1)

    def test_all_endpoint_exports_survive_with_source_notation(self):
        self.case.table["text"] = ("<table><tr><th>Antibody</th><th>IC50 (nM)</th><th>KD (nM)</th><th>EC50 (nM)</th></tr>"
            "<tr><td>Ab1</td><td>&lt;0.5</td><td>1e-9</td><td>4.0</td></tr></table>")
        self.case.table["text_sha256"] = hashlib.sha256(self.case.table["text"].encode()).hexdigest()
        self.case.write_inputs()
        native_dir = self.root / "native"
        native = run_from_ocr("DOC", self.case.table_path, self.case.pages_path, native_dir,
                              semantic_provider="rules", max_repair_rounds=0)
        native["measurements"] = [{"antibody_id": "Ab1", "metric": endpoint, "value": value,
            "operator": operator, "unit": "nM", "value_ref": f"table-1:R1C{index}",
            "source_table": "table-1", "header_refs": [f"table-1:R0C{index}"],
            "unit_refs": [f"table-1:R0C{index}"], "condition_refs": []}
            for index, (endpoint, value, operator) in enumerate((("IC50", 0.5, "<"), ("KD", 1e-9, "="), ("EC50", 4.0, "=")), 1)]
        (native_dir / "dataset.json").write_text(json.dumps(native))
        dataset = export_sda_result("DOC", native_dir, self.output, self.root, TokenUsageRecorder().report())
        rows = {r["METRIC"]: r for r in dataset["records"]}
        self.assertEqual(set(rows), {"IC50", "KD", "EC50"})
        self.assertEqual(rows["IC50"]["VALUE"], "<0.5")
        self.assertEqual(rows["KD"]["VALUE"], "1e-9")
        self.assertEqual(rows["EC50"]["VALUE"], 4.0)
        self.assertEqual(dataset["coverage"]["covered_result_cells"], 3)

    def test_model_reviews_survive_export_and_studio_without_numbering_duplication(self):
        self.case.test_semantic_graph_with_scripted_provider_checks_real_contracts()
        native = self.root / "semantic"
        state = json.loads((native / "agent_state.json").read_text())
        validation = next(v for v in state["construct_validations"] if v["construct_key"] == "Ab1")
        validation["anarci_review"] = {"status": "passed", "chains": {"VH": {"domains": [
            {"chain_type": "H", "regions": {"CDR3": "ACDF"}, "numbering": [{"position": 105, "residue": "A"}]}]}}}
        validation["esm2_review"] = {"status": "manual_review", "chains": {"VH": {
            "metric_status": "scored", "mean_pll": -1.2345, "pseudo_perplexity": 3.4367}}}
        state["sequence_evaluation"] = {"status": "completed", "summary": {"construct_count": 2}}
        (native / "agent_state.json").write_text(json.dumps(state))
        exported = export_sda_result("DOC", native, self.output, self.root, TokenUsageRecorder().report())
        self.assertEqual(exported["sequence_evaluation"]["status"], "completed")
        record = next(r for r in Workspace(self.root)._build("DOC", self.output, self.root)["records"] if r["antibody_id"] == "Ab1")
        self.assertEqual(record["anarci_review_status"], "passed")
        self.assertEqual(record["esm2_review"]["chains"]["VH"]["mean_pll"], -1.2345)
        self.assertNotIn("numbering", record["anarci_review"]["chains"]["VH"]["domains"][0])
        self.assertIn("numbering", validation["anarci_review"]["chains"]["VH"]["domains"][0])

    def test_retry_and_resume_usage_is_counted_once_and_unknown_stays_unknown(self):
        attempts = []
        event_paths = []
        for index, counts in enumerate(((10, 2), (20, 3)), 1):
            directory = self.root / f"request-{index}"
            directory.mkdir()
            path = directory / "events.jsonl"
            path.write_text(json.dumps({"type": "turn.completed", "usage": {
                "input_tokens": counts[0], "output_tokens": counts[1], "cached_input_tokens": 5}}))
            event_paths.append(str(path))
        missing = self.root / "timeout/events.jsonl"
        event_paths.append(str(missing))
        for index, cached in enumerate((False, True), 1):
            directory = self.root / f"attempt-{index}"
            directory.mkdir()
            call = {"task": "classification", "cache_hit": cached, "request_event_files": event_paths}
            (directory / "llm_calls.jsonl").write_text(json.dumps(call) + "\n")
            attempts.append({"directory": str(directory)})
        usage = collect_usage(attempts, "DOC")["totals"]
        self.assertEqual(usage["total_tokens"], 35)
        self.assertEqual(usage["requests"], 3)
        self.assertEqual(usage["requests_without_usage"], 1)
        self.assertEqual(usage["cached_input_tokens"], 10)

    def test_pdf_resume_uses_dataagent_checkpoint_without_ocr(self):
        self.run_main()
        report = {"document_id": "DOC", "extraction_mode": "dataagent", "result_dir": str(self.output),
            "run_dir": str(self.root), "wall_seconds": 1, "phases": [], "preprocessing_status": "complete",
            "structured_document": str(self.root / "structured/document.json"),
            "started_at_utc": datetime.now(timezone.utc).isoformat()}
        (self.root / "run_summary.json").write_text(json.dumps(report))
        commands = []
        def call(command, **kwargs):
            commands.append(command)
            return graph.main(command[2:])
        with patch.object(runner.subprocess, "call", call), \
             patch("dataagent.page_index.build_page_index", side_effect=AssertionError("Unexpected OCR")):
            resumed = runner.resume_pdf(self.root, repair_rounds=0)
        self.assertTrue(resumed["resumed"])
        self.assertEqual(resumed["status"], "partial")
        self.assertIn("--dataagent-resume", commands[0])
        self.assertTrue((self.output / "report.html").is_file())

    def test_web_reanalysis_uses_saved_pages_and_the_dataagent_route(self):
        workspace = Workspace(self.root)
        inputs = self.root / "data/DOC"
        inputs.mkdir(parents=True)
        run_dataagent_pipeline(inputs, self.root / "results", self.case.pages_path,
                              table_ocr=self.case.table_path, semantic_provider="rules", repair_rounds=0)
        run = workspace.state / "patents/DOC/runs/reanalysis"
        workspace.update_job("DOC", input_dir=str(inputs), text_context_dir=str(run / "data/DOC"),
                             previous_input_dir=str(inputs), previous_result_dir=str(self.output),
                             result_dir=str(run / "results/DOC"), status="queued")
        server = StudioServer(("127.0.0.1", 0), workspace, python=sys.executable, ocr_device="cpu")
        self.addCleanup(server.server_close)
        commands = []
        def command(identity, args, log):
            commands.append(args)
            self.assertTrue(str(args[1]).endswith("/src/graph.py"))
            return graph.main(args[2:] + ["--semantic-provider", "rules", "--document-repair-rounds", "0"])
        with patch.object(server, "command", command), \
             patch("dataagent.page_index.build_page_index", side_effect=AssertionError("Unexpected page OCR")), \
             patch("dataagent.agent.graph.extract_document_blocks", side_effect=AssertionError("Unexpected PDF text")):
            server.run_pipeline("DOC", run, "analyze")
        self.assertEqual(workspace.jobs()["DOC"]["status"], "completed")
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0][commands[0].index("--text-pages-jsonl") + 1], str(self.case.pages_path))
        report = json.loads((run / "run_summary.json").read_text())
        self.assertEqual(report["extraction_mode"], "dataagent")
        self.assertEqual(len(workspace.bundle("DOC")["records"]), 2)

    def test_web_reanalysis_missing_full_ocr_fails_without_starting_any_ocr(self):
        self.run_main()
        workspace = Workspace(self.root)
        run = workspace.state / "patents/DOC/runs/no-pages"
        workspace.update_job("DOC", input_dir=str(self.root), text_context_dir=str(run / "data/DOC"),
                             previous_input_dir=str(self.root), previous_result_dir=str(self.output), status="queued")
        self.case.pages_path.unlink()
        server = StudioServer(("127.0.0.1", 0), workspace, python=sys.executable, ocr_device="cpu")
        self.addCleanup(server.server_close)
        with patch.object(server, "command", side_effect=AssertionError("No subprocess should start")):
            server.run_pipeline("DOC", run, "analyze")
        self.assertEqual(workspace.jobs()["DOC"]["status"], "failed")
        self.assertIn("缺少已保存的全文 OCR", workspace.jobs()["DOC"]["error"])

    def test_web_reanalysis_recovers_ocr_after_input_preparation_failed(self):
        with patch("dataagent.run_sda.prepare_sda_inputs", side_effect=ValueError("unparseable table")):
            self.assertEqual(self.run_main()["status"], "failed")
        workspace = Workspace(self.root)
        run = workspace.state / "patents/DOC/runs/recover"
        workspace.update_job("DOC", input_dir=str(self.root), text_context_dir=str(run / "data/DOC"),
                             previous_input_dir=str(self.root), previous_result_dir=str(self.output),
                             result_dir=str(run / "results/DOC"), status="queued")
        server = StudioServer(("127.0.0.1", 0), workspace, python=sys.executable, ocr_device="cpu")
        self.addCleanup(server.server_close)
        self.assertEqual(server.table_ocr_source("DOC", self.output), self.case.table_path)
        def command(identity, args, log):
            self.assertEqual(args[args.index("--reuse-table-ocr-from") + 1], str(self.case.table_path))
            return graph.main(args[2:] + ["--semantic-provider", "rules", "--document-repair-rounds", "0"])
        with patch.object(server, "command", command), \
             patch("dataagent.page_index.build_page_index", side_effect=AssertionError("Unexpected OCR")):
            server.run_pipeline("DOC", run, "analyze")
        self.assertEqual(workspace.jobs()["DOC"]["status"], "completed", workspace.jobs()["DOC"])
        self.assertEqual(len(json.loads((run / "results/DOC/dataset.json").read_text())["records"]), 2)


class DataagentPDFEntryTests(unittest.TestCase):
    def test_dataagent2_pdf_entry_forwards_parallel_settings(self):
        case = web_fixtures.WebDocumentFlowTests()
        case.setUp()
        self.addCleanup(case.tearDown)
        old_fake = case.fake_process
        commands = []

        def process(command, **kwargs):
            if str(command[1]).endswith("/graph.py"):
                commands.append(list(map(str, command)))
                code = graph.main(list(map(str, command[2:])))

                class Process:
                    import io
                    stdout = io.StringIO()

                    def wait(self):
                        return code

                    def terminate(self):
                        pass

                return Process()
            return old_fake(command, **kwargs)

        with patch.object(runner.subprocess, "Popen", process), \
             patch("dataagent.page_index.build_page_index", side_effect=AssertionError("Unexpected OCR")):
            report = runner.run_pdf(case.case.source, case.root / "parallel", document_id="demo", device="cpu",
                text_backend="vl", extraction_mode="dataagent2", semantic_provider="rules",
                llm_workers=2, sequence_workers=23, reader_chunk_chars=6000)
        self.assertNotEqual(report["status"], "failed")
        for flag, expected in (("--sequence-workers", "23"), ("--llm-workers", "2"),
                               ("--reader-chunk-chars", "6000"), ("--extraction-mode", "dataagent2")):
            self.assertEqual(commands[0][commands[0].index(flag) + 1], expected)
        self.assertTrue(report["template_agent"]["seed_before_reader"])
        self.assertEqual(report["template_agent"]["reader_chunk_chars"], 6000)
        dataset = json.loads((Path(report["result_dir"]) / "dataset.json").read_text())
        self.assertEqual(dataset["extraction_engine"], "dataagent2")

    def test_fresh_pdf_entry_calls_real_dataagent_after_existing_ocr(self):
        case = web_fixtures.WebDocumentFlowTests()
        case.setUp()
        self.addCleanup(case.tearDown)
        old_fake = case.fake_process
        commands = []
        def process(command, **kwargs):
            if str(command[1]).endswith("/graph.py"):
                commands.append(list(map(str, command)))
                code = graph.main(list(map(str, command[2:])))
                class Process:
                    import io
                    stdout = io.StringIO()
                    def wait(self):
                        return code
                    def terminate(self):
                        pass
                return Process()
            return old_fake(command, **kwargs)
        with patch.object(runner.subprocess, "Popen", process), \
             patch("dataagent.page_index.build_page_index", side_effect=AssertionError("Unexpected OCR")):
            report = runner.run_pdf(case.case.source, case.root / "main", document_id="demo", device="cpu",
                text_backend="vl", template_agent=False, semantic_provider="rules")
        self.assertEqual(report["extraction_mode"], "dataagent")
        self.assertEqual(commands[0][commands[0].index("--extraction-mode") + 1], "dataagent")
        dataset = json.loads((Path(report["result_dir"]) / "dataset.json").read_text())
        self.assertEqual(dataset["schema_version"], "antibody-dataset-2.0")
        self.assertTrue((Path(report["result_dir"]) / "report.html").is_file())
        from dataagent.table_structure import verified_header_links
        prepared = json.loads((Path(dataset["dataagent_directory"]) / "sda_input.json").read_text())
        self.assertTrue(prepared["tables"][1]["verified_context"]["support_tables"])
        links = verified_header_links(prepared["tables"], prepared["table_contexts"])
        self.assertEqual(links[prepared["tables"][1]["source_id"]], [prepared["tables"][0]["source_id"]])


if __name__ == "__main__":
    unittest.main()
