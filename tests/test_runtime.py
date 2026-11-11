"""Tests for the detector abstraction and the runtime wiring (no real camera)."""

from __future__ import annotations

import time

import cv2
import numpy as np
import pytest

from app.config import (
    AppConfig,
    CameraConfig,
    ConfigManager,
    DetectorConfig,
    MqttConfig,
    ScenarioConfig,
    default_demo_scenario,
    save_config,
)
from app.detection import Box, Detection
from app.capture import FramePacket
from app.detector import NullDetector, YoloDetector, build_detector, resolve_device
from app.runtime import Runtime


# --------------------------------------------------------------------------- #
# helper detectors
# --------------------------------------------------------------------------- #
class FakeDetector:
    """Returns the same synthetic person detection on every frame."""

    def __init__(self, detections: list[Detection] | None = None) -> None:
        self.name = "fake"
        self._detections = detections

    def class_names(self) -> dict[int, str]:
        return {0: "person", 15: "cat"}

    def detect(self, frame, *, confidence=None):  # noqa: ANN001
        if self._detections is not None:
            return list(self._detections)
        height, width = frame.shape[:2]
        return [
            Detection(
                class_id=0,
                class_name="person",
                confidence=0.9,
                box=Box(
                    width * 0.4,
                    height * 0.15,
                    width * 0.6,
                    height * 0.95,
                ),
            )
        ]

    def info(self) -> dict:
        return {"name": "fake", "available": True, "device": "cpu", "model": "fake", "stats": {}}

    def close(self) -> None:
        return None


# --------------------------------------------------------------------------- #
# detector
# --------------------------------------------------------------------------- #
def test_null_detector_is_safe() -> None:
    detector = NullDetector("chưa cài ultralytics")
    assert detector.detect(np.zeros((32, 32, 3), dtype=np.uint8)) == []
    info = detector.info()
    assert info["available"] is False
    assert "person" in detector.class_names().values()


def test_resolve_device_passthrough_and_auto() -> None:
    assert resolve_device("cpu") == "cpu"
    assert resolve_device("cuda:1") == "cuda:1"
    auto = resolve_device("auto")
    assert auto in {"cpu", "mps", "cuda:0"} or auto.startswith("cuda")


def test_build_detector_falls_back_when_ultralytics_missing(monkeypatch) -> None:
    monkeypatch.setattr("app.detector.YoloDetector.__init__", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no module")))
    detector = build_detector(DetectorConfig(), None)
    assert isinstance(detector, NullDetector)
    assert detector.info()["available"] is False


def _ultralytics_available() -> bool:
    try:
        import ultralytics  # noqa: F401, PLC0415
    except Exception:  # noqa: BLE001
        return False
    return True


@pytest.mark.skipif(not _ultralytics_available(), reason="ultralytics chưa được cài")
def test_yolo_detector_real_model_smoke() -> None:  # pragma: no cover - heavy
    detector = YoloDetector("yolo11n.pt", device="cpu", imgsz=320, confidence=0.3)
    frame = np.full((320, 320, 3), 128, dtype=np.uint8)
    detections = detector.detect(frame)
    assert isinstance(detections, list)
    assert detector.info()["available"] is True
    detector.close()


# --------------------------------------------------------------------------- #
# runtime integration with an image camera
# --------------------------------------------------------------------------- #
def build_config(image_path, *, camera_id: str = "cam_anh") -> AppConfig:
    zone_id = "khu_bep"
    camera = CameraConfig.model_validate(
        {
            "id": camera_id,
            "name": "Ảnh tĩnh",
            "source": "image",
            "url": str(image_path),
            "lazy_open": False,
            "fps": 10,
            "detect_fps": 10,
            "zones": [
                {
                    "id": zone_id,
                    "name": "Khu bếp",
                    "polygon": [[0.05, 0.05], [0.95, 0.05], [0.95, 0.95], [0.05, 0.95]],
                    "classes": ["person"],
                    "enter_confirm": {"frames": 1, "of": 1},
                    "exit_grace_s": 0.2,
                }
            ],
        }
    )
    scenario = default_demo_scenario(f"{camera_id}/{zone_id}", scenario_id="den_bep")
    scenario.cooldown_s = 0
    return AppConfig.model_validate(
        {
            "mqtt": MqttConfig.model_validate({"host": "127.0.0.1", "port": 1, "dry_run": True}).model_dump(),
            "detector": DetectorConfig(detect_fps=10).model_dump(),
            "cameras": [camera.model_dump(by_alias=True)],
            "scenarios": [scenario.model_dump(by_alias=True)],
        }
    )


@pytest.fixture()
def image_path(tmp_path):
    path = tmp_path / "scene.png"
    image = np.full((240, 320, 3), 40, dtype=np.uint8)
    cv2.rectangle(image, (120, 20), (200, 200), (10, 200, 10), -1)
    cv2.imwrite(str(path), image)
    return path


def make_runtime(image_path, tmp_path, *, monkeypatch=None) -> Runtime:
    cfg = build_config(image_path)
    config_path = tmp_path / "config.yaml"
    save_config(cfg, config_path)
    manager = ConfigManager(config_path, cfg)
    runtime = Runtime(manager, enable_detector=False)
    runtime.mqtt.start = lambda: None  # type: ignore[method-assign] - no broker in tests
    runtime.mqtt.stop = lambda publish_offline=True: None  # type: ignore[method-assign]
    return runtime


def wait_for(predicate, timeout: float = 6.0, interval: float = 0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return None


def test_runtime_detect_enter_fires_scenario_command(image_path, tmp_path) -> None:
    runtime = make_runtime(image_path, tmp_path)
    runtime.start()
    try:
        assert wait_for(lambda: runtime.capture.get("cam_anh").latest()) is not None
        runtime.detector = FakeDetector()
        runtime.detector_enabled = True
        runtime._start_inference("cam_anh")

        event = wait_for(
            lambda: next(
                (e for e in runtime.events.recent(kind="command", limit=50) if "den_bep" in e["message"]),
                None,
            )
        )
        assert event is not None, "không có lệnh nào được phát"
        assert event["data"]["dry_run"] is True
        assert event["data"]["payload"] == {"state": "ON", "brightness": 254, "transition": 1}

        state = runtime.camera_view("cam_anh")
        assert state is not None
        assert state["zone_states"]["khu_bep"]["occupied"] is True
        assert state["zone_states"]["khu_bep"]["count"] == 1
    finally:
        runtime.stop()


def test_runtime_simulation_forces_occupancy(image_path, tmp_path) -> None:
    runtime = make_runtime(image_path, tmp_path)
    runtime.start()
    try:
        assert wait_for(lambda: runtime.capture.get("cam_anh").latest()) is not None
        result = runtime.simulate_zone("cam_anh/khu_bep", True, count=2)
        assert result["ok"] is True
        event = wait_for(
            lambda: next(
                (e for e in runtime.events.recent(kind="command", limit=50) if "den_bep" in e["message"]),
                None,
            )
        )
        assert event is not None
        assert runtime.simulator.state()["zones"]["cam_anh/khu_bep"]["occupied"] is True

        runtime.clear_simulation("cam_anh/khu_bep")
        assert runtime.simulator.state()["zones"] == {}
    finally:
        runtime.stop()


def test_runtime_time_scale_changes_clock(image_path, tmp_path) -> None:
    runtime = make_runtime(image_path, tmp_path)
    try:
        result = runtime.set_simulation_scale(10)
        assert result["scale"] == 10.0
        assert runtime.simulator.scale == 10.0
        runtime.set_simulation_scale(1)
        assert runtime.simulator.scale == 1.0
    finally:
        runtime.stop()


def test_runtime_camera_lost_vacant_policy_publishes_failsafe(image_path, tmp_path) -> None:
    runtime = make_runtime(image_path, tmp_path)
    camera = runtime.config.camera("cam_anh")
    camera.on_camera_lost = "vacant"
    runtime.start()
    try:
        assert wait_for(lambda: runtime.capture.get("cam_anh").latest()) is not None
        runtime.detector = FakeDetector()
        runtime.detector_enabled = True
        runtime._start_inference("cam_anh")
        # first a genuine "occupied" so the fail-safe has something to change
        assert wait_for(lambda: runtime.camera_view("cam_anh")["zone_states"]["khu_bep"]["occupied"] is True)

        runtime._on_camera_lost(camera)
        assert wait_for(
            lambda: runtime.camera_view("cam_anh")["zone_states"]["khu_bep"]["occupied"] is False
        )
        assert any("fail-safe" in e["message"] for e in runtime.events.recent(limit=50))
    finally:
        runtime.stop()


def test_runtime_state_snapshot_shape(image_path, tmp_path) -> None:
    runtime = make_runtime(image_path, tmp_path)
    runtime.start()
    try:
        snapshot = runtime.snapshot_state()
        for key in ("cameras", "mqtt", "scenarios", "simulator", "detector", "loopback", "dry_run"):
            assert key in snapshot
        assert snapshot["dry_run"] is True
        assert snapshot["cameras"][0]["id"] == "cam_anh"
        assert snapshot["scenarios"][0]["id"] == "den_bep"
    finally:
        runtime.stop()


def test_zone_crop_box_returns_padded_union():
    from app.config import ZoneConfig
    from app.zones import zone_crop_box

    zones = [
        ZoneConfig.model_validate(
            {"id": "a", "name": "A", "polygon": [[0.1, 0.1], [0.4, 0.1], [0.4, 0.4], [0.1, 0.4]]}
        ),
        ZoneConfig.model_validate(
            {"id": "b", "name": "B", "polygon": [[0.5, 0.5], [0.8, 0.5], [0.8, 0.8], [0.5, 0.8]]}
        ),
    ]
    crop = zone_crop_box(zones, 1000, 1000)
    assert crop is not None
    x1, y1, x2, y2 = crop
    assert x1 < 100 and y1 < 100 and x2 > 800 and y2 > 800

    # a zone covering the whole frame is not worth cropping
    full = [
        ZoneConfig.model_validate(
            {"id": "f", "name": "F", "polygon": [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]]}
        )
    ]
    assert zone_crop_box(full, 1000, 1000) is None
    assert zone_crop_box([], 1000, 1000) is None


def test_shift_detection_moves_box_back_to_full_frame():
    from app.detection import Detection, shift_detection

    det = Detection(class_id=0, class_name="person", confidence=0.9, box=Box(10, 20, 30, 40))
    shifted = shift_detection(det, 100, 200)
    assert shifted.box.as_tuple() == (110, 220, 130, 240)
    assert shift_detection(det, 0, 0) is det


def test_crop_to_zones_sends_a_slice_and_shifts_boxes_back(image_path, tmp_path):
    """A small zone must produce a cropped inference frame and full-frame boxes."""
    from app.zones import zone_crop_box

    runtime = make_runtime(image_path, tmp_path)
    runtime.config.detector.crop_to_zones = True
    camera = runtime.config.camera("cam_anh")
    assert camera is not None
    # shrink the zone to the right half so a crop actually happens
    camera.zones[0].polygon = [[0.55, 0.10], [0.95, 0.10], [0.95, 0.55], [0.55, 0.55]]
    runtime._rebuild_engines()
    crop = zone_crop_box(camera.zones, 320, 240)
    assert crop is not None and crop[2] - crop[0] < 320

    seen: list[tuple[int, ...]] = []
    detector = FakeDetector()
    original_detect = detector.detect

    def recording_detect(frame, *, confidence=None):  # noqa: ANN001
        seen.append(frame.shape)
        return original_detect(frame, confidence=confidence)

    detector.detect = recording_detect  # type: ignore[method-assign]

    runtime.start()
    try:
        assert wait_for(lambda: runtime.capture.get("cam_anh").latest()) is not None
        runtime.detector = detector
        runtime.detector_enabled = True
        runtime._start_inference("cam_anh")
        assert wait_for(lambda: seen, timeout=6), "detector chưa được gọi"

        height, width = seen[0][:2]
        assert width < 320 or height < 240, f"phải suy luận trên ảnh đã cắt, nhận {seen[0]}"

        assert wait_for(
            lambda: runtime.camera_view("cam_anh")["zone_states"]["khu_bep"]["occupied"] is True
        ), "vẫn phải phát hiện người trong vùng sau khi cắt"
        detection = runtime.camera_view("cam_anh")["detections"][0]
        box = detection["box"]
        assert 0 <= box[0] < box[2] <= 320 and 0 <= box[1] < box[3] <= 240, box
    finally:
        runtime.stop()


def test_zone_enter_event_is_recorded_on_first_occupancy(image_path, tmp_path):
    runtime = make_runtime(image_path, tmp_path)
    runtime.start()
    try:
        assert wait_for(lambda: runtime.capture.get("cam_anh").latest()) is not None
        runtime.detector = FakeDetector()
        runtime.detector_enabled = True
        runtime._start_inference("cam_anh")

        event = wait_for(lambda: (runtime.events.recent(kind="zone_enter") or [None])[0])
        assert event is not None, "phải ghi lại sự kiện có người vào vùng"
        assert "Khu bếp" in event["message"]
        assert event["data"]["zone"] == "cam_anh/khu_bep"
        # no spurious exit at boot
        assert runtime.events.recent(kind="zone_exit") == []
    finally:
        runtime.stop()


def test_with_webcam_on_fresh_config_wires_test_camera_engines(tmp_path, monkeypatch):
    """``--with-webcam`` on a brand-new config must also wire zones + scenario.

    Regression: ``ensure_test_camera`` appends the camera before the config
    listener is registered, so engines were built from the empty config and the
    test camera captured frames without ever evaluating a zone.
    """
    cfg = AppConfig(cameras=[], scenarios=[], setup_completed=False)
    config_path = tmp_path / "config.yaml"
    save_config(cfg, config_path)
    manager = ConfigManager(config_path, cfg)
    runtime = Runtime(manager, enable_detector=False, with_webcam=True)
    runtime.mqtt.start = lambda: None  # type: ignore[method-assign] - no broker in tests
    runtime.mqtt.stop = lambda publish_offline=True: None  # type: ignore[method-assign]
    monkeypatch.setattr("app.runtime.request_camera_permission", lambda *a, **k: (True, "ok"))
    monkeypatch.setattr(
        Runtime,
        "webcams",
        lambda self, force=False: [
            {"index": 0, "name": "FaceTime HD Camera", "builtin": True, "is_iphone": False, "recommended": True}
        ],
    )
    runtime.start()
    try:
        assert runtime.config.camera("cam_test_laptop") is not None
        engine = runtime._zone_engines.get("cam_test_laptop")
        assert engine is not None, "phải có zone engine cho camera test vừa thêm"
        assert "khuvuc_test" in engine.zone_ids()
        ids = [entry["id"] for entry in runtime.scenario_engine.status()]
        assert "test_den_bep" in ids, f"kịch bản test chưa được nạp: {ids}"
        state_scenarios = runtime.snapshot_state()["scenarios"]
        assert [entry["id"] for entry in state_scenarios] == ["test_den_bep"]
        assert state_scenarios[0]["camera_id"] == "cam_test_laptop"
        assert state_scenarios[0]["test_mode"] == "prefix"
    finally:
        runtime.stop()


# --------------------------------------------------------------------------- #
# hot reload must never break the pipeline (regression: IndexError)
# --------------------------------------------------------------------------- #
def test_on_config_change_does_not_crash_on_ids_without_dash(image_path, tmp_path) -> None:
    """Camera id như ``live_sdp`` (không có dấu '-') từng làm chết listener.

    Lỗi đó khiến camera thêm từ Web UI không có luồng suy luận → vùng đứng ở
    "Đang xác định…" mãi mãi.
    """
    cfg = build_config(image_path, camera_id="live_sdp")
    config_path = tmp_path / "config.yaml"
    save_config(cfg, config_path)
    manager = ConfigManager(config_path, cfg)
    runtime = Runtime(manager, enable_detector=True)
    runtime.mqtt.start = lambda: None  # type: ignore[method-assign]
    runtime.mqtt.stop = lambda publish_offline=True: None  # type: ignore[method-assign]
    runtime.detector = FakeDetector()
    runtime.detector_enabled = True
    try:
        runtime._on_config_change(cfg, cfg)  # type: ignore[arg-type]
        assert "live_sdp" in runtime._infer_threads
        assert runtime._infer_threads["live_sdp"].is_alive()
    finally:
        runtime.stop()


def test_on_config_change_runs_remaining_steps_after_a_failure(image_path, tmp_path) -> None:
    """Một bước lỗi không được bỏ qua các bước sau (kịch bản, giả lập…)."""
    runtime = make_runtime(image_path, tmp_path)
    called: list[str] = []

    def boom() -> None:
        raise RuntimeError("bước hỏng")

    runtime._rebuild_engines = boom  # type: ignore[method-assign]
    runtime.scenario_engine.reconfigure = lambda scenarios: called.append("kịch bản")  # type: ignore[method-assign]
    runtime.simulator.bind_engines = lambda getter: called.append("giả lập")  # type: ignore[method-assign]
    runtime._on_config_change(runtime.config, runtime.config)
    assert called == ["kịch bản", "giả lập"]
    assert runtime.events.recent(level="error")


def test_sync_inference_stops_threads_of_removed_cameras(image_path, tmp_path) -> None:
    runtime = make_runtime(image_path, tmp_path)
    runtime.detector = FakeDetector()
    runtime.detector_enabled = True
    try:
        runtime._sync_inference(runtime.config)
        assert "cam_anh" in runtime._infer_threads
        runtime._sync_inference(AppConfig.model_validate({"cameras": []}))
        assert "cam_anh" not in runtime._infer_threads
    finally:
        runtime.stop()


# --------------------------------------------------------------------------- #
# Hồi quy: lỗi suy luận KHÔNG được biến thành "vùng trống" (tắt đèn oan)
# --------------------------------------------------------------------------- #
def test_inference_failure_does_not_make_zone_vacant(image_path, tmp_path) -> None:
    runtime = make_runtime(image_path, tmp_path)

    class BrokenDetector:
        name = "broken"
        available = True

        def class_names(self) -> dict[int, str]:
            return {0: "person"}

        def detect(self, frame, *, confidence=None):  # noqa: ANN001
            raise RuntimeError("model hỏng")

        def info(self) -> dict:
            return {"name": "broken", "available": True, "device": "cpu"}

        def close(self) -> None:
            return None

    runtime.detector = BrokenDetector()
    runtime._rebuild_engines()  # noqa: SLF001 - dựng engine cho camera như lúc start()
    camera = runtime.config.cameras[0]
    engine = runtime._zone_engines[camera.id]  # noqa: SLF001
    zone_id = camera.zones[0].id

    frame = np.zeros((240, 320, 3), dtype=np.uint8)
    packet = FramePacket(seq=1, frame=frame, captured_at=0.0, wall_time=0.0, width=320, height=240)

    # Người đang ở trong vùng (đã xác nhận bằng các khung trước đó).
    engine.force_zone(zone_id, True, 1)
    assert engine.evaluate(0.0, [], 320, 240)[0].occupied is True

    runtime._process_detections(  # noqa: SLF001
        camera, packet, [], 10.0, 4.0, False, sampled=False
    )
    state = runtime._cameras[camera.id].zones[zone_id]  # noqa: SLF001
    assert state.occupied is True, "lỗi suy luận bị hiểu thành 'không có ai'"
    diagnostics = runtime.diagnostics()
    assert diagnostics["cameras"][camera.id]["sampled_frame"] is False


def test_diagnostics_reports_reasons_and_counters(image_path, tmp_path) -> None:
    runtime = make_runtime(image_path, tmp_path)
    runtime._note_inference_error("cam_anh", RuntimeError("CUDA out of memory"))  # noqa: SLF001
    diagnostics = runtime.diagnostics()
    camera_row = diagnostics["cameras"]["cam_anh"]
    assert camera_row["inference_errors"] == 1
    assert "CUDA out of memory" in (camera_row["inference_last_error"] or "")
    assert diagnostics["scenarios"], "phải có 'reason' cho từng kịch bản"
    assert all("reason" in row and row["reason"] for row in diagnostics["scenarios"])
    assert "cameras" in diagnostics and "decisions" in diagnostics and "warnings" in diagnostics


# --------------------------------------------------------------------------- #
# camera bị chủ động tắt KHÔNG được báo là "mất hình"
# --------------------------------------------------------------------------- #
def test_disabled_camera_is_reported_as_off_not_lost(image_path, tmp_path) -> None:
    """Tắt camera trong Cài đặt mà bảng chẩn đoán lại báo "mất hình" (đỏ).

    Người dùng đọc đó là sự cố hệ thống. Nay ``status`` do máy chủ quyết định:
    ``off`` (tắt theo ý người dùng) khác hẳn ``lost`` (mất hình thật).
    """
    runtime = make_runtime(image_path, tmp_path)
    runtime.start()
    try:
        assert wait_for(lambda: runtime.capture.get("cam_anh").latest()) is not None
        assert runtime.diagnostics()["cameras"]["cam_anh"]["status"] == "ok"

        assert runtime.set_camera_enabled("cam_anh", False)["ok"] is True
        diag = runtime.diagnostics()["cameras"]["cam_anh"]
        assert diag["enabled"] is False
        assert diag["status"] == "off"
        assert runtime.camera_view("cam_anh")["status"] == "off"

        assert runtime.set_camera_enabled("cam_anh", True)["ok"] is True
        assert wait_for(lambda: runtime.diagnostics()["cameras"]["cam_anh"]["status"] in {"ok", "starting"})
    finally:
        runtime.stop()


def test_camera_status_distinguishes_lost_from_starting(image_path, tmp_path) -> None:
    runtime = make_runtime(image_path, tmp_path)
    camera = runtime.config.camera("cam_anh")
    assert runtime._camera_status(camera, {"connected": False, "error": None}) == "starting"
    assert runtime._camera_status(camera, {"connected": False, "error": "Mất tín hiệu"}) == "lost"
    assert runtime._camera_status(camera, {"connected": True, "stale": True}) == "stale"
    assert runtime._camera_status(camera, {"connected": True, "stale": False}) == "ok"


# --------------------------------------------------------------------------- #
# bộ nhớ / ổ đĩa: dọn trạng thái camera đã xoá + dọn nhật ký cũ
# --------------------------------------------------------------------------- #
def test_deleting_a_camera_drops_its_runtime_state(image_path, tmp_path) -> None:
    """Xoá camera phải xoá luôn trạng thái runtime của nó (chống rò rỉ bộ nhớ)."""
    runtime = make_runtime(image_path, tmp_path)
    runtime.start()
    try:
        assert wait_for(lambda: runtime.capture.get("cam_anh").latest()) is not None
        runtime.detector = FakeDetector()
        runtime.detector_enabled = True
        runtime._start_inference("cam_anh")
        assert wait_for(
            lambda: runtime.camera_view("cam_anh")["zone_states"].get("khu_bep", {}).get("occupied")
        )

        runtime._infer_errors["cam_anh"] = 2
        runtime._infer_last_error["cam_anh"] = "boom"
        runtime._state_since_wall["cam_anh/khu_bep"] = 1.0
        assert "cam_anh" in runtime._cameras

        assert runtime.delete_camera("cam_anh")["ok"] is True

        assert "cam_anh" not in runtime._cameras
        assert "cam_anh" not in runtime._infer_errors
        assert "cam_anh" not in runtime._infer_last_error
        assert not [key for key in runtime._state_since_wall if key.startswith("cam_anh/")]
        assert runtime.diagnostics()["cameras"] == {}
        assert runtime.scenario_engine.status() == []
    finally:
        runtime.stop()


def test_housekeeping_deletes_logs_older_than_retention(image_path, tmp_path) -> None:
    """``logging.retention_days`` (mặc định 30) phải thực sự xoá file cũ."""
    runtime = make_runtime(image_path, tmp_path)
    logs = tmp_path / "logs"
    logs.mkdir(exist_ok=True)
    old = logs / "vision-automation.log.5"
    old.write_text("cu", encoding="utf-8")
    import os

    stamp = time.time() - 40 * 86400
    os.utime(old, (stamp, stamp))
    fresh = logs / "loopback-ffmpeg.log"
    fresh.write_text("moi", encoding="utf-8")

    runtime.config.logging.file = str(logs / "vision-automation.log")
    runtime.config.logging.retention_days = 30
    runtime.config.web.snapshot_dir = str(tmp_path / "snapshots")

    report = runtime.run_housekeeping(force=True)

    assert report["removed"] == 1
    assert not old.exists()
    assert fresh.exists()
    assert runtime.config.logging.retention_days == 30


def test_housekeeping_keeps_the_current_log_file(image_path, tmp_path) -> None:
    """File nhật ký đang ghi không bao giờ bị xoá, kể cả khi mtime cũ."""
    runtime = make_runtime(image_path, tmp_path)
    logs = tmp_path / "logs"
    logs.mkdir(exist_ok=True)
    current = logs / "vision-automation.log"
    current.write_text("dang ghi", encoding="utf-8")
    import os

    stamp = time.time() - 400 * 86400
    os.utime(current, (stamp, stamp))

    runtime.config.logging.file = str(current)
    runtime.config.web.snapshot_dir = str(tmp_path / "snapshots")
    report = runtime.run_housekeeping(force=True)

    assert current.exists()
    assert report["removed"] == 0


def test_diagnostics_reports_resources(image_path, tmp_path) -> None:
    runtime = make_runtime(image_path, tmp_path)
    runtime.config.logging.file = str(tmp_path / "logs" / "vision-automation.log")
    runtime.config.web.snapshot_dir = str(tmp_path / "snapshots")

    resources = runtime.diagnostics()["resources"]

    assert resources["retention_days"] == 30
    assert resources["disk"]["ok"] is True
    assert resources["logs"]["files"] >= 0
    assert "rss_mb" in resources
    assert resources["pid"] > 0
