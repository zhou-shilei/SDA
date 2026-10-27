import copy
from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PIL import Image, ImageDraw

from document.pdf2doc import structure_table_evidence, _ground_table_cells, parse_grid
from document.sequence_ocr import refine, line_crop, addressed_row_lines, sequence_line, sequence_candidates, ruled_cell_boxes
from dataagent.question_workflow import collect_diagnostics
from dataagent.llm_sequence_agent import ground_decision


class HybridSequenceOCRTests(unittest.TestCase):
    def test_visible_rules_locate_unlabeled_continuation_and_ignore_sequence_underlines(self):
        image = Image.new("RGB", (600, 300), "white")
        draw = ImageDraw.Draw(image)
        for x in (10, 80, 150, 590):
            draw.line((x, 10, x, 290), fill="black", width=2)
        for y in (10, 110, 290):
            draw.line((10, y, 590, y), fill="black", width=2)
        draw.line((155, 50, 580, 50), fill="black", width=2)
        grid = parse_grid('<table><tr><td></td><td></td><td>ACDEFGHIKLMNPQ</td></tr><tr><td>AB</td><td>SEQ ID NO:1</td><td>RSTVWYACDEFGHI</td></tr></table>', "T")
        boxes = ruled_cell_boxes(image, grid)
        self.assertEqual(len(boxes), 6)
        self.assertLess(boxes["T:R0C2"][1], 50)
        self.assertGreater(boxes["T:R0C2"][3], 50)
        draw.line((10, 190, 590, 190), fill="black", width=2)
        self.assertEqual(ruled_cell_boxes(image, grid), {})

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.image = self.root / "table.png"
        self.value = "QMQLVESGGGVVQPGRSLRLSCAAS"
        image = Image.new("RGB", (500, 80), "white")
        draw = ImageDraw.Draw(image)
        draw.text((8, 20), "AB-1", fill="black")
        draw.text((100, 20), self.value, fill="black")
        image.save(self.image)
        self.metadata = {"id": "T", "document_id": "DOC", "page": 1,
                         "bbox": [0, 0, 500, 80], "bbox_padded": [0, 0, 500, 80],
                         "width_px": 500, "height_px": 80}
        self.html = '<table><tr><td>AB-1</td><td>QMQLVESGGGVVQPRSLRLSCAAS</td><td>1.2</td></tr></table>'

    def provider(self, output=None):
        local = Mock()
        local.predict.return_value = [{"rec_texts": ["AB-1", self.value, "1.2"],
                                       "rec_boxes": [[8, 18, 40, 36], [100, 18, 430, 36], [460, 18, 490, 36]]}]
        rec = Mock()
        rec.predict.return_value = [{"rec_text": self.value if output is None else output, "rec_score": .99}]
        ori = Mock()
        ori.predict.return_value = [{"label_names": ["0"], "scores": [1.0]}]
        return Mock(return_value=(local, rec, ori))

    def test_refill_changes_only_sequence_and_keeps_literal_audit(self):
        evidence = structure_table_evidence(self.html, self.image, self.metadata)
        original = copy.deepcopy(evidence)
        result = refine(evidence, model_provider=self.provider())
        self.assertEqual(evidence, original)
        self.assertEqual(result["vl_text"], self.html)
        self.assertEqual(result["sequence_ocr"]["successful_cells"], 1)
        cells = {c["ref"]: c for c in result["cells"]}
        self.assertEqual(cells["T:R0C1"]["text"], self.value)
        self.assertEqual(cells["T:R0C0"]["text"], "AB-1")
        self.assertEqual(cells["T:R0C2"]["text"], "1.2")
        self.assertEqual(hashlib.sha256(result["text"].encode()).hexdigest(), result["text_sha256"])
        line = cells["T:R0C1"]["sequence_ocr"]["lines"][0]
        self.assertEqual(hashlib.sha256(Path(line["image_path"]).read_bytes()).hexdigest(), line["image_sha256"])

    def test_failure_logs_identify_cell_and_match_saved_audit(self):
        output = io.StringIO()
        with redirect_stdout(output):
            result = refine(structure_table_evidence(self.html, self.image, self.metadata),
                            model_provider=self.provider("QMQLVESGGGVVOPGRSLRLSCAAS"))
        events = [json.loads(line.removeprefix("table-ocr ")) for line in output.getvalue().splitlines()
                  if line.startswith("table-ocr ")]
        cell = next(e for e in events if e["phase"] == "sequence_cell_result")
        self.assertEqual((cell["table_id"], cell["page"], cell["ref"]), ("T", 1, "T:R0C1"))
        self.assertEqual(cell["status"], "failed")
        self.assertEqual(cell["error"], result["sequence_ocr"]["cells"][0]["error"])
        self.assertEqual(events[-1]["phase"], "sequence_complete")
        self.assertEqual(events[-1]["failed_cells"], 1)
        self.assertTrue(all(e["timestamp"] and e["pid"] for e in events))

    def test_grounding_and_reparse_preserve_refinement_status(self):
        result = refine(structure_table_evidence(self.html, self.image, self.metadata), model_provider=self.provider())
        _ground_table_cells(result, self.metadata)
        cell = next(c for c in result["cells"] if c["ref"] == "T:R0C1")
        self.assertEqual(cell["sequence_ocr"]["status"], "success")
        self.assertEqual(cell["vl_text"], "QMQLVESGGGVVQPRSLRLSCAAS")
        self.assertEqual(cell["text"], self.value)

    def test_noncanonical_output_stays_failed_and_cannot_supply_final_residues(self):
        evidence = structure_table_evidence(self.html, self.image, self.metadata)
        result = refine(evidence, model_provider=self.provider("QMQLVESGGGVVOPGRSLRLSCAAS"))
        cell = next(c for c in result["cells"] if c["ref"] == "T:R0C1")
        self.assertEqual(result["text"], self.html)
        self.assertEqual(cell["sequence_ocr"]["status"], "failed")
        decision = {"entity_id": "ab", "chain": "VH", "status": "resolved", "value": cell["text"],
                    "seq_id": None, "source_scope": "variable", "fragments": [
                        {"source_ref": "s", "quote": cell["text"], "occurrence": 0}],
                    "evidence_refs": ["s"], "rationale": "source", "source_conflicts": []}
        with self.assertRaisesRegex(ValueError, "re-recognition failed"):
            ground_decision(decision, {"s": {**cell, "kind": "cell"}})

    def test_one_short_line_cannot_replace_a_long_addressed_sequence(self):
        evidence = structure_table_evidence(self.html, self.image, self.metadata)
        result = refine(evidence, model_provider=self.provider(self.value[:10]))
        cell = next(c for c in result["cells"] if c["ref"] == "T:R0C1")
        self.assertEqual(cell["sequence_ocr"]["status"], "failed")
        self.assertIn("incomplete", cell["sequence_ocr"]["error"])
        self.assertEqual(result["text"], self.html)

    def test_nonsequence_tables_do_not_load_another_model(self):
        text = '<table><tr><td>AB-1</td><td>IC50</td><td>1.2 nM</td></tr></table>'
        loader = Mock(side_effect=AssertionError("must not load"))
        result = refine(structure_table_evidence(text, self.image, self.metadata), model_provider=loader)
        self.assertEqual(result["text"], text)
        self.assertEqual(result["sequence_ocr"]["status"], "not_applicable")
        loader.assert_not_called()

    def test_small_isolated_glyph_tail_survives_crop(self):
        image = Image.new("RGB", (200, 70), "white")
        draw = ImageDraw.Draw(image)
        draw.rectangle((20, 10, 170, 28), fill="black")
        draw.line((80, 28, 86, 35), fill="black", width=2)
        crop, box = line_crop(image, [20, 10, 170, 29])
        self.assertGreaterEqual(box[3], 36)
        self.assertEqual(box[0], 20)
        self.assertGreater(crop.height, 26)

    def test_row_anchor_never_borrows_the_next_antibody(self):
        grid = parse_grid('<table><tr><td>AB-1</td><td>ACDEFGHIKLMNPQ</td></tr><tr><td>AB-2</td><td>RSTVWYACDEFGHI</td></tr></table>', "T")
        lines = [{"text": "AB-1", "bbox": [0, 0, 40, 20]},
                 {"text": "ACDEFGHIKLMNPQ", "bbox": [80, 0, 250, 20]},
                 {"text": "AB-2", "bbox": [0, 60, 40, 80]},
                 {"text": "RSTVWYACDEFGHI", "bbox": [80, 60, 250, 80]}]
        selected = addressed_row_lines(grid["physical"][0][1], grid, lines)
        self.assertEqual([l["text"] for l in selected], ["ACDEFGHIKLMNPQ"])
        lines.append({"text": "AB-1", "bbox": [0, 120, 40, 140]})
        self.assertEqual(addressed_row_lines(grid["physical"][0][1], grid, lines), [])

    def test_terminal_short_residue_line_is_not_dropped(self):
        self.assertTrue(sequence_line("SS"))
        self.assertTrue(sequence_line("Y"))
        self.assertFalse(sequence_line("SEQ ID NO:4"))
        self.assertTrue(sequence_line("SEQ ID NO: 67 ACDEFGHIKLMNPQ"))

    def multiline(self, joined=False):
        parts = [self.value, "ACDEFGHIKLMNPQRSTVWYACDE", "RSTVWYACDEFGHIKLMNPQRST"]
        neighbor = "WYACDEFGHIKLMNPQRSTVWYAC"
        picture = Image.new("RGB", (520, 240), "white")
        draw = ImageDraw.Draw(picture)
        draw.line((195, 15, 195, 230), fill="black", width=2)
        draw.text((8, 62), "CSJ117 VL", fill="black")
        draw.text((100, 62), "SEQ ID NO: 65", fill="black")
        for i, part in enumerate(parts):
            draw.text((202, 40 + i * 25), part, fill="black")
        draw.text((8, 182), "OTHER VH", fill="black")
        draw.text((100, 182), "SEQ ID NO: 68", fill="black")
        draw.text((202, 182), neighbor, fill="black")
        picture.save(self.image)
        metadata = {**self.metadata, "bbox": [0, 0, 520, 240], "bbox_padded": [0, 0, 520, 240],
                    "width_px": 520, "height_px": 240}
        text = ('<table><tr><td>CSJ117 VL</td><td>SEQ ID NO: 65</td><td>' + ''.join(parts) +
                '</td></tr><tr><td>OTHER VH</td><td>SEQ ID NO: 68</td><td>' + neighbor + '</td></tr></table>')
        lines = [("CSJ117 VL", [8, 62, 80, 80]), ("SEQ ID NO: 65", [100, 62, 181, 80]),
                 (parts[0], [202, 38, 490, 56]), (parts[1], [175, 63, 490, 81]),
                 (parts[2], [202, 88, 490, 106]), ("OTHER VH", [8, 182, 80, 200]),
                 ("SEQ ID NO: 68", [100, 182, 181, 200]), (neighbor, [202, 182, 490, 200])]
        if joined:
            lines.pop(1)
            lines[2] = ("SEQ ID NO: 65 " + parts[1], [100, 63, 490, 81])
        loader = self.provider()
        local, rec, _ = loader.return_value
        local.predict.return_value = [{"rec_texts": [t for t, _ in lines], "rec_boxes": [b for _, b in lines]}]
        rec.predict.side_effect = [[{"rec_text": p, "rec_score": .99}] for p in [*parts, neighbor]]
        result = refine(structure_table_evidence(text, self.image, metadata), model_provider=loader)
        first = result["sequence_ocr"]["cells"][0]
        self.assertEqual(first["status"], "success", first)
        self.assertEqual(first["sequence"], ''.join(parts))
        self.assertEqual(len(first["lines"]), 3)
        self.assertTrue(all(line["bbox_crop"][0] > 196 for line in first["lines"]))
        self.assertEqual(result["sequence_ocr"]["cells"][1]["sequence"], neighbor)

    def test_multiline_sequence_crossing_number_column_keeps_all_rows(self):
        self.multiline()

    def test_joined_leading_seq_id_is_clipped_in_pixels_without_dropping_residues(self):
        self.multiline(joined=True)

    def test_short_cdr_requires_column_context_and_does_not_select_names(self):
        grid = parse_grid('<table><tr><td>Name</td><td>HCDR1</td></tr><tr><td>REFERENCE</td><td>ACDEFG</td></tr></table>', "T")
        self.assertEqual(set(sequence_candidates(grid)), {"T:R1C1"})
        other = parse_grid('<table><tr><td>Name</td><td>Target</td></tr><tr><td>REFERENCE</td><td>ACDEFG</td></tr></table>', "T")
        self.assertEqual(sequence_candidates(other), {})

    def test_resolved_table_text_disagreement_is_not_a_question(self):
        state = {"sequence_extraction": {"decisions": [{"entity_id": "ab", "chain": "VH", "status": "resolved",
                  "source_conflicts": [{"table_ref": "t", "text_ref": "p", "message": "Q/O"}]}]}}
        self.assertEqual(collect_diagnostics(state), [])

    def test_page_text_comparison_is_advisory_and_survives_web_projection(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "studio"))
        from sda_data import attach_table_text_comparison
        result = refine(structure_table_evidence(self.html, self.image, self.metadata), model_provider=self.provider())
        entry = result["sequence_ocr"]["cells"][0]
        source_box = entry["lines"][0]["bbox_crop"]
        page_line = {"line_id": "p0001-l0001", "text": self.value, "bbox": source_box}
        _ground_table_cells(result, self.metadata, page_lines=[page_line])
        record = {"vh": self.value, "sda_fields": {"VH": {"provenance": [{"source_ref": "T:R0C1"}]}}}
        ocr = [{"source_id": "T", "rows": result["grid"]}]
        shown = attach_table_text_comparison(record, ocr)
        self.assertEqual(shown["sequence_table_text_status"], "matched")
        original_text = result["text"]
        page_line["text"] = self.value.replace("Q", "O", 1)
        _ground_table_cells(result, self.metadata, page_lines=[page_line])
        shown = attach_table_text_comparison(record, [{"source_id": "T", "rows": result["grid"]}])
        self.assertEqual(shown["sequence_table_text_status"], "mismatch")
        self.assertEqual(shown["vh"], self.value)
        self.assertEqual(result["text"], original_text)
        self.assertEqual(len(shown["sequence_source_conflicts"]), 1)
        self.assertNotIn("sequence_source_conflicts", record)


if __name__ == "__main__":
    unittest.main()
