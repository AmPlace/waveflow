#!/usr/bin/env python3
"""Independent SDTV Provider Plugin using managed HTTP and environment cryptography."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import sys
import threading
import time
import uuid
from typing import Any

import cryptography
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


PAGE_URL = "https://v.iqilu.com/live/sdtv/"
EXCHANGE_URL = "https://feiying.litenews.cn/api/v1/auth/exchange"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
           "Referer": "https://v.iqilu.com/", "Origin": "https://v.iqilu.com"}
CHANNELS = {"sdws": "24581", "sdql": "24584", "sdxw": "24602", "sdty": "24587",
            "sdsh": "24596", "sdzy": "24593", "sdnk": "24599", "sdwl": "24590", "sdse": "24605"}
WRITE_LOCK = threading.Lock()
CALLBACK_LOCK = threading.Lock()
CALLBACKS: dict[str, tuple[threading.Event, dict[str, Any] | None]] = {}


def frame(value: dict[str, Any]) -> None:
    body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    with WRITE_LOCK:
        sys.stdout.buffer.write(f"Content-Length: {len(body)}\r\nContent-Type: application/json; charset=utf-8\r\n\r\n".encode() + body)
        sys.stdout.buffer.flush()


def read_frame() -> dict[str, Any] | None:
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        if line == b"\r\n":
            break
        key, value = line.decode("ascii").rstrip("\r\n").split(": ", 1)
        headers[key.lower()] = value
    return json.loads(sys.stdin.buffer.read(int(headers["content-length"])).decode())


def reply(request: dict[str, Any], result: Any = None, error: dict[str, Any] | None = None) -> None:
    frame({"protocol_version": "1.1", "kind": "response", "sender": "plugin", "request_id": request["request_id"],
           "status": "error" if error else "ok", "result": result, "error": error, "diagnostics": {}})


def failure(code: str, message: str, category: str = "provider") -> dict[str, Any]:
    return {"code": code, "message": message, "retryable": code != "RESOURCE_NOT_FOUND",
            "category": category, "details": {}}


def fetch(request: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    callback_id = f"plugin:{uuid.uuid4().hex}"
    event = threading.Event()
    with CALLBACK_LOCK:
        CALLBACKS[callback_id] = (event, None)
    frame({"protocol_version": "1.1", "kind": "request", "sender": "plugin", "request_id": callback_id,
           "method": "core.http.fetch", "plugin_instance": request.get("plugin_instance"),
           "deadline_unix_ms": int((time.time() + 10) * 1000),
           "context": {"parent_request_id": request["request_id"]}, "payload": payload})
    if not event.wait(10):
        with CALLBACK_LOCK:
            CALLBACKS.pop(callback_id, None)
        raise RuntimeError("PLUGIN_TIMEOUT")
    with CALLBACK_LOCK:
        response = CALLBACKS.pop(callback_id)[1] or {}
    if response.get("status") == "error":
        raise RuntimeError(str((response.get("error") or {}).get("code") or "TEMPORARY_UPSTREAM_FAILURE"))
    return response.get("result") or {}


def aes_encrypt(text: str, key: str) -> bytes:
    key_bytes = key.encode()[:16].ljust(16, b"0")
    padder = padding.PKCS7(128).padder()
    padded = padder.update(text.encode()) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key_bytes), modes.CBC(b"0" * 16)).encryptor()
    return encryptor.update(padded) + encryptor.finalize()


def aes_decrypt(data: bytes, key: str) -> str:
    key_bytes = key.encode()[:16].ljust(16, b"0")
    decryptor = Cipher(algorithms.AES(key_bytes), modes.CBC(b"0" * 16)).decryptor()
    padded = decryptor.update(data) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    return (unpadder.update(padded) + unpadder.finalize()).decode()


def resolve(request: dict[str, Any], fixed_time: float | None = None) -> None:
    resource = str((request.get("payload") or {}).get("resource_id") or "").strip("/").lower()
    channel_id = CHANNELS.get(resource, resource if resource.isdigit() else "")
    if not channel_id:
        reply(request, error=failure("RESOURCE_NOT_FOUND", "SDTV channel is not supported"))
        return
    try:
        page = str(fetch(request, {"method": "GET", "url": PAGE_URL, "headers": HEADERS,
                                   "response_mode": "text"}).get("body") or "")
        salt_match = re.search(r"mxpx\s*=\s*'([^']+)'", page)
        key_match = re.search(r"aly\s*=\s*'([^']+)'", page)
        if not salt_match or not key_match:
            raise ValueError("missing salt/key")
        salt, aes_key = salt_match.group(1), key_match.group(1)
        timestamp = int((time.time() if fixed_time is None else fixed_time) * 1000)
        signature = hashlib.md5(f"{channel_id}{timestamp}{salt}".encode()).hexdigest()
        body = base64.b64encode(aes_encrypt(json.dumps({"channelMark": channel_id}), aes_key)).decode()
        exchange = fetch(request, {"method": "POST", "url": f"{EXCHANGE_URL}?t={timestamp}&s={signature}",
                                   "headers": {**HEADERS, "Content-Type": "text/plain"},
                                   "body": {"text": body}, "response_mode": "text"})
        decoded = json.loads(aes_decrypt(base64.b64decode(str(exchange.get("body") or "")), aes_key))
        url = str(decoded.get("data") or "").strip() if isinstance(decoded, dict) else ""
        if not url.startswith(("http://", "https://")):
            raise ValueError("missing stream")
    except RuntimeError as exc:
        code = str(exc)
        stable = code if code in {"CAPABILITY_DENIED", "PLUGIN_TIMEOUT", "AUTH_FAILED", "RATE_LIMITED"} else "TEMPORARY_UPSTREAM_FAILURE"
        reply(request, error=failure(stable, "SDTV upstream request failed", "network"))
        return
    except Exception:
        reply(request, error=failure("TEMPORARY_UPSTREAM_FAILURE", "SDTV response processing failed"))
        return
    reply(request, {"descriptor_version": "1.0", "transport": "hls", "url": url,
                    "headers": {"Referer": "https://v.iqilu.com/"}, "credential_refs": [],
                    "ttl_seconds": 1800, "expires_at": None, "volatile_url": True,
                    "requires_proxy": False, "warnings": [],
                    "provider_diagnostics": {"dependency_origin": cryptography.__file__}})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--identity", default="org.waveflow/sdtv")
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
                "owned_schemes": [{"scheme": "sdtv", "contract": "tv_provider"}],
                "capabilities": ["tv.resolve_stream"], "permissions": ["network"],
                "dependency_origin": cryptography.__file__})
        elif method == "runtime.health":
            reply(request, {"healthy": True})
        elif method == "runtime.shutdown":
            reply(request, {"accepted": True})
            return
        elif method == "tv.resolve_stream":
            threading.Thread(target=resolve, args=(request, args.fixed_time), daemon=True).start()
        else:
            reply(request, error=failure("RESOURCE_NOT_FOUND", "Provider method is not supported"))


if __name__ == "__main__":
    main()
