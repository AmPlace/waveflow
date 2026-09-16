#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import uuid


def read_frame():
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        if line == b"\r\n":
            break
        name, value = line.decode("ascii").rstrip("\r\n").split(": ", 1)
        headers[name.lower()] = value
    return json.loads(sys.stdin.buffer.read(int(headers["content-length"])).decode("utf-8"))


write_lock = threading.Lock()


def write_frame(payload):
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    with write_lock:
        sys.stdout.buffer.write(
            f"Content-Length: {len(body)}\r\nContent-Type: application/json; charset=utf-8\r\n\r\n".encode("ascii") + body
        )
        sys.stdout.buffer.flush()


def response(request, result=None, *, request_id=None, error=None):
    return {
        "protocol_version": "1.0",
        "request_id": request_id or request["request_id"],
        "status": "error" if error else "ok",
        "result": None if error else result,
        "error": error,
        "diagnostics": {},
    }


callback_lock = threading.Lock()
callbacks = {}


def capability(parent, method, payload):
    request_id = f"plugin:{uuid.uuid4().hex}"
    event = threading.Event()
    with callback_lock:
        callbacks[request_id] = [event, None]
    write_frame({
        "protocol_version": "1.1", "kind": "request", "sender": "plugin",
        "request_id": request_id, "method": method,
        "plugin_instance": parent.get("plugin_instance"),
        "deadline_unix_ms": int((time.time() + 5) * 1000),
        "context": {"parent_request_id": parent["request_id"]}, "payload": payload,
    })
    if not event.wait(5):
        raise RuntimeError("capability timeout")
    with callback_lock:
        result = callbacks.pop(request_id)[1]
    if result.get("status") == "error":
        raise CapabilityError(result.get("error") or {})
    return result.get("result")


class CapabilityError(Exception):
    def __init__(self, error):
        self.error = error


def nested_resolve(request, mode):
    try:
        if mode == "nested_crash":
            request_id = f"plugin:{uuid.uuid4().hex}"
            write_frame({
                "protocol_version": "1.1", "kind": "request", "sender": "plugin",
                "request_id": request_id, "method": "core.http.fetch",
                "plugin_instance": request.get("plugin_instance"),
                "deadline_unix_ms": int((time.time() + 5) * 1000),
                "context": {"parent_request_id": request["request_id"]},
                "payload": {"method": "GET", "url": "https://api.example/resolve", "response_mode": "json"},
            })
            time.sleep(0.03)
            os._exit(24)
        result = capability(request, "core.http.fetch", {
            "method": "GET", "url": request["payload"].get("fixture_url", "https://api.example/resolve"),
            "response_mode": "json",
        })
        stream = descriptor()
        stream["url"] = result["body"]["url"]
        write_frame(response(request, stream))
    except CapabilityError as exc:
        write_frame(response(request, error=exc.error))
    except Exception:
        write_frame(response(request, error={"code": "TEMPORARY_UPSTREAM_FAILURE",
                                             "message": "Capability callback failed", "retryable": True,
                                             "category": "capability", "details": {}}))


def descriptor(transport="hls"):
    suffix = {"hls": "m3u8", "dash": "mpd", "http_flv": "flv", "mpegts": "ts",
              "rtsp": "stream", "audio_http": "mp3", "probe_only": ""}[transport]
    url = "" if transport == "probe_only" else (
        f"rtsp://example.invalid/{suffix}" if transport == "rtsp" else f"https://example.invalid/live.{suffix}"
    )
    return {
        "descriptor_version": "1.0", "transport": transport, "url": url,
        "headers": {"Referer": "https://example.invalid/"}, "credential_refs": [],
        "ttl_seconds": 120, "expires_at": None, "volatile_url": True,
        "requires_proxy": False, "warnings": [],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", default="normal")
    parser.add_argument("--identity", default="org.waveflow/synthetic")
    parser.add_argument("--version", default="1.0.0")
    parser.add_argument("--scheme", default="synthetic")
    parser.add_argument("--schemes", default="")
    parser.add_argument("--permissions", default="")
    parser.add_argument("--tv-only", action="store_true")
    parser.add_argument("--radio-owned", action="store_true")
    args = parser.parse_args()
    schemes = [value for value in args.schemes.split(",") if value] or [args.scheme]
    deferred = {}
    metadata_requests = 0
    catalog_requests = 0
    while True:
        request = read_frame()
        if request is None:
            return
        if request.get("kind") == "response" and request.get("sender") == "core":
            with callback_lock:
                callback = callbacks.get(request.get("request_id"))
                if callback:
                    callback[1] = request
                    callback[0].set()
            continue
        method = request["method"]
        if method == "runtime.hello":
            permissions = [v for v in args.permissions.split(",") if v]
            if args.mode == "hello_escalation":
                permissions.append("subprocess")
            provider_contracts = [
                {"contract": "tv_provider", "contract_version": "1.0", "features": ["resolve_stream"]},
            ]
            capabilities = ["tv.resolve_stream"]
            if args.mode.startswith("catalog"):
                provider_contracts.append(
                    {"contract": "channel_catalog", "contract_version": "1.0", "features": ["discover"]}
                )
                capabilities.append("channel_catalog.discover")
            if not args.tv_only:
                provider_contracts.append(
                    {"contract": "radio_provider", "contract_version": "1.0", "features": ["catalog", "resolve_stream"]}
                )
                capabilities.extend(["radio.catalog", "radio.resolve_stream"])
            result = {
                "protocol_version": "1.1" if args.mode.startswith("nested_") else "1.0",
                "plugin": args.identity, "version": args.version,
                "provider_contracts": provider_contracts,
                "owned_schemes": [{
                    "scheme": scheme,
                    "contract": "radio_provider" if args.radio_owned else "tv_provider",
                } for scheme in schemes],
                "capabilities": capabilities,
                "permissions": permissions,
            }
            write_frame(response(request, result))
        elif method == "runtime.health":
            write_frame(response(request, {"healthy": args.mode != "health_fail"}))
        elif method == "runtime.shutdown":
            write_frame(response(request, {"accepted": True}))
            return
        elif method == "runtime.cancel":
            target = request["payload"].get("request_id")
            if target in deferred:
                write_frame(response(deferred.pop(target), descriptor()))
            # Cancellation is a notification: no response is required by the fixture.
        elif method == "tv.resolve_stream":
            if args.mode == "crash":
                os._exit(23)
            if args.mode == "metadata_then_crash":
                metadata_requests += 1
                if metadata_requests == 2:
                    os._exit(23)
            if args.mode == "hang":
                deferred[request["request_id"]] = request
                continue
            if args.mode == "malformed_frame":
                sys.stdout.buffer.write(b"garbage\r\n\r\n")
                sys.stdout.buffer.flush()
                continue
            if args.mode in {"nested_http", "nested_crash", "nested_error"}:
                threading.Thread(target=nested_resolve, args=(request, args.mode), daemon=True).start()
                continue
            if args.mode == "ambient_probe":
                result = descriptor()
                result["provider_diagnostics"] = {
                    "cwd": os.getcwd(), "secret": os.environ.get("WAVEFLOW_TEST_SECRET", ""),
                    "path": os.environ.get("PATH", ""),
                }
                write_frame(response(request, result))
                continue
            result = descriptor(request["payload"].get("transport", "hls"))
            if args.mode in {"metadata", "metadata_then_crash"}:
                result["provider_diagnostics"] = {
                    "identity": {"channel": "fixture", "video": "v-1"},
                    "page_live": True,
                }
                result["probe_hints"] = {
                    "preferred_probe": "http_segment",
                    "alternates": ["ffmpeg", {"reason": "fixture"}],
                }
            if args.mode == "invalid_descriptor":
                result["headers"] = {"Authorization": "redacted-fixture"}
            if args.mode == "wrong_request_id":
                write_frame(response(request, result, request_id="unknown-request"))
                continue
            write_frame(response(request, result))
            if args.mode == "duplicate_response":
                write_frame(response(request, result))
        elif method == "channel_catalog.discover":
            catalog_requests += 1
            if args.mode == "catalog_failure":
                write_frame(response(request, error={
                    "code": "TEMPORARY_UPSTREAM_FAILURE", "message": "catalog fixture failed",
                    "retryable": True, "category": "provider", "details": {},
                }))
                continue
            if args.mode == "catalog_then_crash" and catalog_requests == 2:
                os._exit(25)
            if args.mode == "catalog_foreign":
                items = [{"external_id": "foreign", "name": "Foreign", "reference": "other://x",
                          "kind": "channel", "ttl_seconds": 300}]
            elif args.mode == "catalog_duplicate":
                items = [
                    {"external_id": "same", "name": "A", "reference": f"{args.scheme}://a",
                     "kind": "channel", "ttl_seconds": 300},
                    {"external_id": "same", "name": "B", "reference": f"{args.scheme}://b",
                     "kind": "channel", "ttl_seconds": 300},
                ]
            else:
                items = [
                    {"external_id": "event-1", "name": "Fixture Event", "reference": "synthetic://event-1",
                     "kind": "event", "group": "fixtures", "starts_at": 100, "ends_at": 200,
                     "ttl_seconds": 300, "metadata": {"page_live": True, "identity": {"source": "fixture"}}},
                    {"external_id": "channel-2", "name": "Fixture Channel", "reference": "synthetic://channel-2",
                     "kind": "channel", "ttl_seconds": 300},
                ]
            write_frame(response(request, {"items": items}))
        elif method == "radio.catalog":
            write_frame(response(request, {"stations": [
                {"station_ref": {"provider_key": "synthetic", "provider_station_id": "one"}, "name": "Same Name"},
                {"station_ref": {"provider_key": "synthetic", "provider_station_id": "two"}, "name": "Same Name"},
            ], "next_cursor": None}))
        elif method == "radio.resolve_stream":
            write_frame(response(request, descriptor("audio_http")))
        else:
            write_frame(response(request, error={"code": "RESOURCE_NOT_FOUND", "message": "Unknown method",
                                                    "retryable": False, "category": "request", "details": {}}))


if __name__ == "__main__":
    main()
