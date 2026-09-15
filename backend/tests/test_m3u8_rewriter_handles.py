import asyncio
import importlib
import re
import os
import sys
import unittest
from unittest import mock

import httpx


os.environ["WAVEFLOW_PROXY_HANDLE_SECRET"] = "test-m3u8-handle-secret-32bytes!!!"
os.environ.setdefault("WAVEFLOW_MODE", "nas")
os.environ["WAVEFLOW_DB_PATH"] = ":memory:"
for mod in list(sys.modules):
    if mod in ("security.secrets", "security.proxy_handles", "core.m3u8_rewriter"):
        del sys.modules[mod]

from infrastructure.http_client import request_with_safe_redirects, stream_with_safe_redirects


class M3u8RewriterHandleTest(unittest.TestCase):
    def setUp(self):
        # Other suites intentionally reload the security modules with isolated
        # secrets. Keep the issuer and decoder from the same module generation.
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

    def test_same_upstream_uris_rewrite_to_stable_proxy_handles(self):
        text = """#EXTM3U
#EXT-X-VERSION:7
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",URI="audio/main.m3u8"
#EXT-X-KEY:METHOD=AES-128,URI="keys/live.key?token=1"
#EXT-X-MAP:URI="../init.mp4"
#EXT-X-PART:DURATION=0.333,URI="parts/part-1.m4s"
#EXTINF:6,
seg-100.ts?edge=1
"""
        ctx = RewriteContext(
            base_url="https://cdn.example.com/live/variant/index.m3u8",
            src_id="channel:test",
            src_label="channel:test",
            ctx_id="ctx1",
        )

        first = rewrite_m3u8(text, ctx)
        second = rewrite_m3u8(text, ctx)

        self.assertEqual(first, second)
        self.assertIn("/api/media/proxy/playlist/", first)
        self.assertIn("/api/media/proxy/chunk/", first)
        self.assertNotIn("https://cdn.example.com", first)

    def _decode_rewritten_uri(self, uri: str, kind: str):
        prefix = f"/api/media/proxy/{kind}/"
        self.assertTrue(uri.startswith(prefix), uri)
        handle = uri[len(prefix):].split("?", 1)[0]
        if kind == "chunk":
            handle = handle.rsplit('.', 1)[0]
        return decode_for_kind(handle, kind)

    def test_extensionless_master_variant_uses_playlist_handle(self):
        text = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=800000\nvariant/live?token=one\n"
        rewritten = rewrite_m3u8(text, RewriteContext(
            base_url="https://cdn.example.com/master/index.m3u8",
            src_id="channel:test",
        ))

        uri = rewritten.splitlines()[-1]
        payload = self._decode_rewritten_uri(uri, "playlist")
        self.assertEqual(payload.url, "https://cdn.example.com/master/variant/live?token=one")

    def test_extensionless_media_uri_defaults_to_signed_chunk(self):
        text = "#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXTINF:4,\nsegments/current?token=one\n"
        rewritten = rewrite_m3u8(text, RewriteContext(
            base_url="https://cdn.example.com/live/index.m3u8",
            src_id="channel:test",
        ))

        uri = rewritten.splitlines()[-1]
        payload = self._decode_rewritten_uri(uri, "chunk")
        self.assertEqual(payload.url, "https://cdn.example.com/live/segments/current?token=one")
        self.assertIn("?wf_seq=0", uri)
        self.assertNotIn("https://cdn.example.com", rewritten)

    def test_strict_signed_origin_preserves_path_and_query_across_child_media_flow(self):
        expected = {
            "https://signed.example/live/master.m3u8?exp=master&sig=master": (
                "#EXTM3U\n"
                "#EXT-X-STREAM-INF:BANDWIDTH=1000000\n"
                "child/index.m3u8?exp=child&sig=child\n",
                "application/x-mpegURL",
            ),
            "https://signed.example/live/child/index.m3u8?exp=child&sig=child": (
                "#EXTM3U\n"
                '#EXT-X-KEY:METHOD=AES-128,URI="../keys/key.bin?exp=key&sig=key"\n'
                "#EXTINF:4,\n"
                "../segments/segment.ts?exp=segment&sig=segment\n",
                "application/x-mpegURL",
            ),
            "https://signed.example/live/keys/key.bin?exp=key&sig=key": (b"key", "application/octet-stream"),
            "https://signed.example/live/segments/segment.ts?exp=segment&sig=segment": (b"segment", "video/mp2t"),
        }
        seen = []

        def strict_origin(request):
            url = str(request.url)
            seen.append(url)
            response = expected.get(url)
            if response is None:
                return httpx.Response(403, request=request, text="signature mismatch")
            body, content_type = response
            return httpx.Response(
                200,
                request=request,
                content=body.encode() if isinstance(body, str) else body,
                headers={"Content-Type": content_type},
            )

        async def exercise_flow():
            async with httpx.AsyncClient(transport=httpx.MockTransport(strict_origin)) as client:
                master_url = "https://signed.example/live/master.m3u8?exp=master&sig=master"
                master_response = await request_with_safe_redirects(client, "GET", master_url)
                master_text = master_response.text
                master_rewritten = rewrite_m3u8(
                    master_text,
                    RewriteContext(base_url=str(master_response.url), src_id="channel:signed"),
                )
                child_uri = next(
                    line for line in master_rewritten.splitlines()
                    if line.startswith("/api/media/proxy/playlist/")
                )
                child_payload = self._decode_rewritten_uri(child_uri, "playlist")
                self.assertEqual(child_payload.url, "https://signed.example/live/child/index.m3u8?exp=child&sig=child")

                child_response = await request_with_safe_redirects(client, "GET", child_payload.url)
                child_rewritten = rewrite_m3u8(
                    child_response.text,
                    RewriteContext(base_url=str(child_response.url), src_id="channel:signed"),
                )
                key_uri = re.search(r'URI="(/api/media/proxy/chunk/[^\"]+)"', child_rewritten).group(1)
                segment_uri = next(
                    line for line in child_rewritten.splitlines()
                    if line.startswith("/api/media/proxy/chunk/") and "wf_seq=" in line
                )
                key_payload = self._decode_rewritten_uri(key_uri, "chunk")
                segment_payload = self._decode_rewritten_uri(segment_uri, "chunk")
                self.assertEqual(key_payload.url, "https://signed.example/live/keys/key.bin?exp=key&sig=key")
                self.assertEqual(segment_payload.url, "https://signed.example/live/segments/segment.ts?exp=segment&sig=segment")

                key_response = await stream_with_safe_redirects(client, "GET", key_payload.url)
                segment_response = await stream_with_safe_redirects(client, "GET", segment_payload.url)
                try:
                    self.assertEqual(key_response.status_code, 200)
                    self.assertEqual(segment_response.status_code, 200)
                    self.assertEqual(await key_response.aread(), b"key")
                    self.assertEqual(await segment_response.aread(), b"segment")
                finally:
                    await key_response.aclose()
                    await segment_response.aclose()

        with mock.patch("infrastructure.http_client.assert_safe_target_url", new=mock.AsyncMock()):
            asyncio.run(exercise_flow())

        self.assertEqual(seen, list(expected))

    def test_query_only_variant_uri_uses_playlist_handle(self):
        text = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=800000\n?variant=audio\n"
        rewritten = rewrite_m3u8(text, RewriteContext(
            base_url="https://cdn.example.com/live/master",
            src_id="channel:test",
        ))

        payload = self._decode_rewritten_uri(rewritten.splitlines()[-1], "playlist")
        self.assertEqual(payload.url, "https://cdn.example.com/live/master?variant=audio")

    def test_uri_attribute_tags_use_tag_semantics_without_extensions(self):
        text = """#EXTM3U
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",URI="audio/rendition?token=1"
#EXT-X-I-FRAME-STREAM-INF:BANDWIDTH=100000,URI="iframe/rendition"
#EXT-X-RENDITION-REPORT:URI="../other/rendition",LAST-MSN=10
#EXT-X-SESSION-KEY:METHOD=AES-128,URI="keys/session"
#EXT-X-KEY:METHOD=AES-128,URI="keys/current"
#EXT-X-MAP:URI="init/current"
#EXT-X-PRELOAD-HINT:TYPE=PART,URI="parts/next"
"""
        rewritten = rewrite_m3u8(text, RewriteContext(
            base_url="https://cdn.example.com/live/index.m3u8",
            src_id="channel:test",
        ))

        lines = rewritten.splitlines()
        for index in (1, 2, 3):
            self.assertIn("/api/media/proxy/playlist/", lines[index])
        for index in (4, 5, 6, 7):
            self.assertIn("/api/media/proxy/chunk/", lines[index])
        self.assertNotIn("https://cdn.example.com", rewritten)

    def test_session_data_and_special_schemes_remain_unmodified(self):
        text = """#EXTM3U
#EXT-X-SESSION-DATA:DATA-ID="com.example",URI="https://metadata.example/info.json"
#EXT-X-SESSION-KEY:METHOD=SAMPLE-AES,KEYFORMAT="com.apple.streamingkeydelivery",URI="skd://asset-id"
#EXT-X-KEY:METHOD=AES-128,URI="data:text/plain;base64,AAAA"
"""
        rewritten = rewrite_m3u8(text, RewriteContext(
            base_url="https://cdn.example.com/live/index.m3u8",
            src_id="channel:test",
        ))

        self.assertIn('URI="https://metadata.example/info.json"', rewritten)
        self.assertIn('URI="skd://asset-id"', rewritten)
        self.assertIn('URI="data:text/plain;base64,AAAA"', rewritten)

    def test_utf8_bom_header_is_not_rewritten_as_media_uri(self):
        rewritten = rewrite_m3u8(
            "\ufeff#EXTM3U\r\n#EXTINF:4,\r\nsegment.vtt\r\n",
            RewriteContext(
                base_url="https://cdn.example.com/live/index.m3u8",
                src_id="channel:test",
            ),
        )

        self.assertTrue(rewritten.startswith("\ufeff#EXTM3U\n"))
        self.assertIn("/api/media/proxy/chunk/", rewritten)


if __name__ == "__main__":
    unittest.main()
