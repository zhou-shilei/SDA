"""Source-only grouping must preserve grounding and existing run replay."""

import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dataagent2.contracts import INSTRUCTIONS
from dataagent2.pipeline import reader_payload, reader_request, run
from dataagent2.provider import digest, response_schema, write_json
from dataagent2.source_transport import (ENCODING, decode_payload, encode_payload, instructions,
                                         pack_sources, unpack_sources)
from dataagent2.test_dataagent2 import action_response, fixture, response


class SourceTransportTests(unittest.TestCase):
    def test_pages_captions_sparse_cells_and_original_order_roundtrip(self):
        rows = [[7, 2, "cell", "t", 3, 5, "AA\n BB <br> CC"],
                [1, 1, "text", None, None, None, " 前文\n"],
                [2, 2, "text", "t", None, None, "Table 1. 标题"],
                [8, 2, "cell", "t", 0, 0, "clone"],
                [9, 2, "cell", "t", 0, 3, "Score"],
                [10, 2, "cell", "t", 4, 0, "AB-1"],
                [11, 2, "cell", "t", 4, 3, ""],
                [12, None, "unstructured", "raw", None, None, "<table>literal</table>"],
                [13, 2, "cell", "t", None, 2, "Unknown physical row"]]
        original = copy.deepcopy(rows)
        grouped = pack_sources(rows)
        self.assertEqual(unpack_sources(grouped), original)
        self.assertEqual(rows, original)
        self.assertEqual(grouped[3]["rows"][0][1][1][0], 3)

    def test_same_cell_multiple_sources_are_not_semantically_merged(self):
        rows = [[1, 1, "cell", "t", 0, 0, "FULL"], [2, 1, "cell", "t", 0, 0, "SLICE"]]
        self.assertEqual(unpack_sources(pack_sources(rows)), rows)
        self.assertEqual(len(pack_sources(rows)[0]["rows"][0][1]), 2)

    def test_seed_ocr_styles_context_scope_and_anchors_are_unchanged(self):
        p = reader_payload(fixture(), {"id": "c1", "core": [1, 3], "context": [2]})
        p["source_sequence_ocr"] = {3: {"status": "failed", "error": "Incomplete crop"}}
        p["source_image_styles"] = {3: {"domain": {"start": 2, "end": 10}, "spans": []}}
        p["source_text_styles"] = {3: [{"start": 0, "end": 3, "underline": True}]}
        original = copy.deepcopy(p)
        wire = encode_payload(p)
        self.assertEqual(decode_payload(wire), original)
        for key in set(p) - {"sources"}:
            self.assertEqual(wire[key], original[key])
        wire["initial_dataset"]["entities"][0]["name"] = "Changed"
        self.assertEqual(p, original)

    def test_invalid_or_ambiguous_groups_cannot_silently_drop_evidence(self):
        invalid = [
            [{"page": 1, "kind": "text", "lines": [[1, "a"]], "rows": []}],
            [{"page": 1, "kind": "text", "lines": [[1, "a"], [1, "b"]]}],
            [{"page": 1, "kind": "cell", "table": "t", "rows": [[0, [[0, 1]]]]}],
            [{"page": 1, "kind": "text", "lines": [[True, "a"]]}],
            [{"page": 1, "kind": "text", "lines": []}],
        ]
        for groups in invalid:
            with self.subTest(groups=groups), self.assertRaises(ValueError):
                unpack_sources(groups)
        with self.assertRaisesRegex(ValueError, "Repeated"):
            pack_sources([[1, 1, "text", None, None, None, "a"]] * 2)

    def test_encoding_and_format_rules_do_not_change_other_reader_rules(self):
        self.assertEqual(instructions(INSTRUCTIONS), INSTRUCTIONS)
        changed = instructions(INSTRUCTIONS, "grouped")
        self.assertIn("Cite original INTEGER source IDs", changed)
        for rule in ["FULL initial business seed", "Never merge similar names", "Never omit uncertain residues"]:
            self.assertIn(rule, changed)
        with self.assertRaises(ValueError):
            decode_payload({"source_encoding": "unknown", "sources": []})
        with self.assertRaises(ValueError):
            encode_payload({"source_encoding": ENCODING, "sources": []})
        self.assertEqual(pack_sources([]), [])

    def test_existing_rows_mode_is_identical_to_old_input_and_prompt(self):
        data = fixture()
        chunk = {"id": "c1", "core": [1, 2, 3], "context": []}
        p, prompt = reader_request(data, chunk)
        self.assertEqual(p, reader_payload(data, chunk))
        self.assertEqual(prompt, INSTRUCTIONS)

    def test_grouped_pipeline_can_replay_and_refuses_mixed_output_settings(self):
        from dataagent2.replay import replay
        data = fixture()

        class CachedProvider:
            model, effort, answer_effort = "test", "test", "test"

            def usage(self):
                return {"total_tokens": 0}

            def generate(self, stage, prompt, payload):
                if stage == "reader":
                    self_check.assertEqual(decode_payload(payload), reader_payload(data, {"id": "c1", "core": [1, 2, 3], "context": []}))
                    result = response()
                else:
                    result = action_response(payload)
                request = {"stage": stage, "instructions": prompt, "payload": payload,
                    "schema": response_schema(stage, payload=payload), "model": self.model, "effort": self.effort}
                key = digest(request)
                write_json(output / "requests" / key / "input.json", request)
                write_json(output / "requests" / key / "validated.json", result)
                return result, {"key": key}

        self_check = self
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            report = run(data, CachedProvider(), output, max_questions=0, reader_source_format="grouped")
            self.assertTrue(report["benchmark_complete"])
            expected = json.loads((output / "dataset.json").read_text())
            with patch("dataagent2.replay.load_input", return_value=data), \
                    patch("dataagent2.provider.subprocess.Popen", side_effect=AssertionError("Replay called a model")):
                replay(output)
            actual = json.loads((output / "dataset.checkpoint.json").read_text())
            for key in ["entities", "observations", "rejected_facts"]:
                self.assertEqual(actual[key], expected[key])
            with self.assertRaisesRegex(ValueError, "different inputs"):
                run(data, CachedProvider(), output, max_questions=0)


if __name__ == "__main__":
    unittest.main()
