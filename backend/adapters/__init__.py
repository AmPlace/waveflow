import asyncio
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx


ADAPTER_SUCCESS_TTL_SECONDS = 30 * 60
ADAPTER_FAILURE_TTL_SECONDS = 60


@dataclass(frozen=True)
class AdapterRequest:
    raw_url: str
    adapter: str
    resource_id: str
    query: dict[str, list[str]]


class AdapterResolveError(Exception):
    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        status_code: int = 400,
        retryable: bool = False,
    ):
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.status_code = status_code
        self.retryable = retryable

    def to_payload(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error_code": self.error_code,
            "message": self.message,
            "retryable": self.retryable,
        }


_adapter_cache: dict[str, dict[str, Any]] = {}
_adapter_locks: dict[str, asyncio.Lock] = {}


def _cache_get(cache_key: str) -> dict[str, Any] | None:
    item = _adapter_cache.get(cache_key)
    if not item:
        return None
    if float(item.get("expires_at", 0)) <= time.time():
        _adapter_cache.pop(cache_key, None)
        return None
    if item.get("error"):
        err = item["error"]
        raise AdapterResolveError(
            err["error_code"],
            err["message"],
            status_code=int(err.get("status_code", 400)),
            retryable=bool(err.get("retryable")),
        )
    result = item.get("result")
    return dict(result) if isinstance(result, dict) else None


def _cache_success(cache_key: str, result: dict[str, Any]) -> None:
    if result.get("cacheable") is False:
        _adapter_cache.pop(cache_key, None)
        return

    ttl_value = result.get("ttl")
    ttl = ADAPTER_SUCCESS_TTL_SECONDS if ttl_value is None else int(ttl_value)
    if ttl <= 0:
        _adapter_cache.pop(cache_key, None)
        return

    _adapter_cache[cache_key] = {
        "expires_at": time.time() + max(1, ttl),
        "result": dict(result),
    }


def _cache_failure(cache_key: str, exc: AdapterResolveError) -> None:
    _adapter_cache[cache_key] = {
        "expires_at": time.time() + ADAPTER_FAILURE_TTL_SECONDS,
        "error": {
            **exc.to_payload(),
            "status_code": exc.status_code,
        },
    }


def parse_adapter_url(target_url: str) -> AdapterRequest:
    raw_url = (target_url or "").strip()
    if not raw_url:
        raise AdapterResolveError("invalid_adapter_url", "adapter 地址不能为空")
    if len(raw_url) > 2048:
        raise AdapterResolveError("invalid_adapter_url", "adapter 地址过长")

    try:
        parsed = urlparse(raw_url)
    except ValueError as exc:
        raise AdapterResolveError("invalid_adapter_url", "adapter 地址格式错误") from exc

    scheme = parsed.scheme.lower()
    if scheme == "migu":
        adapter = "migu"
        resource_id = (parsed.netloc or parsed.path.lstrip("/")).strip()
    elif scheme == "hnntv":
        adapter = "hnntv"
        resource_id = (parsed.netloc or parsed.path.lstrip("/")).strip()
    elif scheme == "nmtv":
        adapter = "nmtv"
        resource_id = (parsed.netloc or parsed.path.lstrip("/")).strip()
    elif scheme == "gzstv":
        adapter = "gzstv"
        resource_id = (parsed.netloc or parsed.path.lstrip("/")).strip()
    elif scheme == "sxbc":
        adapter = "sxbc"
        resource_id = (parsed.netloc or parsed.path.lstrip("/")).strip()
    elif scheme == "xjtv":
        adapter = "xjtv"
        resource_id = (parsed.netloc or parsed.path.lstrip("/")).strip()
    elif scheme == "jstv":
        adapter = "jstv"
        resource_id = (parsed.netloc or parsed.path.lstrip("/")).strip()
    elif scheme == "sdtv":
        adapter = "sdtv"
        resource_id = (parsed.netloc or parsed.path.lstrip("/")).strip()
    elif scheme == "sdly":
        adapter = "sdly"
        resource_id = (parsed.netloc or parsed.path.lstrip("/")).strip()
    elif scheme == "douyin":
        adapter = "douyin"
        resource_id = (parsed.netloc or parsed.path.lstrip("/")).strip()
    elif scheme == "douyu":
        adapter = "douyu"
        resource_id = (parsed.netloc or parsed.path.lstrip("/")).strip()
    elif scheme == "huya":
        adapter = "huya"
        resource_id = (parsed.netloc or parsed.path.lstrip("/")).strip()
    elif scheme == "hbtv":
        adapter = "hbtv"
        resource_id = (parsed.netloc or parsed.path.lstrip("/")).strip()
    elif scheme == "hntv":
        adapter = "hntv"
        resource_id = (parsed.netloc or parsed.path.lstrip("/")).strip()
    elif scheme == "tvb":
        adapter = "tvb"
        resource_id = (parsed.netloc or parsed.path.lstrip("/")).strip()
    elif scheme == "nowtv":
        adapter = "nowtv"
        resource_id = (parsed.netloc or parsed.path.lstrip("/")).strip()
    elif scheme == "redbook":
        adapter = "redbook"
        resource_id = (parsed.netloc or parsed.path.lstrip("/")).strip()
    elif scheme == "tiktok":
        adapter = "tiktok"
        resource_id = (parsed.netloc or parsed.path.lstrip("/")).strip()
    elif scheme == "kuaishou":
        adapter = "kuaishou"
        resource_id = (parsed.netloc or parsed.path.lstrip("/")).strip()
    elif scheme == "bilibili":
        adapter = "bilibili"
        resource_id = (parsed.netloc or parsed.path.lstrip("/")).strip()
    elif scheme == "yy":
        adapter = "yy"
        resource_id = (parsed.netloc or parsed.path.lstrip("/")).strip()
    elif scheme == "bigo":
        adapter = "bigo"
        resource_id = (parsed.netloc or parsed.path.lstrip("/")).strip()
    elif scheme == "blued":
        adapter = "blued"
        resource_id = (parsed.netloc or parsed.path.lstrip("/")).strip()
    elif scheme == "soop":
        adapter = "soop"
        resource_id = (parsed.netloc or parsed.path.lstrip("/")).strip()
    elif scheme == "netease":
        adapter = "netease"
        resource_id = (parsed.netloc or parsed.path.lstrip("/")).strip()
    elif scheme == "pandatv":
        adapter = "pandatv"
        resource_id = (parsed.netloc or parsed.path.lstrip("/")).strip()
    elif scheme == "maoer":
        adapter = "maoer"
        resource_id = (parsed.netloc or parsed.path.lstrip("/")).strip()
    elif scheme == "look":
        adapter = "look"
        resource_id = (parsed.netloc or parsed.path.lstrip("/")).strip()
    elif scheme == "flextv":
        adapter = "flextv"
        resource_id = (parsed.netloc or parsed.path.lstrip("/")).strip()
    elif scheme == "popkontv":
        adapter = "popkontv"
        resource_id = (parsed.netloc or parsed.path.lstrip("/")).strip()
    elif scheme == "twitcasting":
        adapter = "twitcasting"
        resource_id = (parsed.netloc or parsed.path.lstrip("/")).strip()
    elif scheme == "baidu":
        adapter = "baidu"
        resource_id = (parsed.netloc or parsed.path.lstrip("/")).strip()
    elif scheme == "weibo":
        adapter = "weibo"
        resource_id = (parsed.netloc or parsed.path.lstrip("/")).strip()
    elif scheme == "kugou":
        adapter = "kugou"
        resource_id = (parsed.netloc or parsed.path.lstrip("/")).strip()
    elif scheme == "twitch":
        adapter = "twitch"
        resource_id = (parsed.netloc or parsed.path.lstrip("/")).strip()
    elif scheme == "huajiao":
        adapter = "huajiao"
        resource_id = (parsed.netloc or parsed.path.lstrip("/")).strip()
    elif scheme == "showroom":
        adapter = "showroom"
        resource_id = (parsed.netloc or parsed.path.lstrip("/")).strip()
    elif scheme == "inke":
        adapter = "inke"
        resource_id = (parsed.netloc or parsed.path.lstrip("/")).strip()
    elif scheme == "acfun":
        adapter = "acfun"
        resource_id = (parsed.netloc or parsed.path.lstrip("/")).strip()
    elif scheme == "zhihu":
        adapter = "zhihu"
        resource_id = (parsed.netloc or parsed.path.lstrip("/")).strip()
    elif scheme == "chzzk":
        adapter = "chzzk"
        resource_id = (parsed.netloc or parsed.path.lstrip("/")).strip()
    elif scheme == "live17":
        adapter = "live17"
        resource_id = (parsed.netloc or parsed.path.lstrip("/")).strip()
    elif scheme == "langlive":
        adapter = "langlive"
        resource_id = (parsed.netloc or parsed.path.lstrip("/")).strip()
    elif scheme == "changliao":
        adapter = "changliao"
        resource_id = (parsed.netloc or parsed.path.lstrip("/")).strip()
    elif scheme == "jd":
        adapter = "jd"
        resource_id = (parsed.netloc or parsed.path.lstrip("/")).strip()
    elif scheme == "faceit":
        adapter = "faceit"
        resource_id = (parsed.netloc or parsed.path.lstrip("/")).strip()
    elif scheme == "lianjie":
        adapter = "lianjie"
        resource_id = (parsed.netloc or parsed.path.lstrip("/")).strip()
    elif scheme == "sixroom":
        adapter = "sixroom"
        resource_id = (parsed.netloc or parsed.path.lstrip("/")).strip()
    elif scheme == "huamao":
        adapter = "huamao"
        resource_id = (parsed.netloc or parsed.path.lstrip("/")).strip()
    elif scheme == "shopee":
        adapter = "shopee"
        resource_id = (parsed.netloc or parsed.path.lstrip("/")).strip()
    elif scheme == "laixiu":
        adapter = "laixiu"
        resource_id = (parsed.netloc or parsed.path.lstrip("/")).strip()
    elif scheme == "picarto":
        adapter = "picarto"
        resource_id = (parsed.netloc or parsed.path.lstrip("/")).strip()
    elif scheme == "fjtv":
        adapter = "fjtv"
        resource_id = (parsed.netloc or parsed.path.lstrip("/")).strip()
    elif scheme == "ptbtv":
        adapter = "ptbtv"
        resource_id = (parsed.netloc or parsed.path.lstrip("/")).strip()
    elif scheme == "nd0593tv":
        adapter = "nd0593tv"
        resource_id = (parsed.netloc or parsed.path.lstrip("/")).strip()
    elif scheme == "qukan":
        adapter = "qukan"
        resource_id = (parsed.netloc or parsed.path.lstrip("/")).strip()
    elif scheme == "woniu":
        adapter = "woniu"
        resource_id = (parsed.netloc or parsed.path.lstrip("/")).strip()
    elif scheme == "youtube":
        adapter = "youtube"
        resource_id = f"{parsed.netloc}{parsed.path}".strip("/")
    elif scheme == "adapter":
        adapter = (parsed.netloc or "").lower().split("@")[-1].split(":")[0]
        resource_id = parsed.path.lstrip("/").strip()
    else:
        raise AdapterResolveError(
            "invalid_adapter_url",
            "暂不支持该 adapter 地址",
        )

    if not adapter or adapter not in _ADAPTER_REGISTRY:
        raise AdapterResolveError("unsupported_adapter", "暂不支持该 adapter")
    if not resource_id:
        raise AdapterResolveError("invalid_adapter_url", "adapter 缺少资源 ID")

    return AdapterRequest(
        raw_url=raw_url,
        adapter=adapter,
        resource_id=resource_id,
        query=parse_qs(parsed.query, keep_blank_values=False),
    )


async def resolve_adapter_source(target_url: str, client: httpx.AsyncClient) -> dict[str, Any]:
    request = parse_adapter_url(target_url)
    cache_key = request.raw_url

    cached = _cache_get(cache_key)
    if cached:
        return cached

    lock = _adapter_locks.setdefault(cache_key, asyncio.Lock())
    async with lock:
        cached = _cache_get(cache_key)
        if cached:
            return cached

        resolver = _ADAPTER_REGISTRY[request.adapter]
        try:
            result = await resolver(request, client)
            result.setdefault("ok", True)
            result.setdefault("adapter", request.adapter)
            result.setdefault("source_type", "hls")
            result.setdefault("direct_playable", True)
            result.setdefault("requires_proxy", False)
            result.setdefault("headers", {})
            result.setdefault("ttl", ADAPTER_SUCCESS_TTL_SECONDS)
            result.setdefault("expires_at", None)
            result.setdefault("warnings", [])
            _cache_success(cache_key, result)
            return dict(result)
        except AdapterResolveError as exc:
            _cache_failure(cache_key, exc)
            raise


from .migu import resolve_migu
from .hbtv import resolve_hbtv
from .hntv import resolve_hntv
from .tvb import resolve_tvb
from .nowtv import resolve_nowtv
from .jstv import resolve_jstv
from .sdtv import resolve_sdtv
from .sdly import resolve_sdly
import importlib as _importlib
resolve_17live = _importlib.import_module(".17live", __package__).resolve_17live
from .hnntv import resolve_hnntv
from .nmtv import resolve_nmtv
from .gzstv import resolve_gzstv
from .sxbc import resolve_sxbc
from .xjtv import resolve_xjtv
from .douyin import resolve_douyin
from .douyu import resolve_douyu
from .huya import resolve_huya
from .redbook import resolve_redbook
from .tiktok import resolve_tiktok
from .kuaishou import resolve_kuaishou
from .bilibili import resolve_bilibili
from .yy import resolve_yy
from .bigo import resolve_bigo
from .blued import resolve_blued
from .soop import resolve_soop
from .netease import resolve_netease
from .pandatv import resolve_pandatv
from .maoer import resolve_maoer
from .look import resolve_look
from .flextv import resolve_flextv
from .popkontv import resolve_popkontv
from .twitcasting import resolve_twitcasting
from .baidu import resolve_baidu
from .weibo import resolve_weibo
from .kugou import resolve_kugou
from .twitch import resolve_twitch
from .huajiao import resolve_huajiao
from .showroom import resolve_showroom
from .inke import resolve_inke
from .acfun import resolve_acfun
from .zhihu import resolve_zhihu
from .chzzk import resolve_chzzk
from .langlive import resolve_langlive
from .changliao import resolve_changliao
from .jd import resolve_jd
from .faceit import resolve_faceit
from .lianjie import resolve_lianjie
from .sixroom import resolve_sixroom
from .huamao import resolve_huamao
from .shopee import resolve_shopee
from .laixiu import resolve_laixiu
from .picarto import resolve_picarto
from .youtube import resolve_youtube
from .fjtv import resolve_fjtv
from .ptbtv import resolve_ptbtv
from .nd0593tv import resolve_nd0593tv
from .qukan import resolve_qukan
from .woniu import resolve_woniu


_ADAPTER_REGISTRY = {
    "migu": resolve_migu,
    "hbtv": resolve_hbtv,
    "hntv": resolve_hntv,
    "tvb": resolve_tvb,
    "nowtv": resolve_nowtv,
    "nmtv": resolve_nmtv,
    "jstv": resolve_jstv,
    "sdtv": resolve_sdtv,
    "sdly": resolve_sdly,
    "hnntv": resolve_hnntv,
    "gzstv": resolve_gzstv,
    "sxbc": resolve_sxbc,
    "xjtv": resolve_xjtv,
    "douyin": resolve_douyin,
    "douyu": resolve_douyu,
    "huya": resolve_huya,
    "redbook": resolve_redbook,
    "tiktok": resolve_tiktok,
    "kuaishou": resolve_kuaishou,
    "bilibili": resolve_bilibili,
    "yy": resolve_yy,
    "bigo": resolve_bigo,
    "blued": resolve_blued,
    "soop": resolve_soop,
    "netease": resolve_netease,
    "pandatv": resolve_pandatv,
    "maoer": resolve_maoer,
    "look": resolve_look,
    "flextv": resolve_flextv,
    "popkontv": resolve_popkontv,
    "twitcasting": resolve_twitcasting,
    "baidu": resolve_baidu,
    "weibo": resolve_weibo,
    "kugou": resolve_kugou,
    "twitch": resolve_twitch,
    "huajiao": resolve_huajiao,
    "showroom": resolve_showroom,
    "inke": resolve_inke,
    "acfun": resolve_acfun,
    "zhihu": resolve_zhihu,
    "chzzk": resolve_chzzk,
    "live17": resolve_17live,
    "langlive": resolve_langlive,
    "changliao": resolve_changliao,
    "jd": resolve_jd,
    "faceit": resolve_faceit,
    "lianjie": resolve_lianjie,
    "sixroom": resolve_sixroom,
    "huamao": resolve_huamao,
    "shopee": resolve_shopee,
    "laixiu": resolve_laixiu,
    "picarto": resolve_picarto,
    "youtube": resolve_youtube,
    "fjtv": resolve_fjtv,
    "ptbtv": resolve_ptbtv,
    "nd0593tv": resolve_nd0593tv,
    "qukan": resolve_qukan,
    "woniu": resolve_woniu,
}


# ── adapter 能力声明（capability registry）────────────────────────────────
# 各 adapter 模块顶层可定义 `ADAPTER_CAPABILITIES = {"cover": True, ...}` 来声明
# 自己支持的可选能力。中央实现（fetch 函数 / 缓存 / 路由）仍可继续放在 main.py，
# 这里只负责采集 + 查询，避免在多处维护硬编码白名单。

import importlib as _importlib_caps


def _collect_adapter_capabilities() -> dict[str, dict[str, bool]]:
    out: dict[str, dict[str, bool]] = {}
    for adapter_name in _ADAPTER_REGISTRY:
        # 模块名约定：与 adapter 名一一对应，特殊的 "live17" 对应 17live.py。
        module_name = "17live" if adapter_name == "live17" else adapter_name
        try:
            module = _importlib_caps.import_module(f".{module_name}", __package__)
        except Exception:
            continue
        caps = getattr(module, "ADAPTER_CAPABILITIES", None)
        if not isinstance(caps, dict):
            continue
        clean = {str(k): bool(v) for k, v in caps.items() if v}
        if clean:
            out[adapter_name] = clean
    return out


_ADAPTER_CAPABILITIES: dict[str, dict[str, bool]] = _collect_adapter_capabilities()


def adapter_supports(adapter_name: str, capability: str) -> bool:
    """判断某个 adapter 是否声明支持指定 capability（如 "cover"）。"""
    return bool(_ADAPTER_CAPABILITIES.get(adapter_name, {}).get(capability))


def adapter_capabilities_map() -> dict[str, list[str]]:
    """返回 {adapter_name: [capability, ...]}，仅包含至少声明了一项能力的 adapter。"""
    return {
        name: sorted(caps.keys())
        for name, caps in _ADAPTER_CAPABILITIES.items()
    }
