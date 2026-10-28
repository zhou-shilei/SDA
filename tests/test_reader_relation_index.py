"""Regressions for source-backed partial mappings and experiment identity."""

import json
import csv
import tempfile
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from dataagent2.contracts import empty_response
from dataagent2.ledger import Ledger
from dataagent2.pipeline import reader_payload, question_tasks
from dataagent2.test_dataagent2 import fixture, response, SEQUENCE
from dataagent2.export_rows import export


class RelationIndexTests(unittest.TestCase):
    def test_reader_uses_relationships_without_repeating_residues_or_measurement_values(self):
        data = fixture()
        data["seed"]["links"] = response()["links"]
        data["seed"]["observations"] = [{"item": "i1", "experiment": "e1", "metric": "IC50",
            "value": "VALUE_NOT_FOR_READER_CONTEXT", "unit": "nM", "s": 2, "refs": [2]}]
        data["style_definitions"] = [{"seq": 1, "name": None, "region": "VH", "value": SEQUENCE, "refs": [2, 3]}]
        payload = reader_payload(data, {"id": "c1", "core": [2], "context": []})
        self.assertNotIn("seed_dataset", payload)
        index = payload["relation_index"]
        self.assertEqual({l["item"] for l in index["links"]}, {"i1", "i2"})
        self.assertEqual({e["id"] for e in index["items"]}, {"i1", "i2"})
        self.assertNotIn(SEQUENCE, json.dumps(index))
        self.assertNotIn("VALUE_NOT_FOR_READER_CONTEXT", json.dumps(payload))
        self.assertEqual(index["definitions"][0]["seq"], 1)

    def test_short_cdr_and_framework_are_preserved_without_becoming_vh(self):
        data = fixture()
        data["sources"][1]["text"] = "Antibody A HCDR1 SEQ ID 2; HFR1 SEQ ID 3"
        data["sources"][2]["text"] = "GFTFSSYA"
        data["sources"][3]["text"] = "EVQLVESGGG"
        r = empty_response()
        for region, seq, sid in [("HCDR1", 2, 2), ("HFR1", 3, 3)]:
            r["links"].append({"item": "i1", "region": region, "seq": seq, "name": None, "refs": [1]})
            r["sequences"].append({"region": region, "seq": seq, "name": None,
                "parts": [{"s": sid, "start": "", "end": ""}], "refs": [1, sid]})
        ledger = Ledger(data)
        ledger.apply(r, [1, 2, 3], "c1")
        result = ledger.assemble()
        item = result["entities"][0]
        self.assertEqual(item["sequences"]["CDRH1"]["value"], "GFTFSSYA")
        self.assertEqual(item["sequences"]["FRH1"]["value"], "EVQLVESGGG")
        self.assertIsNone(item["sequences"]["VH"]["value"])
        self.assertEqual(item["sequence_links"][0]["original_region"], "HCDR1")
        self.assertEqual(result["relation_index"]["definitions"][0]["region"], "CDRH1")

    def test_mapping_survives_missing_definition_and_full_chain_id_is_scoped(self):
        data = fixture()
        data["seed"]["links"] = [{"item": "i1", "region": "H_CHAIN", "seq": 1, "refs": [1], "name": None}]
        result = Ledger(data).assemble()
        item = result["entities"][0]
        self.assertEqual(item["sequences"]["H_CHAIN"]["mapping_status"], "supported")
        self.assertEqual(item["sequences"]["VH"]["seq_ids"], [])
        self.assertEqual(item["sequences"]["VH"]["source_chain_seq_ids"], [1])
        self.assertEqual(item["sequences"]["VH"]["seq_id_scope"], "full_chain")
        self.assertTrue(any(q["seq_ids"] == [1] for q in question_tasks(result, data)))
        self.assertTrue(result["links"])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dataset.json"
            path.write_text(json.dumps(result))
            export(path, Path(tmp) / "review")
            with (Path(tmp) / "review/items.csv").open(encoding="utf-8-sig") as stream:
                row = next(csv.DictReader(stream))
            self.assertEqual(row["H_CHAIN_seq_id"], "1")
            self.assertEqual(json.loads(row["VH_source_chain_seq_ids"]), [1])
            self.assertIn("FRH1_seq_id", row)
            self.assertIn("CDRL3_seq_id", row)

    def experiment_data(self):
        data = fixture()
        data["sources"][2]["text"] = "human ELISA"
        data["sources"][3]["text"] = "monkey ELISA"
        data["sources"][4] = {**data["sources"][2], "id": 4, "source_ref": "t:R2C0", "row": 2, "text": "1.2"}
        data["sources"][5] = {**data["sources"][3], "id": 5, "source_ref": "t:R2C1", "row": 2, "column": 1, "text": "1.2"}
        for name, sid in [("exp_human", 2), ("exp_monkey", 3)]:
            data["seed"]["experiments"].append({"id": name, "table": "t", "refs": [sid], "method": "ELISA"})
            data["seed"]["observations"].append({"item": "i1", "experiment": None, "metric": "IC50", "value": "1.2",
                "unit": None, "s": sid + 2, "table": "t", "refs": [sid, sid + 2]})
        return data

    def test_variable_extraction_retains_full_chain_number_scope(self):
        data = fixture()
        data["sources"][2]["text"] = "Heavy chain SEQ ID 1, with explicitly marked VH boundary"
        data["sources"][3]["text"] = SEQUENCE + "ASTK"
        r = empty_response()
        r["links"] = [{"item": "i1", "region": "H_CHAIN", "seq": 1, "name": None, "refs": [2]}]
        for region, start, end in [("H_CHAIN", "", ""), ("VH", SEQUENCE[:16], SEQUENCE[-16:])]:
            r["sequences"].append({"region": region, "seq": 1, "name": None,
                "parts": [{"s": 3, "start": start, "end": end}], "refs": [2, 3]})
        ledger = Ledger(data)
        ledger.apply(r, [1, 2, 3], "c1")
        vh = ledger.assemble()["entities"][0]["sequences"]["VH"]
        self.assertEqual(vh["value"], SEQUENCE)
        self.assertEqual(vh["seq_id_scope"], "full_chain")
        self.assertEqual(vh["evidence_role"], "derived_variable")

    def test_two_experiments_in_one_table_join_by_distinct_header_evidence(self):
        data = self.experiment_data()
        result = Ledger(data).assemble()
        self.assertEqual({o["s"]: o["experiment"] for o in result["observations"]}, {4: "exp_human", 5: "exp_monkey"})
        # No support for a unique header assignment: remain pending.
        for o in data["seed"]["observations"]:
            o["refs"] = [o["s"]]
        self.assertTrue(all(o["experiment"] is None for o in Ledger(data).assemble()["observations"]))

    def test_conflicting_exp_assignment_is_reported_not_overwritten(self):
        data = self.experiment_data()
        data["seed"]["observations"][0]["experiment"] = "exp_human"
        data["seed"]["observations"].append({**data["seed"]["observations"][0], "experiment": "exp_monkey"})
        result = Ledger(data).assemble()
        self.assertTrue(any(c["kind"] == "measurement_experiment_conflict" for c in result["conflicts"]))
        self.assertFalse(any(o["s"] == 4 for o in result["observations"]))

    def test_cited_tags_and_distinct_donors_survive_without_merging_experiments(self):
        data = self.experiment_data()
        data["seed"]["observations"] = []
        data["sources"][1]["text"] = "human donor 1 donor 2 ELISA"
        r = empty_response()
        r["annotations"] = [{"subject_type": "experiment", "subject": "exp_human", "category": "method", "value": "ELISA", "refs": [1]}]
        for donor in ["donor 1", "donor 2"]:
            r["observations"].append({"item": "i1", "experiment": "exp_human", "metric": "IC50", "value": "1.2",
                "unit": None, "s": 4, "refs": [1, 4], "condition": None, "donor": donor, "replicate": None})
        ledger = Ledger(data)
        ledger.apply(r, list(data["sources"]), "c1")
        result = ledger.assemble()
        self.assertEqual(len(result["observations"]), 2)
        self.assertEqual(result["experiments"][0]["annotations"][0]["refs"], [1])
        self.assertEqual(len({o["id"] for o in result["observations"]}), 2)
        r["annotations"][0]["subject"] = "unknown"
        r["observations"] = [{**r["observations"][0], "donor": "invented donor"}]
        ledger.apply(r, list(data["sources"]), "c2")
        self.assertEqual(len(ledger.rejections), 2)

    def test_condition_enrichment_does_not_duplicate_the_original_seed_cell(self):
        data = self.experiment_data()
        row = data["seed"]["observations"][0]
        data["seed"]["observations"] = [row, {**row, "condition": "pH 5.5", "experiment": "exp_human"}]
        result = Ledger(data).assemble()
        self.assertEqual(len(result["observations"]), 1)
        self.assertEqual(result["observations"][0]["condition"], "pH 5.5")


if __name__ == "__main__":
    unittest.main()
