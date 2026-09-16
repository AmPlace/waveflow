#!/usr/bin/env python3
"""Write the common Desktop runtime metadata after platform staging."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from desktop_runtime_manifest import (  # noqa: E402
    DesktopRuntimeManifestError,
    desktop_runtime_target,
    runtime_metadata_from_lock,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("runtime_root", type=Path)
    parser.add_argument("lock_file", type=Path)
    parser.add_argument("platform_os")
    parser.add_argument("arch")
    parser.add_argument("executable")
    parser.add_argument("ca_runtime_path")
    args = parser.parse_args()

    target = desktop_runtime_target(args.platform_os, args.arch)
    if target is None:
        raise DesktopRuntimeManifestError("unsupported Desktop runtime target")
    lock = json.loads(args.lock_file.read_text(encoding="utf-8"))
    metadata = runtime_metadata_from_lock(
        args.runtime_root,
        lock,
        target,
        executable=args.executable,
        ca_runtime_path=args.ca_runtime_path,
    )
    (args.runtime_root / "runtime.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "os": metadata["os"],
        "arch": metadata["arch"],
        "python_version": metadata["python_version"],
        "executable": metadata["executable"],
        "tree_sha256": metadata["tree_sha256"],
        "tree_file_count": metadata["tree_file_count"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
