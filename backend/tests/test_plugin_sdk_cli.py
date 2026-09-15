from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from plugin_runtime import PluginError, load_manifest, validate_stream_descriptor
from plugin_runtime.process import PluginProcess
from waveflow_plugin_cli import (
    CANONICAL_PLATFORMS,
    build_project,
    init_project,
    lock_dependencies,
    sign_build,
    test_build as run_build_test,
    validate_project,
)


FIXTURES = Path(__file__).parent / "fixtures" / "dependencies"


def manifest(plugin_id: str = "sdk-fixture") -> dict:
    return {"manifest_version": 1, "publisher_id": "org.waveflow", "plugin_id": plugin_id,
        "display_name": "SDK Fixture", "version": "1.0.0", "plugin_api_version": "1.0",
        "core_version_range": ">=0.1.0 <1.0.0",
        "provider_contracts": [{"contract": "tv_provider", "contract_version": "1.0", "features": ["resolve_stream"]}],
        "owned_schemes": [{"scheme": "sdkfixture", "contract": "tv_provider"}],
        "capabilities": ["tv.resolve_stream"],
        "permissions": {"network": {"managed": True, "allowed_hosts": ["api.example"]}},
        "runtime": {"type": "subprocess", "ipc": "stdio_framed_json_v1"},
        "artifacts": [{"os": os_name, "arch": arch, "runtime": "python", "entrypoint": "provider.py",
            "sha256": "0" * 64, "size_bytes": 1,
            "signature": {"algorithm": "ed25519", "key_id": "fixture-key", "value": "UNSIGNED"}}
            for os_name, arch in CANONICAL_PLATFORMS],
        "dependencies": [], "state_schema_version": 1}


PROVIDER = '''from waveflow_plugin_sdk import InvalidResource, PluginApplication, ResolveContext, StreamDescriptor, TVProvider, TVReference

class Provider(TVProvider):
    def resolve_stream(self, reference: TVReference, context: ResolveContext) -> StreamDescriptor:
        if reference.resource_id == "missing":
            raise InvalidResource("fixture missing")
        if reference.resource_id == "explode":
            raise RuntimeError("fixture-secret-must-not-cross-ipc")
        response = context.capabilities.managed_http(
            "https://api.example/resolve", response_mode="json", timeout=3)
        return StreamDescriptor.hls(response.body["url"], ttl_seconds=120,
            volatile_url=True, requires_proxy=True, headers={"Referer": "https://example.invalid/"})

identity, version = PluginApplication.identity_args("org.waveflow/sdk-fixture")
PluginApplication(identity=identity, version=version, permissions=["network"]).register_tv("sdkfixture", Provider()).run()
'''


class PluginSDKCLITest(unittest.IsolatedAsyncioTestCase):
    def test_application_rejects_invalid_and_duplicate_provider_schemes(self):
        from waveflow_plugin_sdk import PluginApplication, TVProvider, StreamDescriptor

        class Provider(TVProvider):
            def resolve_stream(self, reference, context):
                return StreamDescriptor.hls("https://example.invalid/live.m3u8")

        app = PluginApplication(identity="org.example/sdk", version="1.0.0")
        app.register_tv("Demo+1", Provider())
        with self.assertRaises(ValueError):
            app.register_tv("demo+1", Provider())
        with self.assertRaises(ValueError):
            app.register_radio("bad scheme", Provider())

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "provider.py").write_text(PROVIDER)
        (self.root / "manifest.json").write_text(json.dumps(manifest()))

    def tearDown(self):
        self.tmp.cleanup()

    async def test_build_spawn_duplex_descriptor_error_and_shutdown(self):
        self.assertTrue(validate_project(self.root)["valid"])
        first = build_project(self.root)
        first_bytes = Path(first["artifact"]).read_bytes()
        second = build_project(self.root)
        self.assertEqual(first_bytes, Path(second["artifact"]).read_bytes())
        result = await run_build_test(Path(first["manifest"]), method="tv.resolve_stream",
            payload={"scheme": "sdkfixture", "resource_id": "one", "query": {}},
            capabilities={"core.http.fetch": {"status": 200, "headers": {},
                "body": {"url": "https://media.example/live.m3u8"}, "response_mode": "json"}})
        self.assertEqual(result["hello"]["protocol_version"], "1.1")
        self.assertTrue(result["health"]["healthy"])
        self.assertEqual((result["result"]["url"], result["result"]["requires_proxy"]),
                         ("https://media.example/live.m3u8", True))
        validate_stream_descriptor(result["result"])

        built_manifest = load_manifest(first["manifest"])
        process = PluginProcess((sys.executable, first["artifact"], "--identity", built_manifest.identity), "sdk-error")
        await process.start()
        try:
            hello = await process.call("runtime.hello", {})
            process.negotiate_protocol(hello["protocol_version"])
            with self.assertRaises(PluginError) as raised:
                await process.call("tv.resolve_stream", {"scheme": "sdkfixture", "resource_id": "missing"})
            self.assertEqual((raised.exception.code, raised.exception.retryable), ("RESOURCE_NOT_FOUND", False))
            with self.assertRaises(PluginError) as exploded:
                await process.call("tv.resolve_stream", {"scheme": "sdkfixture", "resource_id": "explode"})
            # An uncaught exception is a Plugin defect, not a transient upstream
            # condition: it must not be advertised as retryable.
            self.assertEqual((exploded.exception.code, exploded.exception.retryable),
                             ("PLUGIN_CRASHED", False))
            self.assertNotIn("fixture-secret", exploded.exception.message)
        finally:
            await process.call("runtime.shutdown", {})
            await process.stop()

    async def test_sdk_cancel_interrupts_nested_capability_and_process_remains_usable(self):
        built = build_project(self.root)
        release = asyncio.Event()

        async def capability(_method, _payload, _timeout, _context):
            await release.wait()
            return {"status": 200, "headers": {},
                    "body": {"url": "https://media.example/after-cancel.m3u8"}, "response_mode": "json"}

        process = PluginProcess((sys.executable, built["artifact"], "--identity", "org.waveflow/sdk-fixture"),
                                "sdk-cancel", capability_handler=capability)
        await process.start()
        try:
            hello = await process.call("runtime.hello", {})
            process.negotiate_protocol(hello["protocol_version"])
            with self.assertRaises(PluginError) as timed_out:
                await process.call("tv.resolve_stream", {
                    "scheme": "sdkfixture", "resource_id": "one", "query": {},
                }, timeout=0.1)
            self.assertEqual(timed_out.exception.code, "PLUGIN_TIMEOUT")
            release.set()
            result = await process.call("tv.resolve_stream", {
                "scheme": "sdkfixture", "resource_id": "one", "query": {},
            })
            self.assertEqual(result["url"], "https://media.example/after-cancel.m3u8")
            self.assertEqual(process.protocol_violations, 0)
        finally:
            await process.call("runtime.shutdown", {})
            await process.stop()

    async def test_build_packages_project_resource_for_pyz_runtime(self):
        (self.root / "fixture.txt").write_text("packaged-resource", encoding="utf-8")
        provider = PROVIDER.replace("TVReference", "TVReference, load_resource_text", 1)
        provider = provider.replace(
            'if reference.resource_id == "missing":',
            'if load_resource_text("fixture.txt", anchor=__file__) != "packaged-resource":\n'
            '            raise RuntimeError("resource fixture was not packaged")\n'
            '        if reference.resource_id == "missing":',
        )
        (self.root / "provider.py").write_text(provider)
        built = build_project(self.root)
        with zipfile.ZipFile(built["artifact"]) as archive:
            self.assertIn("fixture.txt", archive.namelist())
        result = await run_build_test(
            Path(built["manifest"]), method="tv.resolve_stream",
            payload={"scheme": "sdkfixture", "resource_id": "one", "query": {}},
            capabilities={"core.http.fetch": {"status": 200, "headers": {},
                "body": {"url": "https://media.example/live.m3u8"}, "response_mode": "json"}},
        )
        self.assertTrue(result["health"]["healthy"])
        self.assertEqual(result["result"]["url"], "https://media.example/live.m3u8")

    def test_validate_build_sign_and_market_package(self):
        built = build_project(self.root)
        private = Ed25519PrivateKey.generate()
        key = self.root / "publisher.pem"
        key.write_bytes(private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                              serialization.NoEncryption()))
        signed = sign_build(built["manifest"], key, key_id="publisher-key")
        package = json.loads(Path(signed["package"]).read_text())
        artifact = Path(package["artifact_references"][0]["local_path"]).read_bytes()
        signature = base64.b64decode(package["plugin_manifest"]["artifacts"][0]["signature"]["value"])
        private.public_key().verify(signature, artifact)
        from plugin_market import manifest_signature_payload
        from plugin_runtime import validate_manifest
        manifest_signature = base64.b64decode(package["manifest_signature"]["value"])
        private.public_key().verify(
            manifest_signature, manifest_signature_payload(validate_manifest(package["plugin_manifest"])),
        )
        self.assertNotIn(private.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                               serialization.NoEncryption()), artifact)
        self.assertEqual(package["package_type"], "plugin_package")
        from plugin_market import candidates_from_packages
        candidates = candidates_from_packages([package], os_name="linux", arch="x86_64")
        self.assertEqual((len(candidates), candidates[0].identity), (1, "org.waveflow/sdk-fixture"))

    def test_offline_lock_covers_real_dependency_families_and_merge(self):
        requirements = self.root / "requirements.in"
        requirements.write_text("fixture\n")
        xxtea_dir = self.root / "xxtea"; xxtea_dir.mkdir()
        shutil_copy(FIXTURES / "xxtea-5.0.0-cp314-cp314-macosx_11_0_arm64.whl", xxtea_dir)
        lock_path = self.root / "dependency-lock.json"
        xxtea = lock_dependencies(requirements, lock_path, wheel_dir=xxtea_dir)
        self.assertEqual([item["name"] for item in xxtea["artifacts"]], ["xxtea"])

        crypto_dir = self.root / "crypto"; crypto_dir.mkdir()
        for name in ("cryptography-48.0.1-cp311-abi3-macosx_10_9_universal2.whl",
                     "cffi-2.0.0-cp314-cp314-macosx_11_0_arm64.whl", "pycparser-3.0-py3-none-any.whl"):
            shutil_copy(FIXTURES / name, crypto_dir)
        crypto = lock_dependencies(requirements, lock_path, wheel_dir=crypto_dir)
        self.assertEqual({item["name"] for item in crypto["artifacts"]}, {"cryptography", "cffi", "pycparser"})

        curl = lock_dependencies(requirements, lock_path, wheel_dir=FIXTURES / "ptbtv")
        self.assertIn("curl-cffi", {item["name"] for item in curl["artifacts"]})
        merged = lock_dependencies(requirements, lock_path, wheel_dir=xxtea_dir, merge=True)
        self.assertIn("xxtea", {item["name"] for item in merged["artifacts"]})

    def test_init_tv_and_radio_templates_use_same_contract(self):
        tv = self.root / "tv"; radio = self.root / "radio"
        init_project(tv, kind="tv", publisher="org.example", plugin_id="tv-demo", scheme="tvdemo")
        init_project(radio, kind="radio", publisher="org.example", plugin_id="radio-demo", scheme="radiodemo")
        self.assertTrue(validate_project(tv)["valid"])
        self.assertTrue(validate_project(radio)["valid"])
        radio_manifest = json.loads((radio / "manifest.json").read_text())
        self.assertEqual(radio_manifest["capabilities"], ["radio.catalog", "radio.resolve_stream"])
        self.assertEqual(radio_manifest["runtime"]["type"], "python")
        self.assertTrue((radio / "dependency-lock.json").is_file())
        self.assertIn("def catalog", (radio / "provider.py").read_text())

    def test_executable_cli_init_validate_build_sign_and_help(self):
        cli = Path(__file__).parents[2] / "waveflow-plugin"
        help_result = subprocess.run([str(cli), "--help"], capture_output=True, text=True, check=False)
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn("{init,validate,lock,build,sign,test}", help_result.stdout)

        project = self.root / "command-project"
        initialized = subprocess.run([
            str(cli), "init", str(project), "--kind", "tv", "--publisher", "org.example",
            "--plugin-id", "command-demo", "--scheme", "commanddemo",
        ], capture_output=True, text=True, check=False)
        self.assertEqual(initialized.returncode, 0, initialized.stderr)
        validated = subprocess.run([str(cli), "validate", str(project)], capture_output=True, text=True, check=False)
        self.assertEqual(validated.returncode, 0, validated.stderr)
        built = subprocess.run([str(cli), "build", str(project)], capture_output=True, text=True, check=False)
        self.assertEqual(built.returncode, 0, built.stderr)
        build_result = json.loads(built.stdout)

        private = Ed25519PrivateKey.generate()
        key = self.root / "command-key.pem"
        key.write_bytes(private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                              serialization.NoEncryption()))
        signed = subprocess.run([
            str(cli), "sign", build_result["manifest"], "--key", str(key), "--key-id", "command-key",
        ], capture_output=True, text=True, check=False)
        self.assertEqual(signed.returncode, 0, signed.stderr)
        tested = subprocess.run([
            str(cli), "test", build_result["manifest"],
            "--payload", '{"scheme":"commanddemo","resource_id":"one","query":{}}',
        ], capture_output=True, text=True, check=False)
        self.assertEqual(tested.returncode, 0, tested.stderr)
        self.assertEqual(json.loads(tested.stdout)["result"]["transport"], "hls")

    def test_executable_cli_lock_offline(self):
        cli = Path(__file__).parents[2] / "waveflow-plugin"
        requirements = self.root / "requirements-command.in"
        requirements.write_text("xxtea==5.0.0\n")
        wheel_dir = self.root / "command-wheels"; wheel_dir.mkdir()
        shutil_copy(FIXTURES / "xxtea-5.0.0-cp314-cp314-macosx_11_0_arm64.whl", wheel_dir)
        output = self.root / "command-lock.json"
        locked = subprocess.run([
            str(cli), "lock", str(requirements), "--output", str(output), "--wheel-dir", str(wheel_dir),
        ], capture_output=True, text=True, check=False)
        self.assertEqual(locked.returncode, 0, locked.stderr)
        self.assertEqual(json.loads(locked.stdout)["artifacts"][0]["name"], "xxtea")


def shutil_copy(source: Path, target: Path) -> None:
    destination = target / source.name if target.is_dir() else target
    destination.write_bytes(source.read_bytes())


if __name__ == "__main__":
    unittest.main()
