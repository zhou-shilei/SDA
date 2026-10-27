"""Default VL parsing, evidence compatibility, cache and artifact lifecycle."""

from contextlib import ExitStack
from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import pymupdf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from document import pdf2doc as paten_ocr
from document.document_vl import normalise_result


TABLE = '<table><tr><td rowspan="2">m8</td><td>HCDR1</td><td>QOIL</td></tr><tr><td>HCDR2</td><td>ABC</td></tr></table>'


def prediction():
    result = Mock()
    result.json = {"res": {"parsing_res_list": [
        {"block_id": 0, "block_order": 1, "block_label": "paragraph_title", "block_content": "实施例 3",
         "block_bbox": [20, 40, 200, 80]},
        {"block_id": 1, "block_order": 2, "block_label": "table", "block_content": TABLE,
         "block_bbox": [20, 100, 300, 200]},
        {"block_id": 2, "block_order": None, "block_label": "image", "block_content": "",
         "block_bbox": [20, 220, 300, 280]},
    ]}}
    result.markdown = {"markdown_texts": '# 实施例 3\n\n' + TABLE + '\n<img src="imgs/chart.jpg">'}

    def save(directory):
        directory = Path(directory)
        (directory / "imgs").mkdir(parents=True, exist_ok=True)
        (directory / "imgs/chart.jpg").write_bytes(b"test asset")
        (directory / "page.md").write_text(result.markdown["markdown_texts"])

    result.save_to_markdown.side_effect = save
    return result


class DocumentVLTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data = self.root / "data"
        self.data.mkdir()
        self.pdf = self.data / "source.pdf"
        with pymupdf.open() as doc:
            for i in range(2):
                page = doc.new_page(width=200, height=300)
                page.insert_text((20, 60), "NATIVE TEXT MUST NOT BE USED")
                if i:
                    page.set_rotation(90)
            doc.save(self.pdf)
        self.models = self.root / "models"
        for model, filenames in (
            ("PP-DocLayoutV3", paten_ocr.MODEL_FILES),
            ("PaddleOCR-VL-1.6", ("model.safetensors", "config.json", "inference.yml", "tokenizer_config.json")),
            (paten_ocr.DETECTOR, paten_ocr.MODEL_FILES), (paten_ocr.RECOGNIZER, paten_ocr.MODEL_FILES),
        ):
            directory = self.models / model
            directory.mkdir(parents=True)
            for filename in filenames:
                (directory / filename).write_text(model + filename)
        self.config = paten_ocr.OCRConfig(device="cpu", max_side=600, model_root=str(self.models))
        self.output = self.root / "output"
        self.engine = Mock()
        self.engine.predict.side_effect = lambda *_args, **_kwargs: iter([prediction()])
        self.stack = ExitStack()
        self.create = self.stack.enter_context(patch.object(paten_ocr, "_create_page_ocr", return_value=self.engine))
        self.stack.enter_context(patch.object(paten_ocr, "_runtime_versions", return_value={"paddleocr": "test"}))
        self.stack.enter_context(patch.dict("os.environ", {
            "SDA_VL_MODEL_DIR": "", "SDA_LAYOUT_MODEL_DIR": "", "SDA_CACHE_ENABLED": "1"}))

    def tearDown(self):
        self.stack.close()
        self.temp.cleanup()

    def run_ocr(self, **kwargs):
        return paten_ocr.ocr_pdf(self.pdf, self.output, document_id="WO123",
                                 config=kwargs.pop("config", self.config), **kwargs)

    def read(self, path):
        return json.loads((self.output / path).read_text())

    def test_default_preserves_structure_coordinates_and_linked_markdown_without_native_text(self):
        self.assertEqual(paten_ocr.OCRConfig().backend, "vl")
        with patch.object(pymupdf.Page, "get_text", side_effect=AssertionError("native text used")):
            summary = self.run_ocr()
        self.assertEqual(summary["status"], "complete")
        self.assertEqual(summary["tables"], 2)
        page = self.read("pages/page-0001.json")
        self.assertEqual(page["blocks"][1]["block_content"], TABLE)
        self.assertEqual(page["blocks"][1]["bbox"], [10, 50, 150, 100])
        self.assertEqual(page["blocks"][1]["bbox_image"], [20, 100, 300, 200])
        self.assertEqual(page["lines"][1]["text"], TABLE)
        self.assertEqual(page["lines"][1]["block_id"], page["blocks"][1]["block_id"])
        self.assertTrue(all(line["confidence"] is None for line in page["lines"]))
        self.assertIsNone(page["quality"]["mean_confidence"])
        self.assertFalse(page["quality"]["confidence_available"])
        self.assertEqual(self.read("pages/page-0002.json")["rotation"], 90)
        md = (self.output / "document.md").read_text()
        self.assertIn(TABLE, md)
        self.assertIn('src="markdown/page-0001/imgs/chart.jpg"', md)
        self.assertNotIn("NATIVE TEXT", md)
        raw = self.read("raw/page-0001.json")
        self.assertEqual(raw["res"]["parsing_res_list"][1]["block_bbox"], [20, 100, 300, 200])
        self.assertIn("local VL", summary["llm_usage_scope"])

    def test_cache_validates_artifacts_without_loading_model_and_repairs_missing_page_assets(self):
        self.run_ocr()
        self.create.reset_mock()
        self.assertEqual(self.run_ocr()["cached_pages"], 2)
        self.create.assert_not_called()
        (self.output / "markdown/page-0001/imgs/chart.jpg").unlink()
        result = self.run_ocr()
        self.assertEqual(result["cached_pages"], 1)
        self.assertTrue((self.output / "markdown/page-0001/imgs/chart.jpg").is_file())
        self.create.reset_mock()
        result = self.run_ocr(config=replace(self.config, workers=2, render_prefetch=2))
        self.assertEqual(result["cached_pages"], 2)
        self.create.assert_not_called()

    def test_tokenizer_and_backend_changes_do_not_reuse_incompatible_pages(self):
        self.run_ocr()
        (self.models / "PaddleOCR-VL-1.6/tokenizer_config.json").write_text("changed tokenizer")
        self.assertEqual(self.run_ocr()["cached_pages"], 0)
        self.engine.predict.side_effect = lambda *_: iter([{"rec_texts": ["QOIL"], "rec_scores": [.9], "rec_boxes": [[20, 40, 200, 80]]}])
        summary = self.run_ocr(config=replace(self.config, backend="paddle"))
        self.assertEqual(summary["cached_pages"], 0)
        self.assertEqual(summary["backend"], "paddle")
        self.assertNotIn("blocks", self.read("pages/page-0001.json"))
        self.engine.predict.side_effect = lambda *_args, **_kwargs: iter([prediction()])
        self.assertEqual(self.run_ocr()["cached_pages"], 0)

    def test_completion_keeps_html_and_block_addresses(self):
        from schema_pipeline import load_completion_document

        self.run_ocr()
        pages = self.output / "pages.jsonl"
        page = json.loads(pages.read_text().splitlines()[0])
        self.assertEqual(page["lines"][1]["text"], TABLE)
        self.assertEqual(page["blocks"][1]["block_id"], "p0001-b0002")
        self.assertEqual(page["width"], 200)
        document = load_completion_document(self.data, "WO123", [], pages)
        row = next(p for p in document["paragraphs"] if p.get("block_label") == "table")
        self.assertEqual(row["text"], TABLE)
        self.assertEqual(row["id"], "ocr:p0001-b0002")
        self.assertEqual(row["ocr_backend"], "vl")

    def test_worker_path_keeps_structure_and_reuses_its_model(self):
        with patch.object(paten_ocr, "_WORKER_ENGINE", None), patch.object(paten_ocr, "_WORKER_INIT_ERROR", None):
            first = paten_ocr._ocr_page_job(self.pdf, 1, self.output / "images/page-0001.png", self.config, "cpu", self.models)
            second = paten_ocr._ocr_page_job(self.pdf, 2, self.output / "images/page-0002.png", self.config, "cpu", self.models)
        self.assertEqual(first["status"], "success")
        self.assertEqual(second["status"], "success")
        self.assertEqual(second["blocks"][1]["block_content"], TABLE)
        self.create.assert_called_once()

    def test_late_inference_failure_does_not_commit_partial_success(self):
        def failing(*_args, **_kwargs):
            yield prediction()
            raise RuntimeError("generation failed")

        self.engine.predict.side_effect = failing
        self.assertEqual(self.run_ocr(pages=[1])["failed_pages"], [1])
        self.assertEqual(self.read("pages/page-0001.json")["lines"], [])
        self.assertFalse((self.output / "raw/page-0001.json").exists())
        self.engine.predict.side_effect = lambda *_args, **_kwargs: iter([prediction()])
        self.assertEqual(self.run_ocr(pages=[1])["newly_recognized_pages"], 1)

    def test_invalid_vl_geometry_and_config_are_rejected(self):
        raw = prediction().json
        raw["res"]["parsing_res_list"][1]["block_bbox"][0] = float("nan")
        with self.assertRaises(ValueError):
            normalise_result(raw, 1, .5, .5)
        for kwargs in ({"backend": "invalid"}, {"max_new_tokens": 0}):
            with self.assertRaises(ValueError):
                paten_ocr.OCRConfig(**kwargs)


if __name__ == "__main__":
    unittest.main()
