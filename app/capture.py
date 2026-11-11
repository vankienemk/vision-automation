"""Frame capture with one dedicated thread per camera.

Design goals (in priority order): never block the UI, never leak, always hand
out the *newest* frame, and recover on its own from camera/network failures.

* Each :class:`CaptureWorker` runs its own thread and owns its
  :class:`cv2.VideoCapture`.  A failure in one camera can never affect another.
* Readers only ever publish into a single-slot mailbox, so slow inference can
  never cause unbounded memory growth — old frames are dropped, not queued.
* RTSP is opened over TCP through the FFmpeg backend, with open/read timeouts
  and exponential backoff reconnect.  A "stale" watchdog reconnects when the
  stream silently freezes (a classic RTSP failure mode).
* Opening a webcam is *lazy*: only when someone watches (MJPEG client) or a
  test scenario needs it.  Closing the last watcher closes the device, so the
  camera LED turns off (privacy) and the device is free for other apps.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

import cv2
import numpy as np

from . import platform_compat as compat
from .config import CameraConfig

LOGGER = logging.getLogger(__name__)

#: Guards the process-wide ``OPENCV_FFMPEG_CAPTURE_OPTIONS`` env var while a
#: stream is being opened (OpenCV reads it at open time only).
_ENV_LOCK = threading.Lock()


# --------------------------------------------------------------------------- #
# frames
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class FramePacket:
    """One captured frame plus the metadata the pipeline needs."""

    seq: int
    frame: np.ndarray
    captured_at: float  # time.monotonic()
    wall_time: float  # time.time()
    width: int
    height: int

    def age(self, now: float | None = None) -> float:
        return (time.monotonic() if now is None else now) - self.captured_at


@dataclass
class CaptureStatus:
    camera_id: str
    connected: bool = False
    is_open: bool = False
    stale: bool = False
    error: str | None = None
    hint: str | None = None
    last_frame_at: float | None = None
    capture_fps: float = 0.0
    frames_total: int = 0
    reconnects: int = 0
    source_desc: str = ""
    watchers: int = 0
    permission_denied: bool = False
    #: Time spent inside the last blocking ``read()``. On a healthy RTSP stream
    #: this is roughly one frame interval; ~0 ms means frames are queued up
    #: (backlog) and the picture shown is older than real time.
    read_ms: float = 0.0
    #: Frame rate the source reports (``CAP_PROP_FPS``), useful to compare with
    #: the measured ``capture_fps``.
    stream_fps: float = 0.0
    #: Age of the newest frame in milliseconds (how old the picture is).
    frame_age_ms: float = 0.0

    def as_dict(self) -> dict:
        return {
            "camera_id": self.camera_id,
            "connected": self.connected,
            "is_open": self.is_open,
            "stale": self.stale,
            "error": self.error,
            "hint": self.hint,
            "last_frame_at": self.last_frame_at,
            "capture_fps": round(self.capture_fps, 1),
            "frames_total": self.frames_total,
            "reconnects": self.reconnects,
            "source": self.source_desc,
            "watchers": self.watchers,
            "permission_denied": self.permission_denied,
            "read_ms": round(self.read_ms, 1),
            "stream_fps": round(self.stream_fps, 1),
            "frame_age_ms": round(self.frame_age_ms, 1),
        }


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def frame_signature(frame: np.ndarray, size: int = 32) -> np.ndarray:
    """Cheap downscaled grayscale thumbnail used for motion scoring."""
    small = cv2.resize(frame, (size, size), interpolation=cv2.INTER_AREA)
    return cv2.cvtColor(small, cv2.COLOR_BGR2GRAY).astype(np.float32)


def motion_score(previous: np.ndarray | None, current: np.ndarray) -> float:
    """Mean absolute difference between two signatures (0 = identical)."""
    if previous is None or previous.shape != current.shape:
        return 255.0
    return float(np.abs(previous - current).mean())


def looks_black(frame: np.ndarray, ratio: float = 0.98) -> bool:
    """True when the frame is almost entirely black (typical of denied access)."""
    if frame is None or frame.size == 0:
        return True
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    return float((gray < 8).mean()) >= ratio


def _set_ffmpeg_options(options: dict[str, str]) -> dict[str, str]:
    """Temporarily install FFmpeg capture options; returns the previous value."""
    previous = os.environ.get("OPENCV_FFMPEG_CAPTURE_OPTIONS", "")
    joined = "|".join(f"{key};{value}" for key, value in options.items())
    os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = joined
    return {"OPENCV_FFMPEG_CAPTURE_OPTIONS": previous}


def _restore_env(saved: dict[str, str]) -> None:
    for key, value in saved.items():
        if value:
            os.environ[key] = value
        else:
            os.environ.pop(key, None)


def camera_authorization_status() -> str:
    """OS camera permission state (see :func:`app.platform_compat.camera_permission_status`).

    macOS reports ``authorized``/``denied``/``restricted``/``not_determined``
    (``unknown`` without pyobjc); Windows and Linux report ``not_required``
    because they have no per-app consent prompt.
    """
    return compat.camera_permission_status()


def request_camera_permission(timeout: float = 25.0) -> tuple[bool, str]:
    """Ask the OS for camera access; MUST be called from the main thread.

    Only macOS prompts. OpenCV cannot prompt from a worker thread ("can not spin
    main run loop from other thread"), so the app asks once at startup and then
    sets ``OPENCV_AVFOUNDATION_SKIP_AUTH=1`` so capture threads never try.
    Returns ``(granted, message)``.
    """
    return compat.request_camera_permission(timeout=timeout)


#: Kept for backwards compatibility; prefer :func:`platform_compat.webcam_permission_hint`.
WEB_CAM_PERMISSION_HINT = compat.WEBCAM_PERMISSION_HINT


# --------------------------------------------------------------------------- #
# webcam enumeration (delegated to platform_compat)
# --------------------------------------------------------------------------- #
def _mark_recommended(devices: list[dict]) -> list[dict]:
    """Rank devices: a built-in, non-iPhone camera is the safe default."""
    for device in devices:
        device.setdefault("model", "")
        device.setdefault("unique_id", "")
        device.setdefault("builtin", False)
        device.setdefault("is_iphone", False)
        device["recommended"] = bool(device["builtin"] and not device["is_iphone"])
    if devices and not any(d["recommended"] for d in devices):
        # Windows (without pygrabber) cannot tell a built-in camera apart; the
        # first DirectShow device is still the sensible default.
        devices[0]["recommended"] = True
    return sorted(devices, key=lambda d: (not d["recommended"], d["is_iphone"], d["index"]))


def list_webcams() -> list[dict]:
    """Enumerate webcams available to this OS, best candidate first.

    * macOS: AVFoundation (pyobjc) or ``system_profiler``; OpenCV exposes no
      device names, so the ordered AVFoundation list is assumed to match.
    * Windows: DirectShow via ``pygrabber`` when installed — the *name* is what
      the user selects, and the index matches ``cv2.CAP_DSHOW`` enumeration.
    * Linux: ``/dev/video*`` nodes.

    Never opens a stream, so the camera LED stays off.
    """
    devices = compat.list_webcam_devices()
    if not devices:
        devices = compat.placeholder_devices()
    return _mark_recommended([dict(item) for item in devices])


def resolve_webcam_index(device: str | int | None) -> int:
    """Translate a configured device (``auto``, name or index) into an index."""
    if isinstance(device, int):
        return device
    text = (device or "auto").strip()
    if text.isdigit():
        return int(text)
    devices = list_webcams()
    if text.lower() not in {"", "auto"}:
        lowered = text.lower()
        for entry in devices:
            if lowered in entry["name"].lower() or lowered == str(entry.get("unique_id", "")).lower():
                return int(entry["index"])
        LOGGER.warning("Không thấy webcam tên '%s', dùng camera mặc định", text)
    for entry in devices:  # first recommended (built-in, not iPhone)
        if entry["recommended"]:
            return int(entry["index"])
    return int(devices[0]["index"]) if devices else 0


# --------------------------------------------------------------------------- #
# capture worker
# --------------------------------------------------------------------------- #
StatusCallback = Callable[[CaptureStatus], None]


class CaptureWorker:
    """Owns one camera in one thread and keeps only the newest frame."""

    def __init__(
        self,
        camera: CameraConfig,
        *,
        logger: logging.Logger | None = None,
        on_status: StatusCallback | None = None,
        mirror_override: bool | None = None,
    ) -> None:
        self.camera = camera
        self.camera_id = camera.id
        self.log = logger or LOGGER.getChild(f"capture.{camera.id}")
        self._on_status = on_status
        self._mirror = camera.mirror if mirror_override is None else mirror_override

        self._cap: cv2.VideoCapture | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._reopen = threading.Event()
        self._lock = threading.Lock()
        self._latest: FramePacket | None = None
        self._seq = 0
        self._sig: np.ndarray | None = None
        self._last_seq_seen = -1
        self._frame_times: list[float] = []
        self._watchers: set[str] = set()
        self._watcher_lock = threading.Lock()
        self._force_open = False
        self._status = CaptureStatus(camera_id=camera.id, source_desc=self._describe())
        self._backoff = camera.reconnect.initial_s
        self._last_error_logged: str | None = None
        self._consecutive_failures = 0
        self._black_frames = 0
        self._image_frame: np.ndarray | None = None

    # -- lifecycle -------------------------------------------------------- #
    def _describe(self) -> str:
        cam = self.camera
        if cam.source == "rtsp":
            from .config import mask_url  # noqa: PLC0415 - avoid cycle at import

            return f"rtsp ({cam.transport.upper()}) {mask_url(cam.url or '')}"
        if cam.source == "webcam":
            width, height = cam.resolution or (1280, 720)
            return f"webcam [{cam.device}] {width}x{height}@{cam.fps}fps"
        if cam.source == "file":
            return f"file {cam.url}"
        return f"image {cam.url}"

    @property
    def is_test(self) -> bool:
        return self.camera.is_test

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_guarded,
            name=f"capture-{self.camera_id}",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        self._reopen.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        self._thread = None
        self._close_capture()
        with self._lock:
            self._latest = None
        self._status.is_open = False
        self._status.connected = False
        self._emit_status()

    def request_reopen(self) -> None:
        self._reopen.set()

    # -- lazy open -------------------------------------------------------- #
    def add_watcher(self, reason: str) -> None:
        with self._watcher_lock:
            self._watchers.add(reason)
            count = len(self._watchers)
        self._status.watchers = count
        if count:
            self._force_open = True
            self._reopen.set()
            self._emit_status()
            if self._thread is None or not self._thread.is_alive():
                self.start()

    def remove_watcher(self, reason: str) -> None:
        with self._watcher_lock:
            self._watchers.discard(reason)
            count = len(self._watchers)
        self._status.watchers = count
        if count == 0 and self.camera.lazy_open:
            self._force_open = False
            # the loop closes the device on its next iteration
        self._emit_status()

    @property
    def watchers(self) -> int:
        with self._watcher_lock:
            return len(self._watchers)

    def _wanted(self) -> bool:
        if not self.camera.lazy_open:
            return True
        if self._force_open:
            return True
        with self._watcher_lock:
            return bool(self._watchers)

    # -- frame access ----------------------------------------------------- #
    def latest(self, *, max_age: float | None = None) -> FramePacket | None:
        with self._lock:
            packet = self._latest
        if packet is None:
            return None
        if max_age is not None and packet.age() > max_age:
            return None
        return packet

    def new_frame(self, last_seq: int) -> FramePacket | None:
        """Return the newest packet only if it is newer than ``last_seq``."""
        packet = self.latest()
        if packet is None or packet.seq <= last_seq:
            return None
        return packet

    def last_seq(self) -> int:
        with self._lock:
            return self._seq

    def signature(self) -> np.ndarray | None:
        with self._lock:
            return self._sig

    def snapshot(self) -> np.ndarray | None:
        packet = self.latest()
        return None if packet is None else packet.frame.copy()

    def status(self) -> CaptureStatus:
        """Live status snapshot (frame age is computed at read time)."""
        with self._lock:
            packet = self._latest
        if packet is not None:
            self._status.frame_age_ms = packet.age() * 1000.0
        return self._status

    # -- status plumbing -------------------------------------------------- #
    def _emit_status(self) -> None:
        if self._on_status is not None:
            try:
                self._on_status(self._status)
            except Exception:  # noqa: BLE001 - callbacks must never break capture
                self.log.debug("status callback lỗi", exc_info=True)

    def _set_error(self, message: str | None, hint: str | None = None) -> None:
        changed = message != self._status.error or hint != self._status.hint
        self._status.error = message
        self._status.hint = hint
        if changed and message:
            self.log.warning("%s%s", message, f" — {hint}" if hint else "")
        self._emit_status()

    def _run_guarded(self) -> None:
        """Keep one camera's crash from killing its capture thread for good.

        A bug anywhere in the loop (or in OpenCV) must degrade into "camera lost +
        retry with backoff", never into a silently dead thread that leaves the UI
        without frames until the app restarts.
        """
        attempt = 0
        while not self._stop_event.is_set():
            try:
                self._run()
                return
            except Exception as exc:  # noqa: BLE001 - never let the thread die
                attempt += 1
                self.log.exception("Luồng camera %s gặp lỗi (%d lần)", self.camera_id, attempt)
                self._mark_lost(f"Lỗi luồng đọc camera: {exc}")
                try:
                    self._close_capture()
                except Exception:  # noqa: BLE001
                    pass
                self._wait_backoff()
        self.log.info("Luồng camera %s dừng", self.camera_id)

    # -- main loop -------------------------------------------------------- #
    def _run(self) -> None:
        self.log.info("Luồng camera %s bắt đầu (%s)", self.camera_id, self._status.source_desc)
        while not self._stop_event.is_set():
            if not self._wanted():
                if self._cap is not None:
                    self._close_capture()
                    self.log.info("Đóng %s (không còn ai xem)", self.camera_id)
                    self._status.is_open = False
                    self._status.connected = False
                    with self._lock:
                        self._latest = None
                    self._emit_status()
                self._stop_event.wait(0.4)
                continue

            if self._cap is None or self._reopen.is_set():
                self._reopen.clear()
                if not self._open():
                    self._wait_backoff()
                    continue

            started = time.perf_counter()
            ok, frame = self._read_frame()
            read_ms = (time.perf_counter() - started) * 1000.0
            self._status.read_ms = read_ms
            if not ok or frame is None:
                self._consecutive_failures += 1
                self.log.debug("Đọc khung hình thất bại (%d lần)", self._consecutive_failures)
                # ``fps`` is optional in the config (None means "device default"),
                # so never do arithmetic on it directly.
                fps_hint = int((self.camera.fps or 15) // 2) or 3
                if self._consecutive_failures >= max(3, fps_hint):
                    self._mark_lost("Mất tín hiệu từ camera")
                    self._close_capture()
                    self._wait_backoff()
                else:
                    time.sleep(0.05)
                continue

            self._consecutive_failures = 0
            self._publish(frame, started)
            self._enforce_rate(started)

    def _read_frame(self) -> tuple[bool, np.ndarray | None]:
        if self._image_frame is not None:
            return True, self._image_frame
        if self._cap is None:
            return False, None
        return self._cap.read()

    def _publish(self, frame: np.ndarray, started: float) -> None:
        if self._mirror and self.camera.source in {"webcam", "file", "image"}:
            frame = cv2.flip(frame, 1)

        # Permission denial and dead plugs both surface as a black image.
        if self.camera.source == "webcam" and self._black_frames < 40:
            if looks_black(frame):
                self._black_frames += 1
                if self._black_frames == 20:
                    self._status.permission_denied = True
                    self._mark_lost("Camera trả về ảnh đen", WEB_CAM_PERMISSION_HINT)
            elif self._black_frames:
                self._black_frames = 0
                self._status.permission_denied = False

        height, width = frame.shape[:2]
        now_mono = time.monotonic()
        with self._lock:
            self._seq += 1
            packet = FramePacket(
                seq=self._seq,
                frame=frame,
                captured_at=now_mono,
                wall_time=time.time(),
                width=width,
                height=height,
            )
            self._latest = packet
            self._sig = frame_signature(frame)

        self._status.connected = True
        self._status.stale = False
        self._status.last_frame_at = time.time()
        self._status.frames_total += 1
        if self._status.error or self._status.permission_denied:
            if not self._status.permission_denied:
                self._status.error = None
                self._status.hint = None
        self._backoff = self.camera.reconnect.initial_s

        # capture FPS = frames seen in the last 2 seconds
        self._frame_times.append(now_mono)
        cutoff = now_mono - 2.0
        while self._frame_times and self._frame_times[0] < cutoff:
            self._frame_times.pop(0)
        if len(self._frame_times) > 1:
            span = self._frame_times[-1] - self._frame_times[0]
            self._status.capture_fps = (len(self._frame_times) - 1) / span if span > 0 else 0.0

    def _enforce_rate(self, started: float) -> None:
        """Pace the reader for sources that deliver frames faster than real time.

        ``file``/``image`` sources must be paced or they spin the CPU. A live
        RTSP stream must **not** be paced: ``read()`` already blocks at the
        stream rate, and sleeping on top of it (e.g. pacing 10 fps while the
        camera sends 11 fps) makes the reader fall behind by a few frames per
        second. The backlog then never drains, the picture drifts seconds away
        from reality and eventually the stream looks frozen.
        """
        source = self.camera.source
        if source == "rtsp":
            return
        if source == "webcam" and not self.camera.fps:
            return
        target = max(0.5, float(self.camera.fps or 10))
        budget = 1.0 / target
        elapsed = time.perf_counter() - started
        remaining = budget - elapsed
        if remaining > 0:
            self._stop_event.wait(min(remaining, 0.5))

    def _wait_backoff(self) -> None:
        delay = self._backoff
        self._backoff = min(
            self.camera.reconnect.max_s, self._backoff * self.camera.reconnect.factor
        )
        self._status.reconnects += 1
        self._emit_status()
        self.log.info("Thử lại camera %s sau %.1fs", self.camera_id, delay)
        self._stop_event.wait(delay)

    def _mark_lost(self, message: str, hint: str | None = None) -> None:
        was_connected = self._status.connected
        self._status.connected = False
        self._status.stale = was_connected
        self._set_error(message, hint)

    # -- open / close ----------------------------------------------------- #
    def _open(self) -> bool:
        cam = self.camera
        self._status.permission_denied = False
        try:
            if cam.source == "rtsp":
                cap = self._open_rtsp()
            elif cam.source == "webcam":
                cap = self._open_webcam()
            elif cam.source == "file":
                cap = self._open_file()
            elif cam.source == "image":
                cap = self._open_image()
            else:  # pragma: no cover - validated by config
                raise ValueError(f"nguồn không hỗ trợ: {cam.source}")
        except Exception as exc:  # noqa: BLE001 - never kill the thread
            self._set_error(f"Không mở được camera: {exc}", self._open_hint())
            return False

        if cap is None:
            return False
        self._cap = cap
        self._status.is_open = True
        self._status.stale = False
        self._status.read_ms = 0.0
        #: Fresh connection: forget the failure tally so a recovered stream is
        #: not marked lost again after a single hiccup.
        self._consecutive_failures = 0
        try:
            reported = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
        except Exception:  # noqa: BLE001
            reported = 0.0
        self._status.stream_fps = reported if 0.0 < reported < 240.0 else 0.0
        self._set_error(None)
        self.log.info(
            "Đã mở camera %s (%s%s)",
            self.camera_id,
            self._status.source_desc,
            f", nguồn báo {self._status.stream_fps:.0f} fps" if self._status.stream_fps else "",
        )
        self._emit_status()
        return True

    def _open_hint(self) -> str:
        cam = self.camera
        if cam.source == "rtsp":
            return (
                "Không kết nối được RTSP: sai mật khẩu/URL? Kiểm tra IP camera, "
                "hoặc thử đổi transport sang UDP trong tab Camera."
            )
        if cam.source == "webcam":
            status = camera_authorization_status()
            if status in {"denied", "restricted"}:
                return compat.webcam_permission_hint()
            if status == "not_determined":
                return compat.webcam_permission_hint()
            # Permission is fine, so the device itself is the problem.
            return (
                "Không mở được webcam: camera đang bị ứng dụng khác giữ "
                "(Zoom/Teams/trình duyệt), đã bị rút, hoặc sai chỉ số/tên thiết bị "
                "trong Cài đặt → Camera test."
            )
        return f"Không đọc được tệp nguồn cho camera '{cam.id}'."

    def _open_rtsp(self) -> cv2.VideoCapture:
        cam = self.camera
        assert cam.url
        # Low-delay defaults keep the picture close to real time: without them
        # FFmpeg buffers whole GOPs, so the live view lags ~3 s behind reality
        # and the backlog never drains (the camera looks "stuck" while it is
        # actually fine). Users can override any key in ``rtsp_options``.
        options: dict[str, str] = {
            "rtsp_transport": cam.transport,
            "fflags": "nobuffer",
            "flags": "low_delay",
            "max_delay": "500000",
            "reorder_queue_size": "0",
        }
        options.update({k: str(v) for k, v in (cam.rtsp_options or {}).items()})
        with _ENV_LOCK:
            saved = _set_ffmpeg_options(options)
            try:
                cap = cv2.VideoCapture(cam.url, cv2.CAP_FFMPEG)
            finally:
                _restore_env(saved)
        if not cap.isOpened():
            cap.release()
            raise RuntimeError("FFmpeg không mở được luồng RTSP")
        # A live stream must be declared dead quickly (a blocked read is what
        # makes the UI freeze for 10+ s), so the read timeout is capped at 5 s
        # even when ``stale_after_s`` is larger.
        read_timeout_ms = int(min(max(cam.stale_after_s, 2.0), 5.0) * 1000)
        try:
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, min(int(cam.stale_after_s * 1000), 8000))
            cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, read_timeout_ms)
        except Exception:  # noqa: BLE001 - unsupported props are fine
            pass
        return cap

    def _open_webcam(self) -> cv2.VideoCapture:
        """Open the local webcam with the best backend for this OS.

        Order comes from :func:`platform_compat.webcam_backend_ids`:
        macOS AVFoundation only (the default fallback can block ~30 s before
        failing, which looks like a hang), Windows DirectShow → Media
        Foundation, Linux V4L2.
        """
        cam = self.camera
        index = resolve_webcam_index(cam.device)
        backends = compat.webcam_backend_ids()
        cap: cv2.VideoCapture | None = None
        opened_with: int | None = None
        for backend in backends:
            candidate = cv2.VideoCapture(index, backend)
            if candidate.isOpened():
                cap, opened_with = candidate, backend
                break
            candidate.release()
        if cap is None and compat.allow_default_webcam_backend():
            candidate = cv2.VideoCapture(index)
            if candidate.isOpened():
                cap, opened_with = candidate, None
            else:
                candidate.release()
        if cap is None:
            status = camera_authorization_status()
            if status == "denied":
                raise RuntimeError(f"{compat.os_label()} từ chối quyền camera")
            raise RuntimeError(
                f"không mở được webcam index {index} (camera đang bận hoặc đã bị rút)"
            )
        width, height = cam.resolution or (1280, 720)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, int(width))
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, int(height))
        if cam.fps:
            cap.set(cv2.CAP_PROP_FPS, int(cam.fps))
        try:
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception:  # noqa: BLE001 - unsupported on some Windows backends
            pass
        self.log.info(
            "Webcam index %d (%dx%d) qua %s",
            index,
            width,
            height,
            compat.webcam_backend_label(opened_with),
        )
        return cap

    def _open_file(self) -> cv2.VideoCapture:
        cam = self.camera
        path = str(cam.url or "")
        if not Path(path).exists():
            raise RuntimeError(f"không thấy tệp {path}")
        cap = cv2.VideoCapture(path)
        if not cap.isOpened():
            cap.release()
            raise RuntimeError(f"không mở được tệp video {path}")
        return cap

    def _open_image(self) -> cv2.VideoCapture:
        cam = self.camera
        path = str(cam.url or "")
        if not Path(path).exists():
            raise RuntimeError(f"không thấy ảnh {path}")
        image = cv2.imread(path)
        if image is None:
            raise RuntimeError(f"không đọc được ảnh {path}")
        self._image_frame = image
        # A still image is re-emitted at the configured rate so the rest of the
        # pipeline (detect_fps, zones, UI) behaves exactly like a live stream.
        return cv2.VideoCapture()  # placeholder, never read from

    def _close_capture(self) -> None:
        cap, self._cap = self._cap, None
        if cap is not None:
            try:
                cap.release()
            except Exception:  # noqa: BLE001
                self.log.debug("release() lỗi", exc_info=True)

    # -- staleness watchdog ----------------------------------------------- #
    def check_stale(self, now: float | None = None) -> bool:
        """Called periodically by the runtime; requests a reconnect if frozen."""
        moment = time.monotonic() if now is None else now
        with self._lock:
            packet = self._latest
        if packet is None or not self._status.connected:
            return False
        if packet.age(moment) > self.camera.stale_after_s:
            self._status.stale = True
            self._mark_lost(
                f"Luồng treo quá {self.camera.stale_after_s:.0f}s",
                "Kết nối camera bị treo — đang tự kết nối lại.",
            )
            return True
        return False


# --------------------------------------------------------------------------- #
# manager
# --------------------------------------------------------------------------- #
class CaptureManager:
    """Owns all capture workers and keeps them in sync with the config."""

    def __init__(self, cameras: Iterable[CameraConfig], *, logger: logging.Logger | None = None) -> None:
        self.log = logger or LOGGER
        self._workers: dict[str, CaptureWorker] = {}
        self._lock = threading.Lock()
        for camera in cameras:
            if camera.enabled:
                self._workers[camera.id] = CaptureWorker(camera, logger=self.log.getChild(camera.id))

    def start(self) -> None:
        for worker in list(self._workers.values()):
            worker.start()

    def stop(self) -> None:
        for worker in list(self._workers.values()):
            worker.stop()
        with self._lock:
            self._workers.clear()

    def get(self, camera_id: str) -> CaptureWorker | None:
        return self._workers.get(camera_id)

    def all(self) -> list[CaptureWorker]:
        return list(self._workers.values())

    def ids(self) -> list[str]:
        return list(self._workers.keys())

    def statuses(self) -> dict[str, dict]:
        return {cid: worker.status().as_dict() for cid, worker in self._workers.items()}

    def check_stale(self) -> list[str]:
        """Run the staleness watchdog; returns the ids that triggered a reopen."""
        stale: list[str] = []
        for camera_id, worker in list(self._workers.items()):
            try:
                if worker.check_stale():
                    stale.append(camera_id)
                    worker.request_reopen()
            except Exception:  # noqa: BLE001
                self.log.debug("watchdog lỗi ở %s", camera_id, exc_info=True)
        return stale

    def sync(self, cameras: Iterable[CameraConfig]) -> list[str]:
        """Apply a hot-reloaded camera list; returns log messages for the UI."""
        messages: list[str] = []
        wanted = {camera.id: camera for camera in cameras if camera.enabled}
        with self._lock:
            for camera_id in list(self._workers.keys()):
                if camera_id not in wanted:
                    self._workers.pop(camera_id).stop()
                    messages.append(f"Đã dừng camera {camera_id}")
            for camera_id, camera in wanted.items():
                existing = self._workers.get(camera_id)
                if existing is None:
                    worker = CaptureWorker(camera, logger=self.log.getChild(camera_id))
                    worker.start()
                    self._workers[camera_id] = worker
                    messages.append(f"Đã thêm camera {camera_id}")
                elif existing.camera.model_dump() != camera.model_dump():
                    existing.camera = camera
                    existing.request_reopen()
                    messages.append(f"Đã cập nhật camera {camera_id}")
        return messages


__all__ = [
    "CaptureManager",
    "camera_authorization_status",
    "request_camera_permission",
    "CaptureStatus",
    "CaptureWorker",
    "FramePacket",
    "WEB_CAM_PERMISSION_HINT",
    "frame_signature",
    "list_webcams",
    "looks_black",
    "motion_score",
    "resolve_webcam_index",
]
