"""MQTT publishing (paho-mqtt v2) with fail-safe behaviour.

Key rules implemented here:

* **Publish only on state change.** The scenario engine already suppresses
  duplicates; this module additionally keeps one pending command *per key*
  (normally the scenario id) so a broker outage never replays a stale sequence
  of ON/OFF — on reconnect only the newest state of each scenario is sent.
* **Never lie about the past.** Commands are queued while disconnected; the
  queue is bounded (``queue_max``) and de-duplicated by key.
* **Availability.** LWT publishes ``offline`` (retained) on an unclean
  disconnect, ``online`` (retained) on connect.
* **Dry-run** keeps the connection (so the UI shows a real status and the test
  button works) but suppresses every publish.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from .config import MqttConfig

LOGGER = logging.getLogger(__name__)

try:  # pragma: no cover - paho is a hard dependency at runtime
    import paho.mqtt.client as mqtt
    from paho.mqtt.enums import CallbackAPIVersion
except Exception:  # noqa: BLE001 - allow tests to import without network deps
    mqtt = None  # type: ignore[assignment]
    CallbackAPIVersion = None  # type: ignore[assignment]


# --------------------------------------------------------------------------- #
# payload templating
# --------------------------------------------------------------------------- #
def _format_value(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, (dict, list)):
        return value
    if value is None:
        return None
    return str(value)


def render_payload(payload: Any, context: dict[str, Any]) -> Any:
    """Recursively substitute ``{{placeholders}}`` in a JSON payload.

    A string that is *exactly* one placeholder keeps the native type, so
    ``"{{class_counts}}"`` becomes a JSON object instead of a string.  Unknown
    placeholders are left untouched so mistakes are visible in the UI monitor.
    """
    if isinstance(payload, str):
        if payload.startswith("{{") and payload.endswith("}}") and payload.count("{{") == 1:
            key = payload[2:-2].strip()
            if key in context:
                return _format_value(context[key])
            return payload
        result = payload
        for key, value in context.items():
            token = "{{" + key + "}}"
            if token in result:
                if isinstance(value, (dict, list)):
                    result = result.replace(token, json.dumps(value, ensure_ascii=False))
                elif isinstance(value, bool):
                    result = result.replace(token, "true" if value else "false")
                else:
                    result = result.replace(token, str(value))
        return result
    if isinstance(payload, dict):
        return {key: render_payload(value, context) for key, value in payload.items()}
    if isinstance(payload, list):
        return [render_payload(item, context) for item in payload]
    return payload


def encode_payload(payload: Any) -> str:
    """Serialize a payload to the MQTT wire format."""
    if isinstance(payload, str):
        return payload
    if isinstance(payload, (dict, list)):
        return json.dumps(payload, ensure_ascii=False)
    if payload is None:
        return ""
    if isinstance(payload, bool):
        return "true" if payload else "false"
    return str(payload)


# --------------------------------------------------------------------------- #
# models
# --------------------------------------------------------------------------- #
@dataclass
class PendingCommand:
    """A publish waiting for a connection (one per key)."""

    key: str
    topic: str
    payload: str
    qos: int
    retain: bool
    created_at: float
    scenario_id: str | None = None
    label: str | None = None
    context: dict = field(default_factory=dict)


@dataclass
class MonitorEntry:
    """One line of the MQTT monitor."""

    ts: float
    direction: str  # "tx" | "rx"
    topic: str
    payload: str
    qos: int = 0
    retain: bool = False
    ok: bool = True
    dry_run: bool = False
    scenario_id: str | None = None
    error: str | None = None

    def as_dict(self) -> dict:
        return {
            "ts": self.ts,
            "time": datetime.fromtimestamp(self.ts).strftime("%H:%M:%S"),
            "direction": self.direction,
            "topic": self.topic,
            "payload": self.payload,
            "qos": self.qos,
            "retain": self.retain,
            "ok": self.ok,
            "dry_run": self.dry_run,
            "scenario_id": self.scenario_id,
            "error": self.error,
        }


# --------------------------------------------------------------------------- #
# publisher
# --------------------------------------------------------------------------- #
class MqttPublisher:
    """Thread-safe MQTT publisher used by the runtime and the web UI."""

    def __init__(
        self,
        cfg: MqttConfig,
        *,
        logger: logging.Logger | None = None,
        on_event: Callable[[dict], None] | None = None,
    ) -> None:
        self.cfg = cfg
        self.log = logger or LOGGER.getChild("mqtt")
        self._on_event = on_event
        self._client = None
        self._lock = threading.RLock()
        self._pending: dict[str, PendingCommand] = {}
        self._monitor: deque[MonitorEntry] = deque(maxlen=max(20, cfg.monitor_max))
        self._connected = threading.Event()
        self._stop = threading.Event()
        self._last_error: str | None = None
        self._connected_at: float | None = None
        self._sent_total = 0
        self._recv_total = 0
        self._dropped_total = 0
        self._dry_run = bool(cfg.dry_run)
        self._availability_published = False

    # -- configuration ---------------------------------------------------- #
    @property
    def dry_run(self) -> bool:
        return self._dry_run

    @dry_run.setter
    def dry_run(self, value: bool) -> None:
        self._dry_run = bool(value)
        self.log.warning("Dry-run %s", "BẬT" if self._dry_run else "TẮT")

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def update_config(self, cfg: MqttConfig) -> None:
        """Apply a hot-reloaded broker config (reconnects when it changed)."""
        old = self.cfg
        self.cfg = cfg
        self._dry_run = bool(cfg.dry_run)
        if (
            old.host != cfg.host
            or old.port != cfg.port
            or old.username != cfg.username
            or old.password != cfg.password
            or old.tls != cfg.tls
            or old.client_id != cfg.client_id
        ):
            self.log.info("Cấu hình MQTT thay đổi — kết nối lại")
            self.restart()

    # -- lifecycle -------------------------------------------------------- #
    def start(self) -> None:
        if mqtt is None:
            self._set_error("Thiếu thư viện paho-mqtt: chạy 'uv pip install paho-mqtt>=2.1'")
            return
        with self._lock:
            if self._client is not None:
                return
            client = mqtt.Client(
                callback_api_version=CallbackAPIVersion.VERSION2,
                client_id=self.cfg.client_id or "vision-automation",
                clean_session=True,
            )
            if self.cfg.username:
                client.username_pw_set(self.cfg.username, self.cfg.password or None)
            if self.cfg.tls:
                import ssl  # noqa: PLC0415

                client.tls_set(
                    ca_certs=self.cfg.ca_certs or None,
                    cert_reqs=ssl.CERT_NONE if self.cfg.tls_insecure else ssl.CERT_REQUIRED,
                )
                if self.cfg.tls_insecure:
                    client.tls_insecure_set(True)
            client.on_connect = self._handle_connect
            client.on_disconnect = self._handle_disconnect
            client.on_message = self._handle_message
            client.on_publish = self._handle_publish
            client.reconnect_delay_set(self.cfg.reconnect_min_s, self.cfg.reconnect_max_s)
            if self.cfg.availability_topic:
                client.will_set(
                    self.cfg.availability_topic,
                    payload="offline",
                    qos=1,
                    retain=True,
                )
            self._client = client

        self._stop.clear()
        try:
            self.log.info(
                "Kết nối MQTT %s:%d (client_id=%s)", self.cfg.host, self.cfg.port, self.cfg.client_id
            )
            self._client.connect_async(self.cfg.host, self.cfg.port, keepalive=self.cfg.keepalive)
            self._client.loop_start()
        except Exception as exc:  # noqa: BLE001 - broker may be down at boot
            self._set_error(f"Không kết nối được MQTT {self.cfg.host}:{self.cfg.port}: {exc}")

    def stop(self, *, publish_offline: bool = True) -> None:
        self._stop.set()
        client = self._client
        if client is None:
            return
        try:
            if publish_offline and self._connected.is_set() and self.cfg.availability_topic:
                client.publish(self.cfg.availability_topic, "offline", qos=1, retain=True)
                time.sleep(0.15)  # give the socket a moment to flush
            client.disconnect()
            client.loop_stop()
        except Exception:  # noqa: BLE001 - shutdown must never raise
            self.log.debug("Lỗi khi đóng MQTT", exc_info=True)
        finally:
            with self._lock:
                self._client = None
            self._connected.clear()

    def restart(self) -> None:
        self.stop(publish_offline=True)
        self.start()

    # -- paho callbacks --------------------------------------------------- #
    def _handle_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        if getattr(reason_code, "is_failure", False) or str(reason_code) not in {"0", "Success"}:
            self._set_error(f"Broker từ chối kết nối: {reason_code}")
            return
        self._connected.set()
        self._connected_at = time.time()
        self._last_error = None
        self.log.info("Đã kết nối MQTT %s:%d", self.cfg.host, self.cfg.port)
        if self.cfg.availability_topic:
            client.publish(self.cfg.availability_topic, "online", qos=1, retain=True)
            self._availability_published = True
        # monitor inbound traffic (our own retained states included)
        try:
            client.subscribe(f"{self.cfg.base_topic}/#", qos=0)
            client.subscribe("+/+/+/state", qos=0)
        except Exception:  # noqa: BLE001
            self.log.debug("Không subscribe được kênh monitor", exc_info=True)
        self._emit({"type": "mqtt_status", "connected": True, "error": None})
        self._flush_pending()

    def _handle_disconnect(self, client, userdata, flags, reason_code, properties=None) -> None:
        self._connected.clear()
        self._connected_at = None
        self._availability_published = False
        self.log.warning("Mất kết nối MQTT (%s) — sẽ tự kết nối lại", reason_code)
        self._emit({"type": "mqtt_status", "connected": False, "error": f"mất kết nối: {reason_code}"})

    def _handle_publish(self, client, userdata, mid, reason_code=None, properties=None) -> None:
        return None

    def _handle_message(self, client, userdata, message) -> None:
        try:
            payload = message.payload.decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            payload = repr(message.payload)
        self._recv_total += 1
        self._record(
            MonitorEntry(
                ts=time.time(),
                direction="rx",
                topic=message.topic,
                payload=payload[:4000],
                qos=int(getattr(message, "qos", 0) or 0),
                retain=bool(getattr(message, "retain", False)),
            )
        )

    # -- publishing ------------------------------------------------------- #
    def publish(
        self,
        topic: str,
        payload: Any,
        *,
        qos: int | None = None,
        retain: bool | None = None,
        key: str | None = None,
        scenario_id: str | None = None,
        context: dict | None = None,
        label: str | None = None,
    ) -> bool:
        """Publish (or queue) one message. Returns True when it hit the wire."""
        body = encode_payload(payload)
        resolved_qos = int(1 if qos is None else qos)
        resolved_retain = bool(retain) if retain is not None else False

        if self._dry_run:
            self.log.debug("[DRY-RUN] %s -> %s", topic, body[:200])
            self._record(
                MonitorEntry(
                    ts=time.time(),
                    direction="tx",
                    topic=topic,
                    payload=body,
                    qos=resolved_qos,
                    retain=resolved_retain,
                    ok=True,
                    dry_run=True,
                    scenario_id=scenario_id,
                )
            )
            return False

        if not self._connected.is_set():
            dedupe_key = key or topic
            with self._lock:
                if len(self._pending) >= max(1, self.cfg.queue_max) and dedupe_key not in self._pending:
                    # evict the oldest queued state: only the latest matters
                    oldest = min(self._pending.values(), key=lambda c: c.created_at)
                    self._pending.pop(oldest.key, None)
                    self._dropped_total += 1
                self._pending[dedupe_key] = PendingCommand(
                    key=dedupe_key,
                    topic=topic,
                    payload=body,
                    qos=resolved_qos,
                    retain=resolved_retain,
                    created_at=time.time(),
                    scenario_id=scenario_id,
                    label=label,
                    context=dict(context or {}),
                )
            self.log.info("MQTT chưa sẵn sàng — xếp hàng %s (%s)", topic, body[:120])
            return False

        return self._send(topic, body, resolved_qos, resolved_retain, scenario_id=scenario_id)

    def _send(
        self,
        topic: str,
        body: str,
        qos: int,
        retain: bool,
        *,
        scenario_id: str | None = None,
    ) -> bool:
        client = self._client
        if client is None:
            return False
        try:
            info = client.publish(topic, payload=body, qos=qos, retain=retain)
            ok = getattr(info, "rc", 0) == 0
            error = None if ok else f"rc={getattr(info, 'rc', '?')}"
        except Exception as exc:  # noqa: BLE001 - never raise into the runtime
            ok = False
            error = str(exc)
        if ok:
            self._sent_total += 1
        else:
            self._set_error(f"Gửi MQTT thất bại tới {topic}: {error}")
        self._record(
            MonitorEntry(
                ts=time.time(),
                direction="tx",
                topic=topic,
                payload=body,
                qos=qos,
                retain=retain,
                ok=ok,
                scenario_id=scenario_id,
                error=error,
            )
        )
        return ok

    def publish_dry_run(
        self,
        topic: str,
        payload: Any,
        command=None,
        *,
        qos: int = 0,
        retain: bool = False,
    ) -> bool:
        """Record a would-be publish without touching the broker."""
        body = encode_payload(payload)
        self.log.debug("[DRY-RUN] %s -> %s", topic, body[:200])
        self._record(
            MonitorEntry(
                ts=time.time(),
                direction="tx",
                topic=topic,
                payload=body,
                qos=int(qos),
                retain=bool(retain),
                ok=True,
                dry_run=True,
                scenario_id=getattr(command, "scenario_id", None),
            )
        )
        return False

    def _flush_pending(self) -> None:
        with self._lock:
            pending = list(self._pending.values())
            self._pending.clear()
        if not pending:
            return
        for command in sorted(pending, key=lambda c: c.created_at):
            age = time.time() - command.created_at
            self.log.info(
                "Gửi lại trạng thái mới nhất %s sau %.1fs mất kết nối", command.topic, age
            )
            self._send(
                command.topic,
                command.payload,
                command.qos,
                command.retain,
                scenario_id=command.scenario_id,
            )

    def publish_command(self, command) -> bool:  # CommandRequest from scenarios
        payload = render_payload(command.action.payload, command.context or {})
        return self.publish(
            command.action.topic,
            payload,
            qos=command.action.qos,
            retain=command.action.retain,
            key=f"scenario:{command.scenario_id}",
            scenario_id=command.scenario_id,
            context=command.context,
            label=command.action.label,
        )

    def publish_zone_state(self, topic: str, state: dict) -> bool:
        return self.publish(topic, state, qos=1, retain=True, key=f"state:{topic}")

    def publish_raw(self, topic: str, payload: Any, qos: int = 1, retain: bool = False) -> bool:
        """Used by the UI test button; always goes out (dry-run excepted)."""
        return self.publish(topic, payload, qos=qos, retain=retain)

    # -- introspection ---------------------------------------------------- #
    def _record(self, entry: MonitorEntry) -> None:
        with self._lock:
            self._monitor.append(entry)
        self._emit({"type": "mqtt_monitor", "entry": entry.as_dict()})

    def _set_error(self, message: str) -> None:
        self._last_error = message
        self.log.error("%s", message)
        self._emit({"type": "mqtt_status", "connected": self.connected, "error": message})

    def _emit(self, event: dict) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event(event)
        except Exception:  # noqa: BLE001
            self.log.debug("on_event lỗi", exc_info=True)

    def monitor(self, *, direction: str | None = None, topic_filter: str = "", limit: int = 100) -> list[dict]:
        with self._lock:
            entries = list(self._monitor)
        if direction in {"tx", "rx"}:
            entries = [e for e in entries if e.direction == direction]
        if topic_filter:
            entries = [e for e in entries if topic_filter in e.topic]
        return [entry.as_dict() for entry in entries][-limit:]

    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    def status(self) -> dict:
        return {
            "connected": self.connected,
            "host": self.cfg.host,
            "port": self.cfg.port,
            "client_id": self.cfg.client_id,
            "dry_run": self._dry_run,
            "error": self._last_error,
            "connected_at": self._connected_at,
            "pending": self.pending_count(),
            "sent": self._sent_total,
            "received": self._recv_total,
            "dropped": self._dropped_total,
            "availability_topic": self.cfg.availability_topic,
            "queue_max": self.cfg.queue_max,
        }

    # -- diagnostics ------------------------------------------------------ #
    def test_connection(self, timeout: float = 5.0) -> tuple[bool, str]:
        """Open a throwaway connection to prove credentials/route work."""
        if mqtt is None:
            return False, "Thiếu thư viện paho-mqtt — chạy: uv pip install 'paho-mqtt>=2.1'"
        result: dict[str, Any] = {"ok": False, "message": "hết thời gian chờ"}
        done = threading.Event()

        def worker() -> None:
            try:
                client = mqtt.Client(
                    callback_api_version=CallbackAPIVersion.VERSION2,
                    client_id=f"{(self.cfg.client_id or 'vision')}-test-{int(time.time())}",
                )
                if self.cfg.username:
                    client.username_pw_set(self.cfg.username, self.cfg.password or None)
                if self.cfg.tls:
                    import ssl  # noqa: PLC0415

                    client.tls_set(ca_certs=self.cfg.ca_certs or None)
                client.connect(self.cfg.host, self.cfg.port, keepalive=10)
                client.disconnect()
                result["ok"] = True
                result["message"] = f"Kết nối MQTT {self.cfg.host}:{self.cfg.port} thành công"
            except Exception as exc:  # noqa: BLE001
                result["ok"] = False
                result["message"] = (
                    f"Không kết nối được MQTT {self.cfg.host}:{self.cfg.port}: {exc}. "
                    "Kiểm tra broker đang chạy, đúng host/port, và user/mật khẩu."
                )
            finally:
                done.set()

        threading.Thread(target=worker, name="mqtt-test", daemon=True).start()
        done.wait(timeout)
        return bool(result["ok"]), str(result["message"])


__all__ = [
    "MonitorEntry",
    "MqttPublisher",
    "PendingCommand",
    "encode_payload",
    "render_payload",
]
