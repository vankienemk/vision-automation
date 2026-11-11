"""Pure-Python detection value objects.

Kept free of numpy/OpenCV so that ``zones.py`` and ``tests`` never need the heavy
vision stack. ``Box`` stores pixel coordinates; ``Detection`` adds the class
metadata produced by a detector.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class Box:
    """Axis aligned box in pixel coordinates (x1, y1 top-left / x2, y2 bottom-right)."""

    x1: float
    y1: float
    x2: float
    y2: float

    @property
    def width(self) -> float:
        return max(0.0, self.x2 - self.x1)

    @property
    def height(self) -> float:
        return max(0.0, self.y2 - self.y1)

    @property
    def area(self) -> float:
        return self.width * self.height

    @property
    def center(self) -> tuple[float, float]:
        return ((self.x1 + self.x2) / 2.0, (self.y1 + self.y2) / 2.0)

    def anchor_bottom_center(self, foot_margin: float = 0.02) -> tuple[float, float]:
        """Feet position: horizontal centre, just above the bottom edge.

        The small ``foot_margin`` keeps the point inside the person silhouette so a
        bbox that ends exactly on a polygon edge is not flipped by one pixel of
        detector jitter.
        """
        cx = (self.x1 + self.x2) / 2.0
        return (cx, self.y2 - self.height * foot_margin)

    def clamped(self, width: float, height: float) -> "Box":
        return Box(
            min(max(self.x1, 0.0), width),
            min(max(self.y1, 0.0), height),
            min(max(self.x2, 0.0), width),
            min(max(self.y2, 0.0), height),
        )

    def as_tuple(self) -> tuple[float, float, float, float]:
        return (self.x1, self.y1, self.x2, self.y2)

    def to_list(self) -> list[float]:
        return [round(self.x1, 2), round(self.y1, 2), round(self.x2, 2), round(self.y2, 2)]


@dataclass(frozen=True, slots=True)
class Detection:
    """One object found in one frame."""

    class_id: int
    class_name: str
    confidence: float
    box: Box
    track_id: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "class_id": self.class_id,
            "class": self.class_name,
            "confidence": round(self.confidence, 4),
            "box": self.box.to_list(),
            "track_id": self.track_id,
        }


def shift_detection(detection: Detection, dx: float, dy: float) -> Detection:
    """Move a detection from crop coordinates back to full-frame coordinates."""
    if dx == 0 and dy == 0:
        return detection
    box = detection.box
    return Detection(
        class_id=detection.class_id,
        class_name=detection.class_name,
        confidence=detection.confidence,
        box=Box(box.x1 + dx, box.y1 + dy, box.x2 + dx, box.y2 + dy),
        track_id=detection.track_id,
    )


@dataclass(frozen=True, slots=True)
class DetectionResult:
    """All detections for a single frame."""

    detections: tuple[Detection, ...] = field(default_factory=tuple)
    width: int = 0
    height: int = 0
    inference_ms: float = 0.0
    frame_ts: float = 0.0

    def __iter__(self):
        return iter(self.detections)

    def __len__(self) -> int:
        return len(self.detections)

    @property
    def empty(self) -> bool:
        return not self.detections

    def as_list(self) -> list[dict[str, Any]]:
        return [d.as_dict() for d in self.detections]
