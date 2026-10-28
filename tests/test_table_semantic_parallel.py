"""Maximum concurrency must exceed eight without losing source ownership."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PIL import Image
from dataagent.agent.schemas import AgentConfig
from dataagent.llm_table_agents import LLMTableClassificationAgent
from document.sequence_styles import review_prepared, validate_style_source

SEQUENCE = "ACDEFGHIKLMNPQRSTVWY" * 4


class TableParallelTests(unittest.TestCase):
    def tables(self, root, count):
        image = root / "table.png"
        Image.new("RGB", (400, 100), "white").save(image)
        return [{"source_id": f"T{i}", "page": i + 1, "image_path": str(image),
                 "width": 400, "height": 100, "bbox": [0, 0, 400, 100],
                 "bbox_padded": [0, 0, 400, 100], "rows": [[{
                     "source_ref": f"T{i}:R0C0", "text": SEQUENCE,
                     "origin_row": 0, "origin_column": 0,
                     "sequence_ocr": {"status": "failed"}}]]} for i in range(count)]

    def test_all_style_requests_start_and_failed_table_does_not_drop_other_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prepared = {"tables": self.tables(root, 9), "document_blocks": []}
            original = copy.deepcopy(prepared)
            barrier = threading.Barrier(9)

            class Provider:
                def generate(self, task, instructions, payload, contract, **kwargs):
                    barrier.wait(timeout=10)
                    if payload["table_id"] == "T4":
                        raise RuntimeError("one image request failed")
                    answer = {"cells": [{"source_ref": payload["cells"][0]["source_ref"],
                        "status": "confirmed", "marked_sequence": SEQUENCE,
                        "bbox_crop": [0, 0, 400, 100], "annotation_quote": "",
                        "domain_chain": "none", "domain_sequence": "", "reason": "literal image"}]}
                    kwargs["validate"](answer)
                    return contract.model_validate(answer).model_dump(), {}

            report = review_prepared(prepared, Provider(), root / "out", max_workers=0)
            self.assertEqual(report["performance"]["workers"], 9)
            self.assertEqual(report["performance"]["completed_tables"], 9)
            self.assertEqual([s["table_id"] for s in report["sources"]], [f"T{i}" for i in range(9) if i != 4])
            self.assertEqual(len(report["failures"]), 1)
            self.assertEqual(report["failures"][0]["table_id"], "T4")
            for actual, previous in zip(prepared["tables"], original["tables"]):
                self.assertEqual(actual["rows"], previous["rows"])
            originals = {c["source_ref"]: c for t in prepared["tables"] for row in t["rows"] for c in row}
            for source in report["sources"]:
                validate_style_source(source, originals)
            self.assertEqual(json.loads((root / "out/sequence_styles.json").read_text()), report)

    def test_all_tables_classify_individually_without_triage_or_eight_worker_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tables = self.tables(root, 9)
            barrier = threading.Barrier(9)

            class Provider:
                audit_path = root / "llm_calls.jsonl"

                def generate(self, task, instructions, payload, contract, **kwargs):
                    if task != "table-classification-v4" or len(payload["tables"]) != 1:
                        raise AssertionError("Maximum mode must submit one direct request per table")
                    barrier.wait(timeout=10)
                    identity = payload["tables"][0]["table_id"]
                    answer = {"tables": [{"table_id": identity, "primary_label": "non_target_table",
                        "labels": ["non_target_table"], "confidence": 1,
                        "entity_axis": "unknown", "measurement_axis": "unknown", "decision": "exclude",
                        "detected_endpoints": [], "evidence_refs": [identity],
                        "explanation": "Non-target fixture", "regions": [], "issues": []}]}
                    answer = contract.model_validate(answer).model_dump()
                    kwargs["validate"](answer)
                    return answer, {"mode": "llm"}

            results = LLMTableClassificationAgent(Provider(), image_roots=[root], max_workers=0,
                                                   coarse_triage=True).run(tables)
            self.assertEqual([r["table_id"] for r in results], [f"T{i}" for i in range(9)])
            performance = json.loads((root / "classification_performance.json").read_text())
            self.assertEqual(performance["workers"], 9)
            self.assertEqual(performance["batch_size"], 1)
            self.assertFalse(performance["coarse_triage"])
            self.assertEqual(performance["completed_tables"], 9)

    def test_zero_workers_passes_config_and_negative_workers_fail(self):
        self.assertEqual(AgentConfig(document_id="DOC", input_dir=".", output_dir=".", llm_workers=0).llm_workers, 0)
        with self.assertRaises(ValueError):
            LLMTableClassificationAgent(None, image_roots=[], max_workers=-1)
        with self.assertRaises(ValueError):
            review_prepared({"tables": []}, None, ".", max_workers=-1)


if __name__ == "__main__":
    unittest.main()
