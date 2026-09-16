import hashlib
import random
import re
import time
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

import httpx

from . import ADAPTER_SUCCESS_TTL_SECONDS, AdapterRequest, AdapterResolveError


MIGU_PLAYURL_ENDPOINT = "https://play.miguvideo.com/playurl/v1/play/playurl"
MIGU_APP_VERSION = "2600034600"
MIGU_CLIENT_CHANNEL_ID = f"{MIGU_APP_VERSION}-99000-201600010010028"
MIGU_SIGN_SUFFIX_PREFIX = "2cac4f2c6c3346a5b34e085725ef7e33migu"
MIGU_DD_CALCU_KEYS = "cdabyzwxkl"
MIGU_ALLOWED_RATES = {"2", "3"}
MIGU_NO_APPCODE_IDS = {"641886683", "641886773"}
MIGU_CLIENT_ID = hashlib.md5(str(int(time.time() * 1000)).encode("utf-8")).hexdigest()


def _md5(value: str) -> str:
    return hashlib.md5(value.encode("utf-8")).hexdigest()


def _first_query_value(query: dict[str, list[str]], key: str, default: str = "") -> str:
    values = query.get(key)
    if not values:
        return default
    return str(values[0] or default).strip()


def _migu_region_error(payload: dict[str, Any]) -> bool:
    text = str(payload)
    return "region" in text.lower() or "地区" in text or "区域" in text


def _nested_get(data: dict[str, Any], *keys: str) -> Any:
    value: Any = data
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _dd_calcu_720p(pu_data: str, channel_id: str) -> str:
    if not pu_data or not channel_id or len(channel_id) <= 6:
        return ""

    date_text = datetime.now().strftime("%Y%m%d")
    result: list[str] = []
    half = len(pu_data) // 2
    for i in range(half):
        result.append(pu_data[len(pu_data) - i - 1])
        result.append(pu_data[i])
        if i == 1:
            result.append("v")
        elif i == 2:
            result.append(MIGU_DD_CALCU_KEYS[int(date_text[2])])
        elif i == 3:
            result.append(MIGU_DD_CALCU_KEYS[int(channel_id[6])])
        elif i == 4:
            result.append("a")
    return "".join(result)


def _append_dd_calcu(play_url: str, channel_id: str) -> str:
    match = re.search(r"(?:[?&])puData=([^&]+)", play_url)
    pu_data = match.group(1) if match else ""
    dd_calcu = _dd_calcu_720p(pu_data, channel_id)
    separator = "&" if "?" in play_url else "?"
    return f"{play_url}{separator}ddCalcu={dd_calcu}&sv=10004&ct=android"


def _validate_migu_request(request: AdapterRequest) -> tuple[str, str]:
    channel_id = request.resource_id.strip("/").split("/", 1)[0]
    if not channel_id.isdigit():
        raise AdapterResolveError("invalid_migu_channel_id", "咪咕频道 ID 必须是数字")

    rate = _first_query_value(request.query, "rate", "3")
    if rate not in MIGU_ALLOWED_RATES:
        raise AdapterResolveError(
            "unsupported_quality",
            "咪咕 adapter V1 只支持未登录标清/高清 rate=2 或 rate=3",
        )
    return channel_id, rate


async def resolve_migu(request: AdapterRequest, client: httpx.AsyncClient) -> dict[str, Any]:
    channel_id, rate = _validate_migu_request(request)

    timestamp = str(int(time.time() * 1000))
    salt = f"{random.randint(0, 999999):06d}25"
    sign_seed = _md5(f"{timestamp}{channel_id}{MIGU_APP_VERSION[:8]}")
    sign = _md5(f"{sign_seed}{MIGU_SIGN_SUFFIX_PREFIX}{salt[:4]}")

    headers = {
        "AppVersion": MIGU_APP_VERSION,
        "TerminalId": "android",
        "X-UP-CLIENT-CHANNEL-ID": MIGU_CLIENT_CHANNEL_ID,
        "ClientId": MIGU_CLIENT_ID,
    }
    if channel_id not in MIGU_NO_APPCODE_IDS:
        headers["appCode"] = "miguvideo_default_android"

    params = {
        "sign": sign,
        "rateType": rate,
        "contId": channel_id,
        "timestamp": timestamp,
        "salt": salt,
        "flvEnable": "true",
        "super4k": "true",
    }

    try:
        response = await client.get(
            MIGU_PLAYURL_ENDPOINT,
            params=params,
            headers=headers,
            timeout=8,
        )
        response.raise_for_status()
        payload = response.json()
    except httpx.TimeoutException as exc:
        raise AdapterResolveError(
            "migu_timeout",
            "咪咕上游解析超时",
            status_code=504,
            retryable=True,
        ) from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise AdapterResolveError(
            "migu_upstream_failed",
            "咪咕上游解析失败",
            status_code=502,
            retryable=True,
        ) from exc

    play_url = _nested_get(payload, "body", "urlInfo", "url")
    resolved_channel_id = str(_nested_get(payload, "body", "content", "contId") or channel_id)
    if not play_url:
        if _migu_region_error(payload):
            raise AdapterResolveError(
                "migu_region_restricted",
                "咪咕源存在地区限制",
                status_code=502,
                retryable=False,
            )
        raise AdapterResolveError(
            "migu_no_play_url",
            "咪咕没有返回可播放地址",
            status_code=502,
            retryable=True,
        )

    parsed_url = urlparse(str(play_url))
    if parsed_url.scheme not in {"http", "https"}:
        raise AdapterResolveError(
            "migu_no_play_url",
            "咪咕返回了不可播放地址",
            status_code=502,
            retryable=True,
        )

    return {
        "ok": True,
        "adapter": "migu",
        "source_type": "hls",
        "url": _append_dd_calcu(str(play_url), resolved_channel_id),
        "direct_playable": True,
        "requires_proxy": False,
        "headers": {},
        "ttl": ADAPTER_SUCCESS_TTL_SECONDS,
        "expires_at": None,
        "warnings": [],
    }

