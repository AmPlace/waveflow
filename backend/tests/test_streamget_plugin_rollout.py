from __future__ import annotations

import asyncio
import copy
import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

from plugin_runtime import LifecycleState, PluginError, validate_manifest


IDENTITY = "org.waveflow/streamget-providers"
SCHEMES = (
    "yy", "bigo", "blued", "soop", "netease", "pandatv", "maoer", "look",
    "flextv", "popkontv", "twitcasting", "baidu", "weibo", "kugou", "twitch", "huajiao",
    "showroom", "inke", "acfun", "zhihu", "chzzk", "live17", "langlive", "changliao",
    "jd", "faceit", "lianjie", "sixroom", "huamao", "shopee", "laixiu", "picarto",
)
SCHEME_SET = set(SCHEMES)
BILIBILI_SCHEME = "bilibili"
DOUYU_SCHEME = "douyu"
DOUYIN_SCHEME = "douyin"
BUNDLE_SCHEME_SET = SCHEME_SET | {BILIBILI_SCHEME, DOUYU_SCHEME, DOUYIN_SCHEME}
BATCHES = (
    ("yy", "bigo", "blued", "soop", "netease", "pandatv", "maoer", "look"),
    ("flextv", "popkontv", "twitcasting", "baidu", "weibo", "kugou", "twitch", "huajiao"),
    ("showroom", "inke", "acfun", "zhihu", "chzzk", "live17", "langlive", "changliao"),
    ("jd", "faceit", "lianjie", "sixroom", "huamao", "shopee", "laixiu", "picarto"),
)
assert len(SCHEMES) == 32 and len({scheme for batch in BATCHES for scheme in batch}) == 32
STREAM_URL_PREFIX = "https://fixture.example/streamget/"


def _clear_modules() -> None:
    for name in (
        "database", "market", "plugin_market", "plugin_permissions", "plugin_production",
        "official_plugin_distribution", "routers.plugins",
    ):
        sys.modules.pop(name, None)


class StreamGetPluginRolloutTest(unittest.IsolatedAsyncioTestCase):
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
        # The bundle is installed by this acceptance and switched in four
        # explicit batches; startup must not perform a hidden bulk takeover.
        os.environ["WAVEFLOW_OFFICIAL_PLUGIN_BOOTSTRAP"] = "0"
        os.environ["WAVEFLOW_OFFICIAL_PLUGIN_ROLLOUT"] = "0"
        _clear_modules()
        self.db = importlib.import_module("database")
        await self.db.initialize()
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(503, text="deterministic rollout has no upstream fetch")
        ))
        self.legacy = mock.AsyncMock(
            side_effect=lambda target, _client: {
                "ok": True,
                "url": f"https://legacy.example/{target.split('://', 1)[0]}.m3u8",
            }
        )
        self.subsystems = []
        self.plugin_requests: list[tuple[str, str]] = []
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
            self.skipTest("STREAMGET-PLUGIN-ROLLOUT-1 requires macOS arm64 / CPython 3.14")

    async def _subsystem(self):
        from plugin_production import ProductionPluginSubsystem

        subsystem = await ProductionPluginSubsystem.create(
            root=Path(self.tmp.name) / "plugin-store", http_client=self.client,
        )
        subsystem.provider_resolver.legacy_resolver = self.legacy
        self.subsystems.append(subsystem)
        return subsystem

    async def _restart(self, subsystem):
        await subsystem.shutdown()
        self.subsystems.remove(subsystem)
        restarted = await self._subsystem()
        startup = await restarted.startup()
        self.assertFalse(any(item.get("rollout") == "plugin" for item in startup))
        return restarted

    async def _wait_reconciliation(self, subsystem):
        for _ in range(40):
            pending = [
                task for task in [
                    *subsystem._lifecycle_reconcile_tasks.values(),
                    *subsystem._ownership_reconcile_tasks.values(),
                    *subsystem._critical_tasks,
                ] if not task.done()
            ]
            if not pending:
                await asyncio.sleep(0)
                if not any(
                    not task.done()
                    for task in [
                        *subsystem._lifecycle_reconcile_tasks.values(),
                        *subsystem._ownership_reconcile_tasks.values(),
                        *subsystem._critical_tasks,
                    ]
                ):
                    return
                continue
            await asyncio.gather(*(asyncio.shield(task) for task in pending))
        self.fail("StreamGet lifecycle reconciliation did not become idle")

    async def _official_package(self) -> dict:
        from official_plugin_distribution import bundled_official_packages

        packages = bundled_official_packages()
        package = next(
            item for item in packages
            if (item.get("plugin_manifest") or {}).get("plugin_id") == "streamget-providers"
        )
        manifest = validate_manifest(package["plugin_manifest"])
        self.assertEqual(manifest.identity, IDENTITY)
        self.assertEqual({scheme for scheme, _contract in manifest.owned_schemes}, BUNDLE_SCHEME_SET)
        self.assertEqual(len(manifest.owned_schemes), 35)
        self.assertEqual(package["version"], "1.3.0")
        return package

    async def _install_official(self, subsystem, package: dict) -> None:
        from plugin_runtime import PluginError

        with self.assertRaises(PluginError) as pending:
            await subsystem.install(IDENTITY, [package])
        self.assertEqual(pending.exception.code, "PERMISSION_APPROVAL_REQUIRED")
        self.assertIsNone(await self.db.get_plugin_installation("org.waveflow", "streamget-providers"))

        await subsystem.approve_permission(IDENTITY, [package], "network.direct", "streamget-rollout-1")
        installed = await subsystem.install(IDENTITY, [package])
        self.assertEqual(
            (installed["trust_state"], installed["source_key"], installed["active_version"],
             installed["lifecycle_state"], installed["enabled"]),
            ("official", "official", "1.3.0", "active", 1),
        )
        row = await self.db.get_plugin_installation("org.waveflow", "streamget-providers")
        self.assertEqual(row["source_package_id"], "official::streamget-providers-plugin")
        self.assertEqual(row["runtime_type"], "python")
        self.assertEqual(row["platform_os"], "macos")
        self.assertEqual(row["platform_arch"], "arm64")
        self.assertEqual(
            (await subsystem.service.permission_projection(IDENTITY))["pending"], [],
        )
        environments = await self.db.list_plugin_python_environments("org.waveflow", "streamget-providers")
        self.assertEqual([item["state"] for item in environments], ["active"])

        instance = subsystem.service.runtime.registry.route("yy")
        self.assertEqual(instance.manifest.identity, IDENTITY)
        self.assertEqual(instance.state, LifecycleState.HEALTHY_ACTIVE)
        self.assertEqual(instance.health, "healthy")
        for scheme in SCHEMES:
            self.assertIs(subsystem.service.runtime.registry.route(scheme), instance)
        self.assertEqual(subsystem.provider_resolver.mode(BILIBILI_SCHEME), "legacy")
        self.assertEqual(subsystem.provider_resolver.mode(DOUYU_SCHEME), "legacy")
        self.assertEqual(subsystem.provider_resolver.mode(DOUYIN_SCHEME), "legacy")

    def _install_deterministic_runtime_request(self, subsystem) -> None:
        """Keep ProviderResolver deterministic while the real process is smoke-tested separately.

        StreamGet intentionally uses direct network, so Core's MockTransport cannot
        intercept its upstream calls.  The provider implementation itself has a
        34-scheme fixture contract test; this seam exercises the production
        resolver/ownership path without turning the rollout test into a flaky
        public-site test.
        """
        async def request(instance, method, payload, *args, **kwargs):
            self.assertEqual(instance.manifest.identity, IDENTITY)
            self.assertEqual(method, "tv.resolve_stream")
            scheme = str(payload["scheme"])
            self.assertIn(scheme, BUNDLE_SCHEME_SET)
            self.plugin_requests.append((scheme, instance.manifest.identity))
            transport = "http_flv" if scheme in {"yy", BILIBILI_SCHEME, DOUYU_SCHEME} else "hls"
            return {
                "descriptor_version": "1.0",
                "transport": transport,
                "url": f"{STREAM_URL_PREFIX}{scheme}{'.flv' if scheme in {BILIBILI_SCHEME, DOUYU_SCHEME} else '.m3u8'}",
                "headers": {},
                "credential_refs": [],
                "ttl_seconds": 0 if scheme == DOUYU_SCHEME else 1800,
                "expires_at": None,
                "volatile_url": scheme not in {BILIBILI_SCHEME, DOUYIN_SCHEME},
                "requires_proxy": False,
                "warnings": [],
            }

        subsystem.service.runtime.request = request

    async def _resolve_plugin(self, subsystem, scheme: str) -> dict:
        result = await subsystem.provider_resolver.resolve(f"{scheme}://room-42", self.client)
        self.assertEqual(subsystem.provider_resolver.mode(scheme), "plugin")
        self.assertEqual(result["stream_descriptor_version"], "1.0")
        suffix = ".flv" if scheme in {BILIBILI_SCHEME, DOUYU_SCHEME} else ".m3u8"
        self.assertEqual(result["url"], f"{STREAM_URL_PREFIX}{scheme}{suffix}")
        self.assertEqual(
            subsystem.service.runtime.registry.route(scheme).manifest.identity,
            IDENTITY,
        )
        return result

    async def _resolve_legacy(self, subsystem, scheme: str) -> dict:
        before = self.legacy.await_count
        result = await subsystem.provider_resolver.resolve(f"{scheme}://room-42", self.client)
        self.assertEqual(subsystem.provider_resolver.mode(scheme), "legacy")
        self.assertEqual(self.legacy.await_count, before + 1)
        self.assertTrue(result["url"].startswith("https://legacy.example/"))
        return result

    async def _assert_plugin_modes(self, subsystem, expected: set[str]) -> None:
        rows = {row["scheme"]: row for row in await self.db.list_plugin_scheme_ownership()}
        for scheme in sorted(BUNDLE_SCHEME_SET):
            row = rows.get(scheme)
            mode = "legacy" if row is None else row["mode"]
            identity = "" if row is None else row["plugin_identity"]
            if scheme in expected:
                self.assertEqual((mode, identity), ("plugin", IDENTITY), scheme)
                self.assertEqual(subsystem.provider_resolver.mode(scheme), "plugin")
            else:
                self.assertEqual(mode, "legacy", scheme)
                self.assertEqual(subsystem.provider_resolver.mode(scheme), "legacy")
        if BILIBILI_SCHEME not in expected:
            self.assertEqual(subsystem.provider_resolver.mode(BILIBILI_SCHEME), "legacy")

    async def test_four_batch_rollout_partial_ownership_crash_recovery_and_guards(self):
        self._require_mac_runtime()
        package = await self._official_package()
        subsystem = await self._subsystem()
        self.assertEqual(await subsystem.startup(), [])
        await self._install_official(subsystem, package)
        await self._assert_plugin_modes(subsystem, set())

        rolled_out: set[str] = set()
        for batch_index, batch in enumerate(BATCHES):
            for scheme in batch:
                result = await subsystem.set_ownership(scheme, "plugin", IDENTITY)
                self.assertEqual((result["mode"], result["plugin"]), ("plugin", IDENTITY))
                rolled_out.add(scheme)

            self._install_deterministic_runtime_request(subsystem)
            for scheme in rolled_out:
                await self._resolve_plugin(subsystem, scheme)
            await self._assert_plugin_modes(subsystem, rolled_out)

            if batch_index == 0:
                # Partial ownership is per scheme even though one runtime owns
                # the whole bundle.  The remaining 24 schemes must remain
                # legacy, and one scheme can roll back independently.
                for scheme in sorted(SCHEME_SET - rolled_out):
                    await self._resolve_legacy(subsystem, scheme)
                rollback_scheme = batch[0]
                await subsystem.set_ownership(rollback_scheme, "legacy")
                await self._resolve_legacy(subsystem, rollback_scheme)
                self.assertEqual(
                    {scheme for scheme in rolled_out
                     if subsystem.provider_resolver.mode(scheme) == "plugin"},
                    rolled_out - {rollback_scheme},
                )
                await subsystem.set_ownership(rollback_scheme, "plugin", IDENTITY)
                await self._resolve_plugin(subsystem, rollback_scheme)

                # A runtime crash is bundle-wide, but only Plugin-owned
                # schemes fail closed.  Legacy-owned schemes still use the
                # legacy resolver and never silently switch owner.
                instance = subsystem.service.runtime.registry.route("yy")
                process = instance.process.process
                self.assertIsNotNone(process)
                process.kill()
                await instance.process._wait_task
                await self._wait_reconciliation(subsystem)
                self.assertEqual(instance.state, LifecycleState.UNHEALTHY)
                plugin_calls = len(self.plugin_requests)
                for scheme in batch:
                    with self.assertRaises(PluginError) as unavailable:
                        await subsystem.provider_resolver.resolve(f"{scheme}://room-42", self.client)
                    self.assertIn(unavailable.exception.code, {"PLUGIN_UNAVAILABLE", "PLUGIN_CRASHED"})
                self.assertEqual(len(self.plugin_requests), plugin_calls)
                await self._resolve_legacy(subsystem, "picarto")

                subsystem = await self._restart(subsystem)
                self._install_deterministic_runtime_request(subsystem)
                await self._assert_plugin_modes(subsystem, rolled_out)
                for scheme in rolled_out:
                    await self._resolve_plugin(subsystem, scheme)

            # Every batch is followed by a real subsystem restart and durable
            # resolver projection check before the next batch begins.
            subsystem = await self._restart(subsystem)
            self._install_deterministic_runtime_request(subsystem)
            await self._assert_plugin_modes(subsystem, rolled_out)
            for scheme in rolled_out:
                await self._resolve_plugin(subsystem, scheme)

        self.assertEqual(rolled_out, SCHEME_SET)
        await self._assert_plugin_modes(subsystem, SCHEME_SET)

        # A malformed, newly-versioned candidate is rejected by the existing
        # signed-package lifecycle.  Canonical ownership and active v1 remain
        # untouched; generic candidate-success coverage lives in the Runtime
        # and Market lifecycle suites.
        bad_candidate = copy.deepcopy(package)
        bad_candidate["version"] = "1.3.1"
        bad_candidate["plugin_manifest"]["version"] = "1.3.1"
        with self.assertRaises(PluginError):
            await subsystem.install(IDENTITY, [bad_candidate])
        row = await self.db.get_plugin_installation("org.waveflow", "streamget-providers")
        self.assertEqual(row["active_version"], "1.3.0")
        await self._assert_plugin_modes(subsystem, SCHEME_SET)

        # With 31 schemes rolled back and one still Plugin-owned, all
        # destructive operations remain guarded.  Only after the final
        # explicit rollback may the identity be disabled/revoked.
        anchor = "yy"
        for scheme in sorted(SCHEME_SET - {anchor}):
            await subsystem.set_ownership(scheme, "legacy")
        await self._assert_plugin_modes(subsystem, {anchor})
        for operation in (subsystem.disable, subsystem.uninstall):
            with self.assertRaises(PluginError) as blocked:
                await operation(IDENTITY)
            self.assertEqual(blocked.exception.code, "SCHEME_CONFLICT")
        with self.assertRaises(PluginError) as blocked:
            await subsystem.revoke_permission(IDENTITY, "network.direct", "streamget-rollout-1")
        self.assertEqual(blocked.exception.code, "SCHEME_CONFLICT")

        await subsystem.set_ownership(anchor, "legacy")
        await subsystem.disable(IDENTITY)
        row = await self.db.get_plugin_installation("org.waveflow", "streamget-providers")
        self.assertEqual((row["enabled"], row["lifecycle_state"]), (0, "disabled"))
        await subsystem.service.enable(IDENTITY)
        await subsystem.revoke_permission(IDENTITY, "network.direct", "streamget-rollout-1")
        projection = await subsystem.service.permission_projection(IDENTITY)
        self.assertEqual([item["name"] for item in projection["pending"]], ["network.direct"])
        with self.assertRaises(PluginError) as denied:
            await subsystem.service.enable(IDENTITY)
        self.assertEqual(denied.exception.code, "PERMISSION_APPROVAL_REQUIRED")
        await subsystem.approve_permission(IDENTITY, [package], "network.direct", "streamget-rollout-1")
        await subsystem.service.enable(IDENTITY)

        self._install_deterministic_runtime_request(subsystem)
        for scheme in SCHEME_SET:
            await subsystem.set_ownership(scheme, "plugin", IDENTITY)
        await self._assert_plugin_modes(subsystem, SCHEME_SET)
        for scheme in SCHEME_SET:
            await self._resolve_plugin(subsystem, scheme)
        self.assertEqual({scheme for scheme, identity in self.plugin_requests if identity == IDENTITY}, SCHEME_SET)

    async def test_bilibili_staged_takeover_restart_rollback_and_update_boundary(self):
        """The newly added scheme is updated with the bundle but rolls out alone."""
        self._require_mac_runtime()
        package = await self._official_package()
        subsystem = await self._subsystem()
        await subsystem.startup()
        await self._install_official(subsystem, package)

        self.assertEqual(subsystem.provider_resolver.mode(BILIBILI_SCHEME), "legacy")
        self._install_deterministic_runtime_request(subsystem)
        legacy_calls = self.legacy.await_count
        await subsystem.set_ownership(BILIBILI_SCHEME, "plugin", IDENTITY)
        result = await self._resolve_plugin(subsystem, BILIBILI_SCHEME)
        self.assertEqual(
            (result["source_type"], result["ttl"], result["volatile_url"], result["requires_proxy"]),
            ("http_flv", 1800, False, False),
        )
        self.assertEqual(self.legacy.await_count, legacy_calls)
        await self._assert_plugin_modes(subsystem, {BILIBILI_SCHEME})

        # Reinstalling the same signed bundle version is idempotent and must
        # not rewrite any scheme ownership, including the staged new scheme.
        await subsystem.install(IDENTITY, [package])
        await self._assert_plugin_modes(subsystem, {BILIBILI_SCHEME})

        subsystem = await self._restart(subsystem)
        self._install_deterministic_runtime_request(subsystem)
        await self._assert_plugin_modes(subsystem, {BILIBILI_SCHEME})
        await self._wait_reconciliation(subsystem)
        await self._resolve_plugin(subsystem, BILIBILI_SCHEME)

        await subsystem.set_ownership(BILIBILI_SCHEME, "legacy")
        await self._resolve_legacy(subsystem, BILIBILI_SCHEME)
        await subsystem.set_ownership(BILIBILI_SCHEME, "plugin", IDENTITY)
        await self._resolve_plugin(subsystem, BILIBILI_SCHEME)
        self.assertEqual(self.legacy.await_count, legacy_calls + 1)
        await self._assert_plugin_modes(subsystem, {BILIBILI_SCHEME})

    async def test_douyu_staged_takeover_restart_rollback_and_update_boundary(self):
        """Douyu owns only its scheme while preserving the 33 existing owners."""
        self._require_mac_runtime()
        package = await self._official_package()
        subsystem = await self._subsystem()
        await subsystem.startup()
        await self._install_official(subsystem, package)

        self.assertEqual(subsystem.provider_resolver.mode(DOUYU_SCHEME), "legacy")
        self.assertEqual(subsystem.provider_resolver.mode("bilibili"), "legacy")
        self._install_deterministic_runtime_request(subsystem)
        legacy_calls = self.legacy.await_count
        await subsystem.set_ownership("picarto", "plugin", IDENTITY)
        await self._resolve_plugin(subsystem, "picarto")
        await subsystem.set_ownership(DOUYU_SCHEME, "plugin", IDENTITY)
        result = await self._resolve_plugin(subsystem, DOUYU_SCHEME)
        self.assertEqual(
            (result["source_type"], result["ttl"], result["volatile_url"], result["requires_proxy"]),
            ("http_flv", 0, True, False),
        )
        self.assertEqual(self.legacy.await_count, legacy_calls)
        await self._resolve_legacy(subsystem, "bilibili")
        await self._assert_plugin_modes(subsystem, {DOUYU_SCHEME, "picarto"})

        # Reinstalling the same signed 34-scheme bundle does not rewrite
        # per-scheme ownership, including the newly staged Douyu owner.
        await subsystem.install(IDENTITY, [package])
        await self._assert_plugin_modes(subsystem, {DOUYU_SCHEME, "picarto"})

        subsystem = await self._restart(subsystem)
        self._install_deterministic_runtime_request(subsystem)
        await self._assert_plugin_modes(subsystem, {DOUYU_SCHEME, "picarto"})
        await self._wait_reconciliation(subsystem)
        await self._resolve_plugin(subsystem, DOUYU_SCHEME)
        await self._resolve_plugin(subsystem, "picarto")

        await subsystem.set_ownership(DOUYU_SCHEME, "legacy")
        await self._resolve_legacy(subsystem, DOUYU_SCHEME)
        await self._resolve_plugin(subsystem, "picarto")
        await subsystem.set_ownership(DOUYU_SCHEME, "plugin", IDENTITY)
        await self._resolve_plugin(subsystem, DOUYU_SCHEME)
        self.assertEqual(self.legacy.await_count, legacy_calls + 2)
        await self._assert_plugin_modes(subsystem, {DOUYU_SCHEME, "picarto"})

    async def test_douyin_staged_takeover_restart_rollback_and_update_boundary(self):
        """Douyin rolls out independently while existing Bundle owners persist."""
        self._require_mac_runtime()
        package = await self._official_package()
        subsystem = await self._subsystem()
        await subsystem.startup()
        await self._install_official(subsystem, package)

        self.assertEqual(subsystem.provider_resolver.mode(DOUYIN_SCHEME), "legacy")
        self.assertEqual(subsystem.provider_resolver.mode(BILIBILI_SCHEME), "legacy")
        self.assertEqual(subsystem.provider_resolver.mode(DOUYU_SCHEME), "legacy")
        self._install_deterministic_runtime_request(subsystem)
        legacy_calls = self.legacy.await_count
        await subsystem.set_ownership("picarto", "plugin", IDENTITY)
        await self._resolve_plugin(subsystem, "picarto")
        await subsystem.set_ownership(DOUYIN_SCHEME, "plugin", IDENTITY)
        result = await self._resolve_plugin(subsystem, DOUYIN_SCHEME)
        self.assertEqual(
            (result["source_type"], result["ttl"], result["volatile_url"], result["requires_proxy"]),
            ("hls", 1800, False, False),
        )
        self.assertEqual(self.legacy.await_count, legacy_calls)
        await self._resolve_legacy(subsystem, BILIBILI_SCHEME)
        await self._assert_plugin_modes(subsystem, {DOUYIN_SCHEME, "picarto"})

        # Updating the signed 35-scheme bundle cannot rewrite existing
        # per-scheme ownership.
        await subsystem.install(IDENTITY, [package])
        await self._assert_plugin_modes(subsystem, {DOUYIN_SCHEME, "picarto"})

        subsystem = await self._restart(subsystem)
        self._install_deterministic_runtime_request(subsystem)
        await self._assert_plugin_modes(subsystem, {DOUYIN_SCHEME, "picarto"})
        await self._wait_reconciliation(subsystem)
        await self._resolve_plugin(subsystem, DOUYIN_SCHEME)
        await self._resolve_plugin(subsystem, "picarto")

        await subsystem.set_ownership(DOUYIN_SCHEME, "legacy")
        await self._resolve_legacy(subsystem, DOUYIN_SCHEME)
        await self._resolve_plugin(subsystem, "picarto")
        await subsystem.set_ownership(DOUYIN_SCHEME, "plugin", IDENTITY)
        await self._resolve_plugin(subsystem, DOUYIN_SCHEME)
        self.assertEqual(self.legacy.await_count, legacy_calls + 2)
        await self._assert_plugin_modes(subsystem, {DOUYIN_SCHEME, "picarto"})

    @unittest.skipUnless(
        os.environ.get("WAVEFLOW_STREAMGET_LIVE_SMOKE") == "1",
        "manual public-network StreamGet smoke is opt-in",
    )
    async def test_representative_live_runtime_smoke(self):
        """Run the signed Plugin process against currently live public rooms.

        The IDs are deliberately kept out of the deterministic suite because
        upstream live status changes.  This opt-in acceptance is run separately
        and records the live result without making normal CI depend on it.
        """
        self._require_mac_runtime()
        package = await self._official_package()
        subsystem = await self._subsystem()
        await subsystem.startup()
        await self._install_official(subsystem, package)
        for scheme, resource in (("twitch", "eslcs"), ("showroom", "573483")):
            self.assertEqual(subsystem.provider_resolver.mode(scheme), "legacy")
            await subsystem.set_ownership(scheme, "plugin", IDENTITY)
            result = await subsystem.provider_resolver.resolve(
                f"{scheme}://{resource}", self.client,
            )
            self.assertEqual(result["stream_descriptor_version"], "1.0")
            self.assertTrue(str(result["url"]).startswith(("http://", "https://")))
            self.assertEqual(subsystem.provider_resolver.mode(scheme), "plugin")


if __name__ == "__main__":
    unittest.main()
