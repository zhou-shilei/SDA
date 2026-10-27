"""Base interface for layout detector adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, List

from ..schema import RawDetection


class DetectorBackend(ABC):
    name: str
    model: str
    model_id: str

    @property
    def metadata(self) -> Dict[str, Any]:
        return {
            "backend": self.name,
            "model": self.model,
            "model_id": self.model_id,
        }

    @abstractmethod
    def predict(self, image_path: Path) -> List[RawDetection]:
        """Predict regions in image pixel coordinates."""
