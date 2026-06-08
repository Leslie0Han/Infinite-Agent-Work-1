#!/bin/bash
cd "$(dirname "$0")"
echo "============================================"
echo "   Starting ComfyUI-API-Modelscope"
echo "============================================"
echo ""

if ! command -v python3 >/dev/null 2>&1; then
  echo "[ERROR] Python 3 not found. Please install Python 3.10+ first."
  echo "Download: https://www.python.org/downloads/"
  echo ""
  read -p "Press Enter to exit..."
  exit 1
fi

if ! python3 - <<'PY' >/dev/null 2>&1
import fastapi, uvicorn, httpx, PIL
PY
then
  echo "[ERROR] Dependencies are missing."
  echo "Please run: ./mac-安装依赖.sh"
  echo ""
  read -p "Press Enter to exit..."
  exit 1
fi

LAN_IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null)"
if [ -z "$LAN_IP" ]; then
  LAN_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
fi
if [ -z "$LAN_IP" ]; then
  LAN_IP="127.0.0.1"
fi
APP_URL="http://${LAN_IP}:3000/"

echo "Visit: ${APP_URL}"
echo "Local: http://127.0.0.1:3000/"
echo "Press Ctrl+C to stop."
echo ""

# Open browser after 3 seconds
sleep 3 && open "${APP_URL}" &

python3 main.py
EXIT_CODE=$?

echo ""
echo "Server stopped. Exit code: ${EXIT_CODE}"
read -p "Press Enter to exit..."
