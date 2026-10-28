"""Chunk boundaries and concurrent completion must preserve source evidence."""

import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from dataagent2.contracts import empty_response
from dataagent2.inputs import load_input, make_chunks, source_row, split_text
from dataagent2.pipeline import run
from dataagent2.provider import Provider, compact, write_json
from dataagent2.test_dataagent2 import fixture, response


class ChunkBoundaryTests(unittest.TestCase):
    def table_data(self, rows=12):
        data = fixture()
        data["sources"] = {}
        for row in range(rows):
            for column in range(3):
                sid = len(data["sources"]) + 1
                data["sources"][sid] = {"id": sid, "source_ref": f"t:R{row}C{column}",
                    "text": ("header" if row < 2 else "value") + " x" * 100,
                    "page": 1, "kind": "cell", "table_id": "t", "row": row, "column": column}
        return data

    def test_table_rows_stay_together_and_continuations_repeat_headers(self):
        data = self.table_data()
        chunks = make_chunks(data, 3000)
        owners = {sid: c["id"] for c in chunks for sid in c["core"]}
        self.assertGreater(len(chunks), 1)
        for row in range(12):
            self.assertEqual(len({owners[s] for s, source in data["sources"].items() if source["row"] == row}), 1)
        for chunk in chunks[1:]:
            self.assertTrue({1, 2, 3} <= set(chunk["context"]))
        core = [sid for chunk in chunks for sid in chunk["core"]]
        self.assertEqual(len(core), len(set(core)))
        self.assertEqual(set(core), set(data["sources"]))
        for chunk in chunks:
            size = len(compact([source_row(data["sources"][sid]) for sid in chunk["context"] + chunk["core"]]))
            self.assertEqual(size, chunk["source_characters"])
            self.assertLessEqual(size, 3000)

    def test_small_table_remains_in_one_chunk(self):
        chunks = make_chunks(self.table_data(rows=3), 3000)
        self.assertEqual(len(chunks), 1)

    def test_boundary_context_includes_both_neighboring_chunks(self):
        data = fixture()
        data["sources"] = {sid: {**data["sources"][1], "id": sid, "page": 1, "text": "x" * 150}
                           for sid in range(1, 50)}
        chunks = make_chunks(data, 3000)
        self.assertGreater(len(chunks), 2)
        middle = chunks[1]
        self.assertTrue(set(chunks[0]["core"]) & set(middle["context"]))
        self.assertTrue(set(chunks[2]["core"]) & set(middle["context"]))

    def test_example_heading_starts_a_new_chunk_when_previous_chunk_is_substantial(self):
        data = fixture()
        data["sources"] = {sid: {**data["sources"][1], "id": sid, "page": 1, "text": "x" * 150}
                           for sid in range(1, 13)}
        data["sources"][10]["text"] = "Example 3. Binding experiments"
        chunks = make_chunks(data, 3000)
        self.assertEqual(chunks[0]["core"], list(range(1, 10)))
        self.assertEqual(chunks[1]["core"][0], 10)

    def test_long_text_splits_at_whitespace_with_reversible_offsets(self):
        text = ("[0123] Printed sequence and prose words.\n\n" * 600) + "END"
        spans = list(split_text(text))
        self.assertEqual("".join(value for _, value in spans), text)
        for offset, value in spans:
            self.assertLessEqual(len(value), 8000)
            self.assertEqual(text[offset:offset + len(value)], value)
        self.assertTrue(all(value.endswith("\n") for _, value in spans[:-1]))
        with tempfile.TemporaryDirectory() as tmp:
            native = Path(tmp)
            write_json(native / "sda_input.json", {"document_id": "test", "tables": [],
                "document_blocks": [{"source_ref": "ocr:long", "block_id": "long", "text": text, "page": 1}]})
            write_json(native / "document_template.json", {"document_id": "test"})
            data = load_input(native)
            self.assertEqual(data["seed"]["entities"], [])
            self.assertEqual([(s["source_offset"], s["text"]) for s in data["sources"].values()], spans)

    def test_indivisible_source_over_budget_is_explicit_and_untruncated(self):
        data = fixture()
        data["sources"] = {1: {**data["sources"][1], "text": "A" * 4000}}
        chunk = make_chunks(data, 3000)[0]
        self.assertEqual(chunk["oversized_source_ids"], [1])
        self.assertEqual(chunk["context"], [])
        self.assertEqual(data["sources"][1]["text"], "A" * 4000)


class ParallelReaderTests(unittest.TestCase):
    def test_all_23_chunks_start_before_any_call_completes_by_default(self):
        for workers in (None, 0):
            with self.subTest(workers=workers):
                all_started = threading.Barrier(23, timeout=5)
                started, lock = [], threading.Lock()
                datasets = []

                class FakeProvider:
                    model, effort = "test", "test"

                    def generate(self, stage, instructions, payload):
                        with lock:
                            started.append(payload["chunk"])
                            datasets.append(copy.deepcopy(payload["seed_dataset"]))
                        # Any hidden 3/8-worker cap prevents this rendezvous.
                        all_started.wait()
                        return empty_response(), {}

                    def usage(self):
                        return {"total_tokens": 0}

                data = fixture()
                data["sources"] = {sid: {**data["sources"][1], "id": sid,
                    "page": sid, "text": "x" * 2800} for sid in range(1, 24)}
                expected_seed = copy.deepcopy(data["seed"])
                with tempfile.TemporaryDirectory() as tmp:
                    options = {} if workers is None else {"workers": workers}
                    report = run(data, FakeProvider(), tmp, chunk_chars=3000, max_questions=0, **options)
                    manifest = json.loads((Path(tmp) / "manifest.json").read_text())
                self.assertTrue(report["reader_complete"])
                self.assertEqual(report["coverage"]["completed_chunks"], 23)
                self.assertEqual(manifest["reader_workers"], 23)
                self.assertEqual(set(started), {f"c{i}" for i in range(1, 24)})
                self.assertEqual(len(started), 23)
                self.assertTrue(expected_seed["entities"])
                self.assertEqual(data["seed"], expected_seed)
                self.assertEqual(datasets, [expected_seed] * 23)

    def test_empty_source_document_does_not_create_a_zero_worker_pool(self):
        class FakeProvider:
            model, effort = "test", "test"

            def generate(self, *args):
                raise AssertionError("Empty document should not call Reader")

            def usage(self):
                return {"total_tokens": 0}

        data = fixture()
        data["sources"] = {}
        data["seed"] = {key: [] for key in data["seed"]}
        with tempfile.TemporaryDirectory() as tmp:
            report = run(data, FakeProvider(), tmp, max_questions=0)
        self.assertEqual(report["coverage"]["total_chunks"], 0)
        self.assertTrue(report["reader_complete"])

    def test_workers_overlap_and_out_of_order_responses_merge_in_document_order(self):
        first_two = threading.Barrier(2, timeout=5)
        second_observed = threading.Event()

        class FakeProvider:
            model, effort = "test", "test"

            def generate(self, stage, instructions, payload):
                if payload["chunk"] in ("c1", "c2"):
                    first_two.wait()
                if payload["chunk"] == "c1":
                    if not second_observed.wait(timeout=5):
                        raise AssertionError("Second chunk was not processed concurrently")
                    result = response()
                    result["issues"].append({"kind": "other", "items": [], "seq": None, "refs": [99]})
                    return result, {}
                return empty_response(), {}

            def usage(self):
                return {"total_tokens": 0}

        data = fixture()
        for sid in (4, 5):
            data["sources"][sid] = {**data["sources"][1], "id": sid, "page": sid, "text": "x" * 2800}
        snapshots = []

        def save(path, value):
            if Path(path).name == "progress.json" and value.get("completed_chunks") == 1:
                second_observed.set()
            if Path(path).name == "dataset.partial.json":
                snapshots.append(copy.deepcopy(value))
            write_json(path, value)

        with tempfile.TemporaryDirectory() as tmp, patch("dataagent2.pipeline.write_json", side_effect=save):
            report = run(data, FakeProvider(), tmp, chunk_chars=3000, workers=2, max_questions=0)
        self.assertTrue(report["reader_complete"])
        self.assertTrue(second_observed.is_set())
        self.assertGreaterEqual(snapshots[0]["reader_coverage"]["completed_chunks"], 2)
        self.assertEqual(snapshots[-1]["reader_coverage"]["completed_chunks"], 3)
        self.assertEqual(report["final"]["variable_chains"], 2)
        self.assertEqual(report["final"]["rejected_facts"], 1)

    def test_unpaced_provider_does_not_wait_for_another_documents_pacing(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch("dataagent2.provider.NEXT_REQUEST_AT", 1000000), \
             patch("dataagent2.provider.time.sleep") as sleep:
            Provider(tmp, request_interval=0).admit()
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
