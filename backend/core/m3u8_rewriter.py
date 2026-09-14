"""统一的 HLS / M3U8 重写器。

输入：上游响应文本 + 上游 ``base_url`` + ``RewriteContext``。
输出：把所有相对/绝对 URI 重写为 ``/api/media/proxy/{kind}/{handle}`` 形式的安全文本。

关键点：

* 处理 EXT-X-STREAM-INF / EXT-X-MEDIA / EXT-X-KEY / EXT-X-MAP / EXT-X-PART 的 URI 属性。
* 普通 URL 行根据扩展名归类为 playlist 或 chunk。
* 相对 URL 一律用 ``urljoin(base_url, line)`` 解析，禁止字符串拼接。
* 重写后 URL 可附加 ``?access_token=...``，仅当来源是 Media Credential 时使用。
* 输出中**绝对不会**包含 raw upstream query / 管理员凭证 / cookie / 上游 URL 明文。

调用方需先做完 SSRF 校验和上游 fetch；本模块只做文本变换 + handle 签发。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import quote, urljoin, urlparse

from security.proxy_handles import issue_cached_handle


_PLAYLIST_URI_TAGS = frozenset({
    "EXT-X-MEDIA",
    "EXT-X-STREAM-INF",
    "EXT-X-RENDITION-REPORT",
    "EXT-X-I-FRAME-STREAM-INF",
})
_CHUNK_URI_TAGS = frozenset({
    "EXT-X-KEY",
    "EXT-X-SESSION-KEY",
    "EXT-X-MAP",
    "EXT-X-PART",
    "EXT-X-PRELOAD-HINT",
})
_HLS_URI_TAGS = _PLAYLIST_URI_TAGS | _CHUNK_URI_TAGS

# 形如 #EXT-X-KEY:METHOD=...,URI="https://..."
_HLS_URI_RE = re.compile(r'(URI=")([^"]*)(")')

_PLAYLIST_EXTENSIONS = (".m3u8", ".m3u")
_SEGMENT_EXTENSIONS = (
    ".ts", ".m4s", ".mp4", ".fmp4", ".m4v", ".aac", ".mp3", ".webm", ".cmfa", ".cmfv", ".vtt",
)

_MEDIA_SEQ_RE = re.compile(r"^\s*#EXT-X-MEDIA-SEQUENCE\s*:\s*(\d+)", re.IGNORECASE)


@dataclass(frozen=True)
class RewriteContext:
    """重写一段 m3u8 文本所需的全部信息。"""

    base_url: str                       # 上游 playlist 的最终 URL（处理 redirect 后）
    src_id: str = ""                    # 稳定频道引用（canonical_key 或 station_id）；写入 handle.src_id
    src_label: str = ""                 # 调试用，写入 handle.src
    ctx_id: str = ""                    # ProxyContext 的 ID（含 ua/referer/cookie）；可空
    propagated_access_token: str = ""   # 仅 Media Credential 时透传到子 URL
    proxy_segments: bool = True         # IPTV 直连模式可关，电台必开
    playlist_ttl: int | None = None
    chunk_ttl: int | None = None
    image_ttl: int | None = None
    rtsp_compat: int = 0                # rtsp kind 专用
    proxy_path_prefix: str = "/api/media/proxy"


def _qs_token(token: str) -> str:
    if not token:
        return ""
    return f"?access_token={quote(token, safe='')}"


def _make_handle_url(
    *,
    kind: str,
    upstream_url: str,
    ctx: RewriteContext,
    internal_seq: int | None = None,
) -> str:
    ttl = None
    if kind == "playlist":
        ttl = ctx.playlist_ttl
    elif kind == "chunk":
        ttl = ctx.chunk_ttl
    elif kind == "image":
        ttl = ctx.image_ttl
    handle = issue_cached_handle(
        kind=kind,
        url=upstream_url,
        ttl_seconds=ttl,
        src=ctx.src_label,
        ctx=ctx.ctx_id,
        src_id=ctx.src_id,
        compat=ctx.rtsp_compat if kind == "rtsp" else 0,
    )
    # Keep signed payloads opaque while giving strict external HLS clients a
    # recognizable media suffix for chunk URLs. The proxy route strips this
    # presentation suffix before verifying the handle.
    handle_suffix = ".ts" if kind == "chunk" else ""
    proxy_url = f"{ctx.proxy_path_prefix}/{kind}/{handle}{handle_suffix}{_qs_token(ctx.propagated_access_token)}"
    if internal_seq is not None and internal_seq >= 0:
        separator = "&" if "?" in proxy_url else "?"
        proxy_url = f"{proxy_url}{separator}wf_seq={internal_seq}"
    return proxy_url


def _classify_uri(uri: str) -> str | None:
    parsed = urlparse(uri)
    path = parsed.path.lower()
    if path.endswith(_PLAYLIST_EXTENSIONS):
        return "playlist"
    if path.endswith(_SEGMENT_EXTENSIONS):
        return "chunk"
    return None


def _rewrite_tag_line(line: str, ctx: RewriteContext) -> str:
    tag_name = line.strip().split(":", 1)[0].lstrip("#").upper()
    if tag_name not in _HLS_URI_TAGS:
        return line

    def _replace(match: re.Match) -> str:
        head, uri, tail = match.group(1), match.group(2), match.group(3)
        scheme = urlparse(uri).scheme.lower()
        # 跳过 data: / skd: (FairPlay Streaming key URI) / urn: 等特殊 scheme
        if scheme and scheme not in ("http", "https"):
            return match.group(0)
        absolute = urljoin(ctx.base_url, uri)
        kind = "playlist" if tag_name in _PLAYLIST_URI_TAGS else "chunk"
        if kind == "chunk" and not ctx.proxy_segments:
            return f"{head}{absolute}{tail}"
        return f"{head}{_make_handle_url(kind=kind, upstream_url=absolute, ctx=ctx)}{tail}"

    return _HLS_URI_RE.sub(_replace, line)


def rewrite_m3u8(text: str, ctx: RewriteContext) -> str:
    """重写 HLS 主/子 playlist 文本。

    返回值末尾会保证有换行（hls.js 对最后一行的 newline 比较宽容，但保险）。
    """
    out: list[str] = []
    # 单遍扫描，维护「下一段的 MEDIA-SEQUENCE 编号」。
    # 用法：遇到 ``#EXT-X-MEDIA-SEQUENCE:N`` 把 ``current_seq`` 设成 N；
    # 每写一个 chunk URL 就 ``+1``。子 playlist / variant playlist 不动。
    # 没有 MEDIA-SEQUENCE 头的 VOD playlist 默认从 0 开始（RFC 8216 §4.3.3.2）。
    current_seq = 0
    seen_media_seq = False
    expects_variant_uri = False
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            out.append(raw_line)
            continue
        directive = stripped.lstrip("\ufeff")
        if directive.startswith("#"):
            mseq_match = _MEDIA_SEQ_RE.match(raw_line)
            if mseq_match:
                try:
                    current_seq = int(mseq_match.group(1))
                    seen_media_seq = True
                except ValueError:
                    pass
            out.append(_rewrite_tag_line(raw_line, ctx))
            if directive.upper().startswith("#EXT-X-STREAM-INF:"):
                expects_variant_uri = True
            continue

        # URL 行（segment 或 variant playlist）
        scheme = urlparse(stripped).scheme.lower()
        if scheme and scheme not in ("http", "https"):
            out.append(raw_line)
            expects_variant_uri = False
            continue
        try:
            absolute = urljoin(ctx.base_url, stripped)
        except ValueError:
            out.append(raw_line)
            continue
        kind = "playlist" if expects_variant_uri else _classify_uri(absolute)
        expects_variant_uri = False
        if kind == "playlist":
            out.append(_make_handle_url(kind="playlist", upstream_url=absolute, ctx=ctx))
        elif kind == "chunk":
            if ctx.proxy_segments:
                out.append(_make_handle_url(
                    kind="chunk",
                    upstream_url=absolute,
                    ctx=ctx,
                    internal_seq=current_seq,
                ))
                current_seq += 1
            else:
                out.append(absolute)
        else:
            # Media playlist URI may be extensionless or query-only. Keeping a raw absolute
            # URL here would bypass signed handles, ProxyContext, redirects and SSRF checks.
            if ctx.proxy_segments:
                out.append(_make_handle_url(
                    kind="chunk",
                    upstream_url=absolute,
                    ctx=ctx,
                    internal_seq=current_seq,
                ))
                current_seq += 1
            else:
                out.append(absolute)
    # seen_media_seq 仅作未来扩展位（比如做 sequence 一致性校验时用），目前无副作用。
    _ = seen_media_seq
    rewritten = "\n".join(out)
    return rewritten if rewritten.endswith("\n") else rewritten + "\n"
