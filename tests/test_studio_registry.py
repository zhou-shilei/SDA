"""Registry polling shares metadata while keeping changed artifacts visible."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.request import urlopen

from studio import sda_data
from studio.sda_data import Workspace
from studio.server import StudioServer


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.results = self.root / "results" / "DOC"
        self.results.mkdir(parents=True)
        self.dataset = self.results / "dataset.json"
        self.workspace = Workspace(self.root)

    def write(self, path, value):
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value), encoding="utf-8")
        temporary.replace(path)

    def test_concurrent_polling_shares_counts_with_inference_cache_disabled(self):
        self.write(self.dataset, {"document_id": "DOC", "status": "partial",
            "extraction_mode": "dataagent2", "entities": [{"METRIC": [{}, {}]}, {"METRIC": [{}]}],
            "records": [{"provenance": ["unused"]}], "summary": {"missing_required": 2}})
        with patch.dict(os.environ, {"SDA_CACHE_ENABLED": "0"}), \
             patch("studio.sda_data.read_json", wraps=sda_data.read_json) as reader, \
             ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(lambda _: self.workspace.registry_dataset(self.results), range(8)))
            self.assertEqual(sum(call.args[0] == self.dataset for call in reader.call_args_list), 1)
        self.assertTrue(all(result == results[0] for result in results))
        metadata, count = results[0]
        self.assertEqual(count, 3)
        self.assertEqual(metadata["summary"]["missing_required"], 2)
        self.assertNotIn("entities", metadata)
        self.assertNotIn("records", metadata)
        self.assertFalse(self.workspace.cache)

    def test_replaced_dataset_updates_public_counts_status_and_warnings(self):
        self.write(self.dataset, {"document_id": "DOC", "status": "partial", "records": [{}],
                                  "summary": {"missing_required": 2}})
        before = self.workspace.patents()[0]
        self.assertEqual((before["record_count"], before["semantic_status"], before["quality_warning_count"]),
                         (1, "partial", 2))
        self.write(self.dataset, {"document_id": "DOC", "status": "complete", "records": [{}, {}],
                                  "summary": {"missing_required": 0}})
        after = self.workspace.patents()[0]
        self.assertEqual((after["record_count"], after["semantic_status"], after["quality_warning_count"]),
                         (2, "complete", 0))

    def test_template_fallback_invalidates_when_template_changes(self):
        self.write(self.dataset, {"document_id": "DOC", "records": [{}]})
        template = self.results / "template.completed.json"
        self.write(template, {"entities": [{"METRIC": [{}, {}]}]})
        self.assertEqual(self.workspace.registry_dataset(self.results)[1], 2)
        self.write(template, {"entities": [{"METRIC": [{}, {}, {}]}]})
        self.assertEqual(self.workspace.registry_dataset(self.results)[1], 3)
        self.write(self.dataset, {"document_id": "DOC", "entities": [], "records": [{}]})
        self.assertEqual(self.workspace.registry_dataset(self.results)[1], 1)

    def test_review_does_not_parse_duplicate_templates_as_ocr_tables(self):
        self.write(self.dataset, {"document_id": "DOC", "entities": [], "records": []})
        self.write(self.results / "template.completed.json", {"entities": []})
        self.write(self.results / "template.json", {"unneeded": "duplicate template"})
        self.write(self.results / "data_construction.json", {"records": []})
        with patch("studio.sda_data.read_json", wraps=sda_data.read_json) as reader:
            bundle = self.workspace._build("DOC", self.results, self.root / "data/DOC")
        names = {call.args[0].name for call in reader.call_args_list}
        self.assertNotIn("template.json", names)
        self.assertNotIn("data_construction.json", names)
        self.assertEqual(bundle["records"], [])

    def test_browser_review_reuses_only_unchanged_source_versions(self):
        self.write(self.dataset, {"document_id": "DOC", "records": [{"NAME": "First"}]})
        with patch.dict(os.environ, {"SDA_CACHE_ENABLED": "0"}), \
             patch.object(self.workspace, "_build", wraps=self.workspace._build) as builder:
            first = self.workspace.review("DOC")
            self.assertIs(self.workspace.review("DOC"), first)
            self.assertEqual(builder.call_count, 1)
            self.assertEqual(first["records"][0]["antibody_id"], "First")
            self.write(self.dataset, {"document_id": "DOC", "records": [{"NAME": "Updated"}]})
            updated = self.workspace.review("DOC")
            self.assertIsNot(updated, first)
            self.assertEqual(builder.call_count, 2)
            self.assertEqual(updated["records"][0]["antibody_id"], "Updated")

    def test_repeated_local_url_redirects_to_the_studio_homepage(self):
        server = StudioServer(("127.0.0.1", 0), self.workspace)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}"
        for address in ("http://127.0.0.1:4182/", "http%3A%2F%2F127.0.0.1%3A4182%2F"):
            with self.subTest(address=address), urlopen(base + "/" + address) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.url, base + "/studio/")
                self.assertIn(b"SeqDataAgent", response.read())

    def test_table_response_keeps_values_links_and_full_saved_snapshot(self):
        record = {"record_id": "r1", "document_id": "DOC", "vh": "EVQL", "vl": "DIQL",
                  "ic50": 1, "ic50_unit": "nM", "evidence": [{"page": 1, "table_id": "t1"}],
                  "sda_fields": {"vh": {"status": "present", "provenance": [{"quote": "EVQL"}]}},
                  "sda_sources": [{"quote": "raw evidence"}],
                  "assay_keywords": [{"key": "SPR", "category": "method", "label_en": "SPR",
                                      "target_id": "TSLP", "evidence": [{"quote": "method evidence"}]}],
                  "entity_annotations": [{"category": "format", "value": "IgG", "evidence": [{}]}]}
        with self.workspace.connection() as db:
            db.execute("INSERT INTO publications VALUES (?, ?, ?, ?)",
                       ("SDA", "DOC", "r1", json.dumps(record)))
        server = StudioServer(("127.0.0.1", 0), self.workspace)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}/api/dataset/records"
        with urlopen(base + "?view=table&dataset_id=SDA") as response:
            projected = json.load(response)["records"][0]
        for key in ("record_id", "vh", "vl", "ic50", "ic50_unit", "evidence"):
            self.assertEqual(projected[key], record[key])
        self.assertEqual(projected["assay_keywords"][0]["target_id"], "TSLP")
        self.assertNotIn("sda_fields", projected)
        self.assertNotIn("sda_sources", projected)
        self.assertNotIn("evidence", projected["assay_keywords"][0])
        with urlopen(base) as response:
            complete = json.load(response)["records"][0]
        for key, value in record.items():
            self.assertEqual(complete[key], value)


if __name__ == "__main__":
    unittest.main()
