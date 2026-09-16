from __future__ import annotations

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


def _clear_modules() -> None:
    for name in ("database", "market", "logo_resolver"):
        sys.modules.pop(name, None)


class LogoPackageTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = os.environ.get("WAVEFLOW_DB_PATH")
        self.old_assets = os.environ.get("WAVEFLOW_MARKET_ASSET_ROOT")
        os.environ["WAVEFLOW_DB_PATH"] = str(Path(self.tmp.name) / "waveflow.db")
        os.environ["WAVEFLOW_MARKET_ASSET_ROOT"] = str(Path(self.tmp.name) / "asset-store")
        _clear_modules()
        import database
        import iptv_channels
        import market
        from logo_resolver import logo_resolver

        self.db = database
        self.iptv_channels = iptv_channels
        self.market = market
        self.logo_resolver = logo_resolver
        await self.db.initialize()
        sub = await self.db.add_subscription("Logo fixture", "fixture://logo")
        await self.db.add_channels_bulk(sub, [{"name": "CCTV5", "url": "https://example.test/cctv5.m3u8"}])
        await self.iptv_channels.sync_iptv_logical_channels()
        self.logical = (await self.db.get_iptv_logical_channels())[0]
        self.asset_root = Path(self.tmp.name) / "package"
        (self.asset_root / "logos").mkdir(parents=True)
        (self.asset_root / "logos" / "cctv5.png").write_bytes(PNG_1X1)

    async def asyncTearDown(self):
        if self.old_db is None:
            os.environ.pop("WAVEFLOW_DB_PATH", None)
        else:
            os.environ["WAVEFLOW_DB_PATH"] = self.old_db
        if self.old_assets is None:
            os.environ.pop("WAVEFLOW_MARKET_ASSET_ROOT", None)
        else:
            os.environ["WAVEFLOW_MARKET_ASSET_ROOT"] = self.old_assets
        _clear_modules()
        self.tmp.cleanup()

    def package(self, package_id: str, *, priority: int = 0, asset_name: str = "cctv5.png"):
        digest = hashlib.sha256(PNG_1X1).hexdigest()
        return self.market._normalize_package({
            "schema_version": 1,
            "id": package_id,
            "name": package_id,
            "kind": "logo_pack",
            "package_type": "content_package",
            "version": "1.0.0",
            "content_capabilities": ["logos"],
            "logo_priority": priority,
            "assets": [{
                "asset_id": "cctv5",
                "path": f"logos/{asset_name}",
                "media_type": "image/png",
                "sha256": digest,
                "size": len(PNG_1X1),
            }],
            "logos": [{"canonical_key": self.logical["canonical_key"], "asset_id": "cctv5"}],
            "_asset_root": str(self.asset_root),
        })

    async def install(self, package):
        staged, _final = await self.market._stage_logo_assets(package)
        bindings = await self.market._logo_binding_specs(package)
        await self.db.install_logo_package_atomic(
            package_id=package["id"],
            market_url="fixture://market",
            installed_version=package["version"],
            metadata_json='{"kind":"logo_pack"}',
            assets=staged,
            bindings=bindings,
        )
        return staged, bindings

    def resolver_channel(self):
        return {"logical_channel_id": self.logical["id"]}

    async def test_asset_contract_rejects_traversal_and_wrong_digest(self):
        with self.assertRaises(self.market.MarketError):
            self.market._normalize_package({
                "id": "bad", "kind": "logo_pack", "package_type": "content_package",
                "content_capabilities": ["logos"],
                "assets": [{"asset_id": "x", "path": "../x.png", "media_type": "image/png", "sha256": "0" * 64, "size": 1}],
                "logos": [{"canonical_key": "CCTV5", "asset_id": "x"}],
            })

        package = self.package("logo::bad")
        package["assets"][0]["sha256"] = "0" * 64
        with self.assertRaises(self.market.MarketError):
            await self.market._stage_logo_assets(package)
        self.assertIsNone(await self.db.get_market_install("logo::bad"))

    async def test_checked_in_fixture_has_curated_logo_set(self):
        fixture_root = Path(__file__).parent / "fixtures" / "logo_package"
        raw = json.loads((fixture_root / "manifest.json").read_text())
        package = self.market._normalize_package({**raw, "_asset_root": str(fixture_root)})
        self.assertEqual(len(package["assets"]), 7)
        self.assertEqual(len(package["logos"]), 7)
        staged, final_dir = await self.market._stage_logo_assets(package)
        self.assertEqual(len(staged), 7)
        self.assertTrue(final_dir.is_dir())

    async def test_logo_pack_persists_and_serves_stable_asset_without_merging(self):
        package = self.package("logo::official", priority=10)
        staged, bindings = await self.install(package)
        self.assertEqual(len(bindings), 1)
        self.assertEqual(len(await self.db.get_iptv_logical_channels()), 1)
        selected = await self.logo_resolver.resolve_many([self.resolver_channel()])
        self.assertEqual(selected[self.logical["id"]]["source_type"], "logo_pack")
        self.assertEqual(selected[self.logical["id"]]["package_id"], "logo::official")
        asset = await self.db.get_active_package_asset("logo::official", "cctv5")
        self.assertEqual(asset["sha256"], hashlib.sha256(PNG_1X1).hexdigest())
        self.assertTrue(Path(asset["stored_path"]).is_file())
        self.assertEqual((await self.db.list_market_installs())[0]["installed_subscription_id"], None)

    async def test_explicit_content_logo_beats_logo_pack_and_uninstall_falls_back(self):
        await self.install(self.package("logo::pack", priority=100))
        explicit = self.package("content::explicit", priority=0)
        staged, _ = await self.market._stage_logo_assets(explicit)
        binding = {
            "logical_channel_id": self.logical["id"],
            "asset_id": "cctv5",
            "binding_type": "content_package",
            "match_type": "stable_identity",
            "match_key": self.logical["canonical_key"],
            "priority": 0,
        }
        await self.db.install_logo_package_atomic(
            package_id=explicit["id"], market_url="fixture://market", installed_version="1.0.0",
            metadata_json='{"kind":"playlist"}', assets=staged, bindings=[binding],
        )
        selected = await self.logo_resolver.resolve_many([self.resolver_channel()])
        self.assertEqual(selected[self.logical["id"]]["package_id"], "content::explicit")
        self.assertTrue(await self.db.uninstall_market_package_atomic("content::explicit"))
        selected = await self.logo_resolver.resolve_many([self.resolver_channel()])
        self.assertEqual(selected[self.logical["id"]]["package_id"], "logo::pack")

    async def test_conflict_is_install_order_independent(self):
        first = self.package("logo::z", priority=5)
        second = self.package("logo::a", priority=5)
        await self.install(first)
        await self.install(second)
        selected = await self.logo_resolver.resolve_many([self.resolver_channel()])
        self.assertEqual(selected[self.logical["id"]]["package_id"], "logo::a")
        await self.db.uninstall_market_package_atomic("logo::a")
        await self.db.uninstall_market_package_atomic("logo::z")
        await self.install(second)
        await self.install(first)
        selected = await self.logo_resolver.resolve_many([self.resolver_channel()])
        self.assertEqual(selected[self.logical["id"]]["package_id"], "logo::a")

    async def test_uninstall_does_not_remove_channel_and_update_failure_keeps_old_binding(self):
        package = self.package("logo::stable")
        await self.install(package)
        old = await self.db.get_active_package_asset("logo::stable", "cctv5")
        broken = self.package("logo::stable")
        broken["assets"][0]["sha256"] = "f" * 64
        with self.assertRaises(self.market.MarketError):
            await self.market._stage_logo_assets(broken)
        still = await self.db.get_active_package_asset("logo::stable", "cctv5")
        self.assertEqual(still["stored_path"], old["stored_path"])
        self.assertTrue(await self.db.uninstall_market_package_atomic("logo::stable"))
        self.assertEqual(len(await self.db.get_iptv_logical_channels()), 1)
        self.assertEqual(await self.db.get_active_package_asset("logo::stable", "cctv5"), None)

    async def test_market_import_lifecycle_installs_logo_only_without_preview(self):
        package = self.package("logo::market")
        self.market._market_cache.update({
            "packages": [package],
            "market": {"schema_version": 1, "packages": [package]},
            "sources": [],
        })
        result = await self.market.import_package("logo::market")
        self.assertEqual(result["channel_count"], 0)
        install = await self.db.get_market_install("logo::market")
        self.assertIsNotNone(install)
        self.assertIsNone(install["installed_subscription_id"])
        self.assertEqual(len(await self.db.get_package_assets("logo::market")), 1)
        detail = await self.market.get_package("logo::market")
        self.assertNotIn("_asset_root", detail)

    async def test_content_package_explicit_logo_uses_same_market_lifecycle(self):
        digest = hashlib.sha256(PNG_1X1).hexdigest()
        package = self.market._normalize_package({
            "schema_version": 1,
            "id": "content::logo",
            "name": "Content with Logo",
            "kind": "playlist",
            "package_type": "content_package",
            "version": "1.0.0",
            "assets": [{
                "asset_id": "cctv5", "path": "logos/cctv5.png", "media_type": "image/png",
                "sha256": digest, "size": len(PNG_1X1),
            }],
            "channel_sources": [{
                "type": "inline_channels",
                "channels": [{
                    "name": "CCTV5", "logo_asset_id": "cctv5",
                    "sources": [{"url": "https://example.test/cctv5.m3u8"}],
                }],
            }],
            "_asset_root": str(self.asset_root),
        })
        self.market._market_cache.update({"packages": [package], "market": {"schema_version": 1}, "sources": []})
        with mock.patch.object(self.market, "_run_epg_binding_maintenance", new=mock.AsyncMock()):
            result = await self.market.import_package("content::logo")
        self.assertEqual(result["source_count"], 1)
        self.assertEqual(len(await self.db.get_package_assets("content::logo")), 1)
        binding = await self.db.get_logical_channel_logo_bindings(self.logical["id"])
        self.assertEqual(binding[0]["binding_type"], "content_package")


if __name__ == "__main__":
    unittest.main()
