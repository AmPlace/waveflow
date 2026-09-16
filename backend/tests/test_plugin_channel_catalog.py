from __future__ import annotations

from unittest import IsolatedAsyncioTestCase
from types import SimpleNamespace
from unittest.mock import AsyncMock

from plugin_channel_catalog import DynamicChannelCatalog, catalog_identity
from plugin_runtime import PluginError, validate_channel_catalog


def item(external_id: str, *, reference: str | None = None, kind: str = "channel", ttl: int = 300, **extra):
    return {
        "external_id": external_id,
        "name": f"Item {external_id}",
        "reference": reference or f"synthetic://{external_id}",
        "kind": kind,
        "ttl_seconds": ttl,
        **extra,
    }


class ChannelCatalogValidationTest(IsolatedAsyncioTestCase):
    def test_sdk_catalog_item_serializes_minimal_generic_schema(self):
        from waveflow_plugin_sdk import ChannelCatalog, ChannelCatalogItem, ChannelCatalogProvider, PluginApplication, TVProvider

        payload = ChannelCatalog((ChannelCatalogItem(
            external_id="event-1", name="Event", reference="synthetic://event-1",
            kind="event", metadata={"live": True},
        ),)).as_contract()
        self.assertEqual(payload["items"][0]["metadata"], {"live": True})
        self.assertEqual(
            validate_channel_catalog(payload, owned_schemes={"synthetic"})["items"][0]["reference"],
            "synthetic://event-1",
        )

        class Provider(TVProvider):
            def resolve_stream(self, reference, context):
                raise NotImplementedError

        class CatalogProvider(ChannelCatalogProvider):
            def discover_channels(self, context):
                return payload

        hello = PluginApplication(identity="org.waveflow/catalog", version="1.0.0")
        hello.register_tv("synthetic", Provider()).register_channel_catalog(CatalogProvider())
        self.assertIn(
            {"contract": "channel_catalog", "contract_version": "1.0", "features": ["discover"]},
            hello.hello()["provider_contracts"],
        )

    def test_valid_schema_and_owned_reference(self):
        result = validate_channel_catalog({"items": [item("one", kind="event", starts_at=100, ends_at=200)]},
                                          owned_schemes={"synthetic"})
        self.assertEqual(result["items"][0]["external_id"], "one")

    def test_foreign_duplicate_and_invalid_metadata_are_rejected(self):
        cases = [
            {"items": [item("foreign", reference="other://one")]},
            {"items": [item("same"), item("same")]},
            {"items": [item("bad", metadata={"value": object()})]},
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                with self.assertRaises(PluginError) as raised:
                    validate_channel_catalog(payload, owned_schemes={"synthetic"})
                self.assertEqual(raised.exception.code, "INVALID_PLUGIN_RESPONSE")

    def test_catalog_contract_requires_tv_provider_and_does_not_own_schemes(self):
        from plugin_runtime import validate_manifest

        base = {
            "manifest_version": 1, "publisher_id": "org.waveflow", "plugin_id": "catalog",
            "display_name": "Catalog", "version": "1.0.0", "plugin_api_version": "1.0",
            "core_version_range": ">=0.1.0 <1.0.0",
            "provider_contracts": [{"contract": "channel_catalog", "contract_version": "1.0", "features": ["discover"]}],
            "owned_schemes": [], "capabilities": [], "permissions": {},
            "runtime": {"type": "subprocess", "ipc": "stdio_framed_json_v1"},
            "artifacts": [{"os": "linux", "arch": "x86_64", "runtime": "native", "entrypoint": "catalog",
                           "sha256": "0" * 64, "size_bytes": 1,
                           "signature": {"algorithm": "ed25519", "key_id": "fixture", "value": "fixture"}}],
            "dependencies": [], "state_schema_version": 1,
        }
        with self.assertRaises(PluginError) as raised:
            validate_manifest(base)
        self.assertEqual(raised.exception.code, "PLUGIN_INCOMPATIBLE")


class DynamicChannelCatalogProjectionTest(IsolatedAsyncioTestCase):
    async def test_refresh_identity_add_delete_stale_and_expiry(self):
        projection = DynamicChannelCatalog(stale_grace_seconds=10)
        first = projection.apply("org.waveflow/synthetic", [item("one"), item("two")], now=100)
        stable = catalog_identity("org.waveflow/synthetic", "one")
        self.assertIn(stable, {value["identity"] for value in first["items"]})

        refreshed = projection.apply(
            "org.waveflow/synthetic", [item("one", name="ignored", metadata={"revision": 2}), item("three")], now=110,
        )
        one = projection.get(stable, now=110)
        self.assertEqual(one["identity"], stable)
        self.assertEqual(one["metadata"], {"revision": 2})
        self.assertEqual({value["external_id"] for value in refreshed["visible_items"]}, {"one", "two", "three"})
        self.assertEqual(projection.get(catalog_identity("org.waveflow/synthetic", "two"), now=110)["state"], "stale")

        projection.apply("org.waveflow/synthetic", [], now=111)
        self.assertEqual(len(projection.status("org.waveflow/synthetic", now=119)["visible_items"]), 3)
        expired = projection.status("org.waveflow/synthetic", now=122)
        self.assertEqual(expired["visible_items"], [])
        self.assertTrue(all(value["state"] == "expired" for value in expired["items"]))

    async def test_failure_keeps_healthy_cache_and_expiry_is_fail_closed(self):
        projection = DynamicChannelCatalog(stale_grace_seconds=10)
        projection.apply("org.waveflow/synthetic", [item("live", ttl=20)], now=100)
        failed = projection.record_failure(
            "org.waveflow/synthetic", PluginError("PLUGIN_TIMEOUT", "catalog timeout"), now=101,
        )
        self.assertEqual([value["external_id"] for value in failed["visible_items"]], ["live"])
        self.assertEqual(projection.get(catalog_identity("org.waveflow/synthetic", "live"), now=131), None)
        self.assertEqual(projection.status("org.waveflow/synthetic", now=131)["failures"]["code"], "PLUGIN_TIMEOUT")

    async def test_event_end_and_click_use_existing_provider_resolver(self):
        projection = DynamicChannelCatalog(stale_grace_seconds=30)
        projection.apply("org.waveflow/synthetic", [item("event", kind="event", ends_at=200)], now=100)
        resolver = SimpleNamespace(resolve=AsyncMock(return_value={"url": "https://example.invalid/live", "source_type": "hls"}))
        client = object()
        resolved = await projection.resolve(
            catalog_identity("org.waveflow/synthetic", "event"), resolver, client, now=150,
        )
        self.assertEqual(resolved["source_type"], "hls")
        resolver.resolve.assert_awaited_once_with("synthetic://event", client)
        with self.assertRaises(PluginError) as raised:
            await projection.resolve(catalog_identity("org.waveflow/synthetic", "event"), resolver, client, now=201)
        self.assertEqual(raised.exception.code, "RESOURCE_NOT_FOUND")

    async def test_static_content_boundary_is_untouched(self):
        static_channels = [{"name": "Static", "url": "https://static.example/live"}]
        before = [dict(value) for value in static_channels]
        projection = DynamicChannelCatalog()
        projection.apply("org.waveflow/synthetic", [item("dynamic")], now=100)
        self.assertEqual(static_channels, before)
        self.assertFalse(hasattr(projection, "db"))
