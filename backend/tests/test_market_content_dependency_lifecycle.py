"""Content Package <-> Plugin dependency V1 lifecycle contract.

The canary is a real Market index document (``fixtures/market_content_canary``)
that declares ``requires_plugins`` for the real bundled ``org.waveflow/fjtv``
Plugin.  The Plugin is built, installed and executed for real (subprocess SDK
artifact, mocked upstream HTTP only), so the install / update / uninstall
lifecycle below exercises production code paths rather than stubs.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


PLUGIN_SOURCE = Path(__file__).parents[1] / "bundled_plugins" / "fjtv" / "plugin.py"
CANARY_DIR = Path(__file__).parent / "fixtures" / "market_content_canary"
CANARY_MARKET = CANARY_DIR / "market.json"
CANARY_MANIFEST = CANARY_DIR / "fjtv-content-canary.manifest.json"
CANARY_MARKET_URL = "https://canary.waveflow.test/market.json"
IDENTITY = "org.waveflow/fjtv"
CONTENT_ID = "official::fjtv-content-canary"
STREAM = "https://live.example/fjtv/live.m3u8?token=canary"


def _clear_modules() -> None:
    for name in ("database", "market", "plugin_market", "plugin_production", "logo_resolver", "iptv_channels"):
        sys.modules.pop(name, None)


class MarketContentDependencyLifecycleTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = os.environ.get("WAVEFLOW_DB_PATH")
        self.old_assets = os.environ.get("WAVEFLOW_MARKET_ASSET_ROOT")
        os.environ["WAVEFLOW_DB_PATH"] = str(Path(self.tmp.name) / "waveflow.db")
        os.environ["WAVEFLOW_MARKET_ASSET_ROOT"] = str(Path(self.tmp.name) / "assets")
        _clear_modules()
        import database
        import market
        import plugin_market
        from plugin_capabilities import CapabilityGateway, CoreCapabilityDispatcher
        from plugin_runtime import PermissionPolicy, PluginRuntime
        from waveflow_plugin_cli import build_sdk_artifact

        self.db = database
        self.market = market
        self.pm = plugin_market
        await self.db.initialize()

        self.artifact = Path(self.tmp.name) / "fjtv-plugin.pyz"
        build_sdk_artifact(PLUGIN_SOURCE, self.artifact)
        self.private = Ed25519PrivateKey.generate()
        public = self.private.public_key().public_bytes(
            encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw,
        )

        def upstream(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[{"m3u8": STREAM}])

        self.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        self.runtime = PluginRuntime(
            permission_policy=PermissionPolicy(frozenset({"network"})),
            capability_dispatcher=CoreCapabilityDispatcher(CapabilityGateway(client=self.client)),
        )
        self.store = plugin_market.PluginArtifactStore(
            Path(self.tmp.name) / "store", allowed_local_roots=[self.artifact.parent],
        )
        self.service = plugin_market.PluginMarketService(
            runtime=self.runtime,
            store=self.store,
            trust_policy=plugin_market.FixtureTrustPolicy({("org.waveflow", "fjtv-canary-key"): public}),
            command_factory=lambda manifest, artifact: (
                sys.executable, str(artifact), "--identity", manifest.identity, "--version", manifest.version,
            ),
            os_name="linux",
            arch="x86_64",
        )
        # Production wires this at startup; without it the Market module cannot
        # see the Plugin subsystem and would skip dependency validation.
        self.market.set_content_dependency_validator(self.service.dependency_projection)
        self._reset_market_cache()
        self._serve = None

        self.safe = mock.patch("plugin_capabilities.assert_safe_target_url", new=mock.AsyncMock())
        self.safe.start()

    async def asyncTearDown(self):
        self.safe.stop()
        self._stop_serving()
        self.market.set_content_dependency_validator(None)
        await self.runtime.shutdown()
        await self.client.aclose()
        for key, value in (("WAVEFLOW_DB_PATH", self.old_db), ("WAVEFLOW_MARKET_ASSET_ROOT", self.old_assets)):
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        _clear_modules()
        self.tmp.cleanup()

    # ── fixtures ────────────────────────────────────────────────────────────

    def _reset_market_cache(self) -> None:
        self.market._market_cache.clear()
        self.market._market_cache.update({
            "market_url": "https://canary.test/market.json",
            "market": {"schema_version": 1},
            "markets": [],
            "packages": [],
            "sources": [],
            "source_entries": {},
            "fetched_at": 0,
            "stale": False,
            "last_error": "",
            "allow_private": False,
        })

    @staticmethod
    def _canary_index():
        return copy.deepcopy(json.loads(CANARY_MARKET.read_text(encoding="utf-8")))

    @classmethod
    def _canary_raw(cls):
        return cls._canary_index()["packages"][0]

    @staticmethod
    def _canary_manifest_raw():
        return copy.deepcopy(json.loads(CANARY_MANIFEST.read_text(encoding="utf-8")))

    @staticmethod
    def _canary_source() -> dict:
        return {
            "id": 0, "source_key": "canary", "name": "Content canary source",
            "url": CANARY_MARKET_URL, "enabled": 1, "allow_private": 0, "is_builtin": 1,
        }

    async def _offer(
        self,
        version: str = "1.0.0",
        version_range: str = ">=1.0.0 <2.0.0",
        *,
        index_raw: dict | None = None,
    ) -> dict:
        """Publish the canary through the real ``market`` source loader.

        Only HTTP transport is replaced: the index is parsed by
        ``_load_source_packages`` and the manifest is resolved lazily by
        ``_resolve_package_manifest``, so index/manifest schema validation and
        the V1 import projection all run for real.
        """
        index = self._canary_index()
        entry = index_raw if index_raw is not None else index["packages"][0]
        if index_raw is not None:
            index["packages"] = [entry]
        entry["version"] = version
        if entry.get("requires_plugins"):
            entry["requires_plugins"][0]["version_range"] = version_range
        manifest = self._canary_manifest_raw()
        manifest["version"] = version

        async def serve(url: str, **_kwargs):
            name = Path(url).name
            text = json.dumps(index, ensure_ascii=False) if name == "market.json" else json.dumps(manifest, ensure_ascii=False)
            return url, text, httpx.Headers()

        self._stop_serving()
        self._serve = mock.patch.object(self.market, "safe_http_fetch", new=serve)
        self._serve.start()
        _market_doc, packages = await self.market._load_source_packages(self._canary_source())
        self.content_id = str(packages[0]["id"])
        self._reset_market_cache()
        self.market._market_cache.update({
            "market_url": CANARY_MARKET_URL,
            "market": _market_doc,
            "packages": packages,
            "sources": [self._canary_source()],
        })
        return packages[0]

    def _stop_serving(self) -> None:
        if getattr(self, "_serve", None) is not None:
            self._serve.stop()
            self._serve = None

    def _plugin_package(self, version: str = "1.0.0") -> dict:
        payload = self.artifact.read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        manifest = {
            "manifest_version": 1, "publisher_id": "org.waveflow", "plugin_id": "fjtv",
            "display_name": "FJTV Provider", "version": version, "plugin_api_version": "1.0",
            "core_version_range": ">=0.1.0 <1.0.0",
            "provider_contracts": [{"contract": "tv_provider", "contract_version": "1.0", "features": ["resolve_stream"]}],
            "owned_schemes": [{"scheme": "fjtv", "contract": "tv_provider"}],
            "capabilities": ["tv.resolve_stream"],
            "permissions": {"network": {"managed": True, "allowed_hosts": ["live.fjtv.net", "mapi-plus.fjtv.net"]}},
            "runtime": {"type": "subprocess", "ipc": "stdio_framed_json_v1"},
            "artifacts": [{
                "os": "linux", "arch": "x86_64", "runtime": "python", "entrypoint": "plugin.pyz",
                "sha256": digest, "size_bytes": len(payload),
                "signature": {
                    "algorithm": "ed25519", "key_id": "fjtv-canary-key",
                    "value": base64.b64encode(self.private.sign(payload)).decode("ascii"),
                },
            }],
            "dependencies": [], "state_schema_version": 1,
        }
        return {
            "schema_version": 1, "id": "official::fjtv-plugin", "original_id": "fjtv-plugin",
            "name": "FJTV Plugin", "kind": "plugin_package", "package_type": "plugin_package",
            "version": version, "plugin_manifest": manifest,
            "artifact_references": [{"sha256": digest, "local_path": str(self.artifact)}],
            "market_source": {"source_key": "official"},
        }

    async def _install_plugin(self, version: str = "1.0.0") -> dict:
        return await self.service.install_from_packages([self._plugin_package(version)], IDENTITY)

    @staticmethod
    def _requirement(version_range: str = ">=1.0.0 <2.0.0", identity: str = IDENTITY) -> dict:
        return {
            "plugin": identity,
            "version_range": version_range,
            "contract": "tv_provider",
            "required_schemes": ["fjtv"],
        }

    async def _import_canary(self, version: str = "1.0.0", version_range: str = ">=1.0.0 <2.0.0"):
        package = await self._offer(version, version_range)
        preview = await self.market.build_preview(package["id"])
        return await self.market.import_package(package["id"], preview_id=preview["preview_id"])

    # ── canary contract ─────────────────────────────────────────────────────

    def test_canary_declares_the_four_key_requires_plugins_contract(self):
        raw = self._canary_raw()
        self.market._validate_package_minimal(raw, context="market.json")
        self.assertEqual(self.market._schema_warnings(raw, index=True), [])
        package = self.market._normalize_package(raw)
        self.assertEqual(package["package_type"], "content_package")
        self.assertEqual(package["requires_plugins"], [self._requirement()])
        self.assertTrue(package["importable"])
        self.assertEqual(self.market._schema_warnings(self._canary_manifest_raw(), manifest=True), [])

    def test_canary_index_carries_no_execution_configuration(self):
        raw = self._canary_raw()
        leaked = sorted(field for field in raw if field in self.market.INDEX_EXECUTION_FIELDS)
        self.assertEqual(leaked, [])
        self.assertTrue(str(raw["manifest_url"]).endswith(".manifest.json"))

    def test_requires_plugins_rejects_any_extra_or_missing_key(self):
        for broken in (
            {"plugin": IDENTITY, "version_range": ">=1.0.0 <2.0.0", "contract": "tv_provider",
             "required_schemes": ["fjtv"], "source": "guessed-from-url"},
            {"plugin": IDENTITY, "version_range": ">=1.0.0", "contract": "tv_provider"},
        ):
            raw = self._canary_raw()
            raw["requires_plugins"] = [broken]
            with self.assertRaises(self.market.MarketError):
                self.market._normalize_package(raw)

    def test_dependency_is_never_inferred_from_a_channel_source_url(self):
        """A fjtv:// channel alone must not create an implied Plugin dependency."""
        raw = self._canary_raw()
        del raw["requires_plugins"]
        package = self.market._normalize_package(raw)
        self.assertEqual(package["requires_plugins"], [])

    # ── install flow ────────────────────────────────────────────────────────

    async def test_install_flow_missing_dependency_then_plugin_then_import_then_resolve(self):
        requirement = self._requirement()
        projection = await self.service.dependency_projection([requirement])
        self.assertEqual(projection["status"], "dependency_missing")

        package = await self._offer()
        preview = await self.market.build_preview(package["id"])
        with self.assertRaises(self.pm.PluginError) as blocked:
            await self.market.import_package(package["id"], preview_id=preview["preview_id"])
        self.assertEqual(blocked.exception.code, "DEPENDENCY_MISSING")
        self.assertIsNone(await self.db.get_market_install(self.content_id))

        installed = await self._install_plugin("1.0.0")
        self.assertEqual((installed["active_version"], installed["enabled"]), ("1.0.0", True))
        self.assertEqual((await self.service.dependency_projection([requirement]))["status"], "ready")

        result = await self.market.import_package(package["id"], preview_id=preview["preview_id"])
        install = await self.db.get_market_install(self.content_id)
        self.assertEqual(install["installed_version"], "1.0.0")
        self.assertEqual(
            json.loads(install["metadata_json"])["requires_plugins"], [requirement],
        )
        self.assertEqual(
            sorted(channel["url"] for channel in await self.db.get_channels(result["subscription_id"])),
            ["fjtv://fjzhpd", "fjtv://xmws"],
        )
        self.assertEqual(
            (await self.service.installed_content_dependency_projection(self.content_id))["status"], "ready",
        )

        from provider_resolver import ProviderResolver

        resolver = ProviderResolver(runtime=self.runtime)
        resolver.set_mode("fjtv", "plugin", IDENTITY)
        resolved = await resolver.resolve("adapter://fjtv/fjzhpd", self.client)
        self.assertEqual(resolved["url"], STREAM)
        self.assertEqual(resolved["stream_descriptor_version"], "1.0")

    async def test_install_flow_rejects_incompatible_plugin_version(self):
        await self._install_plugin("1.0.0")
        package = await self._offer(version_range=">=2.0.0 <3.0.0")
        preview = await self.market.build_preview(package["id"])
        with self.assertRaises(self.pm.PluginError) as blocked:
            await self.market.import_package(package["id"], preview_id=preview["preview_id"])
        self.assertEqual(blocked.exception.code, "PLUGIN_INCOMPATIBLE")
        self.assertIsNone(await self.db.get_market_install(self.content_id))

    # ── update flow ─────────────────────────────────────────────────────────

    async def test_update_flow_revalidates_dependencies(self):
        await self._install_plugin("1.0.0")
        await self._import_canary("1.0.0")
        self.assertEqual((await self.db.get_market_install(self.content_id))["installed_version"], "1.0.0")

        # The new package version raises the requirement beyond the installed
        # Plugin: V1 fails explicitly instead of silently upgrading the Plugin.
        await self._offer("1.1.0", version_range=">=2.0.0 <3.0.0")
        with self.assertRaises(self.pm.PluginError) as blocked:
            await self.market.update_installed_package(self.content_id)
        self.assertEqual(blocked.exception.code, "PLUGIN_INCOMPATIBLE")
        self.assertEqual((await self.db.get_market_install(self.content_id))["installed_version"], "1.0.0")

        await self._offer("1.1.0", version_range=">=1.0.0 <2.0.0")
        await self.market.update_installed_package(self.content_id)
        install = await self.db.get_market_install(self.content_id)
        self.assertEqual(install["installed_version"], "1.1.0")
        self.assertEqual(
            json.loads(install["metadata_json"])["requires_plugins"], [self._requirement()],
        )

    async def test_update_flow_blocks_when_the_plugin_dependency_is_removed(self):
        await self._install_plugin("1.0.0")
        await self._import_canary("1.0.0")
        await self.service.uninstall(IDENTITY, force=True)

        await self._offer("1.1.0")
        with self.assertRaises(self.pm.PluginError) as blocked:
            await self.market.update_installed_package(self.content_id)
        self.assertEqual(blocked.exception.code, "DEPENDENCY_MISSING")
        self.assertEqual((await self.db.get_market_install(self.content_id))["installed_version"], "1.0.0")

    # ── uninstall flow ──────────────────────────────────────────────────────

    async def test_uninstall_refuses_while_a_content_package_depends_on_the_plugin(self):
        await self._install_plugin("1.0.0")
        await self._import_canary("1.0.0")

        projection = await self.service.reverse_dependency_projection(IDENTITY)
        self.assertEqual((projection["status"], projection["dependents"]), ("blocked", [self.content_id]))

        with self.assertRaises(self.pm.PluginError) as blocked:
            await self.service.uninstall(IDENTITY)
        self.assertEqual(blocked.exception.code, "PLUGIN_DEPENDENCY_ACTIVE")
        self.assertEqual(blocked.exception.details["dependents"], [self.content_id])
        self.assertIsNotNone(await self.db.get_plugin_installation("org.waveflow", "fjtv"))

    async def test_forced_uninstall_does_not_cascade_to_the_content_package(self):
        await self._install_plugin("1.0.0")
        result = await self._import_canary("1.0.0")
        subscription = await self.db.get_subscription(result["subscription_id"])
        channels = await self.db.get_channels(result["subscription_id"])

        self.assertTrue(await self.service.uninstall(IDENTITY, force=True))
        self.assertIsNone(await self.db.get_plugin_installation("org.waveflow", "fjtv"))

        # No cascade: the Content Package, its subscription and its channels stay.
        install = await self.db.get_market_install(self.content_id)
        self.assertIsNotNone(install)
        self.assertEqual(await self.db.get_subscription(install["installed_subscription_id"]), subscription)
        self.assertEqual(await self.db.get_channels(install["installed_subscription_id"]), channels)
        self.assertEqual(
            (await self.service.installed_content_dependency_projection(self.content_id))["status"],
            "dependency_missing",
        )

    async def test_uninstall_succeeds_without_force_once_the_dependent_is_removed(self):
        await self._install_plugin("1.0.0")
        result = await self._import_canary("1.0.0")
        await self.market.uninstall_package(self.content_id)
        self.assertIsNone(await self.db.get_market_install(self.content_id))
        self.assertIsNone(await self.db.get_subscription(result["subscription_id"]))

        self.assertEqual(
            (await self.service.reverse_dependency_projection(IDENTITY))["status"], "clear",
        )
        self.assertTrue(await self.service.uninstall(IDENTITY))

    async def test_delisted_dependent_content_package_can_still_be_removed(self):
        """The refusal guard must not become a dead end.

        A dependent Content Package can disappear from the Market while its
        install row persists.  Removal must still work, after which the Plugin
        uninstall proceeds without force.
        """
        await self._install_plugin("1.0.0")
        await self._import_canary("1.0.0")

        self._stop_serving()
        self.market._market_cache["packages"] = []
        self.assertEqual(
            (await self.service.reverse_dependency_projection(IDENTITY))["dependents"], [self.content_id],
        )

        self.assertTrue((await self.market.uninstall_package(self.content_id))["uninstalled"])
        self.assertIsNone(await self.db.get_market_install(self.content_id))
        self.assertTrue(await self.service.uninstall(IDENTITY))

    # ── identity contract ───────────────────────────────────────────────────

    async def test_dependency_identity_must_match_the_installed_manifest(self):
        await self._install_plugin("1.0.0")
        row = await self.db.get_plugin_installation("org.waveflow", "fjtv")
        manifest = json.loads(row["manifest_json"])
        manifest["plugin_id"] = "fjtv-renamed"
        mismatched = {**row, "manifest_json": json.dumps(manifest)}

        projection = await self.service.dependency_projection([self._requirement()])
        self.assertEqual(projection["status"], "ready")

        evaluation = self.pm.evaluate_dependency(self._requirement(), mismatched, self.runtime)
        self.assertEqual(evaluation["status"], "plugin_incompatible")
        self.assertEqual(evaluation["manifest_identity"], "org.waveflow/fjtv-renamed")

    async def test_dependency_rejects_wrong_contract_and_unowned_scheme(self):
        await self._install_plugin("1.0.0")
        wrong_contract = {**self._requirement(), "contract": "radio_provider"}
        self.assertEqual(
            (await self.service.dependency_projection([wrong_contract]))["status"], "plugin_incompatible",
        )
        unowned = {**self._requirement(), "required_schemes": ["not-owned"]}
        self.assertEqual(
            (await self.service.dependency_projection([unowned]))["status"], "plugin_incompatible",
        )
        unknown_identity = self._requirement(identity="org.waveflow/absent")
        self.assertEqual(
            (await self.service.dependency_projection([unknown_identity]))["status"], "dependency_missing",
        )

    async def test_disabled_plugin_dependency_is_provider_unavailable_not_missing(self):
        await self._install_plugin("1.0.0")
        await self.service.disable(IDENTITY)
        self.assertEqual(
            (await self.service.dependency_projection([self._requirement()]))["status"],
            "provider_unavailable",
        )
        await self.service.enable(IDENTITY)
        self.assertEqual(
            (await self.service.dependency_projection([self._requirement()]))["status"], "ready",
        )


if __name__ == "__main__":
    unittest.main()
