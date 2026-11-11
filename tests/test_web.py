"""Web layer tests: REST endpoints, MJPEG, auth — driven through ASGI (no socket)."""

from __future__ import annotations

import asyncio
import time

import cv2
import httpx
import numpy as np
import pytest

from app.config import AppConfig, CameraConfig, ConfigManager, DetectorConfig, MqttConfig, default_demo_scenario, save_config
from app.detection import Box, Detection
from app.runtime import Runtime
from app.web.server import COOKIE_NAME, create_app


class FakeDetector:
    name = "fake"

    def class_names(self) -> dict[int, str]:
        return {0: "person", 15: "cat"}

    def detect(self, frame, *, confidence=None):  # noqa: ANN001
        height, width = frame.shape[:2]
        return [
            Detection(
                class_id=0,
                class_name="person",
                confidence=0.88,
                box=Box(width * 0.4, height * 0.1, width * 0.6, height * 0.95),
            )
        ]

    def info(self) -> dict:
        return {"name": "fake", "available": True, "device": "cpu", "model": "fake", "stats": {}}

    def close(self) -> None:
        return None


def make_config(image_path, *, auth_enabled: bool = False) -> AppConfig:
    camera = CameraConfig.model_validate(
        {
            "id": "cam_test",
            "name": "Ảnh",
            "source": "image",
            "url": str(image_path),
            "lazy_open": False,
            "fps": 8,
            "detect_fps": 8,
            "zones": [
                {
                    "id": "khu_bep",
                    "name": "Khu bếp",
                    "polygon": [[0.05, 0.05], [0.95, 0.05], [0.95, 0.95], [0.05, 0.95]],
                    "classes": ["person"],
                    "enter_confirm": {"frames": 1, "of": 1},
                }
            ],
        }
    )
    scenario = default_demo_scenario("cam_test/khu_bep", scenario_id="den_bep")
    return AppConfig.model_validate(
        {
            "mqtt": MqttConfig.model_validate({"host": "127.0.0.1", "port": 1, "dry_run": True}).model_dump(),
            "detector": DetectorConfig(detect_fps=8).model_dump(),
            "cameras": [camera.model_dump(by_alias=True)],
            "scenarios": [scenario.model_dump(by_alias=True)],
            "web": {
                "auth": {
                    "enabled": auth_enabled,
                    "username": "admin",
                    "password": "matkhau123",
                    "token": "tok3n",
                }
            },
            "setup_completed": True,
        }
    )


@pytest.fixture()
def image_path(tmp_path):
    path = tmp_path / "scene.png"
    image = np.full((240, 320, 3), 35, dtype=np.uint8)
    cv2.rectangle(image, (120, 20), (200, 210), (15, 190, 15), -1)
    cv2.imwrite(str(path), image)
    return path


def build_runtime(tmp_path, image_path, *, auth_enabled: bool = False) -> Runtime:
    cfg = make_config(image_path, auth_enabled=auth_enabled)
    path = tmp_path / "config.yaml"
    save_config(cfg, path)
    runtime = Runtime(ConfigManager(path, cfg), enable_detector=False)
    runtime.mqtt.start = lambda: None  # type: ignore[method-assign]
    runtime.mqtt.stop = lambda publish_offline=True: None  # type: ignore[method-assign]
    return runtime


def client_for(runtime: Runtime) -> httpx.AsyncClient:
    app = create_app(runtime)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://vision.test")


def wait_for(predicate, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    return None


@pytest.mark.asyncio
async def test_index_and_state(tmp_path, image_path):
    runtime = build_runtime(tmp_path, image_path)
    runtime.start()
    try:
        async with client_for(runtime) as client:
            index = await client.get("/")
            assert index.status_code == 200
            assert "Vision Automation" in index.text

            state = await client.get("/api/state")
            assert state.status_code == 200
            payload = state.json()
            assert payload["cameras"][0]["id"] == "cam_test"
            assert payload["dry_run"] is True
            assert payload["detector"]["available"] is False  # --no-detector style runtime

            classes = await client.get("/api/detector/classes")
            assert classes.status_code == 200
    finally:
        runtime.stop()


@pytest.mark.asyncio
async def test_camera_test_connection_endpoint(tmp_path, image_path, monkeypatch):
    """Nút 🔌 Kiểm tra phải trả lời được kể cả khi mạng hỏng (không ném 500)."""
    from app.netcheck import ConnectionCheck

    runtime = build_runtime(tmp_path, image_path)
    runtime.start()
    try:
        async with client_for(runtime) as client:
            calls: list[str] = []

            def fake_check(camera_id: str) -> dict:
                calls.append(camera_id)
                if camera_id != "cam_test":
                    from app.config import ConfigError

                    raise ConfigError(f"Không có camera '{camera_id}'.")
                return ConnectionCheck(
                    ok=False,
                    stage="tcp",
                    message=f"Không tới được camera {camera_id}",
                    hint="Kiểm tra tường lửa",
                    elapsed_ms=12,
                ).as_dict() | {"camera_id": camera_id}

            monkeypatch.setattr(runtime, "check_camera", fake_check)
            resp = await client.post("/api/cameras/cam_test/test-connection")
            assert resp.status_code == 200
            body = resp.json()
            assert body["ok"] is False
            assert body["stage"] == "tcp"
            assert "tường lửa" in body["hint"]
            assert calls == ["cam_test"]

            missing = await client.post("/api/cameras/khong_co/test-connection")
            assert missing.status_code == 404
    finally:
        runtime.stop()


@pytest.mark.asyncio
async def test_index_html_carries_cache_buster_and_zone_controls(tmp_path, image_path):
    """A stale app.js in the browser cache makes fixed bugs look alive."""
    runtime = build_runtime(tmp_path, image_path)
    runtime.start()
    try:
        async with client_for(runtime) as client:
            html = (await client.get("/")).text
            assert "/static/app.js?v=" in html
            assert "/static/style.css?v=" in html
            # Bộ điều khiển mới của trình sửa vùng phải có trong HTML.
            assert 'id="zone-shape"' in html
            assert 'id="zone-id-edit"' in html
            assert 'id="zone-id"' in html and "readonly" in html
    finally:
        runtime.stop()


@pytest.mark.asyncio
async def test_state_reports_asset_version_matching_the_index(tmp_path, image_path):
    """Tab đang mở phải biết máy chủ đã có app.js mới (banner "nạp lại").

    Người dùng thật đã mất thời gian vì tab cũ giữ app.js cũ: nút mới không có
    trong DOM nên "bấm không ăn". Cache-buster chỉ tác dụng khi nạp lại trang,
    nên /api/state phải mang phiên bản asset để client tự phát hiện.
    """
    runtime = build_runtime(tmp_path, image_path)
    runtime.start()
    try:
        async with client_for(runtime) as client:
            html = (await client.get("/")).text
            stamp = html.split("/static/app.js?v=", 1)[1].split('"', 1)[0]
            state = (await client.get("/api/state")).json()
            assert state["asset_version"] == stamp
            # ws dùng cùng payload
            public = (await client.get("/api/state/public")).json()
            assert "asset_version" not in public  # login page không cần
    finally:
        runtime.stop()


@pytest.mark.asyncio
async def test_config_roundtrip_and_validation(tmp_path, image_path):
    runtime = build_runtime(tmp_path, image_path)
    runtime.start()
    try:
        async with client_for(runtime) as client:
            config = (await client.get("/api/config")).json()["config"]
            assert config["cameras"][0]["id"] == "cam_test"

            # masked secrets never leak
            assert config["mqtt"]["password"] in {"", "****", None}

            config["detector"]["detect_fps"] = 3
            result = await client.put("/api/config", json={"config": config})
            assert result.status_code == 200
            assert result.json()["ok"] is True
            assert runtime.config.detector.detect_fps == 3

            yaml_text = (await client.get("/api/config/yaml")).text
            assert "schema_version" in yaml_text

            ok = await client.post("/api/config/validate", content=yaml_text)
            assert ok.json()["ok"] is True

            bad = await client.post("/api/config/validate", content="cameras: [khong_hop_le")
            assert bad.json()["ok"] is False
    finally:
        runtime.stop()


@pytest.mark.asyncio
async def test_zone_save_endpoint(tmp_path, image_path):
    runtime = build_runtime(tmp_path, image_path)
    runtime.start()
    try:
        async with client_for(runtime) as client:
            zones = [
                {
                    "id": "khu_bep",
                    "name": "Khu bếp mới",
                    "polygon": [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]],
                    "classes": ["person"],
                    "color": "#38bdf8",
                    "anchor": "bottom_center",
                    "enter_confirm": {"frames": 3, "of": 5},
                    "exit_grace_s": 3,
                },
                {
                    "id": "ban_an",
                    "name": "Bàn ăn",
                    "polygon": [[0.2, 0.2], [0.5, 0.2], [0.5, 0.5], [0.2, 0.5]],
                    "classes": ["person", "cat"],
                },
            ]
            result = await client.post("/api/cameras/cam_test/zones", json={"zones": zones})
            assert result.status_code == 200
            saved = runtime.config.camera("cam_test")
            assert saved is not None
            assert [zone.id for zone in saved.zones] == ["khu_bep", "ban_an"]
            assert runtime.zone_engine("cam_test").zone_ids() == ["khu_bep", "ban_an"]

            missing = await client.post("/api/cameras/khong_co/zones", json={"zones": []})
            assert missing.status_code == 404
    finally:
        runtime.stop()


@pytest.mark.asyncio
async def test_zone_save_blocks_on_scenario_orphan_with_clear_error(tmp_path, image_path):
    """Deleting a zone a scenario still uses must not produce a cryptic error."""
    runtime = build_runtime(tmp_path, image_path)
    runtime.start()
    try:
        async with client_for(runtime) as client:
            zones = [
                {
                    "id": "vung_khac",
                    "name": "Vùng khác",
                    "polygon": [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]],
                    "classes": ["person"],
                }
            ]
            blocked = await client.post("/api/cameras/cam_test/zones", json={"zones": zones})
            assert blocked.status_code == 409
            body = blocked.json()
            assert body["ok"] is False
            assert [item["scenario_id"] for item in body["orphans"]] == ["den_bep"]
            assert "den_bep" not in body["error"]        # thông điệp dùng tên, không dùng id
            assert "khu_bep" in body["error"]
            assert "hint" in body
            # Cấu hình không bị thay đổi khi lưu bị chặn
            assert [zone.id for zone in runtime.config.camera("cam_test").zones] == ["khu_bep"]
            assert runtime.config.scenario("den_bep") is not None

            forced = await client.post(
                "/api/cameras/cam_test/zones",
                json={"zones": zones, "drop_orphan_scenarios": True},
            )
            assert forced.status_code == 200
            assert forced.json()["dropped_scenarios"] == ["den_bep"]
            assert runtime.config.scenario("den_bep") is None
            assert [zone.id for zone in runtime.config.camera("cam_test").zones] == ["vung_khac"]
    finally:
        runtime.stop()


@pytest.mark.asyncio
async def test_zone_save_rename_rewrites_scenario_reference(tmp_path, image_path):
    """Renaming a zone id must migrate the scenarios instead of breaking them."""
    runtime = build_runtime(tmp_path, image_path)
    runtime.start()
    try:
        async with client_for(runtime) as client:
            zones = [
                {
                    "id": "khu_bep_moi",
                    "name": "Khu bếp",
                    "polygon": [[0.05, 0.05], [0.95, 0.05], [0.95, 0.95], [0.05, 0.95]],
                    "classes": ["person"],
                }
            ]
            result = await client.post(
                "/api/cameras/cam_test/zones",
                json={"zones": zones, "renames": {"khu_bep": "khu_bep_moi"}},
            )
            assert result.status_code == 200
            assert result.json()["renamed"] == {"khu_bep": "khu_bep_moi"}
            scenario = runtime.config.scenario("den_bep")
            assert scenario is not None
            assert scenario.zone == "cam_test/khu_bep_moi"
            assert runtime.zone_engine("cam_test").zone_ids() == ["khu_bep_moi"]
    finally:
        runtime.stop()


@pytest.mark.asyncio
async def test_simulation_and_test_publish(tmp_path, image_path):
    runtime = build_runtime(tmp_path, image_path)
    runtime.start()
    try:
        async with client_for(runtime) as client:
            result = await client.post(
                "/api/simulate", json={"zone": "cam_test/khu_bep", "occupied": True, "count": 2}
            )
            assert result.json()["ok"] is True
            assert runtime.simulator.state()["zones"]["cam_test/khu_bep"]["count"] == 2

            cleared = await client.post("/api/simulate/clear", json={"zone": "cam_test/khu_bep"})
            assert cleared.json()["ok"] is True

            scale = await client.post("/api/simulate/scale", json={"scale": 10})
            assert scale.json()["scale"] == 10.0
            await client.post("/api/simulate/scale", json={"scale": 1})

            sent = await client.post("/api/scenarios/den_bep/test", json={"step": 0})
            body = sent.json()
            assert body["ok"] is True
            assert body["topic"].startswith("zigbee2mqtt")
            assert body["dry_run"] is True

            # second trigger = "vacant" (dim step at +30s)
            dim = (await client.post("/api/scenarios/den_bep/test", json={"trigger": 1, "step": 0})).json()
            assert dim["ok"] is True
            assert dim["payload"]["brightness"] == 80

            # out-of-range trigger index is reported, never crashes
            bad = (await client.post("/api/scenarios/den_bep/test", json={"trigger": 9, "step": 0})).json()
            assert bad["ok"] is False

            missing = await client.post("/api/scenarios/khong_co/test", json={})
            assert missing.json()["ok"] is False
    finally:
        runtime.stop()


@pytest.mark.asyncio
async def test_snapshot_endpoint(tmp_path, image_path):
    runtime = build_runtime(tmp_path, image_path)
    runtime.start()
    try:
        assert wait_for(lambda: runtime.capture.get("cam_test").latest())
        async with client_for(runtime) as client:
            snapshot = await client.get("/snapshot/cam_test.jpg")
            assert snapshot.status_code == 200
            assert snapshot.headers["content-type"] == "image/jpeg"
            assert snapshot.content[:2] == b"\xff\xd8"
            assert len(snapshot.content) > 1000

            missing = await client.get("/snapshot/khong_co.jpg")
            assert missing.status_code == 404
    finally:
        runtime.stop()


def test_render_jpeg_draws_overlay_and_falls_back(tmp_path, image_path):
    """The annotated frame must be a valid JPEG even with no live frame."""
    from app.web.server import JpegCache, _render_jpeg

    runtime = build_runtime(tmp_path, image_path)
    runtime.start()
    cache = JpegCache()
    try:
        assert wait_for(lambda: runtime.capture.get("cam_test").latest())
        data = _render_jpeg(runtime, cache, "cam_test", 70, 480)
        assert data[:2] == b"\xff\xd8"
        cached = cache.get("cam_test")
        assert cached is not None

        # unknown camera: placeholder, never an exception (UI shows offline card)
        placeholder = _render_jpeg(runtime, cache, "khong_co", 70, 480)
        assert placeholder[:2] == b"\xff\xd8"
    finally:
        runtime.stop()


@pytest.mark.asyncio
async def test_mqtt_endpoints(tmp_path, image_path):
    runtime = build_runtime(tmp_path, image_path)
    runtime.start()
    try:
        async with client_for(runtime) as client:
            monitor = await client.get("/api/mqtt/monitor?limit=10")
            assert monitor.status_code == 200

            published = await client.post(
                "/api/mqtt/publish", json={"topic": "test/x", "payload": '{"a":1}', "qos": 1}
            )
            body = published.json()
            assert body["ok"] is True
            assert body["dry_run"] is True

            dry = await client.post("/api/dry-run", json={"value": False})
            assert dry.json()["dry_run"] is False
            assert runtime.config.mqtt.dry_run is False
            await client.post("/api/dry-run", json={"value": True})
    finally:
        runtime.stop()


@pytest.mark.asyncio
async def test_auth_required_and_login(tmp_path, image_path):
    runtime = build_runtime(tmp_path, image_path, auth_enabled=True)
    runtime.start()
    try:
        async with client_for(runtime) as client:
            denied = await client.get("/api/state")
            assert denied.status_code == 401
            assert denied.json()["auth_required"] is True

            page = await client.get("/", follow_redirects=False)
            assert page.status_code in (307, 302, 303)
            assert page.headers["location"] == "/login"

            bad = await client.post("/api/login", json={"username": "admin", "password": "sai"})
            assert bad.status_code == 401

            good = await client.post("/api/login", json={"username": "admin", "password": "matkhau123"})
            assert good.status_code == 200
            raw_cookie = good.headers.get("set-cookie", "")
            assert COOKIE_NAME in raw_cookie
            cookie_pair = raw_cookie.split(";")[0]
            cookie_name, cookie_value = cookie_pair.split("=", 1)

            # httpx's ASGI transport does not keep a cookie jar: send it manually,
            # exactly like a browser would.
            allowed = await client.get("/api/state", cookies={cookie_name: cookie_value})
            assert allowed.status_code == 200

            via_token = await client.get("/api/state?token=tok3n")
            assert via_token.status_code == 200
    finally:
        runtime.stop()


@pytest.mark.asyncio
async def test_events_endpoint_and_wizard(tmp_path, image_path):
    runtime = build_runtime(tmp_path, image_path)
    runtime.start()
    try:
        async with client_for(runtime) as client:
            events = await client.get("/api/events?limit=20")
            assert events.status_code == 200
            assert isinstance(events.json()["events"], list)

            done = await client.post("/api/wizard/complete")
            assert done.json()["ok"] is True
            assert runtime.config.setup_completed is True
    finally:
        runtime.stop()


@pytest.mark.asyncio
async def test_scenario_builder_save_flow(tmp_path, image_path):
    """Mirrors what the "Kịch bản" tab sends: full config with a new scenario."""
    runtime = build_runtime(tmp_path, image_path)
    runtime.start()
    try:
        async with client_for(runtime) as client:
            config = (await client.get("/api/config")).json()["config"]
            config["scenarios"].append(
                {
                    "id": "kich_ban_moi",
                    "name": "Kịch bản mới",
                    "description": "",
                    "enabled": True,
                    "zone": "cam_test/khu_bep",
                    "cooldown_s": 2,
                    "active_schedule": None,
                    "on_enter": [
                        {
                            "after_s": 0,
                            "publish": {
                                "topic": "test/x",
                                "payload": {"state": "ON", "n": "{{count}}"},
                                "qos": 1,
                                "retain": False,
                            },
                        }
                    ],
                    "on_vacant": [
                        {"after_s": 30, "publish": {"topic": "test/x", "payload": {"state": "OFF"}}}
                    ],
                    "triggers": [
                        {
                            "type": "dwell",
                            "seconds": 60,
                            "steps": [
                                {"after_s": 0, "publish": {"topic": "test/y", "payload": {"dwell": True}}}
                            ],
                        }
                    ],
                }
            )
            saved = await client.put("/api/config", json={"config": config})
            assert saved.json()["ok"] is True
            assert "kich_ban_moi" in [s.id for s in runtime.config.scenarios]
            # the scenario engine picks it up without a restart
            assert "kich_ban_moi" in [s["id"] for s in runtime.scenario_engine.status()]

            # a scenario without any action must be rejected with a Vietnamese message
            broken = (await client.get("/api/config")).json()["config"]
            broken["scenarios"].append({"id": "rong", "name": "Rỗng", "zone": "cam_test/khu_bep"})
            rejected = await client.put("/api/config", json={"config": broken})
            body = rejected.json()
            assert body["ok"] is False
            assert "chưa có hành động nào" in body["error"]
    finally:
        runtime.stop()


@pytest.mark.asyncio
async def test_scenario_schedule_saved_and_reported(tmp_path, image_path):
    runtime = build_runtime(tmp_path, image_path)
    runtime.start()
    try:
        async with client_for(runtime) as client:
            config = (await client.get("/api/config")).json()["config"]
            config["scenarios"][0]["active_schedule"] = {
                "from": "18:00",
                "to": "06:00",
                "days": [1, 2, 3, 4, 5],
            }
            result = await client.put("/api/config", json={"config": config})
            assert result.json()["ok"] is True
            schedule = runtime.config.scenarios[0].active_schedule
            assert schedule is not None
            assert schedule.from_time == "18:00"
            assert schedule.to_time == "06:00"
            status = (await client.get("/api/state")).json()["scenarios"][0]
            assert "schedule_active" in status
    finally:
        runtime.stop()


@pytest.mark.asyncio
async def test_camera_enable_and_delete_endpoints(tmp_path, image_path):
    runtime = build_runtime(tmp_path, image_path)
    runtime.start()
    try:
        async with client_for(runtime) as client:
            off = await client.post("/api/cameras/cam_test/enabled", json={"value": False})
            assert off.json()["ok"] is True
            assert runtime.config.camera("cam_test").enabled is False
            # disabled cameras disappear from capture, but stay visible in the state
            assert runtime.capture.get("cam_test") is None
            state = (await client.get("/api/state")).json()
            entry = next(c for c in state["cameras"] if c["id"] == "cam_test")
            assert entry["enabled"] is False

            on = await client.post("/api/cameras/cam_test/enabled", json={"value": True})
            assert on.json()["enabled"] is True
            assert runtime.capture.get("cam_test") is not None

            deleted = await client.post("/api/cameras/cam_test/delete")
            assert deleted.json()["ok"] is True
            assert runtime.config.camera("cam_test") is None
            assert runtime.config.scenarios == []  # its scenarios went with it
            assert runtime.capture.get("cam_test") is None

            missing = await client.post("/api/cameras/khong_co/delete")
            assert missing.json()["ok"] is False
    finally:
        runtime.stop()


@pytest.mark.asyncio
async def test_state_exposes_platform_block(tmp_path, image_path):
    """The UI needs OS details to show Windows/macOS/Linux specific guidance."""
    runtime = build_runtime(tmp_path, image_path)
    runtime.start()
    try:
        async with client_for(runtime) as client:
            state = (await client.get("/api/state")).json()
            platform = state["platform"]
            assert platform["os"] in {"windows", "macos", "linux", "other"}
            assert platform["label"]
            assert platform["webcam_hint"]
            assert platform["loopback_hint"]
            assert isinstance(platform["webcam_backends"], list)
            assert state["shutdown_requested"] is False

            public = (await client.get("/api/state/public")).json()
            assert public["platform"]["os"] == platform["os"]

            info = (await client.get("/api/system/info")).json()
            assert info["os"] == platform["os"]
    finally:
        runtime.stop()


@pytest.mark.asyncio
async def test_shutdown_endpoint_sets_flag(tmp_path, image_path):
    """POST /api/system/shutdown is the scriptable graceful stop (Windows friendly)."""
    runtime = build_runtime(tmp_path, image_path)
    runtime.start()
    try:
        async with client_for(runtime) as client:
            assert runtime.shutdown_requested is False
            response = await client.post("/api/system/shutdown")
            assert response.status_code == 200
            body = response.json()
            assert body["ok"] is True
            assert runtime.shutdown_requested is True
            # The flag is what ``serve`` polls to exit its run loop.
            assert runtime.wait_for_shutdown(0.1) is True
    finally:
        runtime.stop()


@pytest.mark.asyncio
async def test_zone_save_keeps_shape_chosen_in_ui(tmp_path, image_path):
    """UI gửi kèm `shape` (rect/polygon) — phải được validate và lưu lại nguyên vẹn."""
    runtime = build_runtime(tmp_path, image_path)
    runtime.start()
    try:
        async with client_for(runtime) as client:
            zones = [
                {
                    "id": "khu_bep",
                    "name": "Khu bếp",
                    "polygon": [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]],
                    "shape": "rect",
                    "classes": ["person"],
                },
                {
                    "id": "ban_an",
                    "name": "Bàn ăn",
                    "polygon": [[0.2, 0.2], [0.5, 0.2], [0.5, 0.5], [0.2, 0.5]],
                    "shape": "polygon",
                    "classes": ["person"],
                },
            ]
            result = await client.post("/api/cameras/cam_test/zones", json={"zones": zones})
            assert result.status_code == 200, result.text
            saved = runtime.config.camera("cam_test")
            assert saved is not None
            assert [zone.shape for zone in saved.zones] == ["rect", "polygon"]

            bad = [dict(zones[0], shape="blob")]
            rejected = await client.post("/api/cameras/cam_test/zones", json={"zones": bad})
            assert rejected.status_code == 400
            assert "shape" in rejected.text
            assert [zone.shape for zone in runtime.config.camera("cam_test").zones] == ["rect", "polygon"]
    finally:
        runtime.stop()


# --------------------------------------------------------------------------- #
# Mỗi vùng phải kèm "vì sao trống/có người" để UI giải thích cho người dùng
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_state_exposes_zone_analysis(tmp_path, image_path):
    """Mỗi vùng phải kèm "vì sao trống/có người" để UI giải thích cho người dùng."""
    runtime = build_runtime(tmp_path, image_path)
    runtime.start()
    try:
        # Runtime không nạp YOLO trong test: gắn detector giả để có kết quả suy luận.
        runtime.detector = FakeDetector()
        runtime.detector_enabled = True
        runtime._start_inference("cam_test")
        async with client_for(runtime) as client:
            analysis: dict = {}
            for _ in range(200):
                state = await client.get("/api/state")
                assert state.status_code == 200
                zone_state = state.json()["cameras"][0]["zone_states"].get("khu_bep") or {}
                analysis = zone_state.get("analysis") or {}
                if analysis.get("reason"):
                    break
                await asyncio.sleep(0.02)
            assert analysis, "không có phân tích cho vùng khu_bep"
            assert analysis["anchor"] == "bottom_center"
            assert "detections" in analysis
            assert isinstance(analysis["reason"], str) and analysis["reason"]
    finally:
        runtime.stop()
