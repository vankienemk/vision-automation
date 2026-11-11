"""Dọn dẹp định kỳ: nhật ký cũ, ảnh chụp cũ và số liệu tài nguyên (RAM/ổ đĩa).

Mục tiêu: chạy 24/7 hàng tuần liền mà ổ đĩa không bị ăn hết.

Nguyên tắc:
- Chỉ xoá những gì CHẮC CHẮN là do ứng dụng sinh ra (``logs/*.log*``, ảnh trong
  ``snapshots/``), không bao giờ xoá file khác trong thư mục làm việc.
- Không xoá file đang được ghi (``keep`` / file của phiên hiện tại).
- Mọi hàm nhận ``now`` để test được bằng mốc thời gian giả.
- Không phụ thuộc thư viện ngoài (không cần psutil): đọc RSS theo từng hệ điều hành.
"""

from __future__ import annotations

import ctypes
import logging
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

LOGGER = logging.getLogger(__name__)

DAY = 86400.0


@dataclass
class CleanupResult:
    """Kết quả một lần dọn dẹp."""

    checked: int = 0
    removed: int = 0
    freed_bytes: int = 0
    kept: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "checked": self.checked,
            "removed": self.removed,
            "freed_bytes": self.freed_bytes,
            "freed_kb": round(self.freed_bytes / 1024, 1),
            "freed_mb": round(self.freed_bytes / (1024 * 1024), 2),
            "kept": self.kept,
            "errors": list(self.errors),
        }

    def merge(self, other: "CleanupResult") -> "CleanupResult":
        self.checked += other.checked
        self.removed += other.removed
        self.freed_bytes += other.freed_bytes
        self.kept += other.kept
        self.errors.extend(other.errors)
        return self


def _safe_mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def purge_old_files(
    folder: Path | str,
    patterns: Iterable[str],
    *,
    retention_days: int,
    keep: Iterable[Path | str] = (),
    now: float | None = None,
    recursive: bool = False,
) -> CleanupResult:
    """Xoá file khớp ``patterns`` trong ``folder`` nếu cũ hơn ``retention_days`` ngày.

    ``retention_days <= 0`` nghĩa là giữ mãi (không xoá gì).
    """
    result = CleanupResult()
    folder = Path(folder)
    if retention_days <= 0 or not folder.is_dir():
        return result
    moment = time.time() if now is None else now
    cutoff = moment - retention_days * DAY
    protected = {Path(item).resolve() for item in keep}
    seen: set[Path] = set()
    for pattern in patterns:
        iterator = folder.rglob(pattern) if recursive else folder.glob(pattern)
        for path in iterator:
            try:
                resolved = path.resolve()
            except OSError:
                continue
            if resolved in seen or not path.is_file():
                continue
            seen.add(resolved)
            result.checked += 1
            if resolved in protected:
                result.kept += 1
                continue
            mtime = _safe_mtime(path)
            if mtime is None or mtime > cutoff:
                result.kept += 1
                continue
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            try:
                path.unlink()
            except OSError as exc:  # file đang bị giữ trên Windows
                result.errors.append(f"{path.name}: {exc}")
                result.kept += 1
                continue
            result.removed += 1
            result.freed_bytes += size
    if result.removed:
        LOGGER.info(
            "Dọn nhật ký: xoá %d file cũ hơn %d ngày (giải phóng %.1f MB)",
            result.removed,
            retention_days,
            result.freed_bytes / (1024 * 1024),
        )
    return result


def purge_logs(
    folder: Path | str,
    *,
    retention_days: int,
    keep: Iterable[Path | str] = (),
    now: float | None = None,
) -> CleanupResult:
    """Dọn nhật ký: ``*.log``, ``*.log.1``, ``*.log.gz``, ``loopback-*.log``…"""
    patterns = ("*.log", "*.log.*", "*.txt.log", "*.ndjson", "*.jsonl")
    return purge_old_files(folder, patterns, retention_days=retention_days, keep=keep, now=now)


def purge_snapshots(
    folder: Path | str,
    *,
    retention_days: int,
    keep_per_camera: int = 50,
    now: float | None = None,
) -> CleanupResult:
    """Xoá ảnh chụp cũ hơn ``retention_days`` và tỉa bớt nếu một camera giữ quá nhiều."""
    result = purge_old_files(
        folder, ("*.jpg", "*.jpeg", "*.png"), retention_days=retention_days, now=now
    )
    folder = Path(folder)
    if keep_per_camera <= 0 or not folder.is_dir():
        return result
    by_camera: dict[str, list[Path]] = {}
    for path in folder.glob("*.jpg"):
        stem = path.stem  # dạng: <camera>_<timestamp>
        camera_id = stem.rsplit("_", 1)[0] if "_" in stem else stem
        by_camera.setdefault(camera_id, []).append(path)
    for camera_id, files in by_camera.items():
        if len(files) <= keep_per_camera:
            continue
        files.sort(key=lambda item: _safe_mtime(item) or 0.0)
        for stale in files[: len(files) - keep_per_camera]:
            try:
                size = stale.stat().st_size
            except OSError:
                size = 0
            try:
                stale.unlink()
            except OSError as exc:
                result.errors.append(f"{stale.name}: {exc}")
                continue
            result.removed += 1
            result.checked += 1
            result.freed_bytes += size
    return result


def dir_usage(folder: Path | str, *, limit: int = 20000) -> dict[str, Any]:
    """Số file + tổng dung lượng của một thư mục (bỏ qua lỗi quyền)."""
    folder = Path(folder)
    files = 0
    total = 0
    if folder.is_dir():
        for index, path in enumerate(folder.rglob("*")):
            if index >= limit:
                break
            try:
                if path.is_file():
                    files += 1
                    total += path.stat().st_size
            except OSError:
                continue
    return {"path": str(folder), "files": files, "bytes": total, "mb": round(total / (1024 * 1024), 2)}


def disk_free(folder: Path | str) -> dict[str, Any]:
    try:
        usage = shutil.disk_usage(str(folder))
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
    return {
        "ok": True,
        "total_gb": round(usage.total / (1024**3), 2),
        "free_gb": round(usage.free / (1024**3), 2),
        "used_percent": round(100.0 * usage.used / usage.total, 1) if usage.total else 0.0,
    }


_RSS_CACHE: dict[str, Any] = {"at": 0.0, "bytes": 0}
_RSS_TTL = 20.0


def _rss_linux() -> int:
    try:
        with open("/proc/self/statm", "r", encoding="ascii") as handle:
            pages = int(handle.read().split()[1])
        return pages * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError, AttributeError):
        return 0


def _rss_macos() -> int:
    """RSS hiện tại qua ``task_info`` của Mach (chính xác như Activity Monitor).

    ``ru_maxrss`` chỉ là ĐỈNH bộ nhớ nên không phát hiện được rò rỉ đang tăng;
    vì vậy đọc ``resident_size`` trước, ``ps``/``ru_maxrss`` chỉ là dự phòng.
    """
    try:
        class _TimeValue(ctypes.Structure):
            _fields_ = [("seconds", ctypes.c_int), ("microseconds", ctypes.c_int)]

        class _MachTaskBasicInfo(ctypes.Structure):
            _fields_ = [
                ("virtual_size", ctypes.c_uint64),
                ("resident_size", ctypes.c_uint64),
                ("resident_size_max", ctypes.c_uint64),
                ("user_time", _TimeValue),
                ("system_time", _TimeValue),
                ("policy", ctypes.c_int),
                ("suspend_count", ctypes.c_int),
            ]

        lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
        lib.mach_task_self.restype = ctypes.c_uint32
        lib.task_info.argtypes = [
            ctypes.c_uint32,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        lib.task_info.restype = ctypes.c_int
        info = _MachTaskBasicInfo()
        count = ctypes.c_uint32(ctypes.sizeof(_MachTaskBasicInfo) // ctypes.sizeof(ctypes.c_int))
        if lib.task_info(lib.mach_task_self(), 20, ctypes.byref(info), ctypes.byref(count)) == 0:
            if info.resident_size:
                return int(info.resident_size)
    except Exception:  # noqa: BLE001 - chỉ là số liệu chẩn đoán
        pass
    current = _rss_ps()
    if current:
        return current
    try:  # dự phòng cuối: đỉnh RSS (byte) — vẫn cho thấy xu hướng phình
        import resource

        return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    except Exception:  # noqa: BLE001
        return 0


def _rss_windows() -> int:
    class ProcessMemoryCounters(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_ulong),
            ("PageFaultCount", ctypes.c_ulong),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    try:
        counters = ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(ProcessMemoryCounters)
        handle = ctypes.windll.kernel32.GetCurrentProcess()  # type: ignore[attr-defined]
        if ctypes.windll.psapi.GetProcessMemoryInfo(  # type: ignore[attr-defined]
            handle, ctypes.byref(counters), counters.cb
        ):
            return int(counters.WorkingSetSize)
    except Exception:  # noqa: BLE001 - chỉ là số liệu chẩn đoán
        pass
    return 0


def _rss_ps() -> int:
    try:
        output = subprocess.run(
            ["ps", "-o", "rss=", "-p", str(os.getpid())],
            capture_output=True,
            text=True,
            timeout=3.0,
            check=False,
        )
        return int(output.stdout.strip().split()[0]) * 1024
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return 0


def process_rss_bytes(*, use_cache: bool = True) -> int:
    """RSS hiện tại của tiến trình (byte). Trả 0 nếu hệ điều hành không cho đọc."""
    moment = time.time()
    if use_cache and _RSS_CACHE["bytes"] and moment - _RSS_CACHE["at"] < _RSS_TTL:
        return int(_RSS_CACHE["bytes"])
    value = 0
    if sys.platform.startswith("win"):
        value = _rss_windows()
    elif sys.platform == "darwin":
        value = _rss_ps() or _rss_macos()
    else:
        value = _rss_linux()
    if value:
        _RSS_CACHE["bytes"] = value
        _RSS_CACHE["at"] = moment
    return value


def resources_report(
    *,
    workdir: Path | str = ".",
    log_dir: Path | str = "logs",
    snapshot_dir: Path | str | None = "snapshots",
    retention_days: int = 30,
    last_cleanup: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Số liệu tài nguyên cho bảng chẩn đoán trên Web UI."""
    workdir = Path(workdir)
    rss = process_rss_bytes()
    logs = dir_usage(log_dir)
    report: dict[str, Any] = {
        "rss_mb": round(rss / (1024 * 1024), 1) if rss else None,
        "disk": disk_free(workdir),
        "logs": logs,
        "retention_days": int(retention_days),
        "last_cleanup": last_cleanup or {},
        "pid": os.getpid(),
    }
    if snapshot_dir is not None:
        report["snapshots"] = dir_usage(snapshot_dir)
    total = logs["bytes"] + report.get("snapshots", {}).get("bytes", 0)
    report["app_disk_mb"] = round(total / (1024 * 1024), 2)
    return report


def housekeep(
    *,
    workdir: Path | str = ".",
    log_dir: Path | str = "logs",
    snapshot_dir: Path | str | None = "snapshots",
    retention_days: int = 30,
    keep: Iterable[Path | str] = (),
    keep_per_camera: int = 50,
    now: float | None = None,
) -> dict[str, Any]:
    """Dọn nhật ký + ảnh chụp cũ. Trả dict để ghi vào nhật ký/UI."""
    result = purge_logs(log_dir, retention_days=retention_days, keep=keep, now=now)
    if snapshot_dir is not None:
        result.merge(
            purge_snapshots(
                snapshot_dir,
                retention_days=retention_days,
                keep_per_camera=keep_per_camera,
                now=now,
            )
        )
    payload = result.as_dict()
    payload["at"] = time.time()
    payload["retention_days"] = int(retention_days)
    return payload


__all__ = [
    "CleanupResult",
    "dir_usage",
    "disk_free",
    "housekeep",
    "process_rss_bytes",
    "purge_logs",
    "purge_old_files",
    "purge_snapshots",
    "resources_report",
]
