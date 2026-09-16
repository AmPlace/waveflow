"""Resolve the controlled Python runtime used by frozen Desktop builds."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from desktop_runtime_manifest import (
    DesktopRuntimeManifestError,
    desktop_runtime_target,
    validate_runtime_metadata,
)
from plugin_runtime import PluginError


DESKTOP_RUNTIME_DIRECTORY = "python-runtime"
DESKTOP_RUNTIME_METADATA = "runtime.json"
def _runtime_candidate(backend_executable: str | Path, target) -> Path:
    executable = Path(backend_executable).resolve()
    return executable.parent / DESKTOP_RUNTIME_DIRECTORY / target.executable


def resolve_plugin_python_executable() -> Path:
    """Return the interpreter allowed to create/run Python Plugin envs.

    A frozen backend must never fall back to its PyInstaller interpreter or to
    a user/system Python.  The only accepted frozen-build path is the sibling
    runtime staged by the Desktop release builder.  Normal backend/test runs
    retain the existing interpreter behavior.
    """

    if not getattr(sys, "frozen", False):
        executable = Path(sys.executable).resolve()
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise PluginError(
                "PYTHON_RUNTIME_UNSUPPORTED",
                "The controlled Python runtime is unavailable",
                category="runtime",
            )
        return executable

    target = desktop_runtime_target()
    if target is None:
        raise PluginError(
            "PYTHON_RUNTIME_UNSUPPORTED",
            "The frozen Desktop platform is unsupported",
            category="runtime",
        )
    runtime_root = (Path(sys.executable).resolve().parent / DESKTOP_RUNTIME_DIRECTORY).resolve()
    candidate = _runtime_candidate(sys.executable, target).resolve()
    try:
        candidate.relative_to(runtime_root)
    except ValueError as exc:
        raise PluginError(
            "PYTHON_RUNTIME_UNSUPPORTED",
            "The Desktop Python runtime path is invalid",
            category="runtime",
        ) from exc
    try:
        metadata = json.loads((runtime_root / DESKTOP_RUNTIME_METADATA).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PluginError(
            "PYTHON_RUNTIME_UNSUPPORTED",
            "The Desktop Python runtime metadata is invalid",
            category="runtime",
        ) from exc
    expected_version = f"{sys.version_info.major}.{sys.version_info.minor}"
    try:
        validate_runtime_metadata(
            runtime_root,
            metadata,
            target,
            expected_python_version=expected_version,
        )
    except (DesktopRuntimeManifestError, OSError) as exc:
        raise PluginError(
            "PYTHON_RUNTIME_UNSUPPORTED",
            "The Desktop Python runtime is invalid",
            category="runtime",
        ) from exc
    return candidate
