"""End-to-end MQTT test against a REAL broker (amqtt) on localhost.

Verifies the promises that matter for a 24/7 install:
* commands actually reach a broker (topic, payload, QoS, retain);
* availability is announced (retained "online") and the LWT is configured;
* an outage does not replay stale commands — only the latest state per scenario
  is sent after reconnecting;
* dry-run never publishes anything.
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time

import pytest

from app.config import MqttConfig
from app.mqtt_client import MqttPublisher

pytest.importorskip("amqtt", reason="cần amqtt để chạy test broker thật")
from amqtt.broker import Broker as AmqttBroker  # noqa: E402
paho = pytest.importorskip("paho.mqtt.client")

from paho.mqtt.enums import CallbackAPIVersion  # noqa: E402


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def start_broker(port: int):
    config = {
        "listeners": {"default": {"type": "tcp", "bind": f"127.0.0.1:{port}"}},
        "plugins": {
            "amqtt.plugins.authentication.AnonymousAuthPlugin": {"allow_anonymous": True},
            "amqtt.plugins.sys.broker.BrokerSysPlugin": {"sys_interval": 0},
        },
    }
    broker = AmqttBroker(config)
    await broker.start()
    await asyncio.sleep(0.2)
    return broker


class Collector:
    """Background paho subscriber that records every message it sees."""

    def __init__(self, port: int, topic: str = "#") -> None:
        self.messages: list[tuple[str, str, int, bool]] = []
        self._lock = threading.Lock()
        self.connected = threading.Event()
        self.client = paho.Client(callback_api_version=CallbackAPIVersion.VERSION2, client_id=f"collector-{time.time_ns()}")
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.port = port
        self.topic = topic

    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        client.subscribe(self.topic, qos=1)
        self.connected.set()

    def _on_message(self, client, userdata, message) -> None:
        with self._lock:
            self.messages.append(
                (
                    message.topic,
                    message.payload.decode("utf-8", errors="replace"),
                    int(getattr(message, "qos", 0) or 0),
                    bool(getattr(message, "retain", False)),
                )
            )

    async def start(self) -> None:
        # NOTE: every wait must be async — the broker runs in *this* event loop,
        # so blocking it would deadlock the handshake.
        self.client.connect("127.0.0.1", self.port, keepalive=10)
        self.client.loop_start()
        if not await wait_async(lambda: self.connected.is_set(), 8.0):
            raise AssertionError("collector không kết nối được broker")

    def stop(self) -> None:
        try:
            self.client.loop_stop()
            self.client.disconnect()
        except Exception:  # noqa: BLE001
            pass

    def snapshot(self) -> list[tuple[str, str, int, bool]]:
        with self._lock:
            return list(self.messages)

    def find(self, topic: str, needle: str = "") -> list[tuple[str, str, int, bool]]:
        return [m for m in self.snapshot() if m[0] == topic and (needle in m[1])]

    def wait_for(self, predicate, timeout: float = 6.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            found = [m for m in self.snapshot() if predicate(m)]
            if found:
                return found
            time.sleep(0.05)
        return []


async def grab(collector: "Collector", predicate, timeout: float = 6.0):
    """Async version of Collector.wait_for."""
    if await wait_async(lambda: bool([m for m in collector.snapshot() if predicate(m)]), timeout):
        return [m for m in collector.snapshot() if predicate(m)]
    return []


def make_publisher(port: int, **overrides) -> MqttPublisher:
    data = {
        "host": "127.0.0.1",
        "port": port,
        "client_id": "vision-test",
        "availability_topic": "vision/status",
        "dry_run": False,
        "reconnect_min_s": 0.2,
        "reconnect_max_s": 1.0,
        "queue_max": 10,
    }
    data.update(overrides)
    return MqttPublisher(MqttConfig.model_validate(data))


@pytest.mark.asyncio
async def test_publish_reaches_real_broker_with_availability():
    port = free_port()
    broker = await start_broker(port)
    collector = Collector(port)
    await collector.start()
    publisher = make_publisher(port)
    try:
        publisher.start()
        assert await wait_async(lambda: publisher.connected, 8.0), "publisher chưa kết nối"

        assert publisher.publish(
            "zigbee2mqtt/den_bep/set",
            {"state": "ON", "brightness": 254},
            qos=1,
            retain=False,
            key="scenario:den_bep",
        )

        received = await grab(collector, lambda m: m[0] == "zigbee2mqtt/den_bep/set")
        assert received, "broker không nhận được lệnh"
        topic, payload, qos, _retain = received[0]
        assert payload == '{"state": "ON", "brightness": 254}'
        assert qos == 1

        online = await grab(collector, lambda m: m[0] == "vision/status" and m[1] == "online")
        assert online, "phải công bố availability 'online' (retained)"

        # Retained zone state: an *existing* subscriber sees retain=0 by spec,
        # so verify the "retained" property with a brand new subscriber.
        publisher.publish_zone_state("vision/cam_bep/khu_bep/state", {"occupied": True, "count": 1})
        await grab(collector, lambda m: m[0] == "vision/cam_bep/khu_bep/state")
        late = Collector(port)
        await late.start()
        try:
            retained = await grab(
                late, lambda m: m[0] == "vision/cam_bep/khu_bep/state" and m[1].find('"occupied": true') >= 0
            )
            assert retained, "broker phải giữ lại trạng thái vùng (retained)"
            assert retained[0][3] is True, "subscriber mới phải nhận retain=true"
        finally:
            late.stop()
    finally:
        publisher.stop(publish_offline=False)
        collector.stop()
        await broker.shutdown()


@pytest.mark.asyncio
async def test_outage_sends_only_latest_state_after_reconnect():
    port = free_port()
    broker = await start_broker(port)
    collector = Collector(port)
    await collector.start()
    publisher = make_publisher(port)
    try:
        publisher.start()
        assert await wait_async(lambda: publisher.connected, 8.0)

        # broker goes away while the app keeps running
        await broker.shutdown()
        assert await wait_async(lambda: not publisher.connected, 10.0), "publisher phải phát hiện mất kết nối"

        assert publisher.publish("zigbee2mqtt/den_bep/set", {"state": "ON"}, qos=1, key="scenario:den_bep") is False
        assert publisher.publish("zigbee2mqtt/den_bep/set", {"state": "OFF"}, qos=1, key="scenario:den_bep") is False
        assert publisher.pending_count() == 1, "phải gộp thành 1 lệnh chờ duy nhất (không phát lại ON rồi OFF)"

        broker = await start_broker(port)
        assert await wait_async(lambda: publisher.connected, 15.0), "không kết nối lại được"
        assert await wait_async(lambda: publisher.pending_count() == 0, 10.0)

        # only the latest state was sent after the outage
        sent = [e for e in publisher.monitor(direction="tx") if e["topic"] == "zigbee2mqtt/den_bep/set"]
        assert len(sent) == 1, sent
        assert sent[0]["payload"] == '{"state": "OFF"}'

        # and the link really works again (the collector reconnects on its own)
        for _ in range(6):
            publisher.publish("zigbee2mqtt/den_bep/set", {"state": "ON", "brightness": 100}, qos=1)
            delivered = await grab(collector, lambda m: '"brightness": 100' in m[1], timeout=2.0)
            if delivered:
                break
        else:
            pytest.fail("sau khi kết nối lại, broker không nhận được lệnh mới")
    finally:
        publisher.stop(publish_offline=False)
        collector.stop()
        try:
            await broker.shutdown()
        except Exception:  # noqa: BLE001
            pass


@pytest.mark.asyncio
async def test_dry_run_publishes_nothing():
    port = free_port()
    broker = await start_broker(port)
    collector = Collector(port)
    await collector.start()
    publisher = make_publisher(port, dry_run=True)
    try:
        publisher.start()
        assert await wait_async(lambda: publisher.connected, 8.0)
        assert publisher.publish("zigbee2mqtt/den_bep/set", {"state": "ON"}, qos=1) is False
        await asyncio.sleep(0.7)
        assert not collector.find("zigbee2mqtt/den_bep/set"), "dry-run không được gửi gì"
        assert publisher.pending_count() == 0
    finally:
        publisher.stop(publish_offline=False)
        collector.stop()
        await broker.shutdown()


async def wait_async(predicate, timeout: float) -> bool:
    """Async poll — never blocks the loop that also hosts the broker."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if predicate():
                return True
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(0.05)
    return False
