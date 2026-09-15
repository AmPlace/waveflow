from __future__ import annotations

import contextlib
import importlib
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock


def _clear_modules():
    for name in list(sys.modules):
        if name in {"database", "main", "plugin_production", "routers.plugins"}:
            sys.modules.pop(name, None)


class PluginLifespanTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = os.environ.get("WAVEFLOW_DB_PATH")
        os.environ["WAVEFLOW_DB_PATH"] = os.path.join(self.tmp.name, "waveflow.db")
        _clear_modules()
        self.main = importlib.import_module("main")

    async def asyncTearDown(self):
        if self.old_db is None:
            os.environ.pop("WAVEFLOW_DB_PATH", None)
        else:
            os.environ["WAVEFLOW_DB_PATH"] = self.old_db
        _clear_modules()
        self.tmp.cleanup()

    def patches(self, automation):
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(self.main, "create_production_automation_service", new=mock.AsyncMock(return_value=automation)))
        for name in ("_rtsp_hls_cleanup_task", "refresh_logo_template_from_remote"):
            stack.enter_context(mock.patch.object(self.main, name, new=mock.AsyncMock()))
        stack.enter_context(mock.patch.object(self.main, "_clear_stale_rtsp_hls_dirs"))
        stack.enter_context(mock.patch.object(self.main, "_stop_all_rtsp_sessions", new=mock.AsyncMock()))
        stack.enter_context(mock.patch.object(self.main.http_client, "aclose", new=mock.AsyncMock()))
        return stack

    async def test_recovery_and_shutdown_are_lifespan_owned(self):
        events = []
        subsystem = SimpleNamespace(
            provider_resolver=object(),
            startup=mock.AsyncMock(side_effect=lambda: events.append("plugin_start") or []),
            shutdown=mock.AsyncMock(side_effect=lambda: events.append("plugin_stop")),
        )
        automation = SimpleNamespace(
            registry=SimpleNamespace(),
            start=mock.AsyncMock(side_effect=lambda: events.append("automation_start")),
            stop=mock.AsyncMock(side_effect=lambda: events.append("automation_stop")),
        )
        with self.patches(automation), mock.patch.object(
            self.main.ProductionPluginSubsystem, "create", new=mock.AsyncMock(return_value=subsystem),
        ), mock.patch.object(self.main.database, "list_plugin_installations", new=mock.AsyncMock(return_value=[])):
            async with self.main.lifespan(self.main.app):
                self.assertIs(self.main.app.state.plugin_subsystem, subsystem)
                self.assertIs(self.main.app.state.provider_resolver, subsystem.provider_resolver)
                self.assertLess(events.index("plugin_start"), events.index("automation_start"))
        self.assertLess(events.index("automation_stop"), events.index("plugin_stop"))
        subsystem.shutdown.assert_awaited_once()
        self.assertIsNone(self.main.app.state.plugin_subsystem)
        self.assertIsNone(self.main.app.state.provider_resolver)

    async def test_lifespan_wires_and_clears_the_market_dependency_validator(self):
        """Three Market paths have no request context and rely on this wiring.

        Subscription refresh, the auto-update loop and the Market update task
        reach Content dependency validation only through the validator the
        lifespan installs, so both the wiring and the teardown are load-bearing.
        """
        service = SimpleNamespace(dependency_projection=mock.AsyncMock())
        subsystem = SimpleNamespace(
            service=service,
            provider_resolver=object(),
            startup=mock.AsyncMock(return_value=[]),
            shutdown=mock.AsyncMock(),
        )
        automation = SimpleNamespace(
            registry=SimpleNamespace(), start=mock.AsyncMock(), stop=mock.AsyncMock(),
        )
        with self.patches(automation), mock.patch.object(
            self.main.ProductionPluginSubsystem, "create", new=mock.AsyncMock(return_value=subsystem),
        ), mock.patch.object(self.main.database, "list_plugin_installations", new=mock.AsyncMock(return_value=[])):
            async with self.main.lifespan(self.main.app):
                self.assertIs(
                    self.main._market._CONTENT_DEPENDENCY_VALIDATOR, service.dependency_projection,
                )
        self.assertIsNone(self.main._market._CONTENT_DEPENDENCY_VALIDATOR)

    async def test_startup_failure_clears_a_stale_dependency_validator(self):
        """A subsystem that failed to start must not stay reachable through Market."""
        stale = mock.AsyncMock()
        self.main._market.set_content_dependency_validator(stale)
        automation = SimpleNamespace(
            registry=SimpleNamespace(), start=mock.AsyncMock(), stop=mock.AsyncMock(),
        )
        with self.patches(automation), mock.patch.object(
            self.main.ProductionPluginSubsystem, "create", new=mock.AsyncMock(side_effect=RuntimeError("bad plugin")),
        ), mock.patch.object(
            self.main.database, "list_plugin_scheme_ownership", new=mock.AsyncMock(return_value=[]),
        ):
            async with self.main.lifespan(self.main.app):
                self.assertIsNone(self.main._market._CONTENT_DEPENDENCY_VALIDATOR)
        self.assertIsNone(self.main._market._CONTENT_DEPENDENCY_VALIDATOR)

    async def test_bad_plugin_subsystem_does_not_block_core_startup(self):
        automation = SimpleNamespace(registry=SimpleNamespace(), start=mock.AsyncMock(), stop=mock.AsyncMock())
        with self.patches(automation), mock.patch.object(
            self.main.ProductionPluginSubsystem, "create", new=mock.AsyncMock(side_effect=RuntimeError("bad plugin")),
        ), mock.patch.object(
            self.main.database, "list_plugin_scheme_ownership", new=mock.AsyncMock(return_value=[
                {"scheme": "jstv", "mode": "plugin", "plugin_identity": "org.waveflow/jstv"},
            ]),
        ):
            async with self.main.lifespan(self.main.app):
                self.assertIsNone(self.main.app.state.plugin_subsystem)
                resolver = self.main.app.state.provider_resolver
                self.assertIsNotNone(resolver)
                self.assertEqual(resolver.mode("jstv"), "plugin")
                self.assertIsNone(resolver.runtime)
                with self.assertRaises(Exception) as unavailable:
                    await resolver.resolve("jstv://jsws", self.main.http_client)
                self.assertEqual(unavailable.exception.code, "PLUGIN_UNAVAILABLE")
                automation.start.assert_awaited_once()

    async def test_startup_failure_rebuilds_fail_closed_resolver(self):
        automation = SimpleNamespace(registry=SimpleNamespace(), start=mock.AsyncMock(), stop=mock.AsyncMock())
        subsystem = SimpleNamespace(
            provider_resolver=object(),
            startup=mock.AsyncMock(side_effect=RuntimeError("recovery failed")),
            shutdown=mock.AsyncMock(),
        )
        with self.patches(automation), mock.patch.object(
            self.main.ProductionPluginSubsystem, "create", new=mock.AsyncMock(return_value=subsystem),
        ), mock.patch.object(
            self.main.database, "list_plugin_scheme_ownership", new=mock.AsyncMock(return_value=[
                {"scheme": "jstv", "mode": "plugin", "plugin_identity": "org.waveflow/jstv"},
            ]),
        ):
            async with self.main.lifespan(self.main.app):
                self.assertIsNone(self.main.app.state.plugin_subsystem)
                self.assertEqual(self.main.app.state.provider_resolver.mode("jstv"), "plugin")
                self.assertIsNone(self.main.app.state.provider_resolver.runtime)
                with self.assertRaises(Exception) as unavailable:
                    await self.main.app.state.provider_resolver.resolve("jstv://jsws", self.main.http_client)
                self.assertEqual(unavailable.exception.code, "PLUGIN_UNAVAILABLE")
            subsystem.shutdown.assert_awaited_once()

    async def test_startup_failure_preserves_legacy_routing(self):
        automation = SimpleNamespace(registry=SimpleNamespace(), start=mock.AsyncMock(), stop=mock.AsyncMock())
        legacy = mock.AsyncMock(return_value={"url": "https://legacy.example/live.m3u8"})
        with self.patches(automation), mock.patch.object(
            self.main.ProductionPluginSubsystem, "create", new=mock.AsyncMock(side_effect=RuntimeError("bad plugin")),
        ), mock.patch.object(
            self.main.database, "list_plugin_scheme_ownership", new=mock.AsyncMock(return_value=[
                {"scheme": "jstv", "mode": "legacy", "plugin_identity": ""},
            ]),
        ), mock.patch.object(self.main, "resolve_adapter_source", new=legacy):
            async with self.main.lifespan(self.main.app):
                result = await self.main.app.state.provider_resolver.resolve("jstv://jsws", self.main.http_client)
                self.assertEqual(result["url"], "https://legacy.example/live.m3u8")
                legacy.assert_awaited_once()

    async def test_ownership_read_failure_does_not_assume_legacy(self):
        automation = SimpleNamespace(registry=SimpleNamespace(), start=mock.AsyncMock(), stop=mock.AsyncMock())
        with self.patches(automation), mock.patch.object(
            self.main.ProductionPluginSubsystem, "create", new=mock.AsyncMock(side_effect=RuntimeError("bad plugin")),
        ), mock.patch.object(
            self.main.database, "list_plugin_scheme_ownership", new=mock.AsyncMock(side_effect=RuntimeError("db unavailable")),
        ):
            async with self.main.lifespan(self.main.app):
                self.assertIsNone(self.main.app.state.plugin_subsystem)
                self.assertIsNone(self.main.app.state.provider_resolver)


if __name__ == "__main__":
    unittest.main()
