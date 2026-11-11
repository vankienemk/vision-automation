"""Kiểm tra nhanh một nguồn camera có tới được hay không.

Mục đích: khi camera không lên hình, người dùng cần biết NGAY vấn đề nằm ở đâu
— sai mật khẩu, sai cổng, máy kia tắt, hay tường lửa chặn — thay vì đọc log.
Vì vậy mọi bước đều trả về câu tiếng Việt cụ thể kèm cách sửa.

Module này cố tình KHÔNG import OpenCV, không mở luồng video: chỉ phân giải
tên miền, mở socket TCP ngắn rồi gửi một câu ``OPTIONS`` của giao thức RTSP.
Nhờ vậy nó test được bằng cách giả lập ``socket`` và không bao giờ treo app
(mỗi bước đều có timeout, tổng thời gian tối đa ~ ``timeout`` giây).
"""

from __future__ import annotations

import re
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .config import CameraConfig, mask_url

#: Thời gian tối đa cho một lần kiểm tra (giây) — đủ cho mạng LAN chậm.
DEFAULT_TIMEOUT = 4.0

_RTSP_DEFAULT_PORT = 554
_RTSP_REPLY_RE = re.compile(rb"^RTSP/\d\.\d\s+(\d{3})\s*([^\r\n]*)", re.MULTILINE)


@dataclass
class ConnectionCheck:
    """Kết quả một lần kiểm tra kết nối (hiển thị thẳng lên Web UI)."""

    ok: bool
    stage: str          # url | dns | tcp | rtsp | webcam | file | ok
    message: str        # câu chính, tiếng Việt
    hint: str | None = None
    detail: str | None = None
    elapsed_ms: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "stage": self.stage,
            "message": self.message,
            "hint": self.hint,
            "detail": self.detail,
            "elapsed_ms": self.elapsed_ms,
        }


def _fail(stage: str, message: str, hint: str | None, detail: str | None, started: float) -> ConnectionCheck:
    return ConnectionCheck(
        ok=False,
        stage=stage,
        message=message,
        hint=hint,
        detail=detail,
        elapsed_ms=int((time.monotonic() - started) * 1000),
    )


def _ok(stage: str, message: str, detail: str | None, started: float, hint: str | None = None) -> ConnectionCheck:
    return ConnectionCheck(
        ok=True,
        stage=stage,
        message=message,
        hint=hint,
        detail=detail,
        elapsed_ms=int((time.monotonic() - started) * 1000),
    )


def parse_rtsp_url(url: str) -> tuple[str, int, str] | None:
    """Tách ``(scheme, host, port)`` từ URL; ``None`` nếu URL không hợp lệ.

    ``rtsp://cam.local/stream`` → ``("rtsp", "cam.local", 554)``.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    scheme = (parts.scheme or "").lower()
    if scheme not in {"rtsp", "rtsps", "http", "https"}:
        return None
    host = parts.hostname
    if not host:
        return None
    if parts.port:
        port = parts.port
    elif scheme in {"rtsps", "https"}:
        port = 322
    elif scheme == "http":
        port = 80
    else:
        port = _RTSP_DEFAULT_PORT
    return scheme, host, port


def _rtsp_status(raw: bytes) -> tuple[int, str] | None:
    match = _RTSP_REPLY_RE.search(raw)
    if not match:
        return None
    return int(match.group(1)), match.group(2).decode("utf-8", "replace").strip()


def check_rtsp(
    url: str,
    *,
    transport: str = "tcp",
    timeout: float = DEFAULT_TIMEOUT,
    socket_factory: Any | None = None,
) -> ConnectionCheck:
    """Kiểm tra một URL RTSP tới được tới đâu (DNS → TCP → RTSP)."""
    started = time.monotonic()
    parsed = parse_rtsp_url(url)
    if parsed is None:
        return _fail(
            "url",
            "Địa chỉ RTSP không hợp lệ.",
            "Địa chỉ phải có dạng rtsp://nguoi_dung:mat_khau@dia-chi:554/duong-dan",
            mask_url(url),
            started,
        )
    scheme, host, port = parsed
    shown = mask_url(url)

    if scheme in {"http", "https"}:
        return _fail(
            "url",
            f"Địa chỉ đang dùng giao thức {scheme}:// chứ không phải rtsp://.",
            "Camera IP thường dùng rtsp://. Hãy xem lại tài liệu camera để lấy đúng "
            "đường dẫn RTSP (ví dụ rtsp://192.168.1.50:554/stream1).",
            shown,
            started,
        )

    # 1) Phân giải tên miền
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        return _fail(
            "dns",
            f"Không phân giải được địa chỉ “{host}”.",
            "Kiểm tra lại IP/tên miền của camera. Nếu dùng tên như camera.local, hãy thử "
            "nhập thẳng địa chỉ IP (xem trong app quản lý camera hoặc trong router).",
            str(exc),
            started,
        )
    addresses = [info[4][0] for info in infos]

    # 2) Mở TCP — đây là bước phân biệt "máy tắt" với "sai mật khẩu"
    factory = socket_factory or socket.create_connection
    deadline = timeout
    try:
        conn = factory((host, port), timeout=deadline)
    except (TimeoutError, socket.timeout):
        return _fail(
            "tcp",
            f"Không tới được {host}:{port} sau {timeout:.0f} giây.",
            "Ba nguyên nhân thường gặp: (1) máy phát RTSP đang TẮT hoặc chưa bật chức năng "
            "RTSP; (2) hai máy khác mạng Wi-Fi/LAN; (3) tường lửa trên máy phát đang chặn "
            f"cổng {port}. Thử mở bằng VLC trên cùng máy này: Media → Open Network Stream → "
            f"dán {shown}.",
            f"IP phân giải được: {', '.join(addresses)}",
            started,
        )
    except ConnectionRefusedError as exc:
        return _fail(
            "tcp",
            f"Máy {host} có nhưng KHÔNG có dịch vụ RTSP ở cổng {port} (bị từ chối).",
            f"Cổng {port} đóng: có thể camera dùng cổng khác (thử 8554, 554, 10554) hoặc "
            "phải bật RTSP trong phần cài đặt của camera.",
            str(exc),
            started,
        )
    except OSError as exc:
        return _fail(
            "tcp",
            f"Không mở được kết nối TCP tới {host}:{port}.",
            "Kiểm tra dây mạng/Wi-Fi và địa chỉ IP của camera.",
            str(exc),
            started,
        )

    # 3) Hỏi RTSP xem có đúng là camera không
    try:
        with conn:
            conn.settimeout(timeout)
            request = (
                f"OPTIONS {url} RTSP/1.0\r\n"
                "CSeq: 1\r\n"
                "User-Agent: vision-automation\r\n"
                f"Transport: RTP/AVP/{'TCP' if transport.lower() == 'tcp' else 'UDP'}\r\n"
                "\r\n"
            ).encode("utf-8", "replace")
            conn.sendall(request)
            raw = b""
            while len(raw) < 2048:
                try:
                    block = conn.recv(1024)
                except (TimeoutError, socket.timeout):
                    break
                if not block:
                    break
                raw += block
                if b"\r\n\r\n" in raw:
                    break
    except OSError as exc:
        return _ok(
            "rtsp",
            f"Đã kết nối TCP tới {host}:{port} nhưng không hỏi được RTSP.",
            str(exc),
            started,
            hint=f"Thử đổi transport sang {'UDP' if transport.lower() == 'tcp' else 'TCP'}.",
        )

    status = _rtsp_status(raw)
    if status is None:
        return _fail(
            "rtsp",
            f"Cổng {port} của {host} mở nhưng KHÔNG trả lời bằng giao thức RTSP.",
            "Địa chỉ này có thể là trang web/quản trị của camera, không phải luồng RTSP. "
            "Hãy xem lại đường dẫn RTSP trong tài liệu camera (ví dụ /stream1, /live.sdp, "
            "/cam/realmonitor?channel=1&subtype=0).",
            raw[:120].decode("utf-8", "replace") or "(không có dữ liệu trả về)",
            started,
        )

    code, reason = status
    if code == 401:
        return _fail(
            "rtsp",
            "Camera trả lời 401 Unauthorized — sai tài khoản hoặc mật khẩu.",
            "Sửa user/mật khẩu trong địa chỉ RTSP (dạng rtsp://user:matkhau@ip:554/...). "
            "Nếu mật khẩu có ký tự @ : / ?, hãy mã hoá chúng (%40, %3A, %2F, %3F).",
            f"401 {reason}".strip(),
            started,
        )
    if code == 404:
        return _fail(
            "rtsp",
            "Camera trả lời 404 Not Found — đúng máy nhưng sai đường dẫn luồng.",
            "Đường dẫn sau địa chỉ IP chưa đúng. Xem lại tài liệu camera (ví dụ /stream1, "
            "/h264/ch1/main/av_stream, /live/ch0).",
            f"404 {reason}".strip(),
            started,
        )
    if 200 <= code < 300:
        return _ok(
            "ok",
            f"Kết nối RTSP thành công tới {host}:{port}.",
            f"{code} {reason}".strip() or f"{code}",
            started,
        )
    return _fail(
        "rtsp",
        f"Camera trả lời mã {code} {reason}.".strip(),
        "Xem lại user/mật khẩu và đường dẫn luồng RTSP của camera.",
        f"{code} {reason}".strip(),
        started,
    )


def check_camera_source(camera: CameraConfig, *, timeout: float = DEFAULT_TIMEOUT) -> ConnectionCheck:
    """Kiểm tra nguồn của một camera bất kỳ (rtsp / webcam / file / image)."""
    started = time.monotonic()
    if camera.source == "rtsp":
        return check_rtsp(camera.url or "", transport=camera.transport, timeout=timeout)

    if camera.source in {"file", "image"}:
        path = Path(camera.url or "")
        if not path.is_file():
            return _fail(
                "file",
                f"Không thấy tệp “{camera.url}”.",
                "Đường dẫn tính từ thư mục chứa config.yaml. Dùng đường dẫn tuyệt đối "
                "nếu không chắc, hoặc chép tệp vào thư mục dự án.",
                None,
                started,
            )
        size_mb = path.stat().st_size / (1024 * 1024)
        return _ok("ok", f"Tệp tồn tại ({size_mb:.1f} MB).", str(path), started)

    # webcam
    from . import platform_compat as compat  # import muộn: tránh vòng lặp import

    status = compat.camera_permission_status()
    if status in {"denied", "restricted"}:
        return _fail(
            "webcam",
            "Hệ điều hành đang CHẶN quyền truy cập camera của ứng dụng này.",
            compat.webcam_permission_hint(),
            f"Trạng thái quyền: {status}",
            started,
        )
    try:
        devices = compat.list_webcam_devices()
    except Exception as exc:  # pragma: no cover - phụ thuộc hệ điều hành
        return _fail("webcam", "Không liệt kê được webcam.", None, str(exc), started)
    if not devices:
        return _fail(
            "webcam",
            "Không tìm thấy webcam nào trên máy này.",
            "Cắm lại webcam/USB hoặc kiểm tra Camera trong Device Manager (Windows) / "
            "System Information (macOS).",
            None,
            started,
        )
    names = ", ".join(entry.get("name", "?") for entry in devices[:4])
    wanted = (camera.device or "auto").strip()
    if wanted and wanted.lower() != "auto":
        matched = [
            entry
            for entry in devices
            if wanted.lower() in entry.get("name", "").lower() or wanted == str(entry.get("index"))
        ]
        if not matched:
            return _fail(
                "webcam",
                f"Không thấy webcam tên “{wanted}”.",
                f"Webcam đang có: {names}. Chọn lại trong tab Cài đặt → Camera test bằng webcam.",
                None,
                started,
            )
    return _ok(
        "ok",
        f"Thấy webcam: {names}.",
        f"Trạng thái quyền: {status}",
        started,
        hint=None if status == "authorized" else compat.webcam_permission_hint(),
    )
