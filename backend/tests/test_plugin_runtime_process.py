from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

from plugin_runtime import LifecycleState, PermissionPolicy, PluginError, PluginRuntime, validate_manifest
from plugin_channel_catalog import DynamicChannelCatalog


FIXTURE = Path(__file__).parent / "fixtures" / "synthetic_plugin.py"


def manifest_data(*, scheme="synthetic", version="1.0.0", permissions=None, catalog=False,
                  radio_owned=True):
    contracts = [
        {"contract": "tv_provider", "contract_version": "1.0", "features": ["resolve_stream"]},
        {"contract": "radio_provider", "contract_version": "1.0", "features": ["catalog", "resolve_stream"]},
    ]
    if catalog:
        contracts.append({"contract": "channel_catalog", "contract_version": "1.0", "features": ["discover"]})
    return {
        "manifest_version": 1,
        "publisher_id": "org.waveflow",
        "plugin_id": "synthetic",
        "display_name": "Synthetic",
        "version": version,
        "plugin_api_version": "1.0",
        "core_version_range": ">=0.1.0 <1.0.0",
        "provider_contracts": contracts,
        # Production Radio requests must have an explicitly Radio-owned
        # scheme. Catalog-only cases below deliberately use a TV-owned
        # fixture scheme because that is the Channel Catalog contract.
        "owned_schemes": [{"scheme": scheme, "contract": "radio_provider" if radio_owned else "tv_provider"}],
        "capabilities": ["tv.resolve_stream", "radio.catalog", "radio.resolve_stream"]
                      + (["channel_catalog.discover"] if catalog else []),
        "permissions": permissions or {},
        "runtime": {"type": "subprocess", "ipc": "stdio_framed_json_v1"},
        "artifacts": [{
            "os": "linux",
            "arch": "x86_64",
            "runtime": "native",
            "entrypoint": "synthetic-plugin",
            "sha256": "0" * 64,
            "size_bytes": 1,
            "signature": {"algorithm": "ed25519", "key_id": "fixture", "value": "fixture"},
        }],
        "dependencies": [],
        "state_schema_version": 1,
    }


def command(mode="normal", *, version="1.0.0", scheme="synthetic", permissions="", radio_owned=True):
    return [sys.executable, str(FIXTURE), "--mode", mode, "--version", version, "--scheme", scheme,
            "--permissions", permissions] + (["--radio-owned"] if radio_owned else [])


class PluginRuntimeProcessTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.runtimes = []

    async def asyncTearDown(self):
        await asyncio.gather(*(runtime.shutdown() for runtime in self.runtimes), return_exceptions=True)

    def runtime(self, **kwargs):
        value = PluginRuntime(**kwargs); self.runtimes.append(value); return value

    async def active(self, *, mode="normal", version="1.0.0", scheme="synthetic", permissions=None,
                     policy=None):
        runtime = self.runtime(permission_policy=policy or PermissionPolicy())
        radio_owned = not mode.startswith("catalog")
        manifest = validate_manifest(manifest_data(
            scheme=scheme, version=version, permissions=permissions, catalog=mode.startswith("catalog"),
            radio_owned=radio_owned,
        ))
        instance = runtime.install(manifest, command(mode, version=version, scheme=scheme,
                                                     permissions=",".join((permissions or {}).keys()),
                                                     radio_owned=radio_owned))
        await runtime.enable(instance)
        return runtime, instance

    async def test_start_hello_health_tv_radio_and_shutdown(self):
        runtime, instance = await self.active()
        self.assertEqual(instance.state, LifecycleState.HEALTHY_ACTIVE)
        tv = await runtime.request(instance, "tv.resolve_stream", {"reference": {"scheme": "synthetic", "kind": "channel", "resource_id": "one", "query": {}}})
        self.assertEqual(tv["transport"], "hls")
        catalog = await runtime.request(instance, "radio.catalog", {})
        self.assertEqual([v["name"] for v in catalog["stations"]], ["Same Name", "Same Name"])
        self.assertNotEqual(catalog["stations"][0]["station_ref"], catalog["stations"][1]["station_ref"])
        radio = await runtime.request(instance, "radio.resolve_stream", {"station_ref": catalog["stations"][0]["station_ref"]})
        self.assertEqual(radio["transport"], "audio_http")
        await runtime.disable(instance)
        self.assertEqual(instance.state, LifecycleState.INSTALLED_DISABLED)
        self.assertIsNotNone(instance.process.exit_code)

    async def test_optional_channel_catalog_discovery_is_validated_over_ipc(self):
        runtime, instance = await self.active(mode="catalog")
        result = await runtime.request(instance, "channel_catalog.discover", {})
        self.assertEqual([item["external_id"] for item in result["items"]], ["event-1", "channel-2"])
        self.assertEqual(result["items"][0]["metadata"]["identity"], {"source": "fixture"})

    async def test_channel_catalog_foreign_scheme_and_duplicate_identity_fail_closed(self):
        runtime, foreign = await self.active(mode="catalog_foreign", scheme="foreign-fixture")
        with self.assertRaises(PluginError) as foreign_error:
            await runtime.request(foreign, "channel_catalog.discover", {})
        self.assertEqual(foreign_error.exception.code, "INVALID_PLUGIN_RESPONSE")

        runtime2, duplicate = await self.active(mode="catalog_duplicate", scheme="duplicate-fixture")
        with self.assertRaises(PluginError) as duplicate_error:
            await runtime2.request(duplicate, "channel_catalog.discover", {})
        self.assertEqual(duplicate_error.exception.code, "INVALID_PLUGIN_RESPONSE")

    async def test_channel_catalog_refresh_survives_plugin_crash_and_restart(self):
        runtime, instance = await self.active(mode="catalog_then_crash")
        projection = DynamicChannelCatalog(stale_grace_seconds=30)
        first = await projection.refresh_plugin("org.waveflow/synthetic", runtime, instance, now=100)
        self.assertEqual([item["state"] for item in first["items"]], ["active", "active"])

        failed = await projection.refresh_plugin("org.waveflow/synthetic", runtime, instance, now=101)
        self.assertEqual(failed["failures"]["count"], 1)
        self.assertEqual([item["state"] for item in failed["visible_items"]], ["active", "active"])

        async def no_sleep(_delay):
            return None

        runtime.sleep = no_sleep
        await asyncio.sleep(0.05)
        await runtime.restart(instance)
        recovered = await projection.refresh_plugin("org.waveflow/synthetic", runtime, instance, now=102)
        self.assertEqual(recovered["failures"], {})
        self.assertEqual([item["identity"] for item in recovered["items"]], [
            "org.waveflow/synthetic::channel-2", "org.waveflow/synthetic::event-1",
        ])

    async def test_all_transport_descriptors(self):
        runtime, instance = await self.active()
        for transport in ("hls", "dash", "http_flv", "mpegts", "rtsp", "audio_http", "probe_only"):
            with self.subTest(transport=transport):
                result = await runtime.request(instance, "tv.resolve_stream", {"transport": transport})
                self.assertEqual(result["transport"], transport)

    async def test_invalid_descriptor_and_hello_permission_escalation(self):
        runtime, instance = await self.active(mode="invalid_descriptor")
        with self.assertRaises(PluginError) as invalid:
            await runtime.request(instance, "tv.resolve_stream", {})
        self.assertEqual(invalid.exception.code, "INVALID_PLUGIN_RESPONSE")

        runtime2 = self.runtime(permission_policy=PermissionPolicy(frozenset()))
        manifest = validate_manifest(manifest_data(permissions={"cache": {}}))
        escalation = runtime2.install(manifest, command("hello_escalation", permissions="cache"))
        with self.assertRaises(PluginError) as denied:
            await runtime2.enable(escalation)
        self.assertEqual(denied.exception.code, "INVALID_PLUGIN_RESPONSE")
        self.assertEqual(escalation.state, LifecycleState.UNHEALTHY)

    async def test_timeout_cancel_late_response_and_followup(self):
        runtime, instance = await self.active(mode="hang")
        with self.assertRaises(PluginError) as timed_out:
            await runtime.request(instance, "tv.resolve_stream", {}, timeout=0.05)
        self.assertEqual(timed_out.exception.code, "PLUGIN_TIMEOUT")
        await asyncio.sleep(0.05)
        self.assertGreaterEqual(instance.process.protocol_violations, 1)
        catalog = await runtime.request(instance, "radio.catalog", {})
        self.assertEqual(len(catalog["stations"]), 2)

    async def test_caller_cancellation_isolated_from_followup(self):
        runtime, instance = await self.active(mode="hang")
        task = asyncio.create_task(runtime.request(instance, "tv.resolve_stream", {}, timeout=5))
        await asyncio.sleep(0.02)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.02)
        self.assertGreaterEqual(instance.process.protocol_violations, 1)
        self.assertEqual(len((await runtime.request(instance, "radio.catalog", {}))["stations"]), 2)

    async def test_unknown_duplicate_and_malformed_response_are_isolated(self):
        runtime, wrong = await self.active(mode="wrong_request_id")
        with self.assertRaises(PluginError) as timed_out:
            await runtime.request(wrong, "tv.resolve_stream", {}, timeout=0.05)
        self.assertEqual(timed_out.exception.code, "PLUGIN_TIMEOUT")
        self.assertGreaterEqual(wrong.process.protocol_violations, 1)

        runtime2, duplicate = await self.active(mode="duplicate_response", scheme="duplicate")
        await runtime2.request(duplicate, "tv.resolve_stream", {})
        await asyncio.sleep(0.02)
        self.assertGreaterEqual(duplicate.process.protocol_violations, 1)

        runtime3, malformed = await self.active(mode="malformed_frame", scheme="malformed")
        with self.assertRaises(PluginError) as invalid:
            await runtime3.request(malformed, "tv.resolve_stream", {}, timeout=0.2)
        self.assertEqual(invalid.exception.code, "INVALID_PLUGIN_RESPONSE")
        await asyncio.sleep(0.05)
        self.assertEqual(malformed.state, LifecycleState.UNHEALTHY)
        with self.assertRaises(PluginError) as missing:
            runtime3.registry.route("malformed")
        self.assertEqual(missing.exception.code, "SCHEME_UNOWNED")

    async def test_crash_fails_pending_unregisters_and_other_plugin_survives(self):
        runtime = self.runtime()
        crashing = runtime.install(validate_manifest(manifest_data(scheme="crashing")), command("crash", scheme="crashing"))
        healthy = runtime.install(validate_manifest(manifest_data(scheme="healthy")), command("normal", scheme="healthy"))
        await runtime.enable(crashing); await runtime.enable(healthy)
        with self.assertRaises(PluginError) as crashed:
            await runtime.request(crashing, "tv.resolve_stream", {}, timeout=1)
        self.assertEqual(crashed.exception.code, "PLUGIN_CRASHED")
        await asyncio.sleep(0.05)
        self.assertEqual(crashing.state, LifecycleState.UNHEALTHY)
        with self.assertRaises(PluginError) as missing:
            runtime.registry.route("crashing")
        self.assertEqual(missing.exception.code, "SCHEME_UNOWNED")
        self.assertEqual((await runtime.request(healthy, "tv.resolve_stream", {}))["transport"], "hls")

    async def test_restart_quarantine_with_injected_clock_and_sleep(self):
        now = [100.0]; sleeps = []
        async def fake_sleep(value): sleeps.append(value)
        runtime = self.runtime(clock=lambda: now[0], sleep=fake_sleep, max_starts=3)
        instance = runtime.install(validate_manifest(manifest_data()), command("crash"))
        await runtime.enable(instance)
        with self.assertRaises(PluginError):
            await runtime.request(instance, "tv.resolve_stream", {}, timeout=1)
        await asyncio.sleep(0.02)
        # First restart is allowed (initial start counts as one), then crash it again.
        await runtime.restart(instance)
        with self.assertRaises(PluginError):
            await runtime.request(instance, "tv.resolve_stream", {}, timeout=1)
        await asyncio.sleep(0.02)
        await runtime.restart(instance)
        with self.assertRaises(PluginError):
            await runtime.request(instance, "tv.resolve_stream", {}, timeout=1)
        await asyncio.sleep(0.02)
        with self.assertRaises(PluginError) as quarantined:
            await runtime.restart(instance)
        self.assertEqual(quarantined.exception.code, "PLUGIN_QUARANTINED")
        self.assertEqual(instance.state, LifecycleState.QUARANTINED)
        self.assertEqual(sleeps, [1, 2])
        runtime.registry.recover(instance)
        self.assertEqual(instance.state, LifecycleState.INSTALLED_DISABLED)

    async def test_candidate_success_and_failure_rollback(self):
        runtime = self.runtime()
        old = runtime.install(validate_manifest(manifest_data(version="1.0.0")), command("normal", version="1.0.0"))
        await runtime.enable(old)
        candidate = runtime.install(validate_manifest(manifest_data(version="1.1.0")), command("normal", version="1.1.0"))
        await runtime.activate_candidate(old, candidate)
        self.assertIs(runtime.registry.route("synthetic"), candidate)
        self.assertEqual(old.state, LifecycleState.INSTALLED_DISABLED)

        failed = runtime.install(validate_manifest(manifest_data(version="1.2.0")), command("health_fail", version="1.2.0"))
        with self.assertRaises(PluginError):
            await runtime.activate_candidate(candidate, failed)
        self.assertIs(runtime.registry.route("synthetic"), candidate)
        self.assertEqual(candidate.state, LifecycleState.HEALTHY_ACTIVE)

    async def test_uninstall_removes_stopped_instance(self):
        runtime, instance = await self.active()
        await runtime.uninstall(instance)
        self.assertEqual(instance.state, LifecycleState.ABSENT)
        self.assertNotIn(instance.instance_id, runtime.registry.instances)

    async def test_sanitized_environment_and_explicit_working_directory_reach_process(self):
        runtime = self.runtime()
        manifest = validate_manifest(manifest_data())
        with tempfile.TemporaryDirectory() as directory:
            instance = runtime.install(manifest, command("ambient_probe"),
                                       environment={"PATH": "/usr/bin"}, working_directory=directory)
            await runtime.enable(instance)
            result = await runtime.request(instance, "tv.resolve_stream", {})
            diagnostics = result["provider_diagnostics"]
            self.assertEqual(Path(diagnostics["cwd"]).resolve(), Path(directory).resolve())
            self.assertEqual({key: diagnostics[key] for key in ("secret", "path")},
                             {"secret": "", "path": "/usr/bin"})

    async def test_metadata_survives_ipc_bridge_and_restart_without_authority(self):
        from provider_resolver import ProviderResolver

        runtime, instance = await self.active(mode="metadata_then_crash")
        descriptor = await runtime.request(instance, "tv.resolve_stream", {})
        bridged = ProviderResolver._bridge_descriptor("synthetic", descriptor)
        self.assertEqual(bridged["provider_diagnostics"], {
            "identity": {"channel": "fixture", "video": "v-1"}, "page_live": True,
        })
        self.assertEqual(bridged["probe_hints"], {
            "preferred_probe": "http_segment", "alternates": ["ffmpeg", {"reason": "fixture"}],
        })
        self.assertNotIn("security", bridged)
        self.assertNotIn("commands", bridged)

        with self.assertRaises(PluginError) as crashed:
            await runtime.request(instance, "tv.resolve_stream", {})
        self.assertEqual(crashed.exception.code, "PLUGIN_CRASHED")
        await asyncio.sleep(0.05)
        await runtime.restart(instance)
        restarted = await runtime.request(instance, "tv.resolve_stream", {})
        self.assertEqual(restarted["provider_diagnostics"], descriptor["provider_diagnostics"])
        self.assertEqual(restarted["probe_hints"], descriptor["probe_hints"])
