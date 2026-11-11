"""Test bộ kiểm tra kết nối camera (``app/netcheck.py``).

Mọi phép thử ở đây đều GIẢ LẬP socket — không mở kết nối mạng thật, không bật
webcam — nên chạy được ở mọi máy và không phụ thuộc camera của người dùng.
"""

from __future__ import annotations

import socket

import pytest

from app.config import CameraConfig
from app.netcheck import check_camera_source, check_rtsp, parse_rtsp_url

URL = "rtsp://admin:matkhau@192.168.1.50:554/stream1"


# --------------------------------------------------------------------------- #
# 1. Tách URL
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("rtsp://cam.local/stream", ("rtsp", "cam.local", 554)),
        ("rtsp://cam.local:8554/live.sdp", ("rtsp", "cam.local", 8554)),
        ("rtsps://cam.local/x", ("rtsps", "cam.local", 322)),
        ("http://cam.local/x", ("http", "cam.local", 80)),
    ],
)
def test_parse_rtsp_url(url, expected):
    assert parse_rtsp_url(url) == expected


@pytest.mark.parametrize("url", ["", "khong-phai-url", "ftp://cam/x", "rtsp:///x"])
def test_parse_rtsp_url_rejects_bad_values(url):
    assert parse_rtsp_url(url) is None


# --------------------------------------------------------------------------- #
# 2. Socket giả để điều khiển từng bước
# --------------------------------------------------------------------------- #
class FakeSocket:
    def __init__(self, reply: bytes = b"", *, raise_on_recv: Exception | None = None):
        self.reply = reply
        self.raise_on_recv = raise_on_recv
        self.sent = b""
        self.closed = False

    def settimeout(self, _timeout):  # noqa: D401 - API của socket
        return None

    def sendall(self, data: bytes) -> None:
        self.sent += data

    def recv(self, _size: int) -> bytes:
        if self.raise_on_recv is not None:
            raise self.raise_on_recv
        block, self.reply = self.reply, b""
        return block

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.closed = True
        return False


def _factory(sock=None, *, error: Exception | None = None):
    def make(_address, timeout=None):  # noqa: ARG001 - chữ ký giống create_connection
        if error is not None:
            raise error
        return sock

    return make


def test_dns_failure_says_which_host(monkeypatch):
    def boom(*_a, **_k):
        raise socket.gaierror("nodename nor servname provided")

    monkeypatch.setattr(socket, "getaddrinfo", boom)
    result = check_rtsp("rtsp://khong-co-that.local/live", socket_factory=_factory(FakeSocket()))
    assert result.ok is False
    assert result.stage == "dns"
    assert "khong-co-that.local" in result.message
    assert result.hint and "IP" in result.hint


def test_tcp_timeout_is_reported_as_unreachable():
    result = check_rtsp(URL, socket_factory=_factory(error=TimeoutError("timed out")))
    assert result.ok is False
    assert result.stage == "tcp"
    assert "Không tới được" in result.message
    assert "tường lửa" in (result.hint or "")
    # Không được để lộ mật khẩu ra UI/log
    assert "matkhau" not in str(result.as_dict())


def test_connection_refused_points_at_the_port():
    result = check_rtsp(URL, socket_factory=_factory(error=ConnectionRefusedError("refused")))
    assert result.ok is False
    assert result.stage == "tcp"
    assert "KHÔNG có dịch vụ RTSP" in result.message
    assert "554" in result.message


def test_rtsp_ok_reports_success():
    sock = FakeSocket(b"RTSP/1.0 200 OK\r\nCSeq: 1\r\nPublic: OPTIONS, DESCRIBE\r\n\r\n")
    result = check_rtsp(URL, socket_factory=_factory(sock))
    assert result.ok is True
    assert result.stage == "ok"
    assert "thành công" in result.message
    # Đã gửi đúng câu OPTIONS của giao thức RTSP
    assert sock.sent.startswith(b"OPTIONS rtsp://")
    assert b"CSeq: 1" in sock.sent
    assert sock.closed is True


def test_401_means_wrong_password():
    sock = FakeSocket(b"RTSP/1.0 401 Unauthorized\r\nCSeq: 1\r\n\r\n")
    result = check_rtsp(URL, socket_factory=_factory(sock))
    assert result.ok is False
    assert result.stage == "rtsp"
    assert "401" in result.message
    assert "mật khẩu" in result.hint


def test_404_means_wrong_stream_path():
    sock = FakeSocket(b"RTSP/1.0 404 Not Found\r\nCSeq: 1\r\n\r\n")
    result = check_rtsp(URL, socket_factory=_factory(sock))
    assert result.ok is False
    assert "đường dẫn" in (result.hint or "").lower()


def test_non_rtsp_service_on_open_port_is_detected():
    sock = FakeSocket(b"HTTP/1.1 400 Bad Request\r\n\r\n")
    result = check_rtsp(URL, socket_factory=_factory(sock))
    assert result.ok is False
    assert result.stage == "rtsp"
    assert "KHÔNG trả lời bằng giao thức RTSP" in result.message


def test_http_url_is_rejected_with_rtsp_advice():
    result = check_rtsp("http://192.168.1.50/stream", socket_factory=_factory(FakeSocket()))
    assert result.ok is False
    assert result.stage == "url"
    assert "rtsp://" in result.message


# --------------------------------------------------------------------------- #
# 3. Kiểm tra theo loại nguồn của camera
# --------------------------------------------------------------------------- #
def _camera(**data) -> CameraConfig:
    payload = {"id": "cam", "name": "Camera", "source": "rtsp", "url": URL, "zones": []}
    payload.update(data)
    return CameraConfig.model_validate(payload)


def test_image_source_reports_missing_file(tmp_path):
    camera = _camera(source="image", url=str(tmp_path / "khong-co.jpg"))
    result = check_camera_source(camera)
    assert result.ok is False
    assert result.stage == "file"
    assert "Không thấy tệp" in result.message


def test_image_source_ok_when_file_exists(tmp_path):
    target = tmp_path / "anh.jpg"
    target.write_bytes(b"\xff\xd8\xff\xd9")
    result = check_camera_source(_camera(source="image", url=str(target)))
    assert result.ok is True
    assert result.stage == "ok"


def test_webcam_source_denied_gives_permission_hint(monkeypatch):
    from app import platform_compat as compat

    monkeypatch.setattr(compat, "camera_permission_status", lambda: "denied")
    monkeypatch.setattr(compat, "webcam_permission_hint", lambda: "Mở System Settings → Camera")
    result = check_camera_source(_camera(source="webcam", device="auto", url=None))
    assert result.ok is False
    assert result.stage == "webcam"
    assert "CHẶN quyền" in result.message
    assert "System Settings" in (result.hint or "")


def test_webcam_source_reports_available_names(monkeypatch):
    from app import platform_compat as compat

    monkeypatch.setattr(compat, "camera_permission_status", lambda: "authorized")
    monkeypatch.setattr(
        compat,
        "list_webcam_devices",
        lambda: [{"index": 0, "name": "FaceTime HD Camera", "builtin": True}],
    )
    result = check_camera_source(_camera(source="webcam", device="auto", url=None))
    assert result.ok is True
    assert "FaceTime HD Camera" in result.message


def test_webcam_source_warns_when_named_device_is_missing(monkeypatch):
    from app import platform_compat as compat

    monkeypatch.setattr(compat, "camera_permission_status", lambda: "authorized")
    monkeypatch.setattr(
        compat, "list_webcam_devices", lambda: [{"index": 0, "name": "FaceTime HD Camera"}]
    )
    result = check_camera_source(_camera(source="webcam", device="Webcam USB", url=None))
    assert result.ok is False
    assert "Webcam USB" in result.message
    assert "FaceTime HD Camera" in (result.hint or "")
