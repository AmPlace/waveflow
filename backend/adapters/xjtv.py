import base64
import hashlib
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding

from . import AdapterRequest, AdapterResolveError


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


def _normalize_channel_id(resource_id: str) -> str:
    value = resource_id.strip("/").lower().replace("_", "-")
    if value in XJTV_CHANNELS:
        return value
    compact = value.replace("-", "")
    if compact in XJTV_CODE_TO_API_ID:
        return XJTV_CODE_TO_API_ID[compact]
    return value


async def _get_json(client: httpx.AsyncClient, path: str, *, params: dict[str, Any]) -> dict[str, Any]:
    try:
        response = await client.get(
            f"{XJTV_API_BASE}{path}",
            params=params,
            headers=XJTV_HEADERS,
            follow_redirects=True,
            timeout=10.0,
        )
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPError as exc:
        raise AdapterResolveError(
            "xjtv_request_failed",
            f"新疆广电接口请求失败: {exc}",
            status_code=502,
            retryable=True,
        ) from exc
    except ValueError as exc:
        raise AdapterResolveError(
            "xjtv_response_parse_failed",
            "新疆广电接口返回的不是有效 JSON",
            status_code=502,
            retryable=True,
        ) from exc

    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise AdapterResolveError(
            "xjtv_api_failed",
            f"新疆广电接口返回失败: {payload.get('message', '') if isinstance(payload, dict) else ''}",
            status_code=502,
            retryable=True,
        )
    return payload


async def _get_token(client: httpx.AsyncClient) -> tuple[str, str]:
    payload = await _get_json(client, XJTV_GEN_TOKEN_PATH, params={"json": "true"})
    data = payload.get("data")
    if not isinstance(data, dict):
        raise AdapterResolveError(
            "xjtv_token_parse_failed",
            "新疆广电 token 数据格式不正确",
            status_code=502,
            retryable=True,
        )
    token = str(data.get("token") or "").strip()
    public_key = str(data.get("publicKey") or "").strip()
    if not token or not public_key:
        raise AdapterResolveError(
            "xjtv_token_parse_failed",
            "新疆广电 token 数据缺少 token/publicKey",
            status_code=502,
            retryable=True,
        )
    return token, public_key


async def _get_timestamp(client: httpx.AsyncClient) -> str:
    payload = await _get_json(client, XJTV_TIMESTAMP_PATH, params={"json": "true"})
    timestamp = str(payload.get("data") or "").strip()
    if not timestamp:
        raise AdapterResolveError(
            "xjtv_timestamp_parse_failed",
            "新疆广电时间戳为空",
            status_code=502,
            retryable=True,
        )
    return timestamp


def _encrypt_token(token: str, public_key_pem: str) -> str:
    try:
        public_key = serialization.load_pem_public_key(public_key_pem.encode("utf-8"))
        encrypted = public_key.encrypt(token.encode("utf-8"), padding.PKCS1v15())
        return base64.b64encode(encrypted).decode("ascii")
    except Exception as exc:
        raise AdapterResolveError(
            "xjtv_token_encrypt_failed",
            "新疆广电 token RSA 加密失败",
            status_code=502,
            retryable=True,
        ) from exc


async def _signed_params(client: httpx.AsyncClient, path: str) -> dict[str, str]:
    token, public_key = await _get_token(client)
    timestamp = await _get_timestamp(client)
    encrypted_token = _encrypt_token(token, public_key)
    digest = hashlib.md5(f"{token}{timestamp}{path.lstrip('/')}".encode("utf-8")).hexdigest()
    return {
        "json": "true",
        "stamp": timestamp,
        "sign": f"{digest}{encrypted_token}",
    }


async def _get_channel_list(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    params = await _signed_params(client, XJTV_CHANNEL_LIST_PATH)
    payload = await _get_json(client, XJTV_CHANNEL_LIST_PATH, params=params)
    data = payload.get("data")
    if not isinstance(data, list):
        raise AdapterResolveError(
            "xjtv_channel_list_parse_failed",
            "新疆广电频道列表数据格式不正确",
            status_code=502,
            retryable=True,
        )
    return [item for item in data if isinstance(item, dict)]


def _extract_auth_key_expiry(play_url: str) -> int | None:
    try:
        parsed = urlparse(play_url)
    except ValueError:
        return None

    auth_key = (parse_qs(parsed.query).get("auth_key") or [""])[0].strip()
    if not auth_key:
        return None
    expires_at = auth_key.split("-", 1)[0]
    try:
        return int(expires_at)
    except ValueError:
        return None


def _cache_ttl_from_expiry(expires_at: int | None) -> int:
    if not expires_at:
        return XJTV_FALLBACK_TTL_SECONDS
    return max(0, int(expires_at - time.time() - XJTV_SIGN_REFRESH_MARGIN_SECONDS))


def _channel_value(item: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = item.get(key)
        if value is not None:
            text = str(value).strip()
            if text:
                return text
    return ""


async def resolve_xjtv(request: AdapterRequest, client: httpx.AsyncClient) -> dict[str, Any]:
    channel_id = _normalize_channel_id(request.resource_id)
    expected = XJTV_CHANNELS.get(channel_id)
    if not expected:
        supported = ", ".join(XJTV_CHANNELS)
        aliases = ", ".join(item["code"].replace("-", "") for item in XJTV_CHANNELS.values())
        raise AdapterResolveError(
            "invalid_xjtv_channel_id",
            f"不支持的新疆频道 ID: {channel_id}，支持的频道有: {supported}；别名: {aliases}",
        )

    channels = await _get_channel_list(client)
    selected = None
    for item in channels:
        api_id = _channel_value(item, "Id", "id")
        simple_name = _channel_value(item, "SimpleName", "simpleName").lower()
        if api_id == expected["api_id"] or simple_name == expected["code"]:
            selected = item
            break

    if not selected:
        raise AdapterResolveError(
            "xjtv_no_channel",
            f"新疆广电频道列表里没有找到频道: {channel_id}",
            status_code=502,
            retryable=True,
        )

    if selected.get("IsForbidden") is True:
        raise AdapterResolveError(
            "xjtv_channel_forbidden",
            f"新疆广电频道当前不可播放: {expected['name']}",
            status_code=502,
            retryable=True,
        )

    play_url = _channel_value(selected, "PlayStreamUrl", "playStreamUrl", "url", "streamUrl")
    if not play_url:
        raise AdapterResolveError(
            "xjtv_no_play_url",
            f"新疆广电没有返回有效直播地址: {expected['name']}",
            status_code=502,
            retryable=True,
        )

    expires_at = _extract_auth_key_expiry(play_url)
    ttl = _cache_ttl_from_expiry(expires_at)
    url_ttl = max(0, int(expires_at - time.time())) if expires_at else None

    return {
        "ok": True,
        "adapter": "xjtv",
        "source_type": "hls",
        "url": play_url,
        "direct_playable": True,
        "requires_proxy": False,
        "headers": {
            "Referer": XJTV_PAGE_REFERER,
            "User-Agent": XJTV_HEADERS["User-Agent"],
        },
        "ttl": ttl,
        "expires_at": expires_at,
        "warnings": [],
        "channel_id": channel_id,
        "channel_name": _channel_value(selected, "ChineseName", "name") or expected["name"],
        "channel_code": _channel_value(selected, "SimpleName", "simpleName") or expected["code"].upper(),
        "url_expires_at": expires_at,
        "url_ttl": url_ttl,
        "volatile_url": True,
    }
