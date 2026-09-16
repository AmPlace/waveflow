from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import database
from automation import AutomationHandlerResult
from bundled_plugins.yunting.plugin import CATALOG_PROVINCES, PROVINCES, Provider
from plugin_production import ProductionPluginSubsystem
from plugin_runtime import PluginError as CorePluginError
from plugin_runtime.registry import LifecycleState
from radio_core import RadioCatalogBridge
from radio_tasks import RADIO_CATALOG_TASK_TYPE, create_radio_task_definition
from routers.radio import _active_radio_owner_identities
from waveflow_plugin_sdk import PluginError, ResolveContext


class _Response:
    status = 200

    def __init__(self, records):
        self.body = {"data": records}


class _YuntingCapabilities:
    def __init__(self, records_by_region, *, failed_regions=()):
        self.records_by_region = records_by_region
        self.failed_regions = set(failed_regions)
        self.calls = []

    def managed_http(self, url, *, query, headers, response_mode, timeout):
        region = str(query["provinceCode"])
        self.calls.append((region, timeout))
        if region in self.failed_regions:
            raise PluginError(
                "TEMPORARY_UPSTREAM_FAILURE",
                "Yunting upstream timeout",
                retryable=True,
                category="timeout",
            )
        return _Response(self.records_by_region.get(region, []))


def _context(capabilities):
    return ResolveContext("test-yunting-resilience", 0, {}, capabilities, lambda: False)


def _complete_records():
    records = {}
    station_index = 0
    for region_index, region in enumerate(PROVINCES):
        count = 29 if region_index < 6 else 30
        records[region] = []
        for _ in range(count):
            station_index += 1
            records[region].append({
                "contentId": f"local-{station_index}",
                "title": f"Local {station_index}",
                "image": f"https://img.radio.cn/{station_index}.png",
            })
    records["0"] = [
        {
            "contentId": f"cnr-{index}",
            "title": f"CNR {index}",
            "image": f"https://img.radio.cn/cnr-{index}.png",
        }
        for index in range(19)
    ]
    return records


class YuntingCatalogContractTest(unittest.TestCase):
    def test_catalog_merges_31_provinces_and_central_without_identity_change(self):
        capabilities = _YuntingCapabilities(_complete_records())
        catalog = Provider().catalog({}, _context(capabilities))

        self.assertEqual(len(CATALOG_PROVINCES), len(PROVINCES) + 1)
        self.assertEqual(len(capabilities.calls), 32)
        self.assertEqual(len(catalog["stations"]), 943)
        station_ids = {
            item["station_ref"]["provider_station_id"] for item in catalog["stations"]
        }
        self.assertEqual(len(station_ids), 943)
        central = [item for item in catalog["stations"] if item["metadata"]["province_code"] == "0"]
        self.assertEqual(len(central), 19)
        self.assertTrue(all(item["logo_url"].startswith("https://") for item in catalog["stations"]))

    def test_duplicate_content_id_keeps_first_stable_identity(self):
        records = _complete_records()
        records["0"][0]["contentId"] = records[PROVINCES[0]][0]["contentId"]
        capabilities = _YuntingCapabilities(records)

        catalog = Provider().catalog({}, _context(capabilities))

        self.assertEqual(len(catalog["stations"]), 942)
        duplicate = catalog["stations"][0]
        self.assertEqual(duplicate["metadata"]["province_code"], PROVINCES[0])

    def test_partial_region_failure_rejects_incomplete_snapshot_with_region(self):
        capabilities = _YuntingCapabilities(_complete_records(), failed_regions={"350000"})

        with self.assertRaises(PluginError) as raised:
            Provider().catalog({}, _context(capabilities))

        self.assertEqual(raised.exception.code, "TEMPORARY_UPSTREAM_FAILURE")
        self.assertIn("350000", raised.exception.message)
        self.assertIn("catalog_incomplete", raised.exception.details["provider_code"])

    def test_relative_or_missing_official_image_uses_empty_logo_fallback(self):
        records = _complete_records()
        records["0"][0]["image"] = "/relative/logo.png"
        records["0"][1].pop("image")
        capabilities = _YuntingCapabilities(records)

        catalog = Provider().catalog({}, _context(capabilities))

        central = {
            item["station_ref"]["provider_station_id"]: item
            for item in catalog["stations"]
            if item["metadata"]["province_code"] == "0"
        }
        self.assertEqual(central["cnr-0"]["logo_url"], "")
        self.assertEqual(central["cnr-1"]["logo_url"], "")


class RadioCatalogResilienceTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = os.environ.get("WAVEFLOW_DB_PATH")
        os.environ["WAVEFLOW_DB_PATH"] = str(Path(self.tmp.name) / "waveflow.db")
        await database.initialize()

    async def asyncTearDown(self):
        if self.old_db is None:
            os.environ.pop("WAVEFLOW_DB_PATH", None)
        else:
            os.environ["WAVEFLOW_DB_PATH"] = self.old_db
        self.tmp.cleanup()

    @staticmethod
    def _catalog(name="Station"):
        return {"stations": [{
            "station_ref": {"provider_key": "synthetic", "provider_station_id": name.lower()},
            "name": name,
            "ttl_seconds": 300,
        }]}

    async def test_last_good_catalog_is_stale_but_visible_for_24_hours(self):
        bridge = RadioCatalogBridge()
        await bridge.refresh("org.waveflow/synthetic", self._catalog(), owned_schemes={"synthetic"}, now=100)
        generation = (await database.list_radio_catalog_states())[0]["published_generation"]

        await bridge.record_failure("org.waveflow/synthetic", PluginError(
            "PLUGIN_TIMEOUT", "Plugin request timed out", retryable=True, category="timeout",
        ))

        visible = await database.list_radio_stations(now_unix=100 + 300 + 600)
        self.assertEqual(len(visible), 1)
        self.assertEqual(visible[0]["lifecycle_state"], "stale")
        state = (await database.list_radio_catalog_states(now_unix=100 + 300 + 600))[0]
        self.assertEqual(state["status"], "stale")
        self.assertEqual(state["published_generation"], generation)
        self.assertIn("PLUGIN_TIMEOUT", state["last_error"])

        expired = await database.list_radio_stations(now_unix=100 + 300 + 86401)
        self.assertEqual(expired, [])
        expired_state = (await database.list_radio_catalog_states(
            now_unix=100 + 300 + 86401,
        ))[0]
        self.assertEqual(expired_state["status"], "expired")

    async def test_complete_yunting_catalog_reaches_durable_rows_with_logos(self):
        capabilities = _YuntingCapabilities(_complete_records())
        catalog = Provider().catalog({}, _context(capabilities))
        result = await RadioCatalogBridge().refresh(
            "org.waveflow/yunting", catalog, owned_schemes={"yunting"}, now=100,
        )

        stations = await database.list_radio_stations(owner_identity="org.waveflow/yunting", now_unix=101)
        self.assertEqual(result["published"], 943)
        self.assertEqual(len(stations), 943)
        self.assertEqual(
            sum(item["metadata"]["province_code"] == "0" for item in stations),
            19,
        )
        self.assertTrue(all(item["logo_url"].startswith("https://") for item in stations))

    async def test_success_clears_stale_state_and_publishes_new_generation(self):
        bridge = RadioCatalogBridge()
        await bridge.refresh("org.waveflow/synthetic", self._catalog("Old"), owned_schemes={"synthetic"}, now=100)
        await bridge.record_failure("org.waveflow/synthetic", RuntimeError("upstream unavailable"))
        before = (await database.list_radio_catalog_states())[0]["published_generation"]

        result = await bridge.refresh(
            "org.waveflow/synthetic", self._catalog("New"), owned_schemes={"synthetic"}, now=200,
        )

        self.assertEqual(result["status"], "success")
        state = (await database.list_radio_catalog_states(now_unix=201))[0]
        self.assertEqual(state["status"], "success")
        self.assertGreater(state["published_generation"], before)
        self.assertEqual(state["last_error"], "")
        self.assertEqual((await database.list_radio_stations(now_unix=201))[0]["name"], "New")

    async def test_catalog_task_propagates_single_error_summary(self):
        class Subsystem:
            async def refresh_radio_catalog(self, _identity):
                return {"status": "failed", "error": "PLUGIN_TIMEOUT: Plugin request timed out"}

        definition = create_radio_task_definition(
            "org.waveflow/synthetic", RADIO_CATALOG_TASK_TYPE, Subsystem(),
        )
        result = await definition.handler(SimpleNamespace(stop_requested=lambda: False))

        self.assertIsInstance(result, AutomationHandlerResult)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.failed_count, 1)
        self.assertIn("PLUGIN_TIMEOUT", result.error)

    async def test_catalog_refresh_retries_retryable_plugin_failure_once(self):
        identity = "org.waveflow/synthetic"
        instance = SimpleNamespace(
            state=LifecycleState.HEALTHY_ACTIVE,
            manifest=SimpleNamespace(owned_schemes=(("synthetic", "radio_provider"),)),
        )

        class Runtime:
            def __init__(self):
                self.calls = 0

            async def request(self, _instance, _method, _payload, *, timeout):
                self.calls += 1
                if self.calls == 1:
                    raise CorePluginError("PLUGIN_TIMEOUT", "temporary timeout", retryable=True, category="timeout")
                return {"stations": [{
                    "station_ref": {"provider_key": "synthetic", "provider_station_id": "one"},
                    "name": "One",
                }]}

        class Bridge:
            async def refresh(self, *_args, **_kwargs):
                return {"status": "success", "published": 1}

        runtime = Runtime()
        subsystem = object.__new__(ProductionPluginSubsystem)
        subsystem.service = SimpleNamespace(
            _active={identity: instance}, runtime=runtime,
        )
        subsystem.radio_catalog = Bridge()
        subsystem._radio_catalog_locks = {}
        subsystem._shutting_down = False

        database_module = ProductionPluginSubsystem.refresh_radio_catalog.__globals__["db"]
        with (
            mock.patch.object(database_module, "begin_radio_catalog_refresh", new=mock.AsyncMock(return_value=1)),
            mock.patch("plugin_production.asyncio.sleep", new=mock.AsyncMock()),
        ):
            result = await subsystem.refresh_radio_catalog(identity)

        self.assertEqual(result["status"], "success")
        self.assertEqual(runtime.calls, 2)


class RadioOwnerVisibilityTest(unittest.TestCase):
    @staticmethod
    def _request(state=LifecycleState.HEALTHY_ACTIVE, *, health="healthy", radio=True):
        contract = SimpleNamespace(
            contract="radio_provider" if radio else "tv_provider",
            features=frozenset({"catalog"}),
        )
        instance = SimpleNamespace(
            state=state,
            health=health,
            manifest=SimpleNamespace(
                provider_contracts=(contract,),
                owned_schemes=(("yunting", "radio_provider"),) if radio else (("yunting", "tv_provider"),),
            ),
        )
        return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
            plugin_subsystem=SimpleNamespace(service=SimpleNamespace(
                _active={"org.waveflow/yunting": instance},
            )),
        )))

    def test_only_healthy_active_radio_owner_is_visible(self):
        self.assertEqual(
            _active_radio_owner_identities(self._request()),
            {"org.waveflow/yunting"},
        )
        self.assertEqual(
            _active_radio_owner_identities(self._request(LifecycleState.INSTALLED_DISABLED)),
            set(),
        )
        self.assertEqual(
            _active_radio_owner_identities(self._request(radio=False)),
            set(),
        )


if __name__ == "__main__":
    unittest.main()
