from __future__ import annotations

from typing import Any


class PluginError(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool = False,
                 category: str = "provider", details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.category = category
        self.details = dict(details or {})

    def as_contract(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message[:500], "retryable": self.retryable,
                "category": self.category, "details": self.details}


class InvalidResource(PluginError):
    def __init__(self, message: str = "Provider resource is not supported"):
        super().__init__("RESOURCE_NOT_FOUND", message)


class NotLive(PluginError):
    def __init__(self, message: str = "Provider resource is not live"):
        super().__init__("NOT_LIVE", message, retryable=True)


class UpstreamFailure(PluginError):
    def __init__(self, message: str = "Provider upstream request failed"):
        super().__init__("TEMPORARY_UPSTREAM_FAILURE", message, retryable=True, category="network")


class TemporaryFailure(PluginError):
    def __init__(self, message: str = "Provider request temporarily failed"):
        super().__init__("TEMPORARY_UPSTREAM_FAILURE", message, retryable=True)


class AuthFailure(PluginError):
    def __init__(self, message: str = "Provider authentication failed"):
        super().__init__("AUTH_FAILED", message, category="auth")


class RateLimited(PluginError):
    def __init__(self, message: str = "Provider upstream rate limited the request"):
        super().__init__("RATE_LIMITED", message, retryable=True, category="network")
