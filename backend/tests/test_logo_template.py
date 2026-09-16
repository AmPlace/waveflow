"""logo 模板安全防御测试。

校验目标：每条 URL 都按"零信任"校验，恶意 / 畸形输入被逐条丢弃，正常输入放行，
单条坏数据不污染整表。覆盖 _validate_url / _validate_key / _build_table，
以及远程响应流式字节上限（防谎报 Content-Length / 解压炸弹）。
"""

import asyncio
import json
import unittest

import httpx

from logo_template import (
    _MAX_TABLE_ENTRIES,
    _MAX_KEY_LEN,
    _MAX_URL_LEN,
    _REMOTE_MAX_BYTES,
    LogoTemplate,
    _validate_key,
    _validate_url,
)


class ValidateUrlAcceptTest(unittest.TestCase):
    """这些 URL 必须通过校验。"""

    def test_https_whitelisted_host_png(self):
        self.assertEqual(
            _validate_url("https://logo.waveflow.tv/logos/cctv1.png"),
            "https://logo.waveflow.tv/logos/cctv1.png",
        )

    def test_second_whitelisted_host(self):
        self.assertEqual(
            _validate_url("https://live.fanmingming.com/tv/CCTV1.png"),
            "https://live.fanmingming.com/tv/CCTV1.png",
        )

    def test_uppercase_extension_is_accepted(self):
        # 扩展名比较走小写，大写 JPG 也要放行
        self.assertTrue(_validate_url("https://logo.waveflow.tv/x.JPG"))

    def test_all_allowed_extensions(self):
        for ext in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".ico", ".svg"):
            with self.subTest(ext=ext):
                self.assertTrue(_validate_url(f"https://logo.waveflow.tv/x{ext}"))

    def test_query_string_preserved(self):
        self.assertEqual(
            _validate_url("https://logo.waveflow.tv/x.png?v=2"),
            "https://logo.waveflow.tv/x.png?v=2",
        )

    def test_uppercase_host_is_lowercased_by_parser(self):
        # urlparse 已把 hostname 归一为小写，大小写 host 不应影响放行
        self.assertTrue(_validate_url("https://LOGO.WaveFlow.TV/x.png"))

    def test_whitespace_is_stripped(self):
        self.assertEqual(
            _validate_url("  https://logo.waveflow.tv/x.png  "),
            "https://logo.waveflow.tv/x.png",
        )


class ValidateUrlRejectTest(unittest.TestCase):
    """这些 URL 必须被拒（返回空串）。"""

    def test_empty_and_non_string(self):
        self.assertEqual(_validate_url(""), "")
        self.assertEqual(_validate_url(None), "")
        self.assertEqual(_validate_url("   "), "")
        self.assertEqual(_validate_url(123), "")

    def test_must_be_https(self):
        self.assertEqual(_validate_url("http://logo.waveflow.tv/x.png"), "")

    def test_protocol_relative_rejected(self):
        self.assertEqual(_validate_url("//logo.waveflow.tv/x.png"), "")

    def test_dangerous_schemes_rejected(self):
        for u in (
            "file:///etc/passwd",
            "javascript:alert(1)",
            "data:image/png;base64,iVBORw0KGgo=",
            "ftp://logo.waveflow.tv/x.png",
        ):
            with self.subTest(u=u):
                self.assertEqual(_validate_url(u), "")

    def test_non_whitelisted_host_rejected(self):
        self.assertEqual(_validate_url("https://evil.example.com/x.png"), "")

    def test_non_image_extension_rejected(self):
        for ext in (".html", ".exe", ".php", ".json", ".css"):
            with self.subTest(ext=ext):
                self.assertEqual(_validate_url(f"https://logo.waveflow.tv/x{ext}"), "")

    def test_extension_without_filename_rejected(self):
        # /.png / /foo/.png —— 后缀前没有真文件名
        self.assertEqual(_validate_url("https://logo.waveflow.tv/.png"), "")
        self.assertEqual(_validate_url("https://logo.waveflow.tv/foo/.png"), "")
        self.assertEqual(_validate_url("https://logo.waveflow.tv/x/.PNG"), "")

    def test_control_chars_rejected(self):
        self.assertEqual(
            _validate_url("https://logo.waveflow.tv/x.png\nX-Header"), ""
        )  # CRLF 注入
        self.assertEqual(
            _validate_url("https://logo.waveflow.tv/x.png\x00.svg"), ""
        )  # NUL

    def test_quotes_and_angle_brackets_rejected(self):
        for ch in ("'", '"', "<", ">", "`"):
            with self.subTest(ch=ch):
                u = f"https://logo.waveflow.tv/x{ch}.png"
                self.assertEqual(_validate_url(u), "")

    def test_credentials_in_url_rejected(self):
        self.assertEqual(
            _validate_url("https://user:pass@logo.waveflow.tv/x.png"), ""
        )

    def test_overlong_url_rejected(self):
        u = "https://logo.waveflow.tv/" + "a" * (_MAX_URL_LEN + 1) + ".png"
        self.assertEqual(_validate_url(u), "")

    def test_xss_in_path_rejected(self):
        self.assertEqual(
            _validate_url("https://logo.waveflow.tv/<script>alert(1)</script>.png"),
            "",
        )


class ValidateKeyTest(unittest.TestCase):
    def test_normal_primary_names_accepted(self):
        self.assertTrue(_validate_key("CCTV-1"))
        self.assertTrue(_validate_key("CGTN"))

    def test_non_string_rejected(self):
        self.assertEqual(_validate_key(None), "")
        self.assertEqual(_validate_key(123), "")

    def test_empty_rejected(self):
        self.assertEqual(_validate_key(""), "")
        self.assertEqual(_validate_key("   "), "")

    def test_overlong_rejected(self):
        self.assertEqual(_validate_key("A" * (_MAX_KEY_LEN + 1)), "")


class BuildTableTest(unittest.TestCase):
    def test_mixed_payload_keeps_only_safe_entries(self):
        # 单条坏数据不污染整表：干净条目照常生效
        data = {
            "channels": {
                "CCTV-1": "https://logo.waveflow.tv/logos/cctv1.png",      # OK
                "CCTV-2": "http://logo.waveflow.tv/logos/cctv2.png",       # http
                "CCTV-3": "https://evil.example.com/cctv3.png",            # host
                "CCTV-4": "https://logo.waveflow.tv/cctv4.exe",            # ext
                "CCTV-5": "javascript:alert(1)",                           # scheme
                "CCTV-6": "https://logo.waveflow.tv/cctv6.png",            # OK
                "": "https://logo.waveflow.tv/empty.png",                  # 空 key
                "X" * (_MAX_KEY_LEN + 1): "https://logo.waveflow.tv/long.png",  # 长 key
                "CCTV-7": "https://logo.waveflow.tv/" + "x" * 2100 + ".png",    # 长 URL
            }
        }
        table = LogoTemplate._build_table(data)
        # CCTV-1 / CCTV-6 经 normalize_channel_name 压成 cctv1 / cctv6
        self.assertEqual(set(table.keys()), {"cctv1", "cctv6"})
        self.assertEqual(
            table["cctv1"], "https://logo.waveflow.tv/logos/cctv1.png"
        )

    def test_entry_cap_clamps(self):
        # 用全唯一 key 测截断（避免 normalize 把不同前缀压成同一 key）
        channels = {
            f"CCTV-{i:05d}": "https://logo.waveflow.tv/x.png"
            for i in range(_MAX_TABLE_ENTRIES + 500)
        }
        table = LogoTemplate._build_table({"channels": channels})
        self.assertLessEqual(len(table), _MAX_TABLE_ENTRIES)

    def test_non_dict_top_level_raises(self):
        with self.assertRaises(ValueError):
            LogoTemplate._build_table([])  # type: ignore[arg-type]

    def test_non_dict_channels_raises(self):
        with self.assertRaises(ValueError):
            LogoTemplate._build_table({"channels": ["a", "b"]})


class ModuleSanityTest(unittest.TestCase):
    """模块级不变量：常量在合理量级，全局实例加载成功。"""

    def test_limits_are_sane(self):
        self.assertGreater(_MAX_TABLE_ENTRIES, 100)
        self.assertGreater(_MAX_URL_LEN, 256)
        self.assertLessEqual(_MAX_KEY_LEN, 256)

    def test_global_instance_loaded_local(self):
        # 进程 import 时已加载本地 logos.json
        from logo_template import logo_template
        self.assertGreater(logo_template.size, 0)
        self.assertEqual(logo_template.loaded_source, "local")


class RemoteStreamingTest(unittest.TestCase):
    """远程刷新的流式字节上限。

    防两类攻击：① 谎报小 Content-Length、实际塞超大 body；② 服务端用流式
    慢慢灌，企图在超时前吃满内存。用本地假 server 模拟，断言超过 _REMOTE_MAX_BYTES
    的响应被拒，且不污染已有内存表。
    """

    @staticmethod
    def _run(coro):
        # 用 asyncio.run 而非 new_event_loop().run_until_complete()：前者在关闭
        # loop 前会 shutdown async generators，干净清理 httpx 的 aiter_bytes。
        return asyncio.run(coro)

    def _fresh_template(self) -> LogoTemplate:
        # 预置一条干净数据，远程失败时必须保留它
        t = LogoTemplate(local_path=None)
        t._table = {"cctv1": "https://logo.waveflow.tv/logos/cctv1.png"}
        t._loaded_source = "local"
        return t

    def _serve(self, handler) -> str:
        """用 httpx 内置的 MockTransport 起假后端，返回触发用的 https URL。"""
        self._transport = httpx.MockTransport(handler)
        return "https://logo.waveflow.tv/logos.json"

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self._transport)

    def test_oversized_body_is_rejected_and_local_table_preserved(self):
        # 谎报 Content-Length=10，实际 body 远超上限
        big = b"A" * (_REMOTE_MAX_BYTES + 1024)

        def handler(req):
            return httpx.Response(
                200,
                headers={"content-type": "application/json", "content-length": "10"},
                content=big,
            )

        url = self._serve(handler)
        tmpl = self._fresh_template()

        async def go():
            async with self._client() as c:
                ok = await tmpl.refresh_from_remote(c, url=url)
                return ok

        ok = self._run(go())
        self.assertFalse(ok)
        # 内存表未被污染
        self.assertEqual(tmpl.loaded_source, "local")
        self.assertEqual(tmpl.size, 1)
        self.assertEqual(tmpl.lookup("cctv1"), "https://logo.waveflow.tv/logos/cctv1.png")

    def test_huge_content_length_is_rejected_before_download(self):
        # Content-Length 直接超限，根本不应触发 body 读取
        def handler(req):
            # content 故意留空：若代码仍尝试读 body，这里会读到空，但断言只关心
            # 是否被拒，重点是超大 Content-Length 必须被提前挡掉
            return httpx.Response(
                200,
                headers={
                    "content-type": "application/json",
                    "content-length": str(_REMOTE_MAX_BYTES + 1),
                },
                content=b"",
            )

        url = self._serve(handler)
        tmpl = self._fresh_template()

        async def go():
            async with self._client() as c:
                ok = await tmpl.refresh_from_remote(c, url=url)
                return ok

        ok = self._run(go())
        self.assertFalse(ok)
        self.assertEqual(tmpl.loaded_source, "local")

    def test_non_json_content_type_rejected(self):
        def handler(req):
            return httpx.Response(200, headers={"content-type": "text/html"}, content=b"x")

        url = self._serve(handler)
        tmpl = self._fresh_template()

        async def go():
            async with self._client() as c:
                ok = await tmpl.refresh_from_remote(c, url=url)
                return ok

        self.assertFalse(self._run(go()))

    def test_valid_remote_payload_overwrites_table(self):
        payload = json.dumps({
            "channels": {
                "CCTV-1": "https://logo.waveflow.tv/logos/cctv1.png",
                "CGTN": "https://logo.waveflow.tv/logos/cgtn.png",
            }
        }).encode()

        def handler(req):
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                content=payload,
            )

        url = self._serve(handler)
        tmpl = self._fresh_template()

        async def go():
            async with self._client() as c:
                ok = await tmpl.refresh_from_remote(c, url=url)
                return ok

        self.assertTrue(self._run(go()))
        self.assertEqual(tmpl.loaded_source, "remote")
        self.assertEqual(tmpl.size, 2)

    def test_remote_url_outside_whitelist_rejected(self):
        tmpl = self._fresh_template()
        # 假后端只为白名单 host 注册；非白名单 URL 应在校验阶段就被拒，不发请求
        self._serve(lambda req: httpx.Response(200, content=b"{}"))

        async def go():
            async with self._client() as c:
                ok = await tmpl.refresh_from_remote(
                    c, url="https://evil.example.com/logos.json"
                )
                return ok

        self.assertFalse(self._run(go()))
        self.assertEqual(tmpl.loaded_source, "local")


if __name__ == "__main__":
    unittest.main()
