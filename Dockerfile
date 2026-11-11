# =============================================================================
# Vision Automation — image chạy 24/7 trên NAS / mini-PC / Raspberry Pi / server
#
#   docker compose up -d --build
#   # mở http://<ip-máy-chủ>:8080
#
# Lưu ý quan trọng về camera:
#   * network_mode: host là BẮT BUỘC trên Linux nếu muốn dùng camera IP trong LAN,
#     multicast/mDNS (ONVIF) và cổng MQTT/Web ổn định.
#   * Trên macOS, Docker Desktop chạy trong máy ảo nên KHÔNG truy cập được webcam
#     của laptop và cũng không tới được camera IP trong LAN một cách đáng tin cậy.
#     => Trên macOS hãy chạy NATIVE bằng Python (xem README).
# =============================================================================
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app \
    YOLO_CONFIG_DIR=/data/.yolo \
    OMP_NUM_THREADS=2 \
    MALLOC_ARENA_MAX=2

# OpenCV headless cần libglib; ffmpeg giúp xử lý RTSP/file ổn định hơn
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        libglib2.0-0 \
        libgomp1 \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Cài thư viện trước để tận dụng cache layer (torch rất nặng)
COPY requirements-docker.txt ./
RUN pip install --upgrade pip \
    && pip install -r requirements-docker.txt

COPY app ./app
COPY config.example.yaml ./config.example.yaml

# Config, log, snapshot và model nằm ngoài image để không mất khi rebuild
VOLUME ["/data"]
RUN mkdir -p /data/logs /data/snapshots /data/.yolo
WORKDIR /data

EXPOSE 8080

# Healthcheck: API trạng thái công khai (không cần đăng nhập)
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${VISION_PORT:-8080}/api/state/public" || exit 1

# --no-webcam: trong container không có webcam; camera IP khai báo qua Web UI
ENTRYPOINT ["python", "-m", "app"]
CMD ["--config", "/data/config.yaml", "--no-webcam"]
