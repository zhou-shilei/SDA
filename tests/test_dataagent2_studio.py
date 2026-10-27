"""Exercise real dataagent2 extraction/export and the upload engine contract offline."""

import json
from pathlib import Path
import sys
import threading
import shutil
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pymupdf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import graph
from dataagent.run_sda import run_from_ocr
from dataagent2.contracts import empty_response
from dataagent2.inputs import load_input
from dataagent2.provider import Provider, write_json
from dataagent2.studio_pipeline import run_studio_pipeline, export_result, refresh_table_sequences
from dataagent2.ledger import Ledger
from studio.sda_data import Workspace
from studio.server import StudioServer
from studio.comparison import run_comparison
import test_dataagent_migration as fixtures

SEQUENCE = "EVQLVESGGGLVQPGGSLRLSCAASGFTFSSYAMSWVRQAPGKGLEWVSAISGSGGSTYYADSVKGRFTISRDNSKNTLYLQMNSLRAEDTAVYYCARDRGYWGQGTLVTVSS"


class ScriptedProvider(Provider):
    fail = False
    duplicate_experiment = False

    def generate(self, stage, instructions, payload):
        self.record({"attempt_id": f"{stage}-test", "stage": stage, "attempt": 1,
            "status": "failed" if self.fail else "ok",
            "usage": None if self.fail else {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110}})
        if self.fail:
            raise RuntimeError("scripted reader failure")
        rows = {row[6]: row[0] for row in payload["sources"]}
        result = empty_response()
        result["new_items"] = [{"name": n, "refs": [rows[n]]} for n in ("Ab1", "Ab2")]
        sid = rows["Ab1 VH variable sequence: " + SEQUENCE]
        result["links"] = [{"item": "Ab1", "region": "VH", "seq": None, "name": "Ab1 VH", "refs": [sid]}]
        result["sequences"] = [{"region": "VH", "seq": None, "name": "Ab1 VH",
            "parts": [{"s": sid, "start": SEQUENCE[:16], "end": SEQUENCE[-16:]}], "refs": [sid]}]
        result["experiments"] = [{"id": "e1", "table": "table-1", "method": "ELISA",
            "target": None, "species": None, "system": None, "conditions": None,
            "refs": [rows["Table 1. Binding inhibition IC50 by ELISA"]]}]
        if self.duplicate_experiment:
            result["experiments"].append({**result["experiments"][0], "method": "Binding ELISA"})
        result["observations"] = [{"item": name, "experiment": "e1", "metric": "IC50",
            "value": value, "unit": "nM", "s": rows[value], "refs": [rows[value], rows["IC50 (nM)"]]}
            for name, value in (("Ab1", "0.5"), ("Ab2", "1.2"))]
        result["relations"] = [{"subject": "Ab1", "relation": "tested_in", "object": "e1", "refs": [rows["Ab1"]]}]
        return result, {"cache_hit": False}


class Dataagent2StudioTests(unittest.TestCase):
    def setUp(self):
        self.case = fixtures.DataagentMigrationTests()
        self.case.setUp()
        self.addCleanup(self.case.temporary.cleanup)
        self.root = self.case.root
        self.output = self.root / "results/DOC"
        self.case.page["lines"].append({"line_id": "p0001-l0003", "text": "Ab1 VH variable sequence: " + SEQUENCE,
                                        "bbox": [40, 510, 550, 540]})
        self.case.write_inputs()

    def seed(self, *args, **kwargs):
        kwargs["semantic_provider"] = "rules"
        state = run_from_ocr(*args, **kwargs)
        # Simulate actual shared-stage usage without invoking an external model.
        (Path(args[3]) / "llm_calls.jsonl").write_text(json.dumps({"task": "document-template-test",
            "usage": {"input_tokens": 30, "output_tokens": 10, "total_tokens": 40}}) + "\n")
        return state

    def invoke(self):
        with patch("dataagent.run_sda.run_from_ocr", side_effect=self.seed), \
             patch("dataagent.agent.graph.build_graph", side_effect=AssertionError("Old full graph invoked")), \
             patch("dataagent.agent.graph.sequence_extract_node", side_effect=AssertionError("Old sequence agent invoked")), \
             patch("dataagent.agent.graph.repair_evidence_node", side_effect=AssertionError("Old repair invoked")), \
             patch("dataagent2.studio_pipeline.Provider", ScriptedProvider):
            return run_studio_pipeline(self.root, self.root / "results", self.case.pages_path,
                table_ocr=self.case.table_path, extract_sequences=False)

    def test_fresh_ocr_runs_new_reader_and_exports_sequences_measurements_and_provenance(self):
        report = self.invoke()
        self.assertNotEqual(report["status"], "failed", report["errors"])
        dataset = json.loads((self.output / "dataset.json").read_text())
        self.assertEqual(dataset["extraction_mode"], "dataagent2")
        self.assertEqual(len(dataset["records"]), 2)
        self.assertEqual(dataset["reader_coverage"]["completed_chunks"], dataset["reader_coverage"]["total_chunks"])
        template = json.loads((self.output / "template.completed.json").read_text())
        ab1 = next(e for e in template["entities"] if e["name"] == "Ab1")
        self.assertEqual(ab1["VH"]["sequence"]["value"], SEQUENCE)
        self.assertNotEqual(ab1["VL"]["sequence"]["status"], "present")

        self.assertEqual(template["agent"]["name"], "dataagent2")
        sources = json.loads((self.output / "document_sources.json").read_text())
        fragment = next(s for k, s in sources.items() if k.startswith("dataagent2:fragment:"))
        self.assertEqual(fragment["quote"], SEQUENCE)
        self.assertEqual(fragment["page"], 1)
        bundle = Workspace(self.root)._build("DOC", self.output, self.root)
        self.assertEqual(len(bundle["records"]), 2)
        self.assertEqual(len(bundle["ocr"]), 1)
        self.assertEqual(report["token_usage"]["totals"]["total_tokens"], 150)
        self.assertEqual(report["token_usage"]["totals"]["requests"], 2)

    def test_registry_counts_do_not_materialize_all_review_bundles(self):
        self.invoke()
        workspace = Workspace(self.root)
        with patch.object(workspace, "bundle", side_effect=AssertionError("Registry loaded complete review provenance")):
            patent = next(p for p in workspace.patents() if p["document_id"] == "DOC")
        self.assertEqual(patent["record_count"], 2)

    def test_reader_failure_preserves_partial_dataset_and_unknown_usage(self):
        with patch.object(ScriptedProvider, "fail", True):
            report = self.invoke()
        self.assertEqual(report["status"], "failed")
        self.assertIn("DOC", report["errors"])
        final = json.loads((self.output / "dataagent2/dataset.json").read_text())
        self.assertEqual(final["status"], "partial")
        self.assertTrue(final["reader_coverage"]["failed_chunks"])
        self.assertTrue((self.output / "template.completed.json").is_file())
        self.assertEqual(report["token_usage"]["totals"]["requests_without_usage"], 1)

    def test_repeated_experiment_ids_keep_variants_without_overwriting_metadata(self):
        with patch.object(ScriptedProvider, "duplicate_experiment", True):
            report = self.invoke()
        self.assertNotEqual(report["status"], "failed", report["errors"])
        template = json.loads((self.output / "template.completed.json").read_text())
        self.assertEqual(len(template["experiments"]), 1)
        experiment = template["experiments"][0]
        self.assertIsNone(experiment["METHOD"])
        self.assertEqual({v["method"] for v in experiment["SOURCE_VARIANTS"]}, {"ELISA", "Binding ELISA"})
        self.assertTrue(any(i["kind"] == "dataagent2_experiment_variants" for i in template["issues"]))
        self.assertEqual(len(json.loads((self.output / "dataset.json").read_text())["records"]), 2)

    def test_existing_output_is_preserved(self):
        self.output.mkdir(parents=True)
        marker = self.output / "dataset.json"
        marker.write_text('{"previous": true}')
        with self.assertRaisesRegex(ValueError, "全新输出目录"):
            self.invoke()
        self.assertEqual(marker.read_text(), '{"previous": true}')

    def test_constant_regions_export_with_original_citations(self):
        seed = self.root / "seed"
        run_from_ocr("DOC", self.case.table_path, self.case.pages_path, seed,
                     semantic_provider="rules", seed_only=True)
        data = load_input(seed)
        sid = next(k for k, s in data["sources"].items() if s["text"].endswith(SEQUENCE))
        ledger = Ledger(data)
        r = empty_response()
        r["new_items"] = [{"name": "Ab1", "refs": [sid]}]
        r["links"] = [{"item": "Ab1", "region": "CL", "name": "constant light", "seq": None, "refs": [sid]}]
        r["sequences"] = [{"region": "CL", "name": "constant light", "seq": None,
            "parts": [{"s": sid, "start": SEQUENCE[:16], "end": SEQUENCE[-16:]}], "refs": [sid]}]
        ledger.apply(r, [sid], "c1")
        final = ledger.assemble()
        final.update(status="partial", reader_coverage={})
        self.output.mkdir(parents=True)
        result = export_result(data, final, seed, self.output)
        ab1 = next(e for e in result["entities"] if e["NAME"] == "Ab1")
        self.assertIsNone(ab1["VL"]["sequence"])
        constant = ab1["other_sequences"][0]["record"]
        self.assertEqual(constant["region"], "CL")
        self.assertEqual(constant["sequence"], SEQUENCE)
        self.assertEqual(constant["evidence"][0]["quote"], SEQUENCE)

    def test_stale_sequence_ocr_refreshes_into_new_file(self):
        from document.sequence_ocr import VERSION, REVISION
        old = {**self.case.table, "text": '<table><tr><td>' + SEQUENCE + '</td></tr></table>',
               "vl_text": '<table><tr><td>' + SEQUENCE + '</td></tr></table>',
               "sequence_ocr": {"version": VERSION, "revision": REVISION - 1}}
        self.case.table_path.write_text(json.dumps(old) + '\n')
        original = self.case.table_path.read_bytes()
        output = self.root / "refreshed.jsonl"
        with patch("dataagent.sda_inputs._resolve_image", return_value=self.root / "original.png"), \
             patch("document.sequence_ocr.refine", side_effect=lambda e: {**e, "sequence_ocr": {"version": VERSION, "revision": REVISION}}) as recognize, \
             patch("document.pdf2doc._ground_table_cells"):
            self.assertEqual(refresh_table_sequences(self.case.table_path, output), output)
        recognize.assert_called_once()
        self.assertEqual(self.case.table_path.read_bytes(), original)
        with patch("document.sequence_ocr.refine", side_effect=AssertionError("Repeated current OCR")):
            self.assertEqual(refresh_table_sequences(output, self.root / "again.jsonl"), output)

    def test_current_failed_ocr_is_retried_using_isolated_image_assets(self):
        from document.sequence_ocr import VERSION, REVISION
        from PIL import Image
        image = self.root / "old-table.png"
        Image.new("RGB", (100, 100), "white").save(image)
        original = image.read_bytes()
        old = {**self.case.table, "text": '<table><tr><td>' + SEQUENCE + '</td></tr></table>',
               "sequence_ocr": {"version": VERSION, "revision": REVISION,
                                "cells": [{"ref": "T:R0C0", "status": "failed"}]}}
        self.case.table_path.write_text(json.dumps(old) + '\n')
        output = self.root / "new-run/refreshed.jsonl"

        def recognize(evidence):
            self.assertTrue(Path(evidence["image_path"]).is_relative_to(output.parent))
            self.assertEqual(Path(evidence["image_path"]).read_bytes(), original)
            return {**evidence, "sequence_ocr": {"version": VERSION, "revision": REVISION, "cells": []}}

        with patch("dataagent.sda_inputs._resolve_image", return_value=image), \
             patch("document.sequence_ocr.refine", side_effect=recognize) as retry, \
             patch("document.pdf2doc._ground_table_cells"):
            refresh_table_sequences(self.case.table_path, output)
        retry.assert_called_once()
        self.assertEqual(image.read_bytes(), original)

    def test_cli_dispatches_dataagent2_instead_of_old_pipeline(self):
        with patch("dataagent2.studio_pipeline.run_studio_pipeline", return_value={"status": "partial"}) as new, \
             patch("dataagent.main_pipeline.run_dataagent_pipeline", side_effect=AssertionError("Wrong engine")):
            self.assertEqual(graph.main(["--extraction-mode", "dataagent2", "--text-pages-jsonl", str(self.case.pages_path)]), 0)
        new.assert_called_once()

    def test_merged_cells_use_physical_addresses_once(self):
        self.case.table["text"] = "<table><tr><th rowspan='2'>Antibody</th><th colspan='2'>KD</th></tr><tr><th>Human</th><th>Mouse</th></tr></table>"
        self.case.table.pop("text_sha256")
        self.case.write_inputs()
        seed = self.root / "merged-seed"
        run_from_ocr("DOC", self.case.table_path, self.case.pages_path, seed, semantic_provider="rules", seed_only=True)
        data = load_input(seed)
        cells = [s for s in data["sources"].values() if s["kind"] == "cell"]
        self.assertEqual(len(cells), 4)
        self.assertEqual(len({s["source_ref"] for s in cells}), 4)
        self.assertEqual(next(s["row"] for s in cells if s["text"] == "Antibody"), 0)

    def test_comparison_uses_identical_ocr_and_keeps_running_baseline(self):
        workspace = Workspace(self.root)
        parent = workspace.state / "patents/DOC"
        structured = parent / "structured"
        for name in ("layout", "page_ocr", "table_ocr"):
            (structured / name).mkdir(parents=True)
        with pymupdf.open() as pdf:
            pdf.new_page()
            pdf.save(parent / "source.pdf")
        shutil.copyfile(self.case.pages_path, structured / "page_ocr/pages.jsonl")
        shutil.copyfile(self.case.table_path, structured / "table_ocr/tables.jsonl")
        write_json(structured / "document.json", {"document_id": "DOC"})
        write_json(structured / "summary.json", {"status": "complete", "total_pages": 1, "tables": 1})
        write_json(structured / "page_ocr/summary.json", {"status": "complete", "full_document_ocr_complete": True})
        workspace.update_job("DOC", status="processing", stage="sequence_extract", extraction_engine="dataagent1",
            filename="DOC.pdf", input_dir=str(structured / "layout"), result_dir=str(parent / "baseline/results/DOC"),
            structured_dir=str(structured), run_id="baseline")
        server = StudioServer(("127.0.0.1", 0), workspace)
        baseline = workspace.jobs()["DOC"]
        try:
            with patch.object(server.workers, "submit") as submit:
                comparison = server.compare("DOC", engine="dataagent2")
                again = server.compare("DOC", engine="dataagent2")
                self.assertEqual(comparison["document_id"], again["document_id"])
                submit.assert_called_once()
            identity = comparison["document_id"]
            snapshot = Path(comparison["structured_dir"])
            self.assertEqual((snapshot / "table_ocr/tables.jsonl").read_bytes(), self.case.table_path.read_bytes())
            self.assertEqual((snapshot / "page_ocr/pages.jsonl").read_bytes(), self.case.pages_path.read_bytes())
            self.assertEqual((workspace.state / "patents" / identity / "source.pdf").read_bytes(), (parent / "source.pdf").read_bytes())
            self.assertEqual(workspace.jobs()["DOC"], baseline)
            def command(document, args, log):
                self.assertEqual(document, identity)
                self.assertEqual(args[args.index("--document-id") + 1], "DOC")
                self.assertEqual(args[args.index("--extraction-mode") + 1], "dataagent2")
                with patch("dataagent.run_sda.run_from_ocr", side_effect=self.seed), \
                     patch("dataagent2.studio_pipeline.Provider", ScriptedProvider):
                    return graph.main(args[2:] + ["--no-document-template-agent"])
            with patch.object(server, "command", side_effect=command):
                run_comparison(server, identity, Path(comparison["log"]).parent)
            self.assertEqual(workspace.jobs()[identity]["status"], "completed", workspace.jobs()[identity])
            self.assertEqual(workspace.jobs()["DOC"], baseline)
            row = next(p for p in workspace.patents() if p["document_id"] == identity)
            self.assertEqual(row["record_count"], 2)
            self.assertEqual(row["extraction_engine"], "dataagent2")
            self.assertEqual(row["processing_metrics"]["time"]["scope"], "reanalysis")
        finally:
            server.server_close()


class EngineUploadTests(unittest.TestCase):
    def test_upload_validation_persistence_dispatch_and_retry(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = Workspace(root)
            server = StudioServer(("127.0.0.1", 0), workspace)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            with pymupdf.open() as pdf:
                pdf.new_page()
                pdf_bytes = pdf.tobytes()
            def upload(identity, engine):
                boundary = "sda-engine-test"
                body = f'--{boundary}\r\nContent-Disposition: form-data; name="document_id"\r\n\r\n{identity}\r\n'.encode()
                if engine is not None:
                    body += f'--{boundary}\r\nContent-Disposition: form-data; name="extraction_engine"\r\n\r\n{engine}\r\n'.encode()
                body += f'--{boundary}\r\nContent-Disposition: form-data; name="pdf"; filename="demo.pdf"\r\nContent-Type: application/pdf\r\n\r\n'.encode()
                body += pdf_bytes + f'\r\n--{boundary}--\r\n'.encode()
                return urlopen(Request(f"http://127.0.0.1:{server.server_port}/api/patents/import", data=body,
                    headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}), timeout=5)
            try:
                with patch.object(server.workers, "submit"):
                    for identity, engine in (("new-doc", "dataagent2"), ("old-doc", "dataagent1"), ("default-doc", None)):
                        with upload(identity, engine) as response:
                            patent = json.load(response)["patent"]
                        self.assertEqual(patent["extraction_engine"], engine or "dataagent1")
                    with self.assertRaises(HTTPError) as caught:
                        upload("bad-doc", "unknown")
                    self.assertEqual(caught.exception.code, 400)
                    self.assertFalse((workspace.state / "patents/bad-doc").exists())
                    job = workspace.jobs()["new-doc"]
                    run = Path(job["log"]).parent
                    commands = []
                    def command(identity, args, log):
                        commands.append(args)
                        work = Path(args[args.index("--run-dir") + 1])
                        write_json(work / "run_summary.json", {"status": "partial"})
                        return 0
                    with patch.object(server, "command", side_effect=command):
                        server.run_pipeline("new-doc", run)
                    self.assertEqual(commands[0][commands[0].index("--extraction-mode") + 1], "dataagent2")
                    self.assertEqual(server.enqueue("new-doc")["extraction_engine"], "dataagent2")
                    workspace.update_job("new-doc", status="completed")
                    self.assertEqual(server.enqueue("new-doc", engine="dataagent1")["extraction_engine"], "dataagent1")
                server.update_progress("new-doc", 'sda-dataagent2-progress {"phase":"reader","completed":2,"total":5}')
                job = workspace.jobs()["new-doc"]
                self.assertEqual((job["stage"], job["stage_current"], job["stage_total"]), ("reader", 2, 5))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
