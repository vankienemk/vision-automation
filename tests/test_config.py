"""Tests for config loading, validation, secrets and atomic persistence."""

from __future__ import annotations

from pathlib import Path

import pytest

from pydantic import ValidationError

from app.config import (
    MASK,
    AppConfig,
    ConfigError,
    ConfigManager,
    ScheduleConfig,
    config_to_ui_dict,
    config_to_yaml,
    default_demo_scenario,
    load_config,
    load_config_text,
    merge_masked,
    save_config,
)
from app.config import ZoneConfig


# --------------------------------------------------------------------------- #
# loading / env expansion
# --------------------------------------------------------------------------- #
def test_load_full_config(base_cfg: AppConfig) -> None:
    assert base_cfg.schema_version == 1
    assert len(base_cfg.cameras) == 1
    camera = base_cfg.cameras[0]
    assert camera.id == "cam_bep"
    # ${RTSP_PASS} must be expanded at load time
    assert camera.url == "rtsp://user:cam-pw@192.168.1.50:554/stream1"
    assert base_cfg.mqtt.password == "s3cret-mqtt"
    zone = camera.zones[0]
    assert zone.enter_confirm.frames == 3 and zone.enter_confirm.of == 5
    assert zone.polygon[0] == (0.1, 0.3)


def test_env_var_missing_raises(base_yaml: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MQTT_PASSWORD", raising=False)
    with pytest.raises(ConfigError) as excinfo:
        load_config_text(base_yaml)
    assert "MQTT_PASSWORD" in str(excinfo.value)


def test_env_var_default_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MY_UNSET_TOKEN", raising=False)
    cfg = load_config_text(
        "mqtt:\n  host: h\n  password: ${MY_UNSET_TOKEN:-fallback}\ncameras: []\nscenarios: []\n"
    )
    assert cfg.mqtt.password == "fallback"


def test_bad_yaml_reports_error() -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_config_text("mqtt: [unclosed\n")
    assert "YAML" in str(excinfo.value)


def test_unknown_key_rejected(base_yaml: str) -> None:
    broken = base_yaml.replace("  detect_fps: 4", "  detect_fps: 4\n  typo_key: 1")
    with pytest.raises(ConfigError) as excinfo:
        load_config_text(broken)
    assert "typo_key" in str(excinfo.value)


def test_missing_file_message(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_config(tmp_path / "nope.yaml")
    assert "config.example.yaml" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# validation rules
# --------------------------------------------------------------------------- #
def test_polygon_outside_unit_square(base_yaml: str) -> None:
    broken = base_yaml.replace("[0.60, 0.90]", "[1.60, 0.90]")
    with pytest.raises(ConfigError) as excinfo:
        load_config_text(broken)
    assert "0..1" in str(excinfo.value)


def test_polygon_needs_three_points(base_yaml: str) -> None:
    broken = base_yaml.replace(
        "[[0.10, 0.30], [0.60, 0.30], [0.60, 0.90], [0.10, 0.90]]", "[[0.10, 0.30], [0.60, 0.90]]"
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config_text(broken)
    assert "3 đỉnh" in str(excinfo.value)


def test_duplicate_camera_id(base_yaml: str) -> None:
    marker = "\nscenarios:"
    second = (
        "\n  - id: cam_bep\n    name: Camera trùng\n"
        "    source: rtsp\n    url: rtsp://192.168.1.51:554/s1\n"
    )
    broken = base_yaml.replace(marker, second + marker, 1)
    with pytest.raises(ConfigError) as excinfo:
        load_config_text(broken)
    assert "trùng" in str(excinfo.value)


def test_scenario_unknown_zone(base_yaml: str) -> None:
    broken = base_yaml.replace("zone: cam_bep/khu_bep", "zone: cam_bep/khong_ton_tai")
    with pytest.raises(ConfigError) as excinfo:
        load_config_text(broken)
    assert "khong_ton_tai" in str(excinfo.value)


def test_vote_frames_greater_than_of(base_yaml: str) -> None:
    broken = base_yaml.replace("{frames: 3, of: 5}", "{frames: 7, of: 5}")
    with pytest.raises(ConfigError) as excinfo:
        load_config_text(broken)
    assert "frames" in str(excinfo.value)


def test_rtsp_camera_requires_rtsp_url(base_yaml: str) -> None:
    broken = base_yaml.replace(
        "url: rtsp://user:${RTSP_PASS}@192.168.1.50:554/stream1", "url: http://192.168.1.50/stream"
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config_text(broken)
    assert "rtsp://" in str(excinfo.value)


def test_auth_enabled_without_password() -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_config_text("cameras: []\nscenarios: []\nweb:\n  auth:\n    enabled: true\n    password: null\n")
    assert "mật khẩu" in str(excinfo.value)


def test_topic_with_wildcard_rejected(base_yaml: str) -> None:
    broken = base_yaml.replace("topic: zigbee2mqtt/den_bep/set", "topic: zigbee2mqtt/+/set", 1)
    with pytest.raises(ConfigError) as excinfo:
        load_config_text(broken)
    assert "wildcard" in str(excinfo.value)


def test_scenario_without_actions_rejected(base_yaml: str) -> None:
    start = base_yaml.index("    on_enter:")
    end = base_yaml.index("    cooldown_s: 2")
    broken = base_yaml[:start] + base_yaml[end:]
    with pytest.raises(ConfigError) as excinfo:
        load_config_text(broken)
    assert "chưa có hành động" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# schedule
# --------------------------------------------------------------------------- #
def test_schedule_normal_window() -> None:
    schedule = ScheduleConfig.model_validate({"from": "08:00", "to": "17:30"})
    assert schedule.is_active(9, 0, 0)
    assert schedule.is_active(17, 30, 0)
    assert not schedule.is_active(7, 59, 0)
    assert not schedule.is_active(17, 31, 0)


def test_schedule_crosses_midnight() -> None:
    schedule = ScheduleConfig.model_validate({"from": "18:00", "to": "06:00"})
    assert schedule.is_active(23, 0, 2)
    assert schedule.is_active(2, 0, 2)
    assert not schedule.is_active(12, 0, 2)


def test_schedule_with_weekdays() -> None:
    schedule = ScheduleConfig.model_validate({"from": "00:00", "to": "23:59", "days": [0, 1, 2]})
    assert schedule.is_active(10, 0, 1)
    assert not schedule.is_active(10, 0, 5)


def test_schedule_invalid_time() -> None:
    with pytest.raises(Exception):
        ScheduleConfig.model_validate({"from": "25:00", "to": "06:00"})


# --------------------------------------------------------------------------- #
# secrets + persistence
# --------------------------------------------------------------------------- #
def test_ui_dict_masks_secrets(base_cfg: AppConfig) -> None:
    data = config_to_ui_dict(base_cfg)
    assert data["mqtt"]["password"] == MASK
    assert data["cameras"][0]["url"] == "rtsp://user:****@192.168.1.50:554/stream1"
    # non-secret fields stay readable
    assert data["cameras"][0]["id"] == "cam_bep"


def test_yaml_dump_masks_secrets(base_cfg: AppConfig) -> None:
    text = config_to_yaml(base_cfg)
    assert "s3cret-mqtt" not in text
    assert "cam-pw" not in text
    assert MASK in text


def test_merge_masked_restores_secret(base_cfg: AppConfig) -> None:
    data = config_to_ui_dict(base_cfg)
    data["detector"]["detect_fps"] = 6  # a real edit
    merged = merge_masked(data, base_cfg)
    assert merged["mqtt"]["password"] == "s3cret-mqtt"
    assert merged["cameras"][0]["url"] == "rtsp://user:cam-pw@192.168.1.50:554/stream1"
    assert merged["detector"]["detect_fps"] == 6


def test_merge_masked_accepts_new_url_with_placeholder(base_cfg: AppConfig) -> None:
    data = config_to_ui_dict(base_cfg)
    data["cameras"][0]["url"] = "rtsp://user:****@192.168.1.50:554/stream1"
    merged = merge_masked(data, base_cfg)
    assert merged["cameras"][0]["url"].endswith("cam-pw@192.168.1.50:554/stream1")


def test_merge_masked_replaces_other_host_placeholder(base_cfg: AppConfig) -> None:
    data = config_to_ui_dict(base_cfg)
    data["cameras"][0]["url"] = "rtsp://user:****@10.0.0.5:554/new"
    merged = merge_masked(data, base_cfg)
    # different host -> do not reuse the old password, drop credentials instead
    assert merged["cameras"][0]["url"] == "rtsp://10.0.0.5:554/new"


def test_atomic_save_keeps_backup(tmp_path: Path, base_cfg: AppConfig) -> None:
    path = tmp_path / "config.yaml"
    save_config(base_cfg, path)
    assert path.exists()
    first = path.read_text(encoding="utf-8")
    assert "cam-pw" in first  # real value is persisted

    base_cfg.detector.detect_fps = 8
    save_config(base_cfg, path)
    backup = path.with_suffix(".yaml.bak")
    assert backup.exists()
    assert backup.read_text(encoding="utf-8") == first
    assert (path.stat().st_mode & 0o777) == 0o600


def test_config_manager_update_and_listener(tmp_path: Path, config_path: Path, base_cfg: AppConfig) -> None:
    manager = ConfigManager(config_path, base_cfg)
    seen: list[tuple[int, int]] = []
    manager.add_listener(lambda old, new: seen.append((old.detector.imgsz, new.detector.imgsz)))

    data = config_to_ui_dict(manager.current)
    data["detector"]["imgsz"] = 320
    updated = manager.update_data(data)

    assert updated.detector.imgsz == 320
    assert seen == [(640, 320)]
    assert manager.version == 2
    reloaded = load_config(config_path)
    assert reloaded.detector.imgsz == 320
    assert reloaded.mqtt.password == "s3cret-mqtt"


def test_config_manager_update_yaml(tmp_path: Path, config_path: Path, base_cfg: AppConfig, base_yaml: str) -> None:
    manager = ConfigManager(config_path, base_cfg)
    new_yaml = base_yaml.replace("detect_fps: 4", "detect_fps: 7")
    cfg = manager.update_yaml(new_yaml)
    assert cfg.detector.detect_fps == 7


def test_config_manager_rejects_invalid_yaml(tmp_path: Path, config_path: Path, base_cfg: AppConfig, base_yaml: str) -> None:
    manager = ConfigManager(config_path, base_cfg)
    with pytest.raises(ConfigError):
        manager.update_yaml(base_yaml.replace("[0.60, 0.90]", "[9.0, 0.90]"))
    # current config must be unchanged after a rejected save
    assert manager.current.cameras[0].zones[0].polygon[2] == (0.6, 0.9)


def test_roundtrip_yaml_is_loadable(base_cfg: AppConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MQTT_PASSWORD", "x")
    text = config_to_yaml(base_cfg, mask_secrets=False)
    again = load_config_text(text)
    assert again.model_dump() == base_cfg.model_dump()


# --------------------------------------------------------------------------- #
# factories
# --------------------------------------------------------------------------- #
def test_demo_scenario_shape() -> None:
    scenario = default_demo_scenario("cam_bep/khu_bep")
    assert scenario.id == "den_bep"
    assert len(scenario.on_enter) == 1
    assert [step.after_s for step in scenario.on_vacant] == [30, 120]
    triggers = scenario.all_triggers()
    assert [t.type for t in triggers] == ["enter", "vacant"]
    assert triggers[1].steps[1].publish.payload == {"state": "OFF", "transition": 2}


# --------------------------------------------------------------------------- #
# friendly validation messages (scenario → zone)
# --------------------------------------------------------------------------- #
def test_scenario_pointing_at_missing_zone_says_how_to_fix() -> None:
    """The old message ("(root): Kịch bản tham chiếu zone … không tồn tại") told
    the user nothing.  It must now name the scenario, list the zones that exist
    and spell out the fix."""
    data = starter_data()
    data["scenarios"][0]["zone"] = "cam_bep/khuvuc_test"
    with pytest.raises(ConfigError) as excinfo:
        load_config_text(_dump_yaml(data))
    message = str(excinfo.value)
    assert "Đèn bếp" in message                      # tên kịch bản
    assert "cam_bep/khuvuc_test" in message          # vùng đang trỏ tới
    assert "khu_bep" in message                      # vùng đang có
    assert "Cách sửa" in message
    assert "(root)" not in message


def starter_data() -> dict:
    """Smallest config with one camera, one zone and one demo scenario."""
    from app.config import CameraConfig

    camera = CameraConfig.model_validate(
        {
            "id": "cam_bep",
            "name": "Camera bếp",
            "source": "rtsp",
            "url": "rtsp://127.0.0.1:554/stream1",
            "zones": [
                {
                    "id": "khu_bep",
                    "name": "Khu bếp",
                    "polygon": [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]],
                }
            ],
        }
    )
    scenario = default_demo_scenario("cam_bep/khu_bep")
    cfg = AppConfig.model_validate(
        {
            "cameras": [camera.model_dump(by_alias=True)],
            "scenarios": [scenario.model_dump(by_alias=True)],
            "setup_completed": True,
        }
    )
    return cfg.model_dump(by_alias=True)


def _dump_yaml(data: dict) -> str:
    import yaml

    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False)


# --------------------------------------------------------------------------- #
# Windows hardening
# --------------------------------------------------------------------------- #
def test_atomic_write_retries_when_target_is_locked(tmp_path):
    """Windows antivirus can hold the target for a few ms: PermissionError retry."""
    import os as os_module

    from app import config as config_module

    target = tmp_path / "config.yaml"
    target.write_text("schema_version: 1\n", encoding="utf-8")

    real_replace = os_module.replace
    calls = {"n": 0}

    def flaky_replace(src, dst, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError(13, "The process cannot access the file")
        return real_replace(src, dst, *args, **kwargs)

    patcher = pytest.MonkeyPatch()
    patcher.setattr(os_module, "replace", flaky_replace)
    try:
        config_module.atomic_write(target, "schema_version: 1\nsetup_completed: true\n")
    finally:
        patcher.undo()

    assert calls["n"] == 3
    assert "setup_completed" in target.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Kiểu vùng (shape) — do Web UI chọn, dùng để biết kéo góc/cạnh thế nào
# --------------------------------------------------------------------------- #
def test_zone_shape_defaults_to_none_for_old_configs(base_cfg: AppConfig) -> None:
    """Config cũ chưa có `shape` → None (UI tự nhận biết theo hình học)."""
    zone = base_cfg.cameras[0].zones[0]
    assert zone.shape is None


def test_zone_shape_accepts_polygon_and_rect(base_yaml: str) -> None:
    for shape in ("polygon", "rect"):
        text = base_yaml.replace(
            "        polygon: [[0.10, 0.30], [0.60, 0.30], [0.60, 0.90], [0.10, 0.90]]",
            "        shape: " + shape + "\n        polygon: [[0.10, 0.30], [0.60, 0.30], [0.60, 0.90], [0.10, 0.90]]",
        )
        cfg = load_config_text(text, "test")
        assert cfg.cameras[0].zones[0].shape == shape


def test_zone_shape_rejects_unknown_value(base_yaml: str) -> None:
    text = base_yaml.replace(
        "        polygon: [[0.10, 0.30], [0.60, 0.30], [0.60, 0.90], [0.10, 0.90]]",
        "        shape: blob\n        polygon: [[0.10, 0.30], [0.60, 0.30], [0.60, 0.90], [0.10, 0.90]]",
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config_text(text, "test")
    assert "shape" in str(excinfo.value)


def test_zone_shape_survives_yaml_and_ui_roundtrip(tmp_path: Path) -> None:
    data = starter_data()
    data["cameras"][0]["zones"][0]["shape"] = "rect"
    cfg = AppConfig.model_validate(data)
    assert cfg.cameras[0].zones[0].shape == "rect"

    path = tmp_path / "config.yaml"
    save_config(cfg, path)
    reloaded = load_config(path)
    assert reloaded.cameras[0].zones[0].shape == "rect"
    assert "shape: rect" in config_to_yaml(reloaded)
    ui = config_to_ui_dict(reloaded)
    assert ui["cameras"][0]["zones"][0]["shape"] == "rect"


# --------------------------------------------------------------------------- #
# Lịch hoạt động: khung qua đêm + danh sách thứ (lỗi thật: mất "ngày sở hữu")
# --------------------------------------------------------------------------- #
def test_cross_midnight_window_uses_the_previous_day_for_after_midnight() -> None:
    # 22:00 Thứ 2 → 06:00 Thứ 3: 01:00 Thứ 3 vẫn thuộc "đêm Thứ 2".
    schedule = ScheduleConfig.model_validate({"from": "22:00", "to": "06:00", "days": [0]})
    assert schedule.is_active(23, 0, 0)      # 23:00 Thứ 2
    assert schedule.is_active(1, 0, 1)       # 01:00 Thứ 3 (đuôi của đêm Thứ 2)
    assert not schedule.is_active(1, 0, 2)   # 01:00 Thứ 4 (đêm Thứ 3, không bật)
    assert not schedule.is_active(12, 0, 0)  # giữa trưa Thứ 2: ngoài khung


def test_schedule_rejects_equal_bounds() -> None:
    # from == to từng được hiểu ngầm là "cả ngày" — dễ gây bật đèn 24/7 ngoài ý muốn.
    with pytest.raises(Exception) as excinfo:
        ScheduleConfig.model_validate({"from": "08:00", "to": "08:00"})
    assert "08:00" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# Vùng: hình học vô nghĩa phải bị chặn ngay ở tầng cấu hình
# --------------------------------------------------------------------------- #
def test_zone_with_duplicate_consecutive_points_is_cleaned() -> None:
    starter_data = globals()['starter_data']()
    camera = starter_data["cameras"][0]
    camera["zones"][0]["polygon"] = [
        [0.10, 0.30], [0.10, 0.30], [0.60, 0.30], [0.60, 0.90], [0.10, 0.90],
    ]
    cfg = AppConfig.model_validate(starter_data)
    polygon = cfg.cameras[0].zones[0].polygon
    assert len(polygon) == 4, "điểm trùng liền kề phải được tự bỏ"


def test_zone_with_zero_area_is_rejected() -> None:
    starter_data = globals()['starter_data']()
    camera = starter_data["cameras"][0]
    camera["zones"][0]["polygon"] = [[0.10, 0.30], [0.20, 0.30], [0.30, 0.30], [0.40, 0.30]]
    with pytest.raises(Exception) as excinfo:
        AppConfig.model_validate(starter_data)
    assert "diện tích" in str(excinfo.value) or "thẳng hàng" in str(excinfo.value)


def test_zone_state_topic_colliding_with_action_topic_is_reported() -> None:
    starter_data = globals()['starter_data']()
    camera = starter_data["cameras"][0]
    action_topic = starter_data["scenarios"][0]["on_enter"][0]["publish"]["topic"]
    camera["zones"][0]["state_topic"] = action_topic
    cfg = AppConfig.model_validate(starter_data)
    warnings = cfg.topic_warnings()
    assert any(action_topic in warning for warning in warnings), warnings


# --------------------------------------------------------------------------- #
# Cách xác định "trong vùng": 5 chế độ + ngưỡng % (yêu cầu song song nhiều vùng)
# --------------------------------------------------------------------------- #
def test_anchor_accepts_new_inside_modes() -> None:
    for anchor in ("bottom_center", "center", "any_overlap", "fully_inside", "inside_ratio", "overlap_ratio"):
        zone = ZoneConfig.model_validate(
            {"id": "z", "name": "Z", "polygon": [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9]], "anchor": anchor}
        )
        assert zone.anchor == anchor


def test_anchor_rejects_unknown_mode() -> None:
    with pytest.raises(ValidationError):
        ZoneConfig.model_validate(
            {"id": "z", "name": "Z", "polygon": [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9]], "anchor": "magic"}
        )


def test_overlap_ratio_bounds_are_validated() -> None:
    for bad in (0.0, 1.5):
        with pytest.raises(ValidationError):
            ZoneConfig.model_validate(
                {
                    "id": "z",
                    "name": "Z",
                    "polygon": [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9]],
                    "anchor": "inside_ratio",
                    "overlap_ratio": bad,
                }
            )
