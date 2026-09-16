import asyncio
import json
import sys
import types
import unittest
from unittest import IsolatedAsyncioTestCase, mock

import httpx

import iptv_probe
from routers import media_proxy


class _CompletedProcess:
    def __init__(self, *, returncode=0, stdout=b"", stderr=b""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.killed = False

    async def communicate(self):
        return self.stdout, self.stderr

    def kill(self):
        self.killed = True
        self.returncode = -9

    async def wait(self):
        return self.returncode


class _TimeoutProcess(_CompletedProcess):
    def __init__(self):
        super().__init__(returncode=None)

    async def communicate(self):
        raise asyncio.TimeoutError


class _HangingStderr:
    async def readline(self):
        await asyncio.Event().wait()


class _CancellableFfmpegProcess(_CompletedProcess):
    def __init__(self):
        super().__init__(returncode=None)
        self.stderr = _HangingStderr()


class ProbeMediaToolClassificationTest(IsolatedAsyncioTestCase):
    async def test_probe_diagnostics_redact_final_url_query(self):
        result = iptv_probe._empty_result(
            probe_status="online",
            live_status="live",
            probe_method="http_stream",
            speed_mbps=1.0,
            probe_meta_json=json.dumps({
                "safe_final_url": "https://public.example/live.ts?token=secret",
            }),
        )
        with mock.patch.object(
            iptv_probe,
            "_probe_http_stream",
            new=mock.AsyncMock(return_value=result),
        ), mock.patch.object(
            iptv_probe,
            "_enrich_with_ffprobe",
            new=mock.AsyncMock(side_effect=lambda value, *_: value),
        ):
            observed = await iptv_probe.probe_channel_source(
                {"url": "https://public.example/live.ts", "source_type": "http"},
                None,
            )

        metadata = json.loads(observed["probe_meta_json"])
        self.assertEqual(
            metadata["safe_final_url"],
            "https://public.example/live.ts?token=%5Bredacted%5D",
        )
        self.assertNotIn("secret", observed["probe_meta_json"])

    async def test_admin_probe_redacts_final_url_query(self):
        class Response:
            status_code = 200
            url = httpx.URL("https://public.example/final?token=secret")
            headers = {"content-type": "text/plain", "content-length": "2"}
            content = b"ok"

            async def aclose(self):
                return None

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
                new=mock.AsyncMock(return_value=Response()),
            ):
                observed = await media_proxy.admin_probe_url(
                    {"url": "https://public.example/start"}, {}
                )
        finally:
            if old_main is not None:
                sys.modules["main"] = old_main
            else:
                sys.modules.pop("main", None)

        self.assertEqual(
            observed["final_url"],
            "https://public.example/final?token=%5Bredacted%5D",
        )

    async def test_http_success_with_unavailable_ffprobe_stays_online(self):
        result = iptv_probe._empty_result(
            probe_status="online",
            live_status="live",
            probe_method="http_stream",
            speed_mbps=1.0,
        )
        with mock.patch.object(
            iptv_probe,
            "_probe_media_info",
            new=mock.AsyncMock(return_value={
                "meta": {"ffprobe": False, "ffprobe_error": "ffprobe_not_found"}
            }),
        ):
            enriched = await iptv_probe._enrich_with_ffprobe(
                result, "https://public.example/live.ts", {}
            )

        self.assertEqual(enriched["probe_status"], "online")
        meta = json.loads(enriched["probe_meta_json"])
        self.assertEqual(meta["probe_quality"], "http_only")
        self.assertEqual(meta["ffprobe_error"], "ffprobe_not_found")

    async def test_http_timeout_remains_timeout_when_ffmpeg_is_unavailable(self):
        fallback = iptv_probe._empty_result(
            probe_status="unsupported",
            probe_method="ffmpeg",
            last_error="ffmpeg_not_found",
        )
        with mock.patch.object(
            iptv_probe,
            "_probe_http_stream",
            new=mock.AsyncMock(side_effect=httpx.ReadTimeout("slow origin")),
        ), mock.patch.object(
            iptv_probe,
            "_probe_with_ffmpeg",
            new=mock.AsyncMock(return_value=fallback),
        ):
            result = await iptv_probe.probe_channel_source(
                {"url": "https://public.example/live.ts", "source_type": "http"},
                None,
            )

        self.assertEqual(result["probe_status"], "timeout")
        self.assertEqual(result["last_error"], "timeout")
        self.assertEqual(json.loads(result["probe_meta_json"])["http_error"], "timeout")
        self.assertEqual(
            json.loads(result["probe_meta_json"])["media_fallback_error"],
            "ffmpeg_not_found",
        )

    async def test_ffprobe_spawn_failure_is_stable_and_non_sensitive(self):
        with mock.patch.object(
            iptv_probe,
            "assert_safe_target_url",
            new=mock.AsyncMock(),
        ), mock.patch.object(
            iptv_probe, "media_tool_bin", return_value="/private/tool/ffprobe"
        ), mock.patch.object(
            iptv_probe.asyncio,
            "create_subprocess_exec",
            new=mock.AsyncMock(side_effect=PermissionError("/private/tool/ffprobe")),
        ):
            result = await iptv_probe._probe_media_info(
                "https://public.example/live.m3u8?token=secret", {}
            )

        meta = result["meta"]
        self.assertEqual(meta["ffprobe_error"], "ffprobe_spawn_failed")
        self.assertNotIn("/private/tool/ffprobe", json.dumps(result))
        self.assertNotIn("secret", json.dumps(result))

    async def test_ffprobe_nonzero_exit_does_not_persist_stderr_or_url(self):
        process = _CompletedProcess(
            returncode=1,
            stderr=b"failed to open https://public.example/live?token=secret",
        )
        with mock.patch.object(
            iptv_probe,
            "assert_safe_target_url",
            new=mock.AsyncMock(),
        ), mock.patch.object(
            iptv_probe, "media_tool_bin", return_value="/usr/bin/ffprobe"
        ), mock.patch.object(
            iptv_probe.asyncio,
            "create_subprocess_exec",
            new=mock.AsyncMock(return_value=process),
        ):
            result = await iptv_probe._probe_media_info(
                "https://public.example/live.m3u8?token=secret", {}
            )

        self.assertEqual(result["meta"]["ffprobe_error"], "ffprobe_failed")
        self.assertEqual(result["meta"]["ffprobe_exit_code"], 1)
        self.assertNotIn("secret", json.dumps(result))

    async def test_ffprobe_timeout_kills_and_reaps_child(self):
        process = _TimeoutProcess()
        with mock.patch.object(
            iptv_probe,
            "assert_safe_target_url",
            new=mock.AsyncMock(),
        ), mock.patch.object(
            iptv_probe, "media_tool_bin", return_value="/usr/bin/ffprobe"
        ), mock.patch.object(
            iptv_probe.asyncio,
            "create_subprocess_exec",
            new=mock.AsyncMock(return_value=process),
        ):
            result = await iptv_probe._probe_media_info(
                "https://public.example/live.m3u8", {}
            )

        self.assertEqual(result["meta"]["ffprobe_error"], "ffprobe_timeout")
        self.assertTrue(process.killed)

    async def test_ffmpeg_spawn_failure_is_stable(self):
        with mock.patch.object(
            iptv_probe,
            "assert_safe_target_url",
            new=mock.AsyncMock(),
        ), mock.patch.object(
            iptv_probe, "media_tool_bin", return_value="/private/tool/ffmpeg"
        ), mock.patch.object(
            iptv_probe.asyncio,
            "create_subprocess_exec",
            new=mock.AsyncMock(side_effect=PermissionError("/private/tool/ffmpeg")),
        ):
            result = await iptv_probe._probe_with_ffmpeg(
                "https://public.example/live.ts?token=secret", {}, reason="http_timeout"
            )

        self.assertEqual(result["last_error"], "ffmpeg_spawn_failed")
        self.assertNotIn("/private/tool/ffmpeg", json.dumps(result))
        self.assertNotIn("secret", json.dumps(result))

    async def test_ffmpeg_no_stream_does_not_persist_stderr_or_url(self):
        process = _CompletedProcess(
            returncode=1,
            stderr=b"Invalid data from https://public.example/live.ts?token=secret",
        )
        with mock.patch.object(
            iptv_probe,
            "assert_safe_target_url",
            new=mock.AsyncMock(),
        ), mock.patch.object(
            iptv_probe, "media_tool_bin", return_value="/usr/bin/ffmpeg"
        ), mock.patch.object(
            iptv_probe.asyncio,
            "create_subprocess_exec",
            new=mock.AsyncMock(return_value=process),
        ):
            result = await iptv_probe._probe_with_ffmpeg(
                "https://public.example/live.ts?token=secret", {}, reason="http_exception"
            )

        self.assertEqual(result["last_error"], "ffmpeg_no_stream")
        self.assertNotIn("secret", json.dumps(result))

    async def test_ffmpeg_cancellation_kills_child_process(self):
        process = _CancellableFfmpegProcess()
        spawned = asyncio.Event()

        async def spawn(*_args, **_kwargs):
            spawned.set()
            return process

        with mock.patch.object(
            iptv_probe,
            "assert_safe_target_url",
            new=mock.AsyncMock(),
        ), mock.patch.object(
            iptv_probe, "media_tool_bin", return_value="/usr/bin/ffmpeg"
        ), mock.patch.object(
            iptv_probe.asyncio,
            "create_subprocess_exec",
            new=spawn,
        ):
            task = asyncio.create_task(iptv_probe._probe_with_ffmpeg(
                "https://public.example/live.ts", {}, reason="http_timeout"
            ))
            await spawned.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertTrue(process.killed)


if __name__ == "__main__":
    unittest.main()
