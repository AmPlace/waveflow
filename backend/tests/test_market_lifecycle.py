import asyncio
import os
import sys
import tempfile
import unittest
from unittest import mock


def _clear_modules():
    for name in list(sys.modules):
        if name in {"database", "market"}:
            sys.modules.pop(name, None)


class MarketLifecycleTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._old_db_path = os.environ.get("WAVEFLOW_DB_PATH")
        self._old_handle_secret = os.environ.get("WAVEFLOW_PROXY_HANDLE_SECRET")
        os.environ["WAVEFLOW_DB_PATH"] = os.path.join(self._tmpdir.name, "waveflow.db")
        os.environ["WAVEFLOW_PROXY_HANDLE_SECRET"] = "market-lifecycle-test-secret-32-bytes"
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

    async def _create_source(self, key, url):
        source_id = await self.db.create_market_source(
            name=key,
            url=url,
            source_key=key,
            enabled=1,
            allow_private=0,
        )
        return await self.db.get_market_source(source_id)

    @staticmethod
    def _source_payload(source, package_ids):
        public = {
            "id": source["id"],
            "source_key": source["source_key"],
            "name": source["name"],
            "url": source["url"],
            "enabled": True,
            "allow_private": False,
            "is_builtin": False,
        }
        market_doc = {
            "schema_version": 1,
            "market_version": "1",
            "updated_at": "2026-08-05T00:00:00Z",
            "_source": public,
        }
        packages = [
            {
                "id": package_id,
                "original_id": package_id,
                "name": package_id,
                "kind": "playlist",
                "version": "1.0.0",
                "supported_in_v1": True,
                "previewable": True,
                "importable": True,
                "_manifest_loaded": True,
                "market_url": source["url"],
                "market_source": public,
            }
            for package_id in package_ids
        ]
        return market_doc, packages

    async def test_single_source_refresh_preserves_other_source_packages(self):
        source_a = await self._create_source("source-a", "https://a.test/market.json")
        source_b = await self._create_source("source-b", "https://b.test/market.json")
        payloads = {
            source_a["id"]: self._source_payload(source_a, ["source-a::a1"]),
            source_b["id"]: self._source_payload(source_b, ["source-b::b1"]),
        }

        async def load_source(source):
            return payloads[source["id"]]

        with mock.patch.object(self.market, "ensure_market_sources", return_value=[source_a, source_b]), \
                mock.patch.object(self.market, "_load_source_packages", side_effect=load_source):
            await self.market.refresh_market()
            payloads[source_b["id"]] = self._source_payload(source_b, ["source-b::b2"])
            await self.market.refresh_market(source_id=source_b["id"])

        self.assertEqual(
            {item["id"] for item in self.market._market_cache["packages"]},
            {"source-a::a1", "source-b::b2"},
        )

    async def test_full_refresh_replaces_each_source_entry_without_cross_source_loss(self):
        source_a = await self._create_source("source-a", "https://a.test/market.json")
        source_b = await self._create_source("source-b", "https://b.test/market.json")
        payloads = {
            source_a["id"]: self._source_payload(source_a, ["source-a::a1"]),
            source_b["id"]: self._source_payload(source_b, ["source-b::b1"]),
        }

        async def load_source(source):
            return payloads[source["id"]]

        with mock.patch.object(self.market, "ensure_market_sources", return_value=[source_a, source_b]), \
                mock.patch.object(self.market, "_load_source_packages", side_effect=load_source) as loader:
            await self.market.refresh_market()
            payloads[source_a["id"]] = self._source_payload(source_a, ["source-a::a2"])
            payloads[source_b["id"]] = self._source_payload(source_b, ["source-b::b2"])
            await self.market.refresh_market()

        self.assertEqual(loader.await_count, 4)
        self.assertEqual(
            {item["id"] for item in self.market._market_cache["packages"]},
            {"source-a::a2", "source-b::b2"},
        )

    async def test_concurrent_refreshes_for_same_source_finish_in_invocation_order(self):
        source = await self._create_source("source-a", "https://a.test/market.json")
        first_entered = asyncio.Event()
        release_first = asyncio.Event()
        load_count = 0
        active = 0
        max_active = 0

        async def load_source(_source):
            nonlocal load_count, active, max_active
            load_count += 1
            active += 1
            max_active = max(max_active, active)
            if load_count == 1:
                first_entered.set()
                await release_first.wait()
                result = self._source_payload(source, ["source-a::old"])
            else:
                result = self._source_payload(source, ["source-a::new"])
            active -= 1
            return result

        with mock.patch.object(self.market, "ensure_market_sources", return_value=[source]), \
                mock.patch.object(self.market, "_load_source_packages", side_effect=load_source):
            first = asyncio.create_task(self.market.refresh_market(source_id=source["id"]))
            await first_entered.wait()
            second = asyncio.create_task(self.market.refresh_market(source_id=source["id"]))
            await asyncio.sleep(0)
            release_first.set()
            await asyncio.gather(first, second)

        self.assertEqual(load_count, 2)
        self.assertEqual(max_active, 1)
        self.assertEqual(
            [item["id"] for item in self.market._market_cache["packages"]],
            ["source-a::new"],
        )

    async def test_refresh_completion_after_source_url_change_is_discarded(self):
        source = await self._create_source("source-a", "https://a.test/market.json")
        entered = asyncio.Event()
        release = asyncio.Event()

        async def load_source(_source):
            entered.set()
            await release.wait()
            return self._source_payload(source, ["source-a::old"])

        with mock.patch.object(self.market, "ensure_market_sources", return_value=[source]), \
                mock.patch.object(self.market, "_load_source_packages", side_effect=load_source):
            refresh = asyncio.create_task(self.market.refresh_market(source_id=source["id"]))
            await entered.wait()
            await self.market.update_source(source["id"], {"url": "https://new.test/market.json"})
            release.set()
            await refresh

        source_after = await self.db.get_market_source(source["id"])
        self.assertEqual(self.market._market_cache["packages"], [])
        self.assertEqual(source_after["last_status"], "")
        self.assertEqual(source_after["last_error"], "")

    async def test_partial_failure_retains_last_successful_source_cache(self):
        source_a = await self._create_source("source-a", "https://a.test/market.json")
        source_b = await self._create_source("source-b", "https://b.test/market.json")
        first = {
            source_a["id"]: self._source_payload(source_a, ["source-a::a1"]),
            source_b["id"]: self._source_payload(source_b, ["source-b::b1"]),
        }

        async def initial_load(source):
            return first[source["id"]]

        with mock.patch.object(self.market, "ensure_market_sources", return_value=[source_a, source_b]), \
                mock.patch.object(self.market, "_load_source_packages", side_effect=initial_load):
            await self.market.refresh_market()

        async def partial_load(source):
            if source["id"] == source_b["id"]:
                raise self.market.MarketError("temporary failure", 502)
            return self._source_payload(source_a, ["source-a::a2"])

        with mock.patch.object(self.market, "ensure_market_sources", return_value=[source_a, source_b]), \
                mock.patch.object(self.market, "_load_source_packages", side_effect=partial_load):
            summary = await self.market.refresh_market()

        self.assertEqual(
            {item["id"] for item in self.market._market_cache["packages"]},
            {"source-a::a2", "source-b::b1"},
        )
        self.assertTrue(summary["stale"])
        self.assertIn("temporary failure", summary["last_error"])
        source_b_after = await self.db.get_market_source(source_b["id"])
        self.assertEqual(source_b_after["last_status"], "error")
        self.assertIn("temporary failure", source_b_after["last_error"])

    async def test_first_source_failure_state_survives_unrelated_cache_rebuild(self):
        source_a = await self._create_source("source-a", "https://a.test/market.json")

        with mock.patch.object(self.market, "ensure_market_sources", return_value=[source_a]), \
                mock.patch.object(
                    self.market,
                    "_load_source_packages",
                    side_effect=self.market.MarketError("first fetch failed", 502),
                ):
            result = await self.market.refresh_market()

        self.assertEqual(result["refresh_status"], "failed")
        self.assertEqual(result["source_results"][0]["status"], "failed")
        self.assertFalse(result["source_results"][0]["usable_for_update"])

        self.assertTrue(self.market._market_cache["stale"])
        self.assertIn("first fetch failed", self.market._market_cache["last_error"])

        await self.market.create_source(
            "Source B",
            "https://b.test/market.json",
            enabled=False,
        )

        self.assertTrue(self.market._market_cache["stale"])
        self.assertIn("first fetch failed", self.market._market_cache["last_error"])

    async def test_source_disable_delete_and_url_change_update_cache_immediately(self):
        source = await self._create_source("source-a", "https://a.test/market.json")
        payload = self._source_payload(source, ["source-a::a1"])
        with mock.patch.object(self.market, "ensure_market_sources", return_value=[source]), \
                mock.patch.object(self.market, "_load_source_packages", return_value=payload):
            await self.market.refresh_market()

        await self.market.update_source(source["id"], {"enabled": False})
        self.assertEqual(self.market._market_cache["packages"], [])

        await self.market.update_source(source["id"], {
            "enabled": True,
            "url": "https://new.test/market.json",
        })
        self.assertEqual(self.market._market_cache["packages"], [])

        self.market._market_cache["packages"] = payload[1]
        await self.market.delete_source(source["id"])
        self.assertEqual(self.market._market_cache["packages"], [])

    async def test_source_create_and_metadata_update_keep_cache_consistent(self):
        created = await self.market.create_source(
            "Source A",
            "https://a.test/market.json",
        )
        self.assertIn(created["id"], {item["id"] for item in self.market._market_cache["sources"]})

        source = await self.db.get_market_source(created["id"])
        payload = self._source_payload(source, [f"{source['source_key']}::a1"])
        with mock.patch.object(self.market, "ensure_market_sources", return_value=[source]), \
                mock.patch.object(self.market, "_load_source_packages", return_value=payload):
            await self.market.refresh_market()

        await self.market.update_source(source["id"], {"name": "Renamed Source"})
        self.assertEqual(len(self.market._market_cache["packages"]), 1)
        self.assertEqual(
            self.market._market_cache["packages"][0]["market_source"]["name"],
            "Renamed Source",
        )

    def _installable_package(self, version="1.0.0", custom_ua="", package_id="pkg", requires_proxy=False):
        return {
            "id": package_id,
            "original_id": package_id,
            "name": "Package",
            "kind": "playlist",
            "version": version,
            "updated_at": "2026-08-05T00:00:00Z",
            "supported_in_v1": True,
            "previewable": True,
            "importable": True,
            "requires_proxy": requires_proxy,
            "custom_ua": custom_ua,
            "channel_sources": [],
            "defaults": {},
            "market_url": "https://market.test/market.json",
            "manifest_url": "",
            "_manifest_loaded": True,
        }

    def _set_preview(
        self,
        channels,
        version="1.0.0",
        custom_ua="",
        package_id="pkg",
        requires_proxy=False,
        preview_id="preview",
    ):
        package = self._installable_package(
            version=version,
            custom_ua=custom_ua,
            package_id=package_id,
            requires_proxy=requires_proxy,
        )
        self.market._market_cache["market"] = {"schema_version": 1}
        self.market._market_cache["packages"] = [package]
        self.market._preview_cache[preview_id] = {
            "preview_id": preview_id,
            "package": package,
            "all_channels": channels,
            "channel_count": len({item.get("name") for item in channels}),
            "source_count": len(channels),
            "warnings": [],
            "expires_at": 10**12,
        }
        return preview_id

    async def test_market_update_preserves_subscription_rows_and_source_ids(self):
        from security.source_ids import source_id_for

        initial = [
            {"name": "A", "url": "https://a.test/live.m3u8", "source_type": "hls"},
            {"name": "B", "url": "https://b.test/live.m3u8", "source_type": "hls"},
        ]
        preview_id = self._set_preview(initial, version="1.0.0")
        installed = await self.market.import_package("pkg", preview_id=preview_id)
        subscription_id = installed["subscription_id"]
        before_rows = await self.db.get_channels(subscription_id)
        before = {row["url"]: (row["id"], source_id_for(row)) for row in before_rows}

        updated = [
            {"name": "B renamed", "url": "https://b.test/live.m3u8", "source_type": "hls"},
            {"name": "A renamed", "url": "https://a.test/live.m3u8", "source_type": "hls"},
            {"name": "C", "url": "https://c.test/live.m3u8", "source_type": "hls"},
        ]
        preview_id = self._set_preview(updated, version="1.1.0")
        result = await self.market.import_package("pkg", preview_id=preview_id, reinstall=True)
        after_rows = await self.db.get_channels(result["subscription_id"])
        after = {row["url"]: (row["id"], source_id_for(row)) for row in after_rows}

        self.assertEqual(result["subscription_id"], subscription_id)
        self.assertEqual(after["https://a.test/live.m3u8"], before["https://a.test/live.m3u8"])
        self.assertEqual(after["https://b.test/live.m3u8"], before["https://b.test/live.m3u8"])
        self.assertNotIn(after["https://c.test/live.m3u8"][1], {item[1] for item in before.values()})

    async def test_market_update_removes_missing_source_and_preserves_survivor_after_reorder(self):
        from security.source_ids import source_id_for

        initial = [
            {"name": "A", "url": "https://a.test/live.m3u8", "source_type": "hls"},
            {"name": "B", "url": "https://b.test/live.m3u8", "source_type": "hls"},
        ]
        result = await self.market.import_package("pkg", preview_id=self._set_preview(initial))
        before_rows = await self.db.get_channels(result["subscription_id"])
        before = {row["url"]: (row["id"], source_id_for(row)) for row in before_rows}

        updated = [
            {"name": "C", "url": "https://c.test/live.m3u8", "source_type": "hls"},
            {"name": "B renamed", "url": "https://b.test/live.m3u8", "source_type": "hls"},
        ]
        await self.market.import_package(
            "pkg",
            preview_id=self._set_preview(updated, version="1.1.0"),
            reinstall=True,
        )
        after_rows = await self.db.get_channels(result["subscription_id"])
        after = {row["url"]: (row["id"], source_id_for(row)) for row in after_rows}

        self.assertNotIn("https://a.test/live.m3u8", after)
        self.assertEqual(after["https://b.test/live.m3u8"], before["https://b.test/live.m3u8"])
        self.assertNotIn(after["https://c.test/live.m3u8"][1], {item[1] for item in before.values()})

    async def test_auto_market_source_item_ids_do_not_break_row_identity_after_reorder(self):
        from security.source_ids import source_id_for

        package = {"id": "pkg"}
        channel_source = {"id": "inline"}
        channel = {"id": "channel", "name": "Channel"}
        source_defaults = {}

        def normalized(sources):
            rows = []
            for index, source in enumerate(sources):
                row, warning = self.market._normalize_source(
                    channel,
                    source,
                    package,
                    channel_source,
                    source_defaults,
                    index,
                )
                self.assertIsNone(warning)
                rows.append(row)
            return rows

        initial = normalized([
            {"url": "https://a.test/live.m3u8", "type": "hls"},
            {"url": "https://b.test/live.m3u8", "type": "hls"},
        ])
        subscription_id = await self.db.add_subscription(
            title="Package",
            url="market://pkg",
            channel_count=2,
        )
        await self.db.add_channels_bulk(subscription_id, initial)
        before_rows = await self.db.get_channels(subscription_id)
        before = {row["url"]: (row["id"], source_id_for(row)) for row in before_rows}

        reordered = normalized([
            {"url": "https://b.test/live.m3u8", "type": "hls"},
            {"url": "https://a.test/live.m3u8", "type": "hls"},
        ])
        await self.db.add_channels_bulk(subscription_id, reordered)
        after_rows = await self.db.get_channels(subscription_id)
        after = {row["url"]: (row["id"], source_id_for(row)) for row in after_rows}

        self.assertEqual(after, before)

    async def test_market_normalize_propagates_safe_rtsp_timestamp_mode(self):
        package = {"id": "pkg"}
        channel_source = {"id": "inline"}
        channel = {"id": "channel", "name": "Channel"}

        configured, warning = self.market._normalize_source(
            channel,
            {
                "url": "rtsp://configured.example/live",
                "type": "rtsp",
                "rtsp_timestamp_mode": "pts_from_dts",
            },
            package,
            channel_source,
            {},
            0,
        )
        self.assertIsNone(warning)
        self.assertEqual(configured["rtsp_timestamp_mode"], "pts_from_dts")

        invalid, warning = self.market._normalize_source(
            channel,
            {
                "url": "rtsp://invalid.example/live",
                "type": "rtsp",
                "rtsp_timestamp_mode": "-vf evil",
            },
            package,
            channel_source,
            {},
            1,
        )
        self.assertIsNone(warning)
        self.assertEqual(invalid["rtsp_timestamp_mode"], "passthrough")

    async def test_market_update_failure_rolls_back_subscription_install_and_channels(self):
        initial = [{"name": "A", "url": "https://a.test/live.m3u8", "source_type": "hls"}]
        preview_id = self._set_preview(initial, version="1.0.0")
        installed = await self.market.import_package("pkg", preview_id=preview_id)
        subscription_id = installed["subscription_id"]
        install_before = await self.db.get_market_install("pkg")
        rows_before = await self.db.get_channels(subscription_id)

        invalid = [{"name": "", "url": "https://invalid.test/live.m3u8", "source_type": "hls"}]
        preview_id = self._set_preview(invalid, version="1.1.0")
        with self.assertRaises(Exception):
            await self.market.import_package("pkg", preview_id=preview_id, reinstall=True)

        install_after = await self.db.get_market_install("pkg")
        rows_after = await self.db.get_channels(subscription_id)
        self.assertEqual(install_after["installed_subscription_id"], install_before["installed_subscription_id"])
        self.assertEqual(install_after["installed_version"], install_before["installed_version"])
        self.assertEqual(
            [(row["id"], row["name"], row["url"]) for row in rows_after],
            [(row["id"], row["name"], row["url"]) for row in rows_before],
        )

    async def test_update_aborts_after_uninstall_commits_during_preflight(self):
        initial = [{"name": "A", "url": "https://a.test/live.m3u8", "source_type": "hls"}]
        await self.market.import_package("pkg", preview_id=self._set_preview(initial, version="1.0.0"))

        preflight_entered = asyncio.Event()
        release_preflight = asyncio.Event()
        original_get_install = self.db.get_market_install
        first_call = True

        async def gated_get_install(package_id):
            nonlocal first_call
            install = await original_get_install(package_id)
            if first_call:
                first_call = False
                preflight_entered.set()
                await release_preflight.wait()
            return install

        with mock.patch.object(self.db, "get_market_install", side_effect=gated_get_install):
            pending_update = asyncio.create_task(self.market.update_installed_package("pkg"))
            await preflight_entered.wait()

            uninstall = await self.market.uninstall_package("pkg")
            self.assertTrue(uninstall["uninstalled"])

            release_preflight.set()
            with self.assertRaisesRegex(self.market.MarketError, "安装状态已变化"):
                await pending_update

        self.assertIsNone(await self.db.get_market_install("pkg"))

    async def test_update_installed_package_revalidates_and_applies_normal_update(self):
        initial = [{"name": "A", "url": "https://a.test/live.m3u8", "source_type": "hls"}]
        await self.market.import_package("pkg", preview_id=self._set_preview(initial, version="1.0.0"))

        updated = [{"name": "B", "url": "https://b.test/live.m3u8", "source_type": "hls"}]
        self._set_preview(updated, version="1.1.0")
        with mock.patch.object(
            self.market,
            "build_preview",
            new=mock.AsyncMock(return_value={"preview_id": "preview"}),
        ):
            result = await self.market.update_installed_package("pkg")

        self.assertEqual(result["channel_count"], 1)
        install = await self.db.get_market_install("pkg")
        self.assertEqual(install["installed_version"], "1.1.0")
        self.assertEqual(
            [row["url"] for row in await self.db.get_channels(result["subscription_id"])],
            ["https://b.test/live.m3u8"],
        )

    async def test_atomic_update_rolls_back_when_failure_occurs_after_channel_sync(self):
        initial = [{"name": "A", "url": "https://a.test/live.m3u8", "source_type": "hls"}]
        installed = await self.market.import_package("pkg", preview_id=self._set_preview(initial))
        subscription_id = installed["subscription_id"]
        install_before = await self.db.get_market_install("pkg")
        rows_before = await self.db.get_channels(subscription_id)
        original_sync = self.db._sync_channels_conn

        def fail_after_sync(conn, sub_id, prepared):
            original_sync(conn, sub_id, prepared)
            raise RuntimeError("fail after channel sync")

        updated = [{"name": "B", "url": "https://b.test/live.m3u8", "source_type": "hls"}]
        with mock.patch.object(self.db, "_sync_channels_conn", side_effect=fail_after_sync):
            with self.assertRaisesRegex(RuntimeError, "fail after channel sync"):
                await self.market.import_package(
                    "pkg",
                    preview_id=self._set_preview(updated, version="1.1.0"),
                    reinstall=True,
                )

        install_after = await self.db.get_market_install("pkg")
        rows_after = await self.db.get_channels(subscription_id)
        self.assertEqual(install_after["installed_version"], install_before["installed_version"])
        self.assertEqual(
            [(row["id"], row["name"], row["url"]) for row in rows_after],
            [(row["id"], row["name"], row["url"]) for row in rows_before],
        )

    async def test_child_source_custom_ua_is_not_promoted_to_subscription_default(self):
        channels = [
            {
                "name": "Needs UA",
                "url": "https://a.test/live.m3u8",
                "source_type": "hls",
                "custom_ua": "Source-UA/1",
            },
            {
                "name": "No UA",
                "url": "https://b.test/live.m3u8",
                "source_type": "hls",
                "custom_ua": "",
            },
        ]
        preview_id = self._set_preview(channels)
        result = await self.market.import_package("pkg", preview_id=preview_id)
        subscription = await self.db.get_subscription(result["subscription_id"])
        rows = {row["name"]: row for row in await self.db.get_channels(result["subscription_id"])}

        self.assertEqual(subscription["custom_ua"], "")
        self.assertEqual(rows["Needs UA"]["custom_ua"], "Source-UA/1")
        self.assertEqual(rows["No UA"]["custom_ua"], "")

    async def test_child_force_proxy_is_isolated_and_root_default_remains_explicit(self):
        channels = [
            {
                "name": "Proxy Child",
                "url": "https://a.test/live.m3u8",
                "source_type": "hls",
                "force_proxy": True,
            },
            {
                "name": "Direct Child",
                "url": "https://b.test/live.m3u8",
                "source_type": "hls",
                "force_proxy": False,
            },
        ]
        result = await self.market.import_package("pkg", preview_id=self._set_preview(channels))
        subscription = await self.db.get_subscription(result["subscription_id"])
        rows = {row["name"]: row for row in await self.db.get_channels(result["subscription_id"])}

        self.assertEqual(subscription["force_proxy"], 0)
        self.assertEqual(rows["Proxy Child"]["force_proxy"], 1)
        self.assertEqual(rows["Direct Child"]["force_proxy"], 0)

        await self.market.import_package(
            "pkg",
            preview_id=self._set_preview(channels, version="1.1.0", requires_proxy=True),
            reinstall=True,
        )
        subscription = await self.db.get_subscription(result["subscription_id"])
        self.assertEqual(subscription["force_proxy"], 1)

    async def test_empty_market_update_preserves_existing_install(self):
        initial = [{"name": "A", "url": "https://a.test/live.m3u8", "source_type": "hls"}]
        installed = await self.market.import_package("pkg", preview_id=self._set_preview(initial))
        subscription_id = installed["subscription_id"]
        install_before = await self.db.get_market_install("pkg")
        rows_before = await self.db.get_channels(subscription_id)

        with self.assertRaisesRegex(self.market.MarketError, "没有可导入"):
            await self.market.import_package(
                "pkg",
                preview_id=self._set_preview([], version="1.1.0"),
                reinstall=True,
            )

        install_after = await self.db.get_market_install("pkg")
        rows_after = await self.db.get_channels(subscription_id)
        self.assertEqual(install_after["installed_version"], install_before["installed_version"])
        self.assertEqual(
            [(row["id"], row["url"]) for row in rows_after],
            [(row["id"], row["url"]) for row in rows_before],
        )

    async def test_market_update_preserves_auto_update_and_atomically_advances_install_record(self):
        initial = [{"name": "A", "url": "https://a.test/live.m3u8", "source_type": "hls"}]
        installed = await self.market.import_package("pkg", preview_id=self._set_preview(initial))
        await self.db.update_market_install("pkg", auto_update=1)

        updated = [{"name": "A", "url": "https://a.test/live.m3u8", "source_type": "hls"}]
        await self.market.import_package(
            "pkg",
            preview_id=self._set_preview(updated, version="1.1.0"),
            reinstall=True,
        )
        install = await self.db.get_market_install("pkg")

        self.assertEqual(install["installed_subscription_id"], installed["subscription_id"])
        self.assertEqual(install["installed_version"], "1.1.0")
        self.assertEqual(install["auto_update"], 1)

    def test_version_status_is_explicit(self):
        status = self.market._version_status
        self.assertEqual(status("1.0.0", "1.0.0"), "same")
        self.assertEqual(status("1.1.0", "1.0.0"), "upgrade")
        self.assertEqual(status("1.0.0", "1.1.0"), "downgrade")
        self.assertEqual(status("nightly-b", "nightly-a"), "different")
        self.assertEqual(status("", "1.0.0"), "unknown")

    async def test_update_all_skips_same_version(self):
        package = self._installable_package(version="1.0.0")
        self.market._market_cache["market"] = {"schema_version": 1}
        self.market._market_cache["packages"] = [package]
        subscription_id = await self.db.add_subscription(
            title="Package", url="market://pkg", channel_count=1,
        )
        await self.db.add_channels_bulk(subscription_id, [
            {"name": "A", "url": "https://a.test/live.m3u8", "source_type": "hls"},
        ])
        await self.db.upsert_market_install(
            package_id="pkg",
            market_url=package["market_url"],
            installed_subscription_id=subscription_id,
            installed_version="1.0.0",
        )

        with mock.patch.object(self.market, "update_installed_package") as update:
            result = await self.market.run_installed_updates()

        update.assert_not_awaited()
        self.assertEqual(result["updated"], 0)
        self.assertEqual(result["skipped"], 1)

    async def test_update_all_only_updates_upgrade_versions(self):
        statuses = {
            "same": ("1.0.0", "1.0.0"),
            "upgrade": ("1.1.0", "1.0.0"),
            "downgrade": ("1.0.0", "1.1.0"),
            "different": ("nightly-b", "nightly-a"),
            "unknown": ("", "1.0.0"),
        }
        packages = []
        for package_id, (current, installed_version) in statuses.items():
            package = self._installable_package(version=current, package_id=package_id)
            packages.append(package)
            subscription_id = await self.db.add_subscription(
                title=package_id,
                url=f"market://{package_id}",
                channel_count=1,
            )
            await self.db.add_channels_bulk(subscription_id, [
                {"name": package_id, "url": f"https://{package_id}.test/live.m3u8", "source_type": "hls"},
            ])
            await self.db.upsert_market_install(
                package_id=package_id,
                market_url=package["market_url"],
                installed_subscription_id=subscription_id,
                installed_version=installed_version,
            )
        self.market._market_cache["market"] = {"schema_version": 1}
        self.market._market_cache["packages"] = packages

        with mock.patch.object(self.market, "update_installed_package", return_value={"subscription_id": 1}) as update:
            result = await self.market.run_installed_updates()

        update.assert_awaited_once_with("upgrade")
        self.assertEqual(result["updated"], 1)
        self.assertEqual(result["skipped"], 4)
        result_statuses = {
            item["package_id"]: item.get("version_status")
            for item in result["results"]
        }
        self.assertEqual(result_statuses["same"], "same")
        self.assertEqual(result_statuses["downgrade"], "downgrade")
        self.assertEqual(result_statuses["different"], "different")
        self.assertEqual(result_statuses["unknown"], "unknown")

    async def test_uninstall_removes_install_subscription_and_channels_atomically(self):
        channels = [{"name": "A", "url": "https://a.test/live.m3u8", "source_type": "hls"}]
        installed = await self.market.import_package("pkg", preview_id=self._set_preview(channels))
        subscription_id = installed["subscription_id"]

        result = await self.market.uninstall_package("pkg")

        self.assertTrue(result["uninstalled"])
        self.assertIsNone(await self.db.get_market_install("pkg"))
        self.assertIsNone(await self.db.get_subscription(subscription_id))
        self.assertEqual(await self.db.get_channels(subscription_id), [])

    async def test_concurrent_package_updates_are_serialized_per_package(self):
        channels = [{"name": "A", "url": "https://a.test/live.m3u8", "source_type": "hls"}]
        preview_id = self._set_preview(channels)
        entered = asyncio.Event()
        release = asyncio.Event()
        active = 0
        max_active = 0
        call_count = 0

        async def delayed_install(**_kwargs):
            nonlocal active, max_active, call_count
            call_count += 1
            active += 1
            max_active = max(max_active, active)
            if call_count == 1:
                entered.set()
                await release.wait()
            active -= 1
            return 1

        with mock.patch.object(self.db, "install_market_package_atomic", side_effect=delayed_install):
            first = asyncio.create_task(self.market.import_package("pkg", preview_id=preview_id))
            await entered.wait()
            second = asyncio.create_task(self.market.import_package("pkg", preview_id=preview_id))
            await asyncio.sleep(0)
            self.assertEqual(call_count, 1)
            release.set()
            await asyncio.gather(first, second)

        self.assertEqual(call_count, 2)
        self.assertEqual(max_active, 1)


if __name__ == "__main__":
    unittest.main()
