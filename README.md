# Vision Automation — Camera thông minh điều khiển thiết bị

Ứng dụng Python đọc luồng camera (RTSP/webcam/video/ảnh), **phát hiện đối tượng
(mặc định: người)** trong **những vùng bạn tự vẽ trên ảnh**, rồi chạy **kịch bản**
và **gửi lệnh JSON qua MQTT** (Zigbee2MQTT, Home Assistant, Tasmota/ESPHome…).

Thiết kế cho chạy **24/7** trên mini-PC / NAS / Raspberry Pi 5 / PC, **ưu tiên CPU**,
**hoàn toàn cục bộ** (không gửi ảnh lên cloud), và **không nhấp nháy đèn**
(debounce + trễ rời vùng + chỉ gửi khi trạng thái thật sự đổi).

> **Trên macOS (Apple Silicon) phải chạy NATIVE, không dùng Docker.**
> Docker Desktop chạy trong máy ảo Linux nên **không truy cập được webcam của
> laptop** và thường không tới được camera IP trong LAN. Docker chỉ dùng cho
> Linux (NAS/mini-PC/Pi) — xem mục [Docker](#8-docker-chạy-trên-nas-mini-pc--raspberry-pi).

---

> 🟢 **Chưa rành kỹ thuật?** Đọc [`HUONG-DAN-NHANH.md`](HUONG-DAN-NHANH.md) trước — hướng dẫn
> từng bước để bật và kiểm thử (bấm đúp **`Khoi-dong.command`**, không cần gõ lệnh), kèm bảng
> lỗi thường gặp. Tài liệu dưới đây là bản kỹ thuật đầy đủ.

---

## Mục lục

1. [Tính năng](#1-tính-năng)
2. [Yêu cầu hệ thống](#2-yêu-cầu-hệ-thống)
3. [Cài đặt trên macOS (Apple Silicon)](#3-cài-đặt-trên-macos-apple-silicon)
4. [Cài đặt trên **Windows** (máy chạy 24/7)](#4-cài-đặt-trên-windows-máy-chạy-247)
5. [Cài đặt trên Raspberry Pi / mini-PC / NAS](#5-cài-đặt-trên-raspberry-pi--mini-pc--nas)
6. [Chạy lần đầu & trình hướng dẫn](#6-chạy-lần-đầu--trình-hướng-dẫn)
7. [Web UI — hướng dẫn từng tab](#7-web-ui--hướng-dẫn-từng-tab)
8. [Cấu hình `config.yaml`](#8-cấu-hình-configyaml)
9. [Docker (chạy trên NAS / mini-PC / Raspberry Pi)](#9-docker-chạy-trên-nas-mini-pc--raspberry-pi)
10. [systemd — chạy 24/7 trên Linux](#10-systemd--chạy-247-trên-linux)
11. [Camera test bằng webcam (macOS)](#11-camera-test-bằng-webcam-macos)
12. [Hiệu năng & tinh chỉnh](#12-hiệu-năng--tinh-chỉnh)
13. [Độ ổn định & an toàn (fail-safe)](#13-độ-ổn-định--an-toàn-fail-safe)
14. [MQTT: topic, payload, Home Assistant](#14-mqtt-topic-payload-home-assistant)
15. [Quyền riêng tư](#15-quyền-riêng-tư)
16. [Xử lý sự cố](#16-xử-lý-sự-cố)
17. [Kiểm thử](#17-kiểm-thử)
18. [Kiến trúc mã nguồn](#18-kiến-trúc-mã-nguồn)
    - [18.1 Vòng sửa “nút Cài đặt không ăn” + “RTSP không kết nối”](#181-vòng-sửa-nút-trong-cài-đặt-không-ăn--rtsp-không-kết-nối-đo-thật)

---

## 1. Tính năng

| Nhóm | Nội dung |
|---|---|
| Nhận diện | Ultralytics YOLO (`yolo11n.pt`, 80 lớp COCO), đổi model/imgsz/thiết bị/ngưỡng/`detect_fps` ngay trên UI |
| Vùng | Vẽ **trực tiếp trên ảnh**: click thêm điểm, nhấn đúp để đóng, kéo điểm, kéo cả vùng, xoá điểm, Undo/Redo, hình chữ nhật nhanh; lưu **toạ độ chuẩn hoá 0..1** |
| Đếm đúng | **5 cách xác định trong vùng**: `bottom_center` (điểm chân, mặc định), `center`, `any_overlap`, `fully_inside` (toàn bộ đối tượng), `inside_ratio` (≥ X% đối tượng, thanh trượt trên UI); đếm theo từng lớp; **mọi vùng dùng chung một kết quả suy luận** và mỗi vùng hiện lý do khi trống |
| Chống nhấp nháy | Bỏ phiếu **3/5 khung** + `exit_grace_s` (phải vắng liên tục mới coi là trống) |
| Kịch bản | Trình tạo bằng form: chọn vùng → điều kiện → thêm bước (hành động + chờ) trên **timeline trực quan**; tab YAML nâng cao có kiểm tra |
| Điều kiện | `enter`, `vacant`, `count_ge`, `count_le`, `dwell`, `class_present` |
| MQTT | paho-mqtt ≥ 2.0; LWT + `online/offline`, QoS/retain riêng từng bước, **chỉ gửi khi trạng thái đổi**, hàng chờ có giới hạn, khi mất mạng **chỉ gửi trạng thái mới nhất** của mỗi kịch bản |
| Payload | Mẫu `{{count}} {{zone}} {{camera}} {{timestamp}} {{class_counts}}`; presets Zigbee2MQTT / HA / Tasmota-ESPHome / Raw JSON |
| Trạng thái vùng | Mỗi vùng phát retained `vision/<camera>/<vùng>/state` |
| Web UI | FastAPI + HTML/JS thuần (**không cần npm/build**), tiếng Việt, dùng tốt trên điện thoại, MJPEG + WebSocket |
| Mô phỏng | Giả lập “có người / trống” trong vùng, tăng tốc thời gian **x10** để thử kịch bản |
| Dry-run | Công tắc trên header, hiển thị rõ ràng — không gửi lệnh thật |
| An toàn | 24/7: reconnect camera & broker, phát hiện luồng treo, hàng chờ có giới hạn, log tự xoay vòng, không rò rỉ bộ nhớ |
| Riêng tư | Chỉ xử lý cục bộ; **không lưu ảnh** trừ khi bạn bấm “Chụp ảnh” |
| macOS | Webcam qua AVFoundation, xin quyền đúng cách, **chỉ mở khi có người xem** (đèn webcam tắt khi không dùng), tuỳ chọn phát webcam qua RTSP loopback |

---

## 2. Yêu cầu hệ thống

- **Hệ điều hành**: **Windows 10/11** (khuyến nghị cho máy chạy 24/7), macOS Apple
  Silicon (dev/test), Linux (mini-PC/NAS/Pi5). Cùng một mã nguồn: phần khác biệt OS
  nằm trong `app/platform_compat.py`.
- **Python 3.11 hoặc 3.12** (khuyến nghị **3.12**).
  - Chưa nên dùng 3.13/3.14: wheel `torch` cho macOS ARM và một số gói phụ thuộc
    chưa ổn định. Nếu máy chỉ có 3.14, hãy tạo venv bằng 3.12:
    ```bash
    # macOS (Homebrew)
    brew install python@3.12
    # hoặc dùng uv (nhanh, tự tải Python 3.12)
    uv venv --python 3.12 .venv
    ```
- RAM: **≥ 2 GB** trống (model nano + OpenCV). CPU: bất kỳ 4 lõi trở lên là đủ.
- Camera: RTSP (IP camera) hoặc webcam. Không cần GPU — GPU (CUDA/MPS) chỉ để nhanh hơn.
- MQTT broker: Mosquitto / Zigbee2MQTT / Home Assistant (bất kỳ broker MQTT 3.1.1).

---

## 3. Cài đặt trên macOS (Apple Silicon)

> **Chạy native trên macOS** (không Docker) — Docker Desktop không thấy webcam
> và rất hay lỗi với camera IP trong LAN.

```bash
cd "Camera smarthome/vision_automation"

# 1) Tạo môi trường ảo bằng Python 3.12
uv venv --python 3.12 .venv            # hoặc: python3.12 -m venv .venv
source .venv/bin/activate

# 2) Cài thư viện
uv pip install --python .venv/bin/python -r requirements.txt
# hoặc: pip install -r requirements.txt

# 3) Chạy
python -m app
```

Mở **http://127.0.0.1:8080** trên máy, hoặc `http://<ip-lan>:8080` từ điện thoại.

### Apple Silicon (M1/M2/M3/M4)

- `detector.device: auto` sẽ chọn **MPS** (GPU Apple) rồi mới tới CPU.
- Nếu MPS lỗi (một số phép toán chưa hỗ trợ), app **tự chuyển sang CPU**, ghi log
  và hiển thị thiết bị đang dùng ở tab Trực tiếp.
- Lúc khởi động app chạy **warmup** rồi báo `inference ms` + thiết bị trong UI.
  Trên M1 với `yolo11n` + `imgsz=640`: khoảng **25–60 ms/khung** ở MPS.

### Quyền camera trên macOS

Lần đầu mở webcam, macOS sẽ hỏi quyền. Nếu bạn từng bấm “Không cho”:

1. **System Settings → Privacy & Security → Camera**
2. Bật cho **Terminal** (hoặc VS Code / iTerm — ứng dụng bạn dùng để chạy app)
3. **Thoát và mở lại** Terminal, rồi chạy lại app
4. Trong app: tab **Trực tiếp → Kết nối lại**

App xin quyền **sớm, trên luồng chính** (yêu cầu của AVFoundation) và đặt
`OPENCV_AVFOUNDATION_SKIP_AUTH=1` để các luồng camera không cố hỏi lại (OpenCV
không thể mở hộp thoại từ luồng phụ).

Nếu camera bị **ứng dụng khác giữ** (Zoom, FaceTime, Google Meet…), hãy đóng ứng
dụng đó — macOS chỉ cho **một** tiến trình mở webcam tại một thời điểm.

---

## 4. Cài đặt trên **Windows** (máy chạy 24/7)

> Đây là môi trường chạy production được hỗ trợ chính thức: **native Windows +
> Python**, không Docker, không WSL. Toàn bộ phần OS-specific (DirectShow,
> Media Foundation, quyền camera, ffmpeg, tín hiệu dừng) nằm trong
> `app/platform_compat.py`, nên cùng một mã nguồn chạy được trên Windows / macOS /
> Linux — chỉ khác backend camera và cách đóng gói dịch vụ.

### 4.1 Cài nhanh (khuyến nghị)

```bat
:: 1. Cai Python 3.12 (tich "Add python.exe to PATH") tu python.org
:: 2. Copy thu muc vision_automation sang vi du C:\vision-automation
:: 3. Bam dup:
Khoi-dong.bat
```

`Khoi-dong.bat` làm hết: kiểm tra Python → tạo `.venv` → cài thư viện (torch bản
CPU ~200 MB) → chạy `run.py` → tự mở `http://127.0.0.1:8080`. Tắt bằng
**`Dung-lai.bat`** (gửi `POST /api/system/shutdown`: báo MQTT *offline*, đóng
camera, lưu `config.yaml`).

### 4.2 Cài bằng tay

```bat
python -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install --index-url https://download.pytorch.org/whl/cpu torch torchvision
python -m pip install -r requirements.txt
python run.py --config config.yaml --with-webcam
```

> Dùng **`run.py`** thay cho `python -m app` khi thư mục hiện tại không phải thư
> mục chương trình (Task Scheduler / NSSM / shortcut): `run.py` tự thêm thư mục
> của nó vào `sys.path`, còn `python -m app` chỉ chạy được khi đứng đúng thư mục.

### 4.3 Windows khác macOS ở đâu?

| Việc | macOS | Windows |
|---|---|---|
| Backend webcam | AVFoundation (`cv2.CAP_AVFOUNDATION`) | DirectShow (`CAP_DSHOW`) → Media Foundation (`CAP_MSMF`) → mặc định |
| Tên webcam | pyobjc `AVCaptureDevice` / `system_profiler` | `pygrabber` (DirectShow); thiếu thì dùng `Camera 0/1/2` |
| Quyền camera | Hộp thoại hệ thống, phải hỏi ở **main thread** | Không có prompt: `not_required`; bị chặn thì sửa Settings → Privacy → Camera |
| ffmpeg loopback | `-f avfoundation -pixel_format nv12 -c:v h264_videotoolbox` | `-f dshow -rtbufsize 100M -i video=<tên> -c:v libx264` |
| ffmpeg/mediamtx | `brew install ffmpeg mediamtx` | `winget install Gyan.FFmpeg` + tải `mediamtx.exe` từ GitHub, hoặc trỏ `testing.ffmpeg_bin`/`testing.mediamtx_bin` |
| Dừng an toàn | `SIGTERM`/`Ctrl+C` | `Ctrl+C`, `CTRL_BREAK`, `Dung-lai.bat`, hoặc `POST /api/system/shutdown` |
| Chạy 24/7 | `launchd` / Terminal | Task Scheduler hoặc NSSM (service) |
| Docker | Không dùng (Docker Desktop không thấy webcam/LAN camera) | Chỉ dùng cho camera RTSP — container **không** thấy webcam laptop |

### 4.4 Chạy 24/7 trên Windows

```bat
schtasks /create /tn "VisionAutomation" /sc onstart /ru SYSTEM /rl highest ^
 /tr "C:\vision-automation\.venv\Scripts\pythonw.exe C:\vision-automation\run.py --config C:\vision-automation\config.yaml --no-webcam"
```

* `pythonw.exe`: không hiện cửa sổ đen. `--no-webcam`: máy 24/7 chỉ dùng camera IP.
* NSSM: *Path* = `.venv\Scripts\python.exe`, *Startup directory* = thư mục dự án,
  *Arguments* = `run.py --config config.yaml --no-webcam`.
* Chương trình tự `chdir` về thư mục chứa `config.yaml` (`app/main.py:prepare_workdir`),
  nên `logs/`, `snapshots/`, `.yolo/`, `yolo11n.pt` luôn nằm cạnh config dù dịch vụ
  khởi chạy từ `C:\Windows\System32`.
* Console Windows dùng `cp1258/cp437` → `app/main.py:configure_stdio()` chuyển
  stdout/stderr sang UTF-8 (`errors="replace"`) để tiếng Việt không làm chết tiến trình
  khi log bị chuyển hướng ra file.

### 4.5 Windows: lỗi thường gặp

| Hiện tượng | Cách sửa |
|---|---|
| `python is not recognized` | Cài lại Python 3.12, tích “Add python.exe to PATH”, mở `cmd` mới |
| Hình webcam đen | Settings → Privacy & security → Camera → bật “Camera access” + “Let desktop apps access your camera”, khởi động lại app |
| “camera đang bận” | Chỉ **một** tiến trình được mở webcam: đóng Zoom/Teams/Chrome (loopback cũng giữ webcam) |
| `pip install torch` rất chậm | Dùng `--index-url https://download.pytorch.org/whl/cpu` (bản CPU nhỏ hơn ~10×) |
| Muốn dùng GPU NVIDIA | `pip install --index-url https://download.pytorch.org/whl/cu124 torch torchvision`, rồi `detector.device: cuda` |
| Không tải được `yolo11n.pt` | Tải trên máy khác rồi copy `yolo11n.pt` vào cạnh `config.yaml` |
| Cổng 8080 bận | `python run.py --port 8090` |
| Muốn biết máy thiếu gì | Bấm đúp `Kiem-tra-Windows.bat` (= `python run.py --doctor`) |

📄 Hướng dẫn chi tiết từng bước (kèm hình dung cho người mới):
**[`HUONG-DAN-WINDOWS.md`](HUONG-DAN-WINDOWS.md)**.

---

## 5. Cài đặt trên Raspberry Pi / mini-PC / NAS

```bash
# Ubuntu/Debian (Pi 5, mini-PC, NAS có shell)
sudo apt update
sudo apt install -y python3.12 python3.12-venv python3-pip ffmpeg libgl1 libglib2.0-0

sudo mkdir -p /opt/vision-automation && sudo chown "$USER" /opt/vision-automation
cp -r vision_automation/* /opt/vision-automation/
cd /opt/vision-automation

python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# chạy thử
python -m app
```

Rồi cài **systemd** để chạy 24/7: xem [mục 10](#10-systemd--chạy-247-trên-linux).

Trên Pi 5 nên giảm tải: `detector.imgsz: 480`, `detect_fps: 3`,
`detector.motion_gating: true`, `web.stream_width: 640`.

### Synology / QNAP (Container Manager)

Dùng Docker (mục 9). NAS không có webcam — chỉ dùng camera IP (RTSP).

---

## 6. Chạy lần đầu & trình hướng dẫn

```bash
python -m app                 # tạo config.yaml nếu chưa có
```

Các tham số dòng lệnh:

| Tham số | Ý nghĩa |
|---|---|
| `--config <path>` | Đường dẫn `config.yaml` (mặc định `./config.yaml`) |
| `--port 8080` | Đổi cổng Web UI |
| `--host 0.0.0.0` | Đổi địa chỉ lắng nghe |
| `--dry-run` | Chạy thử: **không gửi lệnh MQTT thật** |
| `--with-webcam` | Tự thêm camera test bằng webcam laptop (không đụng camera RTSP) |
| `--no-webcam` | Không tự thêm camera test |
| `--no-detector` | Không nạp model (chỉ kiểm tra camera/MQTT/UI) |
| `--no-web` | Chạy không mở Web UI (vẫn gửi MQTT) |
| `--log-level DEBUG` | Log chi tiết |

**Trình hướng dẫn (wizard)** tự mở khi `setup_completed: false`:

1. Nhập **URL RTSP** → app thêm camera và hiện ảnh xem trước để bạn xác nhận.
2. Nhập **MQTT broker** (host/port/user/password) → nút “Kiểm tra MQTT”.
3. Tạo kịch bản mẫu **“Đèn bếp”** (bật khi có người, mờ sau 30 s, tắt sau 2 phút).

Bỏ qua bước nào cũng được — mọi thứ sửa lại được sau trong UI.

---

## 7. Web UI — hướng dẫn từng tab

### 📹 Trực tiếp

- Ảnh MJPEG thời gian thực, có **khung nhận diện + nhãn + độ tin cậy** và
  **vùng bán trong suốt** (xanh = có người, xám = trống).
- Thống kê: FPS camera, FPS suy luận, thời gian suy luận, thiết bị, số vùng đang có người,
  **Trễ hình** (tuổi của khung đang hiển thị — rê chuột để xem thêm thời gian đọc khung
  và fps nguồn báo).
- Header: 3 đèn trạng thái (Camera / MQTT / Model), công tắc **Dry-run**, thời gian hoạt động.
- Mất camera: khung hình **đứng ở ảnh cuối** + cảnh báo đỏ, UI **không bao giờ treo**.
- Hình bị trễ (đường truyền đệm khung): cảnh báo vàng `Hình đang trễ X.X s` —
  xem [§16](#16-xử-lý-sự-cố) để biết cách khắc phục.
- **Bảng chẩn đoán** (ngay dưới phần trạng thái vùng): FPS camera, fps nguồn báo,
  **tuổi khung hình (ms)**, thời gian đọc khung, FPS/thời gian suy luận, lần suy luận
  gần nhất cách đây bao lâu, **số lỗi suy luận**, số lần mở lại kết nối, trạng thái
  từng vùng (kèm "chờ xác nhận vắng" và "đang mô phỏng"), MQTT đã gửi/nhận và số lệnh
  đang xếp hàng.
- **Giải thích quyết định**: danh sách 30 quyết định gần nhất, mỗi dòng nói rõ
  *vì sao* có (hoặc không có) lệnh — ví dụ
  `Gửi “Giảm sáng” tới zigbee2mqtt/den_bep/set vì vùng “Khu bếp” đã trống đủ thời gian chờ rời vùng`.
  Thẻ kịch bản ở tab Mô phỏng/Nhật ký cũng có dòng 💡 giải thích trạng thái hiện tại.

### ⬛ Vùng phát hiện

1. Chọn camera (camera test có nhãn **TEST**). App **nhớ camera đang chọn** (lưu trong trình duyệt),
   nên mở lại UI hay khởi động lại app vẫn vào đúng camera đó — không tự nhảy về camera đầu danh sách.
0. **Ảnh để vẽ là ẢNH TĨNH** (app tự "đóng băng" khung hình khi mở tab): vẽ trên hình
   đang chạy thì người/đồ vật di chuyển làm mọi thao tác kéo-thả đều trượt. Bấm
   **📷 Chụp lại** để lấy khung mới, **▶ Trực tiếp** để xem luồng thật (nhãn cạnh nút cho
   biết đang ở chế độ nào). Ảnh tĩnh cũng giúp webcam nhả máy (đèn tắt) khi không cần.
   Đang sửa mà rời tab/đổi camera khi **chưa lưu** thì app **hỏi trước** (biểu tượng • trên tab).
2. Bấm **Vẽ đa giác** rồi click từng điểm trên ảnh; **nhấn đúp** hoặc **Hoàn tất vùng** để đóng.
   (Hoặc **Vẽ hình chữ nhật** rồi kéo chuột để tạo nhanh.)
3. **Con trỏ chuột cho biết bấm vào sẽ ra gì** (kèm nhãn nổi ngay cạnh con trỏ):

   | Rê chuột tới | Con trỏ | Điểm/cạnh | Kéo sẽ |
   |---|---|---|---|
   | **Điểm (góc)** | ✋ bàn tay | điểm đó **sáng vàng** | chỉ điểm đó chạy — 2 cạnh dính vào nó xoay theo, **các điểm khác đứng yên** |
   | **Giữa cạnh** (đa giác) | **+** | cạnh đó tô vàng | chèn **1 điểm mới** đúng trên cạnh rồi kéo điểm đó |
   | **Giữa cạnh** (hình chữ nhật) | ↕ / ↔ | cạnh đó tô vàng | dời riêng cạnh đó (đổi kích thước) |
   | **Giữa vùng** | ✥ di chuyển | vùng đang sửa | dời **cả vùng** |
   | Ngoài vùng | ✛ vẽ | – | bấm để thêm điểm khi đang vẽ vùng mới |

4. **Điểm của mọi vùng đều kéo được**: bấm trúng một **điểm** của vùng khác là app tự chuyển sang sửa
   vùng đó và kéo luôn điểm ấy. Bấm vào **bên trong** vùng khác thì **không** tự đổi vùng đang sửa
   (chỉ hiện nhắc chọn trong danh sách) — tránh cảnh "vùng nhảy qua nhảy lại" khi đang canh hình.
5. **Đổi kiểu vùng khi đang sửa**: ô **Kiểu vùng** (*Đa giác tự do / Hình chữ nhật / Toàn khung hình*).
   Kiểu bạn chọn được **lưu vào config** (`shape: rect|polygon`) nên app không tự đoán lại:
   * **Hình chữ nhật** — kéo **góc** = đổi chiều dài/rộng (2 góc kề chạy theo, luôn vuông góc, có kẹp
     để không bị lật); kéo **chấm giữa cạnh** = dời riêng cạnh đó. Không bao giờ tự chèn thêm điểm thứ 5.
   * **Đa giác tự do** — kéo điểm nào chỉ điểm đó chạy; kéo chấm giữa cạnh để thêm điểm.
6. **Undo/Redo** (`Ctrl/Cmd+Z`, `Ctrl/Cmd+Shift+Z`), **Esc** để huỷ vùng đang vẽ. Khi đang vẽ vùng mới
   có **thanh vàng** báo *"Đang vẽ vùng mới — N điểm"* để không ai bị kẹt ở chế độ vẽ; bấm trúng điểm
   của một vùng có sẵn sẽ **tự huỷ** bản nháp và sửa vùng đó.
7. Đặt **tên**, **màu**, **cách xác định trong vùng**, **ngưỡng tin cậy riêng**, bật/tắt vùng.

   **Cách xác định "trong vùng"** — 5 chế độ (chọn ở ô *Cách xác định "trong vùng"*):

   | Chế độ | Khi nào một đối tượng được tính là trong vùng | Dùng khi |
   |---|---|---|
   | **Điểm chân đối tượng** | điểm giữa-mép-dưới khung đối tượng nằm trong đa giác | camera nhìn chếch/từ trên xuống, thấy cả chân người |
   | **Tâm đối tượng** | tâm khung đối tượng nằm trong đa giác | camera nhìn ngang, người đứng giữa khung |
   | **Chạm vào vùng là tính** | khung đối tượng chạm/giao với đa giác (dù chỉ 1 px) | muốn nhạy nhất, không bỏ sót người đi ngang mép |
   | **Toàn bộ đối tượng nằm trong vùng** | cả khung đối tượng nằm gọn trong đa giác (≥98% diện tích) | chỉ tính khi người đã vào hẳn trong vùng |
   | **% đối tượng nằm trong vùng** | ≥ **X%** diện tích khung đối tượng nằm trong đa giác (thanh trượt 5–95%) | **camera nhìn gần**: chân người bị cắt mép dưới nên "điểm chân" luôn nằm ngoài |

   > **Lỗi hay gặp:** vùng **không bao giờ báo có người** dù người đứng rõ trong vùng. Nguyên nhân
   > thường là chế độ **Điểm chân** trong khi đa giác không với tới mép dưới khung hình (camera gần,
   > chân người ở ngoài ảnh). App giờ ghi rõ lý do ngay trong tab này (khung trống **"Phân tích trực
   > tiếp"**) và trong danh sách vùng ở tab **Trực tiếp** — ví dụ *"điểm chân nằm NGOÀI vùng — chọn
   > cách xác định khác"*. Cách sửa nhanh: đổi sang **% đối tượng nằm trong vùng** rồi hạ ngưỡng
   > xuống 30–40%, hoặc kéo đa giác xuống sát mép dưới.

   **Các vùng chạy song song trên CÙNG một kết quả suy luận:** model chỉ chạy **một lần** cho mỗi khung
   hình; toạ độ mọi đối tượng được quy đổi **một lần**, rồi **từng vùng** tự quyết định theo chế độ
   riêng của nó. Thêm 5 vùng không làm tăng thời gian suy luận — chỉ tăng vài phép toán toạ độ.
   Trên ảnh có **chấm neo** ở điểm chân mỗi đối tượng: **xanh** = đang được ít nhất một vùng tính,
   **hổ phách** = không vùng nào tính (nhìn là biết ngay vì sao vùng trống).
8. Tick **lớp đối tượng** (mặc định `person`); ô tìm kiếm giúp lọc nhanh trong 80 lớp.
9. **Lưu vùng** — toạ độ lưu dạng chuẩn hoá 0..1 nên đổi độ phân giải vẫn đúng.

> **Mã vùng (id) mặc định bị khoá 🔒.** (Vùng **vừa tạo** thì mã tự sinh theo tên cho tới lần **Lưu** đầu tiên,
> sau đó khoá.) Đổi **tên** vùng không bao giờ đổi **mã**, nên kịch bản không
> thể "mất vùng" vì một lần sửa tên. Muốn đổi mã thật thì bấm 🔒 → app gửi kèm `renames` và server
> **tự cập nhật mọi kịch bản** sang mã mới.
> Nếu bạn xoá một vùng đang được kịch bản dùng, server trả về **409** kèm `orphans`
> (`{scenario_id, scenario_name, zone_ref}`); UI hiện hộp thoại *"Bấm OK để xoá luôn các kịch bản đó"*
> rồi gửi lại với `drop_orphan_scenarios: true` — thay vì báo lỗi `(root): cấu hình không hợp lệ`.

Khung **Xem trước nhận diện** hiển thị ảnh mới nhất đã vẽ khung — dùng để canh vùng cho khớp.

### ⚙️ Kịch bản

- **Chọn vùng áp dụng** (ví dụ `Camera bếp · Khu bếp`).
- **Điều kiện kích hoạt**: Có người vào / Vùng trống / Số người ≥ N / Số người ≤ N /
  Ở lâu hơn X giây / Có lớp cụ thể (`cat`, `dog`…).
- **Các bước**: mỗi bước = *chờ bao nhiêu giây* + *gửi topic gì, payload gì, QoS, retain*.
  Nếu kịch bản có nhiều điều kiện (ví dụ vừa “Có người vào” vừa “Vùng trống”), các chip
  ngay trên khung bước cho bạn chọn **đang sửa bước của điều kiện nào**.
- **Dòng thời gian** hiển thị **tất cả điều kiện**, mỗi điều kiện một hàng, kèm nút
  *Gửi thử* cho từng bước.
- Nút **📤 Gửi thử** ở từng bước (gửi thật tới broker; nếu đang Dry-run thì chỉ ghi log).
- **Mẫu nhanh**: Zigbee2MQTT / Home Assistant / Tasmota-ESPHome / Raw JSON.
- **Khung giờ hoạt động** (hỗ trợ vắt qua nửa đêm, ví dụ 18:00 → 06:00).
- **Mô phỏng payload**: xem trước JSON sau khi thay biến.
- **Nâng cao — YAML**: sửa toàn bộ `config.yaml`, nút **Kiểm tra** validate trước khi **Lưu**.

Các biến dùng trong payload:

| Biến | Ý nghĩa |
|---|---|
| `{{count}}` | Số đối tượng trong vùng |
| `{{zone}}` | Mã vùng (ví dụ `khu_bep`) |
| `{{camera}}` | Mã camera |
| `{{timestamp}}` | Thời điểm ISO 8601 |
| `{{class_counts}}` | Object đếm theo lớp, ví dụ `{"person": 2}` |

### 🧪 Mô phỏng

- Bấm **Có người / Trống / Tắt** cho từng vùng — không cần đứng trước camera.
- **Tốc độ thời gian**: x1 / x5 / x10 / x30 (chỉ đồng hồ kịch bản tăng tốc,
  camera và nhận diện vẫn chạy thật).
- **Trạng thái kịch bản**: thanh đếm ngược tới bước tiếp theo.

### 📜 Nhật ký

- Luồng log trực tiếp (lọc theo loại/mức) + bảng **Lịch sử** (vào/ra vùng,
  lệnh đã gửi, giờ, OK/lỗi/dry-run).
- **Tiến trình kịch bản**: mỗi kịch bản một thẻ hiện trạng thái (có người/trống)
  và đếm ngược tới bước “mờ/tắt”.

### 🔌 MQTT

- Trạng thái kết nối, số đã gửi/nhận/đang chờ/bị bỏ, lỗi gần nhất.
- **Kiểm tra kết nối** (mở kết nối thử tới broker).
- **Gửi thử** một topic/payload tuỳ ý (QoS, retain).
- **Theo dõi gửi/nhận** có lọc theo topic và chiều.

### 🛠️ Cài đặt

- Danh sách camera (đèn xanh/đỏ), thêm camera RTSP, xem nhanh.
- **Camera test bằng webcam**: chọn webcam, **Bật camera test**, **Chuyển thành camera thật**,
  **Xoá camera test**, và tuỳ chọn **Phát webcam qua RTSP loopback**.
- Nhận diện: model, thiết bị (`auto/mps/cpu/cuda:0`), imgsz, ngưỡng tin cậy, `detect_fps`, motion gating.
- MQTT broker, Web UI (cổng, đăng nhập).
- **Lưu cài đặt** — áp dụng **ngay**, không cần khởi động lại (trừ đổi cổng web).

### Đăng nhập (tuỳ chọn)

Bật trong `web.auth.enabled` (hoặc tab Cài đặt). Hỗ trợ:
- Đăng nhập bằng user/password (cookie ký HMAC, không cần thư viện ngoài).
- Truy cập nhanh bằng token: `http://<ip>:8080/?token=<WEB_TOKEN>`
  (tiện cho thẻ `<img>` MJPEG hoặc dashboard).

---

## 8. Cấu hình `config.yaml`

Xem **`config.example.yaml`** — mọi khoá đều có chú thích tiếng Việt.
Cấu hình được **kiểm tra bằng pydantic** (`schema_version: 1`), lưu **nguyên tử**
(ghi tệp tạm → `fsync` → `os.replace`) và **tự sao lưu** bản cũ thành `config.yaml.bak`.

Điểm cần biết:

```yaml
detector:
  device: auto        # auto | cpu | mps | cuda:0
  detect_fps: 4       # suy luận 4 lần/giây — KHÔNG phải mỗi khung hình

cameras:
  - id: cam_bep
    source: rtsp      # rtsp | webcam | file | image
    transport: tcp    # đổi sang udp nếu TCP chập chờn
    stale_after_s: 10 # luồng treo quá 10s -> tự kết nối lại
    on_camera_lost: hold   # hold | vacant | publish_custom
    zones:
      - id: khu_bep
        polygon: [[0.12,0.35],[0.72,0.35],[0.78,0.95],[0.08,0.95]]
        enter_confirm: {frames: 3, of: 5}   # 3/5 khung mới công nhận
        exit_grace_s: 3                     # vắng 3s mới coi là trống
```

### Bí mật & biến môi trường

```yaml
mqtt:
  password: ${MQTT_PASSWORD}          # đọc từ biến môi trường
  username: ${MQTT_USER:-mqtt_user}   # có giá trị mặc định
```

- UI và log **luôn hiện `****`** thay cho mật khẩu/token.
- Xuất YAML từ UI cũng đã che bí mật; khi lưu lại, giá trị `****` được **giữ nguyên**
  (không ghi đè mật khẩu thật bằng dấu sao).

---

## 9. Docker (chạy trên NAS / mini-PC / Raspberry Pi)

> Trên **macOS** hãy chạy native (mục 3). Docker Desktop không truy cập được webcam laptop.

```bash
mkdir -p data
cp config.example.yaml data/config.yaml          # tuỳ chọn: app tự tạo nếu thiếu
docker compose up -d --build
docker compose logs -f
```

- Ảnh chạy **`opencv-python-headless`** (nhẹ, không cần X11) + `ffmpeg`.
- `network_mode: host` để tới camera IP trong LAN và mDNS/ONVIF.
  Nếu không dùng được host network, bỏ dòng đó và bật `ports: ["8080:8080"]`.
- Dữ liệu bền: `./data` chứa `config.yaml`, `logs/`, `snapshots/`, model `.yolo/`.
- Container tự `--no-webcam` (không có webcam trong container).
- Kèm broker nội bộ (tuỳ chọn): `docker compose --profile mqtt up -d`.

Muốn build/kiểm tra image mà chưa bật Docker Desktop thì phải khởi động Docker trước;
nếu daemon chưa chạy, `docker compose up` sẽ báo `Cannot connect to the Docker daemon`.

---

## 10. systemd — chạy 24/7 trên Linux

```bash
sudo cp vision-automation.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now vision-automation

systemctl status vision-automation
journalctl -u vision-automation -f
```

Unit đã đặt: `Restart=always`, `RestartSec=5`, `KillSignal=SIGTERM`, `TimeoutStopSec=30`,
giới hạn RAM (`MemoryMax=3G`), và các lớp bảo mật systemd cơ bản.
**Sửa `User=`, `WorkingDirectory=`, `ExecStart=`** cho khớp máy bạn.

Khi nhận `SIGTERM`/`Ctrl-C`, app sẽ **gửi “offline” cho MQTT**, đóng luồng camera
và **lưu `config.yaml`** trước khi thoát.

---

## 11. Camera test bằng webcam (macOS / Windows)

Mục đích: thử **toàn bộ pipeline** (vẽ vùng → phát hiện → kịch bản → MQTT) ngay
trên laptop, không cần camera IP.

### Bật camera test

- Tab **Cài đặt → Camera test bằng webcam → Bật camera test**, hoặc
- Chạy: `python -m app --with-webcam`

App thêm camera `cam_test_laptop`:

```yaml
id: cam_test_laptop
source: webcam
device: auto            # chọn "FaceTime HD Camera" (bỏ qua iPhone Continuity Camera)
resolution: [1280, 720]
fps: 15
mirror: true
test: true
lazy_open: true         # chỉ mở khi có người xem -> đèn webcam tắt khi không dùng
```

- **Không ghi đè / không đổi thứ tự** các camera RTSP đang có.
- Camera test được gắn nhãn **TEST** trong UI; kịch bản của nó mặc định chạy
  **dry-run** hoặc gửi vào nhánh **`test/`** (`testing.test_topic_prefix`).
- **Đèn webcam**: chỉ sáng khi bạn đang xem tab Trực tiếp (hoặc kịch bản test cần).
  Đóng tab / dừng xem → app đóng thiết bị → **đèn tắt** (riêng tư + không chiếm camera).
- **Nhận diện thiết bị theo TÊN** (không theo chỉ số OpenCV): app liệt kê qua
  AVFoundation, ưu tiên camera tích hợp, **bỏ qua iPhone/iPad Continuity** trừ khi bạn
  chọn thủ công. Không có pyobjc thì dùng `system_profiler`.

### Phát webcam qua RTSP (loopback) — tuỳ chọn

```bash
brew install ffmpeg mediamtx
```

Tab **Cài đặt → Phát webcam qua RTSP (loopback)**:

```
ffmpeg (avfoundation) → h264_videotoolbox → mediamtx → rtsp://127.0.0.1:8554/laptop
```

Sau đó app thêm camera `cam_test_rtsp` trỏ vào URL đó và chuyển `cam_test_laptop`
sang đọc qua RTSP. **Chỉ một tiến trình được mở webcam** (ffmpeg giữ webcam), nên
tránh lỗi “device busy”. Thiếu `ffmpeg`/`mediamtx` → app hiện hướng dẫn tiếng Việt,
**không crash**.

App tự xử lý hai cái bẫy thường gặp của `avfoundation` trên máy Apple Silicon:

| Lỗi ffmpeg | Nguyên nhân | App làm gì |
|---|---|---|
| `Selected framerate (15.000000) is not supported by the device` | FaceTime HD chỉ có 1280x720@30 và 640x480@30 | Đọc danh sách “Supported modes”, tự thử lại với đúng fps thiết bị hỗ trợ, rồi hạ tốc độ phát bằng `-r` |
| `Selected pixel format (yuv420p) is not supported by the input device` | Webcam chỉ nhận `nv12`/`uyvy422`/`yuyv422`/`0rgb` | Luôn mở input bằng `-pixel_format nv12` |

Ngoài ra app **tự sinh** `logs/mediamtx-loopback.yml` (chỉ bật RTSP :8554, tắt
RTMP/HLS/WebRTC/MoQ/API) để tránh lỗi `bind: address already in use` khi máy đã có
dịch vụ khác chiếm cổng 8000/8888/8892.

Nếu bật loopback mà vẫn lỗi, xem hai tệp log sau (đường dẫn tương đối với thư mục dự án):

```bash
tail -n 20 logs/loopback-ffmpeg.log     # lỗi mở webcam / mã hoá
tail -n 20 logs/loopback-mediamtx.log   # lỗi cổng, xác nhận "stream is available"
```

---

## 12. Hiệu năng & tinh chỉnh

Suy luận chạy theo `detect_fps` (**mặc định 4 lần/giây**), không phải mỗi khung hình;
khung hình luôn lấy **bản mới nhất** (bỏ khung cũ, không tích tụ bộ nhớ).

| Muốn… | Chỉnh |
|---|---|
| Nhẹ CPU hơn | `detect_fps: 2–3`, `imgsz: 416–480`, `motion_gating: true` |
| Bắt người ở xa tốt hơn | `imgsz: 960`, dùng camera sub-stream độ phân giải cao hơn |
| Đèn phản ứng nhanh hơn | `detect_fps: 5`, `enter_confirm: {frames: 2, of: 3}` |
| Không nhấp nháy khi người đứng yên | tăng `enter_confirm` lên 3/5 hoặc 4/5, `exit_grace_s: 5` |
| Xem trên điện thoại mạng yếu | `web.stream_width: 640`, `web.mjpeg_fps: 5`, `web.jpeg_quality: 60` |
| Tiết kiệm khi vắng | `motion_gating: true` + `idle_detect_fps: 1` (chỉ giảm khi cảnh **tĩnh và vùng trống** — không bao giờ dùng chuyển động để kết luận “trống” khi đang có người) |

UI hiển thị **FPS camera / FPS suy luận / ms suy luận** để bạn cân chỉnh ngay.

---

## 13. Độ ổn định & an toàn (fail-safe)

| Tình huống | Hành vi |
|---|---|
| Mất camera | Kết nối lại **exponential backoff** (0.5s → 30s); phát hiện luồng **treo** qua `stale_after_s`; UI hiện ảnh cuối + cảnh báo |
| Mất camera + `on_camera_lost: hold` (mặc định) | **Không đổi trạng thái đèn** (an toàn nhất) |
| Mất camera + `vacant` | Coi như vùng trống (đèn tắt sau timeline) |
| Mất camera + `publish_custom` | Gửi đúng payload bạn cấu hình |
| Mất MQTT | Hàng chờ **có giới hạn** (`queue_max`), mỗi kịch bản chỉ giữ **1 lệnh mới nhất**; khi kết nối lại chỉ gửi trạng thái mới nhất (không phát lại ON→OFF cũ) |
| Khởi động app | Trạng thái vùng là **unknown** → **không gửi OFF oan**; chỉ khi xác định được mới gửi lệnh |
| Tắt app (SIGTERM/Ctrl-C) | Gửi `offline` (LWT/availability), đóng luồng camera, lưu config |
| Lỗi suy luận (model/thiếu torch) | Chuyển sang `NullDetector`, app vẫn chạy camera + UI, log rõ cách cài |
| Thiếu ultralytics/torch | App **không crash**, cảnh báo, hướng dẫn cài |
| Lỗi ở một camera | **Cô lập theo luồng**: camera khác không bị ảnh hưởng |
| Log phình to | `RotatingFileHandler` (5 MB × 3 bản) |

---

## 14. MQTT: topic, payload, Home Assistant

### Topic mặc định

| Topic | Retain | Nội dung |
|---|---|---|
| `vision/status` | ✔ | `online` / `offline` (LWT) |
| `vision/<camera>/<vùng>/state` | ✔ | `{"occupied":true,"count":1,"class_counts":{"person":1},"since":"2026-01-01T18:00:00","zone":"khu_bep","camera":"cam_bep"}` |

### Preset payload

**Zigbee2MQTT**
```json
{ "state": "ON", "brightness": 254, "transition": 1 }
```

**Home Assistant (MQTT light)**
```json
{ "state": "ON", "brightness": 255 }
```
Thêm vào `configuration.yaml`:
```yaml
mqtt:
  light:
    - name: "Đèn bếp"
      command_topic: "zigbee2mqtt/den_bep/set"
      state_topic: "zigbee2mqtt/den_bep"
      schema: json
      brightness: true
```

**Tasmota / ESPHome**: payload text `Power1 ON`, hoặc JSON `{"power":"ON"}` tuỳ cấu hình.

**Raw JSON** (ví dụ gửi kèm số người):
```json
{ "lenh": "bat", "so_nguoi": "{{count}}", "vung": "{{zone}}" }
```

### Kiểm tra nhanh bằng CLI

```bash
mosquitto_sub -h 192.168.1.10 -v -t 'vision/#'
mosquitto_pub -h 192.168.1.10 -t zigbee2mqtt/den_bep/set -m '{"state":"ON"}'
```

---

## 15. Quyền riêng tư

- Mọi xử lý diễn ra **trong máy bạn**; không có API cloud, không telemetry.
- **Không lưu ảnh** trên đĩa. Ảnh chỉ nằm trong RAM (khung mới nhất).
- Nút **Chụp ảnh** mới ghi 1 tệp JPEG vào `snapshots/` (tắt bằng `web.allow_snapshot_save: false`).
- Webcam **chỉ mở khi có người xem** (`lazy_open: true`) → đèn tắt khi không dùng.
- Model YOLO được tải **một lần** từ Ultralytics về máy, sau đó chạy offline.

---

### Vẽ vùng / kéo điểm — những điều nên biết

| Hiện tượng | Nguyên nhân & cách xử lý |
|---|---|
| Kéo điểm mà **cả vùng chạy theo** | Bạn đang bấm vào **giữa vùng** (con trỏ ✥) chứ không phải vào điểm (con trỏ ✋). Rê chuột cho tới khi **điểm sáng vàng** rồi mới kéo. |
| Vùng vẽ ra **không khớp** hình | Đang ở chế độ **▶ Trực tiếp** (hình động). Bấm **📷 Chụp lại** để vẽ trên ảnh tĩnh. |
| Không thêm được điểm | Vùng **hình chữ nhật** thì kéo chấm giữa cạnh = đổi kích thước (không thêm điểm). Muốn tự do thêm điểm: đổi **Kiểu vùng → Đa giác tự do**. |
| Lưu báo “vùng tự cắt nhau” | Các cạnh cắt chéo (hình số 8). Sửa lại thứ tự/khoảng cách điểm — vùng tự cắt cho kết quả “trong vùng” sai. |
| Lưu báo “đang được kịch bản sử dụng” | Đổi **tên** vùng thay vì xoá, hoặc xoá kịch bản đó trước. (Mã vùng đã bị **khoá** để đổi tên không làm hỏng kịch bản.) |

## 16. Xử lý sự cố

| Hiện tượng | Nguyên nhân & cách sửa |
|---|---|
| `Không kết nối được RTSP: sai mật khẩu/URL?` | Sai URL/mật khẩu, sai IP, camera chặn IP. Bấm **🔌 Kiểm tra** cạnh camera trong tab **Cài đặt** — app tự phân biệt: *không phân giải được tên miền* / *không tới được IP: cổng* / *cổng mở nhưng không phải RTSP* / *401 sai mật khẩu* / *404 sai đường dẫn luồng*, kèm cách sửa. Kiểm tra thêm bằng VLC. **Thử đổi transport sang UDP.** |
| Nguồn RTSP **đã bật lại** mà app vẫn không kết nối | Bấm **🔌 Kiểm tra**: nếu báo *“Không tới được `<ip>:<cổng>` sau 4 giây”* thì lỗi **không nằm ở app** — máy phát không mở cổng đó ra mạng. Kiểm tra trên **máy phát**: (1) dịch vụ RTSP đang chạy và **listen trên 0.0.0.0** chứ không phải `127.0.0.1` — Windows: `netstat -ano \| findstr 8554`, macOS/Linux: `lsof -iTCP:8554 -sTCP:LISTEN`; (2) mở cổng trong tường lửa (Windows: `New-NetFirewallRule -DisplayName RTSP -Direction Inbound -LocalPort 8554 -Protocol TCP -Action Allow`); (3) hai máy **cùng một mạng LAN** (mạng khách / bật “client isolation” thì máy này không thấy máy kia dù ping được); (4) app phát là app trên điện thoại thì phải để màn hình sáng. Thử mở bằng VLC trên **cùng máy này**: Media → Open Network Stream. |
| Nút / công tắc trong tab **Cài đặt** bấm không ăn | Đã sửa ở bản này: trước đây danh sách camera bị dựng lại ~8 lần/giây (theo nhịp WebSocket) nên `mousedown` và `mouseup` rơi vào 2 phần tử khác nhau → trình duyệt không phát sinh sự kiện `click`. Nay panel chỉ vẽ lại khi dữ liệu **thật sự đổi** và **không** vẽ lại khi đang giữ chuột. Nếu vẫn gặp: **Ctrl/Cmd+Shift+R** để bỏ `app.js` cũ trong cache. |
| Hình đứng, không cập nhật | Luồng treo — app tự kết nối lại sau `stale_after_s`. Nếu lặp lại, giảm `imgsz`, hoặc chuyển camera sang sub-stream. Xem [§16.1](#161-hình-bị-trễ-hoặc-treo-liên-tục-rtsp). |
| `Trễ hình` báo ≥ 1 giây, hình “đi sau” thực tế | Đường truyền đang đệm khung (GOP dài, mạng chậm, CPU đuối). Xem [§16.1](#161-hình-bị-trễ-hoặc-treo-liên-tục-rtsp). |
| `FPS suy luận 0.0` + vùng ghi `Đang xác định…` | Camera mới thêm từ UI chưa có luồng suy luận (lỗi cũ đã sửa ở bản này). Kiểm tra log có `Luồng suy luận <id> bắt đầu`; nếu thiếu, tắt rồi bật lại camera trong tab **Cài đặt**, hoặc khởi động lại app. |
| Ảnh đen / “chưa có tín hiệu” | macOS chưa cấp quyền Camera → System Settings → Privacy & Security → Camera → bật cho Terminal/VS Code → **khởi động lại app**. Hoặc webcam đang bị Zoom/FaceTime giữ. |
| `device busy` khi mở webcam | Chỉ **một** tiến trình được dùng webcam. Đóng app khác, hoặc tắt loopback rồi mở lại. |
| Đèn nhấp nháy | Tăng `enter_confirm` (3/5 hoặc 4/5) và `exit_grace_s` lên 4–5 giây; kiểm tra vùng có bao gồm vùng nhiễu (TV, gương). |
| Không thấy lệnh MQTT | Đang bật **Dry-run**? (header có băng vàng). Kiểm tra tab MQTT → “Kiểm tra kết nối”, đúng host/port/user/password, và topic có đúng không. |
| `MQTT mất kết nối` liên tục | Sai user/password, broker chặn `client_id` trùng, firewall. Xem log tab Nhật ký. |
| Không thấy ai trong vùng dù có người | Xem dòng **Phân tích trực tiếp** trong tab Vùng phát hiện (hoặc lý do ngay dưới tên vùng ở tab Trực tiếp): nếu báo *"điểm chân nằm NGOÀI vùng"* thì đổi **Cách xác định** sang **% đối tượng nằm trong vùng** (hạ xuống 30–40%) hoặc kéo vùng xuống sát mép dưới; nếu báo *"dưới ngưỡng tin cậy"* thì hạ **Ngưỡng tin cậy riêng**; nếu báo *"khác lớp"* thì tick lại đúng lớp (ví dụ `person`). |
| Vùng báo có người khi không có ai / báo trống chập chờn | Chọn chế độ **Toàn bộ đối tượng nằm trong vùng** (chặt hơn), tăng `enter_confirm.frames`/`of` trong YAML, hoặc tăng `exit_grace_s`. |
| Chấm neo trên ảnh màu hổ phách | Đối tượng không được vùng nào tính — bình thường khi người đứng ngoài mọi vùng; nếu đang đứng trong vùng thì xem lý do như dòng trên. |
| Model không nạp được | Chưa cài `ultralytics`/`torch`: `pip install -r requirements.txt`. Máy không có mạng lần đầu → tải trước `yolo11n.pt` và đặt cạnh `config.yaml`. |
| Web UI không mở được từ điện thoại | Dùng `web.host: 0.0.0.0`, mở `http://<ip-máy>:8080`, kiểm tra firewall; nếu bật auth thì đăng nhập. |
| Máy chạy chậm / nóng | Giảm `detect_fps`, `imgsz`; bật `motion_gating`; đặt `device: cpu` nếu MPS gây nóng. |
| `Kịch bản … đang trỏ tới vùng …` khi bấm **Lưu vùng** | Vùng đang bị kịch bản dùng nhưng đã bị xoá/đổi mã. UI **hỏi trước**: *"Bấm OK để xoá luôn các kịch bản đó"*; nếu đổi mã thì server tự cập nhật kịch bản. Muốn giữ kịch bản: **đổi tên** vùng (mã vẫn khoá 🔒) thay vì xoá, hoặc vào tab **Kịch bản** chọn lại vùng. |
| Mở UI lại vẫn thấy lỗi cũ / giao diện như trước khi cập nhật | Trình duyệt giữ `app.js` cũ trong cache. Bản này tự gắn `?v=<mtime>` vào link JS/CSS nên chỉ cần **F5**; nếu vẫn cũ thì **Ctrl/Cmd + Shift + R**. |
| Tab đang mở từ hôm trước: nút mới “không ăn”, giao diện thiếu mục vừa cập nhật | Tab cũ giữ `app.js` **đã nạp trong bộ nhớ** (cache-buster chỉ có tác dụng khi nạp lại trang). Bản này tự phát hiện: `/api/state` mang `asset_version`, khi lệch với bản đang chạy thì hiện **thanh vàng** *“🔄 Máy chủ vừa có bản giao diện mới…”* + nút **Nạp lại ngay**. Không thấy thanh đó mà vẫn nghi bản cũ: **F5** (hoặc Ctrl/Cmd+Shift+R). |
| UI tự nhảy về camera đầu danh sách | Đã sửa: camera đang chọn được nhớ trong trình duyệt (`localStorage`), và tab **Trực tiếp** ↔ tab **Vùng phát hiện** luôn nói về cùng một camera. Xoá dữ liệu trang web (site data) sẽ quên lựa chọn này. |
| Không kéo được điểm / không thêm được điểm trên cạnh vùng | Đưa chuột tới **điểm** — điểm đó sáng vàng, con trỏ thành ✋; tới **giữa cạnh** — con trỏ thành **+** (đa giác) hoặc ↕/↔ (hình chữ nhật). Nếu không thấy gì đổi: đang ở chế độ **vẽ vùng mới** (có thanh vàng) → nhấn **Esc**, hoặc bấm trúng một điểm của vùng có sẵn để tự huỷ bản nháp. |
| Kéo một điểm mà cả vùng chạy theo / hình chữ nhật bị méo | Đã sửa: **đa giác** chỉ dời đúng điểm đang kéo; **hình chữ nhật** kéo góc = đổi chiều dài/rộng (có kẹp, không lật), kéo chấm giữa cạnh = dời riêng cạnh đó. Kiểm tra ô **Kiểu vùng** đúng với ý bạn (kiểu được lưu vào config). |
| Bấm vào chỗ khác tự chuyển sang vùng khác | Đã bỏ tự chuyển khi bấm bên trong vùng khác; chỉ chuyển khi bấm trúng **một điểm** của vùng đó, hoặc chọn trong danh sách **Vùng đang sửa**. |
| Port 8080 bị chiếm | `--port 8090` hoặc `web.port: 8090`. |

Log chi tiết: `logs/vision-automation.log` (tự xoay vòng) và tab **Nhật ký**.

### 16.1 Hình bị trễ hoặc treo liên tục (RTSP)

Đây là sự cố hay gặp nhất với camera IP giá rẻ. Ba nguyên nhân, kiểm tra theo thứ tự:

**1. Đọc khung bị chậm hơn nguồn phát (nguyên nhân phổ biến nhất của “trễ 3 giây”)**

Nếu app đọc với tốc độ thấp hơn tốc độ camera phát (ví dụ đọc 10 fps trong khi camera
phát 11–12 fps), phần dư **dồn lại mãi không bao giờ được xả** → hình ngày càng đi sau
thực tế, cuối cùng trông như “đứng”. Bản này đã sửa: nguồn RTSP **không bị giới hạn tốc
độ đọc** nữa — luôn đọc nhanh nhất có thể và chỉ giữ **khung mới nhất**.
Kiểm tra tab **Trực tiếp** → ô **Trễ hình**:

| Trễ hình | Ý nghĩa | Việc cần làm |
|---|---|---|
| 20–150 ms | Bình thường | Không cần làm gì |
| 200–900 ms | Hơi trễ, còn chấp nhận | Giảm `web.stream_width`, hoặc dùng sub-stream |
| ≥ 1 s (chữ vàng) | Đang đệm khung | Giảm độ phân giải camera (sub-stream 640×480), đổi `transport: udp`, giảm `imgsz` |

**2. Mạng/đường truyền chậm hoặc mất gói**

- Đổi `transport: tcp` → `udp` trong tab **Cài đặt** (UDP nhanh hơn, chịu mất gói tốt hơn
  cho LAN dây, nhưng hay rách hình trên Wi-Fi yếu).
- Dùng **sub-stream** của camera (thường `.../stream2` hoặc `.../sub`) ở 640×480.
- Với camera qua Wi-Fi: kiểm tra cường độ sóng; ưu tiên dây LAN.

**3. Nguồn phát bị treo (lỗi của camera / NVR, không phải của app)**

App tự phát hiện: không có khung mới trong `stale_after_s` giây (mặc định 10) → đóng kết
nối, mở lại với backoff 0.5 → 1 → 2 … 30 giây. Log sẽ có chuỗi:

```
Đã mở camera live_sdp (...)          ← mở lại thành công
WARNING Luồng treo quá 10s — Kết nối camera bị treo — đang tự kết nối lại.
```

Nếu chuỗi này lặp lại đều đặn (khoảng 20 giây một lần), hãy kiểm tra bằng chính ffmpeg
(có sẵn ở `.run/tools/ffmpeg` nếu bạn tải về, hoặc `brew install ffmpeg`):

```bash
ffmpeg -rtsp_transport tcp -i "rtsp://user:pass@192.168.1.50:554/stream1" \
       -t 20 -f null - 2>&1 | tail -5
```

`-f null -` chỉ đọc, không ghi file. Nếu ffmpeg cũng báo lỗi/giật → lỗi ở camera/mạng;
nếu ffmpeg đọc êm còn app thì treo → xem mục 1 và 2 ở trên, và gửi log cho người phát triển.

**Các tham số liên quan** (tab **Cài đặt** → YAML, hoặc `config.yaml`):

```yaml
cameras:
  - id: cam_bep
    source: rtsp
    url: rtsp://user:pass@192.168.1.50:554/stream1
    transport: tcp          # thử udp nếu mạng LAN dây tốt
    stale_after_s: 10       # coi là treo nếu quá 10 s không có khung mới
    rtsp_options:           # tuỳ chọn ffmpeg nâng cao (ghi đè mặc định)
      rtsp_flags: prefer_tcp
detector:
  imgsz: 640                # 416/320 nếu CPU yếu
  detect_fps: 4             # 3–5 là đủ cho đèn tự động
```

App đã tự bật sẵn các tuỳ chọn low-delay (`fflags=nobuffer`, `flags=low_delay`,
`max_delay=500000`, `reorder_queue_size=0`) và đặt timeout đọc tối đa 5 giây, nên một
kết nối chết không thể treo UI lâu nữa.

---

## 17. Kiểm thử

```bash
source .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest            # chạy toàn bộ
python -m pytest -q         # tóm tắt
```

Bộ test (**272 test**) phủ:

- **`tests/test_config.py`** — validate, che bí mật, ghi nguyên tử, hot reload.
- **`tests/test_zones.py`** — hình học đa giác, **5 chế độ xác định trong vùng**
  (`bottom_center`/`center`/`any_overlap`/`fully_inside`/`inside_ratio`), **nhiều vùng chạy
  song song trên một tập đối tượng**, phần phân tích "vì sao vùng trống", bỏ phiếu 3/5 + grace.
- **`tests/test_scenarios.py`** — máy trạng thái với **đồng hồ giả**: timeline mờ/tắt,
  khung giờ, dwell, count_ge/le, class_present, cooldown, tạm dừng khi chỉnh tay.
- **`tests/test_capture.py`** — nguồn ảnh/video, khung mới nhất, lazy open, cách ly lỗi.
- **`tests/test_mqtt.py`** — template payload + escape JSON, dry-run (kể cả sau khi
  broker nối lại), hàng chờ theo topic + TTL, `start()` tự phục hồi, tạm dừng khi chỉnh tay.
- **`tests/test_zone_editor_ui.py`** — **Playwright + Chromium thật** (DPR 1 **và** 2):
  kéo điểm/cạnh/góc đúng dưới con trỏ, không xê dịch điểm khác, con trỏ + nhãn đổi theo
  thao tác, bấm đúp không sinh điểm trùng, đóng băng khung hình, cảnh báo khi bỏ thay đổi,
  bảng chẩn đoán hiện số thật, chặn vùng tự cắt nhau.
- **`tests/test_mqtt_integration.py`** — **broker MQTT thật** (amqtt): gửi/nhận, retained,
  mất mạng rồi kết nối lại, dry-run không gửi gì.
- **`tests/test_runtime.py`** — nối toàn bộ pipeline với detector giả, mô phỏng, fail-safe.
- **`tests/test_web.py`** — REST/ASGI: state, config, vùng (kể cả chặn xoá vùng đang bị kịch bản
  dùng → 409 + `orphans`, và đổi mã vùng → kịch bản tự cập nhật), mô phỏng, snapshot, auth,
  cache-buster của `index.html`.
- **`tests/test_webcam_safety.py`** — thêm/chuyển/xoá camera test (không mở webcam thật),
  webcam lỗi không ảnh hưởng camera khác, phát hiện thiếu ffmpeg/mediamtx.
- **`tests/test_shutdown.py`** — chạy CLI thật, gửi `SIGTERM`, kiểm tra thoát êm (mã 0),
  log “Đã dừng” và config được lưu.

Không có test nào mở webcam thật (tránh bật đèn camera của bạn) hay gửi lệnh tới thiết bị thật.

---

## 18. Kiến trúc mã nguồn

```
vision_automation/
├── app/
│   ├── __main__.py         # python -m app
│   ├── main.py             # CLI, chdir về thư mục config, UTF-8 console, dừng êm
│   ├── platform_compat.py  # MỌI phần khác biệt OS: backend webcam, quyền camera,
│   │                       #   ffmpeg loopback (avfoundation/dshow/v4l2), tín hiệu
│   ├── config.py           # pydantic models, load/save nguyên tử, hot reload
│   ├── detection.py        # Box / Detection (thuần Python, không numpy)
│   ├── capture.py          # luồng đọc camera: RTSP/webcam/file/image, reconnect
│   ├── detector.py         # Detector protocol + Ultralytics YOLO (MPS→CPU)
│   ├── zones.py            # hình học đa giác + bỏ phiếu/grace (thuần Python)
│   ├── scenarios.py        # máy trạng thái kịch bản với đồng hồ tiêm được
│   ├── mqtt_client.py      # paho v2: LWT, hàng chờ, template payload
│   ├── simulator.py        # giả lập vùng + tăng tốc thời gian
│   ├── runtime.py          # ghép mọi thứ, sự kiện, fail-safe, snapshot cho UI
│   ├── annotate.py         # vẽ khung nhận diện + vùng + HUD (OpenCV)
│   └── web/
│       ├── server.py       # FastAPI: REST, WebSocket, MJPEG, auth
│       └── static/         # index.html, app.js, style.css (không build)
├── tests/                  # 272 test (pytest + Playwright UI), gồm test_platform.py, test_netcheck.py
├── run.py                  # khởi chạy từ BẤT KỲ thư mục nào (Task Scheduler/NSSM)
├── Khoi-dong.bat           # Windows: bấm đúp để chạy (tự tạo .venv + cài lib)
├── Dung-lai.bat            # Windows: dừng êm qua POST /api/system/shutdown
├── Kiem-tra-Windows.bat    # Windows: kiểm tra môi trường (python run.py --doctor)
├── HUONG-DAN-WINDOWS.md    # hướng dẫn Windows từng bước cho người mới
├── config.example.yaml
├── requirements.txt / requirements-dev.txt / requirements-docker.txt
├── Dockerfile / docker-compose.yml
├── vision-automation.service
└── README.md
```

Luồng dữ liệu:

```
CaptureWorker (1 luồng/camera, giữ khung MỚI NHẤT)
        │  FramePacket
        ▼
InferenceThread (1 luồng/camera, chạy theo detect_fps)
        │  list[Detection]
        ▼
ZoneEngine (bỏ phiếu 3/5 + exit_grace)  ──►  ZoneState
        │
        ▼
ScenarioEngine (đồng hồ có thể tăng tốc)  ──►  CommandRequest
        │
        ▼
MqttPublisher (dry-run / hàng chờ / chỉ gửi khi đổi trạng thái)
        │
        ▼
Web UI (WebSocket ~8 Hz + MJPEG) · EventBus (Nhật ký)
```

**Logic thuần Python** (zones, scenarios, config, detection) **không phụ thuộc
OpenCV/MQTT**, nên test được với **đồng hồ giả** và chạy nhanh.

---

## 18.1 Vòng sửa “nút trong Cài đặt không ăn” + “RTSP không kết nối” (đo thật)

| Kiểm tra | Kết quả đo |
|---|---|
| Nguyên nhân nút bấm “không ăn” | `MutationObserver` trên `#settings-cameras` đếm **7 lần dựng lại/giây** (do WebSocket đẩy trạng thái 8 Hz). Click chuột **giữ 250 ms** vào công tắc → **không có** sự kiện `click` (mousedown và mouseup rơi vào 2 node khác nhau) |
| Sau khi sửa (`setHtml` + chốt con trỏ) | **0 mutation/giây** khi dữ liệu không đổi; cùng cú click giữ 250 ms → `POST /api/cameras/cam_test_laptop/enabled` → toast *“Đã tắt cam_test_laptop”* |
| Test hồi quy | `tests/test_zone_editor_ui.py`: `test_settings_panel_is_not_rebuilt_when_nothing_changes`, `test_real_click_on_camera_switch_lands_while_state_updates` (giả lập trạng thái đẩy 30 ms/lần) |
| `POST /api/cameras/<id>/test-connection` (RTSP hỏng thật) | trả sau **4.0 s**: `{ok:false, stage:"tcp", message:"Không tới được 192.168.200.141:8554 sau 4 giây.", hint:"…(1) máy phát TẮT RTSP; (2) khác mạng; (3) tường lửa chặn cổng 8554… thử VLC", detail:"IP phân giải được: 192.168.200.141"}` |
| Cùng endpoint với webcam | `{ok:true, stage:"ok", message:"Thấy webcam: FaceTime HD Camera."}` |
| Chẩn đoán mạng độc lập (máy này) | ping `192.168.200.141` **có trả lời** (97–116 ms, ARP có MAC), nhưng **0/33 cổng mở** (554, 8554, 1935, 8080, 445, 3389, …) → kết luận: dịch vụ RTSP không listen ra mạng, **không phải lỗi app**; đối chứng `test.mosquitto.org:1883` và LAN `192.168.200.159:8080` đều kết nối được |
| Hộp quyền camera | trước: luôn in “macOS chưa cho phép…” dù đã cấp quyền; nay **“✅ Đã cấp quyền camera cho ứng dụng này.”** (nền xanh), chỉ hiện hướng dẫn cấp quyền khi thật sự bị chặn |
| Thẻ camera trong Cài đặt | hiện thêm dòng 💡 **gợi ý sửa** (`capture.hint`) và nút **🔌 Kiểm tra** với kết quả chi tiết ngay dưới tên camera |
| Tab cũ giữ `app.js` cũ (bẫy “bấm không ăn” lần 2) | `GET /api/state` trả `asset_version` = mtime của `app.js`/`style.css`. Đo trên app thật cổng 8080: mở trang → **0** banner; đổi mtime (giả lập phát hành bản mới) → **2.5 s sau banner hiện** *“🔄 Máy chủ vừa có bản giao diện mới…”* + nút **Nạp lại ngay** |

## 19. Những gì đã chạy thật trên máy Apple Silicon (M1)

Bảng dưới là kết quả đo được khi phát triển, không phải suy đoán:

| Hạng mục | Kết quả |
|---|---|
| `pytest` | **272/272 pass** (~37 s) |
| RTSP thật `rtsp://192.168.1.50:554/live.sdp` (640×480, nguồn 10 fps) | chạy **> 6 phút liên tục**: `capture_fps 10.0`, `read_ms 110`, **`frame_age_ms 30`** (trước khi sửa: trễ ~3 s, hễ ~20 s lại `Luồng treo quá 10s`), **0** lần treo trong log, `FPS suy luận 4.0` |
| Sự cố “treo liên tục + trễ 3 s” | hai lỗi gốc: (1) `_on_config_change` ném `IndexError` tại `running = {name.split("-", 1)[1] …}` nên **không bao giờ tạo luồng suy luận** cho camera thêm từ UI; (2) đọc RTSP bị **giới hạn tốc độ theo `fps`** nên khung dư dồn mãi → trễ dần rồi đứng. Đã sửa + thêm 6 test hồi quy |
| Hot-reload bật/tắt camera đang chạy | `{"value": false}` → `Dừng luồng suy luận live_sdp`; `{"value": true}` → `Luồng suy luận live_sdp bắt đầu`; **không** còn `IndexError` |
| YOLO trên MPS (`yolo11n.pt`, `imgsz=640`) | **15–53 ms/khung**, thiết bị chọn `mps`, warmup ~5 s |
| Nguồn `image` (bus.jpg) | vùng `khu_nguoi` → `occupied=true, count=3` → lệnh `[DRY-RUN] … {"state":"ON","brightness":254}` |
| Retained zone state | `vision/cam_anh/khu_nguoi/state` với `since` là giờ thực (không phải 1970) |
| MJPEG `/stream/<id>.mjpg` | ~10 fps, ranh giới `--visionframe` + JPEG hợp lệ |
| Webcam FaceTime HD (qua AVFoundation) | mở **chỉ khi** có người xem MJPEG: 14.5 fps; đóng sau 3 s khi không còn ai xem → **đèn webcam tắt** |
| Lazy open | `cam_test_laptop` ở trạng thái `is_open=false` khi không ai xem |
| RTSP loopback | ffmpeg → MediaMTX → `rtsp://127.0.0.1:8554/laptop`: `cam_test_rtsp` đọc được (~9 fps, suy luận 15.3 ms), **không “device busy”**; tắt loopback → không còn tiến trình nào giữ webcam |
| Thiếu ffmpeg/mediamtx | Báo hướng dẫn `brew install …`, app **không crash** |
| MQTT broker thật (amqtt) | QoS1, retained đọc được bởi subscriber mới, broker mất → hàng chờ chỉ giữ lệnh mới nhất |
| `SIGTERM` | thoát mã **0**, log “Đang dừng hệ thống…” → “Đã dừng”, `config.yaml` được lưu + `.bak` |
| Khởi chạy từ thư mục khác (`cd /tmp && python …/run.py --config …/config.yaml`) | `run.py` import được package, `prepare_workdir` tự `chdir` về thư mục config → `logs/`, `config.yaml.bak` được tạo **cạnh config** |
| Banner khởi động | in `Web UI (máy này)`, `Điện thoại/máy khác: http://<LAN IP>:8080`, `Hệ điều hành: macOS 15.7.9` |
| `POST /api/system/shutdown` | trả `{"ok":true}` → log “Dừng theo yêu cầu API” → “Đang dừng hệ thống…” → “Đã dừng”, tiến trình thoát, `config.yaml` + `.bak` được lưu |
| `/api/state` → `platform` | `{os, label, python, webcam_backends, webcam_permission, webcam_hint, webcam_probe, loopback_hint, avfoundation, local_ip}`; `/api/system/info` trả cùng nội dung |
| Nhánh Windows (mô phỏng `sys.platform = "win32"`) | backend `CAP_DSHOW → CAP_MSMF`; quyền camera `not_required`; ffmpeg loopback `-f dshow … -c:v libx264`; hint `winget`; test trong `tests/test_platform.py` (**23 test**) |
| Nhánh Linux (mô phỏng `linux`) | backend `CAP_V4L2`, device `/dev/video*`, ffmpeg `-f v4l2`, hint `apt` |
| Đọc tay `os.replace` bị khoá (Windows AV) | `app/config.py:_replace_with_retry` thử lại 6 lần thay vì báo lỗi lưu config |

**Sửa lỗi trình sửa vùng + chọn camera (đã kiểm bằng chạy thật + mô phỏng JS):**

| Kiểm tra | Kết quả |
|---|---|
| Đổi **tên** vùng có đổi **mã** không | Không — `syncZoneAttributes()` chỉ đổi `zone.id` khi người dùng mở khoá 🔒; mô phỏng bằng `node` cho `id = khu_bep` dù tên đã thành "Vùng bếp mới" |
| Đổi mã vùng kèm `renames` | `POST /api/cameras/cam_test_laptop/zones` → **200**, `renamed={'khuvuc_test': 'khuvuc_test_2'}`, kịch bản `test_den_bep` tự thành `cam_test_laptop/khuvuc_test_2`; đổi lại thì trả về nguyên trạng |
| Xoá vùng đang có kịch bản dùng | **409** + `orphans=[{scenario_id: test_den_bep, …}]`, config **không đổi**; gửi lại với `drop_orphan_scenarios: true` → 200 và kịch bản bị xoá |
| Thông điệp lỗi cũ `(root): Kịch bản tham chiếu zone …` | Nay: *"Kịch bản 'Đèn bếp' (den_bep) đang trỏ tới vùng 'cam_bep/khuvuc_test' nhưng camera 'cam_bep' không có vùng 'khuvuc_test' (đang có: khu_bep). Cách sửa: …"* (test `test_scenario_pointing_at_missing_zone_says_how_to_fix`) |
| Kéo chấm giữa cạnh | Mô phỏng `pointerdown(200,32)` → chèn `[0.5, 0.1]` vào cạnh trên, kéo tiếp thành `[0.5, 0.4]` |
| **Vùng không tự nhảy khi bấm** | Mô phỏng DOM thật (`jsdom` + `index.html` + `app.js`): `active=1 (vung_2)` → `pointerdown` bên trong `toan_khung` → `active` **vẫn 1** (trước đây nhảy sang 0) |
| **Đa giác: kéo 1 điểm chỉ điểm đó chạy** | `[[0.252,0.137],…]` kéo góc 0 → chỉ `[0]` đổi, 4 điểm còn lại **nguyên** |
| **Đa giác: kéo chấm giữa cạnh** | Chèn đúng 1 điểm tại `[0.53,0.187]` trên cạnh, kéo tiếp → `[0.5,0.05]`, các điểm khác nguyên |
| **Hình chữ nhật: kéo góc** | `[0.05,0.05]…[0.95,0.95]` kéo góc 2 → `[[0.05,0.05],[0.75,0.05],[0.75,0.75],[0.05,0.75]]` (2 góc kề chạy theo, vẫn vuông góc); kéo vượt cạnh đối diện → **kẹp** ở `0.94`, không lật |
| **Hình chữ nhật: kéo chấm giữa cạnh** | Cạnh trên `y=0.05 → 0.3`, 3 điểm khác **nguyên**, vẫn đúng 4 điểm (không chèn điểm thứ 5) |
| **Con trỏ theo thao tác** | giữa vùng → `move` + nhãn “Kéo để di chuyển cả vùng”; điểm → `grab` + điểm sáng vàng + nhãn “Kéo để chỉnh điểm”; cạnh đa giác → con trỏ **+** (SVG) ; cạnh hình chữ nhật → `ns-resize`/`ew-resize` |
| **Kẹt bản nháp (nguyên nhân “không kéo được điểm”)** | Có bản nháp 1 điểm rồi bấm trúng **điểm** của vùng → bản nháp **tự huỷ**, thanh vàng ẩn, điểm vẫn kéo được (trước đây mọi cú bấm bị bản nháp “ăn” nên không kéo được gì) |
| **Bấm điểm của vùng khác** | `active=0`, bấm đúng điểm của `vung_2` → `active=1` và điểm đó dời theo chuột |
| **Lưu `shape`** | `ZoneConfig.shape: "polygon" \| "rect" \| null`; `POST …/zones` với `shape` → 200 và config giữ nguyên; `shape: blob` → **400** (`test_zone_save_keeps_shape_chosen_in_ui`) |
| **Undo / Delete** vẫn đúng | Xoá điểm 5→4, `Ctrl+Z` → về 5 điểm đúng toạ độ |
| **Nhãn nổi theo con trỏ** | Hiện khi ở trên vùng/điểm/cạnh, **ẩn** khi `pointerleave` hoặc ra ngoài vùng |

| Kéo góc hình chữ nhật | `[[0.1,0.1],[0.8,0.1],[0.8,0.8],[0.1,0.8]]` — vẫn `isAxisAlignedRect = true` (đã sửa lỗi kiểm tra "rect" **sau** khi đổi toạ độ) |
| Bấm trong vùng khác | Chuyển `ZE.active` sang vùng đó |
| Nhớ camera | `localStorage['va.liveCamera'] = live_sdp` → `currentLiveCamera()` trả `live_sdp` dù nó đứng thứ 2; camera bị xoá thì quay về camera còn lại và ghi nhớ lại |
| Cache trình duyệt | `GET /` trả `/static/app.js?v=<mtime>` và `/static/style.css?v=<mtime>` (`Cache-Control: no-cache`) |

**Docker (đã chạy thật):** image `vision-automation:latest` (~10.6 GB vì có torch) build
bằng legacy builder (`DOCKER_BUILDKIT=0 docker build -t vision-automation:latest .`) vì
sandbox chặn thư mục hoạt động của buildx. Container chạy với nguồn `image`:

| Kiểm tra | Kết quả |
|---|---|
| `GET /` và `GET /api/state` từ máy host (map cổng 8123) | **200** |
| Model trong container | `yolo11n.pt` trên **cpu**, warmup 2.4 s, tự tải model lần đầu |
| Nhận diện | `bus` 0.888, vùng `khu_nguoi` → `occupied=true, count=2`, suy luận 104 ms @ 2 fps |
| Kịch bản | bắn `test/den/set {"state":"ON"}` (dry-run) |
| `GET /stream/cam_anh.mjpg` | 39 khung/4 s (~10 fps), 7.2 MB |

Lưu ý: với **bridge network** container không thấy broker ở `127.0.0.1:1883`
(`mqtt.connected=false`) — đó là lý do `docker-compose.yml` dùng `network_mode: host`
(mục 13). Khi chạy compose trên mini-PC/NAS: `docker compose up -d && docker compose logs -f`.
Luôn truyền `--no-webcam` (hoặc `with_webcam: false`) vì container **không** truy cập được
webcam của macOS.

---

## Giấy phép & ghi công

- Mã nguồn trong repo này: dùng tự do cho mục đích cá nhân.
- [Ultralytics YOLO](https://github.com/ultralytics/ultralytics) — AGPL-3.0.
- [OpenCV](https://opencv.org/), [FastAPI](https://fastapi.tiangolo.com/),
  [paho-mqtt](https://github.com/eclipse/paho.mqtt.python),
  [MediaMTX](https://github.com/bluenviron/mediamtx).
