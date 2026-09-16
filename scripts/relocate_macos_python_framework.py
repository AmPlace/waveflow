#!/usr/bin/env python3
"""Make the official macOS Python framework self-contained and signable.

The python.org macOS installer is an official source artifact, but its
framework is installed under /Library.  This release-time transformation
rewrites only references to that bundled framework to loader-relative paths.
It never changes system framework references and never runs inside the
WaveFlow application at runtime.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import subprocess


ARCHIVE_FRAMEWORK_PREFIX = "/Library/Frameworks/Python.framework/Versions/3.14/"
MACHO_MARKER = "Mach-O"


def _is_macho(path: Path) -> bool:
    try:
        result = subprocess.run(
            ["file", "-b", str(path)], check=True, capture_output=True, text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return False
    return MACHO_MARKER in result.stdout


def _dependencies(path: Path) -> set[str]:
    try:
        result = subprocess.run(
            ["otool", "-L", str(path)], check=True, capture_output=True, text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return set()
    # Dependency lines are indented; architecture/header lines are not.
    values: set[str] = set()
    for line in result.stdout.splitlines():
        if not line[:1].isspace():
            continue
        match = re.match(r"\s+(\S+)\s+\(", line)
        if match and match.group(1).startswith(ARCHIVE_FRAMEWORK_PREFIX):
            values.add(match.group(1))
    return values


def _dylib_id(path: Path) -> str | None:
    try:
        result = subprocess.run(
            ["otool", "-D", str(path)], check=True, capture_output=True, text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    lines = result.stdout.splitlines()
    return lines[1].strip() if len(lines) > 1 else None


def _relative_load_path(path: Path, target: Path) -> str:
    relative = os.path.relpath(target, path.parent).replace(os.sep, "/")
    return f"@loader_path/{relative}"


def relocate(framework: Path) -> list[Path]:
    framework = framework.resolve()
    version_root = framework / "Versions" / "3.14"
    if not version_root.is_dir():
        raise RuntimeError(f"CPython 3.14 framework is missing: {version_root}")
    changed: list[Path] = []
    for current, directories, files in os.walk(framework, followlinks=False):
        directories[:] = sorted(name for name in directories if not (Path(current) / name).is_symlink())
        for name in sorted(files):
            path = Path(current) / name
            if path.is_symlink() or not _is_macho(path):
                continue
            for old in sorted(_dependencies(path)):
                target = version_root / old[len(ARCHIVE_FRAMEWORK_PREFIX):]
                if not target.exists():
                    raise RuntimeError(f"Bundled dependency target is missing: {target}")
                subprocess.run(
                    ["install_name_tool", "-change", old, _relative_load_path(path, target), str(path)],
                    check=True,
                )
                changed.append(path)
            identity = _dylib_id(path)
            if identity and identity.startswith(ARCHIVE_FRAMEWORK_PREFIX):
                subprocess.run(
                    ["install_name_tool", "-id", f"@rpath/{Path(identity).name}", str(path)],
                    check=True,
                )
                changed.append(path)
    return sorted(set(changed))


def sign_runtime(framework: Path, identity: str) -> None:
    """Ad-hoc sign by default; release CI may pass a Developer ID identity."""
    for current, directories, files in os.walk(framework, followlinks=False):
        directories[:] = sorted(name for name in directories if not (Path(current) / name).is_symlink())
        for name in sorted(files):
            path = Path(current) / name
            if path.is_symlink() or not _is_macho(path):
                continue
            subprocess.run(["codesign", "--force", "--sign", identity, str(path)], check=True)
    # Re-sign nested Tcl/Tk frameworks before Python.app and the outer
    # framework. `codesign --deep` alone may preserve a stale nested seal after
    # install_name_tool changed one of their Mach-O files.
    nested_frameworks = sorted(
        (path for path in framework.rglob("*.framework") if path.is_dir()),
        key=lambda path: len(path.parts), reverse=True,
    )
    for bundle in nested_frameworks:
        subprocess.run(["codesign", "--force", "--deep", "--sign", identity, str(bundle)], check=True)
    # Sign the Python.app and outer framework after their nested Mach-O files.
    app = framework / "Versions" / "3.14" / "Resources" / "Python.app"
    if app.is_dir():
        subprocess.run(["codesign", "--force", "--sign", identity, str(app)], check=True)
    subprocess.run(["codesign", "--force", "--sign", identity, str(framework)], check=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("framework", type=Path)
    parser.add_argument("--codesign-identity", default="-")
    args = parser.parse_args()
    changed = relocate(args.framework)
    sign_runtime(args.framework, args.codesign_identity)
    subprocess.run(["codesign", "--verify", "--deep", "--strict", str(args.framework)], check=True)
    print(f"Relocated and signed {args.framework} ({len(set(changed))} Mach-O files changed)")


if __name__ == "__main__":
    main()
