import asyncio
import base64
import json
import re
from typing import Any

import httpx

from . import AdapterRequest, AdapterResolveError


SXBC_STREAM_URLS = (
    "http://toutiao.cnwest.com/static/v1/stream.js",
    "http://toutiao.cnwest.com/static/v1/group/stream.js",
)
SXBC_HEADERS = {
    "Accept": "*/*",
    "Referer": "http://live.snrtv.com/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
}
SXBC_TTL_SECONDS = 10 * 60
_VAR_RE = re.compile(r"\b(sTV|sRadio)\s*=\s*([\"'])(.*?)\2", re.DOTALL)
_BLOCKED_URL_MARKERS = (
    "://alzbl.snrtv.com/live/sxtv.m3u8",
    "://stream.snrtv.com/snrtv-6.m3u8",
)


async def _fetch_stream_js(client: httpx.AsyncClient) -> str:
    last_error: Exception | None = None
    for url in SXBC_STREAM_URLS:
        try:
            response = await client.get(
                url,
                headers=SXBC_HEADERS,
                follow_redirects=True,
                timeout=10.0,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            last_error = exc
            continue

        text = response.text
        if "sTV" in text and "sRadio" in text:
            return text

    raise AdapterResolveError(
        "sxbc_stream_js_failed",
        f"陕西广电频道表请求失败: {last_error}" if last_error else "陕西广电频道表缺少加密变量",
        status_code=502,
        retryable=True,
    )


def _extract_encrypted_vars(source: str) -> tuple[str, str]:
    values = {match.group(1): match.group(3) for match in _VAR_RE.finditer(source)}
    s_tv = values.get("sTV", "").strip()
    s_radio = values.get("sRadio", "").strip()
    if not s_tv or not s_radio:
        raise AdapterResolveError(
            "sxbc_stream_js_parse_failed",
            "陕西广电频道表没有找到 sTV/sRadio",
            status_code=502,
            retryable=True,
        )
    if len(s_tv) <= 16 or len(s_radio) <= 16:
        raise AdapterResolveError(
            "sxbc_stream_js_parse_failed",
            "陕西广电频道表加密变量长度不正确",
            status_code=502,
            retryable=True,
        )
    return s_tv, s_radio


async def _openssl_decrypt_zero_padded(key: bytes, iv: bytes, ciphertext: bytes) -> bytes:
    process = await asyncio.create_subprocess_exec(
        "openssl",
        "enc",
        "-aes-128-cbc",
        "-d",
        "-K",
        key.hex(),
        "-iv",
        iv.hex(),
        "-nopad",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate(ciphertext)
    if process.returncode != 0:
        detail = stderr.decode("utf-8", "ignore").strip()
        raise AdapterResolveError(
            "sxbc_decrypt_failed",
            f"陕西广电频道表解密失败: {detail or 'openssl failed'}",
            status_code=502,
            retryable=True,
        )
    return stdout.rstrip(b"\x00")


async def _undress(key: bytes, iv: bytes, payload: str) -> Any:
    try:
        ciphertext = base64.b64decode(payload, validate=True)
    except ValueError as exc:
        raise AdapterResolveError(
            "sxbc_decrypt_failed",
            "陕西广电频道表不是有效 base64",
            status_code=502,
            retryable=True,
        ) from exc

    plaintext = await _openssl_decrypt_zero_padded(key, iv, ciphertext)
    try:
        return json.loads(plaintext.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AdapterResolveError(
            "sxbc_decrypt_failed",
            "陕西广电频道表解密结果不是有效 JSON",
            status_code=502,
            retryable=True,
        ) from exc


async def _load_channel_maps(client: httpx.AsyncClient) -> dict[str, dict[str, Any]]:
    source = await _fetch_stream_js(client)
    s_tv, s_radio = _extract_encrypted_vars(source)
    key = s_tv[:16].encode("utf-8")
    iv = s_radio[:16].encode("utf-8")

    tv_payload, radio_payload = await asyncio.gather(
        _undress(key, iv, s_tv[16:]),
        _undress(key, iv, s_radio[16:]),
    )

    def normalize_map(payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            return {}
        inner = payload.get("sxbc")
        return inner if isinstance(inner, dict) else payload

    return {
        "tv": normalize_map(tv_payload),
        "radio": normalize_map(radio_payload),
    }


def _first_query_value(query: dict[str, list[str]], key: str, default: str = "") -> str:
    values = query.get(key)
    if not values:
        return default
    return str(values[0] or default).strip()


def _select_channel(channel_maps: dict[str, dict[str, Any]], channel_id: str, preferred_type: str) -> tuple[str, dict[str, Any]]:
    for source_type in (preferred_type, "radio" if preferred_type == "tv" else "tv"):
        item = channel_maps.get(source_type, {}).get(channel_id)
        if isinstance(item, dict):
            return source_type, item

    supported = []
    for source_type, items in channel_maps.items():
        supported.extend(f"{source_type}:{item_id}" for item_id in items)
    raise AdapterResolveError(
        "invalid_sxbc_channel_id",
        f"不支持的陕西频道 ID: {channel_id}，支持的频道有: {', '.join(supported)}",
    )


def _is_blocked_play_url(play_url: str) -> bool:
    return any(marker in play_url for marker in _BLOCKED_URL_MARKERS)


async def resolve_sxbc(request: AdapterRequest, client: httpx.AsyncClient) -> dict[str, Any]:
    channel_id = request.resource_id.strip("/").lower()
    preferred_type = _first_query_value(request.query, "type", "tv").lower()
    if preferred_type not in {"tv", "radio"}:
        preferred_type = "tv"

    channel_maps = await _load_channel_maps(client)
    channel_type, channel = _select_channel(channel_maps, channel_id, preferred_type)

    play_url = str(channel.get("m3u8") or "").strip()
    if not play_url or _is_blocked_play_url(play_url):
        raise AdapterResolveError(
            "sxbc_no_play_url",
            f"陕西广电频道 {channel_id} 当前没有有效直播地址",
            status_code=502,
            retryable=True,
        )

    return {
        "ok": True,
        "adapter": "sxbc",
        "source_type": "hls",
        "url": play_url,
        "direct_playable": True,
        "requires_proxy": False,
        "headers": dict(SXBC_HEADERS),
        "ttl": SXBC_TTL_SECONDS,
        "expires_at": None,
        "warnings": [],
        "channel_id": channel_id,
        "channel_name": str(channel.get("name") or channel_id),
        "channel_type": channel_type,
        "logo_url": str(channel.get("logo") or ""),
        "playlist_url": str(channel.get("playlist") or ""),
        "volatile_url": True,
    }
