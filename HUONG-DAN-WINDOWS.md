# Hướng dẫn chạy Vision Automation trên Windows (native)

Tài liệu này dành cho **máy Windows** — nơi bạn sẽ chạy 24/7. Chương trình chạy
**trực tiếp** (native) trên Windows, không cần Docker, không cần Linux, không cần
máy ảo. Mọi thứ cấu hình được trên Web UI, không phải sửa file.

> macOS chỉ dùng để viết/test. Lý do: Docker Desktop trên máy Mac **không** truy
> cập được webcam và gặp khó với camera LAN; Windows native thì không có vấn đề đó.

---

## 1. Chuẩn bị (làm 1 lần)

### 1.1 Cài Python 3.12

1. Vào https://www.python.org/downloads/windows/ → tải **Python 3.12.x** (bản
   64-bit "Windows installer").
2. Khi cài, **tích ô “Add python.exe to PATH”** ở màn hình đầu tiên, rồi bấm
   *Install Now*.
3. Kiểm tra: bấm `Win + R`, gõ `cmd`, Enter, rồi gõ:

   ```bat
   python --version
   ```

   Phải hiện `Python 3.12.x`. Nếu báo *“python is not recognized”* → cài lại và
   nhớ tích “Add to PATH”.

> Vì sao 3.12? `torch`/`ultralytics` (bộ nhận diện AI) chưa có bản build cho
> Python 3.14 — dùng 3.12 là an toàn nhất.

### 1.2 Copy thư mục chương trình

Copy thư mục `vision_automation` sang máy Windows, ví dụ `C:\vision-automation`.
Nên đặt ở đường dẫn **không có dấu cách, không có tiếng Việt** (tránh lỗi lạ của
một số thư viện).

### 1.3 Cài thư viện

Cách dễ nhất: **bấm đúp `Khoi-dong.bat`** — lần chạy đầu nó tự tạo môi trường
riêng `.venv`, tự cài thư viện (5–15 phút, cần Internet) rồi tự mở trình duyệt.

Nếu muốn tự cài bằng tay (mở `cmd` tại thư mục chương trình):

```bat
python -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install --index-url https://download.pytorch.org/whl/cpu torch torchvision
python -m pip install -r requirements.txt
```

Dòng có `--index-url .../cpu` giúp tải bản AI **CPU ~200 MB** thay vì bản CUDA
~2,5 GB. Máy có card NVIDIA và muốn dùng GPU thì xem mục 8.

---

## 2. Kiểm tra môi trường (nên làm trước)

Bấm đúp **`Kiem-tra-Windows.bat`** — nó in ra một bảng: phiên bản Python, các thư
viện (OpenCV/torch/…), **tên webcam tìm thấy**, quyền camera, ffmpeg/mediamtx,
cấu hình hiện tại và **kết nối thử MQTT**. Cuối bảng là mục *“CẦN XỬ LÝ”* với cách
sửa cụ thể. Không mở camera nên **đèn webcam không bật**.

Tương đương dòng lệnh: `python run.py --doctor`.

---

## 3. Chạy chương trình

Bấm đúp **`Khoi-dong.bat`**. Cửa sổ đen hiện log, sau ~6 giây trình duyệt tự mở
`http://127.0.0.1:8080`.

Nếu chưa thấy trình duyệt tự mở, tự mở và gõ địa chỉ trên.

Lần chạy đầu sẽ có **trình hướng dẫn (wizard)** 3 bước:

1. **Camera**: dán URL RTSP của camera IP (ví dụ
   `rtsp://user:matkhau@192.168.1.50:554/stream1`) → bấm *Kiểm tra kết nối* → thấy
   hình là đúng.
2. **MQTT**: nhập IP broker (Home Assistant / Zigbee2MQTT / Mosquitto), user/mật
   khẩu → *Kiểm tra*.
3. **Kịch bản mẫu** “Đèn bếp”: bật đèn khi có người, tắt sau khi ra 2 phút.

### Tắt chương trình

* Bấm **`Dung-lai.bat`** (cách an toàn nhất: gửi yêu cầu dừng qua API → chương
  trình gửi MQTT “offline”, đóng camera, lưu `config.yaml`).
* Hoặc bấm `Ctrl + C` trong cửa sổ đen rồi đóng cửa sổ.
* Đừng tắt bằng Task Manager (mất trạng thái MQTT “offline”).

---

## 4. Dùng từ điện thoại

1. Điện thoại và máy tính **cùng một mạng Wi-Fi**.
2. Lúc khởi động, cửa sổ đen in 2 dòng:

   ```
   ➜  Web UI (máy này): http://127.0.0.1:8080
   ➜  Điện thoại/máy khác: http://192.168.1.23:8080
   ```

3. Mở địa chỉ thứ hai trên điện thoại.
4. Lần đầu Windows sẽ hỏi **tường lửa** → chọn **Allow access** (Private
   networks). Nếu đã lỡ bấm Cancel: mở *Windows Defender Firewall → Allow an app
   through the firewall → Change settings → tick Python*.

Nên bật đăng nhập khi mở ra mạng LAN: tab **Cài đặt → Bảo mật** → bật *Yêu cầu
đăng nhập*, đặt mật khẩu.

---

## 5. Tự chạy khi khởi động máy (24/7)

### Cách A — Task Scheduler (không cần cài thêm gì)

Mở `cmd` **với quyền Administrator** rồi chạy (sửa đường dẫn cho đúng máy bạn):

```bat
schtasks /create /tn "VisionAutomation" /sc onstart /ru SYSTEM /rl highest ^
 /tr "C:\vision-automation\.venv\Scripts\pythonw.exe C:\vision-automation\run.py --config C:\vision-automation\config.yaml --no-webcam"

schtasks /run /tn "VisionAutomation"
```

Ghi chú:

* Dùng **`run.py`** (không phải `-m app`): Task Scheduler khởi chạy ở
  `C:\Windows\System32`, nên `python -m app` sẽ không tìm thấy thư viện. `run.py`
  tự thêm thư mục chương trình vào đường dẫn nên chạy được từ bất kỳ đâu.
* `pythonw.exe` = chạy **không hiện cửa sổ đen**.
* `--no-webcam` = không tự thêm camera test của laptop (máy chạy 24/7 chỉ dùng
  camera IP). Bỏ cờ này nếu muốn dùng webcam.
* Muốn chạy cả khi chưa đăng nhập → chọn `/ru SYSTEM` như trên.
* Dừng: `schtasks /end /tn "VisionAutomation"` hoặc chạy `Dung-lai.bat`.
* Chương trình tự `chdir` về thư mục chứa `config.yaml`, nên log/snapshot/model
  luôn nằm cạnh config dù Task Scheduler khởi chạy từ `C:\Windows\System32`.

### Cách B — NSSM (chạy như service Windows)

1. Tải NSSM tại https://nssm.cc/download, giải nén, chạy `nssm.exe install VisionAutomation`.
2. *Path*: `C:\vision-automation\.venv\Scripts\python.exe`
3. *Startup directory*: `C:\vision-automation`
4. *Arguments*: `C:\vision-automation\run.py --config C:\vision-automation\config.yaml --no-webcam`
5. Tab *I/O*: trỏ *stdout/stderr* vào `C:\vision-automation\logs\service.log`.
6. *Install service*. Dừng service: `nssm stop VisionAutomation` (NSSM gửi
   `CTRL_BREAK`; chương trình đã bắt tín hiệu này nên vẫn tắt an toàn).

---

## 6. Kiểm tra nhanh sau khi cài (5 phút)

| Bước | Làm gì | Kết quả đúng |
|---|---|---|
| 1 | Tab **Trực tiếp** | Có hình camera, badge xanh `MQTT` |
| 2 | Vẽ vùng | Tab **Vùng** → kéo 1 hình chữ nhật quanh chỗ hay có người → *Lưu* |
| 3 | Bật **Dry-run** (công tắc trên header) | Viền vàng “DRY-RUN” hiện rõ |
| 4 | Đứng vào vùng | Ô vùng chuyển xanh, log có `zone_enter`, lệnh ghi `[DRY-RUN]` |
| 5 | Tab **Mô phỏng** → *Có người* | Kịch bản chạy không cần đứng trước camera |
| 6 | Tắt Dry-run, thử lại | Đèn/broker nhận lệnh JSON thật |
| 7 | Tab **MQTT** → *Kiểm tra* | “Kết nối OK”, monitor hiện topic gửi/nhận |

Không thấy hình? Xem mục 7.

---

## 7. Lỗi thường gặp và cách sửa

| Hiện tượng | Nguyên nhân | Cách sửa |
|---|---|---|
| `python is not recognized` | Chưa có Python trong PATH | Cài lại Python 3.12, tích “Add python.exe to PATH”, mở `cmd` mới |
| `Khoi-dong.bat` báo không tạo được `.venv` | Thiếu quyền ghi / Python lỗi | Chuyển thư mục về `C:\vision-automation`, chạy `cmd` bằng quyền Administrator |
| `pip install` dừng ở `torch` | Mạng chậm / hết dung lượng | Chạy lại (pip tải tiếp), hoặc cài bản CPU như mục 1.3 |
| Không tải được `yolo11n.pt` | Máy không có Internet | Tải trên máy khác rồi copy `yolo11n.pt` vào thư mục chương trình |
| Hình webcam **đen** | Windows chặn quyền Camera | Settings → Privacy & security → Camera → bật *Camera access* + *Let desktop apps access your camera*, khởi động lại app |
| Webcam báo **đang bận** | Zoom/Teams/Chrome đang giữ camera | Đóng các app đó; chỉ **một** tiến trình được mở webcam (loopback cũng tính) |
| Không kết nối được **RTSP** | Sai mật khẩu/URL, hoặc camera chỉ nhận UDP | Sửa URL; tab Camera → đổi *transport* `tcp` → `udp` |
| MQTT `connected=false` | Broker không chạy / sai cổng / firewall | Kiểm tra broker, thử cổng 1883, mở firewall cho cổng đó |
| Cổng 8080 đã dùng | App khác chiếm cổng | Chạy `python -m app --port 8090` |
| Đèn **nhấp nháy** | Nhận diện chập chờn | Tăng *K-of-N* (3/5) và *thời gian chờ rời vùng* (exit grace) trong cấu hình vùng |
| Máy chậm / CPU 100% | Model lớn, `detect_fps` cao | Tab Cài đặt → `imgsz: 416`, `detect_fps: 2`, bật *motion gating* |
| Log đầy ổ | File log lớn | Log đã tự xoay vòng (`max_bytes`, `backup_count`); giảm `backup_count` nếu cần |

Xem log chi tiết: `logs\vision-automation.log` (cùng thư mục `config.yaml`).

---

## 8. Dùng card đồ họa NVIDIA (tuỳ chọn, chỉ khi cần)

Mặc định chương trình chạy **CPU** — với `yolo11n` ở 3–5 fps thì CPU là đủ và
tiết kiệm điện (máy mini-PC/NAS chạy 24/7).

Nếu có GPU và muốn nhanh hơn:

```bat
.venv\Scripts\activate
python -m pip uninstall -y torch torchvision
python -m pip install --index-url https://download.pytorch.org/whl/cu124 torch torchvision
```

Rồi trong tab **Cài đặt → Nhận diện** đặt `device: cuda` (hoặc để `auto`). Vào lại
tab Trực tiếp sẽ thấy dòng “thiết bị: cuda:0” + số ms suy luận.

---

## 9. Docker trên Windows (không dùng cho webcam)

`Dockerfile` + `docker-compose.yml` vẫn dùng được trên Windows, nhưng:

* Container **không** thấy webcam của laptop → chỉ dùng cho **camera IP (RTSP)**.
* Không thấy broker `127.0.0.1` → cần `network_mode: host` (Linux) hoặc trỏ
  `mqtt.host` sang IP thật của máy chạy broker.

Vì bạn chạy trên chính máy Windows → chạy **native** như mục 3 là đơn giản và ổn
định nhất.

---

## 10. Lệnh hữu ích

```bat
python run.py --help                          :: xem mọi tham số (hoặc python -m app --help)
python -m app --config config.yaml            :: chạy bình thường
python -m app --dry-run --with-webcam         :: chạy thử, thêm camera test
python -m app --no-web                        :: không mở web UI (chỉ MQTT)
python -m app --no-detector                   :: không nạp AI (kiểm tra camera/MQTT)
python -m app --port 8090                     :: đổi cổng web
```

Dừng an toàn từ xa (script/điện thoại):

```bat
curl -X POST http://127.0.0.1:8080/api/system/shutdown
```

(nếu bật đăng nhập: thêm `?token=<token trong Cài đặt>`).
