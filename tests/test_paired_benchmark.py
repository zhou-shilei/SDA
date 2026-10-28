"""Guard against inflated recovery from crossed pairs and label corruption."""

import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from paired_benchmark import compare, identity


class PairedBenchmarkTest(unittest.TestCase):
    def setUp(self):
        self.h, self.l = "EVQ" + "A" * 100, "DIQ" + "C" * 100
        self.gold = [{"Antibody_Name": "ACT017", "vh_sequence_aa": self.h,
                      "vl_sequence_aa": self.l}]

    def score(self, rows, gold=None):
        return compare("WO2017021539A2", self.gold if gold is None else gold, rows)

    def test_cross_record_chain_pooling_cannot_form_a_correct_pair(self):
        result = self.score([
            {"name": "ACT017", "vh": self.h, "vl": "T" * 103},
            {"name": "ACT017", "vh": "G" * 103, "vl": self.l},
        ])
        self.assertTrue(result["details"][0]["vh_exact_exists"])
        self.assertTrue(result["details"][0]["vl_exact_exists"])
        self.assertEqual(result["paired_exact_exists"], 0)

    def test_conflicting_alternative_prevents_unambiguous_success(self):
        result = self.score([
            {"name": "ACT017", "vh": self.h, "vl": self.l},
            {"name": "ACT017", "vh": self.h, "vl": "T" * 103},
        ])
        self.assertEqual(result["paired_exact_exists"], 1)
        self.assertEqual(result["paired_exact_unambiguous"], 0)

    def test_duplicate_same_pair_is_not_ambiguity(self):
        row = {"name": "act-017", "vh": self.h, "vl": self.l}
        self.assertEqual(self.score([row, row])["paired_exact_unambiguous"], 1)

    def test_pair_without_correct_identity_is_diagnostic_only(self):
        result = self.score([{"name": "another clone", "vh": self.h, "vl": self.l}])
        self.assertEqual(result["paired_exact_unambiguous"], 0)
        self.assertEqual(result["sequence_pair_recovered_ignoring_identity"], 1)

    def test_terminal_residue_difference_is_reported_without_fixing(self):
        result = self.score([{"name": "ACT017", "vh": self.h, "vl": self.l[:-1]}])
        self.assertEqual(result["paired_exact_unambiguous"], 0)
        self.assertTrue(result["details"][0]["candidate_records"][0]["vl_boundary_containment"])

    def test_noncanonical_gold_is_quarantined(self):
        gold = [{**self.gold[0], "vl_sequence_aa": "Н" + self.l[1:]}]
        result = self.score([{"name": "ACT017", "vh": self.h, "vl": self.l}], gold)
        self.assertEqual(result["eligible_gold_pairs"], 0)
        self.assertIsNone(result["paired_exact_unambiguous_recovery"])

    def test_humanized_constructs_and_mutants_stay_distinct(self):
        doc = "WO2018068182A1"
        self.assertEqual(identity(doc, "6F3 humanized (Variable)"), identity(doc, "6F3 Hu"))
        self.assertNotEqual(identity(doc, "6F3 murine"), identity(doc, "6F3 Hu"))
        self.assertNotEqual(identity(doc, "ACT017"), identity(doc, "ACT017K"))


if __name__ == "__main__":
    unittest.main()
