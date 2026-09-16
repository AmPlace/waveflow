from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any


ERROR_CODES = frozenset({
    "DEPENDENCY_MISSING",
    "PLUGIN_UNAVAILABLE", "PLUGIN_INCOMPATIBLE", "PLUGIN_CRASHED", "PLUGIN_TIMEOUT", "PLUGIN_QUARANTINED",
    "SCHEME_UNOWNED", "SCHEME_CONFLICT",
    "AUTH_REQUIRED", "AUTH_FAILED", "RATE_LIMITED", "REGION_BLOCKED",
    "RESOURCE_NOT_FOUND", "NOT_LIVE", "TEMPORARY_UPSTREAM_FAILURE",
    "CAPABILITY_DENIED", "INVALID_CAPABILITY_REQUEST", "INVALID_PLUGIN_RESPONSE",
    "ARTIFACT_NOT_FOUND", "ARTIFACT_INVALID", "ARTIFACT_INTEGRITY_FAILED",
    "ARTIFACT_SIGNATURE_INVALID", "PLUGIN_UNTRUSTED", "PLATFORM_UNSUPPORTED",
    "PLUGIN_CANDIDATE_CONFLICT",
    "PYTHON_RUNTIME_UNSUPPORTED", "DEPENDENCY_LOCK_INVALID", "DEPENDENCY_ARTIFACT_NOT_FOUND",
    "DEPENDENCY_ARTIFACT_INTEGRITY_FAILED", "DEPENDENCY_PLATFORM_UNSUPPORTED",
    "DEPENDENCY_INSTALL_FAILED", "PLUGIN_ENVIRONMENT_INVALID",
    "PERMISSION_APPROVAL_REQUIRED",
    "DEVELOPER_MODE_REQUIRED",
})


@dataclass(eq=False)
class PluginError(Exception):
    code: str
    message: str
    retryable: bool = False
    category: str = "plugin"
    details: dict[str, Any] = field(default_factory=dict)
    internal_diagnostics: dict[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if self.code not in ERROR_CODES:
            self.code = "INVALID_PLUGIN_RESPONSE"
        Exception.__init__(self, self.message)

    def as_contract(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "category": self.category,
            "details": dict(self.details),
        }


def invalid_response(message: str = "Plugin returned an invalid response") -> PluginError:
    return PluginError("INVALID_PLUGIN_RESPONSE", message, category="protocol")
