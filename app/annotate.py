"""Frame annotation for the live view (bounding boxes, zones, HUD).

Kept separate from the web layer so the drawing code can be unit tested and so
``server.py`` stays about HTTP only.
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

import cv2
import numpy as np

Point = tuple[float, float]

#: BGR colours
COLOR_OCCUPIED = (98, 197, 34)  # green (UI accent #22c55e)
COLOR_VACANT = (150, 150, 150)  # gray
COLOR_BOX = (60, 200, 255)  # amber-ish for detections
COLOR_TEXT = (255, 255, 255)
COLOR_BADGE = (40, 40, 220)  # red-ish for TEST / errors
COLOR_DRY = (30, 170, 240)  # orange for dry-run

FONT = cv2.FONT_HERSHEY_SIMPLEX

def _put_text(frame: np.ndarray, text: str, org: Point, *, scale: float = 0.5, color=COLOR_TEXT, thickness: int = 1) -> None:
    """Draw text with a subtle shadow so it stays readable on any background.

    OpenCV cannot render Vietnamese diacritics with its Hershey fonts; the UI
    shows the accurate strings in HTML, so overlays use ASCII-safe text.
    """
    safe = _ascii_safe(text)
    x, y = int(org[0]), int(org[1])
    cv2.putText(frame, safe, (x + 1, y + 1), FONT, scale, (0, 0, 0), thickness + 1, cv2.LINE_AA)
    cv2.putText(frame, safe, (x, y), FONT, scale, color, thickness, cv2.LINE_AA)


def _ascii_safe(text: str) -> str:
    table = {
        "đ": "d", "Đ": "D", "ă": "a", "â": "a", "ê": "e", "ô": "o", "ơ": "o", "ư": "u",
        "á": "a", "à": "a", "ả": "a", "ã": "a", "ạ": "a",
        "ấ": "a", "ầ": "a", "ẩ": "a", "ẫ": "a", "ậ": "a",
        "ắ": "a", "ằ": "a", "ẳ": "a", "ẵ": "a", "ặ": "a",
        "é": "e", "è": "e", "ẻ": "e", "ẽ": "e", "ẹ": "e",
        "ế": "e", "ề": "e", "ể": "e", "ễ": "e", "ệ": "e",
        "í": "i", "ì": "i", "ỉ": "i", "ĩ": "i", "ị": "i",
        "ó": "o", "ò": "o", "ỏ": "o", "õ": "o", "ọ": "o",
        "ố": "o", "ồ": "o", "ổ": "o", "ỗ": "o", "ộ": "o",
        "ớ": "o", "ờ": "o", "ở": "o", "ỡ": "o", "ợ": "o",
        "ú": "u", "ù": "u", "ủ": "u", "ũ": "u", "ụ": "u",
        "ứ": "u", "ừ": "u", "ử": "u", "ữ": "u", "ự": "u",
        "ý": "y", "ỳ": "y", "ỷ": "y", "ỹ": "y", "ỵ": "y",
    }
    out = []
    for char in text:
        if char in table:
            out.append(table[char])
        elif ord(char) > 127:
            out.append("?")
        else:
            out.append(char)
    return "".join(out)


def _hex_to_bgr(value: str) -> tuple[int, int, int]:
    text = (value or "").lstrip("#")
    if len(text) != 6:
        return COLOR_VACANT
    try:
        r = int(text[0:2], 16)
        g = int(text[2:4], 16)
        b = int(text[4:6], 16)
    except ValueError:
        return COLOR_VACANT
    return (b, g, r)


def polygon_pixels(polygon: Sequence[Sequence[float]], width: int, height: int) -> np.ndarray:
    points = [
        (int(round(px * width)), int(round(py * height))) for px, py in polygon
    ]
    if not points:
        return np.zeros((0, 2), dtype=np.int32)
    return np.array(points, dtype=np.int32)


def draw_zones(
    frame: np.ndarray,
    zones: Iterable[dict],
    *,
    alpha: float = 0.22,
    thickness: int = 2,
) -> np.ndarray:
    """Draw semi-transparent zone polygons (green = occupied, gray = vacant)."""
    height, width = frame.shape[:2]
    overlay = frame.copy()
    has_fill = False
    for zone in zones:
        polygon = zone.get("polygon") or []
        if len(polygon) < 3:
            continue
        points = polygon_pixels(polygon, width, height)
        occupied = zone.get("occupied")
        base = _hex_to_bgr(zone.get("color", "#22c55e"))
        border = COLOR_OCCUPIED if occupied is True else (
            base if occupied is None else COLOR_VACANT
        )
        if occupied is True:
            cv2.fillPoly(overlay, [points], border)
            has_fill = True
        cv2.polylines(frame, [points], True, border, thickness, cv2.LINE_AA)

        label = str(zone.get("name") or zone.get("id") or "")
        count = int(zone.get("count") or 0)
        text = f"{label}: {'CO NGUOI' if occupied is True else ('TRONG' if occupied is False else '?')}"
        if occupied is True and count:
            text += f" ({count})"
        anchor = points[0]
        _put_text(frame, text, (anchor[0] + 4, max(14, anchor[1] - 6)), scale=0.45, color=border)

    if has_fill:
        cv2.addWeighted(overlay, alpha, frame, 1.0 - alpha, 0, dst=frame)
    return frame


def draw_detections(frame: np.ndarray, detections: Iterable[dict], *, thickness: int = 2) -> np.ndarray:
    for detection in detections:
        box = detection.get("box")
        if not box or len(box) != 4:
            continue
        x1, y1, x2, y2 = (int(round(v)) for v in box)
        cv2.rectangle(frame, (x1, y1), (x2, y2), COLOR_BOX, thickness)
        label = f"{detection.get('class', '?')} {float(detection.get('confidence', 0)) * 100:.0f}%"
        _put_text(frame, label, (x1 + 2, max(12, y1 - 4)), scale=0.45, color=COLOR_BOX)
    return frame


def draw_badge(frame: np.ndarray, text: str, *, origin: Point = (8, 8), color=COLOR_BADGE) -> None:
    x, y = int(origin[0]), int(origin[1])
    safe = _ascii_safe(text)
    (tw, th), baseline = cv2.getTextSize(safe, FONT, 0.5, 1)
    cv2.rectangle(frame, (x, y), (x + tw + 12, y + th + baseline + 8), color, -1)
    _put_text(frame, safe, (x + 6, y + th + 4), scale=0.5, color=COLOR_TEXT)


def draw_hud(
    frame: np.ndarray,
    *,
    camera_name: str,
    capture_fps: float = 0.0,
    inference_fps: float = 0.0,
    inference_ms: float = 0.0,
    connected: bool = False,
    mqtt_connected: bool = False,
    dry_run: bool = False,
    is_test: bool = False,
    warning: str | None = None,
) -> np.ndarray:
    """Bottom-left stats + top badges (TEST / DRY-RUN / offline warning)."""
    height, width = frame.shape[:2]
    lines = [
        _ascii_safe(camera_name),
        f"Capture {capture_fps:.1f} fps | Infer {inference_fps:.1f} fps | {inference_ms:.0f} ms",
        f"MQTT {'OK' if mqtt_connected else 'OFF'} | Camera {'OK' if connected else 'LOST'}",
    ]
    y = height - 12 - (len(lines) - 1) * 16
    for index, line in enumerate(lines):
        _put_text(frame, line, (10, y + index * 16), scale=0.45)

    x = 8
    if is_test:
        draw_badge(frame, "TEST", origin=(x, 8), color=(200, 120, 20))
        x += 70
    if dry_run:
        draw_badge(frame, "DRY-RUN", origin=(x, 8), color=COLOR_DRY)
    if warning:
        draw_badge(frame, warning, origin=(8, 40 if is_test or dry_run else 8))

    if not connected:
        # Dim the frozen frame so a lost stream is unmistakable in the UI.
        dim = frame.copy()
        cv2.rectangle(dim, (0, 0), (width, height), (0, 0, 0), -1)
        cv2.addWeighted(dim, 0.45, frame, 0.55, 0, dst=frame)
    return frame


def annotate(
    frame: np.ndarray,
    overlay: dict[str, Any],
    *,
    meta: dict[str, Any] | None = None,
) -> np.ndarray:
    """Return an annotated copy of ``frame`` (never mutates the capture buffer)."""
    canvas = frame.copy()
    draw_zones(canvas, overlay.get("zones") or [])
    draw_detections(canvas, overlay.get("detections") or [])
    info = meta or {}
    if info or overlay.get("camera"):
        draw_hud(
            canvas,
            camera_name=str(info.get("camera_name") or overlay.get("camera") or ""),
            capture_fps=float(info.get("capture_fps") or 0.0),
            inference_fps=float(info.get("inference_fps") or 0.0),
            inference_ms=float(info.get("inference_ms") or 0.0),
            connected=bool(info.get("connected", True)),
            mqtt_connected=bool(info.get("mqtt_connected", False)),
            dry_run=bool(info.get("dry_run", False)),
            is_test=bool(info.get("is_test", False)),
            warning=info.get("warning"),
        )
    return canvas


def encode_jpeg(frame: np.ndarray, *, quality: int = 70, width: int | None = None) -> bytes:
    """Encode BGR frame to JPEG, optionally scaled down to ``width``."""
    if width and frame.shape[1] > width:
        ratio = width / float(frame.shape[1])
        height = max(1, int(round(frame.shape[0] * ratio)))
        frame = cv2.resize(frame, (width, height), interpolation=cv2.INTER_AREA)
    ok, buffer = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
    if not ok:
        raise RuntimeError("Không mã hoá được JPEG")
    return buffer.tobytes()


def placeholder_jpeg(width: int = 960, height: int = 540, message: str = "Chua co hinh anh") -> bytes:
    """Gray placeholder used before the first frame / after a camera loss."""
    canvas = np.full((height, width, 3), 32, dtype=np.uint8)
    cv2.putText(canvas, "CAMERA OFFLINE", (width // 2 - 130, height // 2), FONT, 0.9, (90, 90, 90), 2, cv2.LINE_AA)
    if message:
        cv2.putText(canvas, _ascii_safe(message), (width // 2 - 90, height // 2 + 34), FONT, 0.6, (70, 70, 70), 1, cv2.LINE_AA)
    return encode_jpeg(canvas, quality=60)


__all__ = [
    "annotate",
    "draw_badge",
    "draw_detections",
    "draw_hud",
    "draw_zones",
    "encode_jpeg",
    "placeholder_jpeg",
    "polygon_pixels",
]
