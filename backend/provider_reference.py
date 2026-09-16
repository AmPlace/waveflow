"""Provider-neutral parsing of WaveFlow source references.

This module knows how a reference is *written*.  It deliberately knows nothing
about how one is resolved: provider resolution is Plugin-only and lives in
``provider_resolver``.  Nothing here may import an adapter, a registry of
providers, or any provider-specific resolver.

Two reference shapes exist, and both are preserved verbatim from the previous
``adapters`` parser so existing sources keep resolving to the same
``resource_id``:

* ``<scheme>://<resource>`` — the resource is the authority, or the path when
  there is no authority.
* ``<scheme>://<authority><path>`` — the resource keeps the path, used by
  schemes whose identifiers legitimately contain a slash.

The third form, ``adapter://<scheme>/<resource>``, is the generic escape hatch
for any scheme and is parsed the same way regardless of provider.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

MAX_REFERENCE_LENGTH = 2048


@dataclass(frozen=True)
class ProviderReference:
    raw_url: str
    scheme: str
    resource_id: str
    query: dict[str, list[str]]


class ProviderReferenceError(Exception):
    """A source reference is malformed or cannot be parsed."""

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


# Schemes whose reference carries the whole path.  Everything not listed here
# takes the authority, falling back to the path when the authority is empty.
_PATH_CARRYING_SCHEMES = frozenset({"youtube"})

# Schemes that historically resolved through a Core adapter and therefore used
# the authority form.  Kept as reference-syntax compatibility data only: the
# entries no longer imply that anything in Core can resolve them.
_AUTHORITY_FORM_SCHEMES = frozenset({
    'acfun', 'baidu', 'bigo', 'bilibili', 'blued', 'changliao', 'chzzk', 'douyin',
    'douyu', 'faceit', 'fjtv', 'flextv', 'gzstv', 'hbtv', 'hnntv', 'hntv', 'huajiao',
    'huamao', 'huya', 'inke', 'jd', 'jstv', 'kuaishou', 'kugou', 'laixiu', 'langlive',
    'lianjie', 'live17', 'look', 'maoer', 'migu', 'nd0593tv', 'netease', 'nmtv',
    'nowtv', 'pandatv', 'picarto', 'popkontv', 'ptbtv', 'qukan', 'redbook', 'sdly',
    'sdtv', 'shopee', 'showroom', 'sixroom', 'soop', 'sxbc', 'tiktok', 'tvb',
    'twitcasting', 'twitch', 'weibo', 'woniu', 'xjtv', 'yy', 'zhihu',
})


def parse_provider_reference(target_url: str) -> ProviderReference:
    """Parse a source reference into its scheme, resource id and query.

    Raises ``ProviderReferenceError`` when the value is not a usable reference.
    It never decides whether the scheme is *supported*: that is ownership and
    Plugin routing, and an unknown scheme parses fine and fails closed later.
    """
    raw_url = (target_url or "").strip()
    if not raw_url:
        raise ProviderReferenceError("invalid_provider_reference", "源地址不能为空")
    if len(raw_url) > MAX_REFERENCE_LENGTH:
        raise ProviderReferenceError("invalid_provider_reference", "源地址过长")

    try:
        parsed = urlparse(raw_url)
    except ValueError as exc:
        raise ProviderReferenceError(
            "invalid_provider_reference", "源地址格式错误",
        ) from exc

    scheme = parsed.scheme.lower()
    if scheme == "adapter":
        # adapter://<scheme>/<resource> — the generic escape hatch.
        scheme = (parsed.netloc or "").lower().split("@")[-1].split(":")[0]
        resource_id = parsed.path.lstrip("/").strip()
    elif scheme in _AUTHORITY_FORM_SCHEMES:
        resource_id = (parsed.netloc or parsed.path.lstrip("/")).strip()
    else:
        # Path-carrying schemes, plus every scheme Core has no rule for.
        resource_id = f"{parsed.netloc}{parsed.path}".strip("/")

    if not scheme:
        raise ProviderReferenceError("invalid_provider_reference", "源地址缺少 scheme")
    if not resource_id:
        raise ProviderReferenceError("invalid_provider_reference", "源地址缺少资源 ID")

    return ProviderReference(
        raw_url=raw_url,
        scheme=scheme,
        resource_id=resource_id,
        query=parse_qs(parsed.query, keep_blank_values=False),
    )
