from __future__ import annotations

import argparse
import base64
import hashlib
import json
import shutil
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from official_plugin_distribution import (
    OFFICIAL_DISTRIBUTION_ROOT, OFFICIAL_PUBLISHER_ID, load_bundled_official_market,
    load_official_trust_rows, validate_rollout_policy,
)
from plugin_runtime import PluginError, validate_manifest
from waveflow_plugin_cli import build_project, sign_build, validate_project


PLAN_PATH = OFFICIAL_DISTRIBUTION_ROOT / "release-plan.json"
DEFAULT_OUTPUT = OFFICIAL_DISTRIBUTION_ROOT / "distribution"
REPOSITORY_ROOT = Path(__file__).resolve().parent.parent


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise PluginError("INVALID_PLUGIN_RESPONSE", "Official release metadata is invalid", category="release")
    return value


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _dependency_sources(item: dict[str, Any], manifest: Any, staging: Path, plugin_id: str) -> list[dict[str, str]]:
    lock = manifest.runtime.get("dependency_lock") if manifest.runtime.get("type") == "python" else None
    lock_artifacts = list((lock or {}).get("artifacts") or [])
    source_items = item.get("dependency_sources") or []
    if not lock_artifacts:
        if source_items:
            raise PluginError("DEPENDENCY_LOCK_INVALID", "Dependency sources exist without a Python lock", category="dependency")
        return []
    if not isinstance(source_items, list):
        raise PluginError("DEPENDENCY_LOCK_INVALID", "Official dependency sources are invalid", category="dependency")
    by_filename: dict[str, Path] = {}
    for source_item in source_items:
        if not isinstance(source_item, dict) or set(source_item) != {"filename", "source"}:
            raise PluginError("DEPENDENCY_LOCK_INVALID", "Official dependency source metadata is invalid", category="dependency")
        filename = str(source_item.get("filename") or "")
        filename_path = Path(filename)
        if (not filename or filename_path.is_absolute() or filename_path.name != filename
                or ".." in filename_path.parts):
            raise PluginError("DEPENDENCY_LOCK_INVALID", "Official dependency filename is unsafe", category="dependency")
        source = (OFFICIAL_DISTRIBUTION_ROOT / str(source_item.get("source") or "")).resolve()
        if filename in by_filename or not source.is_file() or not source.is_relative_to(REPOSITORY_ROOT):
            raise PluginError("DEPENDENCY_ARTIFACT_NOT_FOUND", "Official dependency artifact is unavailable", category="dependency")
        by_filename[filename] = source
    expected_filenames = {str(artifact.get("filename") or "") for artifact in lock_artifacts}
    if set(by_filename) != expected_filenames:
        raise PluginError("DEPENDENCY_LOCK_INVALID", "Official dependency sources do not match the Plugin lock", category="dependency")
    references: list[dict[str, str]] = []
    for artifact in lock_artifacts:
        filename = str(artifact.get("filename") or "")
        source = by_filename.get(filename)
        if source is None:
            raise PluginError("DEPENDENCY_ARTIFACT_NOT_FOUND", f"Official dependency artifact is unavailable: {filename}", category="dependency")
        if source.stat().st_size != artifact["size_bytes"] or _sha256(source) != artifact["sha256"]:
            raise PluginError("DEPENDENCY_ARTIFACT_INTEGRITY_FAILED", f"Official dependency artifact is invalid: {filename}", category="dependency")
        target = staging / "payloads" / "dependencies" / plugin_id / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        references.append({
            "sha256": artifact["sha256"],
            "url": f"payloads/dependencies/{plugin_id}/{filename}",
        })
    return references


def _copy_plugin_resources(source: Path, project: Path, entrypoint: str) -> None:
    """Copy non-build Plugin resources into the isolated build project."""
    excluded_files = {
        "manifest.json", "dependency-lock.json", "requirements.in", "requirements.txt",
        "README", "README.md", "README.rst", "README.txt",
    }
    excluded_dirs = {".git", "__pycache__", ".pytest_cache", "dist"}
    entrypoint_path = (source / entrypoint).resolve()
    for path in sorted(source.rglob("*")):
        if not path.is_file() or path.resolve() == entrypoint_path or path.name in excluded_files:
            continue
        relative = path.relative_to(source)
        if any(part in excluded_dirs for part in relative.parts) or path.name.startswith("."):
            continue
        target = project / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)


def _private_key(path: Path) -> Ed25519PrivateKey:
    resolved = path.expanduser().resolve(strict=True)
    if resolved.is_relative_to(REPOSITORY_ROOT):
        raise PluginError("AUTH_FAILED", "Official signing key must remain outside the repository", category="trust")
    raw = resolved.read_bytes()
    try:
        key = serialization.load_pem_private_key(raw, password=None)
    except ValueError:
        key = Ed25519PrivateKey.from_private_bytes(raw)
    if not isinstance(key, Ed25519PrivateKey):
        raise PluginError("AUTH_FAILED", "Official signing key is not Ed25519", category="trust")
    return key


def _assert_anchor(key: Ed25519PrivateKey, key_id: str, trust_path: Path | None = None) -> None:
    encoded = base64.b64encode(key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw,
    )).decode()
    matches = [row for row in load_official_trust_rows(trust_path)
               if row["publisher_id"] == OFFICIAL_PUBLISHER_ID and row["key_id"] == key_id and row["enabled"]]
    if len(matches) != 1 or matches[0]["public_key"] != encoded:
        raise PluginError("PLUGIN_UNTRUSTED", "Signing key does not match the official trust anchor", category="trust")


def build_release(
    *, signing_key: Path, key_id: str, output: Path = DEFAULT_OUTPUT, trust_path: Path | None = None,
) -> dict[str, Any]:
    key = _private_key(signing_key)
    _assert_anchor(key, key_id, trust_path)
    plan = _json(PLAN_PATH)
    if (plan.get("schema_version") != 1 or plan.get("publisher_id") != OFFICIAL_PUBLISHER_ID
            or not isinstance(plan.get("plugins"), list) or not plan["plugins"]):
        raise PluginError("INVALID_PLUGIN_RESPONSE", "Official release plan is invalid", category="release")

    output = output.resolve()
    staging = output.with_name(output.name + ".staging")
    shutil.rmtree(staging, ignore_errors=True)
    (staging / "payloads").mkdir(parents=True)
    (staging / "packages").mkdir(parents=True)
    packages: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="waveflow-official-release-") as directory:
        workspace_root = Path(directory)
        for item in sorted(plan["plugins"], key=lambda value: str(value.get("plugin_id") or "")):
            plugin_id = str(item.get("plugin_id") or "")
            source = (OFFICIAL_DISTRIBUTION_ROOT / str(item.get("source") or "")).resolve()
            if not source.is_relative_to(REPOSITORY_ROOT) or not source.is_dir():
                raise PluginError("ARTIFACT_NOT_FOUND", "Official Plugin source is unavailable", category="release")
            project = workspace_root / plugin_id
            project.mkdir()
            manifest_source = source / "manifest.json"
            manifest_data = _json(manifest_source)
            manifest = validate_manifest(manifest_data)
            if manifest.publisher_id != OFFICIAL_PUBLISHER_ID or manifest.plugin_id != plugin_id:
                raise PluginError("PLUGIN_INCOMPATIBLE", "Official Plugin identity is inconsistent", category="release")
            entrypoint = str(manifest.artifacts[0]["entrypoint"])
            shutil.copyfile(manifest_source, project / "manifest.json")
            shutil.copyfile(source / entrypoint, project / entrypoint)
            _copy_plugin_resources(source, project, entrypoint)
            validate_project(project)

            artifact_name = f"{plugin_id}-{manifest.version}.pyz"
            built = build_project(project, output=project / "dist" / artifact_name)
            signed = sign_build(built["manifest"], signing_key, key_id=key_id)
            package = _json(Path(signed["package"]))
            artifact_source = Path(built["artifact"])
            artifact_target = staging / "payloads" / artifact_name
            shutil.copyfile(artifact_source, artifact_target)
            package.update({
                "schema_version": 1,
                "id": f"official::{plugin_id}-plugin",
                "name": str(item.get("name") or package.get("name") or manifest.display_name),
                "description": str(item.get("description") or ""),
                "kind": "plugin_package",
                "package_type": "plugin_package",
                "version": manifest.version,
                "updated_at": str(plan.get("updated_at") or ""),
                "status": "active",
                "source_origin": "official",
                "source_policy": "signed_release",
                "risk_level": "official_signed_not_sandboxed",
                "regions": deepcopy(item.get("regions") or []),
                "operators": deepcopy(item.get("operators") or []),
                "providers": deepcopy(item.get("providers") or []),
                "languages": deepcopy(item.get("languages") or []),
                "categories": deepcopy(item.get("categories") or ["插件", "Provider"]),
                "publisher": {
                    "id": manifest.publisher_id,
                    "name": str(item.get("publisher_name") or "WaveFlow"),
                },
                "published_at": str(item.get("published_at") or plan.get("updated_at") or ""),
                "compatibility": deepcopy(item.get("compatibility") or {}),
                "links": deepcopy(item.get("links") or {}),
                "catalog": deepcopy(item.get("catalog") or {"sort_weight": 0, "featured": False}),
                "artifact_references": [{
                    "sha256": package["plugin_manifest"]["artifacts"][0]["sha256"],
                    "url": f"payloads/{artifact_name}",
                }],
                "dependency_references": _dependency_sources(item, manifest, staging, plugin_id),
            })
            tags = item.get("tags")
            if not isinstance(tags, list):
                raise PluginError("INVALID_PLUGIN_RESPONSE", "Official Plugin package tags metadata is missing", category="release")
            display = item.get("display")
            if not isinstance(display, dict) or "tags" in display:
                raise PluginError("INVALID_PLUGIN_RESPONSE", "Official Plugin package display metadata is missing", category="release")
            package["tags"] = deepcopy(tags)
            package["display"] = deepcopy(display)
            rollout = item.get("rollout")
            if rollout is not None:
                try:
                    package["rollout"] = validate_rollout_policy(rollout)
                except PluginError as exc:
                    raise PluginError(exc.code, exc.message, category="release") from exc
            package.pop("market_source", None)
            _write_json(staging / "packages" / f"{plugin_id}.market-package.json", package)
            packages.append(package)

    market = {
        "schema_version": 1,
        "market_version": str(plan.get("release_version") or ""),
        "updated_at": str(plan.get("updated_at") or ""),
        "packages": sorted(packages, key=lambda item: item["id"]),
    }
    _write_json(staging / "market.json", market)
    load_bundled_official_market(staging)
    if output.exists():
        shutil.rmtree(output)
    staging.replace(output)
    return {
        "publisher": OFFICIAL_PUBLISHER_ID,
        "key_id": key_id,
        "output": str(output),
        "package_count": len(packages),
        "packages": [item["id"] for item in packages],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build signed WaveFlow official Plugin release artifacts")
    parser.add_argument("--signing-key", type=Path, required=True)
    parser.add_argument("--key-id", required=True)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    try:
        result = build_release(signing_key=args.signing_key, key_id=args.key_id, output=args.output)
    except (OSError, ValueError, json.JSONDecodeError, PluginError) as exc:
        if isinstance(exc, PluginError):
            print(json.dumps({"error": exc.as_contract()}, ensure_ascii=False))
        else:
            print(json.dumps({"error": {"code": "OFFICIAL_RELEASE_FAILED", "message": str(exc)}}))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
