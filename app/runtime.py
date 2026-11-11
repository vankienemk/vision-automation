"""Runtime orchestration: capture → inference → zones → scenarios → MQTT.

Threading model (deliberately simple and leak-free):

=====================  =====================================================
capture threads        one per camera (``app.capture``), keep newest frame
inference threads      one per camera, run at ``detect_fps`` (never per frame)
control thread         ticks the scenario engine, staleness watchdog, and
                       publishes zone states — one loop for all cameras
paho loop thread       MQTT I/O
asyncio (uvicorn)      web UI only, reads immutable snapshots
=====================  =====================================================

The runtime never lets an exception escape into a thread: capture loss, broker
loss, detector loss and config errors degrade to logged events plus a UI state,
because the app must survive days unattended.
"""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import os
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

import numpy as np

from .capture import (
    CaptureManager,
    camera_authorization_status,
    CaptureStatus,
    CaptureWorker,
    FramePacket,
    list_webcams,
    motion_score,
    request_camera_permission,
)
from .config import (
    AppConfig,
    CameraConfig,
    ConfigError,
    ConfigManager,
    ScenarioConfig,
    ZoneConfig,
    default_demo_scenario,
    load_config,
    save_config,
    starter_config,
)
from .detection import Detection, shift_detection
from .detector import Detector, build_detector
from .netcheck import check_camera_source
from . import platform_compat as compat
from .mqtt_client import MqttPublisher, render_payload
from .scenarios import CommandRequest, ScaledClock, ScenarioEngine
from .simulator import Simulator
from .zones import ZoneEngine, ZoneState, zone_crop_box

LOGGER = logging.getLogger(__name__)

BASE_TOPIC_FALLBACK = "vision"


# --------------------------------------------------------------------------- #
# events (log + history ring shared with the web UI)
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class Event:
    ts: float
    kind: str  # zone_enter | zone_exit | command | camera | mqtt | config | error | info
    message: str
    level: str = "info"
    data: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "ts": self.ts,
            "time": datetime.fromtimestamp(self.ts).strftime("%H:%M:%S"),
            "datetime": datetime.fromtimestamp(self.ts).isoformat(timespec="seconds"),
            "kind": self.kind,
            "level": self.level,
            "message": self.message,
            "data": self.data,
        }


class EventBus:
    """Bounded event ring with subscribers (WebSocket clients)."""

    def __init__(self, size: int = 500) -> None:
        self._events: deque[Event] = deque(maxlen=max(50, size))
        self._lock = threading.Lock()
        self._subscribers: list[Callable[[Event], None]] = []
        self._seq = 0

    def add(self, kind: str, message: str, level: str = "info", **data: Any) -> Event:
        event = Event(ts=time.time(), kind=kind, message=message, level=level, data=data)
        with self._lock:
            self._seq += 1
            self._events.append(event)
            subscribers = list(self._subscribers)
        for callback in subscribers:
            try:
                callback(event)
            except Exception:  # noqa: BLE001
                LOGGER.debug("Event subscriber lỗi", exc_info=True)
        return event

    def subscribe(self, callback: Callable[[Event], None]) -> None:
        with self._lock:
            self._subscribers.append(callback)

    def unsubscribe(self, callback: Callable[[Event], None]) -> None:
        with self._lock:
            if callback in self._subscribers:
                self._subscribers.remove(callback)

    def recent(self, *, kind: str | None = None, level: str | None = None, limit: int = 100) -> list[dict]:
        with self._lock:
            events = list(self._events)
        if kind:
            events = [e for e in events if e.kind == kind]
        if level:
            events = [e for e in events if e.level == level]
        return [event.as_dict() for event in events][-limit:]

    @property
    def seq(self) -> int:
        return self._seq


# --------------------------------------------------------------------------- #
# per camera runtime
# --------------------------------------------------------------------------- #
@dataclass
class CameraRuntimeState:
    """Latest visual + inference state of one camera (read by the web layer)."""

    camera_id: str
    detections: list[Detection] = field(default_factory=list)
    zones: dict[str, ZoneState] = field(default_factory=dict)
    inference_ms: float = 0.0
    infer_fps: float = 0.0
    last_infer_at: float | None = None
    last_frame_seq: int = 0
    frame_width: int = 0
    frame_height: int = 0
    motion_static: bool = False
    effective_detect_fps: float = 0.0
    lost_handled: bool = False
    frame_age_ms: float = 0.0
    sampled_frame: bool = True


MEDIAMTX_LOOPBACK_CONFIG = """\
logLevel: info
rtsp: yes
rtspAddress: :8554
rtspTransports: [tcp]
rtspEncryption: "no"
rtmp: no
hls: no
webrtc: no
srt: no
moq: no
api: no
metrics: no
pprof: no
playback: no
paths:
  all_others:
"""


class LoopbackStream:
    """Optional RTSP loopback: ffmpeg (webcam → RTSP) + MediaMTX server.

    The goal is to feed the *same* webcam into the exact RTSP code path, so the
    production pipeline can be tested end-to-end.  Only one process may own the
    webcam, therefore the loopback publishes to ``rtsp://127.0.0.1:8554/laptop``
    and the ``cam_test_laptop`` camera must then read from that URL.
    """

    def __init__(self, testing_cfg, *, logger: logging.Logger | None = None) -> None:
        self.cfg = testing_cfg
        self.log = logger or LOGGER.getChild("loopback")
        self._mediamtx: subprocess.Popen | None = None
        self._ffmpeg: subprocess.Popen | None = None
        self._log_dir = Path("logs")

    # -- availability ----------------------------------------------------- #
    def _resolve_bin(self, configured: str, which_name: str) -> str | None:
        """Resolve a configured binary: explicit path first, then ``PATH``.

        A configured-but-missing path must NOT report "ready" — otherwise the UI
        says the loopback is available and then ``Popen`` blows up with
        ``[Errno 2] No such file or directory`` on start. Windows binaries are
        resolved with the ``.exe`` suffix by :mod:`app.platform_compat`.
        """
        return compat.resolve_executable(configured, which_name)

    def ffmpeg_path(self) -> str | None:
        return self._resolve_bin(self.cfg.ffmpeg_bin, "ffmpeg")

    def mediamtx_path(self) -> str | None:
        return self._resolve_bin(self.cfg.mediamtx_bin, "mediamtx")

    def available(self) -> tuple[bool, str]:
        missing = []
        if not self.ffmpeg_path():
            missing.append("ffmpeg")
        if not self.mediamtx_path():
            missing.append("mediamtx")
        if missing:
            return (
                False,
                "Thiếu " + " và ".join(missing) + ". " + compat.loopback_install_hint(),
            )
        return True, "Sẵn sàng"

    def running(self) -> bool:
        return bool(
            (self._mediamtx and self._mediamtx.poll() is None)
            or (self._ffmpeg and self._ffmpeg.poll() is None)
        )

    # -- lifecycle -------------------------------------------------------- #
    def _launch_ffmpeg(
        self,
        rate: int,
        width: int,
        height: int,
        device_index: int,
        fps: int,
        device_name: str = "",
    ) -> None:
        """Start ffmpeg publishing the webcam to :attr:`TestingConfig.webcam_rtsp_url`.

        The argv comes from :func:`app.platform_compat.build_loopback_ffmpeg_args`:
        macOS uses AVFoundation + ``h264_videotoolbox``, Windows DirectShow +
        ``libx264``, Linux V4L2 + ``libx264``.
        """
        self._ffmpeg_log = self._log_dir / "loopback-ffmpeg.log"
        handle = open(self._ffmpeg_log, "wb")  # noqa: SIM115 - kept by Popen
        self._ffmpeg_err = handle
        args = compat.build_loopback_ffmpeg_args(
            url=self.cfg.webcam_rtsp_url,
            device_index=device_index,
            device_name=device_name,
            rate=rate,
            width=width,
            height=height,
            fps=fps,
            binary=str(self.ffmpeg_path()),
        )
        self.log.info("ffmpeg loopback: %s", " ".join(args))
        self._ffmpeg = subprocess.Popen(  # noqa: S603 - configured binary
            args,
            stdout=subprocess.DEVNULL,
            stderr=handle,
        )

    def _close_ffmpeg_log(self) -> None:
        handle = getattr(self, "_ffmpeg_err", None)
        if handle is not None:
            try:
                handle.close()
            except Exception:  # noqa: BLE001
                pass
            self._ffmpeg_err = None

    def start(
        self,
        *,
        device_index: int,
        resolution: tuple[int, int],
        fps: int,
        device_name: str = "",
    ) -> tuple[bool, str]:
        ok, message = self.available()
        if not ok:
            return False, message
        if self.running():
            return True, "Loopback đang chạy"
        self._log_dir.mkdir(parents=True, exist_ok=True)
        width, height = resolution
        try:
            # A generated minimal config keeps MediaMTX on RTSP only: the bundled
            # default also opens HTTP/HLS/WebRTC ports that may already be taken
            # ("listen udp :8000: bind: address already in use").
            config_path = self._log_dir / "mediamtx-loopback.yml"
            config_path.write_text(MEDIAMTX_LOOPBACK_CONFIG, encoding="utf-8")
            self._mediamtx_log = self._log_dir / "loopback-mediamtx.log"
            mtx_handle = open(self._mediamtx_log, "wb")  # noqa: SIM115 - kept by Popen
            self._mediamtx_err = mtx_handle
            self._mediamtx = subprocess.Popen(  # noqa: S603 - configured binary
                [str(self.mediamtx_path()), str(config_path.resolve())],
                stdout=mtx_handle,
                stderr=mtx_handle,
                # run inside logs/ so MediaMTX drops its generated TLS files there
                # instead of polluting the project root (auto.crt / auto.key)
                cwd=str(self._log_dir.resolve()),
            )
            time.sleep(1.5)  # let the RTSP server bind :8554
            if self._mediamtx.poll() is not None:
                detail = self._read_tail(self._mediamtx_log)
                self.stop()
                message = "MediaMTX không khởi động được (cổng 8554 bận?)"
                if detail:
                    message = f"{message}. Chi tiết: {detail}"
                return False, message
        except Exception as exc:  # noqa: BLE001 - missing tools
            self.stop()
            return False, f"Không chạy được MediaMTX: {exc}"

        # Camera devices refuse frame rates they do not expose (the macOS FaceTime
        # HD camera only offers 30 fps at 1280x720), so try the configured rate and
        # fall back to the rates the device/driver actually supports.
        candidates: list[int] = [] if compat.is_macos() else compat.loopback_rate_candidates(fps)
        if not candidates:
            candidates = [max(1, fps)]
        tried: list[int] = []
        last_error = ""
        for _attempt in range(4):
            pending = [rate for rate in candidates if rate not in tried]
            if not pending:
                break
            rate = pending[0]
            tried.append(rate)
            try:
                self._launch_ffmpeg(rate, width, height, device_index, fps, device_name)
            except Exception as exc:  # noqa: BLE001 - missing binary / busy device
                self.stop()
                return False, f"Không chạy được ffmpeg: {exc}"
            time.sleep(2.5)
            assert self._ffmpeg is not None
            if self._ffmpeg.poll() is None:
                self.log.info(
                    "Loopback RTSP chạy tại %s (nguồn %dx%d@%s fps → %s fps, %s)",
                    self.cfg.webcam_rtsp_url,
                    width,
                    height,
                    rate,
                    fps,
                    compat.os_label(),
                )
                return True, f"Đang phát webcam tại {self.cfg.webcam_rtsp_url}"
            last_error = self._ffmpeg_error_tail()
            full_log = self._read_tail(getattr(self, "_ffmpeg_log", None), lines=200)
            self._close_ffmpeg_log()
            self._ffmpeg = None
            # macOS ffmpeg prints a "Supported modes" list we can mine for the
            # rates the device really offers.
            for value in compat.parse_supported_rates(full_log, width, height):
                rate_value = max(1, int(round(value)))
                if rate_value not in candidates:
                    self.log.warning(
                        "Webcam không hỗ trợ %s fps, thử lại với %s fps", rate, rate_value
                    )
                    candidates.append(rate_value)
        self.stop()
        hint = (
            "ffmpeg không publish được webcam. Camera có thể đang bị app khác giữ "
            "(đóng Zoom/Teams/Camera rồi thử lại)."
        )
        if last_error:
            hint = f"{hint} Chi tiết: {last_error}"
        return False, hint

    def stop(self) -> None:
        for proc in (self._ffmpeg, self._mediamtx):
            if proc is not None and proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=5)
                except Exception:  # noqa: BLE001
                    try:
                        proc.kill()
                    except Exception:  # noqa: BLE001
                        pass
        self._ffmpeg = None
        self._mediamtx = None
        self._close_ffmpeg_log()
        mtx_handle = getattr(self, "_mediamtx_err", None)
        if mtx_handle is not None:
            try:
                mtx_handle.close()
            except Exception:  # noqa: BLE001
                pass
            self._mediamtx_err = None

    def _read_tail(self, path, lines: int = 3) -> str:
        if path is None:
            return ""
        try:
            text = Path(path).read_text(errors="replace").strip()
        except Exception:  # noqa: BLE001
            return ""
        if not text:
            return ""
        tail = [line for line in text.splitlines() if line.strip()][-lines:]
        return " | ".join(tail)[:400]

    def _ffmpeg_error_tail(self, lines: int = 3) -> str:
        """Last useful ffmpeg stderr lines, for a Vietnamese error message."""
        path = getattr(self, "_ffmpeg_log", None)
        if path is None:
            return ""
        try:
            text = Path(path).read_text(errors="replace").strip()
        except Exception:  # noqa: BLE001
            return ""
        if not text:
            return ""
        tail = [line for line in text.splitlines() if line.strip()][-lines:]
        return " | ".join(tail)[:400]


# --------------------------------------------------------------------------- #
# the runtime
# --------------------------------------------------------------------------- #
class Runtime:
    """Wires everything together and exposes a thread-safe state snapshot."""

    def __init__(
        self,
        config_manager: ConfigManager,
        *,
        logger: logging.Logger | None = None,
        dry_run: bool | None = None,
        with_webcam: bool = False,
        enable_detector: bool = True,
    ) -> None:
        self.config_manager = config_manager
        cfg = config_manager.current
        self._dry_run_override: bool | None = None if dry_run is None else bool(dry_run)
        if dry_run is not None:
            cfg.mqtt.dry_run = bool(dry_run)
        self.log = logger or LOGGER.getChild("runtime")
        self.events = EventBus(cfg.logging.event_ring)
        self.clock = ScaledClock(_MonotonicClock(), 1.0)
        self.simulator = Simulator(self.clock, logger=self.log.getChild("sim"))
        self.scenario_engine = ScenarioEngine(self.config.scenarios, self.clock)
        self.capture = CaptureManager(self.config.cameras, logger=self.log.getChild("capture"))
        self.loopback = LoopbackStream(self.config.testing, logger=self.log.getChild("loopback"))
        self.detector: Detector | None = None
        self.mqtt = MqttPublisher(
            self.config.mqtt,
            logger=self.log.getChild("mqtt"),
            on_event=lambda event: self._on_mqtt_event(event),
            on_manual_override=self.notify_manual_override,
        )
        self.detector_enabled = enable_detector
        self._cameras: dict[str, CameraRuntimeState] = {}
        self._cameras_lock = threading.RLock()
        self._zone_engines: dict[str, ZoneEngine] = {}
        self._infer_threads: dict[str, threading.Thread] = {}
        self._infer_stops: dict[str, threading.Event] = {}
        self._stop = threading.Event()
        #: Set by ``POST /api/system/shutdown`` (or a Windows ``CTRL_BREAK``)
        #: so a script/Task Scheduler job can stop the app gracefully.
        self._shutdown_requested = threading.Event()
        self._control_thread: threading.Thread | None = None
        self._started_at = time.time()
        self._lost_forced: dict[str, set[str]] = {}
        self._published_states: dict[str, str] = {}
        self._infer_marks: dict[str, deque[float]] = {}
        self._webcam_cache: tuple[float, list[dict]] | None = None
        self._platform_cache: tuple[float, dict] | None = None
        self._zone_occupied_last: dict[str, bool | None] = {}
        self._state_since_wall: dict[str, float] = {}
        self._last_custom_loss: dict[str, float] = {}
        self._log_handler: logging.Handler | None = None
        self._webcam_requested = with_webcam
        self._lock = threading.RLock()
        # Chẩn đoán: lỗi suy luận theo camera + nhật ký quyết định gần nhất.
        self._infer_errors: dict[str, int] = {}
        self._infer_consecutive_errors: dict[str, int] = {}
        self._infer_last_error: dict[str, str] = {}
        self._decisions: deque[dict[str, Any]] = deque(maxlen=80)

    # -- accessors -------------------------------------------------------- #
    @property
    def config(self) -> AppConfig:
        """Always the manager's current config (hot reload can happen anytime)."""
        return self.config_manager.current

    @property
    def dry_run(self) -> bool:
        return self.mqtt.dry_run

    @dry_run.setter
    def dry_run(self, value: bool) -> None:
        self._dry_run_override = bool(value)
        self.mqtt.dry_run = bool(value)
        try:
            self.config.mqtt.dry_run = bool(value)
        except Exception:  # noqa: BLE001 - config is best-effort here
            pass
        self.events.add(
            "config",
            f"Dry-run {'BẬT' if value else 'TẮT'}",
            level="warning" if value else "info",
        )

    def camera_config(self, camera_id: str) -> CameraConfig | None:
        return self.config.camera(camera_id)

    def zone_engine(self, camera_id: str) -> ZoneEngine | None:
        return self._zone_engines.get(camera_id)

    def zone_engines(self) -> dict[str, ZoneEngine]:
        return self._zone_engines

    # -- logging ---------------------------------------------------------- #
    def _install_log_handler(self) -> None:
        """Mirror log records into the event bus so the UI can stream them."""
        root = logging.getLogger("app")

        class _Bridge(logging.Handler):
            def __init__(self, bus: EventBus) -> None:
                super().__init__()
                self.bus = bus

            def emit(self, record: logging.LogRecord) -> None:
                try:
                    level = record.levelname.lower()
                    kind = "error" if record.levelno >= logging.ERROR else "info"
                    self.bus.add(kind, record.getMessage(), level=level, logger=record.name)
                except Exception:  # noqa: BLE001
                    pass

        handler = _Bridge(self.events)
        handler.setLevel(logging.INFO)
        root.addHandler(handler)
        self._log_handler = handler

    # -- lifecycle -------------------------------------------------------- #
    def start(self) -> None:
        self._install_log_handler()
        self.log.info("Khởi động vision-automation (cấu hình %s)", self.config_manager.path)
        self._sync_override_topics()
        self._rebuild_engines()
        self.simulator.bind_engines(lambda: self._zone_engines)

        if self._webcam_requested:
            self.ensure_test_camera(requested=True)
        elif self.config.testing.auto_webcam and any(c.source == "webcam" for c in self.config.cameras):
            self.log.info("Camera webcam có trong cấu hình — không tự bật (mở khi có người xem)")

        if self.detector_enabled:
            self.detector = build_detector(self.config.detector, self.log.getChild("detector"))
            info = self.detector.info()
            self.events.add(
                "info",
                f"Model {info.get('model')} trên thiết bị {info.get('device')}",
                stats=info.get("stats", {}),
            )
        else:
            self.log.info("Detector bị tắt (--no-detector) — chỉ chạy camera + UI")

        # macOS camera permission must be requested from the main thread, before
        # any capture thread opens the device. It also sets
        # OPENCV_AVFOUNDATION_SKIP_AUTH so worker threads never try to prompt.
        if self._webcam_requested or any(c.source == "webcam" for c in self.config.cameras):
            granted, message = request_camera_permission()
            self.events.add("camera", message, level="info" if granted else "warning")

        self.capture.start()
        self.mqtt.start()
        self.config_manager.add_listener(self._on_config_change)

        # ``ensure_test_camera`` runs earlier in ``start()`` — before the config
        # listener exists — so nothing rebuilt the zone/scenario engines for the
        # camera it appended. Without this second rebuild a first run with
        # ``--with-webcam`` would capture frames but never evaluate zones or run
        # the auto-created test scenario.
        self._rebuild_engines()
        self.scenario_engine.reconfigure(self.config.scenarios)
        self.simulator.bind_engines(lambda: self._zone_engines)

        for camera in self.config.cameras:
            if camera.enabled:
                self._start_inference(camera.id)

        self._stop.clear()
        self._control_thread = threading.Thread(target=self._control_loop, name="control", daemon=True)
        self._control_thread.start()
        self.events.add("info", "Hệ thống đã sẵn sàng")

    def stop(self) -> None:
        self.log.info("Đang dừng hệ thống…")
        self._stop.set()
        if self._control_thread is not None:
            self._control_thread.join(timeout=5)
            self._control_thread = None
        for event in list(self._infer_stops.values()):
            event.set()
        for thread in list(self._infer_threads.values()):
            if thread.is_alive():
                thread.join(timeout=3)
        self._infer_threads.clear()
        self._infer_stops.clear()
        self.capture.stop()
        self.loopback.stop()
        try:
            self.mqtt.stop(publish_offline=True)
        except Exception:  # noqa: BLE001
            self.log.debug("Đóng MQTT lỗi", exc_info=True)
        if self.detector is not None:
            try:
                self.detector.close()
            except Exception:  # noqa: BLE001
                pass
        if self._log_handler is not None:
            logging.getLogger("app").removeHandler(self._log_handler)
            self._log_handler = None
        self.log.info("Đã dừng")

    # -- engine wiring ---------------------------------------------------- #
    def _sync_override_topics(self) -> None:
        """Bảng ``state_topic -> zone_ref`` cho tính năng "tạm dừng khi chỉnh tay".

        Trước đây ``manual_override`` có trong config nhưng KHÔNG được nối: bấm
        công tắc tường thì app vẫn ghi đè. Nay mỗi ``manual_override.state_topic``
        của kịch bản được subscribe và chuyển thành lệnh tạm dừng.
        """
        mapping: dict[str, str] = {}
        for scenario in self.config.scenarios:
            override = scenario.manual_override
            if override is None or not override.state_topic:
                continue
            mapping[override.state_topic] = scenario.zone
        try:
            self.mqtt.set_override_topics(mapping)
        except Exception:  # noqa: BLE001 - không được làm hỏng hot reload
            self.log.warning("Không đặt được topic theo dõi chỉnh tay", exc_info=True)

    def _rebuild_engines(self) -> None:
        engines: dict[str, ZoneEngine] = {}
        for camera in self.config.cameras:
            if not camera.enabled:
                continue
            engine = self._zone_engines.get(camera.id)
            if engine is None:
                engine = ZoneEngine(camera.id, camera.zones, self.config.detector.min_confidence)
            else:
                engine.global_min_confidence = self.config.detector.min_confidence
                wanted = {zone.id: zone for zone in camera.zones}
                for zone_id in list(engine.zone_ids()):
                    if zone_id not in wanted:
                        engine.remove_zone(zone_id)
                for zone in camera.zones:
                    engine.add_zone(zone)
            engines[camera.id] = engine
        self._zone_engines = engines
        with self._cameras_lock:
            for camera_id in engines:
                state = self._cameras.get(camera_id)
                if state is None:
                    self._cameras[camera_id] = CameraRuntimeState(camera_id=camera_id)
                else:
                    camera = self.config.camera(camera_id)
                    valid = {zone.id for zone in (camera.zones if camera else [])}
                    state.zones = {zid: zs for zid, zs in state.zones.items() if zid in valid}

    # -- inference -------------------------------------------------------- #
    def _start_inference(self, camera_id: str) -> None:
        if not self.detector_enabled:
            return
        existing = self._infer_threads.get(camera_id)
        if existing is not None and existing.is_alive():
            return
        stop_event = threading.Event()
        thread = threading.Thread(
            target=self._inference_loop,
            args=(camera_id, stop_event),
            name=f"infer-{camera_id}",
            daemon=True,
        )
        self._infer_threads[camera_id] = thread
        self._infer_stops[camera_id] = stop_event
        thread.start()

    def _stop_inference(self, camera_id: str) -> None:
        event = self._infer_stops.pop(camera_id, None)
        if event is not None:
            event.set()
        thread = self._infer_threads.pop(camera_id, None)
        if thread is not None and thread.is_alive():
            thread.join(timeout=2)

    def _sync_inference(self, cfg: AppConfig) -> None:
        """Start/stop one inference thread per *enabled* camera.

        Runs on every hot reload: a camera added in the Web UI must get a
        detector thread immediately, and a removed/disabled camera must not
        leave a thread spinning forever.
        """
        if not self.detector_enabled:
            return
        enabled = {camera.id for camera in cfg.cameras if camera.enabled}
        for camera_id in enabled:
            self._start_inference(camera_id)
        for camera_id in set(self._infer_threads) - enabled:
            self.log.info("Dừng luồng suy luận %s (camera đã tắt hoặc bị xoá)", camera_id)
            self._stop_inference(camera_id)

    def _pause(self, stop_event: threading.Event, seconds: float) -> None:
        """Sleep that wakes up as soon as either stop flag is set."""
        deadline = time.monotonic() + max(0.0, seconds)
        while not self._stop.is_set() and not stop_event.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            self._stop.wait(min(remaining, 0.05))

    def _inference_loop(self, camera_id: str, stop_event: threading.Event | None = None) -> None:
        stop_event = stop_event or threading.Event()
        self.log.info("Luồng suy luận %s bắt đầu", camera_id)
        last_seq = 0
        last_infer = 0.0
        fps_marks: deque[float] = deque(maxlen=30)
        prev_signature: np.ndarray | None = None
        while not self._stop.is_set() and not stop_event.is_set():
            try:
                worker = self.capture.get(camera_id)
                if worker is None:
                    self._pause(stop_event, 1.0)
                    continue
                packet = worker.new_frame(last_seq)
                if packet is None:
                    self._pause(stop_event, 0.02)
                    continue

                camera = self.config.camera(camera_id)
                if camera is None:
                    self._pause(stop_event, 0.5)
                    continue

                detect_fps = max(0.5, float(camera.detect_fps or self.config.detector.detect_fps))
                signature = worker.signature()
                is_static = False
                if self.config.detector.motion_gating:
                    score = motion_score(prev_signature, signature) if signature is not None else 255.0
                    is_static = score < self.config.detector.motion_threshold
                    if is_static and self._camera_is_vacant(camera_id):
                        # Never slow down while somebody is present: a still person
                        # must still be detected, otherwise the light would blink.
                        detect_fps = max(0.2, float(self.config.detector.idle_detect_fps))
                prev_signature = signature

                interval = 1.0 / detect_fps
                now_real = time.monotonic()
                if now_real - last_infer < interval:
                    self._pause(stop_event, min(interval - (now_real - last_infer), 0.05))
                    continue
                last_infer = now_real
                last_seq = packet.seq

                # Khung hình đã cũ (suy luận chậm/trễ nhịp) thì BỎ, chờ khung mới:
                # nếu không, một khung vài giây trước sẽ được coi là "bây giờ".
                frame_age = packet.age()
                if frame_age > max(0.5, 2.0 / detect_fps):
                    self.log.debug(
                        "Bỏ khung cũ %.2fs của %s (chậm hơn nhịp %.1f fps)",
                        frame_age,
                        camera_id,
                        detect_fps,
                    )
                    self._pause(stop_event, 0.02)
                    continue

                detections: list[Detection] = []
                frame = packet.frame
                offset_x = offset_y = 0
                if camera.crop_to_zones or self.config.detector.crop_to_zones:
                    # Crop to the union of the zones: fewer pixels to scan, and a
                    # person standing in the zone occupies more of the input
                    # (better small-object recall). A slice is a view: no copy.
                    crop = zone_crop_box(camera.zones, packet.width, packet.height)
                    if crop is not None:
                        x1, y1, x2, y2 = crop
                        frame = packet.frame[y1:y2, x1:x2]
                        offset_x, offset_y = x1, y1
                started = time.perf_counter()
                # ``sampled`` = khung hình này CÓ kết quả suy luận đáng tin. Khi
                # model lỗi/chưa sẵn sàng, danh sách rỗng KHÔNG được hiểu là
                # "phòng không có ai" — nếu không đèn sẽ tắt oan sau exit_grace_s.
                sampled = True
                try:
                    if self.detector is None or not getattr(self.detector, "available", True):
                        detections = []
                        sampled = False
                    else:
                        # Ngưỡng gửi xuống model phải là ngưỡng THẤP NHẤT mà một
                        # vùng có thể cần; nếu chỉ dùng ngưỡng chung thì kéo
                        # "độ tin cậy tối thiểu" của vùng xuống thấp hơn sẽ
                        # KHÔNG có tác dụng (box đã bị model lọc mất trước đó).
                        zone_floor = [
                            zone.min_confidence
                            for zone in camera.zones
                            if zone.enabled and zone.min_confidence is not None
                        ]
                        conf_hint = self.config.detector.min_confidence
                        if zone_floor:
                            conf_hint = min(conf_hint, *zone_floor)
                        detections = self.detector.detect(frame, confidence=conf_hint)
                        if offset_x or offset_y:
                            detections = [shift_detection(d, offset_x, offset_y) for d in detections]
                        self._infer_consecutive_errors[camera_id] = 0
                except Exception as exc:  # noqa: BLE001 - never kill the loop
                    self.log.error("Suy luận lỗi ở %s: %s", camera_id, exc, exc_info=True)
                    self.events.add(
                        "error",
                        f"Suy luận lỗi ở {camera_id}: {exc} — vùng giữ nguyên trạng thái "
                        "gần nhất (không coi là vắng)",
                        level="error",
                    )
                    detections = []
                    sampled = False
                    self._note_inference_error(camera_id, exc)
                elapsed_ms = (time.perf_counter() - started) * 1000.0

                self._process_detections(
                    camera, packet, detections, elapsed_ms, detect_fps, is_static, sampled=sampled
                )
                fps_marks.append(time.monotonic())
            except Exception:  # noqa: BLE001 - a detector hiccup must not kill the thread
                self.log.exception("Luồng suy luận %s lỗi — tự thử lại sau 1s", camera_id)
                self.events.add(
                    "error",
                    f"Luồng suy luận {camera_id} gặp lỗi — đang tự phục hồi",
                    level="error",
                )
                self._stop.wait(1.0)

        self.log.info("Luồng suy luận %s kết thúc", camera_id)

    def _note_inference_error(self, camera_id: str, exc: BaseException) -> None:
        """Ghi nhận lỗi suy luận (để hiện trên bảng chẩn đoán)."""
        with self._cameras_lock:
            self._infer_errors[camera_id] = self._infer_errors.get(camera_id, 0) + 1
            self._infer_consecutive_errors[camera_id] = (
                self._infer_consecutive_errors.get(camera_id, 0) + 1
            )
            self._infer_last_error[camera_id] = f"{type(exc).__name__}: {exc}"[:400]

    def _record_decision(self, **payload: Any) -> None:
        """Lưu một dòng "vì sao app quyết định như vậy" cho UI (mục Giải thích quyết định)."""
        entry = {"ts": time.time(), "time": _iso(time.time())}
        entry.update(payload)
        with self._cameras_lock:
            self._decisions.append(entry)

    def decisions(self, limit: int = 30) -> list[dict[str, Any]]:
        with self._cameras_lock:
            return list(self._decisions)[-limit:]

    def notify_manual_override(self, zone_ref: str) -> None:
        """Người dùng vừa chỉnh tay thiết bị → tạm dừng tự động cho vùng đó."""
        self.scenario_engine.notify_manual_override(zone_ref)
        pause = max(
            (runner.status().get("paused_remaining_s", 0.0) for runner in self.scenario_engine.runners()),
            default=0.0,
        )
        if pause:
            self.events.add(
                "info",
                f"Phát hiện chỉnh tay ở {zone_ref} — tạm dừng tự động {pause:.0f}s",
                zone=zone_ref,
            )
            self._record_decision(
                kind="manual_override",
                message=f"Thấy thiết bị của {zone_ref} bị chỉnh tay → tạm dừng kịch bản {pause:.0f}s",
                zone=zone_ref,
            )

    def _camera_is_vacant(self, camera_id: str) -> bool:
        with self._cameras_lock:
            state = self._cameras.get(camera_id)
            if state is None or not state.zones:
                return False
            return all(zone.occupied is not True for zone in state.zones.values())

    def _process_detections(
        self,
        camera: CameraConfig,
        packet: FramePacket,
        detections: list[Detection],
        elapsed_ms: float,
        detect_fps: float,
        is_static: bool,
        *,
        sampled: bool = True,
    ) -> None:
        engine = self._zone_engines.get(camera.id)
        if engine is None:
            return
        now = self.clock.now()
        states, changes = engine.transitions(
            now, detections, packet.width, packet.height, sample=sampled
        )

        with self._cameras_lock:
            state = self._cameras.setdefault(camera.id, CameraRuntimeState(camera_id=camera.id))
            state.detections = list(detections)
            state.inference_ms = elapsed_ms
            state.last_infer_at = time.time()
            state.frame_width = packet.width
            state.frame_height = packet.height
            state.motion_static = is_static
            state.effective_detect_fps = detect_fps
            state.last_frame_seq = packet.seq
            state.frame_age_ms = packet.age() * 1000.0
            state.sampled_frame = sampled
            state.zones = {zone_state.zone_id: zone_state for zone_state in states}

        marks = self._infer_marks.setdefault(camera.id, deque(maxlen=20))
        marks.append(time.monotonic())
        if len(marks) > 1:
            span = marks[-1] - marks[0]
            with self._cameras_lock:
                self._cameras[camera.id].infer_fps = (len(marks) - 1) / span if span > 0 else 0.0

        self._feed_zone_states(camera, states, sampled=sampled)

    # -- commands --------------------------------------------------------- #
    def _feed_zone_states(
        self, camera: CameraConfig, states: Iterable[ZoneState], *, sampled: bool = True
    ) -> None:
        """Emit events, feed the scenario engine and publish retained state."""
        for zone_state in states:
            if sampled:
                self._emit_zone_event(zone_state)
            for command in self.scenario_engine.on_zone_state(zone_state, sampled=sampled):
                self._publish_command(command, camera)
            if sampled:
                self._publish_zone_state(camera, zone_state)

    def _emit_zone_event(self, zone_state: ZoneState) -> None:
        ref = zone_state.ref
        previous = self._zone_occupied_last.get(ref)
        self._zone_occupied_last[ref] = zone_state.occupied
        if zone_state.occupied is None:
            return
        if previous == zone_state.occupied:
            return
        if previous is None and zone_state.occupied is False:
            # Boot/first observation of a vacant zone: stay silent so a "vùng đã
            # trống" never appears before any "có người vào".
            return
        if zone_state.occupied:
            self.events.add(
                "zone_enter",
                f"Có người vào vùng '{zone_state.name}' (đếm {zone_state.count})",
                level="info",
                zone=ref,
                count=zone_state.count,
                class_counts=dict(zone_state.class_counts),
            )
        else:
            self.events.add(
                "zone_exit",
                f"Vùng '{zone_state.name}' đã trống",
                level="info",
                zone=ref,
            )

    def _publish_command(self, command: CommandRequest, camera: CameraConfig) -> None:
        scenario = self.config.scenario(command.scenario_id)
        action = command.action
        if not action.enabled:
            return
        mode = scenario.effective_test_mode(camera.is_test) if scenario else "normal"
        topic = action.topic
        if mode in {"skip", "disabled"}:
            self.events.add(
                "command", f"Bỏ qua lệnh {topic} (kịch bản tắt)", data=command.context
            )
            return
        if mode == "prefix":
            prefix = self.config.testing.test_topic_prefix
            if not topic.startswith(prefix):
                topic = prefix + topic
        dry_run = self.mqtt.dry_run or mode == "dry_run"
        payload = render_payload(action.payload, command.context or {})
        if dry_run:
            ok = self.mqtt.publish_dry_run(
                topic, payload, command, qos=action.qos, retain=action.retain
            )
        else:
            ok = self.mqtt.publish(
                topic,
                payload,
                qos=action.qos,
                retain=action.retain,
                key=f"scenario:{command.scenario_id}:{topic}",
                scenario_id=command.scenario_id,
                context=command.context,
            )
        rendered = _short_payload(payload)
        self._record_decision(
            kind="command",
            message=(
                f"{'[DRY-RUN] ' if dry_run else ''}Gửi “{command.step_label or command.trigger}” tới {topic} "
                f"vì {_trigger_reason(command)}"
            ),
            zone=command.zone_ref,
            scenario=command.scenario_id,
            topic=topic,
            payload=payload,
            ok=ok or dry_run,
            dry_run=dry_run,
        )
        self.events.add(
            "command",
            f"{'[DRY-RUN] ' if dry_run else ''}{scenario.name if scenario else command.scenario_id} → {topic} {rendered}",
            level="info" if ok or dry_run else "error",
            scenario=command.scenario_id,
            topic=topic,
            payload=payload,
            ok=ok,
            dry_run=dry_run,
            trigger=command.trigger,
            zone=command.zone_ref,
        )

    def _publish_zone_state(self, camera: CameraConfig, zone_state: ZoneState) -> None:
        if not self.config.mqtt.publish_zone_state:
            return
        camera_cfg = self.config.camera(zone_state.camera_id)
        if camera_cfg is None:
            return
        zone_cfg = next((z for z in camera_cfg.zones if z.id == zone_state.zone_id), None)
        if zone_cfg is None or not zone_cfg.publish_state or not zone_cfg.enabled:
            return
        if zone_state.occupied is None:
            return  # unknown: never publish a spurious "vacant"
        # "since" must be wall-clock (the scenario clock is monotonic and can be
        # scaled during simulation), so track the transition time ourselves.
        key = zone_state.ref
        signature = f"{bool(zone_state.occupied)}:{zone_state.count}"
        previous = self._published_states.get(key)
        if previous == signature:
            return
        self._published_states[key] = signature
        previous_occupancy = previous.split(":", 1)[0] if previous else None
        if previous_occupancy != signature.split(":", 1)[0]:
            self._state_since_wall[key] = time.time()
        since_wall = self._state_since_wall.setdefault(key, time.time())
        topic = zone_cfg.state_topic or f"{self.config.mqtt.base_topic}/{zone_state.camera_id}/{zone_state.zone_id}/state"
        payload = {
            "occupied": bool(zone_state.occupied),
            "count": int(zone_state.count),
            "class_counts": dict(zone_state.class_counts),
            "since": _iso(since_wall),
            "zone": zone_state.zone_id,
            "camera": zone_state.camera_id,
        }
        self.mqtt.publish_zone_state(topic, payload)

    # -- control loop ----------------------------------------------------- #
    def _control_loop(self) -> None:
        tick_interval = 0.2
        last_status: dict[str, bool] = {}
        while not self._stop.is_set():
            try:
                started = time.perf_counter()
                for command in self.scenario_engine.tick():
                    camera = self.config.camera(command.camera_id)
                    if camera is not None:
                        self._publish_command(command, camera)
                self._propagate_forced_states()
                for camera_id in self.capture.check_stale():
                    self.events.add("camera", f"Camera {camera_id} bị treo — đang kết nối lại", level="warning")
                self._handle_camera_health(last_status)
                elapsed = time.perf_counter() - started
                self._stop.wait(max(0.05, tick_interval - elapsed))
            except Exception as exc:  # noqa: BLE001 - control loop must survive
                self.log.error("Lỗi vòng điều khiển: %s", exc, exc_info=True)
                self._stop.wait(1.0)

    def _propagate_forced_states(self) -> None:
        """Advance simulated / fail-safe zones even without fresh frames."""
        now = self.clock.now()
        for camera in self.config.cameras:
            engine = self._zone_engines.get(camera.id)
            if engine is None or not engine.forced_zones():
                continue
            states = engine.evaluate_forced(now)
            if states:
                # Mirror into the runtime snapshot so the UI shows the simulated
                # state even when the camera has no frames (e.g. the webcam is
                # closed because nobody is watching the stream).
                with self._cameras_lock:
                    state = self._cameras.setdefault(camera.id, CameraRuntimeState(camera_id=camera.id))
                    merged = dict(state.zones)
                    merged.update({zone_state.zone_id: zone_state for zone_state in states})
                    state.zones = merged
            self._feed_zone_states(camera, states)

    def _handle_camera_health(self, last_status: dict[str, bool]) -> None:
        for camera in self.config.cameras:
            worker = self.capture.get(camera.id)
            if worker is None:
                continue
            status = worker.status()
            healthy = status.connected and not status.stale
            previous = last_status.get(camera.id)
            last_status[camera.id] = healthy
            if previous is None:
                # Lần quan sát ĐẦU TIÊN: coi như lúc khởi động camera đang khoẻ.
                # Nếu không, một camera hỏng sẵn từ lúc bật app sẽ không bao giờ
                # sinh chuyển trạng thái ⇒ on_camera_lost (kể cả publish_custom)
                # không chạy.
                if not healthy:
                    self._on_camera_lost(camera)
                continue
            if previous and not healthy:
                self._on_camera_lost(camera)
            elif not previous and healthy:
                self._on_camera_recovered(camera)

    def _on_camera_lost(self, camera: CameraConfig) -> None:
        self.events.add("camera", f"Mất tín hiệu camera {camera.name}", level="warning")
        policy = camera.on_camera_lost
        engine = self._zone_engines.get(camera.id)
        if engine is None:
            return
        if policy == "vacant":
            forced = self._lost_forced.setdefault(camera.id, set())
            for zone in camera.zones:
                if not zone.enabled:
                    continue
                engine.force_zone(zone.id, False, 0)
                forced.add(zone.id)
            self.events.add("camera", f"Camera {camera.name} mất: coi như vùng trống (fail-safe)")
            # Áp dụng NGAY thay vì đợi nhịp điều khiển kế tiếp: fail-safe phải
            # tất định (và nhờ vậy không phụ thuộc thời điểm camera mất).
            self._propagate_forced_states()
        elif policy == "publish_custom" and camera.on_camera_lost_publish:
            now = time.time()
            if now - self._last_custom_loss.get(camera.id, 0.0) < 5.0:
                return
            self._last_custom_loss[camera.id] = now
            for action in camera.on_camera_lost_publish:
                if not action.enabled:
                    continue
                context = {
                    "camera": camera.id,
                    "zone": "",
                    "zone_name": "",
                    "count": 0,
                    "class_counts": {},
                    "timestamp": datetime.now().isoformat(timespec="seconds"),
                    "scenario": "camera_lost",
                }
                self.mqtt.publish(
                    action.topic,
                    action.payload,
                    qos=action.qos,
                    retain=action.retain,
                    key=f"camera-lost:{camera.id}",
                    context=context,
                )
                self.events.add(
                    "command", f"Camera mất → gửi {action.topic} ({_short_payload(action.payload)})"
                )
        else:  # hold by default: keep the last known state, publish nothing
            self.events.add(
                "camera", f"Camera {camera.name} mất: giữ nguyên trạng thái (không đổi đèn)"
            )

    def _on_camera_recovered(self, camera: CameraConfig) -> None:
        self.events.add("camera", f"Camera {camera.name} đã hoạt động lại")
        forced = self._lost_forced.pop(camera.id, set())
        engine = self._zone_engines.get(camera.id)
        if engine is not None:
            for zone_id in forced:
                engine.clear_force(zone_id)

    # -- MQTT events ------------------------------------------------------ #
    def _on_mqtt_event(self, event: dict) -> None:
        if event.get("type") == "mqtt_status":
            connected = bool(event.get("connected"))
            self.events.add(
                "mqtt",
                "MQTT đã kết nối" if connected else f"MQTT mất kết nối ({event.get('error') or 'đang thử lại'})",
                level="info" if connected else "warning",
            )

    # -- config hot reload ------------------------------------------------ #
    def _on_config_change(self, old: AppConfig, new: AppConfig) -> None:
        if self._dry_run_override is not None:
            self.mqtt.dry_run = self._dry_run_override
        self.log.info("Áp dụng cấu hình mới (version %s)", self.config_manager.version)

        def step(name: str, fn: Callable[[], None]) -> None:
            """Run one reload step; a failure never skips the remaining steps.

            Before this guard the first exception (an ``IndexError`` on camera
            ids without a dash) aborted the whole listener, so a camera added
            from the Web UI never got an inference thread and its zones stayed
            on "Đang xác định…" forever.
            """
            try:
                fn()
            except Exception:  # noqa: BLE001 - one bad step must not stop the rest
                self.log.exception("Áp dụng cấu hình: bước '%s' lỗi", name)
                self.events.add(
                    "error",
                    f"Lỗi khi áp dụng cấu hình ({name}) — các phần khác vẫn chạy, xem log",
                    level="error",
                )

        step("vùng/kịch bản", self._rebuild_engines)
        step("model", lambda: self._apply_detector_config(old, new))
        step("MQTT", lambda: self.mqtt.update_config(new.mqtt))
        step("camera", lambda: self._sync_capture(new))
        step("suy luận", lambda: self._sync_inference(new))
        step("kịch bản", lambda: self.scenario_engine.reconfigure(new.scenarios))
        step("chỉnh tay", self._sync_override_topics)
        step("giả lập", lambda: self.simulator.bind_engines(lambda: self._zone_engines))
        self.events.add("config", "Đã áp dụng cấu hình — không cần khởi động lại")

    def _sync_capture(self, cfg: AppConfig) -> None:
        for message in self.capture.sync(cfg.cameras):
            self.events.add("config", message)

    def _apply_detector_config(self, old: AppConfig, new: AppConfig) -> None:
        if old.detector.model_dump() == new.detector.model_dump():
            return
        if not self.detector_enabled:
            return
        try:
            self.detector = build_detector(new.detector, self.log.getChild("detector"))
            self.events.add("config", f"Đã nạp lại model {new.detector.model}")
        except Exception as exc:  # noqa: BLE001
            self.events.add("error", f"Không nạp lại được model: {exc}", level="error")

    # -- webcam helpers --------------------------------------------------- #
    def ensure_test_camera(self, *, requested: bool = False) -> dict:
        """Create ``cam_test_laptop`` without touching existing RTSP cameras."""
        testing = self.config.testing
        camera_id = testing.test_camera_id
        if self.config.camera(camera_id) is not None:
            if requested:
                self.capture.sync(self.config.cameras)
            return {"created": False, "camera_id": camera_id}
        if not requested and not testing.auto_webcam:
            return {"created": False, "camera_id": camera_id}

        device = testing.webcam_device
        if device == "auto":
            cams = self.webcams()
            preferred = next((c for c in cams if c["recommended"]), None)
            device = preferred["name"] if preferred else "0"
        width, height = testing.webcam_resolution
        zone_id = "khuvuc_test"
        camera_data = {
            "id": camera_id,
            "name": "Camera test (webcam laptop)",
            "source": "webcam",
            "device": device,
            "resolution": [width, height],
            "fps": testing.webcam_fps,
            "mirror": True,
            "test": True,
            "auto_added": True,
            "lazy_open": True,
            "detect_fps": self.config.detector.detect_fps,
            "zones": [
                {
                    "id": zone_id,
                    "name": "Vùng test",
                    "polygon": [[0.08, 0.12], [0.92, 0.12], [0.92, 0.95], [0.08, 0.95]],
                    "classes": ["person"],
                }
            ],
        }
        scenario_data = default_demo_scenario(
            f"{camera_id}/{zone_id}",
            scenario_id="test_den_bep",
            name="Test: đèn bếp (giả lập)",
            topic="test/zigbee2mqtt/den_bep/set",
        ).model_dump(by_alias=True)
        scenario_data["test_mode"] = "prefix"

        def mutate(data: dict) -> None:
            data.setdefault("cameras", []).append(camera_data)
            data.setdefault("scenarios", []).append(scenario_data)

        try:
            self.config_manager.patch_raw(mutate)
        except ConfigError as exc:
            self.events.add("error", f"Không thêm được camera test: {exc}", level="error")
            return {"created": False, "camera_id": camera_id, "error": str(exc)}
        self.events.add(
            "config",
            "Đã thêm camera test (webcam). Camera RTSP giữ nguyên.",
            level="info",
        )
        return {"created": True, "camera_id": camera_id, "device": device}

    def promote_test_camera(self, camera_id: str) -> dict:
        if self.config.camera(camera_id) is None:
            return {"ok": False, "error": "Không thấy camera."}

        def mutate(data: dict) -> None:
            target = next((c for c in data.get("cameras", []) if c["id"] == camera_id), None)
            if target is None:
                raise ConfigError("Camera đã bị xoá.")
            target["test"] = False
            target["auto_added"] = False
            target["name"] = (
                str(target.get("name", camera_id))
                .replace("Camera test", "Camera")
                .replace("(webcam laptop)", "(webcam)")
            )
            for scenario in data.get("scenarios", []):
                if str(scenario.get("zone", "")).split("/", 1)[0] == camera_id:
                    scenario["test_mode"] = None

        try:
            self.config_manager.patch_raw(mutate)
        except ConfigError as exc:
            return {"ok": False, "error": str(exc)}
        self.events.add("config", f"Camera {camera_id} đã thành camera thật")
        return {"ok": True, "camera_id": camera_id}

    def delete_camera(self, camera_id: str) -> dict:
        """Remove a camera and every scenario that pointed at it."""
        camera = self.config.camera(camera_id)
        if camera is None:
            return {"ok": False, "error": "Không thấy camera."}
        is_test = camera.is_test

        def mutate(data: dict) -> None:
            data["cameras"] = [c for c in data.get("cameras", []) if c["id"] != camera_id]
            data["scenarios"] = [
                s
                for s in data.get("scenarios", [])
                if str(s.get("zone", "")).split("/", 1)[0] != camera_id
            ]

        try:
            self.config_manager.patch_raw(mutate)
        except ConfigError as exc:
            return {"ok": False, "error": str(exc)}
        worker = self.capture.get(camera_id)
        if worker is not None:
            worker.stop()
        self.events.add(
            "config", f"Đã xoá camera {'test ' if is_test else ''}{camera_id}"
        )
        return {"ok": True, "camera_id": camera_id}

    def delete_test_camera(self, camera_id: str) -> dict:
        return self.delete_camera(camera_id)

    def set_camera_enabled(self, camera_id: str, enabled: bool) -> dict:
        """Turn a camera on/off from the UI (capture starts/stops accordingly)."""
        if self.config.camera(camera_id) is None:
            return {"ok": False, "error": "Không thấy camera."}

        def mutate(data: dict) -> None:
            target = next((c for c in data.get("cameras", []) if c["id"] == camera_id), None)
            if target is None:
                raise ConfigError("Camera đã bị xoá.")
            target["enabled"] = bool(enabled)

        try:
            self.config_manager.patch_raw(mutate)
        except ConfigError as exc:
            return {"ok": False, "error": str(exc)}
        self.events.add(
            "config",
            f"{'Bật' if enabled else 'Tắt'} camera {camera_id}",
        )
        return {"ok": True, "camera_id": camera_id, "enabled": bool(enabled)}

    def start_loopback(self) -> dict:
        testing = self.config.testing
        camera = self.config.camera(testing.test_camera_id)
        device_index = 0
        device_name = ""
        if camera is not None:
            from .capture import list_webcams, resolve_webcam_index  # noqa: PLC0415

            device_index = resolve_webcam_index(camera.device)
            for entry in list_webcams():
                if int(entry["index"]) == device_index:
                    device_name = str(entry.get("name") or "")
                    break
        ok, message = self.loopback.start(
            device_index=device_index,
            resolution=testing.webcam_resolution,
            fps=testing.webcam_fps,
            device_name=device_name,
        )
        self.events.add(
            "camera", f"Loopback RTSP: {message}", level="info" if ok else "error"
        )
        if not ok:
            return {"ok": False, "message": message}
        # ffmpeg now owns the webcam: point the test camera at the RTSP URL so
        # no second process fights for the device ("device busy").
        if camera is not None and camera.source == "webcam":
            worker = self.capture.get(camera.id)
            if worker is not None:
                worker.stop()

            def mutate(data: dict) -> None:
                target = next(
                    (c for c in data.get("cameras", []) if c["id"] == camera.id), None
                )
                if target is None:
                    return
                target["source"] = "rtsp"
                target["url"] = testing.webcam_rtsp_url
                target["transport"] = "tcp"
                target["name"] = "Camera test (RTSP loopback)"

            try:
                self.config_manager.patch_raw(mutate)
            except ConfigError as exc:
                self.events.add("error", f"Không chuyển được camera test sang RTSP: {exc}", level="error")
        self._ensure_rtsp_test_camera()
        return {"ok": True, "message": message, "url": testing.webcam_rtsp_url}

    def stop_loopback(self) -> dict:
        self.loopback.stop()
        self.events.add("camera", "Đã tắt loopback RTSP")
        return {"ok": True}

    def _ensure_rtsp_test_camera(self) -> None:
        testing = self.config.testing
        camera_id = testing.test_rtsp_camera_id
        if self.config.camera(camera_id) is not None:
            return
        camera_data = {
            "id": camera_id,
            "name": "Camera test (RTSP loopback)",
            "source": "rtsp",
            "url": testing.webcam_rtsp_url,
            "transport": "tcp",
            "test": True,
            "auto_added": True,
            "detect_fps": self.config.detector.detect_fps,
            "zones": [
                {
                    "id": "khuvuc_test",
                    "name": "Vùng test RTSP",
                    "polygon": [[0.08, 0.12], [0.92, 0.12], [0.92, 0.95], [0.08, 0.95]],
                    "classes": ["person"],
                }
            ],
        }

        def mutate(data: dict) -> None:
            if not any(c["id"] == camera_id for c in data.get("cameras", [])):
                data.setdefault("cameras", []).append(camera_data)

        try:
            self.config_manager.patch_raw(mutate)
        except ConfigError as exc:
            self.events.add("error", f"Không thêm được camera test RTSP: {exc}", level="error")

    # -- state snapshots -------------------------------------------------- #
    def _camera_status(self, camera: Any, status: dict) -> str:
        """Một từ mô tả tình trạng camera để UI không phải tự đoán.

        ``off`` — người dùng chủ động tắt: **không phải lỗi**, không được tô đỏ;
        ``ok`` — đang có khung hình mới; ``stale`` — còn kết nối nhưng khung hình
        đã cũ; ``lost`` — mất hình (có lỗi); ``starting`` — chưa có luồng nào
        (vừa bật / đang mở lần đầu).
        """
        if not getattr(camera, "enabled", True):
            return "off"
        if status.get("connected"):
            return "stale" if status.get("stale") else "ok"
        return "lost" if status.get("error") else "starting"

    def camera_view(self, camera_id: str) -> dict | None:
        worker = self.capture.get(camera_id)
        camera = self.config.camera(camera_id)
        if camera is None:
            return None
        with self._cameras_lock:
            state = self._cameras.get(camera_id)
            zones = (
                {zone_id: zone_state.as_dict() for zone_id, zone_state in state.zones.items()}
                if state is not None
                else {}
            )
            detections = [d.as_dict() for d in state.detections] if state is not None else []
            inference = {
                "ms": round(state.inference_ms, 1) if state else 0.0,
                "fps": round(state.effective_detect_fps, 1) if state else 0.0,
                "static": bool(state.motion_static) if state else False,
                "last_at": state.last_infer_at if state else None,
            }
        capture = (
            worker.status().as_dict()
            if worker
            else CaptureStatus(
                camera_id=camera_id,
                error=None if camera.enabled else "camera đang tắt",
                source_desc=camera.source,
            ).as_dict()
        )
        return {
            "id": camera_id,
            "name": camera.name,
            "source": camera.source,
            "test": camera.is_test,
            "enabled": camera.enabled,
            "mirror": camera.mirror,
            "zones": [
                {
                    "id": zone.id,
                    "name": zone.name,
                    "polygon": [list(p) for p in zone.polygon],
                    "color": zone.color,
                    "classes": zone.classes,
                    "enabled": zone.enabled,
                    "anchor": zone.anchor,
                    "min_confidence": zone.min_confidence,
                }
                for zone in camera.zones
            ],
            "zone_states": zones,
            "detections": detections,
            "inference": inference,
            "capture": capture,
            "status": self._camera_status(camera, capture),
        }

    def _scenario_views(self) -> list[dict]:
        """Scenario status enriched with the camera id and effective test mode.

        The UI uses ``test_mode`` to badge scenarios that run in dry-run or under
        the ``test/`` topic prefix so nobody mistakes them for real commands.
        """
        views: list[dict] = []
        for entry in self.scenario_engine.status():
            zone_ref = str(entry.get("zone", ""))
            camera_id = zone_ref.split("/", 1)[0] if "/" in zone_ref else ""
            scenario = self.config.scenario(str(entry.get("id", "")))
            camera = self.config.camera(camera_id) if camera_id else None
            is_test = bool(camera.is_test) if camera is not None else False
            mode = scenario.effective_test_mode(is_test) if scenario else "normal"
            views.append({**entry, "camera_id": camera_id, "test_mode": mode})
        return views

    # -- platform / lifecycle --------------------------------------------- #
    def platform_info(self, *, max_age: float = 60.0) -> dict:
        """OS details for the web UI (cached: ``local_ip()`` opens a socket)."""
        now = time.time()
        if self._platform_cache is not None and now - self._platform_cache[0] < max_age:
            return self._platform_cache[1]
        info = compat.human_os_summary()
        try:
            info["webcam_permission"] = camera_authorization_status()
        except Exception:  # noqa: BLE001 - never break the UI
            self.log.debug("Không đọc được quyền camera", exc_info=True)
        self._platform_cache = (now, info)
        return info

    def request_shutdown(self, reason: str = "api") -> None:
        """Ask the app to stop gracefully (used by scripts / Windows launchers)."""
        self.log.info("Có yêu cầu dừng ứng dụng (%s)", reason)
        self._shutdown_requested.set()

    @property
    def shutdown_requested(self) -> bool:
        return self._shutdown_requested.is_set()

    def wait_for_shutdown(self, timeout: float = 1.0) -> bool:
        """Block up to ``timeout`` seconds; True when a stop was requested."""
        return self._shutdown_requested.wait(timeout)

    def snapshot_state(self) -> dict:
        # Include disabled cameras too: the Settings tab needs them to be able to
        # switch one back on (the other tabs filter by ``enabled``).
        cameras = [self.camera_view(c.id) for c in self.config.cameras]
        return {
            "generated_at": time.time(),
            "uptime_s": round(time.time() - self._started_at, 1),
            "dry_run": self.mqtt.dry_run,
            "cameras": [c for c in cameras if c],
            "mqtt": self.mqtt.status(),
            "scenarios": self._scenario_views(),
            "simulator": self.simulator.state(),
            "detector": self.detector.info() if self.detector else {"available": False},
            "webcams": self.webcams() if self._has_webcam_camera() else [],
            "loopback": {
                "running": self.loopback.running(),
                "available": self.loopback.available()[0],
                "url": self.config.testing.webcam_rtsp_url,
            },
            "setup_completed": self.config.setup_completed,
            "platform": self.platform_info(),
            "shutdown_requested": self.shutdown_requested,
            "diagnostics": self.diagnostics(),
            "warnings": self.config.topic_warnings(),
        }

    def diagnostics(self) -> dict[str, Any]:
        """Số liệu "sức khoẻ" cho bảng chẩn đoán trên UI.

        Mục đích: mỗi khi có cảm giác "hệ thống chạy sai", người dùng mở tab
        Chẩn đoán là thấy ngay khung hình cũ bao nhiêu ms, suy luận chạy bao
        nhiêu fps, model có lỗi gì, MQTT có hàng đợi hay không, và vì sao vùng
        đang ở trạng thái đó.
        """
        with self._cameras_lock:
            camera_rows: dict[str, Any] = {}
            for camera in self.config.cameras:
                worker = self.capture.get(camera.id)
                status: dict[str, Any] = {}
                if worker is not None:
                    raw = worker.status()
                    status = raw.as_dict() if hasattr(raw, "as_dict") else dict(raw or {})
                state = self._cameras.get(camera.id)
                infer_age = (
                    round(time.time() - state.last_infer_at, 1)
                    if state is not None and state.last_infer_at
                    else None
                )
                camera_rows[camera.id] = {
                    "status": self._camera_status(camera, status),
                    "enabled": camera.enabled,
                    "source": camera.source,
                    "connected": bool(status.get("connected")),
                    "is_open": bool(status.get("is_open")),
                    "stale": bool(status.get("stale")),
                    "error": status.get("error"),
                    "hint": status.get("hint"),
                    "capture_fps": round(float(status.get("capture_fps") or 0.0), 1),
                    "stream_fps": round(float(status.get("stream_fps") or 0.0), 1),
                    "read_ms": round(float(status.get("read_ms") or 0.0), 1),
                    "frame_age_ms": round(float(status.get("frame_age_ms") or 0.0), 1),
                    "reconnects": int(status.get("reconnects") or 0),
                    "inference_ms": round(state.inference_ms, 1) if state else None,
                    "infer_fps": round(state.infer_fps, 1) if state else None,
                    "detect_fps_target": round(state.effective_detect_fps, 1) if state else None,
                    "infer_age_s": infer_age,
                    "sampled_frame": bool(state.sampled_frame) if state else None,
                    "inference_errors": int(self._infer_errors.get(camera.id, 0)),
                    "inference_errors_consecutive": int(
                        self._infer_consecutive_errors.get(camera.id, 0)
                    ),
                    "inference_last_error": self._infer_last_error.get(camera.id),
                    "zones": {
                        zone_id: {
                            "occupied": zone_state.occupied,
                            "count": zone_state.count,
                            "pending_exit_s": zone_state.pending_exit,
                            "forced": zone_state.forced,
                            "since": _iso(_wall_from_monotonic(self.clock, zone_state.since)),
                        }
                        for zone_id, zone_state in (state.zones.items() if state else [])
                    },
                }
            threads = {
                "control": self._control_thread.is_alive() if self._control_thread else False,
                "inference": {cid: thread.is_alive() for cid, thread in self._infer_threads.items()},
                "capture": {
                    camera.id: bool((self.capture.get(camera.id) or None) is not None)
                    for camera in self.config.cameras
                },
                "uvicorn": "main" if self._stop.is_set() else "running",
            }
            mqtt_status = self.mqtt.status()
        detector_info = self.detector.info() if self.detector else {}
        return {
            "generated_at": time.time(),
            "cameras": camera_rows,
            "threads": threads,
            "detector": {
                "name": (detector_info.get("name") if self.detector else None),
                "available": bool(detector_info.get("available")),
                "device": detector_info.get("device"),
                "stats": detector_info.get("stats", {}),
                "errors_total": sum(self._infer_errors.values()),
            },
            "mqtt": {
                "connected": mqtt_status.get("connected"),
                "queued": mqtt_status.get("pending") or mqtt_status.get("queued") or 0,
                "sent": mqtt_status.get("sent"),
                "received": mqtt_status.get("received"),
                "dry_run": mqtt_status.get("dry_run"),
            },
            "scenarios": [
                {
                    "id": row.get("id"),
                    "reason": row.get("reason"),
                    "pending": row.get("pending"),
                    "vacant_retry_in_s": row.get("vacant_retry_in_s"),
                }
                for row in self.scenario_engine.status()
            ],
            "decisions": self.decisions(30),
            "warnings": self.config.topic_warnings(),
        }

    def webcams(self, *, max_age: float = 30.0) -> list[dict]:
        """Enumerate webcams, cached (the macOS query takes ~100 ms)."""
        now = time.time()
        if self._webcam_cache is not None and now - self._webcam_cache[0] < max_age:
            return self._webcam_cache[1]
        try:
            devices = list_webcams()
        except Exception as exc:  # noqa: BLE001 - enumeration must never break the UI
            self.log.warning("Không liệt kê được webcam: %s", exc)
            devices = []
        self._webcam_cache = (now, devices)
        return devices

    def _has_webcam_camera(self) -> bool:
        return any(camera.source == "webcam" for camera in self.config.cameras)

    def frame_for(self, camera_id: str) -> tuple[np.ndarray, dict] | None:
        """Newest frame plus overlay metadata (used by the MJPEG endpoint)."""
        worker = self.capture.get(camera_id)
        if worker is None:
            return None
        packet = worker.latest()
        if packet is None:
            return None
        return packet.frame, self.overlay_data(camera_id)

    def overlay_data(self, camera_id: str) -> dict:
        camera = self.config.camera(camera_id)
        with self._cameras_lock:
            state = self._cameras.get(camera_id)
            raw_detections = list(state.detections) if state else []
            detections = [detection.as_dict() for detection in raw_detections]
            zone_states = (
                {zone_id: zs.as_dict() for zone_id, zs in state.zones.items()} if state else {}
            )
            matched_by: dict[int, list[str]] = {id(item): [] for item in raw_detections}
            if state:
                for zone_id, zone_state in state.zones.items():
                    for detection in zone_state.detections:
                        matched_by.setdefault(id(detection), []).append(zone_id)
            inference = {
                "ms": round(state.inference_ms, 1) if state else 0.0,
                "fps": round(state.effective_detect_fps, 1) if state else 0.0,
            }
        # Điểm neo (điểm chân) + danh sách vùng đang tính đối tượng này: UI vẽ
        # chấm neo, người dùng thấy ngay "vì sao vùng này không thấy người".
        for index, raw in enumerate(raw_detections):
            box = raw.box
            detections[index]["anchor"] = [
                round((box.x1 + box.x2) / 2.0, 1),
                round(box.y2 - (box.y2 - box.y1) * 0.02, 1),
            ]
            detections[index]["in_zones"] = list(matched_by.get(id(raw), []))
        return {
            "camera": camera_id,
            "detections": detections,
            "zones": [
                {
                    "id": zone.id,
                    "name": zone.name,
                    "polygon": [list(p) for p in zone.polygon],
                    "color": zone.color,
                    "occupied": (zone_states.get(zone.id) or {}).get("occupied"),
                    "count": (zone_states.get(zone.id) or {}).get("count", 0),
                    "anchor": zone.anchor,
                    "overlap_ratio": zone.overlap_ratio,
                    "analysis": (zone_states.get(zone.id) or {}).get("analysis", {}),
                }
                for zone in (camera.zones if camera else [])
                if zone.enabled
            ],
            "inference": inference,
        }

    def stats(self) -> dict:
        worker_status = self.capture.statuses()
        capture_fps = {
            camera_id: status.get("capture_fps", 0.0) for camera_id, status in worker_status.items()
        }
        with self._cameras_lock:
            inference = {
                camera_id: {
                    "ms": round(state.inference_ms, 1),
                    "fps": round(state.effective_detect_fps, 1),
                }
                for camera_id, state in self._cameras.items()
            }
        return {
            "capture_fps": capture_fps,
            "inference": inference,
            "detector": self.detector.info() if self.detector else None,
            "mqtt": self.mqtt.status(),
            "events": self.events.seq,
        }

    # -- test / simulation helpers used by the UI ------------------------- #
    def simulate_zone(self, zone_ref: str, occupied: bool, *, count: int = 1) -> dict:
        result = self.simulator.occupy(zone_ref, count=count) if occupied else self.simulator.vacate(zone_ref)
        self.events.add("info", f"Mô phỏng {zone_ref}: {'có người' if occupied else 'trống'}")
        return result

    def clear_simulation(self, zone_ref: str | None = None) -> dict:
        return self.simulator.clear(zone_ref)

    def set_simulation_scale(self, scale: float) -> dict:
        result = self.simulator.set_scale(scale)
        self.events.add("info", f"Mô phỏng: tốc độ thời gian x{result['scale']:.0f}")
        return result

    def check_camera(self, camera_id: str) -> dict:
        """Kiểm tra "camera này có tới được không" và trả lời bằng tiếng Việt.

        Chạy trong thread của endpoint (route khai báo ``def``) nên không chặn
        vòng lặp sự kiện, kể cả khi mạng phải chờ hết timeout.
        """
        camera = self.config.camera(camera_id)
        if camera is None:
            raise ConfigError(f"Không có camera '{camera_id}'.")
        result = check_camera_source(camera).as_dict()
        result["camera_id"] = camera_id
        self.events.add(
            "camera",
            f"Kiểm tra kết nối {camera_id}: {result['message']}",
            level="info" if result["ok"] else "error",
            camera_id=camera_id,
            stage=result["stage"],
        )
        return result

    def test_mqtt(self) -> dict:
        ok, message = self.mqtt.test_connection()
        self.events.add("mqtt", message, level="info" if ok else "error")
        return {"ok": ok, "message": message}

    def send_test_command(
        self, scenario_id: str, trigger_index: int = 0, step_index: int = 0
    ) -> dict:
        scenario = self.config.scenario(scenario_id)
        if scenario is None:
            return {"ok": False, "message": "Không thấy kịch bản."}
        camera_id = scenario.zone.split("/", 1)[0]
        camera = self.config.camera(camera_id)
        if camera is None:
            return {"ok": False, "message": "Không thấy camera của kịch bản."}
        zone_state = None
        with self._cameras_lock:
            camera_state = self._cameras.get(camera_id)
            if camera_state is not None:
                zone_id = scenario.zone.split("/", 1)[1]
                zone_state = camera_state.zones.get(zone_id)
        triggers = scenario.all_triggers()
        if not triggers:
            return {"ok": False, "message": "Kịch bản chưa có bước nào."}
        if not 0 <= trigger_index < len(triggers):
            return {"ok": False, "message": "Không thấy điều kiện kích hoạt này."}
        trigger = triggers[trigger_index]
        if not trigger.steps:
            return {"ok": False, "message": "Điều kiện này chưa có bước nào."}
        step_index = max(0, min(step_index, len(trigger.steps) - 1))
        step = trigger.steps[step_index]
        from .scenarios import CommandRequest as _CommandRequest  # noqa: PLC0415

        context = {
            "count": zone_state.count if zone_state else 0,
            "class_counts": zone_state.class_counts if zone_state else {},
            "zone": scenario.zone.split("/", 1)[1],
            "camera": camera_id,
            "zone_name": zone_state.name if zone_state else "",
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "scenario": scenario.id,
        }
        zone_id = scenario.zone.split("/", 1)[1]
        command = _CommandRequest(
            scenario_id=scenario.id,
            scenario_name=scenario.name,
            zone_ref=scenario.zone,
            camera_id=camera_id,
            zone_id=zone_id,
            zone_name=zone_state.name if zone_state else zone_id,
            trigger=f"test:{trigger.type}",
            step_label=step.label or "Gửi thử",
            action=step.publish,
            context=context,
            created_at=self.clock.now(),
            wall_time=time.time(),
        )
        self._publish_command(command, camera)
        return {
            "ok": True,
            "topic": step.publish.topic,
            "payload": step.publish.payload,
            "dry_run": self.mqtt.dry_run,
        }

    # -- config editing --------------------------------------------------- #
    def update_config_data(self, data: dict) -> dict:
        try:
            self.config_manager.update_data(data)
            return {"ok": True, "version": self.config_manager.version}
        except ConfigError as exc:
            self.events.add("error", f"Cấu hình không hợp lệ: {exc}", level="error")
            return {"ok": False, "error": str(exc)}

    def save(self) -> dict:
        try:
            save_config(self.config_manager.current, self.config_manager.path)
            return {"ok": True, "path": str(self.config_manager.path)}
        except ConfigError as exc:
            return {"ok": False, "error": str(exc)}

    async def run_forever(self) -> None:  # pragma: no cover - used by main
        while not self._stop.is_set():
            await asyncio.sleep(1.0)


class _MonotonicClock:
    """Real clock for the scenario engine (scaled by :class:`ScaledClock`)."""

    def now(self) -> float:
        return time.monotonic()


def _trigger_reason(command: CommandRequest) -> str:
    """Câu giải thích ngắn: lệnh này được gửi vì lý do gì."""
    trigger = command.trigger
    context = command.context or {}
    count = context.get("count", 0)
    zone_name = command.zone_name or command.zone_id or command.zone_ref
    if trigger.startswith("test:"):
        return f"bạn bấm “Gửi thử” ở điều kiện {trigger.split(':', 1)[1]} (vùng “{zone_name}”)"
    if trigger == "enter":
        return f"vùng “{zone_name}” vừa có người vào (đếm {count})"
    if trigger == "vacant":
        return f"vùng “{zone_name}” đã trống đủ thời gian chờ rời vùng"
    if trigger == "dwell":
        return f"có người đứng liên tục trong vùng “{zone_name}”"
    if trigger in ("count_ge", "count_le"):
        return f"số người trong vùng “{zone_name}” hiện là {count}"
    if trigger == "class_present":
        return f"có đúng lớp đối tượng cần tìm trong vùng “{zone_name}”"
    return f"điều kiện {trigger} của vùng “{zone_name}” được thoả"


def _short_payload(payload: Any, limit: int = 120) -> str:
    text = payload if isinstance(payload, str) else str(payload)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _wall_from_monotonic(clock: Any, value: float | None) -> float | None:
    """Đổi mốc trên đồng hồ monotonic (của engine) sang giờ thực để hiển thị."""
    if value is None:
        return None
    try:
        base = getattr(clock, "base", None)
        now_mono = base.now() if base is not None else time.monotonic()
        return time.time() - (now_mono - value)
    except Exception:  # noqa: BLE001 - chỉ để hiển thị
        return None


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts).isoformat(timespec="seconds")


def setup_logging(config: AppConfig) -> None:
    """Configure ``app`` logging with rotation (safe to call twice)."""
    root = logging.getLogger()
    level = getattr(logging, str(config.logging.level).upper(), logging.INFO)
    root.setLevel(level)
    for handler in list(root.handlers):
        if getattr(handler, "_vision_handler", False):
            root.removeHandler(handler)
    formatter = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    console._vision_handler = True  # type: ignore[attr-defined]
    root.addHandler(console)
    log_file = config.logging.file
    if log_file:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        rotating = logging.handlers.RotatingFileHandler(
            path,
            maxBytes=config.logging.max_bytes,
            backupCount=config.logging.backup_count,
            encoding="utf-8",
        )
        rotating.setFormatter(formatter)
        rotating._vision_handler = True  # type: ignore[attr-defined]
        root.addHandler(rotating)


__all__ = [
    "CameraRuntimeState",
    "Event",
    "EventBus",
    "LoopbackStream",
    "Runtime",
    "setup_logging",
]
