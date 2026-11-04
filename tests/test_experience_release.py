"""Prepared cases run in a fresh workspace and leave new parsing available."""
import gzip
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.request import urlopen

import pymupdf
from studio.experience import ExperienceWorkspace
from studio.server import StudioServer


class ExperienceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.directory = self.root / "demo/cases/DOC"
        self.directory.mkdir(parents=True)
        with pymupdf.open() as pdf:
            pdf.new_page().insert_text((30, 30), "Example source evidence")
            pdf.save(self.directory / "source.pdf")
        dataset = {"document_id": "DOC", "status": "partial", "records": [{"record_id": "r1"}],
                   "summary": {"missing_required": 1}, "extraction_mode": "dataagent2"}
        record = {"record_id": "r1", "document_id": "DOC", "antibody_id": "Ab1",
                  "vh": "EVQL", "vl": None, "ic50": 1, "ic50_unit": "nM",
                  "assay_keywords": [], "evidence": [{"page": 1}], "sda_fields": {},
                  "agent_review_status": "review"}
        self.bundle = {"records": [record], "dataset": dataset, "contexts": [], "candidates": [],
                       "classifications": [], "ocr": [], "template": {"agent": {"name": "dataagent2"}},
                       "runtime": {}, "pages": {"page_count": 1, "pages": []}, "crops": {},
                       "run": {"document_id": "DOC", "records": 1, "status": "partial"},
                       "pdf_form": {"kind": "text"}, "coverage": {}, "missing": {}, "tst": {},
                       "document_report": {"status": "partial"}}
        (self.directory / "dataset.json").write_text(json.dumps(dataset))
        (self.directory / "bundle.json.gz").write_bytes(gzip.compress(json.dumps(self.bundle).encode(), mtime=0))
        files = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in self.directory.iterdir()}
        self.manifest = {"package_id": "test-release", "version": "test", "cases": [
            {"document_id": "DOC", "title": "Example", "filename": "DOC.pdf", "record_count": 1,
             "page_count": 1, "files": files}]}
        (self.root / "demo/manifest.json").write_text(json.dumps(self.manifest))
        self.workspace = ExperienceWorkspace(self.root)

    def test_fresh_workspace_has_cases_records_and_preserves_pending_status(self):
        self.assertEqual(self.workspace.validate_cases(), {"cases": 1, "records": 1})
        patent = self.workspace.patents()[0]
        self.assertEqual((patent["document_id"], patent["record_count"], patent["page_count"]), ("DOC", 1, 1))
        self.assertTrue(patent["builtin"])
        self.assertTrue(patent["retry_available"])
        self.assertEqual(patent["semantic_status"], "partial")
        self.assertEqual(self.workspace.published()[0]["agent_review_status"], "review")

    def test_restart_keeps_user_changes_and_does_not_reseed_deleted_rows(self):
        with self.workspace.connection() as db:
            db.execute("DELETE FROM publications")
            db.execute("UPDATE folders SET name='My trial'")
        reopened = ExperienceWorkspace(self.root)
        self.assertEqual(reopened.published(), [])
        self.assertEqual(reopened.folders()[0]["name"], "My trial")
        self.assertEqual(reopened.bundle("DOC")["records"][0]["record_id"], "r1")

    def test_prepared_case_can_start_a_new_pipeline_without_overwriting_package(self):
        before = (self.directory / "source.pdf").read_bytes()
        server = StudioServer(("127.0.0.1", 0), self.workspace)
        self.addCleanup(server.server_close)
        with patch.object(server, "submit_task") as submit:
            result = server.enqueue("DOC", engine="dataagent2")
        self.assertEqual(result["status"], "queued")
        submit.assert_called_once_with("DOC")
        self.assertEqual((self.workspace.state / "patents/DOC/source.pdf").read_bytes(), before)
        self.assertEqual((self.directory / "source.pdf").read_bytes(), before)

    def test_http_serves_gallery_review_and_original_page_without_development_files(self):
        server = StudioServer(("127.0.0.1", 0), self.workspace)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}"
        with urlopen(base + "/api/experience") as response:
            case = json.load(response)["cases"][0]
        self.assertNotIn("files", case)
        with urlopen(base + "/api/patents/DOC/review.json") as response:
            result = json.load(response)
        self.assertEqual(result["records"][0]["vh"], "EVQL")
        with urlopen(base + "/api/patents/DOC/pages/0001.png") as response:
            self.assertTrue(response.read().startswith(b"\x89PNG"))
        with urlopen(base + "/studio/experience.js") as response:
            self.assertIn(b"api/experience", response.read())


if __name__ == "__main__":
    unittest.main()
