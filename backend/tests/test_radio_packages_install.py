import asyncio
import base64
import json
from pathlib import Path
import unittest
from urllib.parse import urlsplit
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import httpx

from tests.plugin_sources import plugin_source
from tests import test_plugin_developer_sideload as developer_tests


class RadioPackagesInstallTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = developer_tests.DeveloperSideloadLifecycleTest()
        await self.fixture.asyncSetUp()
        import automation
        import radio_tasks

        task_types = patch.multiple(
            radio_tasks, AutomationTaskDefinition=automation.AutomationTaskDefinition,
            AutomationHandlerResult=automation.AutomationHandlerResult,
        )
        task_types.start()
        self.addCleanup(task_types.stop)

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    async def test_signed_fixture_packages_install_schedule_resolve_and_disable(self):
        from automation import AutomationRegistry, AutomationRepository, AutomationService
        from plugin_market import manifest_signature_payload
        from plugin_runtime import PluginError, validate_manifest
        from waveflow_plugin_cli import build_project

        subsystem = await self.fixture._make_subsystem()
        key = Ed25519PrivateKey.generate()
        public = base64.b64encode(key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw,
        )).decode()
        subsystem.trust_policy.replace([{
            "publisher_id": "org.waveflow", "key_id": "test-radio", "public_key": public,
            "trust_level": "official", "enabled": True, "require_manifest_signature": True,
        }])
        station = {"stationuuid": "01234567-89ab-cdef-0123-456789abcdef", "name": "Fixture",
                   "countrycode": "DE", "hls": 0, "url_resolved": "https://media.example/audio"}
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=[station])))
        subsystem.capability_gateway.client = client

        async def fixture_target(url, **policy):
            self.assertEqual(urlsplit(url).hostname, "all.api.radio-browser.info")
            self.assertEqual(policy["allowed_hosts"], ["all.api.radio-browser.info"])

        automation = AutomationService(registry=AutomationRegistry(), repository=AutomationRepository(self.fixture.db))
        subsystem.automation_service = automation
        await automation.start()
        hook_errors = []
        reconcile = subsystem.service.lifecycle_changed

        async def observed_reconcile(identity):
            try:
                await reconcile(identity)
            except Exception as error:
                hook_errors.append(f"{type(error).__name__}: {error}")
                raise

        subsystem.service.lifecycle_changed = observed_reconcile
        try:
            with patch.object(subsystem.capability_gateway, "_validate_http_target", fixture_target):
                for plugin_id, count in (("hk-sg-radio", 34), ("radiobrowser", 1)):
                    with self.subTest(plugin=plugin_id):
                        built = build_project(
                            plugin_source(plugin_id),
                            output=subsystem.download_root / plugin_id / "plugin.pyz",
                        )
                        data = json.loads(Path(built["manifest"]).read_text())
                        payload = Path(built["artifact"]).read_bytes()
                        for artifact in data["artifacts"]:
                            artifact["signature"] = {"algorithm": "ed25519", "key_id": "test-radio",
                                                     "value": base64.b64encode(key.sign(payload)).decode()}
                        manifest = validate_manifest(data)
                        identity = manifest.identity
                        package = {
                            "id": f"fixture::{plugin_id}", "kind": "plugin_package", "package_type": "plugin_package",
                            "version": "1.0.0", "plugin_manifest": data,
                            "manifest_signature": {"algorithm": "ed25519", "key_id": "test-radio",
                                "value": base64.b64encode(key.sign(manifest_signature_payload(manifest))).decode()},
                            "artifact_references": [{"sha256": built["sha256"], "local_path": built["artifact"]}],
                            "market_source": {"source_key": "fixture"},
                        }
                        install = asyncio.create_task(subsystem.service.install_from_packages([package], identity))
                        done, _ = await asyncio.wait({install}, timeout=30)
                        if not done:
                            subsystem.service.begin_shutdown()
                            await asyncio.gather(install, return_exceptions=True)
                            self.fail("Install did not converge: " + "; ".join(hook_errors[-2:]))
                        await install

                        async def ready():
                            while True:
                                rows = await self.fixture.db.list_radio_stations(owner_identity=identity)
                                if len(rows) == count:
                                    return rows
                                await asyncio.sleep(0.01)

                        rows = await asyncio.wait_for(ready(), timeout=5)
                        source_id = rows[0]["sources"][0]["source_id"]
                        resolved = await subsystem.radio_resolver.resolve_source(source_id, station_id=rows[0]["station_id"])
                        self.assertIn(resolved["source_type"], {"hls", "audio_http"})
                        await subsystem.disable(identity)
                        with self.assertRaises(PluginError):
                            await subsystem.radio_resolver.resolve_source(source_id, station_id=rows[0]["station_id"])
                        await subsystem.uninstall(identity)
                        self.assertNotIn(f"radio_refresh:catalog:{identity}", automation.registry)
        finally:
            subsystem._shutting_down = True
            await automation.stop()
            await client.aclose()


if __name__ == "__main__":
    unittest.main()
