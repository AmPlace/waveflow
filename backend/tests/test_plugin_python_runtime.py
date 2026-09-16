from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from packaging import tags

from plugin_python_runtime import PythonEnvironmentManager, dependency_lock_digest
from plugin_runtime import PluginError, validate_manifest


def _wheel(path: Path, name: str, version: str, value: str) -> dict:
    dist = name.replace("-", "_")
    filename = f"{dist}-{version}-py3-none-any.whl"
    target = path / filename
    dist_info = f"{dist}-{version}.dist-info"
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"{dist}/__init__.py", f"VALUE = {value!r}\n")
        archive.writestr(f"{dist_info}/METADATA", f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n")
        archive.writestr(f"{dist_info}/WHEEL", "Wheel-Version: 1.0\nGenerator: waveflow-test\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
        archive.writestr(f"{dist_info}/RECORD", "")
    payload = target.read_bytes()
    return {"name": name, "version": version, "filename": filename, "url": f"https://deps.example/{filename}",
            "sha256": hashlib.sha256(payload).hexdigest(), "size_bytes": len(payload),
            "python_tag": "py3", "abi_tag": "none", "platform_tag": "any", "path": target}


def _manifest(plugin_id: str, version: str, artifacts: list[dict]):
    lock_items = [{key: value for key, value in item.items() if key != "path"} for item in artifacts]
    data = {
        "manifest_version": 1, "publisher_id": "org.waveflow", "plugin_id": plugin_id,
        "display_name": plugin_id, "version": version, "plugin_api_version": "1.0",
        "core_version_range": ">=0.1.0 <1.0.0",
        "provider_contracts": [{"contract": "tv_provider", "contract_version": "1.0", "features": ["resolve_stream"]}],
        "owned_schemes": [{"scheme": plugin_id, "contract": "tv_provider"}],
        "capabilities": ["tv.resolve_stream"], "permissions": {},
        "runtime": {"type": "python", "ipc": "stdio_framed_json_v1",
                    "python_version_range": f">={sys.version_info.major}.{sys.version_info.minor}.0 <{sys.version_info.major}.{sys.version_info.minor + 1}.0",
                    "entrypoint": "plugin.py", "dependency_lock": {"lock_version": 1, "artifacts": lock_items}},
        "artifacts": [{"os": "macos", "arch": "arm64", "runtime": "python", "entrypoint": "plugin.py",
                       "sha256": "0" * 64, "size_bytes": 1,
                       "signature": {"algorithm": "ed25519", "key_id": "fixture", "value": "fixture"}}],
        "dependencies": [], "state_schema_version": 1,
    }
    return validate_manifest(data)


class PythonRuntimeContractTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.manager = PythonEnvironmentManager(self.root / "store")

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_shared_content_addressed_cache_and_isolated_environments(self):
        dependency = _wheel(self.root, "fixture-dependency", "1.0.0", "shared")
        first = _manifest("python-one", "1.0.0", [dependency])
        second = _manifest("python-two", "1.0.0", [dependency])
        refs = {dependency["sha256"]: dependency["path"]}
        env1, env2 = await asyncio.gather(self.manager.prepare(first, refs), self.manager.prepare(second, refs))
        self.assertNotEqual(env1.path, env2.path)
        self.assertEqual(len(self.manager.cache_objects()), 1)
        for env in (env1, env2):
            self.assertIn("include-system-site-packages = false",
                          (env.path / "pyvenv.cfg").read_text(encoding="utf-8").lower())
            proc = await asyncio.create_subprocess_exec(
                str(env.python), "-I", "-c", "import fixture_dependency; print(fixture_dependency.VALUE)",
                stdout=asyncio.subprocess.PIPE)
            stdout, _ = await proc.communicate()
            self.assertEqual(stdout.strip(), b"shared")

    async def test_concurrent_missing_artifact_fetches_only_once_inside_digest_lock(self):
        dependency = _wheel(self.root, "fixture-fetched", "1.0.0", "fetched")
        first = _manifest("python-fetch-one", "1.0.0", [dependency])
        second = _manifest("python-fetch-two", "1.0.0", [dependency])
        calls = 0

        async def fetch(item, directory):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.02)
            target = directory / f"download-{calls}.whl"
            target.write_bytes(dependency["path"].read_bytes())
            return target

        await asyncio.gather(
            self.manager.prepare(first, {}, fetch=fetch),
            self.manager.prepare(second, {}, fetch=fetch),
        )
        self.assertEqual(calls, 1)
        self.assertEqual(len(self.manager.cache_objects()), 1)

    async def test_lock_versions_do_not_share_mutable_environment(self):
        one = _wheel(self.root, "fixture-versioned", "1.0.0", "one")
        two = _wheel(self.root, "fixture-versioned", "2.0.0", "two")
        first = _manifest("python-versioned", "1.0.0", [one])
        second = _manifest("python-versioned", "2.0.0", [two])
        env1 = await self.manager.prepare(first, {one["sha256"]: one["path"]})
        env2 = await self.manager.prepare(second, {two["sha256"]: two["path"]})
        self.assertNotEqual(env1.path, env2.path)
        self.assertEqual(len(self.manager.cache_objects()), 2)
        for env, expected in ((env1, b"one"), (env2, b"two")):
            proc = await asyncio.create_subprocess_exec(str(env.python), "-I", "-c",
                "import fixture_versioned; print(fixture_versioned.VALUE)", stdout=asyncio.subprocess.PIPE)
            stdout, _ = await proc.communicate()
            self.assertEqual(stdout.strip(), expected)

    async def test_invalid_lock_platform_integrity_and_environment_rebuild(self):
        dependency = _wheel(self.root, "fixture-rebuild", "1.0.0", "ok")
        manifest = _manifest("python-rebuild", "1.0.0", [dependency])
        refs = {dependency["sha256"]: dependency["path"]}
        env = await self.manager.prepare(manifest, refs)
        (env.path / "waveflow-environment.json").unlink()
        rebuilt = await self.manager.rebuild(manifest, {})
        self.assertTrue(rebuilt.python.is_file())
        self.assertEqual(rebuilt.lock_digest, dependency_lock_digest(manifest.runtime["dependency_lock"]))

        bad = dict(dependency); bad["sha256"] = "0" * 64
        with self.assertRaises(PluginError) as integrity:
            await self.manager.prepare(_manifest("python-bad", "1.0.0", [bad]), {bad["sha256"]: bad["path"]})
        self.assertEqual(integrity.exception.code, "DEPENDENCY_ARTIFACT_INTEGRITY_FAILED")
        incompatible = dict(dependency); incompatible["python_tag"] = "cp999"
        with self.assertRaises(PluginError) as platform:
            await self.manager.prepare(_manifest("python-platform", "1.0.0", [incompatible]), refs)
        self.assertEqual(platform.exception.code, "DEPENDENCY_PLATFORM_UNSUPPORTED")

        conflict = dict(dependency); conflict["name"] = "fixture-conflict"
        conflict_manifest = _manifest("python-conflict", "1.0.0", [conflict])
        with self.assertRaises(PluginError) as metadata:
            await self.manager.prepare(conflict_manifest, {conflict["sha256"]: conflict["path"]})
        self.assertEqual(metadata.exception.code, "DEPENDENCY_ARTIFACT_INTEGRITY_FAILED")

    def test_manifest_rejects_ranges_unhashed_and_non_wheel_locks(self):
        dependency = _wheel(self.root, "fixture-lock", "1.0.0", "ok")
        manifest = _manifest("python-lock", "1.0.0", [dependency])
        self.assertEqual(manifest.runtime["type"], "python")
        for mutate in (
            lambda item: item.update(version="latest"),
            lambda item: item.update(filename="setup.py"),
            lambda item: item.update(url="http://deps.example/a.whl"),
            lambda item: item.update(sha256=""),
        ):
            data = json.loads(json.dumps(manifest.raw))
            mutate(data["runtime"]["dependency_lock"]["artifacts"][0])
            with self.assertRaises(PluginError):
                validate_manifest(data)

    async def test_multi_platform_candidates_select_exactly_one_current_wheel(self):
        current = _wheel(self.root, "fixture-platform", "1.0.0", "current")
        foreign = dict(current)
        foreign.update(filename="fixture_platform-1.0.0-cp999-cp999-linux_x86_64.whl",
                       url="https://deps.example/fixture_platform-1.0.0-cp999-cp999-linux_x86_64.whl",
                       sha256="f" * 64, size_bytes=99, python_tag="cp999", abi_tag="cp999",
                       platform_tag="linux_x86_64")
        manifest = _manifest("python-platform-candidates", "1.0.0", [current, foreign])
        env = await self.manager.prepare(manifest, {current["sha256"]: current["path"]})
        self.assertEqual([item["sha256"] for item in env.dependencies], [current["sha256"]])

    async def test_frozen_sidecar_subprocesses_inherit_no_bytecode_policy(self):
        sidecar = self.root / "app" / "python-runtime" / "bin" / "python3.14"
        manager = PythonEnvironmentManager(self.root / "sidecar-store", python_executable=sidecar)

        class Process:
            returncode = 0

            async def communicate(self):
                return b"", b""

        with mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(sys, "executable", str(self.root / "app" / "waveflow-backend")), \
                mock.patch("plugin_python_runtime.asyncio.create_subprocess_exec", new=mock.AsyncMock(
                    return_value=Process(),
                )) as create_process:
            await manager._run((str(sidecar), "-B", "-m", "venv", str(self.root / "staging")))

        environment = create_process.call_args.kwargs["env"]
        self.assertEqual(environment["PYTHONDONTWRITEBYTECODE"], "1")


if __name__ == "__main__":
    unittest.main()
