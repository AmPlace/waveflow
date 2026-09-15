from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .errors import PluginError


#: Core capabilities exercised by official Plugins.  Part of SDK V1.
V1_CAPABILITY_METHODS = frozenset({"core.http.fetch"})

#: Implemented seams that no official Plugin adopts yet.  ``core.secret.get``
#: is a reserved, always-denied seam; ``core.config.get`` has no production
#: population path in V1; the cache is in-process volatile state.  These are
#: not part of SDK V1 and may change without a major version.
PREVIEW_CAPABILITY_METHODS = frozenset({
    "core.cache.get", "core.cache.set", "core.cache.delete",
    "core.config.get", "core.log", "core.secret.get",
})


@dataclass(frozen=True)
class CapabilityResponse:
    status: int
    headers: dict[str, str]
    body: Any
    response_mode: str


class CapabilityClient:
    def __init__(self, invoke: Callable[[str, dict[str, Any], float], Any]):
        self._invoke = invoke

    def call(self, method: str, payload: dict[str, Any], *, timeout: float = 10.0) -> Any:
        return self._invoke(method, payload, timeout)

    def managed_http(self, url: str, *, method: str = "GET", headers: dict[str, str] | None = None,
                     query: dict[str, Any] | None = None, json_body: Any = None, text_body: str | None = None,
                     response_mode: str = "text", timeout: float = 10.0) -> CapabilityResponse:
        """Perform a policy-bound HTTP request through Core (SDK V1).

        Requires the ``network.managed`` manifest permission.  Target hosts,
        redirects, response size and timeouts are enforced by Core, not by the
        Plugin.
        """
        if json_body is not None and text_body is not None:
            raise ValueError("managed_http accepts either json_body or text_body")
        payload: dict[str, Any] = {"method": method, "url": url, "headers": dict(headers or {}),
                                   "response_mode": response_mode}
        if query is not None:
            payload["query"] = dict(query)
        if json_body is not None:
            payload["body"] = {"json": json_body}
        elif text_body is not None:
            payload["body"] = {"text": text_body}
        result = self.call("core.http.fetch", payload, timeout=timeout)
        return CapabilityResponse(int(result.get("status", 0)), dict(result.get("headers") or {}),
                                  result.get("body"), str(result.get("response_mode") or response_mode))

    # --- preview seams (not SDK V1) -------------------------------------
    # The methods below are reachable but unvalidated by official Plugins.
    # See ``PREVIEW_CAPABILITY_METHODS``.

    def cache_get(self, key: str, *, allow_stale: bool = False) -> dict[str, Any]:
        """Preview. In-process volatile cache entry; not durable across restart."""
        return self.call("core.cache.get", {"key": key, "allow_stale": allow_stale})

    def cache_set(self, key: str, value: Any, *, ttl_seconds: float | None = None) -> dict[str, Any]:
        """Preview. In-process volatile cache entry; not durable across restart."""
        return self.call("core.cache.set", {"key": key, "value": value, "ttl_seconds": ttl_seconds})

    def cache_delete(self, key: str) -> dict[str, Any]:
        """Preview. In-process volatile cache entry; not durable across restart."""
        return self.call("core.cache.delete", {"key": key})

    def config_get(self, key: str) -> dict[str, Any]:
        """Preview. V1 Core provides no production Plugin configuration store."""
        return self.call("core.config.get", {"key": key})

    def log(self, level: str, message: str, **fields: Any) -> None:
        """Preview. Structured log event, redacted and rate-limited by Core."""
        self.call("core.log", {"level": level, "message": message, "fields": fields})

    def secret_get(self, name: str) -> Any:
        """Preview. Reserved seam; V1 Core denies ``core.secret.get``."""
        return self.call("core.secret.get", {"name": name})


def capability_error(error: dict[str, Any]) -> PluginError:
    return PluginError(str(error.get("code") or "TEMPORARY_UPSTREAM_FAILURE"),
                       str(error.get("message") or "Core capability failed"),
                       retryable=bool(error.get("retryable")),
                       category=str(error.get("category") or "capability"),
                       details=error.get("details") if isinstance(error.get("details"), dict) else {})
