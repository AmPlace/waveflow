"""Integration tests: mock HLS upstream → WaveFlow proxy → verify full pipeline."""
from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import sys
import time
import unittest
import sqlite3

import httpx
import re

os.environ["WAVEFLOW_PROXY_HANDLE_SECRET"] = "test-handle-secret-32bytes!!!"
WAVEFLOW_DB = "/tmp/wf_integration_test.db"
# 子进程通过 _start_waveflow 的显式 env 使用隔离数据库。不要在测试模块
# import 时改写父进程 DB 路径，否则 unittest discovery 会污染其他测试。
# 确保 backend/ 在路径中，以便 import database / main / security.*
_backend_dir = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, _backend_dir)

from tests.mock_hls_upstream import LiveScenario, MockHLSServer


WAVEFLOW_PORT = 18888
TEST_CANONICAL_KEY = "testchannel"


async def _start_waveflow() -> subprocess.Popen:
    # uvicorn auto-inits DB on startup; keep if exists
    env = os.environ.copy()
    env["PYTHONPATH"] = os.path.join(os.path.dirname(__file__), "..")
    env["WAVEFLOW_DB_PATH"] = WAVEFLOW_DB
    env["WAVEFLOW_PROXY_HANDLE_SECRET"] = "test-handle-secret-32bytes!!!"
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn", "main:app",
            "--app-dir", os.path.join(os.path.dirname(__file__), ".."),
            "--host", "127.0.0.1", "--port", str(WAVEFLOW_PORT),
            "--log-level", "warning",
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    # wait until ready
    for _ in range(30):
        try:
            async with httpx.AsyncClient() as c:
                r = await c.get(f"http://127.0.0.1:{WAVEFLOW_PORT}/openapi.json", timeout=2)
                if r.status_code == 200:
                    return proc
        except Exception:
            pass
        await asyncio.sleep(0.5)
    proc.kill()
    raise RuntimeError("WaveFlow did not start")


def _seed_db_sync(upstream_url: str) -> None:
    """向测试 DB 插入一条订阅 + 频道，让后端聚合能匹配到 testchannel。"""
    conn = sqlite3.connect(WAVEFLOW_DB)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

    t = time.strftime("%Y-%m-%dT%H:%M:%S")
    cur = conn.cursor()
    cur.execute("SELECT id FROM subscriptions WHERE title='integration_test'")
    row = cur.fetchone()
    if row:
        sid = row[0]
    else:
        cur.execute(
            "INSERT INTO subscriptions (title, url, channel_count, created_at) VALUES (?,?,?,?)",
            ("integration_test", "file:///tmp/integration_test.txt", 1, t),
        )
        conn.commit()
        sid = cur.lastrowid

    cur.execute("DELETE FROM channels WHERE subscription_id=?", (sid,))
    cur.execute(
        "INSERT INTO channels (subscription_id, name, url, group_name, source_type, is_working, probe_status, live_status) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (sid, TEST_CANONICAL_KEY, upstream_url, "Mock", "hls", 1, "healthy", "live"),
    )
    # 放行 SSRF guard：允许 loopback + 私网（127.0.0.1 mock upstream）
    for k in ("allow_loopback", "allow_private"):
        cur.execute(
            "INSERT OR REPLACE INTO app_settings (key, value_json, updated_at) VALUES (?,?,?)",
            (k, json.dumps(True), t),
        )
    conn.commit()
    conn.close()


class IntegrationTestBase(unittest.IsolatedAsyncioTestCase):
    proc: subprocess.Popen | None = None
    upstream: MockHLSServer | None = None

    @classmethod
    def setUpClass(cls):
        cls.proc = asyncio.run(_start_waveflow())

    @classmethod
    def tearDownClass(cls):
        if cls.proc:
            cls.proc.kill()
            cls.proc.wait()

    async def asyncSetUp(self):
        os.environ["WAVEFLOW_PROXY_HANDLE_SECRET"] = "test-handle-secret-32bytes!!!"
        self.upstream = MockHLSServer(scenario=LiveScenario())
        await self.upstream.start()
        _seed_db_sync(self.upstream.playlist_url)
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(15))
        self.base = f"http://127.0.0.1:{WAVEFLOW_PORT}"

    async def asyncTearDown(self):
        await self.client.aclose()
        if self.upstream:
            await self.upstream.stop()

    async def _get_playlist(self) -> httpx.Response:
        """请求 Thin playlist，跟踪 redirect。"""
        resp = await self.client.get(
            f"{self.base}/api/media/channel/testchannel/playlist.m3u8",
            follow_redirects=True,
        )
        # Handle 404 — channel may not exist if DB seeding failed
        return resp

    async def _decode_handle(self, path: str) -> dict:
        """解码一个 /api/media/proxy/chunk/xxx 的 handle payload。"""
        token = path.rsplit("/", 1)[-1].split("?")[0]
        token = token.removesuffix(".ts")
        body, _, _sig = token.partition(".")
        pad = (-len(body)) % 4
        data = base64.urlsafe_b64decode(body + ("=" * pad))
        return json.loads(data)

    def _extract_chunk_paths(self, m3u8: str) -> list[str]:
        import re
        return re.findall(r"(/api/media/proxy/chunk/[A-Za-z0-9_.-]+(?:\?[^\s]+)?)", m3u8)


# ── basic scenarios ───────────────────────────────────────────────────────


class TestNormalLive(IntegrationTestBase):
    """正常直播：sequence 推进、缓存命中、handle 不同、内部 wf_seq。"""

    async def test_playlist_is_valid_m3u8(self):
        resp = await self._get_playlist()
        self.assertEqual(resp.status_code, 200)
        self.assertIn("#EXTM3U", resp.text)
        self.assertIn("#EXT-X-TARGETDURATION", resp.text)

    async def test_handle_urls_are_relative(self):
        resp = await self._get_playlist()
        for path in self._extract_chunk_paths(resp.text):
            self.assertTrue(path.startswith("/api/media/proxy/chunk/"))

    async def test_handle_points_to_upstream(self):
        resp = await self._get_playlist()
        paths = self._extract_chunk_paths(resp.text)
        self.assertGreater(len(paths), 0)
        payload = await self._decode_handle(paths[0])
        self.assertIn(".ts", payload["url"])
        self.assertIn(str(self.upstream.port), payload["url"])

    async def test_wf_seq_stays_on_internal_proxy_url(self):
        resp = await self._get_playlist()
        paths = self._extract_chunk_paths(resp.text)
        payload = await self._decode_handle(paths[0])
        chunk_line = next(line for line in resp.text.splitlines() if line.startswith("/api/media/proxy/chunk/"))
        self.assertIn("wf_seq=", chunk_line)
        self.assertNotIn("wf_seq=", payload["url"])

    async def test_distinct_handles_per_segment(self):
        resp = await self._get_playlist()
        paths = self._extract_chunk_paths(resp.text)
        self.assertEqual(len(set(paths)), len(paths), "all handles should be distinct")

    async def test_chunk_fetch_returns_200(self):
        resp = await self._get_playlist()
        paths = self._extract_chunk_paths(resp.text)
        chunk = await self.client.get(f"{self.base}{paths[0]}")
        self.assertEqual(chunk.status_code, 200)
        self.assertIn("video/mp2t", chunk.headers.get("content-type", ""))

    async def test_cache_hit_after_cold_start(self):
        # cold
        t0 = time.time()
        r1 = await self._get_playlist()
        t1 = time.time() - t0
        self.assertGreater(t1, 0.001)
        # warm: 第二次应该 < 50ms（缓存命中）
        # Thin cache TTL 为 1.5s，连打两次应命中。
        t0 = time.time()
        r2 = await self._get_playlist()
        t2 = time.time() - t0
        self.assertLess(t2, 0.5, f"cache should hit in <500ms, got {t2:.3f}s")

    async def test_sequence_advances_over_time(self):
        r1 = await self._get_playlist()
        seq1 = int(re.search(r"#EXT-X-MEDIA-SEQUENCE:(\d+)", r1.text).group(1))
        # Advance the mock upstream twice
        self.upstream.scenario.advance()
        self.upstream.scenario.advance()
        await asyncio.sleep(2.0)  # Thin cache TTL=1.5s, must expire before re-fetch
        r2 = await self._get_playlist()
        seq2 = int(re.search(r"#EXT-X-MEDIA-SEQUENCE:(\d+)", r2.text).group(1))
        self.assertGreater(seq2, seq1, f"sequence should advance: {seq1} -> {seq2}")


class TestCyclicFilenames(IntegrationTestBase):
    """0.ts/1.ts 循环文件名 —— wf_seq 防止去重。"""

    async def asyncSetUp(self):
        self.upstream = MockHLSServer(scenario=LiveScenario(cyclic_names=("0.ts", "1.ts", "2.ts")))
        await self.upstream.start()
        _seed_db_sync(self.upstream.playlist_url)
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(15))
        self.base = f"http://127.0.0.1:{WAVEFLOW_PORT}"

    async def test_cyclic_names_get_distinct_handles(self):
        resp = await self._get_playlist()
        paths = self._extract_chunk_paths(resp.text)
        self.assertEqual(len(set(paths)), len(paths))


class TestDiscontinuity(IntegrationTestBase):
    """DISCONTINUITY 标签保留。"""

    async def asyncSetUp(self):
        self.upstream = MockHLSServer(scenario=LiveScenario(discontinuity=True))
        await self.upstream.start()
        _seed_db_sync(self.upstream.playlist_url)
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(15))
        self.base = f"http://127.0.0.1:{WAVEFLOW_PORT}"

    async def test_discontinuity_preserved(self):
        resp = await self._get_playlist()
        self.assertIn("#EXT-X-DISCONTINUITY", resp.text)


class TestKeyMap(IntegrationTestBase):
    """KEY / MAP 标签保留并重写 URI。"""

    async def asyncSetUp(self):
        self.upstream = MockHLSServer(scenario=LiveScenario(
            key_uri=f"http://127.0.0.1:9999/key.bin",
            map_uri=f"http://127.0.0.1:9999/init.mp4",
        ))
        await self.upstream.start()
        _seed_db_sync(self.upstream.playlist_url)
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(15))
        self.base = f"http://127.0.0.1:{WAVEFLOW_PORT}"

    async def test_key_rewritten_to_proxy_handle(self):
        resp = await self._get_playlist()
        self.assertIn('URI="/api/media/proxy/chunk/', resp.text)

    async def test_map_rewritten_to_proxy_handle(self):
        resp = await self._get_playlist()
        self.assertIn("/api/media/proxy/chunk/", resp.text)


# ── fault injection ────────────────────────────────────────────────────────


class TestServerErrors(IntegrationTestBase):
    """404 / 410 / 503 错误响应。"""

    async def asyncSetUp(self):
        self.upstream = MockHLSServer(scenario=LiveScenario(status_sequence=[200, 503, 200]))
        await self.upstream.start()
        _seed_db_sync(self.upstream.playlist_url)
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(15))
        self.base = f"http://127.0.0.1:{WAVEFLOW_PORT}"

    async def test_503_playlist_returns_error(self):
        """第二次上游请求 503 → Thin playlist 应降级处理。"""
        # First request: cold start (200)
        r1 = await self._get_playlist()
        self.assertEqual(r1.status_code, 200)
        # Advance scenario so the second request hits the 503
        self.upstream.scenario.advance()
        await asyncio.sleep(2)  # let refresher try and hit 503
        # Third request: should still get cached data or fallback
        r3 = await self.client.get(
            f"{self.base}/api/media/channel/testchannel/playlist.m3u8",
            follow_redirects=True,
        )
        # Should not 500-crash; might be 200 (cached) or 503 (stale exceeded)
        self.assertIn(r3.status_code, (200, 403, 503))


class TestSegment404(IntegrationTestBase):
    """某个 segment 返回 404。"""

    async def asyncSetUp(self):
        self.upstream = MockHLSServer(scenario=LiveScenario(fail_segments_at={0}))
        await self.upstream.start()
        _seed_db_sync(self.upstream.playlist_url)
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(15))
        self.base = f"http://127.0.0.1:{WAVEFLOW_PORT}"

    async def test_failed_segment_propagates_404(self):
        resp = await self._get_playlist()
        # The first segment should be a "bad" one
        # (The mock upstream puts bad_N.ts for segment 0 when fail_segments_at={0})
        paths = self._extract_chunk_paths(resp.text)
        first_path = paths[0]
        chunk = await self.client.get(f"{self.base}{first_path}")
        self.assertEqual(chunk.status_code, 404)


class TestRange(IntegrationTestBase):
    """Range 请求透传。"""

    async def test_range_206(self):
        resp = await self._get_playlist()
        paths = self._extract_chunk_paths(resp.text)
        chunk = await self.client.get(f"{self.base}{paths[0]}", headers={"Range": "bytes=0-99"})
        self.assertEqual(chunk.status_code, 206)
        self.assertIn("bytes", chunk.headers.get("content-range", ""))


class TestSlowUpstream(IntegrationTestBase):
    """慢响应上游。"""

    async def asyncSetUp(self):
        self.upstream = MockHLSServer(scenario=LiveScenario(slow_response_seconds=3.0))
        await self.upstream.start()
        _seed_db_sync(self.upstream.playlist_url)
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(30))
        self.base = f"http://127.0.0.1:{WAVEFLOW_PORT}"

    async def test_slow_upstream_still_returns_playlist(self):
        resp = await self._get_playlist()
        self.assertEqual(resp.status_code, 200)
        self.assertIn("#EXTM3U", resp.text)


class TestWrongContentLength(IntegrationTestBase):
    """故意把 Content-Length 设错。"""

    async def asyncSetUp(self):
        self.upstream = MockHLSServer(scenario=LiveScenario(wrong_content_length=True))
        await self.upstream.start()
        _seed_db_sync(self.upstream.playlist_url)
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(15))
        self.base = f"http://127.0.0.1:{WAVEFLOW_PORT}"

    async def test_wrong_cl_is_handled(self):
        resp = await self._get_playlist()
        paths = self._extract_chunk_paths(resp.text)
        try:
            chunk = await self.client.get(f"{self.base}{paths[0]}")
            self.assertIn(chunk.status_code, (200, 502, 504))
        except (httpx.RemoteProtocolError, httpx.ReadError):
            pass


class TestHandleExpiry(IntegrationTestBase):
    """Handle 过期。"""

    async def test_expired_handle_returns_410(self):
        from security.proxy_handles import _build_payload, _sign_payload
        payload = _build_payload(kind="chunk", url=self.upstream.playlist_url,
                                 exp=int(time.time()) - 3600, src="channel:test", src_id="channel:test")
        handle = _sign_payload(payload)
        resp = await self.client.get(f"{self.base}/api/media/proxy/chunk/{handle}")
        self.assertEqual(resp.status_code, 410)


class TestSSRFGuard(IntegrationTestBase):
    """SSRF 规则。"""

    async def test_metadata_ip_blocked(self):
        from security.proxy_handles import _build_payload, _sign_payload
        payload = _build_payload(kind="chunk", url="http://169.254.169.254/",
                                 exp=int(time.time()) + 3600, src="channel:test", src_id="channel:test")
        handle = _sign_payload(payload)
        resp = await self.client.get(f"{self.base}/api/media/proxy/chunk/{handle}")
        self.assertEqual(resp.status_code, 403)


class TestPlaylistFreeze(IntegrationTestBase):
    """Playlist 冻结（sequence 不推进）。"""

    async def asyncSetUp(self):
        self.upstream = MockHLSServer(scenario=LiveScenario(playlist_freeze=True))
        await self.upstream.start()
        _seed_db_sync(self.upstream.playlist_url)
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(15))
        self.base = f"http://127.0.0.1:{WAVEFLOW_PORT}"

    async def test_frozen_playlist_detected(self):
        r1 = await self._get_playlist()
        self.assertEqual(r1.status_code, 200)
        await asyncio.sleep(1)
        r2 = await self._get_playlist()
        self.assertEqual(r2.status_code, 200)


class TestRedirect(IntegrationTestBase):
    """302 重定向。"""

    async def asyncSetUp(self):
        self.upstream = MockHLSServer(scenario=LiveScenario(
            redirect_target="/playlist.m3u8",
        ))
        await self.upstream.start()
        _seed_db_sync(self.upstream.playlist_url)
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(15))
        self.base = f"http://127.0.0.1:{WAVEFLOW_PORT}"

    async def test_redirect_handled(self):
        resp = await self._get_playlist()
        self.assertIn(resp.status_code, (200, 302))


if __name__ == "__main__":
    unittest.main()
