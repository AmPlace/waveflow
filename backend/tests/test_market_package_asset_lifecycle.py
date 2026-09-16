import asyncio
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c6360f8cfc000000301010018dd8db00000000049454e44ae426082"
)


def _clear_market_modules() -> None:
    for name in (
        "database",
        "market",
        "logo_resolver",
        "iptv_channels",
        "routers.media_proxy",
    ):
        sys.modules.pop(name, None)


class MarketPackageAssetLifecycleTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = os.environ.get("WAVEFLOW_DB_PATH")
        self.old_assets = os.environ.get("WAVEFLOW_MARKET_ASSET_ROOT")
        os.environ["WAVEFLOW_DB_PATH"] = str(Path(self.tmp.name) / "waveflow.db")
        os.environ["WAVEFLOW_MARKET_ASSET_ROOT"] = str(Path(self.tmp.name) / "assets")
        _clear_market_modules()
        import database
        import market

        self.db = database
        self.market = market
        await self.db.initialize()
        self.asset_root = Path(self.tmp.name) / "package"
        (self.asset_root / "logos").mkdir(parents=True)
        (self.asset_root / "logos" / "fixture.png").write_bytes(PNG_1X1)
        self.market._market_cache.clear()
        self.market._market_cache.update({
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

    async def asyncTearDown(self):
        if self.old_db is None:
            os.environ.pop("WAVEFLOW_DB_PATH", None)
        else:
            os.environ["WAVEFLOW_DB_PATH"] = self.old_db
        if self.old_assets is None:
            os.environ.pop("WAVEFLOW_MARKET_ASSET_ROOT", None)
        else:
            os.environ["WAVEFLOW_MARKET_ASSET_ROOT"] = self.old_assets
        _clear_market_modules()
        self.tmp.cleanup()

    async def _source(self):
        source_id = await self.db.create_market_source(
            name="Fixture source",
            url="https://fixture.test/market.json",
            source_key="fixture",
            enabled=1,
            allow_private=0,
        )
        return await self.db.get_market_source(source_id)

    @staticmethod
    def _payload(source):
        public = {
            "id": source["id"],
            "source_key": source["source_key"],
            "name": source["name"],
            "url": source["url"],
            "enabled": True,
            "allow_private": False,
            "is_builtin": False,
        }
        package = {
            "id": "fixture::package",
            "original_id": "package",
            "name": "Fixture package",
            "kind": "playlist",
            "package_type": "content_package",
            "version": "1.0.0",
            "supported_in_v1": True,
            "previewable": True,
            "importable": True,
            "channel_sources": [],
            "defaults": {},
            "market_url": source["url"],
            "market_source": public,
            "_manifest_loaded": True,
        }
        return {
            "schema_version": 1,
            "market_version": "1",
            "updated_at": "2026-08-19T00:00:00Z",
            "_source": public,
        }, [package]

    async def test_installed_package_remains_projectable_after_restart_and_source_failure(self):
        source = await self._source()
        payload = self._payload(source)
        with mock.patch.object(self.market, "ensure_market_sources", return_value=[source]), \
                mock.patch.object(self.market, "_load_source_packages", return_value=payload):
            await self.market.refresh_market()

        subscription_id = await self.db.add_subscription(
            title="Fixture package", url="market://fixture::package", channel_count=1,
        )
        await self.db.upsert_market_install(
            package_id="fixture::package",
            market_url=source["url"],
            installed_subscription_id=subscription_id,
            installed_version="1.0.0",
            metadata_json=json.dumps({
                "name": "Fixture package",
                "kind": "playlist",
                "version": "1.0.0",
                "market_source": {"source_key": "fixture"},
            }),
        )

        _clear_market_modules()
        import database as restarted_db
        import market as restarted_market

        await restarted_db.initialize()
        restarted_source = await restarted_db.get_market_source(source["id"])
        with mock.patch.object(restarted_market, "ensure_market_sources", return_value=[restarted_source]), \
                mock.patch.object(
                    restarted_market,
                    "_load_source_packages",
                    side_effect=restarted_market.MarketError("source unavailable", 502),
                ):
            packages = await restarted_market.list_packages({})

        self.assertEqual([item["id"] for item in packages], ["fixture::package"])
        self.assertTrue(packages[0]["installed"])
        self.assertTrue(packages[0].get("catalog_unavailable"))

    async def test_content_install_rolls_back_when_logo_publication_fails(self):
        package = self.market._normalize_package({
            "id": "content::logo-failure",
            "name": "Content with Logo",
            "kind": "playlist",
            "package_type": "content_package",
            "version": "1.0.0",
            "assets": [{
                "asset_id": "fixture",
                "path": "logos/fixture.png",
                "media_type": "image/png",
                "sha256": hashlib.sha256(PNG_1X1).hexdigest(),
                "size": len(PNG_1X1),
            }],
            "channel_sources": [{
                "type": "inline_channels",
                "channels": [{
                    "name": "Fixture",
                    "logo_asset_id": "fixture",
                    "sources": [{"url": "https://fixture.test/live.m3u8"}],
                }],
            }],
            "_asset_root": str(self.asset_root),
            "_manifest_loaded": True,
        })
        self.market._market_cache.update({
            "packages": [package],
            "market": {"schema_version": 1},
            "sources": [],
        })
        preview = await self.market.build_preview(package["id"])
        original_replace = self.db._replace_package_logo_state_conn

        def fail_after_logo_state(*args, **kwargs):
            original_replace(*args, **kwargs)
            raise RuntimeError("logo publication failed")

        with mock.patch.object(
            self.db,
            "_replace_package_logo_state_conn",
            side_effect=fail_after_logo_state,
        ):
            with self.assertRaisesRegex(RuntimeError, "logo publication failed"):
                await self.market.import_package(package["id"], preview_id=preview["preview_id"])

        self.assertIsNone(await self.db.get_market_install(package["id"]))
        self.assertEqual(await self.db.list_market_installs(), [])

    async def test_asset_integrity_cache_is_scoped_to_stored_path(self):
        import routers.media_proxy as media_proxy

        digest_v1 = hashlib.sha256(PNG_1X1).hexdigest()
        package_v1 = {
            "package_id": "logo::cache",
            "asset_id": "fixture",
            "package_version": "1.0.0",
            "relative_path": "logos/fixture.png",
            "media_type": "image/png",
            "sha256": digest_v1,
            "size_bytes": len(PNG_1X1),
            "stored_path": str(Path(self.tmp.name) / "asset-v1.png"),
        }
        Path(package_v1["stored_path"]).write_bytes(PNG_1X1)
        await self.db.install_logo_package_atomic(
            package_id="logo::cache",
            market_url="fixture://market",
            installed_version="1.0.0",
            metadata_json='{"kind":"logo_pack"}',
            assets=[package_v1],
            bindings=[],
        )
        await media_proxy.package_asset("logo::cache", "fixture", None)
        old_stat = Path(package_v1["stored_path"]).stat()

        payload_v2 = bytearray(PNG_1X1)
        payload_v2[-10] ^= 1
        payload_v2 = bytes(payload_v2)
        package_v2 = {**package_v1,
                      "package_version": "2.0.0",
                      "sha256": hashlib.sha256(payload_v2).hexdigest(),
                      "stored_path": str(Path(self.tmp.name) / "asset-v2.png")}
        Path(package_v2["stored_path"]).write_bytes(payload_v2)
        os.utime(package_v2["stored_path"], ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
        await self.db.install_logo_package_atomic(
            package_id="logo::cache",
            market_url="fixture://market",
            installed_version="2.0.0",
            metadata_json='{"kind":"logo_pack"}',
            assets=[package_v2],
            bindings=[],
        )

        response = await media_proxy.package_asset("logo::cache", "fixture", None)
        self.assertEqual(str(response.path), package_v2["stored_path"])

    def _content_package(
        self,
        package_id: str,
        version: str,
        *,
        asset_id: str,
        payload: bytes = PNG_1X1,
        name: str = "Fixture",
    ) -> dict:
        relative_path = f"logos/{asset_id}.png"
        (self.asset_root / relative_path).write_bytes(payload)
        return self.market._normalize_package({
            "id": package_id,
            "name": name,
            "kind": "playlist",
            "package_type": "content_package",
            "version": version,
            "market_url": "https://market.test/index.json?token=fixture-secret",
            "market_source": {
                "id": 1,
                "source_key": "fixture",
                "name": "Fixture source",
                "url": "https://market.test/index.json?token=fixture-secret",
            },
            "assets": [{
                "asset_id": asset_id,
                "path": relative_path,
                "media_type": "image/png",
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size": len(payload),
            }],
            "channel_sources": [{
                "type": "inline_channels",
                "channels": [{
                    "name": name,
                    "sources": [{"url": f"https://fixture.test/{name}.m3u8"}],
                    "logo_asset_id": asset_id,
                }],
            }],
            "_asset_root": str(self.asset_root),
            "_manifest_loaded": True,
        })

    def _set_package(self, package: dict) -> None:
        self.market._market_cache.update({
            "packages": [package],
            "market": {"schema_version": 1},
            "sources": [],
        })

    async def test_content_update_replaces_asset_and_keeps_subscription_identity(self):
        package_v1 = self._content_package(
            "content::versioned", "1.0.0", asset_id="logo-a",
        )
        self._set_package(package_v1)
        await self.market.import_package(package_v1["id"])
        install_v1 = await self.db.get_market_install(package_v1["id"])
        channels_v1 = await self.db.get_channels(install_v1["installed_subscription_id"])
        asset_v1 = await self.db.get_active_package_asset(package_v1["id"], "logo-a")

        payload_v2 = bytes(bytearray(PNG_1X1[:-1]) + bytes([PNG_1X1[-1] ^ 1]))
        package_v2 = self._content_package(
            package_v1["id"], "2.0.0", asset_id="logo-b", payload=payload_v2,
        )
        self._set_package(package_v2)
        result = await self.market.update_installed_package(package_v1["id"])

        install_v2 = await self.db.get_market_install(package_v1["id"])
        channels_v2 = await self.db.get_channels(install_v1["installed_subscription_id"])
        asset_v2 = await self.db.get_active_package_asset(package_v1["id"], "logo-b")
        self.assertEqual(result["subscription_id"], install_v1["installed_subscription_id"])
        self.assertEqual(install_v2["installed_subscription_id"], install_v1["installed_subscription_id"])
        self.assertEqual(install_v2["installed_version"], "2.0.0")
        self.assertEqual(channels_v2[0]["id"], channels_v1[0]["id"])
        self.assertEqual(asset_v2["package_version"], "2.0.0")
        self.assertIsNone(await self.db.get_active_package_asset(package_v1["id"], "logo-a"))
        self.assertFalse(Path(asset_v1["stored_path"]).exists())

    async def test_binding_replace_failure_preserves_previous_binding(self):
        logical_id = "lc_binding"
        conn = self.db._connect()
        try:
            with conn:
                now = self.db._utc_now()
                conn.execute(
                    "INSERT INTO iptv_logical_channels(id, canonical_key, display_name, status, created_at, updated_at) VALUES(?, ?, ?, 'active', ?, ?)",
                    (logical_id, "Fixture", "Fixture", now, now),
                )
        finally:
            conn.close()

        asset = {
            "package_id": "logo::binding",
            "asset_id": "logo",
            "package_version": "1.0.0",
            "relative_path": "logos/fixture.png",
            "media_type": "image/png",
            "sha256": hashlib.sha256(PNG_1X1).hexdigest(),
            "size_bytes": len(PNG_1X1),
            "stored_path": str(self.tmp.name + "/binding.png"),
        }
        Path(asset["stored_path"]).write_bytes(PNG_1X1)
        binding = {
            "logical_channel_id": logical_id,
            "asset_id": "logo",
            "binding_type": "logo_pack",
            "match_type": "stable_identity",
            "match_key": "Fixture",
            "priority": 1,
        }
        await self.db.install_logo_package_atomic(
            package_id="logo::binding",
            market_url="fixture://market",
            installed_version="1.0.0",
            metadata_json='{"kind":"logo_pack"}',
            assets=[asset],
            bindings=[binding],
        )

        invalid_binding = {**binding, "logical_channel_id": "missing-logical"}
        with self.assertRaises(Exception):
            await self.db.replace_package_logo_bindings_atomic(
                "logo::binding", "1.0.0", [invalid_binding],
            )
        bindings = await self.db.get_logical_channel_logo_bindings(logical_id)
        self.assertEqual([(item["asset_id"], item["package_version"]) for item in bindings], [("logo", "1.0.0")])

    async def test_uninstall_does_not_delete_user_owned_subscription(self):
        subscription_id = await self.db.add_subscription(
            "User subscription", "fixture://user-owned",
        )
        await self.db.add_channels_bulk(subscription_id, [{
            "name": "User channel", "url": "https://fixture.test/user.m3u8",
        }])
        await self.db.upsert_market_install(
            package_id="content::foreign",
            market_url="fixture://market",
            installed_subscription_id=subscription_id,
            installed_version="1.0.0",
            metadata_json='{"kind":"playlist"}',
        )

        result = await self.market.uninstall_package("content::foreign")

        self.assertTrue(result["uninstalled"])
        self.assertIsNone(await self.db.get_market_install("content::foreign"))
        self.assertIsNotNone(await self.db.get_subscription(subscription_id))
        self.assertEqual(len(await self.db.get_channels(subscription_id)), 1)

    async def test_uninstall_does_not_delete_asset_path_still_owned_by_another_package(self):
        shared_path = Path(self.tmp.name) / "shared-logo.png"
        shared_path.write_bytes(PNG_1X1)
        digest = hashlib.sha256(PNG_1X1).hexdigest()

        def asset(package_id: str) -> dict:
            return {
                "package_id": package_id,
                "asset_id": "shared",
                "package_version": "1.0.0",
                "relative_path": "logos/shared.png",
                "media_type": "image/png",
                "sha256": digest,
                "size_bytes": len(PNG_1X1),
                "stored_path": str(shared_path),
            }

        await self.db.install_logo_package_atomic(
            package_id="logo::owner-a",
            market_url="fixture://market",
            installed_version="1.0.0",
            metadata_json='{"kind":"logo_pack"}',
            assets=[asset("logo::owner-a")],
            bindings=[],
        )
        await self.db.install_logo_package_atomic(
            package_id="logo::owner-b",
            market_url="fixture://market",
            installed_version="1.0.0",
            metadata_json='{"kind":"logo_pack"}',
            assets=[asset("logo::owner-b")],
            bindings=[],
        )

        self.assertTrue((await self.market.uninstall_package("logo::owner-a"))["uninstalled"])
        self.assertTrue(shared_path.exists())
        self.assertIsNotNone(await self.db.get_active_package_asset("logo::owner-b", "shared"))

    async def test_restart_snapshot_drops_url_query_parameters(self):
        package = self._content_package(
            "content::snapshot", "1.0.0", asset_id="snapshot-logo",
        )
        snapshot = self.market._market_package_snapshot(package)
        encoded = json.dumps(snapshot, ensure_ascii=False)
        self.assertNotIn("token=fixture-secret", encoded)
        self.assertNotIn("?token", str(snapshot.get("market_url") or ""))
        self.assertNotIn("url", snapshot.get("market_source") or {})

    async def test_install_persistence_drops_market_url_query_parameters(self):
        package = self._content_package(
            "content::persisted-url", "1.0.0", asset_id="persisted-logo",
        )
        package["manifest_url"] = "https://market.test/manifest.json?token=fixture-secret"
        self._set_package(package)

        await self.market.import_package(package["id"])

        install = await self.db.get_market_install(package["id"])
        metadata = json.loads(install["metadata_json"])
        persisted = json.dumps(metadata, ensure_ascii=False)
        self.assertEqual(install["market_url"], "https://market.test/index.json")
        self.assertEqual(metadata["manifest_url"], "https://market.test/manifest.json")
        self.assertNotIn("fixture-secret", persisted)
        self.assertNotIn("url", metadata["market_source"])

    async def test_cancelled_asset_stage_cleans_worker_result(self):
        package = self._normalize_package_for_cancellation()
        started = asyncio.Event()
        release = asyncio.Event()
        original_to_thread = self.market.asyncio.to_thread

        async def delayed_worker(function, *args, **kwargs):
            started.set()
            await release.wait()
            return function(*args, **kwargs)

        async def delayed_to_thread(function, *args, **kwargs):
            return await delayed_worker(function, *args, **kwargs)

        try:
            with mock.patch.object(self.market.asyncio, "to_thread", delayed_to_thread):
                task = asyncio.create_task(self.market._stage_logo_assets(package))
                await started.wait()
                task.cancel()
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        finally:
            self.market.asyncio.to_thread = original_to_thread

        store_root = Path(os.environ["WAVEFLOW_MARKET_ASSET_ROOT"])
        self.assertFalse((store_root / ".staging").exists())
        self.assertEqual(list(store_root.glob("**/*")), [])

    def _normalize_package_for_cancellation(self) -> dict:
        package = self._content_package(
            "content::cancel", "1.0.0", asset_id="cancel-logo",
        )
        return package


if __name__ == "__main__":
    unittest.main()
