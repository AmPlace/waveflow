"""Provider resolution is Plugin-only: no Core adapter fallback may exist.

These tests pin the product decision rather than incidental wiring, so they
assert behaviour (fail closed) and the absence of the removed machinery.
"""

from __future__ import annotations

import importlib.util
import inspect
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from plugin_runtime import PluginError
from provider_resolver import ProviderResolver, UNOWNED_MODE


BACKEND_ROOT = Path(__file__).resolve().parents[1]

PROVIDER_SCHEMES = ("jstv", "fjtv", "hbtv", "huya", "youtube", "woniu", "xjtv")


class _FakeManifest:
    def __init__(self, identity: str):
        self.identity = identity


class _FakeInstance:
    def __init__(self, identity: str):
        self.manifest = _FakeManifest(identity)


class _FakeRegistry:
    def __init__(self, identity: str):
        self._identity = identity

    def route(self, scheme: str) -> _FakeInstance:
        return _FakeInstance(self._identity)


class _FakeRuntime:
    """Stands in for PluginRuntime without spawning a real Plugin process."""

    def __init__(self, identity: str = "org.waveflow/jstv", *, fail: bool = False):
        self.registry = _FakeRegistry(identity)
        self._fail = fail
        self.calls: list[tuple[str, dict]] = []

    async def request(self, instance, method: str, payload: dict) -> dict:
        self.calls.append((method, payload))
        if self._fail:
            raise PluginError("PLUGIN_UNAVAILABLE", "Plugin is unhealthy", category="lifecycle")
        return {
            "transport": "hls",
            "url": "https://plugin.example/live.m3u8",
            "descriptor_version": "1.0",
            "requires_proxy": False,
        }


class EmptyOwnershipFailsClosedTest(unittest.IsolatedAsyncioTestCase):
    """Criterion A: a fresh install with no ownership rows must fail closed."""

    async def test_every_scheme_fails_closed_without_ownership(self):
        resolver = ProviderResolver(runtime=None)
        for scheme in PROVIDER_SCHEMES:
            with self.subTest(scheme=scheme):
                self.assertEqual(resolver.mode(scheme), UNOWNED_MODE)
                with self.assertRaises(PluginError) as ctx:
                    await resolver.resolve(f"{scheme}://room-1", mock.Mock())
                self.assertEqual(ctx.exception.code, "PLUGIN_UNAVAILABLE")

    async def test_no_ownership_row_reaches_a_core_adapter(self):
        # The strongest available evidence that no fallback exists: the module
        # that used to hold the resolvers is gone entirely.
        self.assertIsNone(importlib.util.find_spec("adapters"))

    async def test_legacy_mode_is_storable_but_not_routable(self):
        resolver = ProviderResolver(runtime=None, ownership={"jstv": "legacy"})
        self.assertEqual(resolver.mode("jstv"), "legacy")
        with self.assertRaises(PluginError) as ctx:
            await resolver.resolve("jstv://room-1", mock.Mock())
        self.assertEqual(ctx.exception.code, "PLUGIN_UNAVAILABLE")


class HealthyPluginResolvesTest(unittest.IsolatedAsyncioTestCase):
    """Criterion B: plugin ownership with a healthy Plugin resolves normally."""

    async def test_plugin_owner_resolves_through_the_plugin(self):
        runtime = _FakeRuntime()
        resolver = ProviderResolver(runtime=runtime)
        resolver.set_mode("jstv", "plugin", "org.waveflow/jstv")

        result = await resolver.resolve("jstv://room-1", mock.Mock())

        self.assertEqual(result["url"], "https://plugin.example/live.m3u8")
        self.assertEqual(result["stream_descriptor_version"], "1.0")
        self.assertEqual(runtime.calls[0][0], "tv.resolve_stream")


class UnhealthyPluginFailsClosedTest(unittest.IsolatedAsyncioTestCase):
    """Criterion C: an unavailable Plugin fails closed, never falls back."""

    async def test_runtime_unavailable_fails_closed(self):
        resolver = ProviderResolver(runtime=None)
        resolver.set_mode("jstv", "plugin", "org.waveflow/jstv")
        with self.assertRaises(PluginError) as ctx:
            await resolver.resolve("jstv://room-1", mock.Mock())
        self.assertEqual(ctx.exception.code, "PLUGIN_UNAVAILABLE")

    async def test_unhealthy_plugin_fails_closed(self):
        resolver = ProviderResolver(runtime=_FakeRuntime(fail=True))
        resolver.set_mode("jstv", "plugin", "org.waveflow/jstv")
        with self.assertRaises(PluginError) as ctx:
            await resolver.resolve("jstv://room-1", mock.Mock())
        self.assertEqual(ctx.exception.code, "PLUGIN_UNAVAILABLE")

    async def test_reconciling_scheme_fails_closed(self):
        runtime = _FakeRuntime()
        resolver = ProviderResolver(runtime=runtime)
        resolver.set_mode("jstv", "plugin", "org.waveflow/jstv")
        resolver.fail_closed("jstv")
        with self.assertRaises(PluginError) as ctx:
            await resolver.resolve("jstv://room-1", mock.Mock())
        self.assertEqual(ctx.exception.code, "PLUGIN_UNAVAILABLE")
        self.assertEqual(runtime.calls, [])

    async def test_disabled_plugin_ownership_still_fails_closed(self):
        # Ownership points at a Plugin, but the installation was disabled: the
        # resolver has no runtime to reach, so it must not guess.
        resolver = ProviderResolver(runtime=None)
        resolver.set_mode("jstv", "plugin", "org.waveflow/jstv", available=False)
        with self.assertRaises(PluginError) as ctx:
            await resolver.resolve("jstv://room-1", mock.Mock())
        self.assertEqual(ctx.exception.code, "PLUGIN_UNAVAILABLE")


class UnownedProjectionTest(unittest.TestCase):
    """Forgetting a projection must read back as unowned, not as legacy."""

    def test_forget_makes_an_owned_scheme_unowned_again(self):
        resolver = ProviderResolver(runtime=None)
        resolver.set_mode("jstv", "plugin", "org.waveflow/jstv")
        self.assertEqual(resolver.mode("jstv"), "plugin")

        resolver.forget("jstv")

        self.assertEqual(resolver.mode("jstv"), UNOWNED_MODE)
        self.assertTrue(resolver.is_available("jstv"))

    def test_forget_drops_a_legacy_projection(self):
        resolver = ProviderResolver(runtime=None, ownership={"jstv": "legacy"})
        self.assertEqual(resolver.mode("jstv"), "legacy")

        resolver.forget("jstv")

        self.assertEqual(resolver.mode("jstv"), UNOWNED_MODE)


class UnsupportedProviderSchemeTest(unittest.IsolatedAsyncioTestCase):
    """Criterion D: redbook and tiktok are explicitly unsupported, not legacy."""

    async def test_redbook_and_tiktok_are_unsupported(self):
        resolver = ProviderResolver(runtime=_FakeRuntime())
        for scheme in ("redbook", "tiktok"):
            with self.subTest(scheme=scheme):
                self.assertEqual(resolver.mode(scheme), UNOWNED_MODE)
                with self.assertRaises(PluginError) as ctx:
                    await resolver.resolve(f"{scheme}://room-1", mock.Mock())
                self.assertEqual(ctx.exception.code, "PLUGIN_UNAVAILABLE")


class NoLegacyMachineryRemainsTest(unittest.TestCase):
    """Criterion E: no Core provider adapter path survives anywhere."""

    def _production_sources(self) -> list[Path]:
        # Only first-party Core sources.  Vendored/virtualenv trees ship their
        # own unrelated modules and must not be scanned.
        skip = {"tests", "data", "__pycache__", ".venv", "venv", "site-packages", "node_modules"}
        return [
            path for path in BACKEND_ROOT.rglob("*.py")
            if not any(part in skip for part in path.relative_to(BACKEND_ROOT).parts)
        ]

    def test_no_legacy_symbol_in_production_sources(self):
        banned = ("resolve_adapter_source", "_ADAPTER_REGISTRY", "from adapters", "import adapters")
        sources = self._production_sources()
        self.assertTrue(sources, "no production sources found to scan")
        offenders = []
        for path in sources:
            text = path.read_text(encoding="utf-8", errors="replace")
            for symbol in banned:
                if symbol in text:
                    offenders.append(f"{path.relative_to(BACKEND_ROOT)}: {symbol}")
        self.assertEqual(offenders, [])

    def test_adapters_package_is_gone(self):
        self.assertIsNone(importlib.util.find_spec("adapters"))
        self.assertFalse((BACKEND_ROOT / "adapters").exists())

    def test_resolver_has_no_legacy_resolver_dependency(self):
        for signature in (
            inspect.signature(ProviderResolver.__init__),
            inspect.signature(ProviderResolver.from_ownership_rows),
        ):
            self.assertNotIn("legacy_resolver", signature.parameters)

    def test_legacy_is_not_routable(self):
        from provider_resolver import ROUTABLE_MODES

        self.assertNotIn("legacy", ROUTABLE_MODES)
        self.assertIn("plugin", ROUTABLE_MODES)

    def test_build_scripts_do_not_import_the_deleted_adapter_package(self):
        # Packaging lives outside backend/, so the production source scan above
        # cannot see it.  A --hidden-import for a deleted module is dead load
        # that would silently rot again.
        repo_root = BACKEND_ROOT.parent
        scripts = sorted((repo_root / "scripts").glob("build-*"))
        self.assertTrue(scripts, "no build scripts found to scan")
        offenders = [
            f"{path.relative_to(repo_root)}"
            for path in scripts
            if "adapters" in path.read_text(encoding="utf-8", errors="replace")
        ]
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
