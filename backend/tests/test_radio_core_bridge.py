from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

import database
from plugin_runtime import PermissionPolicy, PluginError, PluginRuntime, validate_manifest
from radio_core import (
    RADIO_DESCRIPTOR_CACHE_MAX_ENTRIES,
    RadioCatalogBridge,
    RadioResolver,
)
from security.source_ids import MediaSourceIdentity, media_source_id_for, media_source_revision_for, source_id_for, source_revision_for


FIXTURE = Path(__file__).parent / "fixtures" / "synthetic_plugin.py"


def _manifest():
    return validate_manifest({
        "manifest_version": 1,
        "publisher_id": "org.waveflow",
        "plugin_id": "synthetic",
        "display_name": "Synthetic Radio",
        "version": "1.0.0",
        "plugin_api_version": "1.0",
        "core_version_range": ">=0.1.0 <1.0.0",
        "provider_contracts": [
            {"contract": "tv_provider", "contract_version": "1.0", "features": ["resolve_stream"]},
            {"contract": "radio_provider", "contract_version": "1.0", "features": ["catalog", "resolve_stream"]},
        ],
        "owned_schemes": [{"scheme": "synthetic", "contract": "radio_provider"}],
        "capabilities": ["tv.resolve_stream", "radio.catalog", "radio.resolve_stream"],
        "permissions": {},
        "runtime": {"type": "subprocess", "ipc": "stdio_framed_json_v1"},
        "artifacts": [{
            "os": "linux", "arch": "x86_64", "runtime": "native",
            "entrypoint": "synthetic-plugin", "sha256": "0" * 64, "size_bytes": 1,
            "signature": {"algorithm": "ed25519", "key_id": "fixture", "value": "fixture"},
        }],
        "dependencies": [],
        "state_schema_version": 1,
    })


class RadioCoreBridgeTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = os.environ.get("WAVEFLOW_DB_PATH")
        os.environ["WAVEFLOW_DB_PATH"] = str(Path(self.tmp.name) / "waveflow.db")
        await database.initialize()
        self.runtime = PluginRuntime(permission_policy=PermissionPolicy())
        self.instance = self.runtime.install(
            _manifest(), [sys.executable, str(FIXTURE), "--mode", "normal", "--scheme", "synthetic", "--radio-owned"],
        )
        await self.runtime.enable(self.instance)

    async def asyncTearDown(self):
        await self.runtime.shutdown()
        if self.old_db is None:
            os.environ.pop("WAVEFLOW_DB_PATH", None)
        else:
            os.environ["WAVEFLOW_DB_PATH"] = self.old_db
        self.tmp.cleanup()

    async def test_catalog_persistence_resolve_and_last_good(self):
        catalog = await self.runtime.request(self.instance, "radio.catalog", {})
        bridge = RadioCatalogBridge()
        result = await bridge.refresh(
            "org.waveflow/synthetic", catalog, owned_schemes={"synthetic"}, now=100,
        )
        self.assertEqual(result["published"], 2)
        stations = await database.list_radio_stations(now_unix=101)
        self.assertEqual(len(stations), 2)
        self.assertTrue(all(item["station_id"].startswith("radio_station_") for item in stations))
        self.assertEqual(stations[0]["sources"][0]["lifecycle_state"], "active")

        source = stations[0]["sources"][0]
        resolver = RadioResolver(runtime=self.runtime, clock=lambda: 101)
        descriptor = await resolver.resolve_source(source["source_id"], station_id=stations[0]["station_id"])
        self.assertEqual(descriptor["domain"], "radio")
        self.assertEqual(descriptor["source_id"], source["source_id"])
        self.assertEqual(descriptor["source_type"], "audio_http")
        self.assertEqual(descriptor["url"], "https://example.invalid/live.mp3")
        cached = await resolver.resolve_source(source["source_id"], station_id=stations[0]["station_id"])
        self.assertEqual(cached, descriptor)

        changed = dict(catalog)
        changed["stations"] = [dict(item) for item in catalog["stations"]]
        changed["stations"][0]["playback_config"] = {"profile": "backup"}
        await bridge.refresh("org.waveflow/synthetic", changed, owned_schemes={"synthetic"}, now=102)
        refreshed = await database.get_radio_station_source(source["source_id"], station_id=stations[0]["station_id"], now_unix=103)
        self.assertEqual(refreshed["source_id"], source["source_id"])
        self.assertNotEqual(refreshed["source_revision"], source["source_revision"])

        await bridge.record_failure("org.waveflow/synthetic", RuntimeError("fixture timeout"))
        preserved = await database.list_radio_stations(now_unix=103)
        self.assertEqual(len(preserved), 2)
        self.assertEqual(preserved[0]["sources"][0]["lifecycle_state"], "active")

    async def test_refresh_marks_missing_stale_without_cross_provider_dedup(self):
        bridge = RadioCatalogBridge()
        first = {
            "stations": [{
                "station_ref": {"provider_key": "synthetic", "provider_station_id": "one"},
                "name": "Same Name", "ttl_seconds": 60,
            }, {
                "station_ref": {"provider_key": "other", "provider_station_id": "one"},
                "name": "Same Name", "ttl_seconds": 60,
            }],
        }
        await bridge.refresh("org.waveflow/synthetic", first, owned_schemes={"synthetic", "other"}, now=200)
        rows = await database.list_radio_stations(now_unix=201)
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0]["station_id"], rows[1]["station_id"])
        self.assertNotEqual(rows[0]["sources"][0]["source_id"], rows[1]["sources"][0]["source_id"])

        await bridge.refresh(
            "org.waveflow/synthetic",
            {"stations": [first["stations"][0]]},
            owned_schemes={"synthetic", "other"}, now=210,
        )
        rows = await database.list_radio_stations(now_unix=211)
        stale = next(item for item in rows if item["provider_key"] == "other")
        self.assertEqual(stale["lifecycle_state"], "stale")
        self.assertEqual(stale["sources"][0]["lifecycle_state"], "stale")

    async def test_explicit_source_discriminator_keeps_one_station_with_two_sources(self):
        bridge = RadioCatalogBridge()
        await bridge.refresh(
            "org.waveflow/synthetic",
            {"stations": [
                {"station_ref": {"provider_key": "synthetic", "provider_station_id": "one"},
                 "source_discriminator": "primary", "name": "Station", "playback_config": {"profile": "primary"}},
                {"station_ref": {"provider_key": "synthetic", "provider_station_id": "one"},
                 "source_discriminator": "backup", "name": "Station", "playback_config": {"profile": "backup"}},
            ]},
            owned_schemes={"synthetic"}, now=250,
        )
        stations = await database.list_radio_stations(now_unix=251)
        self.assertEqual(len(stations), 1)
        self.assertEqual(len(stations[0]["sources"]), 2)
        self.assertEqual(
            {source["source_discriminator"] for source in stations[0]["sources"]},
            {"primary", "backup"},
        )
        self.assertNotEqual(*[source["source_id"] for source in stations[0]["sources"]])

    async def test_catalog_generation_rejects_late_old_publication_and_failure(self):
        bridge = RadioCatalogBridge()
        generation_a = await database.begin_radio_catalog_refresh("org.waveflow/synthetic")
        generation_b = await database.begin_radio_catalog_refresh("org.waveflow/synthetic")
        newer = await bridge.refresh(
            "org.waveflow/synthetic",
            {"stations": [{"station_ref": {"provider_key": "synthetic", "provider_station_id": "new"}, "name": "New"}]},
            owned_schemes={"synthetic"}, now=300, generation=generation_b,
        )
        older = await bridge.refresh(
            "org.waveflow/synthetic",
            {"stations": [{"station_ref": {"provider_key": "synthetic", "provider_station_id": "old"}, "name": "Old"}]},
            owned_schemes={"synthetic"}, now=200, generation=generation_a,
        )
        self.assertEqual(newer["status"], "success")
        self.assertEqual(older["status"], "stale")
        stations = await database.list_radio_stations(now_unix=301)
        self.assertEqual([item["provider_station_id"] for item in stations], ["new"])
        await bridge.record_failure("org.waveflow/synthetic", RuntimeError("late failure"), generation=generation_a)
        stations = await database.list_radio_stations(now_unix=301)
        self.assertEqual(stations[0]["sources"][0]["last_error"], "")

    async def test_resolve_old_revision_cannot_update_new_source_health(self):
        bridge = RadioCatalogBridge()
        await bridge.refresh(
            "org.waveflow/synthetic",
            {"stations": [{
                "station_ref": {"provider_key": "synthetic", "provider_station_id": "race"},
                "name": "Race", "playback_config": {"profile": "old"},
            }]},
            owned_schemes={"synthetic"}, now=350,
        )
        station = (await database.list_radio_stations(now_unix=351))[0]
        source = station["sources"][0]
        started = asyncio.Event()
        release = asyncio.Event()

        class FakeRuntime:
            def __init__(self):
                self.instance = types.SimpleNamespace(
                    manifest=types.SimpleNamespace(
                        identity="org.waveflow/synthetic",
                        owned_schemes=(("synthetic", "radio_provider"),),
                    )
                )
                self.registry = types.SimpleNamespace(route=lambda _scheme: self.instance)

            async def request(self, _instance, method, _payload):
                self.assert_method = method
                started.set()
                await release.wait()
                return {
                    "descriptor_version": "1.0", "transport": "audio_http",
                    "url": "https://example.invalid/race.mp3", "headers": {},
                    "credential_refs": [], "ttl_seconds": 60, "expires_at": None,
                    "volatile_url": False, "requires_proxy": False, "warnings": [],
                }

        resolver = RadioResolver(runtime=FakeRuntime(), clock=lambda: 351)
        task = asyncio.create_task(resolver.resolve_source(source["source_id"], station_id=station["station_id"]))
        await started.wait()
        await bridge.refresh(
            "org.waveflow/synthetic",
            {"stations": [{
                "station_ref": {"provider_key": "synthetic", "provider_station_id": "race"},
                "name": "Race", "playback_config": {"profile": "new"},
            }]},
            owned_schemes={"synthetic"}, now=352,
        )
        release.set()
        with self.assertRaises(PluginError) as caught:
            await task
        self.assertEqual(caught.exception.code, "PLUGIN_CANDIDATE_CONFLICT")
        current = await database.get_radio_station_source(source["source_id"], station_id=station["station_id"], now_unix=353)
        self.assertNotEqual(current["source_revision"], source["source_revision"])
        self.assertEqual(current["health_status"], "unknown")

    async def test_resolver_cache_pruning_is_bounded(self):
        resolver = RadioResolver(clock=lambda: 100)
        for index in range(RADIO_DESCRIPTOR_CACHE_MAX_ENTRIES + 5):
            resolver._descriptor_cache[("radio", f"source-{index}", "revision")] = (1000 + index, {"index": index})
        resolver._prune_caches()
        self.assertLessEqual(len(resolver._descriptor_cache), RADIO_DESCRIPTOR_CACHE_MAX_ENTRIES)

    async def test_programme_bridge_persists_radio_snapshot_and_reuses_revision_cache(self):
        bridge = RadioCatalogBridge()
        await bridge.refresh(
            "org.waveflow/synthetic",
            {"stations": [{
                "station_ref": {"provider_key": "synthetic", "provider_station_id": "programme-one"},
                "name": "Programme Station", "playback_config": {"profile": "primary"},
                "ttl_seconds": 60,
            }]},
            owned_schemes={"synthetic"}, now=400,
        )
        station = (await database.list_radio_stations(now_unix=401))[0]
        source = station["sources"][0]

        class FakeRuntime:
            def __init__(self):
                self.calls = []
                self.instance = types.SimpleNamespace(
                    manifest=types.SimpleNamespace(
                        identity="org.waveflow/synthetic",
                        owned_schemes=(("synthetic", "radio_provider"),),
                    )
                )
                self.registry = types.SimpleNamespace(route=lambda _scheme: self.instance)

            async def request(self, _instance, method, payload):
                self.calls.append((method, payload))
                self_id = payload["station_ref"]["provider_station_id"]
                if method == "radio.programme":
                    return {
                        "station_ref": {"provider_key": "synthetic", "provider_station_id": self_id},
                        "revision": "programme-rev-1",
                        "programmes": [{"provider_programme_id": "p1", "title": "Now", "updated_at": 401}],
                    }
                raise AssertionError(method)

        runtime = FakeRuntime()
        resolver = RadioResolver(runtime=runtime, clock=lambda: 401)
        result = await resolver.resolve_programme(source["source_id"], station_id=station["station_id"])
        self.assertEqual(result["programmes"][0]["title"], "Now")
        self.assertEqual(runtime.calls[0][1]["playback_config"], {"profile": "primary"})
        persisted = await database.get_radio_programme_snapshot(source["source_id"], now_unix=402)
        self.assertEqual(persisted["revision"], "programme-rev-1")
        await resolver.resolve_programme(source["source_id"], station_id=station["station_id"])
        self.assertEqual(len(runtime.calls), 1)

    async def test_bounds_ownership_and_identity_compatibility(self):
        bridge = RadioCatalogBridge()
        with self.assertRaises(PluginError):
            await bridge.refresh(
                "org.waveflow/synthetic",
                {"stations": [{
                    "station_ref": {"provider_key": "foreign", "provider_station_id": "one"},
                    "name": "Foreign",
                }]},
                owned_schemes={"synthetic"}, now=300,
            )
        with self.assertRaises(PluginError):
            await bridge.refresh(
                "org.waveflow/synthetic",
                {"stations": [{
                    "station_ref": {"provider_key": "synthetic", "provider_station_id": "one"},
                    "name": "A", "metadata": {"too_big": "x" * 2049},
                }]},
                owned_schemes={"synthetic"}, now=300,
            )
        identity = MediaSourceIdentity("radio", "org.waveflow/synthetic", "one")
        self.assertNotEqual(media_source_id_for(identity), media_source_id_for(
            MediaSourceIdentity("iptv", "org.waveflow/synthetic", "one"),
        ))
        source = {"url": "https://example.test/live.m3u8", "source_type": "hls"}
        self.assertEqual(source_id_for(source), source_id_for(dict(source)))
        self.assertEqual(source_revision_for(source), source_revision_for(dict(source)))
        self.assertEqual(
            media_source_revision_for({"reference": "station:one", "playback": "hls"}),
            media_source_revision_for({"playback": "hls", "reference": "station:one"}),
        )
        self.assertNotEqual(
            media_source_revision_for({"reference": "station:one", "playback": "hls"}),
            media_source_revision_for({"reference": "station:one", "playback": "audio"}),
        )

    async def test_domain_media_dispatcher_uses_signed_radio_stream_handle(self):
        from routers import media_proxy
        from security.dependencies import MediaAccessContext
        from security.proxy_handles import decode_for_kind

        previous_main = sys.modules.get("main")
        sys.modules["main"] = types.SimpleNamespace()
        try:
            response = await media_proxy._serve_resolved_source_playlist(
                resolved_url="https://audio.example.test/live.mp3",
                resolved_st="audio_http",
                custom_ua="WaveFlow/Radio",
                referer="https://audio.example.test/",
                cookie="",
                no_ua=False,
                canonical_key="radio_station_one",
                source_id="src_radio_one",
                source_revision="revision-one",
                access=MediaAccessContext(source="anonymous"),
                domain="radio",
                handle_ttl=60,
            )
        finally:
            if previous_main is None:
                sys.modules.pop("main", None)
            else:
                sys.modules["main"] = previous_main
        self.assertEqual(response.status_code, 307)
        handle = response.headers["location"].rsplit("/", 1)[-1]
        payload = decode_for_kind(handle, "stream")
        self.assertEqual(payload.src_id, "radio:src_radio_one")
        self.assertTrue(payload.src.startswith("radio:station:"))


if __name__ == "__main__":
    unittest.main()
