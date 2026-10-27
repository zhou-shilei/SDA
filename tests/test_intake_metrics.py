"""Statistics must preserve measured scope, concurrency, and unknown usage."""

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from studio.processing_metrics import processing_metrics


class IntakeMetricsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.result = self.root / "runs/current/results/DOC"
        self.result.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def stats(self, run=None, runtime=None, job=None):
        return processing_metrics(self.root, "DOC", self.result, job or {}, run or {}, runtime or {})

    def write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))

    def test_full_run_time_wins_over_latest_cached_stage_and_resumes_are_grouped(self):
        report = {"wall_seconds": 419.6, "resumed": True, "phases": [
            {"name": "layout", "seconds": 25}, {"name": "document_resume", "seconds": 3},
            {"name": "document_resume", "seconds": 4}]}
        stats = self.stats(report, {"wall_seconds": 2.7})
        self.assertEqual(stats["time"]["seconds"], 419.6)
        self.assertEqual(stats["time"]["scope"], "cumulative")
        resume = stats["time"]["stages"][1]
        self.assertEqual((resume["seconds"], resume["count"]), (7, 2))

    def test_parallel_branch_durations_are_details_and_do_not_inflate_total(self):
        structured = self.root / "runs/current/structured"
        self.write(structured / "run_summary.json", {"phases": [
            {"name": "orientation_and_layout", "seconds": 5}, {"name": "recognition", "seconds": 60}]})
        self.write(structured / "recognition_summary.json", {"runtime": {
            "text": {"started": 100, "finished": 150}, "table": {"started": 101, "finished": 160}}})
        stats = self.stats({"wall_seconds": 70, "structured_dir": str(structured),
                            "phases": [{"name": "structured_document", "seconds": 68}]})
        self.assertEqual(stats["time"]["seconds"], 70)
        branches = [row for row in stats["time"]["stages"] if row["level"] == 2]
        self.assertEqual([(r["stage"], r["seconds"]) for r in branches], [("text_ocr", 50), ("table_ocr", 59)])

    def test_live_usage_file_is_not_added_to_duplicate_report_copies(self):
        usage = {"totals": {"total_tokens": 12000, "input_tokens": 10000, "output_tokens": 2000,
                             "cached_input_tokens": 7000, "requests_without_usage": 1},
                 "by_stage": [{"stage": "document_extraction", "total_tokens": 12000}]}
        self.write(self.result / "_token_usage.json", usage)
        stats = self.stats({"token_usage": usage}, {"token_usage": usage})
        self.assertEqual(stats["tokens"]["totals"]["total_tokens"], 12000)
        self.assertEqual(stats["tokens"]["totals"]["unknown_requests"], 1)
        self.assertEqual(len(stats["tokens"]["stages"]), 1)

    def test_unknown_and_measured_zero_remain_distinct(self):
        missing = self.stats()
        self.assertIsNone(missing["time"]["seconds"])
        self.assertFalse(missing["tokens"]["available"])
        zero = self.stats({"wall_seconds": 0, "token_usage": {"totals": {"total_tokens": 0, "requests": 0}}})
        self.assertEqual(zero["time"]["seconds"], 0)
        self.assertTrue(zero["tokens"]["available"])
        self.assertEqual(zero["tokens"]["totals"]["total_tokens"], 0)

    def test_printed_style_repair_has_reanalysis_scope(self):
        stats = self.stats({"run_kind": "printed_style_repair", "wall_seconds": 292.726})
        self.assertEqual(stats["time"]["scope"], "reanalysis")
        self.assertEqual(stats["time"]["seconds"], 292.726)

    def test_cache_only_snapshot_does_not_replace_completed_cumulative_usage_with_zero(self):
        self.write(self.result / "_token_usage.json", {"totals": {"total_tokens": 0, "requests": 0}})
        stats = self.stats({"resumed": True, "status": "complete", "token_usage": {
            "totals": {"total_tokens": 285964, "input_tokens": 258153, "output_tokens": 27811}}})
        self.assertEqual(stats["tokens"]["totals"]["total_tokens"], 285964)

    def test_active_run_prefers_current_usage_snapshot(self):
        self.write(self.result / "_token_usage.json", {"totals": {"total_tokens": 1200}})
        stats = self.stats({"token_usage": {"totals": {"total_tokens": 100}}}, job={"status": "processing"})
        self.assertEqual(stats["tokens"]["totals"]["total_tokens"], 1200)

    def test_processing_can_read_parent_report_before_result_files_exist(self):
        self.write(self.result.parent.parent / "run_summary.json", {"document_id": "DOC", "phases": [
            {"name": "structured_document", "seconds": 10}]})
        stats = self.stats(job={"status": "processing", "started_at": "2026-10-25T01:00:00+00:00"})
        self.assertTrue(stats["time"]["running"])
        self.assertEqual(stats["time"]["started_at"], "2026-10-25T01:00:00+00:00")
        self.assertEqual(stats["time"]["stages"][0]["seconds"], 10)

    def test_stage_only_history_and_corrupt_optional_usage_do_not_claim_full_measurement(self):
        (self.result / "_token_usage.json").write_text("{")
        stats = self.stats(runtime={"wall_seconds": 2.7, "token_usage": {"totals": {"total_tokens": None}}})
        self.assertEqual(stats["time"]["scope"], "stage_only")
        self.assertEqual(stats["time"]["seconds"], 2.7)
        self.assertIsNone(stats["tokens"]["totals"]["total_tokens"])

    @unittest.skipUnless(shutil.which("node"), "Node is required for frontend formatting checks")
    def test_frontend_formats_tokens_in_k_and_preserves_live_unknown_and_partial_states(self):
        result = subprocess.run(["node", str(ROOT / "tests/test_intake_metrics.mjs")],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
