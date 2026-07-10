#!/bin/bash
set -u

cd "$(dirname "$0")" || exit 1

APP_NAME="Infinite Agent Work"
PORT="3000"
LOCAL_URL="http://127.0.0.1:${PORT}/"
PYEXE=""

echo "============================================"
echo "   ${APP_NAME}"
echo "============================================"
echo ""

find_python() {
  if [ -n "${PYEXE_OVERRIDE:-}" ] && [ -x "${PYEXE_OVERRIDE}" ]; then
    PYEXE="${PYEXE_OVERRIDE}"
    return 0
  fi

  if [ -x "./python/bin/python3" ]; then
    PYEXE="./python/bin/python3"
    return 0
  fi

  if [ -x "./python/python3" ]; then
    PYEXE="./python/python3"
    return 0
  fi

  if [ -x "./.venv/bin/python" ]; then
    PYEXE="./.venv/bin/python"
    return 0
  fi

  if command -v python3 >/dev/null 2>&1; then
    PYEXE="$(command -v python3)"
    return 0
  fi

  return 1
}

check_python_version() {
  "${PYEXE}" - <<'PY' >/dev/null 2>&1
import sys
raise SystemExit(0 if sys.version_info >= (3, 10) else 1)
PY
}

ensure_pip() {
  if "${PYEXE}" -m pip --version >/dev/null 2>&1; then
    return 0
  fi

  echo "[INFO] pip not found. Trying to enable pip..."
  "${PYEXE}" -m ensurepip --upgrade
}

ensure_dependencies() {
  if "${PYEXE}" - <<'PY' >/dev/null 2>&1
import fastapi, uvicorn, httpx, PIL, requests, pydantic, multipart, websockets
PY
  then
    return 0
  fi

  echo "[INFO] Installing required Python packages..."
  if [ -d packages ]; then
    if "${PYEXE}" -m pip install --no-index --find-links=packages -r requirements.txt; then
      return 0
    fi
    echo "[WARN] Offline install failed. Trying online install..."
  fi

  "${PYEXE}" -m pip install -r requirements.txt
}

detect_lan_ip() {
  LAN_IP="$(ipconfig getifaddr en0 2>/dev/null || true)"
  if [ -z "${LAN_IP}" ]; then
    LAN_IP="$(ipconfig getifaddr en1 2>/dev/null || true)"
  fi
  if [ -z "${LAN_IP}" ]; then
    LAN_IP="$(route get default 2>/dev/null | awk '/interface:/{print $2}' | xargs -I{} ipconfig getifaddr {} 2>/dev/null | head -n 1)"
  fi
  if [ -z "${LAN_IP}" ]; then
    LAN_IP="127.0.0.1"
  fi
}

if ! find_python; then
  echo "[ERROR] Python 3.10+ was not found."
  echo ""
  echo "Install Python from https://www.python.org/downloads/"
  echo "Or place a bundled runtime at ./python/bin/python3"
  echo ""
  read -r -p "Press Enter to exit..."
  exit 1
fi

if ! check_python_version; then
  echo "[ERROR] Python 3.10+ is required."
  "${PYEXE}" --version
  echo ""
  read -r -p "Press Enter to exit..."
  exit 1
fi

if ! ensure_pip; then
  echo "[ERROR] Could not enable pip for this Python runtime."
  echo ""
  read -r -p "Press Enter to exit..."
  exit 1
fi

if ! ensure_dependencies; then
  echo "[ERROR] Dependency installation failed."
  echo "Check your network, or put offline wheels in the packages folder."
  echo ""
  read -r -p "Press Enter to exit..."
  exit 1
fi

if [ -n "${LAUNCHER_CHECK_ONLY:-}" ]; then
  echo "[OK] Launcher checks passed."
  exit 0
fi

detect_lan_ip
APP_URL="http://${LAN_IP}:${PORT}/"

echo ""
echo "Visit: ${APP_URL}"
echo "Local: ${LOCAL_URL}"
echo "Press Ctrl+C to stop."
echo ""

( sleep 3 && open "${APP_URL}" >/dev/null 2>&1 ) &

"${PYEXE}" main.py
EXIT_CODE=$?

echo ""
echo "Server stopped. Exit code: ${EXIT_CODE}"
read -r -p "Press Enter to exit..."
exit "${EXIT_CODE}"
