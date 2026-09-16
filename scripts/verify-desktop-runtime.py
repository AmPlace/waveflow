#!/usr/bin/env python3
"""Verify one staged Desktop runtime using the common manifest contract."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from desktop_runtime_manifest import (  # noqa: E402
    DesktopRuntimeManifestError,
    desktop_runtime_target,
    validate_runtime_metadata,
)


def _safe_runtime_file(root: Path, relative: str) -> Path:
    path = PurePosixPath(relative)
    if path.is_absolute() or "\\" in relative or any(part in {"", ".", ".."} for part in path.parts):
        raise DesktopRuntimeManifestError("runtime certificate path is invalid")
    candidate = (root.joinpath(*path.parts)).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise DesktopRuntimeManifestError("runtime certificate path escapes its root") from exc
    return candidate


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("runtime_root", type=Path)
    parser.add_argument("platform_os")
    parser.add_argument("arch")
    parser.add_argument("expected_python_version")
    args = parser.parse_args()

    root = args.runtime_root.resolve()
    metadata = json.loads((root / "runtime.json").read_text(encoding="utf-8"))
    target = desktop_runtime_target(args.platform_os, args.arch)
    if target is None:
        raise DesktopRuntimeManifestError("unsupported Desktop runtime target")
    executable = validate_runtime_metadata(
        root,
        metadata,
        target,
        expected_python_version=args.expected_python_version,
    )
    ca_bundle = metadata.get("ca_bundle")
    if not isinstance(ca_bundle, dict):
        raise DesktopRuntimeManifestError("runtime CA bundle metadata is missing")
    ca_file = _safe_runtime_file(root, str(ca_bundle.get("runtime_path") or ""))
    if not ca_file.is_file():
        raise DesktopRuntimeManifestError("runtime CA bundle is missing")
    actual_ca_sha256 = hashlib.sha256(ca_file.read_bytes()).hexdigest()
    if actual_ca_sha256 != ca_bundle.get("pem_sha256"):
        raise DesktopRuntimeManifestError("runtime CA bundle integrity check failed")
    print(json.dumps({
        "runtime_root": str(root),
        "executable": str(executable),
        "tree_sha256": metadata["tree_sha256"],
        "tree_file_count": metadata["tree_file_count"],
        "ca_pem_sha256": actual_ca_sha256,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
