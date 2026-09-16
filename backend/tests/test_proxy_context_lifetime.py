import unittest

import security.proxy_context as pc
from security.proxy_handles import DEFAULT_TTL_BY_KIND


class ProxyContextTtlTest(unittest.TestCase):
    def test_default_ttl_covers_max_handle_ttl(self):
        max_handle_ttl = max(DEFAULT_TTL_BY_KIND.values())
        self.assertGreaterEqual(pc._DEFAULT_TTL, max_handle_ttl)

    def test_put_and_get_returns_same_object(self):
        pc.reset_for_tests()
        ctx_id = pc.get_registry().put(
            pc.ProxyContext(custom_ua="UA", referer="https://x/", source_id="cid")
        )
        got = pc.get_registry().get(ctx_id)
        self.assertIsNotNone(got)
        self.assertEqual(got.custom_ua, "UA")
        self.assertEqual(got.referer, "https://x/")
        # 默认 TTL 必须把 ctx 至少撑过 chunk handle 的 6h
        self.assertGreater(got.expires_at, 0)

    def test_source_revision_participates_in_fingerprint(self):
        pc.reset_for_tests()
        first = pc.get_registry().put(
            pc.ProxyContext(custom_ua="UA", referer="https://x/", source_id="src", source_revision="rev1")
        )
        second = pc.get_registry().put(
            pc.ProxyContext(custom_ua="UA", referer="https://x/", source_id="src", source_revision="rev2")
        )
        self.assertNotEqual(first, second)


if __name__ == "__main__":
    unittest.main()
