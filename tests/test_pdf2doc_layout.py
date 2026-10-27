"""Validate consolidated layout, crop provenance, and legacy process entry points."""

import copy
from dataclasses import asdict
import json
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import pymupdf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from document import pdf2doc, pdf2fig


class PDFLayoutTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source.pdf"
        with pymupdf.open() as pdf:
            for index in range(2):
                page = pdf.new_page(width=300, height=400)
                page.insert_text((40, 180 if index == 0 else 35),
                                 "Table 1" if index == 0 else "Table 1 continued")
                page.insert_text((40, 230), "Sample A    1.25")
            pdf.save(self.source)
        self.payload = self.regions_for(self.source)
        self.regions = self.root / "regions.json"
        self.regions.write_text(json.dumps(self.payload))

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def regions_for(source):
        with pymupdf.open(source) as pdf:
            pages = []
            for index, page in enumerate(pdf):
                width, height = page.rect.width, page.rect.height
                pages.append({
                    "page": index + 1, "width_pt": width, "height_pt": height,
                    "detections": [{
                        "id": f"p{index + 1:04d}-r0001", "label": "table",
                        "confidence": 0.95,
                        "bbox": [width * 0.1, height * 0.5, width * 0.9, height * 0.95],
                    }],
                })
        return {
            "document": {"path": str(source), "sha256": pdf2doc.file_digest(source)},
            "settings": {"coordinate_space": "pdf_points", "bbox_format": "xyxy"},
            "pages": pages, "errors": [],
        }

    def test_saved_regions_crop_from_pdf_and_retain_provenance_without_model(self):
        payload = copy.deepcopy(self.payload)
        payload["pages"][0]["detections"] += [
            {"id": "figure-1", "label": "image", "confidence": 0.99,
             "bbox": [50, 70, 150, 120]},
            {"id": "low-confidence", "label": "table", "confidence": 0.3,
             "bbox": [30, 130, 270, 150]},
        ]
        self.regions.write_text(json.dumps(payload))
        output = self.root / "crops"
        with patch.object(pdf2doc, "PatentRegionLocator", side_effect=AssertionError("model loaded")):
            manifest = pdf2doc.extract_pdf(
                self.source, output, document_id="demo", regions_json=self.regions,
                orientation="off", labels=("table", "figure"), crop_dpi=144, padding_pt=0,
            )
        self.assertEqual(manifest["status"], "complete")
        self.assertEqual(manifest["image_count"], 3)
        self.assertEqual(manifest["selected_pages"], [1, 2])
        self.assertEqual(manifest["source_pdf_sha256"], pdf2doc.file_digest(self.source))
        self.assertEqual({c["id"] for c in manifest["images"]},
                         {"p0001-r0001", "p0002-r0001", "figure-1"})
        for crop in manifest["images"]:
            image = output / crop["image"]
            self.assertTrue(image.name.startswith("demo_"))
            self.assertEqual(crop["sha256"], pdf2doc.file_digest(image))
            pixmap = pymupdf.Pixmap(image)
            self.assertEqual([pixmap.width, pixmap.height], [crop["width_px"], crop["height_px"]])
            box = crop["bbox"]
            self.assertEqual(pixmap.width, round((box[2] - box[0]) * 2))
            self.assertEqual(pixmap.height, round((box[3] - box[1]) * 2))
        contexts = json.loads((output / "table_contexts.json").read_text())
        self.assertEqual({c["table_id"] for c in contexts}, {"p0001-r0001", "p0002-r0001"})
        logical = json.loads((output / "logical_tables.json").read_text())
        self.assertEqual({key for group in logical for key in group["table_ids"]},
                         {"p0001-r0001", "p0002-r0001"})

    def test_serial_detection_fuses_duplicates_and_converts_pixel_coordinates(self):
        def predict(backend, image_path):
            pixmap = pymupdf.Pixmap(image_path)
            box = [pixmap.width * 0.1, pixmap.height * 0.5,
                   pixmap.width * 0.9, pixmap.height * 0.95]
            return [pdf2doc.RawDetection("table", score, box, backend.model,
                                        backend.model_id, backend.name)
                    for score in (0.9, 0.85)]

        with patch.object(pdf2doc.PPDocLayoutV3Backend, "predict", predict):
            manifest = pdf2doc.extract_pdf(self.source, self.root / "serial", document_id="demo",
                                           orientation="off", dpi=144, crop_dpi=72, padding_pt=0)
        self.assertEqual(manifest["status"], "complete")
        self.assertEqual(manifest["image_count"], 2)
        for crop in manifest["images"]:
            self.assertEqual(crop["bbox"], [30, 200, 270, 380])
            self.assertEqual([crop["width_px"], crop["height_px"]], [240, 180])
            self.assertEqual(len(crop["provenance"]), 2)

    def test_reuses_prepared_geometry_and_maps_crops_to_source(self):
        prepared = self.root / "prepared"
        prepared.mkdir()
        working = prepared / "prepared.pdf"
        orientation = pdf2doc.prepare_pdf(
            self.source, working, prepared / "page_orientation.json",
            detector=lambda page, _directory: {
                "clockwise": 90 if page.number == 0 else 0,
                "status": "rotated" if page.number == 0 else "upright", "probes": [],
            },
        )
        regions = prepared / "regions.json"
        regions.write_text(json.dumps(self.regions_for(working)))
        manifest = pdf2doc.extract_pdf(self.source, self.root / "reused", document_id="demo",
                                       regions_json=regions, crop_dpi=72)
        self.assertEqual(manifest["orientation"]["status"], "reused_preparation")
        self.assertEqual(manifest["orientation"]["rotated_pages"], [1])
        self.assertEqual(pdf2doc.file_digest(Path(manifest["processing_pdf"])),
                         pdf2doc.file_digest(working))
        for crop in manifest["images"]:
            mapping = orientation["pages"][crop["page"] - 1]["working_to_source"]
            expected = pymupdf.Rect(crop["bbox"]) * pymupdf.Matrix(mapping)
            self.assertEqual(crop["bbox_source"], list(expected))

    def test_rejects_foreign_regions_and_keeps_existing_output(self):
        self.payload["document"]["sha256"] = "0" * 64
        self.regions.write_text(json.dumps(self.payload))
        with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
            pdf2doc.extract_pdf(self.source, self.root / "foreign", regions_json=self.regions)
        existing = self.root / "existing"
        existing.mkdir()
        marker = existing / "keep.txt"
        marker.write_text("preserve")
        with self.assertRaisesRegex(ValueError, "new or empty"):
            pdf2doc.extract_pdf(self.source, existing)
        self.assertEqual(marker.read_text(), "preserve")

    def test_legacy_exports_and_both_cli_paths_work_outside_repository(self):
        self.assertIs(pdf2fig.extract_pdf, pdf2doc.extract_pdf)
        self.assertIs(pdf2fig.PatentRegionLocator, pdf2doc.PatentRegionLocator)
        self.assertIs(pdf2fig.main, pdf2doc.layout_crop_main)
        for name in ("pdf2doc", "pdf2fig"):
            with self.subTest(entry=name):
                output = self.root / name
                process = subprocess.run(
                    [sys.executable, str(ROOT / f"src/document/{name}.py"),
                     *(["--layout-only"] if name == "pdf2doc" else []),
                     "--input", str(self.source), "--output-dir", str(output),
                     "--document-id", "demo", "--regions-json", str(self.regions),
                     "--orientation", "off", "--crop-dpi", "72"],
                    cwd=self.root, capture_output=True, text=True, timeout=30,
                )
                self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
                manifest = json.loads((output / "demo_manifest.json").read_text())
                self.assertEqual(manifest["image_count"], 2)

    def test_parallel_worker_dispatches_to_unified_script(self):
        # 空任务验证真实子进程入口和 JSONL 生命周期，无需下载或加载模型权重。
        job = {"id": "pp-test", "backend": "pp_doclayoutv3", "python": sys.executable,
               "device": "cpu", "threads": 1, "pp_engine": None, "model_dirs": {},
               "thresholds": {}, "pages": [], "results": str(self.root / "worker.jsonl"),
               "log": str(self.root / "worker.log")}
        job_path = self.root / "job.json"
        job_path.write_text(json.dumps(job))
        result = pdf2doc.supervise(job, job_path, queue.Queue())
        self.assertEqual(result["exit_code"], 0, Path(job["log"]).read_text())
        self.assertEqual(Path(job["results"]).read_text(), "")

    def test_parallel_results_merge_into_crops_and_report_invalid_boxes(self):
        def worker(job, _job_path, events):
            rows = []
            for page in reversed(job["pages"]):
                width, height = page["width_px"], page["height_px"]
                boxes = [[width * 0.1, height * 0.5, width * 0.9, height * 0.95],
                         [0, 0, 0, 10]]
                rows.append({
                    "page": page["number"], "backend": job["backend"], "ok": True,
                    "detections": [asdict(pdf2doc.RawDetection(
                        "table", 0.95, box, "PP-DocLayoutV3", "test", job["backend"],
                    )) for box in boxes],
                })
                events.put((job["backend"], page["number"]))
            Path(job["results"]).write_text("".join(json.dumps(row) + "\n" for row in rows))
            return {"id": job["id"], "backend": job["backend"], "exit_code": 0}

        with patch.object(pdf2doc, "supervise", worker):
            manifest = pdf2doc.extract_pdf(
                self.source, self.root / "parallel", document_id="demo", orientation="off",
                execution="parallel", cpu_workers=1, max_workers=1,
                dpi=144, crop_dpi=72, padding_pt=0,
            )
        self.assertEqual(manifest["status"], "partial")
        self.assertEqual(manifest["image_count"], 2)
        self.assertEqual([c["page"] for c in manifest["images"]], [1, 2])
        self.assertEqual([e["error_type"] for e in manifest["errors"]],
                         ["InvalidBoundingBox", "InvalidBoundingBox"])
        for crop in manifest["images"]:
            self.assertEqual(crop["bbox"], [30, 200, 270, 380])


if __name__ == "__main__":
    unittest.main()
