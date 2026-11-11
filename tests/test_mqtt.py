"""Tests for MQTT payload rendering, queueing and fail-safe behaviour."""

from __future__ import annotations

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
