#!/usr/bin/env bash
set -euo pipefail

# Compatibility name for the Desktop build pipeline.  Only an explicit
# official .pkg is accepted; source directories/Homebrew runtimes are no
# longer valid inputs.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
exec "$SCRIPT_DIR/build-desktop-python-runtime.sh" "$@"
