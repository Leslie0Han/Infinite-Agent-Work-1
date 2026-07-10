#!/bin/bash
set -u

cd "$(dirname "$0")" || exit 1

PYEXE=""

echo "============================================"
echo "   Installing dependencies"
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

if ! find_python; then
  echo "[ERROR] Python 3.10+ was not found."
  echo "Install Python from https://www.python.org/downloads/"
  read -r -p "Press Enter to exit..."
  exit 1
fi

if ! "${PYEXE}" - <<'PY' >/dev/null 2>&1
import sys
raise SystemExit(0 if sys.version_info >= (3, 10) else 1)
PY
then
  echo "[ERROR] Python 3.10+ is required."
  "${PYEXE}" --version
  read -r -p "Press Enter to exit..."
  exit 1
fi

"${PYEXE}" --version

echo ""
echo "[1/2] Checking pip..."
if ! "${PYEXE}" -m pip --version >/dev/null 2>&1; then
  echo "pip not found. Trying to enable pip..."
  "${PYEXE}" -m ensurepip --upgrade || {
    echo "[ERROR] Could not enable pip."
    read -r -p "Press Enter to exit..."
    exit 1
  }
fi

echo "[2/2] Installing dependencies..."
if [ -d packages ]; then
  if "${PYEXE}" -m pip install --no-index --find-links=packages -r requirements.txt; then
    echo ""
    echo "Done. Run './mac-启动服务.sh' or double-click 'mac-启动服务.command'."
    read -r -p "Press Enter to exit..."
    exit 0
  fi
  echo "[WARN] Offline install failed. Trying online install..."
fi

if ! "${PYEXE}" -m pip install -r requirements.txt; then
  echo ""
  echo "[ERROR] Dependency installation failed."
  echo "Check your network, or put offline wheels in the packages folder."
  read -r -p "Press Enter to exit..."
  exit 1
fi

echo ""
echo "Done. Run './mac-启动服务.sh' or double-click 'mac-启动服务.command'."
echo "If macOS says permission denied, run:"
echo "  chmod +x ./mac-安装依赖.sh ./mac-启动服务.sh ./mac-启动服务.command"
read -r -p "Press Enter to exit..."
