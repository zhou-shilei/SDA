"""Guard the counting and evidence rules used in the family comparison."""

import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import patent_family_overlap as overlap


class FamilyOverlapTests(unittest.TestCase):
    def test_us_publications_keep_application_and_grant_distinct(self):
        self.assertEqual(overlap.normalize_us_number("US 07,988,970 B2"), "7988970")
        self.assertEqual(overlap.normalize_us_number("US20100221263A1"), "20100221263")
        self.assertIsNone(overlap.normalize_us_number("WO20100221263A1"))
        self.assertIsNone(overlap.us_doc_number({"country": "EP", "doc-number": "7988970"}))
        self.assertEqual(overlap.us_doc_number({"doc-number": "US7988970"}), "7988970")

    def test_only_family_heading_for_requested_document_is_evidence(self):
        evidence = '''Patent
Source: find({"ref_id":"https://patents.google.com/patent/US7988970B2/en","pattern":"Family Applications"});
L10: SEQ ID=999
L11: Cited patent US1234567A
L12: ## ID=4321
'''
        rows = overlap.parse_google_evidence(evidence)
        self.assertEqual([(x["poisk_patent_id"], x["family_id"]) for x in rows], [("7988970", "4321")])
        self.assertEqual(overlap.parse_google_evidence(evidence.replace("## ID=4321", "SEQ ID=4321")), [])
        self.assertEqual(overlap.parse_google_evidence(evidence + "L13: ## ID=5678\n"), [])

    @staticmethod
    def family(fid, us):
        return {"family_id": fid, "representative": "US" + us[0] if us else "",
                "title": "Example", "docs_count": len(us), "us_numbers": us,
                "earliest_listed_publication": 20000101,
                "latest_listed_publication": 20250101,
                "listed_countries": ["US"] if us else []}

    def run_comparison(self, families, member_index, poisk, google):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            with patch.object(overlap, "load_pad", return_value=(families, member_index)), \
                 patch.object(overlap, "load_poisk", return_value=poisk), \
                 patch.object(overlap, "load_google", return_value=google), \
                 contextlib.redirect_stdout(io.StringIO()):
                overlap.analyze(run)
            return json.loads((run / "summary.json").read_text())

    def test_duplicate_grants_collapse_and_unknowns_remain_unknown(self):
        families = {"1": self.family("1", ["100", "101"]),
                    "2": self.family("2", []), "3": self.family("3", [])}
        summary = self.run_comparison(families, {"100": {"1"}, "101": {"1"}},
                                      {"100": ["r1"], "101": ["r2"], "102": ["r3"]}, {})
        self.assertEqual(summary["confirmed_overlap_families"], 1)
        self.assertEqual(summary["poisk_resolved_families"], 1)
        self.assertEqual(summary["unresolved_poisk_patents"], 1)
        self.assertEqual(summary["overlap_families_upper_bound"], 2)
        self.assertEqual(summary["pad_families_without_poisk_bounds"], [1, 2])

    def test_google_adds_overlap_and_outside_families_without_guessing(self):
        families = {"1": self.family("1", []), "2": self.family("2", [])}
        def evidence(fid):
            return [{"family_id": fid, "source_url": "https://patents.google.com/",
                     "evidence_file": "evidence.txt"}]
        summary = self.run_comparison(families, {}, {"100": ["r1"], "101": ["r2"]},
                                      {"100": evidence("1"), "101": evidence("9")})
        self.assertEqual(summary["confirmed_overlap_families"], 1)
        self.assertEqual(summary["poisk_families_outside_pad_snapshot"], 1)
        self.assertEqual(summary["poisk_resolved_families"], 2)
        self.assertEqual(summary["unresolved_poisk_patents"], 0)

    def test_ambiguous_and_disagreeing_ids_are_not_silently_counted(self):
        families = {"1": self.family("1", ["100"]), "2": self.family("2", ["100"])}
        google = {"101": [{"family_id": "9", "source_url": "url", "evidence_file": "file"}]}
        summary = self.run_comparison(families, {"100": {"1", "2"}, "101": {"1"}},
                                      {"100": ["r1"], "101": ["r2"]}, google)
        self.assertEqual(summary["confirmed_overlap_families"], 0)
        self.assertEqual(summary["unresolved_poisk_patents"], 2)
        self.assertEqual(summary["poisk_mapping_status"]["family_id_disagreement"], 1)


if __name__ == "__main__":
    unittest.main()
