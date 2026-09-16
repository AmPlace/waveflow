#!/usr/bin/env bash
set -euo pipefail

RUNTIME_ROOT="${1:?usage: verify-desktop-python-runtime.sh RUNTIME_ROOT}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
PYTHON="$RUNTIME_ROOT/bin/python3.14"

if [[ ! -e "$PYTHON" || ! -x "$PYTHON" ]]; then
  echo "runtime executable is missing or not executable" >&2
  exit 1
fi
PYTHONPATH="$ROOT_DIR/backend" python3 - "$RUNTIME_ROOT" <<'PY'
import hashlib
import json, sys
from pathlib import Path
from desktop_runtime_manifest import runtime_tree_digest
root = Path(sys.argv[1]).resolve()
metadata = json.loads((root / "runtime.json").read_text(encoding="utf-8"))
actual, count = runtime_tree_digest(root)
assert metadata["tree_sha256"] == actual
assert metadata["tree_file_count"] == count
assert metadata["executable"] == "bin/python3.14"
ca = metadata["ca_bundle"]
ca_file = (root / ca["runtime_path"]).resolve()
assert ca_file.is_relative_to(root)
assert hashlib.sha256(ca_file.read_bytes()).hexdigest() == ca["pem_sha256"]
print(json.dumps({"tree_sha256": actual, "tree_file_count": count}, sort_keys=True))
PY

PYTHONDONTWRITEBYTECODE=1 "$PYTHON" -B -I -c 'import platform, ssl, sys; assert sys.version_info[:2] == (3, 14); assert platform.machine() == "arm64"; assert ssl.get_default_verify_paths().cafile; ssl.create_default_context(); print(sys.executable); print(sys.prefix); print(ssl.OPENSSL_VERSION)'

if find "$RUNTIME_ROOT/framework/Python.framework" -type f -print0 | while IFS= read -r -d '' file; do
  file -b "$file" | grep -q 'Mach-O' || continue
  otool -L "$file" 2>/dev/null
done | grep -q '/Library/Frameworks/Python.framework/Versions/3.14'; then
  echo "runtime contains an absolute build/install framework reference" >&2
  exit 1
fi
codesign --verify --deep --strict "$RUNTIME_ROOT/framework/Python.framework"
echo "Desktop Python runtime verification: PASS"
