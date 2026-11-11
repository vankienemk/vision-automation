"""Tests for MQTT payload rendering, queueing and fail-safe behaviour."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from app import mqtt_client as mqtt_client_module
from app.config import MqttConfig
from app.mqtt_client import MqttPublisher, encode_payload, render_payload


class StubPublishInfo:
    rc = 0


class StubClient:
    """Minimal paho stand-in that records publishes."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, int, bool]] = []

    def publish(self, topic, payload=None, qos=0, retain=False):  # noqa: ANN001
        body = payload.decode() if isinstance(payload, bytes) else str(payload)
        self.sent.append((topic, body, int(qos), bool(retain)))
        return StubPublishInfo()


def make_publisher(**overrides) -> MqttPublisher:
    data = {
        "host": "127.0.0.1",
        "port": 1883,
        "dry_run": True,
        "availability_topic": "vision/status",
    }
    data.update(overrides)
    return MqttPublisher(MqttConfig.model_validate(data))


# --------------------------------------------------------------------------- #
# templates
# --------------------------------------------------------------------------- #
def test_render_payload_replaces_all_supported_placeholders() -> None:
    context = {
        "count": 2,
        "zone": "khu_bep",
        "camera": "cam_bep",
        "timestamp": "2024-05-15T20:00:00",
        "class_counts": {"person": 2},
    }
    payload = {
        "state": "ON",
        "persons": "{{count}}",
        "where": "{{zone}} on {{camera}}",
        "at": "{{timestamp}}",
        "counts": "{{class_counts}}",
        "nested": {"z": "{{zone}}", "list": ["{{count}}", "static"]},
    }
    rendered = render_payload(payload, context)
    assert rendered["persons"] == 2  # whole-value placeholder keeps native type
    assert rendered["where"] == "khu_bep on cam_bep"
    assert rendered["counts"] == {"person": 2}
    assert rendered["nested"] == {"z": "khu_bep", "list": [2, "static"]}


def test_render_payload_keeps_unknown_placeholder_visible() -> None:
    assert render_payload("{{nope}}", {"count": 1}) == "{{nope}}"


def test_encode_payload_formats() -> None:
    assert encode_payload({"a": 1}) == '{"a": 1}'
    assert encode_payload("plain") == "plain"
    assert encode_payload(True) == "true"
    assert encode_payload(3) == "3"
    assert encode_payload(None) == ""


# --------------------------------------------------------------------------- #
# dry-run
# --------------------------------------------------------------------------- #
def test_dry_run_records_monitor_entry_but_does_not_queue() -> None:
    publisher = make_publisher(dry_run=True)
    assert publisher.publish("test/x", {"state": "ON"}) is False
    assert publisher.pending_count() == 0
    entries = publisher.monitor(direction="tx")
    assert len(entries) == 1
    assert entries[0]["dry_run"] is True
    assert entries[0]["topic"] == "test/x"
    assert entries[0]["payload"] == '{"state": "ON"}'


def test_dry_run_zone_state_is_recorded() -> None:
    publisher = make_publisher(dry_run=True)
    publisher.publish_zone_state("vision/cam/z/state", {"occupied": True})
    entries = publisher.monitor()
    assert entries[-1]["topic"] == "vision/cam/z/state"


# --------------------------------------------------------------------------- #
# offline queue: only the LATEST state per key survives
# --------------------------------------------------------------------------- #
def test_offline_queue_keeps_only_latest_per_key() -> None:
    publisher = make_publisher(dry_run=False)
    assert publisher.publish("zigbee/den/set", {"state": "ON"}, key="scenario:den_bep") is False
    assert publisher.publish("zigbee/den/set", {"state": "OFF"}, key="scenario:den_bep") is False
    assert publisher.publish("zigbee/quat/set", {"state": "ON"}, key="scenario:quat") is False
    assert publisher.pending_count() == 2
    status = publisher.status()
    assert status["pending"] == 2

    client = StubClient()
    publisher._client = client
    publisher._connected.set()
    publisher._flush_pending()
    assert publisher.pending_count() == 0
    topics = {topic for topic, *_ in client.sent}
    assert topics == {"zigbee/den/set", "zigbee/quat/set"}
    # the latest (OFF) state wins for the light
    light_payloads = [body for topic, body, *_ in client.sent if topic == "zigbee/den/set"]
    assert light_payloads == ['{"state": "OFF"}']


def test_queue_is_bounded() -> None:
    publisher = make_publisher(dry_run=False, queue_max=3)
    for index in range(10):
        publisher.publish(f"t/{index}", {"i": index}, key=f"k{index}")
    assert publisher.pending_count() <= 3
    assert publisher.status()["dropped"] >= 1


def test_connected_publish_goes_out_with_qos_and_retain() -> None:
    publisher = make_publisher(dry_run=False)
    client = StubClient()
    publisher._client = client
    publisher._connected.set()
    assert publisher.publish("vision/a/b/state", {"occupied": True}, qos=1, retain=True) is True
    assert client.sent == [("vision/a/b/state", '{"occupied": true}', 1, True)]
    entries = publisher.monitor(direction="tx")
    assert entries[-1]["ok"] is True


def test_publish_command_renders_templates() -> None:
    from app.config import PublishAction
    from app.scenarios import CommandRequest

    publisher = make_publisher(dry_run=False)
    client = StubClient()
    publisher._client = client
    publisher._connected.set()
    command = CommandRequest(
        scenario_id="den_bep",
        scenario_name="Đèn bếp",
        zone_ref="cam_bep/khu_bep",
        camera_id="cam_bep",
        zone_id="khu_bep",
        zone_name="Khu bếp",
        trigger="enter",
        step_label="Bật đèn",
        action=PublishAction.model_validate(
            {"topic": "zigbee/den/set", "payload": {"state": "ON", "count": "{{count}}"}}
        ),
        context={"count": 2, "zone": "khu_bep"},
        created_at=0.0,
        wall_time=0.0,
    )
    assert publisher.publish_command(command) is True
    assert client.sent[-1][1] == '{"state": "ON", "count": 2}'


def test_monitor_filters_and_limits() -> None:
    publisher = make_publisher(dry_run=True)
    for index in range(5):
        publisher.publish("test/a", {"i": index})
    publisher.publish("other/b", {"i": 9})
    assert len(publisher.monitor(topic_filter="test/a")) == 5
    assert len(publisher.monitor(direction="rx")) == 0
    assert len(publisher.monitor(limit=2)) == 2


def test_status_reports_expected_fields() -> None:
    publisher = make_publisher(dry_run=True)
    status = publisher.status()
    for key in ("connected", "host", "port", "dry_run", "pending", "availability_topic"):
        assert key in status
    assert status["dry_run"] is True


# --------------------------------------------------------------------------- #
# LỖI 1 — dry-run không được rò rỉ lệnh đã xếp hàng sau khi kết nối lại
# --------------------------------------------------------------------------- #
def test_dry_run_switch_clears_queued_commands() -> None:
    """Bật dry-run phải xoá sạch hàng đợi đang chờ broker."""
    publisher = make_publisher(dry_run=False)
    assert publisher.publish("zigbee/den/set", {"state": "ON"}, key="scenario:den") is False
    assert publisher.pending_count() == 1

    publisher.dry_run = True

    assert publisher.pending_count() == 0
    assert publisher.status()["dropped"] == 1


def test_flush_pending_drops_queue_when_dry_run_is_on() -> None:
    """Kể cả khi cờ dry-run bật trực tiếp, _flush_pending không được gửi gì."""
    publisher = make_publisher(dry_run=False)
    assert publisher.publish("zigbee/den/set", {"state": "ON"}, key="scenario:den") is False
    assert publisher.publish("zigbee/quat/set", {"state": "ON"}, key="scenario:quat") is False
    assert publisher.pending_count() == 2

    # bật cờ mà không đi qua setter (mô phỏng đua luồng / hot-reload)
    publisher._dry_run = True

    client = StubClient()
    publisher._client = client
    publisher._connected.set()
    publisher._flush_pending()

    assert client.sent == [], "dry-run không được phát lệnh thật"
    assert publisher.pending_count() == 0
    dropped = [entry for entry in publisher.monitor(direction="tx") if entry["error"]]
    assert dropped, "phải ghi lại việc bỏ lệnh trong monitor"
    assert all("bỏ" in entry["error"] for entry in dropped)


def test_restart_clears_pending_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hàng đợi thuộc broker cũ — restart() phải xoá nó."""
    publisher = make_publisher(dry_run=False)
    assert publisher.publish("zigbee/den/set", {"state": "ON"}, key="scenario:den") is False
    assert publisher.pending_count() == 1

    monkeypatch.setattr(publisher, "start", lambda: None)
    publisher.restart()

    assert publisher.pending_count() == 0
    assert publisher.status()["dropped"] == 1


# --------------------------------------------------------------------------- #
# LỖI 2 — start() lỗi không được giết MQTT vĩnh viễn
# --------------------------------------------------------------------------- #
class FakePahoClient:
    """paho Client giả: ghi lại lời gọi, có thể giả lập connect_async nổ lỗi."""

    instances: list["FakePahoClient"] = []
    fail_connect: bool = False

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.args = args
        self.kwargs = kwargs
        self.connect_attempts = 0
        self.loop_started = False
        self.loop_stopped = False
        self.sent: list[tuple[str, str, int, bool]] = []
        FakePahoClient.instances.append(self)

    def username_pw_set(self, *args: Any, **kwargs: Any) -> None:
        return None

    def tls_set(self, *args: Any, **kwargs: Any) -> None:
        return None

    def tls_insecure_set(self, *args: Any, **kwargs: Any) -> None:
        return None

    def reconnect_delay_set(self, *args: Any, **kwargs: Any) -> None:
        return None

    def will_set(self, *args: Any, **kwargs: Any) -> None:
        return None

    def connect_async(self, host: str, port: int, keepalive: int = 60) -> None:
        self.connect_attempts += 1
        if FakePahoClient.fail_connect:
            raise OSError("không tới được broker")

    def loop_start(self) -> None:
        self.loop_started = True

    def loop_stop(self) -> None:
        self.loop_stopped = True

    def disconnect(self) -> None:
        return None

    def subscribe(self, *args: Any, **kwargs: Any) -> None:
        return None

    def publish(
        self, topic: str, payload: Any = None, qos: int = 0, retain: bool = False
    ) -> StubPublishInfo:
        body = payload.decode() if isinstance(payload, bytes) else str(payload)
        self.sent.append((topic, body, int(qos), bool(retain)))
        return StubPublishInfo()


def test_failed_start_reports_error_and_allows_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    """start() lỗi phải dọn client để lần start() sau còn thử lại được."""
    FakePahoClient.instances = []
    FakePahoClient.fail_connect = True
    monkeypatch.setattr(
        mqtt_client_module, "mqtt", SimpleNamespace(Client=FakePahoClient)
    )
    publisher = make_publisher(dry_run=False)

    publisher.start()

    status = publisher.status()
    assert status["connected"] is False
    assert status["error"], "phải giữ lại thông báo lỗi cho người dùng"
    assert publisher._client is None, "client lỗi phải được dọn để còn thử lại"
    assert FakePahoClient.instances[0].loop_stopped is True, "phải dừng vòng lặp đã bật"

    # broker sống lại → start() phải thực sự thử kết nối, không return sớm
    FakePahoClient.fail_connect = False
    publisher.start()

    assert len(FakePahoClient.instances) == 2, "phải tạo client mới cho lần thử lại"
    assert FakePahoClient.instances[1].connect_attempts == 1
    assert publisher._client is FakePahoClient.instances[1]


# --------------------------------------------------------------------------- #
# LỖI 3 — hàng đợi offline: không mất topic thứ hai, không phát lại lệnh quá cũ
# --------------------------------------------------------------------------- #
def test_offline_queue_keeps_two_topics_of_one_scenario() -> None:
    """Cùng key nhưng khác topic thì phải giữ cả hai lệnh."""
    publisher = make_publisher(dry_run=False)
    assert publisher.publish("zigbee/den/set", {"state": "ON"}, key="scenario:phong") is False
    assert publisher.publish("zigbee/quat/set", {"state": "ON"}, key="scenario:phong") is False
    assert publisher.pending_count() == 2

    client = StubClient()
    publisher._client = client
    publisher._connected.set()
    publisher._flush_pending()

    assert len(client.sent) == 2, "không được gộp hai topic khác nhau vào một chỗ"
    assert {topic for topic, *_ in client.sent} == {"zigbee/den/set", "zigbee/quat/set"}


def test_stale_queued_command_is_dropped() -> None:
    """Lệnh chờ quá TTL thì bỏ, kèm ghi chú tiếng Việt trong monitor."""
    publisher = make_publisher(dry_run=False)
    assert publisher.publish("zigbee/den/set", {"state": "ON"}, key="scenario:den") is False
    ttl = float(getattr(publisher.cfg, "queue_ttl_s", 900))
    for command in publisher._pending.values():
        command.created_at -= ttl + 60.0

    client = StubClient()
    publisher._client = client
    publisher._connected.set()
    publisher._flush_pending()

    assert client.sent == [], "lệnh quá cũ không được phát lại"
    assert publisher.pending_count() == 0
    assert publisher.status()["dropped"] >= 1
    dropped = [entry for entry in publisher.monitor(direction="tx") if entry["error"]]
    assert dropped, "phải ghi lại việc bỏ lệnh quá cũ"
    assert "bỏ" in dropped[-1]["error"]


def test_fresh_queued_command_is_still_sent() -> None:
    """Lệnh còn mới thì vẫn phát bình thường (TTL không chặn nhầm)."""
    publisher = make_publisher(dry_run=False)
    assert publisher.publish("zigbee/den/set", {"state": "ON"}, key="scenario:den") is False

    client = StubClient()
    publisher._client = client
    publisher._connected.set()
    publisher._flush_pending()

    assert client.sent == [("zigbee/den/set", '{"state": "ON"}', 1, False)]


# --------------------------------------------------------------------------- #
# LỖI 4 — thay thế mẫu trong payload chuỗi JSON phải được escape
# --------------------------------------------------------------------------- #
def test_render_payload_escapes_json_string_payload() -> None:
    rendered = render_payload('{"msg": "{{zone_name}}"}', {"zone_name": 'Vùng "bếp"'})
    assert isinstance(rendered, str)
    assert json.loads(rendered)["msg"] == 'Vùng "bếp"'


def test_render_payload_escapes_backslash_in_json_string_payload() -> None:
    rendered = render_payload('{"path": "{{path}}"}', {"path": r"C:\nhà bếp"})
    assert json.loads(rendered)["path"] == r"C:\nhà bếp"


def test_render_payload_keeps_plain_text_readable() -> None:
    """Payload văn bản thường vẫn phải đọc được, không nhét dấu \\ vào."""
    rendered = render_payload("Báo: {{zone_name}}", {"zone_name": 'Vùng "bếp"'})
    assert rendered == 'Báo: Vùng "bếp"'


# --------------------------------------------------------------------------- #
# Hồi quy: "tạm dừng khi người dùng chỉnh tay" từng có trong config nhưng
# KHÔNG được nối (không ai subscribe state_topic) nên không bao giờ chạy.
# --------------------------------------------------------------------------- #
def test_manual_override_topic_triggers_callback() -> None:
    seen: list[str] = []
    fake_client = FakePahoClient()
    publisher = MqttPublisher(
        MqttConfig.model_validate({"host": "127.0.0.1", "dry_run": True}),
        on_manual_override=seen.append,
    )
    publisher.set_override_topics({"nha/den/state": "cam_bep/khu_bep"})
    publisher._handle_connect(fake_client, None, {}, 0)  # noqa: SLF001
    message = type("Msg", (), {"topic": "nha/den/state", "payload": b'{"state":"OFF"}', "qos": 0, "retain": False})()
    publisher._handle_message(fake_client, None, message)  # noqa: SLF001
    assert seen == ["cam_bep/khu_bep"]


def test_manual_override_ignores_unrelated_topics() -> None:
    seen: list[str] = []
    fake_client = FakePahoClient()
    publisher = MqttPublisher(
        MqttConfig.model_validate({"host": "127.0.0.1", "dry_run": True}),
        on_manual_override=seen.append,
    )
    publisher.set_override_topics({"nha/den/state": "cam_bep/khu_bep"})
    publisher._handle_connect(fake_client, None, {}, 0)  # noqa: SLF001
    message = type("Msg", (), {"topic": "nha/quat/state", "payload": b"1", "qos": 0, "retain": False})()
    publisher._handle_message(fake_client, None, message)  # noqa: SLF001
    assert seen == []


def test_dry_run_does_not_publish_availability() -> None:
    fake_client = FakePahoClient()
    publisher = MqttPublisher(
        MqttConfig.model_validate(
            {"host": "127.0.0.1", "dry_run": True, "availability_topic": "vision/status"}
        )
    )
    publisher._handle_connect(fake_client, None, {}, 0)  # noqa: SLF001
    topics = [call[0] for call in fake_client.sent]
    assert not any("vision/status" in str(topic) for topic in topics), topics
