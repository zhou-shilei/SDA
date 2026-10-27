"""Shared detection schema and geometry helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Sequence


MODEL_SOURCE = {
    "PP-DocLayoutV3": "https://github.com/PaddlePaddle/PaddleOCR",
    "DocLayout-YOLO": "https://github.com/opendatalab/DocLayout-YOLO",
    "Table Transformer (TATR)": "https://github.com/microsoft/table-transformer",
}


@dataclass
class RawDetection:
    """One model's prediction in rendered-image pixel coordinates."""

    label: str
    score: float
    bbox_px: Sequence[float]
    model: str
    model_id: str
    backend: str
    category_id: int | None = None
    raw_label: str | None = None
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "label": canonical_label(self.label),
            "raw_label": self.raw_label or self.label,
            "confidence": round(float(self.score), 6),
            "bbox_px": [round(float(v), 3) for v in self.bbox_px],
            "model": self.model,
            "model_id": self.model_id,
            "backend": self.backend,
            "category_id": self.category_id,
            "extra": self.extra,
        }


def canonical_label(label: str) -> str:
    """Normalize equivalent labels from the three model vocabularies."""

    value = str(label or "unknown").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "table": "table",
        "table_cell": "table_cell",
        "table_caption": "table_caption",
        "table_title": "table_caption",
        "figure_table_title": "figure_title",
        "figure_caption": "figure_caption",
        "image_caption": "figure_caption",
        "picture": "figure",
        "title": "document_title",
        "doc_title": "document_title",
        "paragraph_title": "paragraph_title",
        "text": "text",
        "plain_text": "text",
    }
    return aliases.get(value, value)


def clamp_bbox(bbox: Sequence[float], width: float, height: float) -> List[float]:
    """Clamp and order an xyxy box; raise on a degenerate prediction."""

    if len(bbox) != 4:
        raise ValueError(f"bbox must contain four values, got {bbox!r}")
    x1, y1, x2, y2 = (float(v) for v in bbox)
    x1, x2 = sorted((max(0.0, min(width, x1)), max(0.0, min(width, x2))))
    y1, y2 = sorted((max(0.0, min(height, y1)), max(0.0, min(height, y2))))
    if x2 <= x1 or y2 <= y1:
        raise ValueError(f"degenerate bbox {bbox!r}")
    return [x1, y1, x2, y2]


def iou(left: Sequence[float], right: Sequence[float]) -> float:
    """Intersection-over-union for xyxy boxes."""

    ix1 = max(float(left[0]), float(right[0]))
    iy1 = max(float(left[1]), float(right[1]))
    ix2 = min(float(left[2]), float(right[2]))
    iy2 = min(float(left[3]), float(right[3]))
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    intersection = iw * ih
    if intersection == 0:
        return 0.0
    left_area = max(0.0, float(left[2]) - float(left[0])) * max(
        0.0, float(left[3]) - float(left[1])
    )
    right_area = max(0.0, float(right[2]) - float(right[0])) * max(
        0.0, float(right[3]) - float(right[1])
    )
    union = left_area + right_area - intersection
    return intersection / union if union else 0.0


def weighted_box(detections: Iterable[RawDetection]) -> List[float]:
    """Confidence-weighted box fusion for one same-label cluster."""

    items = list(detections)
    weights = [max(1e-6, float(item.score)) for item in items]
    total = sum(weights)
    return [
        sum(weight * float(item.bbox_px[index]) for item, weight in zip(items, weights))
        / total
        for index in range(4)
    ]


def cluster_detections(
    detections: Sequence[RawDetection], merge_iou: float = 0.5
) -> List[List[RawDetection]]:
    """Cluster same-label boxes across model backends.

    This is deliberately conservative: a table detector cannot merge into a
    figure detector merely because the boxes overlap.  Different model
    predictions for the same semantic label are fused and retained in the
    provenance list.
    """

    clusters: List[List[RawDetection]] = []
    for detection in sorted(detections, key=lambda item: float(item.score), reverse=True):
        label = canonical_label(detection.label)
        for cluster in clusters:
            if canonical_label(cluster[0].label) == label and any(
                iou(detection.bbox_px, member.bbox_px) >= merge_iou for member in cluster
            ):
                cluster.append(detection)
                break
        else:
            clusters.append([detection])
    return clusters


def fused_detection(cluster: Sequence[RawDetection]) -> Dict[str, Any]:
    """Build the public, provenance-preserving region object."""

    if not cluster:
        raise ValueError("cannot fuse an empty detection cluster")
    label = canonical_label(cluster[0].label)
    best = max(cluster, key=lambda item: float(item.score))
    # The fused value is an evidence score, not a recalibrated probability.
    # The cap prevents multi-model support from producing invalid confidence.
    confidence = min(1.0, float(best.score) + 0.05 * (len(cluster) - 1))
    provenance = []
    for item in cluster:
        nested_provenance = item.extra.get("source_provenance") if item.extra else None
        if isinstance(nested_provenance, list) and nested_provenance:
            provenance.extend(nested_provenance)
            continue
        provenance.append(
            {
                "model": item.model,
                "model_id": item.model_id,
                "backend": item.backend,
                "source": MODEL_SOURCE.get(item.model),
                "raw_label": item.raw_label or item.label,
                "confidence": round(float(item.score), 6),
                "bbox_px": [round(float(v), 3) for v in item.bbox_px],
                "category_id": item.category_id,
                "extra": item.extra,
            }
        )
    return {
        "label": label,
        "confidence": round(confidence, 6),
        "bbox_px": [round(float(v), 3) for v in weighted_box(cluster)],
        "provenance": provenance,
    }
