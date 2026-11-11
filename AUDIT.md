# AUDIT — `vision_automation` (BƯỚC 1: kiểm tra chỉ-đọc, trước khi sửa)

**Ngày:** 2026-11-12 · **Nhánh:** `main` · **Baseline:** 183 test pass (trước audit)
**Phạm vi:** toàn bộ `app/` + `app/web/static/` (≈10.300 dòng).
**Máy kiểm chứng:** macOS 15.7.9 / Apple Silicon M1 (dev) — production là Windows.

## Phương pháp

1. **Đọc code** từng file, ghi lại `đường/dẫn: dòng` cho mọi kết luận.
2. **Test tự động**: pytest cho phần logic thuần (fake clock, replay JSONL) và
   Playwright (headless Chromium) cho trình sửa vùng — chạy ở `deviceScaleFactor`
   1 **và** 2 để bắt lỗi toạ độ/DPR.
3. Ba vòng đọc song song độc lập cho phần pipeline (toạ độ, state machine,
   MQTT/thread) rồi **đọc lại tận nơi** các kết luận nghiêm trọng trước khi ghi vào đây.

### Quy ước cột “Trạng thái”

| Nhãn | Nghĩa |
|---|---|
| `xác minh` | Đã xác minh bằng đọc code +/hoặc test tái hiện được **trên máy này**. |
| `cần kiểm chứng` | Suy ra từ code nhưng **chưa** đo được trên phần cứng/RTSP/broker thật. Không coi là đã chắc chắn. |
| `đã sửa` / `chưa sửa` | Cập nhật ở BƯỚC 4. |

### Mức độ

`Blocker` = hỏng tính năng chính / gây hiểu sai nghiêm trọng · `High` = sai hành vi
người dùng thấy rõ · `Med` = sai trong tình huống hẹp · `Low` = vệ sinh/dễ nhầm.

---

## 0. Tóm tắt

| Mức | Số lượng | Ghi chú |
|---|---|---|
| Blocker | 3 | 2 lỗi toạ độ trình sửa vùng + 1 lỗi “suy luận lỗi ⇒ coi như vắng” |
| High | 8 | mất thay đổi chưa lưu, dry-run rò rỉ, van an toàn chết, rò tài nguyên, race |
| Med | 12 | nhóm lỗi tình huống hẹp (cooldown, lịch qua đêm, watcher, snapshot…) |
| Low | 9 | cấu hình chết, đếm số liệu lệch, nhánh không dùng tới |

**5 việc nghiêm trọng nhất (theo thứ tự ưu tiên sửa):**

1. **`A1` + `A2` — toạ độ trình sửa vùng sai** ⇒ đúng triệu chứng *“kéo thả điểm
   hoạt động kém”*: rê chuột lên điểm không nhận ra điểm, kéo thành ra di chuyển cả
   vùng, và vùng đã lưu bị lệch so với hình.
2. **`B1` — suy luận lỗi bị coi là “không có ai”** ⇒ đèn có thể TẮT khi người vẫn
   đang ở trong phòng.
3. **`B2` — bộ đếm chờ-rời-vùng không được xoá khi người hiện lại** ⇒ vùng báo
   trống sớm ⇒ giảm sáng/tắt đèn sớm.
4. **`A4` — thay đổi vùng chưa lưu bị bỏ im lặng** khi đổi tab/đổi camera.
5. **`B3` + `B4` + `B5`** — dry-run có thể rò lệnh thật sau khi broker nối lại;
   “tạm dừng khi người dùng chỉnh tay” (`manual_override`) **chưa được nối** nên
   không bao giờ chạy; mỗi lần camera treo lại rò một `cv2.VideoCapture`.

---

## 1. BẢNG PHÁT HIỆN — A. Trình sửa vùng (Web UI)

| ID | Vị trí | Mức | Triệu chứng người dùng thấy | Tái hiện | Nguyên nhân gốc | Cách sửa dự kiến | Trạng thái |
|---|---|---|---|---|---|---|---|
| **A1** | `app/web/static/app.js:1129-1143` (`nearestVertex`), `app/web/static/app.js:1166-1195` (`hitEdge`) | **Blocker** | Rê chuột lên điểm/cạnh gần như **không** nhận ra: con trỏ không đổi, điểm không sáng, kéo vào điểm thì lại **di chuyển cả vùng**. | `tests/test_zone_editor_ui.py::test_vertex_drag_follows_pointer[1]` | Trộn hai hệ toạ độ: `px,py` lấy từ `event.clientX/clientY` (**toạ độ màn hình**) nhưng so với `image.x + vertex[0]*image.w` (**toạ độ trong canvas**): `const dist = Math.hypot(vx - px, vy - py)`. Sai lệch đúng bằng `rect.left/rect.top` của canvas (hàng trăm px) ⇒ bán kính bắt 22 px vô nghĩa. | Quy đổi con trỏ về hệ canvas một lần (`clientPoint → canvasPoint`) rồi dùng chung cho `nearestVertex`/`hitEdge`/`pickVertex`. | xác minh |
| **A2** | `app/web/static/app.js:855-860` (`imageBox`) + CSS `app/web/static/style.css:214` | **Blocker** | Vùng vẽ ra **không nằm dưới con trỏ**; vùng đã lưu bị lệch/lớn hơn thực tế. Lệch càng rõ khi cửa sổ rộng hoặc luồng hình nhỏ (640×480). | `tests/test_zone_editor_ui.py::test_rect_corner_drag_keeps_rectangle` | `imageBox` tự đoán khung ảnh: `scale = Math.min(rect.width/naturalW, rect.height/naturalH)` → **phóng to** ảnh, nhưng CSS `max-width/max-height: 100%` **không bao giờ phóng to** ảnh nhỏ. Ví dụ panel 1100 px, ảnh 640×480: code tưởng ảnh rộng ~725 px, thực tế 640 px ⇒ lệch tới ~85 px. | Lấy khung ảnh **thật** từ `img.getBoundingClientRect()` (đã là CSS px, tự đúng với `object-fit: contain`, flex và mọi DPR); chỉ dùng công thức letterbox khi ảnh chưa tải. | xác minh |
| **A3** | `app/web/static/app.js:713-715`; HTML `app/web/static/index.html:124-127` | High | Vẽ vùng trên **hình đang chạy** ⇒ người/đồ vật di chuyển trong lúc kéo, khó canh điểm. | Mở tab “Vùng phát hiện” khi camera đang có người | `#zone-img` trỏ `/stream/<id>.mjpg` (luồng trực tiếp); không có chế độ “đóng băng khung hình”. | Thêm nút **“Chụp khung hình”** + chế độ đóng băng: trình sửa dùng `/snapshot/<id>.jpg` (ảnh tĩnh), có nút “Xem trực tiếp”. | chưa sửa |
| **A4** | `app/web/static/app.js:126-138` (`switchTab`), `app/web/static/app.js:694-724` (`loadZoneEditor`), `app/web/static/app.js:2412-2418` (đổi camera) | High | Đang sửa vùng, **đổi tab hoặc đổi camera là mất sạch** thay đổi vừa kéo — không hỏi, không cảnh báo. | `tests/test_zone_editor_ui.py::test_leaving_with_unsaved_edits_asks_before_discarding` | `loadZoneEditor()` luôn `ZE.zones = clone(cam.zones)` và `ZE.dirty = false`; không chỗ nào kiểm tra `ZE.dirty` trước khi ghi đè. | Chặn bằng `confirm()` khi `ZE.dirty` (hỏi “Lưu / Bỏ / Ở lại”), và hiện dấu • trên tab khi chưa lưu. | chưa sửa |
| **A5** | `app/web/static/app.js:1435-1547` (`pointerdown`), `app/web/static/app.js:1619-1622` (`dblclick`) | High | Bấm để **thêm điểm** lại thành kéo điểm (rung tay 1–2 px là điểm chạy); bấm đúp để đóng vùng đôi khi sinh điểm thừa. | `tests/test_zone_editor_ui.py::test_double_click_finish_has_no_duplicate_points` | Mọi thao tác bắt đầu ngay ở `pointerdown`, **không có ngưỡng phân biệt click và kéo** (yêu cầu 4 px); `dblclick` đến **sau** hai `pointerdown` đã xử lý xong. | Thêm ngưỡng `DRAG_THRESHOLD_PX = 4`: dưới ngưỡng ⇒ coi là click (thêm/chọn điểm), trên ngưỡng ⇒ kéo; chặn sinh điểm khi phát hiện cụm bấm đúp. | chưa sửa |
| **A6** | `app/web/static/app.js:1629-1639` (phím Delete), `app/web/static/app.js:1602` (tạo HCN) | Med | Xoá điểm có thể làm vùng còn 2 điểm (không vẽ được); kéo tạo hình chữ nhật quá dẹt thì **im lặng không tạo gì**. | Xoá 2 lần liên tiếp trên vùng 4 điểm | `draft.splice(...)` không kiểm tra số điểm tối thiểu; tạo HCN yêu cầu `> 0.02` cả hai trục mà không báo lý do. | Chặn xoá khi còn 3 điểm (kèm toast giải thích); tạo HCN tối thiểu thì nắn thành HCN 1×1 px + toast, thay vì im lặng. | chưa sửa |
| **A7** | `app/web/static/app.js:969-1111` (`drawZoneCanvas`) | Med | Vùng tự cắt nhau (hình số 8), điểm trùng nhau, vùng quá nhỏ vẫn lưu được ⇒ kết quả nhận diện khó hiểu. | Vẽ 4 điểm theo hình số 8 rồi Lưu | Không có kiểm tra tự-cắt/trùng/diện tích ở **cả** client và server (`app/config.py` `ZoneConfig` chỉ kiểm tra ≥3 điểm và 0..1). | Thêm hàm `polygonSelfIntersects` + kiểm tra trùng/ngưỡng diện tích ở UI và validator phía server (thông điệp tiếng Việt). | chưa sửa |
| **A8** | `app/web/static/app.js:1730-1778` (`saveZones`) | — | **Đạt:** chặn lưu khi <3 điểm, hiện lỗi server bằng tiếng Việt, xử lý 409 khi vùng đang bị kịch bản dùng (hỏi xoá kịch bản). | `tests/test_web.py::test_zone_save_blocks_on_scenario_orphan_with_clear_error` | — | — | xác minh |
| **A9** | `app/web/static/app.js:1330-1348`, `app/web/static/app.js:1640-1646` | — | **Đạt:** có thanh báo “đang vẽ vùng mới”, Esc huỷ, Undo/Redo (`app.js:726-748`), Delete xoá điểm đã chọn. | `tests/test_zone_editor_ui.py::test_double_click_finish_has_no_duplicate_points` | — | — | xác minh |
| **A10** | `app/web/static/app.js:1270-1327` | Low | Nhãn nổi/con trỏ chỉ hiện khi trúng điểm/cạnh ⇒ **phụ thuộc A1**; khi A1 sai thì cả phản hồi trực quan cũng mất. | `tests/test_zone_editor_ui.py::test_hover_vertex_shows_cursor_and_tip` | Cùng gốc với A1. | Sửa A1 là hết; thêm test hồi quy cho con trỏ + nhãn. | chưa sửa |

---

## 2. BẢNG PHÁT HIỆN — B. Pipeline: suy luận → vùng → kịch bản → MQTT

### B.1 Luồng dữ liệu & toạ độ (B1 trong báo cáo vòng đọc)

| ID | Vị trí | Mức | Triệu chứng | Nguyên nhân gốc | Cách sửa dự kiến | Trạng thái |
|---|---|---|---|---|---|---|
| **B1** | `app/runtime.py:754-764` | **Blocker** | Suy luận lỗi (model treo/lỗi CUDA/hết RAM) ⇒ `detections = []` được coi là **khung hình thật không có ai** ⇒ sau `exit_grace_s` vùng báo trống ⇒ 30 s sau giảm sáng, 120 s sau **TẮT ĐÈN khi người vẫn ở trong phòng**. `NullDetector.detect` (`app/detector.py:99-101`) cũng luôn trả `[]`. | `except Exception: detections = []` rồi **vẫn** gọi `_process_detections(...)`; không có trạng thái “sức khoẻ detector”. | Khi suy luận lỗi: **bỏ qua** cập nhật vùng (giữ trạng thái cũ), đếm lỗi, phát event `error` + hiện trong bảng chẩn đoán. Coi `None` = “không có kết quả”, khác `[]` = “0 đối tượng”. | xác minh |
| **B2** | `app/zones.py:245-264` | High | Vùng báo trống **sớm hơn** `exit_grace_s`: người đi ra đi vào (nhận diện chập chờn) làm đèn nháy/tắt non. | Khi `votes < frames` lần đầu, code đặt `_vacant_candidate_since`; nếu người hiện lại (`votes >= frames`) thì **nhánh đó không chạy** nên mốc thời gian **không được xoá** (`app/zones.py:264` `return self.occupied, False`). Lần hụt sau, `now - mốc` đã > grace ⇒ trống ngay. | Xoá `_vacant_candidate_since = None` mỗi khi `votes >= frames`. | xác minh |
| **B3** | `app/runtime.py:755`, `app/detector.py:215`, `app/zones.py:443-449` | Med | Kéo thanh “độ tin cậy tối thiểu” của vùng **xuống thấp hơn** mức chung: **không có tác dụng gì**. | Detector đã lọc bằng `conf=cfg.min_confidence` **trước khi** vùng nhìn thấy (`detect(frame)` không truyền `confidence`), nên box yếu không bao giờ tới được vùng. | Truyền `confidence=min(các ngưỡng vùng, ngưỡng chung)` xuống `detect()`, rồi lọc lại theo từng vùng. UI hiện cảnh báo khi ngưỡng vùng < ngưỡng chung. | xác minh |
| **B4** | `app/zones.py:356-361` | Med | Vùng bị **tắt** giữ nguyên `occupied/count` cũ mà `updated_at` vẫn được làm mới ⇒ UI hiện trạng thái “tươi” nhưng là dữ liệu chết; bật lại thì cửa sổ bỏ phiếu cũ quyết định ngay. | `if not zone.enabled: state.updated_at = now; continue` — không `tracker.reset()`, không `occupied=None`. | Khi vùng bị tắt: `tracker.reset()` + `occupied=None` (unknown). | xác minh |
| **B5** | `app/runtime.py:784-808`, `app/capture.py:360-365` | Low-Med | Sau khi suy luận chậm/trễ nhịp, một khung hình **cũ vài giây** vẫn được xử lý như “bây giờ” ⇒ lệnh MQTT trễ so với thực tế. | `new_frame()` chỉ so `seq`; `_process_detections` không kiểm tra `packet.age()`; `last_infer_at = time.time()`. | Bỏ qua khung cũ hơn `~2/detect_fps`; dùng `packet.wall_time` cho mốc thời gian. | xác minh |
| **B6** | `app/detection.py:63-70`, `app/scenarios.py:203` | Low | Không truy được lệnh MQTT về **khung hình nào** đã sinh ra nó (`context.timestamp` là giờ gửi, không phải giờ chụp). | `Detection`/`ZoneState` không mang `seq`/`wall_time`. | Thêm `frame_seq`, `frame_wall_time` vào `ZoneState` + `context` của lệnh. | xác minh |

### B.2 State machine kịch bản

| ID | Vị trí | Mức | Triệu chứng | Nguyên nhân gốc | Cách sửa dự kiến | Trạng thái |
|---|---|---|---|---|---|---|
| **B7** | `app/scenarios.py:372` + `app/scenarios.py:387` + `app/scenarios.py:388-390` | Med | Người rời vùng rồi quay lại rồi rời đi **lần 2 trong `cooldown_s`** ⇒ **không có timeline giảm sáng/tắt đèn**; đèn ở mãi trạng thái ON. | `_last_fire[key]` được đóng dấu lúc **bắt đầu** trigger; khoá `f"{spec.type}:{spec.count}:{spec.seconds}:{classes}"` giống nhau cho mọi lần “vắng”; bị cooldown từ chối ⇒ bỏ luôn cả timeline, **không có hẹn lại**. (Mặc định `exit_grace_s=3` > `cooldown_s=2` che lỗi này.) | Với `type == "vacant"`: hoặc miễn cooldown, hoặc hẹn lại đúng `cooldown_s` rồi chạy (deferred retry). | xác minh |
| **B8** | `app/config.py:161-171` | Med | Lịch hoạt động qua đêm + danh sách thứ bị **sai ngày**: `from: 22:00, to: 06:00, days: [0]` (Thứ 2) **không** hoạt động lúc 01:00 Thứ 3. | `if self.days is not None and weekday not in self.days: return False` chạy **trước** phép so giờ; với khung qua đêm, “ngày sở hữu” là ngày hôm trước. | Khi `start > end` và `now <= end`: kiểm tra `days` bằng `(weekday - 1) % 7`. | xác minh |
| **B9** | `app/config.py:166-167` | Low | `from == to` được hiểu là **cả ngày** (dễ tưởng là “không bao giờ”). | Nhánh `if start == end: return True` dùng làm mốc “cả ngày” nhưng không có kiểm tra/cảnh báo. | Validator: từ chối `from == to`, hoặc thêm cờ `all_day` tường minh. | xác minh |
| **B10** | `app/scenarios.py:273-280` | Low | `count_le: 0` bắn **ngay khi khởi động** trong phòng trống (trái với nguyên tắc “không bắn OFF lúc boot”). | Nhánh “trạng thái ban đầu là vắng” chỉ `clear_timers()` rồi **rơi xuống** `_check_level_triggers(now)`. | `return` sớm ở nhánh đó (không chạy trigger mức cho tới lần chuyển trạng thái đầu tiên). | xác minh |
| **B11** | `app/scenarios.py:244-245`, `425`, `432` | Med | `count_ge`/`count_le`/`class_present` dùng **số đếm thô của một khung** (không bỏ phiếu K-of-N) ⇒ nhận diện nhấp nháy là bắn lệnh. | `self._count = state.count` lấy số của khung hiện tại; chỉ có chặn theo cạnh (`_count_flags`) + cooldown. | Bỏ phiếu K-of-N cho `class_counts` (hoặc chỉ chạy khi `occupied` đã xác nhận) trước khi so ngưỡng. | xác minh |
| **B12** | `app/scenarios.py:422-429`, `445-450` | Low | Sau khi sửa kịch bản trong UI (đổi thứ tự/ thêm trigger), một lần `dwell`/`count` có thể bị **bỏ** hoặc bắn **thừa một lần**. | `_count_flags`/`_dwell_fired` đánh khoá theo **chỉ số** `enumerate(all_triggers())`, trong khi `reconfigure()` giữ nguyên runner và các set này. | Đánh khoá theo mô tả ổn định (`type:count:seconds:classes`). | xác minh |
| **B13** | `app/scenarios.py:223`, `app/config.py:245-260` | High | Tuỳ chọn **“tạm dừng tự động khi người dùng chỉnh tay”** (`manual_override`, mặc định 10 phút, có trong config mẫu) **không bao giờ chạy**: bấm công tắc tường thì app vẫn ghi đè. | `notify_manual_override` chỉ có định nghĩa (`app/scenarios.py:223`, `573`), **không có chỗ gọi** trong `app/`; `state_topic` của manual override không được subscribe (`app/mqtt_client.py:292-293` chỉ subscribe `base_topic/#` và `+/+/+/state`). | Subscribe `state_topic` của manual override; khi có tin nhắn ⇒ `scenario_engine.notify_manual_override(zone_ref)`. | xác minh |
| **B14** | — | — | **Đạt:** mốc thời gian tính từ **lúc chuyển trạng thái**, không phải từ lần gửi trước; mỗi bước chỉ bắn **một lần** (`app/scenarios.py:328`); người quay lại giữa timeline ⇒ huỷ hẹn **và** bắn ON lại (`app/scenarios.py:260-266`); vắng lần 2 ⇒ timeline chạy lại từ đầu (`app/scenarios.py:267-272`, `388-394`); `cooldown_s` chỉ chặn **bắt đầu trigger**, không chặn từng bước (`app/scenarios.py:326-332`); `dwell` chỉ bắn lại sau khi có vắng thật (`app/scenarios.py:443-450`); lịch được kiểm tra **lại lúc bắn** (`app/scenarios.py:307-317`); trạng thái boot = unknown ⇒ **không** bắn OFF oan (`app/zones.py:166`, `app/scenarios.py:257-258`, `app/runtime.py:913-914`). | — | — | xác minh |

### B.3 MQTT

| ID | Vị trí | Mức | Triệu chứng | Nguyên nhân gốc | Cách sửa dự kiến | Trạng thái |
|---|---|---|---|---|---|---|
| **B15** | `app/mqtt_client.py:344` (cổng dry-run), `app/mqtt_client.py:450-467` (`_flush_pending`), `app/mqtt_client.py:183-186` (setter) | High | Bật **Dry-run** để thử, broker nối lại ⇒ các lệnh đã xếp **từ trước khi bật dry-run** được gửi **THẬT**. | Dry-run chỉ kiểm tra **lúc xếp hàng**; `_flush_pending` gọi `_send` trực tiếp; setter `dry_run` không xoá `_pending`. | Kiểm tra `_dry_run` lại trong `_flush_pending` **và** xoá `_pending` khi bật/tắt dry-run; xoá luôn khi `restart()` đổi broker. | xác minh |
| **B16** | `app/mqtt_client.py:245`, `213-215` | High | Một lần `start()` lỗi (host sai) ⇒ MQTT **chết vĩnh viễn**: mọi lần `start()` sau trả về ngay, mọi lệnh xếp hàng mãi. | `self._client = client` được gán **trước** `connect_async`; nhánh `except` không gán lại `None`. | `self._client = None` trong nhánh lỗi. | xác minh |
| **B17** | `app/mqtt_client.py:364-379`, `886` (`runtime.py`) | Med | Mất mạng lâu: khi nối lại chỉ gửi “trạng thái mới nhất” — nhưng khoá là **theo kịch bản**, nên kịch bản bắn 2 topic khác nhau (đèn + quạt) thì **mất** trạng thái một thiết bị; và lệnh cũ hàng giờ vẫn được gửi lại. | `key=f"scenario:{scenario_id}"`; hàng đợi chỉ giới hạn **số lượng**, không giới hạn **tuổi**. | Khoá `scenario:{id}:{topic}`; thêm TTL (bỏ lệnh cũ hơn N giây). | xác minh |
| **B18** | `app/mqtt_client.py:74-78` | Low-Med | Payload dạng **chuỗi JSON thô** có placeholder mà giá trị chứa `"` (ví dụ tên vùng `Vùng "bếp"`) ⇒ JSON hỏng, thiết bị nhận lệnh sai. Payload dạng dict/an toàn. | Chèn chuỗi thô `result.replace(token, str(value))`, không escape. | Escape khi payload là chuỗi (không phải dict/list) và trông giống JSON. | xác minh |
| **B19** | `app/mqtt_client.py:263`, `288` | Med | Dry-run vẫn ghi `online`/`offline` (retained) lên broker ⇒ broker tưởng có thiết bị thật, còn retained state bẩn. | Hai lần publish availability gọi thẳng `client.publish`, bỏ qua `publish()`/`_dry_run`. | Khi dry-run: không publish availability (hoặc dùng topic `.../dryrun`). | xác minh |
| **B20** | `app/runtime.py:858-893`, `app/config.py:615-640` | Med | Hai kịch bản cùng topic mâu thuẫn ⇒ **ai gửi sau thắng**, không có trọng tài/ưu tiên. | Không có `priority`, không có `_last_published[topic]`. | Thêm trọng tài theo topic (ưu tiên + chống đảo thứ tự) và hiện cảnh báo khi 2 kịch bản trùng topic. | xác minh (thiết kế) |
| **B21** | `app/runtime.py:903-916`, `app/mqtt_client.py:483` | Med | Có thể đặt `state_topic` của vùng **trùng** topic lệnh ⇒ retained JSON trạng thái vùng **đè** lên topic lệnh của đèn. | Không đối chiếu chéo topic. | Validator cảnh báo/chặn khi `state_topic` trùng topic publish. | xác minh |
| **B22** | `app/mqtt_client.py:292-293` | — | **Đạt:** LWT retained + `offline` khi thoát sạch; QoS/retain theo từng action tới đúng dây; `loop_start()` ⇒ publish từ thread khác an toàn; monitor ring + hàng đợi có khoá. | — | — | xác minh |

### B.4 Thread / tài nguyên / rò rỉ

| ID | Vị trí | Mức | Triệu chứng | Nguyên nhân gốc | Cách sửa dự kiến | Trạng thái |
|---|---|---|---|---|---|---|
| **B23** | `app/capture.py:442-447` + `app/capture.py:585` | High | Camera treo liên tục (đúng log của bạn: `Luồng treo quá 10s` mỗi ~20 s) ⇒ mỗi lần mở lại **rò một `cv2.VideoCapture` + thread giải mã FFmpeg + socket**; chạy nhiều ngày là phình RAM/tay nghề. | Nhánh mở lại `if self._cap is None or self._reopen.is_set(): self._reopen.clear(); if not self._open():` **không** `_close_capture()` trước; `_open()` gán đè `self._cap = cap`. | Gọi `_close_capture()` ở đầu nhánh mở lại (và giữ `_reopen` cho tới khi mở thành công). | xác minh |
| **B24** | `app/zones.py` (không có khoá) + `app/web/server.py:441-449` → `app/runtime.py:1535-1538` | High | Bấm **Mô phỏng** trong UI đúng lúc luồng suy luận đang chạy ⇒ có thể “RuntimeError: dictionary changed size during iteration” (bị nuốt, mất một khung) hoặc vùng nhảy trạng thái qua lại. | `ZoneEngine` không có `Lock` nhưng bị sửa từ **thread asyncio** (`_forced`) và đọc/ghi từ **thread suy luận** + **thread điều khiển**. | Thêm `threading.RLock` trong `ZoneEngine` (evaluate/transitions/force_zone/add_zone/remove_zone). | xác minh |
| **B25** | `app/config.py:903-916` + `app/runtime.py:1083,1088` | High | **Bấm Lưu cấu hình** đúng lúc kịch bản đang tick ⇒ control thread có thể lỗi khi duyệt `_runners` (bị nuốt) và một kịch bản có thể bị tick 2 lần/không lần. | Listener được gọi **đồng bộ trên thread asyncio**; `ScenarioEngine.reconfigure` sửa `_runners`/`_by_zone` trong khi thread khác `tick()`/`on_zone_state()`; không có khoá. | `RLock` trong `ScenarioEngine` + duyệt trên bản sao (`list(self._runners.values())`). | xác minh |
| **B26** | `app/runtime.py:829-836`, `917-926` | Med | Mất/trùng event `zone_enter`/`zone_exit`; retained state có thể bị bỏ qua hoặc gửi 2 lần. | `_zone_occupied_last`, `_published_states`, `_state_since_wall` được read-modify-write từ **2 thread**, không khoá. | Chạy cả `_feed_zone_states` dưới `_cameras_lock`. | xác minh |
| **B27** | `app/web/server.py:551-573` | Med | Hai tab cùng xem một camera: **đóng một tab là camera bị nhả** cho cả hai ⇒ tab còn lại đứng hình (với webcam là **đèn tắt**). | Khoá watcher `f"mjpeg:{camera_id}:{id(worker)}"` **dùng chung** cho mọi client, mà `_watchers` là `set` ⇒ chỉ 1 slot. | Khoá watcher duy nhất cho mỗi request (`uuid4`). | xác minh |
| **B28** | `app/web/server.py:589-596` | Med | Mỗi lần gọi `/snapshot/<id>.jpg` ghi **một file mới**, không xoá ⇒ thư mục `snapshots/` phình vô hạn. | Không có giới hạn số file/tuổi. | Giữ N file mới nhất (mặc định 50) hoặc chỉ lưu khi người dùng bấm. | xác minh |
| **B29** | `app/runtime.py:987-992` | Med | Camera **hỏng ngay từ lúc khởi động** không sinh chuyển trạng thái ⇒ `on_camera_lost` (kể cả `publish_custom`) **không chạy**. | `if previous is None: continue` chỉ ghi nhận trạng thái, không coi là chuyển tiếp. | Khởi tạo `last_status[id] = True` (coi như khoẻ lúc boot). | xác minh |
| **B30** | `app/runtime.py:906-909` | Low | Tắt một vùng đang có người ⇒ retained `.../state` giữ `occupied: true` **mãi mãi**. | Chỉ `return` khi vùng tắt, không gửi bản xoá. | Khi vùng bị tắt/xoá: publish retained `{"occupied": null}` hoặc xoá retained. | xác minh |
| **B31** | `app/capture.py:379-385`; `app/mqtt_client.py:404-407` | Low | Số liệu trên UI (fps/đã gửi/đã nhận) lệch nhẹ; không hỏng dữ liệu. | `CaptureStatus` và `_sent_total` được cập nhật ngoài khoá. | Đưa vào khoá tương ứng. | xác minh |
| **B32** | — | — | **Đạt:** mọi bộ đệm đều có giới hạn (khung mới nhất 1 slot, ring event 500, monitor 200, cửa sổ fps ~2 s, JPEG cuối 1/ngăn); tất cả thread đều daemon và có nơi join (`Runtime.stop`, `CaptureWorker.stop`, `uvicorn`); subprocess ffmpeg/mediamtx được terminate→wait→kill; không copy khung hình thừa trong luồng MJPEG. | — | — | xác minh |

---

## 3. Đã kiểm tra và THẤY ĐÚNG (không cần sửa)

Ghi lại để lần sau không “sửa” nhầm:

- **Không scale lại box của YOLO**: box lấy nguyên từ `box.xyxy` (`app/detector.py:262`), không có phép chia `imgsz` nào trong `app/`.
- **Cắt vùng (crop) rồi cộng lại offset** trước khi kiểm tra vùng: `app/runtime.py:751-757` + `shift_detection` (`app/detection.py:84-95`).
- **Polygon chuẩn hoá 0..1** được đổi sang pixel bằng **đúng** kích thước khung hình ở mọi nơi: `app/zones.py:442`, `app/zones.py:497-500`, `app/annotate.py:78-84`; validator ép chặt 0..1 (`app/config.py:296-302`).
- **Gương (mirror)** chỉ lật **một lần**, trước cả suy luận và hiển thị, và dùng **cùng một** object khung hình: `app/capture.py:479` → `packet.frame` → `app/runtime.py:742` và `app/runtime.py:1478-1481`.
- **Neo `bottom_center`** tính trên box của khung hình gốc: `app/detection.py:46-47` (lề chân 2% để không rơi đúng mép vùng).
- **`exit_grace_s` dùng đồng hồ đơn điệu** (`time.monotonic`), không dùng `time.time()`: `app/runtime.py:446`, `app/scenarios.py:1637-1638`… (không bị nhảy giờ hệ thống).
- **Cửa sổ bỏ phiếu là của từng vùng** (`PresenceTracker` riêng cho mỗi `zone_id`): `app/zones.py:302-304`.
- **MQTT publish từ thread khác là an toàn** (`loop_start()`, không `loop_forever`): `app/mqtt_client.py:252`.
- **Hot-reload không làm mất timeline**: `ScenarioEngine.reconfigure` giữ nguyên runner (`app/scenarios.py:531-538` + `184-190`), `_rebuild_engines` giữ nguyên `ZoneEngine`/`PresenceTracker` (`app/runtime.py:619-645`).
- **Lưu cấu hình nguyên tử + `.bak`**, có retry `os.replace` cho Windows: `app/config.py:790-825`.
- **Không bao giờ publish OFF oan lúc boot**: `occupied=None` → `unknown` (`app/zones.py:166`, `app/runtime.py:913-914`).

---

## 4. Kế hoạch sửa (tóm tắt — chi tiết ở `REPORT.md`)

1. **Nhóm Blocker (A1, A2, B1)** — sửa trước, mỗi lỗi kèm test đỏ→xanh.
2. **Nhóm High** (A3, A4, A5, B2, B13, B15, B16, B23, B24, B25) — sửa theo thứ tự tác động/chi phí.
3. **Nhóm Med/Low** — sửa những cái rẻ và rõ ràng; cái nào cần đổi kiến trúc thì ghi lại thành “rủi ro còn lại”.
4. **Bảng chẩn đoán** (mục 1.5 yêu cầu) + **“Giải thích quyết định”** cho mỗi lệnh MQTT.
5. **Kiểm thử**: pytest (fake clock + replay JSONL + test hồi quy cho từng lỗi) và
   Playwright (DPR 1 & 2) cho trình sửa vùng.

> Trạng thái từng mục sẽ được cập nhật ngay trong bảng trên ở BƯỚC 4.
