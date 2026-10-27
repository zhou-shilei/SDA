"""Sequence-specific geometry: wrapped rows, corrected text and literal slices."""

import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from document.pdf2doc import _ground_table_cells
from dataagent.sda_inputs import _rows
from dataagent.sequence_evidence import SequenceLocator, source_digest, locate_sequence_provenance


SEQUENCE = "QMQLVESGGGVVQPGRSLRLSCAASGFTFRTYGMHWVRQAPGKGLEWVAVVWYDGSNKHYADSVKGRFTITRDNSKNTLNLQMNSLRAEDTAVYYCAR"


class SequenceLocationTests(unittest.TestCase):
    def table(self, mismatch=False):
        target = SEQUENCE[:40] + "_" + SEQUENCE[40:] + " (下划线标注的是可变区VH)"
        text = ("<table><tr><th>Name</th><th>SEQ ID</th><th>Sequence</th></tr>"
                f"<tr><td>Ab1</td><td>SEQ ID NO:1</td><td>{target}</td></tr></table>")
        ocr = SEQUENCE[:42] + ("Y" if mismatch else SEQUENCE[42]) + SEQUENCE[43:]
        entries = [("Name", [20, 10, 50, 20]), ("SEQ ID", [80, 10, 115, 20]), ("Sequence", [180, 10, 250, 20]),
                   (ocr[:35], [140, 40, 295, 50]), ("Ab1", [20, 60, 40, 70]),
                   ("SEQ ID NO:", [70, 55, 115, 65]), ("1", [85, 70, 95, 80]),
                   (ocr[35:70], [140, 60, 295, 70]), (ocr[70:], [140, 80, 270, 90])]
        evidence = {"table_id": "T", "text": text, "cells": []}
        _ground_table_cells(evidence, {"bbox": [0, 0, 310, 110]}, page_lines=[
            {"line_id": str(i), "text": t, "bbox": b} for i, (t, b) in enumerate(entries)])
        return {"source_id": "T", "page": 1, "rows": _rows(text, "T", evidence)}, evidence

    def test_wrapped_sequence_with_ocr_discrepancy_is_a_row_not_character_claim(self):
        table, evidence = self.table(mismatch=True)
        cell = table["rows"][1][2]
        self.assertEqual(cell["precision"], "ocr_row")
        self.assertEqual(cell["bbox_pdf"], [140, 40, 295, 90])
        self.assertFalse(cell["geometry_alignment"]["text_verified"])
        self.assertEqual(len(cell["ocr_lines"]), 3)
        self.assertIn("_", cell["text"])
        # The reviewed source is distinct from the immutable OCR text.
        reviewed = {**cell, "text": SEQUENCE, "kind": "cell", "table_id": "T", "page": 1,
                    "image_review": {"original_ref": "T:R1C2"}}
        fragment = {"source_ref": "reviewed:T:R1C2", "table_id": "T", "row_index": 1, "col_index": 2,
                    "char_start": 0, "char_end": len(SEQUENCE), "text": SEQUENCE, "quote": SEQUENCE,
                    "source_sha256": source_digest(reviewed)}
        locator = SequenceLocator([table], [], [], sources={fragment["source_ref"]: reviewed})
        self.addCleanup(locator.close)
        result = locator.locate({"vh": SEQUENCE, "sequence_resolution": {"vh": {"segments": [fragment]}}}, "vh")
        self.assertEqual(result["status"], "located")
        self.assertEqual(result["covered_residues"], len(SEQUENCE))
        self.assertEqual(result["segments"][0]["precision"], "ocr_row")
        self.assertEqual(result["segments"][0]["source_ref"], fragment["source_ref"])
        self.assertEqual(result["segments"][0]["text"], SEQUENCE)

    def test_literal_prose_slice_uses_its_own_source_box(self):
        prefix = "Ab1 VH sequence: "
        source = {"text": prefix + SEQUENCE, "page": 2, "bbox": [20, 30, 290, 50], "coordinate_space": "pdf_points"}
        segment = {"source_ref": "block:vh", "char_start": len(prefix), "char_end": len(prefix) + len(SEQUENCE),
                   "text": SEQUENCE, "source_sha256": source_digest(source)}
        locator = SequenceLocator([], [], [], sources={"block:vh": source})
        self.addCleanup(locator.close)
        result = locator.locate({"vh": SEQUENCE, "sequence_resolution": {"vh": {"segments": [segment]}}}, "vh")
        self.assertEqual(result["status"], "located")
        self.assertEqual(result["segments"][0]["page"], 2)
        self.assertEqual(result["segments"][0]["bbox"], [20, 30, 290, 50])
        self.assertEqual(result["segments"][0]["precision"], "ocr_line")
        changed = copy.deepcopy(segment)
        changed["source_sha256"] = "changed"
        result = locator.locate({"vh": SEQUENCE, "sequence_resolution": {"vh": {"segments": [changed]}}}, "vh")
        self.assertEqual(result["status"], "unlocated")
        self.assertEqual(result["reason"], "agent_fragment_source_changed")

    def test_exact_wrapped_lines_keep_the_variable_slice_and_residue_ranges(self):
        table = {"source_id": "T", "page": 1, "rows": [[{"text": SEQUENCE,
            "bbox_pdf": [20, 30, 290, 70], "ocr_lines": [
                {"text": SEQUENCE[:40], "bbox_pdf": [20, 30, 290, 40]},
                {"text": SEQUENCE[40:], "bbox_pdf": [20, 60, 290, 70]}]}]]}
        value = SEQUENCE[10:60]
        fragment = {"source_ref": "T:R0C0", "table_id": "T", "row_index": 0, "col_index": 0,
                    "char_start": 10, "char_end": 60, "text": value}
        locator = SequenceLocator([table], [], [])
        self.addCleanup(locator.close)
        result = locator.locate({"vh": value, "sequence_resolution": {"vh": {"segments": [fragment]}}}, "vh")
        self.assertEqual(result["status"], "located")
        self.assertEqual([(s["sequence_start"], s["sequence_end"]) for s in result["segments"]], [(0, 30), (30, 50)])
        self.assertEqual("".join(s["text"] for s in result["segments"]), value)
        self.assertTrue(all(s["precision"] == "ocr_line" for s in result["segments"]))

    def test_legacy_nested_citations_are_deduplicated_and_joined_without_searching_elsewhere(self):
        lines = [{"source_id": f"ocr:p1-l{i}", "text": text, "page": 1, "kind": "text", "bbox": box}
                 for i, (text, box) in enumerate([(SEQUENCE[:40], [20, 30, 290, 40]),
                                                  (SEQUENCE[40:], [20, 60, 290, 70])])]
        result = locate_sequence_provenance(SEQUENCE, [{"extraction_evidence": lines}, *lines], chain="vh")
        self.assertEqual(result["status"], "located")
        self.assertEqual(len(result["segments"]), 2)
        self.assertEqual("".join(s["text"] for s in result["segments"]), SEQUENCE)
        self.assertEqual(locate_sequence_provenance("Y" + SEQUENCE, lines, chain="vh")["status"], "unlocated")


if __name__ == "__main__":
    unittest.main()
