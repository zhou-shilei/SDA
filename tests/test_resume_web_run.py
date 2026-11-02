"""Reporting failures must preserve completed extraction and allow offline recovery."""

import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]
spec = importlib.util.spec_from_file_location("resume_web_run", ROOT / "try/agent_handoff/resume_web_run.py")
resume = importlib.util.module_from_spec(spec)
spec.loader.exec_module(resume)

from dataagent2.provider import write_json
from units.timing import write_timing_report


class ReportRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.run = Path(temporary.name) / "run/document"
        self.document = self.run / "results/DOC"
        self.native = self.document / "dataagent2"
        self.native.mkdir(parents=True)
        (self.run / "source.pdf").write_bytes(b"unchanged source PDF")
        self.report = {"document_id": "DOC", "result_dir": str(self.document), "status": "running",
            "source_pdf_sha256": hashlib.sha256((self.run / "source.pdf").read_bytes()).hexdigest(),
            "wall_seconds": 100, "phases": [{"name": "document_extraction", "seconds": 100, "returncode": 1}]}
        write_json(self.run / "run_summary.json", self.report)
        write_json(self.document / "_timings.json", {"wall_seconds": 125})
        write_json(self.native / "report.json", {"benchmark_complete": True, "reader_complete": True,
            "action_complete": True, "question_api_failures": 0})
        write_json(self.native / "sources.json", {"1": {"text": "7"}})
        write_json(self.native / "dataset.json", {"entities": [{"id": "e1", "name": "AB-1",
            "sequences": {"VH": {"status": "missing_mapping"}, "VL": {"status": "missing_mapping"}}}],
            "observations": [{"id": "o1", "item": "e1", "s": 1, "value": "7", "unit": None, "refs": [1]}],
            "issues": [], "summary": {"observations": 1}})
        write_json(self.document / "dataset.json", {"status": "partial", "entities": [{"id": "e1"}],
            "records": [{"id": "o1", "value": "7"}], "experiments": [], "relations": [],
            "preprocessing": {"status": "partial"}})

    def test_timing_writer_accepts_missing_optional_diagnostics_without_mutating_input(self):
        supplied = {"wall_seconds": 125}
        path = write_timing_report(self.document, supplied)
        saved = resume.read(path)
        self.assertEqual(saved["slowest_requests"], [])
        self.assertTrue((self.document / "_timings.txt").exists())
        self.assertEqual(supplied, {"wall_seconds": 125})

    def test_finalize_preserves_business_bytes_and_never_calls_a_model(self):
        original = (self.document / "dataset.json").read_bytes()
        with patch("dataagent2.provider.Provider.generate", side_effect=AssertionError("Unexpected model call")), \
             patch("document.run_document_pdf.write_html_report"):
            result = resume.finalize_saved(self.run, update_web=False)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["records"], 1)
        self.assertEqual(result["wall_seconds"], 125)
        self.assertEqual(result["phases"][-1]["seconds"], 25)
        self.assertEqual(result["phases"][-1]["failure_scope"], "report_generation")
        self.assertEqual(result["report_warnings"], [])
        self.assertEqual(original, (self.document / "dataset.json").read_bytes())
        self.assertEqual(resume.read(self.run / "run_summary.json")["status"], "partial")
        self.assertEqual(resume.read(self.document.parent / "_batch_summary.json")["status"], "partial")
        audit = resume.read(self.run / "recovery/finalization.json")
        self.assertEqual(audit["dataset_sha256_before"], audit["dataset_sha256_after"])
        self.assertEqual(audit["model_calls_made"], 0)

    def test_optional_report_failure_does_not_hide_completed_business_export(self):
        with patch("document.run_document_pdf.write_html_report", side_effect=RuntimeError("HTML unavailable")):
            result = resume.finalize_saved(self.run, update_web=False)
        saved = resume.read(self.run / "run_summary.json")
        self.assertEqual(saved["status"], "partial")
        self.assertEqual(result["report_warnings"][0]["error"], "HTML unavailable")

    def test_incomplete_processing_cannot_be_finalized(self):
        write_json(self.native / "report.json", {"benchmark_complete": False, "reader_complete": True,
            "action_complete": False, "question_api_failures": 1})
        original = (self.run / "run_summary.json").read_bytes()
        with self.assertRaisesRegex(ValueError, "incomplete model processing"):
            resume.finalize_saved(self.run, update_web=False)
        self.assertEqual(original, (self.run / "run_summary.json").read_bytes())


if __name__ == "__main__":
    unittest.main()
