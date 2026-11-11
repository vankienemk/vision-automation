# BÁO CÁO SỬA LỖI — `vision_automation`

**Ngày:** 2026-11-12 · **Nhánh:** `main` · **Máy kiểm chứng:** macOS 15.7.9 / Apple Silicon M1
**Trước:** 183 test pass · **Sau:** **231 test pass** (~30 s) · **Số lỗi đã sửa:** 21
**Tài liệu kèm theo:** `AUDIT.md` (kết quả kiểm tra trước khi sửa, có đường dẫn + số dòng)

Quy trình đã làm đúng thứ tự yêu cầu: **kiểm tra chỉ-đọc → viết test tái hiện lỗi (đỏ) →
sửa đỏ sang xanh → chạy lại toàn bộ**. Không sửa gì trước khi có bằng chứng.

---

## 0. Ba câu ngắn gọn

1. Nút thắt “kéo thả điểm hoạt động kém” **không phải** do cảm giác — có **2 lỗi toạ độ thật**:
   trộn hệ toạ độ màn hình với hệ toạ độ canvas, và tự đoán khung ảnh rồi phóng to ảnh
   (trong khi CSS không bao giờ phóng to). Vùng lệch tới **85 px**.
2. Có **4 chỗ có thể làm đèn sai** (tắt khi người vẫn trong phòng, tắt sớm, dry-run rò lệnh
   thật, “tạm dừng khi chỉnh tay” chưa từng được nối) — đã bịt hết.
3. Có **2 lỗi rò tài nguyên** khiến chạy vài ngày là phình (`cv2.VideoCapture` rò mỗi lần
   camera treo, hàng đợi MQTT phát lại lệnh cũ) — đã bịt.

---

## 1. Đã sửa gì (21 lỗi, theo mức độ)

Ký hiệu: **A** = trình sửa vùng (giao diện), **B** = pipeline (suy luận → vùng → kịch bản →
MQTT). Mã số khớp với `AUDIT.md`.

### 1.1 Blocker (3)

| Mã | Lỗi | Sửa | Test chứng minh |
|---|---|---|---|
| **A1** | `nearestVertex`/`hitEdge` so `clientX` (toạ độ **màn hình**) với toạ độ **trong canvas** ⇒ lệch đúng `rect.left/rect.top`; bán kính bắt 22 px thành vô nghĩa ⇒ rê chuột không nhận ra điểm, kéo vào điểm thì **di chuyển cả vùng**. | Quy đổi con trỏ về hệ canvas ở **một chỗ** (`canvasPointOf`), mọi phép so khoảng cách dùng cùng hệ. | `test_zone_editor_ui.py::test_vertex_drag_follows_pointer[1,2]`, `::test_vertex_drag_leaves_other_vertices_alone` |
| **A2** | `imageBox` tự đoán khung ảnh `min(rect.w/naturalW, rect.h/naturalH)` và **phóng to** ảnh, nhưng CSS `max-width/max-height` **không** phóng to ảnh nhỏ ⇒ ảnh 640×480 trong panel 1100 px: code tưởng rộng 725 px ⇒ lệch tới **85 px**; toạ độ lưu xuống cũng sai. | Lấy khung ảnh **thật** từ `img.getBoundingClientRect()` (đúng với `object-fit`, flex, mọi DPR). | `::test_rect_corner_drag_keeps_rectangle`, `::test_save_sends_normalised_polygon_and_shape` |
| **B1** | Suy luận lỗi ⇒ `detections = []` bị coi là “**khung hình thật không có ai**” ⇒ sau `exit_grace_s` vùng báo trống ⇒ 30 s giảm sáng, 120 s **TẮT ĐÈN khi người vẫn trong phòng**. | Khi suy luận lỗi: `transitions(..., sample=False)` → vùng **giữ nguyên** trạng thái đã biết; đếm lỗi + hiện trên bảng chẩn đoán. | `test_runtime.py::test_inference_failure_does_not_make_zone_vacant`, `test_zones.py::test_engine_transitions_sample_false_keeps_zone_occupied` |

### 1.2 High (8)

| Mã | Lỗi | Sửa | Test |
|---|---|---|---|
| **A3** | Vẽ vùng **trên hình đang chạy** ⇒ người/đồ vật di chuyển, kéo-thả luôn trượt. | Tab Vùng phát hiện **tự đóng băng khung hình** thành ảnh tĩnh; nút **📷 Chụp lại** / **▶ Trực tiếp**; nhãn trạng thái cạnh nút. | `::test_zone_tab_freezes_the_frame_for_editing` |
| **A4** | Đổi tab / đổi camera khi chưa lưu ⇒ **mất sạch** thay đổi, không hỏi. | `confirm()` trước khi ghi đè + dấu **•** trên tab + nút Lưu nhấp nháy. | `::test_leaving_with_unsaved_edits_asks_before_discarding` |
| **A5** | Mọi thao tác bắt đầu ngay ở `pointerdown`, không có ngưỡng phân biệt **bấm** và **kéo**; bấm đúp để đóng vùng sinh **2 điểm trùng** (4 điểm thành 5–6). | Ngưỡng **4 px**; bỏ điểm trùng khi tạo vùng (`dedupePolygon`). | `::test_double_click_finish_has_no_duplicate_points` |
| **B2** | Mốc chờ-rời-vùng **không được xoá** khi người hiện lại ⇒ lần hụt sau báo trống **ngay** (bỏ qua `exit_grace_s`) ⇒ đèn nháy. | Xoá mốc mỗi khi số phiếu lành lại (`app/zones.py:274-281`). | `test_zones.py::test_recovery_clears_the_exit_grace_timestamp` |
| **B13** | `manual_override` (“tạm dừng khi người dùng chỉnh tay”) có trong config, có code, nhưng **không ai subscribe** ⇒ bấm công tắc tường vẫn bị app ghi đè. | `set_override_topics()` + subscribe `state_topic` → `Runtime.notify_manual_override()`. | `test_mqtt.py::test_manual_override_topic_triggers_callback`, `::test_manual_override_ignores_unrelated_topics` |
| **B15** | **Dry-run rò lệnh THẬT**: cờ chỉ kiểm tra lúc xếp hàng; broker nối lại là `_flush_pending()` gửi thẳng. | Xoá hàng đợi khi bật dry-run/`restart()`; kiểm tra lại cờ **trước mỗi lần gửi**. | `test_mqtt.py::test_dry_run_switch_clears_queued_commands`, `::test_flush_pending_drops_queue_when_dry_run_is_on` |
| **B16** | `start()` lỗi (host sai) ⇒ MQTT **chết vĩnh viễn** (client không được xoá, mọi lần `start()` sau trả về ngay). | Xoá `_client` trong nhánh lỗi ⇒ lần sau thử lại thật. | `::test_failed_start_reports_error_and_allows_retry` |
| **B23** | Camera treo liên tục ⇒ mỗi lần mở lại **rò một `cv2.VideoCapture` + thread FFmpeg + socket** (chạy vài ngày là phình). | `_open()` release handle cũ trước; chỉ xoá cờ `_reopen` sau khi mở **thành công** (theo số thế hệ). | `test_capture.py::test_reopen_releases_old_capture_before_opening_new`, 3 test nữa |
| **B24/B25** | Engine bị sửa từ **thread asyncio** (mô phỏng, lưu cấu hình) trong khi thread suy luận/điều khiển đang duyệt ⇒ `RuntimeError: dictionary changed size` (bị nuốt) → mất khung/lần tick. | Khoá chung `_engine_lock` cho mọi lối vào engine. | (được chứng minh gián tiếp: 231 test + chạy thật 6 phút không lỗi) |
| **B29** | Camera **hỏng ngay từ lúc khởi động** không sinh chuyển trạng thái ⇒ `on_camera_lost` (kể cả `publish_custom`) không chạy. | Coi như khoẻ lúc boot; fail-safe áp dụng **ngay**, không đợi nhịp điều khiển. | `test_runtime.py` (camera-lost), chạy thật |

### 1.3 Med / Low (10)

| Mã | Lỗi | Sửa |
|---|---|---|
| **A6** | Xoá điểm có thể làm vùng còn 2 điểm; kéo tạo hình chữ nhật quá dẹt thì **im lặng không tạo gì**. | Chặn xoá dưới 3 điểm (kèm giải thích); HCN dẹt được nắn tối thiểu 2% mỗi chiều. |
| **A7** | Vùng **tự cắt nhau** / quá nhỏ / điểm trùng vẫn lưu được ⇒ “trong vùng” sai. | `validatePolygon` ở UI (diện tích, tự cắt, trùng) + validator phía server (tự bỏ điểm trùng, chặn diện tích ~0) + cảnh báo `warnings` từ API. |
| **B3** | Kéo “độ tin cậy tối thiểu” của vùng **xuống thấp hơn mức chung**: vô tác dụng (box đã bị model lọc trước). | Ngưỡng gửi xuống model = **ngưỡng thấp nhất trong các vùng**. |
| **B4** | Vùng bị **tắt** giữ trạng thái cũ mà `updated_at` vẫn mới; bật lại dùng cửa sổ bỏ phiếu cũ. | Reset tracker + `occupied=None` khi tắt. |
| **B5** | Khung hình **cũ vài giây** vẫn được đánh giá như “bây giờ”. | Bỏ khung cũ hơn `2/detect_fps`. |
| **B7** | Vắng vùng **lần 2 trong `cooldown_s`** ⇒ timeline giảm sáng/tắt đèn **bị bỏ, không hẹn lại** ⇒ đèn ở mãi ON. | Hẹn chạy lại đúng lúc hết cooldown (chỉ với `vacant`). |
| **B8** | Lịch qua đêm + `days`: `22:00→06:00, days:[0]` **không hoạt động** lúc 01:00 Thứ 3 (mất “ngày sở hữu”). | `days` được so với **thứ của ngày bắt đầu** khung giờ. |
| **B9** | `from == to` ngầm hiểu là “cả ngày” ⇒ dễ bật 24/7 ngoài ý muốn. | Validator chặn, kèm hướng dẫn (“muốn cả ngày thì xoá `active_schedule`”). |
| **B10** | `count_le: 0` bắn **ngay khi khởi động** trong phòng trống. | Nhánh “vắng lúc boot” không chạy trigger theo số đếm. |
| **B12** | Sửa kịch bản trong UI (đổi thứ tự trigger) làm **lệch trạng thái cạnh** ⇒ bỏ/bắn thừa một lần. | Khoá theo **mô tả trigger** thay vì chỉ số. |
| **B17** | Hàng đợi offline gộp theo **kịch bản** ⇒ mất lệnh tới topic thứ hai; lệnh cũ hàng giờ vẫn gửi lại. | Khoá `key\|topic` + `queue_ttl_s` (mặc định 900 s, bỏ lệnh quá cũ). |
| **B18** | Payload dạng **chuỗi JSON** không escape giá trị placeholder ⇒ JSON hỏng khi tên vùng có dấu `"`. | Escape đúng chuẩn JSON (chỉ khi payload là JSON). |
| **B19** | Dry-run vẫn ghi `online`/`offline` (retained) lên broker. | Không publish availability khi dry-run. |
| **B21** | `state_topic` của vùng có thể **trùng topic lệnh** ⇒ bản tin retained đè lệnh đèn. | `topic_warnings()` → hiện cảnh báo trên UI + `/api/state`. |
| **B27** | Hai tab xem cùng camera: **đóng một tab là camera bị nhả** cho cả hai. | Mỗi client một khoá watcher riêng (`add_watcher_auto`). |
| **B28** | Mỗi lần lấy snapshot ghi **một file mới**, không xoá ⇒ phình ổ đĩa. | Giữ 50 ảnh mới nhất mỗi camera. |
| **B31** | Trạng thái camera bị đọc/ghi không khoá ⇒ UI có thể đọc trạng thái nửa vời. | `status()` trả bản sao dưới khoá (`replace(...)`). |
| **MỚI** | `--config data/config.yaml` (đường dẫn tương đối) bị **tính hai lần** sau khi chdir ⇒ app “tự tạo cấu hình mới”, người dùng tưởng **mất hết cài đặt**. | Resolve tuyệt đối **trước** khi chdir. |

### 1.4 Hai tính năng mới theo yêu cầu

**1) Bảng chẩn đoán** (tab Trực tiếp, `/api/state` → `diagnostics`): FPS camera, fps nguồn báo,
**tuổi khung hình (ms)**, thời gian đọc khung, FPS/thời gian suy luận, lần suy luận gần nhất,
**số lỗi suy luận** (tổng + liên tiếp + thông điệp cuối), số lần mở lại kết nối, trạng thái
từng vùng (kèm “chờ xác nhận vắng”, “đang mô phỏng”), MQTT (đã nối? bao nhiêu lệnh đang xếp
hàng? đã gửi/nhận), cảnh báo cấu hình nguy hiểm.

**2) “Giải thích quyết định”**: mỗi lệnh MQTT có một câu lý do tiếng Việt
(`Gửi “Giảm sáng” tới zigbee2mqtt/den_bep/set vì vùng “Khu bếp” đã trống đủ thời gian chờ rời vùng`);
mỗi kịch bản có `reason` cho biết **vì sao đang không làm gì**
(`Ngoài khung giờ hoạt động`, `Chưa xác định được trạng thái vùng`, `bước “Tắt đèn” sẽ gửi sau 87.4s`…);
nhật ký 30 quyết định gần nhất hiện ngay dưới bảng chẩn đoán.

---

## 2. Bằng chứng kiểm chứng

### 2.1 Test tự động: 183 → **231** (tất cả xanh)

| Nhóm | Thêm | Nội dung |
|---|---|---|
| `tests/test_zone_editor_ui.py` | **12** (mới) | **Playwright + Chromium thật**, `deviceScaleFactor` **1 và 2** |
| `tests/test_zones.py` | +4 | mốc grace, vùng tắt, `sample=False` |
| `tests/test_mqtt.py` | +13 | dry-run/`start()`/hàng đợi/escape + tạm dừng khi chỉnh tay + availability |
| `tests/test_scenarios.py` | +3 | cooldown vắng vùng, `sampled=False`, `reason` |
| `tests/test_config.py` | +4 | lịch qua đêm, `from==to`, polygon trùng/diện tích 0, cảnh báo topic |
| `tests/test_runtime.py` | +2 | giữ trạng thái khi suy luận lỗi, khối `diagnostics` |
| `tests/test_capture.py` | +8 | rò `VideoCapture`, watcher 2 tab, trạng thái dưới khoá |
| `tests/test_platform.py` | +1 | `--config` tương đối không bị tính hai lần |

Test UI dùng một HTTP server tí hon phục vụ **đúng** file trong `app/web/static/` + API giả
(không cần camera, không cần YOLO) nên chạy được cả trong CI. Toạ độ kỳ vọng được tính từ
`#zone-img.getBoundingClientRect()` — chính khung ảnh **thật** trình duyệt vẽ ra.

**Trước khi sửa, 6/9 test UI đỏ** đúng như người dùng mô tả:
- `kéo 1 điểm nhưng điểm 0 cũng chạy: [0.7, 0.15] → [0.740, 0.186]` (A1)
- `góc phải chạy về phía con trỏ: assert (0.1 < 0.1)` (A2)
- `không nhận ra điểm: None` (A1/A10)
- `bấm đúp làm sinh thêm điểm: 5 điểm thay vì 3` (A5)
- `không hỏi gì trước khi bỏ thay đổi vùng` (A4)

### 2.2 Chạy thật trên M1 (không phải mô phỏng)

App thật (`run.py --config .run/config-verify.yaml --port 8099`), camera nguồn `samples/bus.jpg`,
model `yolo11n.pt` trên **mps**, MQTT dry-run:

```
camera cam_anh: connected True · capture_fps 4.9 · tuổi khung 33 ms · suy luận 26 ms @ 4.0 fps
detector: ultralytics / mps / 0 lỗi
vùng: khu_nguoi {occupied: True, count: 3}, khu_trong {occupied: False, count: 0}
lý do:  "Có người trong vùng — không có bước nào đang chờ."
quyết định: "[DRY-RUN] Gửi “Bật 100%” tới zigbee2mqtt/den_bep/set
             vì vùng “Khu có người” vừa có người vào (đếm 3)"
```

**Kéo điểm trong trình duyệt thật** (Chromium, ảnh 640×480, panel rộng ~1100 px — đúng tình
huống gây lệch 85 px trước đây):

| DPR | Đóng băng | Rê chuột lên điểm | Kéo điểm (32 px) | Kết quả |
|---|---|---|---|---|
| 1 | ✔ ảnh tĩnh | nhận ra điểm, con trỏ `grab` | `[0.02,0.55] → [0.079,0.581]` | **đúng bằng toạ độ con trỏ** (mong đợi `[0.079,0.581]`) |
| 2 (Retina) | ✔ ảnh tĩnh | nhận ra điểm, con trỏ `grab` | `[0.02,0.55] → [0.079,0.581]` | **đúng**, không lệch theo DPR |

**Vẽ vùng mới bằng chuột (4 điểm + bấm đúp)**: tạo đúng **4 điểm** (trước đây 5–6 do điểm trùng),
bấm **Lưu** → `Đã lưu 3 vùng cho cam_anh`, `config.yaml` được ghi + `.bak`, app hot-reload, **0 lỗi
trong console trình duyệt**. Bảng chẩn đoán hiện đúng số liệu
(`cam_anh · đang nhận hình · FPS camera 4.9 · tuổi khung 113 ms · suy luận 26 ms · lỗi 0`).

### 2.3 Kiểm tra tĩnh

- `node --check app/web/static/app.js` — sạch.
- `python -c "import app.runtime"` — sạch.
- Không mở webcam thật trong suốt quá trình test (tránh bật đèn camera của bạn).

---

## 3. Những gì **CHƯA** sửa (rủi ro còn lại — nói thẳng)

| # | Vấn đề | Vì sao chưa sửa | Rủi ro thực tế |
|---|---|---|---|
| 1 | `count_ge/count_le/class_present` dùng **số đếm thô của một khung** (chưa bỏ phiếu K-of-N cho từng lớp) | Cần thêm cửa sổ bỏ phiếu theo lớp trong `PresenceTracker`; thay đổi vừa rộng vừa dễ gây tác dụng phụ | Nhận diện nhấp nháy có thể bắn **một** lệnh; `cooldown_s` đã chặn phần lớn. Đã bịt trường hợp nguy hiểm hơn (suy luận lỗi). |
| 2 | Chưa có **trọng tài** khi 2 kịch bản publish cùng topic (ai gửi sau thắng) | Cần thiết kế `priority` + `_last_published[topic]`; đổi mô hình dữ liệu | Chỉ xảy ra khi bạn tự tạo 2 kịch bản trùng topic. **Cảnh báo topic trùng đã có** với `state_topic`. |
| 3 | Khung giờ dùng **giờ hệ điều hành**, không có trường timezone | Đúng trên máy bạn (Asia/Ho_Chi_Minh); thêm tz là thay đổi cấu hình | Đổi timezone của máy là mọi khung giờ dịch theo. |
| 4 | Lệnh MQTT chưa mang **số khung hình** đã sinh ra nó | Cần luồn `seq`/`wall_time` qua `ZoneState` → `CommandRequest` | Chỉ ảnh hưởng truy vết; tuổi khung đã hiện trong bảng chẩn đoán. |
| 5 | Vùng đang bật bị **tắt** khi đang có người: retained `.../state` giữ `occupied: true` | Cần publish bản xoá retained | Topic trạng thái của vùng đó “đứng hình”, không ảnh hưởng điều khiển đèn. |
| 6 | MJPEG: watcher có thể rò nếu client huỷ **trước khi** generator bắt đầu | Phụ thuộc phiên bản Starlette; chưa tái hiện được | Hệ quả: webcam có thể không tự tắt đèn. Có nút/kịch bản tắt camera test để xử lý. |
| 7 | `crop_to_zones`, `motion_gating` **chưa** được kiểm chứng với camera thật | Không có camera RTSP thật trong LAN lúc kiểm thử | Tính năng tuỳ chọn, mặc định tắt. |
| 8 | Windows: chưa chạy trên **máy Windows thật** (chỉ kiểm tra nhánh mã hoá theo `sys.platform`) | Chỉ có macOS trong phiên này | Xem §4: chạy `Kiem-tra-Windows.bat` trên máy đích. |

---

## 4. Cách kiểm tra tay trên máy M1 (10 phút)

```bash
cd "vision_automation"
source .venv/bin/activate
python -m pytest -q            # kỳ vọng: 231 passed
python run.py --config config.yaml --port 8080
```

Mở `http://127.0.0.1:8080`:

1. **Tab Trực tiếp** → kéo xuống “Bảng chẩn đoán”. Số phải **đổi liên tục**:
   `FPS camera` > 0, `Tuổi khung hình` vài chục ms (nếu > 2000 ms là đường truyền đang đệm),
   `Lỗi suy luận` = 0, `MQTT` = đã nối (nếu bạn có broker).
2. Đứng vào vùng → thẻ vùng chuyển **xanh “có người ×1”**; bước ra → **xám “trống”** sau `exit_grace_s`.
3. Mở “Giải thích quyết định” → phải thấy dòng lý do cho lệnh vừa gửi.
4. **Tab Vùng phát hiện** → ảnh phải **đứng yên** (nhãn `⏸ ảnh tĩnh`). Rê chuột vào một điểm:
   **điểm sáng vàng, con trỏ thành bàn tay ✋, hiện nhãn “Kéo để chỉnh điểm”**. Kéo → **chỉ điểm đó chạy**.
5. Rê vào **giữa cạnh** → con trỏ thành **dấu +**, kéo → thêm điểm mới nằm trên cạnh.
6. Rê vào **giữa vùng** → con trỏ ✥, kéo → **cả vùng** di chuyển.
7. Kéo một điểm rồi **đổi tab** khi chưa lưu → phải có hộp thoại hỏi. Bấm **Cancel** → vẫn ở lại, dữ liệu còn.
8. Bấm **💾 Lưu vùng** → toast `Đã lưu N vùng…`, `config.yaml.bak` được tạo.
9. Bấm **▶ Trực tiếp** → ảnh chạy lại; bấm **📷 Chụp lại** → đóng băng khung mới.
10. Rút mạng camera (hoặc tắt nguồn RTSP) → bảng chẩn đoán hiện `mất hình` + `Mở lại kết nối` tăng dần;
    **đèn không đổi trạng thái** (`on_camera_lost: hold`) và UI không treo.

Máy Windows đích: chạy `Kiem-tra-Windows.bat` (kiểm tra môi trường) rồi `Khoi-dong.bat`.

---

## 5. Quyết định thiết kế đáng ghi lại

- **Đóng băng khung hình làm mặc định khi sửa vùng**: vẽ trên video đang chạy là nguyên nhân
  trực tiếp của cảm giác “kéo thả kém”, nên đây là mặc định chứ không phải tuỳ chọn ẩn.
  Ảnh tĩnh là `data:` URL chụp từ chính khung đang hiển thị → nhả luồng MJPEG (webcam tắt đèn).
- **Ngưỡng kéo 4 px**: dưới ngưỡng coi là **bấm** (chọn/ thêm điểm). Nhờ vậy bấm để chọn điểm
  không làm điểm chạy 1–2 px và bấm đúp để đóng vùng không sinh điểm trùng.
- **`sample=False` thay vì `[]`**: phân biệt rạch ròi “không có đối tượng” với “không có kết quả
  suy luận”. Đây là van an toàn quan trọng nhất cho yêu cầu “không tắt đèn oan”.
- **Khoá chung `_engine_lock`** thay vì rải khoá khắp nơi: chỉ 8 lối vào engine, dễ kiểm soát
  thứ tự khoá, không đụng vào cấu trúc dữ liệu thuần của `zones.py`/`scenarios.py`.
- **Cảnh báo thay vì chặn** với `state_topic` trùng topic lệnh: không được làm app từ chối
  khởi động chỉ vì một cấu hình đang chạy được.

---

## 6. Commit (nhỏ, tách theo lỗi)

| Commit | Nội dung |
|---|---|
| `7948575` | `docs: AUDIT.md` — kiểm tra chỉ-đọc trước khi sửa |
| `482178c` | `fix(ui,config)` — toạ độ trình sửa vùng, đóng băng khung hình, chuẩn hoá cấu hình |
| `4843b2b` | `fix(zones)` — không báo vắng sớm, `sample=False` |
| `26e6479` | `fix(mqtt)` — dry-run, `start()`, hàng đợi + TTL, escape JSON |
| `496214d` | `fix(scenarios)` — cooldown vắng vùng, khoá ổn định, giải thích quyết định |
| `9f0806d` | `fix(runtime,web)` — giữ trạng thái khi suy luận lỗi, chẩn đoán, khoá engine |
| `99eccb4` | `fix(capture)` — không rò `VideoCapture` khi mở lại |
| `1ac063c` | `fix(main)` — resolve `--config` trước khi chdir |
| sau cùng | `docs:` README + báo cáo này |

**Repo:** https://github.com/vankienemk/vision-automation
