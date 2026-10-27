import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dataagent.llm_io import ConnectivityResult
from dataagent.llm_runtime import CodexProvider
from dataagent.llm_sequence_agent import SequenceExtractionAgent
from dataagent.sequence_evidence import build_document_structure
from document import pdf2doc
import test_pdf2doc_document as fixture


class FreshInferenceTests(unittest.TestCase):
    def test_identical_requests_ignore_existing_cache_and_keep_separate_audits(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            invocations = []
            def process(command, **kwargs):
                output = Path(command[command.index("--output-last-message") + 1])
                invocations.append(output)
                class Process:
                    returncode = 0
                    def communicate(self, prompt, timeout=None):
                        output.write_text('{"status":"ok"}')
                        return json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 2}}), ""
                return Process()
            def request(provider):
                return provider.generate("probe", "Return ok", {}, ConnectivityResult)
            with patch("dataagent.llm_runtime.subprocess.Popen", process):
                with patch.dict(os.environ, {"SDA_CACHE_ENABLED": "1"}):
                    provider = CodexProvider(root / "cache", audit_path=root / "cached/llm_calls.jsonl")
                    self.assertFalse(request(provider)[1]["cache_hit"])
                    self.assertTrue(request(provider)[1]["cache_hit"])
                self.assertEqual(len(invocations), 1)
                with patch.dict(os.environ, {"SDA_CACHE_ENABLED": "0"}):
                    provider = CodexProvider(root / "cache", audit_path=root / "fresh/llm_calls.jsonl")
                    for _ in range(2):
                        metadata = request(provider)[1]
                        self.assertFalse(metadata["cache_hit"])
                        self.assertFalse(metadata["cache_enabled"])
                self.assertEqual(len(invocations), 3)
                self.assertEqual(len(list((root / "cache").glob("*/validated.json"))), 1)
                self.assertEqual(len(list((root / "fresh/requests").iterdir())), 2)
                self.assertFalse(list((root / "fresh").rglob("validated.json")))

    def test_sequence_batches_do_not_reuse_or_write_cache_when_provider_disables_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            class Provider:
                cache_enabled = False
                cache_dir = Path(temporary) / "cache"
                calls = 0
                def generate(self, task, instructions, payload, contract, **kwargs):
                    self.calls += 1
                    result = {"document_id": "DOC", "request_refs": [], "search_queries": [], "complete": True,
                        "decisions": [{"entity_id": "Ab1", "chain": chain, "status": "unresolved", "value": None,
                            "seq_id": None, "source_scope": "unknown", "fragments": [], "evidence_refs": [],
                            "rationale": "No sequence supplied"} for chain in ("VH", "VL")]}
                    kwargs["validate"](result)
                    return result, {"cache_hit": False}
            provider = Provider()
            agent = SequenceExtractionAgent(provider, max_workers=1)
            graph = build_document_structure("DOC", [])
            template = {"entity_template": {"entities": [{"entity_id": "Ab1", "canonical_name": "Ab1", "raw_names": ["Ab1"]}]}}
            for _ in range(2):
                result = agent.run(graph, template)
                self.assertFalse(result["batches"][0]["cache_hit"])
            self.assertEqual(provider.calls, 2)
            self.assertFalse(provider.cache_dir.exists())

    def test_disabled_page_cache_recognizes_every_page_again(self):
        case = fixture.PDFDocumentTests()
        case.setUp()
        self.addCleanup(case.tearDown)
        output = case.root / "ocr"
        with patch.dict(os.environ, {"SDA_CACHE_ENABLED": "1"}):
            pdf2doc.ocr_pdf(case.source, output, document_id="demo", config=case.config)
        before = case.engine.predict.call_count
        with patch.dict(os.environ, {"SDA_CACHE_ENABLED": "0"}), \
             patch.object(pdf2doc, "_cached_page", side_effect=AssertionError("Cache read attempted")):
            summary = pdf2doc.ocr_pdf(case.source, output, document_id="demo", config=case.config)
        self.assertEqual(case.engine.predict.call_count - before, 2)
        self.assertEqual(summary["cached_pages"], 0)


if __name__ == "__main__":
    unittest.main()
