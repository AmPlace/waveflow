"""Controlled local Developer Plugin package loading.

This module deliberately does not participate in Market discovery.  A local
Developer package is accepted only when the caller supplies a filesystem path
and the production subsystem has explicitly enabled Developer Mode.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from plugin_market import MAX_DEVELOPER_ARTIFACT_BYTES, current_platform
from plugin_runtime import PluginError, validate_manifest
from plugin_python_runtime import select_dependency_artifacts


DEVELOPER_LOCAL_SOURCE_KEY = "developer_local"
DEVELOPER_LOCAL_TRUST_CLASS = "developer_local"
UNSIGNED_DEVELOPER_SIGNATURE = "UNSIGNED"
MAX_DEVELOPER_MANIFEST_BYTES = 1024 * 1024


def _read_json(path: Path) -> dict[str, Any]:
    try:
        if path.stat().st_size > MAX_DEVELOPER_MANIFEST_BYTES:
            raise PluginError("ARTIFACT_INVALID", "Developer manifest exceeds the size limit", category="manifest")
        value = json.loads(path.read_text(encoding="utf-8"))
    except PluginError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PluginError("INVALID_PLUGIN_RESPONSE", "Developer manifest is not valid JSON", category="manifest") from exc
    if not isinstance(value, dict):
        raise PluginError("INVALID_PLUGIN_RESPONSE", "Developer manifest must contain an object", category="manifest")
    return value


def _safe_local_file(root: Path, relative: str, *, label: str) -> Path:
    candidate = (root / relative).resolve()
    if not candidate.is_relative_to(root) or not candidate.is_file():
        raise PluginError("ARTIFACT_NOT_FOUND", f"Developer {label} is missing", category="artifact")
    return candidate


def _verify_file(path: Path, *, digest: str, size: int, label: str) -> None:
    if size < 1 or size > MAX_DEVELOPER_ARTIFACT_BYTES:
        raise PluginError("ARTIFACT_INVALID", f"Developer {label} size is outside the allowed limit", category="artifact")
    try:
        actual_size = path.stat().st_size
        payload = path.read_bytes()
    except OSError as exc:
        raise PluginError("ARTIFACT_NOT_FOUND", f"Developer {label} cannot be read", category="artifact") from exc
    if actual_size != size or len(payload) != size or hashlib.sha256(payload).hexdigest() != digest:
        raise PluginError("ARTIFACT_INTEGRITY_FAILED", f"Developer {label} digest or size mismatch", category="artifact")


def _manifest_path(value: str | Path) -> tuple[Path, Path]:
    supplied = Path(value).expanduser()
    try:
        supplied = supplied.resolve(strict=True)
    except OSError as exc:
        raise PluginError("ARTIFACT_NOT_FOUND", "Developer package path does not exist", category="artifact") from exc
    if supplied.is_dir():
        manifest = supplied / "manifest.json"
        root = supplied
    elif supplied.name == "manifest.json":
        manifest = supplied
        root = supplied.parent
    elif supplied.suffix == ".pyz":
        manifest = supplied.parent / "manifest.json"
        root = supplied.parent
    else:
        raise PluginError(
            "INVALID_PLUGIN_RESPONSE",
            "Developer install accepts a package directory, manifest.json, or .pyz path",
            category="artifact",
        )
    if not manifest.is_file():
        raise PluginError("ARTIFACT_NOT_FOUND", "Developer package manifest.json is missing", category="manifest")
    return root.resolve(), manifest.resolve()


def load_developer_package(value: str | Path) -> dict[str, Any]:
    """Load and verify one unsigned local package without trusting its claims.

    The returned package uses the same candidate shape as Market packages, so
    the normal lifecycle, artifact store, dependency environment, and runtime
    activation code can be reused.  The local marker is intentionally kept
    outside the Plugin manifest and is never treated as Official trust.
    """

    root, manifest_path = _manifest_path(value)
    data = _read_json(manifest_path)
    manifest = validate_manifest(data)
    os_name, arch = current_platform()
    artifacts = [item for item in manifest.artifacts if item["os"] == os_name and item["arch"] == arch]
    if not artifacts:
        raise PluginError("PLATFORM_UNSUPPORTED", "Developer package has no compatible artifact", category="compatibility")

    artifact_references: list[dict[str, str]] = []
    for artifact in artifacts:
        signature = artifact.get("signature") or {}
        if signature.get("value") != UNSIGNED_DEVELOPER_SIGNATURE:
            raise PluginError(
                "PLUGIN_UNTRUSTED",
                "Local Developer install requires an explicit UNSIGNED artifact marker",
                category="trust",
            )
        path = _safe_local_file(root, str(artifact["entrypoint"]), label="artifact")
        _verify_file(
            path,
            digest=str(artifact["sha256"]),
            size=int(artifact["size_bytes"]),
            label="artifact",
        )
        artifact_references.append({"sha256": str(artifact["sha256"]), "local_path": str(path)})

    dependency_references: list[dict[str, str]] = []
    lock = manifest.runtime.get("dependency_lock") or {}
    for dependency in select_dependency_artifacts(lock):
        filename = str(dependency["filename"])
        candidates = [root / "dependencies" / filename, root / filename]
        path = next((candidate.resolve() for candidate in candidates if candidate.is_file()), None)
        if path is None or not path.is_relative_to(root) or not path.is_file():
            raise PluginError(
                "DEPENDENCY_ARTIFACT_NOT_FOUND",
                f"Developer dependency wheel is missing: {filename}",
                category="dependency",
            )
        _verify_file(
            path,
            digest=str(dependency["sha256"]),
            size=int(dependency["size_bytes"]),
            label=f"dependency {filename}",
        )
        dependency_references.append({"sha256": str(dependency["sha256"]), "local_path": str(path)})

    return {
        "id": f"developer-local::{manifest.identity}",
        "name": manifest.display_name,
        "kind": "plugin_package",
        "package_type": "plugin_package",
        "version": manifest.version,
        "plugin_manifest": data,
        "manifest_signature": None,
        "artifact_references": artifact_references,
        "dependency_references": dependency_references,
        "market_source": {"source_key": DEVELOPER_LOCAL_SOURCE_KEY, "is_builtin": False},
        "_developer_local": True,
    }
