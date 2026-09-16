#!/usr/bin/env bash
set -euo pipefail

# Build a relocatable runtime from an explicit official python.org installer.
# This script never searches PATH for a Python runtime source, downloads a
# package, or invokes Homebrew.  Download/provenance policy belongs to the
# release runner; this step verifies the locked source and transforms it.
SOURCE_PKG="${1:?usage: build-desktop-python-runtime.sh SOURCE_PKG DEST_DIR [LOCK_FILE] [CA_BUNDLE_WHEEL]}"
DEST_DIR="${2:?usage: build-desktop-python-runtime.sh SOURCE_PKG DEST_DIR [LOCK_FILE] [CA_BUNDLE_WHEEL]}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
LOCK_FILE="${3:-$ROOT_DIR/desktop_runtime/cpython-3.14.7-macos11-arm64.json}"
CA_BUNDLE_WHEEL="${4:-${WAVEFLOW_DESKTOP_CA_BUNDLE_WHEEL:-}}"

if [[ "$(uname -s)" != "Darwin" || "$(uname -m)" != "arm64" ]]; then
  echo "Formal Desktop runtime currently supports macOS arm64 only" >&2
  exit 1
fi
if [[ ! -f "$SOURCE_PKG" || "${SOURCE_PKG##*.}" != "pkg" ]]; then
  echo "SOURCE_PKG must be an explicit official macOS .pkg artifact" >&2
  exit 1
fi
if [[ ! -f "$LOCK_FILE" ]]; then
  echo "Runtime provenance lock is missing: $LOCK_FILE" >&2
  exit 1
fi

read -r EXPECTED_VERSION EXPECTED_SHA256 EXPECTED_URL EXPECTED_FILENAME EXPECTED_PROVENANCE_URL EXPECTED_SIGSTORE_URL CA_VERSION CA_FILENAME CA_SHA256 CA_URL CA_PEM_PATH CA_PEM_SHA256 < <(
  python3 - "$LOCK_FILE" <<'PY'
import json, sys
lock = json.load(open(sys.argv[1], encoding="utf-8"))
source = lock["source"]
ca = lock["ca_bundle"]
print(
    lock["python_version"],
    source["sha256"],
    source["url"],
    source["filename"],
    source.get("provenance_url", "-"),
    source.get("sigstore_url", "-"),
    ca["version"],
    ca["filename"],
    ca["sha256"],
    ca["url"],
    ca["pem_path"],
    ca["pem_sha256"],
)
PY
)
if [[ "$(basename "$SOURCE_PKG")" != "$EXPECTED_FILENAME" ]]; then
  echo "Runtime source filename does not match provenance lock" >&2
  exit 1
fi
ACTUAL_SHA256="$(shasum -a 256 "$SOURCE_PKG" | awk '{print $1}')"
if [[ "$ACTUAL_SHA256" != "$EXPECTED_SHA256" ]]; then
  echo "Runtime source SHA-256 does not match provenance lock" >&2
  exit 1
fi
if [[ -z "$CA_BUNDLE_WHEEL" || ! -f "$CA_BUNDLE_WHEEL" ]]; then
  echo "An explicit locked certifi wheel is required: WAVEFLOW_DESKTOP_CA_BUNDLE_WHEEL" >&2
  exit 1
fi
if [[ "$(basename "$CA_BUNDLE_WHEEL")" != "$CA_FILENAME" ]]; then
  echo "CA bundle wheel filename does not match provenance lock" >&2
  exit 1
fi
CA_ACTUAL_SHA256="$(shasum -a 256 "$CA_BUNDLE_WHEEL" | awk '{print $1}')"
if [[ "$CA_ACTUAL_SHA256" != "$CA_SHA256" ]]; then
  echo "CA bundle wheel SHA-256 does not match provenance lock" >&2
  exit 1
fi
if [[ "$CA_PEM_PATH" = /* || "$CA_PEM_PATH" == *..* ]]; then
  echo "CA bundle member path is invalid" >&2
  exit 1
fi

WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/waveflow-python-runtime.XXXXXX")"
cleanup() { rm -rf -- "$WORK_DIR"; }
trap cleanup EXIT
mkdir -p "$WORK_DIR/pkg" "$WORK_DIR/framework-root/Library/Frameworks"
xar -xf "$SOURCE_PKG" -C "$WORK_DIR/pkg"
PAYLOAD="$WORK_DIR/pkg/Python_Framework.pkg/Payload"
if [[ ! -f "$PAYLOAD" ]]; then
  echo "Official package does not contain Python_Framework.pkg/Payload" >&2
  exit 1
fi
mkdir -p "$WORK_DIR/framework-root/Library/Frameworks/Python.framework"
tar -xzf "$PAYLOAD" -C "$WORK_DIR/framework-root/Library/Frameworks/Python.framework"

SOURCE_FRAMEWORK="$WORK_DIR/framework-root/Library/Frameworks/Python.framework"
SOURCE_PYTHON="$SOURCE_FRAMEWORK/Versions/3.14/bin/python3.14"
if [[ ! -f "$SOURCE_PYTHON" ]]; then
  echo "Official package does not contain CPython 3.14" >&2
  exit 1
fi
PACKAGE_VERSION="$(grep -o 'CFBundleShortVersionString="[^"]*"' "$WORK_DIR/pkg/Python_Framework.pkg/PackageInfo" | head -1 | cut -d'"' -f2)"
if [[ "$PACKAGE_VERSION" != "$EXPECTED_VERSION" ]]; then
  echo "Runtime package version mismatch: $PACKAGE_VERSION" >&2
  exit 1
fi

STAGE="$WORK_DIR/runtime"
mkdir -p "$STAGE/framework" "$STAGE/bin"
cp -a "$SOURCE_FRAMEWORK" "$STAGE/framework/Python.framework"
CA_FILE="$STAGE/framework/Python.framework/Versions/3.14/etc/openssl/cert.pem"
mkdir -p "$(dirname "$CA_FILE")"
unzip -p "$CA_BUNDLE_WHEEL" "$CA_PEM_PATH" > "$WORK_DIR/cert.pem"
CA_ACTUAL_PEM_SHA256="$(shasum -a 256 "$WORK_DIR/cert.pem" | awk '{print $1}')"
if [[ "$CA_ACTUAL_PEM_SHA256" != "$CA_PEM_SHA256" || ! -s "$WORK_DIR/cert.pem" ]]; then
  echo "CA bundle PEM does not match provenance lock" >&2
  exit 1
fi
cp "$WORK_DIR/cert.pem" "$CA_FILE"
chmod 0644 "$CA_FILE"
# Python.org normally asks the user to run Install Certificates.  The
# release build performs the same controlled data installation from the
# verified wheel, without pip or a runtime network/build step.
cat > "$STAGE/framework/Python.framework/Versions/3.14/lib/python3.14/sitecustomize.py" <<'PY'
"""Use the relocatable runtime's verified CA bundle for stdlib TLS."""
from __future__ import annotations

import os
import sys
from pathlib import Path


if not os.environ.get("SSL_CERT_FILE"):
    _candidate = Path(getattr(sys, "base_prefix", sys.prefix)) / "etc" / "openssl" / "cert.pem"
    if _candidate.is_file():
        os.environ["SSL_CERT_FILE"] = str(_candidate)
PY
# The upstream installer carries development-only PrivateHeaders links whose
# targets are not shipped.  They are not needed by an embedded runtime and
# make the final signed framework look incomplete to strict codesign.
rm -f \
  "$STAGE/framework/Python.framework/Versions/3.14/Frameworks/Tcl.framework/PrivateHeaders" \
  "$STAGE/framework/Python.framework/Versions/3.14/Frameworks/Tk.framework/PrivateHeaders"
python3 "$ROOT_DIR/scripts/relocate_macos_python_framework.py" \
  "$STAGE/framework/Python.framework" \
  --codesign-identity "${WAVEFLOW_DESKTOP_RUNTIME_CODESIGN_IDENTITY:--}"
ln -s ../framework/Python.framework/Versions/3.14/bin/python3.14 "$STAGE/bin/python3.14"

STAGE_PYTHON="$STAGE/bin/python3.14"
PYTHONDONTWRITEBYTECODE=1 SSL_CERT_FILE="$CA_FILE" "$STAGE_PYTHON" -B -I -S -c 'import platform, ssl, sys; assert sys.version_info[:2] == (3, 14); assert platform.machine() == "arm64"; assert ssl.get_default_verify_paths().cafile; ssl.create_default_context(); print(ssl.OPENSSL_VERSION)' >/dev/null

PYTHONPATH="$ROOT_DIR/backend" python3 - "$STAGE" "$EXPECTED_VERSION" "$EXPECTED_URL" "$EXPECTED_FILENAME" "$EXPECTED_SHA256" "$EXPECTED_PROVENANCE_URL" "$EXPECTED_SIGSTORE_URL" "$CA_VERSION" "$CA_FILENAME" "$CA_SHA256" "$CA_URL" "$CA_PEM_PATH" "$CA_PEM_SHA256" <<'PY'
import json, sys
from pathlib import Path
from desktop_runtime_manifest import runtime_tree_digest

root = Path(sys.argv[1])
version = sys.argv[2]
url = sys.argv[3]
filename = sys.argv[4]
source_sha256 = sys.argv[5]
provenance_url = sys.argv[6]
sigstore_url = sys.argv[7]
ca_version = sys.argv[8]
ca_filename = sys.argv[9]
ca_sha256 = sys.argv[10]
ca_url = sys.argv[11]
ca_pem_path = sys.argv[12]
ca_pem_sha256 = sys.argv[13]
tree_sha256, tree_file_count = runtime_tree_digest(root)
metadata = {
    "schema_version": 1,
    "runtime_type": "python",
    "python_version": version,
    "python_version_range": ">=3.14.0 <3.15.0",
    "python_abi": "cp314",
    "os": "macos",
    "arch": "arm64",
    "executable": "bin/python3.14",
    "source": {
        "kind": "python.org official macOS installer",
        "url": url,
        "filename": filename,
        "sha256": source_sha256,
        "provenance_url": None if provenance_url == "-" else provenance_url,
        "sigstore_url": None if sigstore_url == "-" else sigstore_url,
    },
    "ca_bundle": {
        "package": "certifi",
        "version": ca_version,
        "filename": ca_filename,
        "url": ca_url,
        "sha256": ca_sha256,
        "pem_path": ca_pem_path,
        "pem_sha256": ca_pem_sha256,
        "runtime_path": "framework/Python.framework/Versions/3.14/etc/openssl/cert.pem",
    },
    "tree_sha256": tree_sha256,
    "tree_file_count": tree_file_count,
}
(root / "runtime.json").write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(json.dumps(metadata, sort_keys=True))
PY

FINAL_PARENT="$(cd "$(dirname "$DEST_DIR")" && pwd -P)"
FINAL_NAME="$(basename "$DEST_DIR")"
mkdir -p "$FINAL_PARENT"
rm -rf -- "$FINAL_PARENT/.${FINAL_NAME}.staging" "$FINAL_PARENT/.${FINAL_NAME}.previous"
mv "$STAGE" "$FINAL_PARENT/.${FINAL_NAME}.staging"
if [[ -e "$DEST_DIR" || -L "$DEST_DIR" ]]; then
  mv "$DEST_DIR" "$FINAL_PARENT/.${FINAL_NAME}.previous"
fi
if ! mv "$FINAL_PARENT/.${FINAL_NAME}.staging" "$DEST_DIR"; then
  if [[ -e "$FINAL_PARENT/.${FINAL_NAME}.previous" ]]; then
    mv "$FINAL_PARENT/.${FINAL_NAME}.previous" "$DEST_DIR"
  fi
  exit 1
fi
rm -rf -- "$FINAL_PARENT/.${FINAL_NAME}.previous"

ARTIFACT_OUTPUT="${WAVEFLOW_DESKTOP_PYTHON_RUNTIME_ARTIFACT:-}"
if [[ -n "$ARTIFACT_OUTPUT" ]]; then
  mkdir -p "$(dirname "$ARTIFACT_OUTPUT")"
  tar -czf "$ARTIFACT_OUTPUT" -C "$FINAL_PARENT" "$FINAL_NAME"
  ARTIFACT_SHA256="$(shasum -a 256 "$ARTIFACT_OUTPUT" | awk '{print $1}')"
  printf '{"filename":"%s","sha256":"%s","source_sha256":"%s"}\n' \
    "$(basename "$ARTIFACT_OUTPUT")" "$ARTIFACT_SHA256" "$EXPECTED_SHA256" \
    > "${ARTIFACT_OUTPUT}.json"
  echo "Runtime artifact: $ARTIFACT_OUTPUT ($ARTIFACT_SHA256)"
fi
echo "Built relocatable CPython $EXPECTED_VERSION runtime: $DEST_DIR"
