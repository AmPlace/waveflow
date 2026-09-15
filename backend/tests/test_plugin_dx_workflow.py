"""Third-party developer workflow regressions for the Plugin SDK CLI.

These tests cover the gaps a Plugin author hits between ``waveflow-plugin``
saying "valid" and the runtime actually being able to load the artifact:
platform coverage, Python range, artifact integrity, dependency wheel layout,
derived ``test`` defaults, and diagnosable crashes.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

import httpx

from plugin_market import current_platform
from plugin_runtime import PluginError, validate_manifest
from waveflow_plugin_cli import (
    CANONICAL_PLATFORMS,
    build_project,
    init_project,
    lock_dependencies,
    test_build as run_sdk_test,
    validate_project,
)


FIXTURES = Path(__file__).parent / "fixtures" / "dependencies"
XXTEA_WHEEL = "xxtea-5.0.0-cp314-cp314-macosx_11_0_arm64.whl"

CRASHING_PROVIDER = '''raise RuntimeError("provider-exploded-at-import")

from waveflow_plugin_sdk import PluginApplication, ResolveContext, StreamDescriptor, TVProvider, TVReference

class Provider(TVProvider):
    def resolve_stream(self, reference: TVReference, context: ResolveContext) -> StreamDescriptor:
        return StreamDescriptor.hls("https://example.invalid/live.m3u8")

identity, version = PluginApplication.identity_args("org.example/demo-tv", "0.1.0")
PluginApplication(identity=identity, version=version).register_tv("demotv", Provider()).run()
'''

HTTP_PROVIDER = '''from waveflow_plugin_sdk import PluginApplication, ResolveContext, StreamDescriptor, TVProvider, TVReference

class Provider(TVProvider):
    def resolve_stream(self, reference: TVReference, context: ResolveContext) -> StreamDescriptor:
        response = context.capabilities.managed_http("https://api.example/resolve", response_mode="json")
        return StreamDescriptor.hls(response.body["url"], ttl_seconds=60)

identity, version = PluginApplication.identity_args("org.example/demo-tv", "0.1.0")
PluginApplication(identity=identity, version=version).register_tv("demotv", Provider()).run()
'''


def _scaffold(root: Path, *, name: str, kind: str, scheme: str) -> Path:
    project = root / name
    init_project(project, kind=kind, publisher="org.example", plugin_id=name, scheme=scheme)
    return project


def _built(root: Path, project: Path) -> dict:
    return build_project(project)


class TemplateContractTest(unittest.TestCase):
    def test_template_covers_the_developer_machine(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = _scaffold(root, name="demo-tv", kind="tv", scheme="demotv")
            report = validate_project(project)
            self.assertTrue(report["valid"])
            # The scaffold used to declare linux/x86_64 only, which made every
            # step after `build` fail on the author's own machine.
            self.assertIn(current_platform(), CANONICAL_PLATFORMS)
            self.assertEqual(report["platform"]["current"], list(current_platform()))
            self.assertIn(list(current_platform()), report["platform"]["declared"])

    def test_template_artifact_is_loadable_by_the_developer_loader(self):
        from plugin_developer import load_developer_package

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = _scaffold(root, name="demo-tv", kind="tv", scheme="demotv")
            built = build_project(project)
            package = load_developer_package(built["manifest"])
            self.assertEqual(package["market_source"]["source_key"], "developer_local")

    def test_scheme_grammar_matches_between_core_and_sdk(self):
        from plugin_runtime.manifest import SCHEME_RE as CORE_SCHEME_RE
        from waveflow_plugin_sdk.application import SCHEME_RE as SDK_SCHEME_RE

        self.assertEqual(CORE_SCHEME_RE.pattern, SDK_SCHEME_RE.pattern)

    def test_manifest_rejects_a_scheme_the_sdk_would_reject(self):
        with tempfile.TemporaryDirectory() as directory:
            project = _scaffold(Path(directory), name="demo-tv", kind="tv", scheme="demotv")
            manifest = json.loads((project / "manifest.json").read_text(encoding="utf-8"))
            manifest["owned_schemes"] = [{"scheme": "x", "contract": "tv_provider"}]
            with self.assertRaises(PluginError) as raised:
                validate_manifest(manifest)
            self.assertEqual(raised.exception.code, "INVALID_PLUGIN_RESPONSE")
            self.assertIn("'x'", raised.exception.message)


class ValidateRuntimeConsistencyTest(unittest.TestCase):
    def _project(self, root: Path, *, scheme: str = "demotv") -> Path:
        project = _scaffold(root, name="demo-tv", kind="tv", scheme=scheme)
        return project

    def test_validate_rejects_an_unreachable_python_range(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = self._project(root)
            manifest = json.loads((project / "manifest.json").read_text(encoding="utf-8"))
            manifest["runtime"]["python_version_range"] = ">=3.99.0 <4.0.0"
            (project / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            with self.assertRaises(PluginError) as raised:
                validate_project(project)
            self.assertEqual(raised.exception.code, "PYTHON_RUNTIME_UNSUPPORTED")
            self.assertIn(">=3.99.0 <4.0.0", raised.exception.message)

    def test_validate_rejects_a_manifest_with_no_artifact_for_this_host(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = self._project(root)
            current = current_platform()
            foreign = next(item for item in CANONICAL_PLATFORMS if item != current)
            manifest = json.loads((project / "manifest.json").read_text(encoding="utf-8"))
            manifest["artifacts"] = [item for item in manifest["artifacts"]
                                     if (item["os"], item["arch"]) == foreign]
            (project / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            with self.assertRaises(PluginError) as raised:
                validate_project(project)
            self.assertEqual(raised.exception.code, "PLATFORM_UNSUPPORTED")
            self.assertIn(f"{current[0]}/{current[1]}", raised.exception.message)

    def test_validate_detects_a_modified_built_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = self._project(root)
            built = build_project(project)
            self.assertTrue(validate_project(built["manifest"])["valid"])
            artifact = Path(built["artifact"])
            artifact.write_bytes(artifact.read_bytes() + b"tamper")
            with self.assertRaises(PluginError) as raised:
                validate_project(built["manifest"])
            self.assertEqual(raised.exception.code, "ARTIFACT_INTEGRITY_FAILED")

    def test_source_project_placeholder_digest_is_not_checked(self):
        with tempfile.TemporaryDirectory() as directory:
            project = self._project(Path(directory))
            # A freshly scaffolded project has no built artifact; the all-zero
            # placeholder must not be treated as a mismatch.
            self.assertTrue(validate_project(project)["valid"])


class DependencyPackagingTest(unittest.TestCase):
    def _dependency_project(self, root: Path) -> Path:
        project = _scaffold(root, name="demo-tv", kind="tv", scheme="demotv")
        (project / "requirements.in").write_text("xxtea==5.0.0\n", encoding="utf-8")
        wheels = project / "dependencies"
        wheels.mkdir()
        shutil.copyfile(FIXTURES / XXTEA_WHEEL, wheels / XXTEA_WHEEL)
        lock_dependencies(
            project / "requirements.in", project / "dependency-lock.json",
            wheel_dir=wheels, manifest_path=project / "manifest.json",
        )
        return project

    def test_build_stages_wheels_beside_the_artifact_and_not_inside_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = self._dependency_project(root)
            built = build_project(project)
            dist = Path(built["artifact"]).parent
            self.assertTrue((dist / "dependencies" / XXTEA_WHEEL).is_file())
            self.assertEqual(built["dependencies"], [XXTEA_WHEEL])
            with zipfile.ZipFile(built["artifact"]) as archive:
                self.assertEqual([name for name in archive.namelist() if name.endswith(".whl")], [])

    def test_build_fails_fast_when_a_locked_wheel_is_missing(self):
        from plugin_python_runtime import select_dependency_artifacts

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = self._dependency_project(root)
            lock = json.loads((project / "dependency-lock.json").read_text(encoding="utf-8"))
            try:
                select_dependency_artifacts(lock)
            except PluginError:
                self.skipTest("fixture wheels do not target this interpreter")
            # The project manages wheels locally, so a wheel the lock resolves to
            # must actually be there.  (An absent dependencies/ directory means an
            # official-style source build whose wheels come from the release
            # pipeline, which must not fail here.)
            (project / "dependencies" / XXTEA_WHEEL).unlink()
            with self.assertRaises(PluginError) as raised:
                build_project(project)
            self.assertEqual(raised.exception.code, "DEPENDENCY_ARTIFACT_NOT_FOUND")
            self.assertEqual(raised.exception.details["expected_path"], f"dependencies/{XXTEA_WHEEL}")


class TestCommandTest(unittest.IsolatedAsyncioTestCase):
    async def test_defaults_are_derived_from_a_radio_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = _scaffold(root, name="demo-radio", kind="radio", scheme="demoradio")
            built = build_project(project)
            result = await run_sdk_test(Path(built["manifest"]))
            self.assertEqual(result["method"], "radio.catalog")
            self.assertEqual(result["payload"], {})
            self.assertEqual(result["result"]["stations"][0]["station_ref"]["provider_key"], "demoradio")

            resolved = await run_sdk_test(
                Path(built["manifest"]), method="radio.resolve_stream",
                payload={"station_ref": {"provider_key": "demoradio", "provider_station_id": "one"},
                         "playback_config": {}},
            )
            self.assertEqual(resolved["result"]["transport"], "hls")

    async def test_unowned_scheme_is_reported_instead_of_falling_back(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = _scaffold(root, name="demo-tv", kind="tv", scheme="demotv")
            built = build_project(project)
            # The SDK used to answer this with the lone registered provider,
            # so a Plugin could pass `test` while owning no such scheme.
            with self.assertRaises(PluginError) as raised:
                await run_sdk_test(Path(built["manifest"]), method="tv.resolve_stream",
                                 payload={"scheme": "synthetic", "resource_id": "one", "query": {}})
            self.assertEqual(raised.exception.code, "RESOURCE_NOT_FOUND")
            self.assertIn("synthetic", raised.exception.message)

    async def test_startup_crash_surfaces_the_plugin_traceback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = _scaffold(root, name="demo-tv", kind="tv", scheme="demotv")
            (project / "provider.py").write_text(CRASHING_PROVIDER, encoding="utf-8")
            built = build_project(project)
            with self.assertRaises(PluginError) as raised:
                await run_sdk_test(Path(built["manifest"]))
            self.assertEqual(raised.exception.code, "PLUGIN_CRASHED")
            tail = raised.exception.details.get("plugin_stderr") or []
            self.assertTrue(any("provider-exploded-at-import" in line for line in tail), tail)

    async def test_missing_capability_fixture_names_the_method(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = _scaffold(root, name="demo-tv", kind="tv", scheme="demotv")
            (project / "provider.py").write_text(HTTP_PROVIDER, encoding="utf-8")
            built = build_project(project)
            with self.assertRaises(PluginError) as raised:
                await run_sdk_test(Path(built["manifest"]))
            self.assertEqual(raised.exception.code, "CAPABILITY_DENIED")
            self.assertIn("core.http.fetch", raised.exception.message)
            self.assertIn("--capabilities", raised.exception.message)


class ThirdPartyLifecycleTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = os.environ.get("WAVEFLOW_DB_PATH")
        self.old_bootstrap = os.environ.get("WAVEFLOW_OFFICIAL_PLUGIN_BOOTSTRAP")
        os.environ["WAVEFLOW_DB_PATH"] = str(Path(self.tmp.name) / "waveflow.db")
        os.environ["WAVEFLOW_OFFICIAL_PLUGIN_BOOTSTRAP"] = "0"
        for name in ("database", "plugin_developer", "plugin_market", "plugin_production"):
            sys.modules.pop(name, None)
        self.db = importlib.import_module("database")
        await self.db.initialize()
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(503)))
        self.subsystems = []

    async def asyncTearDown(self):
        for subsystem in reversed(self.subsystems):
            await subsystem.shutdown()
        await self.http.aclose()
        if self.old_db is None:
            os.environ.pop("WAVEFLOW_DB_PATH", None)
        else:
            os.environ["WAVEFLOW_DB_PATH"] = self.old_db
        if self.old_bootstrap is None:
            os.environ.pop("WAVEFLOW_OFFICIAL_PLUGIN_BOOTSTRAP", None)
        else:
            os.environ["WAVEFLOW_OFFICIAL_PLUGIN_BOOTSTRAP"] = self.old_bootstrap
        for name in ("database", "plugin_developer", "plugin_market", "plugin_production"):
            sys.modules.pop(name, None)
        self.tmp.cleanup()

    async def _subsystem(self):
        from plugin_production import ProductionPluginSubsystem

        subsystem = await ProductionPluginSubsystem.create(
            root=Path(self.tmp.name) / "plugins", http_client=self.http,
        )
        self.subsystems.append(subsystem)
        return subsystem

    async def test_init_validate_build_test_install_resolve_disable(self):
        root = Path(self.tmp.name)
        project = _scaffold(root, name="demo-tv", kind="tv", scheme="demotv")
        self.assertTrue(validate_project(project)["valid"])

        built = build_project(project)
        report = validate_project(built["manifest"])
        self.assertTrue(report["valid"])
        self.assertEqual(report["plugin"], "org.example/demo-tv")

        harness = await run_sdk_test(Path(built["manifest"]))
        self.assertEqual(harness["result"]["transport"], "hls")

        subsystem = await self._subsystem()
        await subsystem.set_developer_mode(True)
        installed = await subsystem.install_developer_local(built["manifest"])
        self.assertEqual(installed["trust_class"], "developer_local")

        runtime = subsystem.service.runtime
        instance = next(item for item in runtime.registry.instances.values()
                        if item.manifest.identity == "org.example/demo-tv")
        resolved = await runtime.request(instance, "tv.resolve_stream",
                                         {"scheme": "demotv", "resource_id": "one", "query": {}})
        self.assertEqual(resolved["transport"], "hls")

        disabled = await subsystem.disable("org.example/demo-tv")
        self.assertEqual(disabled["lifecycle_state"], "disabled")
        row = await self.db.get_plugin_installation("org.example", "demo-tv")
        self.assertEqual(row["lifecycle_state"], "disabled")


if __name__ == "__main__":
    unittest.main()
