"""验证 market 包导入时频道级（manifest channel.defaults.source / m3u 频道级
EXTVLCOPT/WAVEFLOW）的配置优先级高于源级（sources[i]）。"""
import unittest


class MarketMergePriorityTest(unittest.TestCase):
    """直接调用 market._merge_source_defaults 验证 merge 顺序。

    market 模块顶层 import httpx，本地测试环境可能缺该依赖；这里把测试封进 setUp，
    缺 httpx 时 skip。
    """

    @classmethod
    def setUpClass(cls):
        try:
            from market import _merge_source_defaults  # noqa: F401
        except Exception as exc:  # ModuleNotFoundError 等
            raise unittest.SkipTest(f"market module unavailable: {exc}")

    def test_channel_level_overrides_source_level(self):
        from market import _merge_source_defaults

        package_defaults = {"type": "hls", "headers": {"User-Agent": "PkgUA"}}
        source = {"url": "http://x/a.m3u8", "headers": {"Referer": "https://src/"}}
        channel_overrides = {
            "headers": {"Referer": "https://channel/", "User-Agent": "ChUA"},
            "requires_proxy": True,
        }

        # build_preview 的 merge 顺序：包级 → 源级 → 频道级
        merged = _merge_source_defaults(package_defaults, source, channel_overrides)

        # 频道级 Referer 压过源级
        self.assertEqual(merged["headers"]["Referer"], "https://channel/")
        # 频道级 UA 压过包级
        self.assertEqual(merged["headers"]["User-Agent"], "ChUA")
        # 频道级 requires_proxy=True 生效
        self.assertTrue(merged["requires_proxy"])

    def test_channel_level_missing_falls_back_to_source_level(self):
        from market import _merge_source_defaults

        package_defaults = {"type": "hls", "headers": {"User-Agent": "PkgUA"}}
        source = {"url": "http://x/a.m3u8", "headers": {"Referer": "https://src/"}}
        merged = _merge_source_defaults(package_defaults, source, {})

        self.assertEqual(merged["headers"]["Referer"], "https://src/")
        # 源级没写 UA → 回退包级
        self.assertEqual(merged["headers"]["User-Agent"], "PkgUA")

    def test_no_overrides_uses_package_only(self):
        from market import _merge_source_defaults

        package_defaults = {"type": "hls", "headers": {"User-Agent": "PkgUA"}}
        merged = _merge_source_defaults(package_defaults, {"url": "http://x/"}, None)

        self.assertEqual(merged["headers"]["User-Agent"], "PkgUA")


if __name__ == "__main__":
    unittest.main()
