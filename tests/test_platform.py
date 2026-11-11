"""Platform compatibility tests: Windows / macOS / Linux branches.

These tests never open a camera and never depend on the host OS: ``sys.platform``
is monkeypatched so every branch is exercised on any machine.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Import OpenCV-dependent modules BEFORE any test fakes ``sys.platform``: OpenCV
# resolves its own paths at import time and breaks if it is first imported while
# ``sys.platform`` claims to be another OS.
import app.capture  # noqa: F401  (import side effect is the point)
from app import platform_compat as compat
from app.capture import list_webcams, resolve_webcam_index


@pytest.fixture()
def as_windows(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sys, "platform", "win32")
    return "win32"


@pytest.fixture()
def as_linux(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sys, "platform", "linux")
    return "linux"


@pytest.fixture()
def as_macos(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    return "darwin"


# --------------------------------------------------------------------------- #
# OS predicates
# --------------------------------------------------------------------------- #
def test_os_key_per_platform(as_windows) -> None:
    assert compat.os_key() == "windows"
    assert compat.is_windows() and not compat.is_macos() and not compat.is_linux()


def test_os_key_linux_and_macos(as_linux, monkeypatch: pytest.MonkeyPatch) -> None:
    assert compat.os_key() == "linux"
    assert compat.is_linux()
    monkeypatch.setattr(sys, "platform", "darwin")
    assert compat.os_key() == "macos"
    assert compat.is_macos()
    assert compat.supports_avfoundation() is True


def test_os_label_is_never_empty(as_windows) -> None:
    label = compat.os_label()
    assert label.startswith("Windows")


# --------------------------------------------------------------------------- #
# capture backends
# --------------------------------------------------------------------------- #
def test_webcam_backend_names_per_os(as_windows, monkeypatch: pytest.MonkeyPatch) -> None:
    assert compat.webcam_backend_names() == ("CAP_DSHOW", "CAP_MSMF")
    monkeypatch.setattr(sys, "platform", "darwin")
    assert compat.webcam_backend_names() == ("CAP_AVFOUNDATION",)
    monkeypatch.setattr(sys, "platform", "linux")
    assert compat.webcam_backend_names() == ("CAP_V4L2",)


def test_webcam_backend_ids_are_ints(as_windows) -> None:
    ids = compat.webcam_backend_ids()
    assert ids, "OpenCV must expose the platform backends"
    assert all(isinstance(value, int) for value in ids)
    # Windows simulation on a macOS host: CAP_DSHOW may be missing in this build,
    # so only assert that nothing invalid leaked in.
    assert len(ids) == len(set(ids))


def test_backend_label_contains_name(as_windows) -> None:
    ids = compat.webcam_backend_ids()
    if ids:
        assert "CAP_" in compat.webcam_backend_label(ids[0])
    assert compat.webcam_backend_label(None)


def test_allow_default_backend_only_outside_macos(as_macos, monkeypatch: pytest.MonkeyPatch) -> None:
    assert compat.allow_default_webcam_backend() is False
    monkeypatch.setattr(sys, "platform", "win32")
    assert compat.allow_default_webcam_backend() is True


# --------------------------------------------------------------------------- #
# camera permission
# --------------------------------------------------------------------------- #
def test_permission_not_required_on_windows_and_linux(as_windows, monkeypatch: pytest.MonkeyPatch) -> None:
    assert compat.camera_permission_status() == "not_required"
    granted, message = compat.request_camera_permission(timeout=0.1)
    assert granted is True
    assert message
    monkeypatch.setattr(sys, "platform", "linux")
    assert compat.camera_permission_status() == "not_required"


def test_permission_hint_mentions_os_specific_settings(as_windows, monkeypatch: pytest.MonkeyPatch) -> None:
    assert "Privacy" in compat.webcam_permission_hint()
    monkeypatch.setattr(sys, "platform", "linux")
    assert "/dev/video" in compat.webcam_permission_hint()
    monkeypatch.setattr(sys, "platform", "darwin")
    assert "System Settings" in compat.webcam_permission_hint()


def test_linux_permission_request_reports_missing_video_nodes(as_linux, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(compat.glob, "glob", lambda pattern: [])
    granted, message = compat.request_camera_permission(timeout=0.1)
    assert granted is True  # never block startup
    assert "/dev/video" in message


# --------------------------------------------------------------------------- #
# device enumeration & resolution
# --------------------------------------------------------------------------- #
def test_windows_placeholder_devices_without_pygrabber(as_windows) -> None:
    # pygrabber is not installed in the test env -> [] then placeholders via capture
    devices = list_webcams()
    assert devices, "the UI must always have something to list"
    assert all({"index", "name", "recommended"} <= set(d) for d in devices)
    assert devices[0]["recommended"] is True  # index 0 is the safe default
    assert devices[0]["index"] == 0


def test_linux_placeholder_devices(as_linux, monkeypatch: pytest.MonkeyPatch) -> None:
    mono = pytest.MonkeyPatch()
    mono.setattr(compat.glob, "glob", lambda pattern: ["/dev/video0", "/dev/video2"])
    try:
        devices = compat.list_webcam_devices()
    finally:
        mono.undo()
    assert [d["index"] for d in devices] == [0, 2]
    assert all("/dev/video" in d["name"] for d in devices)


def test_resolve_device_by_name_and_index(as_windows) -> None:
    assert resolve_webcam_index(2) == 2
    assert resolve_webcam_index("1") == 1
    assert resolve_webcam_index("auto") == 0


def test_device_probe_note_is_vietnamese(as_windows, monkeypatch: pytest.MonkeyPatch) -> None:
    assert "DirectShow" in compat.describe_device_probe()
    monkeypatch.setattr(sys, "platform", "linux")
    assert "V4L2" in compat.describe_device_probe()
    monkeypatch.setattr(sys, "platform", "darwin")
    assert "AVFoundation" in compat.describe_device_probe()


# --------------------------------------------------------------------------- #
# loopback (ffmpeg) argument builder
# --------------------------------------------------------------------------- #
def test_loopback_args_macos_keep_avfoundation() -> None:
    mono = pytest.MonkeyPatch()
    mono.setattr(sys, "platform", "darwin")
    try:
        args = compat.build_loopback_ffmpeg_args(
            url="rtsp://127.0.0.1:8554/laptop",
            device_index=0,
            device_name="FaceTime HD Camera",
            rate=30,
            width=1280,
            height=720,
            fps=15,
            binary="/usr/local/bin/ffmpeg",
        )
    finally:
        mono.undo()
    joined = " ".join(args)
    assert args[0] == "/usr/local/bin/ffmpeg"
    assert "-f avfoundation" in joined
    assert "nv12" in args  # the FaceTime HD camera rejects yuv420p input
    assert "h264_videotoolbox" in args
    assert "0:none" in args
    assert "-rtsp_transport tcp" in joined
    assert args[-1] == "rtsp://127.0.0.1:8554/laptop"


def test_loopback_args_windows_use_dshow_and_libx264(as_windows) -> None:
    args = compat.build_loopback_ffmpeg_args(
        url="rtsp://127.0.0.1:8554/laptop",
        device_index=0,
        device_name="Integrated Camera",
        rate=30,
        width=1280,
        height=720,
        fps=15,
        binary=r"C:\tools\ffmpeg.exe",
    )
    joined = " ".join(args)
    assert "-f dshow" in joined
    assert "video=Integrated Camera" in args
    assert "libx264" in args
    assert "avfoundation" not in joined
    assert "nv12" not in args


def test_loopback_args_linux_use_v4l2(as_linux) -> None:
    args = compat.build_loopback_ffmpeg_args(
        url="rtsp://127.0.0.1:8554/laptop",
        device_index=1,
        device_name="",
        rate=15,
        width=640,
        height=480,
        fps=10,
    )
    joined = " ".join(args)
    assert "-f v4l2" in joined
    assert "/dev/video1" in args
    assert "libx264" in args


def test_loopback_hints_per_os(as_windows, monkeypatch: pytest.MonkeyPatch) -> None:
    assert "winget" in compat.loopback_install_hint() or "mediamtx" in compat.loopback_install_hint()
    monkeypatch.setattr(sys, "platform", "darwin")
    assert "brew install" in compat.loopback_install_hint()
    monkeypatch.setattr(sys, "platform", "linux")
    assert "apt" in compat.loopback_install_hint()


def test_loopback_rate_candidates_prefers_configured_rate() -> None:
    rates = compat.loopback_rate_candidates(15)
    assert rates[0] == 15
    assert 30 in rates
    assert len(rates) == len(set(rates))
    assert rates == compat.loopback_rate_candidates(15)


def test_parse_supported_rates_from_ffmpeg_log() -> None:
    log = (
        "[avfoundation @ 0x1] Supported modes:\n"
        "[avfoundation @ 0x1]   1280x720@[30.000000 15.000000]fps\n"
        "[avfoundation @ 0x1]   640x480@[30.000000]fps\n"
    )
    assert compat.parse_supported_rates(log, 1280, 720) == [30.0, 15.0]
    assert compat.parse_supported_rates(log, 320, 240) == []
    assert compat.parse_supported_rates("", 1280, 720) == []


# --------------------------------------------------------------------------- #
# executable resolution
# --------------------------------------------------------------------------- #
def test_resolve_executable_prefers_explicit_file(tmp_path: Path) -> None:
    binary = tmp_path / "ffmpeg"
    binary.write_bytes(b"#!/bin/sh\n")
    assert compat.resolve_executable(str(binary), "ffmpeg") == str(binary)
    assert compat.resolve_executable(str(tmp_path / "missing"), "definitely-not-a-tool") is None


def test_resolve_executable_finds_python_on_path() -> None:
    import shutil

    expected = shutil.which("python3") or shutil.which("python")
    if expected is None:  # pragma: no cover - exotic host
        pytest.skip("no python on PATH")
    resolved = compat.resolve_executable("", Path(expected).name)
    assert resolved is not None


# --------------------------------------------------------------------------- #
# summary block used by /api/state
# --------------------------------------------------------------------------- #
def test_human_os_summary_shape(as_windows) -> None:
    info = compat.human_os_summary()
    for key in (
        "os",
        "label",
        "python",
        "webcam_backends",
        "webcam_permission",
        "webcam_hint",
        "webcam_probe",
        "loopback_hint",
        "avfoundation",
        "local_ip",
    ):
        assert key in info, key
    assert info["os"] == "windows"
    assert info["avfoundation"] is False
    assert isinstance(info["local_ip"], str) and info["local_ip"]


# --------------------------------------------------------------------------- #
# launchers & working directory (Windows Task Scheduler starts elsewhere)
# --------------------------------------------------------------------------- #
def test_prepare_workdir_chdirs_to_config_folder(tmp_path) -> None:
    import os

    from app.main import prepare_workdir

    nested = tmp_path / "may-chu" / "vision"
    nested.mkdir(parents=True)
    previous = os.getcwd()
    try:
        workdir = prepare_workdir(nested / "config.yaml")
        assert workdir == nested.resolve()
        assert Path.cwd() == nested.resolve()
        assert (nested / "logs").parent.exists()
    finally:
        os.chdir(previous)


def test_run_py_works_from_any_directory(tmp_path) -> None:
    """``python -m app`` needs the project dir as CWD; ``run.py`` must not."""
    import subprocess

    run_py = Path(__file__).resolve().parents[1] / "run.py"
    result = subprocess.run(
        [sys.executable, str(run_py), "--help"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "usage" in result.stdout.lower()
    assert "--config" in result.stdout


def test_configure_stdio_is_safe() -> None:
    from app.main import configure_stdio

    configure_stdio()  # must never raise, whatever stdout is
    configure_stdio()
