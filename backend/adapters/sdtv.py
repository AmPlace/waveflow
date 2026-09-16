import base64
import hashlib
import json
import re
import time
from typing import Any

import httpx
from cryptography.hazmat.primitives import padding as sym_padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend

from . import ADAPTER_SUCCESS_TTL_SECONDS, AdapterRequest, AdapterResolveError

SDTV_CHANNELS = {
    "sdws": "24581",  # 山东卫视
    "sdql": "24584",  # 山东齐鲁
    "sdxw": "24602",  # 山东新闻
    "sdty": "24587",  # 山东体育休闲
    "sdsh": "24596",  # 山东生活
    "sdzy": "24593",  # 山东综艺
    "sdnk": "24599",  # 山东农科
    "sdwl": "24590",  # 山东文旅
    "sdse": "24605",  # 山东少儿
}

SDTV_PAGE_URL = "https://v.iqilu.com/live/sdtv/"
SDTV_EXCHANGE_URL = "https://feiying.litenews.cn/api/v1/auth/exchange"
SDTV_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://v.iqilu.com/",
    "Origin": "https://v.iqilu.com",
}


def _aes_encrypt(plaintext: str, key: str) -> bytes:
    k = key.encode()[:16].ljust(16, b"0")
    iv = b"0" * 16
    padder = sym_padding.PKCS7(128).padder()
    padded = padder.update(plaintext.encode()) + padder.finalize()
    enc = Cipher(algorithms.AES(k), modes.CBC(iv), backend=default_backend()).encryptor()
    return enc.update(padded) + enc.finalize()


def _aes_decrypt(data: bytes, key: str) -> str:
    k = key.encode()[:16].ljust(16, b"0")
    iv = b"0" * 16
    dec = Cipher(algorithms.AES(k), modes.CBC(iv), backend=default_backend()).decryptor()
    padded = dec.update(data) + dec.finalize()
    unpadder = sym_padding.PKCS7(128).unpadder()
    return (unpadder.update(padded) + unpadder.finalize()).decode()


async def resolve_sdtv(request: AdapterRequest, client: httpx.AsyncClient) -> dict[str, Any]:
    channel_key = request.resource_id.strip("/").lower()
    channel_id = SDTV_CHANNELS.get(channel_key)
    if not channel_id:
        if channel_key.isdigit():
            channel_id = channel_key
        else:
            supported = ", ".join(SDTV_CHANNELS)
            raise AdapterResolveError(
                "invalid_sdtv_channel_id",
                f"不支持的山东频道 ID: {channel_key}，支持的频道有: {supported}",
            )

    # 获取 salt 和 key
    try:
        resp = await client.get(SDTV_PAGE_URL, headers=SDTV_HEADERS, follow_redirects=True, timeout=10.0)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise AdapterResolveError(
            "sdtv_page_failed",
            f"山东广电页面请求失败: {exc}",
            status_code=502,
            retryable=True,
        ) from exc

    salt_m = re.search(r"mxpx\s*=\s*'([^']+)'", resp.text)
    key_m = re.search(r"aly\s*=\s*'([^']+)'", resp.text)
    if not salt_m or not key_m:
        raise AdapterResolveError(
            "sdtv_parse_failed",
            "山东广电页面无法提取 salt/key",
            status_code=502,
            retryable=True,
        )

    salt = salt_m.group(1)
    aes_key = key_m.group(1)

    # 生成签名并请求播放地址
    t = int(time.time() * 1000)
    s = hashlib.md5(f"{channel_id}{t}{salt}".encode()).hexdigest()
    body = base64.b64encode(
        _aes_encrypt(json.dumps({"channelMark": channel_id}), aes_key)
    ).decode()

    try:
        resp = await client.post(
            f"{SDTV_EXCHANGE_URL}?t={t}&s={s}",
            content=body,
            headers={**SDTV_HEADERS, "Content-Type": "text/plain"},
            timeout=10.0,
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise AdapterResolveError(
            "sdtv_exchange_failed",
            f"山东广电接口请求失败: {exc}",
            status_code=502,
            retryable=True,
        ) from exc

    try:
        result = json.loads(_aes_decrypt(base64.b64decode(resp.text), aes_key))
        play_url = result.get("data", "")
    except Exception as exc:
        raise AdapterResolveError(
            "sdtv_decrypt_failed",
            f"山东广电解密失败: {exc}",
            status_code=502,
            retryable=True,
        ) from exc

    if not play_url or not play_url.startswith("http"):
        raise AdapterResolveError(
            "sdtv_no_url",
            "山东广电没有返回播放地址",
            status_code=502,
            retryable=True,
        )

    return {
        "ok": True,
        "adapter": "sdtv",
        "source_type": "hls",
        "url": play_url,
        "direct_playable": True,
        "requires_proxy": False,
        "headers": {"Referer": "https://v.iqilu.com/"},
        "ttl": 30 * 60,
        "expires_at": None,
        "warnings": [],
        "channel_id": channel_key,
        "channel_name": channel_key,
        "volatile_url": True,
    }
