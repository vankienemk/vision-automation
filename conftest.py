"""Shared pytest fixtures: a realistic config used across the test-suite."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

BASE_YAML = """
schema_version: 1
mqtt:
  host: 192.168.1.10
  port: 1883
  username: user
  password: ${MQTT_PASSWORD}
  client_id: vision-automation
  availability_topic: vision/status
  dry_run: false

detector:
  model: yolo11n.pt
  device: auto
  imgsz: 640
  min_confidence: 0.45
  detect_fps: 4

cameras:
  - id: cam_bep
    name: Camera bếp
    source: rtsp
    url: rtsp://user:${RTSP_PASS}@192.168.1.50:554/stream1
    transport: tcp
    stale_after_s: 10
    zones:
      - id: khu_bep
        name: Khu bếp
        polygon: [[0.10, 0.30], [0.60, 0.30], [0.60, 0.90], [0.10, 0.90]]
        classes: [person]
        min_confidence: 0.5
        anchor: bottom_center
        enter_confirm: {frames: 3, of: 5}
        exit_grace_s: 3

scenarios:
  - id: den_bep
    name: Đèn bếp tự động
    enabled: true
    zone: cam_bep/khu_bep
    active_schedule: {from: "00:00", to: "23:59"}
    on_enter:
      - publish:
          topic: zigbee2mqtt/den_bep/set
          payload: {state: "ON", brightness: 254, transition: 1}
          qos: 1
          retain: false
    on_vacant:
      - after_s: 30
        publish:
          topic: zigbee2mqtt/den_bep/set
          payload: {state: "ON", brightness: 80, transition: 3}
          qos: 1
      - after_s: 120
        publish:
          topic: zigbee2mqtt/den_bep/set
          payload: {state: "OFF", transition: 2}
          qos: 1
    cooldown_s: 2
    manual_override:
      state_topic: zigbee2mqtt/den_bep
      pause_minutes: 10
"""


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MQTT_PASSWORD", "s3cret-mqtt")
    monkeypatch.setenv("RTSP_PASS", "cam-pw")


@pytest.fixture()
def base_yaml() -> str:
    return BASE_YAML


@pytest.fixture()
def base_cfg(base_yaml: str):
    from app.config import load_config_text

    return load_config_text(base_yaml)


@pytest.fixture()
def config_path(tmp_path: Path, base_yaml: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(base_yaml, encoding="utf-8")
    return path


def env_is_set(name: str) -> bool:
    return bool(os.environ.get(name))
