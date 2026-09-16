from __future__ import annotations

import os
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from desktop_entry import configure_desktop_environment
from desktop_runtime_manifest import (
    desktop_runtime_target,
    runtime_metadata_from_lock,
    runtime_tree_digest,
    validate_runtime_metadata,
)
from plugin_desktop_runtime import resolve_plugin_python_executable
from plugin_market import FixtureTrustPolicy, PluginArtifactStore, PluginMarketService
from plugin_production import _python_backed_rollout_enabled
from plugin_runtime import PluginError, PluginRuntime
from plugin_runtime.process import PluginProcess


ROOT = Path(__file__).parents[1]


def _write_runtime_metadata(
    runtime_root: Path,
    *,
    abi: str = "cp314",
    os_name: str = "macos",
    arch: str = "arm64",
    executable: str = "bin/python3.14",
) -> None:
    tree_sha256, tree_file_count = runtime_tree_digest(runtime_root)
    (runtime_root / "runtime.json").write_text(
        json.dumps({
            "schema_version": 1,
            "runtime_type": "python",
            "python_version": "3.14.7",
            "python_abi": abi,
            "os": os_name,
            "arch": arch,
            "executable": executable,
            "tree_sha256": tree_sha256,
            "tree_file_count": tree_file_count,
        }),
        encoding="utf-8",
    )


class DesktopPluginRuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def test_frozen_backend_uses_sibling_controlled_runtime(self):
        with tempfile.TemporaryDirectory(prefix="waveflow runtime ") as temp:
            root = Path(temp)
            backend = root / "backend"
            python = backend / "python-runtime" / "bin" / "python3.14"
            python.parent.mkdir(parents=True)
            python.write_text("#!/bin/sh\n", encoding="utf-8")
            python.chmod(0o755)
            _write_runtime_metadata(python.parent.parent)
            executable = backend / "waveflow-backend"
            executable.write_text("", encoding="utf-8")

            with mock.patch.object(sys, "frozen", True, create=True), \
                    mock.patch.object(sys, "executable", str(executable)):
                self.assertEqual(resolve_plugin_python_executable(), python.resolve())

    async def test_frozen_backend_fails_closed_without_sidecar(self):
        with tempfile.TemporaryDirectory() as temp:
            executable = Path(temp) / "waveflow-backend"
            executable.write_text("", encoding="utf-8")
            with mock.patch.object(sys, "frozen", True, create=True), \
                    mock.patch.object(sys, "executable", str(executable)):
                with self.assertRaises(PluginError) as raised:
                    resolve_plugin_python_executable()
                self.assertEqual(raised.exception.code, "PYTHON_RUNTIME_UNSUPPORTED")

    async def test_frozen_windows_backend_uses_python_exe_without_posix_mode_bits(self):
        with tempfile.TemporaryDirectory(prefix="waveflow windows runtime ") as temp:
            root = Path(temp)
            executable = root / "waveflow-backend.exe"
            python = root / "python-runtime" / "python.exe"
            python.parent.mkdir(parents=True)
            python.write_text("windows runtime", encoding="utf-8")
            _write_runtime_metadata(
                python.parent,
                os_name="windows",
                arch="x86_64",
                executable="python.exe",
            )
            executable.write_text("backend", encoding="utf-8")
            with mock.patch.object(sys, "frozen", True, create=True), \
                    mock.patch.object(sys, "executable", str(executable)), \
                    mock.patch.object(sys, "platform", "win32"), \
                    mock.patch("desktop_runtime_manifest.host_platform.machine", return_value="AMD64"):
                self.assertEqual(resolve_plugin_python_executable(), python.resolve())

    async def test_runtime_target_normalizes_windows_architecture_aliases(self):
        target = desktop_runtime_target("Windows", "AMD64")
        self.assertIsNotNone(target)
        self.assertEqual(
            (target.platform_os, target.arch, target.executable),
            ("windows", "x86_64", "python.exe"),
        )

    async def test_windows_runtime_metadata_round_trip_uses_locked_contract(self):
        with tempfile.TemporaryDirectory(prefix="waveflow windows metadata ") as temp:
            root = Path(temp)
            executable = root / "python.exe"
            executable.write_text("windows runtime", encoding="utf-8")
            lock = json.loads(
                (ROOT.parent / "desktop_runtime" / "cpython-3.14.7-windows-x64.json").read_text(
                    encoding="utf-8",
                )
            )
            target = desktop_runtime_target("windows", "x86_64")
            self.assertIsNotNone(target)
            metadata = runtime_metadata_from_lock(
                root,
                lock,
                target,
                executable="python.exe",
                ca_runtime_path="certifi/cacert.pem",
            )
            (root / "runtime.json").write_text(json.dumps(metadata), encoding="utf-8")
            self.assertEqual(
                validate_runtime_metadata(root, metadata, target, expected_python_version="3.14"),
                executable.resolve(),
            )

    async def test_frozen_backend_rejects_runtime_integrity_mismatch(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            executable = root / "waveflow-backend"
            python = root / "python-runtime" / "bin" / "python3.14"
            python.parent.mkdir(parents=True)
            python.write_text("#!/bin/sh\n", encoding="utf-8")
            python.chmod(0o755)
            _write_runtime_metadata(python.parent.parent)
            python.write_text("#!/bin/sh\ncorrupt\n", encoding="utf-8")
            with mock.patch.object(sys, "frozen", True, create=True), \
                    mock.patch.object(sys, "executable", str(executable)):
                with self.assertRaises(PluginError) as raised:
                    resolve_plugin_python_executable()
                self.assertEqual(raised.exception.code, "PYTHON_RUNTIME_UNSUPPORTED")

    async def test_frozen_backend_ignores_runtime_bytecode_caches(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            executable = root / "waveflow-backend"
            python = root / "python-runtime" / "bin" / "python3.14"
            python.parent.mkdir(parents=True)
            python.write_text("#!/bin/sh\n", encoding="utf-8")
            python.chmod(0o755)
            runtime_root = python.parent.parent
            _write_runtime_metadata(runtime_root)
            cache = runtime_root / "framework" / "Python.framework" / "lib" / "python3.14" / "__pycache__"
            cache.mkdir(parents=True)
            (cache / "site.cpython-314.pyc").write_bytes(b"generated bytecode")
            (runtime_root / "framework" / "Python.framework" / "lib" / "python3.14").mkdir(
                parents=True, exist_ok=True,
            )
            (runtime_root / "framework" / "Python.framework" / "lib" / "module.pyc").write_bytes(
                b"generated bytecode"
            )
            with mock.patch.object(sys, "frozen", True, create=True), \
                    mock.patch.object(sys, "executable", str(executable)):
                self.assertEqual(resolve_plugin_python_executable(), python.resolve())

    async def test_frozen_backend_rejects_wrong_python_abi(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            executable = root / "waveflow-backend"
            python = root / "python-runtime" / "bin" / "python3.14"
            python.parent.mkdir(parents=True)
            python.write_text("#!/bin/sh\n", encoding="utf-8")
            python.chmod(0o755)
            _write_runtime_metadata(python.parent.parent, abi="cp313")
            with mock.patch.object(sys, "frozen", True, create=True), \
                    mock.patch.object(sys, "executable", str(executable)):
                with self.assertRaises(PluginError) as raised:
                    resolve_plugin_python_executable()
                self.assertEqual(raised.exception.code, "PYTHON_RUNTIME_UNSUPPORTED")

    async def test_default_python_plugin_command_uses_injected_runtime(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            sidecar = root / "python-runtime" / "bin" / "python3.14"
            sidecar.parent.mkdir(parents=True)
            sidecar.write_text("", encoding="utf-8")
            sidecar.chmod(0o755)
            _write_runtime_metadata(sidecar.parent.parent)
            service = PluginMarketService(
                runtime=PluginRuntime(),
                store=PluginArtifactStore(root / "store", allowed_local_roots=[root]),
                trust_policy=FixtureTrustPolicy({}),
                python_executable=sidecar,
            )
            try:
                digest = "a" * 64
                manifest = SimpleNamespace(
                    identity="org.waveflow/fixture",
                    version="1.0.0",
                    artifacts=[{"sha256": digest, "runtime": "python"}],
                )
                command = service._default_command(manifest, root / digest / "plugin.pyz")
                self.assertEqual(command[0], str(sidecar.resolve()))
            finally:
                await service.runtime.shutdown()

    async def test_subprocess_pyz_command_uses_controlled_runtime(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            sidecar = root / "python-runtime" / "bin" / "python3.14"
            sidecar.parent.mkdir(parents=True)
            sidecar.write_text("", encoding="utf-8")
            sidecar.chmod(0o755)
            _write_runtime_metadata(sidecar.parent.parent)
            service = PluginMarketService(
                runtime=PluginRuntime(),
                store=PluginArtifactStore(root / "store", allowed_local_roots=[root]),
                trust_policy=FixtureTrustPolicy({}),
                python_executable=sidecar,
            )
            try:
                digest = "b" * 64
                manifest = SimpleNamespace(
                    identity="org.waveflow/fixture-subprocess",
                    version="1.0.0",
                    artifacts=[{"sha256": digest, "runtime": "subprocess"}],
                )
                command = service._default_command(manifest, root / digest / "fixture.pyz")
                self.assertEqual(command, (
                    str(sidecar.resolve()), "-I", "-B", str(root / digest / "fixture.pyz"),
                    "--identity", manifest.identity, "--version", manifest.version,
                ))
            finally:
                await service.runtime.shutdown()

    async def test_real_dependency_free_fjtv_pyz_hello_with_python_process(self):
        artifact = ROOT / "official_plugins" / "distribution" / "payloads" / "fjtv-1.0.0.pyz"
        process = PluginProcess(
            (sys.executable, "-I", str(artifact), "--identity", "org.waveflow/fjtv", "--version", "1.0.0"),
            "desktop-fjtv",
        )
        await process.start()
        try:
            hello = await process.call(
                "runtime.hello", {"protocol_versions": ["1.0", "1.1"], "plugin_instance": "desktop-fjtv"},
            )
            health = await process.call("runtime.health", {})
            self.assertEqual(hello["plugin"], "org.waveflow/fjtv")
            self.assertTrue(health["healthy"])
        finally:
            await process.stop()

    async def test_desktop_data_dir_owns_plugin_store(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(os.environ, {}, clear=True):
            configure_desktop_environment(temp)
            self.assertEqual(os.environ["WAVEFLOW_DB_PATH"], str(Path(temp) / "waveflow.db"))
            self.assertEqual(os.environ["WAVEFLOW_PLUGIN_ROOT"], str(Path(temp) / "plugins"))

    async def test_frozen_desktop_with_controlled_runtime_is_rollout_eligible(self):
        with mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.dict(
                    _python_backed_rollout_enabled.__globals__,
                    {"resolve_plugin_python_executable": mock.Mock(
                        return_value=Path("/app/python-runtime/bin/python3.14"),
                    )},
                ):
            self.assertTrue(_python_backed_rollout_enabled())

    async def test_frozen_desktop_without_controlled_runtime_stays_ineligible(self):
        with mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.dict(
                    _python_backed_rollout_enabled.__globals__,
                    {"resolve_plugin_python_executable": mock.Mock(
                        side_effect=PluginError("PYTHON_RUNTIME_UNSUPPORTED", "missing", category="runtime"),
                    )},
                ):
            self.assertFalse(_python_backed_rollout_enabled())


class DesktopPluginStoreBindingTest(unittest.IsolatedAsyncioTestCase):
    async def test_same_database_binding_survives_restart(self):
        from plugin_production import _bind_plugin_store

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "plugins"
            root.mkdir()
            (root / ".waveflow-plugin-store.json").write_text(
                json.dumps({"binding_id": "database-a", "schema_version": 1}),
                encoding="utf-8",
            )
            with mock.patch(
                "plugin_production.db.get_setting",
                new=mock.AsyncMock(return_value="database-a"),
            ), mock.patch(
                "plugin_production.db.get_or_create_setting",
                new=mock.AsyncMock(return_value="database-a"),
            ):
                await _bind_plugin_store(root)
                await _bind_plugin_store(root)

    async def test_new_database_cannot_claim_existing_store(self):
        from plugin_production import _bind_plugin_store

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "plugins"
            root.mkdir()
            (root / ".waveflow-plugin-store.json").write_text(
                json.dumps({"binding_id": "database-a", "schema_version": 1}),
                encoding="utf-8",
            )
            with mock.patch(
                "plugin_production.db.get_setting",
                new=mock.AsyncMock(return_value=""),
            ), mock.patch(
                "plugin_production.db.get_or_create_setting",
                new=mock.AsyncMock(),
            ) as get_or_create:
                with self.assertRaises(PluginError) as raised:
                    await _bind_plugin_store(root)
                self.assertEqual(raised.exception.code, "ARTIFACT_INVALID")
                get_or_create.assert_not_awaited()

    async def test_different_database_cannot_reuse_existing_store(self):
        from plugin_production import _bind_plugin_store

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "plugins"
            root.mkdir()
            (root / ".waveflow-plugin-store.json").write_text(
                json.dumps({"binding_id": "database-a", "schema_version": 1}),
                encoding="utf-8",
            )
            with mock.patch(
                "plugin_production.db.get_setting",
                new=mock.AsyncMock(return_value="database-b"),
            ), mock.patch(
                "plugin_production.db.get_or_create_setting",
                new=mock.AsyncMock(return_value="database-b"),
            ):
                with self.assertRaises(PluginError) as raised:
                    await _bind_plugin_store(root)
                self.assertEqual(raised.exception.code, "ARTIFACT_INVALID")

    async def test_empty_store_gets_one_durable_binding(self):
        from plugin_production import _bind_plugin_store

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "plugins"
            root.mkdir()
            with mock.patch(
                "plugin_production.db.get_setting",
                new=mock.AsyncMock(return_value=""),
            ), mock.patch(
                "plugin_production.db.get_or_create_setting",
                new=mock.AsyncMock(return_value="database-new"),
            ), mock.patch(
                "plugin_production.db.list_plugin_artifact_references",
                new=mock.AsyncMock(return_value=[]),
            ):
                await _bind_plugin_store(root)
            self.assertEqual(
                json.loads((root / ".waveflow-plugin-store.json").read_text(encoding="utf-8")),
                {"binding_id": "database-new", "schema_version": 1},
            )
