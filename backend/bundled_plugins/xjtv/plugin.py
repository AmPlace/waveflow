#!/usr/bin/env python3
"""Independent XJTV TV Provider using the generic managed HTTP capability."""
from __future__ import annotations

import base64
import hashlib
import json
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding
from waveflow_plugin_sdk import (
    InvalidResource,
    PluginApplication,
    PluginError,
    ResolveContext,
    StreamDescriptor,
    TVProvider,
    TVReference,
)


XJTV_API_BASE = "https://slstapi.xjtvs.com.cn"
XJTV_CHANNEL_LIST_PATH = "/api/TVLiveV100/TVChannelList"
XJTV_GEN_TOKEN_PATH = "/api/TVLiveV100/GenToken"
XJTV_TIMESTAMP_PATH = "/api/Func/Timestamp"
XJTV_PAGE_REFERER = "https://xjtvs.com.cn/column/tv/434"
XJTV_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://xjtvs.com.cn",
    "Referer": XJTV_PAGE_REFERER,
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
}
XJTV_SIGN_REFRESH_MARGIN_SECONDS = 5 * 60
XJTV_FALLBACK_TTL_SECONDS = 10 * 60
XJTV_CHANNELS = {
    "1": {"api_id": "1", "code": "xjtv-1", "name": "新疆卫视"},
    "3": {"api_id": "3", "code": "xjtv-2", "name": "维吾尔语新闻综合频道"},
    "4": {"api_id": "4", "code": "xjtv-3", "name": "哈萨克语新闻综合频道"},
    "16": {"api_id": "16", "code": "xjtv-4", "name": "汉语综艺频道"},
    "17": {"api_id": "17", "code": "xjtv-5", "name": "维吾尔语影视频道"},
    "21": {"api_id": "21", "code": "xjtv-7", "name": "汉语体育健康频道"},
    "23": {"api_id": "23", "code": "xjtv-8", "name": "少儿频道"},
}
XJTV_CODE_TO_API_ID = {item["code"].replace("-", ""): item["api_id"] for item in XJTV_CHANNELS.values()}
XJTV_CODE_TO_API_ID.update({item["code"]: item["api_id"] for item in XJTV_CHANNELS.values()})


def _failure(code: str, message: str, *, retryable: bool = True, status: int | None = None) -> PluginError:
    details: dict[str, Any] = {"provider_code": code}
    if status is not None:
        details["status"] = int(status)
    return PluginError("TEMPORARY_UPSTREAM_FAILURE", message, retryable=retryable,
                       category="provider", details=details)


def _normalize_channel_id(resource_id: str) -> str:
    value = resource_id.strip("/").lower().replace("_", "-")
    if value in XJTV_CHANNELS:
        return value
    return XJTV_CODE_TO_API_ID.get(value.replace("-", ""), value)


def _json_body(response: Any, code: str) -> Any:
    body = response.body
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except ValueError:
            raise _failure(code, "XJTV upstream returned invalid JSON") from None
    return body


def _get_json(context: ResolveContext, path: str, *, query: dict[str, str]) -> dict[str, Any]:
    try:
        response = context.capabilities.managed_http(
            f"{XJTV_API_BASE}{path}", query=query, headers=XJTV_HEADERS,
            response_mode="json", timeout=10,
        )
    except PluginError:
        raise
    if response.status >= 400:
        raise _failure("xjtv_http_error", "XJTV upstream rejected the request", status=response.status)
    payload = _json_body(response, "xjtv_response_parse_failed")
    if not isinstance(payload, dict) or payload.get("success") is not True:
        message = str(payload.get("message") or "") if isinstance(payload, dict) else ""
        raise _failure("xjtv_api_failed", f"XJTV API returned failure: {message}")
    return payload


def _get_token(context: ResolveContext) -> tuple[str, str]:
    payload = _get_json(context, XJTV_GEN_TOKEN_PATH, query={"json": "true"})
    data = payload.get("data")
    if not isinstance(data, dict):
        raise _failure("xjtv_token_parse_failed", "XJTV token data is malformed")
    token, public_key = str(data.get("token") or "").strip(), str(data.get("publicKey") or "").strip()
    if not token or not public_key:
        raise _failure("xjtv_token_parse_failed", "XJTV token data is incomplete")
    return token, public_key


def _get_timestamp(context: ResolveContext) -> str:
    payload = _get_json(context, XJTV_TIMESTAMP_PATH, query={"json": "true"})
    timestamp = str(payload.get("data") or "").strip()
    if not timestamp:
        raise _failure("xjtv_timestamp_parse_failed", "XJTV timestamp is empty")
    return timestamp


def _encrypt_token(token: str, public_key_pem: str) -> str:
    try:
        public_key = serialization.load_pem_public_key(public_key_pem.encode("utf-8"))
        encrypted = public_key.encrypt(token.encode("utf-8"), padding.PKCS1v15())
        return base64.b64encode(encrypted).decode("ascii")
    except Exception:
        raise _failure("xjtv_token_encrypt_failed", "XJTV RSA token encryption failed") from None


def _signed_params(context: ResolveContext, path: str) -> dict[str, str]:
    token, public_key = _get_token(context)
    timestamp = _get_timestamp(context)
    encrypted = _encrypt_token(token, public_key)
    digest = hashlib.md5(f"{token}{timestamp}{path.lstrip('/')}".encode("utf-8")).hexdigest()
    return {"json": "true", "stamp": timestamp, "sign": f"{digest}{encrypted}"}


def _channel_value(item: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = item.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _extract_auth_key_expiry(play_url: str) -> int | None:
    try:
        auth_key = (parse_qs(urlparse(play_url).query).get("auth_key") or [""])[0].strip()
        return int(auth_key.split("-", 1)[0]) if auth_key else None
    except (TypeError, ValueError):
        return None


def _cache_ttl_from_expiry(expires_at: int | None) -> int:
    if not expires_at:
        return XJTV_FALLBACK_TTL_SECONDS
    return max(0, int(expires_at - time.time() - XJTV_SIGN_REFRESH_MARGIN_SECONDS))


class Provider(TVProvider):
    def resolve_stream(self, reference: TVReference, context: ResolveContext) -> StreamDescriptor:
        context.raise_if_cancelled()
        channel_id = _normalize_channel_id(reference.resource_id)
        expected = XJTV_CHANNELS.get(channel_id)
        if not expected:
            raise InvalidResource(f"Unsupported XJTV channel: {channel_id}")
        params = _signed_params(context, XJTV_CHANNEL_LIST_PATH)
        payload = _get_json(context, XJTV_CHANNEL_LIST_PATH, query=params)
        channels = payload.get("data")
        if not isinstance(channels, list):
            raise _failure("xjtv_channel_list_parse_failed", "XJTV channel list is malformed")
        selected = next((item for item in channels if isinstance(item, dict) and (
            _channel_value(item, "Id", "id") == expected["api_id"] or
            _channel_value(item, "SimpleName", "simpleName").lower() == expected["code"]
        )), None)
        if selected is None:
            raise _failure("xjtv_no_channel", f"XJTV channel was not found: {channel_id}")
        if selected.get("IsForbidden") is True:
            raise _failure("xjtv_channel_forbidden", f"XJTV channel is unavailable: {expected['name']}")
        play_url = _channel_value(selected, "PlayStreamUrl", "playStreamUrl", "url", "streamUrl")
        if not play_url or not play_url.startswith(("http://", "https://")):
            raise _failure("xjtv_no_play_url", f"XJTV returned no valid stream: {expected['name']}")
        expires_at = _extract_auth_key_expiry(play_url)
        return StreamDescriptor.hls(
            play_url, headers={"Referer": XJTV_PAGE_REFERER, "User-Agent": XJTV_HEADERS["User-Agent"]},
            ttl_seconds=_cache_ttl_from_expiry(expires_at), expires_at=expires_at,
            volatile_url=True, requires_proxy=False,
            provider_diagnostics={
                "channel_id": channel_id,
                "channel_name": _channel_value(selected, "ChineseName", "name") or expected["name"],
                "channel_code": _channel_value(selected, "SimpleName", "simpleName") or expected["code"].upper(),
                "url_expires_at": expires_at,
            },
        )


def main() -> None:
    identity, version = PluginApplication.identity_args("org.waveflow/xjtv")
    PluginApplication(identity=identity, version=version, permissions=["network"]).register_tv(
        "xjtv", Provider()
    ).run()


if __name__ == "__main__":
    main()
