import asyncio
import importlib
import os
import sys
import tempfile
import unittest
from unittest import mock

import httpx

from infrastructure import http_client as subscription_http


os.environ.setdefault(
    "WAVEFLOW_PROXY_HANDLE_SECRET",
    "subscription-lifecycle-test-secret-32-bytes",
)
os.environ.setdefault("WAVEFLOW_MODE", "nas")


def _channel(name: str, url: str) -> dict:
    return {"name": name, "url": url, "source_type": "hls"}


class SubscriptionDatabaseLifecycleTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.previous_db_path = os.environ.get("WAVEFLOW_DB_PATH")
        os.environ["WAVEFLOW_DB_PATH"] = os.path.join(
            self.tmpdir.name,
            "waveflow.db",
        )
        sys.modules.pop("database", None)
        self.db = importlib.import_module("database")
        await self.db.initialize()

    async def asyncTearDown(self):
        if self.previous_db_path is None:
            os.environ.pop("WAVEFLOW_DB_PATH", None)
        else:
            os.environ["WAVEFLOW_DB_PATH"] = self.previous_db_path
        sys.modules.pop("database", None)
        self.tmpdir.cleanup()

    async def _seed(self) -> int:
        return await self.db.add_subscription_with_channels(
            title="Fixture",
            url="https://fixture.test/list.m3u",
            channels=[_channel("Old", "https://fixture.test/old.m3u8")],
        )

    async def test_success_failure_and_recovery_keep_last_good_snapshot(self):
        subscription_id = await self._seed()
        created = await self.db.get_subscription(subscription_id)
        self.assertEqual(created["last_refresh_status"], "success")
        self.assertTrue(created["last_attempt_at"])
        self.assertEqual(created["last_success_at"], created["last_attempt_at"])

        generation = await self.db.begin_subscription_refresh(subscription_id)
        running = await self.db.get_subscription(subscription_id)
        self.assertEqual(running["last_refresh_status"], "running")
        last_success_at = running["last_success_at"]

        updated = await self.db.mark_subscription_invalid_if_current(
            subscription_id,
            generation,
            status="timeout",
            error="订阅源请求超时",
        )
        self.assertTrue(updated)
        failed = await self.db.get_subscription(subscription_id)
        self.assertEqual(failed["valid"], 0)
        self.assertEqual(failed["last_refresh_status"], "timeout")
        self.assertEqual(failed["last_success_at"], last_success_at)
        self.assertEqual(failed["last_error"], "订阅源请求超时")
        self.assertEqual(
            [row["name"] for row in await self.db.get_channels(subscription_id)],
            ["Old"],
        )
        self.assertEqual(
            [row["name"] for row in await self.db.get_aggregated_channels()],
            ["Old"],
        )

        next_generation = await self.db.begin_subscription_refresh(subscription_id)
        recovered = await self.db.recover_interrupted_subscription_refreshes()
        self.assertEqual(recovered, 1)
        interrupted = await self.db.get_subscription(subscription_id)
        self.assertEqual(interrupted["refresh_generation"], next_generation)
        self.assertEqual(interrupted["last_refresh_status"], "interrupted")
        self.assertEqual(interrupted["last_success_at"], last_success_at)
        self.assertEqual(
            [row["name"] for row in await self.db.get_channels(subscription_id)],
            ["Old"],
        )

    async def test_newer_success_rejects_older_failure_and_config_snapshot(self):
        subscription_id = await self._seed()
        old_generation = await self.db.begin_subscription_refresh(subscription_id)
        new_generation = await self.db.begin_subscription_refresh(subscription_id)
        await self.db.replace_subscription_channels_atomic(
            subscription_id,
            [_channel("New", "https://fixture.test/new.m3u8")],
            valid=1,
            expected_generation=new_generation,
        )
        self.assertFalse(
            await self.db.mark_subscription_invalid_if_current(
                subscription_id,
                old_generation,
                status="failed",
                error="older failure",
            )
        )
        current = await self.db.get_subscription(subscription_id)
        self.assertEqual(current["last_refresh_status"], "success")
        self.assertEqual(current["valid"], 1)

        claimed_generation = await self.db.begin_subscription_refresh(subscription_id)
        await self.db.update_subscription(
            subscription_id,
            url="https://fixture.test/changed.m3u",
        )
        with self.assertRaises(self.db.SubscriptionRefreshSuperseded):
            await self.db.replace_subscription_channels_atomic(
                subscription_id,
                [_channel("Late", "https://fixture.test/late.m3u8")],
                valid=1,
                expected_generation=claimed_generation,
            )
        changed = await self.db.get_subscription(subscription_id)
        self.assertEqual(changed["last_refresh_status"], "config_changed")
        self.assertEqual(changed["valid"], 0)

    async def test_delete_during_refresh_rejects_late_commit(self):
        subscription_id = await self._seed()
        generation = await self.db.begin_subscription_refresh(subscription_id)
        await self.db.delete_subscription(subscription_id)

        with self.assertRaises(self.db.SubscriptionRefreshSuperseded):
            await self.db.replace_subscription_channels_atomic(
                subscription_id,
                [_channel("Late", "https://fixture.test/late.m3u8")],
                valid=1,
                expected_generation=generation,
            )
        self.assertIsNone(await self.db.get_subscription(subscription_id))

    async def test_market_snapshot_records_successful_subscription_state(self):
        subscription_id = await self.db.install_market_package_atomic(
            package_id="fixture-package",
            market_url="https://market.test/index.json",
            title="Market Fixture",
            subscription_url="market://fixture-package",
            channels=[_channel("Market", "https://source.test/market.m3u8")],
            installed_version="1.0.0",
        )

        state = await self.db.get_subscription(subscription_id)
        self.assertEqual(state["last_refresh_status"], "success")
        self.assertTrue(state["last_attempt_at"])
        self.assertEqual(state["last_success_at"], state["last_attempt_at"])


class SubscriptionRouteLifecycleTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.main = importlib.import_module("main")

    async def asyncSetUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.previous_db_path = os.environ.get("WAVEFLOW_DB_PATH")
        os.environ["WAVEFLOW_DB_PATH"] = os.path.join(
            self.tmpdir.name,
            "waveflow.db",
        )
        await self.main.db.initialize()

    async def asyncTearDown(self):
        if self.previous_db_path is None:
            os.environ.pop("WAVEFLOW_DB_PATH", None)
        else:
            os.environ["WAVEFLOW_DB_PATH"] = self.previous_db_path
        self.tmpdir.cleanup()

    async def test_fetch_uses_safe_streaming_boundary_and_bounded_body(self):
        content = (
                b"#EXTM3U\n#EXTINF:-1,Fixture\n"
                b"https://media.test/live.m3u8\n"
        )
        with mock.patch.object(
            self.main,
            "fetch_bytes",
            create=True,
            new=mock.AsyncMock(
                return_value=(
                    "https://source.test/list.m3u",
                    content,
                    httpx.Headers({"content-type": "audio/x-mpegurl; charset=utf-8"}),
                )
            ),
        ) as safe_fetch, mock.patch.object(
            self.main,
            "stream_with_safe_redirects",
            new=mock.AsyncMock(side_effect=AssertionError("media client must not fetch subscriptions")),
        ):
            document = await self.main._fetch_subscription_document(
                "https://source.test/list.m3u",
                custom_ua="Fixture-UA/1",
            )

        self.assertEqual(len(document.channels), 1)
        self.assertEqual(document.channels[0]["name"], "Fixture")
        self.assertEqual(
            safe_fetch.await_args.kwargs["headers"],
            {"User-Agent": "Fixture-UA/1"},
        )
        policy = safe_fetch.await_args.kwargs["policy"]
        self.assertTrue(policy.verify_tls)
        self.assertEqual(policy.connect_timeout, 10.0)
        self.assertEqual(policy.read_timeout, 15.0)
        self.assertEqual(policy.max_response_bytes, 50 * 1024 * 1024)
        self.assertEqual(
            self.main.SUBSCRIPTION_MAX_RESPONSE_BYTES,
            50 * 1024 * 1024,
        )

    async def test_fetch_has_an_overall_timeout_budget(self):
        async def slow_fetch(*_args, **_kwargs):
            await asyncio.sleep(1)

        with mock.patch.object(
            self.main,
            "SUBSCRIPTION_TOTAL_TIMEOUT_SECONDS",
            0.01,
        ), mock.patch.object(
            self.main,
            "fetch_bytes",
            new=slow_fetch,
        ):
            with self.assertRaises(self.main.SubscriptionFetchError) as raised:
                await self.main._fetch_subscription_document(
                    "https://source.test/list.m3u?token=fixture-secret"
                )

        self.assertEqual(raised.exception.code, "timeout")
        self.assertNotIn("fixture-secret", str(raised.exception))

    async def test_fetch_error_is_classified_without_secret_url(self):
        request = httpx.Request(
            "GET",
            "https://source.test/list.m3u?token=fixture-secret",
        )
        failure = httpx.ConnectTimeout(
            "connect https://source.test/list.m3u?token=fixture-secret",
            request=request,
        )
        with mock.patch.object(
            self.main,
            "fetch_bytes",
            create=True,
            new=mock.AsyncMock(side_effect=failure),
        ) as safe_fetch, mock.patch.object(
            self.main,
            "stream_with_safe_redirects",
            new=mock.AsyncMock(side_effect=failure),
        ):
            with self.assertRaises(self.main.SubscriptionFetchError) as raised:
                await self.main._fetch_subscription_document(str(request.url))

        self.assertEqual(raised.exception.code, "timeout")
        self.assertNotIn("fixture-secret", str(raised.exception))
        safe_fetch.assert_awaited_once()

    async def test_fetch_http_status_and_size_errors_are_sanitized(self):
        request = httpx.Request(
            "GET",
            "https://source.test/list.m3u?token=fixture-secret",
        )
        cases = [
            (
                httpx.HTTPStatusError(
                    "upstream body contains fixture-secret",
                    request=request,
                    response=httpx.Response(503, request=request),
                ),
                "http_error",
                "订阅源返回 HTTP 503",
            ),
            (
                subscription_http.ResponseTooLargeError(
                    "response body contains fixture-secret",
                ),
                "response_too_large",
                "订阅源内容超过允许大小",
            ),
        ]
        for failure, expected_code, expected_detail in cases:
            with self.subTest(expected_code=expected_code), mock.patch.object(
                self.main,
                "fetch_bytes",
                new=mock.AsyncMock(side_effect=failure),
            ):
                with self.assertRaises(self.main.SubscriptionFetchError) as raised:
                    await self.main._fetch_subscription_document(str(request.url))

            self.assertEqual(raised.exception.code, expected_code)
            self.assertEqual(raised.exception.detail, expected_detail)
            self.assertNotIn("fixture-secret", str(raised.exception))

    async def test_refresh_failure_persists_sanitized_state_and_keeps_api_stable(self):
        subscription = {
            "id": 42,
            "url": "https://source.test/list.m3u?token=fixture-secret",
            "custom_ua": "",
        }
        error = self.main.SubscriptionFetchError(
            "timeout",
            "订阅源请求超时",
        )
        with mock.patch.object(
            self.main,
            "_fetch_subscription_document",
            new=mock.AsyncMock(side_effect=error),
        ), mock.patch.object(
            self.main.db,
            "begin_subscription_refresh",
            new=mock.AsyncMock(return_value=3),
        ), mock.patch.object(
            self.main.db,
            "mark_subscription_invalid_if_current",
            new=mock.AsyncMock(return_value=True),
        ) as mark_failed:
            with self.assertRaises(self.main.HTTPException) as raised:
                await self.main._refresh_regular_subscription(subscription)

        self.assertEqual(raised.exception.status_code, 502)
        self.assertNotIn("fixture-secret", str(raised.exception.detail))
        self.assertEqual(
            mark_failed.await_args.kwargs,
            {"status": "timeout", "error": "订阅源请求超时"},
        )

    async def test_real_refresh_failure_keeps_last_good_channels_queryable(self):
        subscription_id = await self.main.db.add_subscription_with_channels(
            title="Fixture",
            url="https://source.test/list.m3u",
            channels=[_channel("Old", "https://source.test/old.m3u8")],
        )
        failure = self.main.SubscriptionFetchError(
            "http_error",
            "订阅源返回 HTTP 503",
        )
        with mock.patch.object(
            self.main,
            "_fetch_subscription_document",
            new=mock.AsyncMock(side_effect=failure),
        ):
            with self.assertRaises(self.main.HTTPException) as raised:
                await self.main._refresh_regular_subscription(
                    await self.main.db.get_subscription(subscription_id)
                )

        self.assertEqual(raised.exception.status_code, 502)
        self.assertEqual(
            [row["name"] for row in await self.main.db.get_aggregated_channels()],
            ["Old"],
        )
        state = await self.main.db.get_subscription(subscription_id)
        self.assertEqual(state["last_refresh_status"], "http_error")
        self.assertEqual(state["last_error"], "订阅源返回 HTTP 503")
        self.assertEqual(state["last_success_at"], state["created_at"])

    async def test_parse_failure_is_recorded_without_replacing_snapshot(self):
        subscription_id = await self.main.db.add_subscription_with_channels(
            title="Fixture",
            url="https://source.test/list.m3u",
            channels=[_channel("Old", "https://source.test/old.m3u8")],
        )
        with mock.patch.object(
            self.main,
            "_fetch_subscription_document",
            new=mock.AsyncMock(
                side_effect=self.main.SubscriptionFetchError(
                    "parse_failed",
                    "订阅源内容解析失败",
                )
            ),
        ):
            with self.assertRaises(self.main.HTTPException):
                await self.main._refresh_regular_subscription(
                    await self.main.db.get_subscription(subscription_id)
                )

        self.assertEqual(
            [row["name"] for row in await self.main.db.get_channels(subscription_id)],
            ["Old"],
        )
        self.assertEqual(
            (await self.main.db.get_subscription(subscription_id))["last_refresh_status"],
            "parse_failed",
        )

    async def test_cancelled_refresh_records_cancelled_and_propagates(self):
        subscription = {
            "id": 42,
            "url": "https://source.test/list.m3u",
            "custom_ua": "",
        }
        with mock.patch.object(
            self.main.db,
            "begin_subscription_refresh",
            new=mock.AsyncMock(return_value=7),
        ), mock.patch.object(
            self.main,
            "_fetch_subscription_document",
            new=mock.AsyncMock(side_effect=asyncio.CancelledError()),
        ), mock.patch.object(
            self.main.db,
            "mark_subscription_invalid_if_current",
            new=mock.AsyncMock(return_value=True),
        ) as mark_failed:
            with self.assertRaises(asyncio.CancelledError):
                await self.main._refresh_regular_subscription(subscription)

        mark_failed.assert_awaited_once_with(
            42,
            7,
            status="cancelled",
            error="订阅刷新已取消",
        )

    async def test_refresh_all_isolates_unexpected_failure_and_continues(self):
        subscriptions = [
            {"id": 1, "title": "Broken", "url": "https://one.test/list.m3u"},
            {"id": 2, "title": "Healthy", "url": "https://two.test/list.m3u"},
            {"id": 3, "title": "Rejected", "url": "https://three.test/list.m3u"},
        ]
        outcomes = [
            RuntimeError("fixture-secret-internal-detail"),
            {"channel_count": 2},
            self.main.HTTPException(status_code=502, detail="稳定上游错误"),
        ]
        with mock.patch.object(
            self.main.db,
            "get_subscriptions",
            new=mock.AsyncMock(return_value=subscriptions),
        ), mock.patch.object(
            self.main,
            "_refresh_regular_subscription",
            new=mock.AsyncMock(side_effect=outcomes),
        ), mock.patch(
            "core.cover_cache.invalidate_all_covers",
        ):
            result = await self.main.refresh_all_subscriptions()

        self.assertEqual((result["updated"], result["failed"]), (1, 2))
        self.assertEqual(len(result["results"]), 3)
        self.assertEqual(
            [item["status"] for item in result["results"]],
            ["failed", "updated", "failed"],
        )
        self.assertNotIn("fixture-secret", str(result))

    async def test_refresh_all_bounds_regular_concurrency_and_serializes_market(self):
        subscriptions = [
            {
                "id": index,
                "title": f"Regular {index}",
                "url": f"https://regular-{index}.test/list.m3u",
            }
            for index in range(1, 6)
        ] + [
            {"id": 6, "title": "Market 1", "url": "market://one"},
            {"id": 7, "title": "Market 2", "url": "market://two"},
        ]
        regular_active = 0
        regular_max = 0
        market_active = 0
        market_max = 0
        four_regular_started = asyncio.Event()
        release_regular = asyncio.Event()

        async def refresh_regular(_subscription):
            nonlocal regular_active, regular_max
            regular_active += 1
            regular_max = max(regular_max, regular_active)
            if regular_active == 4:
                four_regular_started.set()
            try:
                await release_regular.wait()
                return {"channel_count": 1}
            finally:
                regular_active -= 1

        async def refresh_market(_subscription):
            nonlocal market_active, market_max
            market_active += 1
            market_max = max(market_max, market_active)
            try:
                await asyncio.sleep(0)
                return {"channel_count": 1}
            finally:
                market_active -= 1

        with mock.patch.object(
            self.main.db,
            "get_subscriptions",
            new=mock.AsyncMock(return_value=subscriptions),
        ), mock.patch.object(
            self.main,
            "_refresh_regular_subscription",
            new=refresh_regular,
        ), mock.patch.object(
            self.main,
            "_refresh_market_subscription",
            new=refresh_market,
        ), mock.patch(
            "core.cover_cache.invalidate_all_covers",
        ):
            refresh_task = asyncio.create_task(
                self.main.refresh_all_subscriptions()
            )
            try:
                await asyncio.wait_for(four_regular_started.wait(), timeout=1)
            except TimeoutError:
                release_regular.set()
                await refresh_task
                raise
            release_regular.set()
            result = await refresh_task

        self.assertEqual(regular_max, 4)
        self.assertEqual(market_max, 1)
        self.assertEqual((result["updated"], result["failed"]), (7, 0))
        self.assertEqual(
            [item["subscription_id"] for item in result["results"]],
            list(range(1, 8)),
        )


if __name__ == "__main__":
    unittest.main()
