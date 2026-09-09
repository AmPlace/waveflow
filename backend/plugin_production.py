from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import shutil
import sys
import tempfile
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin, urlparse

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

import database as db
import market
from plugin_market import (
    PluginArtifactStore, PluginMarketService, current_platform, manifest_signature_payload,
)
from plugin_developer import (
    DEVELOPER_LOCAL_SOURCE_KEY, load_developer_package,
)
from plugin_python_runtime import PythonEnvironmentManager, select_dependency_artifacts
from plugin_desktop_runtime import resolve_plugin_python_executable
from plugin_runtime import LifecycleState, PluginError, PluginRuntime, validate_manifest
from plugin_runtime.permissions import PermissionPolicy
from provider_resolver import ProviderResolver
from radio_core import RadioCatalogBridge, RadioResolver
from plugin_capabilities import CapabilityGateway, CoreCapabilityDispatcher
from plugin_permissions import permission_projection, require_high_risk_approvals
from plugin_channel_catalog import DynamicChannelCatalog
from official_plugin_distribution import (
    OFFICIAL_PUBLISHER_ID, OFFICIAL_RELEASE_ROOT, bundled_official_packages, load_official_trust_rows,
    rollout_policy_allows_runtime,
)


logger = logging.getLogger(__name__)
MAX_PLUGIN_ARTIFACT_BYTES = 64 * 1024 * 1024
MAX_DEPENDENCY_ARTIFACT_BYTES = 64 * 1024 * 1024
PLUGIN_DOWNLOAD_TIMEOUT_SECONDS = 60.0
PLUGIN_DOWNLOAD_REDIRECTS = 3
OFFICIAL_PLUGIN_BOOTSTRAP_SETTING = "official_plugin_bootstrap_v1"
OFFICIAL_PLUGIN_ROLLOUT_SETTING = "official_plugin_rollout_v1"
PLUGIN_STORE_BINDING_SETTING = "plugin_store_binding_v1"
PLUGIN_STORE_MARKER = ".waveflow-plugin-store.json"
PLUGIN_DEVELOPER_MODE_SETTING = "plugin_developer_mode_v1"
RADIO_CATALOG_REQUEST_TIMEOUT_SECONDS = 30.0
RADIO_CATALOG_MAX_ATTEMPTS = 2
RADIO_CATALOG_RETRY_DELAY_SECONDS = 0.25


class ProductionTrustPolicy:
    def __init__(self, rows: Iterable[dict[str, Any]] = ()):
        self.replace(rows)

    def replace(self, rows: Iterable[dict[str, Any]]) -> None:
        keys: dict[tuple[str, str], tuple[bytes, str, bool]] = {}
        for row in rows:
            if not row.get("enabled"):
                continue
            try:
                key = base64.b64decode(str(row["public_key"]), validate=True)
                if len(key) != 32:
                    continue
            except (KeyError, ValueError):
                continue
            keys[(str(row["publisher_id"]), str(row["key_id"]))] = (
                key, str(row["trust_level"]), bool(row.get("require_manifest_signature")),
            )
        self._keys = keys

    def verify(self, manifest, artifact: dict[str, Any], payload: bytes) -> str:
        signature = artifact.get("signature") or {}
        key_id = str(signature.get("key_id") or "")
        trusted = self._keys.get((manifest.publisher_id, key_id))
        if trusted is None:
            raise PluginError("PLUGIN_UNTRUSTED", "Plugin publisher key is not trusted", category="trust")
        key, level, _require_manifest_signature = trusted
        try:
            signature_bytes = base64.b64decode(str(signature.get("value") or ""), validate=True)
            Ed25519PublicKey.from_public_bytes(key).verify(signature_bytes, payload)
        except (ValueError, InvalidSignature) as exc:
            raise PluginError(
                "ARTIFACT_SIGNATURE_INVALID", "Plugin artifact signature is invalid", category="trust"
            ) from exc
        return level

    def verify_manifest(
        self, manifest, artifact: dict[str, Any], signature: dict[str, Any] | None,
    ) -> None:
        artifact_key_id = str((artifact.get("signature") or {}).get("key_id") or "")
        trusted = self._keys.get((manifest.publisher_id, artifact_key_id))
        if trusted is None:
            raise PluginError("PLUGIN_UNTRUSTED", "Plugin publisher key is not trusted", category="trust")
        key, _level, required = trusted
        if signature is None:
            if required:
                raise PluginError(
                    "ARTIFACT_SIGNATURE_INVALID", "Official Plugin manifest signature is required", category="trust",
                )
            return
        if (not isinstance(signature, dict)
                or set(signature) != {"algorithm", "key_id", "value"}
                or signature.get("algorithm") != "ed25519"
                or str(signature.get("key_id") or "") != artifact_key_id):
            raise PluginError("ARTIFACT_SIGNATURE_INVALID", "Plugin manifest signature is invalid", category="trust")
        try:
            encoded = base64.b64decode(str(signature.get("value") or ""), validate=True)
            Ed25519PublicKey.from_public_bytes(key).verify(encoded, manifest_signature_payload(manifest))
        except (ValueError, InvalidSignature) as exc:
            raise PluginError(
                "ARTIFACT_SIGNATURE_INVALID", "Plugin manifest signature is invalid", category="trust",
            ) from exc


async def download_plugin_artifact(
    url: str,
    destination_dir: str | Path,
    *,
    expected_size: int,
    expected_sha256: str,
    max_bytes: int = MAX_PLUGIN_ARTIFACT_BYTES,
    max_redirects: int = PLUGIN_DOWNLOAD_REDIRECTS,
    timeout: float = PLUGIN_DOWNLOAD_TIMEOUT_SECONDS,
    client: httpx.AsyncClient | None = None,
    allow_private: bool = False,
) -> Path:
    if urlparse(url).scheme.lower() != "https":
        raise PluginError("ARTIFACT_INVALID", "Production plugin artifacts require HTTPS", category="artifact")
    if expected_size < 1 or expected_size > max_bytes:
        raise PluginError("ARTIFACT_INVALID", "Plugin artifact size is outside the allowed limit", category="artifact")
    destination = Path(destination_dir)
    destination.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="plugin-download-", dir=destination)
    os.close(fd)
    path = Path(name)
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10.0), follow_redirects=False)
    current = url
    try:
        for redirect_count in range(max_redirects + 1):
            try:
                current = await market._validate_fetch_url(current, allow_private=allow_private)
                async with client.stream("GET", current, follow_redirects=False) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location or redirect_count >= max_redirects:
                            raise PluginError("ARTIFACT_INVALID", "Plugin artifact redirect is invalid", category="artifact")
                        current = urljoin(current, location)
                        continue
                    if response.status_code == 404:
                        raise PluginError("ARTIFACT_NOT_FOUND", "Plugin artifact was not found", category="artifact")
                    response.raise_for_status()
                    declared = response.headers.get("content-length")
                    if declared:
                        try:
                            if int(declared) > max_bytes:
                                raise PluginError("ARTIFACT_INVALID", "Plugin artifact exceeds the size limit", category="artifact")
                        except ValueError as exc:
                            raise PluginError("ARTIFACT_INVALID", "Plugin artifact Content-Length is invalid", category="artifact") from exc
                    digest = hashlib.sha256()
                    size = 0
                    with path.open("wb") as stream:
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            if size > max_bytes:
                                raise PluginError("ARTIFACT_INVALID", "Plugin artifact exceeds the size limit", category="artifact")
                            digest.update(chunk)
                            stream.write(chunk)
                    if size != expected_size or digest.hexdigest() != expected_sha256:
                        raise PluginError("ARTIFACT_INTEGRITY_FAILED", "Plugin artifact integrity check failed", category="artifact")
                    return path
            except PluginError:
                raise
            except (httpx.HTTPError, market.MarketError) as exc:
                raise PluginError("ARTIFACT_INVALID", "Plugin artifact download failed", retryable=True, category="artifact") from exc
        raise PluginError("ARTIFACT_INVALID", "Plugin artifact redirect limit exceeded", category="artifact")
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    finally:
        if owns_client:
            await client.aclose()


@dataclass
class ProductionPluginSubsystem:
    service: PluginMarketService
    trust_policy: ProductionTrustPolicy
    download_root: Path
    http_client: httpx.AsyncClient
    provider_resolver: ProviderResolver
    capability_gateway: CapabilityGateway
    channel_catalog: DynamicChannelCatalog = field(default_factory=DynamicChannelCatalog)
    radio_catalog: RadioCatalogBridge = field(default_factory=RadioCatalogBridge)
    official_release_root: Path = OFFICIAL_RELEASE_ROOT
    _scheme_locks: dict[str, asyncio.Lock] = field(default_factory=dict, init=False, repr=False)
    _critical_tasks: set[asyncio.Task[Any]] = field(default_factory=set, init=False, repr=False)
    _ownership_reconcile_tasks: dict[str, asyncio.Task[Any]] = field(default_factory=dict, init=False, repr=False)
    _ownership_reconcile_generation: dict[str, int] = field(default_factory=dict, init=False, repr=False)
    _lifecycle_reconcile_tasks: dict[str, asyncio.Task[Any]] = field(default_factory=dict, init=False, repr=False)
    _lifecycle_reconcile_generation: dict[str, int] = field(default_factory=dict, init=False, repr=False)
    _radio_catalog_locks: dict[str, asyncio.Lock] = field(default_factory=dict, init=False, repr=False)
    _shutting_down: bool = field(default=False, init=False, repr=False)
    radio_resolver: RadioResolver = field(init=False)
    automation_service: Any = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        # These callbacks are installed on the same service/runtime objects used
        # by all production lifecycle entry points.  That keeps ownership guards
        # and crash persistence on the generic lifecycle path rather than in
        # provider-specific routers.
        self.service.destructive_guard = self._assert_identity_not_owned
        self.service.lifecycle_changed = self._reconcile_radio_tasks
        self.service.runtime.lifecycle_callback = self._on_runtime_lifecycle_event
        self.service.runtime.activation_context = self._runtime_activation_context
        self.radio_resolver = RadioResolver(runtime=self.service.runtime)

    async def _reconcile_radio_tasks(self, identity: str = "") -> None:
        try:
            await self._sync_radio_tasks()
        except Exception:
            # Domain projection cannot roll back an already committed Plugin.
            logger.warning("Radio task reconciliation pending: plugin=%s", identity)
            self._schedule_lifecycle_reconcile(identity)

    async def _sync_radio_tasks(self) -> None:
        if self.automation_service is None or self._shutting_down:
            return
        from radio_tasks import reconcile_radio_automation_tasks

        installed = await db.list_plugin_installations()
        await reconcile_radio_automation_tasks(
            self.automation_service, self,
            retained_owner_identities={f"{row['publisher_id']}/{row['plugin_id']}" for row in installed},
        )

    def _radio_catalog_lock(self, identity: str) -> asyncio.Lock:
        lock = self._radio_catalog_locks.get(identity)
        if lock is None:
            lock = asyncio.Lock()
            self._radio_catalog_locks[identity] = lock
        return lock

    async def _wait_radio_catalog_retry(self) -> None:
        if self._shutting_down:
            raise asyncio.CancelledError
        shutdown_event = getattr(self.service, "_shutdown_event", None)
        if shutdown_event is not None and shutdown_event.is_set():
            raise asyncio.CancelledError
        if shutdown_event is None:
            await asyncio.sleep(RADIO_CATALOG_RETRY_DELAY_SECONDS)
            return
        try:
            await asyncio.wait_for(
                shutdown_event.wait(), timeout=RADIO_CATALOG_RETRY_DELAY_SECONDS,
            )
        except asyncio.TimeoutError:
            return
        raise asyncio.CancelledError

    async def _request_radio_catalog_with_retry(self, instance, *, timeout: float) -> dict[str, Any]:
        for attempt in range(RADIO_CATALOG_MAX_ATTEMPTS):
            try:
                return await self.service.runtime.request(
                    instance, "radio.catalog", {}, timeout=timeout,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                retryable = bool(getattr(exc, "retryable", False)) or isinstance(exc, (TimeoutError, OSError))
                if not retryable or attempt + 1 >= RADIO_CATALOG_MAX_ATTEMPTS:
                    raise
                await self._wait_radio_catalog_retry()
        raise RuntimeError("Radio catalog retry loop did not return")

    @staticmethod
    def _radio_error_text(error: BaseException) -> str:
        code = str(getattr(error, "code", "") or "").strip()
        category = str(getattr(error, "category", "") or "").strip()
        message = str(getattr(error, "message", "") or str(error)).replace("\r", " ").replace("\n", " ").strip()
        prefix = ": ".join(value for value in (code, category) if value)
        return (f"{prefix}: {message}" if prefix else message)[:2048]

    @asynccontextmanager
    async def _runtime_activation_context(self, operation: str, instance):
        """Authorize every production transition that can expose HEALTHY_ACTIVE."""
        identity = instance.manifest.identity
        async with self.service.lifecycle_lock(identity):
            row = await db.get_plugin_installation(
                instance.manifest.publisher_id, instance.manifest.plugin_id,
            )
            if not row:
                raise PluginError(
                    "PLUGIN_UNAVAILABLE", "Plugin installation is unavailable", category="lifecycle",
                )
            if not row.get("enabled") or row.get("quarantined"):
                raise PluginError(
                    "PLUGIN_UNAVAILABLE", "Plugin is not enabled for activation", category="lifecycle",
                )

            if operation == "restart":
                if self.service._active.get(identity) is not instance:
                    raise PluginError(
                        "PLUGIN_UNAVAILABLE", "Plugin restart is no longer authorized", category="lifecycle",
                    )
                expected_version = str(row.get("active_version") or "")
            else:
                if self.service.activation_expectation(instance) != operation:
                    raise PluginError(
                        "PLUGIN_UNAVAILABLE", "Plugin activation is not authorized", category="lifecycle",
                    )
                if operation == "candidate_activation":
                    expected_version = str(
                        row.get("candidate_version") or row.get("active_version") or ""
                    )
                elif operation == "active_enable":
                    expected_version = str(row.get("active_version") or "")
                else:
                    raise PluginError(
                        "PLUGIN_UNAVAILABLE", "Unknown Plugin activation operation", category="lifecycle",
                    )

            if expected_version != instance.manifest.version:
                raise PluginError(
                    "PLUGIN_CANDIDATE_CONFLICT", "Plugin activation version is no longer current",
                    category="lifecycle",
                )
            if operation != "candidate_activation" or not row.get("candidate_version"):
                try:
                    persisted = validate_manifest(json.loads(row.get("manifest_json") or "{}"))
                except (PluginError, json.JSONDecodeError) as exc:
                    raise PluginError(
                        "PLUGIN_UNAVAILABLE", "Installed Plugin manifest is invalid", category="lifecycle",
                    ) from exc
                if (
                    persisted.identity != identity
                    or persisted.version != instance.manifest.version
                    or persisted.owned_schemes != instance.manifest.owned_schemes
                ):
                    raise PluginError(
                        "PLUGIN_CANDIDATE_CONFLICT", "Plugin activation manifest is no longer current",
                        category="lifecycle",
                    )
            await require_high_risk_approvals(instance.manifest, record_pending=False)
            yield

    def _scheme_lock(self, scheme: str) -> asyncio.Lock:
        return self._scheme_locks.setdefault(str(scheme).lower(), asyncio.Lock())

    async def _ownership_row(self, scheme: str) -> dict[str, Any] | None:
        normalized = str(scheme or "").strip().lower()
        return next(
            (row for row in await db.list_plugin_scheme_ownership()
             if str(row.get("scheme") or "").lower() == normalized),
            None,
        )

    async def _assert_identity_not_owned(self, identity: str) -> None:
        for row in await db.list_plugin_scheme_ownership():
            if (str(row.get("plugin_identity") or "") == identity
                    and str(row.get("mode") or "legacy") != "legacy"):
                raise PluginError(
                    "SCHEME_CONFLICT",
                    "Plugin owns a scheme and must be rolled back before this lifecycle operation",
                    category="routing",
                    details={"scheme": str(row.get("scheme") or ""), "plugin": identity},
                )

    @staticmethod
    def _consume_task(
        task: asyncio.Task[Any], *, operation: str = "background_reconciliation", target: str = "",
        report_failure: bool = True, reconciliation_scheduled: bool = False,
    ) -> None:
        if task.cancelled():
            return
        try:
            error = task.exception()
        except asyncio.CancelledError:
            return
        if error is not None and (report_failure or getattr(task, "_waveflow_log_failure", False)):
            logger.error(
                "Plugin lifecycle task failed: operation=%s target=%s error_type=%s "
                "error_code=%s reconciliation_scheduled=%s",
                operation,
                target,
                type(error).__name__,
                getattr(error, "code", "PLUGIN_LIFECYCLE_FAILED"),
                str(reconciliation_scheduled).lower(),
            )

    def _track_critical(
        self, task: asyncio.Task[Any], *, operation: str, target: str,
    ) -> asyncio.Task[Any]:
        self._critical_tasks.add(task)

        def done(value: asyncio.Task[Any]) -> None:
            self._critical_tasks.discard(value)
            reconciliation_scheduled = bool(
                target in self._ownership_reconcile_tasks
                and not self._ownership_reconcile_tasks[target].done()
            )
            self._consume_task(
                value, operation=operation, target=target, report_failure=False,
                reconciliation_scheduled=reconciliation_scheduled,
            )

        task.add_done_callback(done)
        return task

    @staticmethod
    async def _await_critical(task: asyncio.Task[Any]) -> Any:
        """Defer caller cancellation until the independent critical task converges."""
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

    async def _on_runtime_lifecycle_event(
        self, event: str, instance, exit_code: int | None,
    ) -> None:
        """Queue durable Runtime projection; process monitoring never waits on SQLite."""
        if event not in {"unexpected_exit", "healthy_active"} or self._shutting_down:
            return
        self._schedule_lifecycle_reconcile(instance.manifest.identity)

    def _schedule_lifecycle_reconcile(self, identity: str) -> None:
        if self._shutting_down:
            return
        generation = self._lifecycle_reconcile_generation.get(identity, 0) + 1
        self._lifecycle_reconcile_generation[identity] = generation
        task = self._lifecycle_reconcile_tasks.get(identity)
        if task is None or task.done():
            task = asyncio.create_task(
                self._lifecycle_reconcile_worker(identity),
                name=f"plugin-lifecycle-reconcile:{identity}",
            )
            self._lifecycle_reconcile_tasks[identity] = task
            task.add_done_callback(
                lambda value: self._consume_task(
                    value, operation="runtime_reconciliation", target=identity,
                )
            )

    async def _lifecycle_reconcile_worker(self, identity: str) -> None:
        delay = 0.0
        try:
            while not self._shutting_down:
                generation = self._lifecycle_reconcile_generation.get(identity, 0)
                try:
                    await self._reconcile_runtime_identity(identity)
                    await self._sync_radio_tasks()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # Do not leak SQLite paths, subprocess stderr, or upstream
                    # details.  A later retry or restart deterministically
                    # projects the current Runtime state again.
                    logger.warning("Plugin lifecycle persistence will retry: plugin=%s", identity)
                    delay = min(0.5, max(0.01, delay * 2 or 0.01))
                    await asyncio.sleep(delay)
                    continue
                delay = 0.0
                if generation == self._lifecycle_reconcile_generation.get(identity, 0):
                    return
        finally:
            current = self._lifecycle_reconcile_tasks.get(identity)
            if current is asyncio.current_task():
                self._lifecycle_reconcile_tasks.pop(identity, None)

    async def _reconcile_runtime_identity(self, identity: str) -> None:
        publisher, separator, plugin_id = identity.partition("/")
        if not separator:
            return
        async with self.service.lifecycle_lock(identity):
            instance = self.service._active.get(identity)
            row = await db.get_plugin_installation(publisher, plugin_id)
            if not row or not row.get("enabled") or instance is None:
                return
            if (instance.state == LifecycleState.HEALTHY_ACTIVE
                    and instance.health == "healthy"):
                await db.set_plugin_enabled(
                    publisher, plugin_id, True, lifecycle_state="active", error="",
                )
            elif instance.state == LifecycleState.UNHEALTHY:
                await db.set_plugin_enabled(
                    publisher, plugin_id, True, lifecycle_state="unavailable",
                    error="PLUGIN_CRASHED: Plugin process exited unexpectedly",
                )
            else:
                return
        for ownership in await db.list_plugin_scheme_ownership():
            if (str(ownership.get("plugin_identity") or "") == identity
                    and str(ownership.get("mode") or "legacy") != "legacy"):
                self._schedule_ownership_reconcile(str(ownership.get("scheme") or ""))

    @classmethod
    async def create(
        cls, *, root: str | Path, http_client: httpx.AsyncClient,
        command_factory=None,
    ) -> "ProductionPluginSubsystem":
        root = Path(root).resolve()
        root.mkdir(parents=True, exist_ok=True)
        await _bind_plugin_store(root)
        downloads = root / "downloads"
        downloads.mkdir(parents=True, exist_ok=True)
        trust_rows = [row for row in await db.list_plugin_publisher_trust()
                      if row.get("publisher_id") != OFFICIAL_PUBLISHER_ID]
        trust_rows.extend(_official_trust_rows())
        trust = ProductionTrustPolicy(trust_rows)
        gateway = CapabilityGateway(client=http_client)
        dispatcher = CoreCapabilityDispatcher(gateway)
        python_executable = resolve_plugin_python_executable()
        runtime = PluginRuntime(
            permission_policy=PermissionPolicy(frozenset({"network", "cache"})),
            capability_dispatcher=dispatcher,
        )
        store = PluginArtifactStore(root / "artifacts", allowed_local_roots=[downloads])
        service = PluginMarketService(
            runtime=runtime, store=store, trust_policy=trust, command_factory=command_factory,
            python_executable=python_executable,
            python_environments=PythonEnvironmentManager(root, python_executable=python_executable),
            dependency_fetcher=lambda item, directory: download_dependency_artifact(
                item["url"], directory, expected_size=item["size_bytes"],
                expected_sha256=item["sha256"], client=http_client),
        )
        ownership_rows = await db.list_plugin_scheme_ownership()
        resolver = ProviderResolver.from_ownership_rows(ownership_rows, runtime=runtime)
        return cls(service, trust, downloads, http_client, resolver, gateway)

    async def startup(self) -> list[dict[str, Any]]:
        try:
            results = await self.service.recover_enabled()
        except BaseException:
            await self.service.runtime.shutdown()
            raise
        try:
            results.extend(await self.bootstrap_official_plugins())
        except BaseException as error:
            # A corrupt/missing release-local catalog must not tear down
            # already recovered installations or the rest of Core.
            results.append({
                "plugin": f"{OFFICIAL_PUBLISHER_ID}/*", "status": "unavailable",
                "error": _safe_bootstrap_error(error),
            })
        try:
            results.extend(await self.rollout_official_plugins())
        except BaseException as error:
            results.append({
                "plugin": f"{OFFICIAL_PUBLISHER_ID}/*", "status": "unavailable",
                "error": _safe_bootstrap_error(error), "rollout": "failed",
            })
        # Ownership rows are durable desired state.  Resolver construction keeps
        # every non-legacy row fail-closed until recovery/bootstrap/rollout has
        # completed and this final production preflight proves it routable.
        for ownership in await db.list_plugin_scheme_ownership():
            await self._reconcile_ownership_serialized(str(ownership.get("scheme") or ""))
        return results

    async def bootstrap_official_plugins(self) -> list[dict[str, Any]]:
        """Preinstall release-bundled official Plugins once, without taking ownership."""
        enabled = os.environ.get("WAVEFLOW_OFFICIAL_PLUGIN_BOOTSTRAP", "1").strip().lower()
        if enabled in {"0", "false", "no", "off"}:
            return []
        try:
            state = json.loads(await db.get_setting(OFFICIAL_PLUGIN_BOOTSTRAP_SETTING, "{}"))
        except json.JSONDecodeError:
            state = {}
        completed = set(state.get("completed") or []) if isinstance(state, dict) else set()
        results: list[dict[str, Any]] = []
        changed = False
        for package in bundled_official_packages(self.official_release_root):
            manifest = validate_manifest(package.get("plugin_manifest"))
            identity = manifest.identity
            existing = await db.get_plugin_installation(manifest.publisher_id, manifest.plugin_id)
            if existing:
                # Existing rows are not sufficient evidence of a healthy
                # installation.  Recovery normally activates a complete row;
                # a damaged referenced artifact is repaired only from this
                # exact, already trusted bundled candidate.  Disabled or
                # explicitly uninstalled installations remain untouched.
                if not existing.get("enabled") or existing.get("lifecycle_state") in {
                    "disabled", "quarantined",
                }:
                    if identity not in completed:
                        completed.add(identity)
                        changed = True
                    continue
                if self.service.installation_healthy(existing):
                    if identity not in completed:
                        completed.add(identity)
                        changed = True
                    continue
                if not self.service.installed_artifact_valid(existing):
                    try:
                        repaired = await self.repair(identity, [package])
                    except BaseException as error:
                        results.append({
                            "plugin": identity, "status": "unavailable", "error": _safe_bootstrap_error(error),
                            "bootstrap": "repair_failed",
                        })
                    else:
                        if identity not in completed:
                            completed.add(identity)
                            changed = True
                        results.append({
                            "plugin": identity, "status": repaired.get("lifecycle_state") or "active",
                            "bootstrap": "repaired",
                        })
                continue
            if identity in completed:
                # A completed entry with no installation is an explicit uninstall;
                # startup must not silently reinstall it.
                continue
            try:
                installed = await self.install(identity, [package])
            except BaseException as error:
                results.append({
                    "plugin": identity, "status": "unavailable", "error": _safe_bootstrap_error(error),
                })
                continue
            completed.add(identity)
            changed = True
            results.append({
                "plugin": identity, "status": installed.get("lifecycle_state") or "active",
                "bootstrap": "installed",
            })
        if changed:
            await db.set_setting(
                OFFICIAL_PLUGIN_BOOTSTRAP_SETTING,
                json.dumps({"version": 1, "completed": sorted(completed)}, separators=(",", ":"), sort_keys=True),
            )
        return results

    async def rollout_official_plugins(self) -> list[dict[str, Any]]:
        """Apply release-declared ownership once on Python-backed deployments."""
        if not _python_backed_rollout_enabled():
            return []
        try:
            state = json.loads(await db.get_setting(OFFICIAL_PLUGIN_ROLLOUT_SETTING, "{}"))
        except json.JSONDecodeError:
            state = {}
        completed = set(state.get("completed") or []) if isinstance(state, dict) else set()
        ownership = {row["scheme"]: row for row in await db.list_plugin_scheme_ownership()}
        results: list[dict[str, Any]] = []
        changed = False
        rollout_os, rollout_arch = current_platform()
        rollout_python = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        for package in bundled_official_packages(self.official_release_root):
            rollout = package.get("rollout")
            if not isinstance(rollout, dict):
                continue
            baseline = validate_manifest(package.get("plugin_manifest"))
            identity = baseline.identity
            schemes = [scheme for scheme, _contract in baseline.owned_schemes]
            pending_keys = [f"{identity}:{scheme}" for scheme in schemes
                            if f"{identity}:{scheme}" not in completed]
            if not pending_keys:
                continue
            if not rollout_policy_allows_runtime(
                rollout, os_name=rollout_os, arch=rollout_arch, python_version=rollout_python,
            ):
                results.append({
                    "plugin": identity, "status": "unavailable", "rollout": "blocked",
                    "error": (
                        "PLATFORM_UNSUPPORTED: official Plugin rollout requires a runtime acceptance "
                        f"for {rollout_os}/{rollout_arch}/cp{sys.version_info.major}{sys.version_info.minor}"
                    ),
                })
                continue
            row = await db.get_plugin_installation(baseline.publisher_id, baseline.plugin_id)
            if (not row or row.get("trust_state") != "official"
                    or row.get("source_key") != "official"
                    or row.get("source_package_id") != package.get("id")):
                results.append({
                    "plugin": identity, "status": "unavailable", "rollout": "blocked",
                    "error": "PLUGIN_UNAVAILABLE: verified official installation is required",
                })
                continue
            try:
                installed = validate_manifest(json.loads(row.get("manifest_json") or "{}"))
                installed_schemes = {scheme for scheme, _contract in installed.owned_schemes}
                if not set(schemes).issubset(installed_schemes):
                    raise PluginError(
                        "SCHEME_CONFLICT", "Installed Plugin no longer owns the release rollout scheme",
                        category="routing",
                    )
                for scheme in schemes:
                    completion_key = f"{identity}:{scheme}"
                    if completion_key in completed:
                        continue
                    current = ownership.get(scheme)
                    if current and current.get("mode") not in {"legacy", "plugin"}:
                        raise PluginError(
                            "SCHEME_CONFLICT", "Existing migration ownership must be resolved before rollout",
                            category="routing",
                        )
                    if current and current.get("mode") == "plugin" and current.get("plugin_identity") != identity:
                        raise PluginError(
                            "SCHEME_CONFLICT", "Another Plugin already owns the rollout scheme", category="routing",
                        )
                    if current and current.get("mode") == "plugin":
                        await self._ownership_preflight(scheme, identity)
                    else:
                        result = await self.set_ownership(scheme, "plugin", identity)
                        ownership[scheme] = {
                            "scheme": scheme, "mode": result["mode"], "plugin_identity": result["plugin"],
                        }
                    completed.add(completion_key)
                    changed = True
                results.append({
                    "plugin": identity, "status": "active", "rollout": "plugin", "schemes": schemes,
                })
            except BaseException as error:
                results.append({
                    "plugin": identity, "status": "unavailable", "rollout": "blocked",
                    "error": _safe_bootstrap_error(error),
                })
        if changed:
            await db.set_setting(
                OFFICIAL_PLUGIN_ROLLOUT_SETTING,
                json.dumps({"version": 1, "completed": sorted(completed)}, separators=(",", ":"), sort_keys=True),
            )
        return results

    async def shutdown(self) -> None:
        # Signal stopping before waiting.  A post-commit projection retry may
        # never converge in this process, but its durable intent must remain
        # for the next startup recovery pass.
        self._shutting_down = True
        self.service.begin_shutdown()
        try:
            await asyncio.wait_for(self.service.wait_for_critical_tasks(), timeout=5.0)
        except asyncio.TimeoutError:
            self.service.cancel_critical_tasks()
            await self.service.wait_for_critical_tasks()
        if self._critical_tasks:
            await asyncio.gather(
                *(asyncio.shield(task) for task in tuple(self._critical_tasks)),
                return_exceptions=True,
            )
        await self.service.runtime.shutdown()
        workers = [
            *self._ownership_reconcile_tasks.values(),
            *self._lifecycle_reconcile_tasks.values(),
        ]
        for task in workers:
            task.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)

    async def reload_trust(self) -> None:
        rows = [row for row in await db.list_plugin_publisher_trust()
                if row.get("publisher_id") != OFFICIAL_PUBLISHER_ID]
        rows.extend(_official_trust_rows())
        self.trust_policy.replace(rows)

    async def set_ownership(self, scheme: str, mode: str, plugin_identity: str = '') -> dict[str, Any]:
        scheme = str(scheme or '').strip().lower()
        if mode not in {"legacy", "plugin", "migration_test"}:
            raise PluginError("INVALID_PLUGIN_RESPONSE", "Invalid provider ownership mode", category="routing")

        # The caller owns only the wait, never the commit.  Once accepted, this
        # operation retains an independent task through durable write and
        # resolver reconciliation, so repeated request cancellation cannot
        # split SQLite desired state from the in-memory projection.
        task = self._track_critical(
            asyncio.create_task(
                self._set_ownership_serialized(scheme, mode, plugin_identity),
                name=f"plugin-ownership:{scheme}:{mode}",
            ),
            operation="ownership_transition",
            target=scheme,
        )
        return await self._await_critical(task)

    async def _set_ownership_serialized(
        self, scheme: str, mode: str, plugin_identity: str,
    ) -> dict[str, Any]:
        # Lock order is always scheme -> canonical identity.  Destructive
        # lifecycle operations only acquire identity, never scheme, so there is
        # no reverse dependency.
        async with self._scheme_lock(scheme):
            self.provider_resolver.fail_closed(scheme)
            try:
                current = await self._ownership_row(scheme)
            except BaseException as error:
                self._schedule_ownership_reconcile(scheme)
                raise PluginError(
                    "PLUGIN_UNAVAILABLE", "Plugin ownership state is unavailable", category="persistence",
                ) from error
            try:
                identity = ""
                if mode != "legacy":
                    identity = str(plugin_identity or "")
                    if not identity and current and current.get("mode") != "legacy":
                        identity = str(current.get("plugin_identity") or "")
                    if not identity:
                        try:
                            identity = self.service.runtime.registry.route(scheme).manifest.identity
                        except PluginError:
                            identity = ""
                elif current and current.get("mode") != "legacy":
                    identity = str(current.get("plugin_identity") or "")

                if identity:
                    async with self.service.lifecycle_lock(identity):
                        return await self._set_ownership_locked(scheme, mode, plugin_identity)
                return await self._set_ownership_locked(scheme, mode, plugin_identity)
            except BaseException as transition_error:
                # fail_closed is only a transition state.  Never infer the old
                # owner from local variables: project the durable desired row.
                try:
                    await self._reconcile_scheme_from_durable_state_locked(scheme)
                except BaseException:
                    self._schedule_ownership_reconcile(scheme)
                if isinstance(transition_error, PluginError):
                    raise
                raise PluginError(
                    "PLUGIN_UNAVAILABLE", "Plugin ownership transition failed", category="persistence",
                ) from transition_error

    async def _set_ownership_locked(
        self, scheme: str, mode: str, plugin_identity: str,
    ) -> dict[str, Any]:
        if mode != "legacy":
            plugin_identity, _instance = await self._ownership_preflight(scheme, plugin_identity)
        else:
            plugin_identity = ''
        try:
            await db.set_plugin_scheme_ownership(scheme, mode, plugin_identity)
        except BaseException as write_error:
            # The database operation can commit and still surface an exception
            # (for example cancellation/fault injection after SQLite returns).
            # Reload desired state instead of guessing or issuing a rollback.
            try:
                durable = await self._project_durable_ownership_locked(scheme)
            except BaseException as reconcile_error:
                self._schedule_ownership_reconcile(scheme)
                raise PluginError(
                    "PLUGIN_UNAVAILABLE", "Plugin ownership reconciliation failed", category="persistence",
                ) from reconcile_error
            if (str(durable.get("mode") or "legacy") == mode
                    and str(durable.get("plugin_identity") or "") == plugin_identity):
                return self._public_ownership(durable)
            raise PluginError(
                "PLUGIN_UNAVAILABLE", "Plugin ownership update failed", category="persistence",
            ) from write_error
        try:
            durable = await self._project_durable_ownership_locked(scheme)
        except BaseException as reconcile_error:
            self._schedule_ownership_reconcile(scheme)
            raise PluginError(
                "PLUGIN_UNAVAILABLE", "Plugin ownership reconciliation failed", category="persistence",
            ) from reconcile_error
        return self._public_ownership(durable)

    @staticmethod
    def _public_ownership(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "scheme": str(row.get("scheme") or ""),
            "mode": str(row.get("mode") or "legacy"),
            "plugin": str(row.get("plugin_identity") or ""),
        }

    async def _project_durable_ownership_locked(self, scheme: str) -> dict[str, Any]:
        """Project durable desired state; Plugin state remains unavailable until preflight passes."""
        self.provider_resolver.fail_closed(scheme)
        row = await self._ownership_row(scheme)
        if row is None:
            row = {"scheme": scheme, "mode": "legacy", "plugin_identity": ""}
        mode = str(row.get("mode") or "legacy")
        identity = str(row.get("plugin_identity") or "")
        self.provider_resolver.set_mode(scheme, mode, identity, available=False)
        if mode == "legacy":
            self.provider_resolver.mark_available(scheme)
            return row
        try:
            await self._ownership_preflight(scheme, identity)
        except PluginError:
            # Desired ownership remains visible, but routing fails closed until
            # a healthy lifecycle event schedules another reconciliation.
            return row
        self.provider_resolver.mark_available(scheme)
        return row

    async def _reconcile_scheme_from_durable_state_locked(self, scheme: str) -> dict[str, Any]:
        """Rebuild one Resolver projection while the caller owns its scheme lock."""
        self.provider_resolver.fail_closed(scheme)
        row = await self._ownership_row(scheme)
        identity = "" if row is None else str(row.get("plugin_identity") or "")
        mode = "legacy" if row is None else str(row.get("mode") or "legacy")
        if identity and mode != "legacy":
            async with self.service.lifecycle_lock(identity):
                return await self._project_durable_ownership_locked(scheme)
        return await self._project_durable_ownership_locked(scheme)

    def _schedule_ownership_reconcile(self, scheme: str) -> None:
        scheme = str(scheme or "").strip().lower()
        if not scheme or self._shutting_down:
            return
        self._ownership_reconcile_generation[scheme] = (
            self._ownership_reconcile_generation.get(scheme, 0) + 1
        )
        task = self._ownership_reconcile_tasks.get(scheme)
        if task is None or task.done():
            task = asyncio.create_task(
                self._ownership_reconcile_worker(scheme),
                name=f"plugin-ownership-reconcile:{scheme}",
            )
            self._ownership_reconcile_tasks[scheme] = task
            task.add_done_callback(
                lambda value: self._consume_task(
                    value, operation="ownership_reconciliation", target=scheme,
                )
            )

    async def _ownership_reconcile_worker(self, scheme: str) -> None:
        delay = 0.0
        try:
            while not self._shutting_down:
                generation = self._ownership_reconcile_generation.get(scheme, 0)
                try:
                    await self._reconcile_ownership_serialized(scheme)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning("Plugin ownership reconciliation will retry: scheme=%s", scheme)
                    delay = min(0.5, max(0.01, delay * 2 or 0.01))
                    await asyncio.sleep(delay)
                    continue
                delay = 0.0
                if generation == self._ownership_reconcile_generation.get(scheme, 0):
                    return
        finally:
            current = self._ownership_reconcile_tasks.get(scheme)
            if current is asyncio.current_task():
                self._ownership_reconcile_tasks.pop(scheme, None)

    async def _reconcile_ownership_serialized(self, scheme: str) -> dict[str, Any]:
        async with self._scheme_lock(scheme):
            return await self._reconcile_scheme_from_durable_state_locked(scheme)

    async def _ownership_preflight(self, scheme: str, plugin_identity: str = '') -> tuple[str, Any]:
        """Require a healthy, durable installation before routing production traffic."""
        instance = self.service.runtime.registry.route(scheme)
        resolved_identity = instance.manifest.identity
        if plugin_identity and resolved_identity != plugin_identity:
            raise PluginError("SCHEME_CONFLICT", "Requested Plugin does not own this scheme", category="routing")
        publisher, separator, plugin_id = resolved_identity.partition("/")
        row = await db.get_plugin_installation(publisher, plugin_id) if separator else None
        if not row:
            raise PluginError("PLUGIN_UNAVAILABLE", "Plugin installation state is missing", category="routing",
                              details={"scheme": scheme, "plugin": resolved_identity})
        if not row.get("enabled") or row.get("quarantined"):
            raise PluginError("PLUGIN_UNAVAILABLE", "Plugin is not enabled and healthy", category="routing",
                              details={"scheme": scheme, "plugin": resolved_identity,
                                       "lifecycle_state": row.get("lifecycle_state", "")})
        if (row.get("lifecycle_state") != "active"
                or instance.state != LifecycleState.HEALTHY_ACTIVE
                or instance.health != "healthy"):
            raise PluginError("PLUGIN_UNAVAILABLE", "Plugin runtime is not healthy", category="routing",
                              details={"scheme": scheme, "plugin": resolved_identity,
                                       "lifecycle_state": row.get("lifecycle_state", "")})
        active_version = str(row.get("active_version") or "")
        if active_version != instance.manifest.version:
            raise PluginError("PLUGIN_CANDIDATE_CONFLICT", "Plugin installation version is not active in runtime",
                              category="routing", details={"scheme": scheme, "plugin": resolved_identity})
        try:
            persisted_manifest = validate_manifest(json.loads(row.get("manifest_json") or "{}"))
        except (PluginError, json.JSONDecodeError) as exc:
            raise PluginError("PLUGIN_UNAVAILABLE", "Installed Plugin manifest is invalid", category="routing",
                              details={"scheme": scheme, "plugin": resolved_identity}) from exc
        if (persisted_manifest.identity != resolved_identity
                or persisted_manifest.version != active_version
                or persisted_manifest.owned_schemes != instance.manifest.owned_schemes):
            raise PluginError("PLUGIN_CANDIDATE_CONFLICT", "Persisted Plugin manifest does not match active runtime",
                              category="routing", details={"scheme": scheme, "plugin": resolved_identity})
        if scheme not in {owned_scheme for owned_scheme, _contract in persisted_manifest.owned_schemes}:
            raise PluginError("SCHEME_CONFLICT", "Plugin manifest does not own this scheme", category="routing",
                              details={"scheme": scheme, "plugin": resolved_identity})
        permissions = await permission_projection(persisted_manifest)
        if permissions["pending"]:
            raise PluginError(
                "PERMISSION_APPROVAL_REQUIRED", "Plugin permissions must be approved before ownership rollout",
                category="permission", details={
                    "permissions": [item["name"] for item in permissions["pending"]],
                },
            )
        return resolved_identity, instance

    async def disable(self, identity: str) -> dict[str, Any]:
        return await self.service.disable(identity)

    async def refresh_channel_catalog(
        self, identity: str, *, timeout: float = 15.0, now: float | None = None,
    ) -> dict[str, Any]:
        """Refresh an optional catalog; Automation owns when this is called."""
        instance = self.service._active.get(identity)
        if instance is None or instance.state != LifecycleState.HEALTHY_ACTIVE:
            return self.channel_catalog.record_failure(
                identity,
                PluginError("PLUGIN_UNAVAILABLE", "Plugin catalog provider is unavailable", category="lifecycle"),
                now=now,
            )
        return await self.channel_catalog.refresh_plugin(
            identity, self.service.runtime, instance, timeout=timeout, now=now,
        )

    async def refresh_radio_catalog(
        self, identity: str, *, timeout: float = RADIO_CATALOG_REQUEST_TIMEOUT_SECONDS,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Refresh the optional Radio catalog into durable Radio tables.

        This is intentionally a callable bridge rather than a Plugin-owned
        scheduler.  Automation can invoke it later using the existing task
        ownership rules.
        """
        async with self._radio_catalog_lock(identity):
            generation = await db.begin_radio_catalog_refresh(identity)
            instance = self.service._active.get(identity)
            if instance is None or instance.state != LifecycleState.HEALTHY_ACTIVE:
                error = PluginError("PLUGIN_UNAVAILABLE", "Radio Plugin is unavailable", category="lifecycle")
                await self.radio_catalog.record_failure(identity, error, generation=generation)
                return {"status": "failed", "error": error.as_contract()}
            owned_schemes = frozenset(
                scheme for scheme, contract in instance.manifest.owned_schemes
                if contract == "radio_provider"
            )
            if not owned_schemes:
                error = PluginError(
                    "SCHEME_CONFLICT", "Radio Plugin does not declare a Radio-owned scheme", category="routing",
                )
                await self.radio_catalog.record_failure(identity, error, generation=generation)
                return {"status": "failed", "error": error.as_contract()}
            try:
                catalog = await self._request_radio_catalog_with_retry(instance, timeout=timeout)
                result = await self.radio_catalog.refresh(
                    identity, catalog, owned_schemes=owned_schemes, now=now,
                    generation=generation,
                )
                return {"status": result.get("status", "success"), **result}
            except Exception as exc:
                await self.radio_catalog.record_failure(identity, exc, generation=generation)
                return {"status": "failed", "error": self._radio_error_text(exc)}

    async def refresh_radio_programmes(
        self, identity: str, *, timeout: float = 15.0, now: float | None = None,
    ) -> dict[str, Any]:
        """Refresh provider-native Radio programme snapshots via Automation.

        The durable station/source rows are the work list.  No TV EPG state or
        Plugin-owned scheduler is involved, and one source failure does not
        discard another source's last-good snapshot.
        """
        instance = self.service._active.get(identity)
        if instance is None or instance.state != LifecycleState.HEALTHY_ACTIVE:
            return {"status": "failed", "checked": 0, "updated": 0, "failed": 1,
                    "error": "Radio Plugin is unavailable"}
        checked = updated = failed = 0
        errors: list[str] = []
        for station in await db.list_radio_stations(owner_identity=identity, now_unix=now):
            for source in station.get("sources") or []:
                if source.get("lifecycle_state") == "expired":
                    continue
                checked += 1
                try:
                    await self.radio_resolver.resolve_programme(
                        source["source_id"], station_id=station["station_id"],
                    )
                    updated += 1
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    failed += 1
                    errors.append(f"{source.get('source_id')}: {type(exc).__name__}")
        return {
            "status": "success" if failed == 0 else ("partial" if updated else "failed"),
            "checked": checked, "updated": updated, "failed": failed,
            "errors": errors[:32],
        }

    async def uninstall(self, identity: str) -> bool:
        removed = await self.service.uninstall(identity)
        if removed:
            self.channel_catalog.remove_plugin(identity)
        if removed and identity.startswith(f"{OFFICIAL_PUBLISHER_ID}/"):
            await self._remember_official_bootstrap(identity)
        return removed

    @staticmethod
    async def _remember_official_bootstrap(identity: str) -> None:
        try:
            state = json.loads(await db.get_setting(OFFICIAL_PLUGIN_BOOTSTRAP_SETTING, "{}"))
        except json.JSONDecodeError:
            state = {}
        completed = set(state.get("completed") or []) if isinstance(state, dict) else set()
        completed.add(identity)
        await db.set_setting(
            OFFICIAL_PLUGIN_BOOTSTRAP_SETTING,
            json.dumps({"version": 1, "completed": sorted(completed)}, separators=(",", ":"), sort_keys=True),
        )

    async def approve_permission(self, identity: str, packages: Iterable[dict[str, Any]],
                                 permission: str, actor: str) -> dict[str, Any]:
        prepared, temp_paths = await self._prepare_packages(packages)
        try:
            async with self.service.lifecycle_lock(identity):
                return await self.service.approve_permission(identity, prepared, permission, actor)
        finally:
            for path in temp_paths:
                path.unlink(missing_ok=True)

    async def revoke_permission(self, identity: str, permission: str, actor: str) -> dict[str, Any]:
        return await self.service.revoke_permission(identity, permission, actor)

    async def install(self, identity: str, packages: Iterable[dict[str, Any]]) -> dict[str, Any]:
        prepared, temp_paths = await self._prepare_packages(packages)
        try:
            return await self.service.install_from_packages(prepared, identity)
        finally:
            for path in temp_paths:
                path.unlink(missing_ok=True)

    async def developer_mode_enabled(self) -> bool:
        return (await db.get_setting(PLUGIN_DEVELOPER_MODE_SETTING, "0")).strip() == "1"

    async def set_developer_mode(self, enabled: bool) -> dict[str, Any]:
        await db.set_setting(PLUGIN_DEVELOPER_MODE_SETTING, "1" if enabled else "0")
        return {"enabled": bool(enabled)}

    async def install_developer_local(self, path: str) -> dict[str, Any]:
        if not await self.developer_mode_enabled():
            raise PluginError(
                "DEVELOPER_MODE_REQUIRED",
                "Developer Mode must be enabled before installing a local Plugin",
                category="trust",
            )
        prepared, staging_root = await self._stage_developer_local_package(path)
        manifest = validate_manifest(prepared["plugin_manifest"])
        identity = manifest.identity
        try:
            return await self.service.install_developer_local([prepared], identity)
        finally:
            await asyncio.to_thread(shutil.rmtree, staging_root, True)

    async def approve_developer_local_permission(
        self, path: str, permission: str, actor: str,
    ) -> dict[str, Any]:
        """Approve a high-risk permission for an explicitly selected local package.

        Local packages must use the same normal permission approval boundary as
        Market packages, but their unsigned marker is verified by the local
        trust policy rather than the Official publisher trust anchor.
        """
        if not await self.developer_mode_enabled():
            raise PluginError(
                "DEVELOPER_MODE_REQUIRED",
                "Developer Mode must be enabled before approving a local Plugin permission",
                category="trust",
            )
        package, staging_root = await self._stage_developer_local_package(path)
        try:
            manifest = validate_manifest(package["plugin_manifest"])
            return await self.service.approve_permission(
                manifest.identity, [package], permission, actor,
            )
        finally:
            await asyncio.to_thread(shutil.rmtree, staging_root, True)

    async def _stage_developer_local_package(
        self, path: str,
    ) -> tuple[dict[str, Any], Path]:
        package = await asyncio.to_thread(load_developer_package, path)
        staging_root = Path(tempfile.mkdtemp(prefix="developer-local-", dir=self.download_root))
        try:
            local_artifacts = []
            for item in package.get("artifact_references") or []:
                source = Path(str(item["local_path"])).resolve(strict=True)
                target = staging_root / f"artifact-{item['sha256']}"
                await asyncio.to_thread(shutil.copyfile, source, target)
                local_artifacts.append({"sha256": item["sha256"], "local_path": str(target)})
            local_dependencies = []
            for item in package.get("dependency_references") or []:
                source = Path(str(item["local_path"])).resolve(strict=True)
                target = staging_root / f"dependency-{item['sha256']}.whl"
                await asyncio.to_thread(shutil.copyfile, source, target)
                local_dependencies.append({"sha256": item["sha256"], "local_path": str(target)})
            prepared = dict(package)
            prepared["artifact_references"] = local_artifacts
            prepared["dependency_references"] = local_dependencies
            prepared["market_source"] = {
                "source_key": DEVELOPER_LOCAL_SOURCE_KEY,
                "is_builtin": False,
            }
            return prepared, staging_root
        except BaseException:
            await asyncio.to_thread(shutil.rmtree, staging_root, True)
            raise

    async def repair(self, identity: str, packages: Iterable[dict[str, Any]]) -> dict[str, Any]:
        prepared, temp_paths = await self._prepare_packages(packages)
        try:
            return await self.service.repair_from_packages(prepared, identity)
        finally:
            for path in temp_paths:
                path.unlink(missing_ok=True)

    async def _prepare_packages(self, packages: Iterable[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[Path]]:
        prepared: list[dict[str, Any]] = []
        temp_paths: list[Path] = []
        os_name, arch = current_platform()
        for package in packages:
            package = json.loads(json.dumps(package))
            manifest = package.get("plugin_manifest") or {}
            references = package.get("artifact_references") or []
            local_references = []
            for artifact in manifest.get("artifacts") or []:
                if artifact.get("os") != os_name or artifact.get("arch") != arch:
                    continue
                remote = next((item for item in references if item.get("sha256") == artifact.get("sha256")), None)
                if not remote:
                    continue
                bundled_path = remote.get("_bundled_path")
                if bundled_path and package.get("_bundled_release") is True:
                    path = await asyncio.to_thread(
                        _stage_bundled_plugin_artifact,
                        Path(str(bundled_path)), self.official_release_root, self.download_root,
                        int(artifact["size_bytes"]), str(artifact["sha256"]),
                    )
                elif remote.get("url"):
                    base_url = str(package.get("manifest_url") or package.get("market_url") or "")
                    path = await download_plugin_artifact(
                        urljoin(base_url, str(remote["url"])), self.download_root,
                        expected_size=int(artifact["size_bytes"]), expected_sha256=str(artifact["sha256"]),
                        client=self.http_client,
                    )
                else:
                    continue
                temp_paths.append(path)
                local_references.append({"sha256": artifact["sha256"], "local_path": str(path)})
            package["artifact_references"] = local_references
            dependency_references = package.get("dependency_references") or []
            local_dependencies = []
            lock = (manifest.get("runtime") or {}).get("dependency_lock") or {}
            selected = {
                item["sha256"]: item
                for item in select_dependency_artifacts(lock)
            } if lock.get("artifacts") else {}
            for digest, dependency in selected.items():
                remote = next(
                    (item for item in dependency_references
                     if isinstance(item, dict) and item.get("sha256") == digest),
                    None,
                )
                if not remote:
                    continue
                bundled_path = remote.get("_bundled_path")
                if bundled_path and package.get("_bundled_release") is True:
                    path = await asyncio.to_thread(
                        _stage_bundled_plugin_artifact,
                        Path(str(bundled_path)), self.official_release_root, self.download_root,
                        int(dependency["size_bytes"]), str(dependency["sha256"]),
                    )
                elif remote.get("url"):
                    base_url = str(package.get("manifest_url") or package.get("market_url") or "")
                    path = await download_dependency_artifact(
                        urljoin(base_url, str(remote["url"])), self.download_root,
                        expected_size=int(dependency["size_bytes"]), expected_sha256=str(dependency["sha256"]),
                        client=self.http_client,
                    )
                else:
                    continue
                temp_paths.append(path)
                local_dependencies.append({"sha256": digest, "local_path": str(path)})
            package["dependency_references"] = local_dependencies
            prepared.append(package)
        return prepared, temp_paths


async def download_dependency_artifact(
    url: str, destination_dir: str | Path, *, expected_size: int, expected_sha256: str,
    client: httpx.AsyncClient | None = None,
) -> Path:
    try:
        return await download_plugin_artifact(
            url, destination_dir, expected_size=expected_size, expected_sha256=expected_sha256,
            max_bytes=MAX_DEPENDENCY_ARTIFACT_BYTES, client=client,
        )
    except PluginError as exc:
        mapping = {
            "ARTIFACT_NOT_FOUND": "DEPENDENCY_ARTIFACT_NOT_FOUND",
            "ARTIFACT_INTEGRITY_FAILED": "DEPENDENCY_ARTIFACT_INTEGRITY_FAILED",
            "ARTIFACT_INVALID": "DEPENDENCY_ARTIFACT_INTEGRITY_FAILED",
        }
        raise PluginError(mapping.get(exc.code, exc.code), "Dependency artifact download failed",
                          retryable=exc.retryable, category="dependency") from exc


def default_plugin_root() -> Path:
    configured = os.environ.get("WAVEFLOW_PLUGIN_ROOT")
    if configured:
        return Path(configured)
    configured_db = os.environ.get("WAVEFLOW_DB_PATH")
    if configured_db:
        if configured_db == ":memory:" or configured_db.startswith("file:"):
            raise PluginError(
                "ARTIFACT_INVALID",
                "An in-memory or URI database requires an explicit Plugin store root",
                category="artifact",
            )
        return Path(configured_db).expanduser().resolve().parent / "plugins"
    return Path(__file__).resolve().parent / "data" / "plugins"


def _read_plugin_store_marker(root: Path) -> str:
    marker = root / PLUGIN_STORE_MARKER
    if not marker.exists():
        return ""
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PluginError(
            "ARTIFACT_INVALID", "Plugin store binding marker is invalid", category="artifact",
        ) from exc
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema_version", "binding_id"}
        or payload.get("schema_version") != 1
        or not isinstance(payload.get("binding_id"), str)
        or not payload["binding_id"]
    ):
        raise PluginError(
            "ARTIFACT_INVALID", "Plugin store binding marker is invalid", category="artifact",
        )
    return payload["binding_id"]


def _write_plugin_store_marker(root: Path, binding_id: str) -> None:
    marker = root / PLUGIN_STORE_MARKER
    fd, temporary_name = tempfile.mkstemp(prefix=f"{PLUGIN_STORE_MARKER}.", dir=root)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(
                {"schema_version": 1, "binding_id": binding_id},
                stream,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            stream.flush()
            os.fsync(stream.fileno())
        try:
            # Hard-link publication is atomic and refuses to overwrite a
            # marker concurrently published by a different database.
            os.link(temporary, marker)
        except FileExistsError:
            if _read_plugin_store_marker(root) != binding_id:
                raise PluginError(
                    "ARTIFACT_INVALID",
                    "Plugin store and database binding do not match",
                    category="artifact",
                )
    finally:
        temporary.unlink(missing_ok=True)


def _validate_unbound_plugin_store(root: Path, references: Iterable[str]) -> None:
    artifacts_root = (root / "artifacts").resolve()
    managed_roots = (
        (artifacts_root / "installed").resolve(),
        (artifacts_root / "staged").resolve(),
    )
    live = {Path(value).resolve() for value in references}
    if any(not any(path.is_relative_to(managed) for managed in managed_roots) for path in live):
        raise PluginError(
            "ARTIFACT_INVALID",
            "Database Plugin artifact references belong to a different store",
            category="artifact",
        )
    # An old (pre-binding-marker) store can have empty version directories
    # after its artifact files were removed.  An empty/unrelated database must
    # not be allowed to claim and then clean that durable store either.
    durable_footprints = any(
        path.is_dir()
        for managed in managed_roots
        if managed.exists()
        for path in managed.glob("*/*/*")
    ) or any(
        path.is_dir()
        for container in (root / "environments", root / "workspaces")
        if container.exists()
        for path in container.glob("*/*")
    )
    if not live and durable_footprints:
        raise PluginError(
            "ARTIFACT_INVALID",
            "Plugin store contains durable state not referenced by this database",
            category="artifact",
        )
    existing = {
        artifact.resolve()
        for managed in managed_roots
        if managed.exists()
        for artifact in managed.glob("*/*/*/*/artifact")
    }
    if existing - live:
        raise PluginError(
            "ARTIFACT_INVALID",
            "Plugin store contains artifacts not referenced by this database",
            category="artifact",
        )


async def _bind_plugin_store(root: Path) -> None:
    """Bind one durable Plugin store to exactly one durable database.

    Orphan cleanup is destructive, so it must not run merely because a caller
    supplied an empty or unrelated database.  The DB value and filesystem
    marker form a generic ownership fence around all production cleanup paths.
    """
    persisted = await db.get_setting(PLUGIN_STORE_BINDING_SETTING, "")
    marker = await asyncio.to_thread(_read_plugin_store_marker, root)
    if marker and not persisted:
        raise PluginError(
            "ARTIFACT_INVALID",
            "Plugin store is already bound to another database",
            category="artifact",
        )
    selected = persisted or uuid.uuid4().hex
    binding_id = await db.get_or_create_setting(PLUGIN_STORE_BINDING_SETTING, selected)
    if marker:
        if marker != binding_id:
            raise PluginError(
                "ARTIFACT_INVALID",
                "Plugin store and database binding do not match",
                category="artifact",
            )
        return
    references = await db.list_plugin_artifact_references()
    await asyncio.to_thread(_validate_unbound_plugin_store, root, references)
    await asyncio.to_thread(_write_plugin_store_marker, root, binding_id)


def _official_trust_rows() -> list[dict[str, Any]]:
    """Load built-in and deployment public keys; private keys never enter Core."""
    try:
        rows = load_official_trust_rows()
    except PluginError:
        logger.exception("Bundled official Plugin trust anchor is invalid")
        rows = []
    raw = os.environ.get("WAVEFLOW_OFFICIAL_PLUGIN_KEYS", "")
    if not raw:
        return rows
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Ignoring invalid WAVEFLOW_OFFICIAL_PLUGIN_KEYS configuration")
        return rows
    if not isinstance(data, dict):
        return rows
    for publisher_id, keys in data.items():
        if str(publisher_id) != OFFICIAL_PUBLISHER_ID:
            logger.warning("Ignoring non-official publisher in WAVEFLOW_OFFICIAL_PLUGIN_KEYS")
            continue
        if not isinstance(keys, dict):
            continue
        for key_id, public_key in keys.items():
            if any(row["publisher_id"] == publisher_id and row["key_id"] == key_id for row in rows):
                logger.warning("Ignoring deployment key that attempts to replace a bundled official trust anchor")
                continue
            rows.append({
                "publisher_id": str(publisher_id), "key_id": str(key_id),
                "public_key": str(public_key), "trust_level": "official",
                "enabled": 1, "require_manifest_signature": True,
            })
    return rows


def _python_backed_rollout_enabled() -> bool:
    if getattr(sys, "frozen", False):
        # A frozen Desktop backend is eligible only when the release supplied
        # controlled sidecar has already passed its runtime/manifest/integrity
        # validation.  This keeps Desktop on the same generic rollout path as
        # ordinary Python deployments without treating the PyInstaller
        # interpreter as a Plugin runtime or adding provider-specific policy.
        try:
            resolve_plugin_python_executable()
        except PluginError:
            return False
        return True
    if os.environ.get("WAVEFLOW_MODE", "nas").strip().lower() == "desktop":
        return False
    value = os.environ.get("WAVEFLOW_OFFICIAL_PLUGIN_ROLLOUT")
    if value is None:
        bootstrap = os.environ.get("WAVEFLOW_OFFICIAL_PLUGIN_BOOTSTRAP", "1").strip().lower()
        return bootstrap not in {"0", "false", "no", "off"}
    return value.strip().lower() not in {"0", "false", "no", "off"}


def _stage_bundled_plugin_artifact(
    source: Path, release_root: Path, destination_dir: Path, expected_size: int, expected_sha256: str,
) -> Path:
    release_root = release_root.resolve()
    source = source.resolve(strict=True)
    if not source.is_file() or not source.is_relative_to(release_root):
        raise PluginError("CAPABILITY_DENIED", "Bundled Plugin artifact path is not trusted", category="artifact")
    if expected_size < 1 or expected_size > MAX_PLUGIN_ARTIFACT_BYTES:
        raise PluginError("ARTIFACT_INVALID", "Plugin artifact size is outside the allowed limit", category="artifact")
    destination_dir.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="plugin-bundled-", dir=destination_dir)
    os.close(fd)
    target = Path(name)
    digest = hashlib.sha256()
    size = 0
    try:
        with source.open("rb") as read, target.open("wb") as write:
            for chunk in iter(lambda: read.read(1024 * 1024), b""):
                size += len(chunk)
                if size > MAX_PLUGIN_ARTIFACT_BYTES:
                    raise PluginError("ARTIFACT_INVALID", "Plugin artifact exceeds the size limit", category="artifact")
                digest.update(chunk)
                write.write(chunk)
        if size != expected_size or digest.hexdigest() != expected_sha256:
            raise PluginError("ARTIFACT_INTEGRITY_FAILED", "Plugin artifact integrity check failed", category="artifact")
        return target
    except BaseException:
        target.unlink(missing_ok=True)
        raise


def _safe_bootstrap_error(error: BaseException) -> str:
    if isinstance(error, PluginError):
        return f"{error.code}: {error.message}"[:1024]
    return "Official Plugin bootstrap failed"
