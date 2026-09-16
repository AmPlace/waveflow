#!/usr/bin/env python3
"""Independent GZSTV Provider Plugin using managed HTTP."""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import uuid
from typing import Any
from urllib.parse import parse_qs, urlparse


API_BASE = "https://api.gzstv.com/v1/tv"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
CHANNELS = {
    "ch01": "贵州卫视",
    "ch02": "公共频道",
    "ch03": "影视文艺频道",
    "ch04": "大众生活频道",
    "ch05": "生态·乡村频道",
    "ch06": "科教健康频道",
    "ch13": "贵州移动电视",
}
WRITE_LOCK = threading.Lock()
CALLBACK_LOCK = threading.Lock()
CALLBACKS: dict[str, tuple[threading.Event, dict[str, Any] | None]] = {}


def frame(value: dict[str, Any]) -> None:
    body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    with WRITE_LOCK:
        sys.stdout.buffer.write(
            f"Content-Length: {len(body)}\r\nContent-Type: application/json; charset=utf-8\r\n\r\n".encode()
            + body
        )
        sys.stdout.buffer.flush()


def read_frame() -> dict[str, Any] | None:
    headers: dict[str, str] = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        if line == b"\r\n":
            break
        name, value = line.decode("ascii").rstrip("\r\n").split(": ", 1)
        headers[name.lower()] = value
    return json.loads(sys.stdin.buffer.read(int(headers["content-length"])).decode())


def reply(request: dict[str, Any], result: Any = None, error: dict[str, Any] | None = None) -> None:
    frame({"protocol_version": "1.1", "kind": "response", "sender": "plugin",
           "request_id": request["request_id"], "status": "error" if error else "ok",
           "result": result, "error": error, "diagnostics": {}})


def provider_error(code: str, message: str, *, retryable: bool = False,
                   category: str = "provider") -> dict[str, Any]:
    return {"code": code, "message": message, "retryable": retryable,
            "category": category, "details": {}}


def fetch(request: dict[str, Any], channel_id: str) -> dict[str, Any]:
    callback_id = f"plugin:{uuid.uuid4().hex}"
    event = threading.Event()
    with CALLBACK_LOCK:
        CALLBACKS[callback_id] = (event, None)
    frame({"protocol_version": "1.1", "kind": "request", "sender": "plugin",
           "request_id": callback_id, "method": "core.http.fetch",
           "plugin_instance": request.get("plugin_instance"),
           "deadline_unix_ms": int((time.time() + 10) * 1000),
           "context": {"parent_request_id": request["request_id"]},
           "payload": {"method": "GET", "url": f"{API_BASE}/{channel_id}/",
                       "query": {"fields": "description,title,stream_url,image,author"},
                       "headers": {"Accept": "application/json, text/plain, */*",
                                   "Origin": "https://www.gzstv.com",
                                   "Referer": f"https://www.gzstv.com/tv/{channel_id}",
                                   "User-Agent": USER_AGENT},
                       "response_mode": "json"}})
    if not event.wait(10):
        with CALLBACK_LOCK:
            CALLBACKS.pop(callback_id, None)
        raise RuntimeError("PLUGIN_TIMEOUT")
    with CALLBACK_LOCK:
        response = CALLBACKS.pop(callback_id)[1] or {}
    if response.get("status") == "error":
        raise RuntimeError(str((response.get("error") or {}).get("code") or "TEMPORARY_UPSTREAM_FAILURE"))
    return response.get("result") or {}


def expiry_from_url(url: str) -> int | None:
    value = (parse_qs(urlparse(url).query).get("txTime") or [""])[0].strip()
    if not value:
        return None
    try:
        return int(value, 16)
    except ValueError:
        return None


def resolve(request: dict[str, Any], now: float | None = None) -> None:
    resource = str((request.get("payload") or {}).get("resource_id") or "").strip("/").lower()
    channel_id = f"ch{int(resource):02d}" if resource.isdigit() else resource
    channel_name = CHANNELS.get(channel_id)
    if channel_name is None:
        reply(request, error=provider_error("RESOURCE_NOT_FOUND", "GZSTV channel is not supported"))
        return
    try:
        result = fetch(request, channel_id)
    except RuntimeError as exc:
        code = str(exc)
        stable = code if code in {"CAPABILITY_DENIED", "PLUGIN_TIMEOUT", "AUTH_FAILED", "RATE_LIMITED"} else "TEMPORARY_UPSTREAM_FAILURE"
        reply(request, error=provider_error(stable, "GZSTV upstream request failed", retryable=True,
                                            category="network"))
        return
    payload = result.get("body")
    url = str(payload.get("stream_url") or "").strip() if isinstance(payload, dict) else ""
    if not url.startswith(("http://", "https://")):
        reply(request, error=provider_error("TEMPORARY_UPSTREAM_FAILURE",
                                            f"GZSTV {channel_name} returned no playable stream", retryable=True))
        return
    current = time.time() if now is None else now
    expires_at = expiry_from_url(url)
    ttl = max(0, int(expires_at - current - 60)) if expires_at else 300
    reply(request, {"descriptor_version": "1.0", "transport": "hls", "url": url,
                    "headers": {"Referer": "https://www.gzstv.com/", "User-Agent": USER_AGENT},
                    "credential_refs": [], "ttl_seconds": ttl, "expires_at": expires_at,
                    "volatile_url": True, "requires_proxy": False, "warnings": []})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--identity", default="org.waveflow/gzstv")
    parser.add_argument("--version", default="1.0.0")
    parser.add_argument("--fixed-time", type=float)
    args = parser.parse_args()
    while True:
        request = read_frame()
        if request is None:
            return
        if request.get("kind") == "response" and request.get("sender") == "core":
            with CALLBACK_LOCK:
                pending = CALLBACKS.get(request.get("request_id"))
                if pending:
                    CALLBACKS[request["request_id"]] = (pending[0], request)
                    pending[0].set()
            continue
        method = request.get("method")
        if method == "runtime.hello":
            reply(request, {"protocol_version": "1.1", "plugin": args.identity, "version": args.version,
                "provider_contracts": [{"contract": "tv_provider", "contract_version": "1.0", "features": ["resolve_stream"]}],
                "owned_schemes": [{"scheme": "gzstv", "contract": "tv_provider"}],
                "capabilities": ["tv.resolve_stream"], "permissions": ["network"]})
        elif method == "runtime.health":
            reply(request, {"healthy": True})
        elif method == "runtime.shutdown":
            reply(request, {"accepted": True})
            return
        elif method == "tv.resolve_stream":
            threading.Thread(target=resolve, args=(request, args.fixed_time), daemon=True).start()
        else:
            reply(request, error=provider_error("RESOURCE_NOT_FOUND", "Provider method is not supported"))


if __name__ == "__main__":
    main()
