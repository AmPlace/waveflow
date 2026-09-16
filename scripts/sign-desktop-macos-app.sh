#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${1:?usage: sign-desktop-macos-app.sh WaveFlow.app}"
IDENTITY="${WAVEFLOW_DESKTOP_CODESIGN_IDENTITY:--}"

if [[ ! -d "$APP_DIR/Contents/Resources/backend/python-runtime/framework/Python.framework" ]]; then
  echo "Desktop Python runtime is missing from the packaged app" >&2
  exit 1
fi

# electron-builder intentionally removes release-local signatures while it
# assembles the .app.  Sign the embedded framework first, then the app.  A
# real Developer ID identity can be supplied by the release host; '-' is only
# the explicit local/ad-hoc structural validation mode.
codesign --deep --force --sign "$IDENTITY" \
  "$APP_DIR/Contents/Resources/backend/python-runtime/framework/Python.framework"
codesign --deep --force --sign "$IDENTITY" "$APP_DIR"
codesign --verify --deep --strict "$APP_DIR"
echo "Desktop macOS app signing/verification: PASS (identity=$IDENTITY)"
