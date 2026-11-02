"""Experimental encoding must preserve characters, IDs and complete seed data."""

import copy
import unittest

from dataagent2.pipeline import reader_payload
from dataagent2.reader_transport import (adaptive_chunks, catalogue_payload, decode, experimental_input,
                                         pack_records, unpack_records)
from dataagent2.test_dataagent2 import fixture, response


class ReaderTransportTests(unittest.TestCase):
    def test_adaptive_chunks_keep_dense_sequence_blocks_and_every_source(self):
        data = fixture()
        for sid in range(1, 5):
            data["sources"][sid] = {**data["sources"][1], "id": sid, "page": sid,
                "text": "Ordinary prose." if sid != 3 else "EVQLVESGGG " * 6,
                "kind": "text", "table_id": None, "row": None, "column": None}
        chunks = [{"id": f"c{s}", "core": [s], "context": []} for s in range(1, 5)]
        result = adaptive_chunks(data, chunks)
        self.assertEqual([c["core"] for c in result], [[1, 2], [3], [4]])
        self.assertIn("dense_sequence_letters", result[1]["preservation_reasons"])

    def test_native_catalogue_preserves_source_format_and_uses_small_global_counts(self):
        data = fixture()
        data["seed"]["observations"] = [{"item": "i1", "s": 1, "metric": "IC50", "value": "0.25", "refs": [1]}]
        p = reader_payload(data, {"id": "c1", "core": [2], "context": []})
        c, instructions = experimental_input(p, "catalogue_native")
        self.assertEqual(c["sources"], p["sources"])
        self.assertNotIn("transport", c)
        self.assertNotIn("observation_addresses", c["dataset_catalogue"])
        self.assertEqual(c["dataset_catalogue"]["observation_counts"], [{"item": "i1", "count": 1}])
        self.assertIn("prior context, not new evidence", instructions)

    def test_full_encoding_preserves_missing_null_unicode_and_order(self):
        data = fixture()
        data["seed"]["links"] = response()["links"]
        data["seed"]["entities"][0]["native_id"] = None
        data["seed"]["entities"][1]["note"] = "中文 \n literal"
        data["seed"]["observations"] = [{"item": "i1", "value": "0.123", "unit": None, "refs": [1], "s": 1},
                                         {"item": "i2", "value": "0.123", "refs": [1], "s": 1}]
        payload = reader_payload(data, {"id": "c1", "core": [1, 2, 3], "context": []})
        before = copy.deepcopy(payload)
        packed, _ = experimental_input(payload, "compact_full")
        self.assertEqual(decode(packed), before)
        self.assertEqual(payload, before)
        self.assertNotIn("dataset_catalogue", packed)

    def test_source_groups_do_not_change_table_coordinates_or_source_order(self):
        data = fixture()
        data["sources"][3]["text"] = "AA\nBB <br> CC"
        payload = reader_payload(data, {"id": "c1", "core": [1, 3], "context": [2]})
        packed, _ = experimental_input(payload, "compact_full")
        self.assertEqual(decode(packed)["sources"], payload["sources"])
        self.assertEqual([r[0] for r in decode(packed)["sources"]], [2, 1, 3])

    def test_catalogue_keeps_every_name_and_unmatched_source(self):
        data = fixture()
        data["sources"][2]["text"] = "A previously unnamed shared-chain statement."
        p = reader_payload(data, {"id": "c1", "core": [2], "context": []})
        c = catalogue_payload(p)
        self.assertEqual({e["id"] for e in c["dataset_catalogue"]["entities"]}, {"i1", "i2"})
        self.assertEqual(c["initial_dataset"]["entities"], [])
        self.assertEqual(c["sources"], p["sources"])
        self.assertEqual(decode(experimental_input(p, "catalogue")[0]), c)

    def test_names_do_not_match_a_variant_by_prefix(self):
        data = fixture()
        data["seed"]["entities"][0].update(name="AB-1", names=["AB-1"])
        data["seed"]["entities"][1].update(name="AB-1a", names=["AB-1a"])
        data["sources"][2]["text"] = "AB-1a was tested."
        p = reader_payload(data, {"id": "c1", "core": [2], "context": []})
        self.assertEqual(catalogue_payload(p)["retrieval"]["selected_items"], ["i2"])

    def test_number_match_retrieves_candidates_but_not_inferred_new_links(self):
        data = fixture()
        data["seed"]["links"] = response()["links"]
        p = reader_payload(data, {"id": "c1", "core": [2], "context": []})
        c = catalogue_payload(p)
        self.assertEqual(c["retrieval"]["selected_items"], ["i1", "i2"])
        self.assertEqual(c["initial_dataset"]["links"], data["seed"]["links"])

    def test_decoder_rejects_missing_or_repeated_record_positions(self):
        packed = pack_records([{"a": 1}, {"a": 2}])
        packed["tables"][0]["positions"] = [0, 0]
        with self.assertRaisesRegex(ValueError, "Repeated"):
            unpack_records(packed)

    def test_typed_catalogue_keeps_local_values_and_indexes_omitted_detail(self):
        data = fixture()
        data["sources"][2]["text"] = "Antibody A VH SEQ ID 1"
        data["seed"]["observations"] = [{"item": "i1", "s": 1, "metric": "IC50", "value": "0.25", "refs": [1]},
                                         {"item": "i2", "s": 2, "metric": "IC50", "value": "0.5", "refs": [2]}]
        p = reader_payload(data, {"id": "c1", "core": [2], "context": []})
        c = catalogue_payload(p, typed=True)
        self.assertEqual([o["s"] for o in c["initial_dataset"]["observations"]], [2])
        self.assertEqual([o["s"] for o in c["dataset_catalogue"]["observation_addresses"]], [1])
        self.assertEqual(decode(experimental_input(p, "catalogue_typed")[0]), c)
        packed = pack_records([{"a": 1}, {"b": 2}])
        packed["tables"].pop()
        with self.assertRaisesRegex(ValueError, "omitted"):
            unpack_records(packed)


if __name__ == "__main__":
    unittest.main()
