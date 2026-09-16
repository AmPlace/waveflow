#!/usr/bin/env python3
"""Independent SXBC TV Provider with in-process AES-CBC decoding."""
from __future__ import annotations

import base64
import json
import re
from typing import Any

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from waveflow_plugin_sdk import InvalidResource, PluginApplication, PluginError, ResolveContext, StreamDescriptor, TVProvider, TVReference


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


def _failure(code: str, message: str) -> PluginError:
    return PluginError("TEMPORARY_UPSTREAM_FAILURE", message, retryable=True,
                       category="provider", details={"provider_code": code})


def _extract_encrypted_vars(source: str) -> tuple[str, str]:
    values = {match.group(1): match.group(3) for match in _VAR_RE.finditer(source)}
    s_tv, s_radio = values.get("sTV", "").strip(), values.get("sRadio", "").strip()
    if not s_tv or not s_radio or len(s_tv) <= 16 or len(s_radio) <= 16:
        raise _failure("sxbc_stream_js_parse_failed", "SXBC stream variables are missing or malformed")
    return s_tv, s_radio


def _decrypt_zero_padded(key: bytes, iv: bytes, payload: str) -> bytes:
    try:
        ciphertext = base64.b64decode(payload, validate=True)
        if not ciphertext or len(ciphertext) % 16:
            raise ValueError("ciphertext is not a complete AES block")
        decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        plaintext = decryptor.update(ciphertext) + decryptor.finalize()
        return plaintext.rstrip(b"\x00")
    except (ValueError, TypeError):
        raise _failure("sxbc_decrypt_failed", "SXBC encrypted payload could not be decrypted") from None


def _undress(key: bytes, iv: bytes, payload: str) -> Any:
    plaintext = _decrypt_zero_padded(key, iv, payload)
    try:
        return json.loads(plaintext.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise _failure("sxbc_decrypt_failed", "SXBC decrypted payload is not valid JSON") from None


def _managed_text(context: ResolveContext, url: str) -> str:
    response = context.capabilities.managed_http(
        url, headers=SXBC_HEADERS, response_mode="text", timeout=10,
    )
    return str(response.body or "")


def _load_channel_maps(context: ResolveContext) -> dict[str, dict[str, Any]]:
    last_error: PluginError | None = None
    source = ""
    for url in SXBC_STREAM_URLS:
        try:
            source = _managed_text(context, url)
        except PluginError as exc:
            if exc.code in {"CAPABILITY_DENIED", "PLUGIN_TIMEOUT", "AUTH_FAILED", "RATE_LIMITED"}:
                raise
            last_error = exc
            continue
        if "sTV" in source and "sRadio" in source:
            break
    else:
        if last_error:
            raise last_error
        raise _failure("sxbc_stream_js_failed", "SXBC channel table request failed")

    s_tv, s_radio = _extract_encrypted_vars(source)
    key, iv = s_tv[:16].encode("utf-8"), s_radio[:16].encode("utf-8")
    tv_payload, radio_payload = _undress(key, iv, s_tv[16:]), _undress(key, iv, s_radio[16:])

    def normalize(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        inner = value.get("sxbc")
        return inner if isinstance(inner, dict) else value

    return {"tv": normalize(tv_payload), "radio": normalize(radio_payload)}


def _select_tv(channel_maps: dict[str, dict[str, Any]], channel_id: str) -> dict[str, Any]:
    item = channel_maps.get("tv", {}).get(channel_id)
    if isinstance(item, dict):
        return item
    raise InvalidResource(f"Unsupported SXBC TV channel: {channel_id}")


class Provider(TVProvider):
    def resolve_stream(self, reference: TVReference, context: ResolveContext) -> StreamDescriptor:
        channel_id = reference.resource_id.strip("/").lower()
        preferred_type = str((reference.query.get("type") or ["tv"])[0] or "tv").lower()
        if preferred_type == "radio":
            raise PluginError(
                "RESOURCE_NOT_FOUND",
                "SXBC radio channels require the separate Radio Plugin contract",
                details={"provider_code": "sxbc_radio_requires_radio_plugin"},
            )
        channel_maps = _load_channel_maps(context)
        channel = _select_tv(channel_maps, channel_id)
        play_url = str(channel.get("m3u8") or "").strip()
        if not play_url or any(marker in play_url for marker in _BLOCKED_URL_MARKERS):
            raise _failure("sxbc_no_play_url", f"SXBC TV channel {channel_id} has no valid stream")
        if not play_url.startswith(("http://", "https://")):
            raise _failure("sxbc_no_play_url", f"SXBC TV channel {channel_id} has an invalid stream")
        return StreamDescriptor.hls(
            play_url, headers=SXBC_HEADERS, ttl_seconds=SXBC_TTL_SECONDS,
            volatile_url=True, requires_proxy=False,
        )


def main() -> None:
    identity, version = PluginApplication.identity_args("org.waveflow/sxbc")
    PluginApplication(identity=identity, version=version, permissions=["network"]).register_tv(
        "sxbc", Provider()
    ).run()


if __name__ == "__main__":
    main()
