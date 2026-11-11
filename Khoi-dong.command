#!/bin/bash
# ============================================================================
#  VISION AUTOMATION — khởi động bằng cách BẤM ĐÚP vào file này (macOS).
#  Không cần biết dòng lệnh. Cần dừng: bấm Ctrl + C ở cửa sổ Terminal vừa mở.
# ============================================================================
set -u
cd "$(dirname "$0")" || exit 1

PORT="${PORT:-8080}"

ARGS=("--with-webcam")
for arg in "$@"; do
  case "$arg" in
    --no-webcam) ARGS=("--no-webcam") ;;
    --dry-run) ARGS+=("--dry-run") ;;
  esac
done

echo "================================================================"
echo "  VISION AUTOMATION — đang khởi động"
echo "  Thư mục: $(pwd)"
echo "  Cổng web: ${PORT}"
echo "================================================================"
echo

# --- Nếu app đã chạy sẵn trên cổng này: chỉ mở trình duyệt, không chạy trùng ---
if curl -fsS -o /dev/null --max-time 2 "http://127.0.0.1:${PORT}/" 2>/dev/null; then
  echo "ℹ️  Ứng dụng đã chạy sẵn trên cổng ${PORT}."
  [ "${NO_BROWSER:-0}" = "1" ] || open "http://127.0.0.1:${PORT}/"
  echo "   Đã mở lại giao diện: http://127.0.0.1:${PORT}"
  echo
  read -r -p "Nhấn Enter để đóng cửa sổ này..." _
  exit 0
fi

# --- Kiểm tra môi trường Python ---
PY=".venv/bin/python"
if [ ! -x "$PY" ]; then
  echo "❌ Chưa có môi trường Python của dự án (thư mục .venv)."
  echo
  echo "   Cách sửa — copy 3 dòng sau dán vào Terminal rồi Enter:"
  echo "     cd \"$(pwd)\""
  echo "     export UV_CACHE_DIR=\"\$PWD/.uvcache\" && uv venv --python 3.12 .venv"
  echo "     .venv/bin/python -m pip install -r requirements.txt"
  echo
  read -r -p "Nhấn Enter để đóng..." _
  exit 1
fi

if [ ! -f config.yaml ]; then
  echo "ℹ️  Lần chạy đầu tiên: sẽ tự tạo file config.yaml và mở trang “Thiết lập lần đầu”."
  echo
fi

# --- Tự mở trình duyệt khi web server đã sẵn sàng ---
(
  for _ in $(seq 1 240); do
    if curl -fsS -o /dev/null --max-time 1 "http://127.0.0.1:${PORT}/" 2>/dev/null; then
      [ "${NO_BROWSER:-0}" = "1" ] || open "http://127.0.0.1:${PORT}/"
      exit 0
    fi
    sleep 0.5
  done
) &

echo "→ Giao diện sẽ tự mở trong trình duyệt: http://127.0.0.1:${PORT}"
echo "→ Nếu trình duyệt không tự mở, hãy mở tay địa chỉ trên."
echo "→ ĐỂ DỪNG: bấm Ctrl + C tại cửa sổ này."
echo

"$PY" -m app --config config.yaml --port "$PORT" "${ARGS[@]}"
CODE=$?

echo
if [ "$CODE" -eq 0 ]; then
  echo "✅ Đã dừng an toàn (báo 'offline' cho MQTT và lưu cấu hình)."
else
  echo "⚠️  Ứng dụng dừng với mã lỗi ${CODE}."
  echo "   Xem chi tiết: logs/vision-automation.log"
  echo "   Hoặc mở file HUONG-DAN-NHANH.md phần “Lỗi thường gặp”."
fi
read -r -p "Nhấn Enter để đóng cửa sổ..." _
