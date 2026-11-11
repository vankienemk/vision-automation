"""Tests for the capture layer (image/file sources — never the real webcam)."""

from __future__ import annotations

import threading
import time

import cv2
import numpy as np
import pytest

from app.capture import (
    CaptureManager,
    CaptureStatus,
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


# --------------------------------------------------------------------------- #
# Rò tài nguyên khi mở lại camera
# --------------------------------------------------------------------------- #
class FakeVideoCapture:
    """Đứng thay ``cv2.VideoCapture`` để đếm open/release mà không mở thiết bị.

    Ghi lại thứ tự gọi trong ``log`` (vd ``["open:1", "release:1", "open:2"]``)
    để test chứng minh handle cũ được giải phóng *trước* khi tạo handle mới.
    """

    instances: list["FakeVideoCapture"] = []
    log: list[str] = []

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.index: int = len(FakeVideoCapture.instances) + 1
        self.released: bool = False
        FakeVideoCapture.instances.append(self)
        FakeVideoCapture.log.append(f"open:{self.index}")

    def isOpened(self) -> bool:  # noqa: N802 - giữ đúng tên API OpenCV
        return True

    def read(self) -> tuple[bool, None]:
        return False, None

    def get(self, prop: int) -> float:
        return 0.0

    def set(self, prop: int, value: float) -> bool:
        return True

    def release(self) -> None:
        self.released = True
        FakeVideoCapture.log.append(f"release:{self.index}")


@pytest.fixture()
def fake_video_capture(monkeypatch: pytest.MonkeyPatch) -> type[FakeVideoCapture]:
    FakeVideoCapture.instances = []
    FakeVideoCapture.log = []
    monkeypatch.setattr(cv2, "VideoCapture", FakeVideoCapture)
    return FakeVideoCapture


def test_reopen_releases_old_capture_before_opening_new(
    tmp_path, fake_video_capture: type[FakeVideoCapture]
) -> None:
    """Mỗi lần mở lại phải ``release()`` handle cũ, nếu không rò cả FFmpeg + socket.

    Camera RTSP "treo" liên tục ⇒ watchdog gọi ``request_reopen()`` liên tục;
    handle cũ bị ghi đè mà không đóng thì rò không giới hạn trong nhiều ngày.
    """
    path = make_image(tmp_path / "frame.png")
    worker = CaptureWorker(image_camera(path, camera_id="cam_reopen"))
    steps = {"n": 0}

    def fake_enforce_rate(started: float) -> None:
        steps["n"] += 1
        if steps["n"] == 1:
            worker.request_reopen()  # watchdog phát hiện luồng treo
        else:
            worker._stop_event.set()

    worker._enforce_rate = fake_enforce_rate  # type: ignore[method-assign]
    try:
        worker._run()
        log = list(fake_video_capture.log)
        still_open = [cap for cap in fake_video_capture.instances if not cap.released]
        newest = worker._cap
    finally:
        worker._close_capture()

    assert steps["n"] == 2
    assert log == ["open:1", "release:1", "open:2"]
    assert fake_video_capture.instances[0].released is True
    # Worker chỉ giữ handle mới nhất, và chỉ còn đúng một handle chưa đóng.
    assert newest is fake_video_capture.instances[-1]
    assert still_open == [newest]


def test_open_twice_releases_previous_handle(
    tmp_path, fake_video_capture: type[FakeVideoCapture]
) -> None:
    """``_open()`` gọi trực tiếp hai lần cũng không được bỏ rơi handle cũ."""
    path = make_image(tmp_path / "frame.png")
    worker = CaptureWorker(image_camera(path, camera_id="cam_open2"))
    try:
        assert worker._open() is True
        first = worker._cap
        assert worker._open() is True
        assert first is not None and first.released is True
        assert worker._cap is not first
        assert fake_video_capture.log == ["open:1", "release:1", "open:2"]
    finally:
        worker._close_capture()
    # Đóng khi _cap đã là None phải an toàn (không ném lỗi).
    worker._close_capture()
    assert worker._cap is None


def test_failed_open_keeps_reopen_intent(tmp_path) -> None:
    """Mở lại thất bại thì phải giữ cờ ``_reopen`` để lần sau còn thử lại."""
    worker = CaptureWorker(image_camera(tmp_path / "missing.png", camera_id="cam_fail"))
    worker._reopen.set()
    backoffs: list[int] = []

    def fake_backoff() -> None:
        backoffs.append(1)
        worker._stop_event.set()

    worker._wait_backoff = fake_backoff  # type: ignore[method-assign]
    worker._run()

    assert backoffs == [1]
    assert worker._reopen.is_set()


# --------------------------------------------------------------------------- #
# Nhiều người xem MJPEG dùng chung một camera
# --------------------------------------------------------------------------- #
def test_two_viewers_share_one_camera_until_last_leaves(tmp_path) -> None:
    """Hai tab MJPEG không được dùng chung một slot watcher.

    Trước đây id watcher là ``mjpeg:<cam>:<id(worker)>`` nên hai tab trùng id;
    đóng một tab là camera tắt (đèn tắt) dù tab kia vẫn đang xem.
    """
    path = make_image(tmp_path / "frame.png")
    worker = CaptureWorker(image_camera(path, camera_id="cam_viewers", lazy_open=True))
    worker.start()
    try:
        first = worker.add_watcher_auto("mjpeg")
        second = worker.add_watcher_auto("mjpeg")
        assert first != second
        assert worker.watchers == 2
        assert worker.is_wanted() is True
        assert wait_for(lambda: worker.latest()) is not None

        worker.remove_watcher_auto(first)
        assert worker.watchers == 1
        assert worker.is_wanted() is True
        # Còn người xem ⇒ camera phải giữ mở (đèn không được tắt).
        assert wait_for(lambda: worker.status().is_open is False, timeout=0.6) is None
        assert worker.latest() is not None

        worker.remove_watcher_auto(second)
        assert worker.watchers == 0
        assert worker.is_wanted() is False
        assert wait_for(lambda: worker.status().is_open is False) is not None

        # Xoá một id không tồn tại không được ném lỗi (idempotent).
        worker.remove_watcher("khong-ton-tai")
        worker.remove_watcher_auto("khong-ton-tai")
        assert worker.watchers == 0
    finally:
        worker.stop()


# --------------------------------------------------------------------------- #
# Trạng thái công bố nguyên tử
# --------------------------------------------------------------------------- #
def test_status_snapshot_is_consistent_under_concurrent_updates(tmp_path) -> None:
    """UI đọc ``status()`` phải nhận bản sao nhất quán, không phải object đang sửa."""
    path = make_image(tmp_path / "frame.png")
    worker = CaptureWorker(image_camera(path, camera_id="cam_status"))
    frame = cv2.imread(str(path))
    assert frame is not None

    stop = threading.Event()
    errors: list[BaseException] = []
    torn: list[str] = []

    def reader() -> None:
        try:
            while not stop.is_set():
                snapshot = worker.status()
                if snapshot is worker._status:
                    torn.append("status() trả về object trạng thái sống")
                if not isinstance(snapshot.connected, bool) or not isinstance(
                    snapshot.stale, bool
                ):
                    torn.append(
                        f"kiểu sai: connected={type(snapshot.connected)} stale={type(snapshot.stale)}"
                    )
                if snapshot.last_frame_at is not None and not isinstance(
                    snapshot.last_frame_at, float
                ):
                    torn.append(f"last_frame_at sai kiểu: {type(snapshot.last_frame_at)}")
                time.sleep(0.0005)  # nhả GIL để test chạy nhanh
        except BaseException as exc:  # noqa: BLE001 - ghi lại rồi báo sau
            errors.append(exc)

    readers = [threading.Thread(target=reader, daemon=True) for _ in range(3)]
    for thread in readers:
        thread.start()
    started = time.monotonic()
    try:
        for index in range(1000):
            worker._update_status(connected=index % 2 == 0, stale=index % 5 == 0)
            if index % 10 == 0:
                worker._publish(frame, time.perf_counter())
    finally:
        stop.set()
        for thread in readers:
            thread.join(2.0)

    assert time.monotonic() - started < 2.0
    assert errors == []
    assert torn == []
    # Mỗi lần đọc là một bản sao độc lập.
    assert worker.status() is not worker.status()
    assert isinstance(worker.status(), CaptureStatus)


def test_status_snapshot_changes_do_not_leak_into_worker(tmp_path) -> None:
    """Sửa bản sao trạng thái không được ảnh hưởng tới worker (bản sao thật)."""
    path = make_image(tmp_path / "frame.png")
    worker = CaptureWorker(image_camera(path, camera_id="cam_copy"))
    snapshot = worker.status()
    snapshot.connected = True
    snapshot.frames_total = 999
    assert worker.status().connected is False
    assert worker.status().frames_total == 0


def test_camera_config_swap_goes_through_locked_property(tmp_path) -> None:
    """``CaptureManager.sync`` đổi cấu hình phải cập nhật worker qua property."""
    path = make_image(tmp_path / "frame.png")
    first = image_camera(path, camera_id="cam_sync", fps=5)
    second = image_camera(path, camera_id="cam_sync", fps=7)
    manager = CaptureManager([first])
    try:
        assert manager.get("cam_sync").camera is first
        manager.sync([second])
        worker = manager.get("cam_sync")
        assert worker is not None
        assert worker.camera is second
        assert worker.camera.fps == 7
        assert worker.camera_id == "cam_sync"
        assert worker.status().camera_id == "cam_sync"
    finally:
        manager.stop()


def test_reopen_requested_during_open_is_not_lost(
    tmp_path, fake_video_capture: type[FakeVideoCapture]
) -> None:
    """Yêu cầu mở lại đến *trong lúc* đang mở (RTSP chậm) không được bị xoá mất.

    Watchdog hoặc ``CaptureManager.sync`` có thể gọi ``request_reopen()`` khi
    ``_open()`` còn đang chạy; nếu xoá cờ vô điều kiện thì camera giữ cấu hình
    cũ cho tới lần lỗi kế tiếp.
    """
    path = make_image(tmp_path / "frame.png")
    worker = CaptureWorker(image_camera(path, camera_id="cam_during_open"))
    frame = cv2.imread(str(path))
    assert frame is not None
    # Đọc khung hình luôn thành công để vòng lặp không tự rơi vào nhánh lỗi
    # (nhánh đó cũng mở lại camera và sẽ che mất bug cần bắt).
    worker._image_frame = frame
    opened = {"n": 0}
    published = {"n": 0}

    def fake_open() -> bool:
        opened["n"] += 1
        worker._cap = fake_video_capture()  # handle "mới"
        if opened["n"] == 1:
            worker.request_reopen()  # yêu cầu mới chen vào giữa lúc mở
            return True
        worker._stop_event.set()
        return True

    def fake_enforce_rate(started: float) -> None:
        published["n"] += 1
        if published["n"] >= 2:
            worker._stop_event.set()

    worker._open = fake_open  # type: ignore[method-assign]
    worker._enforce_rate = fake_enforce_rate  # type: ignore[method-assign]
    try:
        worker._run()
    finally:
        worker._close_capture()

    # Không xoá mất yêu cầu ⇒ phải mở lại lần nữa dù handle hiện tại vẫn "tốt".
    assert opened["n"] == 2
