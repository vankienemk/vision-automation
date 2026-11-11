# HƯỚNG DẪN NHANH — dành cho người không rành kỹ thuật

> ⚠️ **Bạn sẽ chạy 24/7 trên máy Windows**, không phải máy Mac này. Tài liệu này
> dùng để **thử trên Mac** (nếu muốn); còn hướng dẫn cài và chạy trên **Windows**
> ở file **[`HUONG-DAN-WINDOWS.md`](HUONG-DAN-WINDOWS.md)** — bấm đúp
> `Khoi-dong.bat` là chạy, bấm `Kiem-tra-Windows.bat` để kiểm tra máy thiếu gì.

Tài liệu này chỉ để **bật lên và thử**, theo đúng thứ tự, khoảng **15 phút**.
Không cần sửa file, không cần biết lập trình. Mọi thứ chỉnh trong trình duyệt.

> Máy này **đã cài đặt xong** (Python + thư viện + model nhận diện nằm sẵn trong thư mục dự án).
> Bạn **không phải cài thêm gì**.

Ba khái niệm cần biết trước (đọc 20 giây):

| Từ | Nghĩa đơn giản |
|---|---|
| **RTSP** | "Địa chỉ luồng video" của camera IP (ví dụ `rtsp://admin:matkhau@192.168.1.50:554/stream1`) |
| **MQTT** | Cách app ra lệnh cho thiết bị (đèn, công tắc thông minh) — cần một "broker" (bộ phát tin) |
| **Dry-run** | Chế độ **chạy thử**: app tính toán đầy đủ nhưng **không gửi lệnh thật** ra thiết bị |

---

## Bước 1 — Khởi động (chọn 1 trong 2 cách)

### Cách A — bấm đúp (dễ nhất)

1. Mở thư mục `vision_automation` trong Finder.
2. Bấm đúp file **`Khoi-dong.command`**.
3. Một cửa sổ **Terminal** hiện ra và tự chạy.
4. Đợi ~10–20 giây, **trình duyệt tự mở** trang `http://127.0.0.1:8080`.

> Nếu macOS báo *"không thể mở vì không rõ nguồn gốc"*: **chuột phải** vào file → **Open** → **Open** lần nữa.

### Cách B — dán 1 dòng lệnh

Mở **Terminal** (Launchpad → gõ "Terminal") rồi dán đúng 2 dòng sau, mỗi dòng Enter một lần:

```bash
cd "/Users/a/Documents/deepseek harness/Camera smarthome/vision_automation"
.venv/bin/python -m app --with-webcam
```

Rồi mở trình duyệt tới: **http://127.0.0.1:8080**

**Bạn sẽ thấy:** cửa sổ Terminal chạy các dòng chữ, trong đó có dòng kiểu
`Model yolo11n.pt sẵn sàng trên mps (imgsz=640, warmup ... ms)` (mps = dùng chip Apple)
và `Web UI: http://127.0.0.1:8080`.

> ❗ **Không đóng cửa sổ Terminal** khi đang dùng. Đóng = tắt app.
> Điện thoại trong cùng wifi cũng mở được: `http://<IP-máy-Mac>:8080` (xem IP trong Cài đặt mạng).

---

## Bước 2 — Trang "Thiết lập lần đầu"

Lần đầu mở sẽ hiện hộp thoại **Thiết lập lần đầu** gồm 3 trang — cứ bấm **Tiếp →** rồi **Hoàn tất**.

> Nút **"Bỏ qua"** = thoát luôn hộp thoại này (không tạo kịch bản mẫu). Muốn có kịch bản mẫu "Đèn bếp" thì đi hết 3 trang và bấm **Hoàn tất**.

| Trang | Nếu bạn **có** camera IP | Nếu bạn **chưa** có camera IP |
|---|---|---|
| 1. Kết nối camera | Dán URL RTSP → bấm **🔍 Kiểm tra kết nối** → thấy hình là đạt → **Tiếp →** | Bấm **Tiếp →** (dùng webcam laptop ở bước 3) |
| 2. Kết nối MQTT broker | Nhập Host/Port/User/Password → **🔌 Kiểm tra MQTT** | Bấm **Tiếp →** (app chạy Dry-run, không gửi lệnh thật) |
| 3. Tạo kịch bản mẫu "Đèn bếp" | Để nguyên → **Hoàn tất** | Để nguyên → **Hoàn tất** |

> Nếu bạn đang dùng **camera test webcam** thì app đã có sẵn kịch bản mẫu riêng, nên bước 3 sẽ **không tạo trùng** — cứ bấm **Hoàn tất**.

Sau khi hoàn tất, bạn vào được giao diện chính với 7 thẻ:
**📹 Trực tiếp · ⬛ Vùng phát hiện · ⚙️ Kịch bản · 🧪 Mô phỏng · 📜 Nhật ký · 🔌 MQTT · 🛠️ Cài đặt**

---

## Bước 3 — TEST 1: thấy hình và thấy máy "nhận diện" người

Mục đích: chứng minh camera + AI hoạt động. **Không cần camera IP.**

Khi khởi động bằng `Khoi-dong.command`, app **đã tự tạo sẵn** một camera test bằng webcam laptop:

- Camera: **Camera test (webcam laptop)** — mã `cam_test_laptop`, có nhãn **TEST** trên hình
- Vùng có sẵn: **Vùng test** (khung lớn giữa hình)
- Kịch bản có sẵn: **Test: đèn bếp (giả lập)** — chạy ở chế độ **chạy thử**, không điều khiển đèn thật

Các bước:

1. Sang thẻ **📹 Trực tiếp**.
2. Ở ô chọn camera (góc trái), chọn **Camera test (webcam laptop)**.
3. **Đứng trước laptop** hoặc giơ tay ra trước camera.

*(Nếu không thấy camera test trong danh sách: thẻ **🛠️ Cài đặt** → mục webcam → **↻ Quét lại** → chọn **FaceTime HD Camera** → **＋ Bật camera test** → **💾 Lưu cài đặt**.)*

**Bạn sẽ thấy:**
- Hình video của bạn (lưu ý: ảnh được làm ngược như gương — bình thường).
- **Khung chữ nhật xanh** + nhãn `person 0.87` quanh người bạn → AI đang hoạt động.
- Ở cạnh trên: `capture 15 fps`, `infer 4 fps`, `~25 ms`, và 2 chấm trạng thái **CAMERA / MQTT**.

**Lần đầu macOS sẽ hỏi quyền Camera** → bấm **Allow / Cho phép**.
Nếu lỡ bấm "Don't Allow": mở **System Settings → Privacy & Security → Camera** → bật cho **Terminal** (hoặc ứng dụng bạn dùng) → **thoát app và mở lại**.

> 💡 Camera test có cơ chế **tự tắt**: chỉ mở khi bạn đang xem hình. Rời khỏi thẻ Trực tiếp ~3 giây là **đèn webcam tắt**. Đây là chủ ý (riêng tư + tiết kiệm điện).

---

## Bước 4 — TEST 2: vẽ "vùng phát hiện"

Mục đích: dạy app biết **chỗ nào cần quan tâm** (ví dụ: khu bếp, không tính lối đi).

1. Vào thẻ **⬛ Vùng phát hiện**.
2. Ô **Camera**: chọn camera của bạn. Ô **Vùng đang sửa**: chọn vùng có sẵn, hoặc bấm **＋ Vùng mới** để tạo vùng trống.
3. Bấm **✏️ Vẽ đa giác** rồi **nhấn từng điểm** trên ảnh để vẽ đường bao quanh khu vực (3 điểm trở lên).
4. **Nhấn đúp** (hoặc bấm **✔ Hoàn tất vùng**) để đóng đa giác.
5. Bấm **💾 Lưu vùng**.

> App **nhớ camera bạn đang chọn**: lần sau mở trình duyệt (hoặc khởi động lại app) là vào đúng camera đó,
> không tự nhảy về camera đầu danh sách.

Thao tác sửa (khi đang vẽ):

| Muốn làm gì | Làm thế nào |
|---|---|
| Di chuyển 1 điểm (đỉnh) | Đưa chuột tới **điểm** — điểm sáng vàng, con trỏ thành ✋ → kéo. **Chỉ điểm đó chạy**, các điểm khác đứng yên |
| **Thêm điểm ở giữa một cạnh** (đa giác) | Đưa chuột tới **giữa cạnh** — con trỏ thành dấu **+** → kéo. Điểm mới nằm đúng trên cạnh nên hình không méo |
| **Đổi chiều dài/rộng** (hình chữ nhật) | Kéo **điểm góc** — 2 góc kề chạy theo, hình vẫn vuông góc |
| **Dời riêng 1 cạnh** (hình chữ nhật) | Kéo **chấm giữa cạnh** — con trỏ thành ↕/↔ |
| Di chuyển cả vùng | Đưa chuột vào giữa vùng (con trỏ ✥) rồi kéo |
| Xoá 1 điểm | Bấm chọn điểm → nhấn phím **Delete** |
| Lùi / làm lại | **↶ Hoàn tác** / **↷ Làm lại** |
| Vẽ nhanh hình chữ nhật | Bấm **▭ Vẽ hình chữ nhật** rồi kéo 1 đường chéo |
| Đổi vùng đa giác ⇄ hình chữ nhật ⇄ toàn khung | Ô **Kiểu vùng** ở cột Thuộc tính (đổi được cả khi đang sửa vùng có sẵn; kiểu được **lưu lại**) |
| Sửa vùng khác | Bấm trúng **một điểm** của vùng đó (tự chuyển sang vùng đó), hoặc chọn trong danh sách **Vùng đang sửa**. Bấm vào *bên trong* vùng khác sẽ **không** tự đổi vùng |
| Đang vẽ dở vùng mới mà muốn sửa vùng cũ | Nhấn **Esc** (hoặc bấm trúng một điểm của vùng cũ) — thanh vàng “Đang vẽ vùng mới” sẽ tắt |

Ở cột phải (Thuộc tính vùng) bạn đặt được: **tên**, **màu**, **kiểu vùng**, **lớp cần phát hiện** (mặc định `person` = người),
**độ tin cậy tối thiểu**, **cách xác định "trong vùng"**, **số khung xác nhận (K-of-N)**, **thời gian chờ rời vùng**.

> **Mã vùng (id) được khoá 🔒** để đổi tên vùng không làm hỏng kịch bản (vùng vừa tạo thì mã tự sinh theo
> tên cho tới khi bấm Lưu). Chỉ bấm 🔒 khi thật sự muốn đổi mã —
> app sẽ tự cập nhật kịch bản sang mã mới. Nếu bạn **xoá** một vùng đang được kịch bản dùng, app sẽ hỏi
> *"có xoá luôn kịch bản đó không"* thay vì báo lỗi khó hiểu.

> Muốn đèn không nhấp nháy: tăng **K-of-N** lên `3/5` và **exit grace** lên `4–5` giây.

**Bạn sẽ thấy:** vùng có màu **xám = trống**, **xanh = có người**, và số đếm `đang có: 1` cập nhật khi bạn đi vào/ra vùng.

---

## Bước 5 — TEST 3: giả lập có người (không cần đứng trước camera)

Mục đích: thử kịch bản mà không phải đi đi lại lại. **Bắt buộc thử bước này trước khi dùng thật.**

1. Trên thanh trên cùng, **BẬT công tắc Dry-run** (nền đổi màu + có dải cảnh báo "DRY-RUN" = chưa gửi lệnh thật).
2. Vào thẻ **🧪 Mô phỏng**.
3. Tìm vùng của bạn → bấm **Có người** (số người = 1).
4. Sang thẻ **📜 Nhật ký** → xem bảng **Sự kiện gần đây**.

**Bạn sẽ thấy** trong Nhật ký:
- `zone_enter` — có người vào vùng
- một dòng lệnh kiểu:
  - với camera test: `Test: đèn bếp (giả lập) → test/zigbee2mqtt/den_bep/set {"state":"ON","brightness":254}`
  - với camera/vùng thật: `Đèn bếp tự động → zigbee2mqtt/den_bep/set {"state":"ON","brightness":254}`
  - kèm nhãn `[DRY-RUN]` nếu Dry-run đang bật

5. Quay lại **🧪 Mô phỏng** → bấm **Trống** (vắng người).
6. Ở mục **Tốc độ thời gian**, bấm chip **x30** → đồng hồ kịch bản chạy nhanh 30 lần.

**Bạn sẽ thấy** ở ô "Trạng thái kịch bản": đếm ngược `Giảm sáng` rồi `Tắt đèn` chạy nhanh, và 2 lệnh nữa xuất hiện trong Nhật ký (mờ 30 giây → tắt sau 2 phút, giờ chỉ mất vài giây).

✅ Nếu 3 lệnh (BẬT → MỜ → TẮT) xuất hiện đúng thứ tự này thì **phần logic đã chạy đúng**.

---

## Bước 6 — TEST 4: kiểm tra MQTT thật (chỉ khi bạn có broker)

Chưa có broker thì **bỏ qua bước này**, app vẫn dùng Dry-run bình thường.
Broker là gì? Là phần mềm trung gian nhận lệnh và chuyển cho đèn. Nếu bạn dùng **Home Assistant** thì đã có sẵn broker (Mosquitto).

1. Thẻ **🛠️ Cài đặt** → mục MQTT: nhập **Host** (ví dụ `192.168.1.10`), **Port** `1883`, **User**, **Password** → **💾 Lưu cài đặt**.
2. Thẻ **🔌 MQTT** → bấm **🔌 Kiểm tra kết nối**.
3. Chấm **MQTT** trên thanh trên cùng phải chuyển **xanh**. Nếu đỏ → xem bảng lỗi ở Phần cuối.
4. Mục **Gửi thử một lệnh**: nhập topic (ví dụ `zigbee2mqtt/den_bep/set`) và payload `{"state":"ON"}` → bấm **📤 Gửi**.
5. Mục giám sát bên dưới sẽ hiện tin nhắn vừa gửi (và tin nhắn nhận được).

> Khi đã chắc chắn, **TẮT Dry-run** trên thanh trên cùng thì lệnh mới thực sự điều khiển đèn.

---

## Bước 7 — TEST 5: camera IP thật (RTSP)

Nếu bạn có camera IP:

1. Thẻ **🛠️ Cài đặt** → **＋ Thêm camera**.
2. Điền: **Tên**, **Nguồn** = `rtsp`, **URL** = `rtsp://user:matkhau@192.168.1.50:554/stream1`, **Transport** = `tcp`.
3. **💾 Lưu cài đặt** → sang **📹 Trực tiếp** chọn camera mới.

**Nếu không thấy hình**, thử theo thứ tự:
1. Đổi **Transport** sang `udp` (một số camera rẻ chỉ chạy UDP).
2. Kiểm tra lại **mật khẩu** trong URL (đây là lỗi phổ biến nhất).
3. Mở thử URL đó bằng **VLC** (VLC → File → Open Network) — nếu VLC cũng không xem được thì lỗi ở camera/mạng, không phải ở app.

---

## Bước 8 — Tắt app đúng cách

Tại cửa sổ **Terminal**: bấm **Ctrl + C** (giữ phím Control rồi bấm C).

**Bạn sẽ thấy** các dòng:
```
Nhận tín hiệu ... — đang dừng web server
Đang dừng hệ thống…
Đã dừng
```
App sẽ báo "offline" cho MQTT và lưu cấu hình. Cấu hình được giữ trong `config.yaml`
(mỗi lần lưu tự tạo bản sao lưu `config.yaml.bak`).

> Đừng tắt bằng cách đóng cửa sổ Terminal (Cmd + Q) — dùng Ctrl + C để app thoát sạch.

---

## Bước 9 — Cho chạy 24/7 (khi đã thử xong)

| Cách | Phù hợp với | Làm thế nào |
|---|---|---|
| **Đơn giản nhất** | Mac dùng hằng ngày | Bấm đúp `Khoi-dong.command` sau mỗi lần khởi động máy; bật **TẮT Dry-run** |
| **Docker** | Máy chủ/NAS/Pi5 | `docker compose up -d` (chỉ trên Linux/NAS — **Docker trên macOS không thấy được webcam**) |
| **systemd** | Máy Linux chạy 24/7 | Copy `vision-automation.service` vào `/etc/systemd/system/` (xem `README.md`) |

Trên macOS muốn tự chạy khi đăng nhập: **System Settings → General → Login Items** → thêm `Terminal`,
hoặc dùng app Automator chạy file `Khoi-dong.command`.

Trước khi chạy thật, kiểm tra 5 điều:
- ☐ **Dry-run đã TẮT** (không còn dải vàng trên đầu trang)
- ☐ Đã thử **Mô phỏng** và 3 lệnh BẬT/MỜ/TẮT ra đúng thứ tự
- ☐ **MQTT** xanh, đã **Gửi thử** thành công 1 lệnh tới đúng đèn
- ☐ Đã đứng thử trước camera: vào/ra vùng 5 lần, **đèn không nhấp nháy** (nếu nhấp nháy: tăng K-of-N và exit grace)
- ☐ Đã kiểm tra **mất mạng**: rút mạng camera 10 giây → app tự kết nối lại, không cần khởi động lại

---

## Lỗi thường gặp — và cách sửa

| Hiện tượng | Nguyên nhân | Cách sửa |
|---|---|---|
| Hình **đen thui**, không có ảnh | macOS chưa cấp quyền Camera | System Settings → Privacy & Security → **Camera** → bật cho **Terminal** → tắt app, mở lại |
| Báo "đang được dùng bởi ứng dụng khác" (device busy) | Zoom/Meet/FaceTime đang mở camera | Thoát ứng dụng đó rồi bấm **↻ Kết nối lại** |
| "Không kết nối được RTSP: sai mật khẩu?..." | Sai URL/mật khẩu, hoặc camera chặn TCP | Đổi Transport `tcp`→`udp`; thử URL bằng VLC |
| Camera RTSP **đã bật lại mà vẫn không lên hình** | Thường **không phải lỗi app**: máy phát không mở cổng ra mạng | Tab **⚙️ Cài đặt** → bấm **🔌 Kiểm tra** ở dòng camera đó. Nếu báo *"Không tới được `<ip>:<cổng>`"*: trên **máy phát** kiểm tra dịch vụ đang chạy + `netstat -ano \| findstr 8554`, mở cổng trong tường lửa, và hai máy phải **cùng một mạng LAN** |
| Bấm nút / công tắc trong **Cài đặt** mà không có phản ứng | (đã sửa) trình duyệt giữ `app.js` cũ, hoặc panel bị vẽ lại đúng lúc đang bấm | Nhấn **Ctrl/Cmd + Shift + R** để tải lại giao diện mới |
| Chấm **MQTT đỏ** | Broker chưa chạy / sai host / sai mật khẩu | Thẻ **🔌 MQTT** → **Kiểm tra kết nối**; đọc thông báo lỗi tiếng Việt hiện ra |
| Đèn **nhấp nháy** liên tục | Nhận diện chập chờn | Tăng **K-of-N** = 3/5, **exit grace** = 4–5 giây, tăng **độ tin cậy** lên 0.5–0.6 |
| Đèn **bật rồi không tắt** | Kịch bản chưa có bước "tắt khi vắng" | Thẻ **⚙️ Kịch bản** → chọn kịch bản → thêm bước vào nhánh **Vắng người** |
| Nhận diện **chậm / CPU cao** | máy yếu hoặc imgsz lớn | **Cài đặt** → giảm `detect_fps` xuống 2–3, giảm `imgsz` về 416, bật **crop_to_zones** |
| App **tự dừng** | xem log | Mở file `logs/vision-automation.log` (dòng cuối là lý do), hoặc gửi file đó cho người hỗ trợ |
| Trình duyệt báo **không truy cập được 127.0.0.1:8080** | App chưa chạy xong / đã tắt | Xem cửa sổ Terminal còn chạy không; đợi thêm 10 giây rồi tải lại trang |
| Ô **Trễ hình** hiện **vàng (≥ 1 giây)**, hình "đi sau" thực tế | Đường truyền đang đệm khung | **Cài đặt** → `web.stream_width` 960 → 640, hoặc dùng **sub-stream** của camera (640×480), hoặc đổi Transport sang `udp` |
| Băng đỏ **"Luồng treo quá 10s"** lặp lại đều đặn | Camera/NVR tự ngắt luồng, hoặc mạng chập chờn | App **tự nối lại** (không cần làm gì). Nếu lặp mãi: kiểm tra dây/Wi-Fi của camera, hạ độ phân giải — xem `README.md` §16.1 |
| **FPS suy luận 0.0** + vùng ghi **"Đang xác định…"** | Camera vừa thêm từ giao diện mà luồng suy luận chưa chạy | Khởi động lại app; nếu vẫn vậy, tắt rồi bật lại camera trong **Cài đặt** |

Nút **?** ở góc trên phải giao diện luôn mở lại bảng trợ giúp này.

---

## File nào ở đâu (chỉ cần biết khi cần)

| File | Nội dung |
|---|---|
| `config.yaml` | Toàn bộ cấu hình (tự tạo lần đầu). **Sửa qua giao diện**, không sửa tay |
| `config.yaml.bak` | Bản sao lưu tự động của lần lưu trước |
| `logs/vision-automation.log` | Nhật ký chi tiết — gửi file này khi cần hỗ trợ |
| `snapshots/` | Ảnh chụp **chỉ khi bạn bấm 📷 Chụp ảnh** (app không tự lưu ảnh — riêng tư) |
| `Khoi-dong.command` | File bấm đúp để khởi động |
| `README.md` | Tài liệu kỹ thuật đầy đủ (khi cần đọc sâu) |

**Sao lưu:** chỉ cần copy `config.yaml`. **Khôi phục:** copy lại file đó rồi khởi động lại app.
