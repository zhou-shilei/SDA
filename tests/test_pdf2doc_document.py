"""Exercise the complete document pipeline with only model prediction replaced."""

from contextlib import ExitStack, redirect_stdout
from dataclasses import replace
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import pymupdf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from document import pdf2doc


HEADER = '<table><tr><th>Sample</th><th>IC50 (nM)</th></tr><tr><td>A</td><td>1.25</td></tr></table>'
CONTINUED = '<table><tr><td>B</td><td>2.5</td></tr></table>'


class PDFDocumentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source.pdf"
        with pymupdf.open() as pdf:
            for index in range(2):
                page = pdf.new_page(width=300, height=400)
                if index == 0:
                    page.insert_text((40, 185), "Table 1")
            pdf.set_metadata({"title": "Structure verification"})
            pdf.save(self.source)
        self.boxes = [[30, 200, 270, 380], [30, 20, 270, 200]]
        self.regions = self.root / "regions.json"
        self.regions.write_text(json.dumps({
            "document": {"path": str(self.source), "sha256": pdf2doc.file_digest(self.source)},
            "pages": [{"page": i + 1, "width_pt": 300, "height_pt": 400,
                       "detections": [{"id": f"table-{i + 1}", "label": "table", "confidence": 0.95,
                                       "bbox": box}]} for i, box in enumerate(self.boxes)], "errors": [],
        }))
        models = self.root / "models"
        for name, files in ((pdf2doc.LAYOUT_MODEL, pdf2doc.MODEL_FILES),
                            (pdf2doc.DETECTOR, pdf2doc.MODEL_FILES),
                            (pdf2doc.RECOGNIZER, pdf2doc.MODEL_FILES),
                            (pdf2doc.VL_MODEL, ("model.safetensors", "config.json", "inference.yml", "tokenizer_config.json"))):
            for filename in files:
                path = models / name / filename
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(name)
        self.config = pdf2doc.OCRConfig(device="cpu", model_root=str(models), max_side=400)
        self.output = self.root / "result"
        self.engine = Mock()
        self.engine.predict.side_effect = self.predict_page
        self.stack = ExitStack()
        self.stack.enter_context(patch.object(pdf2doc, "_create_page_ocr", return_value=self.engine))
        self.stack.enter_context(patch.object(pdf2doc, "_runtime_versions", return_value={"paddleocr": "test"}))
        self.table_model = self.stack.enter_context(patch.object(pdf2doc, "extract_table_evidence_batch", self.predict_tables))

    def tearDown(self):
        self.stack.close()
        pdf2doc.get_ocr.cache_clear()
        self.temporary.cleanup()

    def predict_page(self, image, **kwargs):
        index = int(Path(image).stem.split("-")[-1]) - 1
        blocks = [{"block_id": 0, "block_label": "text", "block_order": 0,
                   "block_content": f"Body page {index + 1}", "block_bbox": [10, 5, 280, 15]},
                  {"block_id": 1, "block_label": "table", "block_order": 1,
                   "block_content": HEADER if index == 0 else CONTINUED, "block_bbox": self.boxes[index]}]
        def save(directory):
            Path(directory, "page.md").write_text(blocks[1]["block_content"])
        return iter([SimpleNamespace(json={"res": {"parsing_res_list": blocks}},
                                     markdown={"markdown_texts": blocks[1]["block_content"]}, save_to_markdown=save)])

    def predict_tables(self, paths, metadata):
        return [pdf2doc.structure_table_evidence(HEADER if item["page"] == 1 else CONTINUED, path, item)
                for path, item in zip(paths, metadata)]

    def run_document(self, **kwargs):
        kwargs.setdefault("ocr_parallel", False)
        options = {"regions_json": self.regions, "orientation": "off", "crop_dpi": 72}
        options.update(kwargs.pop("layout_options", {}))
        return pdf2doc.run_pdf2doc(self.source, self.output, document_id="demo", config=self.config,
                                   layout_options=options, **kwargs)

    def read(self, name):
        return json.loads((self.output / name).read_text())

    def test_full_pipeline_exports_structure_and_verified_header_without_duplication(self):
        original = self.source.read_bytes()
        summary = self.run_document(table_batch_size=2)
        self.assertEqual(summary["status"], "complete")
        self.assertTrue(summary["full_document_complete"])
        self.assertEqual(pdf2doc._table_vl_model_dir, str(Path(self.config.model_root) / pdf2doc.VL_MODEL))
        document = self.read("document.json")
        self.assertEqual(document["metadata"]["title"], "Structure verification")
        self.assertEqual(document["source_pdf_sha256"], pdf2doc.file_digest(self.source))
        self.assertEqual(self.source.read_bytes(), original)
        self.assertEqual([p["page"] for p in document["pages"]], [1, 2])
        self.assertEqual(len(document["tables"]), 2)
        first, second = document["tables"]
        self.assertEqual(first["text"], HEADER)
        self.assertEqual(second["text"], CONTINUED)
        self.assertEqual(second["grid"][0][0]["text"], "B")
        self.assertEqual(second["inherited_header"][0]["table_id"], "table-1")
        self.assertEqual(second["logical_table_id"], first["logical_table_id"])
        self.assertEqual(second["inherited_header"][0]["header_rows"][0][0]["ref"], "table-1:R0C0")
        self.assertEqual(len(document["logical_tables"]), 1)
        for page in document["pages"]:
            self.assertEqual(page["reading_order"], [b["block_id"] for b in page["blocks"]])
            self.assertEqual(page["blocks"][1]["bbox_source"], page["blocks"][1]["bbox"])
            self.assertTrue((self.output / page["image"]).is_file())
        for table in document["tables"]:
            self.assertTrue(Path(table["image_path"]).is_file())
        markdown = (self.output / "document.md").read_text()
        self.assertEqual(markdown.count(HEADER), 1)
        self.assertEqual(markdown.count(CONTINUED), 1)
        self.assertNotIn("<table", (self.output / "document.txt").read_text())
        for name in ("pages.jsonl", "summary.json", "run_summary.json", "source_info.json",
                     "page_ocr/document.json", "table_ocr/table-1.json", "table_ocr/tables.jsonl"):
            self.assertTrue((self.output / name).is_file(), name)
        self.assertEqual(self.read("run_summary.json")["status"], "complete")

    def test_page_ocr_cell_boxes_reach_structured_and_reusable_table_exports(self):
        def predict(image, **kwargs):
            first = Path(image).stem.endswith("0001")
            texts = ["Sample", "IC50 (nM)", "A", "1.25"] if first else ["B", "2.5"]
            boxes = [[50, 210, 100, 220], [170, 210, 230, 220], [60, 250, 70, 260], [180, 250, 210, 260]] if first else [
                [60, 60, 70, 70], [180, 60, 210, 70]]
            return iter([{"rec_texts": texts, "rec_scores": [0.99] * len(texts), "rec_boxes": boxes}])
        self.engine.predict.side_effect = predict
        self.config = replace(self.config, backend="paddle")
        self.run_document()
        table = self.read("document.json")["tables"][0]
        raw_table = self.read("table_ocr/table-1.json")
        self.assertEqual(table["grid"][1][1]["bbox_pdf"], [180, 250, 210, 260])
        self.assertEqual(table["grid"][1][1]["bbox_source"], [180, 250, 210, 260])
        self.assertEqual(table["grid"][1][1]["precision"], "ocr_text")
        self.assertEqual(raw_table["cells"], table["cells"])
        self.assertEqual(raw_table["text"], HEADER)
        exported = [json.loads(line) for line in (self.output / "table_ocr/tables.jsonl").read_text().splitlines()]
        self.assertEqual(exported[0]["grid"][1][1]["bbox_pdf"], [180, 250, 210, 260])

    def test_table_batch_failure_is_recorded_and_page_vl_remains_available(self):
        output = io.StringIO()
        with patch.object(pdf2doc, "extract_table_evidence_batch", side_effect=RuntimeError("table failure")):
            with redirect_stdout(output):
                summary = self.run_document()
        self.assertEqual(summary["status"], "partial")
        document = self.read("document.json")
        self.assertEqual(len(document["errors"]), 2)
        self.assertTrue(all(t["source"] == "page_ocr" for t in document["tables"]))
        self.assertTrue(all(t["crop_ocr_status"] == "failed" for t in document["tables"]))
        self.assertEqual(document["tables"][0]["text"], HEADER)
        self.assertEqual(self.read("table_ocr/table-1.json")["status"], "failed")
        events = [json.loads(line.removeprefix("sda-ocr-progress "))
                  for line in output.getvalue().splitlines() if line.startswith("sda-ocr-progress ")]
        tables = [e for e in events if e["branch"] == "table"]
        self.assertEqual([e["completed"] for e in tables], [0, 1, 2, 2])
        self.assertEqual(tables[-1]["status"], "failed")
        self.assertEqual(tables[-2]["failed"], 2)
        self.assertEqual(tables[-2]["table_status"], "failed")

    def test_page_failure_preserves_successful_tables_and_marks_page(self):
        def predict(image, **kwargs):
            if "0002" in str(image):
                raise RuntimeError("page failure")
            return self.predict_page(image, **kwargs)
        self.engine.predict.side_effect = predict
        summary = self.run_document()
        self.assertEqual(summary["status"], "partial")
        document = self.read("document.json")
        self.assertEqual(document["pages"][1]["status"], "failed")
        self.assertEqual(len(document["tables"]), 2)
        self.assertIn("page failure", (self.output / "document.md").read_text())

    def test_disable_crop_ocr_reuses_page_tables_and_subset_has_explicit_coverage(self):
        with patch.object(pdf2doc, "extract_table_evidence_batch", side_effect=AssertionError("crop OCR called")):
            summary = self.run_document(table_ocr=False, layout_options={"pages": [1]})
        self.assertEqual(summary["status"], "complete")
        self.assertFalse(summary["full_document_complete"])
        document = self.read("document.json")
        self.assertEqual(document["unselected_pages"], [2])
        self.assertEqual(document["tables"][0]["source"], "page_ocr")

    def test_foreign_page_evidence_rejected_and_existing_output_not_overwritten(self):
        self.run_document()
        before = (self.output / "document.json").read_bytes()
        with self.assertRaisesRegex(ValueError, "新目录或空目录"):
            self.run_document()
        self.assertEqual((self.output / "document.json").read_bytes(), before)
        layout = self.read("layout/demo_manifest.json")
        pages = [json.loads(line) for line in (self.output / "page_ocr/pages.jsonl").read_text().splitlines()]
        pages[0]["pdf_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "工作 PDF"):
            pdf2doc.assemble_document(pdf2doc.load_pdf(self.source), layout,
                                      {"pages": pages, "tables": [], "errors": []})

    def test_cli_runs_all_stages_and_help_needs_no_models(self):
        with patch.object(pdf2doc, "run_pdf2doc", wraps=pdf2doc.run_pdf2doc) as run:
            status = pdf2doc.main(["--input", str(self.source), "--output-dir", str(self.output),
                                   "--document-id", "demo", "--regions-json", str(self.regions),
                                   "--orientation", "off", "--ocr-model-root", self.config.model_root,
                                   "--max-side", "400", "--crop-dpi", "72", "--ocr-execution", "serial"])
        self.assertEqual(status, 0)
        run.assert_called_once()
        result = subprocess.run([sys.executable, str(ROOT / "src/document/pdf2doc.py"), "--help"],
                                cwd=self.root, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--table-backend", result.stdout)
        self.assertIn("--layout-only", result.stdout)

    def test_rotation_maps_ocr_and_tables_back_to_original_page(self):
        self.boxes = [[40, 50, 300, 250], [40, 50, 300, 250]]
        def detect(backend, image):
            return [pdf2doc.RawDetection("table", 0.95, self.boxes[0], backend.model,
                                         backend.model_id, backend.name)]
        with patch.object(pdf2doc.OrientationDetector, "__call__", return_value={
                "clockwise": 90, "status": "rotated", "probes": []}), \
                patch.object(pdf2doc.PPDocLayoutV3Backend, "predict", detect):
            summary = self.run_document(layout_options={"regions_json": None, "orientation": "auto", "dpi": 72})
        self.assertEqual(summary["status"], "complete")
        document = self.read("document.json")
        self.assertEqual(document["source_coordinate_frame"], "unrotated_cropbox_points_top_left")
        self.assertEqual(document["pages"][0]["width_pt"], 400)
        self.assertEqual(document["pages"][0]["source_geometry"]["width_pt"], 300)
        self.assertEqual(document["tables"][0]["bbox_source"], [50, 100, 250, 360])
        self.assertEqual(document["pages"][0]["blocks"][1]["bbox_source"], [50, 100, 250, 360])

    def test_page_vl_tables_survive_when_layout_detects_no_tables(self):
        payload = json.loads(self.regions.read_text())
        for page in payload["pages"]:
            page["detections"] = []
        self.regions.write_text(json.dumps(payload))
        with patch.object(pdf2doc, "extract_table_evidence_batch", side_effect=AssertionError("no crops")):
            summary = self.run_document()
        self.assertEqual(summary["status"], "complete")
        document = self.read("document.json")
        self.assertEqual(len(document["tables"]), 2)
        self.assertTrue(all(t["source"] == "page_ocr" for t in document["tables"]))

    def test_column_mismatch_keeps_tables_separate_without_inherited_header(self):
        with patch(__name__ + ".CONTINUED", '<table><tr><td>B</td><td>2.5</td><td>3.5</td></tr></table>'):
            self.run_document()
        document = self.read("document.json")
        child = document["tables"][1]
        self.assertEqual(child["inherited_header"], [])
        self.assertIn("continuation_columns_do_not_align", child["continuation"]["issues"])
        self.assertEqual(len(document["logical_tables"]), 2)

    def test_plain_ocr_retains_raw_lines_but_exports_each_table_once(self):
        self.config = replace(self.config, backend="paddle")
        def predict(image, **kwargs):
            index = int(Path(image).stem.split("-")[-1]) - 1
            row = [40, 220, 240, 240] if index == 0 else [40, 70, 240, 90]
            return iter([{"rec_texts": ["Body", "DUPLICATE TABLE LINE"], "rec_scores": [0.99, 0.99],
                          "rec_boxes": [[10, 5, 280, 15], row]}])
        self.engine.predict.side_effect = predict
        self.run_document()
        document = self.read("document.json")
        self.assertEqual(len(document["pages"][0]["lines"]), 2)
        self.assertEqual(len(document["pages"][0]["blocks"]), 2)
        markdown = (self.output / "document.md").read_text()
        self.assertNotIn("DUPLICATE TABLE LINE", markdown)
        self.assertEqual(markdown.count(HEADER), 1)

    def test_total_ocr_failure_exports_failed_document_and_evidence(self):
        self.engine.predict.side_effect = RuntimeError("page failure")
        with patch.object(pdf2doc, "extract_table_evidence_batch", side_effect=RuntimeError("table failure")):
            summary = self.run_document()
        self.assertEqual(summary["status"], "failed")
        self.assertEqual(len(self.read("document.json")["errors"]), 4)
        self.assertIn("Table OCR failed", (self.output / "document.md").read_text())

    def test_markdown_assets_are_relative_to_unified_output(self):
        self.run_document()
        document = self.read("document.json")
        document["pages"][0]["blocks"].append({
            "block_label": "image", "block_content": '<img src="imgs/figure.png">',
        })
        pdf2doc.export_document(document, self.output)
        markdown = (self.output / "document.md").read_text()
        self.assertIn('src="page_ocr/markdown/page-0001/imgs/figure.png"', markdown)
        self.assertNotIn("<img", (self.output / "document.txt").read_text())

    def test_invalid_table_options_fail_before_creating_output_or_loading_models(self):
        for options in ({"table_batch_size": 0}, {"table_backend": "unknown"}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.run_document(**options)
        self.assertFalse(self.output.exists())

    def test_figure_paths_and_original_coordinates_are_exported(self):
        payload = json.loads(self.regions.read_text())
        payload["pages"][0]["detections"].append({
            "id": "figure-1", "label": "figure", "confidence": 0.95, "bbox": [40, 70, 120, 130],
        })
        self.regions.write_text(json.dumps(payload))
        self.run_document(layout_options={"labels": ("table", "figure")})
        figure = self.read("document.json")["figures"][0]
        self.assertTrue((self.output / figure["image"]).is_file())
        self.assertEqual(figure["bbox_source"], figure["bbox"])
        self.assertIn(f"]({figure['image']})", (self.output / "document.md").read_text())


if __name__ == "__main__":
    unittest.main()
