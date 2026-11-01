from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from poisk_binding_audit import classify, explicit_audit_statement
from poisk_affinity_audit import classify as numeric_classify, VALUE, METHOD


class BindingEvidenceTests(unittest.TestCase):
    def row(self, value="", method="", **changes):
        row = {"unique_record_id": "test", "antibody_identifier": "Ab1",
               "target.protein_name": "EGFR", "target.gene_names": "EGFR",
               "audit.verdict": "CONFIRMED", "audit.flags": "", "audit.reasoning": "",
               VALUE: value, METHOD: method}
        row.update(changes)
        return row

    def result(self, row):
        return classify(row, numeric_classify(row))

    def test_direct_assay_does_not_require_kd_type_or_scalar(self):
        row = self.row("1-2 nM", "SPR")
        self.assertEqual(self.result(row)["binding_evidence_category"], "positive_direct")
        row = self.row("0.3 nM", "Binding ELISA")
        self.assertTrue(self.result(row)["binding_supported_by_release"])

    def test_functional_ic50_alone_is_not_direct_binding(self):
        row = self.row("IC50 = 2 nM", "Neutralization Assay")
        self.assertEqual(self.result(row)["binding_evidence_category"], "functional_or_competition_only")

    def test_empty_affinity_can_have_targeted_binding_statement(self):
        row = self.row(**{"audit.reasoning": "The antibody Ab1 binds to human EGFR (Table 2)."})
        self.assertEqual(self.result(row)["binding_evidence_category"], "positive_statement")
        row["audit.reasoning"] = "The target is EGFR. All fields are supported."
        self.assertFalse(self.result(row)["binding_supported_by_release"])

    def test_negated_claim_other_target_and_parent_are_not_positive(self):
        for sentence in ["The antibody Ab1 does not bind to EGFR.",
                         "The antibody Ab1 fails to bind to EGFR.",
                         "The antibody Ab1 doesn't bind to EGFR.",
                         "The antibody Ab1 may bind to EGFR.",
                         "The antibody Ab1 binds to CD3.",
                         "The parental antibody binds to EGFR."]:
            row = self.row(**{"audit.reasoning": sentence})
            self.assertEqual(explicit_audit_statement(row), "")
        row = self.row(**{"target.protein_name": "IL-2", "target.gene_names": "IL2",
                          "audit.reasoning": "The antibody Ab1 binds to IL-23."})
        self.assertEqual(explicit_audit_statement(row), "")

    def test_nd_off_rate_and_no_low_are_distinct(self):
        self.assertEqual(self.result(self.row("No detectable binding", "ELISA"))["binding_evidence_category"], "reported_no_binding")
        self.assertEqual(self.result(self.row("Not Detected", "FACS"))["binding_evidence_category"], "uncertain")
        self.assertEqual(self.result(self.row("No/Low binding detected", "ELISA"))["binding_evidence_category"], "uncertain")
        self.assertTrue(self.result(self.row("High (undetectable off-rate)", "SPR"))["binding_supported_by_release"])

    def test_audit_quality_and_material_flags_are_separate(self):
        row = self.row("2 nM", "SPR", **{"audit.flags": "discovery_methodology.strategies"})
        self.assertTrue(self.result(row)["binding_supported_by_release"])
        row["audit.flags"] = "target.binding_affinity.affinity_value"
        self.assertEqual(self.result(row)["binding_evidence_category"], "positive_clue_needs_review")
        row["audit.flags"] = ""
        row["audit.verdict"] = "REVIEW"
        self.assertFalse(self.result(row)["binding_supported_by_release"])
        row["audit.verdict"] = "CONFIRMED"
        row["target.protein_name"] = "Target not reported"
        self.assertFalse(self.result(row)["binding_supported_by_release"])

    def test_reviewed_nd_with_observed_binding_is_not_negative(self):
        row = self.row("Not Detected", "FACS", **{"unique_record_id": "073696"})
        self.assertEqual(self.result(row)["binding_evidence_category"], "positive_statement")

    def test_reviewed_nd_with_explicit_no_binding_is_negative(self):
        for rid in ["000010", "050268"]:
            row = self.row("Not Detected", "ELISA", **{"unique_record_id": rid})
            self.assertEqual(self.result(row)["binding_evidence_category"], "reported_no_binding")
        row = self.row("Not Detected", "ELISA", **{"unique_record_id": "050266", "audit.verdict": "PLAUSIBLE"})
        self.assertEqual(self.result(row)["binding_evidence_category"], "negative_clue_needs_review")


if __name__ == "__main__":
    unittest.main()
