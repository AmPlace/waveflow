from __future__ import annotations

import base64
import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from plugin_runtime import PluginError, validate_manifest
from plugin_runtime.manifest import RANGE_PART_RE, SUPPORTED_ARCH, SUPPORTED_OS, _range_allows


OFFICIAL_PUBLISHER_ID = "org.waveflow"
OFFICIAL_SOURCE_KEY = "official"
OFFICIAL_DISTRIBUTION_ROOT = Path(__file__).resolve().with_name("official_plugins")
OFFICIAL_RELEASE_ROOT = OFFICIAL_DISTRIBUTION_ROOT / "distribution"
OFFICIAL_TRUST_PATH = OFFICIAL_DISTRIBUTION_ROOT / "publisher-trust.json"


def validate_rollout_policy(value: Any) -> dict[str, Any] | None:
    """Validate the deployment policy carried by an official Market package.

    The policy is deliberately package-generic.  It describes where a
    release has completed runtime acceptance; it does not name providers or
    add a second ownership mechanism.  An omitted ``eligible_platforms``
    field preserves the original rollout behavior for the first official
    Python-backed rollout.
    """
    if value is None:
        return None
    if not isinstance(value, dict):
        raise PluginError("ARTIFACT_INVALID", "Bundled Plugin rollout policy is invalid", category="distribution")
    required = {"deployment", "default_ownership"}
    if not required.issubset(value) or not set(value).issubset(required | {"eligible_platforms"}):
        raise PluginError("ARTIFACT_INVALID", "Bundled Plugin rollout policy is invalid", category="distribution")
    if value.get("deployment") != "python_backed" or value.get("default_ownership") != "plugin":
        raise PluginError("ARTIFACT_INVALID", "Bundled Plugin rollout policy is invalid", category="distribution")
    targets = value.get("eligible_platforms")
    if targets is None:
        return {"deployment": "python_backed", "default_ownership": "plugin"}
    if not isinstance(targets, list) or not targets:
        raise PluginError("ARTIFACT_INVALID", "Bundled Plugin rollout platforms are invalid", category="distribution")
    normalized: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for target in targets:
        if not isinstance(target, dict) or set(target) != {"os", "arch", "python_version_range"}:
            raise PluginError("ARTIFACT_INVALID", "Bundled Plugin rollout platform is invalid", category="distribution")
        os_name = target.get("os")
        arch = target.get("arch")
        python_range = target.get("python_version_range")
        if (os_name not in SUPPORTED_OS or arch not in SUPPORTED_ARCH
                or not isinstance(python_range, str) or not python_range
                or not all(RANGE_PART_RE.fullmatch(part) for part in python_range.split())):
            raise PluginError("ARTIFACT_INVALID", "Bundled Plugin rollout platform is invalid", category="distribution")
        key = (os_name, arch, python_range)
        if key in seen:
            raise PluginError("ARTIFACT_INVALID", "Bundled Plugin rollout platforms contain duplicates", category="distribution")
        seen.add(key)
        normalized.append({"os": os_name, "arch": arch, "python_version_range": python_range})
    return {
        "deployment": "python_backed",
        "default_ownership": "plugin",
        "eligible_platforms": normalized,
    }


def rollout_policy_allows_runtime(
    value: dict[str, Any] | None, *, os_name: str, arch: str, python_version: str,
) -> bool:
    """Return whether a validated rollout policy permits this runtime.

    A policy without an explicit acceptance matrix retains the legacy
    semantics: the normal artifact/runtime preflight remains authoritative.
    An explicit matrix is an allow-list, so an unvalidated deployment cannot
    take ownership merely because its wheel tags happen to be compatible.
    """
    policy = validate_rollout_policy(value)
    if policy is None or "eligible_platforms" not in policy:
        return True
    return any(
        target["os"] == os_name
        and target["arch"] == arch
        and _range_allows(python_version, target["python_version_range"])
        for target in policy["eligible_platforms"]
    )


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PluginError(
            "ARTIFACT_INVALID", "Bundled official Plugin metadata is unavailable", category="distribution",
        ) from exc
    if not isinstance(value, dict):
        raise PluginError(
            "ARTIFACT_INVALID", "Bundled official Plugin metadata is invalid", category="distribution",
        )
    return value


def load_official_trust_rows(path: str | Path | None = None) -> list[dict[str, Any]]:
    """Load public release keys shipped with WaveFlow; no private material is accepted."""
    data = _load_json(Path(path) if path else OFFICIAL_TRUST_PATH)
    if data.get("schema_version") != 1 or not isinstance(data.get("publishers"), list):
        raise PluginError("PLUGIN_UNTRUSTED", "Official publisher trust metadata is invalid", category="trust")
    rows: list[dict[str, Any]] = []
    seen_publishers: set[str] = set()
    seen_key_ids: set[str] = set()
    seen_public_keys: set[bytes] = set()
    for publisher in data["publishers"]:
        if (not isinstance(publisher, dict)
                or publisher.get("publisher_id") != OFFICIAL_PUBLISHER_ID
                or not isinstance(publisher.get("keys"), list)):
            raise PluginError("PLUGIN_UNTRUSTED", "Official publisher trust metadata is invalid", category="trust")
        publisher_id = str(publisher["publisher_id"])
        if publisher_id in seen_publishers:
            raise PluginError("PLUGIN_UNTRUSTED", "Official publisher trust metadata contains duplicates", category="trust")
        seen_publishers.add(publisher_id)
        for item in publisher["keys"]:
            if not isinstance(item, dict):
                raise PluginError("PLUGIN_UNTRUSTED", "Official publisher key metadata is invalid", category="trust")
            key_id = str(item.get("key_id") or "")
            encoded = str(item.get("public_key") or "")
            try:
                raw = base64.b64decode(encoded, validate=True)
            except ValueError as exc:
                raise PluginError("PLUGIN_UNTRUSTED", "Official publisher key is invalid", category="trust") from exc
            if not key_id or len(raw) != 32:
                raise PluginError("PLUGIN_UNTRUSTED", "Official publisher key is invalid", category="trust")
            if key_id in seen_key_ids or raw in seen_public_keys:
                raise PluginError("PLUGIN_UNTRUSTED", "Official publisher trust metadata contains duplicate keys", category="trust")
            seen_key_ids.add(key_id)
            seen_public_keys.add(raw)
            rows.append({
                "publisher_id": OFFICIAL_PUBLISHER_ID,
                "key_id": key_id,
                "public_key": encoded,
                "trust_level": "official",
                "enabled": 1 if item.get("enabled", True) else 0,
                "description": str(item.get("description") or "WaveFlow official release key"),
                "require_manifest_signature": True,
            })
    if not any(row["enabled"] for row in rows):
        raise PluginError("PLUGIN_UNTRUSTED", "No official publisher key is enabled", category="trust")
    return rows


def _release_artifact(root: Path, url: str) -> Path:
    parsed = urlparse(url)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        raise PluginError("ARTIFACT_INVALID", "Bundled official artifact reference is invalid", category="artifact")
    relative = Path(unquote(parsed.path))
    if relative.is_absolute() or ".." in relative.parts:
        raise PluginError("ARTIFACT_INVALID", "Bundled official artifact path is unsafe", category="artifact")
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise PluginError("ARTIFACT_NOT_FOUND", "Bundled official artifact is missing", category="artifact")
    return path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_bundled_official_market(root: str | Path | None = None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Read immutable release-local packages and attach private local artifact handles."""
    release_root = (Path(root) if root else OFFICIAL_RELEASE_ROOT).resolve()
    market = _load_json(release_root / "market.json")
    if market.get("schema_version") != 1 or not isinstance(market.get("packages"), list):
        raise PluginError("ARTIFACT_INVALID", "Bundled official Market is invalid", category="distribution")
    packages: list[dict[str, Any]] = []
    seen_package_ids: set[str] = set()
    seen_identity_versions: set[tuple[str, str]] = set()
    for raw in market["packages"]:
        if not isinstance(raw, dict) or raw.get("package_type") != "plugin_package":
            raise PluginError("ARTIFACT_INVALID", "Bundled official package is invalid", category="distribution")
        package = deepcopy(raw)
        package_id = package.get("id")
        if not isinstance(package_id, str) or not package_id or package_id in seen_package_ids:
            raise PluginError("ARTIFACT_INVALID", "Bundled official package identity is invalid or duplicated", category="distribution")
        seen_package_ids.add(package_id)
        manifest = validate_manifest(package.get("plugin_manifest"))
        if manifest.publisher_id != OFFICIAL_PUBLISHER_ID:
            raise PluginError("PLUGIN_UNTRUSTED", "Bundled Plugin publisher is not official", category="trust")
        if package.get("version") != manifest.version:
            raise PluginError("PLUGIN_INCOMPATIBLE", "Bundled Plugin package version is inconsistent", category="distribution")
        identity_version = (manifest.identity, manifest.version)
        if identity_version in seen_identity_versions:
            raise PluginError("PLUGIN_INCOMPATIBLE", "Bundled Plugin identity/version is duplicated", category="distribution")
        seen_identity_versions.add(identity_version)
        rollout = validate_rollout_policy(package.get("rollout"))
        if rollout is not None:
            package["rollout"] = rollout
        references = package.get("artifact_references")
        if not isinstance(references, list):
            raise PluginError("ARTIFACT_INVALID", "Bundled official artifact references are invalid", category="artifact")
        platforms = [(item["os"], item["arch"]) for item in manifest.artifacts]
        if len(platforms) != len(set(platforms)):
            raise PluginError("ARTIFACT_INVALID", "Bundled Plugin artifacts contain duplicate platforms", category="artifact")
        manifest_digests = {item["sha256"] for item in manifest.artifacts}
        local_references = []
        seen_reference_digests: set[str] = set()
        seen_reference_urls: set[str] = set()
        for reference in references:
            if (not isinstance(reference, dict)
                    or set(reference) != {"sha256", "url"}
                    or not isinstance(reference.get("url"), str)):
                raise PluginError("ARTIFACT_INVALID", "Bundled official artifact reference is invalid", category="artifact")
            if reference["sha256"] in seen_reference_digests or reference["url"] in seen_reference_urls:
                raise PluginError("ARTIFACT_INVALID", "Bundled official artifact references contain duplicates", category="artifact")
            if reference["sha256"] not in manifest_digests:
                raise PluginError("ARTIFACT_INVALID", "Bundled artifact reference is absent from the Plugin manifest", category="artifact")
            seen_reference_digests.add(reference["sha256"])
            seen_reference_urls.add(reference["url"])
            path = _release_artifact(release_root, reference["url"])
            digest = _sha256(path)
            if digest != reference.get("sha256"):
                raise PluginError("ARTIFACT_INTEGRITY_FAILED", "Bundled official artifact was modified", category="artifact")
            declared = [item for item in manifest.artifacts if item["sha256"] == digest]
            if not declared or any(path.stat().st_size != item["size_bytes"] for item in declared):
                raise PluginError("ARTIFACT_INTEGRITY_FAILED", "Bundled official artifact size is invalid", category="artifact")
            local_references.append({
                "sha256": digest,
                "url": reference["url"],
                "_bundled_path": str(path),
            })
        if seen_reference_digests != manifest_digests:
            raise PluginError("ARTIFACT_INVALID", "Bundled artifact references do not cover the Plugin manifest", category="artifact")
        manifest_signature = package.get("manifest_signature")
        artifact_key_ids = {item["signature"]["key_id"] for item in manifest.artifacts}
        if (not isinstance(manifest_signature, dict)
                or set(manifest_signature) != {"algorithm", "key_id", "value"}
                or manifest_signature.get("algorithm") != "ed25519"
                or not isinstance(manifest_signature.get("value"), str)
                or not manifest_signature["value"]
                or len(artifact_key_ids) != 1
                or manifest_signature.get("key_id") != next(iter(artifact_key_ids))):
            raise PluginError("PLUGIN_UNTRUSTED", "Bundled Plugin manifest signature is invalid", category="trust")
        package["artifact_references"] = local_references
        runtime = manifest.runtime
        if runtime.get("type") == "python":
            lock_artifacts = runtime.get("dependency_lock", {}).get("artifacts", [])
            dependency_references = package.get("dependency_references")
            if not isinstance(dependency_references, list):
                raise PluginError(
                    "DEPENDENCY_LOCK_INVALID", "Bundled Python dependency references are invalid", category="dependency",
                )
            if len(dependency_references) != len(lock_artifacts):
                raise PluginError(
                    "DEPENDENCY_LOCK_INVALID", "Bundled dependency references do not exactly match the Plugin lock", category="dependency",
                )
            local_dependencies = []
            seen_digests: set[str] = set()
            for reference in dependency_references:
                if (not isinstance(reference, dict)
                        or set(reference) != {"sha256", "url"}
                        or not isinstance(reference.get("url"), str)):
                    raise PluginError(
                        "DEPENDENCY_LOCK_INVALID", "Bundled dependency reference is invalid", category="dependency",
                    )
                matching = [item for item in lock_artifacts if item.get("sha256") == reference.get("sha256")]
                if len(matching) != 1:
                    raise PluginError(
                        "DEPENDENCY_LOCK_INVALID", "Bundled dependency is absent from the Plugin lock", category="dependency",
                    )
                if reference["sha256"] in seen_digests:
                    raise PluginError(
                        "DEPENDENCY_LOCK_INVALID", "Bundled dependency references contain a duplicate artifact", category="dependency",
                    )
                seen_digests.add(reference["sha256"])
                path = _release_artifact(release_root, reference["url"])
                digest = _sha256(path)
                if digest != reference["sha256"] or path.stat().st_size != matching[0]["size_bytes"]:
                    raise PluginError(
                        "DEPENDENCY_ARTIFACT_INTEGRITY_FAILED", "Bundled dependency artifact was modified", category="dependency",
                    )
                local_dependencies.append({
                    "sha256": digest,
                    "url": reference["url"],
                    "_bundled_path": str(path),
                })
            expected_digests = {item["sha256"] for item in lock_artifacts}
            if {item["sha256"] for item in local_dependencies} != expected_digests:
                raise PluginError(
                    "DEPENDENCY_LOCK_INVALID", "Bundled dependency references do not cover the Plugin lock", category="dependency",
                )
            package["dependency_references"] = local_dependencies
        package["market_source"] = {
            "source_key": OFFICIAL_SOURCE_KEY,
            "name": "WaveFlow 官方 Market",
            "is_builtin": True,
        }
        package["_bundled_release"] = True
        packages.append(package)
    result = deepcopy(market)
    result["packages"] = deepcopy(packages)
    result["distribution"] = "bundled_signed_release"
    return result, packages


def bundled_official_packages(root: str | Path | None = None) -> list[dict[str, Any]]:
    return load_bundled_official_market(root)[1]
