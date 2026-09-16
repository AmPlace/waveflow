from __future__ import annotations

import base64
import json
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


os.environ.setdefault("WAVEFLOW_MODE", "nas")
os.environ.setdefault("WAVEFLOW_DB_PATH", ":memory:")
os.environ.setdefault(
    "WAVEFLOW_PROXY_HANDLE_SECRET",
    "rtsp-source-playback-test-secret-32-bytes",
)

import main
from routers import media_proxy
from rtsp_playback import (
    RtspTimestampMode,
    RtspVideoMode,
    resolve_rtsp_playback_options,
)
from security.dependencies import MediaAccessContext
from security.proxy_handles import (
    HandleSignatureError,
    decode_for_kind,
    issue_handle,
)
from security.source_ids import source_id_for, source_revision_for


class RtspPlaybackOptionsTest(unittest.TestCase):
    def test_defaults_configured_invalid_and_transcode_normalization(self):
        default = resolve_rtsp_playback_options()
        configured = resolve_rtsp_playback_options(
            {"rtsp_timestamp_mode": "pts_from_dts"}
        )
        invalid = resolve_rtsp_playback_options(
            {"rtsp_timestamp_mode": "-vf evil"}
        )
        transcode = resolve_rtsp_playback_options(
            {"rtsp_timestamp_mode": "pts_from_dts"}, compat=True,
        )

        self.assertEqual(default.video_mode, RtspVideoMode.COPY)
        self.assertEqual(default.timestamp_mode, RtspTimestampMode.PASSTHROUGH)
        self.assertEqual(configured.timestamp_mode, RtspTimestampMode.PTS_FROM_DTS)
        self.assertEqual(invalid.timestamp_mode, RtspTimestampMode.PASSTHROUGH)
        self.assertEqual(transcode.video_mode, RtspVideoMode.TRANSCODE)
        self.assertEqual(transcode.timestamp_mode, RtspTimestampMode.PASSTHROUGH)

    def test_source_revision_changes_but_source_identity_does_not(self):
        base = {
            "id": 10,
            "subscription_id": 2,
            "url": "rtsp://camera.example/live",
            "source_type": "rtsp",
        }
        explicit_default = {**base, "rtsp_timestamp_mode": "passthrough"}
        configured = {**base, "rtsp_timestamp_mode": "pts_from_dts"}

        self.assertEqual(source_id_for(base), source_id_for(configured))
        self.assertEqual(source_revision_for(base), source_revision_for(explicit_default))
        self.assertNotEqual(source_revision_for(base), source_revision_for(configured))

    def test_ffmpeg_command_only_adds_setts_for_copy_pts_from_dts(self):
        def command(options):
            return main._build_rtsp_ffmpeg_command(
                ffmpeg="/usr/bin/ffmpeg",
                target_url="rtsp://camera.example/live",
                custom_ua="UA",
                session_id="a" * 24,
                session_dir=Path("/tmp/rtsp-source-playback"),
                playback_options=options,
            )

        passthrough = command(resolve_rtsp_playback_options())
        pts_from_dts = command(resolve_rtsp_playback_options(
            {"rtsp_timestamp_mode": "pts_from_dts"}
        ))
        transcode = command(resolve_rtsp_playback_options(
            {"rtsp_timestamp_mode": "pts_from_dts"}, compat=True,
        ))

        self.assertNotIn("-bsf:v", passthrough)
        self.assertIn("-bsf:v", pts_from_dts)
        bsf_index = pts_from_dts.index("-bsf:v")
        self.assertEqual(pts_from_dts[bsf_index + 1], "setts=pts=DTS")
        self.assertIn("-c:a", pts_from_dts)
        self.assertNotIn("-bsf:v", transcode)
        self.assertIn("libx264", transcode)

    def test_session_identity_includes_video_and_timestamp_modes(self):
        target = "rtsp://camera.example/live"
        default = main._rtsp_session_id(target)
        dts = main._rtsp_session_id(
            target,
            playback_options=resolve_rtsp_playback_options(
                {"rtsp_timestamp_mode": "pts_from_dts"}
            ),
        )
        transcode = main._rtsp_session_id(target, compat=True)
        self.assertEqual(len({default, dts, transcode}), 3)


class RtspSignedHandleTest(unittest.TestCase):
    def test_timestamp_mode_roundtrip_old_default_and_invalid_issue(self):
        configured = issue_handle(
            kind="rtsp",
            url="rtsp://camera.example/live",
            timestamp_mode="pts_from_dts",
        )
        old_shape = issue_handle(
            kind="rtsp",
            url="rtsp://camera.example/live",
        )
        self.assertEqual(
            decode_for_kind(configured, "rtsp").timestamp_mode,
            "pts_from_dts",
        )
        self.assertEqual(
            decode_for_kind(old_shape, "rtsp").timestamp_mode,
            "passthrough",
        )
        with self.assertRaises(ValueError):
            issue_handle(
                kind="rtsp",
                url="rtsp://camera.example/live",
                timestamp_mode="-vf evil",
            )

    def test_timestamp_payload_tamper_fails_signature(self):
        handle = issue_handle(
            kind="rtsp",
            url="rtsp://camera.example/live",
            timestamp_mode="pts_from_dts",
        )
        body_part, signature_part = handle.split(".", 1)
        payload = json.loads(base64.urlsafe_b64decode(
            body_part + "=" * (-len(body_part) % 4)
        ))
        payload["timestamp_mode"] = "passthrough"
        tampered_body = base64.urlsafe_b64encode(
            json.dumps(payload, separators=(",", ":")).encode()
        ).rstrip(b"=").decode()
        with self.assertRaises(HandleSignatureError):
            decode_for_kind(f"{tampered_body}.{signature_part}", "rtsp")


class RtspRoutePropagationTest(unittest.IsolatedAsyncioTestCase):
    async def test_formal_source_signs_timestamp_mode(self):
        import main as current_main

        source = {
            "id": 1,
            "subscription_id": 2,
            "url": "rtsp://camera.example/live",
            "source_type": "rtsp",
            "rtsp_timestamp_mode": "pts_from_dts",
        }
        with mock.patch.object(current_main, "config_rtsp_proxy_enabled", return_value=True):
            response = await media_proxy._serve_iptv_source_playlist(
                source,
                "camera",
                MediaAccessContext(source="anonymous"),
            )
        handle = response.headers["location"].split(
            "/api/media/proxy/rtsp/", 1
        )[1]
        self.assertEqual(
            decode_for_kind(handle, "rtsp").timestamp_mode,
            "pts_from_dts",
        )

    async def test_signed_route_restores_playback_options(self):
        handle = issue_handle(
            kind="rtsp",
            url="rtsp://camera.example/live",
            timestamp_mode="pts_from_dts",
        )
        runtime_main = sys.modules["main"]
        with mock.patch.object(
            runtime_main,
            "serve_rtsp_playlist_response",
            new=mock.AsyncMock(return_value=SimpleNamespace(status_code=200)),
        ) as serve:
            await media_proxy.media_proxy_rtsp(
                handle,
                MediaAccessContext(source="anonymous"),
            )
        options = serve.await_args.kwargs["playback_options"]
        self.assertEqual(options.video_mode, RtspVideoMode.COPY)
        self.assertEqual(options.timestamp_mode, RtspTimestampMode.PTS_FROM_DTS)

    async def test_smart_rtsp_redirect_preserves_source_and_credential(self):
        from tests.test_subscription_export import _request
        source = {
            "url": "rtsp://camera.example/live",
            "source_type": "rtsp",
            "enabled": True,
            "is_working": 1,
            "rtsp_timestamp_mode": "pts_from_dts",
            "source_id": "camera-source",
        }
        channel = {"canonical_key": "camera", "urls": [source]}
        with mock.patch.object(
            main,
            "_get_aggregated_iptv_channels",
            new=mock.AsyncMock(return_value=([channel], [])),
        ), mock.patch.object(
            main,
            "serve_rtsp_playlist_response",
            new=mock.AsyncMock(return_value=SimpleNamespace(status_code=200)),
        ) as serve:
            response = await main.iptv_smart_playlist(
                "camera", _request(), MediaAccessContext(source="credential", propagated_access_token="fixture"),
            )
        serve.assert_not_awaited()
        self.assertEqual(response.status_code, 307)
        self.assertIn("source_id=camera-source", response.headers["location"])
        self.assertIn("access_token=fixture", response.headers["location"])


if __name__ == "__main__":
    unittest.main()
