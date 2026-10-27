"""Check spatial provenance across OCR, adapters, source rotation and UI export."""

import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from PIL import Image
from document.pdf2doc import _ground_table_cells, structure_table_evidence
from dataagent.sda_inputs import prepare_sda_inputs, read_prepared
from dataagent.sequence_evidence import build_document_structure, SequenceLocator
from dataagent.main_pipeline import _sources_and_coverage


HTML = ("<table><tr><th>Sample</th><th>IC50</th></tr>"
        "<tr><td>A</td><td>0.5</td></tr><tr><td>B</td><td>0.5</td></tr></table>")


class CellGeometryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.image = self.root / "table.png"
        Image.new("RGB", (440, 440), "white").save(self.image)
        self.meta = {"id": "T", "document_id": "DOC", "page": 1, "bbox": [100, 200, 300, 400],
                     "bbox_padded": [90, 190, 310, 410], "width_px": 440, "height_px": 440,
                     "crop_to_pdf": [0.5, 0, 0, 0.5, 90, 190]}
        entries = [("Sample", [110, 210, 160, 220]), ("IC50", [210, 210, 255, 220]),
                   ("A", [120, 240, 130, 250]), ("0.5", [220, 240, 240, 250]),
                   ("B", [120, 280, 130, 290]), ("0.5", [220, 280, 240, 290])]
        self.lines = [{"line_id": f"p0001-l{i + 1:04d}", "text": text, "bbox": box, "confidence": 0.97}
                      for i, (text, box) in enumerate(entries)]

    def evidence(self, text=HTML):
        return structure_table_evidence(text, self.image, self.meta, blocks=[
            {"block_content": text, "block_label": "table", "block_bbox": [0, 0, 440, 440]}])

    def test_repeated_values_follow_row_anchors_and_padding_and_rotation(self):
        result = self.evidence()
        # The same number in a caption must never become a cell candidate.
        lines = self.lines + [{"line_id": "caption", "text": "0.5", "bbox": [220, 180, 240, 190]}]
        _ground_table_cells(result, self.meta, page_lines=lines, working_to_source=[0, 1, -1, 0, 500, 0])
        first, second = result["grid"][1][1], result["grid"][2][1]
        self.assertEqual(first["bbox_pdf"], [220, 240, 240, 250])
        self.assertEqual(first["bbox_crop"], [260, 100, 300, 120])
        self.assertEqual(first["bbox_source"], [250, 220, 260, 240])
        self.assertEqual(second["bbox_pdf"], [220, 280, 240, 290])
        self.assertEqual(result["cell_locations"]["located"], 6)
        self.assertEqual(first["geometry_source_refs"], ["ocr:p0001-l0004"])
        self.assertEqual(result["text"], HTML)
        before = copy.deepcopy(result["cells"])
        _ground_table_cells(result, self.meta, page_lines=lines, working_to_source=[0, 1, -1, 0, 500, 0])
        self.assertEqual(result["cells"], before)

    def test_ambiguous_values_and_whole_table_blocks_are_unlocated(self):
        result = self.evidence("<table><tr><td>0.5</td></tr><tr><td>0.5</td></tr></table>")
        _ground_table_cells(result, self.meta, page_lines=self.lines[3::2])
        self.assertEqual(result["cell_locations"]["located"], 0)
        self.assertTrue(all(not cell["bbox_pdf"] for cell in result["cells"]))
        only_block = self.evidence()
        _ground_table_cells(only_block, self.meta, page_lines=[{
            "text": "A", "bbox": self.meta["bbox"], "granularity": "block"}])
        self.assertEqual(only_block["cell_locations"]["located"], 0)

    def test_merged_cells_keep_one_physical_address_and_location(self):
        text = "<table><tr><th rowspan='2'>Sample</th><th>IC50</th></tr><tr><td>0.5</td></tr></table>"
        result = self.evidence(text)
        _ground_table_cells(result, self.meta, page_lines=self.lines[:2] + [self.lines[3]])
        self.assertIs(result["grid"][0][0], result["grid"][1][0])
        self.assertEqual(result["grid"][1][0]["ref"], "T:R0C0")
        self.assertEqual(result["grid"][1][0]["bbox_pdf"], [110, 210, 160, 220])
        self.assertEqual(len(result["cells"]), 3)

    def test_crop_pixels_are_never_treated_as_pdf_points(self):
        result = self.evidence()
        result["lines"] = [{"text": line["text"], "bbox": line["bbox"], "coordinate_space": "crop_pixels"}
                           for line in self.lines]
        _ground_table_cells(result, self.meta)
        self.assertEqual(result["grid"][1][0]["bbox_crop"], [120, 240, 130, 250])
        self.assertEqual(result["grid"][1][0]["bbox_pdf"], [150, 310, 155, 315])

    def test_adapter_and_export_preserve_geometry_and_sequence_location(self):
        result = self.evidence()
        result.update(status="success")
        tables = self.root / "tables.jsonl"
        tables.write_text(json.dumps(result) + "\n")
        pages = self.root / "pages.jsonl"
        pages.write_text(json.dumps({"document_id": "DOC", "page_number": 1, "status": "success",
            "pdf_sha256": "a" * 64, "coordinate_space": "pdf_points", "lines": self.lines}) + "\n")
        path = prepare_sda_inputs("DOC", tables, pages, self.root / "out")
        prepared = read_prepared(path, "DOC")
        table = prepared["tables"][0]
        self.assertEqual(table["rows"][1][1]["bbox_pdf"], [220, 240, 240, 250])
        graph = build_document_structure("DOC", [table])
        self.assertEqual(graph["sources"]["T:R1C1"]["bbox_pdf"], [220, 240, 240, 250])
        sources, _, grids = _sources_and_coverage({"document_structure": graph}, prepared)
        self.assertEqual(sources["T:R1C1"]["bbox_pdf"], [220, 240, 240, 250])
        self.assertEqual(grids["table:T"]["rows"][1][1]["bbox_crop"], [260, 100, 300, 120])
        locator = SequenceLocator([table], [], [])
        self.addCleanup(locator.close)
        locator._source("T")
        self.assertEqual(locator._fallback_box(("T", 1, 1)), ([220, 240, 240, 250], "ocr_text"))

    def test_explicit_cell_boxes_require_literal_source_identity(self):
        result = self.evidence()
        result["cells"][2].update(bbox_pdf=[120, 240, 130, 250], precision="ocr_cell")
        result["cells"][3].update(text="OTHER", bbox_pdf=[220, 240, 240, 250])
        _ground_table_cells(result, self.meta)
        self.assertEqual(result["grid"][1][0]["bbox_pdf"], [120, 240, 130, 250])
        self.assertEqual(result["grid"][1][1]["bbox_pdf"], [])

    def test_conflicting_or_outside_explicit_coordinates_are_not_reused(self):
        result = self.evidence()
        result["cells"][2].update(bbox_crop=[10, 10, 20, 20], bbox_pdf=[120, 240, 130, 250])
        result["cells"][3].update(bbox_pdf=[500, 500, 600, 600])
        _ground_table_cells(result, self.meta)
        self.assertEqual(result["cell_locations"]["located"], 0)

    def test_page_method_boxes_already_in_pdf_points_are_not_scaled_again(self):
        method = {"page": 1, "source": "page_ocr", "coordinate_space": "pdf_points",
                  "text": "ACDEFGHIK", "lines": [{"text": "ACDEFGHIK", "bbox": [20, 30, 120, 40]}]}
        locator = SequenceLocator([], [method], [])
        self.addCleanup(locator.close)
        locator._source("method:p1")
        self.assertEqual(locator._fallback_box(("method:p1", 0, 0)), ([20, 30, 120, 40], "ocr_line"))
        legacy = SequenceLocator([], [{**method, "source": "pdf_text"}], [])
        self.addCleanup(legacy.close)
        legacy._source("method:p1")
        self.assertEqual(legacy._fallback_box(("method:p1", 0, 0)), ([], "unlocated"))


if __name__ == "__main__":
    unittest.main()
