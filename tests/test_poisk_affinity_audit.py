import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from poisk_affinity_audit import classify, quantities, VALUE, METHOD


class AffinityAuditTests(unittest.TestCase):
    def test_spr_and_concentration_do_not_imply_kd(self):
        x = classify({VALUE: "0.2 nM", METHOD: "SPR (Biacore)"})
        self.assertEqual(x["explicit_metrics"], "")
        self.assertEqual(x["single_molar_point_candidate_M"], "")

    def test_explicit_unicode_ic50_and_method_labels(self):
        x = classify({VALUE: "IC₅₀ = 2 μM"})
        self.assertEqual(x["explicit_metrics"], "IC50")
        self.assertEqual(x["single_molar_point_candidate_M"], "0.000002")
        x = classify({VALUE: "2 nM", METHOD: "Flow Cytometry (IC50)"})
        self.assertEqual(x["explicit_metrics"], "IC50")

    def test_kinetic_kd_is_separate_and_does_not_erase_real_kd(self):
        x = classify({VALUE: "ka: 4.9E5 1/Ms, kd: 9.8E-5 1/s"})
        self.assertNotIn("KD", x["explicit_metrics"])
        self.assertIn("kinetic_kd_inverse_time", x["screening_flags"])
        x = classify({VALUE: "8.55E-04 /s (kd)"})
        self.assertNotIn("KD", x["explicit_metrics"])
        x = classify({VALUE: "KD1 = 218 nM, KD2 = 0.4 nM"})
        self.assertEqual(x["explicit_metrics"], "KD")
        self.assertEqual(x["quantity_count"], 2)
        x = classify({VALUE: "Kd1 ~ 3.2 x 10^-7 1/s (dissociation rate)"})
        self.assertNotIn("KD", x["explicit_metrics"])
        x = classify({VALUE: "KD = 2 nM; kd = 1e-4 s^-1"})
        self.assertEqual(x["explicit_metrics"], "KD")
        self.assertIn("kinetic_kd_inverse_time", x["screening_flags"])

    def test_bounds_ranges_relative_and_mixed_are_not_point_labels(self):
        for text in ["Kd of 1x10^-8 M or less", "IC50: 0.5-1.8 uM",
                     "IC50 more than 5 nM", "IC50 2-fold better than 1 nM",
                     "KD = 2 nM; IC50 = 5 nM", "KD = -1 nM",
                     "KD = 1.1 +/- 0.2 nM", "KD = 1.1, 2.2 nM"]:
            with self.subTest(text=text):
                self.assertEqual(classify({VALUE: text})["single_molar_point_candidate_M"], "")
        x = classify({VALUE: "KD = 2 nM; IC50 = 5 nM"})
        self.assertEqual(set(x["explicit_metrics"].split("|")), {"KD", "IC50"})

    def test_scientific_and_mass_units_are_not_confused(self):
        x = classify({VALUE: "KD = 1.616 × 10^(-9) M"})
        self.assertEqual(x["single_molar_point_candidate_M"], "1.616E-9")
        x = classify({VALUE: "IC50: 0.06 ug/mL"})
        self.assertEqual(x["single_molar_point_candidate_M"], "")
        self.assertIn("mass_concentration_not_molar", x["screening_flags"])
        self.assertEqual(quantities("KA = 4e8 M^-1"), [])

    def test_audit_mentions_do_not_become_explicit_labels(self):
        x = classify({VALUE: "3 nM", "audit.reasoning": "The reported KD is disputed."})
        self.assertEqual(x["explicit_metrics"], "")
        self.assertEqual(x["audit_only_single_metric_hint"], "KD")
        self.assertIn("not_confirmed_metric", x["screening_flags"])
        x = classify({VALUE: "", METHOD: "KD measurement"})
        self.assertEqual(x["explicit_metrics"], "")


if __name__ == "__main__":
    unittest.main()
