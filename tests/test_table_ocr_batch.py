import asyncio
from concurrent.futures import ThreadPoolExecutor
import copy
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pipeline import BatchPipeline, PipelineConfig
import table_ocr


class TableOCRBatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.images = [self.root / f"WO2026000001A1_table-{n:04d}.png" for n in range(4)]
        for i, image in enumerate(self.images):
            image.write_bytes(bytes([i]))
        self.metadata = [{"id": f"T{i}", "document_id": "WO2026000001A1", "page": i + 1,
                          "bbox": [10, 20, 30, 40]} for i in range(4)]
        self.html = ['<table><tr><td rowspan="2">A</td><td>IC50</td></tr><tr><td>1.2</td></tr></table>',
                     '<table><tr><td>VH</td><td>EVQLVESGGGLEQPGGSLRLS</td></tr></table>',
                     '<table><tr><td>B</td><td>&lt;3</td></tr></table>',
                     '<table><tr><td>C</td><td>4</td></tr></table>']
        table_ocr.configure_ocr_worker(batch_size=6)

    def tearDown(self):
        table_ocr.configure_ocr_worker()
        self.tmp.cleanup()

    def result(self, index, path=None):
        return SimpleNamespace(json={"res": {"input_path": str(path or self.images[index]),
            "parsing_res_list": [{"block_content": self.html[index]}]}})

    def model(self, results):
        def predict(**kwargs):
            self.submitted = kwargs
            yield from results
        return SimpleNamespace(predict=predict, paddlex_pipeline=SimpleNamespace(
            vl_rec_model=SimpleNamespace(batch_sampler=SimpleNamespace(batch_size=1))))

    def test_out_of_order_results_keep_image_identity_sequence_and_spans(self):
        model = self.model([self.result(1), self.result(0)])
        with patch.object(table_ocr, "get_ocr", return_value=model):
            outputs = table_ocr.extract_table_evidence_batch(self.images[:2], self.metadata[:2])
        self.assertEqual(self.submitted["input"], [str(p) for p in self.images[:2]])
        self.assertEqual([o["table_id"] for o in outputs], ["T0", "T1"])
        self.assertEqual([o["text"] for o in outputs], self.html[:2])
        self.assertEqual(outputs[0]["grid"][0][0]["ref"], outputs[0]["grid"][1][0]["ref"])
        self.assertIn("EVQLVESGGGLEQPGGSLRLS", outputs[1]["text"])
        self.assertEqual(outputs[0]["batch_runtime"]["configured_submission_batch_size"], 6)
        self.assertEqual(outputs[0]["batch_runtime"]["effective_model_batch_size"], 1)
        self.assertEqual(outputs[0]["batch_runtime"]["submission_size"], 2)

    def test_missing_duplicate_or_unknown_result_is_not_assigned_by_position(self):
        for results in [[self.result(0)], [self.result(0), self.result(0)],
                        [self.result(0), self.result(1, self.root / "foreign.png")]]:
            with self.subTest(results=len(results)), patch.object(table_ocr, "get_ocr", return_value=self.model(results)):
                with self.assertRaises(ValueError):
                    table_ocr.extract_table_evidence_batch(self.images[:2], self.metadata[:2])

    def test_generator_failure_does_not_return_a_partially_completed_batch(self):
        def predict(**kwargs):
            yield self.result(0)
            raise RuntimeError("generation failed")
        with patch.object(table_ocr, "get_ocr", return_value=SimpleNamespace(predict=predict)):
            with self.assertRaisesRegex(RuntimeError, "generation failed"):
                table_ocr.extract_table_evidence_batch(self.images[:2], self.metadata[:2])

    def test_invalid_inputs_fail_before_loading_a_model(self):
        with patch.object(table_ocr, "get_ocr") as loader:
            for paths, metadata in [([], []), ([self.images[0]] * 2, self.metadata[:2]),
                                    (self.images[:2], self.metadata[:1])]:
                with self.assertRaises(ValueError):
                    table_ocr.extract_table_evidence_batch(paths, metadata)
            with self.assertRaises(FileNotFoundError):
                table_ocr.extract_table_evidence_batch([self.root / "missing.png"])
            loader.assert_not_called()
        for size in [0, -1, True]:
            with self.assertRaises(ValueError):
                table_ocr.configure_ocr_worker(batch_size=size)
        with self.assertRaises(ValueError):
            PipelineConfig(ocr_batch_size=0)

    def test_single_image_api_keeps_metadata_and_reports_actual_batch(self):
        table_ocr.configure_ocr_worker(batch_size=1)
        with patch.object(table_ocr, "get_ocr", return_value=self.model([self.result(0)])) as loader:
            evidence = table_ocr.extract_table_evidence(self.images[0], self.metadata[0])
        loader.assert_called_once()
        self.assertEqual(evidence["text"], self.html[0])
        self.assertEqual(evidence["table_id"], "T0")
        self.assertEqual(evidence["document_id"], "WO2026000001A1")
        self.assertEqual(evidence["batch_runtime"]["effective_model_batch_size"], 1)

    def pipeline(self):
        config = PipelineConfig(extraction_mode="document", text_pages_jsonl="unused.jsonl",
            ocr_workers=2, ocr_batch_size=3, retries=0, text_sequences=False)
        pipeline = BatchPipeline(self.root, self.root / "out", config)
        pipeline.crop_metadata = {str(p): m for p, m in zip(self.images, self.metadata)}
        return pipeline

    def test_pipeline_publishes_all_batch_results_before_continuation_waits(self):
        pipeline = self.pipeline()
        batches = []
        def extract(paths, metadata):
            batches.append(list(paths))
            return [table_ocr.structure_table_evidence(self.html[self.images.index(Path(p))], p, m)
                    for p, m in zip(paths, metadata)]
        async def context(metadata, text):
            # The second image's continuation may depend on the first image,
            # even when they are returned by the same future.
            if metadata["id"] == "T1":
                self.assertTrue(pipeline._ocr_ready[str(self.images[0])].is_set())
            return {}
        pipeline._continuation_context = context
        with patch("pipeline.extract_table_evidence_batch", side_effect=extract), ThreadPoolExecutor(2) as pool:
            asyncio.run(asyncio.wait_for(pipeline._produce_ocr(iter(self.images), pool), timeout=3))
        self.assertEqual(sorted(map(len, batches)), [1, 3])
        self.assertEqual(pipeline.metrics["ocr"]["tables_succeeded"], 4)
        self.assertEqual(len(pipeline.outputs), 4)
        self.assertEqual([json.loads(p.read_text())["image_path"] for p in pipeline.outputs.values()],
                         [str(p) for p in self.images])
        for p in self.images:
            self.assertTrue(pipeline._ocr_ready[str(p)].is_set())
            self.assertEqual(pipeline._ocr_states[str(p)]["table_evidence"]["image_path"], str(p))

    def test_pipeline_does_not_silently_shift_incomplete_batch_outputs(self):
        pipeline = self.pipeline()
        def incomplete(paths, metadata):
            return []
        with patch("pipeline.extract_table_evidence_batch", side_effect=incomplete), ThreadPoolExecutor(2) as pool:
            asyncio.run(pipeline._produce_ocr(iter(self.images), pool))
        self.assertEqual(pipeline.metrics["ocr"]["tables_succeeded"], 0)
        self.assertEqual(pipeline.metrics["ocr"]["tables_failed"], 4)
        self.assertTrue(all(pipeline._ocr_ready[str(p)].is_set() for p in self.images))


if __name__ == "__main__":
    unittest.main()
