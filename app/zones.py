"""Zone geometry and presence detection (pure Python, no OpenCV).

Two responsibilities:

*Geometry* — decide whether a detection is inside a polygon. Polygons are stored
normalised (0..1) so a zone keeps working after switching RTSP sub-stream ->
main-stream, or when the webcam resolution changes.

*Presence* — turn a noisy per-frame "is somebody inside?" signal into a stable
``occupied`` boolean using K-of-N voting plus an exit grace period. This is the
first line of defence against blinking lights.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Iterable, Literal, Sequence

from .config import ZoneConfig
from .detection import Box, Detection

Point = tuple[float, float]
AnchorMode = Literal["bottom_center", "center", "any_overlap", "overlap_ratio"]


# --------------------------------------------------------------------------- #
# geometry primitives
# --------------------------------------------------------------------------- #
def polygon_area(points: Sequence[Point]) -> float:
    """Shoelace area (always positive)."""
    if len(points) < 3:
        return 0.0
    total = 0.0
    for index in range(len(points)):
        x1, y1 = points[index]
        x2, y2 = points[(index + 1) % len(points)]
        total += x1 * y2 - x2 * y1
    return abs(total) / 2.0


def polygon_bbox(points: Sequence[Point]) -> tuple[float, float, float, float]:
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return (min(xs), min(ys), max(xs), max(ys))


def point_in_polygon(px: float, py: float, polygon: Sequence[Point]) -> bool:
    """Ray casting. Handles convex and concave polygons; edges count as inside."""
    inside = False
    count = len(polygon)
    if count < 3:
        return False
    j = count - 1
    for i in range(count):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        # on-edge tolerance
        cross = (px - xi) * (yj - yi) - (py - yi) * (xj - xi)
        if abs(cross) < 1e-9:
            if min(xi, xj) - 1e-9 <= px <= max(xi, xj) + 1e-9 and min(yi, yj) - 1e-9 <= py <= max(yi, yj) + 1e-9:
                return True
        if (yi > py) != (yj > py):
            x_cross = (xj - xi) * (py - yi) / (yj - yi) + xi
            if px < x_cross:
                inside = not inside
        j = i
    return inside


def _clip_edge(points: list[Point], inside, intersect) -> list[Point]:
    if not points:
        return []
    output: list[Point] = []
    previous = points[-1]
    previous_inside = inside(previous)
    for current in points:
        current_inside = inside(current)
        if current_inside:
            if not previous_inside:
                output.append(intersect(previous, current))
            output.append(current)
        elif previous_inside:
            output.append(intersect(previous, current))
        previous, previous_inside = current, current_inside
    return output


def clip_polygon_to_box(polygon: Sequence[Point], box: Box) -> list[Point]:
    """Sutherland-Hodgman clip of ``polygon`` against an axis aligned rectangle."""
    points = list(polygon)
    x1, y1, x2, y2 = box.x1, box.y1, box.x2, box.y2

    def clip_vertical(points_in: list[Point], x_edge: float, keep_greater: bool) -> list[Point]:
        def inside(pt: Point) -> bool:
            return pt[0] >= x_edge if keep_greater else pt[0] <= x_edge

        def intersect(a: Point, b: Point) -> Point:
            if abs(b[0] - a[0]) < 1e-12:
                return (x_edge, a[1])
            t = (x_edge - a[0]) / (b[0] - a[0])
            return (x_edge, a[1] + t * (b[1] - a[1]))

        return _clip_edge(points_in, inside, intersect)

    def clip_horizontal(points_in: list[Point], y_edge: float, keep_greater: bool) -> list[Point]:
        def inside(pt: Point) -> bool:
            return pt[1] >= y_edge if keep_greater else pt[1] <= y_edge

        def intersect(a: Point, b: Point) -> Point:
            if abs(b[1] - a[1]) < 1e-12:
                return (a[0], y_edge)
            t = (y_edge - a[1]) / (b[1] - a[1])
            return (a[0] + t * (b[0] - a[0]), y_edge)

        return _clip_edge(points_in, inside, intersect)

    points = clip_vertical(points, x1, True)
    points = clip_vertical(points, x2, False)
    points = clip_horizontal(points, y1, True)
    points = clip_horizontal(points, y2, False)
    return points


def box_polygon_overlap_area(box: Box, polygon: Sequence[Point]) -> float:
    """Exact area of the intersection between a box and a polygon (pixels²)."""
    clipped = clip_polygon_to_box(polygon, box)
    return polygon_area(clipped) if len(clipped) >= 3 else 0.0


def normalised_to_pixels(polygon: Sequence[Point], width: int, height: int) -> list[Point]:
    return [(x * width, y * height) for x, y in polygon]


def anchor_point(box: Box, mode: AnchorMode) -> Point:
    if mode == "center":
        return box.center
    return box.anchor_bottom_center()


# --------------------------------------------------------------------------- #
# per zone evaluation
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class ZoneCounts:
    """Raw (un-smoothed) per-frame picture of one zone."""

    total: int = 0
    class_counts: dict[str, int] = field(default_factory=dict)
    matched: list[Detection] = field(default_factory=list)

    def add(self, detection: Detection) -> None:
        self.total += 1
        self.class_counts[detection.class_name] = self.class_counts.get(detection.class_name, 0) + 1
        self.matched.append(detection)


@dataclass(slots=True)
class ZoneState:
    """Stable presence state of one zone (output of voting + grace)."""

    camera_id: str
    zone_id: str
    name: str
    occupied: bool | None = None
    count: int = 0
    class_counts: dict[str, int] = field(default_factory=dict)
    since: float | None = None
    updated_at: float = 0.0
    forced: bool = False
    detections: list[Detection] = field(default_factory=list)
    pending_exit: float | None = None

    @property
    def ref(self) -> str:
        return f"{self.camera_id}/{self.zone_id}"

    def as_dict(self) -> dict[str, object]:
        return {
            "camera": self.camera_id,
            "zone_id": self.zone_id,
            "zone": self.ref,
            "name": self.name,
            "occupied": self.occupied,
            "count": self.count,
            "class_counts": dict(self.class_counts),
            "since": self.since,
            "updated_at": self.updated_at,
            "forced": self.forced,
            "pending_exit": self.pending_exit,
        }


class PresenceTracker:
    """K-of-N voting + exit grace for a single zone.

    ``occupied`` is ``None`` until the tracker has seen enough evidence; that
    "unknown" state is what keeps the app from publishing an OFF command right
    after boot.
    """

    def __init__(self, frames: int, of: int, exit_grace_s: float) -> None:
        self.frames = max(1, frames)
        self.of = max(self.frames, of)
        self.exit_grace_s = max(0.0, exit_grace_s)
        self._window: deque[bool] = deque(maxlen=self.of)
        self.occupied: bool | None = None
        self.since: float | None = None
        self._vacant_candidate_since: float | None = None
        self.count = 0
        self.class_counts: dict[str, int] = {}

    def reset(self) -> None:
        self._window.clear()
        self.occupied = None
        self.since = None
        self._vacant_candidate_since = None
        self.count = 0
        self.class_counts = {}

    def reconfigure(self, frames: int, of: int, exit_grace_s: float) -> None:
        window = list(self._window)
        self.frames = max(1, frames)
        self.of = max(self.frames, of)
        self.exit_grace_s = max(0.0, exit_grace_s)
        self._window = deque(window[-self.of :], maxlen=self.of)

    def update(self, now: float, counts: ZoneCounts) -> tuple[bool | None, bool]:
        """Feed one frame. Returns ``(occupied, changed)``."""
        self.count = counts.total
        self.class_counts = dict(counts.class_counts)
        present = counts.total > 0
        self._window.append(present)
        votes = sum(self._window)

        if self.occupied is not True and votes >= self.frames:
            changed = self.occupied is not True
            self.occupied = True
            if changed:
                self.since = now
            self._vacant_candidate_since = None
            return self.occupied, changed

        if self.occupied is True and votes < self.frames:
            if self._vacant_candidate_since is None:
                self._vacant_candidate_since = now
            if now - self._vacant_candidate_since >= self.exit_grace_s:
                self.occupied = False
                self.since = now
                self._vacant_candidate_since = None
                return self.occupied, True
            return self.occupied, False

        if self.occupied is None and len(self._window) >= self.of and votes < self.frames:
            # enough empty frames to declare "nobody here" for the first time
            if self._vacant_candidate_since is None:
                self._vacant_candidate_since = now
            if now - self._vacant_candidate_since >= self.exit_grace_s:
                self.occupied = False
                self.since = now
                self._vacant_candidate_since = None
                return self.occupied, True
        return self.occupied, False

    def force(self, now: float, occupied: bool, count: int, class_counts: dict[str, int] | None = None) -> tuple[bool | None, bool]:
        """Override the state (simulation / camera-lost policy)."""
        changed = self.occupied != occupied
        self.occupied = occupied
        if changed:
            self.since = now
        self.count = count if occupied else 0
        self.class_counts = dict(class_counts or {})
        self._vacant_candidate_since = None
        self._window.clear()
        self._window.extend([occupied] * self.of)
        return self.occupied, changed

    @property
    def pending_exit_s(self) -> float | None:
        return self._vacant_candidate_since


class ZoneEngine:
    """Evaluates every zone of one camera against the current detections."""

    def __init__(self, camera_id: str, zones: Sequence[ZoneConfig], global_min_confidence: float) -> None:
        self.camera_id = camera_id
        self.global_min_confidence = global_min_confidence
        self._zones: dict[str, ZoneConfig] = {}
        self._trackers: dict[str, PresenceTracker] = {}
        self._states: dict[str, ZoneState] = {}
        self._forced: dict[str, tuple[bool, int, dict[str, int]]] = {}
        for zone in zones:
            self.add_zone(zone)

    # -- lifecycle -------------------------------------------------------- #
    def add_zone(self, zone: ZoneConfig) -> None:
        self._zones[zone.id] = zone
        tracker = self._trackers.get(zone.id)
        if tracker is None:
            self._trackers[zone.id] = PresenceTracker(
                zone.enter_confirm.frames, zone.enter_confirm.of, zone.exit_grace_s
            )
        else:
            tracker.reconfigure(zone.enter_confirm.frames, zone.enter_confirm.of, zone.exit_grace_s)
        if zone.id not in self._states:
            self._states[zone.id] = ZoneState(self.camera_id, zone.id, zone.name)

    def remove_zone(self, zone_id: str) -> None:
        self._zones.pop(zone_id, None)
        self._trackers.pop(zone_id, None)
        self._states.pop(zone_id, None)
        self._forced.pop(zone_id, None)

    def zone_ids(self) -> list[str]:
        return list(self._zones)

    def zones(self) -> list[ZoneConfig]:
        return list(self._zones.values())

    # -- simulation hooks -------------------------------------------------- #
    def force_zone(self, zone_id: str, occupied: bool, count: int = 1, class_counts: dict[str, int] | None = None) -> None:
        if not occupied:
            self._forced[zone_id] = (False, 0, {})
        else:
            counts = dict(class_counts or {})
            if not counts:
                zone = self._zones.get(zone_id)
                default_class = (zone.classes[0] if zone and zone.classes else "person")
                counts = {default_class: max(1, count)}
            self._forced[zone_id] = (True, max(1, count), counts)

    def clear_force(self, zone_id: str | None = None) -> None:
        if zone_id is None:
            self._forced.clear()
        else:
            self._forced.pop(zone_id, None)

    def is_forced(self, zone_id: str) -> bool:
        return zone_id in self._forced

    def forced_zones(self) -> list[str]:
        return list(self._forced)

    # -- evaluation ------------------------------------------------------- #
    def evaluate(
        self,
        now: float,
        detections: Sequence[Detection],
        frame_width: int,
        frame_height: int,
    ) -> list[ZoneState]:
        """Return the state of every zone, in config order."""
        states: list[ZoneState] = []
        for zone_id, zone in self._zones.items():
            state = self._states[zone_id]
            if not zone.enabled:
                state.updated_at = now
                states.append(state)
                continue
            counts = self._count_zone(zone, detections, frame_width, frame_height)
            tracker = self._trackers[zone_id]
            forced = self._forced.get(zone_id)
            if forced is not None:
                occupied, changed = tracker.force(now, forced[0], forced[1], forced[2])
                state.count = forced[1]
                state.class_counts = dict(forced[2])
                state.forced = True
                state.detections = []
            else:
                previous = tracker.occupied
                occupied, changed = tracker.update(now, counts)
                state.count = counts.total
                state.class_counts = counts.class_counts
                state.forced = False
                state.detections = counts.matched
                _ = (previous, changed)
            state.occupied = occupied
            state.since = tracker.since
            state.updated_at = now
            pending = tracker.pending_exit_s
            state.pending_exit = (
                max(0.0, zone.exit_grace_s - (now - pending)) if pending is not None and occupied else None
            )
            states.append(state)
        return states

    def evaluate_forced(self, now: float) -> list[ZoneState]:
        """Refresh only the *forced* zones (simulation / camera-lost fail-safe).

        Forced zones exist precisely when no real frame may be available (dead
        camera, user simulation), so they must be able to advance the scenario
        engine without waiting for inference.
        """
        states: list[ZoneState] = []
        for zone_id, forced in list(self._forced.items()):
            zone = self._zones.get(zone_id)
            state = self._states.get(zone_id)
            if zone is None or state is None or not zone.enabled:
                continue
            tracker = self._trackers[zone_id]
            occupied, _changed = tracker.force(now, forced[0], forced[1], forced[2])
            state.count = forced[1]
            state.class_counts = dict(forced[2])
            state.forced = True
            state.detections = []
            state.occupied = occupied
            state.since = tracker.since
            state.updated_at = now
            state.pending_exit = None
            states.append(state)
        return states

    def transitions(
        self,
        now: float,
        detections: Sequence[Detection],
        frame_width: int,
        frame_height: int,
    ) -> tuple[list[ZoneState], list[tuple[ZoneState, bool]]]:
        """Evaluate and also report which zones changed state this frame."""
        before = {zone_id: state.occupied for zone_id, state in self._states.items()}
        states = self.evaluate(now, detections, frame_width, frame_height)
        changes: list[tuple[ZoneState, bool]] = []
        for state in states:
            previous = before.get(state.zone_id)
            if state.occupied is not None and previous != state.occupied:
                changes.append((state, bool(state.occupied)))
        return states, changes

    def _count_zone(
        self,
        zone: ZoneConfig,
        detections: Sequence[Detection],
        frame_width: int,
        frame_height: int,
    ) -> ZoneCounts:
        counts = ZoneCounts()
        if frame_width <= 0 or frame_height <= 0:
            return counts
        polygon = normalised_to_pixels(zone.polygon, frame_width, frame_height)
        min_confidence = zone.min_confidence if zone.min_confidence is not None else self.global_min_confidence
        allowed = {name.lower() for name in zone.classes}
        for detection in detections:
            if allowed and detection.class_name.lower() not in allowed:
                continue
            if detection.confidence < min_confidence:
                continue
            if self._match(zone, detection, polygon):
                counts.add(detection)
        return counts

    @staticmethod
    def _match(zone: ZoneConfig, detection: Detection, polygon: Sequence[Point]) -> bool:
        box = detection.box
        anchor = zone.anchor
        if anchor == "any_overlap":
            return box_polygon_overlap_area(box, polygon) > 0.0
        if anchor == "overlap_ratio":
            if box.area <= 0:
                return False
            ratio = box_polygon_overlap_area(box, polygon) / box.area
            return ratio >= zone.overlap_ratio
        point = anchor_point(box, "center" if anchor == "center" else "bottom_center")
        return point_in_polygon(point[0], point[1], polygon)


def zone_crop_box(
    zones: Sequence[ZoneConfig],
    frame_width: int,
    frame_height: int,
    *,
    padding: float = 0.08,
) -> tuple[int, int, int, int] | None:
    """Union bbox (pixels) of the enabled zones, padded — for crop-then-detect.

    Returns ``None`` when cropping would not help (no zones, or the union covers
    almost the whole frame), so callers can fall back to full-frame inference.
    """
    if frame_width <= 0 or frame_height <= 0:
        return None
    xs: list[float] = []
    ys: list[float] = []
    for zone in zones:
        if not zone.enabled or len(zone.polygon) < 3:
            continue
        for px, py in zone.polygon:
            xs.append(px)
            ys.append(py)
    if not xs:
        return None
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    pad_x = (max_x - min_x) * padding
    pad_y = (max_y - min_y) * padding
    x1 = max(0.0, min_x - pad_x) * frame_width
    y1 = max(0.0, min_y - pad_y) * frame_height
    x2 = min(1.0, max_x + pad_x) * frame_width
    y2 = min(1.0, max_y + pad_y) * frame_height
    box_w = x2 - x1
    box_h = y2 - y1
    if box_w < 32 or box_h < 32:
        return None
    if box_w * box_h >= 0.75 * frame_width * frame_height:
        return None  # gần như toàn khung: cắt chỉ tốn công
    return (int(x1), int(y1), int(x2), int(y2))


def make_zone_state_unknown(camera_id: str, zone: ZoneConfig, now: float | None = None) -> ZoneState:
    return ZoneState(camera_id=camera_id, zone_id=zone.id, name=zone.name, occupied=None, updated_at=now or time.time())


def summarise_states(states: Iterable[ZoneState]) -> dict[str, dict[str, object]]:
    return {state.ref: state.as_dict() for state in states}


__all__ = [
    "PresenceTracker",
    "zone_crop_box",
    "ZoneCounts",
    "ZoneEngine",
    "ZoneState",
    "anchor_point",
    "box_polygon_overlap_area",
    "clip_polygon_to_box",
    "make_zone_state_unknown",
    "normalised_to_pixels",
    "point_in_polygon",
    "polygon_area",
    "polygon_bbox",
    "summarise_states",
]
