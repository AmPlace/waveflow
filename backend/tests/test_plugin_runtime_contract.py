from __future__ import annotations

import asyncio
import json
import unittest

from plugin_runtime import (
    LifecycleState, MemoryCapabilityStubs, PermissionGate, PermissionPolicy, PluginError,
    PluginInstance, PluginRegistry, encode_frame, read_frame, validate_manifest,
    validate_descriptor_metadata, validate_stream_descriptor,
)
from plugin_runtime.validation import (
    RADIO_CATALOG_MAX_BYTES,
    RADIO_CATALOG_MAX_ITEMS,
    validate_radio_catalog,
)


def manifest_data(*, scheme="synthetic", version="1.0.0", permissions=None, core_range=">=0.1.0 <1.0.0"):
    return {
        "manifest_version": 1, "publisher_id": "org.waveflow", "plugin_id": "synthetic",
        "display_name": "Synthetic", "version": version, "plugin_api_version": "1.0",
        "core_version_range": core_range,
        "provider_contracts": [
            {"contract": "tv_provider", "contract_version": "1.0", "features": ["resolve_stream"]},
            {"contract": "radio_provider", "contract_version": "1.0", "features": ["catalog", "resolve_stream"]},
        ],
        "owned_schemes": [{"scheme": scheme, "contract": "tv_provider"}],
        "capabilities": ["tv.resolve_stream", "radio.catalog", "radio.resolve_stream"],
        "permissions": permissions or {},
        "runtime": {"type": "subprocess", "ipc": "stdio_framed_json_v1"},
        "artifacts": [{"os": "linux", "arch": "x86_64", "runtime": "native",
                       "entrypoint": "synthetic-plugin", "sha256": "0" * 64, "size_bytes": 1,
                       "signature": {"algorithm": "ed25519", "key_id": "fixture", "value": "fixture"}}],
        "dependencies": [], "state_schema_version": 1,
    }


def descriptor(transport="hls"):
    return {"descriptor_version": "1.0", "transport": transport,
            "url": "" if transport == "probe_only" else "https://example.invalid/live",
            "headers": {}, "credential_refs": [], "ttl_seconds": 10, "expires_at": None,
            "volatile_url": True, "requires_proxy": False, "warnings": []}


class ManifestValidationTest(unittest.TestCase):
    def test_valid_manifest_and_stable_identity(self):
        manifest = validate_manifest(manifest_data())
        self.assertEqual(manifest.identity, "org.waveflow/synthetic")
        self.assertEqual(manifest.version, "1.0.0")

    def test_malformed_incompatible_and_duplicate_scheme(self):
        cases = []
        missing = manifest_data(); missing.pop("plugin_id"); cases.append((missing, "INVALID_PLUGIN_RESPONSE"))
        bad_api = manifest_data(); bad_api["plugin_api_version"] = "2.0"; cases.append((bad_api, "PLUGIN_INCOMPATIBLE"))
        bad_core = manifest_data(core_range=">=2.0.0 <3.0.0"); cases.append((bad_core, "PLUGIN_INCOMPATIBLE"))
        bad_semver = manifest_data(); bad_semver["version"] = "latest"; cases.append((bad_semver, "INVALID_PLUGIN_RESPONSE"))
        bad_range = manifest_data(); bad_range["core_version_range"] = "^0.1"; cases.append((bad_range, "INVALID_PLUGIN_RESPONSE"))
        duplicate = manifest_data(); duplicate["owned_schemes"].append(dict(duplicate["owned_schemes"][0])); cases.append((duplicate, "SCHEME_CONFLICT"))
        bad_permission = manifest_data(); bad_permission["permissions"] = "all"; cases.append((bad_permission, "INVALID_PLUGIN_RESPONSE"))
        bad_runtime = manifest_data(); bad_runtime["runtime"] = {"type": "python"}; cases.append((bad_runtime, "PLUGIN_INCOMPATIBLE"))
        for data, code in cases:
            with self.subTest(code=code):
                with self.assertRaises(PluginError) as raised:
                    validate_manifest(data)
                self.assertEqual(raised.exception.code, code)


class ProtocolCodecTest(unittest.IsolatedAsyncioTestCase):
    async def _reader(self, data: bytes, chunks: tuple[int, ...] = ()):
        reader = asyncio.StreamReader()
        if not chunks:
            reader.feed_data(data)
        else:
            offset = 0
            for size in chunks:
                reader.feed_data(data[offset:offset + size]); offset += size
            reader.feed_data(data[offset:])
        reader.feed_eof()
        return reader

    async def test_partial_and_multiple_frames(self):
        first = encode_frame({"n": 1}); second = encode_frame({"n": 2})
        reader = await self._reader(first + second, (1, 2, 3, 5, 8))
        self.assertEqual(await read_frame(reader), {"n": 1})
        self.assertEqual(await read_frame(reader), {"n": 2})

    async def test_malformed_truncated_invalid_and_oversized_frames(self):
        bad_frames = [
            b"Bad\r\n\r\n{}",
            b"Content-Length: nope\r\nContent-Type: application/json; charset=utf-8\r\n\r\n{}",
            b"Content-Length: 5\r\nContent-Type: application/json; charset=utf-8\r\n\r\n{}",
            b"Content-Length: 2\r\nContent-Type: application/json; charset=utf-8\r\n\r\n\xff\xff",
            b"Content-Length: 1048577\r\nContent-Type: application/json; charset=utf-8\r\n\r\n",
        ]
        for frame in bad_frames:
            with self.subTest(frame=frame[:20]):
                with self.assertRaises(PluginError):
                    await read_frame(await self._reader(frame))
        with self.assertRaises(PluginError):
            encode_frame({"body": "x" * (1024 * 1024)})


class ValidationPermissionRegistryTest(unittest.TestCase):
    @staticmethod
    def _radio_station(index: int, *, metadata_padding: int = 0):
        return {
            "station_ref": {"provider_key": "synthetic", "provider_station_id": f"station-{index}"},
            "name": f"Synthetic Station {index}",
            "logo_url": f"https://images.example.invalid/radio/{index}.png",
            "group_name": "Synthetic Region",
            "country": "CN",
            "language": "zh-CN",
            "frequency": "FM 100.0",
            "metadata": {"region": "synthetic", "padding": "x" * metadata_padding},
            "playback_config": {"region": "synthetic"},
            "ttl_seconds": 7200,
        }

    def test_radio_catalog_accepts_realistic_catalog_larger_than_legacy_limits(self):
        catalog = {"stations": [self._radio_station(index) for index in range(924)]}
        serialized = json.dumps(catalog, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.assertGreater(len(serialized), 256 * 1024)

        normalized = validate_radio_catalog(catalog, owned_schemes={"synthetic"})

        self.assertEqual(len(normalized["stations"]), 924)
        self.assertEqual(RADIO_CATALOG_MAX_ITEMS, 2048)
        self.assertEqual(RADIO_CATALOG_MAX_BYTES, 512 * 1024)

    def test_radio_catalog_rejects_item_and_serialized_size_overflow(self):
        too_many = {"stations": [
            {"station_ref": {"provider_key": "synthetic", "provider_station_id": str(index)}}
            for index in range(RADIO_CATALOG_MAX_ITEMS + 1)
        ]}
        with self.assertRaises(PluginError) as item_error:
            validate_radio_catalog(too_many, owned_schemes={"synthetic"})
        self.assertIn("too many stations", str(item_error.exception))

        too_large = {"stations": [self._radio_station(index, metadata_padding=256) for index in range(1500)]}
        with self.assertRaises(PluginError) as size_error:
            validate_radio_catalog(too_large, owned_schemes={"synthetic"})
        self.assertIn("size limit", str(size_error.exception))

    def test_radio_catalog_keeps_field_source_and_logo_boundaries(self):
        oversized_name = self._radio_station(1)
        oversized_name["name"] = "x" * 2049
        duplicate = self._radio_station(2)
        relative_logo = self._radio_station(3)
        relative_logo["logo_url"] = "/logos/radio.png"
        for catalog in (
            {"stations": [oversized_name]},
            {"stations": [duplicate, dict(duplicate)]},
            {"stations": [relative_logo]},
        ):
            with self.subTest(catalog=catalog):
                with self.assertRaises(PluginError):
                    validate_radio_catalog(catalog, owned_schemes={"synthetic"})

    def test_all_stream_transports_and_secret_header_rejection(self):
        for transport in ("hls", "dash", "http_flv", "mpegts", "audio_http", "probe_only"):
            self.assertEqual(validate_stream_descriptor(descriptor(transport))["transport"], transport)
        rtsp = descriptor("rtsp"); rtsp["url"] = "rtsp://example.invalid/live"
        self.assertEqual(validate_stream_descriptor(rtsp)["transport"], "rtsp")
        rich = descriptor("dash")
        rich.update({"quality_variants": [{"name": "best"}], "drm": {"scheme": "widevine"},
                     "encryption": {"method": "aes-128"},
                     "provider_diagnostics": {"id": "safe", "nested": {"live": True}},
                     "probe_hints": {"page_live": True, "identity": ["channel", "video"]}})
        validate_stream_descriptor(rich)
        bad = descriptor(); bad["headers"] = {"Cookie": "redacted"}
        with self.assertRaises(PluginError) as raised:
            validate_stream_descriptor(bad)
        self.assertEqual(raised.exception.code, "INVALID_PLUGIN_RESPONSE")

    def test_descriptor_metadata_is_bounded_json_data(self):
        valid = {"identity": {"channel_id": "channel-1", "revision": 2}, "live": True,
                 "values": [None, 1, 1.5, "safe"]}
        self.assertEqual(validate_descriptor_metadata(valid), valid)

        invalid_values = [
            {"callable": lambda: None},
            {"bytes": b"not-json"},
            {"value": float("inf")},
            {"nested": {"a": {"b": {"c": {"d": {"e": {"f": True}}}}}}},
            {"items": list(range(65))},
            {"text": "x" * 2049},
            {"text": "x" * (16 * 1024)},
        ]
        for value in invalid_values:
            with self.subTest(value=type(next(iter(value.values()))).__name__):
                with self.assertRaises(PluginError) as raised:
                    validate_descriptor_metadata(value)
                self.assertEqual(raised.exception.code, "INVALID_PLUGIN_RESPONSE")

        bad_descriptor = descriptor()
        bad_descriptor["probe_hints"] = {"unsupported": object()}
        with self.assertRaises(PluginError):
            validate_stream_descriptor(bad_descriptor)

    def test_descriptor_bridge_preserves_generic_extensions_without_promoting_authority(self):
        from provider_resolver import ProviderResolver

        base = descriptor()
        bridged = ProviderResolver._bridge_descriptor("synthetic", base)
        self.assertNotIn("provider_diagnostics", bridged)
        self.assertNotIn("probe_hints", bridged)

        base.update({
            "provider_diagnostics": {"identity": {"channel_id": "c-1"}},
            "probe_hints": {"page_live": True, "probe_status": "online"},
        })
        bridged = ProviderResolver._bridge_descriptor("synthetic", base)
        self.assertEqual(bridged["provider_diagnostics"], {"identity": {"channel_id": "c-1"}})
        self.assertEqual(bridged["probe_hints"], {"page_live": True, "probe_status": "online"})
        self.assertEqual(bridged["headers"], {})
        self.assertFalse(bridged["requires_proxy"])

    def test_sdk_descriptor_serializes_generic_metadata_contract(self):
        from waveflow_plugin_sdk import StreamDescriptor

        value = StreamDescriptor.hls(
            "https://example.invalid/live",
            provider_diagnostics={"identity": {"channel_id": "c-1"}},
            probe_hints={"page_live": True},
        ).as_contract()
        self.assertEqual(validate_stream_descriptor(value), value)

    def test_permission_allow_deny_and_memory_stubs(self):
        manifest = validate_manifest(manifest_data(permissions={"cache": {}, "subprocess": {}}))
        gate = PermissionGate(manifest, PermissionPolicy(frozenset({"cache"})))
        gate.require("cache")
        with self.assertRaises(PluginError) as denied:
            gate.require("subprocess")
        self.assertEqual(denied.exception.code, "CAPABILITY_DENIED")
        with self.assertRaises(PluginError):
            gate.require("undeclared")
        stubs = MemoryCapabilityStubs(gate)
        stubs.cache_set("key", "value")
        self.assertEqual(stubs.cache_get("key"), "value")

    def test_registry_conflict_unhealthy_and_lifecycle_transitions(self):
        registry = PluginRegistry()
        first = PluginInstance("first", validate_manifest(manifest_data()))
        second = PluginInstance("second", validate_manifest(manifest_data(version="1.1.0")))
        registry.install(first); registry.install(second)
        first.transition(LifecycleState.STARTING); first.transition(LifecycleState.HANDSHAKING); registry.activate(first)
        second.transition(LifecycleState.STARTING); second.transition(LifecycleState.HANDSHAKING)
        with self.assertRaises(PluginError) as conflict:
            registry.activate(second)
        self.assertEqual(conflict.exception.code, "SCHEME_CONFLICT")
        registry.mark_unhealthy(first)
        with self.assertRaises(PluginError) as missing:
            registry.route("synthetic")
        self.assertEqual(missing.exception.code, "SCHEME_UNOWNED")
        with self.assertRaises(PluginError):
            first.transition(LifecycleState.HEALTHY_ACTIVE)
