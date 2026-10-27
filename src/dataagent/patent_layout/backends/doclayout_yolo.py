"""DocLayout-YOLO adapter."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from .base import DetectorBackend
from ..schema import RawDetection


class DocLayoutYOLOBackend(DetectorBackend):
    name = "doclayout_yolo"
    model = "DocLayout-YOLO"
    model_id = "juliozhao/DocLayout-YOLO-DocStructBench"

    def __init__(
        self,
        model_path: str | Path | None = None,
        threshold: float = 0.2,
        imgsz: int = 1024,
        device: str | None = None,
    ) -> None:
        self.model_path = Path(model_path).expanduser() if model_path else None
        self.threshold = threshold
        self.imgsz = imgsz
        self.device = device
        self._model = None

    @property
    def metadata(self) -> Dict[str, Any]:
        data = super().metadata
        data.update(
            {
                "model_source": self.model_id,
                "threshold": self.threshold,
                "imgsz": self.imgsz,
                "model_path": str(self.model_path) if self.model_path else None,
            }
        )
        return data

    def _resolve_model_path(self) -> str:
        if self.model_path and self.model_path.is_file():
            return str(self.model_path)
        if self.model_path and self.model_path.is_dir():
            candidates = sorted(self.model_path.glob("*.pt"))
            if candidates:
                return str(candidates[0])
        try:
            from huggingface_hub import hf_hub_download  # type: ignore
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError(
                "DocLayout-YOLO requires doclayout-yolo and huggingface-hub; "
                "download the checkpoint into DataAgent/vendor/models first"
            ) from exc
        return hf_hub_download(
            repo_id=self.model_id,
            filename="doclayout_yolo_docstructbench_imgsz1024.pt",
        )

    def _load(self) -> Any:
        if self._model is not None:
            return self._model
        try:
            from doclayout_yolo import YOLOv10  # type: ignore
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError(
                "DocLayout-YOLO requires the doclayout-yolo package; "
                "see DataAgent/vendor/requirements-models.txt"
            ) from exc
        self._model = YOLOv10(self._resolve_model_path())
        return self._model

    def predict(self, image_path: Path) -> List[RawDetection]:
        model = self._load()
        kwargs: Dict[str, Any] = {
            "imgsz": self.imgsz,
            "conf": self.threshold,
        }
        if self.device:
            kwargs["device"] = self.device
        results = model.predict(str(image_path), **kwargs)
        detections: List[RawDetection] = []
        for result in results:
            names = getattr(result, "names", {})
            boxes = getattr(result, "boxes", None)
            if boxes is None:
                continue
            xyxy = boxes.xyxy.detach().cpu().tolist()
            confidences = boxes.conf.detach().cpu().tolist()
            classes = boxes.cls.detach().cpu().tolist()
            for coordinate, score, category_id in zip(xyxy, confidences, classes):
                category_id = int(category_id)
                label = names.get(category_id, str(category_id)) if isinstance(names, dict) else str(category_id)
                detections.append(
                    RawDetection(
                        label=str(label),
                        raw_label=str(label),
                        score=float(score),
                        bbox_px=coordinate,
                        model=self.model,
                        model_id=self.model_id,
                        backend=self.name,
                        category_id=category_id,
                    )
                )
        return detections
