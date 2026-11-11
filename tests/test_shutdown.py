"""End-to-end: SIGTERM must shut down gracefully (publish offline, save config).

Runs the real CLI in a subprocess with an image camera (no detector, no broker),
so it is fast and never touches a real camera.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

cv2 = pytest.importorskip("cv2")
np = pytest.importorskip("numpy")
yaml = pytest.importorskip("yaml")

# SIGTERM cannot be sent to a Windows process (the Windows console has no such
# signal; ``Popen.terminate()`` there is TerminateProcess, i.e. not graceful).
# The graceful stop path on Windows is CTRL_BREAK / POST /api/system/shutdown,
# covered by tests/test_platform.py + tests/test_web.py.
pytestmark = pytest.mark.skipif(
    sys.platform.startswith("win"), reason="SIGTERM không tồn tại trên Windows"
)

ROOT = Path(__file__).resolve().parents[1]


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_http(port: int, timeout: float = 30.0) -> bool:
    import urllib.request

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state/public", timeout=1) as response:
                if response.status == 200:
                    return True
        except Exception:  # noqa: BLE001 - not up yet
            time.sleep(0.2)
    return False


def test_sigterm_shuts_down_cleanly(tmp_path):
    image = tmp_path / "scene.png"
    cv2.imwrite(str(image), np.full((180, 240, 3), 60, np.uint8))
    port = free_port()
    config_path = tmp_path / "config.yaml"
    config = {
        "schema_version": 1,
        "setup_completed": True,
        "mqtt": {
            "host": "127.0.0.1",
            "port": 1,  # nothing is listening: the app must still boot
            "dry_run": True,
        },
        "detector": {"detect_fps": 4},
        "web": {"host": "127.0.0.1", "port": port},
        "logging": {"level": "INFO", "file": str(tmp_path / "app.log")},
        "cameras": [
            {
                "id": "cam_anh",
                "name": "Ảnh",
                "source": "image",
                "url": str(image),
                "lazy_open": False,
                "fps": 5,
                "zones": [
                    {
                        "id": "z",
                        "name": "Z",
                        "polygon": [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]],
                    }
                ],
            }
        ],
        "scenarios": [],
    }
    config_path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")

    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    env["YOLO_CONFIG_DIR"] = str(tmp_path / ".yolo")
    process = subprocess.Popen(  # noqa: S603 - fixed argv
        [sys.executable, "-m", "app", "--config", str(config_path), "--no-detector"],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        assert wait_http(port), "web UI không khởi động"
        process.send_signal(signal.SIGTERM)
        stdout, _ = process.communicate(timeout=45)
    finally:
        if process.poll() is None:  # pragma: no cover - safety net
            process.kill()
            process.communicate()

    assert process.returncode == 0, f"phải thoát êm, nhận mã {process.returncode}\n{stdout[-2000:]}"
    assert "Đang dừng hệ thống" in stdout
    assert "Đã dừng" in stdout
    # config persisted (with a .bak of the previous version after the save)
    assert config_path.exists()
    saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert saved["schema_version"] == 1
