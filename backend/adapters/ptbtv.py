import hashlib
import json
import time
from typing import Any

import httpx

from . import AdapterRequest, AdapterResolveError




_PTBTV_API_KEY = "f33ba15effa5c10e873bf3842afb46a6"
_PTBTV_API_SECRET = "YWFkZDYwMjNkNzMzNzUwZWJjYjE4NWFjZjY3YmQyYzE="
_PTBTV_API_VERSION = "1.0.0"

_PTBTV_CHANNEL_INFO_URL = "https://www.ptbtv.com/m2o/channel/channel_info.php"

# 关键：必须跟 curl_cffi `impersonate="chrome"` 的 TLS 指纹自洽。
# curl_cffi 的 chrome 指纹是 macOS 版本，UA 也要写 macOS Chrome；
# 如果写成 Windows Chrome，ptbtv 的 WAF 会识别为"指纹是 Mac、UA 写 Win"
# 的伪造客户端，直接 403。
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

PTBTV_CHANNELS: dict[str, dict[str, str]] = {
    "1":       {"name": "莆田1套",   "channel_id": "4", "referer": "https://www.ptbtv.com/live/pt1t/"},
    "2":       {"name": "莆田2套",   "channel_id": "5", "referer": "https://www.ptbtv.com/live/pt2t/"},
    "xianyou": {"name": "仙游电视",  "channel_id": "6", "referer": "https://www.ptbtv.com/live/xyds/"},
}

_PTBTV_ALIASES: dict[str, str] = {
    "ptbtv-1":   "1",
    "ptbtv-2":   "2",
    "pt1":       "1",
    "pt2":       "2",
    "xianyoutv": "xianyou",
    "xy":        "xianyou",
}


def _build_sign_headers() -> dict[str, str]:
    timestamp = str(int(time.time()))
    sign_text = f"{_PTBTV_API_KEY}&{_PTBTV_API_SECRET}&{_PTBTV_API_VERSION}&{timestamp}"
    signature = hashlib.md5(sign_text.encode()).hexdigest()
    return {
        "X-API-TIMESTAMP": timestamp,
        "X-API-KEY": _PTBTV_API_KEY,
        "X-AUTH-TYPE": "md5",
        "X-API-VERSION": _PTBTV_API_VERSION,
        "X-API-SIGNATURE": signature,
    }


def _build_headers(referer: str) -> dict[str, str]:
    return {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Cache-Control": "no-cache",
        "Origin": "https://www.ptbtv.com",
        "Pragma": "no-cache",
        "Referer": referer,
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "User-Agent": _USER_AGENT,
        "X-Requested-With": "XMLHttpRequest",
        **_build_sign_headers(),
    }


async def _fetch_with_curl_cffi(channel_id: str, headers: dict[str, str]) -> tuple[int, str] | None:
    """走 curl_cffi 的 chrome 指纹。

    返回:
      - (status_code, text) 表示 curl_cffi 跑通了（不论 status_code 是不是 200，
        交给上层判断是否当成可信结果）。
      - None 表示 curl_cffi 不可用或自身异常，让上层回退到 httpx。
    """
    try:
        from curl_cffi.requests import AsyncSession
    except Exception:
        return None
    try:
        async with AsyncSession(impersonate="chrome", timeout=10) as session:
            resp = await session.get(
                _PTBTV_CHANNEL_INFO_URL,
                params={"channel_id": str(channel_id)},
                headers=headers,
            )
        return resp.status_code, resp.text
    except Exception:
        return None


async def resolve_ptbtv(request: AdapterRequest, client: httpx.AsyncClient) -> dict[str, Any]:
    channel_key = request.resource_id.strip("/").lower()
    channel_key = _PTBTV_ALIASES.get(channel_key, channel_key)
    entry = PTBTV_CHANNELS.get(channel_key)
    if not entry:
        supported = ", ".join(sorted(PTBTV_CHANNELS))
        raise AdapterResolveError(
            "invalid_ptbtv_channel_id",
            f"不支持的莆田频道: {channel_key}，支持的频道有: {supported}",
        )

    headers = _build_headers(entry["referer"])

   
    text: str | None = None
    cffi_result = await _fetch_with_curl_cffi(entry["channel_id"], headers)
    if cffi_result is not None and cffi_result[0] == 200 and cffi_result[1]:
        text = cffi_result[1]

    if text is None:
        try:
            resp = await client.get(
                _PTBTV_CHANNEL_INFO_URL,
                params={"channel_id": entry["channel_id"]},
                headers=headers,
                timeout=10.0,
                follow_redirects=True,
            )
            resp.raise_for_status()
            text = resp.text
        except httpx.HTTPError as exc:
            raise AdapterResolveError(
                "ptbtv_request_failed",
                f"莆田广电接口请求失败: {exc}",
                status_code=502,
                retryable=True,
            ) from exc

    try:
        data = json.loads(text)
        play_url = data[0]["m3u8"]
    except (ValueError, KeyError, TypeError, IndexError) as exc:
        raise AdapterResolveError(
            "ptbtv_parse_failed",
            f"莆田广电频道 {entry['name']} 接口返回结构异常: {exc}",
            status_code=502,
            retryable=True,
        ) from exc

    if not isinstance(play_url, str) or not play_url.startswith(("http://", "https://")):
        raise AdapterResolveError(
            "ptbtv_no_stream",
            f"莆田广电频道 {entry['name']} 没有返回有效播放地址",
            status_code=502,
            retryable=True,
        )

    return {
        "ok": True,
        "adapter": "ptbtv",
        "source_type": "hls",
        "url": play_url,
        "direct_playable": True,
        "requires_proxy": False,
        "headers": {},
        "ttl": 3 * 60,
        "expires_at": None,
        "warnings": [],
        "channel_id": channel_key,
        "channel_name": entry["name"],
        "volatile_url": True,
    }
