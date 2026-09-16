from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import IsolatedAsyncioTestCase, mock

from fastapi import HTTPException

import database
import main
from rtsp_playback import resolve_rtsp_playback_options


class _AliveProcess:
    def __init__(self):
        self.terminated = False
        self.killed = False
        self.stderr = None
        self.returncode = None

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


class _EofPipe:
    def __init__(self, payload: bytes = b""):
        self.payload = payload
        self.closed = False

    def read(self, _size):
        if self.payload:
            payload, self.payload = self.payload, b""
            return payload
        return b""

    def close(self):
        self.closed = True


class _StubbornProcess(_AliveProcess):
    def __init__(self):
        super().__init__()
        self.wait_calls = 0

    def terminate(self):
        self.terminated = True

    def wait(self):
        self.wait_calls += 1
        if self.wait_calls == 1:
            raise subprocess.TimeoutExpired("ffmpeg", 5)
        return self.returncode


class RtspStartupSingleFlightTest(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._old_sessions = main.RTSP_HLS_SESSIONS
        self._old_startups = main._RTSP_HLS_STARTUPS
        self._old_reserved = main._RTSP_RESERVED_SESSIONS
        self._old_quota_lock = main._RTSP_HLS_QUOTA_LOCK
        self._old_shutting_down = main._RTSP_HLS_SHUTTING_DOWN
        self._old_root = main.RTSP_HLS_ROOT
        self._old_db_path = os.environ.get("WAVEFLOW_DB_PATH")
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["WAVEFLOW_DB_PATH"] = str(Path(self._tmp.name) / "waveflow.db")
        await database.initialize()
        main.RTSP_HLS_SESSIONS = {}
        main._RTSP_HLS_STARTUPS = {}
        main._RTSP_RESERVED_SESSIONS = set()
        main._RTSP_HLS_QUOTA_LOCK = asyncio.Lock()
        main._RTSP_HLS_SHUTTING_DOWN = False
        main.RTSP_HLS_ROOT = Path(self._tmp.name)

    async def asyncTearDown(self):
        pending = list(main._RTSP_HLS_STARTUPS.values())
        for task in pending:
            if not task.done():
                task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        await main._stop_all_rtsp_sessions()
        self.assertEqual(main._RTSP_HLS_STARTUPS, {})
        self.assertEqual(main._RTSP_RESERVED_SESSIONS, set())
        self.assertEqual(main.RTSP_HLS_SESSIONS, {})
        main._RTSP_HLS_STARTUPS.clear()
        main._RTSP_RESERVED_SESSIONS.clear()
        main._RTSP_HLS_SHUTTING_DOWN = self._old_shutting_down
        main.RTSP_HLS_SESSIONS = self._old_sessions
        main._RTSP_HLS_STARTUPS = self._old_startups
        main._RTSP_RESERVED_SESSIONS = self._old_reserved
        main._RTSP_HLS_QUOTA_LOCK = self._old_quota_lock
        main.RTSP_HLS_ROOT = self._old_root
        if self._old_db_path is None:
            os.environ.pop("WAVEFLOW_DB_PATH", None)
        else:
            os.environ["WAVEFLOW_DB_PATH"] = self._old_db_path
        self._tmp.cleanup()

    async def _wait_for_no_startup(self):
        for _ in range(20):
            if not main._RTSP_HLS_STARTUPS:
                return
            await asyncio.sleep(0)
        self.fail("RTSP startup entry was not cleaned up")

    async def _cancel_real_startup_after_popen(self, segment_count: int):
        target = "rtsp://camera-cancel.local/live"
        session_id = main._rtsp_session_id(target)
        popen_started = asyncio.Event()
        process = _AliveProcess()
        process.stderr = _EofPipe(b"startup stderr")

        def popen(*_args, **_kwargs):
            session_dir = main.RTSP_HLS_ROOT / session_id
            (session_dir / "index.m3u8").write_text("#EXTM3U\n", encoding="utf-8") if segment_count else None
            for index in range(segment_count):
                (session_dir / f"seg_{index:05d}.ts").write_bytes(b"segment")
            popen_started.set()
            return process

        with mock.patch.object(main, "_ffmpeg_bin", return_value="/usr/bin/ffmpeg"), mock.patch.object(
            main.subprocess, "Popen", side_effect=popen,
        ):
            owner = asyncio.create_task(main._ensure_rtsp_hls_session(target))
            await asyncio.wait_for(popen_started.wait(), timeout=1)
            old_session = main.RTSP_HLS_SESSIONS[session_id]
            startup = main._RTSP_HLS_STARTUPS[session_id]
            startup.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await owner
            await self._wait_for_no_startup()

        self.assertTrue(process.terminated)
        self.assertFalse(old_session["stderr_thread"].is_alive())
        self.assertFalse(main.RTSP_HLS_SESSIONS)
        self.assertFalse(main._RTSP_RESERVED_SESSIONS)
        self.assertFalse((main.RTSP_HLS_ROOT / session_id).exists())
        return process

    async def test_ten_same_key_waiters_share_one_startup(self):
        started = asyncio.Event()
        release = asyncio.Event()
        calls = []

        async def start(target_url, custom_ua, compat):
            calls.append((target_url, custom_ua, compat))
            started.set()
            await release.wait()
            return "a" * 24, Path("/tmp/shared-index.m3u8")

        with mock.patch.object(main, "_start_rtsp_hls_session", new=start):
            waiters = [
                asyncio.create_task(main._ensure_rtsp_hls_session("rtsp://camera.local/live"))
                for _ in range(10)
            ]
            await started.wait()
            await asyncio.sleep(0)
            self.assertEqual(len(calls), 1)
            release.set()
            results = await asyncio.gather(*waiters)

        self.assertEqual(results, [("a" * 24, Path("/tmp/shared-index.m3u8"))] * 10)
        await self._wait_for_no_startup()

    async def test_actual_ffmpeg_startup_path_calls_popen_once(self):
        session_id = main._rtsp_session_id("rtsp://camera.local/live")
        popen_calls = 0

        def popen(*_args, **_kwargs):
            nonlocal popen_calls
            popen_calls += 1
            session_dir = main.RTSP_HLS_ROOT / session_id
            (session_dir / "index.m3u8").write_text("#EXTM3U\n", encoding="utf-8")
            for index in range(main.RTSP_HLS_START_SEGMENTS):
                (session_dir / f"seg_{index:05d}.ts").write_bytes(b"segment")
            return _AliveProcess()

        with mock.patch.object(main, "_ffmpeg_bin", return_value="/usr/bin/ffmpeg"), mock.patch.object(
            main.subprocess,
            "Popen",
            side_effect=popen,
        ):
            results = await asyncio.gather(*[
                main._ensure_rtsp_hls_session("rtsp://camera.local/live")
                for _ in range(3)
            ])

        self.assertEqual(popen_calls, 1)
        self.assertEqual(results, [results[0]] * 3)
        self.assertEqual(main.RTSP_HLS_SESSIONS[session_id]["startup_state"], "ready")
        await self._wait_for_no_startup()

    async def test_different_keys_start_in_parallel(self):
        started_keys = set()
        both_started = asyncio.Event()
        release = asyncio.Event()

        async def start(target_url, custom_ua, compat):
            started_keys.add(target_url)
            if len(started_keys) == 2:
                both_started.set()
            await release.wait()
            return main._rtsp_session_id(target_url, custom_ua, compat), Path("/tmp/index.m3u8")

        with mock.patch.object(main, "_start_rtsp_hls_session", new=start):
            first = asyncio.create_task(main._ensure_rtsp_hls_session("rtsp://camera-a.local/live"))
            second = asyncio.create_task(main._ensure_rtsp_hls_session("rtsp://camera-b.local/live"))
            await asyncio.wait_for(both_started.wait(), timeout=1)
            release.set()
            results = await asyncio.gather(first, second)

        self.assertNotEqual(results[0][0], results[1][0])
        self.assertEqual(len(started_keys), 2)
        await self._wait_for_no_startup()

    async def test_timestamp_modes_have_independent_single_flight_sessions(self):
        started_modes = set()
        both_started = asyncio.Event()
        release = asyncio.Event()
        calls = []

        async def start(target_url, custom_ua, compat, *, playback_options=None):
            calls.append(playback_options.timestamp_mode.value)
            started_modes.add(playback_options.timestamp_mode.value)
            if len(started_modes) == 2:
                both_started.set()
            await release.wait()
            return (
                main._rtsp_session_id(
                    target_url,
                    custom_ua,
                    compat,
                    playback_options=playback_options,
                ),
                Path("/tmp/index.m3u8"),
            )

        passthrough = resolve_rtsp_playback_options()
        pts_from_dts = resolve_rtsp_playback_options(
            {"rtsp_timestamp_mode": "pts_from_dts"}
        )
        target = "rtsp://camera-modes.local/live"
        with mock.patch.object(main, "_start_rtsp_hls_session", new=start):
            waiters = [
                asyncio.create_task(main._ensure_rtsp_hls_session(
                    target, playback_options=pts_from_dts,
                )),
                asyncio.create_task(main._ensure_rtsp_hls_session(
                    target, playback_options=pts_from_dts,
                )),
                asyncio.create_task(main._ensure_rtsp_hls_session(
                    target, playback_options=passthrough,
                )),
            ]
            await asyncio.wait_for(both_started.wait(), timeout=1)
            release.set()
            results = await asyncio.gather(*waiters)

        self.assertEqual(calls.count("pts_from_dts"), 1)
        self.assertEqual(calls.count("passthrough"), 1)
        self.assertEqual(results[0], results[1])
        self.assertNotEqual(results[0][0], results[2][0])
        await self._wait_for_no_startup()

    async def test_startup_failure_is_shared_cleaned_and_retryable(self):
        started = asyncio.Event()
        fail = asyncio.Event()
        calls = 0

        async def start(target_url, custom_ua, compat):
            nonlocal calls
            calls += 1
            if calls == 1:
                started.set()
                await fail.wait()
                raise HTTPException(status_code=502, detail="ffmpeg exited")
            return "b" * 24, Path("/tmp/retry-index.m3u8")

        with mock.patch.object(main, "_start_rtsp_hls_session", new=start):
            waiters = [
                asyncio.create_task(main._ensure_rtsp_hls_session("rtsp://camera.local/live"))
                for _ in range(4)
            ]
            await started.wait()
            await asyncio.sleep(0)
            self.assertEqual(calls, 1)
            fail.set()
            results = await asyncio.gather(*waiters, return_exceptions=True)

            self.assertEqual(calls, 1)
            self.assertTrue(all(isinstance(result, HTTPException) for result in results))
            self.assertTrue(all(result.status_code == 502 for result in results))
            await self._wait_for_no_startup()

            retried = await main._ensure_rtsp_hls_session("rtsp://camera.local/live")

        self.assertEqual(retried[0], "b" * 24)
        self.assertEqual(calls, 2)
        await self._wait_for_no_startup()

    async def test_startup_timeout_is_shared_and_retryable(self):
        started = asyncio.Event()
        timeout = asyncio.Event()
        calls = 0

        async def start(target_url, custom_ua, compat):
            nonlocal calls
            calls += 1
            if calls == 1:
                started.set()
                await timeout.wait()
                raise HTTPException(status_code=504, detail="startup timeout")
            return "c" * 24, Path("/tmp/timeout-retry-index.m3u8")

        with mock.patch.object(main, "_start_rtsp_hls_session", new=start):
            first = asyncio.create_task(main._ensure_rtsp_hls_session("rtsp://camera.local/live"))
            second = asyncio.create_task(main._ensure_rtsp_hls_session("rtsp://camera.local/live"))
            await started.wait()
            timeout.set()
            results = await asyncio.gather(first, second, return_exceptions=True)
            self.assertTrue(all(isinstance(result, HTTPException) for result in results))
            self.assertTrue(all(result.status_code == 504 for result in results))
            await self._wait_for_no_startup()
            retried = await main._ensure_rtsp_hls_session("rtsp://camera.local/live")

        self.assertEqual(retried[0], "c" * 24)
        self.assertEqual(calls, 2)

    async def test_waiter_cancellation_does_not_cancel_shared_startup(self):
        started = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def start(target_url, custom_ua, compat):
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return "d" * 24, Path("/tmp/cancel-index.m3u8")

        with mock.patch.object(main, "_start_rtsp_hls_session", new=start):
            owner = asyncio.create_task(main._ensure_rtsp_hls_session("rtsp://camera.local/live"))
            cancelled_waiter = asyncio.create_task(main._ensure_rtsp_hls_session("rtsp://camera.local/live"))
            surviving_waiter = asyncio.create_task(main._ensure_rtsp_hls_session("rtsp://camera.local/live"))
            await started.wait()
            cancelled_waiter.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await cancelled_waiter
            self.assertFalse(owner.done())
            self.assertFalse(surviving_waiter.done())
            release.set()
            result_owner, result_survivor = await asyncio.gather(owner, surviving_waiter)

        self.assertEqual(result_owner, result_survivor)
        self.assertEqual(calls, 1)
        await self._wait_for_no_startup()

    async def test_creator_waiter_cancellation_does_not_cancel_shared_startup(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def start(target_url, custom_ua, compat):
            started.set()
            await release.wait()
            return "creator-cancel"[:24].ljust(24, "a"), Path("/tmp/creator-cancel.m3u8")

        with mock.patch.object(main, "_start_rtsp_hls_session", new=start):
            creator = asyncio.create_task(main._ensure_rtsp_hls_session("rtsp://camera.local/live"))
            survivor = asyncio.create_task(main._ensure_rtsp_hls_session("rtsp://camera.local/live"))
            await started.wait()
            creator.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await creator
            self.assertFalse(survivor.done())
            release.set()
            result = await survivor

        self.assertEqual(result[0], "creator-cancel"[:24].ljust(24, "a"))
        await self._wait_for_no_startup()

    async def test_startup_task_cancelled_before_body_releases_reservation_without_popen(self):
        target = "rtsp://camera-before-popen.local/live"
        original_create_task = asyncio.create_task
        captured = {}

        def create_startup_task(coro, **kwargs):
            task = original_create_task(coro, **kwargs)
            captured["task"] = task
            task.cancel()
            return task

        with mock.patch.object(main.asyncio, "create_task", side_effect=create_startup_task), mock.patch.object(
            main.subprocess, "Popen",
        ) as popen:
            owner = original_create_task(main._ensure_rtsp_hls_session(target))
            with self.assertRaises(asyncio.CancelledError):
                await owner

        await self._wait_for_no_startup()
        popen.assert_not_called()
        self.assertFalse(main.RTSP_HLS_SESSIONS)
        self.assertFalse(main._RTSP_RESERVED_SESSIONS)

    async def test_process_stop_uses_kill_after_bounded_terminate_wait(self):
        process = _StubbornProcess()
        await main._stop_rtsp_process(process)
        self.assertTrue(process.terminated)
        self.assertTrue(process.killed)
        self.assertEqual(process.wait_calls, 2)

    async def test_cancel_immediately_after_popen_cleans_process_session_dir_and_stderr(self):
        await self._cancel_real_startup_after_popen(0)

    async def test_cancel_after_first_segment_cleans_partial_startup(self):
        await self._cancel_real_startup_after_popen(1)

    async def test_popen_failure_cleans_directory_and_reservation(self):
        target = "rtsp://camera-popen-failure.local/live"
        session_id = main._rtsp_session_id(target)
        with mock.patch.object(main, "_ffmpeg_bin", return_value="/usr/bin/ffmpeg"), mock.patch.object(
            main.subprocess, "Popen", side_effect=OSError("popen failed"),
        ):
            with self.assertRaises(HTTPException) as ctx:
                await main._ensure_rtsp_hls_session(target)

        self.assertEqual(ctx.exception.status_code, 500)
        await self._wait_for_no_startup()
        self.assertFalse(main.RTSP_HLS_SESSIONS)
        self.assertFalse(main._RTSP_RESERVED_SESSIONS)
        self.assertFalse((main.RTSP_HLS_ROOT / session_id).exists())

    async def test_cancelled_startup_can_retry_without_old_cleanup_touching_new_session(self):
        target = "rtsp://camera-retry.local/live"
        session_id = main._rtsp_session_id(target)
        calls = 0
        first_process = _AliveProcess()
        second_process = _AliveProcess()
        first_popen = asyncio.Event()

        def popen(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            process = first_process if calls == 1 else second_process
            if calls == 1:
                first_popen.set()
            else:
                session_dir = main.RTSP_HLS_ROOT / session_id
                (session_dir / "index.m3u8").write_text("#EXTM3U\n", encoding="utf-8")
                for index in range(main.RTSP_HLS_START_SEGMENTS):
                    (session_dir / f"seg_{index:05d}.ts").write_bytes(b"segment")
            return process

        with mock.patch.object(main, "_ffmpeg_bin", return_value="/usr/bin/ffmpeg"), mock.patch.object(
            main.subprocess, "Popen", side_effect=popen,
        ):
            first = asyncio.create_task(main._ensure_rtsp_hls_session(target))
            await asyncio.wait_for(first_popen.wait(), timeout=1)
            old_session = main.RTSP_HLS_SESSIONS[session_id]
            main._RTSP_HLS_STARTUPS[session_id].cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first
            await self._wait_for_no_startup()

            retried = await main._ensure_rtsp_hls_session(target)
            self.assertEqual(retried[0], session_id)
            self.assertEqual(main.RTSP_HLS_SESSIONS[session_id]["startup_state"], "ready")
            await main._stop_rtsp_session(session_id, expected_session=old_session)
            self.assertIn(session_id, main.RTSP_HLS_SESSIONS)

        self.assertEqual(calls, 2)
        self.assertTrue(first_process.terminated)
        self.assertEqual(main.RTSP_HLS_SESSIONS[session_id]["process"], second_process)

    async def test_shutdown_cancels_startups_and_stops_ready_sessions(self):
        targets = [
            "rtsp://camera-shutdown-a.local/live",
            "rtsp://camera-shutdown-b.local/live",
            "rtsp://camera-shutdown-ready.local/live",
        ]
        processes = {}
        started = {target: asyncio.Event() for target in targets}

        def popen(cmd, **_kwargs):
            target = cmd[cmd.index("-i") + 1]
            session_id = main._rtsp_session_id(target)
            process = _AliveProcess()
            processes[target] = process
            if target.endswith("ready.local/live"):
                session_dir = main.RTSP_HLS_ROOT / session_id
                (session_dir / "index.m3u8").write_text("#EXTM3U\n", encoding="utf-8")
                for index in range(main.RTSP_HLS_START_SEGMENTS):
                    (session_dir / f"seg_{index:05d}.ts").write_bytes(b"segment")
            started[target].set()
            return process

        with mock.patch.object(main, "_ffmpeg_bin", return_value="/usr/bin/ffmpeg"), mock.patch.object(
            main.subprocess, "Popen", side_effect=popen,
        ):
            await main._ensure_rtsp_hls_session(targets[2])
            startup_waiters = [
                asyncio.create_task(main._ensure_rtsp_hls_session(target))
                for target in targets[:2]
            ]
            await asyncio.gather(*(started[target].wait() for target in targets[:2]))
            await main._stop_all_rtsp_sessions()
            results = await asyncio.gather(*startup_waiters, return_exceptions=True)

        self.assertTrue(all(isinstance(result, asyncio.CancelledError) for result in results))
        self.assertTrue(all(process.terminated for process in processes.values()))
        self.assertFalse(main.RTSP_HLS_SESSIONS)
        self.assertFalse(main._RTSP_HLS_STARTUPS)
        self.assertFalse(main._RTSP_RESERVED_SESSIONS)
        for target in targets:
            self.assertFalse((main.RTSP_HLS_ROOT / main._rtsp_session_id(target)).exists())

    async def test_shutdown_gate_prevents_concurrent_startup_creation(self):
        target = "rtsp://camera-shutdown-race.local/live"
        processes = []

        def popen(*_args, **_kwargs):
            process = _AliveProcess()
            processes.append(process)
            return process

        with mock.patch.object(main, "_ffmpeg_bin", return_value="/usr/bin/ffmpeg"), mock.patch.object(
            main.subprocess, "Popen", side_effect=popen,
        ):
            await main._RTSP_HLS_QUOTA_LOCK.acquire()
            owner = asyncio.create_task(main._ensure_rtsp_hls_session(target))
            await asyncio.sleep(0)
            shutdown = asyncio.create_task(main._stop_all_rtsp_sessions(shutdown=True))
            main._RTSP_HLS_QUOTA_LOCK.release()
            shutdown_result, owner_result = await asyncio.gather(shutdown, owner, return_exceptions=True)

        self.assertIsNone(shutdown_result)
        self.assertTrue(isinstance(owner_result, (HTTPException, asyncio.CancelledError)), repr(owner_result))
        self.assertTrue(all(process.terminated for process in processes))
        self.assertFalse(main.RTSP_HLS_SESSIONS)
        self.assertFalse(main._RTSP_HLS_STARTUPS)
        self.assertFalse(main._RTSP_RESERVED_SESSIONS)
        main._RTSP_HLS_SHUTTING_DOWN = False

    async def test_ready_session_uses_hot_path_without_new_startup(self):
        session_id = main._rtsp_session_id("rtsp://camera.local/live")
        session_dir = main.RTSP_HLS_ROOT / session_id
        session_dir.mkdir(parents=True)
        playlist = session_dir / "index.m3u8"
        playlist.touch()
        process = _AliveProcess()
        main.RTSP_HLS_SESSIONS[session_id] = {
            "process": process,
            "dir": session_dir,
            "last_access": 0,
            "startup_state": "ready",
        }

        with mock.patch.object(main, "_start_rtsp_hls_session", new=mock.AsyncMock()) as start:
            result = await main._ensure_rtsp_hls_session("rtsp://camera.local/live")

        self.assertEqual(result, (session_id, playlist))
        start.assert_not_awaited()
        self.assertFalse(process.terminated)

    async def test_concurrent_session_stop_is_idempotent(self):
        session_id = main._rtsp_session_id("rtsp://camera-stop-race.local/live")
        session_dir = main.RTSP_HLS_ROOT / session_id
        session_dir.mkdir(parents=True)
        process = _AliveProcess()
        main.RTSP_HLS_SESSIONS[session_id] = {
            "process": process,
            "dir": session_dir,
            "last_access": 0,
            "startup_state": "ready",
        }

        await asyncio.gather(
            main._stop_rtsp_session(session_id),
            main._stop_rtsp_session(session_id),
        )

        self.assertTrue(process.terminated)
        self.assertFalse(main.RTSP_HLS_SESSIONS)
        self.assertFalse(session_dir.exists())

    async def test_cleanup_skips_starting_session(self):
        session_id = "e" * 24
        process = _AliveProcess()
        main.RTSP_HLS_SESSIONS[session_id] = {
            "process": process,
            "last_access": 0,
            "startup_state": "starting",
        }
        stop_event = asyncio.Event()
        calls = 0

        async def fake_wait_for(awaitable, timeout):
            nonlocal calls
            if hasattr(awaitable, "close"):
                awaitable.close()
            calls += 1
            if calls > 1:
                stop_event.set()
            raise asyncio.TimeoutError

        with mock.patch.object(main.asyncio, "wait_for", new=fake_wait_for):
            await main._rtsp_hls_cleanup_task(stop_event)

        self.assertIn(session_id, main.RTSP_HLS_SESSIONS)
        self.assertFalse(process.terminated)


class RtspSessionQuotaTest(RtspStartupSingleFlightTest):
    def _register_ready(self, target_url: str) -> tuple[str, Path, _AliveProcess]:
        session_id = main._rtsp_session_id(target_url)
        session_dir = main.RTSP_HLS_ROOT / session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        playlist = session_dir / "index.m3u8"
        playlist.touch()
        process = _AliveProcess()
        main.RTSP_HLS_SESSIONS[session_id] = {
            "process": process,
            "dir": session_dir,
            "last_access": 0,
            "startup_state": "ready",
        }
        return session_id, playlist, process

    async def test_limit_rejects_new_session_before_startup(self):
        calls = []

        async def start(target_url, custom_ua, compat):
            calls.append(target_url)
            session_id, playlist, _process = self._register_ready(target_url)
            return session_id, playlist

        with mock.patch.object(main, "get_effective_settings_sync", return_value=type("Settings", (), {"rtsp_max_sessions": 2})()), \
                mock.patch.object(main, "_start_rtsp_hls_session", new=start):
            await main._ensure_rtsp_hls_session("rtsp://camera-a.local/live")
            await main._ensure_rtsp_hls_session("rtsp://camera-b.local/live")
            with self.assertRaises(HTTPException) as ctx:
                await main._ensure_rtsp_hls_session("rtsp://camera-c.local/live")

        self.assertEqual(ctx.exception.status_code, 503)
        self.assertEqual(ctx.exception.detail, "RTSP session capacity reached")
        self.assertEqual(calls, [
            "rtsp://camera-a.local/live",
            "rtsp://camera-b.local/live",
        ])

    async def test_same_key_waiters_consume_one_slot(self):
        started = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def start(target_url, custom_ua, compat):
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            session_id, playlist, _process = self._register_ready(target_url)
            return session_id, playlist

        with mock.patch.object(main, "get_effective_settings_sync", return_value=type("Settings", (), {"rtsp_max_sessions": 1})()), \
                mock.patch.object(main, "_start_rtsp_hls_session", new=start):
            waiters = [
                asyncio.create_task(main._ensure_rtsp_hls_session("rtsp://camera.local/live"))
                for _ in range(10)
            ]
            await started.wait()
            await asyncio.sleep(0)
            self.assertEqual(calls, 1)
            self.assertEqual(main._RTSP_RESERVED_SESSIONS, {
                main._rtsp_session_id("rtsp://camera.local/live"),
            })
            release.set()
            results = await asyncio.gather(*waiters)

        self.assertEqual(results, [results[0]] * 10)
        self.assertEqual(calls, 1)
        self.assertEqual(len(main.RTSP_HLS_SESSIONS), 1)
        self.assertFalse(main._RTSP_RESERVED_SESSIONS)

    async def test_different_keys_compete_atomically_for_last_slot(self):
        started = asyncio.Event()
        release = asyncio.Event()
        calls = []

        async def start(target_url, custom_ua, compat):
            calls.append(target_url)
            started.set()
            await release.wait()
            session_id, playlist, _process = self._register_ready(target_url)
            return session_id, playlist

        with mock.patch.object(main, "get_effective_settings_sync", return_value=type("Settings", (), {"rtsp_max_sessions": 1})()), \
                mock.patch.object(main, "_start_rtsp_hls_session", new=start):
            first = asyncio.create_task(main._ensure_rtsp_hls_session("rtsp://camera-a.local/live"))
            second = asyncio.create_task(main._ensure_rtsp_hls_session("rtsp://camera-b.local/live"))
            await started.wait()
            await asyncio.sleep(0)
            release.set()
            results = await asyncio.gather(first, second, return_exceptions=True)

        self.assertEqual(len(calls), 1)
        self.assertEqual(sum(isinstance(result, HTTPException) for result in results), 1)
        rejected = next(result for result in results if isinstance(result, HTTPException))
        self.assertEqual((rejected.status_code, rejected.detail), (503, "RTSP session capacity reached"))
        self.assertEqual(len(main.RTSP_HLS_SESSIONS), 1)

    async def test_startup_failure_releases_slot_for_next_session(self):
        started = asyncio.Event()
        fail = asyncio.Event()
        calls = []

        async def start(target_url, custom_ua, compat):
            calls.append(target_url)
            if len(calls) == 1:
                started.set()
                await fail.wait()
                raise HTTPException(status_code=502, detail="ffmpeg exited")
            session_id, playlist, _process = self._register_ready(target_url)
            return session_id, playlist

        with mock.patch.object(main, "get_effective_settings_sync", return_value=type("Settings", (), {"rtsp_max_sessions": 1})()), \
                mock.patch.object(main, "_start_rtsp_hls_session", new=start):
            first = asyncio.create_task(main._ensure_rtsp_hls_session("rtsp://camera-a.local/live"))
            await started.wait()
            fail.set()
            with self.assertRaises(HTTPException) as ctx:
                await first
            self.assertEqual(ctx.exception.status_code, 502)
            await self._wait_for_no_startup()
            self.assertFalse(main._RTSP_RESERVED_SESSIONS)
            retried = await main._ensure_rtsp_hls_session("rtsp://camera-b.local/live")

        self.assertEqual(retried[0], main._rtsp_session_id("rtsp://camera-b.local/live"))
        self.assertEqual(calls, [
            "rtsp://camera-a.local/live",
            "rtsp://camera-b.local/live",
        ])

    async def test_startup_timeout_releases_slot_for_next_session(self):
        started = asyncio.Event()
        timeout = asyncio.Event()
        calls = []

        async def start(target_url, custom_ua, compat):
            calls.append(target_url)
            if len(calls) == 1:
                started.set()
                await timeout.wait()
                raise HTTPException(status_code=504, detail="startup timeout")
            session_id, playlist, _process = self._register_ready(target_url)
            return session_id, playlist

        with mock.patch.object(main, "get_effective_settings_sync", return_value=type("Settings", (), {"rtsp_max_sessions": 1})()), \
                mock.patch.object(main, "_start_rtsp_hls_session", new=start):
            first = asyncio.create_task(main._ensure_rtsp_hls_session("rtsp://camera-a.local/live"))
            await started.wait()
            timeout.set()
            with self.assertRaises(HTTPException) as ctx:
                await first
            self.assertEqual(ctx.exception.status_code, 504)
            await self._wait_for_no_startup()
            self.assertFalse(main._RTSP_RESERVED_SESSIONS)
            retried = await main._ensure_rtsp_hls_session("rtsp://camera-b.local/live")

        self.assertEqual(retried[0], main._rtsp_session_id("rtsp://camera-b.local/live"))
        self.assertEqual(calls, [
            "rtsp://camera-a.local/live",
            "rtsp://camera-b.local/live",
        ])

    async def test_ready_session_is_not_counted_twice(self):
        target = "rtsp://camera-a.local/live"
        session_id, playlist, process = self._register_ready(target)

        with mock.patch.object(main, "get_effective_settings_sync", return_value=type("Settings", (), {"rtsp_max_sessions": 1})()), \
                mock.patch.object(main, "_start_rtsp_hls_session", new=mock.AsyncMock()) as start:
            result = await main._ensure_rtsp_hls_session(target)

        self.assertEqual(result, (session_id, playlist))
        self.assertFalse(main._RTSP_RESERVED_SESSIONS)
        start.assert_not_awaited()
        self.assertFalse(process.terminated)

    async def test_lowering_limit_does_not_kill_existing_sessions(self):
        processes = []
        for index in range(3):
            _session_id, _playlist, process = self._register_ready(f"rtsp://camera-{index}.local/live")
            processes.append(process)

        with mock.patch.object(main, "get_effective_settings_sync", return_value=type("Settings", (), {"rtsp_max_sessions": 1})()), \
                mock.patch.object(main, "_start_rtsp_hls_session", new=mock.AsyncMock()) as start:
            with self.assertRaises(HTTPException) as ctx:
                await main._ensure_rtsp_hls_session("rtsp://camera-new.local/live")

        self.assertEqual(ctx.exception.status_code, 503)
        self.assertEqual(len(main.RTSP_HLS_SESSIONS), 3)
        self.assertTrue(all(not process.terminated for process in processes))
        start.assert_not_awaited()

    async def test_stopping_session_restores_capacity(self):
        calls = []

        async def start(target_url, custom_ua, compat):
            calls.append(target_url)
            session_id, playlist, _process = self._register_ready(target_url)
            return session_id, playlist

        with mock.patch.object(main, "get_effective_settings_sync", return_value=type("Settings", (), {"rtsp_max_sessions": 1})()), \
                mock.patch.object(main, "_start_rtsp_hls_session", new=start):
            session_id, _playlist = await main._ensure_rtsp_hls_session("rtsp://camera-a.local/live")
            await main._stop_rtsp_session(session_id)
            await main._ensure_rtsp_hls_session("rtsp://camera-b.local/live")

        self.assertEqual(calls, [
            "rtsp://camera-a.local/live",
            "rtsp://camera-b.local/live",
        ])
        self.assertEqual(len(main.RTSP_HLS_SESSIONS), 1)

    async def test_shutdown_stops_all_sessions_and_restores_capacity(self):
        calls = []

        async def start(target_url, custom_ua, compat):
            calls.append(target_url)
            session_id, playlist, _process = self._register_ready(target_url)
            return session_id, playlist

        with mock.patch.object(main, "get_effective_settings_sync", return_value=type("Settings", (), {"rtsp_max_sessions": 1})()), \
                mock.patch.object(main, "_start_rtsp_hls_session", new=start):
            await main._ensure_rtsp_hls_session("rtsp://camera-a.local/live")
            await main._stop_all_rtsp_sessions()
            await main._ensure_rtsp_hls_session("rtsp://camera-b.local/live")

        self.assertEqual(calls, [
            "rtsp://camera-a.local/live",
            "rtsp://camera-b.local/live",
        ])
        self.assertEqual(len(main.RTSP_HLS_SESSIONS), 1)


if __name__ == "__main__":
    unittest.main()
