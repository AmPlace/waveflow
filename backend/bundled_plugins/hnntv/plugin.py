#!/usr/bin/env python3
"""Independent HNNTV Provider Plugin with direct live and managed replay HTTP."""
from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
import uuid
from datetime import datetime
from typing import Any, Callable
from urllib.parse import urlencode
from urllib.request import Request, urlopen


LIVE_URL = "http://ps.hnntv.cn/ps/livePlayUrl"
SCHEDULE_URL = "https://www.hnntv.cn/api/schedule/byDay"
REPLAY_URL = "https://ps.hnntv.cn/ps/wbPlayUrl"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
           "Referer": "https://www.hnntv.cn/"}
CHANNELS = {
    "hnws": {"code": "STHaiNan_channel_lywsgq", "channel_id": 13, "name": "海南卫视"},
    "ssws": {"code": "STHaiNan_channel_ssws", "channel_id": 5, "name": "三沙卫视"},
    "xwpd": {"code": "STHaiNan_channel_xwpd", "channel_id": 3, "name": "海南新闻频道"},
    "wlpd": {"code": "wlpd", "channel_id": 6, "name": "海南文旅频道"},
    "jjpd": {"code": "jjpd", "channel_id": 1, "name": "海南自贸频道"},
    "ggpd": {"code": "ggpd", "channel_id": 4, "name": "海南公共频道"},
    "sepd": {"code": "sepd", "channel_id": 7, "name": "海南少儿频道"},
}
URL_RE = re.compile(r'"url"\s*:\s*"([^"]+)"', re.IGNORECASE)
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


def failure(code: str, message: str, *, retryable: bool = True,
            category: str = "provider") -> dict[str, Any]:
    return {"code": code, "message": message, "retryable": retryable,
            "category": category, "details": {}}


def extract_url(payload: Any, raw_text: str = "") -> str:
    if isinstance(payload, dict):
        for key in ("url", "playUrl", "play_url", "m3u8", "m3u8Url"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for key in ("resultSet", "data", "result"):
            value = extract_url(payload.get(key))
            if value:
                return value
    if isinstance(payload, list):
        for item in payload:
            value = extract_url(item)
            if value:
                return value
    match = URL_RE.search(raw_text) if raw_text else None
    return match.group(1).replace("\\/", "/") if match else ""


def direct_live(channel_code: str, *, opener: Callable[..., Any] = urlopen) -> tuple[Any, str]:
    query = urlencode({"appCode": "", "token": "", "channelCode": channel_code})
    request = Request(f"{LIVE_URL}?{query}", headers=HEADERS, method="GET")
    with opener(request, timeout=10) as response:
        raw = response.read().decode("utf-8", errors="replace")
    try:
        return json.loads(raw), raw
    except json.JSONDecodeError:
        return None, raw


def managed_fetch(request: dict[str, Any], url: str, query: dict[str, Any]) -> dict[str, Any]:
    callback_id = f"plugin:{uuid.uuid4().hex}"
    event = threading.Event()
    with CALLBACK_LOCK:
        CALLBACKS[callback_id] = (event, None)
    frame({"protocol_version": "1.1", "kind": "request", "sender": "plugin", "request_id": callback_id,
           "method": "core.http.fetch", "plugin_instance": request.get("plugin_instance"),
           "deadline_unix_ms": int((time.time() + 10) * 1000),
           "context": {"parent_request_id": request["request_id"]},
           "payload": {"method": "GET", "url": url, "query": query,
                       "headers": HEADERS, "response_mode": "text"}})
    if not event.wait(10):
        with CALLBACK_LOCK:
            CALLBACKS.pop(callback_id, None)
        raise RuntimeError("PLUGIN_TIMEOUT")
    with CALLBACK_LOCK:
        response = CALLBACKS.pop(callback_id)[1] or {}
    if response.get("status") == "error":
        raise RuntimeError(str((response.get("error") or {}).get("code") or "TEMPORARY_UPSTREAM_FAILURE"))
    return response.get("result") or {}


def parse_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip().replace("T", " ").removesuffix("Z")
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def parse_response(result: dict[str, Any]) -> tuple[Any, str]:
    raw = str(result.get("body") or "")
    try:
        return json.loads(raw), raw
    except json.JSONDecodeError:
        return None, raw


def descriptor(url: str, ttl: int, diagnostics: dict[str, str] | None = None) -> dict[str, Any]:
    return {"descriptor_version": "1.0", "transport": "hls", "url": url,
            "headers": dict(HEADERS), "credential_refs": [], "ttl_seconds": ttl,
            "expires_at": None, "volatile_url": True, "requires_proxy": False,
            "warnings": [], "provider_diagnostics": diagnostics or {}}


def resolve(request: dict[str, Any], fixture_live_url: str = "") -> None:
    payload = request.get("payload") or {}
    resource = str(payload.get("resource_id") or "").strip("/").lower()
    channel = CHANNELS.get(resource)
    if channel is None:
        reply(request, error=failure("RESOURCE_NOT_FOUND", "HNNTV channel is not supported", retryable=False))
        return
    query = payload.get("query") or {}
    playseek_values = query.get("playseek") or []
    playseek = str(playseek_values[0] or "").strip() if playseek_values else ""
    try:
        if not playseek:
            if fixture_live_url:
                play_url = fixture_live_url
            else:
                live_payload, raw = direct_live(channel["code"])
                play_url = extract_url(live_payload, raw)
            if not play_url:
                raise ValueError("missing live URL")
            reply(request, descriptor(play_url, 1800))
            return
        parts = playseek.split("-", 1)
        if len(parts) != 2 or any(not re.fullmatch(r"\d{14}", item) for item in parts):
            reply(request, error=failure("RESOURCE_NOT_FOUND", "HNNTV playseek is invalid", retryable=False))
            return
        start_at = datetime.strptime(parts[0], "%Y%m%d%H%M%S")
        schedule_payload, _raw = parse_response(managed_fetch(
            request, SCHEDULE_URL, {"channelId": channel["channel_id"]}))
        result_set = schedule_payload.get("resultSet") if isinstance(schedule_payload, dict) else None
        if not isinstance(result_set, list):
            raise ValueError("invalid schedule")
        candidates = []
        for day in result_set:
            for schedule in day.get("schedules", []) if isinstance(day, dict) else []:
                if not isinstance(schedule, dict):
                    continue
                begin, end = parse_datetime(schedule.get("startDatetime")), parse_datetime(schedule.get("endDatetime"))
                if begin and end:
                    candidates.append((abs((begin - start_at).total_seconds()), -(end - begin).total_seconds(), schedule))
        if not candidates:
            raise ValueError("missing schedule")
        candidates.sort(key=lambda item: (item[0], item[1]))
        selected = candidates[0][2]
        schedule_id = str(selected.get("id") or "").strip()
        if not schedule_id:
            raise ValueError("missing schedule id")
        replay_payload, raw = parse_response(managed_fetch(request, REPLAY_URL, {
            "scheduleId": schedule_id, "channelCode": channel["code"], "appCode": "hnntv",
            "token": "", "startTime": "", "endTime": ""}))
        play_url = extract_url(replay_payload, raw)
        if not play_url:
            raise ValueError("missing replay URL")
        reply(request, descriptor(play_url, 300, {"schedule_id": schedule_id,
                                                  "program_name": str(selected.get("programName") or selected.get("name") or "")}))
    except RuntimeError as exc:
        code = str(exc)
        stable = code if code in {"CAPABILITY_DENIED", "PLUGIN_TIMEOUT", "AUTH_FAILED", "RATE_LIMITED"} else "TEMPORARY_UPSTREAM_FAILURE"
        reply(request, error=failure(stable, "HNNTV upstream request failed", category="network"))
    except Exception:
        reply(request, error=failure("TEMPORARY_UPSTREAM_FAILURE", "HNNTV response processing failed"))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--identity", default="org.waveflow/hnntv")
    parser.add_argument("--version", default="1.0.0")
    parser.add_argument("--fixture-live-url", default="")
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
                "owned_schemes": [{"scheme": "hnntv", "contract": "tv_provider"}],
                "capabilities": ["tv.resolve_stream"], "permissions": ["network"]})
        elif method == "runtime.health":
            reply(request, {"healthy": True})
        elif method == "runtime.shutdown":
            reply(request, {"accepted": True})
            return
        elif method == "tv.resolve_stream":
            threading.Thread(target=resolve, args=(request, args.fixture_live_url), daemon=True).start()
        else:
            reply(request, error=failure("RESOURCE_NOT_FOUND", "Provider method is not supported", retryable=False))


if __name__ == "__main__":
    main()
