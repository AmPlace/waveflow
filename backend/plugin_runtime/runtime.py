from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from typing import Any, AsyncContextManager

from .errors import PluginError, invalid_response
from .manifest import PluginManifest
from .permissions import PermissionGate, PermissionPolicy
from .process import PluginProcess
from .registry import LifecycleState, PluginInstance, PluginRegistry
from .validation import (
    validate_channel_catalog, validate_radio_catalog, validate_radio_programme,
    validate_stream_descriptor, validate_visual_metadata,
)


logger = logging.getLogger("waveflow.plugin_runtime")


class PluginRuntime:
    def __init__(self, *, registry: PluginRegistry | None = None, permission_policy: PermissionPolicy | None = None,
                 clock: Callable[[], float] | None = None,
                 sleep: Callable[[float], Awaitable[None]] | None = None,
                 restart_window: float = 600.0, max_starts: int = 3,
                 capability_dispatcher: Any | None = None,
                 lifecycle_callback: Callable[[str, PluginInstance, int | None], Awaitable[None]] | None = None,
                 activation_context: Callable[[str, PluginInstance], AsyncContextManager[None]] | None = None):
        self.registry = registry or PluginRegistry()
        self.permission_policy = permission_policy or PermissionPolicy()
        self.clock = clock or time.monotonic
        self.sleep = sleep or asyncio.sleep
        self.restart_window = restart_window
        self.max_starts = max_starts
        self.capability_dispatcher = capability_dispatcher
        self.lifecycle_callback = lifecycle_callback
        self.activation_context = activation_context
        self._commands: dict[str, tuple[str, ...]] = {}
        self._execution: dict[str, tuple[dict[str, str] | None, str | None]] = {}
        self._gates: dict[str, PermissionGate] = {}
        self._closed = False

    def install(self, manifest: PluginManifest, command: Sequence[str], *, instance_id: str | None = None,
                environment: dict[str, str] | None = None, working_directory: str | None = None) -> PluginInstance:
        instance = PluginInstance(instance_id or str(uuid.uuid4()), manifest)
        self.registry.install(instance)
        self._commands[instance.instance_id] = tuple(command)
        self._execution[instance.instance_id] = (environment, working_directory)
        self._gates[instance.instance_id] = PermissionGate(manifest, self.permission_policy)
        return instance

    @asynccontextmanager
    async def _activation_scope(self, operation: str, instance: PluginInstance):
        if self.activation_context is None:
            yield
            return
        async with self.activation_context(operation, instance):
            yield

    async def enable(
        self, instance: PluginInstance, *, activate: bool = True,
        activation_operation: str = "active_enable",
    ) -> None:
        if activate:
            async with self._activation_scope(activation_operation, instance):
                await self._enable_unchecked(instance, activate=True)
            return
        await self._enable_unchecked(instance, activate=False)

    async def _enable_unchecked(self, instance: PluginInstance, *, activate: bool) -> None:
        if self._closed:
            raise PluginError("PLUGIN_UNAVAILABLE", "Plugin runtime is shutting down", category="lifecycle")
        if instance.state == LifecycleState.QUARANTINED:
            raise PluginError("PLUGIN_QUARANTINED", "Plugin is quarantined", category="lifecycle")
        instance.transition(LifecycleState.STARTING)
        self._record_start(instance)
        async def dispatch(method: str, payload: dict[str, Any], timeout: float, context: dict[str, Any]) -> Any:
            if self.capability_dispatcher is None:
                raise PluginError("CAPABILITY_DENIED", "Core capabilities are unavailable", category="permission")
            return await self.capability_dispatcher.dispatch(
                instance.manifest.identity, self._gates[instance.instance_id], method, payload,
                timeout=timeout, context=context,
            )
        environment, working_directory = self._execution[instance.instance_id]
        process = PluginProcess(self._commands[instance.instance_id], instance.instance_id,
                                on_exit=lambda code: self._on_exit(instance, code), capability_handler=dispatch,
                                environment=environment, working_directory=working_directory)
        instance.process = process
        try:
            await process.start()
            instance.transition(LifecycleState.HANDSHAKING)
            hello = await process.call("runtime.hello", {
                "core_protocol_versions": ["1.1", "1.0"],
                "manifest_identity": instance.manifest.identity,
            }, timeout=5.0)
            self._validate_hello(instance, hello)
            process.negotiate_protocol(str(hello["protocol_version"]))
            health = await process.call("runtime.health", {}, timeout=5.0)
            if not isinstance(health, dict) or health.get("healthy") is not True:
                raise invalid_response("Plugin health check failed")
            if activate:
                self.registry.activate(instance)
                await self._emit_lifecycle("healthy_active", instance)
        except BaseException as exc:
            # Capture the Plugin's own stderr before the process goes away.  It
            # never reaches ``as_contract``; developer-local callers and the SDK
            # test harness opt in explicitly.
            tail = process.diagnostic_tail()
            await process.stop(graceful=False)
            self.registry.mark_unhealthy(instance)
            if tail and isinstance(exc, PluginError):
                exc.internal_diagnostics = {**dict(exc.internal_diagnostics or {}), "plugin_stderr": tail}
            raise

    def _validate_hello(self, instance: PluginInstance, hello: Any) -> None:
        if not isinstance(hello, dict) or hello.get("plugin") != instance.manifest.identity or hello.get("version") != instance.manifest.version:
            raise invalid_response("Plugin hello identity/version mismatch")
        if hello.get("protocol_version") not in {"1.0", "1.1"}:
            raise PluginError("PLUGIN_INCOMPATIBLE", "Plugin protocol version is incompatible", category="compatibility")
        declared_contracts = {(c.contract, c.contract_version): c.features for c in instance.manifest.provider_contracts}
        claimed_contracts: dict[tuple[str, str], set[str]] = {}
        for item in hello.get("provider_contracts", []):
            if not isinstance(item, dict):
                raise invalid_response("Plugin hello contains invalid contracts")
            key = (item.get("contract"), item.get("contract_version"))
            features = item.get("features")
            if key not in declared_contracts or not isinstance(features, list) or not set(features).issubset(declared_contracts[key]):
                raise invalid_response("Plugin hello exceeds declared contracts")
            if item.get("contract") == "tv_visual_provider":
                declared = next(c for c in instance.manifest.provider_contracts if c.contract == "tv_visual_provider")
                if set(item.get("schemes") or []) != set(declared.schemes):
                    raise invalid_response("Plugin hello visual scheme declaration mismatch")
            claimed_contracts[key] = set(features)
        if set(claimed_contracts) != set(declared_contracts):
            raise invalid_response("Plugin hello omits a declared contract")
        claimed_schemes = {(v.get("scheme"), v.get("contract")) for v in hello.get("owned_schemes", []) if isinstance(v, dict)}
        if claimed_schemes != set(instance.manifest.owned_schemes):
            raise invalid_response("Plugin hello scheme declaration mismatch")
        capabilities = hello.get("capabilities")
        if not isinstance(capabilities, list) or not set(capabilities).issubset(instance.manifest.capabilities):
            raise invalid_response("Plugin hello exceeds declared capabilities")
        self._gates[instance.instance_id].validate_hello(hello.get("permissions", []))

    async def request(self, instance: PluginInstance, method: str, payload: dict[str, Any], *, timeout: float = 15.0) -> Any:
        if instance.state == LifecycleState.QUARANTINED:
            raise PluginError("PLUGIN_QUARANTINED", "Plugin is quarantined", category="lifecycle")
        if instance.state != LifecycleState.HEALTHY_ACTIVE or not instance.process:
            raise PluginError("PLUGIN_UNAVAILABLE", "Plugin is unavailable", retryable=True, category="lifecycle")
        try:
            result = await instance.process.call(method, payload, timeout=timeout)
        except PluginError as exc:
            if instance.process is not None and instance.process.exit_code is not None:
                tail = instance.process.diagnostic_tail()
                if tail:
                    exc.internal_diagnostics = {**dict(exc.internal_diagnostics or {}), "plugin_stderr": tail}
            raise
        if method in {"tv.resolve_stream", "radio.resolve_stream"}:
            return validate_stream_descriptor(result)
        if method == "tv.visual_metadata":
            contract = next((item for item in instance.manifest.provider_contracts if item.contract == "tv_visual_provider"), None)
            scheme = str(payload.get("scheme") or "").lower()
            if contract is None or "metadata" not in contract.features or scheme not in contract.schemes:
                raise PluginError("RESOURCE_NOT_FOUND", "Plugin does not implement TV visual metadata", category="request")
            return validate_visual_metadata(result)
        if method == "radio.catalog":
            contract = next(
                (item for item in instance.manifest.provider_contracts if item.contract == "radio_provider"),
                None,
            )
            if contract is None or "catalog" not in contract.features:
                raise PluginError(
                    "RESOURCE_NOT_FOUND", "Plugin does not implement a Radio catalog", category="request",
                )
            owned_schemes = frozenset(
                scheme for scheme, declared_contract in instance.manifest.owned_schemes
                if declared_contract == "radio_provider"
            )
            if not owned_schemes:
                raise PluginError(
                    "SCHEME_CONFLICT", "Radio Plugin does not declare a Radio-owned scheme", category="routing",
                )
            return validate_radio_catalog(result, owned_schemes=owned_schemes)
        if method == "radio.programme":
            contract = next((item for item in instance.manifest.provider_contracts if item.contract == "radio_provider"), None)
            if contract is None or "programme" not in contract.features:
                raise PluginError("RESOURCE_NOT_FOUND", "Plugin does not implement Radio programme", category="request")
            owned_schemes = frozenset(scheme for scheme, declared_contract in instance.manifest.owned_schemes if declared_contract == "radio_provider")
            if not owned_schemes:
                raise PluginError(
                    "SCHEME_CONFLICT", "Radio Plugin does not declare a Radio-owned scheme", category="routing",
                )
            return validate_radio_programme(result, owned_schemes=owned_schemes)
        if method == "channel_catalog.discover":
            contract = next(
                (item for item in instance.manifest.provider_contracts if item.contract == "channel_catalog"),
                None,
            )
            if contract is None or "discover" not in contract.features:
                raise PluginError(
                    "RESOURCE_NOT_FOUND", "Plugin does not implement a channel catalog", category="request",
                )
            owned_schemes = frozenset(
                scheme for scheme, declared_contract in instance.manifest.owned_schemes
                if declared_contract == "tv_provider"
            )
            return validate_channel_catalog(result, owned_schemes=owned_schemes)
        return result

    async def disable(self, instance: PluginInstance) -> None:
        if instance.state == LifecycleState.HEALTHY_ACTIVE:
            instance.transition(LifecycleState.DRAINING)
            self.registry.unregister(instance)
            instance.transition(LifecycleState.STOPPING)
        elif instance.state == LifecycleState.UNHEALTHY:
            instance.transition(LifecycleState.STOPPING)
        else:
            raise PluginError("PLUGIN_UNAVAILABLE", "Plugin cannot be disabled from its current state", category="lifecycle")
        if instance.process:
            await instance.process.stop()
        instance.transition(LifecycleState.INSTALLED_DISABLED)
        instance.health = "unknown"

    async def _on_exit(self, instance: PluginInstance, _code: int | None) -> None:
        if instance.state == LifecycleState.HEALTHY_ACTIVE:
            self.registry.mark_unhealthy(instance)
            await self._emit_lifecycle("unexpected_exit", instance, _code)

    async def _emit_lifecycle(
        self, event: str, instance: PluginInstance, exit_code: int | None = None,
    ) -> None:
        if self.lifecycle_callback is None:
            return
        try:
            await self.lifecycle_callback(event, instance, exit_code)
        except Exception:
            # Live Runtime state remains authoritative for routing.  Production
            # observers own durable retry/reconciliation and callback failures
            # must not crash the process monitor itself.
            logger.warning(
                "Plugin lifecycle callback failed: plugin=%s event=%s",
                instance.manifest.identity,
                event,
            )

    def _record_start(self, instance: PluginInstance) -> None:
        now = self.clock()
        instance.start_attempts[:] = [value for value in instance.start_attempts if now - value <= self.restart_window]
        instance.start_attempts.append(now)

    async def restart(self, instance: PluginInstance) -> None:
        if instance.state != LifecycleState.UNHEALTHY:
            raise PluginError("PLUGIN_UNAVAILABLE", "Only unhealthy plugins can restart", category="lifecycle")
        now = self.clock()
        instance.start_attempts[:] = [value for value in instance.start_attempts if now - value <= self.restart_window]
        if len(instance.start_attempts) >= self.max_starts:
            instance.transition(LifecycleState.QUARANTINED)
            self.registry.unregister(instance)
            raise PluginError("PLUGIN_QUARANTINED", "Plugin restart limit exceeded", category="lifecycle")
        await self.sleep(2 ** max(0, len(instance.start_attempts) - 1))
        await self.enable(instance, activation_operation="restart")

    async def activate_candidate(
        self,
        old: PluginInstance,
        candidate: PluginInstance,
        *,
        commit: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        if old.state != LifecycleState.HEALTHY_ACTIVE:
            raise PluginError("PLUGIN_UNAVAILABLE", "Old plugin is not active", category="lifecycle")
        async with self._activation_scope("candidate_activation", candidate):
            try:
                await self.enable(candidate, activate=False)
                self.registry.replace(old, candidate)
                if commit:
                    try:
                        await commit()
                    except BaseException:
                        self.registry.rollback_replace(old, candidate)
                        candidate.transition(LifecycleState.STOPPING)
                        if candidate.process:
                            await candidate.process.stop(graceful=False)
                        candidate.transition(LifecycleState.INSTALLED_DISABLED)
                        raise
                await self._emit_lifecycle("healthy_active", candidate)
            except BaseException:
                if candidate.process:
                    await candidate.process.stop(graceful=False)
                if candidate.state in {LifecycleState.STARTING, LifecycleState.HANDSHAKING}:
                    self.registry.mark_unhealthy(candidate)
                raise
        old.transition(LifecycleState.STOPPING)
        if old.process:
            try:
                await old.process.stop()
            except BaseException:
                # The registry switch and durable commit already succeeded.
                # Old-process cleanup is best effort and must not roll the
                # active candidate back into an inconsistent DB/runtime pair.
                process = old.process.process
                if process is not None and process.returncode is None:
                    process.kill()
        old.transition(LifecycleState.INSTALLED_DISABLED)

    async def shutdown(self) -> None:
        self._closed = True
        for instance in list(self.registry.instances.values()):
            if instance.state in {LifecycleState.HEALTHY_ACTIVE, LifecycleState.UNHEALTHY}:
                try:
                    await self.disable(instance)
                except PluginError:
                    if instance.process:
                        await instance.process.stop(graceful=False)
        if self.capability_dispatcher is not None:
            self.capability_dispatcher.close()

    async def uninstall(self, instance: PluginInstance) -> None:
        if instance.state in {LifecycleState.HEALTHY_ACTIVE, LifecycleState.UNHEALTHY}:
            await self.disable(instance)
        self.registry.remove(instance)
        self._commands.pop(instance.instance_id, None)
        self._execution.pop(instance.instance_id, None)
        self._gates.pop(instance.instance_id, None)
