#!/usr/bin/env python3
"""Independent Migu TV Provider using the generic managed HTTP capability."""
from __future__ import annotations

import hashlib
import random
import re
import time
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from waveflow_plugin_sdk import (
    InvalidResource,
    PluginApplication,
    PluginError,
    ResolveContext,
    StreamDescriptor,
    TVProvider,
    TVReference,
    TemporaryFailure,
)


MIGU_PLAYURL_ENDPOINT = "https://play.miguvideo.com/playurl/v1/play/playurl"
MIGU_APP_VERSION = "2600034600"
MIGU_CLIENT_CHANNEL_ID = f"{MIGU_APP_VERSION}-99000-201600010010028"
MIGU_SIGN_SUFFIX_PREFIX = "2cac4f2c6c3346a5b34e085725ef7e33migu"
MIGU_DD_CALCU_KEYS = "cdabyzwxkl"
MIGU_ALLOWED_RATES = {"2", "3"}
MIGU_NO_APPCODE_IDS = {"641886683", "641886773"}
MIGU_CLIENT_ID = hashlib.md5(str(int(time.time() * 1000)).encode()).hexdigest()


def _failure(provider_code: str, message: str, *, retryable: bool = True) -> PluginError:
    return PluginError(
        "TEMPORARY_UPSTREAM_FAILURE",
        message,
        retryable=retryable,
        category="provider",
        details={"provider_code": provider_code},
    )


def _md5(value: str) -> str:
    return hashlib.md5(value.encode()).hexdigest()


def _first_query_value(query: dict[str, list[str]], key: str, default: str = "") -> str:
    values = query.get(key)
    return str(values[0] or default).strip() if values else default


def _nested_get(data: dict[str, Any], *keys: str) -> Any:
    value: Any = data
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _region_error(payload: dict[str, Any]) -> bool:
    text = str(payload)
    return "region" in text.lower() or "地区" in text or "区域" in text


def _dd_calcu_720p(pu_data: str, channel_id: str) -> str:
    if not pu_data or not channel_id or len(channel_id) <= 6:
        return ""
    result: list[str] = []
    half = len(pu_data) // 2
    for i in range(half):
        result.append(pu_data[len(pu_data) - i - 1])
        result.append(pu_data[i])
        if i == 1:
            result.append("v")
        elif i == 2:
            result.append(MIGU_DD_CALCU_KEYS[int(datetime.now().strftime("%Y%m%d")[2])])
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


def _request_parts(reference: TVReference) -> tuple[str, str, dict[str, str], dict[str, str]]:
    channel_id = reference.resource_id.strip("/").split("/", 1)[0]
    if not channel_id.isdigit():
        raise InvalidResource("Migu channel ID must be numeric")
    rate = _first_query_value(reference.query, "rate", "3")
    if rate not in MIGU_ALLOWED_RATES:
        raise PluginError(
            "RESOURCE_NOT_FOUND",
            "Migu only supports rate=2 or rate=3",
            details={"provider_code": "unsupported_quality"},
        )
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
    return channel_id, rate, headers, params


class Provider(TVProvider):
    def resolve_stream(self, reference: TVReference, context: ResolveContext) -> StreamDescriptor:
        channel_id, _rate, headers, params = _request_parts(reference)
        try:
            response = context.capabilities.managed_http(
                MIGU_PLAYURL_ENDPOINT, query=params, headers=headers, response_mode="json", timeout=8,
            )
            payload = response.body
            if isinstance(payload, str):
                import json
                payload = json.loads(payload)
            if not isinstance(payload, dict):
                raise ValueError("Migu response is not an object")
            play_url = _nested_get(payload, "body", "urlInfo", "url")
            resolved_id = str(_nested_get(payload, "body", "content", "contId") or channel_id)
            if not play_url:
                if _region_error(payload):
                    raise _failure("migu_region_restricted", "Migu stream is region restricted", retryable=False)
                raise _failure("migu_no_play_url", "Migu returned no playable URL")
            if urlparse(str(play_url)).scheme not in {"http", "https"}:
                raise _failure("migu_no_play_url", "Migu returned an invalid playable URL")
        except PluginError:
            raise
        except Exception:
            raise _failure("migu_upstream_failed", "Migu upstream response failed") from None
        return StreamDescriptor.hls(
            _append_dd_calcu(str(play_url), resolved_id),
            ttl_seconds=1800, volatile_url=False, requires_proxy=False,
        )


def main() -> None:
    identity, version = PluginApplication.identity_args("org.waveflow/migu")
    PluginApplication(identity=identity, version=version, permissions=["network"]).register_tv(
        "migu", Provider()
    ).run()


if __name__ == "__main__":
    main()
