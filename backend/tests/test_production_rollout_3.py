from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx


TARGETS = (
    ("hnntv", "org.waveflow/hnntv", "hnntv://hnws"),
    ("ptbtv", "org.waveflow/ptbtv", "ptbtv://pt1"),
)
PLAYSEEK = "20260811120000-20260811123000"
STREAMS = {
    "hnntv-live": "https://media.example/hnntv-rollout/live.m3u8",
    "hnntv-replay": "https://media.example/hnntv-rollout/replay.m3u8",
    "ptbtv": "https://media.example/ptbtv-rollout/live.m3u8",
}


def _clear_modules() -> None:
    for name in (
        "database", "market", "plugin_market", "plugin_permissions", "plugin_production",
        "official_plugin_distribution", "routers.plugins",
    ):
        sys.modules.pop(name, None)


class ProductionRollout3Test(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_env = {
            name: os.environ.get(name)
            for name in (
                "WAVEFLOW_DB_PATH", "WAVEFLOW_MODE", "WAVEFLOW_OFFICIAL_PLUGIN_BOOTSTRAP",
                "WAVEFLOW_OFFICIAL_PLUGIN_ROLLOUT",
            )
        }
        os.environ["WAVEFLOW_DB_PATH"] = str(Path(self.tmp.name) / "waveflow.db")
        os.environ["WAVEFLOW_MODE"] = "nas"
        os.environ["WAVEFLOW_OFFICIAL_PLUGIN_BOOTSTRAP"] = "1"
        os.environ["WAVEFLOW_OFFICIAL_PLUGIN_ROLLOUT"] = "0"
        _clear_modules()
        self.db = importlib.import_module("database")
        await self.db.initialize()
        self.provider_fixture = True
        self.requests: list[httpx.Request] = []

        def upstream(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if self.provider_fixture:
                if request.url.host == "www.ptbtv.com":
                    return httpx.Response(200, json=[{"m3u8": STREAMS["ptbtv"]}])
                if request.url.host == "www.hnntv.cn":
                    return httpx.Response(200, json={"resultSet": [{"schedules": [{
                        "id": "hnntv-rollout-replay",
                        "startDatetime": "2026-08-11 12:00:00",
                        "endDatetime": "2026-08-11 12:30:00",
                        "programName": "HNNTV rollout fixture",
                    }]}]})
                if request.url.host == "ps.hnntv.cn" and request.url.path.endswith("/wbPlayUrl"):
                    return httpx.Response(
                        200,
                        text='{"url":"https:\\/\\/media.example\\/hnntv-rollout\\/replay.m3u8"}',
                    )
            return httpx.Response(503, text="offline")

        self.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        self.legacy = mock.AsyncMock(return_value={"url": "https://legacy.example/live.m3u8"})
        self.subsystems = []
        self.safe = mock.patch("plugin_capabilities.assert_safe_target_url", new=mock.AsyncMock())
        self.safe.start()

    async def asyncTearDown(self):
        for subsystem in reversed(self.subsystems):
            await subsystem.shutdown()
        self.safe.stop()
        await self.client.aclose()
        for name, value in self.old_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        _clear_modules()
        self.tmp.cleanup()

    def _require_mac_runtime(self) -> None:
        from plugin_market import current_platform

        if current_platform() != ("macos", "arm64") or (sys.version_info.major, sys.version_info.minor) != (3, 14):
            self.skipTest("PLUGIN-PRODUCTION-ROLLOUT-3 requires macOS arm64 / CPython 3.14")

    async def _subsystem(self):
        from plugin_production import ProductionPluginSubsystem

        subsystem = await ProductionPluginSubsystem.create(
            root=Path(self.tmp.name) / "plugin-store", http_client=self.client,
        )

        def command_factory(manifest, artifact):
            command = [sys.executable, str(artifact), "--identity", manifest.identity,
                       "--version", manifest.version]
            if manifest.plugin_id == "hnntv":
                command.extend(["--fixture-live-url", STREAMS["hnntv-live"]])
            return tuple(command)

        def runtime_command_factory(manifest, artifact, environment):
            if manifest.plugin_id == "hnntv":
                return command_factory(manifest, artifact)
            python = str(environment.python) if environment else sys.executable
            command = [python, "-I", str(artifact), "--identity", manifest.identity,
                       "--version", manifest.version]
            if manifest.plugin_id == "ptbtv":
                # The provider-focused tests exercise curl-cffi's direct helper
                # with a deterministic Session.  Production rollout uses the
                # same signed artifact but a deterministic managed fallback.
                command.append("--fixture-direct-unavailable")
            return tuple(command)

        subsystem.service.command_factory = command_factory
        subsystem.service.runtime_command_factory = runtime_command_factory
        subsystem.provider_resolver.legacy_resolver = self.legacy
        self.subsystems.append(subsystem)
        return subsystem

    async def _restart(self, subsystem):
        await subsystem.shutdown()
        self.subsystems.remove(subsystem)
        restarted = await self._subsystem()
        await restarted.startup()
        return restarted

    async def _official_packages(self) -> list[dict]:
        from official_plugin_distribution import bundled_official_packages

        packages = bundled_official_packages()
        identities = {
            f"{item['plugin_manifest']['publisher_id']}/{item['plugin_manifest']['plugin_id']}"
            for item in packages
        }
        self.assertTrue({identity for _scheme, identity, _reference in TARGETS}.issubset(identities))
        return packages

    async def _install_approved(self, subsystem, identity: str, packages: list[dict]) -> dict:
        from plugin_runtime import PluginError

        plugin_id = identity.rsplit("/", 1)[1]
        with self.assertRaises(PluginError) as pending:
            await subsystem.install(identity, packages)
        self.assertEqual(pending.exception.code, "PERMISSION_APPROVAL_REQUIRED")
        self.assertIsNone(await self.db.get_plugin_installation("org.waveflow", plugin_id))

        await subsystem.approve_permission(identity, packages, "network.direct", "rollout-3-test")
        installed = await subsystem.install(identity, packages)
        projection = await subsystem.service.permission_projection(identity)
        self.assertEqual(projection["pending"], [])
        self.assertEqual(
            (installed["trust_state"], installed["source_key"], installed["active_version"],
             installed["lifecycle_state"], installed["enabled"]),
            ("official", "official", "1.0.0", "active", 1),
        )
        row = await self.db.get_plugin_installation("org.waveflow", plugin_id)
        self.assertEqual(json.loads(row["manifest_json"])["plugin_id"], plugin_id)
        self.assertEqual(row["source_package_id"], f"official::{plugin_id}-plugin")
        self.assertEqual(subsystem.service.runtime.registry.route(plugin_id).health, "healthy")
        return installed

    async def _resolve_plugin(self, subsystem, scheme: str, identity: str, reference: str) -> dict:
        before = self.legacy.await_count
        result = await subsystem.provider_resolver.resolve(reference, self.client)
        self.assertEqual(result["stream_descriptor_version"], "1.0")
        self.assertEqual(subsystem.provider_resolver.mode(scheme), "plugin")
        self.assertEqual(subsystem.service.runtime.registry.route(scheme).manifest.identity, identity)
        self.assertEqual(self.legacy.await_count, before)
        return result

    async def _assert_permission_revoke_requires_explicit_rollback(
        self, subsystem, scheme: str, identity: str, reference: str,
    ):
        from plugin_runtime import PluginError

        # The generic lifecycle guard deliberately rejects destructive
        # permission mutation while a Plugin owns a scheme.  This is the
        # safety boundary: ownership cannot remain Plugin-owned with a stale
        # or missing high-risk approval.
        with self.assertRaises(PluginError) as owned:
            await subsystem.revoke_permission(identity, "network.direct", "rollout-3-test")
        self.assertEqual(owned.exception.code, "SCHEME_CONFLICT")
        current = next(
            row for row in await self.db.list_plugin_scheme_ownership()
            if row["scheme"] == scheme
        )
        self.assertEqual((current["mode"], current["plugin_identity"]), ("plugin", identity))
        await self._resolve_plugin(subsystem, scheme, identity, reference)

        # Settings must explicitly roll back before revoking.  After revoke,
        # neither activation nor Plugin ownership can be restored implicitly.
        await subsystem.set_ownership(scheme, "legacy")
        await subsystem.provider_resolver.resolve(reference, self.client)
        self.assertEqual(self.legacy.await_count, 1 if scheme == "hnntv" else 2)
        await subsystem.revoke_permission(identity, "network.direct", "rollout-3-test")
        projection = await subsystem.service.permission_projection(identity)
        self.assertEqual([item["name"] for item in projection["pending"]], ["network.direct"])
        with self.assertRaises(PluginError) as disabled:
            await subsystem.service.enable(identity)
        self.assertEqual(disabled.exception.code, "PERMISSION_APPROVAL_REQUIRED")
        with self.assertRaises(PluginError) as no_owner:
            await subsystem.set_ownership(scheme, "plugin", identity)
        # Revocation also stops the runtime, so ownership cannot be restored
        # until the approved lifecycle brings its route back.  The generic
        # registry therefore rejects this attempt as SCHEME_UNOWNED before
        # the ownership row can be written; enable() above is the explicit
        # permission-gate assertion.
        self.assertEqual(no_owner.exception.code, "SCHEME_UNOWNED")
        self.assertEqual(subsystem.provider_resolver.mode(scheme), "legacy")

        await subsystem.approve_permission(identity, await self._official_packages(), "network.direct", "rollout-3-test")
        await subsystem.service.enable(identity)
        await subsystem.set_ownership(scheme, "plugin", identity)
        await self._resolve_plugin(subsystem, scheme, identity, reference)

    async def test_hnntv_then_ptbtv_mac_staged_rollout_and_permission_recovery(self):
        self._require_mac_runtime()
        packages = await self._official_packages()
        subsystem = await self._subsystem()
        startup = await subsystem.startup()
        self.assertEqual(
            {item["plugin"] for item in startup if item.get("status") == "unavailable"},
            {identity for _scheme, identity, _reference in TARGETS}
            | {"org.waveflow/streamget-providers"},
        )
        self.assertEqual(await self.db.list_plugin_scheme_ownership(), [])

        # HNNTV is intentionally the first staged takeover.
        hnntv = "org.waveflow/hnntv"
        await self._install_approved(subsystem, hnntv, packages)
        live = await subsystem.service.runtime.request(
            subsystem.service.runtime.registry.route("hnntv"), "tv.resolve_stream", {"resource_id": "hnws"},
        )
        replay = await subsystem.service.runtime.request(
            subsystem.service.runtime.registry.route("hnntv"), "tv.resolve_stream",
            {"resource_id": "hnws", "query": {"playseek": [PLAYSEEK]}},
        )
        self.assertEqual(live["url"], STREAMS["hnntv-live"])
        self.assertEqual(replay["url"], STREAMS["hnntv-replay"])
        self.assertEqual(replay["provider_diagnostics"], {
            "schedule_id": "hnntv-rollout-replay", "program_name": "HNNTV rollout fixture",
        })
        self.assertEqual({request.url.host for request in self.requests[-2:]}, {"www.hnntv.cn", "ps.hnntv.cn"})

        before = self.legacy.await_count
        await subsystem.set_ownership("hnntv", "plugin", hnntv)
        resolved = await self._resolve_plugin(subsystem, "hnntv", hnntv, "hnntv://hnws")
        self.assertEqual(resolved["url"], STREAMS["hnntv-live"])
        self.assertEqual(self.legacy.await_count, before)
        subsystem = await self._restart(subsystem)
        self.assertEqual(subsystem.provider_resolver.mode("hnntv"), "plugin")
        self.assertEqual(
            (await self._resolve_plugin(subsystem, "hnntv", hnntv, "hnntv://hnws"))["url"],
            STREAMS["hnntv-live"],
        )
        await self._assert_permission_revoke_requires_explicit_rollback(
            subsystem, "hnntv", hnntv, "hnntv://hnws",
        )

        # PTBTV is installed and cut over only after HNNTV has completed its
        # staged, restart, rollback, and permission acceptance.
        ptbtv = "org.waveflow/ptbtv"
        await self._install_approved(subsystem, ptbtv, packages)
        instance = subsystem.service.runtime.registry.route("ptbtv")
        diagnostics = await subsystem.service.runtime.request(
            instance, "tv.resolve_stream", {"scheme": "ptbtv", "resource_id": "pt1"},
        )
        self.assertEqual(diagnostics["url"], STREAMS["ptbtv"])
        self.assertIn(
            str(subsystem.service.python_environments.environments_root),
            diagnostics["provider_diagnostics"]["dependency_origin"],
        )
        self.assertEqual(self.requests[-1].url.host, "www.ptbtv.com")
        env_rows = await self.db.list_plugin_python_environments("org.waveflow", "ptbtv")
        self.assertEqual([row["state"] for row in env_rows], ["active"])

        before = self.legacy.await_count
        await subsystem.set_ownership("ptbtv", "plugin", ptbtv)
        resolved = await self._resolve_plugin(subsystem, "ptbtv", ptbtv, "ptbtv://pt1")
        self.assertEqual(resolved["url"], STREAMS["ptbtv"])
        self.assertEqual(self.legacy.await_count, before)
        subsystem = await self._restart(subsystem)
        self.assertEqual(subsystem.provider_resolver.mode("ptbtv"), "plugin")
        self.assertEqual(
            (await self._resolve_plugin(subsystem, "ptbtv", ptbtv, "ptbtv://pt1"))["url"],
            STREAMS["ptbtv"],
        )
        env_rows = await self.db.list_plugin_python_environments("org.waveflow", "ptbtv")
        self.assertEqual([row["state"] for row in env_rows], ["active"])
        await self._assert_permission_revoke_requires_explicit_rollback(
            subsystem, "ptbtv", ptbtv, "ptbtv://pt1",
        )

        # The remote official source is unavailable, but the installed signed
        # baseline and durable ownership still recover through the subsystem.
        market = importlib.import_module("market")
        with mock.patch.object(
            market, "safe_http_fetch", new=mock.AsyncMock(side_effect=market.MarketError("offline", 502)),
        ):
            refreshed = await market.refresh_market()
        source = next(item for item in refreshed["source_results"] if item["source_key"] == "official")
        self.assertEqual(source["status"], "bundled")

        subsystem = await self._restart(subsystem)
        owners = {row["scheme"]: row for row in await self.db.list_plugin_scheme_ownership()}
        self.assertEqual(
            {(owners[scheme]["mode"], owners[scheme]["plugin_identity"]) for scheme, _identity, _reference in TARGETS},
            {("plugin", identity) for _scheme, identity, _reference in TARGETS},
        )
        for scheme, identity, reference in TARGETS:
            self.assertEqual(subsystem.provider_resolver.mode(scheme), "plugin")
            result = await self._resolve_plugin(subsystem, scheme, identity, reference)
            self.assertIn("media.example", result["url"])
        self.assertEqual(self.legacy.await_count, 2)


if __name__ == "__main__":
    unittest.main()
