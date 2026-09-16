import base64
import hashlib
import random
import string
import time
from typing import Any

import httpx
from cryptography.hazmat.primitives import serialization

from . import ADAPTER_SUCCESS_TTL_SECONDS, AdapterRequest, AdapterResolveError

KANKAN_CHANNELS = {
    "dfws":   {"id": 1, "name": "东方卫视"},
    "xwzh":   {"id": 2, "name": "新闻综合"},
    "dspd":   {"id": 4, "name": "都市频道"},
    "dycj":   {"id": 5, "name": "第一财经"},
    "hhxd":   {"id": 9, "name": "哈哈炫动"},
    "wxty":   {"id": 10, "name": "五星体育"},
    "mde":    {"id": 11, "name": "魔都眼"},
    "xjs":    {"id": 12, "name": "新纪实"},
}

KANKAN_API_URL = "https://kapi.kankanews.com/content/pc/tv/channel/detail"
KANKAN_SIGN_SECRET = "28c8edde3d61a0411511d3b1866f0636"
KANKAN_VERSION = "2.41.9"

KANKAN_PUB_KEY_PEM = b"""-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDP5hzPUW5RFeE2xBT1ERB3hHZI
Votn/qatWhgc1eZof09qKjElFN6Nma461ZAwGpX4aezKP8Adh4WJj4u2O54xCXDt
wzKRqZO2oNZkuNmF2Va8kLgiEQAAcxYc8JgTN+uQQNpsep4n/o1sArTJooZIF17E
tSqSgXDcJ7yDj5rc7wIDAQAB
-----END PUBLIC KEY-----"""

_pub_numbers = serialization.load_pem_public_key(KANKAN_PUB_KEY_PEM).public_numbers()
_PUB_E = _pub_numbers.e
_PUB_N = _pub_numbers.n


def _rsa_public_decrypt(encrypted_b64: str) -> str:
    """RSA 公钥解密 live_address（私钥加密 / 公钥解密模式）"""
    raw = base64.b64decode(encrypted_b64)
    hex_str = raw.hex().upper()
    result = ""
    block_size = 256  # 1024-bit RSA = 128 bytes = 256 hex chars
    for i in range(0, len(hex_str), block_size):
        chunk = hex_str[i : i + block_size]
        if len(chunk) < block_size:
            break
        m = pow(int(chunk, 16), _PUB_E, _PUB_N)
        mb = m.to_bytes(128, "big")
        sep = mb.index(0, 2)
        result += mb[sep + 1 :].decode("utf-8", errors="ignore")
    return result


def _md5(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()


def _make_sign(params: dict[str, str]) -> dict[str, str]:
    """生成签名 headers"""
    nonce = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
    ts = str(int(time.time()))
    extra = {
        "platform": "pc",
        "version": KANKAN_VERSION,
        "nonce": nonce,
        "timestamp": ts,
        "Api-Version": "v1",
    }
    merged = {**params, **extra}
    r = "".join(f"{k}={merged[k]}&" for k in sorted(merged.keys()))
    r += KANKAN_SIGN_SECRET
    sign = _md5(_md5(r))
    return {**extra, "sign": sign}


async def resolve_kankan(request: AdapterRequest, client: httpx.AsyncClient) -> dict[str, Any]:
    channel_key = request.resource_id.strip("/").lower()
    channel = KANKAN_CHANNELS.get(channel_key)
    if not channel:
        if channel_key.isdigit():
            channel_id = channel_key
            channel_name = f"看看新闻-{channel_id}"
        else:
            supported = ", ".join(KANKAN_CHANNELS)
            raise AdapterResolveError(
                "invalid_kankan_channel_id",
                f"不支持的看看新闻频道 ID: {channel_key}，支持的频道有: {supported}",
            )
    else:
        channel_id = str(channel["id"])
        channel_name = channel["name"]

    # 生成签名 headers
    sign_params = {"channel_id": channel_id}
    sign_h = _make_sign(sign_params)

    headers = {
        "accept": "application/json, text/plain, */*",
        "api-version": "v1",
        "origin": "https://live.kankanews.com",
        "referer": "https://live.kankanews.com/",
        "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "platform": sign_h["platform"],
        "version": sign_h["version"],
        "nonce": sign_h["nonce"],
        "timestamp": sign_h["timestamp"],
        "sign": sign_h["sign"],
        "m-uuid": "Z002jbKjI1nzA06jOrEk0",
    }

    # 请求频道详情
    try:
        resp = await client.get(
            KANKAN_API_URL,
            params={"channel_id": channel_id},
            headers=headers,
            follow_redirects=True,
            timeout=10.0,
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise AdapterResolveError(
            "kankan_request_failed",
            f"看看新闻接口请求失败: {exc}",
            status_code=502,
            retryable=True,
        ) from exc

    data = resp.json()
    result = data.get("result")
    if not result or not result.get("live_address"):
        raise AdapterResolveError(
            "kankan_no_data",
            "看看新闻频道未找到或未开播",
            status_code=502,
            retryable=True,
        )

    # RSA 解密 live_address
    try:
        m3u8_url = _rsa_public_decrypt(result["live_address"])
    except Exception as exc:
        raise AdapterResolveError(
            "kankan_decrypt_failed",
            f"看看新闻地址解密失败: {exc}",
            status_code=502,
            retryable=True,
        ) from exc

    if not m3u8_url or not m3u8_url.startswith("http"):
        raise AdapterResolveError(
            "kankan_decrypt_failed",
            "看看新闻解密后地址无效",
            status_code=502,
            retryable=True,
        )

    return {
        "ok": True,
        "adapter": "kankan",
        "source_type": "hls",
        "url": m3u8_url,
        "direct_playable": True,
        "requires_proxy": False,
        "headers": {
            "Referer": "https://live.kankanews.com/",
            "Origin": "https://live.kankanews.com",
        },
        "ttl": 60 * 60,
        "expires_at": None,
        "warnings": [],
        "channel_id": channel_key,
        "channel_name": channel_name,
        "volatile_url": True,
    }
