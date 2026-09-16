from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

from plugin_production import ProductionPluginSubsystem, RADIO_CATALOG_REQUEST_TIMEOUT_SECONDS
from plugin_runtime import LifecycleState


class RadioCatalogCloseoutTest(unittest.IsolatedAsyncioTestCase):
    async def test_production_catalog_refresh_uses_extended_request_budget(self):
        identity = "org.waveflow/yunting"

        class Runtime:
            def __init__(self):
                self.calls = []

            async def request(self, instance, method, payload, *, timeout):
                self.calls.append((instance, method, payload, timeout))
                return {"stations": []}

        class Bridge:
            async def refresh(self, owner, catalog, *, owned_schemes, now, generation):
                return {"status": "success", "published": len(catalog["stations"])}

        runtime = Runtime()
        instance = SimpleNamespace(
            state=LifecycleState.HEALTHY_ACTIVE,
            manifest=SimpleNamespace(owned_schemes=(("yunting", "radio_provider"),)),
        )
        subsystem = object.__new__(ProductionPluginSubsystem)
        subsystem.service = SimpleNamespace(_active={identity: instance}, runtime=runtime)
        subsystem.radio_catalog = Bridge()
        subsystem._radio_catalog_locks = {}
        database_module = ProductionPluginSubsystem.refresh_radio_catalog.__globals__["db"]

        with mock.patch.object(database_module, "begin_radio_catalog_refresh", new=mock.AsyncMock(return_value=7)):
            result = await subsystem.refresh_radio_catalog(identity)

        self.assertEqual(result["status"], "success")
        self.assertEqual(runtime.calls[0][1:], ("radio.catalog", {}, RADIO_CATALOG_REQUEST_TIMEOUT_SECONDS))
        self.assertEqual(RADIO_CATALOG_REQUEST_TIMEOUT_SECONDS, 30.0)

    async def test_catalog_refresh_lock_remains_provider_scoped(self):
        subsystem = object.__new__(ProductionPluginSubsystem)
        subsystem._radio_catalog_locks = {}
        self.assertIs(subsystem._radio_catalog_lock("one"), subsystem._radio_catalog_lock("one"))
        self.assertIsNot(subsystem._radio_catalog_lock("one"), subsystem._radio_catalog_lock("two"))
        self.assertIsInstance(subsystem._radio_catalog_lock("one"), asyncio.Lock)


if __name__ == "__main__":
    unittest.main()
