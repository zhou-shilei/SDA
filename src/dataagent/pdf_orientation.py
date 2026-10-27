"""Prepare an upright PDF before layout, with reversible page coordinates.

Detection uses rendered pixels, never the (often misleading) OCR text layer.
The original file is immutable; the working copy retains PDF text and images.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable

import pymupdf as fitz

try:
    from .ocr_runtime import find_tesseract, tesseract_env
except ImportError:
    from .ocr_runtime import find_tesseract, tesseract_env


VERSION = 1
WORKING_PDF = "prepared.pdf"
MANIFEST = "page_orientation.json"


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def rotation_matrix(width: float, height: float, clockwise: int) -> Any:
    """Map top-left coordinates through a clockwise quarter-turn."""
    return {
        0: fitz.Matrix(1, 0, 0, 1, 0, 0),
        90: fitz.Matrix(0, 1, -1, 0, height, 0),
        180: fitz.Matrix(-1, 0, 0, -1, width, height),
        270: fitz.Matrix(0, -1, 1, 0, 0, width),
    }[clockwise % 360]


class OrientationDetector:
    """Require agreement at two render resolutions before changing a page."""

    def __init__(self) -> None:
        self.executable = find_tesseract()
        if not self.executable:
            raise RuntimeError("PDF orientation requires Tesseract and osd.traineddata")
        self.env = tesseract_env(self.executable, lang="osd")
        self.env["OMP_THREAD_LIMIT"] = "1"

    def _probe(self, page: Any, directory: Path, dpi: int) -> dict[str, Any]:
        # Bound memory for unusually large engineering sheets.
        scale = min(dpi / 72, 4096 / max(page.rect.width, page.rect.height))
        image = directory / f"orientation-{dpi}.png"
        page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False).save(image)
        try:
            result = subprocess.run(
                [self.executable, str(image), "stdout", "--psm", "0", "-l", "osd",
                 "--dpi", str(round(scale * 72))],
                capture_output=True, text=True, env=self.env, timeout=30,
            )
        except subprocess.TimeoutExpired:
            return {"dpi": dpi, "error": "orientation_timeout"}
        rotation = re.search(r"^Rotate:\s*(0|90|180|270)\s*$", result.stdout, re.M)
        confidence = re.search(r"^Orientation confidence:\s*([\d.]+)", result.stdout, re.M)
        if result.returncode or not rotation or not confidence:
            return {"dpi": dpi, "error": "insufficient_text_or_detection_failed",
                    "detail": result.stderr.strip()[-500:]}
        return {"dpi": dpi, "clockwise": int(rotation[1]), "confidence": float(confidence[1])}

    def __call__(self, page: Any, directory: Path) -> dict[str, Any]:
        primary = self._probe(page, directory, 160)
        probes = [primary]
        if "error" in primary or primary["confidence"] < 8:
            return {"clockwise": 0, "status": "needs_review", "probes": probes}
        if primary["clockwise"] == 0:
            return {"clockwise": 0, "status": "upright", "probes": probes}
        confirmation = self._probe(page, directory, 110)
        probes.append(confirmation)
        if (confirmation.get("clockwise") == primary["clockwise"]
                and confirmation.get("confidence", 0) >= 3):
            return {"clockwise": primary["clockwise"], "status": "rotated", "probes": probes}
        return {"clockwise": 0, "status": "needs_review", "probes": probes}


def prepare_pdf(
    source: Path, output: Path, manifest_path: Path, *,
    detector: Callable | None = None, progress: Callable | None = None,
) -> dict[str, Any]:
    source, output, manifest_path = source.resolve(), output.resolve(), manifest_path.resolve()
    if source == output or manifest_path in {source, output}:
        raise ValueError("Source, working PDF and orientation manifest must be distinct")
    if manifest_path.is_file():
        return validate_preparation(source, output, manifest_path)
    # A leftover working copy without a manifest was never committed. Rebuild it.
    detector = detector or OrientationDetector()
    source_hash = file_digest(source)
    output.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    pages = []
    with tempfile.TemporaryDirectory(prefix="pdf-orientation-", dir=output.parent) as temporary:
        directory = Path(temporary)
        with fitz.open(source) as original, fitz.open() as working:
            for index, page in enumerate(original):
                original_rotation = page.rotation
                display_width, display_height = page.rect.width, page.rect.height
                decision = detector(page, directory)
                correction = int(decision["clockwise"])
                if correction not in {0, 90, 180, 270}:
                    raise ValueError(f"Invalid page orientation: {correction}")
                # Flatten /Rotate into the content so every consumer uses the
                # same unrotated coordinate frame, including crop/text APIs.
                page.set_rotation(0)
                width, height = page.rect.width, page.rect.height
                total_rotation = (original_rotation + correction) % 360
                matrix = rotation_matrix(width, height, total_rotation)
                target_rect = page.rect * matrix
                target = working.new_page(width=target_rect.width, height=target_rect.height)
                if page.get_contents():
                    target.show_pdf_page(target.rect, original, index, rotate=-total_rotation)
                pages.append({
                    "page": index + 1, "original_rotation": original_rotation,
                    "correction_clockwise": correction, "status": decision["status"],
                    "probes": decision.get("probes", []),
                    "source_size": [width, height],
                    "working_size": [target_rect.width, target_rect.height],
                    "source_to_working": list(matrix), "working_to_source": list(~matrix),
                    "source_display_to_working": list(rotation_matrix(display_width, display_height, correction)),
                })
                if progress:
                    progress(index + 1, len(original), pages[-1])
            working.set_metadata(original.metadata)
            if original.get_toc():
                working.set_toc(original.get_toc())
            candidate = directory / "prepared.pdf"
            working.save(candidate, garbage=3, deflate=True)
        if file_digest(source) != source_hash:
            raise RuntimeError("Source PDF changed during orientation preparation")
        result = {
            "version": VERSION, "source": source.name, "source_sha256": source_hash,
            "working_pdf": output.name, "working_sha256": file_digest(candidate),
            "coordinate_space": "unrotated_cropbox_points_top_left",
            "detector": "tesseract_osd_160dpi_confirm_110dpi_min8_min3",
            "page_count": len(pages), "pages": pages,
            "rotated_pages": [p["page"] for p in pages if p["correction_clockwise"]],
            "review_pages": [p["page"] for p in pages if p["status"] == "needs_review"],
        }
        pending = directory / "manifest.json"
        pending.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        candidate.replace(output)
        pending.replace(manifest_path)
    return result


def validate_preparation(source: Path, output: Path, manifest_path: Path) -> dict[str, Any]:
    result = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (result.get("version") != VERSION or result.get("source") != source.name
            or result.get("working_pdf") != output.name
            or result.get("source_sha256") != file_digest(source)
            or not output.is_file() or result.get("working_sha256") != file_digest(output)):
        raise ValueError("PDF preparation is stale or incomplete; rebuild derived artifacts together")
    return result


def processing_pdf(root: Path) -> Path:
    """Legacy workspaces keep their geometry; new workspaces use the working PDF."""
    source, output, manifest = root / "source.pdf", root / WORKING_PDF, root / MANIFEST
    if manifest.exists():
        validate_preparation(source, output, manifest)
        # Never combine old geometry with a newly prepared PDF.
        for path, field in [(root / "regions.json", "document"), (root / "pages/pages.json", "source_pdf")]:
            if not path.is_file():
                continue
            payload = json.loads(path.read_text(encoding="utf-8"))
            name = payload.get(field, {})
            if field == "document":
                name = name.get("path", "")
            if not isinstance(name, str) or Path(name).name != output.name:
                raise ValueError("Existing page geometry does not belong to the prepared PDF")
        state_path = root / "agent_run/agent_state.json"
        if state_path.is_file():
            config = json.loads(state_path.read_text(encoding="utf-8")).get("config", {})
            if Path(config.get("source_pdf") or "").name != output.name:
                raise ValueError("Existing agent evidence does not belong to the prepared PDF")
        return output
    return source


def has_legacy_artifacts(root: Path) -> bool:
    return any((root / name).exists() for name in (
        "regions.json", "tables/table_crops.json", "pages/pages.json", "agent_run/agent_state.json",
        "agent_run/ocr_results.json",
    ))
