import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PIL import Image
from document.sequence_styles import marked_residues, validate_cell, validate_style_source, review_prepared, style_image, style_candidates
from document.sequence_ocr import sequence_body, sequence_line
from tables.table_continuation import parse_grid
from dataagent2.inputs import load_input
from dataagent2.ledger import Ledger

DOMAIN = "SYELTQPLSVSVALGQTARITCSGDNIGSKYVHWYQQKPGQAPVLVIYGDNERPSGIPERFSGSNSGNTATLTISRAQAGDEADYYCQAADWVDFYVFGGGTKLTVL"
NOTE = "下划线标注的是轻链可变区VL"


class SequenceStyleTests(unittest.TestCase):
    def test_failed_sequence_ocr_is_sent_to_image_review_without_style_notes(self):
        cell = {"source_ref": "T:R1C2", "text": DOMAIN, "sequence_ocr": {"status": "failed"}}
        table = {"page": 1, "rows": [[cell]]}
        self.assertIn(cell["source_ref"], style_candidates({}, table)[0])
        cell["sequence_ocr"]["status"] = "success"
        self.assertFalse(style_candidates({}, table)[0])

    def test_visual_review_cannot_accept_only_a_middle_line(self):
        cell = self.cell()
        cell.update(marked_sequence=DOMAIN[:75], domain_chain="none", domain_sequence="")
        with self.assertRaisesRegex(ValueError, "incomplete"):
            validate_cell(cell, {"source_ref": cell["source_ref"], "text": DOMAIN + "GQPKAAPSVTLF"}, (400, 100))

    def cell(self):
        return {"source_ref": "T:R1C2", "status": "confirmed", "marked_sequence": "<u>" + DOMAIN + "</u>GQPKAAPSVTLF",
                "bbox_crop": [0, 0, 400, 100], "annotation_quote": NOTE,
                "domain_chain": "VL", "domain_sequence": DOMAIN, "reason": "Printed underline and annotation"}

    def test_nested_styles_retain_exact_character_offsets_in_grid(self):
        grid = parse_grid("<table><tr><td>AA<u>CD<b>EF</b>GH</u>IK</td></tr></table>", "T")
        cell = grid["physical"][0][0]
        self.assertEqual(cell["text"], "AA CD EF GH IK")
        for span in cell["text_styles"]:
            self.assertEqual(cell["text"][span["start"]:span["end"]], span["text"])
        sequence, spans = marked_residues("AA<u>CD<b>EF</b>GH</u>IK")
        self.assertEqual(sequence, "AACDEFGHIK")
        self.assertEqual([s["text"] for s in spans if "bold" in s["styles"]], ["EF"])
        self.assertEqual("".join(s["text"] for s in spans if "underline" in s["styles"]), "CDEFGH")

    def test_annotation_does_not_exclude_a_sequence_from_local_reocr(self):
        body, note = sequence_body(DOMAIN + "（" + NOTE + "）")
        self.assertEqual(body, DOMAIN)
        self.assertIn(NOTE, note)
        self.assertTrue(sequence_line(DOMAIN + "（" + NOTE + "）"))
        self.assertFalse(sequence_line("（" + NOTE + "）"))

    def test_domain_must_match_the_complete_printed_style_extent(self):
        cell = self.cell()
        original = {"source_ref": cell["source_ref"], "text": NOTE}
        self.assertEqual(validate_cell(cell, original, (400, 100))["domain"]["end"], len(DOMAIN))
        cell["marked_sequence"] = "<u>" + DOMAIN + "GQ</u>PKAAPSVTLF"
        with self.assertRaisesRegex(ValueError, "extent"):
            validate_cell(cell, original, (400, 100))

    def test_mutation_note_cannot_become_a_variable_boundary(self):
        cell = self.cell()
        cell["annotation_quote"] = "加粗以及下划线的氨基酸表示相对于亲本突变的氨基酸"
        with self.assertRaisesRegex(ValueError, "explicit"):
            validate_cell(cell, cell, (400, 100))
        cell["domain_chain"], cell["domain_sequence"] = "none", ""
        self.assertIsNone(validate_cell(cell, cell, (400, 100))["domain"])

    def test_note_crop_preserves_origin_and_original_pixels(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = root / "T.png"
            Image.new("RGB", (400, 800), "white").save(image)
            table = {"source_id": "T", "page": 1, "image_path": str(image),
                     "width": 400, "height": 800, "bbox_padded": [10, 20, 410, 820]}
            prepared = {"document_blocks": [{"page": 1, "text": "（" + NOTE + "）", "bbox": [50, 200, 350, 230]}]}
            path, origin = style_image(prepared, table, {"s": {"text": DOMAIN + "（" + NOTE + "）"}}, root)
            self.assertEqual(origin["image_crop_bounds"], [0, 0, 400, 234])
            with Image.open(path) as opened, Image.open(image) as original:
                self.assertEqual(opened.tobytes(), original.crop(origin["image_crop_bounds"]).tobytes())

    def test_mutation_footnote_selects_only_its_own_table(self):
        table = {"source_id": "T", "page": 1, "bbox": [0, 0, 400, 300],
                 "rows": [[{"source_ref": "s", "text": DOMAIN}]]}
        prepared = {"document_blocks": [{"page": 1, "text": "加粗表示相对于亲本突变的氨基酸", "bbox": [0, 305, 400, 320]}]}
        self.assertIn("s", style_candidates(prepared, table)[0])
        table["bbox"] = [0, 0, 400, 100]
        self.assertFalse(style_candidates(prepared, table)[0])

    def test_visual_evidence_is_separate_and_shared_only_by_explicit_seq_mapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = root / "T.png"
            Image.new("RGB", (400, 100), "white").save(image)
            rows = [[{"source_ref": "T:R0C0", "text": "Name", "origin_row": 0, "origin_column": 0}],
                    [{"source_ref": "T:R1C0", "text": "Parent", "origin_row": 1, "origin_column": 0},
                     {"source_ref": "T:R1C1", "text": "SEQ ID NO:57", "origin_row": 1, "origin_column": 1},
                     {"source_ref": "T:R1C2", "text": DOMAIN + "GQPKAAPSVTLF（" + NOTE + "）", "origin_row": 1, "origin_column": 2}]]
            prepared = {"document_id": "DOC", "tables": [{"source_id": "T", "page": 1,
                        "image_path": str(image), "rows": rows, "bbox": [0, 0, 400, 100],
                        "bbox_padded": [0, 0, 400, 100], "width": 400, "height": 100}]}
            original = copy.deepcopy(prepared)
            answer = self.cell()

            class Provider:
                def generate(inner, task, instructions, payload, contract, **kwargs):
                    value = {"cells": [answer]}
                    kwargs["validate"](value)
                    return value, {"input_hash": "test"}

            report = review_prepared(prepared, Provider(), root)
            self.assertEqual(report["summary"]["explicit_domains"], 1)
            self.assertEqual(prepared["tables"][0]["rows"], original["tables"][0]["rows"])
            source = report["sources"][0]
            originals = {c["source_ref"]: c for row in rows for c in row}
            validate_style_source(source, originals)
            self.assertNotEqual(source["source_ref"], "T:R1C2")
            (root / "sda_input.json").write_text(json.dumps(prepared))
            entities = [{"entity_id": n, "canonical_name": n, "raw_names": [n]} for n in ("Parent", "Shared", "Unmapped")]
            template = {"document_id": "DOC", "entity_template": {"entities": entities},
                        "sequence_links": [{"entity_id": n, "chain": "VL", "scope": "full_chain", "seq_id": 57,
                                            "evidence_refs": ["T:R1C1"]} for n in ("Parent", "Shared")]}
            (root / "document_template.json").write_text(json.dumps(template))
            data = load_input(root)
            actual = Ledger(data).assemble()
            self.assertEqual(actual["summary"]["variable_chains"], 2)
            for item in actual["entities"][:2]:
                self.assertEqual(item["sequences"]["VL"]["value"], DOMAIN)
                self.assertEqual(item["sequences"]["VL"]["boundary_method"], "printed_image_style")
                self.assertEqual(item["sequences"]["VL"]["seq_id_scope"], "full_chain")
            self.assertIsNone(actual["entities"][2]["sequences"]["VL"]["value"])
            image.write_bytes(b"changed image")
            with self.assertRaisesRegex(ValueError, "image changed"):
                validate_style_source(source, originals)


if __name__ == "__main__":
    unittest.main()
