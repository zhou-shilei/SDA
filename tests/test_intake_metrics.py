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
from src.dataagent2.timing import PhaseTimings, measured_phases


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

    def test_extraction_recovers_parallel_request_coverage_and_keeps_missing_host_time_unknown(self):
        self.write(self.result / "shared_seed/pipeline_performance.json", {"stages": [
            {"stage": "classify_tables", "elapsed_seconds": 20},
            {"stage": "template", "elapsed_seconds": 40}]})
        self.write(self.result / "dataagent2/manifest.json", {})
        calls = [{"attempt_id": "a-1700000000000000000", "stage": "reader", "seconds": 10},
                 {"attempt_id": "b-1700000005000000000", "stage": "reader", "seconds": 10},
                 {"attempt_id": "c-1700000020000000000", "stage": "answer", "seconds": 4, "status": "failed"},
                 {"attempt_id": "d-1700000025000000000", "stage": "answer", "seconds": 6}]
        (self.result / "dataagent2/calls.jsonl").write_text(
            "\n".join(json.dumps(c) for c in [*calls, calls[0]]) + '\n{"incomplete":')
        stats = self.stats({"wall_seconds": 100, "phases": [{"name": "document_extraction", "seconds": 90}]})
        rows = {row["stage"]: row for row in stats["time"]["stages"]}
        self.assertEqual(stats["time"]["seconds"], 100)
        self.assertEqual(rows["classify_tables"]["seconds"], 20)
        self.assertEqual(rows["dataagent2_reader"]["seconds"], 15)
        self.assertEqual(rows["dataagent2_reader"]["requests"], 2)
        self.assertEqual(rows["dataagent2_reader"]["basis"], "request_coverage")
        self.assertEqual(rows["dataagent2_questions"]["seconds"], 10)
        self.assertIsNone(rows["dataagent2_merge"]["seconds"])

    def test_measured_reader_phase_wins_over_historical_request_coverage(self):
        reader = self.result / "dataagent2"
        self.write(reader / "phase_timings.json", {"events": [
            {"stage": "dataagent2_reader", "seconds": 30},
            {"stage": "dataagent2_merge", "seconds": 12}]})
        (reader / "calls.jsonl").write_text(json.dumps({"attempt_id": "a-1700000000000000000",
            "stage": "reader", "seconds": 20}))
        rows = {row["stage"]: row for row in self.stats()["time"]["stages"]}
        self.assertEqual(rows["dataagent2_reader"]["seconds"], 30)
        self.assertEqual(rows["dataagent2_reader"]["basis"], "phase")
        self.assertEqual(rows["dataagent2_merge"]["seconds"], 12)

    def test_failed_phase_is_persisted_without_hiding_the_original_failure(self):
        timer = PhaseTimings(self.result / "phase_timings.json")
        with self.assertRaisesRegex(RuntimeError, "original failure"):
            with timer.measure("result_export"):
                raise RuntimeError("original failure")
        rows = measured_phases(self.result)
        self.assertEqual(rows[0]["stage"], "result_export")
        self.assertEqual(rows[0]["status"], "failed")
        self.assertGreaterEqual(rows[0]["seconds"], 0)

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
