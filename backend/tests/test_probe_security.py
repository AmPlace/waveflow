import asyncio
import json
import sys
import types
import unittest
from unittest import IsolatedAsyncioTestCase, mock

import httpx

import iptv_probe
from infrastructure import http_client as media_http
from routers import media_proxy
from ssrf_guard import UnsafeTargetError


class ProbeHttpSecurityTest(IsolatedAsyncioTestCase):
    async def _run_probe(self, handler, url, *, source_type="hls", guard=None):
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
        )
        try:
            with mock.patch.object(
                iptv_probe,
                "_enrich_with_ffprobe",
                new=mock.AsyncMock(side_effect=lambda result, *_: result),
            ):
                guard_patch = mock.patch.object(
                    media_http,
                    "assert_safe_target_url",
                    new=guard or mock.AsyncMock(),
                )
                with guard_patch:
                    return await iptv_probe.probe_channel_source(
                        {"url": url, "source_type": source_type},
                        client,
                    )
        finally:
            await client.aclose()

    async def test_http_redirect_to_private_is_rejected_before_second_connection(self):
        seen = []

        def handler(request):
            seen.append(str(request.url))
            return httpx.Response(
                302,
                headers={"location": "http://private.example/live.m3u8"},
                request=request,
            )

        async def guard(url, **_kwargs):
            if "private.example" in url:
                raise UnsafeTargetError("private target")

        result = await self._run_probe(handler, "https://public.example/live.m3u8", guard=guard)

        self.assertEqual(result["probe_status"], "error")
        self.assertEqual(result["last_error"], "ssrf_policy_rejected")
        self.assertEqual(json.loads(result["probe_meta_json"])["security_rejection"], "ssrf_policy")
        self.assertEqual(seen, ["https://public.example/live.m3u8"])

    async def test_private_loopback_and_hard_block_targets_use_same_guard(self):
        blocked_hosts = {
            "private.example",
            "loopback.example",
            "metadata.example",
        }

        async def guard(url, **_kwargs):
            if httpx.URL(url).host in blocked_hosts:
                raise UnsafeTargetError("blocked target")

        for host in blocked_hosts:
            with self.subTest(host=host):
                result = await self._run_probe(
                    lambda request: httpx.Response(200, text="#EXTM3U\nsegment.ts\n", request=request),
                    f"https://{host}/live.m3u8",
                    guard=guard,
                )
                self.assertEqual(result["last_error"], "ssrf_policy_rejected")

    async def test_public_redirect_and_relative_segment_are_probeable(self):
        seen = []

        def handler(request):
            seen.append(str(request.url))
            if request.url.host == "public.example":
                return httpx.Response(
                    302,
                    headers={"location": "https://cdn.example/live/master.m3u8"},
                    request=request,
                )
            if request.url.path.endswith("master.m3u8"):
                return httpx.Response(200, text="#EXTM3U\nsegment.ts\n", request=request)
            return httpx.Response(200, content=b"segment-bytes", request=request)

        result = await self._run_probe(
            handler,
            "https://public.example/live.m3u8",
            guard=mock.AsyncMock(),
        )

        self.assertEqual(result["probe_status"], "online")
        self.assertEqual(seen, [
            "https://public.example/live.m3u8",
            "https://cdn.example/live/master.m3u8",
            "https://cdn.example/live/segment.ts",
        ])

    async def test_variant_redirect_target_is_rejected(self):
        seen = []

        def handler(request):
            seen.append(str(request.url))
            return httpx.Response(
                200,
                text="#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=100\nhttp://private.example/variant.m3u8\n",
                request=request,
            )

        async def guard(url, **_kwargs):
            if "private.example" in url:
                raise UnsafeTargetError("private target")

        result = await self._run_probe(handler, "https://public.example/master.m3u8", guard=guard)

        self.assertEqual(result["last_error"], "ssrf_policy_rejected")
        self.assertEqual(seen, ["https://public.example/master.m3u8"])

    async def test_segment_target_is_rejected_instead_of_being_downgraded(self):
        seen = []

        def handler(request):
            seen.append(str(request.url))
            return httpx.Response(200, text="#EXTM3U\nhttp://private.example/segment.ts\n", request=request)

        async def guard(url, **_kwargs):
            if "private.example" in url:
                raise UnsafeTargetError("private target")

        result = await self._run_probe(handler, "https://public.example/master.m3u8", guard=guard)

        self.assertEqual(result["last_error"], "ssrf_policy_rejected")
        self.assertEqual(seen, ["https://public.example/master.m3u8"])


class ProbeSubprocessSecurityTest(IsolatedAsyncioTestCase):
    async def test_blocked_ffmpeg_target_does_not_spawn_process(self):
        with mock.patch.object(
            iptv_probe,
            "assert_safe_target_url",
            new=mock.AsyncMock(side_effect=UnsafeTargetError("private target")),
        ), mock.patch.object(iptv_probe, "media_tool_bin", return_value="/usr/bin/ffmpeg"), mock.patch.object(
            asyncio,
            "create_subprocess_exec",
            new=mock.AsyncMock(),
        ) as spawn:
            result = await iptv_probe._probe_with_ffmpeg(
                "rtsp://private.example/live",
                {},
                reason="rt_stream",
            )

        self.assertEqual(result["last_error"], "ssrf_policy_rejected")
        spawn.assert_not_awaited()

    async def test_allowed_ffprobe_target_reaches_subprocess_after_validation(self):
        class Process:
            returncode = 0

            async def communicate(self):
                return b'{"streams": []}', b""

        guard = mock.AsyncMock()
        with mock.patch.object(iptv_probe, "assert_safe_target_url", new=guard), mock.patch.object(
            iptv_probe,
            "media_tool_bin",
            return_value="/usr/bin/ffprobe",
        ), mock.patch.object(
            asyncio,
            "create_subprocess_exec",
            new=mock.AsyncMock(return_value=Process()),
        ) as spawn:
            result = await iptv_probe._probe_media_info("https://public.example/live.m3u8", {})

        guard.assert_awaited_once_with(
            "https://public.example/live.m3u8",
            allowed_schemes={"http", "https", "rtmp", "rtsp"},
        )
        spawn.assert_awaited_once()
        self.assertTrue(result["meta"]["ffprobe"])


class AdminProbeSecurityTest(IsolatedAsyncioTestCase):
    async def test_admin_probe_uses_safe_redirect_helper(self):
        class Response:
            status_code = 200
            url = httpx.URL("https://public.example/final")
            headers = {"content-type": "text/plain", "content-length": "2"}
            content = b"ok"
            closed = False

            async def aclose(self):
                self.closed = True

        response = Response()
        fake_main = types.SimpleNamespace(
            http_client=object(),
            CDN_REQUEST_HEADERS={"User-Agent": "WaveFlow-Test"},
        )
        old_main = sys.modules.get("main")
        try:
            sys.modules["main"] = fake_main
            with mock.patch.object(media_proxy, "request_with_safe_redirects", new=mock.AsyncMock(return_value=response)) as safe_request:
                result = await media_proxy.admin_probe_url({"url": "https://public.example/start"}, {})
        finally:
            if old_main is not None:
                sys.modules["main"] = old_main
            else:
                sys.modules.pop("main", None)

        safe_request.assert_awaited_once_with(
            fake_main.http_client,
            "GET",
            "https://public.example/start",
            timeout=8.0,
            headers={"User-Agent": "WaveFlow-Test"},
        )
        self.assertEqual(result["final_url"], "https://public.example/final")
        self.assertTrue(response.closed)

    async def test_admin_probe_rejects_unsafe_redirect(self):
        fake_main = types.SimpleNamespace(
            http_client=object(),
            CDN_REQUEST_HEADERS={"User-Agent": "WaveFlow-Test"},
        )
        old_main = sys.modules.get("main")
        try:
            sys.modules["main"] = fake_main
            with mock.patch.object(
                media_proxy,
                "request_with_safe_redirects",
                new=mock.AsyncMock(side_effect=media_http.RedirectTargetRejected(UnsafeTargetError("blocked"))),
            ):
                with self.assertRaises(media_proxy.HTTPException) as ctx:
                    await media_proxy.admin_probe_url({"url": "https://public.example/start"}, {})
        finally:
            if old_main is not None:
                sys.modules["main"] = old_main
            else:
                sys.modules.pop("main", None)

        self.assertEqual(ctx.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
