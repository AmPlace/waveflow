from __future__ import annotations

import base64
import asyncio
import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import httpx
import xxtea
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from provider_resolver import UNOWNED_MODE


TARGETS = (
    ("nowtv", "org.waveflow/nowtv", "nowtv://NEWS"),
    ("nmtv", "org.waveflow/nmtv", "nmtv://nmws"),
    ("sdtv", "org.waveflow/sdtv", "sdtv://sdws"),
)
STREAMS = {
    "nowtv": "https://media.example/nowtv-rollout/live.m3u8",
    "nmtv": "https://media.example/nmtv-rollout/live.m3u8",
    "sdtv": "https://media.example/sdtv-rollout/live.m3u8",
}
NMTV_KEY = b"5b28bae827e651b3"
SDTV_SALT = "rollout-salt"
SDTV_AES_KEY = "rollout-aes-key!"


def _clear_modules() -> None:
    for name in (
        "database", "market", "plugin_market", "plugin_permissions", "plugin_production",
        "official_plugin_distribution", "routers.plugins",
    ):
        sys.modules.pop(name, None)


def _aes_encrypt(value: str, key: str) -> bytes:
    key_bytes = key.encode()[:16].ljust(16, b"0")
    cipher = Cipher(algorithms.AES(key_bytes), modes.CBC(b"0" * 16))
    padder = padding.PKCS7(128).padder()
    padded = padder.update(value.encode()) + padder.finalize()
    encryptor = cipher.encryptor()
    return encryptor.update(padded) + encryptor.finalize()


class ProductionRollout2Test(unittest.IsolatedAsyncioTestCase):
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
        # The acceptance is staged through the generic ownership lifecycle,
        # rather than allowing startup to switch all three schemes at once.
        os.environ["WAVEFLOW_OFFICIAL_PLUGIN_ROLLOUT"] = "0"
        _clear_modules()
        self.db = importlib.import_module("database")
        await self.db.initialize()
        self.requests: list[httpx.Request] = []

        def upstream(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if request.url.host == "webtvapi.now.com":
                return httpx.Response(200, json={
                    "responseCode": "SUCCESS", "asset": [STREAMS["nowtv"]],
                })
            if request.url.host == "api-bt.nmtv.cn":
                raw = json.dumps({
                    "data": [{"data": {"id": 262, "streamUrls": [STREAMS["nmtv"]]}}],
                }, separators=(",", ":")).encode()
                raw += b" " * (-len(raw) % 4)
                encrypted = xxtea.encrypt(raw, NMTV_KEY, padding=False)
                return httpx.Response(200, text=json.dumps(base64.b64encode(encrypted).decode()))
            if request.url.host == "v.iqilu.com":
                return httpx.Response(
                    200, text=f"mxpx = '{SDTV_SALT}'; aly = '{SDTV_AES_KEY}';",
                )
            if request.url.host == "feiying.litenews.cn":
                encrypted = _aes_encrypt(json.dumps({"data": STREAMS["sdtv"]}), SDTV_AES_KEY)
                return httpx.Response(200, text=base64.b64encode(encrypted).decode())
            return httpx.Response(503, text="offline")

        self.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
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

    async def _subsystem(self):
        from plugin_production import ProductionPluginSubsystem

        subsystem = await ProductionPluginSubsystem.create(
            root=Path(self.tmp.name) / "plugin-store", http_client=self.client,
        )
        self.subsystems.append(subsystem)
        return subsystem

    async def _restart(self, subsystem):
        await subsystem.shutdown()
        self.subsystems.remove(subsystem)
        restarted = await self._subsystem()
        await restarted.startup()
        return restarted

    def _require_mac_runtime(self) -> None:
        from plugin_market import current_platform

        if current_platform() != ("macos", "arm64") or (sys.version_info.major, sys.version_info.minor) != (3, 14):
            self.skipTest("PLUGIN-PRODUCTION-ROLLOUT-2 acceptance requires macOS arm64 / CPython 3.14")

    async def test_mac_cp314_staged_takeover_restart_and_rollback_uses_official_plugins(self):
        self._require_mac_runtime()

        subsystem = await self._subsystem()
        startup = await subsystem.startup()
        installed = {
            item["plugin"] for item in startup if item.get("bootstrap") == "installed"
        }
        self.assertTrue({identity for _scheme, identity, _reference in TARGETS}.issubset(installed))

        for scheme, identity, reference in TARGETS:
            row = await self.db.get_plugin_installation("org.waveflow", scheme)
            self.assertEqual(
                (row["enabled"], row["lifecycle_state"], row["trust_state"], row["source_key"]),
                (1, "active", "official", "official"),
            )
            instance = subsystem.service.runtime.registry.route(scheme)
            self.assertEqual(instance.health, "healthy")
            await subsystem._ownership_preflight(scheme, identity)

            # This is the only ownership mutation in the staged step.  It is
            # the same generic production API used by Settings, not a provider
            # branch or a test-only resolver mode switch.
            result = await subsystem.set_ownership(scheme, "plugin", identity)
            self.assertEqual((result["mode"], result["plugin"]), ("plugin", identity))
            resolved = await subsystem.provider_resolver.resolve(reference, self.client)
            self.assertEqual(resolved["stream_descriptor_version"], "1.0")
            self.assertEqual(resolved["url"], STREAMS[scheme])
            self.assertEqual(subsystem.provider_resolver.mode(scheme), "plugin")
            self.assertEqual(subsystem.service.runtime.registry.route(scheme).manifest.identity, identity)

            if scheme in {"nmtv", "sdtv"}:
                dependency_result = await subsystem.service.runtime.request(
                    instance, "tv.resolve_stream", {"scheme": scheme, "resource_id": reference.rsplit("/", 1)[-1]},
                )
                origin = dependency_result.get("provider_diagnostics", {}).get("dependency_origin", "")
                self.assertIn(str(subsystem.service.python_environments.environments_root), origin)

            # Recovery is performed before the next scheme is cut over.  The
            # ownership row is durable and the installed official runtime is
            # what the restarted resolver actually uses.
            subsystem = await self._restart(subsystem)
            recovered = await subsystem.provider_resolver.resolve(reference, self.client)
            self.assertEqual((recovered["stream_descriptor_version"], recovered["url"]),
                             ("1.0", STREAMS[scheme]))

            # Rolling ownership back to "legacy" reaches no Core adapter: the
            # mode stays storable for data compatibility but is no longer
            # routable, so the scheme is explicitly unavailable instead of
            # silently reverting to a provider adapter Core no longer ships.
            from plugin_runtime import PluginError

            await subsystem.set_ownership(scheme, "legacy")
            with self.assertRaises(PluginError) as rolled_back:
                await subsystem.provider_resolver.resolve(reference, self.client)
            self.assertEqual(rolled_back.exception.code, "PLUGIN_UNAVAILABLE")
            await subsystem.set_ownership(scheme, "plugin", identity)
            restored = await subsystem.provider_resolver.resolve(reference, self.client)
            self.assertEqual((restored["stream_descriptor_version"], restored["url"]),
                             ("1.0", STREAMS[scheme]))

        owners = {row["scheme"]: row for row in await self.db.list_plugin_scheme_ownership()}
        self.assertEqual(
            {(owners[scheme]["mode"], owners[scheme]["plugin_identity"]) for scheme, _identity, _ref in TARGETS},
            {("plugin", identity) for _scheme, identity, _ref in TARGETS},
        )

        market = importlib.import_module("market")
        with mock.patch.object(
            market, "safe_http_fetch", new=mock.AsyncMock(side_effect=market.MarketError("offline", 502)),
        ):
            refreshed = await market.refresh_market()
        source = next(item for item in refreshed["source_results"] if item["source_key"] == "official")
        self.assertEqual(source["status"], "bundled")

        subsystem = await self._restart(subsystem)
        for scheme, identity, reference in TARGETS:
            recovered = await subsystem.provider_resolver.resolve(reference, self.client)
            self.assertEqual((recovered["stream_descriptor_version"], recovered["url"]),
                             ("1.0", STREAMS[scheme]))
            self.assertEqual(subsystem.provider_resolver.mode(scheme), "plugin")
            self.assertEqual(subsystem.service.runtime.registry.route(scheme).manifest.identity, identity)

    async def test_linux_cp311_rollout_policy_blocks_new_takeover_without_touching_ownership(self):
        self._require_mac_runtime()
        subsystem = await self._subsystem()
        await subsystem.startup()
        import plugin_production

        os.environ["WAVEFLOW_OFFICIAL_PLUGIN_ROLLOUT"] = "1"
        with mock.patch.object(plugin_production, "current_platform", return_value=("linux", "x86_64")), \
                mock.patch.object(
                    plugin_production.sys, "version_info",
                    SimpleNamespace(major=3, minor=11, micro=9),
                ):
            results = await subsystem.rollout_official_plugins()

        blocked = {
            item["plugin"] for item in results
            if item.get("plugin") in {identity for _scheme, identity, _reference in TARGETS}
        }
        self.assertEqual(blocked, {identity for _scheme, identity, _reference in TARGETS})
        self.assertTrue(all(
            item["rollout"] == "blocked" and item["error"].startswith("PLATFORM_UNSUPPORTED:")
            for item in results if item.get("plugin") in blocked
        ))
        owners = {row["scheme"]: row for row in await self.db.list_plugin_scheme_ownership()}
        self.assertTrue(all(scheme not in owners for scheme, _identity, _reference in TARGETS))
        # Blocked takeovers leave no ownership row at all, and an unowned
        # scheme is not routable: it resolves to "unowned" rather than falling
        # back to a Core provider adapter.
        self.assertTrue(all(
            subsystem.provider_resolver.mode(scheme) == UNOWNED_MODE
            for scheme, _identity, _reference in TARGETS
        ))


if __name__ == "__main__":
    unittest.main()
