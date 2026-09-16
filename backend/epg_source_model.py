"""Stable product identity and validation for EPG sources."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit


EPG_SOURCE_ORIGINS = frozenset({"builtin", "custom"})
EPG_SOURCE_NAME_MAX_LENGTH = 256
EPG_SOURCE_URL_MAX_LENGTH = 4096


@dataclass(frozen=True, slots=True)
class BuiltinEpgSourcePreset:
    key: str
    name: str
    url: str


BUILTIN_CHINA_EPG_KEY = "china_51zmt"
BUILTIN_CHINA_EPG_PRESET = BuiltinEpgSourcePreset(
    key=BUILTIN_CHINA_EPG_KEY,
    name="51zmt",
    url="http://epg.51zmt.top:8000/e.xml.gz",
)
BUILTIN_EPG_SOURCE_PRESETS = (BUILTIN_CHINA_EPG_PRESET,)


def validate_epg_source_name(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("EPG 来源名称必须是字符串")
    name = value.strip()
    if not name:
        raise ValueError("EPG 来源名称不能为空")
    if len(name) > EPG_SOURCE_NAME_MAX_LENGTH:
        raise ValueError(f"EPG 来源名称不能超过 {EPG_SOURCE_NAME_MAX_LENGTH} 个字符")
    return name


def validate_epg_source_url(value: object) -> str:
    """Validate an admin-provided XMLTV endpoint without normalizing secrets."""
    if not isinstance(value, str):
        raise TypeError("EPG URL 必须是字符串")
    url = value.strip()
    if not url:
        raise ValueError("EPG URL 不能为空")
    if len(url) > EPG_SOURCE_URL_MAX_LENGTH:
        raise ValueError(f"EPG URL 不能超过 {EPG_SOURCE_URL_MAX_LENGTH} 个字符")
    if any(ord(character) < 32 or character.isspace() for character in url):
        raise ValueError("EPG URL 包含非法空白或控制字符")
    try:
        parsed = urlsplit(url)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            raise ValueError("EPG URL 必须是有效的 HTTP 或 HTTPS 地址")
        # Accessing port validates malformed/non-numeric/out-of-range ports.
        _ = parsed.port
    except ValueError as error:
        if str(error).startswith("EPG URL"):
            raise
        raise ValueError("EPG URL 必须是有效的 HTTP 或 HTTPS 地址") from error
    return url


def validate_epg_source_origin(value: object) -> str:
    if value not in EPG_SOURCE_ORIGINS:
        raise ValueError("EPG source_origin 必须是 builtin 或 custom")
    return str(value)


def validate_builtin_key(value: object, *, required: bool) -> str | None:
    if value is None or value == "":
        if required:
            raise ValueError("builtin EPG 来源必须包含 builtin_key")
        return None
    if not isinstance(value, str):
        raise TypeError("builtin_key 必须是字符串")
    key = value.strip()
    if not key or len(key) > 128:
        raise ValueError("builtin_key 长度必须在 1 到 128 之间")
    if not all(character.islower() or character.isdigit() or character in "_-" for character in key):
        raise ValueError("builtin_key 只能包含小写字母、数字、下划线或连字符")
    return key


__all__ = [
    "BUILTIN_CHINA_EPG_KEY",
    "BUILTIN_CHINA_EPG_PRESET",
    "BUILTIN_EPG_SOURCE_PRESETS",
    "BuiltinEpgSourcePreset",
    "EPG_SOURCE_ORIGINS",
    "validate_builtin_key",
    "validate_epg_source_name",
    "validate_epg_source_origin",
    "validate_epg_source_url",
]
