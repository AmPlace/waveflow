import asyncio
import unittest
from unittest import IsolatedAsyncioTestCase, mock


class ProbeSpeedRevisionPropagationTest(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import main

        self.main = main
        self.channel = {
            "id": 7,
            "name": "Channel",
            "url": "https://example.test/live.m3u8",
            "source_type": "hls",
            "custom_ua": "",
            "sub_custom_ua": "Subscription-UA",
            "force_proxy": 0,
            "sub_force_proxy": 0,
        }
        self.main._test_progress.clear()
        self.main._global_test_progress.clear()

    async def asyncTearDown(self):
        self.main._test_progress.clear()
        self.main._global_test_progress.clear()

    def _result(self):
        return {"probe_status": "online", "live_status": "live", "probe_method": "http_stream"}

    async def test_subscription_speed_test_writes_with_captured_revision(self):
        from security.source_ids import source_revision_for

        sub_id = 23
        self.main._test_progress[sub_id] = self.main._empty_test_progress(1)
        self.main._global_test_progress.update(self.main._empty_test_progress(1))
        updates = mock.AsyncMock()
        with mock.patch.object(
            self.main, "probe_channel_source", new=mock.AsyncMock(return_value=self._result())
        ), mock.patch.object(
            self.main.db, "update_channel_probe_result", new=updates
        ), mock.patch.object(
            self.main.db, "update_subscription", new=mock.AsyncMock()
        ), mock.patch.object(
            self.main, "_finish_speed_test", new=mock.AsyncMock()
        ):
            await self.main._run_speed_test_sub(sub_id, [self.channel], asyncio.Event())

        updates.assert_awaited_once_with(
            7,
            self._result(),
            expected_source_revision=source_revision_for(self.channel),
        )

    async def test_global_speed_test_writes_with_captured_revision(self):
        from security.source_ids import source_revision_for

        self.main._global_test_progress.update(self.main._empty_test_progress(1))
        updates = mock.AsyncMock()
        with mock.patch.object(
            self.main, "probe_channel_source", new=mock.AsyncMock(return_value=self._result())
        ), mock.patch.object(
            self.main.db, "update_channel_probe_result", new=updates
        ), mock.patch.object(
            self.main.db, "update_subscription", new=mock.AsyncMock()
        ), mock.patch.object(
            self.main.db, "get_subscriptions", new=mock.AsyncMock(return_value=[])
        ), mock.patch.object(
            self.main, "_finish_speed_test", new=mock.AsyncMock()
        ):
            await self.main._run_speed_test_global([self.channel], asyncio.Event())

        updates.assert_awaited_once_with(
            7,
            self._result(),
            expected_source_revision=source_revision_for(self.channel),
        )

    async def test_unexpected_probe_exception_uses_stable_error(self):
        sub_id = 24
        self.main._test_progress[sub_id] = self.main._empty_test_progress(1)
        self.main._global_test_progress.update(self.main._empty_test_progress(1))
        updates = mock.AsyncMock()
        with mock.patch.object(
            self.main,
            "probe_channel_source",
            new=mock.AsyncMock(side_effect=RuntimeError("token=secret")),
        ), mock.patch.object(
            self.main.db, "update_channel_probe_result", new=updates
        ), mock.patch.object(
            self.main.db, "update_subscription", new=mock.AsyncMock()
        ), mock.patch.object(
            self.main, "_finish_speed_test", new=mock.AsyncMock()
        ):
            await self.main._run_speed_test_sub(sub_id, [self.channel], asyncio.Event())

        self.assertEqual(updates.await_args.args[1]["last_error"], "probe_internal_error")
        self.assertNotIn("secret", repr(updates.await_args))


if __name__ == "__main__":
    unittest.main()
