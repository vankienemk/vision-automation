"""Object detection abstraction.

The rest of the app never imports Ultralytics or torch directly: it talks to a
:class:`Detector` and receives :class:`Detection` values.  That keeps the
inference backend swappable (and testable with a fake detector) and keeps the
heavy imports lazy so the web UI can boot on a machine without torch.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

import numpy as np

from .detection import Box, Detection

LOGGER = logging.getLogger(__name__)

# COCO class names, used when the model has no ``names`` mapping of its own.
COCO_CLASSES: tuple[str, ...] = (
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck",
    "boat", "traffic light", "fire hydrant", "stop sign", "parking meter", "bench",
    "bird", "cat", "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra",
    "giraffe", "backpack", "umbrella", "handbag", "tie", "suitcase", "frisbee",
    "skis", "snowboard", "sports ball", "kite", "baseball bat", "baseball glove",
    "skateboard", "surfboard", "tennis racket", "bottle", "wine glass", "cup",
    "fork", "knife", "spoon", "bowl", "banana", "apple", "sandwich", "orange",
    "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair", "couch",
    "potted plant", "bed", "dining table", "toilet", "tv", "laptop", "mouse",
    "remote", "keyboard", "cell phone", "microwave", "oven", "toaster", "sink",
    "refrigerator", "book", "clock", "vase", "scissors", "teddy bear",
    "hair drier", "toothbrush",
)


@runtime_checkable
class Detector(Protocol):
    """Minimal inference interface used by the runtime."""

    @property
    def name(self) -> str: ...

    def class_names(self) -> dict[int, str]: ...

    def detect(self, frame: np.ndarray, *, confidence: float | None = None) -> list[Detection]: ...

    def info(self) -> dict: ...

    def close(self) -> None: ...


@dataclass
class DetectorStats:
    """Rolling counters exposed in the UI."""

    frames: int = 0
    detections: int = 0
    last_ms: float = 0.0
    total_ms: float = 0.0
    errors: int = 0
    extra: dict = field(default_factory=dict)

    @property
    def avg_ms(self) -> float:
        return self.total_ms / self.frames if self.frames else 0.0

    def as_dict(self) -> dict:
        return {
            "frames": self.frames,
            "detections": self.detections,
            "last_ms": round(self.last_ms, 1),
            "avg_ms": round(self.avg_ms, 1),
            "errors": self.errors,
            **self.extra,
        }


class NullDetector:
    """Fallback used when torch/ultralytics are unavailable.

    It never reports detections, so zones stay vacant and no command is
    published: the app degrades to "camera only" instead of crashing.
    """

    def __init__(self, reason: str = "ultralytics chưa được cài") -> None:
        self.reason = reason
        self.stats = DetectorStats()

    @property
    def name(self) -> str:
        return "null"

    def class_names(self) -> dict[int, str]:
        return {index: label for index, label in enumerate(COCO_CLASSES)}

    def detect(self, frame: np.ndarray, *, confidence: float | None = None) -> list[Detection]:
        self.stats.frames += 1
        return []

    def info(self) -> dict:
        return {
            "name": "null",
            "available": False,
            "reason": self.reason,
            "device": "none",
            "model": "-",
            "imgsz": 0,
            "stats": self.stats.as_dict(),
        }

    def close(self) -> None:
        return None


def resolve_device(requested: str) -> str:
    """Map ``auto`` to a concrete torch device string.

    Preference order on Apple Silicon is ``mps`` then ``cpu``; ``cuda`` is used
    when present.  Unavailable options fall back to ``cpu``.
    """
    want = (requested or "auto").strip().lower()
    if want not in {"auto", ""}:
        return want
    try:  # pragma: no cover - depends on the host
        import torch  # noqa: PLC0415
    except Exception:  # noqa: BLE001 - torch is optional
        return "cpu"
    try:
        if torch.cuda.is_available():
            return "cuda:0"
        mps = getattr(torch.backends, "mps", None)
        if mps is not None and mps.is_available():
            return "mps"
    except Exception:  # noqa: BLE001 - probing must never crash
        LOGGER.debug("Không dò được thiết bị torch, dùng cpu", exc_info=True)
    return "cpu"


class YoloDetector:
    """Ultralytics YOLO backend (``yolo11n.pt`` by default)."""

    def __init__(
        self,
        model_path: str = "yolo11n.pt",
        *,
        device: str = "auto",
        imgsz: int = 640,
        confidence: float = 0.45,
        max_det: int = 50,
        half: bool = False,
        classes_allow: list[str] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.log = logger or LOGGER
        self.model_path = model_path
        self.imgsz = int(imgsz)
        self.confidence = float(confidence)
        self.max_det = int(max_det)
        self.half = bool(half)
        self.classes_allow = set(classes_allow) if classes_allow else None
        self.requested_device = device
        self.device = resolve_device(device)
        self.stats = DetectorStats()
        self._model = None
        self._fallback_note: str | None = None
        self._load()

    # -- loading ---------------------------------------------------------- #
    def _load(self) -> None:
        # Keep Ultralytics' settings cache inside the project: its default
        # ~/Library/Application Support path is often not writable (NAS, Docker,
        # read-only home) and then downloads land in a random temp dir.
        import os  # noqa: PLC0415
        from pathlib import Path  # noqa: PLC0415

        os.environ.setdefault("YOLO_CONFIG_DIR", str(Path.cwd() / ".yolo"))

        from ultralytics import YOLO  # noqa: PLC0415 - lazy heavy import

        self._model = YOLO(self.model_path)
        # Warm up so the first real frame is not 3-5x slower (and so an
        # unsupported device surfaces here, at boot, not 30s later).
        warm = np.zeros((self.imgsz, self.imgsz, 3), dtype=np.uint8)
        started = time.perf_counter()
        try:
            self._infer(warm)
        except Exception as exc:  # noqa: BLE001 - device fallback
            if self.device != "cpu":
                self.log.warning(
                    "Thiết bị '%s' không chạy được (%s) — tự chuyển sang CPU",
                    self.device,
                    exc,
                )
                self._fallback_note = f"{self.device} → cpu: {exc}"
                self.device = "cpu"
                self._infer(warm)
            else:
                raise
        self.stats.last_ms = (time.perf_counter() - started) * 1000.0
        self.log.info(
            "Model %s sẵn sàng trên %s (imgsz=%d, warmup %.0f ms)",
            self.model_path,
            self.device,
            self.imgsz,
            self.stats.last_ms,
        )

    # -- inference -------------------------------------------------------- #
    def _infer(self, frame: np.ndarray, confidence: float | None = None):
        kwargs = {
            "imgsz": self.imgsz,
            "conf": self.confidence if confidence is None else float(confidence),
            "device": self.device,
            "max_det": self.max_det,
            "verbose": False,
        }
        # FP16 only makes sense (and is only supported) on CUDA.
        if self.half and self.device.startswith("cuda"):
            kwargs["half"] = True
        return self._model.predict(frame, **kwargs)

    def detect(self, frame: np.ndarray, *, confidence: float | None = None) -> list[Detection]:
        if frame is None or frame.size == 0:
            return []
        started = time.perf_counter()
        try:
            results = self._infer(frame, confidence)
        except Exception as exc:  # noqa: BLE001 - never kill the worker thread
            self.stats.errors += 1
            if self.device != "cpu":
                self.log.warning("Lỗi suy luận trên %s (%s) — chuyển sang CPU", self.device, exc)
                self._fallback_note = f"{self.device} → cpu: {exc}"
                self.device = "cpu"
                try:
                    results = self._infer(frame, confidence)
                except Exception as exc2:  # noqa: BLE001
                    self.log.error("Suy luận thất bại: %s", exc2)
                    self.stats.frames += 1
                    self.stats.last_ms = (time.perf_counter() - started) * 1000.0
                    self.stats.total_ms += self.stats.last_ms
                    return []
            else:
                self.log.error("Suy luận thất bại: %s", exc)
                self.stats.frames += 1
                self.stats.last_ms = (time.perf_counter() - started) * 1000.0
                self.stats.total_ms += self.stats.last_ms
                return []

        detections: list[Detection] = []
        names = self.class_names()
        for result in results or []:
            boxes = getattr(result, "boxes", None)
            if boxes is None:
                continue
            for box in boxes:
                try:
                    cls_id = int(box.cls[0].item())
                    conf = float(box.conf[0].item())
                    x1, y1, x2, y2 = (int(round(v)) for v in box.xyxy[0].tolist())
                except Exception:  # noqa: BLE001 - malformed row
                    continue
                label = names.get(cls_id, str(cls_id))
                if self.classes_allow is not None and label not in self.classes_allow:
                    continue
                detections.append(
                    Detection(
                        class_id=cls_id,
                        class_name=label,
                        confidence=conf,
                        box=Box(float(x1), float(y1), float(x2), float(y2)),
                    )
                )

        elapsed = (time.perf_counter() - started) * 1000.0
        self.stats.frames += 1
        self.stats.last_ms = elapsed
        self.stats.total_ms += elapsed
        self.stats.detections += len(detections)
        return detections

    def class_names(self) -> dict[int, str]:
        names = getattr(self._model, "names", None)
        if isinstance(names, dict):
            return {int(k): str(v) for k, v in names.items()}
        if isinstance(names, (list, tuple)):
            return {index: str(name) for index, name in enumerate(names)}
        return {index: label for index, label in enumerate(COCO_CLASSES)}

    def info(self) -> dict:
        extra = dict(self.stats.extra)
        if self._fallback_note:
            extra["device_fallback"] = self._fallback_note
        return {
            "name": "ultralytics",
            "available": True,
            "model": self.model_path,
            "device": self.device,
            "requested_device": self.requested_device,
            "imgsz": self.imgsz,
            "classes": len(self.class_names()),
            "stats": DetectorStats(
                frames=self.stats.frames,
                detections=self.stats.detections,
                last_ms=self.stats.last_ms,
                total_ms=self.stats.total_ms,
                errors=self.stats.errors,
                extra=extra,
            ).as_dict(),
        }

    def close(self) -> None:
        self._model = None


def build_detector(cfg, logger: logging.Logger | None = None) -> Detector:
    """Create the configured detector, falling back to :class:`NullDetector`."""
    log = logger or LOGGER
    try:
        return YoloDetector(
            cfg.model,
            device=cfg.device,
            imgsz=cfg.imgsz,
            confidence=cfg.min_confidence,
            max_det=cfg.max_det,
            half=cfg.half,
            classes_allow=cfg.classes_allow,
            logger=log,
        )
    except Exception as exc:  # noqa: BLE001 - optional heavy dependency
        reason = f"không nạp được model {cfg.model}: {exc}"
        log.error(
            "Không nạp được model '%s' (%s). App vẫn chạy nhưng KHÔNG phát hiện được "
            "người. Cài đặt: .venv/bin/uv pip install --python .venv/bin/python ultralytics",
            cfg.model,
            exc,
        )
        return NullDetector(reason=reason)


__all__ = [
    "COCO_CLASSES",
    "Detector",
    "DetectorStats",
    "NullDetector",
    "YoloDetector",
    "build_detector",
    "resolve_device",
]
