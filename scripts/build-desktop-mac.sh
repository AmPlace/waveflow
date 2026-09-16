#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

echo "== Build frontend =="
cd frontend
npm install
npm run build -- --mode desktop
cd "$ROOT_DIR"

echo "== Build backend =="
cd backend

# Keep PyInstaller's writable cache in the build workspace's temp area rather
# than relying on a pre-existing user cache with unknown ownership.
export PYINSTALLER_CONFIG_DIR="${PYINSTALLER_CONFIG_DIR:-${TMPDIR:-/tmp}/waveflow-pyinstaller-config}"

if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi

.venv/bin/pip install -r requirements.txt
.venv/bin/pip install pyinstaller

.venv/bin/pyinstaller \
  --clean \
  --noconfirm \
  --onedir \
  --hidden-import adapters.17live --collect-data zhconv \
  --add-data "config:config" --add-data "official_plugins:official_plugins" \
  --name waveflow-backend \
  desktop_entry.py

cd "$ROOT_DIR"

echo "== Copy backend binary =="
BACKEND_DIST="$ROOT_DIR/backend_dist"
BACKEND_DIST_STAGING="$(mktemp -d "$ROOT_DIR/.backend_dist.staging.XXXXXX")"
BACKEND_DIST_PREVIOUS=""
PUBLISHED_BACKEND_DIST=0

restore_backend_dist() {
  local exit_code=$?
  if [[ "$PUBLISHED_BACKEND_DIST" != "1" ]]; then
    rm -rf -- "$BACKEND_DIST_STAGING"
    if [[ -n "$BACKEND_DIST_PREVIOUS" && ! -e "$BACKEND_DIST" && -e "$BACKEND_DIST_PREVIOUS" ]]; then
      mv "$BACKEND_DIST_PREVIOUS" "$BACKEND_DIST"
    fi
  fi
  exit "$exit_code"
}
trap restore_backend_dist EXIT

cp -a backend/dist/waveflow-backend/. "$BACKEND_DIST_STAGING/"
chmod +x "$BACKEND_DIST_STAGING/waveflow-backend"

echo "== Bundle controlled Python runtime =="
if [ -z "${WAVEFLOW_DESKTOP_PYTHON_RUNTIME_PKG:-}" ]; then
  echo "ERROR: set WAVEFLOW_DESKTOP_PYTHON_RUNTIME_PKG to the locked official CPython 3.14 macOS .pkg" >&2
  exit 1
fi
if [ -z "${WAVEFLOW_DESKTOP_CA_BUNDLE_WHEEL:-}" ]; then
  echo "ERROR: set WAVEFLOW_DESKTOP_CA_BUNDLE_WHEEL to the locked certifi wheel" >&2
  exit 1
fi
bash "$ROOT_DIR/scripts/build-desktop-python-runtime.sh" \
  "$WAVEFLOW_DESKTOP_PYTHON_RUNTIME_PKG" \
  "$BACKEND_DIST_STAGING/python-runtime" \
  "${WAVEFLOW_DESKTOP_PYTHON_RUNTIME_LOCK:-$ROOT_DIR/desktop_runtime/cpython-3.14.7-macos11-arm64.json}" \
  "${WAVEFLOW_DESKTOP_CA_BUNDLE_WHEEL:-}"
bash "$ROOT_DIR/scripts/verify-desktop-python-runtime.sh" "$BACKEND_DIST_STAGING/python-runtime"

echo "== Bundle ffmpeg =="
FFMPEG_SRC=$(ls "$ROOT_DIR/ffmpeg/macos-arm64/ffmpeg" 2>/dev/null)
if [ -z "$FFMPEG_SRC" ]; then
  echo "  Downloading ffmpeg static build for macOS arm64..."
  bash "$ROOT_DIR/scripts/download-ffmpeg.sh" mac
  FFMPEG_SRC="$ROOT_DIR/ffmpeg/macos-arm64/ffmpeg"
fi
if [ -f "$FFMPEG_SRC" ]; then
  cp "$FFMPEG_SRC" "$BACKEND_DIST_STAGING/ffmpeg"
  chmod +x "$BACKEND_DIST_STAGING/ffmpeg"
  echo "  Bundled: $(file "$BACKEND_DIST_STAGING/ffmpeg" | cut -d: -f2-)"
else
  echo "  WARNING: ffmpeg not found, RTSP/HLS will NOT work"
fi

echo "== Publish complete backend bundle =="
if [[ -e "$BACKEND_DIST" || -L "$BACKEND_DIST" ]]; then
  BACKEND_DIST_PREVIOUS="$(mktemp -d "${TMPDIR:-/tmp}/waveflow-backend-dist.previous.XXXXXX")"
  rmdir "$BACKEND_DIST_PREVIOUS"
  mv "$BACKEND_DIST" "$BACKEND_DIST_PREVIOUS"
fi
mv "$BACKEND_DIST_STAGING" "$BACKEND_DIST"
BACKEND_DIST_STAGING=""
PUBLISHED_BACKEND_DIST=1
if [[ -n "$BACKEND_DIST_PREVIOUS" ]]; then
  rm -rf -- "$BACKEND_DIST_PREVIOUS"
  BACKEND_DIST_PREVIOUS=""
fi

echo "== Install desktop deps =="
npm install

echo "== Build Electron main/preload =="
npm run build:electron

echo "== Build unpacked mac app =="
npx electron-builder --mac dir --arm64

APP_DIR="$ROOT_DIR/release/mac-arm64/WaveFlow.app"
echo "== Sign packaged mac app =="
bash "$ROOT_DIR/scripts/sign-desktop-macos-app.sh" "$APP_DIR"

echo "== Build mac zip from the signed app =="
npx electron-builder --prepackaged "$APP_DIR" --mac zip --arm64

echo "== Build mac DMG =="
DMG_BUILD_RC=0
if npx electron-builder --prepackaged "$APP_DIR" --mac dmg --arm64; then
  :
else
  DMG_BUILD_RC=$?
  echo "WARNING: DMG build is pending on a release host with working hdiutil" >&2
  if [[ "${WAVEFLOW_DESKTOP_REQUIRE_DMG:-0}" == "1" ]]; then
    exit "$DMG_BUILD_RC"
  fi
fi

echo "== Done =="
