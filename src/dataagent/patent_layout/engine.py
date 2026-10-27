"""Orchestration, coordinate conversion, fusion and JSON serialization."""

from __future__ import annotations

import hashlib
import json
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence

from .backends.base import DetectorBackend
from .backends.doclayout_yolo import DocLayoutYOLOBackend
from .backends.pp_doclayoutv3 import PPDocLayoutV3Backend
from .backends.table_transformer import TableTransformerBackend
from .render import render_pdf
from .schema import RawDetection, clamp_bbox, cluster_detections, fused_detection


BACKEND_NAMES = ("pp_doclayoutv3", "doclayout_yolo", "tatr")


def _sha256(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _convert_to_pdf_points(
    bbox_px: Sequence[float], width_px: int, height_px: int, width_pt: float, height_pt: float
) -> List[float]:
    scale_x = width_pt / float(width_px)
    scale_y = height_pt / float(height_px)
    return [
        round(float(bbox_px[0]) * scale_x, 3),
        round(float(bbox_px[1]) * scale_y, 3),
        round(float(bbox_px[2]) * scale_x, 3),
        round(float(bbox_px[3]) * scale_y, 3),
    ]


def fuse_page(rendered: Any, raw_detections: Sequence[RawDetection], merge_iou: float) -> tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """One fusion implementation for serial and isolated parallel inference."""
    valid: List[RawDetection] = []
    errors: List[Dict[str, Any]] = []
    for detection in raw_detections:
        try:
            detection.bbox_px = clamp_bbox(detection.bbox_px, rendered.width_px, rendered.height_px)
            valid.append(detection)
        except ValueError:
            errors.append({"page": rendered.number, "backend": detection.backend,
                           "error_type": "InvalidBoundingBox", "error": repr(detection.bbox_px)})
    regions: List[Dict[str, Any]] = []
    for index, cluster in enumerate(cluster_detections(valid, merge_iou), start=1):
        fused = fused_detection(cluster)
        fused.update(id=f"p{rendered.number:04d}-r{index:04d}", page=rendered.number,
                     bbox=_convert_to_pdf_points(fused["bbox_px"], rendered.width_px,
                                                rendered.height_px, rendered.width_pt, rendered.height_pt),
                     bbox_format="xyxy", coordinate_space="pdf_points", bbox_px_format="xyxy",
                     coordinate_space_px="rendered_image_pixels")
        regions.append(fused)
    regions.sort(key=lambda item: (item["bbox"][1], item["bbox"][0], item["label"]))
    return {"page": rendered.number, "width_pt": round(rendered.width_pt, 3),
            "height_pt": round(rendered.height_pt, 3), "width_px": rendered.width_px,
            "height_px": rendered.height_px, "detections": regions}, errors


class PatentRegionLocator:
    """Locate layout regions in every selected page of a patent PDF."""

    def __init__(
        self,
        backends: Sequence[str] = BACKEND_NAMES,
        *,
        model_dirs: Mapping[str, str | Path] | None = None,
        thresholds: Mapping[str, float] | None = None,
        dpi: int = 150,
        merge_iou: float = 0.5,
        device: str | None = None,
        pp_engine: str | None = None,
        allow_missing_backends: bool = False,
        cpu_threads: int | None = None,
    ) -> None:
        self.backend_names = list(backends)
        self.model_dirs = {key: Path(value) for key, value in (model_dirs or {}).items()}
        self.thresholds = {
            "pp_doclayoutv3": 0.2,
            "doclayout_yolo": 0.2,
            "tatr": 0.7,
        }
        self.thresholds.update(thresholds or {})
        self.dpi = dpi
        self.merge_iou = merge_iou
        self.device = device
        self.pp_engine = pp_engine
        self.allow_missing_backends = allow_missing_backends
        self.cpu_threads = cpu_threads
        self.backends: List[DetectorBackend] = []
        self.backend_status: List[Dict[str, Any]] = []
        self._build_backends()

    def _build_backends(self) -> None:
        constructors = {
            "pp_doclayoutv3": lambda: PPDocLayoutV3Backend(
                model_dir=self.model_dirs.get("pp_doclayoutv3"),
                threshold=self.thresholds["pp_doclayoutv3"],
                device=self.device,
                engine=self.pp_engine,
                cpu_threads=self.cpu_threads,
            ),
            "doclayout_yolo": lambda: DocLayoutYOLOBackend(
                model_path=self.model_dirs.get("doclayout_yolo"),
                threshold=self.thresholds["doclayout_yolo"],
                device=self.device,
            ),
            "tatr": lambda: TableTransformerBackend(
                model_dir=self.model_dirs.get("tatr"),
                threshold=self.thresholds["tatr"],
                device=self.device,
            ),
        }
        for name in self.backend_names:
            if name not in constructors:
                raise ValueError(f"Unknown backend {name!r}; choose from {BACKEND_NAMES}")
            backend = constructors[name]()
            self.backends.append(backend)
            self.backend_status.append({"backend": name, "status": "configured", **backend.metadata})

    def locate(
        self,
        pdf_path: str | Path,
        *,
        pages: Iterable[int] | None = None,
        output_path: str | Path | None = None,
    ) -> Dict[str, Any]:
        pdf_path = Path(pdf_path).expanduser().resolve()
        if not pdf_path.is_file():
            raise FileNotFoundError(pdf_path)
        requested_pages = list(pages) if pages is not None else None
        started = time.perf_counter()
        backend_seconds = {backend.name: 0.0 for backend in self.backends}
        results: List[Dict[str, Any]] = []
        errors: List[Dict[str, Any]] = []
        with tempfile.TemporaryDirectory(prefix="patent-layout-") as temporary_dir:
            for rendered in render_pdf(pdf_path, temporary_dir, dpi=self.dpi, pages=requested_pages):
                raw_detections: List[RawDetection] = []
                successful_backends = 0
                for backend in self.backends:
                    backend_started = time.perf_counter()
                    try:
                        raw_detections.extend(backend.predict(rendered.image_path))
                        self._set_status(backend.name, "ok")
                        successful_backends += 1
                    except Exception as exc:  # backend isolation keeps other models useful
                        error = {
                            "page": rendered.number,
                            "backend": backend.name,
                            "error_type": type(exc).__name__,
                            "error": str(exc),
                        }
                        errors.append(error)
                        self._set_status(backend.name, "error", error)
                        if not self.allow_missing_backends:
                            raise RuntimeError(
                                f"{backend.name} failed on PDF page {rendered.number}: {exc}"
                            ) from exc
                    finally:
                        backend_seconds[backend.name] += time.perf_counter() - backend_started
                if not successful_backends:
                    detail = "; ".join(f"{e['backend']}: {e['error']}" for e in errors if e["page"] == rendered.number)
                    raise RuntimeError(f"No layout backend succeeded on PDF page {rendered.number}; check the model runtime. {detail}")
                result, invalid = fuse_page(rendered, raw_detections, self.merge_iou)
                errors.extend(invalid)
                results.append(result)

        payload: Dict[str, Any] = {
            "schema_version": "patent-layout-regions/v1",
            "document": {
                "path": str(pdf_path),
                "filename": pdf_path.name,
                "sha256": _sha256(pdf_path),
            },
            "settings": {
                "dpi": self.dpi,
                "merge_iou": self.merge_iou,
                "backends": self.backend_names,
                "coordinate_space": "pdf_points",
                "bbox_format": "xyxy",
                "device": self.device,
            },
            "models": self.backend_status,
            "errors": errors,
            "pages": results,
            "performance": {"execution": "serial", "elapsed_seconds": round(time.perf_counter() - started, 3),
                            "backend_seconds": {name: round(value, 3) for name, value in backend_seconds.items()}},
        }
        if output_path is not None:
            destination = Path(output_path).expanduser()
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return payload

    def _set_status(self, backend_name: str, status: str, error: Dict[str, Any] | None = None) -> None:
        for entry in self.backend_status:
            if entry["backend"] == backend_name:
                entry["status"] = status
                if error:
                    entry["last_error"] = error
                return
