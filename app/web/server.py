"""FastAPI web layer: REST + WebSocket + MJPEG streams.

No build step and no npm: everything served from ``app/web/static`` is plain
HTML/CSS/JS.  The server is intentionally thin — all state lives in
:class:`app.runtime.Runtime`, which is thread-safe, so handlers only read
snapshots or call small mutators.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import time
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles

from .. import platform_compat as compat
from ..annotate import annotate, encode_jpeg, placeholder_jpeg
from ..config import ConfigError, config_to_ui_dict, config_to_yaml
from ..runtime import Event, Runtime

LOGGER = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
COOKIE_NAME = "vision_auth"
MJPEG_BOUNDARY = "visionframe"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _make_cookie(secret: str, username: str, hours: int) -> str:
    expiry = int(time.time() + hours * 3600)
    payload = f"{username}:{expiry}"
    signature = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()[:40]
    return base64.urlsafe_b64encode(f"{payload}:{signature}".encode()).decode()


def _check_cookie(secret: str, value: str | None) -> bool:
    if not value:
        return False
    try:
        decoded = base64.urlsafe_b64decode(value.encode()).decode()
        username, expiry, signature = decoded.rsplit(":", 2)
        if int(expiry) < time.time():
            return False
        expected = hmac.new(
            secret.encode(), f"{username}:{expiry}".encode(), hashlib.sha256
        ).hexdigest()[:40]
        return hmac.compare_digest(expected, signature)
    except Exception:  # noqa: BLE001 - malformed cookie means "not logged in"
        return False


class JpegCache:
    """Last good JPEG per camera, so the UI shows a frozen frame, never a hang."""

    def __init__(self) -> None:
        self._items: dict[str, tuple[bytes, float]] = {}
        self._lock = asyncio.Lock()

    def get(self, camera_id: str) -> tuple[bytes, float] | None:
        return self._items.get(camera_id)

    def put(self, camera_id: str, data: bytes) -> None:
        self._items[camera_id] = (data, time.time())


def _client_token(request: Request | WebSocket, runtime: Runtime) -> str | None:
    token = request.query_params.get("token")
    if token:
        configured = runtime.config.web.auth.token
        if configured and hmac.compare_digest(token, configured):
            return token
    return None


def _is_authenticated(request: Request | WebSocket, runtime: Runtime) -> bool:
    auth = runtime.config.web.auth
    if not auth.enabled:
        return True
    if _client_token(request, runtime):
        return True
    value = request.cookies.get(COOKIE_NAME)
    return _check_cookie(auth.session_secret or auth.password or "vision", value)


# --------------------------------------------------------------------------- #
# app factory
# --------------------------------------------------------------------------- #
def create_app(runtime: Runtime) -> FastAPI:
    app = FastAPI(title="Vision Automation", docs_url=None, redoc_url=None)
    jpeg_cache = JpegCache()
    app.state.runtime = runtime
    app.state.jpeg_cache = jpeg_cache

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # -- auth -------------------------------------------------------------- #
    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):  # noqa: ANN001, ANN202
        auth = runtime.config.web.auth
        path = request.url.path
        public = (
            path == "/login"
            or path.startswith("/static/")
            or path == "/favicon.ico"
            or path == "/api/login"
            or path == "/api/state/public"
        )
        if public or not auth.enabled or _is_authenticated(request, runtime):
            return await call_next(request)
        if path.startswith("/api/"):
            return JSONResponse({"ok": False, "error": "Chưa đăng nhập", "auth_required": True}, status_code=401)
        return RedirectResponse("/login")

    # -- pages ------------------------------------------------------------- #
    def _render_index() -> str:
        """Serve index.html with a cache-buster appended to app.js / style.css.

        Without it a browser keeps running yesterday's app.js after an upgrade,
        so the user reports bugs that were already fixed.
        """
        target = STATIC_DIR / "index.html"
        try:
            html = target.read_text(encoding="utf-8")
            stamp = str(int(max((STATIC_DIR / name).stat().st_mtime for name in ("app.js", "style.css"))))
        except OSError:
            return ""
        html = html.replace('href="/static/style.css"', f'href="/static/style.css?v={stamp}"')
        html = html.replace('src="/static/app.js"', f'src="/static/app.js?v={stamp}"')
        return html

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        html = _render_index()
        if not html:
            return HTMLResponse("<h1>Thiếu app/web/static/index.html</h1>", status_code=500)
        return HTMLResponse(html, headers={"Cache-Control": "no-cache"})

    @app.get("/login", response_class=HTMLResponse)
    async def login_page() -> HTMLResponse:
        auth = runtime.config.web.auth
        if not auth.enabled:
            return RedirectResponse("/")
        return HTMLResponse(LOGIN_HTML)

    @app.get("/favicon.ico")
    async def favicon() -> Response:
        return Response(status_code=204)

    @app.post("/api/login")
    async def login(request: Request) -> JSONResponse:
        auth = runtime.config.web.auth
        if not auth.enabled:
            return JSONResponse({"ok": True})
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            payload = {}
        username = str(payload.get("username", ""))
        password = str(payload.get("password", ""))
        ok_user = hmac.compare_digest(username, auth.username or "")
        ok_pass = bool(auth.password) and hmac.compare_digest(password, auth.password or "")
        if auth.token and hmac.compare_digest(password, auth.token):
            ok_pass = True
        if not (ok_user and ok_pass):
            return JSONResponse({"ok": False, "error": "Sai tên đăng nhập hoặc mật khẩu."}, status_code=401)
        response = JSONResponse({"ok": True})
        secret = auth.session_secret or auth.password or "vision"
        response.set_cookie(
            COOKIE_NAME,
            _make_cookie(secret, username, auth.session_hours),
            max_age=auth.session_hours * 3600,
            httponly=True,
            samesite="lax",
        )
        return response

    @app.post("/api/logout")
    async def logout() -> JSONResponse:
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE_NAME)
        return response

    # -- state ------------------------------------------------------------- #
    @app.get("/api/state")
    async def state() -> JSONResponse:
        return JSONResponse(runtime.snapshot_state())

    @app.get("/api/state/public")
    async def public_state() -> JSONResponse:
        """Unauthenticated probe used by the login page (no secrets inside)."""
        return JSONResponse(
            {
                "auth_enabled": runtime.config.web.auth.enabled,
                "version": "1.0.0",
                "dry_run": runtime.dry_run,
                "platform": runtime.platform_info(),
            }
        )

    @app.get("/api/stats")
    async def stats() -> JSONResponse:
        return JSONResponse(runtime.stats())

    @app.post("/api/system/shutdown")
    async def system_shutdown(request: Request) -> JSONResponse:
        """Stop the app gracefully (Windows launchers / Task Scheduler / NSSM).

        Publishes MQTT "offline", closes every stream and saves ``config.yaml``
        exactly like Ctrl-C does. Requires the same auth as the rest of the API,
        so ``?token=...`` works for scripts.
        """
        LOGGER.info("API yêu cầu dừng ứng dụng từ %s", getattr(request.client, "host", "?"))
        runtime.request_shutdown("api")
        return JSONResponse({"ok": True, "message": "Đang dừng ứng dụng…"})

    @app.get("/api/system/info")
    async def system_info() -> JSONResponse:
        """OS/platform details for the UI (also embedded in ``/api/state``)."""
        return JSONResponse(runtime.platform_info())

    @app.get("/api/events")
    async def events(limit: int = 100, kind: str | None = None, level: str | None = None) -> JSONResponse:
        return JSONResponse({"events": runtime.events.recent(limit=limit, kind=kind, level=level)})

    # -- config ------------------------------------------------------------ #
    @app.get("/api/config")
    async def get_config() -> JSONResponse:
        return JSONResponse(
            {
                "config": config_to_ui_dict(runtime.config),
                "version": runtime.config_manager.version,
                "path": str(runtime.config_manager.path),
            }
        )

    @app.put("/api/config")
    async def put_config(request: Request) -> JSONResponse:
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": "JSON không hợp lệ."}, status_code=400)
        data = payload.get("config", payload)
        return JSONResponse(runtime.update_config_data(data))

    @app.get("/api/config/yaml", response_class=Response)
    async def get_yaml() -> Response:
        return Response(config_to_yaml(runtime.config), media_type="text/plain; charset=utf-8")

    @app.put("/api/config/yaml")
    async def put_yaml(request: Request) -> JSONResponse:
        text = (await request.body()).decode("utf-8", errors="replace")
        try:
            runtime.config_manager.update_yaml(text)
            return JSONResponse({"ok": True, "version": runtime.config_manager.version})
        except ConfigError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    @app.post("/api/config/validate")
    async def validate_config(request: Request) -> JSONResponse:
        """Live YAML validation for the "Nâng cao" tab (never persists)."""
        text = (await request.body()).decode("utf-8", errors="replace")
        from ..config import load_config_text  # noqa: PLC0415

        try:
            load_config_text(text, "YAML (kiểm tra)")
            return JSONResponse({"ok": True, "message": "YAML hợp lệ ✔"})
        except ConfigError as exc:
            return JSONResponse({"ok": False, "error": str(exc)})

    # -- zones -------------------------------------------------------------- #
    def _orphan_scenarios(
        data: dict[str, Any], camera_id: str, kept_zone_ids: set[str]
    ) -> list[dict[str, str]]:
        """Scenarios of *camera_id* that point at a zone which is about to vanish."""
        orphans: list[dict[str, str]] = []
        for scenario in data.get("scenarios", []) or []:
            ref = str(scenario.get("zone") or "")
            owner, _, zone_id = ref.partition("/")
            if owner != camera_id or not zone_id:
                continue
            if zone_id in kept_zone_ids:
                continue
            orphans.append(
                {
                    "scenario_id": str(scenario.get("id") or ""),
                    "scenario_name": str(scenario.get("name") or scenario.get("id") or ""),
                    "zone_ref": ref,
                    "zone_id": zone_id,
                }
            )
        return orphans

    @app.post("/api/cameras/{camera_id}/zones")
    async def save_zones(camera_id: str, request: Request) -> JSONResponse:
        """Replace a camera's zone list.

        Body: ``{zones: [...], renames: {old_id: new_id}, drop_orphan_scenarios: bool}``.

        ``renames`` lets the UI change a zone id without breaking the scenarios
        that still point at the old id; ``drop_orphan_scenarios`` deletes the
        scenarios whose zone no longer exists (the UI asks the user first and
        then retries, so the error message stays actionable instead of cryptic).
        """
        payload = await request.json()
        zones = payload.get("zones")
        if not isinstance(zones, list):
            return JSONResponse({"ok": False, "error": "Thiếu danh sách zones."}, status_code=400)
        camera = runtime.config.camera(camera_id)
        if camera is None:
            return JSONResponse({"ok": False, "error": "Không thấy camera."}, status_code=404)

        raw_renames = payload.get("renames") or {}
        renames: dict[str, str] = {}
        if isinstance(raw_renames, dict):
            for old_id, new_id in raw_renames.items():
                if isinstance(old_id, str) and isinstance(new_id, str) and old_id != new_id:
                    renames[old_id] = new_id
        drop_orphans = bool(payload.get("drop_orphan_scenarios"))

        kept_ids = {
            str(zone.get("id"))
            for zone in zones
            if isinstance(zone, dict) and zone.get("id")
        }
        before = runtime.config.model_dump(by_alias=True)
        orphans = _orphan_scenarios(before, camera_id, kept_ids)
        # A rename keeps the scenario alive, so those refs are not orphans.
        orphans = [item for item in orphans if item["zone_id"] not in renames]
        if orphans and not drop_orphans:
            names = ", ".join(f"“{item['scenario_name']}”" for item in orphans)
            zone_ids = ", ".join(sorted({item["zone_id"] for item in orphans}))
            return JSONResponse(
                {
                    "ok": False,
                    "error": (
                        f"Không lưu được: vùng “{zone_ids}” đang được kịch bản {names} sử dụng. "
                        "Hãy đổi tên vùng thay vì xoá, hoặc xoá kịch bản đó trước."
                    ),
                    "orphans": orphans,
                    "hint": "Bấm OK ở hộp thoại để xoá luôn các kịch bản đó và lưu vùng.",
                },
                status_code=409,
            )

        def mutate(data: dict[str, Any]) -> None:
            target = next((c for c in data.get("cameras", []) if c["id"] == camera_id), None)
            if target is None:
                raise ConfigError("Camera đã bị xoá.")
            target["zones"] = zones
            scenarios = data.get("scenarios", []) or []
            if renames:
                for scenario in scenarios:
                    ref = str(scenario.get("zone") or "")
                    owner, _, zone_id = ref.partition("/")
                    if owner == camera_id and zone_id in renames:
                        scenario["zone"] = f"{camera_id}/{renames[zone_id]}"
            if drop_orphans and orphans:
                drop = {item["scenario_id"] for item in orphans}
                data["scenarios"] = [s for s in scenarios if str(s.get("id")) not in drop]

        try:
            runtime.config_manager.patch_raw(mutate)
        except ConfigError as exc:
            return JSONResponse(
                {"ok": False, "error": str(exc), "orphans": orphans}, status_code=400
            )
        message = f"Đã lưu {len(zones)} vùng cho {camera_id}"
        if orphans and drop_orphans:
            message += f" (đã xoá {len(orphans)} kịch bản dùng vùng đã mất)"
        if renames:
            message += " · đã cập nhật kịch bản theo mã vùng mới"
        runtime.events.add("config", message)
        return JSONResponse(
            {
                "ok": True,
                "renamed": renames,
                "dropped_scenarios": [item["scenario_id"] for item in orphans] if drop_orphans else [],
            }
        )

    @app.get("/api/detector/classes")
    async def detector_classes() -> JSONResponse:
        detector = runtime.detector
        names = detector.class_names() if detector is not None else {}
        return JSONResponse(
            {
                "classes": [{"id": key, "name": value} for key, value in sorted(names.items())],
                "info": detector.info() if detector is not None else {"available": False},
            }
        )

    # -- scenarios ---------------------------------------------------------- #
    @app.post("/api/scenarios/{scenario_id}/test")
    async def test_scenario(scenario_id: str, request: Request) -> JSONResponse:
        payload: dict[str, Any] = {}
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            pass
        trigger_index = int(payload.get("trigger", 0) or 0)
        step_index = int(payload.get("step", 0) or 0)
        return JSONResponse(runtime.send_test_command(scenario_id, trigger_index, step_index))

    @app.post("/api/scenarios/{scenario_id}/toggle")
    async def toggle_scenario(scenario_id: str, request: Request) -> JSONResponse:
        payload = await request.json()
        enabled = bool(payload.get("enabled", True))

        def mutate(data: dict) -> None:
            target = next((s for s in data.get("scenarios", []) if s["id"] == scenario_id), None)
            if target is None:
                raise ConfigError("Không thấy kịch bản.")
            target["enabled"] = enabled

        try:
            runtime.config_manager.patch_raw(mutate)
        except ConfigError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        return JSONResponse({"ok": True, "enabled": enabled})

    # -- simulation --------------------------------------------------------- #
    @app.post("/api/simulate")
    async def simulate(request: Request) -> JSONResponse:
        payload = await request.json()
        zone_ref = str(payload.get("zone", ""))
        occupied = bool(payload.get("occupied", True))
        count = int(payload.get("count", 1) or 1)
        if not zone_ref:
            return JSONResponse({"ok": False, "error": "Thiếu vùng."}, status_code=400)
        return JSONResponse(runtime.simulate_zone(zone_ref, occupied, count=count))

    @app.post("/api/simulate/clear")
    async def simulate_clear(request: Request) -> JSONResponse:
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            payload = {}
        return JSONResponse(runtime.clear_simulation(payload.get("zone")))

    @app.post("/api/simulate/scale")
    async def simulate_scale(request: Request) -> JSONResponse:
        payload = await request.json()
        return JSONResponse(runtime.set_simulation_scale(float(payload.get("scale", 1) or 1)))

    # -- dry run ------------------------------------------------------------ #
    @app.post("/api/dry-run")
    async def set_dry_run(request: Request) -> JSONResponse:
        payload = await request.json()
        runtime.dry_run = bool(payload.get("value", True))
        return JSONResponse({"ok": True, "dry_run": runtime.dry_run})

    # -- mqtt --------------------------------------------------------------- #
    @app.get("/api/mqtt/monitor")
    async def mqtt_monitor(direction: str | None = None, topic: str = "", limit: int = 100) -> JSONResponse:
        return JSONResponse(
            {"entries": runtime.mqtt.monitor(direction=direction, topic_filter=topic, limit=limit)}
        )

    @app.post("/api/mqtt/test")
    async def mqtt_test() -> JSONResponse:
        return JSONResponse(await asyncio.to_thread(runtime.test_mqtt))

    @app.post("/api/mqtt/publish")
    async def mqtt_publish(request: Request) -> JSONResponse:
        payload = await request.json()
        topic = str(payload.get("topic", "")).strip()
        if not topic:
            return JSONResponse({"ok": False, "error": "Thiếu topic."}, status_code=400)
        body = payload.get("payload", "")
        if isinstance(body, str):
            try:
                body = json.loads(body)
            except Exception:  # noqa: BLE001 - plain string payloads are valid
                pass
        ok = runtime.mqtt.publish_raw(
            topic, body, qos=int(payload.get("qos", 1) or 0), retain=bool(payload.get("retain", False))
        )
        return JSONResponse({"ok": True, "sent": ok, "dry_run": runtime.dry_run})

    # -- webcam / test camera ----------------------------------------------- #
    @app.get("/api/webcams")
    async def webcams() -> JSONResponse:
        return JSONResponse({"webcams": await asyncio.to_thread(runtime.webcams, max_age=5.0)})

    @app.post("/api/test-camera/add")
    async def add_test_camera() -> JSONResponse:
        return JSONResponse(runtime.ensure_test_camera(requested=True))

    @app.post("/api/cameras/{camera_id}/delete")
    async def delete_camera(camera_id: str) -> JSONResponse:
        return JSONResponse(runtime.delete_camera(camera_id))

    @app.post("/api/cameras/{camera_id}/enabled")
    async def set_camera_enabled(camera_id: str, request: Request) -> JSONResponse:
        payload = await request.json()
        return JSONResponse(runtime.set_camera_enabled(camera_id, bool(payload.get("value", True))))

    @app.post("/api/test-camera/{camera_id}/promote")
    async def promote_test_camera(camera_id: str) -> JSONResponse:
        return JSONResponse(runtime.promote_test_camera(camera_id))

    @app.post("/api/test-camera/{camera_id}/delete")
    async def delete_test_camera(camera_id: str) -> JSONResponse:
        return JSONResponse(runtime.delete_test_camera(camera_id))

    @app.post("/api/loopback/start")
    async def loopback_start() -> JSONResponse:
        return JSONResponse(await asyncio.to_thread(runtime.start_loopback))

    @app.post("/api/loopback/stop")
    async def loopback_stop() -> JSONResponse:
        return JSONResponse(runtime.stop_loopback())

    # -- wizard ------------------------------------------------------------- #
    @app.post("/api/wizard/complete")
    async def wizard_complete() -> JSONResponse:
        def mutate(data: dict) -> None:
            data["setup_completed"] = True

        try:
            runtime.config_manager.patch_raw(mutate)
        except ConfigError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        return JSONResponse({"ok": True})

    # -- MJPEG -------------------------------------------------------------- #
    @app.get("/stream/{camera_id}.mjpg")
    async def stream(camera_id: str) -> StreamingResponse:
        worker = runtime.capture.get(camera_id)
        if worker is None:
            raise HTTPException(status_code=404, detail="Không thấy camera")
        watcher = f"mjpeg:{camera_id}:{id(worker)}"
        worker.add_watcher(watcher)
        web = runtime.config.web

        async def frames() -> AsyncIterator[bytes]:
            try:
                interval = 1.0 / max(1.0, float(web.mjpeg_fps))
                while True:
                    started = time.perf_counter()
                    body = await asyncio.to_thread(
                        _render_jpeg, runtime, jpeg_cache, camera_id, web.jpeg_quality, web.stream_width
                    )
                    yield (
                        b"--" + MJPEG_BOUNDARY.encode() + b"\r\n"
                        b"Content-Type: image/jpeg\r\n"
                        b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body + b"\r\n"
                    )
                    elapsed = time.perf_counter() - started
                    await asyncio.sleep(max(0.0, interval - elapsed))
            except asyncio.CancelledError:  # client navigated away
                raise
            finally:
                worker.remove_watcher(watcher)

        return StreamingResponse(
            frames(),
            media_type=f"multipart/x-mixed-replace; boundary={MJPEG_BOUNDARY}",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.get("/snapshot/{camera_id}.jpg")
    async def snapshot(camera_id: str) -> Response:
        if runtime.config.camera(camera_id) is None:
            raise HTTPException(status_code=404, detail="Không thấy camera")
        web = runtime.config.web
        data = await asyncio.to_thread(
            _render_jpeg, runtime, jpeg_cache, camera_id, max(85, web.jpeg_quality), None
        )
        if web.allow_snapshot_save:
            try:
                folder = Path(web.snapshot_dir)
                folder.mkdir(parents=True, exist_ok=True)
                stamp = time.strftime("%Y%m%d-%H%M%S")
                (folder / f"{camera_id}-{stamp}.jpg").write_bytes(data)
            except Exception:  # noqa: BLE001 - saving is a convenience, never fatal
                LOGGER.debug("Không lưu được snapshot", exc_info=True)
        return Response(content=data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    # -- websocket ---------------------------------------------------------- #
    @app.websocket("/ws")
    async def websocket(ws: WebSocket) -> None:
        if not _is_authenticated(ws, runtime):
            await ws.close(code=4401)
            return
        await ws.accept()
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=200)

        def on_event(event: Event) -> None:
            try:
                message = json.dumps({"type": "event", "event": event.as_dict()}, ensure_ascii=False)
            except Exception:  # noqa: BLE001
                return
            try:
                loop.call_soon_threadsafe(queue.put_nowait, message)
            except Exception:  # noqa: BLE001 - closed loop / full queue
                pass

        runtime.events.subscribe(on_event)
        interval = 1.0 / max(1.0, float(runtime.config.web.ui_ws_hz))

        async def pump_events() -> None:
            while True:
                message = await queue.get()
                await ws.send_text(message)

        async def pump_state() -> None:
            while True:
                try:
                    payload = await asyncio.to_thread(runtime.snapshot_state)
                    await ws.send_text(json.dumps({"type": "state", "state": payload}, ensure_ascii=False))
                except (WebSocketDisconnect, RuntimeError):
                    return
                except Exception:  # noqa: BLE001 - never break the socket
                    LOGGER.debug("WS state lỗi", exc_info=True)
                await asyncio.sleep(interval)

        tasks = [asyncio.create_task(pump_events()), asyncio.create_task(pump_state())]
        try:
            while True:
                await ws.receive_text()  # keepalive / client messages ignored
        except WebSocketDisconnect:
            pass
        except Exception:  # noqa: BLE001
            pass
        finally:
            runtime.events.unsubscribe(on_event)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    return app


def _render_jpeg(
    runtime: Runtime,
    cache: JpegCache,
    camera_id: str,
    quality: int,
    width: int | None,
) -> bytes:
    """Annotated JPEG for a camera, falling back to the last good frame."""
    worker = runtime.capture.get(camera_id)
    result = runtime.frame_for(camera_id)
    meta = {
        "camera_name": runtime.config.camera(camera_id).name if runtime.config.camera(camera_id) else camera_id,
        "mqtt_connected": runtime.mqtt.connected,
        "dry_run": runtime.dry_run,
        "is_test": bool(runtime.config.camera(camera_id) and runtime.config.camera(camera_id).is_test),
    }
    if result is None or worker is None:
        cached = cache.get(camera_id)
        if cached is not None:
            return cached[0]
        return placeholder_jpeg(message="Chua co tin hieu tu camera")

    frame, overlay = result
    status = worker.status()
    stats = runtime.stats()["capture_fps"].get(camera_id, 0.0)
    meta.update(
        {
            "capture_fps": stats,
            "inference_fps": overlay.get("inference", {}).get("fps", 0.0),
            "inference_ms": overlay.get("inference", {}).get("ms", 0.0),
            "connected": status.connected and not status.stale,
            "warning": "DUNG HINH CU - CAMERA MAT" if not status.connected else None,
        }
    )
    try:
        canvas = annotate(frame, overlay, meta=meta)
        data = encode_jpeg(canvas, quality=quality, width=width)
    except Exception:  # noqa: BLE001 - never break the stream on a draw error
        LOGGER.exception("Không vẽ được overlay cho %s", camera_id)
        cached = cache.get(camera_id)
        if cached is not None:
            return cached[0]
        return placeholder_jpeg(message="Loi hien thi hinh anh")
    cache.put(camera_id, data)
    return data


LOGIN_HTML = """<!doctype html>
<html lang="vi"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Đăng nhập — Vision Automation</title>
<style>
 body{font-family:system-ui,-apple-system,"Segoe UI",sans-serif;background:#0f172a;color:#e2e8f0;
      display:flex;align-items:center;justify-content:center;height:100vh;margin:0}
 form{background:#1e293b;padding:28px;border-radius:14px;width:min(360px,90vw);box-shadow:0 10px 30px #0006}
 h1{font-size:18px;margin:0 0 18px}
 label{display:block;font-size:13px;margin:12px 0 4px;color:#94a3b8}
 input{width:100%;padding:10px;border-radius:8px;border:1px solid #334155;background:#0f172a;color:#e2e8f0}
 button{margin-top:18px;width:100%;padding:11px;border:0;border-radius:8px;background:#22c55e;color:#04210f;
        font-weight:600;cursor:pointer}
 p.err{color:#f87171;font-size:13px;min-height:18px}
</style></head><body>
<form onsubmit="event.preventDefault();login()">
  <h1>Vision Automation</h1>
  <label for="u">Tên đăng nhập</label>
  <input id="u" autocomplete="username" value="admin">
  <label for="p">Mật khẩu</label>
  <input id="p" type="password" autocomplete="current-password">
  <p class="err" id="err"></p>
  <button type="submit">Đăng nhập</button>
</form>
<script>
async function login(){
  const res = await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({username:document.getElementById('u').value,password:document.getElementById('p').value})});
  const data = await res.json().catch(()=>({}));
  if(res.ok && data.ok){ location.href='/'; }
  else { document.getElementById('err').textContent = data.error || 'Đăng nhập thất bại'; }
}
</script></body></html>
"""


def serve(runtime: Runtime, *, host: str = "0.0.0.0", port: int = 8080) -> None:
    """Run uvicorn (blocking) with the app bound to ``runtime``.

    uvicorn handles SIGTERM by *re-raising* the signal after a graceful shutdown
    (``Server.capture_signals``), which would kill the process before our cleanup
    runs. Because that only happens in the main thread, uvicorn is started in a
    worker thread and signals are handled here: set ``should_exit``, wait for the
    server, and let ``main`` publish "offline", close the cameras and save config.
    """
    import threading  # noqa: PLC0415
    import time  # noqa: PLC0415

    import uvicorn  # noqa: PLC0415 - only needed in web mode

    app = create_app(runtime)
    config = uvicorn.Config(
        app, host=host, port=port, log_level="warning", access_log=False
    )
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, name="uvicorn", daemon=True)
    thread.start()

    stop = threading.Event()

    def handle_signal(signum, frame) -> None:  # noqa: ARG001
        LOGGER.info("Nhận tín hiệu %s — đang dừng web server", signum)
        server.should_exit = True
        stop.set()
        runtime.request_shutdown("signal")

    import signal as signal_module  # noqa: PLC0415

    signal_names = ["SIGTERM", "SIGINT"]
    if compat.is_windows():
        # The Windows console never delivers SIGTERM (that is why
        # ``terminate()`` kills the process outright); CTRL_BREAK is the
        # console-friendly signal, used by Dung-lai.bat / NSSM.
        signal_names.append("SIGBREAK")
    for name in signal_names:
        sig = getattr(signal_module, name, None)
        if sig is None:
            continue
        try:
            signal_module.signal(sig, handle_signal)
        except (ValueError, OSError, RuntimeError):  # not the main thread / unsupported
            LOGGER.debug("Không đặt được handler cho %s", name, exc_info=True)

    shown_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    LOGGER.info("Web UI: http://%s:%d", shown_host, port)
    print(f"\n  ➜  Web UI (máy này): http://{shown_host}:{port}")
    if host in {"0.0.0.0", "::"}:
        lan_ip = compat.local_ip()
        if lan_ip not in {"127.0.0.1", shown_host}:
            print(f"  ➜  Điện thoại/máy khác: http://{lan_ip}:{port}")
    print(f"  ➜  Hệ điều hành: {compat.os_label()}\n", flush=True)

    try:
        while not stop.is_set() and thread.is_alive():
            if runtime.shutdown_requested:  # POST /api/system/shutdown
                LOGGER.info("Dừng theo yêu cầu API")
                server.should_exit = True
                break
            time.sleep(0.3)
    except KeyboardInterrupt:  # pragma: no cover - terminal Ctrl-C
        server.should_exit = True
    finally:
        server.should_exit = True
        thread.join(timeout=20)


__all__ = ["create_app", "serve", "JpegCache"]
