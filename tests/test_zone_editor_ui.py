"""Kiểm thử TRÌNH SỬA VÙNG bằng trình duyệt thật (Playwright headless Chromium).

Mục đích: bắt được nhóm lỗi "kéo thả điểm/vùng hoạt động kém" — những lỗi chỉ
xuất hiện khi canvas thật sự được layout (khác 0,0), ảnh hiển thị khác kích
thước tự nhiên, và màn hình có devicePixelRatio = 2 (MacBook Retina).

Cách làm: dựng một HTTP server tí hon phục vụ đúng các file trong
``app/web/static/`` + vài endpoint giả (``/api/config``, ``/api/state``…), rồi
mở trang bằng Playwright. Nhờ vậy test không cần camera, không cần YOLO, và
chạy được trong CI.

Toạ độ kỳ vọng được tính từ ``#zone-img.getBoundingClientRect()`` — chính là
khung ảnh THẬT do trình duyệt vẽ ra, không phải công thức letterbox tự đoán.
"""

from __future__ import annotations

import io
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import cv2
import numpy as np
import pytest

STATIC = Path(__file__).resolve().parents[1] / "app" / "web" / "static"

playwright_api = pytest.importorskip("playwright.sync_api", reason="cần Playwright để test UI")
sync_playwright = playwright_api.sync_playwright

BROWSERS_PATH = Path(__file__).resolve().parents[1] / ".run" / "pw-browsers"

IMAGE_W, IMAGE_H = 640, 480

ZONES = [
    {
        "id": "khuvuc_test",
        "name": "Vùng thử",
        "polygon": [[0.10, 0.20], [0.60, 0.20], [0.60, 0.80], [0.10, 0.80]],
        "shape": "rect",
        "classes": ["person"],
        "color": "#22c55e",
        "anchor": "bottom_center",
        "min_confidence": 0.45,
        "enter_confirm": {"frames": 3, "of": 5},
        "exit_grace_s": 3,
        "enabled": True,
        "publish_state": True,
    },
    {
        "id": "vung_2",
        "name": "Đa giác",
        "polygon": [[0.70, 0.15], [0.92, 0.25], [0.90, 0.70], [0.80, 0.85], [0.68, 0.60]],
        "shape": "polygon",
        "classes": ["person"],
        "color": "#38bdf8",
        "anchor": "bottom_center",
        "min_confidence": 0.45,
        "enter_confirm": {"frames": 3, "of": 5},
        "exit_grace_s": 3,
        "enabled": True,
        "publish_state": True,
    },
]


def _jpeg_bytes() -> bytes:
    """Ảnh JPEG 640×480 (có vân để mắt người thấy rõ khung hình)."""
    image = np.zeros((IMAGE_H, IMAGE_W, 3), dtype=np.uint8)
    image[:, :] = (28, 34, 44)
    for x in range(0, IMAGE_W, 40):
        cv2.line(image, (x, 0), (x, IMAGE_H), (60, 70, 90), 1)
    for y in range(0, IMAGE_H, 40):
        cv2.line(image, (0, y), (IMAGE_W, y), (60, 70, 90), 1)
    cv2.rectangle(image, (20, 20), (IMAGE_W - 20, IMAGE_H - 20), (90, 140, 200), 3)
    ok, buffer = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
    assert ok
    return buffer.tobytes()


JPEG = _jpeg_bytes()


def _config_payload() -> dict:
    return {
        "config": {
            "schema_version": 1,
            "setup_completed": True,
            "mqtt": {"host": "127.0.0.1", "dry_run": True},
            "detector": {"model": "yolo11n.pt", "detect_fps": 4, "min_confidence": 0.45},
            "cameras": [
                {
                    "id": "cam_test",
                    "name": "Camera thử",
                    "source": "image",
                    "url": "samples/bus.jpg",
                    "enabled": True,
                    "test": False,
                    "zones": ZONES,
                }
            ],
            "scenarios": [],
            "testing": {},
            "web": {"port": 0},
            "logging": {},
        },
        "version": 7,
    }


def _state_payload() -> dict:
    return {
        "generated_at": 0,
        "uptime_s": 12.0,
        "dry_run": True,
        "cameras": [
            {
                "id": "cam_test",
                "name": "Camera thử",
                "source": "image",
                "enabled": True,
                "test": False,
                "capture": {"connected": True, "stale": False, "frame_age_ms": 40, "read_ms": 5},
                "inference": {"fps": 4.0, "ms": 20.0},
                "detections": [],
                "zones": ZONES,
                "zone_states": {
                    "khuvuc_test": {
                        "occupied": True,
                        "count": 2,
                        "class_counts": {"person": 2},
                        "anchor": "bottom_center",
                        "overlap_ratio": 0.5,
                        "analysis": {
                            "detections": 3,
                            "matched": 2,
                            "outside": 1,
                            "ignored_class": 0,
                            "ignored_confidence": 0,
                            "anchor": "bottom_center",
                            "best_ratio": 1.0,
                            "reason": "2 đối tượng trong vùng",
                        },
                    },
                    "vung_2": {
                        "occupied": False,
                        "count": 0,
                        "class_counts": {},
                        "anchor": "bottom_center",
                        "overlap_ratio": 0.5,
                        "analysis": {
                            "detections": 3,
                            "matched": 0,
                            "outside": 1,
                            "ignored_class": 0,
                            "ignored_confidence": 0,
                            "anchor": "bottom_center",
                            "best_ratio": 0.12,
                            "reason": "điểm chân nằm NGOÀI vùng — chọn cách xác định khác",
                        },
                    },
                },
            },
            {
                "id": "cam_off",
                "name": "Camera đang tắt",
                "source": "rtsp",
                "enabled": False,
                "test": False,
                "status": "off",
                "capture": {"connected": False, "stale": False, "error": "camera đang tắt", "source": "rtsp"},
                "inference": {"fps": 0.0, "ms": 0.0},
                "detections": [],
                "zones": [],
                "zone_states": {},
            }
        ],
        "mqtt": {"connected": False, "dry_run": True},
        "scenarios": [],
        "simulator": {"scale": 1.0, "zones": []},
        "detector": {"available": True, "device": "cpu", "name": "fake"},
        "webcams": [],
        "loopback": {"running": False, "available": False},
        "setup_completed": True,
        "platform": {"os": "test", "label": "test"},
        "warnings": [],
        "diagnostics": {
            "cameras": {
                "cam_off": {
                    "status": "off",
                    "enabled": False,
                    "connected": False,
                    "error": "camera đang tắt",
                    "capture_fps": 0.0,
                    "infer_fps": 0.0,
                    "inference_errors": 0,
                    "zones": {
                        "vung_cu": {"occupied": True, "count": 1, "pending_exit_s": None, "forced": False},
                    },
                },
                "cam_test": {
                    "enabled": True,
                    "connected": True,
                    "sampled_frame": True,
                    "capture_fps": 12.5,
                    "stream_fps": 15.0,
                    "read_ms": 7.0,
                    "frame_age_ms": 120.0,
                    "reconnects": 2,
                    "inference_ms": 21.0,
                    "infer_fps": 4.0,
                    "detect_fps_target": 4.0,
                    "infer_age_s": 0.2,
                    "inference_errors": 1,
                    "inference_errors_consecutive": 0,
                    "inference_last_error": None,
                    "error": None,
                    "hint": None,
                    "zones": {
                        "khuvuc_test": {
                            "occupied": True,
                            "count": 2,
                            "pending_exit_s": None,
                            "forced": False,
                            "since": "2026-01-01T10:00:00",
                            "anchor": "bottom_center",
                            "overlap_ratio": 0.5,
                            "analysis": {
                                "detections": 3,
                                "matched": 2,
                                "outside": 1,
                                "ignored_class": 0,
                                "ignored_confidence": 0,
                                "anchor": "bottom_center",
                                "best_ratio": 1.0,
                                "reason": "2 đối tượng trong vùng",
                            },
                        },
                        "vung_2": {
                            "occupied": False,
                            "count": 0,
                            "pending_exit_s": None,
                            "forced": False,
                            "since": None,
                            "anchor": "bottom_center",
                            "overlap_ratio": 0.5,
                            "analysis": {
                                "detections": 3,
                                "matched": 0,
                                "outside": 1,
                                "ignored_class": 0,
                                "ignored_confidence": 0,
                                "anchor": "bottom_center",
                                "best_ratio": 0.12,
                                "reason": "điểm chân nằm NGOÀI vùng — chọn cách xác định khác",
                            },
                        }
                    },
                }
            },
            "threads": {"control": True, "inference": {"cam_test": True}},
            "detector": {"name": "yolo11n.pt", "available": True, "device": "mps", "stats": {}, "errors_total": 1},
            "mqtt": {"connected": True, "queued": 0, "sent": 4, "received": 2, "dry_run": True},
            "scenarios": [{"id": "den_bep", "reason": "Vùng trống — không có bước nào đang chờ.", "pending": []}],
            "decisions": [
                {"ts": 0, "time": "2026-01-01T10:00:00", "kind": "command", "message": "Gửi “Bật đèn” tới nha/den/set"},
            ],
            "warnings": [],
        },
    }


class _Handler(BaseHTTPRequestHandler):
    """Phục vụ file tĩnh thật + API giả. Ghi lại các lần POST /zones."""

    saved_zones: list = []  # noqa: RUF012 - class-level recorder
    posts: list = []        # noqa: RUF012 - mọi POST (dùng cho test nút bấm)
    asset_version: str = "111"  # noqa: RUF012 - phiên bản app.js máy chủ đang phục vụ
    server_version = "ZoneUITest/1.0"

    def log_message(self, *args) -> None:  # noqa: A002 - giữ im lặng log của server
        return None

    def _send(self, body: bytes, ctype: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: dict, status: int = 200) -> None:
        self._send(json.dumps(payload).encode("utf-8"), "application/json", status)

    def do_GET(self) -> None:  # noqa: N802 - tên do http.server quy định
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send((STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif path.startswith("/static/"):
            target = STATIC / path[len("/static/") :]
            if target.is_file():
                ctype = "text/css" if target.suffix == ".css" else "application/javascript"
                self._send(target.read_bytes(), ctype)
            else:
                self._send(b"not found", "text/plain", 404)
        elif path == "/api/config":
            self._json(_config_payload())
        elif path == "/api/state":
            payload = _state_payload()
            payload["asset_version"] = type(self).asset_version
            self._json(payload)
        elif path in ("/api/state/public", "/api/events"):
            self._json({"events": [], "auth_enabled": False, "setup_completed": True})
        elif path == "/api/detector/classes":
            self._json({"classes": [{"id": 0, "name": "person"}], "info": {"available": True, "device": "cpu", "model": "fake"}})
        elif path.startswith("/stream/") or path.startswith("/snapshot/"):
            self._send(JPEG, "image/jpeg")
        else:
            self._send(b"not found", "text/plain", 404)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b"{}"
        path = self.path.split("?", 1)[0]
        type(self).posts.append(path)
        if path.startswith("/api/cameras/") and path.endswith("/zones"):
            data = json.loads(body or b"{}")
            type(self).saved_zones.append(data)
            self._json({"ok": True, "renamed": data.get("renames") or {}, "dropped_scenarios": []})
            return
        self._json({"ok": True})


@pytest.fixture(scope="module")
def ui_base_url():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, name="ui-test-server", daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


@pytest.fixture(scope="module")
def browser():
    if not BROWSERS_PATH.is_dir():
        pytest.skip(f"Chưa tải Chromium cho Playwright (thiếu {BROWSERS_PATH})")
    import os

    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(BROWSERS_PATH))
    with sync_playwright() as play:
        instance = play.chromium.launch()
        try:
            yield instance
        finally:
            instance.close()


@pytest.fixture()
def open_zone_tab(browser, ui_base_url):
    """Mở trang, chuyển sang tab “Vùng phát hiện”, trả (page, image_rect)."""

    def _open(dpr: int = 1):
        context = browser.new_context(device_scale_factor=dpr, viewport={"width": 1180, "height": 820})
        page = context.new_page()
        page.goto(ui_base_url + "/", wait_until="load")
        page.wait_for_function("() => typeof ZE !== 'undefined' && typeof loadZoneEditor === 'function'", timeout=15000)
        page.click("#tabs button[data-tab='zones']")
        page.wait_for_function("() => document.querySelector('#panel-zones').classList.contains('active')", timeout=10000)
        # Ảnh để vẽ chỉ được gán src khi mở tab — chờ tới khi nó tải xong
        # (nếu không, khung ảnh là 0×0 và mọi phép đổi toạ độ đều vô nghĩa).
        page.wait_for_function("() => document.querySelector('#zone-img').naturalWidth > 0", timeout=15000)
        page.evaluate("() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)))")
        return page, _image_rect(page)

    pages: list = []

    def wrapped(dpr: int = 1):
        page, rect = _open(dpr)
        pages.append(page)
        return page, rect

    yield wrapped
    for page in pages:
        try:
            page.context.close()
        except Exception:  # noqa: BLE001 - dọn dẹp không được che lỗi test
            pass


def _image_rect(page) -> dict:
    """Khung ảnh THẬT (CSS px) mà trình duyệt đang vẽ cho ``#zone-img``."""
    return page.evaluate(
        """() => {
            const r = document.querySelector('#zone-img').getBoundingClientRect();
            const c = document.querySelector('#zone-canvas').getBoundingClientRect();
            return {x: r.left, y: r.top, w: r.width, h: r.height,
                    canvas: {x: c.left, y: c.top, w: c.width, h: c.height}};
        }"""
    )


def _screen(rect: dict, point) -> tuple[float, float]:
    return rect["x"] + point[0] * rect["w"], rect["y"] + point[1] * rect["h"]


def _polygon(page, index: int = 0) -> list:
    return page.evaluate("(i) => ZE.zones[i].polygon", index)


def _select_zone(page, index: int) -> None:
    page.evaluate(
        """(i) => {
            ZE.active = i;
            ZE.drag = null;
            ZE.draft = [];
            ZE.hover = null;
            ZE.selectedVertex = -1;
            renderZoneSidebar(); renderZoneAttrs(); drawZoneCanvas();
        }""",
        index,
    )


# --------------------------------------------------------------------------- #
# 1. Kéo một ĐIỂM của đa giác phải rơi đúng dưới con trỏ, ở DPR 1 và DPR 2
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("dpr", [1, 2])
def test_vertex_drag_follows_pointer(open_zone_tab, dpr):
    page, rect = open_zone_tab(dpr)
    _select_zone(page, 1)  # đa giác
    starting = _polygon(page, 1)
    start = starting[2]
    sx, sy = _screen(rect, start)
    page.mouse.move(sx, sy)
    page.mouse.down()
    page.mouse.move(sx - 60, sy - 40, steps=6)
    page.mouse.up()
    after = _polygon(page, 1)
    moved = after[2]
    want_x = (sx - 60 - rect["x"]) / rect["w"]
    want_y = (sy - 40 - rect["y"]) / rect["h"]
    assert abs(moved[0] - want_x) < 0.015, f"lệch trục x: {moved[0]:.3f} vs {want_x:.3f}"
    assert abs(moved[1] - want_y) < 0.015, f"lệch trục y: {moved[1]:.3f} vs {want_y:.3f}"
    # Điểm khác phải đứng yên — nếu không, đây là "di chuyển cả vùng" chứ không
    # phải kéo một điểm (đúng lỗi người dùng báo).
    for index in (0, 1, 3, 4):
        assert after[index] == pytest.approx(starting[index], abs=1e-6), (
            f"kéo 1 điểm nhưng điểm {index} cũng chạy: {starting[index]} → {after[index]}")


# --------------------------------------------------------------------------- #
# 2. Kéo điểm CHỈ làm điểm đó chạy (2 cạnh dính vào nó xoay theo)
# --------------------------------------------------------------------------- #
def test_vertex_drag_leaves_other_vertices_alone(open_zone_tab):
    page, rect = open_zone_tab(2)
    _select_zone(page, 1)
    before = _polygon(page, 1)
    sx, sy = _screen(rect, before[2])
    page.mouse.move(sx, sy)
    page.mouse.down()
    page.mouse.move(sx + 30, sy + 20, steps=4)
    page.mouse.up()
    after = _polygon(page, 1)
    for index in (0, 1, 3, 4):
        assert after[index] == pytest.approx(before[index], abs=1e-6), f"điểm {index} bị xê dịch"


# --------------------------------------------------------------------------- #
# 3. Hình chữ nhật: kéo GÓC đổi kích thước, vẫn 4 điểm và vẫn vuông góc
# --------------------------------------------------------------------------- #
def test_rect_corner_drag_keeps_rectangle(open_zone_tab):
    page, rect = open_zone_tab(2)
    _select_zone(page, 0)  # hình chữ nhật
    sx, sy = _screen(rect, [0.10, 0.20])
    page.mouse.move(sx, sy)
    page.mouse.down()
    page.mouse.move(sx - 40, sy - 30, steps=5)
    page.mouse.up()
    polygon = _polygon(page, 0)
    assert len(polygon) == 4
    assert polygon[0][0] == pytest.approx(polygon[3][0], abs=1e-6)
    assert polygon[0][1] == pytest.approx(polygon[1][1], abs=1e-6)
    assert polygon[2][0] == pytest.approx(polygon[1][0], abs=1e-6)
    assert polygon[2][1] == pytest.approx(polygon[3][1], abs=1e-6)
    assert polygon[0][0] < 0.10 and polygon[0][1] < 0.20, "góc phải chạy về phía con trỏ"


# --------------------------------------------------------------------------- #
# 4. Rê chuột lên ĐIỂM phải đổi con trỏ + hiện nhãn nổi (phản hồi trực quan)
# --------------------------------------------------------------------------- #
def test_hover_vertex_shows_cursor_and_tip(open_zone_tab):
    page, rect = open_zone_tab(2)
    _select_zone(page, 1)
    sx, sy = _screen(rect, _polygon(page, 1)[0])
    page.mouse.move(sx, sy)
    page.mouse.move(sx + 1, sy + 1)
    hover = page.evaluate("() => ZE.hover")
    assert hover and hover.get("kind") == "vertex", f"không nhận ra điểm: {hover}"
    cursor = page.eval_on_selector("#zone-canvas", "el => el.style.cursor")
    assert "grab" in cursor
    tip = page.eval_on_selector("#zone-tip", "el => ({shown: el.classList.contains('show'), text: el.textContent})")
    assert tip["shown"] and "chỉnh điểm" in tip["text"]


# --------------------------------------------------------------------------- #
# 5. Bấm vào CHẤM GIỮA CẠNH của đa giác → thêm đúng 1 điểm nằm trên cạnh đó
# --------------------------------------------------------------------------- #
def test_edge_click_inserts_one_vertex_on_the_edge(open_zone_tab):
    page, rect = open_zone_tab(1)
    _select_zone(page, 1)
    polygon = _polygon(page, 1)
    midpoint = (
        (polygon[0][0] + polygon[1][0]) / 2,
        (polygon[0][1] + polygon[1][1]) / 2,
    )
    sx, sy = _screen(rect, midpoint)
    page.mouse.move(sx, sy)
    page.mouse.down()
    page.mouse.up()
    after = _polygon(page, 1)
    assert len(after) == len(polygon) + 1
    assert after[1] == pytest.approx(list(midpoint), abs=0.02)


# --------------------------------------------------------------------------- #
# 6. Bấm đúp để đóng vùng KHÔNG được sinh điểm trùng
# --------------------------------------------------------------------------- #
def test_double_click_finish_has_no_duplicate_points(open_zone_tab):
    page, rect = open_zone_tab(1)
    page.click("#zone-new")
    page.evaluate("() => { ZE.mode = 'poly'; }")
    points = [[0.20, 0.20], [0.50, 0.22], [0.45, 0.55]]
    for point in points:
        px, py = _screen(rect, point)
        page.mouse.click(px, py)
    last_x, last_y = _screen(rect, points[-1])
    page.mouse.dblclick(last_x, last_y)
    zones = page.evaluate("() => ZE.zones.map((z) => z.polygon)")
    created = zones[-1]
    assert len(created) == 3, f"bấm đúp làm sinh thêm điểm: {created}"
    for index in range(len(created)):
        for other in range(index + 1, len(created)):
            distance = abs(created[index][0] - created[other][0]) + abs(created[index][1] - created[other][1])
            assert distance > 0.01, f"hai điểm trùng nhau: {created}"


# --------------------------------------------------------------------------- #
# 7. Đang sửa mà đổi tab / đổi camera phải HỎI trước khi bỏ thay đổi
# --------------------------------------------------------------------------- #
def test_leaving_with_unsaved_edits_asks_before_discarding(open_zone_tab):
    page, rect = open_zone_tab(1)
    _select_zone(page, 0)
    sx, sy = _screen(rect, [0.60, 0.80])
    page.mouse.move(sx, sy)
    page.mouse.down()
    page.mouse.move(sx + 20, sy + 15, steps=3)
    page.mouse.up()
    assert page.evaluate("() => ZE.dirty") is True

    seen: list = []

    def _handle(dialog):
        seen.append(dialog.message)
        dialog.dismiss()

    page.on("dialog", _handle)
    page.click("#tabs button[data-tab='live']")
    page.wait_for_timeout(200)
    assert seen, "không hỏi gì trước khi bỏ thay đổi vùng"
    assert "chưa lưu" in seen[0].lower() or "lưu" in seen[0].lower()
    # Người dùng bấm Cancel → vẫn ở tab Vùng phát hiện, dữ liệu còn nguyên
    assert page.evaluate("() => document.querySelector('#panel-zones').classList.contains('active')")
    assert page.evaluate("() => ZE.dirty") is True


# --------------------------------------------------------------------------- #
# 8. Lưu vùng: dữ liệu gửi lên là toạ độ chuẩn hoá 0..1 + giữ nguyên `shape`
# --------------------------------------------------------------------------- #
def test_save_sends_normalised_polygon_and_shape(open_zone_tab):
    _Handler.saved_zones.clear()
    page, rect = open_zone_tab(1)
    _select_zone(page, 1)
    sx, sy = _screen(rect, _polygon(page, 1)[1])
    page.mouse.move(sx, sy)
    page.mouse.down()
    page.mouse.move(sx + 10, sy + 10, steps=3)
    page.mouse.up()
    page.click("#zone-save")
    page.wait_for_timeout(400)
    assert _Handler.saved_zones, "không gửi dữ liệu vùng lên server"
    payload = _Handler.saved_zones[-1]
    zones = payload["zones"]
    assert [zone["shape"] for zone in zones] == ["rect", "polygon"]
    for zone in zones:
        for x, y in zone["polygon"]:
            assert 0.0 <= x <= 1.0 and 0.0 <= y <= 1.0


# --------------------------------------------------------------------------- #
# 9. Mở tab Vùng phát hiện phải ĐÓNG BĂNG khung hình (vẽ trên ảnh động rất khó)
# --------------------------------------------------------------------------- #
def test_zone_tab_freezes_the_frame_for_editing(open_zone_tab):
    page, _rect = open_zone_tab(1)
    page.wait_for_function("() => ZE.frozen === true", timeout=8000)
    src = page.eval_on_selector("#zone-img", "el => el.getAttribute('src')")
    assert src.startswith("data:image/"), f"chưa đóng băng khung hình: {src[:40]}"
    state = page.eval_on_selector("#zone-freeze-state", "el => el.textContent")
    assert "ảnh tĩnh" in state


# --------------------------------------------------------------------------- #
# 10. Bảng chẩn đoán phải hiện số liệu thật (không phải chỗ trống)
# --------------------------------------------------------------------------- #
def test_diagnostics_panel_shows_live_numbers(open_zone_tab):
    page, _rect = open_zone_tab(1)
    page.click("#tabs button[data-tab='live']")
    page.wait_for_timeout(300)
    text = page.eval_on_selector("#diag-cameras", "el => el.textContent")
    assert "cam_test" in text
    assert "12.5" in text          # FPS camera
    assert "120" in text           # tuổi khung hình (ms)
    assert "đang nhận hình" in text
    decisions = page.eval_on_selector("#diag-decisions", "el => el.textContent")
    assert "nha/den/set" in decisions


# --------------------------------------------------------------------------- #
# 11. Vùng tự cắt nhau (hình số 8) phải bị CHẶN khi lưu, kèm lời giải thích
# --------------------------------------------------------------------------- #
def test_self_intersecting_polygon_is_rejected_with_reason(open_zone_tab):
    from http.server import BaseHTTPRequestHandler  # noqa: PLC0415

    handler = next(
        cls for cls in BaseHTTPRequestHandler.__subclasses__() if cls.__name__ == "_Handler"
    )
    handler.saved_zones.clear()
    page, _rect = open_zone_tab(1)
    page.evaluate(
        """() => {
            ZE.zones[1].polygon = [[0.10, 0.10], [0.70, 0.70], [0.70, 0.10], [0.20, 0.75]];
            ZE.zones[1].shape = 'polygon';
            renderZoneSidebar(); renderZoneAttrs(); drawZoneCanvas();
        }"""
    )
    page.click("#zone-save")
    page.wait_for_timeout(400)
    assert not handler.saved_zones, "vùng tự cắt nhau vẫn được gửi lên server"
    toasts = page.eval_on_selector("#toasts", "el => el.textContent")
    assert "tự cắt" in toasts.lower(), toasts


# --------------------------------------------------------------------------- #
# 11. Cách xác định "trong vùng": đủ 5 chế độ + thanh % + lưu đúng giá trị
# --------------------------------------------------------------------------- #
def test_anchor_modes_cover_inside_and_percentage(open_zone_tab):
    page, _rect = open_zone_tab(1)
    options = page.eval_on_selector_all(
        "#zone-anchor option", "els => els.map(el => el.value)"
    )
    for value in ("bottom_center", "center", "any_overlap", "fully_inside", "inside_ratio"):
        assert value in options, f"thiếu chế độ xác định '{value}': {options}"
    # Thanh % chỉ hiện khi chọn chế độ "% đối tượng nằm trong vùng"
    assert page.eval_on_selector("#zone-ratio-field", "el => el.hidden") is True
    page.select_option("#zone-anchor", "inside_ratio")
    page.wait_for_timeout(150)
    assert page.eval_on_selector("#zone-ratio-field", "el => el.hidden") is False


def test_anchor_and_ratio_are_sent_to_server(open_zone_tab):
    _Handler.saved_zones.clear()
    page, _rect = open_zone_tab(1)
    _select_zone(page, 0)
    page.select_option("#zone-anchor", "fully_inside")
    page.wait_for_timeout(120)
    page.click("#zone-save")
    page.wait_for_timeout(400)
    assert _Handler.saved_zones, "không gửi dữ liệu vùng lên server"
    first = _Handler.saved_zones[-1]["zones"][0]
    assert first["anchor"] == "fully_inside"

    page.select_option("#zone-anchor", "inside_ratio")
    page.eval_on_selector(
        "#zone-ratio", "el => { el.value = '70'; el.dispatchEvent(new Event('input')); }"
    )
    page.wait_for_timeout(150)
    page.click("#zone-save")
    page.wait_for_timeout(400)
    first = _Handler.saved_zones[-1]["zones"][0]
    assert first["anchor"] == "inside_ratio"
    assert abs(float(first["overlap_ratio"]) - 0.7) < 0.001


# --------------------------------------------------------------------------- #
# 12. Trình sửa vùng phải GIẢI THÍCH vì sao vùng đang trống
# --------------------------------------------------------------------------- #
def test_zone_editor_explains_why_a_zone_is_empty(open_zone_tab):
    page, _rect = open_zone_tab(1)
    _select_zone(page, 1)          # vung_2: mô phỏng "điểm chân nằm NGOÀI vùng"
    page.wait_for_timeout(300)
    text = page.eval_on_selector("#zone-analysis", "el => el.textContent")
    assert "Phân tích trực tiếp" in text
    assert "NGOÀI vùng" in text
    live = page.eval_on_selector("#live-zones", "el => el.textContent")
    assert "ngoài vùng" in live


# --------------------------------------------------------------------------- #
# 13. Nút/switch trong tab Cài đặt phải bấm ăn (lỗi: dựng lại DOM 8 lần/giây
#     làm mousedown và mouseup rơi vào 2 node khác nhau → không có sự kiện click)
# --------------------------------------------------------------------------- #
def _open_settings(browser, ui_base_url):
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    page = context.new_page()
    page.goto(ui_base_url + "/", wait_until="domcontentloaded")
    page.wait_for_function("() => window.__VO__ && window.__VO__.state", timeout=10000)
    page.click("button[data-tab='settings']")
    # input thật bị CSS ẩn (chỉ hiện .track) nên chờ ở trạng thái "attached"
    page.wait_for_selector("[data-cam-enabled]", state="attached", timeout=5000)
    page.wait_for_timeout(300)
    return page


def test_settings_panel_is_not_rebuilt_when_nothing_changes(browser, ui_base_url):
    page = _open_settings(browser, ui_base_url)
    try:
        # Đánh dấu TẤT CẢ node hiện tại; nếu panel bị dựng lại, dấu sẽ biến mất.
        # (trước đây chỉ đánh dấu 1 node nên bài test ngầm giả định chỉ có 1 camera)
        page.eval_on_selector_all("[data-cam-enabled]", "els => els.forEach((el) => { el.__mark = 'giữ-nguyên'; })")
        page.evaluate("() => { for (let i = 0; i < 8; i += 1) window.__VO__.renderAll(); }")
        marks = page.eval_on_selector_all(
            "[data-cam-enabled]", "els => els.map(el => el.__mark || null)"
        )
        assert marks and all(m == "giữ-nguyên" for m in marks), \
            f"panel Cài đặt bị dựng lại dù dữ liệu không đổi: {marks}"
    finally:
        page.context.close()


def test_real_click_on_camera_switch_lands_while_state_updates(browser, ui_base_url):
    _Handler.posts.clear()
    page = _open_settings(browser, ui_base_url)
    try:
        # Giả lập trạng thái đẩy về liên tục như WebSocket thật (8 Hz, ở đây 30 ms).
        page.evaluate(
            "() => { window.__t = setInterval(() => window.__VO__.renderAll(), 30); }"
        )
        box = page.eval_on_selector(
            "[data-cam-enabled='cam_test']",
            "el => { const r = el.closest('label.switch').getBoundingClientRect();"
            " return {x: r.x + r.width / 2, y: r.y + r.height / 2}; }",
        )
        page.mouse.move(box["x"], box["y"])
        page.mouse.down()
        page.wait_for_timeout(200)      # giữ chuột như người thật
        page.mouse.up()
        page.wait_for_timeout(600)
        page.evaluate("() => clearInterval(window.__t)")
        assert any(p.endswith("/cam_test/enabled") for p in _Handler.posts), \
            f"bấm công tắc camera không gửi được yêu cầu nào: {_Handler.posts}"
    finally:
        page.context.close()


def test_camera_card_shows_fix_hint_and_connection_check_button(browser, ui_base_url):
    page = _open_settings(browser, ui_base_url)
    try:
        assert page.eval_on_selector_all("[data-cam-check]", "els => els.length") >= 1
        # Bấm "Kiểm tra" → phải gọi endpoint và hiện kết quả
        if not page.eval_on_selector_all("#settings-cameras .zmeta.fix", "els => els.length"):
            page.click("[data-cam-check]")
            page.wait_for_timeout(500)
        text = page.eval_on_selector("#settings-cameras", "el => el.textContent")
        assert "Kiểm tra" in text
    finally:
        page.context.close()

def test_stale_tab_gets_a_reload_banner(browser, ui_base_url):
    """Tab mở từ hôm trước phải được nhắc nạp lại khi máy chủ đã có app.js mới.

    Người dùng thật đã gặp: máy chủ phục vụ app.js mới nhưng tab giữ bản cũ
    trong bộ nhớ ⇒ nút mới không hề có trong DOM ⇒ "bấm không ăn".
    """
    _Handler.asset_version = "111"
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    page = context.new_page()
    try:
        page.goto(ui_base_url + "/", wait_until="domcontentloaded")
        page.wait_for_function("() => window.__VO__ && window.__VO__.state", timeout=10000)
        page.wait_for_timeout(200)
        assert page.eval_on_selector_all("#ui-stale-banner", "els => els.length") == 0

        # Máy chủ "vừa được cập nhật"
        _Handler.asset_version = "222"
        page.evaluate("() => window.__VO__.setState(Object.assign({}, window.__VO__.state, {asset_version: '222'}))")
        page.wait_for_selector("#ui-stale-banner", timeout=3000)
        text = page.inner_text("#ui-stale-banner")
        assert "bản giao diện mới" in text
        assert page.eval_on_selector_all("#ui-stale-reload", "els => els.length") == 1
    finally:
        _Handler.asset_version = "111"
        page.context.close()

def test_disabled_camera_is_not_shown_as_lost(browser, ui_base_url):
    """Camera chủ động tắt phải hiện "đang tắt" (xám), không phải "mất hình" (đỏ).

    Người dùng đã báo: tắt camera trong Cài đặt thì bảng chẩn đoán lại hiện
    "mất hình" như thể có sự cố. Ngoài ra trạng thái vùng cũ không được khẳng
    định "có người" nữa.
    """
    context = browser.new_context(viewport={"width": 1366, "height": 900})
    page = context.new_page()
    try:
        page.goto(ui_base_url + "/", wait_until="domcontentloaded")
        page.wait_for_function("() => window.__VO__ && window.__VO__.state", timeout=10000)
        page.click("button[data-tab='logs']")   # tab có bảng chẩn đoán
        page.wait_for_timeout(400)

        card = page.eval_on_selector(
            "#diag-cameras .diag-cam:nth-child(1)",
            "el => ({cls: el.className, text: el.innerText})",
        )
        assert "đang tắt" in card["text"], card["text"]
        assert "mất hình" not in card["text"], card["text"]
        assert "bad" not in card["cls"], card["cls"]
        # vùng của camera đang tắt: không khẳng định "có người"
        assert "không đánh giá" in card["text"], card["text"]
        assert "giữ trạng thái cũ" in card["text"], card["text"]

        # tab Cài đặt: chấm xám + "tắt theo ý người dùng", không có ⚠
        page.click("button[data-tab='settings']")
        page.wait_for_selector("[data-cam-enabled='cam_off']", state="attached", timeout=5000)
        page.wait_for_timeout(300)
        text = page.inner_text("#settings-cameras")
        assert "tắt theo ý người dùng" in text
        assert "⚠ camera đang tắt" not in text
        dots = page.eval_on_selector_all(
            "#settings-cameras .zone-item .dot", "els => els.map(e => e.className)"
        )
        assert any("off" in cls for cls in dots), dots

        # Tất cả camera đều tắt ⇒ nhãn đầu trang là "Camera đang tắt", không phải
        # "Camera mất" (báo động sai).
        page.evaluate(
            "() => { const st = JSON.parse(JSON.stringify(window.__VO__.state));"
            " st.cameras.forEach((c) => { c.enabled = false; c.status = 'off';"
            " c.capture = Object.assign({}, c.capture, {connected: false}); });"
            " window.__VO__.setState(st); }"
        )
        page.wait_for_timeout(200)
        assert (page.inner_text("#lbl-camera")).strip() == "Camera đang tắt"
    finally:
        page.context.close()
