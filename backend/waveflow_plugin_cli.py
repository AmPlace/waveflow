from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from email.parser import BytesParser
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from packaging.utils import canonicalize_name, parse_wheel_filename

from plugin_runtime import PluginError, load_manifest, validate_manifest, validate_stream_descriptor
from plugin_runtime.validation import validate_radio_catalog
from plugin_runtime.manifest import SUPPORTED_ARCH, SUPPORTED_OS, _range_allows
from plugin_runtime.process import PluginProcess
from plugin_market import current_platform, manifest_signature_payload
from plugin_python_runtime import select_dependency_artifacts


SDK_ROOT = Path(__file__).with_name("waveflow_plugin_sdk")
FIXED_ZIP_TIME = (1980, 1, 1, 0, 0, 0)

#: Platform matrix the ``init`` template declares.  A pure-Python Plugin
#: artifact runs unchanged on every platform Core supports, and this is the set
#: the official Plugins publish; declaring only one platform made a freshly
#: scaffolded Plugin un-installable on the author's own machine.
CANONICAL_PLATFORMS: tuple[tuple[str, str], ...] = (
    ("linux", "x86_64"), ("linux", "arm64"),
    ("macos", "x86_64"), ("macos", "arm64"),
    ("windows", "x86_64"),
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PluginError("INVALID_PLUGIN_RESPONSE", f"Unable to read {path.name}", category="cli") from exc
    if not isinstance(value, dict):
        raise PluginError("INVALID_PLUGIN_RESPONSE", f"{path.name} must contain an object", category="cli")
    return value


def _verify_built_artifacts(root: Path, manifest: Any) -> None:
    """Check built ``.pyz`` digests so ``validate`` agrees with the runtime loader.

    A source project has no built artifact yet, so its placeholder digest is
    left alone; ``build`` writes the real one and a later ``validate`` of
    ``dist/`` must then agree with it.
    """
    for name in sorted({str(artifact["entrypoint"]) for artifact in manifest.artifacts}):
        if not name.endswith(".pyz"):
            continue
        path = (root / name).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise PluginError("ARTIFACT_NOT_FOUND", f"Built Plugin artifact is missing: {name}",
                              category="artifact")
        actual_sha = _sha256(path)
        actual_size = path.stat().st_size
        expected_size = int(next(item["size_bytes"] for item in manifest.artifacts
                                 if str(item["entrypoint"]) == name))
        if actual_sha != str(next(item["sha256"] for item in manifest.artifacts
                                  if str(item["entrypoint"]) == name)) or actual_size != expected_size:
            raise PluginError(
                "ARTIFACT_INTEGRITY_FAILED",
                f"Built Plugin artifact was modified after build: {name} "
                f"(manifest sha256={str(next(item['sha256'] for item in manifest.artifacts if str(item['entrypoint']) == name))[:12]}"
                f"..., actual sha256={actual_sha[:12]}...). Rebuild the Plugin.",
                category="artifact",
                details={"artifact": name, "expected_size_bytes": expected_size, "actual_size_bytes": actual_size},
            )


def validate_project(path: str | Path) -> dict[str, Any]:
    target = Path(path).resolve()
    manifest_path = target / "manifest.json" if target.is_dir() else target
    manifest = load_manifest(manifest_path)
    root = manifest_path.parent
    entrypoint = manifest.runtime.get("entrypoint") or manifest.artifacts[0]["entrypoint"]
    source = (root / entrypoint).resolve()
    if not source.is_relative_to(root) or not source.is_file():
        raise PluginError("ARTIFACT_NOT_FOUND", f"Plugin entrypoint does not exist: {entrypoint}", category="cli")
    for artifact in manifest.artifacts:
        if Path(artifact["entrypoint"]).is_absolute() or ".." in Path(artifact["entrypoint"]).parts:
            raise PluginError("INVALID_PLUGIN_RESPONSE", "Plugin artifact path is unsafe", category="cli")

    # The checks below mirror what the runtime actually enforces when the
    # package is loaded.  Without them ``validate`` reported success and the
    # developer only discovered the problem as a runtime crash.
    os_name, arch = current_platform()
    platforms = sorted({(str(item["os"]), str(item["arch"])) for item in manifest.artifacts})
    if (os_name, arch) not in platforms:
        raise PluginError(
            "PLATFORM_UNSUPPORTED",
            f"Plugin declares no artifact for this platform ({os_name}/{arch}); declared "
            f"{', '.join(f'{item[0]}/{item[1]}' for item in platforms)}. "
            f"Supported os {sorted(SUPPORTED_OS)}, arch {sorted(SUPPORTED_ARCH)}.",
            category="compatibility",
            details={"current_platform": [os_name, arch], "declared_platforms": [list(item) for item in platforms]},
        )

    python: dict[str, Any] | None = None
    if manifest.runtime["type"] == "python":
        required = str(manifest.runtime["python_version_range"])
        current = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        python = {"required": required, "current": current,
                  "compatible": _range_allows(current, required)}
        if not python["compatible"]:
            raise PluginError(
                "PYTHON_RUNTIME_UNSUPPORTED",
                f"Plugin requires Python {required}, but this interpreter is {current}",
                category="runtime", details=python,
            )

    _verify_built_artifacts(root, manifest)
    return {"plugin": manifest.identity, "version": manifest.version, "entrypoint": entrypoint,
            "runtime": manifest.runtime["type"], "valid": True,
            "platform": {"current": [os_name, arch], "declared": [list(item) for item in platforms]},
            "python": python}


def _wheel_metadata(path: Path, *, base_url: str) -> dict[str, Any]:
    try:
        parsed_name, parsed_version, _build, tags = parse_wheel_filename(path.name)
    except Exception as exc:
        raise PluginError("DEPENDENCY_LOCK_INVALID", f"Invalid wheel filename: {path.name}", category="dependency") from exc
    with zipfile.ZipFile(path) as archive:
        metadata_name = next((name for name in archive.namelist() if name.endswith(".dist-info/METADATA")), None)
        if metadata_name is None:
            raise PluginError("DEPENDENCY_LOCK_INVALID", f"Wheel metadata is missing: {path.name}", category="dependency")
        metadata = BytesParser().parsebytes(archive.read(metadata_name))
    name = canonicalize_name(metadata.get("Name") or str(parsed_name))
    version = str(metadata.get("Version") or parsed_version)
    # A ``py2.py3`` wheel is represented by both interpreter tags by
    # packaging.  The lock describes the Python 3 runtime we support; picking
    # the lexicographically first tag would incorrectly freeze ``py2`` and
    # make an otherwise universal wheel unusable by the runtime selector.
    tag = next((candidate for candidate in tags if candidate.interpreter == "py3"), None)
    tag = tag or sorted(tags, key=str)[0]
    digest = _sha256(path)
    return {"name": name, "version": version, "filename": path.name,
            "url": f"{base_url.rstrip('/')}/{path.name}", "sha256": digest,
            "size_bytes": path.stat().st_size, "python_tag": tag.interpreter,
            "abi_tag": tag.abi, "platform_tag": tag.platform}


def _pypi_url(name: str, version: str, filename: str) -> str:
    url = f"https://pypi.org/pypi/{name}/{version}/json"
    with urllib.request.urlopen(url, timeout=20) as response:
        payload = json.load(response)
    for item in payload.get("urls", []):
        if item.get("filename") == filename and str(item.get("url") or "").startswith("https://"):
            return str(item["url"])
    raise PluginError("DEPENDENCY_ARTIFACT_NOT_FOUND", f"PyPI artifact URL is unavailable: {filename}", category="dependency")


def lock_dependencies(requirements: Path, output: Path, *, wheel_dir: Path | None = None,
                      base_url: str = "https://files.pythonhosted.org/packages/waveflow-lock",
                      platform: str | None = None, python_version: str | None = None,
                      implementation: str = "cp", abi: str | None = None, merge: bool = False,
                      manifest_path: Path | None = None) -> dict[str, Any]:
    if not requirements.is_file():
        raise PluginError("DEPENDENCY_LOCK_INVALID", "Dependency declaration is missing", category="dependency")
    temporary: tempfile.TemporaryDirectory[str] | None = None
    if wheel_dir is None:
        temporary = tempfile.TemporaryDirectory()
        wheel_dir = Path(temporary.name)
        command = [sys.executable, "-m", "pip", "download", "--only-binary=:all:",
                   "--dest", str(wheel_dir), "-r", str(requirements)]
        if platform:
            command.extend(["--platform", platform])
        if python_version:
            command.extend(["--python-version", python_version])
        if implementation:
            command.extend(["--implementation", implementation])
        if abi:
            command.extend(["--abi", abi])
        completed = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
        if completed.returncode:
            raise PluginError("DEPENDENCY_LOCK_INVALID", "Python dependency resolution failed", category="dependency",
                              details={"resolver": "pip"})
    wheels = sorted(wheel_dir.glob("*.whl"))
    if not wheels:
        raise PluginError("DEPENDENCY_ARTIFACT_NOT_FOUND", "No compatible wheels were resolved", category="dependency")
    artifacts = [_wheel_metadata(path, base_url=base_url) for path in wheels]
    if temporary is not None:
        for item in artifacts:
            item["url"] = _pypi_url(item["name"], item["version"], item["filename"])
    existing = _json(output).get("artifacts", []) if merge and output.is_file() else []
    by_key = {(item["name"], item["version"], item["python_tag"], item["abi_tag"], item["platform_tag"]): item
              for item in [*existing, *artifacts]}
    lock = {"lock_version": 1, "artifacts": sorted(by_key.values(), key=lambda item: (
        item["name"], item["version"], item["python_tag"], item["abi_tag"], item["platform_tag"]))}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(lock, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if manifest_path is not None:
        manifest_data = _json(manifest_path)
        runtime = manifest_data.get("runtime")
        if not isinstance(runtime, dict) or runtime.get("type") != "python":
            raise PluginError("PLUGIN_INCOMPATIBLE", "Dependency lock requires a Python Plugin runtime",
                              category="runtime")
        runtime["dependency_lock"] = lock
        validate_manifest(manifest_data)
        manifest_path.write_text(json.dumps(manifest_data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if temporary is not None:
        temporary.cleanup()
    return lock


def _zip_write(archive: zipfile.ZipFile, name: str, data: bytes, *, executable: bool = False) -> None:
    info = zipfile.ZipInfo(name, FIXED_ZIP_TIME)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = ((0o755 if executable else 0o644) & 0xFFFF) << 16
    archive.writestr(info, data)


def _project_resources(root: Path, entrypoint: Path) -> list[tuple[str, bytes]]:
    """Return safe non-build files that a Python Plugin imports at runtime."""
    excluded_files = {
        "manifest.json", "dependency-lock.json", "requirements.in", "requirements.txt",
        "README", "README.md", "README.rst", "README.txt",
    }
    # ``dependencies`` holds dependency wheels.  They are distributed beside the
    # artifact and installed into the managed Python environment, so bundling
    # them inside the ``.pyz`` as well would double every download and is not
    # where the runtime loader looks for them.
    excluded_dirs = {".git", "__pycache__", "dist", ".pytest_cache", "dependencies"}
    resources: list[tuple[str, bytes]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.resolve() == entrypoint.resolve():
            continue
        relative = path.relative_to(root)
        if any(part in excluded_dirs for part in relative.parts) or path.name in excluded_files:
            continue
        if path.suffix in {".pyc", ".pyo", ".pyz"} or path.name.startswith("."):
            continue
        archive_name = str(relative).replace("\\", "/")
        if archive_name == "__main__.py" or archive_name.startswith("waveflow_plugin_sdk/"):
            raise PluginError("INVALID_PLUGIN_RESPONSE", "Plugin resource uses a reserved archive path", category="cli")
        resources.append((archive_name, path.read_bytes()))
    return resources


def build_sdk_artifact(entrypoint: str | Path, output: str | Path, *, resource_root: str | Path | None = None) -> dict[str, Any]:
    source = Path(entrypoint).resolve()
    target = Path(output).resolve()
    if not source.is_file():
        raise PluginError("ARTIFACT_NOT_FOUND", "Plugin entrypoint does not exist", category="cli")
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.with_suffix(target.suffix + ".tmp")
    with zipfile.ZipFile(staging, "w") as archive:
        _zip_write(archive, "__main__.py", source.read_bytes(), executable=True)
        for path in sorted(SDK_ROOT.rglob("*.py")):
            _zip_write(archive, str(Path("waveflow_plugin_sdk") / path.relative_to(SDK_ROOT)), path.read_bytes())
        if resource_root is not None:
            root = Path(resource_root).resolve()
            if not root.is_dir() or not source.is_relative_to(root):
                raise PluginError("ARTIFACT_NOT_FOUND", "Plugin resource root is invalid", category="cli")
            for name, data in _project_resources(root, source):
                _zip_write(archive, name, data)
    os.replace(staging, target)
    return {"artifact": str(target), "sha256": _sha256(target),
            "size_bytes": target.stat().st_size}


def _check_locked_wheels(lock: dict[str, Any], available: set[str], *, manage_wheels: bool) -> list[str]:
    """Report locked wheels this machine will need but cannot find.

    ``select_dependency_artifacts`` resolves the lock against the current
    interpreter, which is what the runtime loader does.  A lock that only
    targets another platform cannot be resolved here and is left alone.

    Wheels are only required once a project manages them locally: official
    Plugins are built from source with no ``dependencies/`` directory and their
    wheels are attached by the release pipeline, so an absent directory is not
    an error.  A directory that exists must however cover the lock, otherwise
    the developer only learns about it when the loader refuses to install.
    """
    if not lock.get("artifacts"):
        return []
    try:
        selected = select_dependency_artifacts(lock)
    except PluginError:
        return []
    missing = [str(item["filename"]) for item in selected if str(item["filename"]) not in available]
    if not missing:
        return []
    if manage_wheels:
        raise PluginError(
            "DEPENDENCY_ARTIFACT_NOT_FOUND",
            f"Locked dependency wheel is missing: {missing[0]}. Download it into dependencies/ first "
            "(pip download -d dependencies -r requirements.in) or re-run `waveflow-plugin lock`.",
            category="dependency",
            details={"missing": missing, "expected_path": f"dependencies/{missing[0]}"},
        )
    return [f"locked wheel is not present in dependencies/: {name}" for name in missing]


def _stage_dependency_wheels(root: Path, dist: Path) -> list[str]:
    """Copy ``<project>/dependencies/*.whl`` to ``<dist>/dependencies/``.

    The developer loader resolves locked wheels at ``<package>/dependencies/<filename>``
    and then ``<package>/<filename>``.  ``build`` used to leave them only inside
    the ``.pyz``, so every dependency-bearing Plugin failed to install locally.
    """
    source = root / "dependencies"
    wheels = sorted(source.glob("*.whl")) if source.is_dir() else []
    if not wheels:
        return []
    layout = dist / "dependencies"
    layout.mkdir(parents=True, exist_ok=True)
    for wheel in wheels:
        shutil.copyfile(wheel, layout / wheel.name)
    return [wheel.name for wheel in wheels]


def build_project(project: str | Path, *, output: str | Path | None = None) -> dict[str, Any]:
    root = Path(project).resolve()
    manifest_data = _json(root / "manifest.json")
    lock_path = root / "dependency-lock.json"
    if lock_path.is_file():
        lock_data = _json(lock_path)
        if manifest_data.get("runtime", {}).get("dependency_lock") != lock_data:
            raise PluginError("DEPENDENCY_LOCK_INVALID", "Manifest dependency lock is not synchronized",
                              category="dependency")
    manifest = validate_manifest(manifest_data)
    entrypoint = str(manifest.runtime.get("entrypoint") or manifest.artifacts[0]["entrypoint"])
    source = (root / entrypoint).resolve()
    if not source.is_relative_to(root) or not source.is_file():
        raise PluginError("ARTIFACT_NOT_FOUND", "Plugin entrypoint does not exist", category="cli")
    target = Path(output).resolve() if output else root / "dist" / f"{manifest.plugin_id}-{manifest.version}.pyz"
    wheels = _stage_dependency_wheels(root, target.parent)
    warnings: list[str] = []
    if manifest.runtime["type"] == "python":
        warnings = _check_locked_wheels(manifest.runtime.get("dependency_lock") or {}, set(wheels),
                                        manage_wheels=(root / "dependencies").is_dir())
    built = build_sdk_artifact(source, target, resource_root=root)
    digest = built["sha256"]
    for artifact in manifest_data["artifacts"]:
        artifact.update({"entrypoint": target.name, "sha256": digest, "size_bytes": target.stat().st_size})
        artifact["signature"] = {
            "algorithm": "ed25519", "key_id": artifact["signature"].get("key_id") or "unsigned",
            "value": "UNSIGNED",
        }
    if manifest_data["runtime"]["type"] == "python":
        manifest_data["runtime"]["entrypoint"] = target.name
    dist_manifest = target.parent / "manifest.json"
    dist_manifest.write_text(json.dumps(manifest_data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"artifact": str(target), "manifest": str(dist_manifest), "sha256": digest,
            "size_bytes": target.stat().st_size, "dependencies": wheels, "warnings": warnings}


def sign_build(manifest_path: str | Path, key_path: str | Path, *, key_id: str) -> dict[str, Any]:
    manifest_file = Path(manifest_path).resolve()
    data = _json(manifest_file)
    manifest = validate_manifest(data)
    artifact = data["artifacts"][0]
    artifact_path = (manifest_file.parent / artifact["entrypoint"]).resolve()
    if not artifact_path.is_relative_to(manifest_file.parent) or not artifact_path.is_file():
        raise PluginError("ARTIFACT_NOT_FOUND", f"Built Plugin artifact is missing: {artifact['entrypoint']}",
                          category="cli")
    if _sha256(artifact_path) != artifact["sha256"] or artifact_path.stat().st_size != artifact["size_bytes"]:
        raise PluginError("ARTIFACT_INTEGRITY_FAILED", "Built Plugin artifact integrity check failed", category="cli")
    payload = artifact_path.read_bytes()
    key_file = Path(key_path)
    if not key_file.is_file():
        raise PluginError("ARTIFACT_NOT_FOUND", f"Signing key does not exist: {key_path}", category="artifact")
    raw = key_file.read_bytes()
    try:
        key = serialization.load_pem_private_key(raw, password=None)
    except (ValueError, TypeError):
        try:
            key = Ed25519PrivateKey.from_private_bytes(raw)
        except ValueError as exc:
            raise PluginError(
                "AUTH_FAILED",
                f"Signing key is not an Ed25519 PEM private key or 32 raw key bytes: {key_path}",
                category="trust",
            ) from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise PluginError("AUTH_FAILED", "Signing key is not Ed25519", category="trust")
    signature = {"algorithm": "ed25519", "key_id": key_id,
                 "value": base64.b64encode(key.sign(payload)).decode()}
    for candidate in data["artifacts"]:
        if (candidate["entrypoint"], candidate["sha256"], candidate["size_bytes"]) != (
                artifact["entrypoint"], artifact["sha256"], artifact["size_bytes"]):
            raise PluginError("ARTIFACT_INTEGRITY_FAILED", "Build contains inconsistent platform artifacts",
                              category="cli")
        candidate["signature"] = dict(signature)
    signed_manifest = validate_manifest(data)
    manifest_signature = {
        "algorithm": "ed25519", "key_id": key_id,
        "value": base64.b64encode(key.sign(manifest_signature_payload(signed_manifest))).decode(),
    }
    manifest_file.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    package = {"schema_version": 1, "id": f"local::{manifest.plugin_id}", "name": manifest.display_name,
               "kind": "plugin_package", "package_type": "plugin_package", "version": manifest.version,
               "plugin_manifest": data,
               "manifest_signature": manifest_signature,
               "artifact_references": [{"sha256": artifact["sha256"], "local_path": str(artifact_path)}],
               "market_source": {"source_key": "local"}}
    package_path = manifest_file.parent / "market-package.json"
    package_path.write_text(json.dumps(package, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"manifest": str(manifest_file), "package": str(package_path), "key_id": key_id}


def default_test_method(manifest: Any) -> str:
    """Pick the provider method this Plugin actually implements.

    The previous default was always ``tv.resolve_stream``, so a freshly
    scaffolded Radio Plugin failed its own ``test`` before the author had done
    anything wrong.
    """
    contracts = {item.contract: item for item in manifest.provider_contracts}
    if "tv_provider" in contracts:
        return "tv.resolve_stream"
    if "radio_provider" in contracts:
        return "radio.catalog" if "catalog" in contracts["radio_provider"].features else "radio.resolve_stream"
    if "channel_catalog" in contracts:
        return "channel_catalog.discover"
    return "tv.visual_metadata"


def _owned_scheme(manifest: Any, contract: str) -> str:
    for scheme, declared in manifest.owned_schemes:
        if declared == contract:
            return scheme
    raise PluginError(
        "RESOURCE_NOT_FOUND",
        f"Plugin declares no {contract}-owned scheme, so no request payload can be derived; "
        "pass --method and --payload explicitly.",
        category="cli",
    )


def default_test_payload(manifest: Any, method: str) -> dict[str, Any]:
    """Build a request payload from the Plugin's real owned scheme.

    The previous default used the synthetic scheme ``synthetic``, which only
    passed because the SDK fell back to a lone registered provider.
    """
    if method in {"tv.resolve_stream", "tv.visual_metadata"}:
        return {"scheme": _owned_scheme(manifest, "tv_provider"), "resource_id": "one", "query": {}}
    if method in {"radio.resolve_stream", "radio.programme"}:
        return {"station_ref": {"provider_key": _owned_scheme(manifest, "radio_provider"),
                                "provider_station_id": "one"},
                "playback_config": {}}
    return {}


async def test_build(manifest_path: Path, *, method: str | None = None, payload: dict[str, Any] | None = None,
                     capabilities: dict[str, Any] | None = None, python: str = sys.executable) -> dict[str, Any]:
    manifest = load_manifest(manifest_path)
    root = manifest_path.parent
    resolved_method = method or default_test_method(manifest)
    resolved_payload = payload if payload is not None else default_test_payload(manifest, resolved_method)
    # ``test`` runs the artifact the loader would run; verify it first so a
    # modified or half-built .pyz is reported as such instead of as a crash.
    _verify_built_artifacts(root, manifest)
    artifact = (root / manifest.artifacts[0]["entrypoint"]).resolve()
    capability_values = capabilities or {}

    async def capability(method_name: str, request: dict[str, Any], _timeout: float, _context: dict[str, Any]) -> Any:
        value = capability_values.get(method_name)
        if value is None:
            raise PluginError(
                "CAPABILITY_DENIED",
                f"Test capability fixture does not define {method_name}. Add it to the JSON file passed with "
                "--capabilities, e.g. {\"" + method_name + "\": {\"status\": 200, \"headers\": {}, "
                "\"body\": {...}, \"response_mode\": \"json\"}}",
                category="capability",
                details={"available_fixtures": sorted(capability_values)},
            )
        return value

    process = PluginProcess((python, str(artifact), "--identity", manifest.identity, "--version", manifest.version),
                            "cli-test", capability_handler=capability)
    await process.start()
    try:
        try:
            hello = await process.call("runtime.hello", {})
            process.negotiate_protocol(str(hello.get("protocol_version") or "1.0"))
            health = await process.call("runtime.health", {})
            result = await process.call(resolved_method, resolved_payload)
        except PluginError as exc:
            # The Plugin's own traceback is the only useful signal when it dies
            # during startup or on a missing packaged resource.  This is the
            # developer's local CLI, so show the bounded, sanitized tail.
            tail = process.diagnostic_tail()
            if tail:
                exc.details = {**dict(exc.details or {}), "plugin_stderr": tail}
            raise
        if resolved_method.endswith("resolve_stream"):
            validate_stream_descriptor(result)
        elif resolved_method == "radio.catalog":
            validate_radio_catalog(
                result,
                owned_schemes={scheme for scheme, contract in manifest.owned_schemes
                               if contract == "radio_provider"},
            )
        return {"hello": hello, "health": health, "method": resolved_method, "payload": resolved_payload,
                "result": result}
    finally:
        try:
            await process.call("runtime.shutdown", {}, timeout=2)
        except PluginError:
            pass
        await process.stop()


def init_project(path: Path, *, kind: str, publisher: str, plugin_id: str, scheme: str) -> None:
    path.mkdir(parents=True, exist_ok=False)
    contract = "tv_provider" if kind == "tv" else "radio_provider"
    features = ["resolve_stream"] if kind == "tv" else ["catalog", "resolve_stream"]
    capabilities = ["tv.resolve_stream"] if kind == "tv" else ["radio.catalog", "radio.resolve_stream"]
    manifest = {"manifest_version": 1, "publisher_id": publisher, "plugin_id": plugin_id,
        "display_name": plugin_id.replace("-", " ").title(), "version": "0.1.0", "plugin_api_version": "1.0",
        "core_version_range": ">=0.1.0 <1.0.0",
        "provider_contracts": [{"contract": contract, "contract_version": "1.0", "features": features}],
        "owned_schemes": [{"scheme": scheme, "contract": contract}], "capabilities": capabilities,
        "permissions": {}, "runtime": {"type": "python", "ipc": "stdio_framed_json_v1",
            "python_version_range": ">=3.11.0 <4.0.0", "entrypoint": "provider.py",
            "dependency_lock": {"lock_version": 1, "artifacts": []}},
        "artifacts": [{"os": os_name, "arch": arch, "runtime": "python", "entrypoint": "provider.py",
                       "sha256": "0" * 64, "size_bytes": 1,
                       "signature": {"algorithm": "ed25519", "key_id": "developer-key", "value": "UNSIGNED"}}
                      for os_name, arch in CANONICAL_PLATFORMS],
        "dependencies": [], "state_schema_version": 1}
    (path / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    base = "TVProvider, TVReference" if kind == "tv" else "RadioProvider, RadioReference"
    provider_class = "TVProvider" if kind == "tv" else "RadioProvider"
    reference = "TVReference" if kind == "tv" else "RadioReference"
    catalog = "" if kind == "tv" else f'''\n    def catalog(self, payload: dict, context: ResolveContext) -> dict:\n        return {{"stations": [{{\n            "station_ref": {{"provider_key": "{scheme}", "provider_station_id": "demo"}},\n            "name": "Demo Station",\n            "ttl_seconds": 300,\n        }}]}}\n'''
    source = f'''from waveflow_plugin_sdk import PluginApplication, {base}, ResolveContext, StreamDescriptor\n\nclass Provider({provider_class}):{catalog}\n    def resolve_stream(self, reference: {reference}, context: ResolveContext) -> StreamDescriptor:\n        return StreamDescriptor.hls("https://example.invalid/live.m3u8", ttl_seconds=60)\n\ndef main() -> None:\n    identity, version = PluginApplication.identity_args("{publisher}/{plugin_id}", "0.1.0")\n    app = PluginApplication(identity=identity, version=version)\n    app.register_{kind}("{scheme}", Provider())\n    app.run()\n\nif __name__ == "__main__":\n    main()\n'''
    (path / "provider.py").write_text(source)
    (path / "requirements.in").write_text("")
    (path / "dependency-lock.json").write_text(json.dumps({"lock_version": 1, "artifacts": []}, indent=2) + "\n")
    (path / "README.md").write_text(f'''# {manifest["display_name"]}

A WaveFlow {contract.replace("_provider", "").upper()} Provider Plugin owning the `{scheme}` scheme.

## Develop

```sh
waveflow-plugin validate .
waveflow-plugin build .
waveflow-plugin test dist/manifest.json
```

- `validate` checks the manifest against the same contract the runtime enforces
  (platform coverage, Python range, built artifact digest).
- `build` writes `dist/<plugin_id>-<version>.pyz` plus `dist/manifest.json`, and
  copies any wheels from `dependencies/` to `dist/dependencies/`.
- `test` spawns the built artifact and calls the provider method derived from
  `provider_contracts`, using a payload built from `owned_schemes`. Override with
  `--method` / `--payload`, and supply Core capability fixtures with
  `--capabilities`.

## Package for a local install

```sh
waveflow-plugin sign dist/manifest.json --key <ed25519.pem> --key-id <key-id>
```

To try it in WaveFlow, open Settings → Plugins → 开发者选项, enable
Developer Mode, then paste the absolute path to dist/manifest.json into
安装本地 Plugin. That entry does not upload files — use a path the WaveFlow
server can read (a container path on NAS/Docker, a local path on desktop).

## Dependencies

Add requirements to `requirements.in`, then:

```sh
pip download -d dependencies -r requirements.in
waveflow-plugin lock requirements.in --output dependency-lock.json --wheel-dir dependencies --manifest manifest.json
```
''')


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="waveflow-plugin",
        description="WaveFlow Plugin SDK: scaffold, validate, build, test and sign a Provider Plugin.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="scaffold a new Plugin project")
    init.add_argument("path", type=Path, help="project directory to create (must not exist)")
    init.add_argument("--kind", choices=("tv", "radio"), default="tv", help="provider contract to scaffold")
    init.add_argument("--publisher", required=True, help="publisher id, e.g. org.example")
    init.add_argument("--plugin-id", required=True, help="plugin id, e.g. demo-tv")
    init.add_argument("--scheme", required=True, help="scheme the Plugin owns, e.g. demotv")

    validate = sub.add_parser("validate", help="check a project or dist/ manifest against the runtime contract")
    validate.add_argument("path", help="project directory or manifest.json")

    lock = sub.add_parser("lock", help="resolve Python dependencies into a dependency-lock.json")
    lock.add_argument("requirements", type=Path, help="requirements file, e.g. requirements.in")
    lock.add_argument("--output", type=Path, required=True, help="lock file to write")
    lock.add_argument("--manifest", type=Path, help="also merge the lock into this manifest.json")
    lock.add_argument("--wheel-dir", type=Path, help="use wheels already downloaded here instead of calling pip")
    lock.add_argument("--base-url", default="https://files.pythonhosted.org/packages/waveflow-lock",
                      help="URL prefix recorded in the lock; the release pipeline rewrites it")
    lock.add_argument("--platform", help="target platform for pip download, e.g. macosx_11_0_arm64")
    lock.add_argument("--python-version", help="target Python version for pip download, e.g. 3.14")
    lock.add_argument("--implementation", default="cp", help="target implementation for pip download")
    lock.add_argument("--abi", help="target ABI for pip download, e.g. cp314")
    lock.add_argument("--merge", action="store_true", help="merge into an existing lock file")

    build = sub.add_parser("build", help="package the project into dist/<plugin_id>-<version>.pyz")
    build.add_argument("project", help="project directory")
    build.add_argument("--output", help="artifact path (default: dist/<plugin_id>-<version>.pyz)")

    sign = sub.add_parser("sign", help="sign a built artifact and write market-package.json")
    sign.add_argument("manifest", help="built manifest.json, usually dist/manifest.json")
    sign.add_argument("--key", required=True, help="Ed25519 private key: PEM file or 32 raw bytes")
    sign.add_argument("--key-id", required=True, help="key id recorded in the signature")

    test = sub.add_parser("test", help="spawn a built artifact and call one provider method")
    test.add_argument("manifest", type=Path, help="built manifest.json, usually dist/manifest.json")
    test.add_argument("--method", default=None,
                      help="provider method to call; derived from provider_contracts when omitted")
    test.add_argument("--payload", default=None,
                      help="JSON request payload; derived from owned_schemes when omitted, e.g. "
                           '\'{"scheme":"demotv","resource_id":"one","query":{}}\'')
    test.add_argument("--capabilities",
                      help="JSON file of Core capability fixtures, e.g. "
                           '\'{"core.http.fetch": {"status": 200, "headers": {}, "body": {}, '
                           '"response_mode": "json"}}\'')
    test.add_argument("--python", default=sys.executable, help="interpreter used to run the artifact")

    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            init_project(args.path, kind=args.kind, publisher=args.publisher, plugin_id=args.plugin_id, scheme=args.scheme); result = {"created": str(args.path)}
        elif args.command == "validate": result = validate_project(args.path)
        elif args.command == "lock": result = lock_dependencies(args.requirements, args.output, wheel_dir=args.wheel_dir,
            base_url=args.base_url, platform=args.platform, python_version=args.python_version,
            implementation=args.implementation, abi=args.abi, merge=args.merge, manifest_path=args.manifest)
        elif args.command == "build": result = build_project(args.project, output=args.output)
        elif args.command == "sign": result = sign_build(args.manifest, args.key, key_id=args.key_id)
        else:
            fixtures = _json(Path(args.capabilities)) if args.capabilities else {}
            payload: dict[str, Any] | None = None
            if args.payload is not None:
                try:
                    payload = json.loads(args.payload)
                except json.JSONDecodeError as exc:
                    raise PluginError(
                        "INVALID_PLUGIN_RESPONSE",
                        f"--payload is not valid JSON: {exc}. Expected an object, e.g. "
                        '\'{"scheme":"demotv","resource_id":"one","query":{}}\'',
                        category="cli",
                    ) from exc
            result = asyncio.run(test_build(args.manifest, method=args.method, payload=payload,
                                            capabilities=fixtures, python=args.python))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (PluginError, OSError, ValueError) as exc:
        code = exc.code if isinstance(exc, PluginError) else "INVALID_PLUGIN_RESPONSE"
        error: dict[str, Any] = {"code": code, "message": str(exc)[:500]}
        if isinstance(exc, PluginError):
            error["category"] = exc.category
            if exc.details:
                error["details"] = dict(exc.details)
        print(json.dumps({"error": error}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
