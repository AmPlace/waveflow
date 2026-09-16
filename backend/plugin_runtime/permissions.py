from __future__ import annotations
from dataclasses import dataclass
from typing import Any

from .errors import PluginError
from .manifest import PluginManifest


@dataclass(frozen=True)
class PermissionPolicy:
    allowed: frozenset[str] = frozenset()

    def allows(self, name: str) -> bool:
        return name in self.allowed


class PermissionGate:
    def __init__(self, manifest: PluginManifest, policy: PermissionPolicy):
        self.manifest = manifest
        self.policy = policy

    @property
    def declared(self) -> frozenset[str]:
        return frozenset(self.manifest.permissions)

    @property
    def granted(self) -> frozenset[str]:
        return frozenset(name for name in self.declared if self.policy.allows(name))

    def require(self, name: str) -> Any:
        if name not in self.declared or not self.policy.allows(name):
            raise PluginError("CAPABILITY_DENIED", "Plugin capability is not permitted", category="permission",
                              details={"capability": name})
        return self.manifest.permissions[name]

    def validate_hello(self, claimed: Any) -> None:
        if not isinstance(claimed, list) or any(not isinstance(v, str) for v in claimed):
            raise PluginError("INVALID_PLUGIN_RESPONSE", "Plugin hello contains invalid permissions", category="protocol")
        if not set(claimed).issubset(self.granted):
            raise PluginError("INVALID_PLUGIN_RESPONSE", "Plugin hello exceeds granted permissions", category="permission")


class MemoryCapabilityStubs:
    def __init__(self, gate: PermissionGate, *, config: dict[str, Any] | None = None,
                 secret_refs: dict[str, str] | None = None):
        self.gate = gate
        self.config = dict(config or {})
        self.secret_refs = dict(secret_refs or {})
        self.cache: dict[str, Any] = {}

    def cache_get(self, key: str) -> Any:
        self.gate.require("cache")
        return self.cache.get(key)

    def cache_set(self, key: str, value: Any) -> None:
        self.gate.require("cache")
        self.cache[key] = value

    def secret_ref(self, key: str) -> str:
        self.gate.require("secrets")
        if key not in self.secret_refs:
            raise PluginError("AUTH_REQUIRED", "Required plugin credential is unavailable", category="auth")
        return self.secret_refs[key]
