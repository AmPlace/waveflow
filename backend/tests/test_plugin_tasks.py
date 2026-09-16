from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock


class PluginTaskTest(unittest.IsolatedAsyncioTestCase):
    async def test_stale_source_is_excluded(self):
        import plugin_tasks
        context = SimpleNamespace(
            stop_requested=lambda: False,
            task_type="auto_update",
            report_progress=mock.AsyncMock(),
        )
        subsystem = SimpleNamespace(install=mock.AsyncMock())
        installation = {
            "publisher_id": "org.example", "plugin_id": "one",
            "source_key": "third", "source_package_id": "third::one", "active_version": "1.0.0",
        }
        package = {"id": "third::one", "version": "2.0.0"}
        refresh = {"source_results": [{
            "source_key": "third", "status": "stale", "usable_for_update": False,
            "package_ids": ["third::one"],
        }]}
        with mock.patch.object(plugin_tasks.market, "refresh_market", new=mock.AsyncMock(return_value=refresh)), \
                mock.patch.object(plugin_tasks.market, "market_packages_snapshot", return_value=[package]), \
                mock.patch.object(plugin_tasks.db, "list_plugin_installations", new=mock.AsyncMock(return_value=[installation])):
            result = await plugin_tasks.run_plugin_update_task(context, subsystem)
        self.assertEqual((result.checked_count, result.updated_count, result.skipped_count), (0, 0, 1))
        subsystem.install.assert_not_awaited()

    async def test_one_plugin_failure_does_not_block_other_update(self):
        import plugin_tasks
        context = SimpleNamespace(
            stop_requested=lambda: False,
            task_type="auto_update",
            report_progress=mock.AsyncMock(),
        )
        installations = [
            {"publisher_id": "org.example", "plugin_id": value, "source_key": "third",
             "source_package_id": f"third::{value}", "active_version": "1.0.0"}
            for value in ("bad", "good")
        ]
        packages = [{"id": f"third::{value}", "version": "2.0.0"} for value in ("bad", "good")]
        refresh = {"source_results": [{
            "source_key": "third", "status": "success", "usable_for_update": True,
            "package_ids": [item["id"] for item in packages],
        }]}
        async def install(identity, _packages):
            if identity.endswith("/bad"):
                raise RuntimeError("fixture failure")
        subsystem = SimpleNamespace(install=mock.AsyncMock(side_effect=install))
        with mock.patch.object(plugin_tasks.market, "refresh_market", new=mock.AsyncMock(return_value=refresh)), \
                mock.patch.object(plugin_tasks.market, "market_packages_snapshot", return_value=packages), \
                mock.patch.object(plugin_tasks.db, "list_plugin_installations", new=mock.AsyncMock(return_value=installations)):
            result = await plugin_tasks.run_plugin_update_task(context, subsystem)
        self.assertEqual(result.status, "partial")
        self.assertEqual((result.checked_count, result.updated_count, result.failed_count), (2, 1, 1))
        self.assertEqual(subsystem.install.await_count, 2)

    async def test_permission_approval_required_is_skipped_and_other_update_continues(self):
        import plugin_tasks
        from plugin_runtime import PluginError
        context = SimpleNamespace(stop_requested=lambda: False, task_type="auto_update", report_progress=mock.AsyncMock())
        installations = [{"publisher_id": "org.example", "plugin_id": value, "source_key": "third",
                          "source_package_id": f"third::{value}", "active_version": "1.0.0"}
                         for value in ("pending", "ready")]
        packages = [{"id": f"third::{value}", "version": "2.0.0"} for value in ("pending", "ready")]
        refresh = {"source_results": [{"source_key": "third", "status": "success", "usable_for_update": True,
                                        "package_ids": [item["id"] for item in packages]}]}
        async def install(identity, _packages):
            if identity.endswith("/pending"):
                raise PluginError("PERMISSION_APPROVAL_REQUIRED", "approval required", category="permission")
        subsystem = SimpleNamespace(install=mock.AsyncMock(side_effect=install))
        with mock.patch.object(plugin_tasks.market, "refresh_market", new=mock.AsyncMock(return_value=refresh)), \
                mock.patch.object(plugin_tasks.market, "market_packages_snapshot", return_value=packages), \
                mock.patch.object(plugin_tasks.db, "list_plugin_installations", new=mock.AsyncMock(return_value=installations)):
            result = await plugin_tasks.run_plugin_update_task(context, subsystem)
        self.assertEqual((result.status, result.checked_count, result.updated_count, result.skipped_count, result.failed_count),
                         ("success", 2, 1, 1, 0))
        self.assertEqual(subsystem.install.await_count, 2)

    async def test_real_provider_plugins_use_the_generic_market_update_task(self):
        import plugin_tasks
        for plugin_id in ("fjtv", "nowtv", "nmtv"):
            with self.subTest(plugin_id=plugin_id):
                context = SimpleNamespace(
                    stop_requested=lambda: False,
                    task_type="auto_update",
                    report_progress=mock.AsyncMock(),
                )
                package_id = f"official::{plugin_id}-plugin"
                installation = {
                    "publisher_id": "org.waveflow", "plugin_id": plugin_id,
                    "source_key": "official", "source_package_id": package_id,
                    "active_version": "1.0.0",
                }
                package = {"id": package_id, "version": "1.1.0"}
                refresh = {"source_results": [{
                    "source_key": "official", "status": "success", "usable_for_update": True,
                    "package_ids": [package_id],
                }]}
                subsystem = SimpleNamespace(install=mock.AsyncMock())
                with mock.patch.object(plugin_tasks.market, "refresh_market", new=mock.AsyncMock(return_value=refresh)), \
                        mock.patch.object(plugin_tasks.market, "market_packages_snapshot", return_value=[package]), \
                        mock.patch.object(plugin_tasks.db, "list_plugin_installations", new=mock.AsyncMock(return_value=[installation])):
                    result = await plugin_tasks.run_plugin_update_task(context, subsystem)
                self.assertEqual((result.status, result.checked_count, result.updated_count), ("success", 1, 1))
                subsystem.install.assert_awaited_once_with(f"org.waveflow/{plugin_id}", [package])


if __name__ == "__main__":
    unittest.main()
