"""Application entry point and CLI.

``python -m app`` starts the whole system: capture threads, inference threads,
the scenario engine, the MQTT publisher and the web UI.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
from pathlib import Path

from . import platform_compat as compat
from .config import ConfigError, ConfigManager, load_config, save_config, starter_config
from .runtime import Runtime, setup_logging

LOGGER = logging.getLogger("app.main")

DEFAULT_CONFIG = "config.yaml"


def configure_stdio() -> None:
    """Make Vietnamese console output safe on Windows (cp1258/cp437 consoles).

    Without this, ``print("Đang dừng…")`` raises ``UnicodeEncodeError`` when
    stdout is redirected to a file/service log on Windows and kills shutdown.
    """
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - redirected streams may refuse
            pass
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")


def prepare_workdir(config_path: Path) -> Path:
    """Run from the config directory so relative paths work everywhere.

    ``logs/``, ``snapshots/``, ``yolo11n.pt`` and ``.yolo`` are all relative.
    Under Windows Task Scheduler / NSSM the current directory is *not* the
    project directory (it is ``C:\\Windows\\System32``), so the app would write its
    log and model cache into system folders — chdir fixes that.
    """
    path = config_path.expanduser().resolve()
    workdir = path.parent
    workdir.mkdir(parents=True, exist_ok=True)
    if Path.cwd() != workdir:
        try:
            os.chdir(workdir)
        except OSError as exc:  # pragma: no cover - exotic permissions
            LOGGER.warning("Không chuyển được thư mục làm việc sang %s: %s", workdir, exc)
    return workdir


def install_shutdown_signals(callback) -> None:  # noqa: ANN001
    """Install graceful-stop handlers for this OS.

    POSIX: SIGINT + SIGTERM. Windows: SIGINT (Ctrl+C) + SIGBREAK (Ctrl+Break) —
    ``SIGTERM`` is never delivered by the Windows console, so ``CTRL_BREAK`` is
    what ``Dung-lai.bat`` / NSSM use.
    """
    signal_names = ["SIGINT", "SIGTERM"]
    if compat.is_windows():
        signal_names.append("SIGBREAK")
    for name in signal_names:
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, callback)
        except (ValueError, OSError, RuntimeError):  # pragma: no cover - not main thread
            LOGGER.debug("Không gắn được handler cho %s", name, exc_info=True)


def _prog_name() -> str:
    """Program name for ``--help``: ``run.py`` when started through the launcher."""
    name = Path(sys.argv[0]).name
    if not name or name in {"-c", "-m"} or name.startswith("python"):
        return "python -m app"
    return name


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=_prog_name(),
        description="Vision Automation — điều khiển thiết bị theo vùng phát hiện trong ảnh camera.",
    )
    parser.add_argument(
        "--config",
        "-c",
        default=DEFAULT_CONFIG,
        help=f"Đường dẫn config.yaml (mặc định: {DEFAULT_CONFIG})",
    )
    parser.add_argument("--port", type=int, default=None, help="Cổng web UI (mặc định 8080)")
    parser.add_argument("--host", default=None, help="Địa chỉ lắng nghe web UI (mặc định 0.0.0.0)")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Chạy thử: không gửi lệnh MQTT thật, chỉ ghi log",
    )
    parser.add_argument(
        "--with-webcam",
        dest="with_webcam",
        action="store_true",
        default=False,
        help="Tự thêm camera test bằng webcam laptop (không ảnh hưởng camera RTSP)",
    )
    parser.add_argument(
        "--no-webcam",
        dest="with_webcam",
        action="store_false",
        help="Không tự thêm camera test webcam",
    )
    parser.add_argument(
        "--no-detector",
        action="store_true",
        help="Không nạp model (chỉ kiểm tra camera + MQTT + web UI)",
    )
    parser.add_argument("--no-web", action="store_true", help="Chạy không mở web UI")
    parser.add_argument(
        "--log-level",
        default=None,
        help="Mức log: DEBUG, INFO, WARNING, ERROR (mặc định theo config)",
    )
    parser.add_argument(
        "--doctor",
        action="store_true",
        help="Kiểm tra môi trường (Python, camera, MQTT, ffmpeg) rồi thoát",
    )
    parser.add_argument("--version", action="version", version="vision-automation 1.0.0")
    return parser


def prepare_config(path: Path) -> None:
    """Create a starter config on first run instead of crashing."""
    if path.exists():
        return
    save_config(starter_config(), path)
    print(f"Đã tạo cấu hình mới: {path}")
    print("Mở web UI để cấu hình camera/kịch bản, hoặc sửa tay config.yaml.")


def build_runtime(args: argparse.Namespace) -> Runtime:
    configure_stdio()
    # TUYỆT ĐỐI phải resolve thành đường dẫn TUYỆT ĐỐI trước khi chdir: nếu
    # không, ``--config data/config.yaml`` (đường dẫn tương đối) sau khi chdir
    # vào ``data/`` sẽ trỏ tới ``data/data/config.yaml`` ⇒ app "tự tạo cấu hình
    # mới" và người dùng tưởng mất hết cài đặt.
    path = Path(args.config).expanduser().resolve()
    prepare_workdir(path)
    prepare_config(path)
    try:
        cfg = load_config(path)
    except ConfigError as exc:
        print(f"LỖI CẤU HÌNH:\n{exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    if args.log_level:
        cfg.logging.level = args.log_level.upper()
    if args.dry_run:
        cfg.mqtt.dry_run = True
    if args.port:
        cfg.web.port = int(args.port)
    if args.host:
        cfg.web.host = str(args.host)

    setup_logging(cfg)
    manager = ConfigManager(path, cfg)
    runtime = Runtime(
        manager,
        logger=logging.getLogger("app.runtime"),
        dry_run=True if args.dry_run else None,
        with_webcam=args.with_webcam,
        enable_detector=not args.no_detector,
    )
    return runtime


def run_doctor(args: argparse.Namespace) -> int:
    """``--doctor``: print a human-readable environment report (no camera opened)."""
    from . import platform_compat as compat

    print("=== KIỂM TRA MÔI TRƯỜNG VISION AUTOMATION ===")
    print(f"Hệ điều hành : {compat.os_label()} ({compat.os_key()})")
    print(f"Python       : {sys.version.split()[0]} ({sys.executable})")
    print(f"Thư mục      : {Path.cwd()}")

    problems: list[str] = []

    # -- thư viện ---------------------------------------------------------- #
    for module, label in (
        ("cv2", "OpenCV"),
        ("numpy", "numpy"),
        ("fastapi", "FastAPI"),
        ("paho.mqtt", "paho-mqtt"),
        ("yaml", "PyYAML"),
        ("pydantic", "pydantic"),
    ):
        try:
            import importlib  # noqa: PLC0415

            imported = importlib.import_module(module)
            version = getattr(imported, "__version__", "")
            if module == "paho.mqtt":
                version = getattr(imported, "__version__", "") or "2.x"
            if module == "cv2":
                version = getattr(imported, "__version__", "")
            print(f"{label:<13}: OK {version}".rstrip())
        except Exception as exc:  # noqa: BLE001
            print(f"{label:<13}: THIẾU ({exc})")
            problems.append(
                f"Thiếu thư viện {label} — chạy: pip install -r requirements.txt"
            )

    # -- AI / thiết bị ----------------------------------------------------- #
    try:
        from .detector import resolve_device  # noqa: PLC0415

        device = resolve_device("auto")
        import torch  # noqa: PLC0415

        print(f"Thiết bị AI  : {device} (torch {torch.__version__})")
    except Exception as exc:  # noqa: BLE001
        print(f"Thiết bị AI  : KHÔNG dùng được ({exc})")
        problems.append("Chưa cài torch/ultralytics — AI sẽ không chạy (camera/MQTT vẫn OK)")

    # -- webcam ------------------------------------------------------------ #
    try:
        from .capture import list_webcams  # noqa: PLC0415

        devices = list_webcams()
        print(f"Webcam       : {len(devices)} thiết bị")
        for device in devices:
            mark = " (khuyên dùng)" if device.get("recommended") else ""
            print(f"  - [{device['index']}] {device['name']}{mark}")
        print(f"Quyền camera : {compat.camera_permission_status()}")
        if not devices:
            problems.append(compat.webcam_permission_hint())
    except Exception as exc:  # noqa: BLE001
        print(f"Webcam       : lỗi ({exc})")

    # -- ffmpeg / mediamtx (loopback tuỳ chọn) ----------------------------- #
    ffmpeg = compat.resolve_executable(
        getattr(args, "ffmpeg_bin", "") or "", "ffmpeg"
    )
    mediamtx = compat.resolve_executable(
        getattr(args, "mediamtx_bin", "") or "", "mediamtx"
    )
    print(f"ffmpeg       : {ffmpeg or 'chưa có (chỉ cần cho loopback RTSP)'}")
    print(f"mediamtx     : {mediamtx or 'chưa có (chỉ cần cho loopback RTSP)'}")

    # -- cấu hình ---------------------------------------------------------- #
    config_path = Path(args.config).expanduser()
    print(f"Config       : {config_path} ({'có' if config_path.exists() else 'chưa có (sẽ tạo)'})")
    cfg = None
    try:
        cfg = load_config(config_path) if config_path.exists() else starter_config()
        cameras = ", ".join(f"{c.id}({c.source})" for c in cfg.cameras) or "chưa có"
        print(f"Camera       : {cameras}")
        print(f"Kịch bản     : {len(cfg.scenarios)}")
    except ConfigError as exc:
        print(f"LỖI cấu hình : {exc}")
        problems.append("config.yaml không hợp lệ — mở web UI (tab Cài đặt) để sửa")

    # -- MQTT -------------------------------------------------------------- #
    if cfg is not None:
        try:
            from .mqtt_client import MqttPublisher  # noqa: PLC0415

            publisher = MqttPublisher(cfg.mqtt, logger=logging.getLogger("app.doctor"))
            ok, message = publisher.test_connection(timeout=5.0)
            try:
                publisher.stop(publish_offline=False)
            except Exception:  # noqa: BLE001
                pass
            target = f"{cfg.mqtt.host}:{cfg.mqtt.port}"
            print(f"MQTT         : {'OK' if ok else 'LỖI'} — {target}")
            if not ok and message:
                print(f"               {message}")
            if not ok:
                problems.append(
                    "Không kết nối MQTT: kiểm tra broker đang chạy, đúng host/port/user-mật khẩu"
                )
        except Exception as exc:  # noqa: BLE001
            print(f"MQTT         : lỗi kiểm tra ({exc})")

    print()
    if problems:
        print("CẦN XỬ LÝ:")
        for item in problems:
            print(f"  • {item}")
        print("\nXem thêm: HUONG-DAN-WINDOWS.md / README.md (mục Xử lý sự cố)")
        return 1
    print("Mọi thứ ổn. Chạy: python run.py --config config.yaml")
    return 0


def run_headless(runtime: Runtime) -> None:
    """Blocking loop for ``--no-web`` (still publishes MQTT)."""

    def handle(signum, frame) -> None:  # noqa: ARG001
        LOGGER.info("Nhận tín hiệu %s — đang dừng an toàn", signum)
        runtime.request_shutdown("signal")

    install_shutdown_signals(handle)
    while not runtime.wait_for_shutdown(1.0):
        pass


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.doctor:
        configure_stdio()
        return run_doctor(args)
    runtime = build_runtime(args)
    runtime.start()

    exit_code = 0
    try:
        if args.no_web:
            run_headless(runtime)
        else:
            from .web.server import serve  # noqa: PLC0415 - keep headless import light

            web = runtime.config.web
            serve(runtime, host=web.host, port=web.port)
    except KeyboardInterrupt:
        pass
    except Exception as exc:  # noqa: BLE001 - report cleanly, then shut down
        LOGGER.exception("Lỗi không mong đợi")
        print(f"LỖI: {exc}", file=sys.stderr)
        exit_code = 1
    finally:
        # Graceful shutdown: publish "offline", close streams, persist config.
        try:
            runtime.stop()
        finally:
            try:
                save_config(runtime.config_manager.current, runtime.config_manager.path)
            except Exception as exc:  # noqa: BLE001
                LOGGER.error("Không lưu được cấu hình khi thoát: %s", exc)
    return exit_code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
