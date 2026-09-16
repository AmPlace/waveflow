from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest import IsolatedAsyncioTestCase, mock

import main


class _AliveProcess:
    def __init__(self):
        self.terminated = False
        self.killed = False
        self.returncode = None
        self.stderr = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self):
        return self.returncode


class RtspHlsStalledWatchdogTest(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._old_sessions = main.RTSP_HLS_SESSIONS
        self._old_startups = main._RTSP_HLS_STARTUPS
        self._old_reserved = main._RTSP_RESERVED_SESSIONS
        self._old_quota_lock = main._RTSP_HLS_QUOTA_LOCK
        self._old_shutting_down = main._RTSP_HLS_SHUTTING_DOWN
        self._old_root = main.RTSP_HLS_ROOT
        self._old_cleanup_task = main._RTSP_HLS_CLEANUP_TASK
        self._old_cleanup_stop = main._RTSP_HLS_CLEANUP_STOP
        self._tmp = tempfile.TemporaryDirectory()
        main.RTSP_HLS_SESSIONS = {}
        main._RTSP_HLS_STARTUPS = {}
        main._RTSP_RESERVED_SESSIONS = set()
        main._RTSP_HLS_QUOTA_LOCK = asyncio.Lock()
        main._RTSP_HLS_SHUTTING_DOWN = False
        main.RTSP_HLS_ROOT = Path(self._tmp.name)
        main._RTSP_HLS_CLEANUP_TASK = None
        main._RTSP_HLS_CLEANUP_STOP = None

    async def asyncTearDown(self):
        await main._stop_all_rtsp_sessions()
        self.assertEqual(main.RTSP_HLS_SESSIONS, {})
        self.assertEqual(main._RTSP_HLS_STARTUPS, {})
        self.assertEqual(main._RTSP_RESERVED_SESSIONS, set())
        main.RTSP_HLS_SESSIONS = self._old_sessions
        main._RTSP_HLS_STARTUPS = self._old_startups
        main._RTSP_RESERVED_SESSIONS = self._old_reserved
        main._RTSP_HLS_QUOTA_LOCK = self._old_quota_lock
        main._RTSP_HLS_SHUTTING_DOWN = self._old_shutting_down
        main.RTSP_HLS_ROOT = self._old_root
        main._RTSP_HLS_CLEANUP_TASK = self._old_cleanup_task
        main._RTSP_HLS_CLEANUP_STOP = self._old_cleanup_stop
        self._tmp.cleanup()

    def _write_playlist(
        self,
        session_dir: Path,
        names: list[str],
        *,
        target_duration: float = 2,
        durations: list[float] | None = None,
        media_sequence: int = 0,
        malformed: bool = False,
    ) -> None:
        if malformed:
            (session_dir / "index.m3u8").write_text(
                "#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXTINF:2,\n",
                encoding="utf-8",
            )
            return
        durations = durations or [target_duration] * len(names)
        lines = [
            "#EXTM3U",
            "#EXT-X-VERSION:3",
            f"#EXT-X-TARGETDURATION:{target_duration:g}",
            f"#EXT-X-MEDIA-SEQUENCE:{media_sequence}",
            "#EXT-X-DISCONTINUITY",
        ]
        for name, duration in zip(names, durations):
            lines.extend([f"#EXTINF:{duration:.3f},", name])
            (session_dir / name).touch()
        (session_dir / "index.m3u8").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _register_ready(
        self,
        name: str,
        *,
        target_duration: float = 2,
        segment_count: int = 2,
    ) -> tuple[str, dict, _AliveProcess]:
        target = f"rtsp://{name}.local/live"
        session_id = main._rtsp_session_id(target)
        session_dir = main.RTSP_HLS_ROOT / session_id
        session_dir.mkdir(parents=True)
        names = [f"seg_{index:05d}.ts" for index in range(segment_count)]
        process = _AliveProcess()
        self._write_playlist(
            session_dir,
            names,
            target_duration=target_duration,
            durations=[target_duration] * len(names),
        )
        session = {
            "process": process,
            "dir": session_dir,
            "last_access": time.time(),
            "startup_state": "ready",
        }
        main._initialize_rtsp_hls_progress(session)
        main.RTSP_HLS_SESSIONS[session_id] = session
        return session_id, session, process

    async def test_short_gop_progress_is_detected_even_when_media_sequence_stays_zero(self):
        session_id, session, process = self._register_ready("progress-short")
        session["last_progress_at"] = time.monotonic() - main.RTSP_HLS_STALL_MIN_SECONDS - 1
        previous_progress_at = session["last_progress_at"]
        session_dir = Path(session["dir"])
        self._write_playlist(session_dir, ["seg_00000.ts", "seg_00001.ts", "seg_00002.ts"])

        await main._rtsp_hls_cleanup_once()

        self.assertIn(session_id, main.RTSP_HLS_SESSIONS)
        self.assertFalse(process.terminated)
        self.assertEqual(session["last_segment_name"], "seg_00002.ts")
        self.assertGreater(session["last_progress_at"], previous_progress_at)

    async def test_same_segment_growth_counts_as_auxiliary_progress(self):
        session_id, session, process = self._register_ready("progress-size")
        session["last_progress_at"] = time.monotonic() - main.RTSP_HLS_STALL_MIN_SECONDS - 1
        segment = Path(session["dir"]) / "seg_00001.ts"
        segment.write_bytes(b"more media bytes")

        await main._rtsp_hls_cleanup_once()

        self.assertIn(session_id, main.RTSP_HLS_SESSIONS)
        self.assertFalse(process.terminated)

    async def test_stalled_ready_session_is_stopped_and_directory_removed(self):
        session_id, session, process = self._register_ready("stalled-short")
        session["last_progress_at"] = time.monotonic() - main.RTSP_HLS_STALL_MIN_SECONDS - 1
        session_dir = Path(session["dir"])

        await main._rtsp_hls_cleanup_once()

        self.assertNotIn(session_id, main.RTSP_HLS_SESSIONS)
        self.assertTrue(process.terminated)
        self.assertFalse(session_dir.exists())
        await main._rtsp_hls_cleanup_once()
        self.assertFalse(process.killed)

    async def test_long_gop_uses_adaptive_timeout_without_false_positive(self):
        session_id, session, process = self._register_ready("long-gop", target_duration=8)
        self.assertEqual(main._rtsp_hls_stall_timeout(session), 32.0)
        session["last_progress_at"] = time.monotonic() - 31

        await main._rtsp_hls_cleanup_once()

        self.assertIn(session_id, main.RTSP_HLS_SESSIONS)
        self.assertFalse(process.terminated)

        session["last_progress_at"] = time.monotonic() - 33
        await main._rtsp_hls_cleanup_once()

        self.assertNotIn(session_id, main.RTSP_HLS_SESSIONS)
        self.assertTrue(process.terminated)

    async def test_one_incomplete_playlist_read_is_not_an_immediate_kill(self):
        session_id, session, process = self._register_ready("transient-parse")
        session["last_progress_at"] = time.monotonic() - main.RTSP_HLS_STALL_MIN_SECONDS - 1
        self._write_playlist(Path(session["dir"]), [], malformed=True)

        await main._rtsp_hls_cleanup_once()

        self.assertIn(session_id, main.RTSP_HLS_SESSIONS)
        self.assertFalse(process.terminated)
        self.assertEqual(session["progress_read_failures"], 1)

    async def test_repeated_incomplete_playlist_eventually_cleans_stalled_session(self):
        session_id, session, process = self._register_ready("persistent-parse")
        session["last_progress_at"] = time.monotonic() - main.RTSP_HLS_STALL_MIN_SECONDS - 1
        self._write_playlist(Path(session["dir"]), [], malformed=True)

        await main._rtsp_hls_cleanup_once()
        await main._rtsp_hls_cleanup_once()

        self.assertNotIn(session_id, main.RTSP_HLS_SESSIONS)
        self.assertTrue(process.terminated)

    async def test_starting_session_is_left_to_startup_timeout(self):
        session_id = "a" * 24
        process = _AliveProcess()
        main.RTSP_HLS_SESSIONS[session_id] = {
            "process": process,
            "dir": main.RTSP_HLS_ROOT / session_id,
            "last_access": 0,
            "last_progress_at": 0,
            "startup_state": "starting",
        }

        await main._rtsp_hls_cleanup_once()

        self.assertIn(session_id, main.RTSP_HLS_SESSIONS)
        self.assertFalse(process.terminated)

    async def test_idle_cleanup_still_wins_over_progress_watchdog(self):
        session_id, session, process = self._register_ready("idle-session")
        session["last_access"] = time.time() - main.RTSP_HLS_IDLE_TTL - 1

        await main._rtsp_hls_cleanup_once()

        self.assertNotIn(session_id, main.RTSP_HLS_SESSIONS)
        self.assertTrue(process.terminated)

    async def test_exited_process_is_cleaned_without_waiting_for_stall_timeout(self):
        session_id, session, process = self._register_ready("exited-process")
        process.returncode = 1

        await main._rtsp_hls_cleanup_once()

        self.assertNotIn(session_id, main.RTSP_HLS_SESSIONS)
        self.assertFalse(process.terminated)
        self.assertFalse((Path(session["dir"])).exists())

    async def test_progress_just_before_stall_boundary_prevents_cleanup(self):
        session_id, session, process = self._register_ready("boundary-progress")
        session["last_progress_at"] = time.monotonic() - main.RTSP_HLS_STALL_MIN_SECONDS - 1
        session_dir = Path(session["dir"])
        self._write_playlist(session_dir, ["seg_00000.ts", "seg_00001.ts", "seg_00002.ts"])

        await main._rtsp_hls_cleanup_once()

        self.assertIn(session_id, main.RTSP_HLS_SESSIONS)
        self.assertFalse(process.terminated)

    async def test_cleanup_task_stops_without_leaving_a_pending_task(self):
        stop_event = asyncio.Event()
        task = asyncio.create_task(main._rtsp_hls_cleanup_task(stop_event))
        await asyncio.sleep(0)
        stop_event.set()

        await asyncio.wait_for(task, timeout=1)

        self.assertTrue(task.done())

    async def test_late_stall_cleanup_cannot_remove_a_replacement_session(self):
        session_id, old_session, old_process = self._register_ready("retry-identity")
        old_session["last_progress_at"] = time.monotonic() - main.RTSP_HLS_STALL_MIN_SECONDS - 1
        replacement_process = _AliveProcess()
        replacement = dict(old_session)
        replacement["process"] = replacement_process
        calls = 0
        original_refresh = main._refresh_rtsp_hls_progress

        def refresh(session, now=None):
            nonlocal calls
            calls += 1
            if calls == 2:
                main.RTSP_HLS_SESSIONS[session_id] = replacement
                return False
            return original_refresh(session, now=now)

        with mock.patch.object(main, "_refresh_rtsp_hls_progress", side_effect=refresh):
            await main._rtsp_hls_cleanup_once()

        self.assertIs(main.RTSP_HLS_SESSIONS[session_id], replacement)
        self.assertFalse(old_process.terminated)
        self.assertFalse(replacement_process.terminated)
