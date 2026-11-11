"""Configuration models, validation, atomic persistence and secret masking.

This module is intentionally free of OpenCV / MQTT / asyncio imports so it can be
unit-tested on any machine (no camera, no broker).

Design notes
------------
* Every model forbids unknown keys (``extra="forbid"``) so a typo in
  ``config.yaml`` fails loudly instead of silently doing nothing.
* Secrets (passwords, tokens, RTSP credentials) support ``${ENV_VAR}`` expansion
  and are masked with ``****`` when sent to the Web UI.
* Saving is atomic: write temp file in the same directory -> fsync -> keep a
  ``.bak`` copy of the previous file -> ``os.replace``.
"""

from __future__ import annotations

import copy
import os
import re
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

SCHEMA_VERSION = 1
MASK = "****"

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_URL_CRED_RE = re.compile(r"^(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*://)(?P<user>[^:/@]+):(?P<pw>[^@/]*)@")


class ConfigError(Exception):
    """Configuration could not be loaded/validated.

    The message is user facing (Vietnamese) and mentions how to fix the problem.
    """


# --------------------------------------------------------------------------- #
# helpers: env expansion, secrets, time
# --------------------------------------------------------------------------- #
def expand_env(value: Any, *, source: str = "config.yaml") -> Any:
    """Recursively expand ``${VAR}`` / ``${VAR:-default}`` inside every string."""
    if isinstance(value, str):
        def repl(match: re.Match[str]) -> str:
            var, default = match.group(1), match.group(2)
            raw = os.environ.get(var)
            if raw is None or raw == "":
                if default is not None:
                    return default
                raise ConfigError(
                    f"{source}: biến môi trường '{var}' chưa được đặt. "
                    f"Hãy export {var}=... trước khi chạy, hoặc sửa giá trị trong config."
                )
            return raw

        return _ENV_RE.sub(repl, value)
    if isinstance(value, dict):
        return {k: expand_env(v, source=source) for k, v in value.items()}
    if isinstance(value, list):
        return [expand_env(v, source=source) for v in value]
    return value


def mask_url(url: str | None) -> str | None:
    """Return ``rtsp://user:****@host`` so credentials never reach the browser."""
    if not url:
        return url
    return _URL_CRED_RE.sub(lambda m: f"{m.group('scheme')}{m.group('user')}:{MASK}@", url)


def strip_url_credentials(url: str) -> str:
    """Remove userinfo entirely (used when the user retypes a masked URL)."""
    return _URL_CRED_RE.sub(lambda m: m.group("scheme"), url)


def _check_id(value: str, what: str) -> str:
    if not _ID_RE.match(value or ""):
        raise ValueError(
            f"{what} '{value}' không hợp lệ: chỉ dùng chữ, số, dấu '_', '-', '.' (tối đa 64 ký tự)."
        )
    return value


def _check_time(value: str, field: str) -> str:
    if not _TIME_RE.match(value):
        raise ValueError(f"{field} phải ở dạng HH:MM (00:00 - 23:59), nhận được '{value}'.")
    return value


def time_to_minutes(value: str) -> int:
    hour, minute = value.split(":")
    return int(hour) * 60 + int(minute)


# --------------------------------------------------------------------------- #
# models
# --------------------------------------------------------------------------- #
class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class VoteConfig(_Base):
    """K-of-N voting used to confirm that a zone became occupied."""

    frames: int = Field(default=3, ge=1, le=60, description="Số frame phải thấy đối tượng")
    of: int = Field(default=5, ge=1, le=120, description="Số frame gần nhất để xét")

    @model_validator(mode="after")
    def _check(self) -> "VoteConfig":
        if self.frames > self.of:
            raise ValueError(f"enter_confirm.frames ({self.frames}) phải <= enter_confirm.of ({self.of}).")
        return self


class ScheduleConfig(_Base):
    """Active time window. Supports windows crossing midnight (18:00 -> 06:00)."""

    from_time: str = Field(default="00:00", alias="from")
    to_time: str = Field(default="23:59", alias="to")
    days: list[int] | None = Field(
        default=None,
        description="Các thứ trong tuần được phép chạy, 0=Thứ 2 ... 6=Chủ nhật. null = mọi ngày",
    )

    @field_validator("from_time", "to_time")
    @classmethod
    def _validate_time(cls, value: str, info) -> str:
        return _check_time(value, info.field_name)

    @field_validator("days")
    @classmethod
    def _validate_days(cls, value: list[int] | None) -> list[int] | None:
        if value is None:
            return value
        cleaned = sorted({int(d) for d in value})
        if not cleaned:
            return None
        for day in cleaned:
            if day < 0 or day > 6:
                raise ValueError("active_schedule.days chỉ nhận 0..6 (0=Thứ 2, 6=Chủ nhật).")
        return cleaned

    def is_active(self, hour: int, minute: int, weekday: int) -> bool:
        """``weekday`` follows ``datetime.weekday()`` (0=Monday)."""
        if self.days is not None and weekday not in self.days:
            return False
        start = time_to_minutes(self.from_time)
        end = time_to_minutes(self.to_time)
        now = hour * 60 + minute
        if start == end:
            return True
        if start < end:
            return start <= now <= end
        # crosses midnight
        return now >= start or now <= end


class PublishAction(_Base):
    """One MQTT publish command. ``payload`` may be any JSON value."""

    topic: str = Field(min_length=1)
    payload: Any = None
    qos: Literal[0, 1, 2] = 0
    retain: bool = False
    enabled: bool = True
    label: str | None = None

    @field_validator("topic")
    @classmethod
    def _validate_topic(cls, value: str) -> str:
        topic = value.strip()
        if not topic:
            raise ValueError("topic không được để trống.")
        if any(ch in topic for ch in ("+", "#")):
            raise ValueError(f"topic '{topic}' không được chứa wildcard (+ hoặc #).")
        return topic


class StepSpec(_Base):
    """A timeline step: wait ``after_s`` seconds then run ``publish``."""

    after_s: float = Field(default=0.0, ge=0, le=86400 * 7)
    publish: PublishAction | None = None
    label: str | None = None
    enabled: bool = True

    @model_validator(mode="after")
    def _require_action(self) -> "StepSpec":
        if self.publish is None:
            raise ValueError("Mỗi bước cần có 'publish' (hành động MQTT).")
        return self

    @property
    def description(self) -> str:
        if self.label:
            return self.label
        minutes = self.after_s / 60.0
        delay = f"{minutes:g} phút" if minutes >= 1 else f"{self.after_s:g} giây"
        return f"sau {delay}: {self.publish.topic if self.publish else '?'}"


TriggerType = Literal["enter", "vacant", "count_ge", "count_le", "dwell", "class_present"]


class TriggerSpec(_Base):
    """A scenario trigger with its own timeline of steps."""

    type: TriggerType
    count: int | None = Field(default=None, ge=0, le=1000)
    seconds: float | None = Field(default=None, ge=0, le=86400 * 7)
    classes: list[str] = Field(default_factory=list)
    steps: list[StepSpec] = Field(default_factory=list)
    cooldown_s: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _check(self) -> "TriggerSpec":
        if self.type in ("count_ge", "count_le") and self.count is None:
            raise ValueError(f"Trigger '{self.type}' cần tham số 'count'.")
        if self.type == "dwell" and self.seconds is None:
            raise ValueError("Trigger 'dwell' cần tham số 'seconds'.")
        if self.type == "class_present" and not self.classes:
            raise ValueError("Trigger 'class_present' cần danh sách 'classes' (vd: [cat, dog]).")
        if not self.steps:
            raise ValueError(f"Trigger '{self.type}' chưa có bước hành động nào.")
        return self


class ManualOverrideConfig(_Base):
    """Pause automation when the light/socket is operated by hand."""

    state_topic: str | None = None
    pause_minutes: float = Field(default=10, ge=0, le=1440)

    @field_validator("state_topic")
    @classmethod
    def _validate_topic(cls, value: str | None) -> str | None:
        if value is None:
            return value
        topic = value.strip()
        if not topic:
            return None
        if any(ch in topic for ch in ("+", "#")):
            raise ValueError("manual_override.state_topic không được chứa wildcard.")
        return topic


class ZoneConfig(_Base):
    """A polygon region of interest. Coordinates are normalised 0..1."""

    id: str
    name: str
    polygon: list[tuple[float, float]]
    # Kiểu vùng do người dùng chọn trong Web UI: "rect" = giữ đúng 4 góc vuông
    # (kéo góc/cạnh là đổi kích thước), "polygon" = đa giác tự do (kéo điểm nào
    # chỉ điểm đó chạy). None = config cũ chưa có trường này → UI tự nhận biết
    # theo hình học. Không ảnh hưởng logic phát hiện (chỉ dùng để dựng hình).
    shape: Literal["polygon", "rect"] | None = None
    classes: list[str] = Field(default_factory=lambda: ["person"])
    min_confidence: float | None = Field(default=None, ge=0.01, le=1.0)
    anchor: Literal["bottom_center", "center", "any_overlap", "overlap_ratio"] = "bottom_center"
    overlap_ratio: float = Field(default=0.5, ge=0.05, le=1.0)
    enter_confirm: VoteConfig = Field(default_factory=VoteConfig)
    exit_grace_s: float = Field(default=3.0, ge=0, le=3600)
    color: str = "#22c55e"
    enabled: bool = True
    publish_state: bool = True
    state_topic: str | None = None

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        return _check_id(value, "zone id")

    @field_validator("polygon")
    @classmethod
    def _validate_polygon(cls, value: list[tuple[float, float]]) -> list[tuple[float, float]]:
        points: list[tuple[float, float]] = []
        for index, point in enumerate(value):
            if len(point) != 2:
                raise ValueError(f"zone.polygon[{index}] phải là [x, y].")
            x, y = float(point[0]), float(point[1])
            if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
                raise ValueError(
                    f"zone.polygon[{index}] = [{x}, {y}] nằm ngoài 0..1. "
                    "Toạ độ zone phải được chuẩn hoá (0..1) theo chiều rộng/cao khung hình."
                )
            if points and abs(points[-1][0] - x) < 1e-9 and abs(points[-1][1] - y) < 1e-9:
                continue  # drop duplicated consecutive vertices
            points.append((round(x, 6), round(y, 6)))
        if len(points) > 2 and points[0] == points[-1]:
            points.pop()
        if len(points) < 3:
            raise ValueError("zone cần ít nhất 3 đỉnh (polygon).")
        return points

    @field_validator("classes")
    @classmethod
    def _validate_classes(cls, value: list[str]) -> list[str]:
        cleaned = [c.strip() for c in value if c and c.strip()]
        if not cleaned:
            raise ValueError("zone.classes phải có ít nhất một class (vd: person).")
        return cleaned

    @field_validator("color")
    @classmethod
    def _validate_color(cls, value: str) -> str:
        color = value.strip()
        if not re.match(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$", color):
            raise ValueError(f"zone.color '{value}' phải là mã màu hex, vd #22c55e.")
        return color


class ReconnectConfig(_Base):
    initial_s: float = Field(default=0.5, ge=0.05, le=60)
    max_s: float = Field(default=30.0, ge=1, le=600)
    factor: float = Field(default=2.0, ge=1.0, le=10.0)


SourceKind = Literal["rtsp", "webcam", "file", "image"]
CameraLostPolicy = Literal["hold", "vacant", "publish_custom"]


class CameraConfig(_Base):
    id: str
    name: str
    source: SourceKind = "rtsp"
    url: str | None = None
    transport: Literal["tcp", "udp"] = "tcp"
    device: str | int | None = None
    resolution: tuple[int, int] | None = None
    fps: float | None = Field(default=None, ge=1, le=120)
    mirror: bool = False
    stale_after_s: float = Field(default=10.0, ge=1, le=600)
    enabled: bool = True
    auto_added: bool = False
    test: bool = False
    detect_fps: float | None = Field(default=None, ge=0.1, le=30)
    zones: list[ZoneConfig] = Field(default_factory=list)
    reconnect: ReconnectConfig = Field(default_factory=ReconnectConfig)
    on_camera_lost: CameraLostPolicy = "hold"
    on_camera_lost_publish: list[PublishAction] = Field(default_factory=list)
    rtsp_options: dict[str, str] = Field(default_factory=dict)
    crop_to_zones: bool | None = None
    lazy_open: bool = False

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        return _check_id(value, "camera id")

    @field_validator("resolution")
    @classmethod
    def _validate_resolution(cls, value: tuple[int, int] | None) -> tuple[int, int] | None:
        if value is None:
            return None
        width, height = int(value[0]), int(value[1])
        if width < 160 or height < 120 or width > 7680 or height > 4320:
            raise ValueError(f"resolution {width}x{height} không hợp lý (160x120 .. 7680x4320).")
        return (width, height)

    @model_validator(mode="after")
    def _validate_source(self) -> "CameraConfig":
        seen: set[str] = set()
        for zone in self.zones:
            if zone.id in seen:
                raise ValueError(f"camera '{self.id}' có 2 zone trùng id '{zone.id}'.")
            seen.add(zone.id)
        if self.source == "rtsp":
            if not self.url:
                raise ValueError(f"camera '{self.id}': source=rtsp cần 'url'.")
            if not re.match(r"^rtsps?://", self.url):
                raise ValueError(
                    f"camera '{self.id}': url phải bắt đầu bằng rtsp:// hoặc rtsps:// (nhận được '{self.url}')."
                )
        elif self.source == "webcam":
            if self.device is None:
                self.device = "auto"
        elif self.source in ("file", "image"):
            if not self.url:
                raise ValueError(f"camera '{self.id}': source={self.source} cần 'url' là đường dẫn tới file.")
        if self.on_camera_lost == "publish_custom" and not self.on_camera_lost_publish:
            raise ValueError(
                f"camera '{self.id}': on_camera_lost=publish_custom nhưng chưa có "
                "on_camera_lost_publish nào."
            )
        return self

    @property
    def is_test(self) -> bool:
        return self.test or self.auto_added


class DetectorConfig(_Base):
    model: str = "yolo11n.pt"
    device: str = "auto"
    imgsz: int = Field(default=640, ge=160, le=1920)
    min_confidence: float = Field(default=0.45, ge=0.01, le=1.0)
    detect_fps: float = Field(default=4.0, ge=0.1, le=30)
    max_det: int = Field(default=50, ge=1, le=300)
    half: bool = False
    classes_allow: list[str] | None = None
    motion_gating: bool = False
    idle_detect_fps: float = Field(default=1.0, ge=0.1, le=30)
    motion_threshold: float = Field(default=1.5, ge=0.1, le=50)
    crop_to_zones: bool = False

    @model_validator(mode="after")
    def _check(self) -> "DetectorConfig":
        if self.idle_detect_fps > self.detect_fps:
            self.idle_detect_fps = self.detect_fps
        return self


class MqttConfig(_Base):
    host: str = "127.0.0.1"
    port: int = Field(default=1883, ge=1, le=65535)
    username: str | None = None
    password: str | None = None
    client_id: str = "vision-automation"
    availability_topic: str = "vision/status"
    base_topic: str = "vision"
    keepalive: int = Field(default=30, ge=5, le=600)
    tls: bool = False
    tls_insecure: bool = False
    ca_certs: str | None = None
    dry_run: bool = False
    queue_max: int = Field(default=50, ge=1, le=1000)
    reconnect_min_s: float = Field(default=1.0, ge=0.2, le=60)
    reconnect_max_s: float = Field(default=30.0, ge=1, le=600)
    publish_zone_state: bool = True
    monitor_max: int = Field(default=200, ge=20, le=2000)

    @model_validator(mode="after")
    def _check(self) -> "MqttConfig":
        if not self.host.strip():
            raise ValueError("mqtt.host không được để trống.")
        if self.reconnect_max_s < self.reconnect_min_s:
            raise ValueError("mqtt.reconnect_max_s phải >= mqtt.reconnect_min_s.")
        return self


class TestingConfig(_Base):
    auto_webcam: bool = True
    publish_webcam_as_rtsp: bool = False
    test_topic_prefix: str = "test/"
    webcam_rtsp_url: str = "rtsp://127.0.0.1:8554/laptop"
    mediamtx_bin: str = "mediamtx"
    ffmpeg_bin: str = "ffmpeg"
    webcam_device: str | int = "auto"
    webcam_resolution: tuple[int, int] = (1280, 720)
    webcam_fps: float = 15.0
    test_camera_id: str = "cam_test_laptop"
    test_rtsp_camera_id: str = "cam_test_rtsp"

    @field_validator("test_topic_prefix")
    @classmethod
    def _validate_prefix(cls, value: str) -> str:
        prefix = value.strip()
        if prefix and not prefix.endswith("/"):
            prefix += "/"
        return prefix

    @field_validator("webcam_rtsp_url")
    @classmethod
    def _validate_rtsp(cls, value: str) -> str:
        if not re.match(r"^rtsps?://", value):
            raise ValueError("testing.webcam_rtsp_url phải bắt đầu bằng rtsp://")
        return value


class AuthConfig(_Base):
    enabled: bool = False
    username: str = "admin"
    password: str | None = None
    token: str | None = None
    session_secret: str | None = None
    session_hours: float = Field(default=12.0, ge=0.1, le=720)

    @model_validator(mode="after")
    def _check(self) -> "AuthConfig":
        if self.enabled and not (self.password or self.token):
            raise ValueError(
                "web.auth.enabled = true nhưng chưa đặt password hoặc token. "
                "Hãy nhập mật khẩu cho Web UI trước khi bật xác thực."
            )
        return self


class WebConfig(_Base):
    host: str = "0.0.0.0"
    port: int = Field(default=8080, ge=1, le=65535)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    mjpeg_fps: float = Field(default=10.0, ge=1, le=30)
    jpeg_quality: int = Field(default=70, ge=30, le=95)
    stream_width: int = Field(default=960, ge=320, le=2560)
    ui_ws_hz: float = Field(default=8.0, ge=1, le=30)
    allow_snapshot_save: bool = True
    snapshot_dir: str = "snapshots"

    @property
    def frame_interval(self) -> float:
        return 1.0 / self.mjpeg_fps if self.mjpeg_fps > 0 else 0.2


class LoggingConfig(_Base):
    level: str = "INFO"
    file: str | None = "logs/vision-automation.log"
    max_bytes: int = Field(default=5_000_000, ge=100_000)
    backup_count: int = Field(default=3, ge=0, le=20)
    event_ring: int = Field(default=500, ge=50, le=20000)

    @field_validator("level")
    @classmethod
    def _validate_level(cls, value: str) -> str:
        level = value.strip().upper()
        if level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            raise ValueError("logging.level phải là DEBUG/INFO/WARNING/ERROR/CRITICAL.")
        return level


class AppConfig(_Base):
    schema_version: int = SCHEMA_VERSION
    setup_completed: bool = False
    mqtt: MqttConfig = Field(default_factory=MqttConfig)
    detector: DetectorConfig = Field(default_factory=DetectorConfig)
    cameras: list[CameraConfig] = Field(default_factory=list)
    scenarios: list[ScenarioConfig] = Field(default_factory=list)
    testing: TestingConfig = Field(default_factory=TestingConfig)
    web: WebConfig = Field(default_factory=WebConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)

    @field_validator("schema_version")
    @classmethod
    def _validate_schema(cls, value: int) -> int:
        if value != SCHEMA_VERSION:
            raise ValueError(
                f"schema_version={value} không được hỗ trợ (chỉ hỗ trợ {SCHEMA_VERSION}). "
                "Hãy xem README mục 'Nâng cấp cấu hình'."
            )
        return value

    @model_validator(mode="after")
    def _validate_references(self) -> "AppConfig":
        cam_ids: set[str] = set()
        for camera in self.cameras:
            if camera.id in cam_ids:
                raise ValueError(f"Có 2 camera trùng id '{camera.id}'.")
            cam_ids.add(camera.id)

        scenario_ids: set[str] = set()
        for scenario in self.scenarios:
            if scenario.id in scenario_ids:
                raise ValueError(f"Có 2 kịch bản trùng id '{scenario.id}'.")
            scenario_ids.add(scenario.id)
            try:
                camera, zone = self.resolve_zone(scenario.zone)
            except ValueError as exc:
                raise ValueError(
                    f"Kịch bản '{scenario.name}' ({scenario.id}) đang trỏ tới vùng "
                    f"'{scenario.zone}' nhưng {exc} "
                    "Cách sửa: mở tab “Kịch bản”, chọn lại vùng cho kịch bản này; "
                    "hoặc xoá kịch bản đó; hoặc tạo lại vùng với đúng mã cũ."
                ) from exc
            # warn-only: class names are validated at runtime against the model
            _ = (camera, zone)
        return self

    # -- lookups ---------------------------------------------------------- #
    def camera(self, camera_id: str) -> CameraConfig | None:
        return next((c for c in self.cameras if c.id == camera_id), None)

    def resolve_zone(self, ref: str) -> tuple[CameraConfig, ZoneConfig]:
        """Resolve ``camera_id/zone_id`` or raise a friendly error."""
        if not ref or "/" not in ref:
            raise ValueError(
                f"zone '{ref}' không hợp lệ: phải ở dạng '<camera_id>/<zone_id>' (vd cam_bep/khu_bep)."
            )
        camera_id, zone_id = ref.split("/", 1)
        camera = self.camera(camera_id)
        if camera is None:
            available = ", ".join(c.id for c in self.cameras) or "chưa có camera nào"
            raise ValueError(f"không có camera '{camera_id}' (đang có: {available}).")
        zone = next((z for z in camera.zones if z.id == zone_id), None)
        if zone is None:
            available = ", ".join(z.id for z in camera.zones) or "chưa có vùng nào"
            raise ValueError(
                f"camera '{camera_id}' không có vùng '{zone_id}' (đang có: {available})."
            )
        return camera, zone

    def scenario(self, scenario_id: str) -> "ScenarioConfig | None":
        return next((s for s in self.scenarios if s.id == scenario_id), None)


class ScenarioConfig(_Base):
    id: str
    name: str
    description: str = ""
    enabled: bool = True
    zone: str
    on_enter: list[StepSpec] = Field(default_factory=list)
    on_vacant: list[StepSpec] = Field(default_factory=list)
    triggers: list[TriggerSpec] = Field(default_factory=list)
    cooldown_s: float = Field(default=2.0, ge=0, le=3600)
    active_schedule: ScheduleConfig | None = None
    manual_override: ManualOverrideConfig | None = None
    publish_state_topic: str | None = None
    test_mode: Literal["normal", "dry_run", "prefix"] | None = None

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        return _check_id(value, "scenario id")

    @field_validator("zone")
    @classmethod
    def _validate_zone_ref(cls, value: str) -> str:
        if "/" not in value or value.count("/") != 1:
            raise ValueError("scenario.zone phải ở dạng '<camera_id>/<zone_id>'.")
        camera_id, zone_id = value.split("/", 1)
        _check_id(camera_id, "camera id")
        _check_id(zone_id, "zone id")
        return value

    @model_validator(mode="after")
    def _check(self) -> "ScenarioConfig":
        if not self.on_enter and not self.on_vacant and not self.triggers:
            raise ValueError(
                f"Kịch bản '{self.id}' chưa có hành động nào: cần on_enter, on_vacant hoặc triggers."
            )
        types = [t.type for t in self.triggers]
        if types.count("enter") > 1 or types.count("vacant") > 1:
            raise ValueError(
                f"Kịch bản '{self.id}' có nhiều trigger 'enter'/'vacant' trong triggers[]. "
                "Hãy dùng on_enter / on_vacant cho 2 loại này."
            )
        return self

    # -- normalised view used by the runtime ------------------------------ #
    def all_triggers(self) -> list[TriggerSpec]:
        """Return every trigger, folding ``on_enter`` / ``on_vacant`` into it."""
        result: list[TriggerSpec] = []
        if self.on_enter:
            result.append(TriggerSpec(type="enter", steps=self.on_enter))
        if self.on_vacant:
            result.append(TriggerSpec(type="vacant", steps=self.on_vacant))
        result.extend(self.triggers)
        return result

    def effective_test_mode(self, is_test_camera: bool) -> str:
        if self.test_mode:
            return self.test_mode
        return "dry_run" if is_test_camera else "normal"


# fix forward reference for ScenarioConfig inside AppConfig
AppConfig.model_rebuild()


# --------------------------------------------------------------------------- #
# load / dump / save
# --------------------------------------------------------------------------- #
def _parse_yaml(text: str, source: str) -> dict[str, Any]:
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{source}: YAML không hợp lệ — {exc}") from exc
    if raw is None:
        raise ConfigError(f"{source}: file rỗng.")
    if not isinstance(raw, dict):
        raise ConfigError(f"{source}: nội dung phải là một mapping (key: value) ở cấp cao nhất.")
    return raw


def _validate(data: dict[str, Any], source: str) -> AppConfig:
    try:
        return AppConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(format_validation_error(exc, source)) from exc


def format_validation_error(exc: ValidationError, source: str = "config.yaml") -> str:
    """Turn a pydantic ValidationError into Vietnamese, one line per problem."""
    lines = [f"{source}: cấu hình không hợp lệ ({exc.error_count()} lỗi):"]
    for error in exc.errors()[:20]:
        location = ".".join(str(part) for part in error.get("loc", ()))
        message = str(error.get("msg", "")).removeprefix("Value error, ")
        lines.append(f"  • {location}: {message}" if location else f"  • {message}")
    if exc.error_count() > 20:
        lines.append(f"  ... và {exc.error_count() - 20} lỗi khác.")
    return "\n".join(lines)


def load_config_text(text: str, source: str = "<text>") -> AppConfig:
    data = _parse_yaml(text, source)
    data = expand_env(data, source=source)
    return _validate(data, source)


def load_config(path: str | os.PathLike[str]) -> AppConfig:
    config_path = Path(path)
    if not config_path.exists():
        raise ConfigError(
            f"Không tìm thấy file cấu hình '{config_path}'. "
            "Hãy copy config.example.yaml thành config.yaml (hoặc chạy wizard trên Web UI)."
        )
    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"Không đọc được '{config_path}': {exc}") from exc
    return load_config_text(text, source=str(config_path))


def _recursive_mask(data: Any, path: tuple[str, ...], masker: "SecretMasker") -> Any:
    if isinstance(data, dict):
        return {key: _recursive_mask(value, path + (key,), masker) for key, value in data.items()}
    if isinstance(data, list):
        return [_recursive_mask(value, path + (str(index),), masker) for index, value in enumerate(data)]
    if isinstance(data, str) and masker.is_secret(path):
        return MASK if data else data
    return data


class SecretMasker:
    """Knows which config paths hold whole-value secrets.

    Camera URLs are masked *partially* (only the password part) by
    :func:`config_to_ui_dict` so the user still sees host/stream while editing.
    """

    SECRET_KEYS = {"password", "token", "session_secret"}

    def is_secret(self, path: tuple[str, ...]) -> bool:
        if not path:
            return False
        return path[-1] in self.SECRET_KEYS


def config_to_ui_dict(cfg: AppConfig) -> dict[str, Any]:
    """Config as JSON for the browser, with secrets masked."""
    data = cfg.model_dump(mode="json", by_alias=True)
    masker = SecretMasker()
    masked = _recursive_mask(data, (), masker)
    for camera in masked.get("cameras", []):
        if camera.get("url"):
            camera["url"] = mask_url(camera["url"])
    return masked


def config_to_yaml(cfg: AppConfig, *, mask_secrets: bool = True) -> str:
    data = cfg.model_dump(mode="json", by_alias=True)
    if mask_secrets:
        for camera in data.get("cameras", []):
            if camera.get("url"):
                camera["url"] = mask_url(camera["url"])
        if data.get("mqtt", {}).get("password"):
            data["mqtt"]["password"] = MASK
        auth = data.get("web", {}).get("auth", {})
        for key in ("password", "token", "session_secret"):
            if auth.get(key):
                auth[key] = MASK
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=None, width=100)


def _unmask(old: Any, new: Any) -> Any:
    """Replace masked placeholders in ``new`` with the real values from ``old``."""
    if isinstance(new, dict) and isinstance(old, dict):
        return {key: _unmask(old.get(key), value) for key, value in new.items()}
    if isinstance(new, list) and isinstance(old, list):
        return [_unmask(old[i] if i < len(old) else None, value) for i, value in enumerate(new)]
    if isinstance(new, str) and old is not None:
        if new == MASK:
            return old
        if MASK in new and isinstance(old, str):
            # e.g. rtsp://user:****@host/stream1 -> keep the stored password
            stripped_new = strip_url_credentials(new)
            stripped_old = strip_url_credentials(old)
            if stripped_new == stripped_old:
                return old
            return strip_url_credentials(new)
    return new


def merge_masked(new_data: dict[str, Any], old_cfg: AppConfig | None) -> dict[str, Any]:
    """Restore real secret values that the UI sent back as ``****``."""
    if old_cfg is None:
        return new_data
    old_data = old_cfg.model_dump(mode="json", by_alias=True)
    return _unmask(old_data, new_data)


def _replace_with_retry(
    source: Path, target: Path, *, attempts: int = 6, delay: float = 0.05
) -> None:
    """``os.replace`` with short retries.

    On Windows an antivirus scanner or the still-open previous handle can make
    ``os.replace`` fail with ``PermissionError`` (WinError 5/32) for a few
    milliseconds; without retries a hot-reload from the UI would surface as a
    scary "không lưu được cấu hình" error.
    """
    last_error: OSError | None = None
    for attempt in range(max(1, attempts)):
        try:
            os.replace(source, target)
            return
        except PermissionError as exc:  # Windows sharing violation / AV scan
            last_error = exc
            time.sleep(delay * (attempt + 1))
    if last_error is not None:
        raise last_error


def atomic_write(path: str | os.PathLike[str], text: str, *, backup: bool = True) -> None:
    """Write ``text`` to ``path`` atomically, keeping ``path.bak`` of the old file."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=str(target.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, 0o600)
        if backup and target.exists():
            try:
                shutil.copy2(target, target.with_suffix(target.suffix + ".bak"))
            except OSError:
                pass
        _replace_with_retry(tmp_path, target)
        try:
            dir_fd = os.open(str(target.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:  # Windows cannot fsync a directory: harmless
            pass
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def save_config(cfg: AppConfig, path: str | os.PathLike[str]) -> None:
    atomic_write(path, config_to_yaml(cfg, mask_secrets=False))


# --------------------------------------------------------------------------- #
# ConfigManager: hot-reload + change notification
# --------------------------------------------------------------------------- #
Listener = Callable[[AppConfig, AppConfig], None]


class ConfigManager:
    """Owns the current ``AppConfig`` and pushes changes to listeners.

    Listeners receive ``(old_config, new_config)`` and must be cheap; the manager
    is used from both the asyncio web loop and worker threads, so every access to
    ``current`` returns an immutable snapshot (pydantic models are treated as
    read-only after validation).
    """

    def __init__(self, path: str | os.PathLike[str], cfg: AppConfig) -> None:
        self.path = Path(path)
        self._cfg = cfg
        self._listeners: list[Listener] = []
        self._lock = threading.RLock()
        self.version = 1

    @property
    def current(self) -> AppConfig:
        return self._cfg

    def add_listener(self, listener: Listener) -> None:
        with self._lock:
            self._listeners.append(listener)

    def remove_listener(self, listener: Listener) -> None:
        with self._lock:
            if listener in self._listeners:
                self._listeners.remove(listener)

    def _apply(self, new_cfg: AppConfig) -> AppConfig:
        with self._lock:
            old = self._cfg
            self._cfg = new_cfg
            self.version += 1
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(old, new_cfg)
            except Exception:  # noqa: BLE001 - a broken listener must not break saving
                import logging

                logging.getLogger(__name__).exception("Config listener %r failed", listener)
        return new_cfg

    def update_data(self, data: dict[str, Any], *, persist: bool = True) -> AppConfig:
        """Validate a full config dict (usually from the UI), persist and apply."""
        restored = merge_masked(data, self._cfg)
        new_cfg = _validate(restored, "Web UI")
        if persist:
            save_config(new_cfg, self.path)
        return self._apply(new_cfg)

    def update_yaml(self, text: str, *, persist: bool = True) -> AppConfig:
        data = _parse_yaml(text, "Web UI (YAML)")
        restored = merge_masked(expand_env(data, source="Web UI (YAML)"), self._cfg)
        new_cfg = _validate(restored, "Web UI (YAML)")
        if persist:
            save_config(new_cfg, self.path)
        return self._apply(new_cfg)

    def patch(self, mutator: Callable[[dict[str, Any]], None], *, persist: bool = True) -> AppConfig:
        """Mutate a copy of the masked dict then validate+save it."""
        data = config_to_ui_dict(self._cfg)
        mutator(data)
        return self.update_data(data, persist=persist)

    def patch_raw(
        self, mutator: Callable[[dict[str, Any]], None], *, persist: bool = True
    ) -> AppConfig:
        """Mutate an *unmasked* config dict (real secrets, plain values).

        Used by the runtime for structural edits (add/remove camera, promote a
        test camera) where working with pydantic models would fight the value
        types.  The result is validated exactly like any other input.
        """
        data = self._cfg.model_dump(by_alias=True)
        mutator(data)
        new_cfg = _validate(data, "Web UI (thay đổi)")
        if persist:
            save_config(new_cfg, self.path)
        return self._apply(new_cfg)

    def reload_from_disk(self) -> AppConfig:
        new_cfg = load_config(self.path)
        return self._apply(new_cfg)


# --------------------------------------------------------------------------- #
# factories used by the wizard / tests
# --------------------------------------------------------------------------- #
def default_demo_scenario(
    zone_ref: str,
    *,
    scenario_id: str = "den_bep",
    name: str = "Đèn bếp tự động",
    topic: str = "zigbee2mqtt/den_bep/set",
    dim_after_s: float = 30,
    off_after_s: float = 120,
) -> ScenarioConfig:
    """The reference "kitchen light" scenario from the specification."""
    return ScenarioConfig(
        id=scenario_id,
        name=name,
        description="Có người: bật 100%. Vắng 30s: giảm 30%. Vắng 120s: tắt. Quay lại: sáng lại.",
        zone=zone_ref,
        on_enter=[
            StepSpec(
                after_s=0,
                publish=PublishAction(
                    topic=topic,
                    payload={"state": "ON", "brightness": 254, "transition": 1},
                    qos=1,
                    label="Bật 100%",
                ),
                label="Bật đèn 100%",
            )
        ],
        on_vacant=[
            StepSpec(
                after_s=dim_after_s,
                publish=PublishAction(
                    topic=topic,
                    payload={"state": "ON", "brightness": 80, "transition": 3},
                    qos=1,
                    label="Giảm còn 30%",
                ),
                label="Giảm sáng",
            ),
            StepSpec(
                after_s=off_after_s,
                publish=PublishAction(
                    topic=topic, payload={"state": "OFF", "transition": 2}, qos=1, label="Tắt đèn"
                ),
                label="Tắt đèn",
            ),
        ],
        cooldown_s=2,
        manual_override=ManualOverrideConfig(state_topic=topic.replace("/set", ""), pause_minutes=10),
    )


def starter_config() -> AppConfig:
    """Minimal valid config written on very first run (before the wizard)."""
    return AppConfig(cameras=[], scenarios=[], setup_completed=False)


def config_signature(cfg: AppConfig) -> str:
    """Stable hash used to detect real changes (e.g. camera source edits)."""
    import hashlib
    import json

    payload = json.dumps(cfg.model_dump(mode="json", by_alias=True), sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


__all__ = [
    "AppConfig",
    "AuthConfig",
    "CameraConfig",
    "ConfigError",
    "ConfigManager",
    "DetectorConfig",
    "LoggingConfig",
    "ManualOverrideConfig",
    "MASK",
    "MqttConfig",
    "PublishAction",
    "ScheduleConfig",
    "ScenarioConfig",
    "SCHEMA_VERSION",
    "StepSpec",
    "TestingConfig",
    "TriggerSpec",
    "VoteConfig",
    "WebConfig",
    "ZoneConfig",
    "atomic_write",
    "config_signature",
    "config_to_ui_dict",
    "config_to_yaml",
    "default_demo_scenario",
    "expand_env",
    "format_validation_error",
    "load_config",
    "load_config_text",
    "mask_url",
    "merge_masked",
    "save_config",
    "starter_config",
    "time_to_minutes",
]
