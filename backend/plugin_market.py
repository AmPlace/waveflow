from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import platform
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from contextlib import contextmanager
from typing import Any, Awaitable, Callable, Iterable, Iterator, Sequence

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

import database as db
from official_plugin_distribution import OFFICIAL_PUBLISHER_ID, OFFICIAL_SOURCE_KEY
from plugin_runtime import LifecycleState, PluginError, PluginInstance, PluginManifest, PluginRuntime, validate_manifest
from plugin_runtime.manifest import _range_allows, _version_tuple
from plugin_python_runtime import PythonEnvironmentManager, select_dependency_artifacts, validate_python_runtime
from plugin_permissions import HIGH_RISK_PERMISSIONS, permission_projection, require_high_risk_approvals, requested_permissions


PLUGIN_PACKAGE_TYPE = "plugin_package"
CONTENT_PACKAGE_TYPE = "content_package"
DEPENDENCY_STATES = frozenset({"ready", "dependency_missing", "plugin_incompatible", "provider_unavailable"})
MAX_DEVELOPER_ARTIFACT_BYTES = 64 * 1024 * 1024
DEVELOPER_LOCAL_TRUST_CLASS = "developer_local"
DEVELOPER_LOCAL_SOURCE_KEY = "developer_local"
UNSIGNED_DEVELOPER_SIGNATURE = "UNSIGNED"
logger = logging.getLogger(__name__)


class LifecycleLock:
    """Task-reentrant lock used for one canonical Plugin lifecycle.

    Runtime activation authorization re-enters the service lifecycle boundary
    from the same task.  Other tasks still serialize on the underlying lock.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._owner: asyncio.Task[Any] | None = None
        self._depth = 0

    async def acquire(self) -> bool:
        current = asyncio.current_task()
        if current is None:
            raise RuntimeError("Plugin lifecycle lock requires an asyncio task")
        if self._owner is current:
            self._depth += 1
            return True
        await self._lock.acquire()
        self._owner = current
        self._depth = 1
        return True

    def release(self) -> None:
        current = asyncio.current_task()
        if self._owner is not current or self._depth <= 0:
            raise RuntimeError("Plugin lifecycle lock released by non-owner")
        self._depth -= 1
        if self._depth == 0:
            self._owner = None
            self._lock.release()

    def locked(self) -> bool:
        return self._lock.locked()

    async def __aenter__(self) -> "LifecycleLock":
        await self.acquire()
        return self

    async def __aexit__(self, _type, _value, _traceback) -> None:
        self.release()


def _safe_error(error: BaseException) -> str:
    if isinstance(error, PluginError):
        return f"{error.code}: {error.message}"[:1024]
    return "Plugin lifecycle operation failed"


def _manifest_json(manifest: PluginManifest) -> str:
    return json.dumps(manifest.raw, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _manifest_sha256(manifest: PluginManifest) -> str:
    return hashlib.sha256(_manifest_json(manifest).encode("utf-8")).hexdigest()


def manifest_signature_payload(manifest: PluginManifest) -> bytes:
    """Canonical release statement covered by a Market Package signature."""
    return b"waveflow-plugin-manifest-v1\0" + _manifest_json(manifest).encode("utf-8")


def current_platform() -> tuple[str, str]:
    system = platform.system().lower()
    os_name = {"darwin": "macos", "linux": "linux", "windows": "windows"}.get(system, system)
    machine = platform.machine().lower()
    arch = {"amd64": "x86_64", "x64": "x86_64", "aarch64": "arm64"}.get(machine, machine)
    return os_name, arch


@dataclass(frozen=True)
class PluginCandidate:
    package_id: str
    source_key: str
    manifest: PluginManifest
    artifact: dict[str, Any]
    local_reference: Path
    dependency_references: dict[str, Path]
    manifest_signature: dict[str, Any] | None

    @property
    def identity(self) -> str:
        return self.manifest.identity


@dataclass(frozen=True)
class PreparedPluginCandidate:
    candidate: PluginCandidate
    staged_artifact: Path
    trust_state: str
    environment: Any | None = None


class FixtureTrustPolicy:
    """Explicit publisher/key trust for local fixtures; no remote trust inference."""

    def __init__(self, trusted_keys: dict[tuple[str, str], bytes]):
        self._keys = dict(trusted_keys)

    def verify(self, manifest: PluginManifest, artifact: dict[str, Any], payload: bytes) -> str:
        signature = artifact.get("signature") or {}
        if signature.get("algorithm") != "ed25519":
            raise PluginError("PLUGIN_INCOMPATIBLE", "Unsupported plugin signature", category="trust")
        key_id = str(signature.get("key_id") or "")
        key = self._keys.get((manifest.publisher_id, key_id))
        if key is None:
            raise PluginError("AUTH_FAILED", "Plugin publisher is not trusted", category="trust")
        try:
            encoded = base64.b64decode(str(signature.get("value") or ""), validate=True)
            Ed25519PublicKey.from_public_bytes(key).verify(encoded, payload)
        except (ValueError, InvalidSignature) as exc:
            raise PluginError("AUTH_FAILED", "Plugin signature verification failed", category="trust") from exc
        return "fixture_trusted"

    def verify_manifest(
        self, manifest: PluginManifest, artifact: dict[str, Any], signature: dict[str, Any] | None,
    ) -> None:
        # Manifest signatures are a compatible Market Package extension. Old
        # fixture/third-party packages remain valid, while fixtures that opt in
        # are checked by the same publisher key as their artifact.
        if signature is None:
            return
        if (not isinstance(signature, dict)
                or set(signature) != {"algorithm", "key_id", "value"}
                or signature.get("algorithm") != "ed25519"):
            raise PluginError("AUTH_FAILED", "Plugin manifest signature is invalid", category="trust")
        key_id = str(signature.get("key_id") or "")
        if key_id != str((artifact.get("signature") or {}).get("key_id") or ""):
            raise PluginError("AUTH_FAILED", "Plugin manifest signer does not match its artifact", category="trust")
        key = self._keys.get((manifest.publisher_id, key_id))
        if key is None:
            raise PluginError("AUTH_FAILED", "Plugin publisher is not trusted", category="trust")
        try:
            encoded = base64.b64decode(str(signature.get("value") or ""), validate=True)
            Ed25519PublicKey.from_public_bytes(key).verify(encoded, manifest_signature_payload(manifest))
        except (ValueError, InvalidSignature) as exc:
            raise PluginError("AUTH_FAILED", "Plugin manifest signature verification failed", category="trust") from exc


class DeveloperLocalTrustPolicy:
    """Trust policy for explicitly selected local Developer packages.

    This is intentionally not a mode on the production Ed25519 policy.  A
    local package must carry the exact unsigned marker and is accepted only by
    the explicit Developer install path; artifact digest and manifest
    validation happen before this policy is invoked.
    """

    def verify(self, manifest: PluginManifest, artifact: dict[str, Any], payload: bytes) -> str:
        signature = artifact.get("signature") or {}
        if (
            set(signature) != {"algorithm", "key_id", "value"}
            or signature.get("algorithm") != "ed25519"
            or signature.get("value") != UNSIGNED_DEVELOPER_SIGNATURE
        ):
            raise PluginError(
                "PLUGIN_UNTRUSTED",
                "Developer package does not contain the required local unsigned marker",
                category="trust",
            )
        return DEVELOPER_LOCAL_TRUST_CLASS

    def verify_manifest(
        self, manifest: PluginManifest, artifact: dict[str, Any], signature: dict[str, Any] | None,
    ) -> None:
        if signature is not None:
            raise PluginError(
                "PLUGIN_UNTRUSTED",
                "Developer local packages cannot use an unverified Market manifest signature",
                category="trust",
            )


def _read_artifact(path: Path, *, max_bytes: int | None = None) -> bytes:
    if max_bytes is not None and path.stat().st_size > max_bytes:
        raise PluginError("ARTIFACT_INVALID", "Plugin artifact exceeds the verification limit", category="artifact")
    with path.open("rb") as stream:
        return stream.read()


class PluginArtifactStore:
    def __init__(self, root: str | Path, *, allowed_local_roots: Iterable[str | Path]):
        self.root = Path(root).resolve()
        self.allowed_local_roots = tuple(Path(value).resolve() for value in allowed_local_roots)
        self.staged_root = self.root / "staged"
        self.installed_root = self.root / "installed"
        self.staged_root.mkdir(parents=True, exist_ok=True)
        self.installed_root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _part(value: str) -> str:
        if not value or value in {".", ".."} or any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789._-" for ch in value):
            raise PluginError("INVALID_PLUGIN_RESPONSE", "Invalid plugin artifact namespace", category="artifact")
        return value

    def _source(self, reference: Path) -> Path:
        resolved = reference.resolve(strict=True)
        if not resolved.is_file() or not any(resolved.is_relative_to(root) for root in self.allowed_local_roots):
            raise PluginError("CAPABILITY_DENIED", "Local plugin artifact is outside an allowed fixture root", category="artifact")
        return resolved

    def stage(self, candidate: PluginCandidate, trust: FixtureTrustPolicy) -> tuple[Path, str]:
        source = self._source(candidate.local_reference)
        artifact = candidate.artifact
        identity_dir = Path(self._part(candidate.manifest.publisher_id)) / self._part(candidate.manifest.plugin_id)
        version = self._part(candidate.manifest.version)
        digest = str(artifact["sha256"])
        target_dir = self.staged_root / identity_dir / version / digest
        target_dir.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix="artifact-", dir=target_dir)
        os.close(fd)
        temp = Path(temp_name)
        try:
            shutil.copyfile(source, temp)
            payload = _read_artifact(temp, max_bytes=int(artifact["size_bytes"]))
            actual = hashlib.sha256(payload).hexdigest()
            if actual != digest or len(payload) != int(artifact["size_bytes"]):
                raise PluginError("INVALID_PLUGIN_RESPONSE", "Plugin artifact digest or size mismatch", category="artifact")
            trust_state = trust.verify(candidate.manifest, artifact, payload)
            staged = target_dir / "artifact"
            os.replace(temp, staged)
            return staged, trust_state
        except BaseException:
            temp.unlink(missing_ok=True)
            shutil.rmtree(target_dir, ignore_errors=True)
            raise

    def promote(self, candidate: PluginCandidate, staged: Path) -> Path:
        identity_dir = Path(self._part(candidate.manifest.publisher_id)) / self._part(candidate.manifest.plugin_id)
        target_dir = self.installed_root / identity_dir / self._part(candidate.manifest.version) / candidate.artifact["sha256"]
        target_dir.parent.mkdir(parents=True, exist_ok=True)
        if target_dir.exists():
            # Repair can target the exact directory already referenced by the
            # durable installation.  Replace only the artifact file so an I/O
            # failure cannot first delete the last known-good directory.
            if not target_dir.is_dir():
                raise PluginError(
                    "ARTIFACT_INTEGRITY_FAILED", "Installed plugin artifact path is invalid", category="artifact",
                )
            os.replace(staged, target_dir / "artifact")
            try:
                staged.parent.rmdir()
            except OSError:
                # The staged directory is not a durable reference and can be
                # collected on the next recovery pass.
                pass
        else:
            os.replace(staged.parent, target_dir)
        return target_dir / "artifact"

    def remove_path(self, value: str | Path) -> None:
        path = Path(value).resolve()
        if path.is_relative_to(self.staged_root) or path.is_relative_to(self.installed_root):
            shutil.rmtree(path.parent, ignore_errors=True)

    def remove_plugin(self, publisher_id: str, plugin_id: str) -> None:
        relative = Path(self._part(publisher_id)) / self._part(plugin_id)
        shutil.rmtree(self.staged_root / relative, ignore_errors=True)
        shutil.rmtree(self.installed_root / relative, ignore_errors=True)

    def cleanup_orphan_staging(self, live_paths: Iterable[str]) -> int:
        live = {str(Path(path).resolve()) for path in live_paths}
        removed = 0
        if not self.staged_root.exists():
            return 0
        for artifact in self.staged_root.glob("*/*/*/*/artifact"):
            if str(artifact.resolve()) not in live:
                shutil.rmtree(artifact.parent, ignore_errors=True)
                removed += 1
        return removed

    def cleanup_orphan_installed(self, live_paths: Iterable[str]) -> int:
        live = {str(Path(path).resolve()) for path in live_paths}
        removed = 0
        if not self.installed_root.exists():
            return 0
        for artifact in self.installed_root.glob("*/*/*/*/artifact"):
            if str(artifact.resolve()) not in live:
                shutil.rmtree(artifact.parent, ignore_errors=True)
                removed += 1
        return removed


def _artifact_reference(package: dict, sha256: str) -> Path:
    matches = [item for item in package.get("artifact_references", [])
               if isinstance(item, dict) and item.get("sha256") == sha256]
    if len(matches) != 1 or set(matches[0]) != {"sha256", "local_path"}:
        raise PluginError("RESOURCE_NOT_FOUND", "Plugin artifact reference is missing or ambiguous", category="artifact")
    return Path(str(matches[0]["local_path"]))


def _assert_package_manifest_consistency(package: dict, manifest: PluginManifest) -> None:
    """Reject a package envelope that contradicts the manifest it carries.

    Publisher, version and signature material are declared twice: once on the
    Market package and once inside ``plugin_manifest``.  The manifest is
    authoritative, so an envelope that disagrees is rejected rather than
    silently ignored -- otherwise a third-party or remote package could present
    itself as one Plugin while installing another.

    This runs at package validation time and therefore does not depend on the
    official builder having produced the package.  Each check is applied only
    when the envelope actually declares the field, because a package is allowed
    to omit envelope metadata entirely.  A declared field that is malformed is
    rejected rather than ignored.
    """
    publisher = package.get("publisher")
    if publisher is not None:
        if not isinstance(publisher, dict):
            raise PluginError(
                "PLUGIN_INCOMPATIBLE",
                "Plugin package publisher is malformed",
                category="market",
            )
        declared_publisher = str(publisher.get("id") or "")
        if declared_publisher and declared_publisher != manifest.publisher_id:
            raise PluginError(
                "PLUGIN_INCOMPATIBLE",
                "Plugin package publisher does not match the manifest publisher",
                category="market",
            )
    declared_version = str(package.get("version") or "")
    if declared_version and declared_version != manifest.version:
        raise PluginError(
            "PLUGIN_INCOMPATIBLE",
            "Plugin package version does not match the manifest version",
            category="market",
        )
    if (str(package.get("id") or "").startswith(f"{OFFICIAL_SOURCE_KEY}::")
            and manifest.publisher_id != OFFICIAL_PUBLISHER_ID):
        raise PluginError(
            "PLUGIN_INCOMPATIBLE",
            "Only the official publisher may publish into the official package namespace",
            category="market",
        )
    signature = package.get("manifest_signature")
    if signature is not None:
        if not isinstance(signature, dict):
            raise PluginError(
                "PLUGIN_INCOMPATIBLE",
                "Plugin package manifest signature is malformed",
                category="market",
            )
        signature_key_id = str(signature.get("key_id") or "")
        if signature_key_id:
            for artifact in manifest.artifacts:
                artifact_key_id = str((artifact.get("signature") or {}).get("key_id") or "")
                if artifact_key_id and artifact_key_id != signature_key_id:
                    raise PluginError(
                        "PLUGIN_INCOMPATIBLE",
                        "Plugin package manifest signature does not match its artifact signature",
                        category="market",
                    )


def candidates_from_packages(
    packages: Iterable[dict], *, os_name: str, arch: str, core_version: str = "0.1.0"
) -> list[PluginCandidate]:
    candidates: list[PluginCandidate] = []
    for package in packages:
        if package.get("package_type") != PLUGIN_PACKAGE_TYPE:
            continue
        manifest = validate_manifest(package.get("plugin_manifest"), core_version=core_version)
        _assert_package_manifest_consistency(package, manifest)
        artifacts = [item for item in manifest.artifacts if item["os"] == os_name and item["arch"] == arch]
        if not artifacts:
            continue
        for artifact in artifacts:
            source = package.get("market_source") or {}
            candidates.append(PluginCandidate(
                str(package.get("id") or ""), str(source.get("source_key") or ""), manifest,
                artifact, _artifact_reference(package, artifact["sha256"]),
                _dependency_references(package, manifest),
                dict(package["manifest_signature"]) if isinstance(package.get("manifest_signature"), dict) else None,
            ))
    return candidates


def _dependency_references(package: dict, manifest: PluginManifest) -> dict[str, Path]:
    if manifest.runtime.get("type") != "python":
        return {}
    references = package.get("dependency_references") or []
    if not isinstance(references, list):
        raise PluginError("DEPENDENCY_LOCK_INVALID", "Dependency references are invalid", category="dependency")
    result = {}
    for item in select_dependency_artifacts(manifest.runtime["dependency_lock"]):
        matches = [ref for ref in references if isinstance(ref, dict) and ref.get("sha256") == item["sha256"]]
        if len(matches) == 1 and set(matches[0]) == {"sha256", "local_path"}:
            result[item["sha256"]] = Path(str(matches[0]["local_path"]))
    return result


def select_candidate(candidates: Iterable[PluginCandidate], identity: str) -> PluginCandidate:
    matching = [candidate for candidate in candidates if candidate.identity == identity]
    if not matching:
        raise PluginError("RESOURCE_NOT_FOUND", "No compatible plugin candidate is available", category="market")
    by_version: dict[str, list[PluginCandidate]] = {}
    for candidate in matching:
        by_version.setdefault(candidate.manifest.version, []).append(candidate)
    version = max(by_version, key=_version_tuple)
    selected = by_version[version]
    digests = {candidate.artifact["sha256"] for candidate in selected}
    if len(digests) != 1:
        raise PluginError("PLUGIN_INCOMPATIBLE", "Conflicting plugin artifacts were published for the same version", category="market")
    selected.sort(key=lambda value: (value.source_key, value.package_id, str(value.local_reference)))
    return selected[0]


def evaluate_dependency(
    requirement: dict[str, Any], installation: dict | None, runtime: PluginRuntime | None
) -> dict[str, Any]:
    identity = str(requirement.get("plugin") or "")
    base = {"plugin": identity, "status": "dependency_missing"}
    if not installation:
        return base
    version = str(installation.get("active_version") or installation.get("installed_version") or "")
    expression = str(requirement.get("version_range") or "")
    try:
        version_ok = _range_allows(version, expression)
    except ValueError:
        version_ok = False
    try:
        manifest = validate_manifest(json.loads(installation["manifest_json"]))
    except (KeyError, TypeError, json.JSONDecodeError, PluginError):
        return {**base, "status": "plugin_incompatible"}
    # The installation row is addressed by publisher/plugin_id, but nothing else
    # guarantees the persisted manifest actually declares that identity.  A row
    # whose manifest identity disagrees must not satisfy a dependency on the
    # address that found it.
    if manifest.identity != identity:
        return {**base, "status": "plugin_incompatible", "manifest_identity": manifest.identity}
    contract = str(requirement.get("contract") or "")
    required_schemes = {str(value) for value in requirement.get("required_schemes", [])}
    contracts = {item.contract for item in manifest.provider_contracts}
    schemes = {scheme for scheme, owned_contract in manifest.owned_schemes if owned_contract == contract}
    if not version_ok or contract not in contracts or not required_schemes.issubset(schemes):
        return {**base, "status": "plugin_incompatible", "version": version}
    if not installation.get("enabled") or installation.get("quarantined") or not runtime:
        return {**base, "status": "provider_unavailable", "version": version}
    try:
        instances = [runtime.registry.route(scheme) for scheme in sorted(required_schemes)]
    except PluginError:
        return {**base, "status": "provider_unavailable", "version": version}
    if any(instance.manifest.identity != identity for instance in instances):
        return {**base, "status": "provider_unavailable", "version": version}
    # The persisted manifest is what was installed; the runtime instance is what
    # is actually serving.  A dependency must hold against both, so the contract
    # is re-checked on the projection that will answer the request.
    if any(
        contract not in {item.contract for item in instance.manifest.provider_contracts}
        for instance in instances
    ):
        return {**base, "status": "provider_unavailable", "version": version}
    # Same asymmetry for version: the durable row already satisfied the range,
    # but the instance answering the request may still be an older one during a
    # commit that has not finished projecting.
    try:
        serving_ok = all(_range_allows(instance.manifest.version, expression) for instance in instances)
    except ValueError:
        serving_ok = False
    if not serving_ok:
        return {**base, "status": "provider_unavailable", "version": version}
    return {**base, "status": "ready", "version": version}


async def content_dependents(identity: str) -> list[str]:
    """Return installed Content Package ids whose ``requires_plugins`` names ``identity``.

    V1 is deliberately a reverse-lookup over the persisted Content install
    metadata: no new dependency schema is introduced and no distribution
    dependency is inferred from a source URL or scheme.  V1 also refuses to
    cascade — the caller decides whether to remove the dependent Content
    Package or force the uninstall.
    """
    dependents: set[str] = set()
    for install in await db.list_market_installs():
        package_id = str(install.get("package_id") or "").strip()
        if not package_id:
            continue
        try:
            metadata = json.loads(install.get("metadata_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        requirements = metadata.get("requires_plugins") if isinstance(metadata, dict) else None
        if not isinstance(requirements, list):
            continue
        if any(
            isinstance(item, dict) and str(item.get("plugin") or "") == identity
            for item in requirements
        ):
            dependents.add(package_id)
    return sorted(dependents)


async def assert_no_content_dependents(identity: str) -> None:
    dependents = await content_dependents(identity)
    if dependents:
        raise PluginError(
            "PLUGIN_DEPENDENCY_ACTIVE",
            "Installed Content packages still require this Plugin",
            category="dependency",
            details={"plugin": identity, "dependents": dependents},
        )


class PluginMarketService:
    def __init__(
        self,
        *,
        runtime: PluginRuntime,
        store: PluginArtifactStore,
        trust_policy: FixtureTrustPolicy,
        command_factory: Callable[[PluginManifest, Path], Sequence[str]] | None = None,
        os_name: str | None = None,
        arch: str | None = None,
        python_environments: PythonEnvironmentManager | None = None,
        python_executable: str | Path | None = None,
        dependency_fetcher: Callable[[dict[str, Any], Path], Any] | None = None,
        runtime_command_factory: Callable[[PluginManifest, Path, Any | None], Sequence[str]] | None = None,
    ):
        self.runtime = runtime
        self.store = store
        self.trust_policy = trust_policy
        self.os_name, self.arch = (os_name, arch) if os_name and arch else current_platform()
        self.python_environments = python_environments or PythonEnvironmentManager(
            self.store.root / "python", python_executable=python_executable or sys.executable,
        )
        self.python_executable = str(Path(
            python_executable or self.python_environments.python_executable,
        ).resolve())
        self.command_factory = command_factory or self._default_command
        self.dependency_fetcher = dependency_fetcher
        self.runtime_command_factory = runtime_command_factory
        self._active: dict[str, PluginInstance] = {}
        self._locks: dict[str, LifecycleLock] = {}
        self._preparation_locks: dict[str, asyncio.Lock] = {}
        self._critical_tasks: set[asyncio.Task[Any]] = set()
        self._activation_expectations: dict[str, str] = {}
        self._shutdown_event = asyncio.Event()
        self.destructive_guard: Callable[[str], Awaitable[None]] | None = None
        self.lifecycle_changed: Callable[[str], Awaitable[None]] | None = None
        self.workspaces_root = self.store.root / "workspaces"
        self.workspaces_root.mkdir(parents=True, exist_ok=True)

    def lifecycle_lock(self, identity: str) -> LifecycleLock:
        """Return the process-wide lifecycle lock for one canonical Plugin."""
        return self._locks.setdefault(str(identity), LifecycleLock())

    async def _notify_lifecycle_changed(self, identity: str) -> None:
        if self.lifecycle_changed is None or self._shutdown_event.is_set():
            return
        task = self._track_critical(
            asyncio.create_task(self.lifecycle_changed(identity), name=f"plugin-domain-reconcile:{identity}"),
            operation="domain_reconciliation", identity=identity,
        )
        await self._await_critical(task)

    def preparation_lock(self, identity: str) -> asyncio.Lock:
        """Serialize filesystem preparation/removal without blocking lifecycle state changes."""
        return self._preparation_locks.setdefault(str(identity), asyncio.Lock())

    def _track_critical(
        self, task: asyncio.Task[Any], *, operation: str, identity: str,
    ) -> asyncio.Task[Any]:
        self._critical_tasks.add(task)

        def done(value: asyncio.Task[Any]) -> None:
            self._critical_tasks.discard(value)
            if value.cancelled():
                return
            error = value.exception()
            if error is not None and getattr(value, "_waveflow_log_failure", False):
                logger.error(
                    "Plugin critical lifecycle task failed: operation=%s plugin=%s "
                    "error_type=%s error_code=%s reconciliation_scheduled=false",
                    operation,
                    identity,
                    type(error).__name__,
                    getattr(error, "code", "PLUGIN_LIFECYCLE_FAILED"),
                )

        task.add_done_callback(done)
        return task

    @staticmethod
    async def _await_critical(task: asyncio.Task[Any]) -> Any:
        """Wait for an independent lifecycle mutation before propagating cancellation."""
        async def settle() -> None:
            try:
                await task
            except BaseException:
                return

        settled = asyncio.create_task(settle(), name=f"settle:{task.get_name()}")
        cancelled = False
        while not settled.done():
            try:
                await asyncio.shield(settled)
            except asyncio.CancelledError:
                cancelled = True
                setattr(task, "_waveflow_log_failure", True)
                current = asyncio.current_task()
                if current is not None:
                    current.uncancel()
        if cancelled:
            raise asyncio.CancelledError
        return task.result()

    def begin_shutdown(self) -> None:
        """Stop lifecycle work while preserving committed durable intent."""
        if self._shutdown_event.is_set():
            return
        self._shutdown_event.set()
        self.cancel_critical_tasks()

    def cancel_critical_tasks(self) -> None:
        for task in tuple(self._critical_tasks):
            if not task.done():
                task.cancel()

    def _ensure_not_shutting_down(self) -> None:
        if self._shutdown_event.is_set():
            raise PluginError(
                "PLUGIN_UNAVAILABLE",
                "Plugin lifecycle service is shutting down",
                category="lifecycle",
            )

    async def wait_for_critical_tasks(self) -> None:
        while self._critical_tasks:
            await asyncio.gather(
                *(asyncio.shield(task) for task in tuple(self._critical_tasks)),
                return_exceptions=True,
            )

    @contextmanager
    def expect_activation(self, instance: PluginInstance, operation: str) -> Iterator[None]:
        if instance.instance_id in self._activation_expectations:
            raise PluginError(
                "PLUGIN_UNAVAILABLE", "Plugin activation is already in progress", category="lifecycle",
            )
        self._activation_expectations[instance.instance_id] = operation
        try:
            yield
        finally:
            self._activation_expectations.pop(instance.instance_id, None)

    def activation_expectation(self, instance: PluginInstance) -> str:
        return self._activation_expectations.get(instance.instance_id, "")

    def _working_directory(self, manifest: PluginManifest, *, create: bool = True) -> Path:
        path = self.workspaces_root / self.store._part(manifest.publisher_id) / self.store._part(manifest.plugin_id)
        if create:
            path.mkdir(parents=True, exist_ok=True)
        return path

    def _runtime_command(self, manifest: PluginManifest, artifact: Path, environment: Any | None) -> Sequence[str]:
        if self.runtime_command_factory:
            return self.runtime_command_factory(manifest, artifact, environment)
        if environment:
            return (str(environment.python), "-I", "-B", str(artifact), "--identity", manifest.identity,
                    "--version", manifest.version)
        return self.command_factory(manifest, artifact)

    def _default_command(self, manifest: PluginManifest, artifact: Path) -> Sequence[str]:
        selected = next(item for item in manifest.artifacts if item["sha256"] == artifact.parent.name)
        if selected["runtime"] == "python":
            return (self.python_executable, "-B", str(artifact), "--identity", manifest.identity,
                    "--version", manifest.version)
        # SDK artifacts are ordinary zip archives rather than executable
        # files.  A subprocess-runtime Plugin may still be dependency-free,
        # but its .pyz must be launched by a real interpreter; this is also
        # what lets a frozen Desktop backend run the same artifact without
        # trying to execute it with the PyInstaller binary.
        if selected["runtime"] == "subprocess" and artifact.suffix == ".pyz":
            return (self.python_executable, "-I", "-B", str(artifact), "--identity", manifest.identity,
                    "--version", manifest.version)
        return (str(artifact),)

    async def _trusted_candidate_from_packages(
        self, packages: Iterable[dict], identity: str,
    ) -> PluginCandidate:
        packages = list(packages)
        candidates = candidates_from_packages(packages, os_name=self.os_name, arch=self.arch)
        matching = [candidate for candidate in candidates if candidate.identity == identity]
        if not matching:
            declared = []
            for package in packages:
                if package.get("package_type") != PLUGIN_PACKAGE_TYPE:
                    continue
                try:
                    manifest = validate_manifest(package.get("plugin_manifest"))
                except PluginError:
                    continue
                if manifest.identity == identity:
                    declared.append(manifest)
            if declared:
                raise PluginError("PLATFORM_UNSUPPORTED", "Plugin has no artifact for this platform", category="compatibility")
            raise PluginError("RESOURCE_NOT_FOUND", "No compatible plugin candidate is available", category="market")
        highest = max((candidate.manifest.version for candidate in matching), key=_version_tuple)
        version_candidates = [candidate for candidate in matching if candidate.manifest.version == highest]
        digests = {candidate.artifact["sha256"] for candidate in version_candidates}
        if len(digests) != 1:
            raise PluginError("PLUGIN_INCOMPATIBLE", "Conflicting plugin artifacts were published for the same version", category="market")
        trusted: list[PluginCandidate] = []
        trust_errors: list[PluginError] = []
        for candidate in version_candidates:
            try:
                payload = _read_artifact(
                    self.store._source(candidate.local_reference),
                    max_bytes=int(candidate.artifact["size_bytes"]),
                )
                if hashlib.sha256(payload).hexdigest() != candidate.artifact["sha256"]:
                    raise PluginError("INVALID_PLUGIN_RESPONSE", "Plugin artifact digest or size mismatch", category="artifact")
                self.trust_policy.verify_manifest(
                    candidate.manifest, candidate.artifact, candidate.manifest_signature,
                )
                self.trust_policy.verify(candidate.manifest, candidate.artifact, payload)
            except PluginError as error:
                trust_errors.append(error)
            else:
                trusted.append(candidate)
        if not trusted:
            raise trust_errors[0]
        manifests = {_manifest_sha256(candidate.manifest) for candidate in trusted}
        if len(manifests) != 1:
            raise PluginError("PLUGIN_INCOMPATIBLE", "Conflicting trusted plugin manifests were published for the same version", category="market")
        return select_candidate(trusted, identity)

    async def _developer_candidate_from_packages(
        self, packages: Iterable[dict], identity: str,
    ) -> PluginCandidate:
        packages = list(packages)
        if any(package.get("_developer_local") is not True for package in packages):
            raise PluginError(
                "CAPABILITY_DENIED",
                "Unsigned Plugin installation requires an explicit local Developer package",
                category="trust",
            )
        candidates = candidates_from_packages(packages, os_name=self.os_name, arch=self.arch)
        matching = [
            candidate for candidate in candidates
            if candidate.identity == identity and candidate.source_key == DEVELOPER_LOCAL_SOURCE_KEY
        ]
        if not matching:
            raise PluginError("RESOURCE_NOT_FOUND", "No compatible local Developer package is available", category="market")
        highest = max((candidate.manifest.version for candidate in matching), key=_version_tuple)
        version_candidates = [candidate for candidate in matching if candidate.manifest.version == highest]
        digests = {candidate.artifact["sha256"] for candidate in version_candidates}
        if len(digests) != 1:
            raise PluginError(
                "PLUGIN_INCOMPATIBLE",
                "Conflicting local Developer artifacts were supplied for the same version",
                category="market",
            )
        policy = DeveloperLocalTrustPolicy()
        trusted: list[PluginCandidate] = []
        for candidate in version_candidates:
            payload = _read_artifact(
                self.store._source(candidate.local_reference),
                max_bytes=MAX_DEVELOPER_ARTIFACT_BYTES,
            )
            if len(payload) != int(candidate.artifact["size_bytes"]):
                raise PluginError("ARTIFACT_INTEGRITY_FAILED", "Developer artifact size mismatch", category="artifact")
            if hashlib.sha256(payload).hexdigest() != candidate.artifact["sha256"]:
                raise PluginError("ARTIFACT_INTEGRITY_FAILED", "Developer artifact digest mismatch", category="artifact")
            policy.verify_manifest(candidate.manifest, candidate.artifact, candidate.manifest_signature)
            policy.verify(candidate.manifest, candidate.artifact, payload)
            trusted.append(candidate)
        return select_candidate(trusted, identity)

    async def install_from_packages(self, packages: Iterable[dict], identity: str) -> dict:
        self._ensure_not_shutting_down()
        candidate = await self._trusted_candidate_from_packages(packages, identity)
        await require_high_risk_approvals(candidate.manifest)
        async with self.preparation_lock(identity):
            prepared = await self._prepare_candidate(candidate)
            task = self._track_critical(
                asyncio.create_task(
                    self._activate_prepared(prepared),
                    name=f"plugin-activation:{identity}:{candidate.manifest.version}",
                ),
                operation="candidate_activation",
                identity=identity,
            )
            return await self._await_critical(task)

    async def install_developer_local(self, packages: Iterable[dict], identity: str) -> dict:
        """Install a user-selected local package through the normal lifecycle."""
        self._ensure_not_shutting_down()
        candidate = await self._developer_candidate_from_packages(packages, identity)
        existing = await db.get_plugin_installation(
            candidate.manifest.publisher_id, candidate.manifest.plugin_id,
        )
        if existing and str(existing.get("trust_class") or "official") != DEVELOPER_LOCAL_TRUST_CLASS:
            raise PluginError(
                "PLUGIN_UNTRUSTED",
                "An Official Plugin cannot be replaced by an unsigned local package; uninstall it explicitly first",
                category="trust",
            )
        await require_high_risk_approvals(candidate.manifest)
        async with self.preparation_lock(identity):
            prepared = await self._prepare_candidate(candidate, trust_policy=DeveloperLocalTrustPolicy())
            task = self._track_critical(
                asyncio.create_task(
                    self._activate_prepared(prepared),
                    name=f"plugin-developer-activation:{identity}:{candidate.manifest.version}",
                ),
                operation="developer_local_activation",
                identity=identity,
            )
            return await self._await_critical(task)

    async def repair_from_packages(self, packages: Iterable[dict], identity: str) -> dict:
        """Repair a damaged installation from an exact trusted artifact.

        Repair is deliberately not an update path: the candidate must match
        the durable active version, artifact digest, and manifest digest.  It
        can therefore restore a missing/corrupt file without changing
        ownership or silently moving the installation to a newer release.
        """
        self._ensure_not_shutting_down()
        candidate = await self._trusted_candidate_from_packages(packages, identity)
        await require_high_risk_approvals(candidate.manifest)
        async with self.preparation_lock(identity):
            row = await db.get_plugin_installation(
                candidate.manifest.publisher_id, candidate.manifest.plugin_id,
            )
            if not row or not row.get("active_version"):
                raise PluginError(
                    "PLUGIN_UNAVAILABLE", "Plugin installation has no active version to repair", category="lifecycle",
                )
            if not row.get("enabled"):
                raise PluginError(
                    "PLUGIN_UNAVAILABLE", "Disabled Plugin installation cannot be repaired", category="lifecycle",
                )
            if (
                str(row.get("active_version") or "") != candidate.manifest.version
                or str(row.get("artifact_sha256") or "") != str(candidate.artifact["sha256"])
                or str(row.get("manifest_sha256") or "") != _manifest_sha256(candidate.manifest)
            ):
                raise PluginError(
                    "PLUGIN_INCOMPATIBLE",
                    "Trusted repair candidate does not match installed immutable package metadata",
                    category="market",
                )
            await db.set_plugin_enabled(
                candidate.manifest.publisher_id, candidate.manifest.plugin_id, True,
                lifecycle_state="unavailable", error="Trusted Plugin artifact repair in progress",
            )
            prepared = await self._prepare_candidate(candidate)
            task = self._track_critical(
                asyncio.create_task(
                    self._activate_prepared(prepared, repair=True),
                    name=f"plugin-repair:{identity}:{candidate.manifest.version}",
                ),
                operation="artifact_repair",
                identity=identity,
            )
            return await self._await_critical(task)

    def installed_artifact_valid(self, row: dict[str, Any]) -> bool:
        """Validate the durable artifact and persisted trust metadata only.

        Runtime health is checked by the normal recovery path.  This helper is
        used by bundled bootstrap to distinguish a damaged artifact, which is
        eligible for exact trusted repair, from an unrelated runtime failure.
        """
        try:
            manifest = validate_manifest(json.loads(row.get("manifest_json") or "{}"))
            if manifest.identity != f"{row.get('publisher_id')}/{row.get('plugin_id')}" \
                    or manifest.version != str(row.get("active_version") or ""):
                return False
            selected = next(
                item for item in manifest.artifacts
                if item["sha256"] == str(row.get("artifact_sha256") or "")
            )
            artifact = Path(str(row.get("artifact_path") or ""))
            if not artifact.is_file() or artifact.stat().st_size != int(selected["size_bytes"]):
                return False
            payload = _read_artifact(artifact, max_bytes=int(selected["size_bytes"]))
            if hashlib.sha256(payload).hexdigest() != str(row.get("artifact_sha256") or ""):
                return False
            try:
                signature = json.loads(row.get("manifest_signature_json") or "{}")
            except json.JSONDecodeError:
                return False
            if str(row.get("trust_class") or "official") == DEVELOPER_LOCAL_TRUST_CLASS:
                developer_policy = DeveloperLocalTrustPolicy()
                developer_policy.verify_manifest(manifest, selected, signature or None)
                developer_policy.verify(manifest, selected, payload)
            else:
                self.trust_policy.verify_manifest(manifest, selected, signature or None)
                self.trust_policy.verify(manifest, selected, payload)
            if not row.get("trust_state") or not row.get("source_key"):
                return False
            return True
        except (OSError, PluginError, StopIteration, TypeError, ValueError, json.JSONDecodeError):
            return False

    def installation_healthy(self, row: dict[str, Any]) -> bool:
        identity = f"{row.get('publisher_id')}/{row.get('plugin_id')}"
        instance = self._active.get(identity)
        return bool(
            row.get("enabled")
            and row.get("lifecycle_state") == "active"
            and instance is not None
            and getattr(instance, "health", "") == "healthy"
            and getattr(getattr(instance, "state", None), "value", "") == LifecycleState.HEALTHY_ACTIVE.value
            and self.installed_artifact_valid(row)
        )

    async def _activate_prepared(self, prepared: PreparedPluginCandidate, *, repair: bool = False) -> dict:
        candidate = prepared.candidate
        try:
            async with self.lifecycle_lock(candidate.identity):
                # Preparation is intentionally outside the lifecycle lock.  The
                # lock-local check is authoritative and the Runtime activation
                # guard re-enters this same boundary immediately before spawn.
                await require_high_risk_approvals(candidate.manifest)
                return await self._activate(prepared, repair=repair)
        except BaseException:
            await self._discard_prepared(prepared)
            raise

    async def _prepare_candidate(
        self, candidate: PluginCandidate, *, trust_policy: Any | None = None,
    ) -> PreparedPluginCandidate:
        staged: Path | None = None
        environment = None
        try:
            staged, trust_state = await asyncio.to_thread(
                self.store.stage, candidate, trust_policy or self.trust_policy,
            )
            if candidate.manifest.runtime.get("type") == "python":
                safe_references = {
                    digest: self.store._source(path)
                    for digest, path in candidate.dependency_references.items()
                }
                environment = await self.python_environments.prepare(
                    candidate.manifest, safe_references, fetch=self.dependency_fetcher,
                )
            return PreparedPluginCandidate(candidate, staged, trust_state, environment)
        except BaseException:
            if staged:
                await asyncio.to_thread(self.store.remove_path, staged)
            raise

    async def _discard_prepared(self, prepared: PreparedPluginCandidate) -> None:
        if prepared.staged_artifact.exists():
            await asyncio.to_thread(self.store.remove_path, prepared.staged_artifact)
        if prepared.environment:
            candidate = prepared.candidate
            row = await db.get_plugin_installation(
                candidate.manifest.publisher_id, candidate.manifest.plugin_id,
            )
            # Never remove an environment reused by the durable active version.
            # A lock-losing/revoked candidate has no durable reference and can
            # be discarded immediately.
            if not row or str(row.get("active_version") or "") != candidate.manifest.version:
                await asyncio.to_thread(
                    self.python_environments.remove_environment, prepared.environment.path,
                )

    async def _remove_artifact_if_unreferenced(self, path: str | Path) -> bool:
        resolved = str(Path(path).resolve())
        live = {str(Path(item).resolve()) for item in await db.list_plugin_artifact_references()}
        if resolved in live:
            return False
        await asyncio.to_thread(self.store.remove_path, path)
        return True

    async def _activate(self, prepared: PreparedPluginCandidate, *, repair: bool = False) -> dict:
        candidate = prepared.candidate
        identity = candidate.identity
        existing = await db.get_plugin_installation(candidate.manifest.publisher_id, candidate.manifest.plugin_id)
        if existing and existing.get("active_version") == candidate.manifest.version:
            if (existing.get("artifact_sha256") == candidate.artifact["sha256"]
                    and existing.get("manifest_sha256") == _manifest_sha256(candidate.manifest)):
                if not repair:
                    await self._discard_prepared(prepared)
                    return self._public(existing)
            else:
                raise PluginError(
                    "PLUGIN_INCOMPATIBLE",
                    "Installed plugin version conflicts with different immutable package metadata",
                    category="market",
                )
        if existing and existing.get("active_version"):
            try:
                if _version_tuple(candidate.manifest.version) < _version_tuple(existing["active_version"]):
                    raise PluginError("PLUGIN_INCOMPATIBLE", "Plugin downgrade is not supported", category="market")
            except ValueError as exc:
                raise PluginError("PLUGIN_INCOMPATIBLE", "Installed plugin version is invalid", category="persistence") from exc
        old = self._active.get(identity)
        staged: Path | None = prepared.staged_artifact
        promoted: Path | None = None
        instance: PluginInstance | None = None
        durably_committed = False
        environment = prepared.environment
        environment_reused = False
        if repair and environment and existing and existing.get("active_version") == candidate.manifest.version:
            environment_reused = any(
                item.get("state") == "active"
                and str(item.get("path") or "") == str(environment.path)
                and str(item.get("lock_digest") or "") == str(environment.lock_digest)
                for item in await db.list_plugin_python_environments(
                    candidate.manifest.publisher_id, candidate.manifest.plugin_id,
                )
            )
        try:
            promoted = await asyncio.to_thread(self.store.promote, candidate, staged)
            if environment and not environment_reused:
                cache_by_digest = {path.parent.name: path for path in self.python_environments.cache_objects()}
                await db.begin_plugin_python_environment(
                    publisher_id=candidate.manifest.publisher_id, plugin_id=candidate.manifest.plugin_id,
                    plugin_version=candidate.manifest.version, runtime_identity=environment.runtime_identity,
                    lock_digest=environment.lock_digest, path=str(environment.path),
                    dependencies=[{**item, "path": str(cache_by_digest[item["sha256"]])}
                                  for item in environment.dependencies],
                )
            manifest_json = _manifest_json(candidate.manifest)
            manifest_signature_json = json.dumps(
                candidate.manifest_signature or {}, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
            )
            await db.begin_plugin_candidate(
                publisher_id=candidate.manifest.publisher_id, plugin_id=candidate.manifest.plugin_id,
                version=candidate.manifest.version, trust_state=prepared.trust_state,
                source_key=candidate.source_key, source_package_id=candidate.package_id,
                manifest_json=manifest_json, manifest_sha256=_manifest_sha256(candidate.manifest),
                manifest_signature_json=manifest_signature_json,
                artifact_sha256=candidate.artifact["sha256"], artifact_path=str(promoted),
                runtime_type=candidate.artifact["runtime"], entrypoint=candidate.artifact["entrypoint"],
                platform_os=self.os_name, platform_arch=self.arch,
                trust_class=DEVELOPER_LOCAL_TRUST_CLASS
                if prepared.trust_state == DEVELOPER_LOCAL_TRUST_CLASS else "official",
            )
            command = self._runtime_command(candidate.manifest, promoted, environment)
            instance = self.runtime.install(candidate.manifest, command,
                                            working_directory=str(self._working_directory(candidate.manifest)))
            async def persist_activation() -> None:
                nonlocal durably_committed
                try:
                    activated = await db.activate_plugin_candidate(
                        publisher_id=candidate.manifest.publisher_id, plugin_id=candidate.manifest.plugin_id,
                        candidate_version=candidate.manifest.version, trust_state=prepared.trust_state,
                        source_key=candidate.source_key, source_package_id=candidate.package_id,
                        manifest_json=manifest_json, manifest_sha256=_manifest_sha256(candidate.manifest),
                        manifest_signature_json=manifest_signature_json,
                        artifact_sha256=candidate.artifact["sha256"], artifact_path=str(promoted),
                        runtime_type=candidate.artifact["runtime"], entrypoint=candidate.artifact["entrypoint"],
                        platform_os=self.os_name, platform_arch=self.arch,
                        environment={"runtime_identity": environment.runtime_identity,
                                     "lock_digest": environment.lock_digest}
                        if environment and not environment_reused else None,
                        trust_class=DEVELOPER_LOCAL_TRUST_CLASS
                        if prepared.trust_state == DEVELOPER_LOCAL_TRUST_CLASS else "official",
                    )
                except BaseException:
                    # A repository call may commit and then fail to return.  The
                    # durable row, never the exception timing, defines whether
                    # pre-commit cleanup is still legal.
                    durable = await db.get_plugin_installation(
                        candidate.manifest.publisher_id, candidate.manifest.plugin_id,
                    )
                    if not self._is_committed_candidate(durable, candidate, promoted):
                        raise
                    activated = True
                if not activated:
                    raise PluginError("PLUGIN_UNAVAILABLE", "Plugin activation state changed concurrently", category="persistence")
                durably_committed = True

            with self.expect_activation(instance, "candidate_activation"):
                if old:
                    await self.runtime.activate_candidate(old, instance, commit=persist_activation)
                else:
                    await self.runtime.enable(
                        instance, activation_operation="candidate_activation",
                    )
                    await persist_activation()
            await self._project_committed_activation(identity, instance)
        except BaseException as error:
            if durably_committed:
                # POST-COMMIT: the candidate is durable desired state.  Keep its
                # artifact/environment and converge live projection forward;
                # never run candidate rollback/cleanup.
                await self._reconcile_committed_activation(identity, instance, old)
                return self._public(await db.get_plugin_installation(
                    candidate.manifest.publisher_id, candidate.manifest.plugin_id
                ))
            # PRE-COMMIT: no durable active row references candidate resources,
            # so rollback and cleanup are both permitted.
            await db.fail_plugin_candidate(
                candidate.manifest.publisher_id, candidate.manifest.plugin_id, candidate.manifest.version,
                _safe_error(error), preserve_unavailable=repair,
            )
            if environment and not environment_reused:
                await db.fail_plugin_python_environment(
                    candidate.manifest.publisher_id, candidate.manifest.plugin_id, candidate.manifest.version,
                    environment.runtime_identity, environment.lock_digest,
                )
                await asyncio.to_thread(self.python_environments.remove_environment, environment.path)
            if instance and instance is not old and instance.instance_id in self.runtime.registry.instances:
                try:
                    await self.runtime.uninstall(instance)
                except PluginError:
                    if instance.process:
                        await instance.process.stop(graceful=False)
            if promoted:
                await self._remove_artifact_if_unreferenced(promoted)
            elif staged:
                await asyncio.to_thread(self.store.remove_path, staged)
            raise
        if old and old.state == LifecycleState.INSTALLED_DISABLED:
            try:
                await self.runtime.uninstall(old)
            except PluginError:
                pass
        if existing and existing.get("artifact_path") and existing["artifact_path"] != str(promoted):
            try:
                # Drop the retained DB reference first; the guarded removal
                # then cannot race a durable reference out of existence.  If
                # either step fails, the unreferenced file is safe to collect
                # during a later recovery pass.
                await db.delete_retained_plugin_artifacts(candidate.manifest.publisher_id, candidate.manifest.plugin_id)
                await self._remove_artifact_if_unreferenced(existing["artifact_path"])
            except (OSError, RuntimeError):
                pass
        return self._public(await db.get_plugin_installation(candidate.manifest.publisher_id, candidate.manifest.plugin_id))

    @staticmethod
    def _is_committed_candidate(
        row: dict[str, Any] | None, candidate: PluginCandidate, artifact: Path | None,
    ) -> bool:
        return bool(
            row
            and str(row.get("active_version") or "") == candidate.manifest.version
            and str(row.get("artifact_sha256") or "") == str(candidate.artifact["sha256"])
            and artifact is not None
            and str(row.get("artifact_path") or "") == str(artifact)
            and str(row.get("manifest_sha256") or "") == _manifest_sha256(candidate.manifest)
        )

    async def _project_committed_activation(
        self, identity: str, instance: PluginInstance,
    ) -> None:
        self._active[identity] = instance
        await self._notify_lifecycle_changed(identity)

    async def _reconcile_committed_activation(
        self, identity: str, instance: PluginInstance | None, old: PluginInstance | None,
    ) -> None:
        """Converge Runtime/service projection to an already committed version."""
        delay = 0.0
        while not self._shutdown_event.is_set():
            try:
                if instance is None:
                    raise PluginError(
                        "PLUGIN_UNAVAILABLE", "Committed Plugin instance is missing", category="lifecycle",
                    )
                row = await db.get_plugin_installation(
                    instance.manifest.publisher_id, instance.manifest.plugin_id,
                )
                if (
                    not row
                    or str(row.get("active_version") or "") != instance.manifest.version
                    or not row.get("enabled")
                ):
                    raise PluginError(
                        "PLUGIN_CANDIDATE_CONFLICT", "Committed Plugin state changed", category="lifecycle",
                    )

                routed = True
                for scheme, _contract in instance.manifest.owned_schemes:
                    try:
                        routed_instance = self.runtime.registry.route(scheme)
                    except PluginError:
                        routed = False
                        break
                    if routed_instance is not instance:
                        routed = False
                        break
                if not routed:
                    if old is not None and old is not instance and old.state in {
                        LifecycleState.HEALTHY_ACTIVE, LifecycleState.UNHEALTHY,
                    }:
                        await self.runtime.disable(old)
                    if instance.state == LifecycleState.HEALTHY_ACTIVE:
                        await self.runtime.disable(instance)
                    with self.expect_activation(instance, "candidate_activation"):
                        await self.runtime.enable(
                            instance, activation_operation="candidate_activation",
                        )
                await self._project_committed_activation(identity, instance)
                return
            except asyncio.CancelledError:
                raise
            except Exception as error:
                if self._shutdown_event.is_set():
                    raise asyncio.CancelledError
                logger.warning(
                    "Committed Plugin activation reconciliation will retry: plugin=%s error_type=%s error_code=%s",
                    identity,
                    type(error).__name__,
                    getattr(error, "code", "PLUGIN_RECONCILIATION_FAILED"),
                )
                delay = min(0.5, max(0.01, delay * 2 or 0.01))
                try:
                    await asyncio.wait_for(self._shutdown_event.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
        raise asyncio.CancelledError

    async def disable(self, identity: str) -> dict:
        async with self.lifecycle_lock(identity):
            if self.destructive_guard is not None:
                await self.destructive_guard(identity)
            return await self._disable_unlocked(identity)

    async def _disable_unlocked(self, identity: str) -> dict:
        row = await self._row(identity)
        instance = self._active.pop(identity, None)
        if instance:
            await self.runtime.disable(instance)
        await db.set_plugin_enabled(row["publisher_id"], row["plugin_id"], False, lifecycle_state="disabled")
        await self._notify_lifecycle_changed(identity)
        return self._public(await db.get_plugin_installation(row["publisher_id"], row["plugin_id"]))

    async def enable(self, identity: str) -> dict:
        async with self.lifecycle_lock(identity):
            return await self._enable_unlocked(identity)

    async def _enable_unlocked(self, identity: str) -> dict:
        row = await self._row(identity)
        if row.get("quarantined"):
            raise PluginError("PLUGIN_QUARANTINED", "Plugin requires explicit recovery", category="lifecycle")
        if identity in self._active:
            if self.installed_artifact_valid(row):
                await self._notify_lifecycle_changed(identity)
                return self._public(row)
            instance = self._active.pop(identity)

            async def fail_closed() -> None:
                await db.set_plugin_enabled(
                    row["publisher_id"], row["plugin_id"], True,
                    lifecycle_state="unavailable", error="Installed artifact integrity check failed",
                )
                await self.runtime.disable(instance)
                await self._notify_lifecycle_changed(identity)

            task = self._track_critical(
                asyncio.create_task(
                    fail_closed(), name=f"plugin-artifact-invalidation:{identity}",
                ),
                operation="artifact_invalidation",
                identity=identity,
            )
            await self._await_critical(task)
            raise PluginError(
                "PLUGIN_UNAVAILABLE",
                "Installed plugin artifact failed integrity verification",
                category="artifact",
            )
        manifest = validate_manifest(json.loads(row["manifest_json"]))
        await require_high_risk_approvals(manifest)
        artifact = Path(row["artifact_path"])
        selected = next(
            (item for item in manifest.artifacts if item["sha256"] == row["artifact_sha256"]),
            None,
        )
        if selected is None:
            await db.set_plugin_enabled(
                row["publisher_id"], row["plugin_id"], True,
                lifecycle_state="unavailable", error="Installed artifact integrity check failed",
            )
            raise PluginError("ARTIFACT_INVALID", "Installed artifact is absent from the manifest", category="artifact")
        if not artifact.is_file():
            await db.set_plugin_enabled(row["publisher_id"], row["plugin_id"], True, lifecycle_state="unavailable", error="Installed artifact integrity check failed")
            raise PluginError("PLUGIN_UNAVAILABLE", "Installed plugin artifact failed integrity verification", category="artifact")
        expected_size = int(selected["size_bytes"])
        try:
            actual_size = artifact.stat().st_size
        except OSError as exc:
            await db.set_plugin_enabled(row["publisher_id"], row["plugin_id"], True, lifecycle_state="unavailable", error="Installed artifact integrity check failed")
            raise PluginError("PLUGIN_UNAVAILABLE", "Installed plugin artifact failed integrity verification", category="artifact") from exc
        if actual_size != expected_size:
            await db.set_plugin_enabled(row["publisher_id"], row["plugin_id"], True, lifecycle_state="unavailable", error="Installed artifact integrity check failed")
            raise PluginError("ARTIFACT_INTEGRITY_FAILED", "Installed plugin artifact failed integrity verification", category="artifact")
        try:
            payload = await asyncio.to_thread(_read_artifact, artifact, max_bytes=expected_size)
        except (OSError, PluginError) as exc:
            await db.set_plugin_enabled(row["publisher_id"], row["plugin_id"], True, lifecycle_state="unavailable", error="Installed artifact integrity check failed")
            raise PluginError("ARTIFACT_INTEGRITY_FAILED", "Installed plugin artifact failed integrity verification", category="artifact") from exc
        if hashlib.sha256(payload).hexdigest() != row["artifact_sha256"]:
            await db.set_plugin_enabled(row["publisher_id"], row["plugin_id"], True, lifecycle_state="unavailable", error="Installed artifact integrity check failed")
            raise PluginError("ARTIFACT_INTEGRITY_FAILED", "Installed plugin artifact failed integrity verification", category="artifact")
        try:
            try:
                manifest_signature = json.loads(row.get("manifest_signature_json") or "{}")
            except json.JSONDecodeError as exc:
                raise PluginError(
                    "ARTIFACT_SIGNATURE_INVALID", "Installed Plugin manifest signature is invalid", category="trust",
                ) from exc
            if str(row.get("trust_class") or "official") == DEVELOPER_LOCAL_TRUST_CLASS:
                developer_policy = DeveloperLocalTrustPolicy()
                developer_policy.verify_manifest(manifest, selected, manifest_signature or None)
                developer_policy.verify(manifest, selected, payload)
            else:
                self.trust_policy.verify_manifest(manifest, selected, manifest_signature or None)
                self.trust_policy.verify(manifest, selected, payload)
        except PluginError as error:
            await db.set_plugin_enabled(
                row["publisher_id"], row["plugin_id"], True,
                lifecycle_state="unavailable", error=_safe_error(error),
            )
            raise
        environment = None
        if manifest.runtime.get("type") == "python":
            try:
                _lock, digest = validate_python_runtime(manifest)
                path = self.python_environments.environment_path(manifest, digest)
                try:
                    environment = self.python_environments.verify(manifest, path)
                except PluginError:
                    environment = await self.python_environments.rebuild(manifest, {})
            except PluginError as exc:
                raise PluginError("PLUGIN_ENVIRONMENT_INVALID", "Installed Plugin environment is unavailable", category="runtime") from exc
        # Manual enable and startup recovery first persist the desired enabled
        # state.  The production activation authority then validates this same
        # row immediately before spawning the process.
        await db.set_plugin_enabled(
            row["publisher_id"], row["plugin_id"], True,
            lifecycle_state="starting", error="",
        )
        instance = self.runtime.install(
            manifest, self._runtime_command(manifest, artifact, environment),
            working_directory=str(self._working_directory(manifest)))
        try:
            with self.expect_activation(instance, "active_enable"):
                await self.runtime.enable(instance, activation_operation="active_enable")
        except BaseException as error:
            await db.set_plugin_enabled(row["publisher_id"], row["plugin_id"], True, lifecycle_state="unavailable", error=_safe_error(error))
            raise
        self._active[identity] = instance
        await db.set_plugin_enabled(row["publisher_id"], row["plugin_id"], True, lifecycle_state="active")
        await self._notify_lifecycle_changed(identity)
        return self._public(await db.get_plugin_installation(row["publisher_id"], row["plugin_id"]))

    async def recover_quarantine(self, identity: str) -> dict:
        async with self.lifecycle_lock(identity):
            return await self._recover_quarantine_unlocked(identity)

    async def _recover_quarantine_unlocked(self, identity: str) -> dict:
        row = await self._row(identity)
        if not row.get("quarantined"):
            raise PluginError("PLUGIN_UNAVAILABLE", "Plugin is not quarantined", category="lifecycle")
        instance = self._active.pop(identity, None)
        if instance and instance.state == LifecycleState.QUARANTINED:
            self.runtime.registry.recover(instance)
        if not await db.recover_quarantined_plugin(row["publisher_id"], row["plugin_id"]):
            raise PluginError("PLUGIN_UNAVAILABLE", "Plugin quarantine state changed", category="persistence")
        await self._notify_lifecycle_changed(identity)
        return self._public(await db.get_plugin_installation(row["publisher_id"], row["plugin_id"]))

    async def uninstall(self, identity: str, *, force: bool = False) -> bool:
        # Filesystem preparation/removal always takes this order.  Permission
        # revoke/disable only need the lifecycle lock and therefore cannot form
        # the reverse edge of a deadlock.
        async with self.preparation_lock(identity):
            async with self.lifecycle_lock(identity):
                # Removing a Plugin does not remove the Content Packages that
                # declare it.  V1 refuses by default and reports the dependents;
                # ``force`` is the explicit operator override that leaves the
                # dependent Content Packages in place, unresolved.
                if not force:
                    await assert_no_content_dependents(identity)
                if self.destructive_guard is not None:
                    await self.destructive_guard(identity)
                return await self._uninstall_unlocked(identity)

    async def _uninstall_unlocked(self, identity: str) -> bool:
        row = await self._row(identity)
        instance = self._active.pop(identity, None)
        if instance:
            await self.runtime.uninstall(instance)
        removed = await db.delete_plugin_installation(row["publisher_id"], row["plugin_id"])
        if json.loads(row["manifest_json"]).get("runtime", {}).get("type") == "python":
            manifest = validate_manifest(json.loads(row["manifest_json"]))
            _lock, digest = validate_python_runtime(manifest)
            await asyncio.to_thread(self.python_environments.remove_environment,
                                    self.python_environments.environment_path(manifest, digest))
        for path in await db.delete_plugin_python_environments(row["publisher_id"], row["plugin_id"]):
            await asyncio.to_thread(self.python_environments.remove_environment, path)
        await asyncio.to_thread(self.store.remove_plugin, row["publisher_id"], row["plugin_id"])
        await asyncio.to_thread(shutil.rmtree,
                                self._working_directory(validate_manifest(json.loads(row["manifest_json"])), create=False), True)
        await self._notify_lifecycle_changed(identity)
        return removed

    async def recover_enabled(self) -> list[dict[str, Any]]:
        results = []
        for row in await db.list_plugin_installations():
            candidate_version = str(row.get("candidate_version") or "")
            if candidate_version:
                artifacts = await db.list_plugin_artifacts(row["publisher_id"], row["plugin_id"], state="candidate")
                environments = [item for item in await db.list_plugin_python_environments(
                    row["publisher_id"], row["plugin_id"]) if item["state"] == "candidate"]
                await db.fail_plugin_candidate(
                    row["publisher_id"], row["plugin_id"], candidate_version,
                    "Interrupted candidate was rolled back during startup recovery",
                )
                for artifact in artifacts:
                    await self._remove_artifact_if_unreferenced(artifact["path"])
                for environment in environments:
                    await db.fail_plugin_python_environment(
                        environment["publisher_id"], environment["plugin_id"], environment["plugin_version"],
                        environment["runtime_identity"], environment["lock_digest"])
                    await asyncio.to_thread(self.python_environments.remove_environment, environment["path"])
                row = await db.get_plugin_installation(row["publisher_id"], row["plugin_id"])
                if not row:
                    results.append({
                        "plugin": f"{artifacts[0]['publisher_id']}/{artifacts[0]['plugin_id']}" if artifacts else "unknown",
                        "status": "unavailable",
                        "error": "Interrupted initial plugin install was removed",
                    })
                    continue
            if not row.get("enabled"):
                continue
            identity = f"{row['publisher_id']}/{row['plugin_id']}"
            try:
                await self.enable(identity)
                results.append({"plugin": identity, "status": "active"})
            except BaseException as error:
                results.append({"plugin": identity, "status": "unavailable", "error": _safe_error(error)})
        # Cleanup must protect both the active installation reference and
        # retained/candidate artifact rows.  The installation row is included
        # explicitly because a prior interrupted DB projection may have left
        # the two tables temporarily out of sync.
        live_artifact_paths = await db.list_plugin_artifact_references()
        await asyncio.to_thread(self.store.cleanup_orphan_staging, live_artifact_paths)
        await asyncio.to_thread(self.store.cleanup_orphan_installed, live_artifact_paths)
        return results

    async def permission_projection(self, identity: str) -> dict[str, Any]:
        row = await self._row(identity)
        return await permission_projection(validate_manifest(json.loads(row["manifest_json"])))

    async def approve_permission(self, identity: str, packages: Iterable[dict], permission: str, actor: str) -> dict[str, Any]:
        packages = list(packages)
        if any(package.get("_developer_local") is True for package in packages):
            candidate = await self._developer_candidate_from_packages(packages, identity)
            trust_policy = DeveloperLocalTrustPolicy()
        else:
            candidates = candidates_from_packages(packages, os_name=self.os_name, arch=self.arch)
            candidate = select_candidate(candidates, identity)
            trust_policy = self.trust_policy
        payload = _read_artifact(self.store._source(candidate.local_reference), max_bytes=int(candidate.artifact["size_bytes"]))
        if len(payload) != int(candidate.artifact["size_bytes"]) or hashlib.sha256(payload).hexdigest() != candidate.artifact["sha256"]:
            raise PluginError("ARTIFACT_INTEGRITY_FAILED", "Plugin artifact failed integrity verification", category="artifact")
        trust_policy.verify(candidate.manifest, candidate.artifact, payload)
        request = next((item for item in requested_permissions(candidate.manifest) if item.name == permission), None)
        if request is None or request.name not in HIGH_RISK_PERMISSIONS:
            raise PluginError("INVALID_CAPABILITY_REQUEST", "Permission is not approvable", category="permission")
        await db.set_plugin_permission_approval(
            candidate.manifest.publisher_id, candidate.manifest.plugin_id, request.name, request.fingerprint,
            approved=True, actor=actor, manifest_version=candidate.manifest.version,
        )
        return await permission_projection(candidate.manifest)

    async def revoke_permission(self, identity: str, permission: str, actor: str) -> dict[str, Any]:
        async with self.lifecycle_lock(identity):
            if self.destructive_guard is not None:
                await self.destructive_guard(identity)
            return await self._revoke_permission_unlocked(identity, permission, actor)

    async def _revoke_permission_unlocked(self, identity: str, permission: str, actor: str) -> dict[str, Any]:
        row = await self._row(identity)
        manifest = validate_manifest(json.loads(row["manifest_json"]))
        request = next((item for item in requested_permissions(manifest) if item.name == permission), None)
        if request is None or request.name not in HIGH_RISK_PERMISSIONS:
            raise PluginError("INVALID_CAPABILITY_REQUEST", "Permission is not revocable", category="permission")
        instance = self._active.pop(identity, None)
        if instance:
            await self.runtime.disable(instance)
        await db.set_plugin_permission_approval(
            manifest.publisher_id, manifest.plugin_id, request.name, request.fingerprint,
            approved=False, actor=actor, manifest_version=manifest.version,
        )
        await db.set_plugin_enabled(
            row["publisher_id"], row["plugin_id"], True,
            lifecycle_state="unavailable", error=f"PERMISSION_APPROVAL_REQUIRED: {permission}",
        )
        await self._notify_lifecycle_changed(identity)
        return await permission_projection(manifest)

    async def dependency_projection(self, requirements: Iterable[dict[str, Any]]) -> dict[str, Any]:
        items = []
        for requirement in requirements:
            identity = str(requirement.get("plugin") or "")
            publisher, _, plugin_id = identity.partition("/")
            row = await db.get_plugin_installation(publisher, plugin_id) if publisher and plugin_id else None
            items.append(evaluate_dependency(requirement, row, self.runtime))
        states = {item["status"] for item in items}
        status = "ready" if not states or states == {"ready"} else next(
            value for value in ("dependency_missing", "plugin_incompatible", "provider_unavailable") if value in states
        )
        return {"status": status, "dependencies": items}

    async def installed_content_dependency_projection(self, package_id: str) -> dict[str, Any]:
        install = await db.get_market_install(package_id)
        if not install:
            raise PluginError("RESOURCE_NOT_FOUND", "Content package is not installed", category="market")
        try:
            metadata = json.loads(install.get("metadata_json") or "{}")
        except json.JSONDecodeError as exc:
            raise PluginError("INVALID_PLUGIN_RESPONSE", "Installed Content package metadata is invalid", category="persistence") from exc
        requirements = metadata.get("requires_plugins") or []
        if not isinstance(requirements, list):
            raise PluginError("INVALID_PLUGIN_RESPONSE", "Installed Content package dependencies are invalid", category="persistence")
        return await self.dependency_projection(requirements)

    async def reverse_dependency_projection(self, identity: str) -> dict[str, Any]:
        """List installed Content Packages that still declare ``identity``."""
        dependents = await content_dependents(identity)
        return {"plugin": identity, "status": "blocked" if dependents else "clear", "dependents": dependents}

    async def _row(self, identity: str) -> dict:
        publisher, separator, plugin_id = identity.partition("/")
        row = await db.get_plugin_installation(publisher, plugin_id) if separator else None
        if not row:
            raise PluginError("RESOURCE_NOT_FOUND", "Plugin is not installed", category="market")
        return row

    @staticmethod
    def _public(row: dict | None) -> dict:
        if not row:
            return {}
        return {
            "plugin": f"{row['publisher_id']}/{row['plugin_id']}",
            "installed_version": row["installed_version"],
            "active_version": row["active_version"],
            "enabled": bool(row["enabled"]),
            "trust_state": row["trust_state"],
            "trust_class": row.get("trust_class") or "official",
            "source_key": row["source_key"],
            "lifecycle_state": row["lifecycle_state"],
            "last_activation_status": row["last_activation_status"],
            "last_error": row["last_error"],
            "quarantined": bool(row.get("quarantined")),
            "runtime": _runtime_projection(row),
        }


def _runtime_projection(row: dict) -> dict[str, Any]:
    try:
        manifest = validate_manifest(json.loads(row["manifest_json"]))
    except Exception:
        return {"type": row.get("runtime_type") or "unknown", "environment_status": "invalid"}
    runtime = manifest.runtime
    if runtime.get("type") != "python":
        return {"type": "subprocess", "environment_status": "not_applicable", "dependency_count": 0}
    lock, digest = validate_python_runtime(manifest)
    selected = select_dependency_artifacts(lock)
    return {"type": "python", "python_version_range": runtime["python_version_range"],
            "environment_status": "ready" if row.get("lifecycle_state") == "active" else "unavailable",
            "dependency_count": len(selected), "lock_digest": digest,
            "dependencies": [{"name": item["name"], "version": item["version"]} for item in selected]}
