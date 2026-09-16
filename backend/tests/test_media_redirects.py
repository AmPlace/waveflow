from __future__ import annotations

import os
import importlib
import sys
import unittest
from unittest import IsolatedAsyncioTestCase, mock

import httpx

os.environ.setdefault("WAVEFLOW_PROXY_HANDLE_SECRET", "media-redirect-test-secret-32bytes")

from infrastructure import http_client as media_http
from ssrf_guard import UnsafeTargetError


class MediaRedirectSecurityTest(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        global RewriteContext, rewrite_m3u8, clear_handle_cache_for_tests, decode_for_kind
        for name in ("core.m3u8_rewriter", "security.proxy_handles", "security.secrets"):
            sys.modules.pop(name, None)
        proxy_handles = importlib.import_module("security.proxy_handles")
        rewriter = importlib.import_module("core.m3u8_rewriter")
        RewriteContext = rewriter.RewriteContext
        rewrite_m3u8 = rewriter.rewrite_m3u8
        clear_handle_cache_for_tests = proxy_handles.clear_handle_cache_for_tests
        decode_for_kind = proxy_handles.decode_for_kind
        clear_handle_cache_for_tests()

    async def _request(self, handler, *, headers=None, max_redirects=5, omit_headers=None):
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
        )
        try:
            return await media_http.request_with_safe_redirects(
                client,
                "GET",
                "https://a.example/start",
                headers=headers,
                max_redirects=max_redirects,
                omit_headers=omit_headers,
            )
        finally:
            await client.aclose()

    async def test_public_redirect_supports_relative_and_protocol_relative_locations(self):
        seen = []
        validated = []

        def handler(request):
            seen.append(str(request.url))
            if request.url.host == "a.example" and request.url.path == "/start":
                return httpx.Response(302, headers={"location": "../next"}, request=request)
            if request.url.host == "a.example" and request.url.path == "/next":
                return httpx.Response(307, headers={"location": "//cdn.example/live"}, request=request)
            return httpx.Response(200, content=b"ok", request=request)

        async def allow(url, **_kwargs):
            validated.append(url)

        with mock.patch.object(media_http, "assert_safe_target_url", new=allow):
            response = await self._request(handler)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.url, httpx.URL("https://cdn.example/live"))
            self.assertEqual(response.content, b"ok")

        self.assertEqual(
            seen,
            [
                "https://a.example/start",
                "https://a.example/next",
                "https://cdn.example/live",
            ],
        )
        self.assertEqual(seen, validated)

    async def test_public_to_private_is_rejected_before_private_connection(self):
        seen = []

        def handler(request):
            seen.append(str(request.url))
            return httpx.Response(
                302,
                headers={"location": "http://192.168.1.10/live"},
                request=request,
            )

        async def guard(url, **_kwargs):
            if "192.168.1.10" in url:
                raise UnsafeTargetError("private target")

        with mock.patch.object(media_http, "assert_safe_target_url", new=guard):
            with self.assertRaises(media_http.RedirectTargetRejected):
                await self._request(handler)

        self.assertEqual(seen, ["https://a.example/start"])

    async def test_loopback_redirect_follows_policy_allow_or_deny(self):
        def handler(request):
            if request.url.host == "a.example":
                return httpx.Response(
                    302,
                    headers={"location": "http://127.0.0.1:8080/live"},
                    request=request,
                )
            return httpx.Response(200, content=b"ok", request=request)

        async def deny_loopback(url, **_kwargs):
            if "127.0.0.1" in url:
                raise UnsafeTargetError("loopback target")

        with mock.patch.object(media_http, "assert_safe_target_url", new=deny_loopback):
            with self.assertRaises(media_http.RedirectTargetRejected):
                await self._request(handler)

        validated = []

        async def allow_loopback(url, **_kwargs):
            validated.append(url)

        with mock.patch.object(media_http, "assert_safe_target_url", new=allow_loopback):
            response = await self._request(handler)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content, b"ok")
        self.assertEqual(validated[-1], "http://127.0.0.1:8080/live")

    async def test_hard_block_redirect_is_rejected_even_when_allow_flags_are_enabled(self):
        seen = []

        def handler(request):
            seen.append(str(request.url))
            return httpx.Response(
                302,
                headers={"location": "http://169.254.169.254/latest"},
                request=request,
            )

        async def guard(url, **_kwargs):
            if "169.254.169.254" in url:
                raise UnsafeTargetError("hard-block target")

        with mock.patch.object(media_http, "assert_safe_target_url", new=guard):
            with self.assertRaises(media_http.RedirectTargetRejected):
                await self._request(handler)
        self.assertEqual(seen, ["https://a.example/start"])

    async def test_multihop_redirect_revalidates_before_blocked_final_hop(self):
        seen = []

        def handler(request):
            seen.append(str(request.url))
            if request.url.host == "a.example":
                return httpx.Response(302, headers={"location": "https://b.example/next"}, request=request)
            return httpx.Response(302, headers={"location": "http://10.0.0.5/private"}, request=request)

        async def guard(url, **_kwargs):
            if "10.0.0.5" in url:
                raise UnsafeTargetError("private target")

        with mock.patch.object(media_http, "assert_safe_target_url", new=guard):
            with self.assertRaises(media_http.RedirectTargetRejected):
                await self._request(handler)
        self.assertEqual(seen, ["https://a.example/start", "https://b.example/next"])

    async def test_redirect_loop_stops_at_maximum(self):
        seen = []

        def handler(request):
            seen.append(str(request.url))
            return httpx.Response(302, headers={"location": "https://a.example/loop"}, request=request)

        async def allow(url, **_kwargs):
            return None

        with mock.patch.object(media_http, "assert_safe_target_url", new=allow):
            with self.assertRaises(httpx.TooManyRedirects):
                await self._request(handler, max_redirects=2)
        self.assertEqual(len(seen), 3)

    async def test_cross_origin_redirect_drops_credentials_but_keeps_media_headers(self):
        final_headers = {}

        def handler(request):
            if request.url.host == "a.example":
                return httpx.Response(302, headers={"location": "https://b.example/final"}, request=request)
            final_headers.update(dict(request.headers))
            return httpx.Response(200, content=b"ok", request=request)

        async def allow(url, **_kwargs):
            return None

        headers = {
            "Cookie": "sid=secret",
            "Authorization": "Bearer secret",
            "Range": "bytes=0-99",
            "If-None-Match": '"etag"',
            "Referer": "https://player.example/",
        }
        with mock.patch.object(media_http, "assert_safe_target_url", new=allow):
            response = await self._request(handler, headers=headers)
        self.assertEqual(response.content, b"ok")
        self.assertNotIn("cookie", final_headers)
        self.assertNotIn("authorization", final_headers)
        self.assertEqual(final_headers.get("range"), "bytes=0-99")
        self.assertEqual(final_headers.get("if-none-match"), '"etag"')
        self.assertEqual(final_headers.get("referer"), "https://player.example/")

    async def test_stateless_client_does_not_reuse_upstream_set_cookie(self):
        seen_cookies = []

        def handler(request):
            seen_cookies.append((str(request.url), request.headers.get("cookie")))
            if request.url.path == "/seed":
                return httpx.Response(
                    200,
                    headers={"set-cookie": "sid=from-upstream; Path=/"},
                    request=request,
                )
            return httpx.Response(200, content=b"ok", request=request)

        client = media_http.StatelessAsyncClient(transport=httpx.MockTransport(handler))
        try:
            await client.get("https://a.example/seed")
            await client.get("https://a.example/same-origin")
            await client.get("https://b.example/cross-origin")
            await client.get(
                "https://a.example/explicit",
                headers={"Cookie": "sid=explicit"},
            )
        finally:
            await client.aclose()

        self.assertEqual(
            seen_cookies,
            [
                ("https://a.example/seed", None),
                ("https://a.example/same-origin", None),
                ("https://b.example/cross-origin", None),
                ("https://a.example/explicit", "sid=explicit"),
            ],
        )

    async def test_omitted_default_header_is_removed_on_every_redirect_hop(self):
        seen_user_agents = []

        def handler(request):
            seen_user_agents.append(request.headers.get("user-agent"))
            if request.url.host == "a.example":
                return httpx.Response(302, headers={"location": "https://b.example/final"}, request=request)
            return httpx.Response(200, content=b"ok", request=request)

        async def allow(url, **_kwargs):
            return None

        with mock.patch.object(media_http, "assert_safe_target_url", new=allow):
            response = await self._request(handler, omit_headers={"User-Agent"})

        self.assertEqual(response.content, b"ok")
        self.assertEqual(seen_user_agents, [None, None])

    async def test_playlist_final_url_is_used_as_rewrite_base(self):
        def handler(request):
            if request.url.host == "a.example":
                return httpx.Response(
                    302,
                    headers={"location": "https://cdn.example/live/master.m3u8"},
                    request=request,
                )
            return httpx.Response(
                200,
                text="#EXTM3U\n#EXTINF:2,\nseg.ts\n",
                request=request,
            )

        async def allow(url, **_kwargs):
            return None

        with mock.patch.object(media_http, "assert_safe_target_url", new=allow):
            response = await self._request(handler)
        rewritten = rewrite_m3u8(
            response.text,
            RewriteContext(
                base_url=str(response.url),
                src_id="channel:test",
                src_label="channel:test",
            ),
        )
        proxy_path = next(
            line for line in rewritten.splitlines()
            if line.startswith("/api/media/proxy/chunk/")
        )
        token = proxy_path.rsplit("/", 1)[-1].split("?", 1)[0].removesuffix(".ts")
        payload = decode_for_kind(token, "chunk")
        self.assertEqual(payload.url, "https://cdn.example/live/seg.ts")
        self.assertIn("?wf_seq=0", proxy_path)

    async def test_stream_redirect_keeps_response_streaming(self):
        seen = []

        def handler(request):
            seen.append(str(request.url))
            if request.url.host == "a.example":
                return httpx.Response(307, headers={"location": "https://cdn.example/live.ts"}, request=request)
            return httpx.Response(
                200,
                stream=httpx.ByteStream(b"flv-ts-bytes"),
                request=request,
            )

        async def allow(url, **_kwargs):
            return None

        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
        )
        try:
            with mock.patch.object(media_http, "assert_safe_target_url", new=allow):
                response = await media_http.stream_with_safe_redirects(
                    client,
                    "GET",
                    "https://a.example/start",
                    headers={"Range": "bytes=0-"},
                )
                chunks = [chunk async for chunk in response.aiter_raw()]
                await response.aclose()
        finally:
            await client.aclose()

        self.assertEqual(seen, ["https://a.example/start", "https://cdn.example/live.ts"])
        self.assertEqual(b"".join(chunks), b"flv-ts-bytes")

    async def test_stream_blocked_redirect_is_rejected_before_second_connection(self):
        seen = []

        def handler(request):
            seen.append(str(request.url))
            return httpx.Response(
                302,
                headers={"location": "http://169.254.169.254/metadata"},
                request=request,
            )

        async def guard(url, **_kwargs):
            if "169.254.169.254" in url:
                raise UnsafeTargetError("hard-block target")

        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
        )
        try:
            with mock.patch.object(media_http, "assert_safe_target_url", new=guard):
                with self.assertRaises(media_http.RedirectTargetRejected):
                    await media_http.stream_with_safe_redirects(
                        client,
                        "GET",
                        "https://a.example/start",
                    )
        finally:
            await client.aclose()

        self.assertEqual(seen, ["https://a.example/start"])


if __name__ == "__main__":
    unittest.main()
