from __future__ import annotations

import asyncio
import importlib.util
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from plugin_runtime import load_manifest
from plugin_runtime.process import PluginProcess
from plugin_python_runtime import PythonEnvironmentManager
from waveflow_plugin_cli import build_project
from waveflow_plugin_sdk import PluginError, ResolveContext, TVReference


ROOT = Path(__file__).parents[1]
SOURCE = ROOT / "bundled_plugins" / "youtube" / "plugin.py"
PROJECT = SOURCE.parent
DEPENDENCIES = ROOT / "official_plugins" / "dependency_artifacts" / "youtube"
CHANNEL_ID = "UC0123456789012345678901"
VIDEO_ID = "abcDEF123_4"
PLAY_URL = "https://video.googlevideo.com/live/video.m3u8"


def _load_module():
    spec = importlib.util.spec_from_file_location("youtube_plugin_fixture", SOURCE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _page(*, live: bool = True) -> str:
    marker = '"isLive":true "watching now"' if live else '"isLive":false'
    return (
        f'<meta itemprop="channelId" content="{CHANNEL_ID}"> '
        f'{{"videoPrimaryInfoRenderer" "videoId":"{VIDEO_ID}" {marker} '
        '"videoSecondaryInfoRenderer"}'
    )


class FakeStream:
    def __init__(self, url: str = PLAY_URL):
        self.url = url

    def to_url(self):
        return self.url


class FakeStreamlink:
    result = {"best": FakeStream()}
    failure: BaseException | None = None
    options: list[tuple[str, object]] = []

    def __init__(self):
        self.options = []
        type(self).options = self.options

    def set_option(self, name, value):
        self.options.append((name, value))

    def streams(self, _url):
        if self.failure:
            raise self.failure
        return self.result


class YouTubePluginContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = _load_module()

    def _provider(self):
        return self.module.Provider()

    def _context(self):
        return ResolveContext("youtube-fixture", 9_999_999_999_999, {}, None)

    def test_reference_parsing_covers_video_channel_live_embed_shorts_and_handle(self):
        cases = {
            "abcDEF123_4": "https://www.youtube.com/watch?v=abcDEF123_4",
            "live/abcDEF123_4": "https://www.youtube.com/live/abcDEF123_4",
            "embed/abcDEF123_4": "https://www.youtube.com/embed/abcDEF123_4",
            "shorts/abcDEF123_4": "https://www.youtube.com/shorts/abcDEF123_4",
            "@waveflow": "https://www.youtube.com/@waveflow",
            CHANNEL_ID: f"https://www.youtube.com/channel/{CHANNEL_ID}",
        }
        for resource, expected in cases.items():
            with self.subTest(resource=resource):
                reference = TVReference("youtube", resource)
                self.assertEqual(self.module._build_youtube_url(reference), expected)
        self.assertEqual(
            self.module._build_youtube_url(TVReference("youtube", "resolve", {"url": ["https://youtu.be/abcDEF123_4"]})),
            "https://youtu.be/abcDEF123_4",
        )

    def test_reference_rejects_untrusted_host_malformed_and_playlist_only_input(self):
        values = (
            "https://evil.example/watch?v=abcDEF123_4",
            "https://evil.youtube.com/watch?v=abcDEF123_4",
            "not a URL",
            "https://www.youtube.com/playlist?list=PLfixture",
        )
        for value in values:
            with self.subTest(value=value):
                with self.assertRaises(self.module.InvalidResource):
                    self.module._build_youtube_url(TVReference("youtube", "resolve", {"url": [value]}))

        for resource in ("invalid-id", "live/not-a-video-id", "channel/not-a-channel-id"):
            with self.subTest(resource=resource):
                with self.assertRaises(self.module.InvalidResource):
                    self.module._build_youtube_url(TVReference("youtube", resource))

    def test_streamlink_descriptor_and_generic_metadata_match_legacy_semantics(self):
        fake = FakeStreamlink
        fake.result = {"best": FakeStream(PLAY_URL), "720p": FakeStream("https://video.googlevideo.com/720.m3u8")}
        fake.failure = None
        with mock.patch.object(self.module, "Streamlink", fake):
            descriptor = self._provider().resolve_stream(
                TVReference("youtube", f"https://www.youtube.com/watch?v={VIDEO_ID}"), self._context(),
            )
        self.assertEqual(
            (descriptor.transport, descriptor.url, descriptor.ttl_seconds,
             descriptor.volatile_url, descriptor.requires_proxy, descriptor.direct_playable),
            ("hls", PLAY_URL, 120, True, True, False),
        )
        self.assertEqual(descriptor.provider_diagnostics["video_id"], VIDEO_ID)
        self.assertEqual(descriptor.provider_diagnostics["available_streams"], ["best", "720p"])
        self.assertEqual(descriptor.probe_hints["video_id"], VIDEO_ID)
        self.assertNotIn("youtube_video_id", descriptor.as_contract())

    def test_probe_only_metadata_round_trip_and_page_live_state(self):
        with mock.patch.object(self.module, "urlopen") as fetch:
            response = mock.MagicMock()
            response.status = 200
            response.read.return_value = _page(live=True).encode()
            fetch.return_value.__enter__.return_value = response
            descriptor = self._provider().resolve_stream(
                TVReference("youtube", "live/abcDEF123_4", {"probe": ["1"]}), self._context(),
            )
        self.assertEqual(
            (descriptor.transport, descriptor.url, descriptor.ttl_seconds,
             descriptor.requires_proxy, descriptor.direct_playable),
            ("probe_only", "", 120, True, False),
        )
        self.assertEqual(descriptor.provider_diagnostics["channel_id"], CHANNEL_ID)
        self.assertTrue(descriptor.probe_hints["page_live"])
        self.assertEqual(descriptor.as_contract()["provider_diagnostics"]["video_id"], VIDEO_ID)

    def test_page_not_live_login_po_token_timeout_and_upstream_taxonomy(self):
        with mock.patch.object(self.module, "urlopen") as fetch:
            response = mock.MagicMock(); response.status = 200; response.read.return_value = _page(live=False).encode()
            fetch.return_value.__enter__.return_value = response
            with self.assertRaises(PluginError) as not_live:
                self._provider().resolve_stream(TVReference("youtube", "live/abcDEF123_4", {"probe": ["1"]}), self._context())
        self.assertEqual((not_live.exception.code, not_live.exception.details["provider_code"]), ("NOT_LIVE", "youtube_not_live"))

        for failure, expected in ((self.module.NoStreamsError("LOGIN_REQUIRED"), "youtube_risk_control"),
                                  (TimeoutError("fixture"), "youtube_resolve_timeout"),
                                  (RuntimeError("upstream"), "youtube_resolve_failed")):
            FakeStreamlink.failure = failure
            with mock.patch.object(self.module, "Streamlink", FakeStreamlink):
                with self.subTest(expected=expected):
                    with self.assertRaises(PluginError) as raised:
                        self._provider().resolve_stream(TVReference("youtube", VIDEO_ID), self._context())
            self.assertEqual(raised.exception.details["provider_code"], expected)
        FakeStreamlink.failure = None

    def test_plugin_is_independent_from_legacy_and_has_no_browser_or_node_boundary(self):
        source = SOURCE.read_text(encoding="utf-8")
        for forbidden in ("adapters.youtube", "backend.adapters.youtube", "import httpx", "chromium", "playwright", "selenium"):
            self.assertNotIn(forbidden, source)
        manifest = load_manifest(PROJECT / "manifest.json")
        self.assertEqual(manifest.identity, "org.waveflow/youtube")
        self.assertEqual(manifest.permissions["network"]["direct"], True)
        self.assertEqual(len(manifest.runtime["dependency_lock"]["artifacts"]), 20)


class YouTubeIsolatedRuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def test_clean_isolated_environment_imports_streamlink_and_plugin_health(self):
        manifest = load_manifest(PROJECT / "manifest.json")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            project.mkdir()
            for filename in ("plugin.py", "manifest.json", "dependency-lock.json"):
                shutil.copyfile(PROJECT / filename, project / filename)
            build = build_project(project, output=root / "youtube.pyz")
            references = {
                item["sha256"]: DEPENDENCIES / item["filename"]
                for item in manifest.runtime["dependency_lock"]["artifacts"]
            }
            manager = PythonEnvironmentManager(root / "plugin-store")
            environment = await manager.prepare(manifest, references)
            probe = await asyncio.create_subprocess_exec(
                str(environment.python), "-I", "-c",
                "import shutil, streamlink, sys; print(streamlink.__version__); print(sys.prefix != sys.base_prefix); print(shutil.which('node') is None)",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={"PATH": "/usr/bin:/bin", "PYTHONNOUSERSITE": "1"},
            )
            stdout, stderr = await probe.communicate()
            self.assertEqual(probe.returncode, 0, stderr.decode())
            self.assertEqual(stdout.decode().splitlines(), ["8.4.0", "True", "True"])

            process = PluginProcess(
                (str(environment.python), "-I", str(build["artifact"]), "--identity", manifest.identity,
                 "--version", manifest.version),
                "youtube-runtime-fixture", environment={"PATH": "/usr/bin:/bin", "LANG": "C"},
            )
            await process.start()
            try:
                hello = await process.call("runtime.hello", {})
                process.negotiate_protocol(hello["protocol_version"])
                health = await process.call("runtime.health", {})
                self.assertEqual(hello["plugin"], manifest.identity)
                self.assertTrue(health["healthy"])
            finally:
                await process.call("runtime.shutdown", {})
                await process.stop()


if __name__ == "__main__":
    unittest.main()
