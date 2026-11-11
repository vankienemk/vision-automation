"""Platform compatibility layer — macOS, Windows, Linux.

Every OS-specific detail of this project funnels through this module so that the
rest of the code stays platform-neutral. The application must run **native** on
a Windows machine (production), on macOS Apple Silicon (development) and on a
Raspberry Pi / mini-PC (Linux).

Design notes
------------
* The predicates (``is_windows()`` …) read ``sys.platform`` at **call time**, so
  tests can monkeypatch ``sys.platform`` and exercise every branch on any host.
* Nothing here imports OpenCV, pyobjc or pygrabber at module import time: every
  platform library is imported lazily so that a missing optional dependency only
  degrades one feature instead of crashing the app.
* All user-facing strings are Vietnamese, matching the rest of the UI.
"""

from __future__ import annotations

import glob
import logging
import os
import shutil
import socket
import sys
import threading
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# OS predicates (call-time, monkeypatch friendly)
# --------------------------------------------------------------------------- #


def is_windows() -> bool:
    """True when running on Windows (``sys.platform`` is ``win32``)."""
    return sys.platform.startswith("win")


def is_macos() -> bool:
    """True when running on macOS (``sys.platform`` is ``darwin``)."""
    return sys.platform == "darwin"


def is_linux() -> bool:
    """True when running on Linux (including Raspberry Pi OS)."""
    return sys.platform.startswith("linux")


def os_key() -> str:
    """Short, stable OS identifier used by the web UI: windows|macos|linux|other."""
    if is_windows():
        return "windows"
    if is_macos():
        return "macos"
    if is_linux():
        return "linux"
    return "other"


def os_label() -> str:
    """Human readable OS name shown in the UI/logs."""
    if is_windows():
        release = sys.getwindowsversion() if hasattr(sys, "getwindowsversion") else None
        if release is not None:
            name = "Windows 11" if getattr(release, "build", 0) >= 22000 else "Windows 10"
            return f"{name} (build {release.build})"
        return "Windows"
    if is_macos():
        try:  # pragma: no cover - depends on the host
            import platform as _platform

            version = _platform.mac_ver()[0]
            return f"macOS {version}".strip()
        except Exception:  # noqa: BLE001
            return "macOS"
    if is_linux():
        try:  # pragma: no cover - depends on the host
            import platform as _platform

            pretty = _platform.freedesktop_os_release().get("PRETTY_NAME")
            if pretty:
                return str(pretty)
        except Exception:  # noqa: BLE001 - old Python / minimal distro
            pass
        return "Linux"
    return sys.platform


def supports_avfoundation() -> bool:
    """Only macOS uses the AVFoundation capture backend."""
    return is_macos()


# --------------------------------------------------------------------------- #
# Webcam capture backends
# --------------------------------------------------------------------------- #

# Preference order per OS. ``CAP_AVFOUNDATION`` does not exist in Windows
# OpenCV builds (AttributeError), and ``CAP_DSHOW`` does not exist on macOS, so
# every candidate is checked with getattr before use.
_BACKENDS_BY_OS: dict[str, tuple[str, ...]] = {
    "windows": ("CAP_DSHOW", "CAP_MSMF"),
    "macos": ("CAP_AVFOUNDATION",),
    "linux": ("CAP_V4L2",),
}


def webcam_backend_names() -> tuple[str, ...]:
    """OpenCV backend attribute names to try for this OS, most preferred first."""
    return _BACKENDS_BY_OS.get(os_key(), ())


def webcam_backend_ids() -> list[int]:
    """Concrete ``cv2.CAP_*`` integer ids that exist in this OpenCV build."""
    try:  # pragma: no cover - cv2 is always installed in practice
        import cv2  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return []
    ids: list[int] = []
    for name in webcam_backend_names():
        value = getattr(cv2, name, None)
        if isinstance(value, int) and value not in ids:
            ids.append(value)
    return ids


def allow_default_webcam_backend() -> bool:
    """May the capture thread fall back to the default OpenCV backend?

    On macOS a *last-resort* default open blocks for ~30 s and prints
    "camera failed to properly initialize", so AVFoundation stays the only
    option there. Windows/Linux benefit from the extra fallback.
    """
    return not is_macos()


def webcam_backend_label(backend_id: int | None) -> str:
    """Readable backend name for logs/status (e.g. ``DirectShow``)."""
    if backend_id is None:
        return "OpenCV mặc định"
    labels = {
        "CAP_DSHOW": "DirectShow",
        "CAP_MSMF": "Media Foundation",
        "CAP_AVFOUNDATION": "AVFoundation",
        "CAP_V4L2": "V4L2",
        "CAP_GSTREAMER": "GStreamer",
    }
    try:  # pragma: no cover
        import cv2  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return str(backend_id)
    for name, label in labels.items():
        if getattr(cv2, name, None) == backend_id:
            return f"{label} ({name})"
    return str(backend_id)


# --------------------------------------------------------------------------- #
# Camera permission (only macOS has an OS-level consent prompt)
# --------------------------------------------------------------------------- #

WEBCAM_PERMISSION_HINT_MACOS = (
    "macOS chưa cho phép truy cập camera. Mở System Settings → Privacy & Security "
    "→ Camera, bật cho Terminal (hoặc VS Code/iTerm) rồi khởi động lại app."
)
WEBCAM_PERMISSION_HINT_WINDOWS = (
    "Windows đang chặn quyền Camera. Mở Settings → Privacy & security → Camera, "
    "bật 'Camera access' và 'Let desktop apps access your camera'. "
    "Nếu là laptop có phím tắt camera (Fn+F8/F10) thì kiểm tra camera đang bật."
)
WEBCAM_PERMISSION_HINT_LINUX = (
    "Không mở được /dev/video*. Kiểm tra: cắm lại webcam, chạy "
    "`ls -l /dev/video*`, và thêm user vào nhóm video: "
    "`sudo usermod -aG video $USER` rồi đăng xuất/đăng nhập lại."
)


def webcam_permission_hint() -> str:
    """Vietnamese guidance for the current OS when the webcam cannot be opened."""
    if is_windows():
        return WEBCAM_PERMISSION_HINT_WINDOWS
    if is_linux():
        return WEBCAM_PERMISSION_HINT_LINUX
    return WEBCAM_PERMISSION_HINT_MACOS


# Backwards-compatible alias (macOS wording of the old module-level constant).
WEBCAM_PERMISSION_HINT = WEBCAM_PERMISSION_HINT_MACOS


def camera_permission_status() -> str:
    """OS camera permission state.

    * macOS: ``authorized`` / ``denied`` / ``restricted`` / ``not_determined``
      (``unknown`` when pyobjc is missing).
    * Windows/Linux: ``not_required`` — there is no OS-level prompt, the only
      failure modes are a privacy setting or a busy/missing device.
    """
    if not is_macos():
        return "not_required"
    try:  # pragma: no cover - macOS only
        import AVFoundation  # noqa: PLC0415
    except Exception:  # noqa: BLE001 - pyobjc is optional
        return "unknown"
    try:  # pragma: no cover
        status = AVFoundation.AVCaptureDevice.authorizationStatusForMediaType_(
            AVFoundation.AVMediaTypeVideo
        )
        return {
            0: "not_determined",
            1: "restricted",
            2: "denied",
            3: "authorized",
        }.get(int(status), "unknown")
    except Exception:  # noqa: BLE001
        return "unknown"


def request_camera_permission(timeout: float = 25.0) -> tuple[bool, str]:
    """Ensure the process is allowed to use the webcam; returns ``(granted, msg)``.

    On macOS OpenCV cannot prompt from a worker thread ("can not spin main run
    loop from other thread"), so the app asks once from the main thread and then
    sets ``OPENCV_AVFOUNDATION_SKIP_AUTH=1`` for the capture threads. On
    Windows/Linux there is nothing to ask: the privacy setting is only
    discoverable by trying to open the device.
    """
    if not is_macos():
        if is_linux() and not glob.glob("/dev/video*"):
            return True, "Không thấy /dev/video* — sẽ thử mở bằng OpenCV khi cần"
        return True, f"{os_label()}: không cần cấp quyền camera ở mức hệ điều hành"

    status = camera_permission_status()
    if status == "authorized":
        os.environ["OPENCV_AVFOUNDATION_SKIP_AUTH"] = "1"
        return True, "macOS đã cấp quyền camera"
    if status in {"denied", "restricted"}:
        os.environ["OPENCV_AVFOUNDATION_SKIP_AUTH"] = "1"
        return False, WEBCAM_PERMISSION_HINT_MACOS
    if status != "not_determined":
        return True, "Không xác định được quyền camera (bỏ qua bước hỏi quyền)"
    try:  # pragma: no cover - macOS only
        import AVFoundation  # noqa: PLC0415

        result: list[bool] = []
        done = threading.Event()

        def handler(granted) -> None:  # noqa: ANN001
            result.append(bool(granted))
            done.set()

        AVFoundation.AVCaptureDevice.requestAccessForMediaType_completionHandler_(
            AVFoundation.AVMediaTypeVideo, handler
        )
        done.wait(timeout)
        os.environ["OPENCV_AVFOUNDATION_SKIP_AUTH"] = "1"
        if result and result[0]:
            return True, "macOS đã cấp quyền camera"
        return False, WEBCAM_PERMISSION_HINT_MACOS
    except Exception as exc:  # noqa: BLE001 - never block startup
        return True, f"Không hỏi được quyền camera ({exc}) — sẽ thử mở bình thường"


# --------------------------------------------------------------------------- #
# Webcam enumeration
# --------------------------------------------------------------------------- #
def _device_entry(
    index: int,
    name: str,
    *,
    model: str = "",
    unique_id: str = "",
    builtin: bool = False,
    is_iphone: bool = False,
) -> dict[str, Any]:
    return {
        "index": index,
        "name": name,
        "model": model or name,
        "unique_id": unique_id,
        "builtin": bool(builtin),
        "is_iphone": bool(is_iphone),
        "recommended": False,
    }


def _macos_devices() -> list[dict[str, Any]]:
    """List AVFoundation capture devices (pyobjc, falling back to system_profiler)."""
    devices: list[dict[str, Any]] = []
    try:  # pragma: no cover - macOS only
        import AVFoundation  # noqa: PLC0415

        discovery = AVFoundation.AVCaptureDeviceDiscoverySession.discoverySessionWithDeviceTypes_mediaType_position_(
            [AVFoundation.AVCaptureDeviceTypeBuiltInWideAngleCamera,
             AVFoundation.AVCaptureDeviceTypeExternalUnknown],
            AVFoundation.AVMediaTypeVideo,
            AVFoundation.AVCaptureDevicePositionUnspecified,
        )
        for index, device in enumerate(discovery.devices() or []):
            name = str(device.localizedName())
            model = str(device.modelID() or name)
            unique_id = str(device.uniqueID() or "")
            lowered = name.lower()
            devices.append(
                _device_entry(
                    index,
                    name,
                    model=model,
                    unique_id=unique_id,
                    builtin=("facetime" in lowered or "built-in" in lowered),
                    is_iphone=("iphone" in lowered or "continuity" in lowered),
                )
            )
    except Exception:  # noqa: BLE001 - pyobjc missing or API changed
        devices = []
    if devices:
        return devices
    return _system_profiler_devices()


def _system_profiler_devices() -> list[dict[str, Any]]:
    """macOS fallback when pyobjc is unavailable (system_profiler, no camera open)."""
    if not shutil.which("system_profiler"):
        return []
    try:  # pragma: no cover - macOS only
        import json

        import subprocess  # noqa: PLC0415

        raw = subprocess.run(
            ["system_profiler", "SPCameraDataType", "-json"],
            capture_output=True,
            timeout=8,
            check=False,
        )
        if raw.returncode != 0:
            return []
        payload = json.loads(raw.stdout.decode("utf-8", "replace") or "{}")
        items = payload.get("SPCameraDataType") or []
        devices = []
        for index, item in enumerate(items):
            name = str(item.get("_name") or f"Camera {index}")
            lowered = name.lower()
            devices.append(
                _device_entry(
                    index,
                    name,
                    model=str(item.get("spcamera_model-id") or name),
                    unique_id=str(item.get("spcamera_unique-id") or ""),
                    builtin=("facetime" in lowered or "built-in" in lowered),
                    is_iphone=("iphone" in lowered or "continuity" in lowered),
                )
            )
        return devices
    except Exception:  # noqa: BLE001
        return []


def _windows_devices() -> list[dict[str, Any]]:
    """Enumerate DirectShow video inputs with pygrabber (no stream is opened)."""
    try:  # pragma: no cover - Windows only, optional dependency
        from pygrabber.dshow_graph import FilterGraph  # noqa: PLC0415
    except Exception:  # noqa: BLE001 - pygrabber is optional
        return []
    try:  # pragma: no cover - Windows/COM only
        names = list(FilterGraph().get_input_devices())
    except Exception:  # noqa: BLE001 - COM not initialised in this thread
        return []
    devices: list[dict[str, Any]] = []
    for index, name in enumerate(names):
        text = str(name)
        lowered = text.lower()
        devices.append(
            _device_entry(
                index,
                text,
                builtin=("integrated" in lowered or "internal" in lowered or "laptop" in lowered),
                is_iphone=("iphone" in lowered),
            )
        )
    return devices


def _linux_devices() -> list[dict[str, Any]]:
    """Enumerate ``/dev/video*`` nodes (V4L2 indices match the file numbers)."""
    devices: list[dict[str, Any]] = []
    for path in sorted(glob.glob("/dev/video*")):
        suffix = path.rsplit("video", 1)[-1]
        if not suffix.isdigit():
            continue
        index = int(suffix)
        devices.append(
            _device_entry(index, f"Webcam {path}", model=path, unique_id=path)
        )
    return devices


def list_webcam_devices() -> list[dict[str, Any]]:
    """List local webcams for the current OS.

    Never opens a stream (the camera LED stays off). Returns ``[]`` when the
    platform cannot enumerate devices without a helper library; callers then
    fall back to :func:`placeholder_devices`.
    """
    if is_macos():
        return _macos_devices()
    if is_windows():
        return _windows_devices()
    if is_linux():
        return _linux_devices()
    return []


def placeholder_devices(count: int = 3) -> list[dict[str, Any]]:
    """Generic device list used when the OS cannot enumerate webcams.

    Lets the UI offer a dropdown and ``device: auto`` still work (index 0).
    """
    return [
        _device_entry(
            index,
            f"Camera {index}" if index else "Camera mặc định (index 0)",
            # index 0 is the safe default when the OS cannot name devices
            builtin=(index == 0),
        )
        for index in range(max(1, count))
    ]


def describe_device_probe() -> str:
    """Short Vietnamese note about how webcams were detected (shown in the UI)."""
    if is_windows():
        return "Windows dùng DirectShow. Cài thêm `pygrabber` để thấy đúng tên webcam."
    if is_linux():
        return "Linux dùng V4L2 (/dev/video*). Thêm user vào nhóm `video` nếu không mở được."
    return "macOS dùng AVFoundation. Cấp quyền Camera cho Terminal/IDE nếu hình bị đen."


# --------------------------------------------------------------------------- #
# RTSP loopback (ffmpeg → MediaMTX) — ffmpeg arguments per OS
# --------------------------------------------------------------------------- #
LOOPBACK_HINT_MACOS = (
    "Chưa có ffmpeg/mediamtx. Cài bằng: brew install ffmpeg mediamtx "
    "(hoặc trỏ testing.ffmpeg_bin / testing.mediamtx_bin tới file đã tải)."
)
LOOPBACK_HINT_WINDOWS = (
    "Chưa có ffmpeg/mediamtx. Cài bằng: winget install Gyan.FFmpeg rồi tải "
    "mediamtx.exe từ github.com/bluenviron/mediamtx/releases — sau đó trỏ "
    "testing.ffmpeg_bin / testing.mediamtx_bin tới 2 file đó (hoặc thêm vào PATH)."
)
LOOPBACK_HINT_LINUX = (
    "Chưa có ffmpeg/mediamtx. Cài bằng: sudo apt install ffmpeg rồi tải "
    "mediamtx từ github.com/bluenviron/mediamtx/releases (hoặc dùng "
    "`docker run bluenviron/mediamtx`), sau đó trỏ testing.mediamtx_bin."
)


def loopback_install_hint() -> str:
    """Vietnamese install guidance for the ffmpeg/MediaMTX loopback tools."""
    if is_windows():
        return LOOPBACK_HINT_WINDOWS
    if is_linux():
        return LOOPBACK_HINT_LINUX
    return LOOPBACK_HINT_MACOS


def loopback_rate_candidates(fps: int) -> list[int]:
    """Frame rates to try when publishing the webcam as RTSP (first = preferred)."""
    wanted = int(fps or 15)
    ordered = [wanted, 30, 15, 10, 25, 20, 60, 5]
    seen: list[int] = []
    for rate in ordered:
        if rate > 0 and rate not in seen:
            seen.append(rate)
    return seen


def parse_supported_rates(log_text: str, width: int, height: int) -> list[float]:
    """Extract supported frame rates for ``WxH`` from an ffmpeg avfoundation log.

    macOS ffmpeg prints a "Supported modes:" block. Returns ``[]`` on other
    platforms / when the block is absent, so callers fall back to
    :func:`loopback_rate_candidates`.
    """
    import re

    rates: list[float] = []
    if not log_text:
        return rates
    # ffmpeg prints e.g. "  1280x720@[30.000000 15.000000]fps" — several rates
    # can share one bracket, so split the captured group on whitespace.
    pattern = re.compile(rf"{int(width)}x{int(height)}@\[([0-9.\s]+)\]")
    for match in pattern.finditer(log_text):
        for chunk in match.group(1).split():
            try:
                rate = float(chunk)
            except ValueError:
                continue
            if rate > 0 and rate not in rates:
                rates.append(rate)
    return rates


def build_loopback_ffmpeg_args(
    *,
    url: str,
    device_index: int,
    device_name: str,
    rate: int,
    width: int,
    height: int,
    fps: int,
    binary: str = "ffmpeg",
) -> list[str]:
    """ffmpeg argv that publishes the local webcam to ``url`` over RTSP/TCP.

    * macOS: AVFoundation input + ``h264_videotoolbox`` hardware encoder — the
      only combination verified on the FaceTime HD camera (``nv12`` is the sole
      pixel format ffmpeg accepts from that device).
    * Windows: DirectShow input + ``libx264`` (no hardware encoder assumption).
    * Linux: V4L2 input + ``libx264``.
    """
    args: list[str] = [
        binary,
        "-hide_banner",
        "-loglevel",
        "warning",
        "-nostdin",
    ]
    if is_macos():
        args += [
            "-f",
            "avfoundation",
            "-pixel_format",
            "nv12",
            "-framerate",
            str(int(rate)),
            "-video_size",
            f"{int(width)}x{int(height)}",
            "-i",
            f"{int(device_index)}:none",
            "-c:v",
            "h264_videotoolbox",
            "-preset",
            "realtime",
            "-tune",
            "zerolatency",
            "-pix_fmt",
            "yuv420p",
        ]
    elif is_windows():
        label = device_name or f"Camera {int(device_index)}"
        args += [
            "-f",
            "dshow",
            "-rtbufsize",
            "100M",
            "-framerate",
            str(int(rate)),
            "-video_size",
            f"{int(width)}x{int(height)}",
            "-i",
            f"video={label}",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-tune",
            "zerolatency",
            "-pix_fmt",
            "yuv420p",
        ]
    else:
        node = device_name if str(device_name).startswith("/dev/") else f"/dev/video{int(device_index)}"
        args += [
            "-f",
            "v4l2",
            "-framerate",
            str(int(rate)),
            "-video_size",
            f"{int(width)}x{int(height)}",
            "-i",
            node,
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-tune",
            "zerolatency",
            "-pix_fmt",
            "yuv420p",
        ]
    args += [
        "-g",
        str(max(1, int(fps) * 2)),
        "-r",
        str(int(fps)),
        "-f",
        "rtsp",
        "-rtsp_transport",
        "tcp",
        url,
    ]
    return args


def resolve_executable(configured: str, which_name: str) -> str | None:
    """Resolve a tool path: explicit file first, then ``PATH`` (adds ``.exe``)."""
    text = (configured or "").strip()
    if text:
        candidate = Path(os.path.expanduser(text))
        if candidate.is_file():
            return str(candidate)
        if is_windows() and not candidate.suffix:
            exe = candidate.with_suffix(".exe")
            if exe.is_file():
                return str(exe)
    found = shutil.which(which_name)
    if found is None and is_windows():
        found = shutil.which(which_name + ".exe")
    return found


# --------------------------------------------------------------------------- #
# Networking / misc helpers
# --------------------------------------------------------------------------- #
def local_ip() -> str:
    """Best-effort LAN IP so the UI can print a URL a phone can open."""
    try:  # pragma: no cover - depends on the host
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(0.3)
            sock.connect(("8.8.8.8", 80))
            return str(sock.getsockname()[0])
    except Exception:  # noqa: BLE001
        return "127.0.0.1"


def human_os_summary() -> dict[str, Any]:
    """Structured platform block embedded in ``/api/state`` for the web UI."""
    return {
        "os": os_key(),
        "label": os_label(),
        "python": sys.version.split()[0],
        "webcam_backends": list(webcam_backend_names()),
        "webcam_permission": camera_permission_status(),
        "webcam_hint": webcam_permission_hint(),
        "webcam_probe": describe_device_probe(),
        "loopback_hint": loopback_install_hint(),
        "avfoundation": supports_avfoundation(),
        "local_ip": local_ip(),
    }
