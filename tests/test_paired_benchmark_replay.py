"""Cache-only replay must reject changed requests without invoking a model."""

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from dataagent2.contracts import SCHEMA, empty_response
from dataagent2.provider import digest
from paired_benchmark_replay import SavedResponses


class SavedResponsesTest(unittest.TestCase):
    def test_exact_cache_hit_never_invokes_model(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "manifest.json").write_text(json.dumps({"model": "test-model", "effort": "low"}))
            payload = {"document": "patent", "sources": []}
            fingerprint = {"stage": "reader", "instructions": "literal source only", "payload": payload,
                           "schema": SCHEMA, "model": "test-model", "effort": "low"}
            folder = root / "requests" / digest(fingerprint)
            folder.mkdir(parents=True)
            (folder / "input.json").write_text(json.dumps(fingerprint))
            (folder / "validated.json").write_text(json.dumps(empty_response()))
            with patch("dataagent2.provider.subprocess.Popen") as model:
                provider = SavedResponses(root)
                response, metadata = provider.generate("reader", "literal source only", payload)
                self.assertEqual(response, empty_response())
                self.assertTrue(metadata["cache_hit"])
                model.assert_not_called()

    def test_cache_miss_fails_without_model_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "manifest.json").write_text(json.dumps({"model": "test-model", "effort": "low"}))
            with patch("dataagent2.provider.subprocess.Popen") as model:
                with self.assertRaisesRegex(RuntimeError, "no model call allowed"):
                    SavedResponses(root).generate("reader", "changed instruction", {"sources": []})
                model.assert_not_called()


if __name__ == "__main__":
    unittest.main()
