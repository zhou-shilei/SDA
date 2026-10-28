"""Checks that evaluation preserves errors and includes short sequence tails."""
import unittest

from ppvl_crop_experiment import REFERENCES, AA, extract_named_row, levenshtein, normalize_transcription


class EvaluationChecks(unittest.TestCase):
    def test_no_silent_ocr_correction(self):
        self.assertEqual(normalize_transcription("ACDO\n Ql"), "ACDOQl")

    def test_only_formatting_removed(self):
        self.assertEqual(normalize_transcription("<table><tr><td>ACD\\nEF</td></tr></table>"), "ACDEF")

    def test_printed_underline_markup_keeps_every_character(self):
        self.assertEqual(normalize_transcription(r"ACD $ \underline{\text{QO}} $ EF"), "ACDQOEF")

    def test_short_tail_and_split_columns_retained(self):
        table = "<table><tr><td>AB-1</td><td>ACDEFGHIKLMNPQRSTVWY</td><td>WGQGTMVTVSS (SEQ ID NO:4)</td></tr></table>"
        self.assertEqual(extract_named_row(table, "AB-1"), "ACDEFGHIKLMNPQRSTVWYWGQGTMVTVSS")

    def test_names_do_not_match_by_substring(self):
        table = "<table><tr><td>AB-10</td><td>ACDEFGHIK</td></tr></table>"
        self.assertEqual(extract_named_row(table, "AB-1"), "")

    def test_distance_reports_insert_delete_replace(self):
        self.assertEqual(levenshtein("ACDE", "ACOE"), 1)
        self.assertEqual(levenshtein("ACDE", "ACD"), 1)
        self.assertEqual(levenshtein("ACDE", "AACDE"), 1)

    def test_frozen_reference_shape(self):
        self.assertEqual(set(REFERENCES), {"AB-1", "AB-2", "AB-5"})
        self.assertTrue(all(len(v) == 122 and set(v) <= AA for v in REFERENCES.values()))


if __name__ == "__main__":
    unittest.main()
