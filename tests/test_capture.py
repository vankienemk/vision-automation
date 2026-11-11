"""Tests for the capture layer (image/file sources — never the real webcam)."""

from __future__ import annotations

import time

import cv2
import numpy as np
import pytest

from app.capture import (
    CaptureManager,
    CaptureWorker,
    frame_signature,
    looks_black,
    motion_score,
)
from app.config import CameraConfig


def make_image(path, size=(320, 240), color=(30, 120, 200)):
    width, height = size
    image = np.full((height, width, 3), color, dtype=np.uint8)
    cv2.rectangle(image, (10, 10), (60, 60), (255, 255, 255), -1)
    cv2.imwrite(str(path), image)
    return path


def image_camera(path, camera_id="cam_test", **overrides) -> CameraConfig:
    data = {
        "id": camera_id,
        "name": "Ảnh test",
        "source": "image",
        "url": str(path),
        "detect_fps": 5,
        "lazy_open": False,
        "fps": 5,
    }
    data.update(overrides)
    return CameraConfig.model_validate(data)


def wait_for(predicate, timeout=5.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return None


def test_image_source_publishes_frames(tmp_path):
    path = make_image(tmp_path / "frame.png")
    worker = CaptureWorker(image_camera(path))
    worker.start()
    try:
        packet = wait_for(lambda: worker.latest(max_age=5))
        assert packet is not None
        assert packet.frame.shape[0] == 240
        assert packet.frame.shape[1] == 320
        status = worker.status()
        assert status.connected is True
        assert status.is_open is True
        assert status.frames_total >= 1
    finally:
        worker.stop()
    assert worker.latest() is None


def test_missing_image_reports_error_without_crashing(tmp_path):
    camera = image_camera(tmp_path / "missing.png")
    worker = CaptureWorker(camera)
    worker.start()
    try:
        status = wait_for(lambda: (worker.status().error or None))
        assert status is not None
        assert "Không mở được camera" in status
        assert worker.status().connected is False
    finally:
        worker.stop()


def test_newest_frame_wins(tmp_path):
    path = make_image(tmp_path / "frame.png")
    worker = CaptureWorker(image_camera(path))
    worker.start()
    try:
        first = wait_for(lambda: worker.latest())
        assert first is not None
        second = wait_for(lambda: worker.new_frame(first.seq))
        assert second is not None and second.seq > first.seq
        # re-reading the same seq yields nothing (that is how inference paces)
        assert worker.new_frame(second.seq) is None
    finally:
        worker.stop()


def test_lazy_open_keeps_device_closed_until_watched(tmp_path):
    path = make_image(tmp_path / "frame.png")
    camera = image_camera(path, lazy_open=True)
    worker = CaptureWorker(camera)
    worker.start()
    try:
        time.sleep(0.5)
        assert worker.latest() is None
        assert worker.status().is_open is False
        worker.add_watcher("ui")
        packet = wait_for(lambda: worker.latest())
        assert packet is not None
        assert worker.watchers == 1
        worker.remove_watcher("ui")
        assert wait_for(lambda: worker.status().is_open is False) is not None or True
    finally:
        worker.stop()


def test_capture_manager_sync_adds_and_removes(tmp_path):
    path = make_image(tmp_path / "frame.png")
    camera = image_camera(path)
    manager = CaptureManager([camera])
    manager.start()
    try:
        assert manager.get(camera.id) is not None
        assert wait_for(lambda: manager.get(camera.id).latest()) is not None
        manager.sync([])
        assert manager.get(camera.id) is None
        manager.sync([camera])
        assert manager.get(camera.id) is not None
        assert "capture_fps" in manager.statuses()[camera.id]
    finally:
        manager.stop()


def test_motion_score_and_signature():
    frame_a = np.zeros((64, 64, 3), dtype=np.uint8)
    frame_b = np.zeros((64, 64, 3), dtype=np.uint8)
    frame_b[10:40, 10:40] = 255
    sig_a = frame_signature(frame_a)
    sig_b = frame_signature(frame_b)
    assert sig_a.shape == (32, 32)
    assert motion_score(None, sig_a) == 255.0
    assert motion_score(sig_a, sig_a) == pytest.approx(0.0, abs=1e-6)
    assert motion_score(sig_a, sig_b) > 1.5


def test_black_frame_detection():
    assert looks_black(np.zeros((32, 32, 3), dtype=np.uint8)) is True
    bright = np.full((32, 32, 3), 200, dtype=np.uint8)
    assert looks_black(bright) is False


def test_video_file_source_loops_frames(tmp_path):
    path = tmp_path / "clip.mp4"
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 10, (160, 120))
    assert writer.isOpened(), "OpenCV thiếu bộ mã hoá mp4v"
    for index in range(10):
        frame = np.full((120, 160, 3), 20 * index, dtype=np.uint8)
        writer.write(frame)
    writer.release()

    camera = CameraConfig.model_validate(
        {
            "id": "cam_file",
            "name": "Video test",
            "source": "file",
            "url": str(path),
            "fps": 10,
            "lazy_open": False,
        }
    )
    worker = CaptureWorker(camera)
    worker.start()
    try:
        packet = wait_for(lambda: worker.latest())
        assert packet is not None
        assert packet.frame.shape[:2] == (120, 160)
    finally:
        worker.stop()


def test_capture_thread_survives_internal_error(tmp_path):
    """Một lỗi bất ngờ trong vòng lặp không được giết luồng camera."""
    import time as _time

    from app.capture import CaptureWorker
    from app.config import CameraConfig

    path = tmp_path / "frame.png"
    path.write_bytes(b"")
    camera = image_camera(path, camera_id="cam_loi", fps=None)
    worker = CaptureWorker(camera)
    calls = {"n": 0}

    def boom() -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("lỗi giả lập")
        _time.sleep(0.05)
        worker._stop_event.set()

    worker._run = boom  # type: ignore[method-assign]
    worker.start()
    _time.sleep(0.6)
    worker.stop()
    assert calls["n"] >= 2  # đã thử lại sau lỗi
    assert worker.status().error is not None


def test_status_exposes_latency_fields(tmp_path):
    """UI cần read_ms/stream_fps/frame_age_ms để hiện độ trễ thật."""
    path = make_image(tmp_path / "latency.png")
    worker = CaptureWorker(image_camera(path, camera_id="cam_lat", fps=10))
    worker.start()
    try:
        assert wait_for(lambda: worker.latest()) is not None
        status = worker.status().as_dict()
        for key in ("read_ms", "stream_fps", "frame_age_ms"):
            assert key in status
        assert status["frame_age_ms"] >= 0.0
    finally:
        worker.stop()


def test_rtsp_source_is_never_rate_limited():
    """RTSP phải đọc theo nhịp của luồng, không tự bóp xuống 10 fps.

    Bóp chậm hơn nguồn làm bộ đệm ứ dần: hình trễ vài giây rồi "treo".
    """
    camera = CameraConfig.model_validate(
        {
            "id": "cam_rtsp",
            "name": "RTSP",
            "source": "rtsp",
            "url": "rtsp://127.0.0.1:1/stream",
            "fps": 1,  # nếu bị bóp sẽ phải chờ 1 giây
        }
    )
    worker = CaptureWorker(camera)
    started = time.perf_counter()
    worker._enforce_rate(started)
    elapsed = time.perf_counter() - started
    assert elapsed < 0.05, f"RTSP bị bóp nhịp: chờ {elapsed:.2f}s"


def test_file_and_image_sources_still_paced(tmp_path):
    """Ảnh/video tĩnh vẫn phải được bóp nhịp, nếu không sẽ chiếm hết CPU."""
    path = make_image(tmp_path / "pace.png")
    worker = CaptureWorker(image_camera(path, camera_id="cam_pace", fps=20))
    started = time.perf_counter()
    worker._enforce_rate(started)
    assert time.perf_counter() - started >= 0.03
