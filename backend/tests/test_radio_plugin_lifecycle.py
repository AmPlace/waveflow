import asyncio
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import httpx
from tests import test_market_plugin_lifecycle as lifecycle_tests


class RadioPluginLifecycleTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = lifecycle_tests.MarketPluginLifecycleTest()
        await self.fixture.asyncSetUp()
        import automation
        import radio_tasks

        task_types = patch.multiple(
            radio_tasks, AutomationTaskDefinition=automation.AutomationTaskDefinition,
            AutomationHandlerResult=automation.AutomationHandlerResult,
        )
        task_types.start()
        self.addCleanup(task_types.stop)
        from automation import AutomationRegistry, AutomationRepository, AutomationService
        from plugin_production import ProductionPluginSubsystem
        from provider_resolver import ProviderResolver

        self.service = self.fixture.service
        self.service.command_factory = lambda manifest, artifact: [
            sys.executable, str(artifact), "--identity", manifest.identity,
            "--version", manifest.version, "--scheme", "synthetic", "--radio-owned",
        ]
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(500)))
        self.subsystem = ProductionPluginSubsystem(
            self.service, self.service.trust_policy, Path(self.fixture.tmp.name), self.client,
            ProviderResolver(runtime=self.fixture.runtime), None,
        )
        self.automation = AutomationService(
            registry=AutomationRegistry(), repository=AutomationRepository(self.fixture.db),
        )
        self.subsystem.automation_service = self.automation
        await self.automation.start()

    async def asyncTearDown(self):
        self.subsystem._shutting_down = True
        await self.automation.stop()
        await self.subsystem.shutdown()
        await self.client.aclose()
        await self.fixture.asyncTearDown()

    def package(self):
        package = self.fixture.package(schemes=("synthetic",))
        manifest = package["plugin_manifest"]
        manifest["provider_contracts"].append({
            "contract": "radio_provider", "contract_version": "1.0", "features": ["catalog", "resolve_stream"],
        })
        manifest["owned_schemes"] = [{"scheme": "synthetic", "contract": "radio_provider"}]
        manifest["capabilities"].extend(["radio.catalog", "radio.resolve_stream"])
        return package

    async def test_normal_install_populates_catalog_without_restart_and_disable_preserves_config(self):
        identity = "org.waveflow/fixture-multi-provider"
        task_id = f"radio_refresh:catalog:{identity}"
        await self.service.install_from_packages([self.package()], identity)
        self.assertIn(task_id, self.automation.registry)

        async def catalog_ready():
            while not await self.fixture.db.list_radio_stations():
                await asyncio.sleep(0.01)

        await asyncio.wait_for(catalog_ready(), timeout=5)
        self.assertEqual(len(await self.fixture.db.list_radio_stations()), 2)
        await self.automation.repository.update_config(task_id, interval_seconds=1234)
        await self.service.disable(identity)
        self.assertNotIn(task_id, self.automation.registry)
        self.assertEqual((await self.automation.repository.get_config(task_id)).interval_seconds, 1234)
        await self.service.enable(identity)
        self.assertIn(task_id, self.automation.registry)
        self.assertEqual((await self.automation.repository.get_config(task_id)).interval_seconds, 1234)
        await self.service.uninstall(identity)
        self.assertNotIn(task_id, self.automation.registry)
        self.assertIsNone(await self.automation.repository.get_config(task_id))

    async def test_domain_sync_failure_does_not_hold_committed_install_open(self):
        from unittest.mock import patch

        identity = "org.waveflow/fixture-multi-provider"
        task_id = f"radio_refresh:catalog:{identity}"
        sync = self.subsystem._sync_radio_tasks
        calls = 0

        async def fail_once():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("fixture reconciliation failure")
            await sync()

        self.fixture.runtime.lifecycle_callback = None
        with patch.object(self.subsystem, "_sync_radio_tasks", fail_once):
            installed = await asyncio.wait_for(self.service.install_from_packages([self.package()], identity), timeout=5)
            self.assertEqual(installed["lifecycle_state"], "active")

            async def reconciled():
                while task_id not in self.automation.registry:
                    await asyncio.sleep(0.01)

            await asyncio.wait_for(reconciled(), timeout=5)
            self.assertGreaterEqual(calls, 2)


if __name__ == "__main__":
    unittest.main()
