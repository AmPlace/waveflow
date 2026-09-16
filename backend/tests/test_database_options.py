"""验证 m3u 中 EXTVLCOPT/KODIPROP/WAVEFLOW 解析的字段能正确入库与回退。"""
import asyncio
import os
import sqlite3
import sys
import tempfile
import unittest


class ChannelOptionsPersistTest(unittest.TestCase):
    """端到端：parse_m3u → add_channels_bulk → get_channels / get_aggregated_channels。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        os.environ["WAVEFLOW_DB_PATH"] = os.path.join(self._tmpdir.name, "t.db")
        # database 模块顶层读 _DB_PATH，需要按需 reload
        for mod in list(sys.modules):
            if mod == "database":
                del sys.modules[mod]

    def tearDown(self):
        os.environ.pop("WAVEFLOW_DB_PATH", None)
        self._tmpdir.cleanup()
        for mod in list(sys.modules):
            if mod == "database":
                del sys.modules[mod]

    def _run(self, coro):
        return asyncio.run(coro)

    def test_channel_level_options_persist_and_subscription_fallbacks(self):
        from m3u8_parser import parse_m3u
        import database as db

        async def go():
            await db.initialize()

            m3u = (
                '#EXTM3U\n'
                '#EXTINF:-1,Q\n'
                '#EXTVLCOPT:http-referrer=https://www.qztv.cn/\n'
                'http://q.example/q.m3u8\n'
                '#EXTINF:-1,A\n'
                '#EXTVLCOPT:http-user-agent=AcmeUA/1.0\n'
                '#EXTVLCOPT:http-referrer=https://example.com/\n'
                '#WAVEFLOW:requires_proxy=1\n'
                'http://example.com/a.m3u8\n'
                '#EXTINF:-1,P\n'
                'http://example.com/p.m3u8\n'
            )
            chans = parse_m3u(m3u)
            self.assertEqual(len(chans), 3)

            sub_id = await db.add_subscription(
                title="t", url="http://t/x.m3u", channel_count=len(chans)
            )
            await db.add_channels_bulk(sub_id, chans)

            rows = {r["name"]: r for r in await db.get_channels(sub_id)}
            # 频道级写入：referer/custom_ua/force_proxy 都正确落库
            self.assertEqual(rows["Q"]["referer"], "https://www.qztv.cn/")
            self.assertEqual(rows["Q"]["custom_ua"], "")
            self.assertEqual(rows["Q"]["force_proxy"], 0)

            self.assertEqual(rows["A"]["referer"], "https://example.com/")
            self.assertEqual(rows["A"]["custom_ua"], "AcmeUA/1.0")
            self.assertEqual(rows["A"]["force_proxy"], 1)

            self.assertEqual(rows["P"]["custom_ua"], "")
            self.assertEqual(rows["P"]["force_proxy"], 0)

            # 设订阅级 UA / force_proxy → 频道级空者回退
            await db.update_subscription(sub_id, custom_ua="SubUA/2", force_proxy=1)
            rows = {r["name"]: r for r in await db.get_channels(sub_id)}
            # 频道级有值的 A 不被订阅级覆盖
            self.assertEqual(rows["A"]["custom_ua"], "AcmeUA/1.0")
            self.assertEqual(rows["A"]["force_proxy"], 1)
            # 频道级空者回退订阅级
            self.assertEqual(rows["Q"]["custom_ua"], "SubUA/2")
            self.assertEqual(rows["Q"]["force_proxy"], 1)
            self.assertEqual(rows["P"]["custom_ua"], "SubUA/2")

            # get_aggregated_channels 也应同样回退
            agg = {r["name"]: r for r in await db.get_aggregated_channels()}
            self.assertEqual(agg["A"]["custom_ua"], "AcmeUA/1.0")
            self.assertEqual(agg["Q"]["custom_ua"], "SubUA/2")
            self.assertEqual(agg["P"]["custom_ua"], "SubUA/2")

        self._run(go())

    def test_cached_database_module_follows_explicit_isolated_path(self):
        import database as db

        first_path = os.environ["WAVEFLOW_DB_PATH"]
        self._run(db.initialize())
        with tempfile.TemporaryDirectory() as second_dir:
            second_path = os.path.join(second_dir, "second.db")
            os.environ["WAVEFLOW_DB_PATH"] = second_path
            self._run(db.initialize())
            with sqlite3.connect(second_path) as conn:
                table = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='plugin_installations'"
                ).fetchone()
            self.assertIsNotNone(table)
            self.assertEqual(db._current_db_path(), second_path)
        os.environ["WAVEFLOW_DB_PATH"] = first_path

    def test_rtsp_timestamp_mode_persists_and_invalid_input_is_safe(self):
        import database as db

        async def go():
            await db.initialize()
            sub_id = await db.add_subscription(
                title="rtsp", url="https://example.test/rtsp.m3u", channel_count=2,
            )
            await db.add_channels_bulk(sub_id, [
                {
                    "name": "configured",
                    "url": "rtsp://configured.example/live",
                    "source_type": "rtsp",
                    "rtsp_timestamp_mode": "pts_from_dts",
                },
                {
                    "name": "invalid",
                    "url": "rtsp://invalid.example/live",
                    "source_type": "rtsp",
                    "rtsp_timestamp_mode": "-vf evil",
                },
            ])
            rows = {row["name"]: row for row in await db.get_channels(sub_id)}
            self.assertEqual(rows["configured"]["rtsp_timestamp_mode"], "pts_from_dts")
            self.assertEqual(rows["invalid"]["rtsp_timestamp_mode"], "passthrough")
            aggregated = {
                row["name"]: row for row in await db.get_aggregated_channels()
            }
            self.assertEqual(
                aggregated["configured"]["rtsp_timestamp_mode"],
                "pts_from_dts",
            )

        self._run(go())


if __name__ == "__main__":
    unittest.main()
