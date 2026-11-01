"""Studio auto-concurrency must preserve overrides and leave memory for page OCR."""

import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from studio.ocr_settings import TABLE_WORKER_LIMIT, table_worker_settings


class StudioOCRSettingsTests(unittest.TestCase):
    def test_idle_80gb_gpu_uses_measured_parallel_limit(self):
        with patch("studio.ocr_settings.subprocess.run", return_value=SimpleNamespace(stdout="81916\n")) as query:
            settings = table_worker_settings("gpu:0", "vl")
        self.assertEqual(settings["workers"], TABLE_WORKER_LIMIT)
        self.assertEqual(settings["policy"], "auto")
        self.assertIn("--id=0", query.call_args.args[0])

    def test_occupied_and_smaller_gpus_reduce_parallelism(self):
        for free, workers in [(40960, 2), (24576, 1), (4000, 1)]:
            with self.subTest(free=free), patch("studio.ocr_settings.subprocess.run", return_value=SimpleNamespace(stdout=str(free))):
                self.assertEqual(table_worker_settings("gpu:2", "vl")["workers"], workers)

    def test_explicit_override_does_not_probe_gpu(self):
        with patch("studio.ocr_settings.subprocess.run", side_effect=AssertionError("Override must stay explicit")):
            settings = table_worker_settings("gpu:0", "vl", 8)
        self.assertEqual(settings["workers"], 8)
        self.assertEqual(settings["policy"], "explicit")

    def test_more_page_predictors_reserve_more_memory(self):
        with patch("studio.ocr_settings.subprocess.run", return_value=SimpleNamespace(stdout="81916")):
            settings = table_worker_settings("gpu:0", "vl", page_workers=3)
        self.assertEqual(settings["workers"], min(TABLE_WORKER_LIMIT, 4))
        self.assertEqual(settings["page_workers"], 3)

    def test_cpu_or_other_backend_defaults_to_one_without_nvidia(self):
        with patch("studio.ocr_settings.subprocess.run", side_effect=AssertionError("No GPU query required")):
            for device, backend in [("cpu", "vl"), ("gpu:0", "paddle")]:
                self.assertEqual(table_worker_settings(device, backend)["workers"], 1)

    def test_missing_or_invalid_gpu_query_falls_back(self):
        for error in [FileNotFoundError(), subprocess.TimeoutExpired("nvidia-smi", 3),
                      subprocess.CalledProcessError(1, "nvidia-smi")]:
            with patch("studio.ocr_settings.subprocess.run", side_effect=error):
                self.assertEqual(table_worker_settings("gpu:0", "vl")["workers"], 1)
        with patch("studio.ocr_settings.subprocess.run", return_value=SimpleNamespace(stdout="N/A")):
            self.assertEqual(table_worker_settings("gpu:0", "vl")["workers"], 1)

    def test_invalid_override_is_rejected(self):
        for value in [0, -1, True, "6"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                table_worker_settings("gpu:0", "vl", value)


if __name__ == "__main__":
    unittest.main()
