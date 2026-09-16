from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .errors import PluginError
from .manifest import PluginManifest


class LifecycleState(str, Enum):
    ABSENT = "ABSENT"
    INSTALLED_DISABLED = "INSTALLED_DISABLED"
    STARTING = "STARTING"
    HANDSHAKING = "HANDSHAKING"
    HEALTHY_ACTIVE = "HEALTHY_ACTIVE"
    DRAINING = "DRAINING"
    STOPPING = "STOPPING"
    UNHEALTHY = "UNHEALTHY"
    QUARANTINED = "QUARANTINED"
    REMOVING = "REMOVING"


ALLOWED_TRANSITIONS = {
    LifecycleState.ABSENT: {LifecycleState.INSTALLED_DISABLED},
    LifecycleState.INSTALLED_DISABLED: {LifecycleState.STARTING, LifecycleState.REMOVING},
    LifecycleState.STARTING: {LifecycleState.HANDSHAKING, LifecycleState.UNHEALTHY},
    LifecycleState.HANDSHAKING: {LifecycleState.HEALTHY_ACTIVE, LifecycleState.UNHEALTHY},
    LifecycleState.HEALTHY_ACTIVE: {LifecycleState.DRAINING, LifecycleState.UNHEALTHY},
    LifecycleState.DRAINING: {LifecycleState.STOPPING},
    LifecycleState.STOPPING: {LifecycleState.INSTALLED_DISABLED, LifecycleState.UNHEALTHY},
    LifecycleState.UNHEALTHY: {LifecycleState.STARTING, LifecycleState.QUARANTINED, LifecycleState.STOPPING},
    LifecycleState.QUARANTINED: {LifecycleState.INSTALLED_DISABLED, LifecycleState.REMOVING},
    LifecycleState.REMOVING: {LifecycleState.ABSENT},
}


@dataclass
class PluginInstance:
    instance_id: str
    manifest: PluginManifest
    state: LifecycleState = LifecycleState.ABSENT
    health: str = "unknown"
    process: Any = None
    start_attempts: list[float] = field(default_factory=list)

    def transition(self, target: LifecycleState) -> None:
        if target not in ALLOWED_TRANSITIONS[self.state]:
            raise PluginError("INVALID_PLUGIN_RESPONSE", f"Illegal plugin lifecycle transition {self.state.value} -> {target.value}", category="lifecycle")
        self.state = target


class PluginRegistry:
    def __init__(self):
        self.instances: dict[str, PluginInstance] = {}
        self._scheme_owners: dict[str, str] = {}

    def install(self, instance: PluginInstance) -> None:
        if instance.instance_id in self.instances:
            raise PluginError("INVALID_PLUGIN_RESPONSE", "Plugin instance already exists", category="registry")
        instance.transition(LifecycleState.INSTALLED_DISABLED)
        self.instances[instance.instance_id] = instance

    def activate(self, instance: PluginInstance) -> None:
        if instance.state != LifecycleState.HANDSHAKING:
            raise PluginError("INVALID_PLUGIN_RESPONSE", "Plugin is not ready for activation", category="lifecycle")
        conflicts = [scheme for scheme, _ in instance.manifest.owned_schemes
                     if self._scheme_owners.get(scheme) not in {None, instance.instance_id}]
        if conflicts:
            raise PluginError("SCHEME_CONFLICT", "Plugin scheme is already owned", category="registry",
                              details={"schemes": sorted(conflicts)})
        instance.transition(LifecycleState.HEALTHY_ACTIVE)
        instance.health = "healthy"
        for scheme, _ in instance.manifest.owned_schemes:
            self._scheme_owners[scheme] = instance.instance_id

    def replace(self, old: PluginInstance, candidate: PluginInstance) -> None:
        if old.state != LifecycleState.HEALTHY_ACTIVE or candidate.state != LifecycleState.HANDSHAKING:
            raise PluginError("INVALID_PLUGIN_RESPONSE", "Plugin instances are not ready for atomic replacement", category="lifecycle")
        if old.manifest.identity != candidate.manifest.identity:
            raise PluginError("PLUGIN_INCOMPATIBLE", "Candidate identity differs from the active plugin", category="compatibility")
        candidate_schemes = {scheme for scheme, _ in candidate.manifest.owned_schemes}
        old_schemes = {scheme for scheme, _ in old.manifest.owned_schemes}
        if not old_schemes.issubset(candidate_schemes):
            raise PluginError(
                "PLUGIN_INCOMPATIBLE",
                "Candidate cannot remove schemes from the active plugin boundary",
                category="compatibility",
            )
        conflicts = [scheme for scheme in candidate_schemes
                     if self._scheme_owners.get(scheme) not in {None, old.instance_id}]
        if conflicts:
            raise PluginError("SCHEME_CONFLICT", "Candidate scheme is owned by another plugin", category="registry",
                              details={"schemes": sorted(conflicts)})
        candidate.transition(LifecycleState.HEALTHY_ACTIVE)
        candidate.health = "healthy"
        # A signed Plugin update may extend its provider boundary.  The
        # replacement is still atomic: new schemes must be unowned (or owned
        # by the old instance).  Active schemes are never silently removed;
        # retirement needs an explicit ownership/lifecycle transition. Durable
        # provider ownership remains a separate Core state and is not changed
        # by this runtime registry operation.
        for scheme in candidate_schemes:
            self._scheme_owners[scheme] = candidate.instance_id
        old.transition(LifecycleState.DRAINING)

    def rollback_replace(self, old: PluginInstance, candidate: PluginInstance) -> None:
        if old.state != LifecycleState.DRAINING or candidate.state != LifecycleState.HEALTHY_ACTIVE:
            raise PluginError("INVALID_PLUGIN_RESPONSE", "Plugin replacement cannot be rolled back", category="lifecycle")
        candidate_schemes = {scheme for scheme, _ in candidate.manifest.owned_schemes}
        old_schemes = {scheme for scheme, _ in old.manifest.owned_schemes}
        if any(self._scheme_owners.get(scheme) != candidate.instance_id for scheme in candidate_schemes):
            raise PluginError("SCHEME_CONFLICT", "Plugin ownership changed during rollback", category="registry")
        for scheme in candidate_schemes:
            if scheme in old_schemes:
                self._scheme_owners[scheme] = old.instance_id
            else:
                self._scheme_owners.pop(scheme, None)
        for scheme in old_schemes - candidate_schemes:
            self._scheme_owners[scheme] = old.instance_id
        old.state = LifecycleState.HEALTHY_ACTIVE
        old.health = "healthy"
        candidate.transition(LifecycleState.DRAINING)

    def unregister(self, instance: PluginInstance) -> None:
        for scheme, owner in list(self._scheme_owners.items()):
            if owner == instance.instance_id:
                self._scheme_owners.pop(scheme, None)

    def mark_unhealthy(self, instance: PluginInstance) -> None:
        self.unregister(instance)
        if instance.state in {LifecycleState.STARTING, LifecycleState.HANDSHAKING, LifecycleState.HEALTHY_ACTIVE, LifecycleState.STOPPING}:
            instance.transition(LifecycleState.UNHEALTHY)
        instance.health = "unhealthy"

    def route(self, scheme: str) -> PluginInstance:
        owner = self._scheme_owners.get(scheme.lower())
        if not owner:
            raise PluginError("SCHEME_UNOWNED", "No active plugin owns this scheme", category="registry")
        instance = self.instances[owner]
        if instance.state == LifecycleState.QUARANTINED:
            raise PluginError("PLUGIN_QUARANTINED", "Plugin is quarantined", category="lifecycle")
        if instance.state != LifecycleState.HEALTHY_ACTIVE:
            raise PluginError("PLUGIN_UNAVAILABLE", "Plugin is unavailable", retryable=True, category="lifecycle")
        return instance

    def recover(self, instance: PluginInstance) -> None:
        if instance.state != LifecycleState.QUARANTINED:
            raise PluginError("INVALID_PLUGIN_RESPONSE", "Only quarantined plugins require explicit recovery", category="lifecycle")
        instance.transition(LifecycleState.INSTALLED_DISABLED)
        instance.health = "unknown"

    def remove(self, instance: PluginInstance) -> None:
        if instance.state not in {LifecycleState.INSTALLED_DISABLED, LifecycleState.QUARANTINED}:
            raise PluginError("PLUGIN_UNAVAILABLE", "Plugin must be stopped before removal", category="lifecycle")
        self.unregister(instance)
        instance.transition(LifecycleState.REMOVING)
        instance.transition(LifecycleState.ABSENT)
        self.instances.pop(instance.instance_id, None)
