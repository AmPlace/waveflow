from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from plugin_runtime import PluginError as RuntimePluginError, load_manifest, validate_stream_descriptor
from plugin_runtime.process import PluginProcess
from plugin_python_runtime import PythonEnvironmentManager
from waveflow_plugin_cli import build_project, validate_project
from waveflow_plugin_sdk import PluginError as SDKPluginError, ResolveContext, TVReference


ROOT = Path(__file__).parents[1]
PROJECT = ROOT / "bundled_plugins" / "tvb"
SOURCE = PROJECT / "plugin.py"
IDENTITY = "org.waveflow/tvb"
SIGNED_URL = "https://edge.example/tvb/news/signed.m3u8?token=fixture"
NEWS_BODY = {
    "meta": {"status": "success"},
    "content": {"url": {"hd": SIGNED_URL, "sd": "https://edge.example/tvb/news/sd.m3u8"}},
}


def _load_plugin():
    spec = importlib.util.spec_from_file_location("tvb_hls_plugin_fixture", SOURCE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FakeCapabilities:
    def __init__(self, body=NEWS_BODY, *, status=200, error=None):
        self.body = body
        self.status = status
        self.error = error
        self.calls: list[dict] = []

    def managed_http(self, url: str, **kwargs):
        self.calls.append({"url": url, **kwargs})
        if self.error is not None:
            raise self.error
        return SimpleNamespace(status=self.status, headers={}, body=self.body, response_mode="text")


def _context(capabilities) -> ResolveContext:
    return ResolveContext("tvb-fixture", 9_999_999_999_999, {}, capabilities)


class TVBHLSDeterministicTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = _load_plugin()

    def test_four_news_channels_match_legacy_golden_descriptor(self):
        for resource in ("I-NEWS", "/c/", "c2", "C3"):
            with self.subTest(resource=resource):
                caps = FakeCapabilities(json.dumps(NEWS_BODY))
                descriptor = self.module.Provider().resolve_stream(
                    TVReference("tvb", resource), _context(caps)
                )
                self.assertEqual(descriptor.url, SIGNED_URL)
                self.assertEqual(
                    (descriptor.transport, descriptor.ttl_seconds, descriptor.volatile_url,
                     descriptor.requires_proxy, descriptor.direct_playable, descriptor.headers),
                    ("hls", 1800, True, True, False, {}),
                )
                self.assertEqual(caps.calls[0]["url"],
                                 f"{self.module.NEWS_CHECKOUT_PREFIX}/ott_{resource.strip('/').upper()}_h264")
                self.assertEqual(caps.calls[0]["query"], {"profile": "safari"})
                self.assertEqual(caps.calls[0]["timeout"], 15)
                contract = validate_stream_descriptor(descriptor.as_contract())
                from provider_resolver import ProviderResolver
                bridged = ProviderResolver._bridge_descriptor("tvb", contract)
                self.assertEqual(bridged["source_type"], "hls")
                self.assertEqual(bridged["url"], SIGNED_URL)
                self.assertTrue(bridged["requires_proxy"])
                self.assertTrue(bridged["volatile_url"])
                self.assertEqual(bridged["ttl"], 1800)

    def test_golden_comparison_uses_the_same_legacy_news_contract(self):
        from adapters.tvb import _resolve_news

        class LegacyResponse:
            status_code = 200
            url = SIGNED_URL

            def __init__(self, body):
                self._body = body

            def json(self):
                return self._body

            def raise_for_status(self):
                return None

        class LegacyClient:
            async def get(self, url, **_kwargs):
                if "/news/checkout/" in url:
                    return LegacyResponse(NEWS_BODY)
                return LegacyResponse(NEWS_BODY)

        async def compare():
            import adapters.tvb as legacy

            legacy._tvb_news_cache.update({"resolved_url": "", "signed_url": "", "expires_at": 0})
            old = await _resolve_news(LegacyClient(), "I-NEWS", "I-NEWS")
            plugin = self.module.Provider().resolve_stream(
                TVReference("tvb", "I-NEWS"), _context(FakeCapabilities(NEWS_BODY))
            )
            self.assertEqual(
                {"url": plugin.url, "source_type": plugin.transport, "headers": plugin.headers,
                 "ttl": plugin.ttl_seconds, "volatile_url": plugin.volatile_url,
                 "requires_proxy": plugin.requires_proxy},
                {"url": old["url"], "source_type": old["source_type"], "headers": old["headers"],
                 "ttl": old["ttl"], "volatile_url": old["volatile_url"],
                 "requires_proxy": old["requires_proxy"]},
            )

        asyncio.run(compare())

    def test_aes128_key_and_segments_remain_on_generic_hls_rewriter_path(self):
        os.environ.setdefault("WAVEFLOW_PROXY_HANDLE_SECRET", "tvb-hls-fixture-secret-32bytes!!!")
        os.environ.setdefault("WAVEFLOW_MODE", "nas")
        from core import m3u8_rewriter

        playlist = """#EXTM3U
#EXT-X-KEY:METHOD=AES-128,URI=\"keys/live.key?token=1\"
#EXTINF:6,
segment-1.ts
"""
        with mock.patch.object(m3u8_rewriter, "issue_cached_handle",
                               side_effect=lambda **kwargs: f"fixture-{kwargs['kind']}"):
            rewritten = m3u8_rewriter.rewrite_m3u8(
                playlist,
                m3u8_rewriter.RewriteContext(
                    base_url="https://edge.example/tvb/news/index.m3u8", src_id="tvb:I-NEWS"
                ),
            )
        self.assertIn('URI="/api/media/proxy/chunk/fixture-chunk.key"', rewritten)
        self.assertIn("/api/media/proxy/chunk/fixture-chunk", rewritten)
        self.assertNotIn("keys/live.key", rewritten)

    def test_malformed_and_upstream_taxonomy_is_retryable_and_bounded(self):
        cases = (
            ("not-json", "tvb_parse_failed"),
            ({"meta": {"status": "failed", "error_message": "offline"}}, "tvb_channel_failed"),
            ({"meta": {"status": "success"}, "content": {"url": {}}}, "tvb_no_url"),
        )
        for body, provider_code in cases:
            with self.subTest(provider_code=provider_code):
                with self.assertRaises(SDKPluginError) as raised:
                    self.module.Provider().resolve_stream(
                        TVReference("tvb", "C"), _context(FakeCapabilities(body))
                    )
                self.assertEqual(raised.exception.code, "TEMPORARY_UPSTREAM_FAILURE")
                self.assertTrue(raised.exception.retryable)
                self.assertEqual(raised.exception.details["provider_code"], provider_code)
        with self.assertRaises(SDKPluginError) as http_error:
            self.module.Provider().resolve_stream(
                TVReference("tvb", "C"), _context(FakeCapabilities(status=503))
            )
        self.assertEqual(http_error.exception.details["provider_code"], "tvb_api_failed")

    def test_dash_channels_fail_closed_without_capability_request(self):
        for resource in ("JADE", "81", "J", "TVBPLUS", "82", "B", "PEARL", "84", "P"):
            caps = FakeCapabilities()
            with self.subTest(resource=resource), self.assertRaises(SDKPluginError) as raised:
                self.module.Provider().resolve_stream(TVReference("tvb", resource), _context(caps))
            self.assertEqual(raised.exception.code, "RESOURCE_NOT_FOUND")
            self.assertEqual(caps.calls, [])

    def test_plugin_source_has_no_legacy_or_drm_execution_dependency(self):
        source = SOURCE.read_text(encoding="utf-8")
        for forbidden in (
            "backend.adapters", "adapters.tvb", "resolve_tvb", "import httpx", "import requests",
            "subprocess", "StreamDescriptor.dash",
        ):
            self.assertNotIn(forbidden, source.lower())
        self.assertIn("TVB_DASH_CHANNELS", source)


class TVBRuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def test_clean_python_runtime_health_and_hls_resolution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "tvb"
            project.mkdir()
            shutil.copyfile(PROJECT / "plugin.py", project / "plugin.py")
            shutil.copyfile(PROJECT / "manifest.json", project / "manifest.json")
            manifest = load_manifest(project / "manifest.json")
            self.assertEqual(validate_project(project)["plugin"], IDENTITY)
            artifact = build_project(project, output=root / "tvb.pyz")
            manager = PythonEnvironmentManager(root / "plugin-store")
            environment = await manager.prepare(manifest, {})

            async def capability(method, payload, _timeout, _context):
                self.assertEqual(method, "core.http.fetch")
                self.assertEqual(payload["url"], f"{self.module_prefix}/ott_I-NEWS_h264")
                self.assertEqual(payload["query"], {"profile": "safari"})
                return {"status": 200, "headers": {}, "body": json.dumps(NEWS_BODY), "response_mode": "text"}

            self.module_prefix = "https://inews-api.tvb.com/news/checkout/live/hd"
            process = PluginProcess(
                (str(environment.python), "-I", str(artifact["artifact"]),
                 "--identity", manifest.identity, "--version", manifest.version),
                "tvb-runtime-fixture", capability_handler=capability,
                environment={"PATH": "/nonexistent", "LANG": "C", "PYTHONNOUSERSITE": "1"},
            )
            await process.start()
            try:
                hello = await process.call("runtime.hello", {})
                process.negotiate_protocol(hello["protocol_version"])
                self.assertEqual(hello["plugin"], IDENTITY)
                self.assertEqual(hello["owned_schemes"], [{"scheme": "tvb", "contract": "tv_provider"}])
                self.assertTrue((await process.call("runtime.health", {}))["healthy"])
                result = await process.call("tv.resolve_stream", {"scheme": "tvb", "resource_id": "I-NEWS"})
                self.assertEqual(result["transport"], "hls")
                self.assertEqual(result["url"], SIGNED_URL)
                self.assertTrue(result["requires_proxy"])
                self.assertEqual(result["ttl_seconds"], 1800)
                with self.assertRaises(RuntimePluginError) as unsupported:
                    await process.call("tv.resolve_stream", {"scheme": "tvb", "resource_id": "JADE"})
                self.assertEqual(unsupported.exception.code, "RESOURCE_NOT_FOUND")
            finally:
                await process.call("runtime.shutdown", {})
                await process.stop()


if __name__ == "__main__":
    unittest.main()
