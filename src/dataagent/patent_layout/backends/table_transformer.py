"""Microsoft Table Transformer (TATR) table detector."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from .base import DetectorBackend
from ..schema import RawDetection


class TableTransformerBackend(DetectorBackend):
    name = "tatr"
    model = "Table Transformer (TATR)"
    model_id = "microsoft/table-transformer-detection"

    def __init__(
        self,
        model_dir: str | Path | None = None,
        threshold: float = 0.7,
        device: str | None = None,
    ) -> None:
        self.model_dir = Path(model_dir).expanduser() if model_dir else None
        self.threshold = threshold
        self.device_name = device
        self._processor = None
        self._model = None
        self._device = None

    @property
    def metadata(self) -> Dict[str, Any]:
        data = super().metadata
        data.update(
            {
                "model_source": self.model_id,
                "threshold": self.threshold,
                "model_dir": str(self.model_dir) if self.model_dir else None,
            }
        )
        return data

    def _load(self) -> tuple[Any, Any, Any]:
        if self._model is not None:
            return self._processor, self._model, self._device
        try:
            import torch  # type: ignore
            from transformers import AutoImageProcessor, AutoModelForObjectDetection  # type: ignore
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError(
                "TATR requires torch and transformers; see DataAgent/vendor/requirements-models.txt"
            ) from exc
        source = str(self.model_dir) if self.model_dir else self.model_id
        self._processor = AutoImageProcessor.from_pretrained(source)
        self._model = AutoModelForObjectDetection.from_pretrained(source)
        if self.device_name:
            self._device = torch.device(self.device_name)
        else:
            self._device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self._model.to(self._device)
        self._model.eval()
        return self._processor, self._model, self._device

    def predict(self, image_path: Path) -> List[RawDetection]:
        from PIL import Image

        processor, model, device = self._load()
        image = Image.open(image_path).convert("RGB")
        inputs = processor(images=image, return_tensors="pt")
        inputs = {key: value.to(device) for key, value in inputs.items()}
        with __import__("torch").no_grad():
            outputs = model(**inputs)
        target_sizes = __import__("torch").tensor([[image.height, image.width]], device=device)
        processed = processor.post_process_object_detection(
            outputs, threshold=self.threshold, target_sizes=target_sizes
        )[0]
        id2label = getattr(model.config, "id2label", {})
        detections: List[RawDetection] = []
        for score, label_id, box in zip(
            processed["scores"].detach().cpu().tolist(),
            processed["labels"].detach().cpu().tolist(),
            processed["boxes"].detach().cpu().tolist(),
        ):
            label = str(id2label.get(int(label_id), "table")).lower()
            # The detection checkpoint is trained for the table class; keep a
            # defensive filter in case a custom TATR checkpoint is supplied.
            if "table" not in label:
                continue
            detections.append(
                RawDetection(
                    label="table",
                    raw_label=label,
                    score=float(score),
                    bbox_px=box,
                    model=self.model,
                    model_id=self.model_id,
                    backend=self.name,
                    category_id=int(label_id),
                )
            )
        return detections
