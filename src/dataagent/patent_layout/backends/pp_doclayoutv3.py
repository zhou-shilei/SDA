"""PP-DocLayoutV3 adapter using PaddleOCR's LayoutDetection API."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List

from .base import DetectorBackend
from ..schema import RawDetection


class PPDocLayoutV3Backend(DetectorBackend):
    name = "pp_doclayoutv3"
    model = "PP-DocLayoutV3"
    model_id = "PaddlePaddle/PP-DocLayoutV3"

    def __init__(
        self,
        model_dir: str | Path | None = None,
        threshold: float = 0.2,
        device: str | None = None,
        engine: str | None = None,
        cpu_threads: int | None = None,
    ) -> None:
        self.model_dir = Path(model_dir).expanduser() if model_dir else None
        self.threshold = threshold
        self.device = device
        self.engine = engine
        self.cpu_threads = cpu_threads
        self._model = None

    @property
    def metadata(self) -> Dict[str, Any]:
        data = super().metadata
        data.update(
            {
                "model_source": self.model_id,
                "threshold": self.threshold,
                "model_dir": str(self.model_dir) if self.model_dir else None,
                "engine": self.engine or "paddle_static(default)",
                "cpu_threads": self.cpu_threads,
            }
        )
        return data

    def _load(self) -> Any:
        if self._model is not None:
            return self._model
        try:
            from paddleocr import LayoutDetection  # type: ignore
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError(
                "PP-DocLayoutV3 requires paddleocr and paddlepaddle; "
                "see DataAgent/vendor/requirements-models.txt"
            ) from exc
        kwargs: Dict[str, Any] = {
            "model_name": "PP-DocLayoutV3",
            "model_dir": str(self.model_dir) if self.model_dir else None,
            "device": self.device,
            "engine": self.engine,
            "cpu_threads": self.cpu_threads,
        }
        kwargs = {key: value for key, value in kwargs.items() if value is not None}
        try:
            self._model = LayoutDetection(**kwargs)
        except TypeError:
            # Older PaddleOCR releases do not expose every v3 keyword.
            for key in ("engine", "device"):
                kwargs.pop(key, None)
            self._model = LayoutDetection(**kwargs)
        return self._model

    @staticmethod
    def _as_dict(result: Any) -> Dict[str, Any]:
        if isinstance(result, dict):
            return result
        value = getattr(result, "json", None)
        if callable(value):
            value = value()
        if isinstance(value, str):
            value = json.loads(value)
        if isinstance(value, dict):
            return value
        # PaddleX result objects often expose a JSON-compatible ``res`` attr.
        value = getattr(result, "res", None)
        if isinstance(value, dict):
            return {"res": value}
        raise TypeError(f"Unsupported PP-DocLayoutV3 result type: {type(result)!r}")

    def predict(self, image_path: Path) -> List[RawDetection]:
        model = self._load()
        results = model.predict(str(image_path), batch_size=1, layout_nms=True)
        detections: List[RawDetection] = []
        for result in results:
            payload = self._as_dict(result)
            boxes = payload.get("res", payload).get("boxes", [])
            for box in boxes:
                score = float(box.get("score", 0.0))
                if score < self.threshold:
                    continue
                label = str(box.get("label", box.get("cls_id", "unknown")))
                coordinate = box.get("coordinate", box.get("bbox"))
                if coordinate is None:
                    continue
                detections.append(
                    RawDetection(
                        label=label,
                        raw_label=label,
                        score=score,
                        bbox_px=coordinate,
                        model=self.model,
                        model_id=self.model_id,
                        backend=self.name,
                        category_id=int(box["cls_id"]) if box.get("cls_id") is not None else None,
                    )
                )
        return detections
