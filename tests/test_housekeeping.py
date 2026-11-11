"""Tests for periodic housekeeping (log retention, disk footprint, RAM readout).

Người dùng yêu cầu: "Nhật ký có limit trong khoảng 30 ngày" và "không có memory
leak gây tràn ram, ổ đĩa" — module này là phần chịu trách nhiệm cho ổ đĩa.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from app import housekeeping
from app.housekeeping import (
    dir_usage,
    housekeep,
    process_rss_bytes,
    purge_logs,
    purge_old_files,
    purge_snapshots,
    resources_report,
)

DAY = 86400.0


def _old_file(path: Path, *, days: float, content: str = "x" * 100) -> Path:
    path.write_text(content, encoding="utf-8")
    stamp = time.time() - days * DAY
    import os

    os.utime(path, (stamp, stamp))
    return path


def test_purge_removes_files_older_than_retention(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    old = _old_file(logs / "vision-automation.log.3", days=40)
    fresh = _old_file(logs / "vision-automation.log", days=2)

    result = purge_old_files(logs, ("*.log*",), retention_days=30)

    assert result.removed == 1
    assert result.kept == 1
    assert not old.exists()
    assert fresh.exists()
    assert result.freed_bytes > 0


def test_purge_keeps_files_that_the_logger_is_still_writing(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    current = _old_file(logs / "vision-automation.log", days=99)

    result = purge_logs(logs, retention_days=30, keep=[current])

    assert result.removed == 0
    assert result.kept == 1
    assert current.exists()


def test_retention_zero_means_keep_forever(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    old = _old_file(logs / "cu.log", days=999)

    result = purge_logs(logs, retention_days=0)

    assert result.checked == 0
    assert old.exists()


def test_missing_folder_is_not_an_error(tmp_path: Path) -> None:
    result = purge_logs(tmp_path / "khong-co", retention_days=30)
    assert result.removed == 0
    assert result.errors == []


def test_purge_snapshots_also_trims_per_camera(tmp_path: Path) -> None:
    folder = tmp_path / "snapshots"
    folder.mkdir()
    # 6 ảnh cũ 40 ngày của cam_a + 3 ảnh mới của cam_b
    for index in range(6):
        _old_file(folder / f"cam_a_{index}.jpg", days=40)
    for index in range(3):
        _old_file(folder / f"cam_b_{index}.jpg", days=1)

    result = purge_snapshots(folder, retention_days=30, keep_per_camera=50)

    assert result.removed == 6
    assert sorted(p.name for p in folder.glob("*.jpg")) == [
        "cam_b_0.jpg",
        "cam_b_1.jpg",
        "cam_b_2.jpg",
    ]


def test_purge_snapshots_trims_beyond_keep_per_camera(tmp_path: Path) -> None:
    folder = tmp_path / "snapshots"
    folder.mkdir()
    for index in range(10):
        path = _old_file(folder / f"cam_a_{index}.jpg", days=0.01)
        stamp = time.time() - (10 - index)  # thứ tự thời gian rõ ràng
        import os

        os.utime(path, (stamp, stamp))

    result = purge_snapshots(folder, retention_days=30, keep_per_camera=4)

    assert result.removed == 6
    kept = sorted(p.name for p in folder.glob("*.jpg"))
    assert kept == ["cam_a_6.jpg", "cam_a_7.jpg", "cam_a_8.jpg", "cam_a_9.jpg"]


def test_housekeep_reports_what_it_did(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    snaps = tmp_path / "snapshots"
    snaps.mkdir()
    _old_file(logs / "cu.log", days=45)
    _old_file(snaps / "cam_a_1.jpg", days=45)

    report = housekeep(workdir=tmp_path, log_dir=logs, snapshot_dir=snaps, retention_days=30)

    assert report["removed"] == 2
    assert report["retention_days"] == 30
    assert report["freed_bytes"] > 0
    assert report["freed_kb"] >= 0
    assert "at" in report


def test_dir_usage_and_disk_free(tmp_path: Path) -> None:
    (tmp_path / "a.log").write_text("12345", encoding="utf-8")
    (tmp_path / "b.log").write_text("12345", encoding="utf-8")

    usage = dir_usage(tmp_path)
    assert usage["files"] == 2
    assert usage["bytes"] == 10

    disk = housekeeping.disk_free(tmp_path)
    assert disk["ok"] is True
    assert disk["total_gb"] > 0
    assert 0.0 <= disk["used_percent"] <= 100.0


def test_process_rss_is_a_number() -> None:
    value = process_rss_bytes(use_cache=False)
    assert isinstance(value, int)
    assert value >= 0


def test_resources_report_shape(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "vision-automation.log").write_text("hello", encoding="utf-8")
    snaps = tmp_path / "snapshots"
    snaps.mkdir()
    (snaps / "cam_a_1.jpg").write_text("x" * 2048, encoding="utf-8")

    report = resources_report(
        workdir=tmp_path,
        log_dir=logs,
        snapshot_dir=snaps,
        retention_days=30,
        last_cleanup={"removed": 3, "at": 123.0},
    )

    assert report["retention_days"] == 30
    assert report["logs"]["files"] == 1
    assert report["snapshots"]["files"] == 1
    assert report["disk"]["ok"] is True
    assert report["last_cleanup"]["removed"] == 3
    assert report["app_disk_mb"] >= 0
    assert report["pid"] > 0
    assert report["rss_mb"] is None or report["rss_mb"] >= 0


@pytest.mark.parametrize("days", [1, 30, 365])
def test_retention_bounds_are_accepted(days: int) -> None:
    assert housekeeping.housekeep(retention_days=days)["retention_days"] == days
