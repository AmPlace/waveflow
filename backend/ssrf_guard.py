"""SSRF 防护：校验服务端代拉的目标 URL 是否安全。

设计要点：
- 「硬黑名单」段（云元数据/链路本地/unspecified）永远挡，无论 ALLOW_PRIVATE 如何设置。
  这些地址没有任何合法流媒体用途，是公共代理探测元数据偷凭证的核心目标。
- RFC1918 私网段由 WAVEFLOW_ALLOW_PRIVATE 控制（默认放行），匹配 IPTV 内网源场景
  （RTSP 摄像头、自建 IPTV 网关、组播）。
- DNS 解析后再判断 IP（防 DNS rebinding：攻击者用首次解析返回公网、二次解析返回内网的域名绕过）。
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from urllib.parse import urlparse

from core.settings_service import get_effective_settings, get_effective_settings_sync


# 永远挡的网络段（即使 ALLOW_PRIVATE=true 也不放行）
_HARD_BLOCKED_NETWORKS = (
    ipaddress.ip_network("169.254.0.0/16"),   # 链路本地，含 AWS/GCP/Azure 元数据 169.254.169.254
    ipaddress.ip_network("fe80::/10"),         # IPv6 链路本地
    ipaddress.ip_network("0.0.0.0/8"),         # IPv4 unspecified
    ipaddress.ip_network("::/128"),            # IPv6 unspecified
)

# Clash/Mihomo Fake-IP uses the RFC 2544 benchmark range.  It is not an
# allowlist: an answer in this range only selects the independent-resolution
# path below.  Keep this list intentionally separate from the general private
# address policy so future synthetic ranges can be added explicitly.
_SYNTHETIC_DNS_NETWORKS = (
    ipaddress.ip_network("198.18.0.0/15"),
)
_REAL_DNS_DEFAULT_ENDPOINTS = (
    "https://cloudflare-dns.com/dns-query",
    "https://dns.google/resolve",
)
_REAL_DNS_TIMEOUT_SECONDS = 2.0
_REAL_DNS_MAX_ADDRESSES = 16
_REAL_DNS_MAX_CNAME_DEPTH = 4
_REAL_DNS_MAX_ANSWER_RECORDS = 64
_REAL_DNS_MAX_RESPONSE_BYTES = 64 * 1024
_REAL_DNS_CACHE_TTL_SECONDS = 5.0
_REAL_DNS_CACHE_MAX_ENTRIES = 256
_REAL_DNS_CACHE: dict[tuple[str, tuple[str, ...]], tuple[tuple[str, ...], float]] = {}
_REAL_DNS_INFLIGHT: dict[tuple[str, tuple[str, ...]], asyncio.Future[list[str]]] = {}


class UnsafeTargetError(ValueError):
    """目标 URL 不安全（命中黑名单或被策略拦截）。"""


def _is_hard_blocked(ip: ipaddress._BaseAddress) -> bool:
    return any(ip in net for net in _HARD_BLOCKED_NETWORKS)


def _is_loopback(ip: ipaddress._BaseAddress) -> bool:
    return ip.is_loopback


def _is_private(ip: ipaddress._BaseAddress) -> bool:
    return (
        ip.is_private
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_unspecified
        or ip.is_multicast
    )


def _is_private_hostname(host: str) -> bool:
    value = (host or "").strip().lower().strip("[]")
    return value in {"localhost", "localhost.localdomain"} or value.endswith(".localhost")


def _is_private_ip_literal(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False
    return _is_private(ip)


async def resolve_host(host: str) -> list[str]:
    """同步 DNS 解析包到 to_thread，避免阻塞事件循环。"""

    def _resolve() -> list[str]:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        return list({info[4][0] for info in infos})

    return await asyncio.to_thread(_resolve)


def _is_synthetic_dns_ip(value: str | ipaddress._BaseAddress) -> bool:
    try:
        address = value if isinstance(value, ipaddress._BaseAddress) else ipaddress.ip_address(value)
    except ValueError:
        return False
    return any(address in network for network in _SYNTHETIC_DNS_NETWORKS)


def _configured_real_dns_endpoints() -> tuple[str, ...]:
    configured = os.environ.get("WAVEFLOW_REAL_DNS_ENDPOINTS", "")
    values = tuple(item.strip() for item in configured.split(",") if item.strip())
    values = values or _REAL_DNS_DEFAULT_ENDPOINTS
    endpoints: list[str] = []
    for value in values:
        try:
            parsed = urllib.parse.urlparse(value)
            if parsed.scheme.lower() != "https" or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError
            _ = parsed.port
        except (ValueError, TypeError):
            # The resolver endpoint is controlled configuration, not caller
            # input.  Invalid endpoints are ignored; if none remain, the
            # authorization path fails closed.
            continue
        endpoints.append(parsed.geturl())
    return tuple(dict.fromkeys(endpoints))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def _doh_query(endpoint: str, host: str, qtype: str, timeout: float) -> tuple[set[str], set[str]]:
    separator = "&" if "?" in endpoint else "?"
    url = f"{endpoint}{separator}{urllib.parse.urlencode({'name': host.rstrip('.'), 'type': qtype})}"
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/dns-json", "User-Agent": "WaveFlow-Core-DNS/1"},
        method="GET",
    )
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            if response.status != 200:
                raise OSError("Independent DNS endpoint returned an HTTP error")
            body = response.read(_REAL_DNS_MAX_RESPONSE_BYTES + 1)
    except (OSError, urllib.error.URLError, urllib.error.HTTPError) as exc:
        raise OSError("Independent DNS endpoint is unavailable") from exc
    if len(body) > _REAL_DNS_MAX_RESPONSE_BYTES:
        raise OSError("Independent DNS response is too large")
    try:
        decoded = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OSError("Independent DNS response is invalid") from exc
    if not isinstance(decoded, dict) or decoded.get("Status") not in {0, None}:
        raise OSError("Independent DNS response returned an error")
    answers = decoded.get("Answer") or []
    if not isinstance(answers, list) or len(answers) > _REAL_DNS_MAX_ANSWER_RECORDS:
        raise OSError("Independent DNS answer count is invalid")
    addresses: set[str] = set()
    cnames: set[str] = set()
    expected_type = 1 if qtype == "A" else 28
    for answer in answers:
        if not isinstance(answer, dict):
            raise OSError("Independent DNS answer is invalid")
        answer_type = answer.get("type")
        data = answer.get("data")
        if answer_type == expected_type and isinstance(data, str):
            try:
                address = ipaddress.ip_address(data.strip())
            except ValueError as exc:
                raise OSError("Independent DNS address is invalid") from exc
            if (expected_type == 1 and address.version != 4) or (expected_type == 28 and address.version != 6):
                raise OSError("Independent DNS address family is invalid")
            addresses.add(str(address))
        elif answer_type == 5 and isinstance(data, str):
            target = data.strip().rstrip(".").lower()
            if target:
                cnames.add(target)
    if len(addresses) > _REAL_DNS_MAX_ADDRESSES:
        raise OSError("Independent DNS response contains too many addresses")
    return addresses, cnames


def _resolve_real_dns_sync(host: str, *, timeout: float) -> list[str]:
    endpoints = _configured_real_dns_endpoints()
    if not endpoints:
        raise OSError("No independent DNS endpoint is configured")
    deadline = time.monotonic() + max(0.01, timeout)
    visited: set[str] = set()

    def resolve(name: str, depth: int) -> set[str]:
        normalized = name.rstrip(".").lower()
        if not normalized or normalized in visited or depth > _REAL_DNS_MAX_CNAME_DEPTH:
            raise OSError("DNS CNAME chain is invalid")
        visited.add(normalized)
        addresses: set[str] = set()
        cname_targets: set[str] = set()
        for qtype in ("A", "AAAA"):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Independent DNS resolution timed out")
            query_failed = True
            last_error: BaseException | None = None
            for endpoint in endpoints:
                try:
                    found, aliases = _doh_query(endpoint, normalized, qtype, remaining)
                except OSError as exc:
                    last_error = exc
                    continue
                query_failed = False
                addresses.update(found)
                cname_targets.update(aliases)
                break
            if query_failed and last_error is not None and qtype == "AAAA" and not addresses and not cname_targets:
                raise OSError("Independent DNS resolution failed") from last_error
        if len(addresses) > _REAL_DNS_MAX_ADDRESSES:
            raise OSError("Independent DNS response contains too many addresses")
        if addresses:
            return addresses
        for target in cname_targets:
            addresses.update(resolve(target, depth + 1))
        return addresses

    resolved = resolve(host, 0)
    if not resolved:
        raise OSError("Independent DNS returned no A or AAAA records")
    if len(resolved) > _REAL_DNS_MAX_ADDRESSES:
        raise OSError("Independent DNS response contains too many addresses")
    return sorted(resolved)


async def resolve_host_independently(host: str, *, timeout: float = _REAL_DNS_TIMEOUT_SECONDS) -> list[str]:
    """Resolve with explicitly configured DNS servers, not system getaddrinfo."""
    return await asyncio.to_thread(_resolve_real_dns_sync, host, timeout=timeout)


async def _resolve_host_independently_cached(host: str) -> list[str]:
    """Short-lived, single-flight cache for expensive fake-IP recovery DNS.

    Only raw resolver answers are cached. The effective private/loopback and
    hard-block policy is intentionally evaluated by ``assert_safe_target_url``
    on every request after this function returns.
    """
    normalized_host = host.rstrip(".").lower()
    key = (normalized_host, _configured_real_dns_endpoints())
    now = time.monotonic()
    cached = _REAL_DNS_CACHE.get(key)
    if cached and cached[1] > now:
        return list(cached[0])
    if cached:
        _REAL_DNS_CACHE.pop(key, None)

    inflight = _REAL_DNS_INFLIGHT.get(key)
    if inflight is not None:
        return list(await asyncio.shield(inflight))

    inflight = asyncio.get_running_loop().create_future()
    _REAL_DNS_INFLIGHT[key] = inflight
    try:
        addresses = await resolve_host_independently(normalized_host)
        expires_at = time.monotonic() + _REAL_DNS_CACHE_TTL_SECONDS
        _REAL_DNS_CACHE[key] = (tuple(addresses), expires_at)
        if len(_REAL_DNS_CACHE) > _REAL_DNS_CACHE_MAX_ENTRIES:
            expired = [cache_key for cache_key, value in _REAL_DNS_CACHE.items() if value[1] <= time.monotonic()]
            for cache_key in expired:
                _REAL_DNS_CACHE.pop(cache_key, None)
            while len(_REAL_DNS_CACHE) > _REAL_DNS_CACHE_MAX_ENTRIES:
                oldest = min(_REAL_DNS_CACHE, key=lambda cache_key: _REAL_DNS_CACHE[cache_key][1])
                _REAL_DNS_CACHE.pop(oldest, None)
        if not inflight.done():
            inflight.set_result(list(addresses))
        return list(addresses)
    except asyncio.CancelledError:
        if not inflight.done():
            inflight.cancel()
        raise
    except BaseException as exc:
        if not inflight.done():
            inflight.set_exception(exc)
            inflight.exception()  # avoid an un-retrieved exception when there are no waiters
        raise
    finally:
        if _REAL_DNS_INFLIGHT.get(key) is inflight:
            _REAL_DNS_INFLIGHT.pop(key, None)


def reset_real_dns_cache_for_tests() -> None:
    _REAL_DNS_CACHE.clear()
    for future in tuple(_REAL_DNS_INFLIGHT.values()):
        if not future.done():
            future.cancel()
    _REAL_DNS_INFLIGHT.clear()


async def _resolve_host_for_policy(host: str) -> list[str]:
    system_ips = await resolve_host(host)
    if any(_is_synthetic_dns_ip(value) for value in system_ips):
        # Do not return the synthetic answer and do not accept a caller-supplied
        # replacement.  The independent result is the only authorization input.
        return await _resolve_host_independently_cached(host)
    return system_ips


async def assert_safe_target_url(
    url: str,
    *,
    allow_private: bool | None = None,
    allow_loopback: bool | None = None,
    allowed_schemes: set[str] | None = None,
) -> None:
    """校验目标 URL 是否安全可代理。不安全抛 UnsafeTargetError。

    allow_private=None 时使用全局 ALLOW_PRIVATE；显式传值可覆盖（market 模块按源配置走）。
    """
    settings = await get_effective_settings()
    schemes = allowed_schemes or {"http", "https"}
    parsed = urlparse((url or "").strip())
    if parsed.scheme.lower() not in schemes:
        raise UnsafeTargetError("URL scheme 不被允许")
    host = parsed.hostname
    if not host:
        raise UnsafeTargetError("缺少 hostname")
    if parsed.username or parsed.password:
        raise UnsafeTargetError("URL 不允许包含用户名或密码")

    effective_allow_private = settings.allow_private if allow_private is None else bool(allow_private)
    effective_allow_loopback = settings.allow_loopback if allow_loopback is None else bool(allow_loopback)

    # 1. 字面量预检（IP 直填或 localhost 主机名）
    if _is_private_hostname(host) or _is_private_ip_literal(host):
        if _is_private_hostname(host) and not effective_allow_loopback:
            raise UnsafeTargetError("安全策略已阻止本机地址")
        if not effective_allow_private:
            raise UnsafeTargetError("安全策略已阻止内网或本机地址")
        # 字面量是私网但允许私网：仍要确认不是硬黑名单段（元数据）
        try:
            ip = ipaddress.ip_address(host.strip("[]"))
            if _is_hard_blocked(ip):
                raise UnsafeTargetError("禁止访问元数据/链路本地地址")
        except ValueError:
            pass  # 是主机名不是 IP，交给下面的 DNS 解析

    # 2. DNS 解析后二次校验（防 rebinding）
    try:
        ips = [ipaddress.ip_address(x) for x in await _resolve_host_for_policy(host)]
    except OSError as exc:
        raise UnsafeTargetError(f"域名解析失败: {exc}") from exc
    if not ips:
        raise UnsafeTargetError("域名没有可用解析结果")

    if any(_is_synthetic_dns_ip(ip) for ip in ips):
        raise UnsafeTargetError("安全策略已阻止 synthetic DNS 地址")

    for ip in ips:
        if _is_hard_blocked(ip):
            raise UnsafeTargetError("禁止访问元数据/链路本地地址")

    if not effective_allow_loopback:
        for ip in ips:
            if _is_loopback(ip):
                raise UnsafeTargetError("安全策略已阻止本机地址")

    if not effective_allow_private:
        for ip in ips:
            if _is_private(ip):
                raise UnsafeTargetError("安全策略已阻止解析到内网或本机地址")


def resolve_target_ips_sync(host: str) -> list[ipaddress._BaseAddress]:
    """同步解析并返回 IP 对象列表，供 RTSP（rtsp scheme）等同步路径复用。"""
    infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return [ipaddress.ip_address(info[4][0]) for info in infos]


def assert_safe_host_ips(
    host: str,
    *,
    allow_private: bool | None = None,
    allow_loopback: bool | None = None,
) -> None:
    """同步版校验：给定 hostname，解析后判断。供 RTSP/同步上下文使用。"""
    if not host:
        raise UnsafeTargetError("缺少 hostname")
    settings = get_effective_settings_sync()
    effective_allow_private = settings.allow_private if allow_private is None else bool(allow_private)
    effective_allow_loopback = settings.allow_loopback if allow_loopback is None else bool(allow_loopback)

    if _is_private_hostname(host) or _is_private_ip_literal(host):
        if _is_private_hostname(host) and not effective_allow_loopback:
            raise UnsafeTargetError("安全策略已阻止本机地址")
        if not effective_allow_private:
            raise UnsafeTargetError("安全策略已阻止内网或本机地址")
        try:
            ip = ipaddress.ip_address(host.strip("[]"))
            if _is_hard_blocked(ip):
                raise UnsafeTargetError("禁止访问元数据/链路本地地址")
        except ValueError:
            pass

    try:
        ips = resolve_target_ips_sync(host)
    except OSError as exc:
        raise UnsafeTargetError(f"域名解析失败: {exc}") from exc
    if not ips:
        raise UnsafeTargetError("域名没有可用解析结果")

    for ip in ips:
        if _is_hard_blocked(ip):
            raise UnsafeTargetError("禁止访问元数据/链路本地地址")
    if not effective_allow_loopback:
        for ip in ips:
            if _is_loopback(ip):
                raise UnsafeTargetError("安全策略已阻止本机地址")
    if not effective_allow_private:
        for ip in ips:
            if _is_private(ip):
                raise UnsafeTargetError("安全策略已阻止解析到内网或本机地址")
