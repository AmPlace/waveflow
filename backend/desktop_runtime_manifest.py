"""Pure-stdlib helpers shared by Desktop runtime build and startup checks."""

from __future__ import annotations

import hashlib
import os
import platform as host_platform
import sys
from dataclasses import dataclass
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any, Iterator


DESKTOP_RUNTIME_METADATA = "runtime.json"
DESKTOP_RUNTIME_SCHEMA_VERSION = 1
DESKTOP_RUNTIME_BYTECODE_DIRECTORY = "__pycache__"
DESKTOP_RUNTIME_BYTECODE_SUFFIXES = (".pyc", ".pyo")


@dataclass(frozen=True)
class DesktopRuntimeTarget:
    platform_os: str
    arch: str
    executable: str
    require_posix_executable: bool


class DesktopRuntimeManifestError(ValueError):
    """Raised when a Desktop runtime does not satisfy the release contract."""


_RUNTIME_TARGETS = {
    ("macos", "arm64"): DesktopRuntimeTarget("macos", "arm64", "bin/python3.14", True),
    ("windows", "x86_64"): DesktopRuntimeTarget("windows", "x86_64", "python.exe", False),
}


def normalize_runtime_os(value: str) -> str:
    normalized = str(value or "").strip().lower()
    return {"darwin": "macos", "mac": "macos", "win32": "windows", "win": "windows"}.get(
        normalized, normalized,
    )


def normalize_runtime_arch(value: str) -> str:
    normalized = str(value or "").strip().lower()
    return {
        "amd64": "x86_64", "x64": "x86_64", "x86-64": "x86_64",
        "aarch64": "arm64", "arm64e": "arm64",
    }.get(normalized, normalized)


def desktop_runtime_target(
    platform_name: str | None = None,
    machine: str | None = None,
) -> DesktopRuntimeTarget | None:
    """Return the supported frozen Desktop runtime target for a host."""
    if platform_name is None:
        platform_name = sys.platform
    if machine is None:
        machine = host_platform.machine()
    platform_os = normalize_runtime_os(platform_name)
    arch = normalize_runtime_arch(machine)
    return _RUNTIME_TARGETS.get((platform_os, arch))


def _safe_relative_runtime_parts(value: Any) -> tuple[str, ...]:
    if not isinstance(value, str) or not value or "\\" in value:
        raise DesktopRuntimeManifestError("runtime executable path is invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise DesktopRuntimeManifestError("runtime executable path is invalid")
    return path.parts


def validate_runtime_metadata(
    root: str | Path,
    metadata: dict[str, Any],
    target: DesktopRuntimeTarget,
    *,
    expected_python_version: str,
) -> Path:
    """Validate a staged runtime and return its executable path.

    This is the single integrity boundary used by the frozen backend and the
    release verification helpers.  Platform adapters only select the target;
    they do not weaken metadata, path, or tree-digest checks.
    """
    root = Path(root).resolve()
    if not isinstance(metadata, dict):
        raise DesktopRuntimeManifestError("runtime metadata is invalid")
    python_version = str(metadata.get("python_version") or "")
    executable = metadata.get("executable")
    expected_abi = f"cp{expected_python_version.replace('.', '')}"
    if (
        metadata.get("schema_version") != DESKTOP_RUNTIME_SCHEMA_VERSION
        or metadata.get("runtime_type") != "python"
        or metadata.get("os") != target.platform_os
        or metadata.get("arch") != target.arch
        or metadata.get("python_abi") != expected_abi
        or executable != target.executable
        or not python_version.startswith(f"{expected_python_version}.")
    ):
        raise DesktopRuntimeManifestError("runtime metadata is incompatible")

    parts = _safe_relative_runtime_parts(executable)
    candidate = (root.joinpath(*parts)).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise DesktopRuntimeManifestError("runtime executable path escapes its root") from exc
    if not candidate.is_file():
        raise DesktopRuntimeManifestError("runtime executable is missing")
    if target.require_posix_executable and not os.access(candidate, os.X_OK):
        raise DesktopRuntimeManifestError("runtime executable is not executable")

    tree_sha256 = metadata.get("tree_sha256")
    tree_file_count = metadata.get("tree_file_count")
    if (
        not isinstance(tree_sha256, str)
        or len(tree_sha256) != 64
        or not isinstance(tree_file_count, int)
        or isinstance(tree_file_count, bool)
    ):
        raise DesktopRuntimeManifestError("runtime integrity metadata is missing")
    actual_digest, actual_count = runtime_tree_digest(root)
    if actual_digest != tree_sha256 or actual_count != tree_file_count:
        raise DesktopRuntimeManifestError("runtime integrity check failed")
    return candidate


def runtime_metadata_from_lock(
    root: str | Path,
    lock: dict[str, Any],
    target: DesktopRuntimeTarget,
    *,
    executable: str,
    ca_runtime_path: str,
) -> dict[str, Any]:
    """Build deterministic runtime metadata from a locked source manifest."""
    python_version = str(lock["python_version"])
    major, minor, _patch = python_version.split(".", 2)
    if lock.get("python_abi") != f"cp{major}{minor}":
        raise DesktopRuntimeManifestError("runtime lock ABI does not match its version")
    if executable != target.executable:
        raise DesktopRuntimeManifestError("runtime executable does not match target")
    tree_sha256, tree_file_count = runtime_tree_digest(root)
    source = dict(lock["source"])
    ca_bundle = dict(lock["ca_bundle"])
    ca_bundle["runtime_path"] = ca_runtime_path
    return {
        "schema_version": DESKTOP_RUNTIME_SCHEMA_VERSION,
        "runtime_type": "python",
        "python_version": python_version,
        "python_version_range": f">={major}.{minor}.0 <{major}.{int(minor) + 1}.0",
        "python_abi": lock["python_abi"],
        "os": target.platform_os,
        "arch": target.arch,
        "executable": executable,
        "source": source,
        "ca_bundle": ca_bundle,
        "tree_sha256": tree_sha256,
        "tree_file_count": tree_file_count,
    }


def _is_runtime_bytecode(relative: str) -> bool:
    parts = Path(relative).parts
    return (
        DESKTOP_RUNTIME_BYTECODE_DIRECTORY in parts
        or Path(relative).suffix in DESKTOP_RUNTIME_BYTECODE_SUFFIXES
    )


def runtime_entries(root: str | Path) -> Iterator[tuple[str, str, str | None, Path | None]]:
    """Yield deterministic runtime entries without following directory links."""
    root = Path(root).resolve()
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        directories.sort()
        files.sort()
        for name in tuple(directories):
            path = current_path / name
            if path.is_symlink():
                directories.remove(name)
                yield path.relative_to(root).as_posix(), "symlink", os.readlink(path), None
        for name in files:
            path = current_path / name
            relative = path.relative_to(root).as_posix()
            if relative == DESKTOP_RUNTIME_METADATA:
                continue
            # CPython may create bytecode while importing the bundled stdlib.
            # It is a runtime cache, not part of the immutable release payload;
            # including it would make a read-only app fail its own integrity
            # check after the first sidecar startup.
            if _is_runtime_bytecode(relative):
                continue
            if path.is_symlink():
                yield relative, "symlink", os.readlink(path), None
            elif path.is_file():
                yield relative, "file", None, path


def runtime_tree_digest(root: str | Path) -> tuple[str, int]:
    """Return a path-independent digest for the installed runtime payload."""
    digest = hashlib.sha256()
    count = 0
    for relative, kind, target, path in runtime_entries(root):
        count += 1
        digest.update(kind.encode("utf-8"))
        digest.update(b"\0")
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        if kind == "symlink":
            digest.update(str(target).encode("utf-8"))
        else:
            assert path is not None
            size = path.stat().st_size
            digest.update(str(size).encode("ascii"))
            digest.update(b"\0")
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
    return digest.hexdigest(), count
