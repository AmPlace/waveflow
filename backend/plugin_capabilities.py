from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
from collections import defaultdict, deque
from typing import Any
from urllib.parse import urljoin

import httpx

from plugin_runtime import PluginError
from plugin_runtime.permissions import PermissionGate
from ssrf_guard import UnsafeTargetError, assert_safe_target_url


MAX_HTTP_REQUEST_BYTES = 256 * 1024
MAX_HTTP_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_HTTP_REDIRECTS = 5
MAX_CACHE_VALUE_BYTES = 256 * 1024
MAX_CAPABILITY_CALLS = 8
MAX_HTTP_CALLS = 4
_SENSITIVE = re.compile(r"authorization|cookie|token|secret|password|signature|api[-_]?key", re.I)


class CapabilityGateway:
    def __init__(self, *, client: httpx.AsyncClient, max_response_bytes: int = MAX_HTTP_RESPONSE_BYTES):
        self.client = client
        self.max_response_bytes = max_response_bytes
        self._cache: dict[str, dict[str, tuple[Any, float | None]]] = {}
        self._config: dict[str, dict[str, Any]] = {}
        self._log_events: dict[str, deque[float]] = defaultdict(deque)

    async def managed_http_fetch(self, identity: str, gate: PermissionGate, request: dict[str, Any],
                                 *, timeout: float) -> dict[str, Any]:
        policy = gate.require("network")
        if isinstance(policy, dict) and policy.get("managed") is False:
            raise PluginError("CAPABILITY_DENIED", "Managed network is not permitted", category="permission")
        if not isinstance(request, dict):
            raise _invalid("HTTP request must be an object")
        method = request.get("method", "GET")
        url = request.get("url")
        headers = request.get("headers", {})
        params = request.get("query")
        mode = request.get("response_mode", "text")
        body = request.get("body")
        if (method not in {"GET", "POST"} or not isinstance(url, str) or not url
                or not isinstance(headers, dict) or (params is not None and not isinstance(params, dict))
                or mode not in {"text", "json", "binary_base64"}
                or any(not isinstance(k, str) or not isinstance(v, (str, int, float, bool))
                       for k, v in (params or {}).items())
                or len(headers) > 32
                or any(not isinstance(k, str) or not k or not isinstance(v, str) or len(k) > 128 or len(v) > 8192
                       for k, v in headers.items())):
            raise _invalid("Invalid managed HTTP request")
        content: bytes | str | None = None
        json_body: Any = None
        if body is not None:
            if isinstance(body, dict) and set(body) == {"json"}:
                json_body = body["json"]
                try:
                    encoded = json.dumps(json_body, separators=(",", ":")).encode()
                except (TypeError, ValueError) as exc:
                    raise _invalid("HTTP JSON body is invalid") from exc
            elif isinstance(body, dict) and set(body) == {"text"} and isinstance(body["text"], str):
                content = body["text"]
                encoded = content.encode()
            else:
                raise _invalid("HTTP body must use text or json encoding")
            if len(encoded) > MAX_HTTP_REQUEST_BYTES:
                raise _invalid("HTTP request body exceeds the limit")
        allow_private = bool(policy.get("allow_private")) if isinstance(policy, dict) else False
        # Plain HTTP is a separately declared, separately approved managed
        # capability.  It only changes the accepted transport scheme; all
        # target, DNS, redirect, host, size, and timeout checks remain active.
        allow_http = bool(policy.get("allow_http")) if isinstance(policy, dict) else False
        allowed_hosts = policy.get("allowed_hosts") if isinstance(policy, dict) else None
        current = url
        for redirect_count in range(MAX_HTTP_REDIRECTS + 1):
            await self._validate_http_target(
                current, allow_private=allow_private, allowed_hosts=allowed_hosts, allow_http=allow_http,
            )
            try:
                async with self.client.stream(
                    method, current, headers=headers, params=params, content=content, json=json_body,
                    follow_redirects=False, timeout=min(max(timeout, 0.01), 30.0),
                ) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location or redirect_count >= MAX_HTTP_REDIRECTS:
                            raise PluginError("TEMPORARY_UPSTREAM_FAILURE", "Managed HTTP redirect failed",
                                              retryable=True, category="network")
                        current = urljoin(current, location)
                        if response.status_code in {301, 302, 303}:
                            method, content, json_body = "GET", None, None
                        params = None
                        continue
                    if response.status_code in {401, 403}:
                        raise PluginError("AUTH_FAILED", "Managed HTTP authentication failed", category="auth")
                    if response.status_code == 429:
                        raise PluginError("RATE_LIMITED", "Managed HTTP upstream rate limited the request",
                                          retryable=True, category="network")
                    if response.status_code >= 500:
                        raise PluginError("TEMPORARY_UPSTREAM_FAILURE", "Managed HTTP upstream failed",
                                          retryable=True, category="network")
                    if response.status_code >= 400:
                        raise PluginError("TEMPORARY_UPSTREAM_FAILURE", "Managed HTTP upstream rejected the request",
                                          retryable=True, category="network")
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > self.max_response_bytes:
                            raise PluginError("INVALID_CAPABILITY_REQUEST", "Managed HTTP response exceeds the limit",
                                              category="capability")
                    selected_headers = {key: value for key, value in response.headers.items()
                                        if key.lower() in {"content-type", "etag", "last-modified", "cache-control"}}
                    result: Any
                    if mode == "binary_base64":
                        result = base64.b64encode(data).decode("ascii")
                    else:
                        try:
                            text = bytes(data).decode(response.encoding or "utf-8")
                        except (UnicodeDecodeError, LookupError) as exc:
                            raise PluginError("INVALID_CAPABILITY_REQUEST", "Managed HTTP response is not text",
                                              category="capability") from exc
                        if mode == "json":
                            try:
                                result = json.loads(text)
                            except json.JSONDecodeError as exc:
                                raise PluginError("INVALID_CAPABILITY_REQUEST", "Managed HTTP response is not JSON",
                                                  category="capability") from exc
                        else:
                            result = text
                    return {"status": response.status_code, "headers": selected_headers,
                            "body": result, "response_mode": mode}
            except PluginError:
                raise
            except (httpx.TimeoutException, asyncio.TimeoutError) as exc:
                raise PluginError("PLUGIN_TIMEOUT", "Managed HTTP request timed out", retryable=True,
                                  category="timeout") from exc
            except httpx.HTTPError as exc:
                raise PluginError("TEMPORARY_UPSTREAM_FAILURE", "Managed HTTP request failed", retryable=True,
                                  category="network") from exc
        raise PluginError("TEMPORARY_UPSTREAM_FAILURE", "Managed HTTP redirect limit exceeded", retryable=True,
                          category="network")

    async def managed_http_get(self, identity: str, gate: PermissionGate, url: str) -> dict[str, Any]:
        result = await self.managed_http_fetch(identity, gate, {"url": url, "response_mode": "binary_base64"}, timeout=30)
        return {"status": result["status"], "body": base64.b64decode(result["body"]),
                "content_type": result["headers"].get("content-type", "")}

    async def _validate_http_target(self, url: str, *, allow_private: bool, allowed_hosts: Any,
                                    allow_http: bool = False) -> None:
        try:
            await assert_safe_target_url(url, allow_private=allow_private, allow_loopback=False,
                                         allowed_schemes={"http", "https"} if allow_http else {"https"})
        except UnsafeTargetError as exc:
            raise PluginError("CAPABILITY_DENIED", "Plugin network target is not permitted",
                              category="permission") from exc
        if allowed_hosts is not None:
            from urllib.parse import urlparse
            host = (urlparse(url).hostname or "").lower()
            if (not isinstance(allowed_hosts, list) or any(not isinstance(item, str) for item in allowed_hosts)
                    or not any(host == item.lower() or host.endswith("." + item.lower()) for item in allowed_hosts)):
                raise PluginError("CAPABILITY_DENIED", "Plugin network host is not permitted", category="permission")

    def log(self, identity: str, level: str, message: str, fields: dict[str, Any] | None = None) -> None:
        if level not in {"debug", "info", "warning", "error"} or not isinstance(message, str):
            raise _invalid("Invalid structured log event")
        now = time.monotonic()
        events = self._log_events[identity]
        while events and now - events[0] > 60:
            events.popleft()
        if len(events) >= 120:
            raise PluginError("RATE_LIMITED", "Plugin logging rate limit exceeded", retryable=True, category="rate_limit")
        events.append(now)
        safe_fields = _redact(fields or {})
        getattr(logging.getLogger("waveflow.plugin"), level)(
            "plugin_event plugin=%s message=%s fields=%s", identity, _redact_text(message[:500]), safe_fields
        )

    def cache_get(self, identity: str, gate: PermissionGate, key: str, *, allow_stale: bool = False) -> dict[str, Any]:
        gate.require("cache")
        _validate_key(key)
        entry = self._cache.setdefault(identity, {}).get(key)
        if entry is None:
            return {"found": False, "value": None, "stale": False}
        value, expires_at = entry
        stale = expires_at is not None and expires_at <= time.monotonic()
        if stale and not allow_stale:
            self._cache[identity].pop(key, None)
            return {"found": False, "value": None, "stale": False}
        return {"found": True, "value": value, "stale": stale}

    def cache_set(self, identity: str, gate: PermissionGate, key: str, value: Any,
                  *, ttl_seconds: float | None = None) -> dict[str, Any]:
        gate.require("cache")
        _validate_key(key)
        try:
            size = len(json.dumps(value, separators=(",", ":")).encode())
        except (TypeError, ValueError) as exc:
            raise _invalid("Cache value is not JSON serializable") from exc
        if size > MAX_CACHE_VALUE_BYTES or (ttl_seconds is not None and (not isinstance(ttl_seconds, (int, float))
                                                                          or ttl_seconds <= 0 or ttl_seconds > 86400)):
            raise _invalid("Invalid cache value or TTL")
        expires_at = None if ttl_seconds is None else time.monotonic() + float(ttl_seconds)
        self._cache.setdefault(identity, {})[key] = (value, expires_at)
        return {"stored": True}

    def cache_delete(self, identity: str, gate: PermissionGate, key: str) -> dict[str, Any]:
        gate.require("cache")
        _validate_key(key)
        existed = self._cache.setdefault(identity, {}).pop(key, None) is not None
        return {"deleted": existed}

    def scoped_config(self, identity: str) -> dict[str, Any]:
        return dict(self._config.get(identity, {}))

    def config_get(self, identity: str, key: str) -> dict[str, Any]:
        _validate_key(key)
        values = self._config.get(identity, {})
        return {"found": key in values, "value": values.get(key)}

    def set_scoped_config(self, identity: str, values: dict[str, Any]) -> None:
        self._config[identity] = dict(values)


class CoreCapabilityDispatcher:
    def __init__(self, gateway: CapabilityGateway, *, max_concurrent: int = MAX_CAPABILITY_CALLS,
                 max_http_concurrent: int = MAX_HTTP_CALLS):
        self.gateway = gateway
        self.max_concurrent = max_concurrent
        self.max_http_concurrent = max_http_concurrent
        self._all: dict[str, asyncio.Semaphore] = {}
        self._http: dict[str, asyncio.Semaphore] = {}
        self._closed = False

    async def dispatch(self, identity: str, gate: PermissionGate, method: str, payload: dict[str, Any],
                       *, timeout: float, context: dict[str, Any] | None = None) -> Any:
        if self._closed:
            raise PluginError("PLUGIN_UNAVAILABLE", "Capability dispatcher is shutting down", category="lifecycle")
        if not method.startswith("core.") or not isinstance(payload, dict):
            raise _invalid()
        semaphore = self._all.setdefault(identity, asyncio.Semaphore(self.max_concurrent))
        if semaphore.locked():
            raise PluginError("RATE_LIMITED", "Plugin capability concurrency limit reached", retryable=True,
                              category="rate_limit")
        async with semaphore:
            if method == "core.http.fetch":
                network = gate.require("network")
                if not isinstance(network, dict) or network.get("managed") is not True:
                    raise PluginError("CAPABILITY_DENIED", "Managed network permission is required",
                                      category="permission", details={"capability": "network.managed"})
                http_sem = self._http.setdefault(identity, asyncio.Semaphore(self.max_http_concurrent))
                if http_sem.locked():
                    raise PluginError("RATE_LIMITED", "Plugin HTTP concurrency limit reached", retryable=True,
                                      category="rate_limit")
                async with http_sem:
                    return await self.gateway.managed_http_fetch(identity, gate, payload, timeout=timeout)
            if method == "core.cache.get":
                return self.gateway.cache_get(identity, gate, payload.get("key"),
                                              allow_stale=payload.get("allow_stale") is True)
            if method == "core.cache.set":
                return self.gateway.cache_set(identity, gate, payload.get("key"), payload.get("value"),
                                              ttl_seconds=payload.get("ttl_seconds"))
            if method == "core.cache.delete":
                return self.gateway.cache_delete(identity, gate, payload.get("key"))
            if method == "core.config.get":
                return self.gateway.config_get(identity, payload.get("key"))
            if method == "core.log":
                self.gateway.log(identity, payload.get("level", "info"), payload.get("message"), payload.get("fields"))
                return {"accepted": True}
            if method == "core.secret.get":
                raise PluginError("CAPABILITY_DENIED", "Secret capability is not available", category="permission")
            raise _invalid("Unknown Core capability method")

    def close(self) -> None:
        self._closed = True


def _invalid(message: str = "Invalid Core capability request") -> PluginError:
    return PluginError("INVALID_CAPABILITY_REQUEST", message, category="capability")


def _validate_key(key: Any) -> None:
    if not isinstance(key, str) or not key or len(key) > 200 or any(ord(ch) < 32 for ch in key):
        raise _invalid("Invalid capability key")


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: "[redacted]" if _SENSITIVE.search(str(key)) else _redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value[:50]]
    return _redact_text(str(value))[:500] if isinstance(value, str) else value


def _redact_text(value: str) -> str:
    value = re.sub(r"(?i)(authorization|cookie|token|secret|password|signature|api[-_]?key)=([^&\s]+)", r"\1=[redacted]", value)
    return value
