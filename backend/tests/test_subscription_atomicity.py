import asyncio
import os
import sys
import tempfile
import threading
import unittest
from unittest import mock


class SubscriptionAtomicityTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._old_db_path = os.environ.get("WAVEFLOW_DB_PATH")
        os.environ["WAVEFLOW_DB_PATH"] = os.path.join(self._tmpdir.name, "waveflow.db")
        sys.modules.pop("database", None)
        import database as db

        self.db = db
        await db.initialize()

    async def asyncTearDown(self):
        if self._old_db_path is None:
            os.environ.pop("WAVEFLOW_DB_PATH", None)
        else:
            os.environ["WAVEFLOW_DB_PATH"] = self._old_db_path
        sys.modules.pop("database", None)
        self._tmpdir.cleanup()

    @staticmethod
    def _channel(name: str, url: str) -> dict:
        return {"name": name, "url": url, "source_type": "hls"}

    async def _create_existing_subscription(self):
        sub_id = await self.db.add_subscription(
            title="existing",
            url="https://example.test/existing.m3u",
            channel_count=2,
        )
        await self.db.add_channels_bulk(sub_id, [
            self._channel("old-a", "https://old.example/a.m3u8"),
            self._channel("old-b", "https://old.example/b.m3u8"),
        ])
        return sub_id

    async def test_create_channel_failure_rolls_back_parent(self):
        original_sync = self.db._sync_channels_conn

        def write_then_fail(*args):
            original_sync(*args)
            raise RuntimeError("injected channel write failure")

        with mock.patch.object(
            self.db,
            "_sync_channels_conn",
            side_effect=write_then_fail,
        ):
            with self.assertRaisesRegex(RuntimeError, "channel write failure"):
                await self.db.add_subscription_with_channels(
                    title="new",
                    url="https://example.test/new.m3u",
                    channels=[self._channel("new", "https://new.example/live.m3u8")],
                )

        self.assertEqual(await self.db.get_subscriptions(), [])
        self.assertEqual(await self.db.get_channels(1), [])

    async def test_create_cancellation_inside_transaction_rolls_back_parent(self):
        original_sync = self.db._sync_channels_conn

        def write_then_cancel(*args):
            original_sync(*args)
            raise asyncio.CancelledError()

        with mock.patch.object(
            self.db,
            "_sync_channels_conn",
            side_effect=write_then_cancel,
        ):
            with self.assertRaises(asyncio.CancelledError):
                await self.db.add_subscription_with_channels(
                    title="cancelled",
                    url="https://example.test/cancelled.m3u",
                    channels=[self._channel("cancelled", "https://new.example/live.m3u8")],
                )

        self.assertEqual(await self.db.get_subscriptions(), [])

    async def test_caller_cancellation_during_create_transaction_leaves_no_parent(self):
        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        original_sync = self.db._sync_channels_conn

        def block_then_fail(*args):
            original_sync(*args)
            entered.set()
            try:
                release.wait(timeout=5)
                raise RuntimeError("injected cancellation rollback")
            finally:
                finished.set()

        with mock.patch.object(self.db, "_sync_channels_conn", side_effect=block_then_fail):
            operation = asyncio.create_task(
                self.db.add_subscription_with_channels(
                    title="cancelled",
                    url="https://example.test/cancelled-during-create.m3u",
                    channels=[self._channel("cancelled", "https://new.example/live.m3u8")],
                )
            )
            await asyncio.to_thread(entered.wait)
            operation.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await operation
            release.set()
            await asyncio.to_thread(finished.wait)

        self.assertEqual(await self.db.get_subscriptions(), [])

    async def test_refresh_channel_failure_keeps_previous_parent_and_channels(self):
        sub_id = await self._create_existing_subscription()
        before_parent = await self.db.get_subscription(sub_id)
        before_channels = await self.db.get_channels(sub_id)
        original_sync = self.db._sync_channels_conn

        def write_then_fail(*args):
            original_sync(*args)
            raise RuntimeError("injected refresh failure")

        with mock.patch.object(
            self.db,
            "_sync_channels_conn",
            side_effect=write_then_fail,
        ):
            with self.assertRaisesRegex(RuntimeError, "refresh failure"):
                await self.db.replace_subscription_channels_atomic(
                    sub_id,
                    [self._channel("new", "https://new.example/live.m3u8")],
                    valid=1,
                )

        self.assertEqual(await self.db.get_subscription(sub_id), before_parent)
        self.assertEqual(await self.db.get_channels(sub_id), before_channels)

    async def test_refresh_cancellation_inside_transaction_keeps_previous_state(self):
        sub_id = await self._create_existing_subscription()
        before_parent = await self.db.get_subscription(sub_id)
        before_channels = await self.db.get_channels(sub_id)
        original_sync = self.db._sync_channels_conn

        def write_then_cancel(*args):
            original_sync(*args)
            raise asyncio.CancelledError()

        with mock.patch.object(
            self.db,
            "_sync_channels_conn",
            side_effect=write_then_cancel,
        ):
            with self.assertRaises(asyncio.CancelledError):
                await self.db.replace_subscription_channels_atomic(
                    sub_id,
                    [self._channel("new", "https://new.example/live.m3u8")],
                    valid=1,
                )

        self.assertEqual(await self.db.get_subscription(sub_id), before_parent)
        self.assertEqual(await self.db.get_channels(sub_id), before_channels)

    async def test_caller_cancellation_during_refresh_transaction_keeps_previous_state(self):
        sub_id = await self._create_existing_subscription()
        before_parent = await self.db.get_subscription(sub_id)
        before_channels = await self.db.get_channels(sub_id)
        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        original_sync = self.db._sync_channels_conn

        def block_then_fail(*args):
            original_sync(*args)
            entered.set()
            try:
                release.wait(timeout=5)
                raise RuntimeError("injected refresh cancellation rollback")
            finally:
                finished.set()

        with mock.patch.object(self.db, "_sync_channels_conn", side_effect=block_then_fail):
            operation = asyncio.create_task(
                self.db.replace_subscription_channels_atomic(
                    sub_id,
                    [self._channel("new", "https://new.example/live.m3u8")],
                    valid=1,
                )
            )
            await asyncio.to_thread(entered.wait)
            operation.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await operation
            release.set()
            await asyncio.to_thread(finished.wait)

        self.assertEqual(await self.db.get_subscription(sub_id), before_parent)
        self.assertEqual(await self.db.get_channels(sub_id), before_channels)

    async def test_normal_create_refresh_and_empty_channel_set_semantics(self):
        sub_id = await self.db.add_subscription_with_channels(
            title="normal",
            url="https://example.test/normal.m3u",
            channels=[
                self._channel("one", "https://normal.example/one.m3u8"),
                self._channel("two", "https://normal.example/two.m3u8"),
            ],
        )
        created = await self.db.get_subscription(sub_id)
        self.assertEqual(created["channel_count"], 2)
        self.assertEqual(len(await self.db.get_channels(sub_id)), 2)

        await self.db.replace_subscription_channels_atomic(
            sub_id,
            [self._channel("replacement", "https://normal.example/replacement.m3u8")],
            valid=1,
        )
        refreshed = await self.db.get_subscription(sub_id)
        self.assertEqual(refreshed["valid"], 1)
        self.assertEqual(refreshed["channel_count"], 1)
        self.assertEqual(
            [row["name"] for row in await self.db.get_channels(sub_id)],
            ["replacement"],
        )

        # The persistence primitive continues to allow a deliberate empty set;
        # the HTTP refresh path keeps its existing empty-upstream semantics.
        await self.db.replace_subscription_channels_atomic(sub_id, [], valid=1)
        empty_parent = await self.db.get_subscription(sub_id)
        self.assertEqual(empty_parent["channel_count"], 0)
        self.assertEqual(await self.db.get_channels(sub_id), [])


if __name__ == "__main__":
    unittest.main()
