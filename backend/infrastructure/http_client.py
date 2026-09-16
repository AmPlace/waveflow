from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urljoin

import httpx

from core.settings_service import get_effective_settings_sync
from ssrf_guard import UnsafeTargetError, assert_safe_target_url


REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
MEDIA_MAX_REDIRECTS = 5
_CROSS_ORIGIN_SENSITIVE_HEADERS = frozenset({
    "authorization",
    "cookie",
    "proxy-authorization",
})


class _StatelessCookies(httpx.Cookies):
    """Keep explicit request cookies without accepting upstream Set-Cookie."""

    def extract_cookies(self, response: httpx.Response) -> None:
        return None


class StatelessAsyncClient(httpx.AsyncClient):
    """An AsyncClient whose cookie state is never populated by responses."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._stateless_cookies = _StatelessCookies(self._cookies)

    @property
    def cookies(self) -> httpx.Cookies:
        return self._stateless_cookies

    @cookies.setter
    def cookies(self, cookies) -> None:
        self._stateless_cookies = _StatelessCookies(cookies)


class RedirectTargetRejected(httpx.HTTPError):
    """A redirect target failed WaveFlow's SSRF policy before any request."""

    def __init__(self, cause: Exception):
        super().__init__(f"redirect target rejected: {cause}")
        self.cause = cause


class ResponseTooLargeError(httpx.HTTPError):
    """The decoded response body exceeded the caller's byte policy."""


def _origin(url: str) -> tuple[str, str, int | None]:
    parsed = httpx.URL(url)
    scheme = parsed.scheme.lower()
    host = (parsed.host or "").lower()
    port = parsed.port
    if port is None and scheme in {"http", "https"}:
        port = 443 if scheme == "https" else 80
    return scheme, host, port


def _headers_for_redirect(
    headers: dict[str, str],
    current_url: str,
    next_url: str,
) -> dict[str, str]:
    """Keep media headers, but do not carry credentials to another origin."""
    if _origin(current_url) == _origin(next_url):
        return headers
    return {
        key: value
        for key, value in headers.items()
        if key.lower() not in _CROSS_ORIGIN_SENSITIVE_HEADERS
    }


async def request_with_safe_redirects(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout=None,
    max_redirects: int = MEDIA_MAX_REDIRECTS,
    omit_headers: set[str] | frozenset[str] | None = None,
) -> httpx.Response:
    """Send a buffered HTTP request with SSRF validation on every redirect hop."""
    current_url = url
    current_headers = dict(headers or {})
    for redirect_count in range(max_redirects + 1):
        try:
            await assert_safe_target_url(
                current_url,
                allowed_schemes={"http", "https"},
            )
        except UnsafeTargetError as exc:
            # Keep the original policy exception available to callers without
            # making infrastructure code depend on FastAPI response semantics.
            raise RedirectTargetRejected(exc) from exc

        request_kwargs = {"headers": current_headers}
        if timeout is not None:
            request_kwargs["timeout"] = timeout
        request = client.build_request(method, current_url, **request_kwargs)
        for header_name in omit_headers or ():
            request.headers.pop(header_name, None)
        response = await client.send(request, follow_redirects=False)
        if response.status_code not in REDIRECT_STATUSES:
            return response

        location = response.headers.get("location")
        if not location:
            await response.aclose()
            raise httpx.HTTPError("redirect missing Location")
        if redirect_count >= max_redirects:
            await response.aclose()
            raise httpx.TooManyRedirects(
                "too many redirects",
                request=response.request,
            )

        next_url = str(httpx.URL(current_url).join(location))
        next_headers = _headers_for_redirect(current_headers, current_url, next_url)
        await response.aclose()
        current_url = next_url
        current_headers = next_headers
    raise httpx.HTTPError("safe redirect request failed")


async def stream_with_safe_redirects(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout=None,
    max_redirects: int = MEDIA_MAX_REDIRECTS,
    omit_headers: set[str] | frozenset[str] | None = None,
) -> httpx.Response:
    """Open a streaming response after validating every redirect target.

    Redirect responses are closed immediately. The returned non-redirect
    response remains open for the caller and therefore preserves streaming and
    backpressure semantics.
    """
    current_url = url
    current_headers = dict(headers or {})
    for redirect_count in range(max_redirects + 1):
        try:
            await assert_safe_target_url(
                current_url,
                allowed_schemes={"http", "https"},
            )
        except UnsafeTargetError as exc:
            raise RedirectTargetRejected(exc) from exc

        request_kwargs = {"headers": current_headers}
        if timeout is not None:
            request_kwargs["timeout"] = timeout
        request = client.build_request(method, current_url, **request_kwargs)
        for header_name in omit_headers or ():
            request.headers.pop(header_name, None)
        response = await client.send(request, stream=True, follow_redirects=False)
        if response.status_code not in REDIRECT_STATUSES:
            return response

        location = response.headers.get("location")
        if not location:
            await response.aclose()
            raise httpx.HTTPError("redirect missing Location")
        if redirect_count >= max_redirects:
            await response.aclose()
            raise httpx.TooManyRedirects(
                "too many redirects",
                request=response.request,
            )

        next_url = str(httpx.URL(current_url).join(location))
        next_headers = _headers_for_redirect(current_headers, current_url, next_url)
        await response.aclose()
        current_url = next_url
        current_headers = next_headers
    raise httpx.HTTPError("safe streaming request failed")


@dataclass(frozen=True)
class FetchPolicy:
    allowed_schemes: set[str] = field(default_factory=lambda: {"http", "https"})
    allow_private: bool | None = None
    allow_loopback: bool | None = None
    max_redirects: int = 5
    connect_timeout: float = 10.0
    read_timeout: float = 30.0
    max_response_bytes: int = 50 * 1024 * 1024
    verify_tls: bool = True


def default_policy(**overrides) -> FetchPolicy:
    settings = get_effective_settings_sync()
    values = {
        "allow_private": settings.allow_private,
        "allow_loopback": settings.allow_loopback,
    }
    values.update(overrides)
    return FetchPolicy(**values)


async def fetch_bytes(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    policy: FetchPolicy | None = None,
) -> tuple[str, bytes, httpx.Headers]:
    policy = policy or default_policy()
    current_url = url
    current_headers = dict(headers or {})
    timeout = httpx.Timeout(policy.read_timeout, connect=policy.connect_timeout)
    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=False,
        verify=policy.verify_tls,
    ) as client:
        for redirect_count in range(policy.max_redirects + 1):
            await assert_safe_target_url(
                current_url,
                allow_private=policy.allow_private,
                allow_loopback=policy.allow_loopback,
                allowed_schemes=policy.allowed_schemes,
            )
            async with client.stream("GET", current_url, headers=current_headers) as resp:
                if resp.status_code in {301, 302, 303, 307, 308}:
                    location = resp.headers.get("location")
                    if not location:
                        raise httpx.HTTPError("redirect missing Location")
                    if redirect_count >= policy.max_redirects:
                        raise httpx.HTTPError("too many redirects")
                    next_url = urljoin(current_url, location)
                    current_headers = _headers_for_redirect(
                        current_headers,
                        current_url,
                        next_url,
                    )
                    current_url = next_url
                    continue
                resp.raise_for_status()
                content_length = resp.headers.get("content-length")
                if content_length:
                    try:
                        if int(content_length) > policy.max_response_bytes:
                            raise ResponseTooLargeError("response too large")
                    except ValueError:
                        pass
                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.aiter_bytes():
                    total += len(chunk)
                    if total > policy.max_response_bytes:
                        raise ResponseTooLargeError("response too large")
                    chunks.append(chunk)
                return str(resp.url), b"".join(chunks), resp.headers
    raise httpx.HTTPError("fetch failed")


async def fetch_text(url: str, **kwargs) -> tuple[str, str, httpx.Headers]:
    final_url, data, headers = await fetch_bytes(url, **kwargs)
    return final_url, data.decode("utf-8", errors="replace"), headers
