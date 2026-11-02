"""Check database column source fields without invoking extraction or models."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.request import urlopen

from studio.sda_data import Workspace
from studio.server import StudioServer


class DatabaseProjectionTests(unittest.TestCase):
    def test_experiment_metadata_is_projected_without_inventing_groups(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "results"
            output.mkdir()
            slot = lambda value: {"status": "present", "value": value, "provenance": []}
            experiment = {"exp_id": "e1", "NAME": "Binding", "METHOD": "SPR", "SYSTEM": "protein",
                "SPECIES": "human", "PROTOCOL_IDS": ["p1"], "ANTIGEN_CONSTRUCT": "ECD"}
            observation = {"observation_id": "m1", "exp_id": "e1", "metric": slot("KD"),
                "value": slot("1"), "unit": slot("nM"), "source": {}}
            template = {"experiments": [experiment], "entities": [{"entity_id": "a1", "name": "Ab1",
                "H_CHAIN": {"sequence": slot("ACDEFG"), "seq_id": slot(38)},
                "L_CHAIN": {"sequence": slot("GHIKLM"), "seq_id": slot(39)},
                "METRIC": [observation], "sequence_locations": {"vh": {}, "vl": {}}}]}
            (output / "dataset.json").write_text("{}")
            path = output / "template.completed.json"
            path.write_text(json.dumps(template))
            workspace = Workspace(root)
            row = workspace._build("DOC", output, root)["records"][0]
            self.assertEqual(row["assay_method"], "SPR")
            self.assertEqual(row["h_chain"], "ACDEFG")
            self.assertEqual(row["l_chain_seq_id"], 39)
            self.assertEqual([s["region"] for s in row["full_chain_sequences"]], ["H_CHAIN", "L_CHAIN"])
            self.assertIsNone(row["vh"])
            self.assertIsNone(row["vl"])
            self.assertEqual(row["assay_system"], "protein")
            self.assertEqual(row["antigen_species"], "human")
            self.assertEqual(row["antigen_construct"], "ECD")
            self.assertIsNone(row["batch_id"])
            self.assertIsNone(row["comparison_group_id"])
            experiment.update(BATCH_ID="run-1", COMPARISON_GROUP_ID="G1", COMPARISON_STATUS="tentative")
            path.write_text(json.dumps(template))
            row = workspace._build("DOC", output, root)["records"][0]
            self.assertEqual(row["batch_id"], "run-1")
            self.assertEqual(row["comparison_group_id"], "G1")
            self.assertEqual(row["comparison_status"], "tentative")

    def test_new_browser_module_is_served(self):
        with tempfile.TemporaryDirectory() as directory:
            server = StudioServer(("127.0.0.1", 0), Workspace(Path(directory)))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with urlopen(f"http://127.0.0.1:{server.server_port}/studio/database_columns.js") as response:
                    self.assertIn(b"export const DATABASE_COLUMNS", response.read())
            finally:
                server.shutdown()
                server.server_close()
                thread.join()


if __name__ == "__main__":
    unittest.main()
