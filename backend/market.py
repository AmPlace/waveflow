import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import time
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

import database as db
from m3u8_parser import (
    channel_name_semantics,
    detect_source_type,
    normalize_channel_name,
    parse_m3u,
    parse_youtube_video_id,
)
from plugin_runtime.manifest import RANGE_PART_RE, SUPPORTED_CONTRACTS
from ssrf_guard import UnsafeTargetError, assert_safe_target_url


SCHEMA_VERSION = 1
SUPPORTED_IMPORT_KINDS = {"playlist", "dynamic_playlist", "mixed"}
SUPPORTED_PACKAGE_KINDS = SUPPORTED_IMPORT_KINDS | {"logo_pack", "plugin_package"}
SUPPORTED_CHANNEL_SOURCE_TYPES = {"inline_channels", "playlist"}
CONTENT_PACKAGE_TYPE = "content_package"
PLUGIN_PACKAGE_TYPE = "plugin_package"
LOGO_PACKAGE_KIND = "logo_pack"
LOGO_CAPABILITY = "logos"
LOGO_MEDIA_TYPES = {"image/png", "image/webp", "image/jpeg"}
LOGO_MAX_ASSETS = 512
LOGO_MAX_ASSET_BYTES = 5 * 1024 * 1024
LOGO_MAX_ID_LENGTH = 128
LOGO_MAX_PATH_LENGTH = 256
INDEX_EXECUTION_FIELDS = {
    "defaults",
    "channel_sources",
    "inline_channels",
    "channels",
    "channels_url",
    "source_defaults",
    "headers",
    "assets",
    "logos",
}
RECOMMENDED_INDEX_FIELDS = {
    "id",
    "name",
    "description",
    "kind",
    "package_type",
    "version",
    "updated_at",
    "manifest_url",
    "regions",
    "operators",
    "providers",
    "languages",
    "categories",
    "tags",
    "status",
    "source_origin",
    "source_policy",
    "risk_level",
    "importable",
    "previewable",
    "supported_in_v1",
    "display",
    "publisher",
    "published_at",
    "compatibility",
    "replacement",
    "links",
    "catalog",
}
PACKAGE_STATUS_VALUES = {"active", "experimental", "stable", "deprecated", "broken", "unknown"}
CATEGORY_TAXONOMY = {
    "央视", "卫视", "地方", "新闻", "体育", "电影", "少儿", "教育", "纪实",
    "广播", "国际", "港澳台", "购物", "剧场", "动画", "音乐", "综艺", "戏曲",
    "财经", "生活", "影视", "电视", "插件", "Provider", "健康", "宗教", "民族", "韩流",
}
NON_OPERATOR_VALUES = {
    "cn", "jp", "hk", "tw", "mo", "global", "world", "ard", "zdf", "tdm",
    "youtube", "radiobrowser", "platform", "provider", "broadcaster", "香港",
    "澳门", "台湾", "福建", "北京", "上海", "广东", "湖南", "日本", "德国",
}
LEGACY_PACKAGE_FIELDS = {"region", "language", "provider"}
DEFAULT_MARKET_URL = "https://market.waveflow.tv/market.json"
OFFICIAL_MARKET_SOURCE_KEY = "official"
MARKET_URL = os.environ.get("WAVEFLOW_MARKET_URL", DEFAULT_MARKET_URL).strip() or DEFAULT_MARKET_URL
ALLOW_PRIVATE_MARKET_URLS = os.environ.get("WAVEFLOW_MARKET_ALLOW_PRIVATE", "").strip().lower() in {"1", "true", "yes", "on"}
PREVIEW_TTL_SECONDS = 10 * 60
MAX_FETCH_BYTES = 10 * 1024 * 1024
MARKET_REFRESH_ERROR_MAX_LENGTH = 2048

logger = logging.getLogger(__name__)

_market_cache: dict[str, Any] = {
    "market_url": MARKET_URL,
    "market": None,
    "markets": [],
    "packages": [],
    "sources": [],
    "source_entries": {},
    "fetched_at": 0,
    "stale": False,
    "last_error": "",
    "allow_private": ALLOW_PRIVATE_MARKET_URLS,
}
_preview_cache: dict[str, dict[str, Any]] = {}
_package_update_locks: dict[str, asyncio.Lock] = {}
_market_source_refresh_locks: dict[str, asyncio.Lock] = {}


class MarketError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _validate_fetch_url(url: str, *, allow_private: bool = False) -> str:
    normalized = (url or "").strip()
    parsed = urlparse(normalized)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise MarketError("只允许 http/https 远程资源", 400)
    if not parsed.hostname:
        raise MarketError("远程资源 URL 缺少 hostname", 400)

    try:
        # Market has one persisted private-network opt-in. Preserve its
        # historical loopback behavior while keeping hard-block and fake-IP
        # policy in the shared guard.
        await assert_safe_target_url(
            normalized,
            allow_private=allow_private,
            allow_loopback=allow_private,
            allowed_schemes={"http", "https"},
        )
    except UnsafeTargetError as exc:
        message = str(exc)
        status_code = 502 if message.startswith("域名解析失败:") or message == "域名没有可用解析结果" else 400
        raise MarketError(message, status_code) from exc
    return parsed.geturl()


async def safe_http_fetch(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    allow_private: bool = False,
    max_bytes: int = MAX_FETCH_BYTES,
    max_redirects: int = 3,
    timeout: float = 15.0,
) -> tuple[str, str, httpx.Headers]:
    """Fetch a remote market resource with scheme/IP checks before every request.

    V1 intentionally validates each redirect target. It does not actively probe
    stream content; callers interpret the returned text by manifest/URL rules.
    """
    allow_private = bool(allow_private or ALLOW_PRIVATE_MARKET_URLS)
    current_url = await _validate_fetch_url(url, allow_private=allow_private)
    request_headers = {k: v for k, v in (headers or {}).items() if v}

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=5.0), follow_redirects=False) as client:
            for redirect_count in range(max_redirects + 1):
                async with client.stream("GET", current_url, headers=request_headers) as resp:
                    if resp.status_code in {301, 302, 303, 307, 308}:
                        location = resp.headers.get("location")
                        if not location:
                            raise MarketError("远程资源重定向缺少 Location", 502)
                        if redirect_count >= max_redirects:
                            raise MarketError("远程资源重定向次数过多", 502)
                        current_url = await _validate_fetch_url(urljoin(current_url, location), allow_private=allow_private)
                        continue

                    try:
                        resp.raise_for_status()
                    except httpx.HTTPStatusError as exc:
                        raise MarketError(f"远程资源返回 HTTP {resp.status_code}", 502) from exc
                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in resp.aiter_bytes():
                        size += len(chunk)
                        if size > max_bytes:
                            raise MarketError("远程资源超过大小限制", 413)
                        chunks.append(chunk)
                    content = b"".join(chunks)
                    encoding = resp.encoding or "utf-8"
                    return current_url, content.decode(encoding, errors="replace"), resp.headers
    except httpx.HTTPError as exc:
        raise MarketError(f"远程资源拉取失败: {exc}", 502) from exc

    raise MarketError("远程资源拉取失败", 502)


def _as_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _clean_headers(headers: dict | None) -> dict[str, str]:
    allowed = {"User-Agent", "Referer", "Cookie"}
    result: dict[str, str] = {}
    for key, value in (headers or {}).items():
        canonical = next((item for item in allowed if item.lower() == str(key).lower()), str(key))
        if canonical in allowed and value is not None:
            result[canonical] = str(value)
    return result


def _merge_dict(base: dict | None, override: dict | None) -> dict:
    result = deepcopy(base or {})
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict) and value is not None:
            result[key] = _merge_dict(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def _merge_source_defaults(*items: dict | None) -> dict:
    result: dict[str, Any] = {}
    for item in items:
        if not item:
            continue
        headers = _merge_dict(result.get("headers", {}), item.get("headers", {}))
        result = _merge_dict(result, item)
        if headers:
            result["headers"] = headers
    result["headers"] = _clean_headers(result.get("headers", {}))
    return result


def _logo_safe_relative_path(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > LOGO_MAX_PATH_LENGTH:
        raise MarketError("Logo asset path 无效", 400)
    raw = value.replace("\\", "/").strip()
    path = PurePosixPath(raw)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise MarketError("Logo asset path 必须是安全的 package-relative path", 400)
    return "/".join(path.parts)


def _normalize_logo_assets(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > LOGO_MAX_ASSETS:
        raise MarketError("assets 必须是受限 array", 400)
    result: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_paths: set[str] = set()
    for raw in value:
        if not isinstance(raw, dict):
            raise MarketError("asset 项格式无效", 400)
        asset_id = str(raw.get("asset_id") or "").strip()
        if not asset_id or len(asset_id) > LOGO_MAX_ID_LENGTH or asset_id in seen_ids:
            raise MarketError("asset_id 无效或重复", 400)
        relative_path = _logo_safe_relative_path(raw.get("path") or raw.get("relative_path"))
        if relative_path in seen_paths:
            raise MarketError("asset path 重复", 400)
        media_type = str(raw.get("media_type") or "").strip().lower()
        if media_type not in LOGO_MEDIA_TYPES:
            raise MarketError("Logo 仅支持 PNG/WebP/JPEG", 400)
        try:
            size = int(raw.get("size"))
        except (TypeError, ValueError) as exc:
            raise MarketError("asset size 无效", 400) from exc
        digest = str(raw.get("sha256") or "").strip().lower()
        if size <= 0 or size > LOGO_MAX_ASSET_BYTES or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise MarketError("asset size 或 sha256 无效", 400)
        seen_ids.add(asset_id)
        seen_paths.add(relative_path)
        result.append({
            "asset_id": asset_id,
            "relative_path": relative_path,
            "media_type": media_type,
            "sha256": digest,
            "size_bytes": size,
        })
    return result


def _logo_match_keys(value: Any) -> list[str]:
    text = str(value or "").strip()
    if not text:
        return []
    keys: list[str] = []
    try:
        semantic = channel_name_semantics(text)
        keys.extend([str(semantic.get("canonical_candidate") or "").strip()])
    except Exception:
        pass
    try:
        keys.append(normalize_channel_name(text))
    except Exception:
        pass
    keys.append(text)
    return list(dict.fromkeys(item for item in keys if item))


def _normalize_logo_entries(value: Any, assets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > LOGO_MAX_ASSETS:
        raise MarketError("logos 必须是受限 array", 400)
    asset_ids = {item["asset_id"] for item in assets}
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for raw in value:
        if not isinstance(raw, dict):
            raise MarketError("logos 项格式无效", 400)
        asset_id = str(raw.get("logo_asset_id") or raw.get("asset_id") or "").strip()
        canonical_key = str(raw.get("canonical_key") or raw.get("channel_key") or "").strip()
        if not asset_id or asset_id not in asset_ids or not canonical_key:
            raise MarketError("logo entry 缺少有效 canonical_key/asset_id", 400)
        key = (canonical_key, asset_id)
        if key in seen:
            raise MarketError("logo entry 重复", 400)
        aliases = raw.get("aliases") or []
        if not isinstance(aliases, list):
            raise MarketError("logo aliases 必须是 array", 400)
        try:
            priority = max(0, min(100, int(raw.get("priority", 0))))
        except (TypeError, ValueError) as exc:
            raise MarketError("logo priority 无效", 400) from exc
        seen.add(key)
        result.append({
            "canonical_key": canonical_key[:LOGO_MAX_ID_LENGTH],
            "aliases": [str(alias).strip()[:LOGO_MAX_ID_LENGTH] for alias in aliases[:16] if str(alias).strip()],
            "asset_id": asset_id,
            "priority": priority,
        })
    return result


def _package_supported(package: dict) -> bool:
    if (
        package.get("package_type", CONTENT_PACKAGE_TYPE) == CONTENT_PACKAGE_TYPE
        and package.get("kind") == LOGO_PACKAGE_KIND
    ):
        return (
            bool(package.get("supported_in_v1", True))
            and LOGO_CAPABILITY in (package.get("content_capabilities") or [])
            and (
                bool(package.get("assets")) and bool(package.get("logos"))
                or bool(package.get("manifest_url"))
            )
        )
    return (
        package.get("package_type", CONTENT_PACKAGE_TYPE) == CONTENT_PACKAGE_TYPE
        and package.get("kind") in SUPPORTED_IMPORT_KINDS
        and bool(package.get("supported_in_v1", True))
    )


def _schema_warnings(package: dict, *, index: bool = False, manifest: bool = False) -> list[str]:
    warnings: list[str] = []
    if index:
        leaked = sorted(field for field in INDEX_EXECUTION_FIELDS if field in package)
        if leaked:
            warnings.append(f"market.json 索引不应包含执行配置字段: {', '.join(leaked)}")
        missing = sorted(field for field in RECOMMENDED_INDEX_FIELDS if field not in package)
        if missing:
            warnings.append(f"market.json 索引字段不完整: {', '.join(missing[:8])}{'…' if len(missing) > 8 else ''}")
    if manifest and package.get("kind") == LOGO_PACKAGE_KIND:
        if LOGO_CAPABILITY not in (package.get("content_capabilities") or []):
            warnings.append("logo_pack 必须声明 content_capabilities: logos")
        if not package.get("assets") or not package.get("logos"):
            warnings.append("logo_pack 缺少 assets 或 logos")
    elif manifest and package.get("kind") in SUPPORTED_IMPORT_KINDS:
        channel_sources = package.get("channel_sources")
        if not isinstance(channel_sources, list) or not channel_sources:
            warnings.append("manifest 缺少 channel_sources，无法预览或导入")
    return warnings


def _validate_package_minimal(raw: dict, *, context: str) -> None:
    if not isinstance(raw, dict):
        raise MarketError(f"{context} package 必须是 JSON object", 400)
    if not str(raw.get("id") or "").strip():
        raise MarketError(f"{context} package 缺少 id", 400)
    if not str(raw.get("kind") or "").strip():
        raise MarketError(f"{context} package 缺少 kind", 400)
    if context in {"market.json", "bundled official market"}:
        if not str(raw.get("name") or "").strip():
            raise MarketError(f"{context} package 缺少 name", 400)
        required = ("description", "package_type", "version", "updated_at")
        missing = [field for field in required if field not in raw]
        if missing:
            raise MarketError(f"{context} package 缺少字段: {', '.join(missing)}", 400)
        if raw.get("package_type") not in {CONTENT_PACKAGE_TYPE, PLUGIN_PACKAGE_TYPE}:
            raise MarketError(f"{context} package_type 无效", 400)
        if not isinstance(raw.get("description"), str):
            raise MarketError(f"{context} package description 必须是 string", 400)


def _normalize_plugin_requirements(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise MarketError("requires_plugins 必须是 array", 400)
    result: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"plugin", "version_range", "contract", "required_schemes"}:
            raise MarketError("requires_plugins 项格式无效", 400)
        identity = item.get("plugin")
        version_range = item.get("version_range")
        contract = item.get("contract")
        schemes = item.get("required_schemes")
        if (not isinstance(identity, str) or identity.count("/") != 1
                or not all(identity.split("/"))
                or not isinstance(version_range, str)
                or not version_range.split()
                or not all(RANGE_PART_RE.fullmatch(part) for part in version_range.split())
                or contract not in SUPPORTED_CONTRACTS
                or not isinstance(schemes, list) or not schemes
                or any(not isinstance(scheme, str) or not scheme for scheme in schemes)
                or len(set(schemes)) != len(schemes)):
            raise MarketError("requires_plugins 项格式无效", 400)
        result.append({
            "plugin": identity,
            "version_range": version_range,
            "contract": contract,
            "required_schemes": list(schemes),
        })
    result.sort(key=lambda item: (item["plugin"], item["contract"], item["version_range"], item["required_schemes"]))
    return result


async def ensure_market_sources() -> list[dict]:
    official = await db.get_market_source_by_key(OFFICIAL_MARKET_SOURCE_KEY)
    if not official:
        await db.upsert_market_source(
            source_key=OFFICIAL_MARKET_SOURCE_KEY,
            name="WaveFlow 官方 Market",
            url=MARKET_URL,
            enabled=1,
            allow_private=1 if ALLOW_PRIVATE_MARKET_URLS else 0,
            is_builtin=1,
        )
    return await db.list_market_sources()


async def list_sources() -> list[dict]:
    return await ensure_market_sources()


def _source_public(source: dict | None) -> dict:
    source = source or {}
    out = {
        "id": source.get("id"),
        "source_key": source.get("source_key", ""),
        "name": source.get("name", ""),
        "url": source.get("url", ""),
        "enabled": bool(source.get("enabled", 1)),
        "allow_private": bool(source.get("allow_private", 0)),
        "is_builtin": bool(source.get("is_builtin", 0)),
        "last_fetched_at": source.get("last_fetched_at", ""),
        "last_status": source.get("last_status", ""),
        "last_error": source.get("last_error", ""),
    }
    return out


def _package_source_id(source: dict, package_id: str) -> str:
    source_key = str(source.get("source_key") or "").strip()
    if source_key == OFFICIAL_MARKET_SOURCE_KEY:
        return package_id
    return f"{source_key}::{package_id}" if source_key else package_id


def _attach_source(package: dict, source: dict, raw_id: str | None = None) -> dict:
    result = deepcopy(package)
    original_id = raw_id or str(result.get("id") or "")
    result["original_id"] = original_id
    result["id"] = _package_source_id(source, original_id)
    result["market_source"] = _source_public(source)
    return result


def _source_cache_key(source: dict | None) -> str:
    source = source or {}
    return str(source.get("source_key") or source.get("id") or "").strip()


def _source_fetch_revision(source: dict | None) -> tuple[int, str, bool, bool]:
    source = source or {}
    return (
        int(source.get("id") or 0),
        str(source.get("url") or ""),
        bool(source.get("enabled")),
        bool(source.get("allow_private")),
    )


def _source_allow_private(source: dict | None) -> bool:
    source = source or {}
    return bool(source.get("allow_private") or ALLOW_PRIVATE_MARKET_URLS)


def _source_revision_value(source: dict | None) -> str:
    source_id, url, enabled, allow_private = _source_fetch_revision(source)
    return json.dumps(
        {
            "allow_private": allow_private,
            "enabled": enabled,
            "source_id": source_id,
            "url": url,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _sanitize_refresh_error(value: Any) -> str:
    text = str(value or "").replace("\x00", " ")
    text = re.sub(r"\b[A-Za-z]:\\[^\s;]+", "[local-path]", text)
    text = re.sub(
        r"(?<!:)/(?:Users|home|private|tmp|var/folders|opt)/[^\s;]+",
        "[local-path]",
        text,
    )
    text = re.sub(r"<[^>]+ object at 0x[0-9a-fA-F]+>", "[internal-object]", text)
    text = " ".join(text.split()).strip()
    if len(text) > MARKET_REFRESH_ERROR_MAX_LENGTH:
        return text[: MARKET_REFRESH_ERROR_MAX_LENGTH - 1].rstrip() + "…"
    return text


def _has_successful_source_cache(entry: dict | None, source: dict) -> bool:
    if not entry or str(entry.get("source_url") or "") != str(source.get("url") or ""):
        return False
    cached_source_allow_private = entry.get("source_allow_private")
    if cached_source_allow_private is not None and bool(cached_source_allow_private) != bool(source.get("allow_private")):
        return False
    cached_fetch_allow_private = entry.get("fetch_allow_private")
    if cached_fetch_allow_private is not None and bool(cached_fetch_allow_private) != _source_allow_private(source):
        return False
    if entry.get("has_successful_cache") is False:
        return False
    return bool(
        entry.get("has_successful_cache")
        or entry.get("last_success_at")
        or entry.get("market")
    )


def _source_refresh_result(
    source: dict,
    *,
    current: dict | None,
    status: str,
    error: Any = "",
    cache_updated: bool = False,
    packages: list[dict] | None = None,
) -> dict:
    accepted_packages = packages or []
    return {
        "source_id": int(source.get("id") or 0),
        "source_key": _source_cache_key(source),
        "source_name": str(source.get("name") or ""),
        "requested_url": str(source.get("url") or ""),
        "current_url": str((current or {}).get("url") or ""),
        "source_revision": _source_revision_value(source),
        "current_source_revision": _source_revision_value(current),
        "status": status,
        "usable_for_update": status == "success",
        "stale": status == "stale",
        "error": _sanitize_refresh_error(error),
        "cache_updated": bool(cache_updated),
        "package_count": len(accepted_packages),
        "package_ids": [
            str(package.get("id") or "")
            for package in accepted_packages
            if str(package.get("id") or "")
        ],
    }


def _with_refresh_results(summary: dict, source_results: list[dict]) -> dict:
    statuses = ("success", "stale", "failed", "revision_discarded", "disabled")
    counts = {
        status: sum(1 for result in source_results if result.get("status") == status)
        for status in statuses
    }
    unsuccessful = counts["stale"] + counts["failed"] + counts["revision_discarded"]
    if unsuccessful and counts["success"]:
        refresh_status = "partial"
    elif unsuccessful:
        refresh_status = "failed"
    else:
        refresh_status = "success"

    result = dict(summary)
    result.update({
        "refresh_status": refresh_status,
        "source_results": source_results,
        "source_result_counts": counts,
        "successful_source_ids": [
            item["source_id"] for item in source_results if item.get("status") == "success"
        ],
        "stale_source_ids": [
            item["source_id"] for item in source_results if item.get("status") == "stale"
        ],
        "failed_source_ids": [
            item["source_id"] for item in source_results if item.get("status") == "failed"
        ],
        "revision_discarded_source_ids": [
            item["source_id"]
            for item in source_results
            if item.get("status") == "revision_discarded"
        ],
        "disabled_source_ids": [
            item["source_id"] for item in source_results if item.get("status") == "disabled"
        ],
    })
    return result


def _source_entries() -> dict[str, dict[str, Any]]:
    entries = _market_cache.get("source_entries")
    if not isinstance(entries, dict):
        entries = {}
        _market_cache["source_entries"] = entries
    return entries


def _rebuild_market_cache(sources: list[dict], *, extra_errors: list[str] | None = None) -> None:
    entries = _source_entries()
    active_sources = [source for source in sources if source.get("enabled")]
    active_by_key = {_source_cache_key(source): source for source in active_sources}

    for key in list(entries):
        source = active_by_key.get(key)
        entry = entries.get(key) or {}
        cached_source_allow_private = entry.get("source_allow_private")
        if cached_source_allow_private is None:
            cached_source_allow_private = any(
                bool(item.get("_allow_private_fetch"))
                for item in entry.get("packages") or []
                if isinstance(item, dict)
            )
        if (
            not source
            or str(entry.get("source_url") or "") != str(source.get("url") or "")
            or bool(cached_source_allow_private) != bool(source.get("allow_private"))
            or bool(entry.get("fetch_allow_private", cached_source_allow_private)) != _source_allow_private(source)
        ):
            entries.pop(key, None)

    markets: list[dict] = []
    packages: list[dict] = []
    errors = list(extra_errors or [])
    stale = False
    for source in active_sources:
        key = _source_cache_key(source)
        entry = entries.get(key)
        if not entry:
            continue
        public_source = _source_public(source)
        market_doc = deepcopy(entry.get("market") or {})
        if market_doc:
            market_doc["_source"] = public_source
            markets.append(market_doc)
        for package in entry.get("packages") or []:
            item = deepcopy(package)
            item["market_source"] = public_source
            packages.append(item)
        if entry.get("stale"):
            stale = True
            if entry.get("last_error"):
                errors.append(f"{source.get('name') or source.get('url')}: {entry['last_error']}")

    error_text = _sanitize_refresh_error("; ".join(dict.fromkeys(error for error in errors if error)))
    _market_cache.update({
        "market_url": ", ".join(str(source.get("url") or "") for source in active_sources),
        "market": markets[0] if markets else {"schema_version": SCHEMA_VERSION, "packages": []},
        "markets": markets,
        "packages": packages,
        "sources": sources,
        "fetched_at": time.time(),
        "stale": bool(stale or error_text),
        "last_error": error_text,
        "allow_private": any(bool(source.get("allow_private")) for source in active_sources),
    })


async def _sync_source_cache_after_mutation(source_id: int | None = None, *, invalidate: bool = False) -> None:
    sources = await db.list_market_sources()
    if source_id is not None and invalidate:
        source = next((item for item in sources if int(item.get("id") or 0) == int(source_id)), None)
        key = _source_cache_key(source)
        if key:
            _source_entries().pop(key, None)
        else:
            for cached_key, entry in list(_source_entries().items()):
                if int(entry.get("source_id") or 0) == int(source_id):
                    _source_entries().pop(cached_key, None)
    _rebuild_market_cache(sources)


async def create_source(name: str, url: str, enabled: bool = True, allow_private: bool = False) -> dict:
    key = f"custom-{secrets.token_hex(4)}"
    source_id = await db.create_market_source(
        name=(name or "第三方 Market").strip(),
        url=url.strip(),
        source_key=key,
        enabled=1 if enabled else 0,
        allow_private=1 if allow_private else 0,
    )
    source = await db.get_market_source(source_id)
    await _sync_source_cache_after_mutation()
    return _source_public(source)


async def update_source(source_id: int, data: dict) -> dict:
    source = await db.get_market_source(source_id)
    if not source:
        raise MarketError("Market 源不存在", 404)
    updates = {}
    for key in ("name", "url"):
        if key in data:
            updates[key] = str(data.get(key) or "").strip()
    for key in ("enabled", "allow_private"):
        if key in data:
            updates[key] = 1 if data.get(key) else 0
    if source.get("is_builtin") and updates.get("url") == "":
        raise MarketError("内置 Market 源 URL 不能为空", 400)
    await db.update_market_source(source_id, **updates)
    updated = await db.get_market_source(source_id)
    invalidate = (
        str(updated.get("url") or "") != str(source.get("url") or "")
        or not bool(updated.get("enabled"))
        or bool(updated.get("allow_private")) != bool(source.get("allow_private"))
    )
    await _sync_source_cache_after_mutation(source_id, invalidate=invalidate)
    return _source_public(updated)


async def delete_source(source_id: int) -> dict:
    source = await db.get_market_source(source_id)
    if not source:
        raise MarketError("Market 源不存在", 404)
    try:
        await db.delete_market_source(source_id)
    except ValueError as exc:
        raise MarketError(str(exc), 400) from exc
    key = _source_cache_key(source)
    if key:
        _source_entries().pop(key, None)
    await _sync_source_cache_after_mutation()
    return {"ok": True}


# ── Package Presentation Contract V1 ─────────────────────────────────
# display 是 package/catalog 的用户可见语义边界。Backend 只做白名单投影，
# 不根据 name、region、operators 或 Plugin manifest 生成展示内容。

BADGE_TONES = {"neutral", "rose", "sky", "emerald", "orange", "violet"}
BADGE_TEXT_MAX_GRAPHEMES = 3
DISPLAY_TEXT_MAX_LENGTH = 240
DISPLAY_SUMMARY_MAX_LENGTH = 360
DISPLAY_BRAND_MAX_LENGTH = 64
DISPLAY_ICON_NAME_MAX_LENGTH = 64
DISPLAY_ICON_TYPES = {"builtin", "image"}
DISPLAY_BUILTIN_BRANDS = {
    "waveflow",
    "youtube",
    "china-mobile",
    "china-unicom",
    "china-telecom",
    "china-broadcast",
}


def _safe_text(value, *, max_chars: int) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = value.strip()
    if not cleaned:
        return ""
    # 拒绝 HTML / SVG / 标签 / 控制字符。
    if "<" in cleaned or ">" in cleaned:
        return ""
    if any(ord(ch) < 0x20 for ch in cleaned):
        return ""
    if len(cleaned) > max_chars:
        cleaned = cleaned[:max_chars]
    return cleaned


def _normalize_display(value) -> dict:
    if not isinstance(value, dict):
        return {}
    out: dict[str, Any] = {}
    subtitle = _safe_text(value.get("subtitle"), max_chars=DISPLAY_TEXT_MAX_LENGTH)
    if subtitle:
        out["subtitle"] = subtitle
    summary = _safe_text(value.get("summary"), max_chars=DISPLAY_SUMMARY_MAX_LENGTH)
    if summary:
        out["summary"] = summary

    identity_raw = value.get("identity")
    if isinstance(identity_raw, dict):
        identity: dict[str, Any] = {}
        brand = _safe_text(identity_raw.get("brand"), max_chars=DISPLAY_BRAND_MAX_LENGTH)
        if brand and brand.lower() in DISPLAY_BUILTIN_BRANDS:
            identity["brand"] = brand.lower()
        icon_raw = identity_raw.get("icon")
        if isinstance(icon_raw, dict) and icon_raw.get("type") in DISPLAY_ICON_TYPES:
            icon_type = icon_raw["type"]
            if icon_type == "builtin":
                name = _safe_text(icon_raw.get("name"), max_chars=DISPLAY_ICON_NAME_MAX_LENGTH)
                if name and re.fullmatch(r"[a-z0-9][a-z0-9._-]*", name.lower()):
                    identity["icon"] = {"type": "builtin", "name": name.lower()}
            else:
                url = str(icon_raw.get("url") or "").strip()
                parsed = urlparse(url)
                if (parsed.scheme.lower() == "https" and parsed.hostname
                        and not parsed.username and not parsed.password
                        and len(url) <= 2048):
                    identity["icon"] = {"type": "image", "url": parsed.geturl()}
        if identity:
            out["identity"] = identity

    badge_raw = value.get("badge")
    if isinstance(badge_raw, dict):
        text = _safe_text(badge_raw.get("text"), max_chars=BADGE_TEXT_MAX_GRAPHEMES * 4)
        # 简单按字符数（非 grapheme cluster；CJK 全角 + emoji 边界由长度上限近似处理）。
        if text:
            text = text[:BADGE_TEXT_MAX_GRAPHEMES] if len(text) > BADGE_TEXT_MAX_GRAPHEMES else text
        tone = badge_raw.get("tone")
        tone = tone if tone in BADGE_TONES else ""
        badge: dict[str, Any] = {}
        if text:
            badge["text"] = text
        if tone:
            badge["tone"] = tone
        if badge:
            out["badge"] = badge
    return out


def _normalize_text_list(value: Any, field: str, *, max_items: int = 128) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > max_items:
        raise MarketError(f"{field} 必须是受限 string array", 400)
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise MarketError(f"{field} 只能包含 string", 400)
        cleaned = item.strip()
        if not cleaned or len(cleaned) > 128 or any(ord(ch) < 0x20 for ch in cleaned):
            raise MarketError(f"{field} 包含无效值", 400)
        result.append(cleaned)
    return result


def _normalize_regions(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 64:
        raise MarketError("regions 必须是受限 array", 400)
    result: list[dict[str, Any]] = []
    for raw in value:
        if not isinstance(raw, dict):
            raise MarketError("regions 项格式无效", 400)
        if set(raw) - {"global", "country", "province", "city"}:
            raise MarketError("regions 项包含未知字段", 400)
        is_global = raw.get("global") is True
        if is_global:
            if any(raw.get(key) not in (None, "") for key in ("country", "province", "city")):
                raise MarketError("global region 不能同时声明 country/province/city", 400)
            result.append({"global": True})
            continue
        country = raw.get("country")
        if not isinstance(country, str) or not country.strip() or country.strip().lower() == "global":
            raise MarketError("非 global region 必须声明 country", 400)
        item: dict[str, Any] = {"country": country.strip().upper()}
        for key in ("province", "city"):
            value_item = raw.get(key)
            if value_item is None:
                item[key] = None
            elif isinstance(value_item, str) and value_item.strip():
                item[key] = value_item.strip()
            else:
                raise MarketError(f"regions.{key} 必须是 string 或 null", 400)
        result.append(item)
    return result


def _normalize_operators(value: Any) -> list[str]:
    operators = _normalize_text_list(value, "operators")
    for operator in operators:
        if operator.lower() in NON_OPERATOR_VALUES or operator in NON_OPERATOR_VALUES:
            raise MarketError(f"operators 包含非运营商值: {operator}", 400)
    return operators


def _normalize_categories(value: Any) -> list[str]:
    categories = _normalize_text_list(value, "categories")
    unknown = [item for item in categories if item not in CATEGORY_TAXONOMY]
    if unknown:
        raise MarketError(f"categories 包含未定义 taxonomy: {', '.join(unknown[:5])}", 400)
    return categories


def _normalize_languages(value: Any) -> list[str]:
    languages = _normalize_text_list(value, "languages")
    for language in languages:
        if not re.fullmatch(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*", language):
            raise MarketError(f"languages 包含无效 locale: {language}", 400)
    return languages


def _normalize_publisher(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise MarketError("publisher 必须是 object", 400)
    if set(value) - {"id", "name"}:
        raise MarketError("publisher 包含未知字段", 400)
    result = {}
    for key in ("id", "name"):
        item = value.get(key)
        if item is not None:
            if not isinstance(item, str) or not item.strip() or len(item.strip()) > 128:
                raise MarketError(f"publisher.{key} 无效", 400)
            result[key] = item.strip()
    if not result:
        raise MarketError("publisher 至少需要 id 或 name", 400)
    return result


def _normalize_compatibility(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise MarketError("compatibility 必须是 object", 400)
    result = {}
    for key, item in value.items():
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", key):
            raise MarketError("compatibility key 无效", 400)
        if not isinstance(item, str) or not item.strip() or len(item.strip()) > 128:
            raise MarketError("compatibility value 无效", 400)
        result[key] = item.strip()
    return result


def _normalize_links(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise MarketError("links 必须是 object", 400)
    allowed = {"homepage", "documentation", "source", "issues"}
    if set(value) - allowed:
        raise MarketError("links 包含未知字段", 400)
    result = {}
    for key, item in value.items():
        if item is None:
            continue
        if not isinstance(item, str):
            raise MarketError(f"links.{key} 必须是 string", 400)
        parsed = urlparse(item.strip())
        if parsed.scheme.lower() != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise MarketError(f"links.{key} 只允许无凭据 HTTPS URL", 400)
        result[key] = parsed.geturl()
    return result


def _normalize_catalog(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict) or set(value) - {"sort_weight", "featured"}:
        raise MarketError("catalog 格式无效", 400)
    result: dict[str, Any] = {}
    if "sort_weight" in value:
        try:
            weight = int(value["sort_weight"])
        except (TypeError, ValueError) as exc:
            raise MarketError("catalog.sort_weight 无效", 400) from exc
        if weight < -1000 or weight > 1000:
            raise MarketError("catalog.sort_weight 超出范围", 400)
        result["sort_weight"] = weight
    if "featured" in value:
        if not isinstance(value["featured"], bool):
            raise MarketError("catalog.featured 必须是 boolean", 400)
        result["featured"] = value["featured"]
    return result


def _normalize_package(raw: dict, *, manifest_url: str = "", market_url: str = "", schema_warnings: list[str] | None = None) -> dict:
    package = deepcopy(raw or {})
    package.setdefault("schema_version", SCHEMA_VERSION)
    if package.get("schema_version") != SCHEMA_VERSION:
        raise MarketError("不支持的 package schema_version", 400)
    legacy_fields = sorted(LEGACY_PACKAGE_FIELDS.intersection(package))
    if legacy_fields:
        raise MarketError(f"Package V1 不再接受旧字段: {', '.join(legacy_fields)}", 400)
    package.setdefault("id", "")
    package.setdefault("name", package.get("id") or "未命名 Market 包")
    package.setdefault("description", "")
    package.setdefault("kind", "playlist")
    if package["kind"] not in SUPPORTED_PACKAGE_KINDS:
        raise MarketError("不支持的 package kind", 400)
    package.setdefault("package_type", PLUGIN_PACKAGE_TYPE if package.get("kind") == PLUGIN_PACKAGE_TYPE else CONTENT_PACKAGE_TYPE)
    if package["package_type"] not in {CONTENT_PACKAGE_TYPE, PLUGIN_PACKAGE_TYPE}:
        raise MarketError("不支持的 package_type", 400)
    if package["package_type"] == PLUGIN_PACKAGE_TYPE and package["kind"] != PLUGIN_PACKAGE_TYPE:
        raise MarketError("plugin_package 必须使用 plugin_package kind", 400)
    if package["package_type"] == CONTENT_PACKAGE_TYPE and package["kind"] == PLUGIN_PACKAGE_TYPE:
        raise MarketError("content_package 不能使用 plugin_package kind", 400)
    package["requires_plugins"] = _normalize_plugin_requirements(package.get("requires_plugins"))
    package.setdefault("plugin_manifest", None)
    package.setdefault("artifact_references", [])
    package.setdefault("version", "")
    package.setdefault("updated_at", "")
    package["regions"] = _normalize_regions(package.get("regions"))
    package["operators"] = _normalize_operators(package.get("operators"))
    package["providers"] = _normalize_text_list(package.get("providers"), "providers")
    package["languages"] = _normalize_languages(package.get("languages"))
    package["categories"] = _normalize_categories(package.get("categories"))
    package["tags"] = _normalize_text_list(package.get("tags"), "tags")
    package.setdefault("status", "unknown")
    if package["status"] not in PACKAGE_STATUS_VALUES:
        raise MarketError("status 无效", 400)
    package.setdefault("source_origin", "unknown")
    package.setdefault("source_policy", "unknown")
    package.setdefault("risk_level", "unknown")
    package["publisher"] = _normalize_publisher(package.get("publisher"))
    package["compatibility"] = _normalize_compatibility(package.get("compatibility"))
    package["links"] = _normalize_links(package.get("links"))
    package["catalog"] = _normalize_catalog(package.get("catalog"))
    if package.get("published_at") is None:
        package["published_at"] = ""
    if not isinstance(package["published_at"], str):
        raise MarketError("published_at 必须是 string", 400)
    if package.get("replacement") is not None:
        if not isinstance(package["replacement"], str) or not package["replacement"].strip():
            raise MarketError("replacement 必须是 package id", 400)
        package["replacement"] = package["replacement"].strip()
    package.setdefault("defaults", {})
    package.setdefault("channel_sources", [])
    package.setdefault("channel_count", 0)
    package.setdefault("source_count", 0)
    capabilities = package.get("content_capabilities") or []
    if not isinstance(capabilities, list):
        raise MarketError("content_capabilities 必须是 array", 400)
    package["content_capabilities"] = list(dict.fromkeys(
        str(item).strip() for item in capabilities if str(item).strip()
    ))
    package["assets"] = _normalize_logo_assets(package.get("assets"))
    package["logos"] = _normalize_logo_entries(package.get("logos"), package["assets"])
    try:
        package["logo_priority"] = max(0, min(100, int(package.get("logo_priority", 0))))
    except (TypeError, ValueError) as exc:
        raise MarketError("logo_priority 无效", 400) from exc
    package.setdefault("contributors", [])
    package.setdefault("manifest_url", manifest_url)
    package.setdefault("market_url", market_url)
    package.setdefault("schema_warnings", [])
    package["display"] = _normalize_display(package.get("display"))
    if schema_warnings:
        package["schema_warnings"] = list(dict.fromkeys([*package.get("schema_warnings", []), *schema_warnings]))

    supported = _package_supported(package)
    package["supported_in_v1"] = supported
    package["previewable"] = bool(package.get("previewable", supported)) and supported and package.get("kind") != LOGO_PACKAGE_KIND
    package["importable"] = bool(package.get("importable", supported)) and supported
    package["plugin_installable"] = bool(
        package["package_type"] == PLUGIN_PACKAGE_TYPE
        and isinstance(package.get("plugin_manifest"), dict)
    )
    if not supported and not package.get("unsupported_reason"):
        package["unsupported_reason"] = "当前版本仅展示，暂不支持预览或导入"
    elif "unsupported_reason" not in package:
        package["unsupported_reason"] = None
    return package


async def _load_manifest(index_item: dict, market_url: str, *, allow_private: bool = False) -> dict:
    manifest_url = index_item.get("manifest_url") or index_item.get("url") or ""
    if manifest_url:
        resolved_url = urljoin(market_url, manifest_url)
        final_url, text, _headers = await safe_http_fetch(resolved_url, allow_private=allow_private)
        try:
            manifest = json.loads(text)
        except json.JSONDecodeError as exc:
            raise MarketError(f"manifest JSON 解析失败: {exc}", 400) from exc
        return _normalize_package(manifest, manifest_url=final_url, market_url=market_url)
    return _normalize_package(index_item, market_url=market_url)


def _cache_package(package: dict) -> None:
    package_id = package.get("id")
    if not package_id:
        return
    packages = _market_cache.get("packages") or []
    for index, item in enumerate(packages):
        if item.get("id") == package_id:
            packages[index] = package
            break
    source_key = _source_cache_key(package.get("market_source"))
    entry = _source_entries().get(source_key)
    if entry:
        for index, item in enumerate(entry.get("packages") or []):
            if item.get("id") == package_id:
                entry["packages"][index] = deepcopy(package)
                break


async def _resolve_package_manifest(package: dict) -> dict:
    if package.get("_manifest_loaded") or not package.get("manifest_url"):
        return package

    manifest_url = urljoin(str(package.get("market_url") or ""), str(package.get("manifest_url") or ""))
    allow_private = await _package_allow_private(package)
    final_url, text, _headers = await safe_http_fetch(manifest_url, allow_private=allow_private)
    try:
        manifest = json.loads(text)
    except json.JSONDecodeError as exc:
        raise MarketError(f"manifest JSON 解析失败: {exc}", 400) from exc

    _validate_package_minimal(manifest, context="manifest")
    manifest_warnings = _schema_warnings(manifest, manifest=True)
    if manifest.get("kind") == LOGO_PACKAGE_KIND and (
        not isinstance(manifest.get("assets"), list)
        or not manifest.get("assets")
        or not isinstance(manifest.get("logos"), list)
        or not manifest.get("logos")
    ):
        raise MarketError("Logo Package manifest 缺少 assets 或 logos", 400)
    if any("缺少 channel_sources" in warning for warning in manifest_warnings):
        raise MarketError("; ".join(manifest_warnings), 400)

    merged = _merge_dict(package, manifest)
    source = package.get("market_source") or {}
    original_id = str(package.get("original_id") or manifest.get("id") or package.get("id") or "")
    loaded = _normalize_package(merged, manifest_url=final_url, market_url=package.get("market_url", ""), schema_warnings=manifest_warnings)
    loaded = _attach_source(loaded, source, original_id)
    loaded["_allow_private_fetch"] = allow_private
    loaded["_manifest_loaded"] = True
    _cache_package(loaded)
    return loaded


async def _package_allow_private(package: dict) -> bool:
    """Resolve private-fetch authority from the current durable source policy.

    The package cache is only a hint.  A source permission can be revoked
    while a stale package object is still being resolved, so the final
    decision must consult the current source row before any network fetch.
    """
    market_source = package.get("market_source") or {}
    source = None
    source_id = market_source.get("id")
    if source_id is not None:
        try:
            source = await db.get_market_source(int(source_id))
        except (TypeError, ValueError):
            source = None
    if source is None:
        source_key = str(market_source.get("source_key") or "").strip()
        if source_key:
            source = await db.get_market_source_by_key(source_key)
    if source is not None:
        if not bool(source.get("enabled", 1)):
            return False
        return _source_allow_private(source)
    if source_id is not None or str(market_source.get("source_key") or "").strip():
        # A package carrying a durable source identity must not fall back to
        # its stale cached permission after that source is deleted.
        return False
    if ALLOW_PRIVATE_MARKET_URLS:
        return True
    return bool(package.get("_allow_private_fetch"))


async def _load_source_packages(source: dict) -> tuple[dict, list[dict]]:
    url = str(source.get("url") or "").strip()
    if not url:
        raise MarketError("Market 源 URL 不能为空", 400)

    allow_private = _source_allow_private(source)
    try:
        final_url, text, _headers = await safe_http_fetch(url, allow_private=allow_private)
    except Exception:
        if str(source.get("source_key") or "") == OFFICIAL_MARKET_SOURCE_KEY:
            return _load_bundled_official_source(source)
        raise
    try:
        market = json.loads(text)
    except json.JSONDecodeError as exc:
        raise MarketError(f"market.json 解析失败: {exc}", 400) from exc
    if market.get("schema_version", SCHEMA_VERSION) != SCHEMA_VERSION:
        raise MarketError("不支持的 Market schema_version", 400)

    # Legacy root-level presentation rules are ignored. They are not part of
    # V1 semantics, but must not invalidate an otherwise loadable catalog.

    def broken_projection(item: Any, fallback_id: str, error: Exception) -> dict:
        """Keep one invalid index item visible without re-validating bad fields.

        A breaking schema rejects the item, but a malformed package must not
        make the whole Market source disappear. Only safe identity/status
        fields are retained for the unsupported card.
        """
        raw = item if isinstance(item, dict) else {}
        kind = raw.get("kind") if raw.get("kind") in SUPPORTED_PACKAGE_KINDS else "playlist"
        safe = {
            "id": fallback_id,
            "name": str(raw.get("name") or fallback_id),
            "description": str(raw.get("description") or ""),
            "kind": kind,
            "package_type": PLUGIN_PACKAGE_TYPE if kind == PLUGIN_PACKAGE_TYPE else CONTENT_PACKAGE_TYPE,
            "version": str(raw.get("version") or ""),
            "updated_at": str(raw.get("updated_at") or ""),
            "status": raw.get("status") if raw.get("status") in PACKAGE_STATUS_VALUES else "unknown",
        }
        broken = _normalize_package(safe, market_url=final_url, schema_warnings=[str(error)])
        broken["supported_in_v1"] = False
        broken["previewable"] = False
        broken["importable"] = False
        broken["unsupported_reason"] = f"索引校验失败: {error}"
        return broken

    packages = []
    for item in market.get("packages", []):
        try:
            _validate_package_minimal(item, context="market.json")
            raw_id = str(item.get("id") or "")
            index_warnings = _schema_warnings(item, index=True)
            loaded = _normalize_package(item, market_url=final_url, schema_warnings=index_warnings)
            loaded["_manifest_loaded"] = not bool(loaded.get("manifest_url"))
            packages.append(_attach_source(loaded, source, raw_id or loaded.get("id")))
        except Exception as exc:
            fallback_id = str(item.get("id") or f"invalid-{len(packages) + 1}") if isinstance(item, dict) else f"invalid-{len(packages) + 1}"
            broken = broken_projection(item, fallback_id, exc)
            packages.append(_attach_source(broken, source, fallback_id))
    for package in packages:
        package["_allow_private_fetch"] = allow_private
    if str(source.get("source_key") or "") == OFFICIAL_MARKET_SOURCE_KEY:
        try:
            _bundled_market, bundled = _load_bundled_official_source(source)
        except Exception:
            logger.exception("Bundled official Plugin fallback is unavailable; using remote official Market")
        else:
            by_id = {str(item.get("original_id") or item.get("id") or ""): index
                     for index, item in enumerate(packages)}
            for fallback in bundled:
                identity = str(fallback.get("original_id") or fallback.get("id") or "")
                current_index = by_id.get(identity)
                if current_index is None:
                    packages.append(fallback)
                    continue
                remote = packages[current_index]
                if _version_status(str(remote.get("version") or ""), str(fallback.get("version") or "")) != "upgrade":
                    # The release-local copy is the immutable baseline for the same
                    # version. Remote sources can only supersede it with a newer one.
                    packages[current_index] = fallback
            market["distribution"] = "remote_with_bundled_fallback"
    market["_source"] = _source_public({**source, "url": final_url})
    return market, packages


def _load_bundled_official_source(source: dict) -> tuple[dict, list[dict]]:
    from official_plugin_distribution import load_bundled_official_market

    market, raw_packages = load_bundled_official_market()
    packages = []
    for item in raw_packages:
        _validate_package_minimal(item, context="bundled official market")
        raw_id = str(item.get("id") or "")
        loaded = _normalize_package(item, market_url=str(source.get("url") or ""))
        loaded["_manifest_loaded"] = True
        loaded["_allow_private_fetch"] = False
        packages.append(_attach_source(loaded, source, raw_id))
    market["_source"] = _source_public(source)
    market["_bundled_fallback"] = True
    return market, packages


async def refresh_market(
    market_url: str | None = None,
    *,
    allow_private: bool = False,
    source_id: int | None = None,
) -> dict:
    all_sources = await ensure_market_sources()
    if market_url:
        source = next((item for item in all_sources if item.get("source_key") == "custom"), None)
        if not source:
            custom_id = await db.create_market_source(
                name="自定义 Market",
                url=market_url.strip(),
                source_key="custom",
                enabled=1,
                allow_private=1 if allow_private else 0,
            )
            source = await db.get_market_source(custom_id)
        else:
            await db.update_market_source(source["id"], url=market_url.strip(), enabled=1, allow_private=1 if allow_private else 0)
            source = await db.get_market_source(source["id"])
        targets = [source] if source else []
    elif source_id:
        source = await db.get_market_source(source_id)
        if not source:
            raise MarketError("Market 源不存在", 404)
        targets = [source]
    else:
        targets = [source for source in all_sources if source.get("enabled")]

    source_results: list[dict] = []
    if not targets:
        sources_latest = await db.list_market_sources()
        _rebuild_market_cache(sources_latest)
        if any(source.get("enabled") for source in sources_latest):
            return _with_refresh_results(market_summary(), source_results)
        _market_cache.update({
            "market_url": "",
            "market": {"schema_version": SCHEMA_VERSION, "packages": []},
            "markets": [],
            "packages": [],
            "sources": sources_latest,
            "source_entries": {},
            "fetched_at": time.time(),
            "stale": False,
            "last_error": "",
            "allow_private": bool(allow_private or ALLOW_PRIVATE_MARKET_URLS),
        })
        return _with_refresh_results(market_summary(), source_results)

    source_errors: list[str] = []
    entries = _source_entries()
    for source in targets:
        if not source.get("enabled"):
            current = await db.get_market_source(source["id"])
            source_results.append(_source_refresh_result(
                source,
                current=current,
                status="disabled",
            ))
            continue
        key = _source_cache_key(source)
        lock = _market_source_refresh_locks.setdefault(key, asyncio.Lock())
        async with lock:
            current = await db.get_market_source(source["id"])
            if not current or _source_fetch_revision(current) != _source_fetch_revision(source):
                source_results.append(_source_refresh_result(
                    source,
                    current=current,
                    status="revision_discarded",
                    error="Market 来源在刷新开始前已变化，旧请求未执行",
                ))
                continue
            try:
                market, source_packages = await _load_source_packages(source)
            except Exception as exc:
                current = await db.get_market_source(source["id"])
                if not current or _source_fetch_revision(current) != _source_fetch_revision(source):
                    source_results.append(_source_refresh_result(
                        source,
                        current=current,
                        status="revision_discarded",
                        error="Market 来源在失败结果返回前已变化，旧结果已丢弃",
                    ))
                    continue
                error = _sanitize_refresh_error(exc)
                source_errors.append(f"{source.get('name') or source.get('url')}: {error}")
                cached = entries.get(key)
                has_successful_cache = _has_successful_source_cache(cached, source)
                if not cached or str(cached.get("source_url") or "") != str(source.get("url") or ""):
                    cached = {
                        "source_id": source.get("id"),
                        "source_url": source.get("url", ""),
                        "source_allow_private": bool(source.get("allow_private")),
                        "fetch_allow_private": _source_allow_private(source),
                        "market": {},
                        "packages": [],
                        "fetched_at": time.time(),
                        "has_successful_cache": False,
                    }
                    entries[key] = cached
                cached["stale"] = True
                cached["last_error"] = error
                await db.update_market_source(
                    source["id"],
                    last_fetched_at=_now_iso(),
                    last_status="error",
                    last_error=error,
                )
                source_results.append(_source_refresh_result(
                    source,
                    current=current,
                    status="stale" if has_successful_cache else "failed",
                    error=error,
                ))
                continue

            current = await db.get_market_source(source["id"])
            if not current or _source_fetch_revision(current) != _source_fetch_revision(source):
                source_results.append(_source_refresh_result(
                    source,
                    current=current,
                    status="revision_discarded",
                    error="Market 来源在成功结果返回前已变化，旧结果已丢弃",
                ))
                continue
            success_at = time.time()
            refresh_status = "bundled" if market.get("_bundled_fallback") else "success"
            entries[key] = {
                "source_id": source.get("id"),
                "source_url": source.get("url", ""),
                "source_allow_private": bool(source.get("allow_private")),
                "fetch_allow_private": _source_allow_private(source),
                "market": market,
                "packages": source_packages,
                "fetched_at": success_at,
                "last_success_at": success_at,
                "has_successful_cache": True,
                "stale": False,
                "last_error": "",
            }
            await db.update_market_source(
                source["id"],
                last_fetched_at=_now_iso(),
                last_status="bundled" if refresh_status == "bundled" else "ok",
                last_error="",
            )
            source_results.append(_source_refresh_result(
                source,
                current=current,
                status=refresh_status,
                cache_updated=True,
                packages=source_packages,
            ))

    sources_latest = await db.list_market_sources()
    _rebuild_market_cache(sources_latest, extra_errors=source_errors)
    return _with_refresh_results(market_summary(), source_results)


def market_summary() -> dict:
    market = _market_cache.get("market") or {"schema_version": SCHEMA_VERSION, "packages": []}
    sources = _market_cache.get("sources") or []
    return {
        "schema_version": market.get("schema_version", SCHEMA_VERSION),
        "market_version": market.get("market_version", ""),
        "updated_at": market.get("updated_at", ""),
        "market_url": _market_cache.get("market_url", ""),
        "package_count": len(_market_cache.get("packages") or []),
        "source_count": len(sources),
        "enabled_source_count": len([source for source in sources if source.get("enabled")]),
        "sources": [_source_public(source) for source in sources],
        "fetched_at": _market_cache.get("fetched_at", 0),
        "stale": bool(_market_cache.get("stale")),
        "last_error": _market_cache.get("last_error", ""),
        "allow_private": bool(_market_cache.get("allow_private")),
        "preview_cache": "V1 单进程内存缓存，服务重启后会清空",
    }


def market_packages_snapshot() -> list[dict]:
    return deepcopy(_market_cache.get("packages") or [])


async def ensure_market_loaded() -> None:
    if _market_cache.get("packages") or _market_cache.get("market") is not None:
        return
    sources = await ensure_market_sources()
    _market_cache["sources"] = sources
    try:
        await refresh_market()
    except Exception as exc:
        _market_cache.update({
            "market": {"schema_version": SCHEMA_VERSION, "packages": []},
            "markets": [],
            "packages": [],
            "sources": await db.list_market_sources(),
            "fetched_at": time.time(),
            "stale": False,
            "last_error": str(exc),
        })


async def _installed_map() -> dict[str, dict]:
    rows = await db.list_market_installs()
    result: dict[str, dict] = {}
    for row in rows:
        sub_id = row.get("installed_subscription_id")
        sub = await db.get_subscription(sub_id) if sub_id else None
        if not sub:
            try:
                metadata = json.loads(row.get("metadata_json") or "{}")
            except json.JSONDecodeError:
                metadata = {}
            if metadata.get("kind") != LOGO_PACKAGE_KIND:
                await db.delete_market_install(row["package_id"])
                continue
        row["subscription"] = sub
        result[row["package_id"]] = row
    for row in await db.list_plugin_installations():
        package_id = str(row.get("source_package_id") or "")
        if not package_id:
            continue
        result[package_id] = {
            "package_id": package_id,
            "installed_version": row.get("active_version") or row.get("installed_version") or "",
            "installed_subscription_id": None,
            "auto_update": True,
            "plugin_identity": f"{row['publisher_id']}/{row['plugin_id']}",
            "trust_state": str(row.get("trust_state") or ""),
        }
    return result


def _installed_metadata(install: dict | None) -> dict:
    if not install:
        return {}
    try:
        return json.loads(install.get("metadata_json") or "{}")
    except json.JSONDecodeError:
        return {}


def _safe_persisted_market_url(value: Any) -> str:
    """Keep restart metadata useful without persisting URL query secrets."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        return urlparse(raw)._replace(query="", fragment="").geturl()
    except ValueError:
        return ""


def _safe_persisted_market_source(value: Any) -> dict:
    source = deepcopy(value) if isinstance(value, dict) else {}
    source.pop("url", None)
    return source


_MARKET_PACKAGE_SNAPSHOT_FIELDS = (
    "id",
    "original_id",
    "name",
    "description",
    "kind",
    "package_type",
    "version",
    "updated_at",
    "regions",
    "operators",
    "providers",
    "languages",
    "categories",
    "tags",
    "status",
    "source_origin",
    "source_policy",
    "risk_level",
    "requires_proxy",
    "requires_resolver",
    "requires_cookie",
    "requires_referer",
    "requires_custom_ua",
    "channel_count",
    "source_count",
    "health",
    "compatibility",
    "publisher",
    "published_at",
    "replacement",
    "links",
    "catalog",
    "contributors",
    "importable",
    "previewable",
    "supported_in_v1",
    "unsupported_reason",
    "manifest_url",
    "market_url",
    "market_source",
    "display",
    "package_type",
    "content_capabilities",
    "logo_priority",
)


def _market_package_snapshot(package: dict) -> dict:
    """Persist only safe package index metadata for restart projection."""
    snapshot = {
        key: deepcopy(package[key])
        for key in _MARKET_PACKAGE_SNAPSHOT_FIELDS
        if key in package
    }
    for key in ("market_url", "manifest_url"):
        if key in snapshot:
            snapshot[key] = _safe_persisted_market_url(snapshot[key])
    source = snapshot.get("market_source")
    if isinstance(source, dict):
        snapshot["market_source"] = _safe_persisted_market_source(source)
    return snapshot


def _installed_package_fallback(install: dict | None) -> dict | None:
    if not install or install.get("plugin_identity"):
        return None
    metadata = _installed_metadata(install)
    snapshot = metadata.get("market_package")
    package = deepcopy(snapshot) if isinstance(snapshot, dict) else {}
    package_id = str(install.get("package_id") or "").strip()
    if not package_id:
        return None
    package.setdefault("id", package_id)
    package.setdefault("original_id", package_id)
    package.setdefault("name", metadata.get("name") or package_id)
    package.setdefault(
        "kind",
        metadata.get("kind") or (LOGO_PACKAGE_KIND if not install.get("installed_subscription_id") else "playlist"),
    )
    package.setdefault("package_type", metadata.get("package_type") or CONTENT_PACKAGE_TYPE)
    package.setdefault("version", install.get("installed_version") or metadata.get("version") or "")
    package.setdefault("market_url", _safe_persisted_market_url(install.get("market_url")))
    package.setdefault("market_source", metadata.get("market_source") or {})
    package["importable"] = False
    package["previewable"] = False
    package["supported_in_v1"] = False
    package["catalog_unavailable"] = True
    package["catalog_stale"] = True
    package["source_origin"] = package.get("source_origin") or "installed_state"
    package["unsupported_reason"] = "Market 源当前不可用，已保留已安装状态"
    return package


def _catalog_with_installed_fallback(
    catalog_packages: list[dict],
    installed: dict[str, dict],
) -> list[dict]:
    packages = list(catalog_packages)
    catalog_ids = {str(package.get("id") or "") for package in packages}
    for package_id in sorted(installed):
        if package_id in catalog_ids:
            continue
        fallback = _installed_package_fallback(installed.get(package_id))
        if fallback:
            packages.append(fallback)
    return packages


_NUMERIC_VERSION_RE = re.compile(r"^[vV]?(\d+(?:[._-]\d+)*)$")


def _numeric_version(value: str) -> tuple[int, ...] | None:
    match = _NUMERIC_VERSION_RE.fullmatch((value or "").strip())
    if not match:
        return None
    return tuple(int(part) for part in re.split(r"[._-]", match.group(1)))


def _version_status(current_version: str, installed_version: str) -> str:
    current = str(current_version or "").strip()
    installed = str(installed_version or "").strip()
    if not current or not installed:
        return "unknown"
    if current == installed:
        return "same"
    current_numeric = _numeric_version(current)
    installed_numeric = _numeric_version(installed)
    if current_numeric is None or installed_numeric is None:
        return "different"
    width = max(len(current_numeric), len(installed_numeric))
    current_padded = current_numeric + (0,) * (width - len(current_numeric))
    installed_padded = installed_numeric + (0,) * (width - len(installed_numeric))
    if current_padded > installed_padded:
        return "upgrade"
    if current_padded < installed_padded:
        return "downgrade"
    return "same"


def _update_available(package: dict, install: dict | None) -> bool:
    if not install:
        return False
    return _version_status(
        package.get("version", ""),
        install.get("installed_version", ""),
    ) == "upgrade"


async def list_packages(filters: dict[str, str | bool]) -> list[dict]:
    await ensure_market_loaded()
    installed = await _installed_map()
    packages = []
    catalog_packages = _catalog_with_installed_fallback(
        list(_market_cache.get("packages") or []), installed,
    )
    search = str(filters.get("search") or "").strip().lower()
    region = str(filters.get("region") or "").strip()
    operator = str(filters.get("operator") or "").strip()
    provider = str(filters.get("provider") or "").strip()
    kind = str(filters.get("kind") or "").strip()
    status = str(filters.get("status") or "").strip()
    category = str(filters.get("category") or "").strip()
    tag = str(filters.get("tag") or "").strip()
    supported_only = bool(filters.get("supported_only"))
    importable_only = bool(filters.get("importable_only"))

    for package in catalog_packages:
        region_values = _package_region_values(package)
        haystack = _package_search_haystack(package)
        if search and search not in haystack:
            continue
        if region and region not in region_values:
            continue
        if operator and operator not in (package.get("operators") or []):
            continue
        if provider and provider not in (package.get("providers") or []):
            continue
        if kind and package.get("kind") != kind:
            continue
        if status and package.get("status") != status:
            continue
        if category and category not in (package.get("categories") or []):
            continue
        if tag and tag not in (package.get("tags") or []):
            continue
        if supported_only and not (
            package.get("supported_in_v1")
            or package.get("plugin_installable")
            or package.get("catalog_unavailable")
        ):
            continue
        if importable_only and not package.get("importable"):
            continue
        item = _package_card(package)
        install = installed.get(package.get("id"))
        item["installed"] = bool(install)
        item["installed_version"] = install.get("installed_version", "") if install else ""
        item["installed_subscription_id"] = install.get("installed_subscription_id") if install else None
        item["auto_update"] = bool(install.get("auto_update")) if install else False
        item["version_status"] = _version_status(
            package.get("version", ""),
            install.get("installed_version", "") if install else "",
        )
        item["update_available"] = _update_available(package, install)
        if install and install.get("plugin_identity"):
            item["installed_trust_state"] = install.get("trust_state") or ""
        packages.append(item)
    return packages


def _package_region_values(package: dict) -> list[str]:
    values: list[str] = []
    for region in package.get("regions") or []:
        if region.get("global") is True:
            values.append("global")
        else:
            values.extend(str(region.get(key) or "") for key in ("country", "province", "city"))
    return [value for value in values if value]


def _package_search_haystack(package: dict) -> str:
    values = [
        package.get("id", ""),
        package.get("original_id", ""),
        package.get("name", ""),
        package.get("description", ""),
        package.get("published_at", ""),
        " ".join(_package_region_values(package)),
        " ".join(package.get("operators") or []),
        " ".join(package.get("providers") or []),
        " ".join(package.get("languages") or []),
        " ".join(package.get("categories") or []),
        " ".join(package.get("tags") or []),
    ]
    publisher = package.get("publisher") or {}
    if isinstance(publisher, dict):
        values.extend([publisher.get("id", ""), publisher.get("name", "")])
    manifest = package.get("plugin_manifest") or {}
    if isinstance(manifest, dict):
        values.extend([manifest.get("publisher_id", ""), manifest.get("display_name", "")])
        values.extend(
            str(item.get("scheme") or "")
            for item in manifest.get("owned_schemes") or []
            if isinstance(item, dict)
        )
    return " ".join(str(value or "") for value in values).lower()


def _package_card(package: dict) -> dict:
    keys = [
        "id", "name", "description", "kind", "version", "published_at", "updated_at", "regions",
        "operators", "providers", "languages", "categories", "tags", "status", "source_origin",
        "source_policy", "risk_level", "requires_proxy", "requires_resolver",
        "requires_cookie", "requires_referer", "requires_custom_ua", "channel_count",
        "source_count", "health", "compatibility", "contributors", "importable",
        "previewable", "supported_in_v1", "unsupported_reason", "schema_warnings",
        "manifest_url", "market_url", "market_source", "display", "installed",
        "installed_version", "auto_update", "update_available",
        "version_status", "publisher", "replacement", "links", "catalog",
        "package_type", "requires_plugins", "plugin_manifest",
        "plugin_installable", "content_capabilities", "asset_count", "logo_count",
        "logo_priority", "catalog_unavailable", "catalog_stale",
    ]
    card = {key: deepcopy(package.get(key)) for key in keys if key in package}
    card["asset_count"] = len(package.get("assets") or [])
    card["logo_count"] = len(package.get("logos") or [])
    if card.get("package_type") == PLUGIN_PACKAGE_TYPE:
        manifest = card.pop("plugin_manifest", None) or {}
        if isinstance(manifest, dict):
            raw_permissions = manifest.get("permissions") or {}
            permissions = []
            network = raw_permissions.get("network") if isinstance(raw_permissions, dict) else None
            if isinstance(network, dict):
                if network.get("managed") is True:
                    permissions.append("network.managed")
                if network.get("direct") is True:
                    permissions.append("network.direct")
                if network.get("allow_http") is True:
                    permissions.append("network.managed_http")
            if isinstance(raw_permissions, dict):
                permissions.extend(
                    str(key) for key, value in raw_permissions.items()
                    if key != "network" and value is True
                )
            card["plugin"] = {
                "publisher_id": str(manifest.get("publisher_id") or ""),
                "plugin_id": str(manifest.get("plugin_id") or ""),
                "display_name": str(manifest.get("display_name") or package.get("name") or ""),
                "version": str(manifest.get("version") or package.get("version") or ""),
                "provider_contracts": deepcopy(manifest.get("provider_contracts") or []),
                "owned_schemes": deepcopy(manifest.get("owned_schemes") or []),
                "platforms": [
                    {key: artifact.get(key) for key in ("os", "arch", "runtime")}
                    for artifact in manifest.get("artifacts") or [] if isinstance(artifact, dict)
                ],
                "permissions": sorted(set(permissions)),
                "dependencies": [],
            }
            seen_dependencies: set[tuple[Any, Any]] = set()
            lock_artifacts = (manifest.get("runtime", {}).get("dependency_lock", {}).get("artifacts", [])
                              if isinstance(manifest.get("runtime"), dict) else [])
            for item in lock_artifacts:
                if not isinstance(item, dict):
                    continue
                dependency_key = (item.get("name"), item.get("version"))
                if dependency_key in seen_dependencies:
                    continue
                seen_dependencies.add(dependency_key)
                card["plugin"]["dependencies"].append({
                    "name": item.get("name"), "version": item.get("version"),
                })
    return card


async def get_package(package_id: str, *, include_internal: bool = False) -> dict:
    await ensure_market_loaded()
    installed = await _installed_map()
    package = next((item for item in _market_cache.get("packages") or [] if item.get("id") == package_id), None)
    if not package:
        package = _installed_package_fallback(installed.get(package_id))
    if not package:
        raise MarketError("Market 包不存在", 404)
    try:
        package = await _resolve_package_manifest(package)
    except Exception as exc:
        package = deepcopy(package)
        package["supported_in_v1"] = False
        package["previewable"] = False
        package["importable"] = False
        package["unsupported_reason"] = f"manifest 加载失败: {exc}"
        _cache_package(package)
    result = deepcopy(package)
    # Trusted local resource roots are lifecycle inputs, never API data.
    if not include_internal:
        result.pop("_asset_root", None)
        result.pop("_bundled_asset_root", None)
    if result.get("package_type") == PLUGIN_PACKAGE_TYPE:
        # Local artifact references are lifecycle-service inputs, not an admin
        # read projection. In particular, never expose Core filesystem paths.
        result.pop("artifact_references", None)
        result.pop("dependency_references", None)
    install = installed.get(package_id)
    result["installed"] = bool(install)
    result["installed_version"] = install.get("installed_version", "") if install else ""
    result["installed_subscription_id"] = install.get("installed_subscription_id") if install else None
    result["auto_update"] = bool(install.get("auto_update")) if install else False
    result["version_status"] = _version_status(
        package.get("version", ""),
        install.get("installed_version", "") if install else "",
    )
    result["update_available"] = _update_available(package, install)
    if install and install.get("plugin_identity"):
        result["installed_trust_state"] = install.get("trust_state") or ""
    return result


async def _run_epg_binding_maintenance(trigger: str) -> None:
    """Run post-commit EPG maintenance without changing Market outcomes."""
    try:
        from epg_maintenance import run_epg_binding_maintenance
        result = await run_epg_binding_maintenance(sync_logical=True, trigger=trigger)
        if result.get('status') != 'success':
            logger.warning(
                'market_epg_binding_maintenance_deferred',
                extra={
                    'epg_binding_maintenance': {
                        'trigger': trigger,
                        'error': str(result.get('error') or '')[:512],
                    }
                },
            )
    except asyncio.CancelledError:
        raise
    except Exception as error:
        logger.warning(
            'market_epg_binding_maintenance_trigger_failed',
            extra={
                'epg_binding_maintenance': {
                    'trigger': trigger,
                    'error_type': type(error).__name__,
                }
            },
        )


async def uninstall_package(package_id: str) -> dict:
    lock = _package_update_locks.setdefault(package_id, asyncio.Lock())
    async with lock:
        old_assets = await db.get_package_assets(package_id)
        uninstalled = await db.uninstall_market_package_atomic(package_id)
        if uninstalled:
            await _remove_old_logo_files(old_assets, [])
    if uninstalled:
        await _run_epg_binding_maintenance('market_uninstall')
    return {"ok": True, "uninstalled": uninstalled}


async def update_install_config(package_id: str, *, auto_update: bool | None = None) -> dict:
    installed = await db.get_market_install(package_id)
    if not installed:
        raise MarketError("Market 包尚未安装", 404)
    values: dict[str, int] = {}
    if auto_update is not None:
        values["auto_update"] = 1 if auto_update else 0
    if values:
        await db.update_market_install(package_id, **values)
    updated = await db.get_market_install(package_id)
    return {
        "ok": True,
        "package_id": package_id,
        "auto_update": bool((updated or {}).get("auto_update")),
    }


async def update_installed_package(package_id: str) -> dict:
    preflight = await db.get_market_install(package_id)
    if not preflight:
        raise MarketError("Market 包尚未安装", 404)

    # The snapshot is only a preflight hint.  Re-read the durable install
    # record after acquiring the same lifecycle lock used by uninstall and
    # compare its durable generation before allowing reinstall=True.
    lock = _package_update_locks.setdefault(package_id, asyncio.Lock())
    async with lock:
        installed = await db.get_market_install(package_id)
        if not installed:
            raise MarketError("Market 包安装状态已变化，更新已中止", 409)
        if (
            str(installed.get("installed_at") or "") != str(preflight.get("installed_at") or "")
            or str(installed.get("installed_subscription_id") or "") != str(
                preflight.get("installed_subscription_id") or ""
            )
            or str(installed.get("installed_version") or "") != str(preflight.get("installed_version") or "")
        ):
            raise MarketError("Market 包安装状态已变化，更新已中止", 409)
        return await _import_package_locked(package_id, reinstall=True)


async def run_installed_updates(auto_update_only: bool = False) -> dict:
    await ensure_market_loaded()
    rows = await db.list_market_installs()
    packages = {
        str(package.get("id") or ""): package
        for package in (_market_cache.get("packages") or [])
    }
    results: list[dict[str, Any]] = []
    updated = 0
    skipped = 0
    failed = 0

    for install in rows:
        package_id = str(install.get("package_id") or "").strip()
        if not package_id:
            continue
        if auto_update_only and not install.get("auto_update"):
            skipped += 1
            results.append({
                "package_id": package_id,
                "status": "skipped",
                "reason": "auto_update disabled",
            })
            continue
        package = packages.get(package_id)
        if not package:
            skipped += 1
            results.append({
                "package_id": package_id,
                "status": "skipped",
                "reason": "package missing from current market",
                "version_status": "unknown",
            })
            continue
        version_status = _version_status(
            package.get("version", ""),
            install.get("installed_version", ""),
        )
        if version_status != "upgrade":
            skipped += 1
            results.append({
                "package_id": package_id,
                "status": "skipped",
                "reason": f"version status: {version_status}",
                "version_status": version_status,
            })
            continue
        sub_id = install.get("installed_subscription_id")
        sub = await db.get_subscription(sub_id) if sub_id else None
        if not sub:
            await db.delete_market_install(package_id)
            skipped += 1
            results.append({
                "package_id": package_id,
                "status": "skipped",
                "reason": "installed subscription missing",
            })
            continue
        try:
            result = await update_installed_package(package_id)
            updated += 1
            results.append({
                "package_id": package_id,
                "status": "updated",
                "subscription_id": result.get("subscription_id"),
                "channel_count": result.get("channel_count", 0),
                "source_count": result.get("source_count", 0),
                "version_status": version_status,
            })
        except Exception as exc:
            failed += 1
            results.append({
                "package_id": package_id,
                "status": "failed",
                "error": str(exc),
            })

    return {
        "ok": True,
        "updated": updated,
        "skipped": skipped,
        "failed": failed,
        "results": results,
    }


def _source_type_for(source: dict) -> str:
    declared = str(source.get("type") or source.get("source_type") or "").strip()
    if declared:
        return declared.lower()
    return detect_source_type(str(source.get("url") or ""))


def _source_headers(source: dict) -> dict[str, str]:
    return _clean_headers(source.get("headers") or {})


def _source_tracking_ids(channel: dict, source: dict, package: dict, channel_source: dict, source_index: int) -> dict[str, str]:
    package_id = str(package.get("id") or "").strip()
    channel_source_id = str(channel_source.get("id") or channel_source.get("name") or "").strip()
    channel_id = str(
        channel.get("id")
        or channel.get("canonical_key")
        or channel.get("tvg_id")
        or (channel.get("epg") or {}).get("tvg_id")
        or channel.get("name")
        or ""
    ).strip()
    explicit_source_id = str(source.get("id") or source.get("source_id") or "").strip()
    if explicit_source_id:
        source_item_id = explicit_source_id
    else:
        seed = "|".join([
            package_id,
            channel_source_id,
            channel_id,
            str(source_index),
            str(source.get("url") or ""),
        ])
        source_item_id = f"auto-{hashlib.sha1(seed.encode('utf-8')).hexdigest()[:16]}"
    return {
        "market_package_id": package_id,
        "market_source_id": channel_source_id,
        "market_channel_id": channel_id,
        "market_source_item_id": source_item_id,
    }


def _normalize_source(
    channel: dict,
    source: dict,
    package: dict,
    channel_source: dict,
    source_defaults: dict,
    source_index: int,
    channel_overrides: dict | None = None,
) -> tuple[dict | None, str | None]:
    if isinstance(source, str):
        source = {"url": source}
    # merge 顺序（左 → 右，后者覆盖前者）：
    #   包级 source_defaults  <  源级 source  <  频道级 channel_overrides
    # 频道级最高，符合"频道配置压过源/包"的语义。
    merged = _merge_source_defaults(source_defaults, source, channel_overrides)
    url = str(merged.get("url") or "").strip()
    source_type = _source_type_for(merged)
    headers = _source_headers(merged)
    custom_ua = headers.get("User-Agent", "")
    referer = headers.get("Referer", "")
    has_cookie = bool(headers.get("Cookie") or merged.get("requires_cookie"))

    if has_cookie:
        return None, f"{channel.get('name', '未命名频道')} 源需要 Cookie，V1 已跳过"
    if source_type in {"provider", "remote_resolver", "bilibili_live", "douyin_live", "huya_live", "douyu_live", "unknown"}:
        return None, f"{channel.get('name', '未命名频道')} 源类型 {source_type} V1 暂不支持"
    if not url:
        return None, f"{channel.get('name', '未命名频道')} 源缺少 URL"

    from rtsp_playback import normalize_rtsp_timestamp_mode

    return {
        "name": str(channel.get("name") or "未命名频道"),
        "url": url,
        "logo_url": str(channel.get("logo") or channel.get("logo_url") or ""),
        "logo_asset_id": str(channel.get("logo_asset_id") or ""),
        "group_name": str(channel.get("group_name") or channel.get("group") or _first(channel.get("categories")) or "其他"),
        "tvg_id": str((channel.get("epg") or {}).get("tvg_id") or channel.get("tvg_id") or channel.get("id") or ""),
        "tvg_name": str((channel.get("epg") or {}).get("tvg_name") or channel.get("tvg_name") or channel.get("name") or ""),
        "source_type": source_type,
        "youtube_video_id": str(merged.get("youtube_video_id") or parse_youtube_video_id(url)),
        "custom_ua": custom_ua,
        "referer": referer,
        "rtsp_timestamp_mode": normalize_rtsp_timestamp_mode(
            merged.get("rtsp_timestamp_mode")
        ),
        "force_proxy": 1 if (
            merged.get("requires_proxy")
            or merged.get("requires_referer")
            or merged.get("requires_custom_ua")
            or custom_ua
            or referer
        ) else 0,
        **_source_tracking_ids(channel, merged, package, channel_source, source_index),
    }, None


def _first(value: Any) -> str:
    items = _as_list(value)
    return str(items[0]) if items else ""


async def _channels_from_inline(source: dict, package: dict) -> tuple[list[dict], list[str]]:
    warnings: list[str] = []
    channels = list(source.get("channels") or [])
    channels_url = str(source.get("channels_url") or "").strip()
    allow_private = await _package_allow_private(package)
    if channels_url:
        headers = _clean_headers(source.get("headers") or {})
        final_url, text, _headers = await safe_http_fetch(
            urljoin(package.get("manifest_url") or package.get("market_url") or "", channels_url),
            headers=headers,
            allow_private=allow_private,
        )
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise MarketError(f"channels_url JSON 解析失败: {exc}", 400) from exc
        if isinstance(data, list):
            channels.extend(data)
        else:
            channels.extend(data.get("channels") or [])
        warnings.append(f"已从 channels_url 加载频道: {final_url}")
    return channels, warnings


async def _channels_from_playlist(source: dict, package: dict) -> tuple[list[dict], list[str]]:
    url = str(source.get("url") or "").strip()
    if not url:
        raise MarketError("playlist channel_source 缺少 url", 400)
    headers = _clean_headers(source.get("headers") or {})
    final_url, text, _resp_headers = await safe_http_fetch(
        url,
        headers=headers,
        allow_private=await _package_allow_private(package),
    )
    channels = parse_m3u(text)
    warnings = [f"已解析动态订阅: {final_url}"]
    for ch in channels:
        # m3u 里 #EXTVLCOPT/#KODIPROP/#WAVEFLOW 描述的是"这个频道整体的播放
        # 要求"——属于频道级，因此写到 channel.defaults.source；优先级
        # build_preview 里高于 sources[i]（源级）。
        ch_overrides_headers: dict[str, str] = {}
        if ch.get("referer"):
            ch_overrides_headers["Referer"] = str(ch["referer"])
        if ch.get("custom_ua"):
            ch_overrides_headers["User-Agent"] = str(ch["custom_ua"])
        ch_overrides: dict = {}
        if ch_overrides_headers:
            ch_overrides["headers"] = ch_overrides_headers
        if ch.get("force_proxy"):
            ch_overrides["requires_proxy"] = True
        if ch_overrides:
            existing = (ch.get("defaults") or {}).get("source") or {}
            ch.setdefault("defaults", {})["source"] = {**existing, **ch_overrides,
                "headers": {**existing.get("headers", {}), **ch_overrides.get("headers", {})}}
        ch.setdefault("sources", [{"url": ch.get("url", "")}])
    return channels, warnings


async def build_preview(package_id: str) -> dict:
    package = await get_package(package_id, include_internal=True)
    if not package.get("previewable"):
        raise MarketError(package.get("unsupported_reason") or "该包当前版本不可预览", 400)

    defaults = package.get("defaults") or {}
    channel_defaults = defaults.get("channel") or {}
    package_source_defaults = defaults.get("source") or {}
    entries: list[dict] = []
    warnings: list[str] = []
    unsupported_source_count = 0

    for channel_source in package.get("channel_sources") or []:
        source_type = channel_source.get("type")
        if source_type not in SUPPORTED_CHANNEL_SOURCE_TYPES:
            warnings.append(f"channel_source {source_type} V1 暂不支持，已跳过")
            continue
        if source_type == "inline_channels":
            channels, source_warnings = await _channels_from_inline(channel_source, package)
        else:
            channels, source_warnings = await _channels_from_playlist(channel_source, package)
        warnings.extend(source_warnings)

        source_defaults = _merge_source_defaults(
            package_source_defaults,
            channel_source.get("source_defaults"),
        )
        for raw_channel in channels:
            channel = _merge_dict(channel_defaults, raw_channel)
            # channel.defaults.source 是\u300c频道级\u300d配置，最高优先；
            # source_defaults 只承载\u300c包级 + channel_source 级\u300d的默认。
            channel_overrides = (channel.get("defaults") or {}).get("source") or {}
            raw_sources = channel.get("sources")
            if not raw_sources and channel.get("url"):
                raw_sources = [{"url": channel.get("url"), "source_type": channel.get("source_type")}]
            for source_index, raw_source in enumerate(raw_sources or []):
                entry, warning = _normalize_source(
                    channel,
                    raw_source,
                    package,
                    channel_source,
                    source_defaults,
                    source_index,
                    channel_overrides=channel_overrides,
                )
                if entry:
                    entries.append(entry)
                else:
                    unsupported_source_count += 1
                    if warning:
                        warnings.append(warning)

    preview_id = secrets.token_urlsafe(18)
    now = time.time()
    result = {
        "preview_id": preview_id,
        "package": _package_card(package),
        "channels": entries[:200],
        "channel_count": len({entry["name"] for entry in entries}),
        "source_count": len(entries),
        "direct_source_count": len([entry for entry in entries if not entry.get("force_proxy") and not entry.get("custom_ua") and not entry.get("referer")]),
        "proxy_source_count": len([entry for entry in entries if entry.get("force_proxy") or entry.get("custom_ua") or entry.get("referer")]),
        "unsupported_source_count": unsupported_source_count,
        "warnings": list(dict.fromkeys([*(package.get("schema_warnings") or []), *warnings]))[:50],
        "created_at": now,
        "expires_at": now + PREVIEW_TTL_SECONDS,
        "cache_note": "V1 单进程内存缓存，服务重启后会清空",
    }
    _preview_cache[preview_id] = {
        **result,
        "all_channels": entries,
        "manifest": package,
    }
    _drop_expired_previews()
    return result


def _drop_expired_previews() -> None:
    now = time.time()
    for preview_id in list(_preview_cache):
        if _preview_cache[preview_id].get("expires_at", 0) < now:
            _preview_cache.pop(preview_id, None)


def _logo_store_root() -> Path:
    configured = os.environ.get("WAVEFLOW_MARKET_ASSET_ROOT", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    db_path = os.environ.get("WAVEFLOW_DB_PATH", "").strip()
    if db_path and db_path != ":memory:":
        return (Path(db_path).expanduser().resolve().parent / "market_assets").resolve()
    return (Path(__file__).resolve().parent / "data" / "market_assets").resolve()


def _trusted_logo_asset_root(package: dict) -> Path:
    # Asset bytes must come from a trusted bundled/developer-local package
    # resource root.  A public manifest cannot supply an arbitrary filesystem
    # path; remote asset URLs are deliberately unsupported in V1.
    candidate = package.get("_asset_root") or package.get("_bundled_asset_root")
    if not candidate:
        raise MarketError("Logo Package 缺少受控的本地资源根目录", 400)
    root = Path(str(candidate)).expanduser().resolve()
    if not root.is_dir():
        raise MarketError("Logo Package 资源根目录不存在", 400)
    return root


def _asset_magic_matches(media_type: str, payload: bytes) -> bool:
    if media_type == "image/png":
        return payload.startswith(b"\x89PNG\r\n\x1a\n")
    if media_type == "image/jpeg":
        return payload.startswith(b"\xff\xd8\xff")
    if media_type == "image/webp":
        return len(payload) >= 12 and payload[:4] == b"RIFF" and payload[8:12] == b"WEBP"
    return False


async def _stage_logo_assets(package: dict) -> tuple[list[dict], Path | None]:
    assets = package.get("assets") or []
    if not assets:
        return [], None
    source_root = _trusted_logo_asset_root(package)
    store_root = _logo_store_root()
    package_key = hashlib.sha256(str(package.get("id") or "").encode("utf-8")).hexdigest()[:24]
    version_key = hashlib.sha256(str(package.get("version") or "").encode("utf-8")).hexdigest()[:24]
    token = secrets.token_hex(8)
    staging = store_root / ".staging" / token
    final_dir = store_root / package_key / version_key / token

    def _stage() -> tuple[list[dict], Path]:
        try:
            staging.mkdir(parents=True, exist_ok=False)
            staged: list[dict] = []
            for asset in assets:
                relative = str(asset["relative_path"])
                source = (source_root / relative).resolve()
                if source != source_root and source_root not in source.parents:
                    raise MarketError("Logo asset path 越过 package 根目录", 400)
                if not source.is_file() or source.is_symlink():
                    raise MarketError(f"Logo asset 不存在: {relative}", 400)
                payload = source.read_bytes()
                if len(payload) != int(asset["size_bytes"]):
                    raise MarketError(f"Logo asset size 不匹配: {asset['asset_id']}", 400)
                digest = hashlib.sha256(payload).hexdigest()
                if digest != str(asset["sha256"]):
                    raise MarketError(f"Logo asset digest 不匹配: {asset['asset_id']}", 400)
                if not _asset_magic_matches(str(asset["media_type"]), payload):
                    raise MarketError(f"Logo asset media_type 不匹配: {asset['asset_id']}", 400)
                target = staging / asset["asset_id"]
                target.write_bytes(payload)
                staged.append({
                    **asset,
                    "stored_path": str(final_dir / asset["asset_id"]),
                })
            final_dir.parent.mkdir(parents=True, exist_ok=True)
            staging.rename(final_dir)
            return staged, final_dir
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise

    worker = asyncio.create_task(asyncio.to_thread(_stage))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        # ``to_thread`` cannot cancel filesystem work already in flight. Wait
        # for its result, remove a late-published directory, then propagate the
        # cancellation to the caller.
        try:
            _staged, late_final_dir = await asyncio.shield(worker)
        except BaseException:
            pass
        else:
            await _remove_logo_stage_dir(late_final_dir)
        raise


async def _remove_logo_stage_dir(final_dir: Path | None) -> None:
    if not final_dir:
        return
    root = _logo_store_root()
    try:
        resolved = final_dir.resolve()
        root_resolved = root.resolve()
    except OSError:
        return
    if resolved == root_resolved or root_resolved not in resolved.parents:
        return

    def _remove() -> None:
        shutil.rmtree(resolved, ignore_errors=True)
        parent = resolved.parent
        while parent != root_resolved and root_resolved in parent.parents:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent
        try:
            (root_resolved / ".staging").rmdir()
        except OSError:
            pass

    await asyncio.to_thread(_remove)


async def _logo_binding_specs(package: dict, preview_entries: list[dict] | None = None) -> list[dict]:
    logical_rows = [
        row for row in await db.get_iptv_logical_channels()
        if str(row.get("status") or "") != "orphaned"
    ]
    if not logical_rows:
        return []
    exact: dict[str, list[dict]] = {}
    normalized: dict[str, list[dict]] = {}
    for row in logical_rows:
        canonical = str(row.get("canonical_key") or "").strip()
        exact.setdefault(canonical, []).append(row)
        for key in _logo_match_keys(canonical):
            normalized.setdefault(key, []).append(row)

    specs: list[dict] = []
    if package.get("kind") == LOGO_PACKAGE_KIND:
        for entry in package.get("logos") or []:
            candidates = [(str(entry.get("canonical_key") or ""), "exact")]
            candidates.extend((alias, "alias") for alias in entry.get("aliases") or [])
            for key, match_kind in candidates:
                matches = exact.get(key, [])
                match_type = "stable_identity" if matches and key in exact else match_kind
                if not matches:
                    normalized_matches = []
                    for candidate_key in _logo_match_keys(key):
                        normalized_matches.extend(normalized.get(candidate_key, []))
                    unique = {str(row["id"]): row for row in normalized_matches}
                    matches = list(unique.values()) if len(unique) == 1 else []
                    match_type = "normalized" if matches else match_type
                if len(matches) != 1:
                    continue
                specs.append({
                    "logical_channel_id": str(matches[0]["id"]),
                    "asset_id": entry["asset_id"],
                    "binding_type": "logo_pack",
                    "match_type": match_type,
                    "match_key": key,
                    "priority": int(package.get("logo_priority") or 0) + int(entry.get("priority") or 0),
                })
    else:
        entries = preview_entries or []
        for entry in entries:
            asset_id = str(entry.get("logo_asset_id") or "")
            if not asset_id:
                continue
            keys = _logo_match_keys(entry.get("name")) + _logo_match_keys(entry.get("tvg_name"))
            found: dict[str, tuple[dict, str, str]] = {}
            for key in keys:
                matches = exact.get(key, [])
                if len(matches) == 1:
                    found[str(matches[0]["id"])] = (matches[0], "stable_identity", key)
                    continue
                normalized_matches: dict[str, dict] = {}
                for normalized_key in _logo_match_keys(key):
                    for row in normalized.get(normalized_key, []):
                        normalized_matches[str(row["id"])] = row
                if len(normalized_matches) == 1:
                    row = next(iter(normalized_matches.values()))
                    found[str(row["id"])] = (row, "normalized", key)
            for row, match_type, key in found.values():
                specs.append({
                    "logical_channel_id": str(row["id"]),
                    "asset_id": asset_id,
                    "binding_type": "content_package",
                    "match_type": match_type,
                    "match_key": key,
                    "priority": int(package.get("logo_priority") or 0),
                })
    unique: dict[tuple[str, str, str, str], dict] = {}
    for spec in specs:
        unique[(spec["logical_channel_id"], spec["asset_id"], spec["match_type"], spec["match_key"])] = spec
    return list(unique.values())


async def _publish_logo_state(
    package: dict,
    staged_assets: list[dict],
    preview_entries: list[dict] | None = None,
) -> None:
    bindings = await _logo_bindings_for_package(package, staged_assets, preview_entries)
    await db.replace_package_logo_state(
        str(package["id"]), str(package.get("version") or ""), staged_assets, bindings,
    )


async def _logo_bindings_for_package(
    package: dict,
    staged_assets: list[dict],
    preview_entries: list[dict] | None = None,
) -> list[dict]:
    bindings = await _logo_binding_specs(package, preview_entries)
    valid_asset_ids = {item["asset_id"] for item in staged_assets}
    return [item for item in bindings if item["asset_id"] in valid_asset_ids]


async def _remove_old_logo_files(old_assets: list[dict], new_assets: list[dict]) -> None:
    keep = {str(item.get("stored_path") or "") for item in new_assets}
    paths = {str(item.get("stored_path") or "") for item in old_assets} - keep
    paths -= await db.get_referenced_package_asset_paths(list(paths))
    root = _logo_store_root()

    def _remove() -> None:
        for raw in paths:
            path = Path(raw)
            try:
                resolved = path.resolve()
                root_resolved = root.resolve()
            except OSError:
                continue
            if (
                not raw
                or path.is_symlink()
                or not path.is_file()
                or resolved == root_resolved
                or root_resolved not in resolved.parents
            ):
                continue
            try:
                path.unlink()
            except OSError:
                continue
        parents = {Path(raw).parent for raw in paths if raw}
        for parent in sorted(parents, key=lambda item: len(item.parts), reverse=True):
            try:
                parent.rmdir()
            except OSError:
                pass

    await asyncio.to_thread(_remove)


async def import_package(
    package_id: str,
    preview_id: str = "",
    prefer_cached_preview: bool = True,
    reinstall: bool = False,
) -> dict:
    lock = _package_update_locks.setdefault(package_id, asyncio.Lock())
    async with lock:
        return await _import_package_locked(
            package_id,
            preview_id=preview_id,
            prefer_cached_preview=prefer_cached_preview,
            reinstall=reinstall,
        )


async def _import_package_locked(
    package_id: str,
    preview_id: str = "",
    prefer_cached_preview: bool = True,
    reinstall: bool = False,
) -> dict:
    package = await get_package(package_id, include_internal=True)
    if not package.get("importable"):
        raise MarketError(package.get("unsupported_reason") or "该包当前版本不可导入", 400)

    installed = await db.get_market_install(package_id)
    preserved_auto_update = int(installed.get("auto_update") or 0) if installed else 0
    if installed:
        try:
            installed_metadata = json.loads(installed.get("metadata_json") or "{}")
        except json.JSONDecodeError:
            installed_metadata = {}
        previous_kind = str(installed_metadata.get("kind") or "").strip()
        if previous_kind and previous_kind != str(package.get("kind") or ""):
            raise MarketError("Market 包类型不能在原地切换", 409)
    if installed and not reinstall:
        sub = await db.get_subscription(installed.get("installed_subscription_id")) if installed.get("installed_subscription_id") else None
        if sub:
            raise MarketError(f"Market 包已安装: {sub.get('title')}", 409)
        raise MarketError("Market 包已安装", 409)

    if package.get("kind") == LOGO_PACKAGE_KIND:
        old_assets = await db.get_package_assets(package_id)
        staged_assets: list[dict] = []
        final_dir: Path | None = None
        try:
            staged_assets, final_dir = await _stage_logo_assets(package)
            metadata = {
                "name": package.get("name"),
                "kind": LOGO_PACKAGE_KIND,
                "package_type": CONTENT_PACKAGE_TYPE,
                "version": package.get("version", ""),
                "content_capabilities": package.get("content_capabilities", []),
                "logos": package.get("logos", []),
                "market_source": _safe_persisted_market_source(package.get("market_source", {})),
                "market_package": _market_package_snapshot(package),
                "imported_at": _now_iso(),
            }
            bindings = await _logo_binding_specs(package)
            await db.install_logo_package_atomic(
                package_id=package_id,
                market_url=_safe_persisted_market_url(
                    package.get("market_url") or _market_cache.get("market_url", "")
                ),
                installed_version=package.get("version", ""),
                metadata_json=json.dumps(metadata, ensure_ascii=False),
                assets=staged_assets,
                bindings=bindings,
                auto_update=preserved_auto_update,
            )
        except BaseException:
            if final_dir and final_dir.exists():
                await _remove_logo_stage_dir(final_dir)
            raise
        await _remove_old_logo_files(old_assets, staged_assets)
        return {
            "ok": True,
            "package_id": package_id,
            "channel_count": 0,
            "source_count": 0,
            "logo_count": len(staged_assets),
            "bindings": len(bindings),
        }

    preview = None
    _drop_expired_previews()
    if preview_id and prefer_cached_preview:
        cached = _preview_cache.get(preview_id)
        if cached and cached.get("package", {}).get("id") == package_id:
            preview = cached
    if preview is None:
        built = await build_preview(package_id)
        preview = _preview_cache[built["preview_id"]]

    channels = preview.get("all_channels") or []
    if not channels:
        raise MarketError("没有可导入的频道源", 400)

    asset_ids = {item["asset_id"] for item in package.get("assets") or []}
    for entry in channels:
        logo_asset_id = str(entry.get("logo_asset_id") or "")
        if logo_asset_id and logo_asset_id not in asset_ids:
            raise MarketError(f"频道引用了不存在的 logo_asset_id: {logo_asset_id}", 400)

    old_assets = await db.get_package_assets(package_id)
    staged_assets: list[dict] = []
    final_dir: Path | None = None
    try:
        staged_assets, final_dir = await _stage_logo_assets(package)
    except BaseException:
        raise
    logo_bindings = await _logo_bindings_for_package(package, staged_assets, channels)

    # subscription 级属性只允许来自 manifest 明确声明，不得从子 source 聚合。
    # 一个 source 因 Referer/headers 需要代理，不能影响同包其他 source。
    # defaults.source.requires_proxy 是 source 级默认值，在 _normalize_source() 中
    # 逐 source 合并，不应提升到订阅级。
    def _truthy(v):
        if v is None:
            return False
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)):
            return v != 0
        return str(v).strip().lower() not in ("", "0", "false", "no", "off", "none", "null")

    force_proxy = 1 if _truthy(package.get("requires_proxy")) else 0
    subscription_custom_ua = str(package.get("custom_ua") or "").strip()
    url = f"market://{package_id}"

    metadata = {
        "name": package.get("name"),
        "kind": package.get("kind"),
        "version": package.get("version", ""),
        "updated_at": package.get("updated_at", ""),
        "manifest_url": _safe_persisted_market_url(package.get("manifest_url", "")),
        "channel_sources": package.get("channel_sources", []),
        "defaults": package.get("defaults", {}),
        "warnings": preview.get("warnings", []),
        "requires_plugins": package.get("requires_plugins", []),
        "market_source": _safe_persisted_market_source(package.get("market_source", {})),
        "market_package": _market_package_snapshot(package),
        "imported_at": _now_iso(),
    }
    try:
        sub_id = await db.install_market_package_atomic(
            package_id=package_id,
            market_url=_safe_persisted_market_url(
                package.get("market_url") or _market_cache.get("market_url", "")
            ),
            title=package.get("name") or package_id,
            subscription_url=url,
            channels=channels,
            installed_version=package.get("version", ""),
            metadata_json=json.dumps(metadata, ensure_ascii=False),
            custom_ua=subscription_custom_ua,
            force_proxy=force_proxy,
            auto_update=preserved_auto_update,
            logo_assets=staged_assets,
            logo_bindings=logo_bindings,
        )
    except db.DuplicateSubscriptionError as exc:
        if final_dir and final_dir.exists():
            await _remove_logo_stage_dir(final_dir)
        raise MarketError("Market 包已安装", 409) from exc
    except BaseException:
        if final_dir and final_dir.exists():
            await _remove_logo_stage_dir(final_dir)
        raise
    await _run_epg_binding_maintenance('market_install')
    visual_status = "success"
    visual_error = ""
    if staged_assets or old_assets:
        try:
            latest_bindings = await _logo_bindings_for_package(package, staged_assets, channels)
            await db.replace_package_logo_bindings_atomic(
                package_id,
                str(package.get("version") or ""),
                latest_bindings,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            visual_status = "degraded"
            visual_error = _sanitize_refresh_error(exc)
            logger.warning(
                "market_logo_binding_maintenance_degraded",
                extra={"package_id": package_id, "error": visual_error},
            )
        await _remove_old_logo_files(
            old_assets,
            staged_assets if staged_assets else [],
        )
    return {
        "ok": True,
        "subscription_id": sub_id,
        "channel_count": preview.get("channel_count", 0),
        "source_count": len(channels),
        "warnings": preview.get("warnings", []),
        "visual_status": visual_status,
        "visual_error": visual_error,
    }
