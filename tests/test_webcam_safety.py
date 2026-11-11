"""macOS/webcam safety tests — everything here runs WITHOUT opening a real camera."""

from __future__ import annotations

import time

import cv2
import numpy as np
import pytest

from app.capture import CaptureManager, CaptureWorker, list_webcams, resolve_webcam_index
from app.config import (
    AppConfig,
    CameraConfig,
    ConfigManager,
    DetectorConfig,
    MqttConfig,
    default_demo_scenario,
    save_config,
)
from app.runtime import LoopbackStream, Runtime


def wait_for(predicate, timeout: float = 6.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    return None


def rtsp_camera(camera_id: str = "cam_bep") -> CameraConfig:
    return CameraConfig.model_validate(
        {
            "id": camera_id,
            "name": "Camera bếp",
            "source": "rtsp",
            "url": "rtsp://user:secret@192.168.1.50:554/stream1",
            "transport": "tcp",
            "zones": [
                {
                    "id": "khu_bep",
                    "name": "Khu bếp",
                    "polygon": [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]],
                    "classes": ["person"],
                }
            ],
        }
    )


def build_runtime(tmp_path, image_path=None) -> Runtime:
    cameras = [rtsp_camera()]
    if image_path is not None:
        cameras.append(
            CameraConfig.model_validate(
                {
                    "id": "cam_anh",
                    "name": "Ảnh",
                    "source": "image",
                    "url": str(image_path),
                    "lazy_open": False,
                    "fps": 8,
                    "zones": [
                        {
                            "id": "z",
                            "name": "Z",
                            "polygon": [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]],
                        }
                    ],
                }
            )
        )
    cfg = AppConfig.model_validate(
        {
            "mqtt": MqttConfig.model_validate(
                {"host": "127.0.0.1", "port": 1, "dry_run": True, "base_topic": "vision"}
            ).model_dump(),
            "detector": DetectorConfig().model_dump(),
            "cameras": [c.model_dump(by_alias=True) for c in cameras],
            "scenarios": [
                default_demo_scenario("cam_bep/khu_bep", scenario_id="den_bep").model_dump(by_alias=True)
            ],
            "testing": {"auto_webcam": True, "test_topic_prefix": "test/"},
            "setup_completed": True,
        }
    )
    path = tmp_path / "config.yaml"
    save_config(cfg, path)
    runtime = Runtime(ConfigManager(path, cfg), enable_detector=False)
    runtime.mqtt.start = lambda: None  # type: ignore[method-assign]
    runtime.mqtt.stop = lambda publish_offline=True: None  # type: ignore[method-assign]
    return runtime


@pytest.fixture()
def image_path(tmp_path):
    path = tmp_path / "scene.png"
    cv2.imwrite(str(path), np.full((180, 240, 3), 50, np.uint8))
    return path


# --------------------------------------------------------------------------- #
# webcam enumeration / mapping
# --------------------------------------------------------------------------- #
def test_list_webcams_marks_recommended_and_skips_iphone():
    devices = list_webcams()
    assert devices, "phải luôn có ít nhất một mục để UI không bị rỗng"
    for device in devices:
        assert {"index", "name", "recommended", "is_iphone"} <= set(device)
    recommended = [d for d in devices if d["recommended"]]
    # iPhone Continuity cameras must never be the recommended default
    assert all(not d["is_iphone"] for d in recommended)


def test_resolve_webcam_index_understands_auto_and_numeric():
    assert isinstance(resolve_webcam_index("auto"), int)
    assert resolve_webcam_index("0") == 0
    assert resolve_webcam_index("2") == 2
    # unknown names fall back to a valid index instead of raising
    assert isinstance(resolve_webcam_index("Webcam Khong Ton Tai"), int)


# --------------------------------------------------------------------------- #
# adding / promoting / deleting the test camera
# --------------------------------------------------------------------------- #
def test_ensure_test_camera_keeps_rtsp_camera_intact(tmp_path):
    runtime = build_runtime(tmp_path)
    before = runtime.config.camera("cam_bep")
    assert before is not None

    result = runtime.ensure_test_camera(requested=True)
    assert result["created"] is True
    camera = runtime.config.camera("cam_test_laptop")
    assert camera is not None
    assert camera.source == "webcam"
    assert camera.test is True
    assert camera.auto_added is True
    assert camera.lazy_open is True  # never opens the LED until someone watches
    assert camera.resolution == (1280, 720)
    assert camera.mirror is True

    # RTSP camera unchanged and still first (never reordered/overwritten)
    assert runtime.config.cameras[0].id == "cam_bep"
    after = runtime.config.camera("cam_bep")
    assert after is not None and after.model_dump() == before.model_dump()

    scenario = next(s for s in runtime.config.scenarios if s.id == "test_den_bep")
    assert scenario.zone.startswith("cam_test_laptop/")
    assert scenario.effective_test_mode(True) == "prefix"

    # idempotent
    again = runtime.ensure_test_camera(requested=True)
    assert again["created"] is False


def test_promote_and_delete_test_camera(tmp_path):
    runtime = build_runtime(tmp_path)
    runtime.ensure_test_camera(requested=True)

    promoted = runtime.promote_test_camera("cam_test_laptop")
    assert promoted["ok"] is True
    camera = runtime.config.camera("cam_test_laptop")
    assert camera is not None
    assert camera.test is False
    assert camera.auto_added is False
    scenario = next(s for s in runtime.config.scenarios if s.id == "test_den_bep")
    assert scenario.test_mode in (None, "")

    deleted = runtime.delete_test_camera("cam_test_laptop")
    assert deleted["ok"] is True
    assert runtime.config.camera("cam_test_laptop") is None
    assert all(not s.id.startswith("test_") for s in runtime.config.scenarios)
    assert runtime.config.camera("cam_bep") is not None


def test_missing_camera_promote_returns_error(tmp_path):
    runtime = build_runtime(tmp_path)
    assert runtime.promote_test_camera("khong_co")["ok"] is False
    assert runtime.delete_test_camera("khong_co")["ok"] is False


def test_test_camera_commands_use_test_prefix_and_dry_run(tmp_path):
    runtime = build_runtime(tmp_path)
    runtime.ensure_test_camera(requested=True)
    runtime.detector_enabled = False
    runtime.start()
    try:
        scenario = next(s for s in runtime.config.scenarios if s.id == "test_den_bep")
        camera = runtime.config.camera("cam_test_laptop")
        assert camera is not None and scenario is not None

        from app.scenarios import CommandRequest

        command = CommandRequest(
            scenario_id=scenario.id,
            scenario_name=scenario.name,
            zone_ref=scenario.zone,
            camera_id=camera.id,
            zone_id=scenario.zone.split("/", 1)[1],
            zone_name="Vùng test",
            trigger="enter",
            step_label="Bật",
            action=scenario.all_triggers()[0].steps[0].publish,
            context={"count": 1, "class_counts": {"person": 1}, "zone": "khuvuc_test", "camera": camera.id},
        )
        runtime._publish_command(command, camera)
        entries = runtime.mqtt.monitor(direction="tx")
        assert entries, "phải có bản ghi monitor"
        assert entries[-1]["topic"].startswith("test/")
        assert entries[-1]["dry_run"] is True
    finally:
        runtime.stop()


# --------------------------------------------------------------------------- #
# failure isolation: broken webcam must not affect RTSP/image cameras
# --------------------------------------------------------------------------- #
def test_unavailable_webcam_fails_without_crashing_and_image_keeps_running(tmp_path, image_path):
    broken = CameraConfig.model_validate(
        {
            "id": "cam_webcam_hong",
            "name": "Webcam không tồn tại",
            "source": "webcam",
            "device": "99",  # index that cannot exist
            "lazy_open": False,
            "fps": 5,
        }
    )
    good = CameraConfig.model_validate(
        {
            "id": "cam_anh",
            "name": "Ảnh",
            "source": "image",
            "url": str(image_path),
            "lazy_open": False,
            "fps": 8,
        }
    )
    manager = CaptureManager([broken, good])
    manager.start()
    try:
        error = wait_for(lambda: manager.get("cam_webcam_hong").status().error, timeout=8)
        assert error is not None
        assert "Không mở được camera" in error
        status = manager.get("cam_webcam_hong").status()
        assert status.connected is False
        assert status.hint  # Vietnamese hint for the UI

        good_packet = wait_for(lambda: manager.get("cam_anh").latest())
        assert good_packet is not None, "camera ảnh phải tiếp tục chạy"
    finally:
        manager.stop()


def test_removed_webcam_device_reports_permission_hint():
    camera = CameraConfig.model_validate(
        {
            "id": "cam_webcam",
            "name": "Webcam",
            "source": "webcam",
            "device": "97",
            "lazy_open": False,
        }
    )
    worker = CaptureWorker(camera)
    worker.start()
    try:
        status = wait_for(lambda: (worker.status().error or None), timeout=8)
        assert status is not None
        hint = worker.status().hint or ""
        assert "Camera" in hint or "quyền" in hint
    finally:
        worker.stop()


# --------------------------------------------------------------------------- #
# RTSP loopback tool detection
# --------------------------------------------------------------------------- #
def test_loopback_reports_missing_tools_in_vietnamese(tmp_path):
    runtime = build_runtime(tmp_path)
    loopback = LoopbackStream(runtime.config.testing)
    available, message = loopback.available()
    if available:  # tools installed on this machine
        assert "Sẵn sàng" in message
    else:
        from app import platform_compat as compat

        assert "ffmpeg" in message
        # The install hint must match the current OS (brew / winget / apt).
        assert compat.loopback_install_hint() in message
        result = loopback.start(device_index=0, resolution=(1280, 720), fps=15)
        assert result[0] is False
        assert result[1] == message
