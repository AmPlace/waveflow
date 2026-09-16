import re
from datetime import datetime
from typing import Any

import httpx

from . import ADAPTER_SUCCESS_TTL_SECONDS, AdapterRequest, AdapterResolveError


HNNTV_CHANNELS = {
    "hnws": {"code": "STHaiNan_channel_lywsgq", "channel_id": 13, "name": "海南卫视"},
    "ssws": {"code": "STHaiNan_channel_ssws", "channel_id": 5, "name": "三沙卫视"},
    "xwpd": {"code": "STHaiNan_channel_xwpd", "channel_id": 3, "name": "海南新闻频道"},
    "wlpd": {"code": "wlpd", "channel_id": 6, "name": "海南文旅频道"},
    "jjpd": {"code": "jjpd", "channel_id": 1, "name": "海南自贸频道"},
    "ggpd": {"code": "ggpd", "channel_id": 4, "name": "海南公共频道"},
    "sepd": {"code": "sepd", "channel_id": 7, "name": "海南少儿频道"},
}

HNNTV_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://www.hnntv.cn/",
}
HNNTV_LIVE_URL = "http://ps.hnntv.cn/ps/livePlayUrl"
HNNTV_SCHEDULE_URL = "https://www.hnntv.cn/api/schedule/byDay"
HNNTV_REPLAY_URL = "https://ps.hnntv.cn/ps/wbPlayUrl"
_JSON_URL_RE = re.compile(r'"url"\s*:\s*"([^"]+)"', re.IGNORECASE)


def _first_query_value(query: dict[str, list[str]], key: str, default: str = "") -> str:
    values = query.get(key)
    if not values:
        return default
    return str(values[0] or default).strip()


def _parse_playseek(value: str) -> tuple[datetime, datetime]:
    parts = value.split("-", 1)
    if len(parts) != 2 or any(not re.fullmatch(r"\d{14}", part) for part in parts):
        raise AdapterResolveError(
            "invalid_hnntv_playseek",
            "海南回看 playseek 必须是 YYYYMMDDHHMMSS-YYYYMMDDHHMMSS",
        )
    return (
        datetime.strptime(parts[0], "%Y%m%d%H%M%S"),
        datetime.strptime(parts[1], "%Y%m%d%H%M%S"),
    )


def _parse_schedule_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    normalized = text.replace("T", " ").removesuffix("Z")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(normalized[: len(fmt)], fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def _extract_first_url(payload: Any, raw_text: str = "") -> str:
    if isinstance(payload, dict):
        for key in ("url", "playUrl", "play_url", "m3u8", "m3u8Url"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for key in ("resultSet", "data", "result"):
            url = _extract_first_url(payload.get(key), "")
            if url:
                return url
    if isinstance(payload, list):
        for item in payload:
            url = _extract_first_url(item, "")
            if url:
                return url
    if raw_text:
        match = _JSON_URL_RE.search(raw_text)
        if match:
            return match.group(1).replace("\\/", "/")
    return ""


async def _get_json(client: httpx.AsyncClient, url: str, *, params: dict[str, Any]) -> tuple[Any, str]:
    try:
        response = await client.get(
            url,
            params=params,
            headers=HNNTV_HEADERS,
            follow_redirects=True,
            timeout=10.0,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise AdapterResolveError(
            "hnntv_request_failed",
            f"海南广电接口请求失败: {exc}",
            status_code=502,
            retryable=True,
        ) from exc

    try:
        return response.json(), response.text
    except ValueError:
        return None, response.text


async def _resolve_live(request: AdapterRequest, client: httpx.AsyncClient, channel: dict[str, Any]) -> dict[str, Any]:
    payload, raw_text = await _get_json(
        client,
        HNNTV_LIVE_URL,
        params={
            "appCode": "",
            "token": "",
            "channelCode": channel["code"],
        },
    )
    play_url = _extract_first_url(payload, raw_text)
    if not play_url:
        raise AdapterResolveError(
            "hnntv_no_live_url",
            "海南广电没有返回有效直播地址",
            status_code=502,
            retryable=True,
        )

    return _result(request, channel, play_url, ttl=ADAPTER_SUCCESS_TTL_SECONDS)


async def _resolve_replay(
    request: AdapterRequest,
    client: httpx.AsyncClient,
    channel: dict[str, Any],
    playseek: str,
) -> dict[str, Any]:
    start_at, _end_at = _parse_playseek(playseek)
    schedule_payload, _ = await _get_json(
        client,
        HNNTV_SCHEDULE_URL,
        params={"channelId": channel["channel_id"]},
    )
    result_set = schedule_payload.get("resultSet") if isinstance(schedule_payload, dict) else None
    if not isinstance(result_set, list):
        raise AdapterResolveError(
            "hnntv_schedule_parse_failed",
            "海南广电节目表数据格式不正确",
            status_code=502,
            retryable=True,
        )

    candidates: list[tuple[float, float, dict[str, Any]]] = []
    for date_schedule in result_set:
        if not isinstance(date_schedule, dict):
            continue
        schedules = date_schedule.get("schedules")
        if not isinstance(schedules, list):
            continue
        for schedule in schedules:
            if not isinstance(schedule, dict):
                continue
            schedule_start = _parse_schedule_datetime(schedule.get("startDatetime"))
            schedule_end = _parse_schedule_datetime(schedule.get("endDatetime"))
            if not schedule_start or not schedule_end:
                continue
            diff = abs((schedule_start - start_at).total_seconds())
            duration = (schedule_end - schedule_start).total_seconds()
            candidates.append((diff, -duration, schedule))

    if not candidates:
        raise AdapterResolveError(
            "hnntv_no_schedule",
            "海南广电节目表里没有可匹配的回看节目",
            status_code=502,
            retryable=True,
        )

    candidates.sort(key=lambda item: (item[0], item[1]))
    best_match = candidates[0][2]
    schedule_id = str(best_match.get("id") or "").strip()
    if not schedule_id:
        raise AdapterResolveError(
            "hnntv_no_schedule_id",
            "海南广电节目表匹配项缺少 scheduleId",
            status_code=502,
            retryable=True,
        )

    replay_payload, raw_text = await _get_json(
        client,
        HNNTV_REPLAY_URL,
        params={
            "scheduleId": schedule_id,
            "channelCode": channel["code"],
            "appCode": "hnntv",
            "token": "",
            "startTime": "",
            "endTime": "",
        },
    )
    play_url = _extract_first_url(replay_payload, raw_text)
    if not play_url:
        raise AdapterResolveError(
            "hnntv_no_replay_url",
            "海南广电没有返回有效回看地址",
            status_code=502,
            retryable=True,
        )

    result = _result(request, channel, play_url, ttl=5 * 60)
    result["schedule_id"] = schedule_id
    result["program_name"] = str(best_match.get("programName") or best_match.get("name") or "")
    return result


def _result(request: AdapterRequest, channel: dict[str, Any], play_url: str, *, ttl: int) -> dict[str, Any]:
    return {
        "ok": True,
        "adapter": "hnntv",
        "source_type": "hls",
        "url": play_url,
        "direct_playable": True,
        "requires_proxy": False,
        "headers": dict(HNNTV_HEADERS),
        "ttl": ttl,
        "expires_at": None,
        "warnings": [],
        "channel_id": request.resource_id.strip("/").lower(),
        "channel_name": channel["name"],
        "volatile_url": True,
    }


async def resolve_hnntv(request: AdapterRequest, client: httpx.AsyncClient) -> dict[str, Any]:
    channel_key = request.resource_id.strip("/").lower()
    channel = HNNTV_CHANNELS.get(channel_key)
    if not channel:
        supported = ", ".join(HNNTV_CHANNELS)
        raise AdapterResolveError(
            "invalid_hnntv_channel_id",
            f"不支持的海南频道 ID: {channel_key}，支持的频道有: {supported}",
        )

    playseek = _first_query_value(request.query, "playseek")
    if playseek:
        return await _resolve_replay(request, client, channel, playseek)
    return await _resolve_live(request, client, channel)
