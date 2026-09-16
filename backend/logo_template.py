"""频道 logo 模板：把"主名 → 完整 https logo URL"集中维护。

设计与现有去重 / 合并管线的关系：
- 不参与 canonical_key 计算。聚合管线先按 canonical_key 合并完之后，本模块
  只是给已经合并好的频道"贴一张更好看的 logo"，不影响去重 / 分组 / EPG /
  播放路径。任何加载 / 校验异常都隔离在本模块内，不会让网站挂。
- 配置里键写主名（如 "CCTV-1"），加载时复用 m3u8_parser.normalize_channel_name()
  算成 canonical_key。alias 已把所有变体映射到主名，所以 1 行配置覆盖几十种
  写法（CCTV-01咪咕 / CCTV1HD / CCTV-1综合ᴴᴰ …），零误判。
- 加载分两层：本地 JSON（随项目走、离线可用）→ 启动后异步拉远程一次覆盖到
  内存（拉失败维持本地，不重试，不写盘）。

安全策略：每条 URL 都按"零信任"思路做完整校验。详见 _validate_url() 和
_REMOTE_* 常量。

TODO(future settings page): 等设置页落地后，把以下项暴露为可配置：
  - 远程 URL（当前硬编码 LOGO_TEMPLATE_REMOTE_URL）
  - 是否启用远程拉取 / 拉取超时 / 是否定时刷新
  - 手动刷新接口
  - host 白名单（是否允许用户加自定义 host）
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any
from urllib.parse import urlparse

import httpx

from m3u8_parser import normalize_channel_name


logger = logging.getLogger(__name__)


# ── 安全 / 限制常量 ─────────────────────────────────────────────────
# 远程 logos.json 地址。后续接入设置页后改成读配置项。
LOGO_TEMPLATE_REMOTE_URL = "https://logo.waveflow.tv/logos.json"
LOGO_TEMPLATE_REMOTE_TIMEOUT = 8.0

# 远程响应体上限：1 MiB 已经能装上千条频道，溢出大概率是恶意/异常。
_REMOTE_MAX_BYTES = 1 * 1024 * 1024

# 整张表条目数上限。全球能想象到的频道也就几千，留 5000 余量。
_MAX_TABLE_ENTRIES = 5000

# 单条目主名 / URL 长度上限。
_MAX_KEY_LEN = 256
_MAX_URL_LEN = 2048

# 仅允许的 logo 文件扩展名（小写比较）。SVG 含脚本风险，但 <img src> 不会执行
# script，且大量公开台标本来就是 SVG，权衡后保留。
_ALLOWED_LOGO_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".ico", ".svg")

# 仅允许的 logo host。第一版从严：只放官方维护点 + 一两个公认 logo 仓。
# 想新增第三方 host 时由维护者显式扩展，避免 SSRF / 钓鱼图风险。
_ALLOWED_LOGO_HOSTS = frozenset({
    "logo.waveflow.tv",
    "live.fanmingming.com",
})

# 控制字符 / 空白 / 引号一律拒。任何"看起来不像 URL"的字符都剔除。
_FORBIDDEN_URL_CHARS = set("\x00\r\n\t\x0b\x0c '\"<>`")


def _validate_url(raw: str) -> str:
    """对模板里的单条 URL 做"零信任"校验。

    通过返回归一化后的 URL；不通过返回空串（调用方丢弃这一条，不抛异常，
    避免一颗坏数据污染整张表）。
    """
    if not isinstance(raw, str):
        return ""
    value = raw.strip()
    if not value:
        return ""
    if len(value) > _MAX_URL_LEN:
        return ""
    if any(ch in _FORBIDDEN_URL_CHARS for ch in value):
        return ""
    if any(ord(ch) < 0x20 for ch in value):
        return ""

    try:
        parsed = urlparse(value)
    except ValueError:
        return ""

    # 必须 https，杜绝 http/file/javascript/data/…
    if parsed.scheme.lower() != "https":
        return ""
    host = (parsed.hostname or "").lower()
    if not host or host not in _ALLOWED_LOGO_HOSTS:
        return ""
    # 路径最后一段（basename）必须是"真文件名 + 白名单图片后缀"。
    # 拒掉两类：① 非图片扩展名；② /.png / /foo/.png 这种后缀前没有文件名的路径。
    # 用 path 而非整 URL，避免 query 干扰判断。
    path = (parsed.path or "").lower()
    basename = path.rsplit("/", 1)[-1]
    ext_len = next(
        (len(ext) for ext in _ALLOWED_LOGO_EXTS if basename.endswith(ext)), 0
    )
    if ext_len == 0 or len(basename) <= ext_len:
        # 没有白名单后缀，或后缀前没有文件名（如 ".png"）
        return ""
    # 不允许带凭据（user:pass@host）
    if parsed.username or parsed.password:
        return ""
    return value


def _validate_key(primary: Any) -> str:
    if not isinstance(primary, str):
        return ""
    name = primary.strip()
    if not name or len(name) > _MAX_KEY_LEN:
        return ""
    key = normalize_channel_name(name)
    if not key or len(key) > _MAX_KEY_LEN:
        return ""
    return key


class LogoTemplate:
    """主名 → logo URL 映射；canonical_key 命中即返回。

    线程安全性：read 走 dict.get，GIL 下原子；远程刷新整表替换 self._table，
    读端不会读到半份数据。任何加载阶段的异常都被吞下并日志记录，绝不向调用
    端外抛。
    """

    def __init__(self, local_path: str | None = None) -> None:
        self._table: dict[str, str] = {}
        self._loaded_source: str = "empty"  # "local" / "remote" / "empty"
        if local_path and os.path.exists(local_path):
            try:
                with open(local_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._table = self._build_table(data)
                self._loaded_source = "local"
                logger.info("logo template 本地加载完成: %d 条", len(self._table))
            except Exception as exc:
                logger.warning("logo template 本地加载失败 (维持空表): %s", exc)

    # ── 公共查询 API ────────────────────────────────────────────────
    def lookup(self, canonical_key: str) -> str:
        if not canonical_key:
            return ""
        return self._table.get(canonical_key, "")

    @property
    def size(self) -> int:
        return len(self._table)

    @property
    def loaded_source(self) -> str:
        return self._loaded_source

    # ── 远程刷新 ───────────────────────────────────────────────────
    async def refresh_from_remote(
        self,
        client: httpx.AsyncClient,
        url: str = LOGO_TEMPLATE_REMOTE_URL,
        timeout: float = LOGO_TEMPLATE_REMOTE_TIMEOUT,
    ) -> bool:
        """启动后调一次。失败维持当前内存表，按用户要求不重试。"""
        # 远程 URL 自身也走 _validate_url 同款检查（scheme + host）：避免被
        # 偷换成 http:// 或非白名单 host。注意远程 URL 路径以 .json 结尾，不
        # 走图片扩展名检查，所以这里手写一个迷你版校验。
        try:
            parsed = urlparse(url)
        except ValueError:
            logger.warning("logo template 远程 URL 非法，跳过: %r", url)
            return False
        if parsed.scheme.lower() != "https" or (parsed.hostname or "").lower() not in _ALLOWED_LOGO_HOSTS:
            logger.warning("logo template 远程 URL 不在白名单，跳过: %r", url)
            return False

        # 用 stream 模式：先拿到 headers 判 Content-Type / Content-Length，再决定
        # 要不要读 body。避免 resp.content 把整个响应一次性吞进内存。
        try:
            async with client.stream(
                "GET", url, timeout=timeout, follow_redirects=False
            ) as resp:
                if resp.status_code >= 400:
                    logger.info(
                        "logo template 远程拉取失败 (维持本地 %d 条): HTTP %s",
                        len(self._table), resp.status_code,
                    )
                    return False

                # Content-Type 必须像 JSON
                ctype = (resp.headers.get("content-type") or "").lower()
                if "json" not in ctype:
                    logger.warning(
                        "logo template 远程 Content-Type 非 JSON: %r，跳过", ctype
                    )
                    return False

                # 先看 Content-Length，超标直接不读 body（省一次大下载）
                clen = resp.headers.get("content-length")
                if clen:
                    try:
                        if int(clen) > _REMOTE_MAX_BYTES:
                            logger.warning(
                                "logo template 远程响应过大 "
                                "(Content-Length=%s > %d)，跳过",
                                clen, _REMOTE_MAX_BYTES,
                            )
                            return False
                    except ValueError:
                        pass

                # 流式累加上限字节再读。防"谎报小 Content-Length 实际塞大响应"
                # 和自动解压炸弹（aiter_bytes 默认吐的是解压后字节，故对解压后
                # 体积做硬上限，比看压缩前 Content-Length 更可靠）。
                # 注意：超限时用 break 而非 return，让 async for 正确 aclose()
                # 掉 aiter_bytes 这个 async generator（否则 Python 3.14 报
                # RuntimeWarning：aclose 未 await）。
                chunks: list[bytes] = []
                total = 0
                overflow = False
                try:
                    async for chunk in resp.aiter_bytes():
                        total += len(chunk)
                        if total > _REMOTE_MAX_BYTES:
                            overflow = True
                            break
                        chunks.append(chunk)
                except Exception as exc:
                    logger.info(
                        "logo template 远程响应读取失败 (维持本地 %d 条): %s",
                        len(self._table), exc,
                    )
                    return False

                if overflow:
                    logger.warning(
                        "logo template 远程响应实际大小超限 "
                        "(已读 %d > %d)，跳过",
                        total, _REMOTE_MAX_BYTES,
                    )
                    return False

                body = b"".join(chunks)
        except Exception as exc:
            logger.info(
                "logo template 远程拉取失败 (维持本地 %d 条): %s",
                len(self._table), exc,
            )
            return False

        try:
            data = json.loads(body.decode("utf-8"))
        except Exception as exc:
            logger.warning("logo template 远程 JSON 解析失败: %s", exc)
            return False

        try:
            new_table = self._build_table(data)
        except Exception as exc:
            logger.warning("logo template 远程数据解析失败: %s", exc)
            return False

        if not new_table:
            logger.info("logo template 远程返回空表，跳过覆盖")
            return False

        self._table = new_table
        self._loaded_source = "remote"
        logger.info("logo template 远程加载完成: %d 条", len(self._table))
        return True

    # ── 内部 ───────────────────────────────────────────────────────
    @staticmethod
    def _build_table(data: dict[str, Any]) -> dict[str, str]:
        if not isinstance(data, dict):
            raise ValueError("logos.json 顶层必须是对象")
        channels = data.get("channels") or {}
        if not isinstance(channels, dict):
            raise ValueError("logos.json.channels 必须是对象")

        table: dict[str, str] = {}
        rejected = 0
        for primary, raw_url in channels.items():
            if len(table) >= _MAX_TABLE_ENTRIES:
                logger.warning(
                    "logo template 条目数超过上限 %d，截断剩余条目",
                    _MAX_TABLE_ENTRIES,
                )
                break
            key = _validate_key(primary)
            if not key:
                rejected += 1
                continue
            url = _validate_url(raw_url)
            if not url:
                rejected += 1
                continue
            table[key] = url
        if rejected:
            logger.info("logo template 跳过非法条目 %d 条", rejected)
        return table


# ── 全局实例 ────────────────────────────────────────────────────────
_local_path = os.path.join(os.path.dirname(__file__), "config", "logos.json")
logo_template = LogoTemplate(_local_path)


async def refresh_logo_template_from_remote(client: httpx.AsyncClient) -> None:
    """供 lifespan 调用的 wrapper；与 asyncio.create_task 兼容。"""
    await logo_template.refresh_from_remote(client)
