import os
import unittest
from unittest import mock


os.environ.setdefault("WAVEFLOW_PROXY_HANDLE_SECRET", "subscription-empty-test-secret-32-bytes")
os.environ.setdefault("WAVEFLOW_MODE", "nas")
os.environ.setdefault("WAVEFLOW_DB_PATH", ":memory:")


class RegularSubscriptionEmptyRefreshTest(unittest.IsolatedAsyncioTestCase):
    async def test_empty_refresh_keeps_existing_channels_and_marks_subscription_invalid(self):
        import main

        subscription = {
            "id": 42,
            "url": "https://example.test/list.m3u",
            "custom_ua": "",
        }
        document = main.parse_m3u_document("#EXTM3U\n")
        with mock.patch.object(main, "_fetch_subscription_document", return_value=document), \
                mock.patch.object(main.db, "begin_subscription_refresh", return_value=1), \
                mock.patch.object(main.db, "mark_subscription_invalid_if_current", return_value=True), \
                mock.patch.object(main.db, "add_channels_bulk") as add_channels, \
                mock.patch.object(main.db, "update_subscription") as update_subscription:
            with self.assertRaises(main.HTTPException) as raised:
                await main._refresh_regular_subscription(subscription)

        self.assertEqual(raised.exception.status_code, 502)
        add_channels.assert_not_awaited()
        update_subscription.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
