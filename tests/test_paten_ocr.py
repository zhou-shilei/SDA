"""Test page provenance, cache invalidation and resumable OCR without model calls."""

from contextlib import ExitStack
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import pymupdf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import paten_ocr


def prediction(text="原始 OQ 序列 1.34.4", score=0.35):
    return {"rec_texts": [text], "rec_scores": [score],
            "rec_polys": [[[10, 20], [100, 20], [100, 40], [10, 40]]]}


def fake_page_job(pdf_path, number, image, config, device, model_root, rendered=None):
    metadata, elapsed = rendered if rendered is not None else paten_ocr._render_pdf_page(pdf_path, number, image, config.max_side)
    return {**metadata, "status": "success", "lines": paten_ocr._normalise_result(prediction(), number, *metadata["pixel_to_page_scale"]),
            "timings": {"render_seconds": elapsed, "ocr_seconds": 0.01, "model_init_seconds": 0.02}}


class PatentOCRTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.pdf = self.root / "WO123.pdf"
        with pymupdf.open() as document:
            for index in range(3):
                page = document.new_page(width=200, height=300)
                page.insert_text((10, 60), "NATIVE TEXT MUST NOT BE USED")
                if index == 1:
                    page.set_rotation(90)
            document.save(self.pdf)
        self.models = self.root / "models"
        for model in (paten_ocr.DETECTOR, paten_ocr.RECOGNIZER):
            directory = self.models / model
            directory.mkdir(parents=True)
            for name in paten_ocr.MODEL_FILES:
                (directory / name).write_text(f"{model}:{name}")
        self.config = paten_ocr.OCRConfig(device="cpu", max_side=600, model_root=str(self.models), backend="paddle")
        self.output = self.root / "ocr"
        self.engine = Mock()
        self.engine.predict.side_effect = lambda *_: iter([prediction()])
        self.stack = ExitStack()
        self.create = self.stack.enter_context(patch.object(paten_ocr, "_create_ocr", return_value=self.engine))
        self.stack.enter_context(patch.object(paten_ocr, "_runtime_versions", return_value={"paddleocr": "test"}))

    def tearDown(self):
        self.stack.close()
        self.temporary.cleanup()

    def run_ocr(self, **kwargs):
        return paten_ocr.ocr_pdf(self.pdf, self.output, config=kwargs.pop("config", self.config), **kwargs)

    def read(self, path):
        return json.loads((self.output / path).read_text(encoding="utf-8"))

    def test_all_pages_raster_only_keep_original_characters_low_confidence_and_rotated_coordinates(self):
        before = paten_ocr._file_hash(self.pdf)
        with patch.object(pymupdf.Page, "get_text", side_effect=AssertionError("native text used")):
            summary = self.run_ocr()
        self.assertEqual(summary["status"], "complete")
        self.assertTrue(summary["full_document_ocr_complete"])
        self.assertEqual(summary["llm_tokens"], 0)
        self.assertEqual(self.create.call_count, 1)
        pages = [json.loads(line) for line in (self.output / "pages.jsonl").read_text().splitlines()]
        self.assertEqual([p["page_number"] for p in pages], [1, 2, 3])
        self.assertEqual(pages[0]["lines"][0]["text"], "原始 OQ 序列 1.34.4")
        self.assertEqual(pages[0]["lines"][0]["bbox"], [5, 10, 50, 20])
        self.assertEqual(pages[0]["quality"]["low_confidence_line_ids"], ["p0001-l0001"])
        self.assertEqual((pages[1]["width"], pages[1]["height"], pages[1]["rotation"]), (300, 200, 90))
        rect = pymupdf.Rect(pages[1]["lines"][0]["bbox"]) * pymupdf.Matrix(pages[1]["page_derotation_matrix"])
        self.assertEqual(list(rect), [10, 250, 20, 295])
        self.assertEqual(paten_ocr._file_hash(self.pdf), before)
        self.assertIn("[PDF page 2]", (self.output / "document.txt").read_text())
        self.assertNotIn("NATIVE TEXT", (self.output / "document.txt").read_text())

    def test_warm_run_never_loads_model_and_separates_cached_from_current_time(self):
        first = self.run_ocr()
        self.create.reset_mock()
        self.engine.reset_mock()
        second = self.run_ocr()
        self.create.assert_not_called()
        self.engine.predict.assert_not_called()
        self.assertEqual(second["cached_pages"], 3)
        self.assertEqual(second["newly_recognized_pages"], 0)
        self.assertEqual(second["timings"]["ocr_seconds"], 0)
        self.assertEqual(second["timings"]["model_init_seconds"], 0)
        self.assertEqual(second["saved_page_ocr_seconds"], first["saved_page_ocr_seconds"])

    def test_failed_page_is_retried_without_repeating_successful_pages(self):
        def predict(path):
            if "0002" in path:
                raise RuntimeError("OCR device failure")
            return iter([prediction()])
        self.engine.predict.side_effect = predict
        first = self.run_ocr()
        self.assertEqual(first["status"], "partial")
        self.assertEqual(first["failed_pages"], [2])
        self.assertEqual(self.read("pages/page-0002.json")["error_stage"], "ocr")
        self.engine.predict.side_effect = lambda *_: iter([prediction("recovered")])
        self.engine.predict.reset_mock()
        second = self.run_ocr()
        self.assertEqual(second["status"], "complete")
        self.assertEqual(second["cached_pages"], 2)
        self.engine.predict.assert_called_once()
        self.assertIn("0002", self.engine.predict.call_args.args[0])

    def test_interruption_commits_finished_pages_and_resumes_pending_pages(self):
        self.engine.predict.side_effect = [iter([prediction()]), KeyboardInterrupt()]
        with self.assertRaises(KeyboardInterrupt):
            self.run_ocr()
        self.assertEqual(self.read("document.json")["status"], "interrupted")
        self.assertEqual(self.read("summary.json")["pending_pages"], [2, 3])
        self.assertTrue((self.output / "pages/page-0001.json").exists())
        self.engine.predict.side_effect = lambda *_: iter([prediction()])
        self.engine.predict.reset_mock()
        result = self.run_ocr()
        self.assertEqual(result["cached_pages"], 1)
        self.assertEqual(self.engine.predict.call_count, 2)

    def test_parameter_model_and_runtime_changes_invalidate_cache(self):
        self.run_ocr()
        changed = replace(self.config, max_side=700)
        self.assertEqual(self.run_ocr(config=changed)["cached_pages"], 0)
        weights = self.models / paten_ocr.RECOGNIZER / "inference.pdiparams"
        weights.write_text("new model weights")
        self.assertEqual(self.run_ocr(config=changed)["cached_pages"], 0)
        with patch.object(paten_ocr, "_runtime_versions", return_value={"paddleocr": "updated"}):
            self.assertEqual(self.run_ocr(config=changed)["cached_pages"], 0)

    def test_corrupt_and_modified_page_results_are_not_reused(self):
        self.run_ocr()
        (self.output / "pages/page-0001.json").write_text("{partial json")
        changed = self.read("pages/page-0002.json")
        changed["lines"][0]["text"] = "silently edited"
        (self.output / "pages/page-0002.json").write_text(json.dumps(changed))
        self.engine.predict.reset_mock()
        result = self.run_ocr()
        self.assertEqual(result["cached_pages"], 1)
        self.assertEqual(self.engine.predict.call_count, 2)

    def test_subset_pages_can_resume_into_full_document_and_force_is_explicit(self):
        result = self.run_ocr(pages=["3,1"])
        self.assertEqual(result["successful_pages"], 2)
        self.assertFalse(result["full_document_ocr_complete"])
        self.assertEqual(result["unselected_pages"], [2])
        self.assertEqual(self.run_ocr()["cached_pages"], 2)
        self.assertEqual(self.run_ocr(force=True, pages=[2])["cached_pages"], 0)

    def test_changed_pdf_wrong_document_and_foreign_output_are_protected(self):
        self.run_ocr()
        with self.assertRaisesRegex(ValueError, "另一份 PDF"):
            self.run_ocr(document_id="different")
        self.pdf.write_bytes(self.pdf.read_bytes() + b"\n% changed\n")
        with self.assertRaisesRegex(ValueError, "另一份 PDF"):
            self.run_ocr()
        foreign = self.root / "foreign"
        foreign.mkdir()
        (foreign / "dataset.json").write_text("{}")
        with self.assertRaisesRegex(ValueError, "非空"):
            paten_ocr.ocr_pdf(self.pdf, foreign, config=self.config)
        self.assertEqual((foreign / "dataset.json").read_text(), "{}")

    def test_concurrent_writers_to_same_output_are_rejected(self):
        with paten_ocr._output_lock(self.output):
            with self.assertRaisesRegex(ValueError, "另一个 OCR"):
                self.run_ocr()

    def test_model_initialization_failure_is_saved_and_not_repeated_for_every_page(self):
        self.create.side_effect = RuntimeError("GPU unavailable")
        result = self.run_ocr()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failed_pages"], [1, 2, 3])
        self.create.assert_called_once()
        self.engine.predict.assert_not_called()
        self.assertEqual(self.read("pages/page-0001.json")["error_stage"], "model_init")

    def test_empty_page_and_malformed_or_incomplete_prediction_are_distinct(self):
        self.engine.predict.side_effect = lambda *_: iter([{"rec_texts": [], "rec_scores": [], "rec_polys": []}])
        result = self.run_ocr(pages=[1])
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["review_pages"], [1])
        malformed = prediction()
        malformed["rec_scores"] = []
        self.engine.predict.side_effect = lambda *_: iter([malformed])
        self.assertEqual(self.run_ocr(pages=[2])["failed_pages"], [2])
        def late_failure(*_):
            yield prediction()
            raise RuntimeError("late GPU error")
        self.engine.predict.side_effect = late_failure
        self.assertEqual(self.run_ocr(pages=[3])["failed_pages"], [3])
        self.assertEqual(self.read("pages/page-0003.json")["lines"], [])

    def test_cli_failure_exit_status_and_argument_validation(self):
        with patch.object(paten_ocr, "ocr_pdf", return_value={"status": "partial", "successful_pages": 1,
                "selected_page_count": 2, "cached_pages": 0, "timings": {"wall_seconds": 1}}):
            self.assertEqual(paten_ocr.main([str(self.pdf), "--output-dir", str(self.output)]), 1)
        for values in (["0"], ["4"], ["3-1"], ["1,,2"], []):
            with self.subTest(values=values), self.assertRaises(ValueError):
                paten_ocr.parse_pages(values, 3)
        self.assertEqual(paten_ocr.parse_pages(["1-2", "2,3"], 3), [1, 2, 3])
        for kwargs in ({"device": "cuda:0"}, {"max_side": 0}, {"batch_size": True}, {"low_confidence": float("nan")}):
            with self.assertRaises(ValueError):
                paten_ocr.OCRConfig(**kwargs)

    def test_cli_help_runs_outside_repository_without_loading_paddle(self):
        result = subprocess.run([sys.executable, str(ROOT / "src/paten_ocr.py"), "--help"],
                                cwd=self.root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--force", result.stdout)
        self.assertNotIn("paddle/utils", result.stderr)

    def test_prefetch_keeps_page_coordinates_and_cache_without_worker_on_warm_run(self):
        serial = self.run_ocr()
        original = self.read("pages/page-0002.json")
        config = replace(self.config, render_prefetch=2)
        with patch.object(paten_ocr, "ProcessPoolExecutor", side_effect=AssertionError("cached run spawned worker")):
            self.assertEqual(self.run_ocr(config=config)["cached_pages"], 3)
        result = self.run_ocr(config=config, force=True)
        current = self.read("pages/page-0002.json")
        self.assertEqual(result["lines"], serial["lines"])
        for key in ("lines", "width", "height", "rotation", "page_derotation_matrix", "image_sha256", "pixel_to_page_scale"):
            self.assertEqual(current[key], original[key])
        self.assertGreater(result["timings"]["render_seconds"], 0)
        self.assertEqual(result["failed_pages"], [])

    def test_prefetch_interruption_preserves_committed_pages_and_resumes(self):
        config = replace(self.config, render_prefetch=2)
        self.engine.predict.side_effect = [iter([prediction()]), KeyboardInterrupt()]
        with self.assertRaises(KeyboardInterrupt):
            self.run_ocr(config=config)
        self.assertEqual(self.read("summary.json")["pending_pages"], [2, 3])
        self.engine.predict.side_effect = lambda *_: iter([prediction()])
        self.assertEqual(self.run_ocr(config=config)["cached_pages"], 1)

    def test_prefetch_worker_error_reaches_caller_and_pool_shuts_down(self):
        with paten_ocr._prefetched_pages(self.pdf, [99], self.output, replace(self.config, render_prefetch=1)) as take:
            with self.assertRaises(IndexError):
                take(99)
        for value in (-1, True, 1.5):
            with self.assertRaises(ValueError):
                replace(self.config, render_prefetch=value)

    def test_parallel_pool_preserves_order_coordinates_and_shares_serial_cache(self):
        config = replace(self.config, workers=2, render_prefetch=2)
        with patch.object(paten_ocr, "_ocr_page_job", fake_page_job):
            result = self.run_ocr(config=config)
        self.create.assert_not_called()
        self.assertEqual(result["status"], "complete")
        pages = [json.loads(line) for line in (self.output / "pages.jsonl").read_text().splitlines()]
        self.assertEqual([p["page_number"] for p in pages], [1, 2, 3])
        self.assertEqual(pages[1]["rotation"], 90)
        self.assertEqual(pages[0]["lines"][0]["text"], prediction()["rec_texts"][0])
        self.assertEqual(result["timings"]["ocr_seconds"], 0.03)
        with patch.object(paten_ocr, "ProcessPoolExecutor", side_effect=AssertionError("cached run spawned worker")):
            self.assertEqual(self.run_ocr(config=config)["cached_pages"], 3)
            self.assertEqual(self.run_ocr()["cached_pages"], 3)

    def test_page_job_reuses_engine_and_returns_failures_without_losing_later_pages(self):
        with patch.object(paten_ocr, "_WORKER_ENGINE", None), patch.object(paten_ocr, "_WORKER_INIT_ERROR", None):
            first = paten_ocr._ocr_page_job(self.pdf, 1, self.output / "1.png", self.config, "cpu", self.models)
            self.engine.predict.side_effect = RuntimeError("device failure")
            second = paten_ocr._ocr_page_job(self.pdf, 2, self.output / "2.png", self.config, "cpu", self.models)
            self.engine.predict.side_effect = lambda *_: iter([prediction()])
            third = paten_ocr._ocr_page_job(self.pdf, 3, self.output / "3.png", self.config, "cpu", self.models)
        self.assertEqual([p["status"] for p in (first, second, third)], ["success", "failed", "success"])
        self.assertEqual(second["error_stage"], "ocr")
        self.assertEqual(second["lines"], [])
        self.create.assert_called_once()

    def test_parallel_failure_and_interrupt_still_commit_and_resume_per_page(self):
        config = replace(self.config, workers=2)
        first = fake_page_job(self.pdf, 1, self.root / "first.png", config, "cpu", self.models)
        failed = {"status": "failed", "lines": [], "timings": {}, "error_stage": "ocr", "error": "GPU failure"}
        def deliver(number):
            return failed if number == 2 else fake_page_job(
                self.pdf, number, self.output / "images" / f"page-{number:04d}.png", config, "cpu", self.models)
        with patch.object(paten_ocr, "_parallel_ocr_pages") as pool:
            pool.return_value.__enter__.return_value = Mock(side_effect=deliver)
            result = self.run_ocr(config=config)
        self.assertEqual(result["failed_pages"], [2])
        self.assertEqual(self.read("pages/page-0002.json")["error_stage"], "ocr")
        with patch.object(paten_ocr, "_ocr_page_job", fake_page_job):
            self.assertEqual(self.run_ocr(config=config)["cached_pages"], 2)
        with patch.object(paten_ocr, "_parallel_ocr_pages") as pool:
            pool.return_value.__enter__.return_value = Mock(side_effect=[first, KeyboardInterrupt()])
            with self.assertRaises(KeyboardInterrupt):
                self.run_ocr(config=config, force=True)
        self.assertEqual(self.read("summary.json")["pending_pages"], [2, 3])

    def test_worker_initialization_failure_is_not_repeated_and_bad_worker_counts_rejected(self):
        self.create.side_effect = RuntimeError("unavailable")
        with patch.object(paten_ocr, "_WORKER_ENGINE", None), patch.object(paten_ocr, "_WORKER_INIT_ERROR", None):
            for number in (1, 2):
                result = paten_ocr._ocr_page_job(self.pdf, number, self.output / f"{number}.png", self.config, "cpu", self.models)
                self.assertEqual(result["error_stage"], "model_init")
        self.create.assert_called_once()
        for workers in (0, -1, True, 1.5):
            with self.assertRaises(ValueError):
                replace(self.config, workers=workers)


if __name__ == "__main__":
    unittest.main()
