from __future__ import annotations

import ipaddress
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


os.environ.setdefault("WAVEFLOW_MODE", "nas")
os.environ.setdefault("WAVEFLOW_DB_PATH", ":memory:")
os.environ.setdefault("WAVEFLOW_PROXY_HANDLE_SECRET", "rtsp-policy-test-secret-32-bytes")

import main
from fastapi import HTTPException
from routers import media_proxy
from security.dependencies import MediaAccessContext
from security.proxy_handles import issue_handle
from ssrf_guard import UnsafeTargetError, assert_safe_host_ips


def _rtsp_channel(url: str = "rtsp://camera.local/live") -> dict:
    return {
        "canonical_key": "camera",
        "urls": [{
            "url": url,
            "source_type": "rtsp",
            "enabled": True,
            "is_working": 1,
        }],
    }


class RtspPolicyTest(unittest.IsolatedAsyncioTestCase):
    async def test_shared_rtsp_disabled_does_not_create_session(self):
        old_channels = main._get_aggregated_iptv_channels
        main._get_aggregated_iptv_channels = mock.AsyncMock(
            return_value=([_rtsp_channel()], [])
        )
        try:
            with mock.patch.object(main, "config_rtsp_proxy_enabled", return_value=False), \
                    mock.patch.object(main, "_ensure_rtsp_hls_session", new=mock.AsyncMock()) as ensure:
                with self.assertRaises(HTTPException) as ctx:
                    await main.serve_rtsp_playlist_response(upstream_url="rtsp://camera.local/live")
                self.assertEqual(ctx.exception.status_code, 503)
                ensure.assert_not_awaited()
        finally:
            main._get_aggregated_iptv_channels = old_channels

    async def test_signed_rtsp_handle_disabled_does_not_create_session(self):
        handle = issue_handle(kind="rtsp", url="rtsp://camera.local/live")
        with mock.patch.object(main, "config_rtsp_proxy_enabled", return_value=False), \
                mock.patch.object(main, "_ensure_rtsp_hls_session", new=mock.AsyncMock()) as ensure:
            with mock.patch.dict(sys.modules, {"main": main}):
                with self.assertRaises(HTTPException) as ctx:
                    await media_proxy.media_proxy_rtsp(
                        handle,
                        MediaAccessContext(source="anonymous"),
                    )
            self.assertEqual(ctx.exception.status_code, 503)
            ensure.assert_not_awaited()

    async def test_shared_and_handle_paths_share_final_rtsp_boundary(self):
        playlist = Path("/tmp/waveflow-rtsp-policy-test.m3u8")
        old_sessions = main.RTSP_HLS_SESSIONS
        main.RTSP_HLS_SESSIONS = {}
        try:
            with mock.patch.object(
                        main,
                        "_get_aggregated_iptv_channels",
                        new=mock.AsyncMock(return_value=([_rtsp_channel()], [])),
                    ), \
                    mock.patch.object(main, "config_rtsp_proxy_enabled", return_value=True), \
                    mock.patch.object(main, "assert_safe_host_ips") as safe_host, \
                    mock.patch.object(main, "_ensure_rtsp_hls_session", new=mock.AsyncMock(
                        return_value=("a" * 24, playlist)
                    )) as ensure:
                await main.serve_rtsp_playlist_response(upstream_url="rtsp://camera.local/live")

                handle = issue_handle(kind="rtsp", url="rtsp://camera.local/live")
                with mock.patch.dict(sys.modules, {"main": main}):
                    await media_proxy.media_proxy_rtsp(
                        handle,
                        MediaAccessContext(source="anonymous"),
                    )

                self.assertEqual(safe_host.call_count, 2)
                self.assertEqual(ensure.await_count, 2)
                self.assertEqual(
                    [call.args[0] for call in safe_host.call_args_list],
                    ["camera.local", "camera.local"],
                )
        finally:
            main.RTSP_HLS_SESSIONS = old_sessions

    async def test_private_rtsp_allowed_enters_session_creation(self):
        playlist = Path("/tmp/waveflow-rtsp-policy-private.m3u8")
        old_sessions = main.RTSP_HLS_SESSIONS
        main.RTSP_HLS_SESSIONS = {}
        try:
            with mock.patch.object(main, "config_rtsp_proxy_enabled", return_value=True), \
                    mock.patch.object(main, "assert_safe_host_ips") as safe_host, \
                    mock.patch.object(main, "_ensure_rtsp_hls_session", new=mock.AsyncMock(
                        return_value=("b" * 24, playlist)
                    )) as ensure:
                await main.serve_rtsp_playlist_response(
                    upstream_url="rtsp://192.168.1.20/live",
                )
                safe_host.assert_called_once_with("192.168.1.20")
                ensure.assert_awaited_once()
        finally:
            main.RTSP_HLS_SESSIONS = old_sessions

    async def test_rtsp_policy_rejects_private_loopback_and_hard_block(self):
        cases = (
            ("rtsp://192.168.1.20/live", "private"),
            ("rtsp://127.0.0.1:8554/live", "loopback"),
            ("rtsp://169.254.169.254/live", "hard-block"),
        )
        for target, label in cases:
            with self.subTest(label=label), \
                    mock.patch.object(main, "config_rtsp_proxy_enabled", return_value=True), \
                    mock.patch.object(
                        main,
                        "assert_safe_host_ips",
                        side_effect=UnsafeTargetError(f"blocked {label}"),
                    ), \
                    mock.patch.object(main, "_ensure_rtsp_hls_session", new=mock.AsyncMock()) as ensure:
                with self.assertRaises(HTTPException) as ctx:
                    await main.serve_rtsp_playlist_response(upstream_url=target)
                self.assertEqual(ctx.exception.status_code, 403)
                ensure.assert_not_awaited()


class ExistingRtspHostPolicyTest(unittest.TestCase):
    def _assert_host_policy(self, host: str, *, allow_private: bool, allow_loopback: bool, ips: list[str]):
        with mock.patch(
            "ssrf_guard.get_effective_settings_sync",
            return_value=SimpleNamespace(
                allow_private=allow_private,
                allow_loopback=allow_loopback,
            ),
        ), mock.patch(
            "ssrf_guard.resolve_target_ips_sync",
            return_value=[ipaddress.ip_address(value) for value in ips],
        ):
            assert_safe_host_ips(host)

    def test_private_address_allowed_when_private_policy_enabled(self):
        self._assert_host_policy(
            "camera.local",
            allow_private=True,
            allow_loopback=False,
            ips=["192.168.1.20"],
        )

    def test_private_address_denied_when_private_policy_disabled(self):
        with self.assertRaises(UnsafeTargetError):
            self._assert_host_policy(
                "camera.local",
                allow_private=False,
                allow_loopback=False,
                ips=["192.168.1.20"],
            )

    def test_loopback_follows_loopback_policy(self):
        with self.assertRaises(UnsafeTargetError):
            self._assert_host_policy(
                "camera.local",
                allow_private=True,
                allow_loopback=False,
                ips=["127.0.0.1"],
            )
        self._assert_host_policy(
            "camera.local",
            allow_private=True,
            allow_loopback=True,
            ips=["127.0.0.1"],
        )

    def test_hard_block_remains_denied_when_allow_flags_enabled(self):
        with self.assertRaises(UnsafeTargetError):
            self._assert_host_policy(
                "169.254.169.254",
                allow_private=True,
                allow_loopback=True,
                ips=["169.254.169.254"],
            )


if __name__ == "__main__":
    unittest.main()
