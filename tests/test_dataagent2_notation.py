"""Sequence notation normalization must remain reversible to its source evidence."""

import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dataagent2.augment_domains import slice_fragments
from dataagent2.ledger import Ledger, ground_sequence
from dataagent2.quality import audit_dataset
from dataagent2.sequence_notation import decode, validate_fragment
from dataagent2.test_dataagent2 import fixture, response


class SequenceNotationTests(unittest.TestCase):
    def test_standard_twenty_codes_and_existing_one_letter_notation(self):
        text = "Ala Arg Asn Asp Cys Gln Glu Gly His Ile Leu Lys Met Phe Pro Ser Thr Trp Tyr Val"
        value, encoding, spans = decode(text)
        self.assertEqual(value, "ARNDCQEGHILKMFPSTWYV")
        self.assertEqual(encoding, "three_letter")
        self.assertEqual([text[a:b] for a, b in spans], text.split())
        self.assertEqual(decode(" E V Q L<br> ALA GLY ")[0], "EVQLALAGLY")
        self.assertEqual(decode("glu val gln leu")[0], "EVQL")

    def test_original_anchors_offsets_and_cross_line_order_are_preserved(self):
        sources = {1: {"text": "SEQ ID 1: Glu Val-Gln<br>Leu (VH)"},
                   2: {"text": "Ser Gly"}}
        value, fragments = ground_sequence([
            {"s": 1, "start": "Glu", "end": "Leu"},
            {"s": 2, "start": "", "end": ""}], sources)
        self.assertEqual(value, "EVQLSG")
        self.assertEqual(fragments[0]["quote"], "Glu Val-Gln<br>Leu")
        self.assertEqual(fragments[0]["start"], len("SEQ ID 1: "))
        for f in fragments:
            source = sources[f["s"]]["text"]
            self.assertEqual(source[f["start"]:f["end"]], f["quote"])
            self.assertEqual(len(f["notation"]["residue_spans"]), len(f["sequence"]))
            self.assertEqual(validate_fragment(f, sources[f["s"]])[0], f["sequence"])

    def test_unknown_modified_mixed_and_ocr_corrupted_tokens_are_rejected(self):
        for text in ("Glu Xaa Gln", "Asx Gly", "Glu Glx", "Sec Ala", "Pyl Ala",
                     "β-Ala Leu", "D-Ala Leu", "Glu G1n Leu", "Glu EVQL",
                     "Glu Val 5 Gln", "Glu Val SEQ ID 1", "GluValGln",
                     "Gl Val", "evql", "Glu--Val", "-Glu Val", "Glu Val-"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                decode(text)

    def test_normalized_sequence_passes_ledger_and_independent_source_audit(self):
        data = fixture()
        data["sources"][3]["text"] = "Glu Val Gln Leu " + "Ala " * 100
        ledger = Ledger(data)
        ledger.apply(response(), [1, 2, 3], "c1")
        result = ledger.assemble()
        self.assertEqual(result["summary"]["variable_chains"], 2)
        self.assertEqual(result["entities"][0]["sequences"]["VH"]["value"], "EVQL" + "A" * 100)
        self.assertEqual(result["summary"]["complete_pairs"], 0)
        self.assertEqual(audit_dataset(result, data["sources"])["literal_errors"], [])
        corrupted = copy.deepcopy(result)
        f = corrupted["entities"][0]["sequences"]["VH"]["fragments"][0]
        f["notation"]["residue_spans"][0][1] += 1
        self.assertTrue(audit_dataset(corrupted, data["sources"])["literal_errors"])
        data["sources"][3]["text"] = data["sources"][3]["text"].replace("Glu", "Gly", 1)
        self.assertTrue(audit_dataset(result, data["sources"])["literal_errors"])

    def test_conversion_does_not_bypass_failed_ocr_or_invent_variable_boundaries(self):
        data = fixture()
        data["sources"][3]["text"] = "Glu Val Gln Leu " + "Ala " * 100
        r = response()
        for link in r["links"]:
            link["region"] = "H_CHAIN"
        r["sequences"][0]["region"] = "H_CHAIN"
        ledger = Ledger(data)
        ledger.apply(r, [1, 2, 3], "c1")
        result = ledger.assemble()
        self.assertEqual(result["summary"]["variable_chains"], 0)
        self.assertEqual(result["entities"][0]["sequences"]["H_CHAIN"]["value"], "EVQL" + "A" * 100)
        data["sources"][3]["sequence_ocr"] = {"status": "failed"}
        with self.assertRaisesRegex(ValueError, "re-recognition failed"):
            ground_sequence([{"s": 3, "start": "", "end": ""}], data["sources"])

    def test_domain_slicing_tracks_residues_in_three_letter_source(self):
        sources = {1: {"text": "Glu ALA GLY"}, 2: {"text": "Val-Gln Leu"}}
        _, fragments = ground_sequence([{"s": s, "start": "", "end": ""} for s in sources], sources)
        selected = slice_fragments(fragments, sources, 1, 5)
        self.assertEqual("".join(f["sequence"] for f in selected), "AGVQ")
        self.assertEqual(selected[0]["quote"], "ALA GLY")
        for f in selected:
            self.assertEqual(validate_fragment(f, sources[f["s"]])[0], f["sequence"])


if __name__ == "__main__":
    unittest.main()
