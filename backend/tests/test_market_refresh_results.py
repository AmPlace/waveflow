import asyncio
import json
import inspect
import os
import sys
import tempfile
import unittest
from unittest import mock


def _clear_modules():
    for name in list(sys.modules):
        if name in {"database", "market"}:
            sys.modules.pop(name, None)


class MarketRefreshResultsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._old_db_path = os.environ.get("WAVEFLOW_DB_PATH")
        self._old_handle_secret = os.environ.get("WAVEFLOW_PROXY_HANDLE_SECRET")
        os.environ["WAVEFLOW_DB_PATH"] = os.path.join(self._tmpdir.name, "waveflow.db")
        os.environ["WAVEFLOW_PROXY_HANDLE_SECRET"] = "market-refresh-results-test-secret"
        _clear_modules()

        import database as db
        import market

        self.db = db
        self.market = market
        await db.initialize()
        market._market_cache.clear()
        market._market_cache.update({
            "market_url": "",
            "market": None,
            "markets": [],
            "packages": [],
            "sources": [],
            "source_entries": {},
            "fetched_at": 0,
            "stale": False,
            "last_error": "",
            "allow_private": False,
        })
        market._preview_cache.clear()

    async def asyncTearDown(self):
        if self._old_db_path is None:
            os.environ.pop("WAVEFLOW_DB_PATH", None)
        else:
            os.environ["WAVEFLOW_DB_PATH"] = self._old_db_path
        if self._old_handle_secret is None:
            os.environ.pop("WAVEFLOW_PROXY_HANDLE_SECRET", None)
        else:
            os.environ["WAVEFLOW_PROXY_HANDLE_SECRET"] = self._old_handle_secret
        _clear_modules()
        self._tmpdir.cleanup()

    async def _create_source(self, key, url, *, enabled=True, allow_private=False, name=None):
        source_id = await self.db.create_market_source(
            name=name or key,
            url=url,
            source_key=key,
            enabled=1 if enabled else 0,
            allow_private=1 if allow_private else 0,
        )
        return await self.db.get_market_source(source_id)

    def _source_payload(self, source, raw_package_ids):
        public = self.market._source_public(source)
        market_doc = {
            "schema_version": 1,
            "market_version": "1",
            "updated_at": "2026-08-05T00:00:00Z",
            "_source": public,
        }
        packages = []
        for raw_id in raw_package_ids:
            package = {
                "id": raw_id,
                "name": raw_id,
                "kind": "playlist",
                "version": "1.0.0",
                "supported_in_v1": True,
                "previewable": True,
                "importable": True,
                "_manifest_loaded": True,
                "market_url": source["url"],
            }
            packages.append(self.market._attach_source(package, source, raw_id))
        return market_doc, packages

    async def _refresh_with_payloads(self, sources, payloads, *, source_id=None):
        async def load_source(source):
            payload = payloads[source["id"]]
            if isinstance(payload, Exception):
                raise payload
            return payload

        with mock.patch.object(self.market, "ensure_market_sources", return_value=sources), \
                mock.patch.object(self.market, "_load_source_packages", side_effect=load_source):
            return await self.market.refresh_market(source_id=source_id)

    async def test_success_returns_source_result_and_preserves_summary_fields(self):
        source = await self._create_source("source-a", "https://a.test/market.json")
        result = await self._refresh_with_payloads(
            [source],
            {source["id"]: self._source_payload(source, ["pkg-a", "pkg-b"])},
        )

        self.assertEqual(result["refresh_status"], "success")
        self.assertEqual(result["package_count"], 2)
        self.assertEqual(result["enabled_source_count"], 1)
        self.assertEqual(len(result["source_results"]), 1)
        source_result = result["source_results"][0]
        self.assertEqual(source_result["source_id"], source["id"])
        self.assertEqual(source_result["source_key"], "source-a")
        self.assertEqual(source_result["source_name"], "source-a")
        self.assertEqual(source_result["requested_url"], source["url"])
        self.assertEqual(source_result["current_url"], source["url"])
        self.assertTrue(source_result["source_revision"])
        self.assertEqual(source_result["source_revision"], source_result["current_source_revision"])
        self.assertEqual(source_result["status"], "success")
        self.assertTrue(source_result["usable_for_update"])
        self.assertFalse(source_result["stale"])
        self.assertEqual(source_result["error"], "")
        self.assertTrue(source_result["cache_updated"])
        self.assertEqual(source_result["package_count"], 2)
        self.assertEqual(source_result["package_ids"], ["source-a::pkg-a", "source-a::pkg-b"])
        self.assertEqual(result["successful_source_ids"], [source["id"]])
        self.assertEqual(result["source_result_counts"]["success"], 1)

    async def test_failed_refresh_with_successful_cache_returns_stale(self):
        source = await self._create_source("source-a", "https://a.test/market.json")
        await self._refresh_with_payloads(
            [source],
            {source["id"]: self._source_payload(source, ["pkg-a"])},
        )

        result = await self._refresh_with_payloads(
            [source],
            {source["id"]: self.market.MarketError("temporary failure", 502)},
        )

        source_result = result["source_results"][0]
        self.assertEqual(result["refresh_status"], "failed")
        self.assertEqual(source_result["status"], "stale")
        self.assertFalse(source_result["usable_for_update"])
        self.assertTrue(source_result["stale"])
        self.assertFalse(source_result["cache_updated"])
        self.assertEqual(source_result["package_count"], 0)
        self.assertEqual(source_result["package_ids"], [])
        self.assertIn("temporary failure", source_result["error"])
        self.assertEqual([item["id"] for item in self.market._market_cache["packages"]], ["source-a::pkg-a"])
        self.assertEqual(result["stale_source_ids"], [source["id"]])

    async def test_first_failure_returns_failed_without_update_packages(self):
        source = await self._create_source("source-a", "https://a.test/market.json")
        result = await self._refresh_with_payloads(
            [source],
            {source["id"]: self.market.MarketError("first fetch failed", 502)},
        )

        source_result = result["source_results"][0]
        self.assertEqual(result["refresh_status"], "failed")
        self.assertEqual(source_result["status"], "failed")
        self.assertFalse(source_result["usable_for_update"])
        self.assertFalse(source_result["stale"])
        self.assertEqual(source_result["package_ids"], [])
        self.assertEqual(result["failed_source_ids"], [source["id"]])
        self.assertEqual(self.market._market_cache["packages"], [])

    async def test_url_change_discards_late_success_without_status_write(self):
        source = await self._create_source("source-a", "https://a.test/market.json")
        entered = asyncio.Event()
        release = asyncio.Event()

        async def load_source(_source):
            entered.set()
            await release.wait()
            return self._source_payload(source, ["old"])

        with mock.patch.object(self.market, "ensure_market_sources", return_value=[source]), \
                mock.patch.object(self.market, "_load_source_packages", side_effect=load_source):
            pending = asyncio.create_task(self.market.refresh_market(source_id=source["id"]))
            await entered.wait()
            await self.market.update_source(source["id"], {"url": "https://new.test/market.json"})
            release.set()
            result = await pending

        source_result = result["source_results"][0]
        self.assertEqual(result["refresh_status"], "failed")
        self.assertEqual(source_result["status"], "revision_discarded")
        self.assertFalse(source_result["usable_for_update"])
        self.assertFalse(source_result["cache_updated"])
        self.assertEqual(source_result["requested_url"], "https://a.test/market.json")
        self.assertEqual(source_result["current_url"], "https://new.test/market.json")
        self.assertNotEqual(source_result["source_revision"], source_result["current_source_revision"])
        source_after = await self.db.get_market_source(source["id"])
        self.assertEqual(source_after["last_status"], "")
        self.assertEqual(source_after["last_error"], "")
        self.assertEqual(self.market._market_cache["packages"], [])

    async def test_allow_private_revoke_invalidates_cache_and_rechecks_stale_package(self):
        source = await self._create_source(
            "source-private",
            "https://private-policy.test/market.json",
            allow_private=True,
        )
        package = {
            "id": "source-private::pkg",
            "original_id": "pkg",
            "name": "Private package",
            "kind": "playlist",
            "version": "1.0.0",
            "manifest_url": "manifest.json",
            "market_url": source["url"],
            "market_source": self.market._source_public(source),
            "_manifest_loaded": False,
            "_allow_private_fetch": True,
        }
        key = self.market._source_cache_key(source)
        self.market._source_entries()[key] = {
            "source_id": source["id"],
            "source_url": source["url"],
            "source_allow_private": True,
            "fetch_allow_private": True,
            "market": {"schema_version": 1},
            "packages": [package],
            "has_successful_cache": True,
        }
        self.market._rebuild_market_cache([source])
        self.assertEqual([item["id"] for item in self.market._market_cache["packages"]], [package["id"]])

        await self.market.update_source(source["id"], {"allow_private": False})

        self.assertNotIn(key, self.market._source_entries())
        self.assertEqual(self.market._market_cache["packages"], [])

        manifest = {
            "id": "pkg",
            "kind": "playlist",
            "version": "1.0.0",
            "channel_sources": [
                {"type": "inline_channels", "channels": []},
            ],
        }
        with mock.patch.object(
            self.market,
            "safe_http_fetch",
            new=mock.AsyncMock(return_value=(
                "https://private-policy.test/manifest.json",
                json.dumps(manifest),
                {},
            )),
        ) as fetch:
            stale_loaded = await self.market._resolve_package_manifest(package)

        self.assertFalse(fetch.await_args.kwargs["allow_private"])
        self.assertFalse(stale_loaded["_allow_private_fetch"])

        await self.market.update_source(source["id"], {"allow_private": True})
        self.assertTrue(await self.market._package_allow_private(package))

    async def test_deleted_or_disabled_source_discards_late_result(self):
        for mutation in ("delete", "disable"):
            with self.subTest(mutation=mutation):
                source = await self._create_source(
                    f"source-{mutation}",
                    f"https://{mutation}.test/market.json",
                )
                entered = asyncio.Event()
                release = asyncio.Event()

                async def load_source(_source):
                    entered.set()
                    await release.wait()
                    return self._source_payload(source, ["pkg"])

                with mock.patch.object(self.market, "ensure_market_sources", return_value=[source]), \
                        mock.patch.object(self.market, "_load_source_packages", side_effect=load_source):
                    pending = asyncio.create_task(self.market.refresh_market(source_id=source["id"]))
                    await entered.wait()
                    if mutation == "delete":
                        await self.market.delete_source(source["id"])
                    else:
                        await self.market.update_source(source["id"], {"enabled": False})
                    release.set()
                    result = await pending

                source_result = result["source_results"][0]
                self.assertEqual(source_result["status"], "revision_discarded")
                self.assertFalse(source_result["usable_for_update"])
                self.assertFalse(source_result["cache_updated"])

    async def test_disabled_single_source_is_explicitly_skipped(self):
        source = await self._create_source(
            "source-disabled",
            "https://disabled.test/market.json",
            enabled=False,
        )

        with mock.patch.object(self.market, "ensure_market_sources", return_value=[source]), \
                mock.patch.object(self.market, "_load_source_packages") as load_source:
            result = await self.market.refresh_market(source_id=source["id"])

        load_source.assert_not_called()
        self.assertEqual(result["refresh_status"], "success")
        self.assertEqual(result["source_results"][0]["status"], "disabled")
        self.assertFalse(result["source_results"][0]["usable_for_update"])
        self.assertEqual(result["source_result_counts"]["disabled"], 1)

    async def test_partial_refresh_keeps_success_usable_and_source_identity_distinct(self):
        official = await self._create_source("official", "https://official.test/market.json")
        third_party = await self._create_source("third-party", "https://third.test/market.json")
        stale_source = await self._create_source("stale-source", "https://stale.test/market.json")
        await self._refresh_with_payloads(
            [stale_source],
            {stale_source["id"]: self._source_payload(stale_source, ["shared"])},
            source_id=stale_source["id"],
        )

        result = await self._refresh_with_payloads(
            [official, third_party, stale_source],
            {
                official["id"]: self._source_payload(official, ["shared"]),
                third_party["id"]: self._source_payload(third_party, ["shared"]),
                stale_source["id"]: self.market.MarketError("stale failure", 502),
            },
        )

        by_key = {item["source_key"]: item for item in result["source_results"]}
        self.assertEqual(result["refresh_status"], "partial")
        self.assertTrue(by_key["official"]["usable_for_update"])
        self.assertTrue(by_key["third-party"]["usable_for_update"])
        self.assertEqual(by_key["official"]["package_ids"], ["shared"])
        self.assertEqual(by_key["third-party"]["package_ids"], ["third-party::shared"])
        self.assertEqual(by_key["stale-source"]["status"], "stale")
        self.assertFalse(by_key["stale-source"]["usable_for_update"])
        self.assertEqual(result["source_result_counts"]["success"], 2)
        self.assertEqual(result["source_result_counts"]["stale"], 1)
        self.assertEqual(
            {item["id"] for item in self.market._market_cache["packages"]},
            {"shared", "third-party::shared", "stale-source::shared"},
        )

    async def test_single_source_failure_does_not_affect_other_source_cache_or_results(self):
        source_a = await self._create_source("source-a", "https://a.test/market.json")
        source_b = await self._create_source("source-b", "https://b.test/market.json")
        await self._refresh_with_payloads(
            [source_a, source_b],
            {
                source_a["id"]: self._source_payload(source_a, ["a"]),
                source_b["id"]: self._source_payload(source_b, ["b"]),
            },
        )

        result = await self._refresh_with_payloads(
            [source_a, source_b],
            {source_a["id"]: self.market.MarketError("source-a failed", 502)},
            source_id=source_a["id"],
        )

        self.assertEqual(len(result["source_results"]), 1)
        self.assertEqual(result["source_results"][0]["source_id"], source_a["id"])
        self.assertEqual(
            {item["id"] for item in self.market._market_cache["packages"]},
            {"source-a::a", "source-b::b"},
        )

    async def test_concurrent_calls_receive_only_their_own_source_results(self):
        source_a = await self._create_source("source-a", "https://a.test/market.json")
        source_b = await self._create_source("source-b", "https://b.test/market.json")
        release_a = asyncio.Event()
        release_b = asyncio.Event()

        async def load_source(source):
            if source["id"] == source_a["id"]:
                await release_a.wait()
            else:
                await release_b.wait()
            return self._source_payload(source, ["pkg"])

        with mock.patch.object(self.market, "ensure_market_sources", return_value=[source_a, source_b]), \
                mock.patch.object(self.market, "_load_source_packages", side_effect=load_source):
            task_a = asyncio.create_task(self.market.refresh_market(source_id=source_a["id"]))
            task_b = asyncio.create_task(self.market.refresh_market(source_id=source_b["id"]))
            await asyncio.sleep(0)
            release_b.set()
            result_b = await task_b
            release_a.set()
            result_a = await task_a

        self.assertEqual([item["source_id"] for item in result_a["source_results"]], [source_a["id"]])
        self.assertEqual([item["source_id"] for item in result_b["source_results"]], [source_b["id"]])

    async def test_same_source_refreshes_remain_serial_and_return_independent_results(self):
        source = await self._create_source("source-a", "https://a.test/market.json")
        first_entered = asyncio.Event()
        release_first = asyncio.Event()
        active = 0
        max_active = 0
        load_count = 0

        async def load_source(_source):
            nonlocal active, max_active, load_count
            load_count += 1
            active += 1
            max_active = max(max_active, active)
            if load_count == 1:
                first_entered.set()
                await release_first.wait()
                raw_id = "first"
            else:
                raw_id = "second"
            active -= 1
            return self._source_payload(source, [raw_id])

        with mock.patch.object(self.market, "ensure_market_sources", return_value=[source]), \
                mock.patch.object(self.market, "_load_source_packages", side_effect=load_source):
            first = asyncio.create_task(self.market.refresh_market(source_id=source["id"]))
            await first_entered.wait()
            second = asyncio.create_task(self.market.refresh_market(source_id=source["id"]))
            await asyncio.sleep(0)
            release_first.set()
            first_result, second_result = await asyncio.gather(first, second)

        self.assertEqual(max_active, 1)
        self.assertEqual(first_result["source_results"][0]["package_ids"], ["source-a::first"])
        self.assertEqual(second_result["source_results"][0]["package_ids"], ["source-a::second"])

    async def test_refresh_error_is_sanitized_and_limited(self):
        source = await self._create_source("source-a", "https://a.test/market.json")
        error = "/Users/private/project/secret.py\x00 " + ("remote-body " * 500)
        result = await self._refresh_with_payloads(
            [source],
            {source["id"]: self.market.MarketError(error, 502)},
        )

        returned_error = result["source_results"][0]["error"]
        self.assertNotIn("/Users/private/project/secret.py", returned_error)
        self.assertNotIn("\x00", returned_error)
        self.assertLessEqual(len(returned_error), 2048)
        source_after = await self.db.get_market_source(source["id"])
        self.assertEqual(source_after["last_error"], returned_error)

    def test_market_refresh_contract_has_no_automation_or_package_update_dependency(self):
        source = inspect.getsource(self.market.refresh_market)
        self.assertNotIn("AutomationRunner", source)
        self.assertNotIn("automation", source)
        self.assertNotIn("update_package", source)
        self.assertNotIn("install_package", source)


if __name__ == "__main__":
    unittest.main()
