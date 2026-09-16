#!/usr/bin/env python3
"""Independent JSTV Provider Plugin using only the public IPC contract."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from typing import Any


SIGN_KEY = "tJanAHkyGtaifaQG4dWe"
ENCRYPTED_BASE = "https://litchi-play-encrypted-site.jstv.com"
REFERER = "https://live.jstv.com/"
PATHS = {
    "jsws": "/applive/jswspro.m3u8", "jsws4k": "/4klive/jsws4kpro.m3u8",
    "jscs": "/applive/jscspro.m3u8", "jszy": "/applive/jszypro.m3u8",
    "jsys": "/applive/jsyspro.m3u8", "jsxw": "/applive/jsxwpro.m3u8",
    "jsjy": "/applive/jsjypro.m3u8", "jsxx": "/applive/jsxxpro.m3u8",
    "ymkt": "/applive/ymktpro.m3u8", "jsgj": "/applive/jsgjpro.m3u8",
    "cftx": "/live/cftxtxjm.m3u8", "nanjing": "/live/nanjing.m3u8",
    "luhe": "/live/luhe.m3u8", "wuxi": "/live/wuxi.m3u8", "xuzhou": "/live/xuzhou.m3u8",
    "pizhou": "/live/pizhou.m3u8", "xinyi": "/live/xinyi.m3u8", "jiawang": "/live/jiawang.m3u8",
    "tongshan": "/live/tongshan.m3u8", "changzhou": "/applive/czpro.m3u8",
    "wujin": "/live/wujin.m3u8", "nantong": "/live/nantong.m3u8",
    "lianyungang": "/live/lianyungang.m3u8", "donghai": "/live/donghai.m3u8",
    "huaian": "/live/huaian.m3u8", "xuyi": "/live/xuyi.m3u8", "hongze": "/live/hongze.m3u8",
    "yancheng": "/live/yancheng.m3u8", "xiangshui": "/live/xiangshui.m3u8",
    "zhenjiang": "/live/zhenjiang.m3u8", "jurong": "/live/jurong.m3u8",
    "taizhou": "/live/taizhou.m3u8", "taixing": "/live/taixing.m3u8",
    "xinghua": "/live/xinghua.m3u8", "jingjiang": "/live/jingjiang.m3u8",
    "suqian": "/live/suqian.m3u8", "siyang": "/live/siyang.m3u8",
}


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


def frame(value: dict[str, Any]) -> None:
    body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    sys.stdout.buffer.write(
        f"Content-Length: {len(body)}\r\nContent-Type: application/json; charset=utf-8\r\n\r\n".encode() + body
    )
    sys.stdout.buffer.flush()


def reply(request: dict[str, Any], result: Any = None, error: dict[str, Any] | None = None) -> None:
    frame({"protocol_version": "1.1", "kind": "response", "sender": "plugin",
           "request_id": request["request_id"], "status": "error" if error else "ok",
           "result": result, "error": error, "diagnostics": {}})


def provider_error(code: str, message: str) -> dict[str, Any]:
    return {"code": code, "message": message, "retryable": False, "category": "provider", "details": {}}


def sign_url(path: str, now: float | None = None) -> str:
    stream_name = path.rsplit("/", 1)[-1].split(".", 1)[0]
    expires_at = int(time.time() if now is None else now) + 180
    tx_time = format(expires_at, "x")
    signature = hashlib.md5(f"{SIGN_KEY}{stream_name}{tx_time}".encode()).hexdigest()
    return f"{ENCRYPTED_BASE}{path}?txSecret={signature}&txTime={tx_time}"


def resolve(request: dict[str, Any], fixed_time: float | None) -> None:
    resource = str((request.get("payload") or {}).get("resource_id") or "").strip("/").lower()
    path = PATHS.get(resource)
    if path:
        url = sign_url(path, fixed_time)
    elif resource.startswith(("http://", "https://")):
        url = resource
    else:
        reply(request, error=provider_error("RESOURCE_NOT_FOUND", "JSTV channel is not supported"))
        return
    reply(request, {"descriptor_version": "1.0", "transport": "hls", "url": url,
                    "headers": {"Referer": REFERER}, "credential_refs": [], "ttl_seconds": 180,
                    "expires_at": None, "volatile_url": True, "requires_proxy": False, "warnings": []})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--identity", default="org.waveflow/jstv")
    parser.add_argument("--version", default="1.0.0")
    parser.add_argument("--fixed-time", type=float)
    args = parser.parse_args()
    while True:
        request = read_frame()
        if request is None:
            return
        method = request.get("method")
        if method == "runtime.hello":
            reply(request, {"protocol_version": "1.1", "plugin": args.identity, "version": args.version,
                "provider_contracts": [{"contract": "tv_provider", "contract_version": "1.0", "features": ["resolve_stream"]}],
                "owned_schemes": [{"scheme": "jstv", "contract": "tv_provider"}],
                "capabilities": ["tv.resolve_stream"], "permissions": []})
        elif method == "runtime.health":
            reply(request, {"healthy": True})
        elif method == "runtime.shutdown":
            reply(request, {"accepted": True})
            return
        elif method == "tv.resolve_stream":
            resolve(request, args.fixed_time)
        else:
            reply(request, error=provider_error("RESOURCE_NOT_FOUND", "Provider method is not supported"))


if __name__ == "__main__":
    main()
