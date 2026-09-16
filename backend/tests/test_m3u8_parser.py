import unittest

from m3u8_parser import (
    adapter_provider,
    detect_source_type,
    parse_youtube_channel_id,
    parse_youtube_video_id,
)


class YoutubeParsingTest(unittest.TestCase):
    def test_live_url_extracts_video_id(self):
        self.assertEqual(
            parse_youtube_video_id("https://www.youtube.com/live/abcDEF123_4?feature=share"),
            "abcDEF123_4",
        )

    def test_youtube_scheme_live_extracts_video_id(self):
        self.assertEqual(parse_youtube_video_id("youtube://live/abcDEF123_4"), "abcDEF123_4")
        self.assertEqual(detect_source_type("youtube://live/abcDEF123_4"), "youtube")

    def test_youtube_scheme_channel_live_extracts_channel_id(self):
        channel_id = "UC12345678901234567890"
        self.assertEqual(parse_youtube_channel_id(f"youtube://channel/{channel_id}/live"), channel_id)
        self.assertEqual(parse_youtube_channel_id(f"youtube://{channel_id}/live"), channel_id)
        self.assertEqual(detect_source_type(f"youtube://channel/{channel_id}/live"), "youtube")

    def test_untrusted_youtube_subdomain_is_not_a_youtube_source(self):
        url = "https://evil.youtube.com/watch?v=abcDEF123_4"
        self.assertEqual(parse_youtube_video_id(url), "")
        self.assertNotEqual(detect_source_type(url), "youtube")

    def test_youtube_video_parser_rejects_non_video_path_and_non_https_authority(self):
        invalid_urls = (
            "https://www.youtube.com/playlist?v=abcDEF123_4",
            "http://www.youtube.com/watch?v=abcDEF123_4",
            "https://user@www.youtube.com/watch?v=abcDEF123_4",
        )
        for url in invalid_urls:
            with self.subTest(url=url):
                self.assertEqual(parse_youtube_video_id(url), "")

    def test_ytsl_scheme_is_not_supported(self):
        self.assertEqual(adapter_provider("ytsl://abcDEF123_4"), "")
        self.assertEqual(detect_source_type("ytsl://abcDEF123_4"), "hls")


class M3uOptionsParsingTest(unittest.TestCase):
    """覆盖 EXTVLCOPT / KODIPROP / WAVEFLOW 三类指令的解析。"""

    def _parse(self, text: str):
        from m3u8_parser import parse_m3u
        return parse_m3u(text)

    def test_extvlcopt_referrer_falls_into_referer(self):
        m3u = (
            "#EXTM3U\n"
            "#EXTINF:-1,泉州新闻\n"
            "#EXTVLCOPT:http-referrer=https://www.qztv.cn/\n"
            "http://live.qztv.cn/live/news.m3u8\n"
        )
        chs = self._parse(m3u)
        self.assertEqual(len(chs), 1)
        self.assertEqual(chs[0]["referer"], "https://www.qztv.cn/")

    def test_extvlcopt_user_agent_falls_into_custom_ua(self):
        m3u = (
            "#EXTM3U\n"
            "#EXTINF:-1,A\n"
            "#EXTVLCOPT:http-user-agent=AcmeUA/1.0\n"
            "http://example.com/a.m3u8\n"
        )
        chs = self._parse(m3u)
        self.assertEqual(chs[0]["custom_ua"], "AcmeUA/1.0")

    def test_kodiprop_stream_headers_decode(self):
        m3u = (
            "#EXTM3U\n"
            "#EXTINF:-1,A\n"
            "#KODIPROP:inputstream.adaptive.stream_headers="
            "Referer=https%3A%2F%2Fwww.qztv.cn%2F&User-Agent=AcmeUA%2F1.0\n"
            "http://example.com/a.m3u8\n"
        )
        chs = self._parse(m3u)
        self.assertEqual(chs[0]["referer"], "https://www.qztv.cn/")
        self.assertEqual(chs[0]["custom_ua"], "AcmeUA/1.0")

    def test_waveflow_requires_proxy_sets_force_proxy(self):
        m3u = (
            "#EXTM3U\n"
            "#EXTINF:-1,A\n"
            "#WAVEFLOW:requires_proxy=1\n"
            "http://example.com/a.m3u8\n"
        )
        chs = self._parse(m3u)
        self.assertEqual(chs[0]["force_proxy"], 1)

    def test_header_value_strips_control_chars_to_avoid_injection(self):
        # \r/\n 在 splitlines 阶段就把 m3u 截成多行，这里直接调内部 helper
        # 来覆盖 _strip_header_value 的去控制字符行为。
        from m3u8_parser import _parse_extvlcopt, _parse_kodiprop
        out = _parse_extvlcopt("#EXTVLCOPT:http-referrer=https://x/\x00Evil: 1")
        self.assertEqual(out, {"referer": "https://x/Evil: 1"})
        # KODIPROP 拼装值里如果带了控制字符，也要被过滤
        out = _parse_kodiprop(
            "#KODIPROP:inputstream.adaptive.stream_headers="
            "Referer=https%3A%2F%2Fx%2F%00&User-Agent=AcmeUA"
        )
        self.assertEqual(out["referer"], "https://x/")
        self.assertEqual(out["custom_ua"], "AcmeUA")

    def test_options_only_attach_to_pending_extinf(self):
        # EXTVLCOPT 出现在两条 EXTINF 之间但已经落地后的间隙时不应污染下一条
        m3u = (
            "#EXTM3U\n"
            "#EXTINF:-1,A\n"
            "http://example.com/a.m3u8\n"
            "#EXTVLCOPT:http-referrer=https://orphan/\n"
            "#EXTINF:-1,B\n"
            "http://example.com/b.m3u8\n"
        )
        chs = self._parse(m3u)
        self.assertEqual(len(chs), 2)
        self.assertFalse(chs[0].get("referer"))
        self.assertFalse(chs[1].get("referer"))


if __name__ == "__main__":
    unittest.main()
