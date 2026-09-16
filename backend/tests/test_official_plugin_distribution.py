from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib
import json
import os
import sqlite3
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from packaging import tags

from tests.plugin_sources import legacy_root, plugin_source, plugins_root


BASE_IDENTITIES = {
    "org.waveflow/jstv", "org.waveflow/fjtv", "org.waveflow/nd0593tv", "org.waveflow/gzstv",
    "org.waveflow/nowtv", "org.waveflow/nmtv", "org.waveflow/sdtv",
}
STREAMGET_IDENTITY = "org.waveflow/streamget-providers"
STREAMGET_SCHEMES = {
    "yy", "bigo", "blued", "soop", "netease", "pandatv", "maoer", "look", "flextv", "popkontv",
    "twitcasting", "baidu", "weibo", "kugou", "twitch", "huajiao", "showroom", "inke", "acfun", "zhihu",
    "chzzk", "live17", "langlive", "changliao", "jd", "faceit", "lianjie", "sixroom", "huamao", "shopee",
    "laixiu", "picarto", "bilibili", "douyu", "douyin",
}
DIRECT_IDENTITIES = {"org.waveflow/ptbtv", "org.waveflow/hnntv", STREAMGET_IDENTITY}
IDENTITIES = BASE_IDENTITIES | DIRECT_IDENTITIES
SCHEMES = ({identity.rsplit("/", 1)[1] for identity in IDENTITIES - {STREAMGET_IDENTITY}} | STREAMGET_SCHEMES)
BASE_SCHEMES = {identity.rsplit("/", 1)[1] for identity in BASE_IDENTITIES}
# Plugin sources no longer live in this repository; Core resolves them through
# an explicit external root (see ``plugin_sources``).  The Plugin id is the
# source directory name, so a plan ``source`` is just the id.
#
# Plugin ids that are implemented against the SDK but have no official release
# channel yet.  Core keeps a legacy adapter for every one of their schemes.
LEGACY_ONLY_PLUGINS = {
    "hbtv", "hntv", "huya", "kuaishou", "migu", "qukan", "sdly", "sxbc",
    "tvb", "woniu", "xjtv", "youtube",
}
DEPENDENCIES = {
    "org.waveflow/nowtv": [],
    "org.waveflow/nmtv": [("xxtea", "5.0.0")],
    "org.waveflow/sdtv": [("cryptography", "48.0.1"), ("cffi", "2.0.0"), ("pycparser", "3.0")],
    "org.waveflow/ptbtv": [
        ("curl-cffi", "0.15.0"), ("cffi", "2.0.0"), ("certifi", "2026.5.20"),
        ("rich", "15.0.0"), ("pycparser", "3.0"), ("markdown-it-py", "4.2.0"),
        ("pygments", "2.20.0"), ("mdurl", "0.1.2"),
    ],
    "org.waveflow/hnntv": [],
    STREAMGET_IDENTITY: [
        ("anyio", "4.13.0"), ("certifi", "2026.5.20"), ("charset-normalizer", "3.4.7"),
        ("deprecated", "1.3.1"), ("distro", "1.9.0"), ("h11", "0.16.0"), ("h2", "4.3.0"),
        ("hpack", "4.1.0"), ("httpcore", "1.0.9"), ("httpx", "0.28.1"), ("hyperframe", "6.1.0"),
        ("idna", "3.16"), ("loguru", "0.7.3"), ("pycryptodome", "3.23.0"), ("pyexecjs", "1.5.1"),
        ("requests", "2.34.2"), ("six", "1.17.0"), ("streamget", "4.0.10"),
        ("tqdm", "4.67.3"), ("urllib3", "2.7.0"), ("wrapt", "2.2.1"),
    ],
}


def _clear_modules() -> None:
    for name in (
        "database", "market", "plugin_market", "plugin_production", "official_plugin_distribution",
        "routers.plugins",
    ):
        sys.modules.pop(name, None)


def _tree(root: Path) -> dict[str, bytes]:
    return {str(path.relative_to(root)): path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()}


class OfficialReleaseBuildTest(unittest.TestCase):
    def test_radio_release_plan_and_resource_backed_artifact(self):
        from build_official_plugins import build_release

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key = Ed25519PrivateKey.generate()
            key_path = root / "fixture-release-key.pem"
            key_path.write_bytes(key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
            ))
            public = base64.b64encode(key.public_key().public_bytes(
                serialization.Encoding.Raw, serialization.PublicFormat.Raw,
            )).decode()
            trust = root / "fixture-trust.json"
            trust.write_text(json.dumps({
                "schema_version": 1,
                "publishers": [{"publisher_id": "org.waveflow", "keys": [{
                    "key_id": "fixture-release-key", "public_key": public, "enabled": True,
                }]}],
            }))
            build_release(
                signing_key=key_path, key_id="fixture-release-key",
                output=root / "release", trust_path=trust, plugin_source_root=plugins_root(),
            )
            market = json.loads((root / "release" / "market.json").read_text())
            radio_ids = {
                item["plugin_manifest"]["plugin_id"]
                for item in market["packages"]
                if item["plugin_manifest"]["provider_contracts"][0]["contract"] == "radio_provider"
            }
            self.assertEqual(radio_ids, {"yunting", "myradio", "hitfm", "hk-sg-radio", "radiobrowser"})
            with zipfile.ZipFile(root / "release" / "payloads" / "hk-sg-radio-1.0.0.pyz") as archive:
                self.assertIn("stations.json", archive.namelist())

    def test_production_anchor_and_committed_packages_verify_without_private_material(self):
        from official_plugin_distribution import (
            OFFICIAL_DISTRIBUTION_ROOT, load_bundled_official_market, load_official_trust_rows,
            rollout_policy_allows_runtime,
        )
        from plugin_production import ProductionTrustPolicy
        from plugin_runtime import validate_manifest

        rows = load_official_trust_rows()
        self.assertEqual({row["publisher_id"] for row in rows}, {"org.waveflow"})
        self.assertTrue(all(row["trust_level"] == "official" for row in rows))
        self.assertTrue(all(row["require_manifest_signature"] for row in rows))
        self.assertEqual(list(OFFICIAL_DISTRIBUTION_ROOT.rglob("*.pem")), [])
        self.assertNotIn("PRIVATE KEY", (OFFICIAL_DISTRIBUTION_ROOT / "publisher-trust.json").read_text())

        _market, packages = load_bundled_official_market()
        self.assertEqual({f"{p['plugin_manifest']['publisher_id']}/{p['plugin_manifest']['plugin_id']}"
                          for p in packages}, IDENTITIES)
        self.assertEqual(_market["market_version"], "1.7.0")
        policy = ProductionTrustPolicy(rows)
        for package in packages:
            manifest = validate_manifest(package["plugin_manifest"])
            for artifact in manifest.artifacts:
                reference = next(item for item in package["artifact_references"]
                                 if item["sha256"] == artifact["sha256"])
                payload = Path(reference["_bundled_path"]).read_bytes()
                policy.verify_manifest(manifest, artifact, package["manifest_signature"])
                self.assertEqual(policy.verify(manifest, artifact, payload), "official")
            identity = manifest.identity
            lock = manifest.runtime.get("dependency_lock", {})
            expected = DEPENDENCIES.get(identity, [])
            self.assertEqual({(item["name"], item["version"]) for item in lock.get("artifacts", [])}, set(expected))
            dependency_refs = package.get("dependency_references", [])
            self.assertEqual(
                {item["sha256"] for item in dependency_refs},
                {item["sha256"] for item in lock.get("artifacts", [])},
            )
            for reference in dependency_refs:
                dependency = next(item for item in lock["artifacts"] if item["sha256"] == reference["sha256"])
                dependency_payload = Path(reference["_bundled_path"]).read_bytes()
                self.assertEqual(len(dependency_payload), dependency["size_bytes"])
                self.assertEqual(hashlib.sha256(dependency_payload).hexdigest(), dependency["sha256"])

        by_identity = {
            f"{item['plugin_manifest']['publisher_id']}/{item['plugin_manifest']['plugin_id']}": item
            for item in packages
        }
        for identity in ("org.waveflow/nowtv", "org.waveflow/nmtv", "org.waveflow/sdtv"):
            policy = by_identity[identity]["rollout"]
            self.assertTrue(rollout_policy_allows_runtime(
                policy, os_name="macos", arch="arm64", python_version="3.14.4",
            ))
            self.assertFalse(rollout_policy_allows_runtime(
                policy, os_name="linux", arch="x86_64", python_version="3.11.9",
            ))
        self.assertTrue(rollout_policy_allows_runtime(
            by_identity["org.waveflow/jstv"]["rollout"],
            os_name="linux", arch="x86_64", python_version="3.11.9",
        ))
        for identity in DIRECT_IDENTITIES:
            self.assertNotIn("rollout", by_identity[identity])
        self.assertEqual(
            {(item["os"], item["arch"]) for item in by_identity["org.waveflow/ptbtv"]["plugin_manifest"]["artifacts"]},
            {("macos", "arm64")},
        )
        self.assertEqual(
            {(item["os"], item["arch"]) for item in by_identity["org.waveflow/hnntv"]["plugin_manifest"]["artifacts"]},
            {("linux", "x86_64"), ("macos", "arm64")},
        )
        self.assertEqual(
            {(item["os"], item["arch"]) for item in by_identity[STREAMGET_IDENTITY]["plugin_manifest"]["artifacts"]},
            {("macos", "arm64")},
        )
        for identity in ("org.waveflow/ptbtv", "org.waveflow/hnntv"):
            network = by_identity[identity]["plugin_manifest"]["permissions"]["network"]
            self.assertTrue(network["managed"] and network["direct"])
        network = by_identity[STREAMGET_IDENTITY]["plugin_manifest"]["permissions"]["network"]
        self.assertTrue(network["direct"])
        self.assertNotIn("managed", network)

    def test_nmtv_sdtv_candidates_cover_published_targets_without_cross_platform_fallback(self):
        from plugin_python_runtime import select_dependency_artifacts
        from plugin_runtime import PluginError

        _market, packages = importlib.import_module("official_plugin_distribution").load_bundled_official_market()
        by_identity = {
            f"{item['plugin_manifest']['publisher_id']}/{item['plugin_manifest']['plugin_id']}": item
            for item in packages
        }
        macos_cp314 = {
            tags.Tag("cp314", "cp314", "macosx_11_0_arm64"),
            tags.Tag("cp311", "abi3", "macosx_10_9_universal2"),
            tags.Tag("py3", "none", "any"),
        }
        linux_cp311 = {
            tags.Tag("cp311", "cp311", "manylinux2014_x86_64"),
            tags.Tag("cp311", "abi3", "manylinux2014_x86_64"),
            tags.Tag("py3", "none", "any"),
        }
        linux_arm64_cp311 = {
            tags.Tag("cp311", "cp311", "manylinux2014_aarch64"),
            tags.Tag("cp311", "abi3", "manylinux2014_aarch64"),
            tags.Tag("py3", "none", "any"),
        }
        expected = {
            "org.waveflow/nmtv": {
                "macos": {"83212c868cd88decde39467579282464853942e36764031f9507ee81e803ac9a"},
                "linux": {"b11d6b8119e1f4413e07f24741b6d1ad78a93012d968afabd15448b9912712ac"},
            },
            "org.waveflow/sdtv": {
                "macos": {
                    "3e4a1a3232eef2e6c732827d5722db29a0cc8b27af2a4d865b094cf954be9ca1",
                    "c654de545946e0db659b3400168c9ad31b5d29593291482c43e3564effbcee13",
                    "b727414169a36b7d524c1c3e31839a521725078d7b2ff038656844266160a992",
                },
                "linux": {
                    "f0d27a5696721ef7a672b8c810f6aded391058e0b9486e63e6d93baf765da691",
                    "8941aaadaf67246224cee8c3803777eed332a19d909b47e29c9842ef1e79ac26",
                    "b727414169a36b7d524c1c3e31839a521725078d7b2ff038656844266160a992",
                },
            },
        }
        for identity, package in by_identity.items():
            if identity not in expected:
                continue
            manifest = package["plugin_manifest"]
            self.assertEqual({(item["os"], item["arch"]) for item in manifest["artifacts"]},
                             {( "macos", "arm64"), ("linux", "x86_64")})
            lock = manifest["runtime"]["dependency_lock"]
            self.assertEqual({item["sha256"] for item in select_dependency_artifacts(lock, supported_tags=macos_cp314)},
                             expected[identity]["macos"])
            self.assertEqual({item["sha256"] for item in select_dependency_artifacts(lock, supported_tags=linux_cp311)},
                             expected[identity]["linux"])
            with self.assertRaises(PluginError) as unsupported:
                select_dependency_artifacts(lock, supported_tags=linux_arm64_cp311)
            self.assertEqual(unsupported.exception.code, "DEPENDENCY_PLATFORM_UNSUPPORTED")

    def test_release_builder_is_deterministic_with_a_test_only_key(self):
        from build_official_plugins import build_release

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key = Ed25519PrivateKey.generate()
            key_path = root / "fixture-release-key.pem"
            key_path.write_bytes(key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
            ))
            public = base64.b64encode(key.public_key().public_bytes(
                serialization.Encoding.Raw, serialization.PublicFormat.Raw,
            )).decode()
            trust = root / "fixture-trust.json"
            trust.write_text(json.dumps({
                "schema_version": 1,
                "publishers": [{"publisher_id": "org.waveflow", "keys": [{
                    "key_id": "fixture-release-key", "public_key": public, "enabled": True,
                }]}],
            }))
            first, second = root / "first", root / "second"
            build_release(signing_key=key_path, key_id="fixture-release-key", output=first, trust_path=trust,
                          plugin_source_root=plugins_root())
            build_release(signing_key=key_path, key_id="fixture-release-key", output=second, trust_path=trust,
                          plugin_source_root=plugins_root())
            self.assertEqual(_tree(first), _tree(second))


class ExternalPluginSourceRootTest(unittest.TestCase):
    """The official builder must accept Plugin sources that live outside the repository.

    Every Plugin source now lives outside the repository, so the release
    pipeline is pointed at an explicitly configured external source root.  That
    root must not weaken the artifact/dependency path checks and must never be
    derived from the process working directory.
    """

    def _release_key(self, root: Path) -> tuple[Path, Path]:
        key = Ed25519PrivateKey.generate()
        key_path = root / "fixture-release-key.pem"
        key_path.write_bytes(key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
        ))
        public = base64.b64encode(key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw,
        )).decode()
        trust = root / "fixture-trust.json"
        trust.write_text(json.dumps({
            "schema_version": 1,
            "publishers": [{"publisher_id": "org.waveflow", "keys": [{
                "key_id": "fixture-release-key", "public_key": public, "enabled": True,
            }]}],
        }))
        return key_path, trust

    def _plan(self, path: Path, *, source: str, source_root: str | None = None) -> Path:
        item: dict = {
            "plugin_id": "yunting",
            "name": "Yunting Radio Provider Plugin",
            "description": "WaveFlow official Yunting Radio Provider.",
            "source": source,
            "tags": ["Radio Provider", "Catalog", "yunting"],
            "providers": ["Yunting"],
            "categories": ["插件", "Radio"],
            "publisher_name": "WaveFlow",
            "display": {"subtitle": "Radio Provider Plugin", "summary": "云听电台目录与播放地址。"},
        }
        if source_root is not None:
            item["source_root"] = source_root
        path.write_text(json.dumps({
            "schema_version": 1, "publisher_id": "org.waveflow",
            "release_version": "1.8.0", "updated_at": "2026-09-15T00:00:00Z",
            "plugins": [item],
        }))
        return path

    def test_external_source_root_builds_the_same_plugin_outside_the_repository(self):
        from build_official_plugins import REPOSITORY_ROOT, build_release
        from official_plugin_distribution import load_bundled_official_market

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key_path, trust = self._release_key(root)
            external = root / "external-plugins"
            external.mkdir()
            shutil.copytree(plugin_source("yunting"), external / "yunting")
            self.assertFalse(external.is_relative_to(REPOSITORY_ROOT))

            external_plan = self._plan(root / "external-plan.json", source="yunting", source_root="external")
            external_output = root / "external-release"
            result = build_release(
                signing_key=key_path, key_id="fixture-release-key", output=external_output,
                trust_path=trust, plugin_source_root=external, plan_path=external_plan,
            )
            self.assertEqual(result["packages"], ["official::yunting-plugin"])
            market, packages = load_bundled_official_market(external_output)
            self.assertEqual({item["plugin_manifest"]["plugin_id"] for item in packages}, {"yunting"})
            self.assertEqual(market["market_version"], "1.8.0")
            self.assertTrue((external_output / "payloads" / "yunting-1.0.0.pyz").is_file())

            # The same source built straight from the real checkout produces
            # identical bytes, so the configured root is not part of the release.
            checkout_plan = self._plan(root / "checkout-plan.json", source="yunting", source_root="external")
            checkout_output = root / "checkout-release"
            build_release(
                signing_key=key_path, key_id="fixture-release-key", output=checkout_output,
                trust_path=trust, plugin_source_root=plugins_root(), plan_path=checkout_plan,
            )
            self.assertEqual(_tree(checkout_output), _tree(external_output))

    def test_the_whole_release_plan_builds_identically_from_an_external_source_root(self):
        """Every official Plugin source can leave the repository unchanged.

        This is the full-scale form of the extraction smoke test: every plan
        entry is copied to a second external root and rebuilt.  The produced
        release must be byte-identical to the one built straight from the
        checkout, which is what makes "the source location is not part of the
        release" an evidenced claim rather than an assumption.
        """
        from build_official_plugins import OFFICIAL_DISTRIBUTION_ROOT, build_release

        plan = json.loads((OFFICIAL_DISTRIBUTION_ROOT / "release-plan.json").read_text())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key_path, trust = self._release_key(root)
            external = root / "external-plugins"
            external.mkdir()
            external_plugins = []
            for item in plan["plugins"]:
                source = plugin_source(item["source"])
                self.assertTrue(source.is_dir(), item["source"])
                shutil.copytree(source, external / source.name)
                external_plugins.append({**item, "source": source.name, "source_root": "external"})
            external_plan = root / "external-plan.json"
            external_plan.write_text(json.dumps({**plan, "plugins": external_plugins}))

            from_checkout = root / "checkout-release"
            from_copy = root / "copied-release"
            build_release(
                signing_key=key_path, key_id="fixture-release-key",
                output=from_checkout, trust_path=trust, plugin_source_root=plugins_root(),
            )
            build_release(
                signing_key=key_path, key_id="fixture-release-key",
                output=from_copy, trust_path=trust,
                plugin_source_root=external, plan_path=external_plan,
            )

            self.assertEqual(_tree(from_checkout), _tree(from_copy))
            market = json.loads((from_copy / "market.json").read_text())
            self.assertEqual(
                {item["plugin_manifest"]["plugin_id"] for item in market["packages"]},
                {item["plugin_id"] for item in plan["plugins"]},
            )
            # The dependency-locked Plugins go through the untouched dependency
            # artifact path checks from the external source root as well.
            self.assertEqual(
                {path.name for path in (from_copy / "payloads" / "dependencies").iterdir()},
                {"nmtv", "sdtv", "ptbtv", "streamget-providers"},
            )

    def test_external_source_root_cannot_escape_and_repository_containment_holds(self):
        from build_official_plugins import build_release
        from plugin_runtime import PluginError

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key_path, trust = self._release_key(root)
            external = root / "external-plugins"
            external.mkdir()
            shutil.copytree(plugin_source("yunting"), root / "outside" / "yunting")

            def build(name: str, *, source: str, source_root: str | None = None, configured: Path | None):
                plan = self._plan(root / f"{name}.json", source=source, source_root=source_root)
                return build_release(
                    signing_key=key_path, key_id="fixture-release-key", output=root / f"{name}-release",
                    trust_path=trust, plugin_source_root=configured, plan_path=plan,
                )

            # Positive control: a Plugin that really is inside the configured
            # root builds.  Without this the rejections below would also pass
            # when external roots are simply unsupported.
            shutil.copytree(plugin_source("yunting"), external / "yunting")
            inside = build("inside", source="yunting", source_root="external", configured=external)
            self.assertEqual(inside["packages"], ["official::yunting-plugin"])

            # An external entry may not traverse out of the configured root...
            with self.assertRaises(PluginError) as escaped:
                build("escape", source="../outside/yunting", source_root="external", configured=external)
            self.assertEqual(escaped.exception.code, "ARTIFACT_NOT_FOUND")
            # ...and cannot smuggle the same source in as an absolute path.
            with self.assertRaises(PluginError) as absolute:
                build("absolute", source=str(root / "outside" / "yunting"), source_root="external",
                      configured=external)
            self.assertEqual(absolute.exception.code, "ARTIFACT_NOT_FOUND")
            # The historical in-repository containment guarantee is unchanged.
            with self.assertRaises(PluginError) as repository:
                build("repository", source="../../../outside/yunting", configured=external)
            self.assertEqual(repository.exception.code, "ARTIFACT_NOT_FOUND")
            with self.assertRaises(PluginError) as absolute_repository:
                build("absolute-repository", source=str(external / "yunting"), configured=external)
            self.assertEqual(absolute_repository.exception.code, "ARTIFACT_NOT_FOUND")

    def test_external_source_root_configuration_is_explicit_and_fail_closed(self):
        from build_official_plugins import build_release
        from plugin_runtime import PluginError

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key_path, trust = self._release_key(root)
            external = root / "external-plugins"
            external.mkdir()
            shutil.copytree(plugin_source("yunting"), external / "yunting")
            plan = self._plan(root / "external-plan.json", source="yunting", source_root="external")

            def build(output: str, **kwargs):
                return build_release(
                    signing_key=kwargs.pop("signing_key", key_path), key_id="fixture-release-key",
                    output=root / output, trust_path=trust, plan_path=plan, **kwargs,
                )

            # A relative root would make the build depend on the working directory.
            with self.assertRaises(PluginError) as relative:
                build("relative-release", plugin_source_root=Path("external-plugins"))
            self.assertEqual(relative.exception.code, "INVALID_PLUGIN_RESPONSE")
            # An external entry without a configured root fails closed.
            with self.assertRaises(PluginError) as unconfigured:
                build("unconfigured-release")
            self.assertEqual(unconfigured.exception.code, "ARTIFACT_NOT_FOUND")
            with self.assertRaises(PluginError) as missing:
                build("missing-release", plugin_source_root=root / "absent")
            self.assertEqual(missing.exception.code, "ARTIFACT_NOT_FOUND")

            # The release key must not live inside the external Plugin source root.
            inside = self._release_key(external)
            with self.assertRaises(PluginError) as key_inside:
                build("key-release", signing_key=inside[0], plugin_source_root=external)
            self.assertEqual(key_inside.exception.code, "AUTH_FAILED")

            # An unrecognised source root marker is rejected instead of ignored.
            unknown = self._plan(root / "unknown-plan.json", source="yunting", source_root="somewhere-else")
            with self.assertRaises(PluginError) as invalid:
                build_release(
                    signing_key=key_path, key_id="fixture-release-key", output=root / "unknown-release",
                    trust_path=trust, plugin_source_root=external, plan_path=unknown,
                )
            self.assertEqual(invalid.exception.code, "INVALID_PLUGIN_RESPONSE")

    def test_repository_source_root_marker_matches_the_implicit_default(self):
        """The ``repository`` marker stays equivalent to omitting it.

        No Plugin source lives in the repository any more, so the rule is
        exercised at the resolution level: both forms must resolve to the same
        in-repository directory and stay inside the repository.
        """
        from build_official_plugins import (
            OFFICIAL_DISTRIBUTION_ROOT, REPOSITORY_ROOT, _resolve_plugin_source,
        )

        implicit = _resolve_plugin_source({"source": "distribution"}, None)
        explicit = _resolve_plugin_source({"source": "distribution", "source_root": "repository"}, None)
        self.assertEqual(implicit, explicit)
        self.assertEqual(implicit, (OFFICIAL_DISTRIBUTION_ROOT / "distribution").resolve())
        self.assertTrue(implicit.is_relative_to(REPOSITORY_ROOT))

    def test_signing_key_must_stay_outside_the_repository(self):
        import build_official_plugins as builder
        from plugin_runtime import PluginError

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            # A stand-in repository root keeps the real checkout untouched while
            # still exercising the real containment check.  It must be
            # canonicalized: the guard compares fully resolved paths.
            repository = root / "repository"
            repository.mkdir()
            key_path, trust = self._release_key(repository)
            plan = self._plan(root / "repo-plan.json", source="yunting")
            with mock.patch.object(builder, "REPOSITORY_ROOT", repository):
                with self.assertRaises(PluginError) as inside:
                    builder.build_release(
                        signing_key=key_path, key_id="fixture-release-key", output=root / "release",
                        trust_path=trust, plan_path=plan,
                    )
            self.assertEqual(inside.exception.code, "AUTH_FAILED")

    def test_cli_requires_an_absolute_plugin_source_root(self):
        import build_official_plugins as builder

        captured: dict = {}

        def fake_build(**kwargs):
            captured.update(kwargs)
            return {"packages": []}

        absolute = Path(tempfile.gettempdir()) / "external-plugins"
        with mock.patch.object(builder, "build_release", side_effect=fake_build):
            with mock.patch.object(sys, "argv", [
                "build_official_plugins.py", "--signing-key", "k.pem", "--key-id", "kid",
                "--output", "out", "--plugin-source-root", str(absolute),
            ]):
                self.assertEqual(builder.main(), 0)
            self.assertEqual(captured["plugin_source_root"], absolute)

            # A relative root would make the release depend on the working directory.
            captured.clear()
            with mock.patch.object(sys, "argv", [
                "build_official_plugins.py", "--signing-key", "k.pem", "--key-id", "kid",
                "--output", "out", "--plugin-source-root", "external-plugins",
            ]):
                with self.assertRaises(SystemExit) as exit_code:
                    builder.main()
            self.assertEqual(exit_code.exception.code, 2)
            self.assertEqual(captured, {})


class OfficialPluginInventoryTest(unittest.TestCase):
    """Pin the plan/distribution classification that extraction depends on.

    Plugin sources live in the market repository, split into ``plugins/`` (what
    the release plan publishes) and ``legacy/`` (implemented against the SDK,
    no official channel yet).  These guards keep the split explicit so a Plugin
    cannot silently gain or lose an official channel, and so no Plugin source
    tree creeps back into this repository.
    """

    def _plan(self) -> dict:
        return json.loads((Path(__file__).parents[1] / "official_plugins" / "release-plan.json").read_text())

    def test_release_plan_entries_match_their_source_manifests(self):
        from build_official_plugins import REPOSITORY_ROOT
        from plugin_runtime import validate_manifest

        plan = self._plan()
        self.assertEqual(plan["publisher_id"], "org.waveflow")
        entries = {item["plugin_id"]: item for item in plan["plugins"]}
        self.assertEqual(len(entries), len(plan["plugins"]), "release plan repeats a plugin_id")
        for plugin_id, item in entries.items():
            with self.subTest(plugin=plugin_id):
                # The plan stores no absolute path and no repository-relative
                # escape, only a directory name resolved against the root the
                # build is handed.
                self.assertEqual(item.get("source_root"), "external")
                self.assertEqual(item["source"], plugin_id)
                source = plugin_source(item["source"])
                self.assertTrue(source.is_dir(), item["source"])
                self.assertFalse(source.is_relative_to(REPOSITORY_ROOT))
                manifest = validate_manifest(json.loads((source / "manifest.json").read_text()))
                # Identity comes from the manifest; the plan must agree with it.
                self.assertEqual(manifest.plugin_id, plugin_id)
                self.assertEqual(manifest.publisher_id, "org.waveflow")
                self.assertEqual(source.name, plugin_id)
                self.assertTrue(item.get("name"), "release plan entry has no name")
                self.assertTrue(item.get("description"), "release plan entry has no description")

    def test_every_plugin_source_is_either_official_or_legacy_only(self):
        from adapters import parse_adapter_url
        from plugin_runtime import validate_manifest

        plan_directories = {item["source"] for item in self._plan()["plugins"]}
        official_directories = {path.name for path in plugins_root().iterdir() if path.is_dir()}
        legacy_directories = {path.name for path in legacy_root().iterdir() if path.is_dir()}

        # Every source directory is accounted for exactly once, so adding a
        # Plugin forces an explicit publish-or-not decision.
        self.assertEqual(official_directories, plan_directories)
        self.assertEqual(legacy_directories, LEGACY_ONLY_PLUGINS)
        self.assertEqual(official_directories & legacy_directories, set())

        # Plugins without an official channel keep their Core legacy adapter, so
        # waiting in legacy/ removes no production path.
        for name in sorted(LEGACY_ONLY_PLUGINS):
            with self.subTest(plugin=name):
                self.assertNotIn(name, plan_directories)
                manifest = validate_manifest(
                    json.loads((legacy_root() / name / "manifest.json").read_text()),
                )
                self.assertTrue(manifest.owned_schemes)
                for scheme, _contract in manifest.owned_schemes:
                    self.assertEqual(parse_adapter_url(f"{scheme}://probe").adapter, scheme)

    def test_no_plugin_source_tree_remains_in_the_repository(self):
        """Extraction only holds while the source tree stays out of the repo."""
        from build_official_plugins import REPOSITORY_ROOT

        for relative in ("bundled_plugins", "backend/bundled_plugins"):
            with self.subTest(path=relative):
                self.assertFalse((REPOSITORY_ROOT / relative).exists())

    def test_distribution_never_contains_a_plugin_outside_the_release_plan(self):
        from official_plugin_distribution import OFFICIAL_RELEASE_ROOT, load_bundled_official_market

        plan_ids = {item["plugin_id"] for item in self._plan()["plugins"]}
        _market, packages = load_bundled_official_market(OFFICIAL_RELEASE_ROOT)
        distributed = {item["plugin_manifest"]["plugin_id"] for item in packages}
        self.assertTrue(distributed, "committed distribution is empty")
        self.assertLessEqual(distributed, plan_ids)

    def test_committed_distribution_survives_runtime_envelope_validation(self):
        from official_plugin_distribution import OFFICIAL_RELEASE_ROOT, load_bundled_official_market
        from plugin_market import _assert_package_manifest_consistency
        from plugin_runtime import validate_manifest

        _market, packages = load_bundled_official_market(OFFICIAL_RELEASE_ROOT)
        for package in packages:
            manifest = validate_manifest(package["plugin_manifest"])
            with self.subTest(plugin=manifest.identity):
                # The committed release must pass the same envelope/manifest check
                # that installs it, independently of the builder that produced it.
                _assert_package_manifest_consistency(package, manifest)
                self.assertEqual(package["publisher"]["id"], manifest.publisher_id)
                self.assertEqual(package["version"], manifest.version)
                self.assertEqual(
                    {item["signature"]["key_id"] for item in package["plugin_manifest"]["artifacts"]},
                    {package["manifest_signature"]["key_id"]},
                )


class ExternalPluginSourceRootInstallTest(unittest.IsolatedAsyncioTestCase):
    """外部 source root 的端到端冒烟：构建 → 签名/打包 → 装载目录 → 安装激活。

    这是抽取后的常规通路：Plugin 源码已不在本仓库，用外部 source root 构建出的
    发布产物必须仍然能走完整的官方安装/激活链路。
    """

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = os.environ.get("WAVEFLOW_DB_PATH")
        os.environ["WAVEFLOW_DB_PATH"] = str(Path(self.tmp.name) / "waveflow.db")
        _clear_modules()
        self.db = importlib.import_module("database")
        await self.db.initialize()
        self.client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(503, text="offline")),
        )
        self.subsystem = None

    async def asyncTearDown(self):
        if self.subsystem is not None:
            await self.subsystem.shutdown()
        await self.client.aclose()
        if self.old_db is None:
            os.environ.pop("WAVEFLOW_DB_PATH", None)
        else:
            os.environ["WAVEFLOW_DB_PATH"] = self.old_db
        _clear_modules()
        self.tmp.cleanup()

    async def test_external_source_root_release_installs_and_activates(self):
        from build_official_plugins import build_release
        from official_plugin_distribution import load_bundled_official_market
        from plugin_production import ProductionPluginSubsystem

        root = Path(self.tmp.name)
        key = Ed25519PrivateKey.generate()
        public = base64.b64encode(key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw,
        )).decode()
        key_path = root / "fixture-release-key.pem"
        key_path.write_bytes(key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
        ))
        trust_path = root / "fixture-trust.json"
        trust_path.write_text(json.dumps({
            "schema_version": 1,
            "publishers": [{"publisher_id": "org.waveflow", "keys": [{
                "key_id": "fixture-release-key", "public_key": public, "enabled": True,
            }]}],
        }))

        # The Plugin source lives outside the Core repository.
        external = root / "external-plugins"
        external.mkdir()
        shutil.copytree(plugin_source("yunting"), external / "yunting")
        plan = root / "external-plan.json"
        plan.write_text(json.dumps({
            "schema_version": 1, "publisher_id": "org.waveflow",
            "release_version": "1.8.0", "updated_at": "2026-09-15T00:00:00Z",
            "plugins": [{
                "plugin_id": "yunting", "name": "Yunting Radio Provider Plugin",
                "description": "External source root smoke", "source": "yunting",
                "source_root": "external",
                "tags": ["Radio Provider"], "providers": ["Yunting"],
                "categories": ["插件", "Radio"], "publisher_name": "WaveFlow",
                "display": {"subtitle": "Radio Provider Plugin", "summary": "外部源根构建冒烟。"},
            }],
        }))
        release = root / "external-release"
        build_release(
            signing_key=key_path, key_id="fixture-release-key", output=release,
            trust_path=trust_path, plugin_source_root=external, plan_path=plan,
        )
        _market, packages = load_bundled_official_market(release)
        self.assertEqual({item["plugin_manifest"]["plugin_id"] for item in packages}, {"yunting"})

        self.subsystem = await ProductionPluginSubsystem.create(
            root=root / "plugin-store", http_client=self.client,
        )
        self.subsystem.official_release_root = release
        # The release above is signed with a test-only key, so the trust anchor
        # is replaced instead of weakening the production verification path.
        self.subsystem.trust_policy.replace([{
            "publisher_id": "org.waveflow", "key_id": "fixture-release-key", "public_key": public,
            "trust_level": "official", "enabled": 1, "require_manifest_signature": True,
        }])
        prepared, temporary_paths = await self.subsystem._prepare_packages(packages)
        try:
            installed = await self.subsystem.service.install_from_packages(
                prepared, "org.waveflow/yunting",
            )
            self.assertEqual(
                (installed["trust_state"], installed["source_key"],
                 installed["lifecycle_state"], installed["enabled"]),
                ("official", "official", "active", 1),
            )
            instance = self.subsystem.service.runtime.registry.route("yunting")
            self.assertEqual((instance.health, instance.manifest.version), ("healthy", "1.0.0"))
            row = await self.db.get_plugin_installation("org.waveflow", "yunting")
            self.assertEqual((row["active_version"], row["trust_state"]), ("1.0.0", "official"))
        finally:
            for path in temporary_paths:
                path.unlink(missing_ok=True)


class OfficialDistributionProductionTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = os.environ.get("WAVEFLOW_DB_PATH")
        self.old_bootstrap = os.environ.get("WAVEFLOW_OFFICIAL_PLUGIN_BOOTSTRAP")
        self.old_rollout = os.environ.get("WAVEFLOW_OFFICIAL_PLUGIN_ROLLOUT")
        os.environ["WAVEFLOW_DB_PATH"] = str(Path(self.tmp.name) / "waveflow.db")
        os.environ["WAVEFLOW_OFFICIAL_PLUGIN_BOOTSTRAP"] = "1"
        os.environ["WAVEFLOW_OFFICIAL_PLUGIN_ROLLOUT"] = "0"
        _clear_modules()
        self.db = importlib.import_module("database")
        await self.db.initialize()
        self.network_requests: list[httpx.Request] = []
        self.provider_fixture = False

        def transport(request: httpx.Request) -> httpx.Response:
            self.network_requests.append(request)
            if self.provider_fixture:
                if request.url.host == "www.ptbtv.com":
                    return httpx.Response(
                        200,
                        json=[{"m3u8": "https://media.example/ptbtv-official/live.m3u8"}],
                    )
                if request.url.host == "www.hnntv.cn":
                    return httpx.Response(200, json={"resultSet": [{"schedules": [{
                        "id": "official-replay",
                        "startDatetime": "2026-08-11 12:00:00",
                        "endDatetime": "2026-08-11 12:30:00",
                        "programName": "Official HNNTV fixture",
                    }]}]})
                if request.url.host == "ps.hnntv.cn" and request.url.path.endswith("/wbPlayUrl"):
                    return httpx.Response(
                        200,
                        text='{"url":"https:\\/\\/media.example\\/hnntv-official\\/replay.m3u8"}',
                    )
            return httpx.Response(503, text="offline")

        self.client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
        self.subsystems = []
        self.safe = mock.patch("plugin_capabilities.assert_safe_target_url", new=mock.AsyncMock())
        self.safe.start()

    async def asyncTearDown(self):
        for subsystem in reversed(self.subsystems):
            await subsystem.shutdown()
        self.safe.stop()
        await self.client.aclose()
        if self.old_db is None:
            os.environ.pop("WAVEFLOW_DB_PATH", None)
        else:
            os.environ["WAVEFLOW_DB_PATH"] = self.old_db
        if self.old_bootstrap is None:
            os.environ.pop("WAVEFLOW_OFFICIAL_PLUGIN_BOOTSTRAP", None)
        else:
            os.environ["WAVEFLOW_OFFICIAL_PLUGIN_BOOTSTRAP"] = self.old_bootstrap
        if self.old_rollout is None:
            os.environ.pop("WAVEFLOW_OFFICIAL_PLUGIN_ROLLOUT", None)
        else:
            os.environ["WAVEFLOW_OFFICIAL_PLUGIN_ROLLOUT"] = self.old_rollout
        _clear_modules()
        self.tmp.cleanup()

    async def _subsystem(self):
        from plugin_production import ProductionPluginSubsystem

        subsystem = await ProductionPluginSubsystem.create(
            root=Path(self.tmp.name) / "plugin-store", http_client=self.client,
        )

        def command_factory(manifest, artifact):
            command = [sys.executable, str(artifact), "--identity", manifest.identity,
                       "--version", manifest.version]
            if manifest.plugin_id == "hnntv":
                command.extend(["--fixture-live-url", "https://media.example/hnntv-official/live.m3u8"])
            return tuple(command)

        def runtime_command_factory(manifest, artifact, environment):
            if manifest.plugin_id == "hnntv":
                return command_factory(manifest, artifact)
            python = str(environment.python) if environment else sys.executable
            command = [python, "-I", str(artifact), "--identity", manifest.identity,
                       "--version", manifest.version]
            if manifest.plugin_id == "ptbtv":
                command.append("--fixture-direct-unavailable")
            return tuple(command)

        # Keep the production subsystem and its generic lifecycle intact while
        # making this release acceptance deterministic at the provider boundary.
        subsystem.service.command_factory = command_factory
        subsystem.service.runtime_command_factory = runtime_command_factory
        self.subsystems.append(subsystem)
        return subsystem

    async def _restart(self, subsystem):
        await subsystem.shutdown()
        self.subsystems.remove(subsystem)
        restarted = await self._subsystem()
        await restarted.startup()
        return restarted

    async def test_fresh_bootstrap_installs_official_plugins_keeps_legacy_and_projects_settings(self):
        subsystem = await self._subsystem()
        results = await subsystem.startup()
        self.assertEqual({item["plugin"] for item in results if item.get("bootstrap") == "installed"}, BASE_IDENTITIES)
        self.assertEqual(
            {item["plugin"] for item in results if item.get("status") == "unavailable"}, DIRECT_IDENTITIES,
        )
        rows = await self.db.list_plugin_installations()
        self.assertEqual({f"{row['publisher_id']}/{row['plugin_id']}" for row in rows}, BASE_IDENTITIES)
        self.assertTrue(all(row["lifecycle_state"] == "active" and row["trust_state"] == "official" for row in rows))
        self.assertEqual(await self.db.list_plugin_scheme_ownership(), [])
        self.assertTrue(all(subsystem.provider_resolver.mode(scheme) == "legacy" for scheme in SCHEMES))

        router = importlib.import_module("routers.plugins")
        projections = [await router._plugin_projection(row) for row in rows]
        self.assertTrue(all(item["runtime_available"] and item["trust_state"] == "official" for item in projections))
        self.assertTrue(all(item["source_provenance"]["source_key"] == "official" for item in projections))
        projection_by_plugin = {item["plugin"]: item for item in projections}
        self.assertEqual(
            projection_by_plugin["org.waveflow/nmtv"]["runtime"],
            {
                "type": "python", "python_version_range": ">=3.11.0 <3.15.0",
                "environment_status": "ready", "dependency_count": 1,
                "dependencies": [{"name": "xxtea", "version": "5.0.0"}],
            },
        )
        self.assertEqual(
            projection_by_plugin["org.waveflow/sdtv"]["runtime"]["dependencies"],
            [{"name": name, "version": version} for name, version in DEPENDENCIES["org.waveflow/sdtv"]],
        )
        self.assertEqual(projection_by_plugin["org.waveflow/nowtv"]["runtime"]["dependency_count"], 0)
        self.assertTrue(all(item["ownership"] == [{
            "scheme": item["owned_schemes"][0], "mode": "legacy", "plugin": "",
        }] for item in projections))
        market = importlib.import_module("market")
        with mock.patch.object(market, "safe_http_fetch", new=mock.AsyncMock(side_effect=market.MarketError("offline", 502))):
            await market.refresh_market()
        cards = [item for item in await market.list_packages({}) if item.get("package_type") == "plugin_package"]
        self.assertEqual(len(cards), len(IDENTITIES))
        cards_by_identity = {
            f"{item['plugin']['publisher_id']}/{item['plugin']['plugin_id']}": item for item in cards
        }
        self.assertTrue(all(cards_by_identity[item]["installed"] for item in BASE_IDENTITIES))
        self.assertTrue(all(not cards_by_identity[item]["installed"] for item in DIRECT_IDENTITIES))
        self.assertEqual(cards_by_identity["org.waveflow/ptbtv"]["plugin"]["permissions"],
                         ["network.direct", "network.managed"])
        self.assertEqual(cards_by_identity["org.waveflow/hnntv"]["plugin"]["permissions"],
                         ["network.direct", "network.managed"])
        self.assertEqual(cards_by_identity[STREAMGET_IDENTITY]["plugin"]["permissions"], ["network.direct"])
        self.assertEqual(self.network_requests, [])
        card_by_plugin = {
            f"{item['plugin']['publisher_id']}/{item['plugin']['plugin_id']}": item for item in cards
        }
        self.assertEqual(card_by_plugin["org.waveflow/nmtv"]["plugin"]["dependencies"],
                         [{"name": "xxtea", "version": "5.0.0"}])
        self.assertEqual(card_by_plugin["org.waveflow/sdtv"]["plugin"]["dependencies"],
                         [{"name": name, "version": version} for name, version in DEPENDENCIES["org.waveflow/sdtv"]])
        detail = await market.get_package("official::nmtv-plugin")
        self.assertNotIn("artifact_references", detail)
        self.assertNotIn("dependency_references", detail)

    async def test_restart_recovers_without_market_and_uninstall_is_not_reversed(self):
        first = await self._subsystem()
        await first.startup()
        await first.shutdown()
        self.subsystems.remove(first)

        second = await self._subsystem()
        recovered = await second.startup()
        self.assertEqual({item["plugin"] for item in recovered if item.get("status") == "active"}, BASE_IDENTITIES)
        self.assertEqual(self.network_requests, [])
        self.assertTrue(all(second.service.runtime.registry.route(scheme).health == "healthy" for scheme in BASE_SCHEMES))

        await second.uninstall("org.waveflow/jstv")
        await second.shutdown()
        self.subsystems.remove(second)
        third = await self._subsystem()
        await third.startup()
        self.assertIsNone(await self.db.get_plugin_installation("org.waveflow", "jstv"))
        self.assertEqual(len(await self.db.list_plugin_installations()), len(BASE_IDENTITIES) - 1)

    async def test_python_dependency_cold_cache_warm_restart_and_environment_projection(self):
        first = await self._subsystem()
        await first.startup()
        environments = first.service.python_environments
        from plugin_python_runtime import select_dependency_artifacts
        expected_digests = {
            item["sha256"]
            for identity in ("org.waveflow/nmtv", "org.waveflow/sdtv")
            for item in select_dependency_artifacts(
                first.service.runtime.registry.route(identity.rsplit("/", 1)[1]).manifest.runtime[
                    "dependency_lock"
                ]
            )
        }
        self.assertEqual({path.parent.name for path in environments.cache_objects()}, expected_digests)
        dependency_rows = await self.db.list_plugin_dependency_artifacts()
        self.assertEqual({row["sha256"] for row in dependency_rows}, expected_digests)
        for identity in ("org.waveflow/nmtv", "org.waveflow/sdtv"):
            rows = await self.db.list_plugin_python_environments("org.waveflow", identity.rsplit("/", 1)[1])
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["state"], "active")
            self.assertTrue(Path(rows[0]["path"], "waveflow-environment.json").is_file())

        await first.shutdown()
        self.subsystems.remove(first)
        second = await self._subsystem()
        recovered = await second.startup()
        self.assertEqual({item["plugin"] for item in recovered if item.get("status") == "active"}, BASE_IDENTITIES)
        self.assertEqual({path.parent.name for path in second.service.python_environments.cache_objects()}, expected_digests)
        self.assertEqual(self.network_requests, [])
        for identity in ("org.waveflow/nmtv", "org.waveflow/sdtv"):
            plugin = identity.rsplit("/", 1)[1]
            rows = await self.db.list_plugin_python_environments("org.waveflow", plugin)
            self.assertEqual([row["state"] for row in rows], ["active"])
            self.assertEqual(second.service.runtime.registry.route(plugin).health, "healthy")

    async def test_missing_official_artifact_is_repaired_from_exact_bundled_candidate(self):
        subsystem = await self._subsystem()
        await subsystem.startup()
        await subsystem.set_ownership("fjtv", "plugin", "org.waveflow/fjtv")
        rows = {
            f"{item['publisher_id']}/{item['plugin_id']}": item
            for item in await self.db.list_plugin_installations()
        }
        artifacts = {identity: Path(row["artifact_path"]) for identity, row in rows.items()}
        expected_digests = {identity: row["artifact_sha256"] for identity, row in rows.items()}
        for artifact in artifacts.values():
            artifact.unlink()
        await subsystem.shutdown()
        self.subsystems.remove(subsystem)

        restarted = await self._subsystem()
        recovered = await restarted.startup()
        repaired = {
            item["plugin"]: item for item in recovered if item.get("bootstrap") == "repaired"
        }
        self.assertEqual(set(repaired), BASE_IDENTITIES)
        self.assertTrue(all(item.get("status") == "active" for item in repaired.values()))
        for identity, artifact in artifacts.items():
            plugin_id = identity.rsplit("/", 1)[1]
            row = await self.db.get_plugin_installation("org.waveflow", plugin_id)
            self.assertEqual(row["artifact_sha256"], expected_digests[identity])
            self.assertTrue(artifact.is_file())
            self.assertEqual(hashlib.sha256(artifact.read_bytes()).hexdigest(), expected_digests[identity])
            self.assertEqual(row["lifecycle_state"], "active")
        self.assertEqual(restarted.provider_resolver.mode("fjtv"), "plugin")
        self.assertTrue(all(restarted.provider_resolver.mode(scheme) == ("plugin" if scheme == "fjtv" else "legacy")
                            for scheme in BASE_SCHEMES))
        self.assertEqual(restarted.service.runtime.registry.route("fjtv").health, "healthy")

    async def test_active_runtime_converges_unavailable_when_artifact_disappears(self):
        from plugin_runtime import PluginError

        subsystem = await self._subsystem()
        await subsystem.startup()
        row = await self.db.get_plugin_installation("org.waveflow", "fjtv")
        artifact = Path(row["artifact_path"])
        artifact.unlink()

        recovered = await subsystem.service.recover_enabled()

        result = next(item for item in recovered if item["plugin"] == "org.waveflow/fjtv")
        self.assertEqual(result["status"], "unavailable")
        row = await self.db.get_plugin_installation("org.waveflow", "fjtv")
        self.assertEqual(row["lifecycle_state"], "unavailable")
        self.assertNotIn("org.waveflow/fjtv", subsystem.service._active)
        with self.assertRaises(PluginError):
            subsystem.service.runtime.registry.route("fjtv")

    async def test_store_binding_rejects_unrelated_database_before_orphan_cleanup(self):
        from plugin_production import ProductionPluginSubsystem
        from plugin_runtime import PluginError

        subsystem = await self._subsystem()
        await subsystem.startup()
        row = await self.db.get_plugin_installation("org.waveflow", "fjtv")
        artifact = Path(row["artifact_path"])
        original_digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        await subsystem.shutdown()
        self.subsystems.remove(subsystem)
        # Model an installation created before the DB/store binding marker was
        # introduced.  The upgrade guard must still reject an unrelated empty
        # database before recovery can treat its live paths as an empty set.
        (Path(self.tmp.name) / "plugin-store" / ".waveflow-plugin-store.json").unlink()

        original_db = os.environ["WAVEFLOW_DB_PATH"]
        os.environ["WAVEFLOW_DB_PATH"] = str(Path(self.tmp.name) / "unrelated.db")
        os.environ["WAVEFLOW_OFFICIAL_PLUGIN_BOOTSTRAP"] = "0"
        try:
            await self.db.initialize()
            with self.assertRaises(PluginError) as mismatch:
                unrelated = await ProductionPluginSubsystem.create(
                    root=Path(self.tmp.name) / "plugin-store", http_client=self.client,
                )
                await unrelated.startup()
            self.assertEqual(
                mismatch.exception.code, "ARTIFACT_INVALID", str(mismatch.exception),
            )
        finally:
            os.environ["WAVEFLOW_DB_PATH"] = original_db
            os.environ["WAVEFLOW_OFFICIAL_PLUGIN_BOOTSTRAP"] = "1"

        self.assertTrue(artifact.is_file())
        self.assertEqual(hashlib.sha256(artifact.read_bytes()).hexdigest(), original_digest)

    async def test_corrupt_official_artifact_repair_failure_remains_unavailable(self):
        subsystem = await self._subsystem()
        await subsystem.startup()
        row = await self.db.get_plugin_installation("org.waveflow", "fjtv")
        artifact = Path(row["artifact_path"])
        artifact.write_bytes(b"corrupt")
        await self.db.set_plugin_enabled("org.waveflow", "fjtv", True, lifecycle_state="unavailable")
        with mock.patch.object(subsystem.service.store, "promote", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                await subsystem.repair("org.waveflow/fjtv", importlib.import_module("official_plugin_distribution").bundled_official_packages())
        row = await self.db.get_plugin_installation("org.waveflow", "fjtv")
        self.assertEqual((row["active_version"], row["lifecycle_state"], row["enabled"]), ("1.0.0", "unavailable", 1))
        self.assertEqual(artifact.read_bytes(), b"corrupt")
        self.assertIn(row["artifact_path"], await self.db.list_plugin_artifact_references())

    async def test_repair_without_matching_candidate_is_immutable_conflict(self):
        from plugin_runtime import PluginError

        subsystem = await self._subsystem()
        await subsystem.startup()
        row = await self.db.get_plugin_installation("org.waveflow", "fjtv")
        artifact = Path(row["artifact_path"])
        artifact.unlink()
        await self.db.set_plugin_enabled("org.waveflow", "fjtv", True, lifecycle_state="unavailable")
        with self.assertRaises(PluginError) as missing:
            await subsystem.service.repair_from_packages([], "org.waveflow/fjtv")
        self.assertEqual(missing.exception.code, "RESOURCE_NOT_FOUND")
        row = await self.db.get_plugin_installation("org.waveflow", "fjtv")
        self.assertEqual((row["active_version"], row["lifecycle_state"]), ("1.0.0", "unavailable"))
        self.assertFalse(artifact.exists())

    async def test_recovery_cleanup_protects_installation_reference_without_artifact_row(self):
        subsystem = await self._subsystem()
        await subsystem.startup()
        row = await self.db.get_plugin_installation("org.waveflow", "fjtv")
        artifact = Path(row["artifact_path"])
        orphan = artifact.parent.parent / ("f" * 64) / "artifact"
        orphan.parent.mkdir(parents=True)
        shutil.copyfile(artifact, orphan)
        connection = sqlite3.connect(os.environ["WAVEFLOW_DB_PATH"])
        try:
            connection.execute(
                "DELETE FROM plugin_artifacts WHERE publisher_id=? AND plugin_id=?",
                ("org.waveflow", "fjtv"),
            )
            connection.commit()
        finally:
            connection.close()
        await subsystem.service.recover_enabled()
        self.assertTrue(artifact.is_file())
        self.assertFalse(orphan.exists())

    async def test_repair_promotion_io_failure_keeps_existing_artifact_bytes(self):
        import plugin_market

        subsystem = await self._subsystem()
        await subsystem.startup()
        row = await self.db.get_plugin_installation("org.waveflow", "fjtv")
        target = Path(row["artifact_path"])
        original = target.read_bytes()
        packages = importlib.import_module("official_plugin_distribution").bundled_official_packages()
        prepared_packages, temporary_paths = await subsystem._prepare_packages(packages)
        try:
            candidate = await subsystem.service._trusted_candidate_from_packages(
                prepared_packages, "org.waveflow/fjtv",
            )
            staged, _trust = await asyncio.to_thread(
                subsystem.service.store.stage, candidate, subsystem.service.trust_policy,
            )
            real_replace = plugin_market.os.replace

            def fail_target(source, destination):
                if Path(destination).resolve() == target.resolve():
                    raise OSError("disk full")
                return real_replace(source, destination)

            with mock.patch.object(plugin_market.os, "replace", side_effect=fail_target):
                with self.assertRaises(OSError):
                    await asyncio.to_thread(
                        subsystem.service.store.promote, candidate, staged,
                    )
            self.assertEqual(target.read_bytes(), original)
            shutil.rmtree(staged.parent, ignore_errors=True)
        finally:
            for path in temporary_paths:
                path.unlink(missing_ok=True)

    async def test_ptbtv_hnntv_official_approval_runtime_resolution_and_revoke_recovery(self):
        from official_plugin_distribution import bundled_official_packages
        from plugin_market import current_platform
        from plugin_runtime import PluginError

        if current_platform() != ("macos", "arm64") or (sys.version_info.major, sys.version_info.minor) != (3, 14):
            self.skipTest("OFFICIAL-PLUGIN-DISTRIBUTION-3 acceptance requires macOS arm64 / CPython 3.14")

        self.provider_fixture = True
        packages = bundled_official_packages()
        package_by_identity = {
            f"{item['plugin_manifest']['publisher_id']}/{item['plugin_manifest']['plugin_id']}": item
            for item in packages
        }
        subsystem = await self._subsystem()

        def representative_scheme(plugin_id: str) -> str:
            return "yy" if plugin_id == "streamget-providers" else plugin_id

        for identity in sorted(DIRECT_IDENTITIES):
            with self.subTest(identity=identity):
                with self.assertRaises(PluginError) as pending:
                    await subsystem.install(identity, packages)
                self.assertEqual(pending.exception.code, "PERMISSION_APPROVAL_REQUIRED")
                plugin_id = identity.rsplit("/", 1)[1]
                self.assertIsNone(await self.db.get_plugin_installation("org.waveflow", plugin_id))

                await subsystem.approve_permission(
                    identity, packages, "network.direct", "official-distribution-3-test",
                )
                installed = await subsystem.install(identity, packages)
                self.assertEqual(
                    (installed["trust_state"], installed["source_key"], installed["lifecycle_state"], installed["enabled"]),
                    ("official", "official", "active", 1),
                )
                scheme = representative_scheme(plugin_id)
                instance = subsystem.service.runtime.registry.route(scheme)
                self.assertEqual((instance.health, instance.state.value), ("healthy", "HEALTHY_ACTIVE"))
                row = await self.db.get_plugin_installation("org.waveflow", plugin_id)
                projection = await importlib.import_module("routers.plugins")._plugin_projection(
                    row, runtime=subsystem.service.runtime,
                )
                self.assertEqual(
                    (projection["trust_state"], projection["source_provenance"]["source_key"],
                     projection["runtime_health"]),
                    ("official", "official", "healthy"),
                )
                expected_ownership = [
                    {"scheme": owned_scheme, "mode": "legacy", "plugin": ""}
                    for owned_scheme in (STREAMGET_SCHEMES if plugin_id == "streamget-providers" else {plugin_id})
                ]
                self.assertEqual(sorted(projection["ownership"], key=lambda item: item["scheme"]),
                                 sorted(expected_ownership, key=lambda item: item["scheme"]))
                self.assertEqual(
                    {item["name"] for item in projection["permissions"]["requested"]},
                    ({"network.direct"} if plugin_id == "streamget-providers"
                     else {"network.direct", "network.managed"}),
                )
                self.assertEqual(projection["permissions"]["pending"], [])

                if plugin_id == "ptbtv":
                    descriptor = await subsystem.service.runtime.request(
                        instance, "tv.resolve_stream", {"scheme": "ptbtv", "resource_id": "pt1"},
                    )
                    self.assertEqual(
                        (descriptor["url"], descriptor["transport"], descriptor["ttl_seconds"],
                         descriptor["volatile_url"], descriptor["requires_proxy"]),
                        ("https://media.example/ptbtv-official/live.m3u8", "hls", 180, True, False),
                    )
                    self.assertIn(
                        str(subsystem.service.python_environments.environments_root),
                        descriptor["provider_diagnostics"]["dependency_origin"],
                    )
                    self.assertEqual(self.network_requests[-1].url.host, "www.ptbtv.com")
                elif plugin_id == "hnntv":
                    live = await subsystem.service.runtime.request(
                        instance, "tv.resolve_stream", {"resource_id": "hnws"},
                    )
                    replay = await subsystem.service.runtime.request(
                        instance, "tv.resolve_stream",
                        {"resource_id": "hnws", "query": {"playseek": ["20260811120000-20260811123000"]}},
                    )
                    self.assertEqual(live["url"], "https://media.example/hnntv-official/live.m3u8")
                    self.assertEqual(replay["url"], "https://media.example/hnntv-official/replay.m3u8")
                    self.assertEqual(replay["provider_diagnostics"], {
                        "schedule_id": "official-replay", "program_name": "Official HNNTV fixture",
                    })
                    self.assertEqual(
                        {request.url.host for request in self.network_requests[-2:]},
                        {"www.hnntv.cn", "ps.hnntv.cn"},
                    )
                else:
                    hello = await subsystem.service.runtime.request(instance, "runtime.hello", {})
                    self.assertEqual({item["scheme"] for item in hello["owned_schemes"]}, STREAMGET_SCHEMES)
                    environment_rows = await self.db.list_plugin_python_environments(
                        "org.waveflow", "streamget-providers",
                    )
                    self.assertEqual(len(environment_rows), 1)
                    self.assertEqual(environment_rows[0]["state"], "active")
                    self.assertTrue(Path(environment_rows[0]["path"], "waveflow-environment.json").is_file())
                    self.assertEqual(str(instance.process.command[0]), str(Path(environment_rows[0]["path"]) / "bin/python"))

                self.assertTrue(all(subsystem.provider_resolver.mode(owned_scheme) == "legacy"
                                    for owned_scheme in (STREAMGET_SCHEMES if plugin_id == "streamget-providers"
                                                         else {plugin_id})))
                self.assertEqual(await self.db.list_plugin_scheme_ownership(), [])

        # A fresh production subsystem recovers both signed installations from
        # durable state without consulting the remote Market.
        subsystem = await self._restart(subsystem)
        for identity in sorted(DIRECT_IDENTITIES):
            plugin_id = identity.rsplit("/", 1)[1]
            row = await self.db.get_plugin_installation("org.waveflow", plugin_id)
            self.assertEqual((row["lifecycle_state"], row["trust_state"], row["enabled"]), ("active", "official", 1))
            scheme = "yy" if plugin_id == "streamget-providers" else plugin_id
            self.assertEqual(subsystem.service.runtime.registry.route(scheme).health, "healthy")
            self.assertTrue(all(subsystem.provider_resolver.mode(owned_scheme) == "legacy"
                                for owned_scheme in (STREAMGET_SCHEMES if plugin_id == "streamget-providers"
                                                     else {plugin_id})))

        # Revocation is enforced by the generic lifecycle for each high-risk
        # package, including startup recovery; re-approval is required to
        # activate it again.
        for identity in sorted(DIRECT_IDENTITIES):
            plugin_id = identity.rsplit("/", 1)[1]
            await subsystem.revoke_permission(identity, "network.direct", "official-distribution-3-test")
            with self.assertRaises(PluginError) as revoked:
                await subsystem.service.enable(identity)
            self.assertEqual(revoked.exception.code, "PERMISSION_APPROVAL_REQUIRED")
            row = await self.db.get_plugin_installation("org.waveflow", plugin_id)
            self.assertEqual((row["lifecycle_state"], row["enabled"]), ("unavailable", 1))
            await subsystem.approve_permission(
                identity, packages, "network.direct", "official-distribution-3-test",
            )
            enabled = await subsystem.service.enable(identity)
            self.assertEqual((enabled["lifecycle_state"], enabled["enabled"]), ("active", 1))
            scheme = "yy" if plugin_id == "streamget-providers" else plugin_id
            self.assertEqual(subsystem.service.runtime.registry.route(scheme).health, "healthy")
            self.assertTrue(all(subsystem.provider_resolver.mode(owned_scheme) == "legacy"
                                for owned_scheme in (STREAMGET_SCHEMES if plugin_id == "streamget-providers"
                                                     else {plugin_id})))

        self.assertEqual(
            {f"{row['publisher_id']}/{row['plugin_id']}" for row in await self.db.list_plugin_installations()},
            BASE_IDENTITIES | DIRECT_IDENTITIES,
        )
        self.assertEqual(await self.db.list_plugin_scheme_ownership(), [])
        self.assertEqual(set(package_by_identity), IDENTITIES)

    async def test_corrupt_bundled_catalog_does_not_tear_down_recovered_installations(self):
        from official_plugin_distribution import OFFICIAL_RELEASE_ROOT

        first = await self._subsystem()
        await first.startup()
        await first.shutdown()
        self.subsystems.remove(first)
        copied = Path(self.tmp.name) / "corrupt-distribution"
        shutil.copytree(OFFICIAL_RELEASE_ROOT, copied)
        payload = next((copied / "payloads").glob("*.pyz"))
        payload.write_bytes(payload.read_bytes() + b"tamper")

        second = await self._subsystem()
        second.official_release_root = copied
        results = await second.startup()
        self.assertEqual({item["plugin"] for item in results if item.get("status") == "active"}, BASE_IDENTITIES)
        self.assertIn("org.waveflow/*", {item["plugin"] for item in results if item.get("status") == "unavailable"})
        self.assertTrue(all(second.service.runtime.registry.route(scheme).health == "healthy" for scheme in BASE_SCHEMES))

    async def test_market_offline_discovers_bundled_packages_without_update_provenance(self):
        market = importlib.import_module("market")
        with mock.patch.object(market, "safe_http_fetch", new=mock.AsyncMock(side_effect=market.MarketError("offline", 502))):
            result = await market.refresh_market()
        packages = await market.list_packages({})
        official = [item for item in packages if item.get("package_type") == "plugin_package"]
        self.assertEqual({f"{item['plugin']['publisher_id']}/{item['plugin']['plugin_id']}" for item in official}, IDENTITIES)
        source = next(item for item in result["source_results"] if item["source_key"] == "official")
        self.assertEqual((source["status"], source["usable_for_update"], source["package_count"]),
                         ("bundled", False, len(IDENTITIES)))
        subsystem = await self._subsystem()
        installed = await subsystem.install("org.waveflow/fjtv", market.market_packages_snapshot())
        self.assertEqual((installed["trust_state"], installed["source_key"], installed["active_version"]),
                         ("official", "official", "1.0.0"))
        self.assertEqual(subsystem.provider_resolver.mode("fjtv"), "legacy")

    async def test_missing_target_platform_fails_before_installation_or_ownership(self):
        from official_plugin_distribution import bundled_official_packages
        from plugin_runtime import PluginError

        subsystem = await self._subsystem()
        prepared, temporary_paths = await subsystem._prepare_packages(bundled_official_packages())
        try:
            subsystem.service.os_name = "linux"
            subsystem.service.arch = "arm64"
            with self.assertRaises(PluginError) as unsupported:
                await subsystem.service.install_from_packages(prepared, "org.waveflow/nmtv")
            self.assertEqual(unsupported.exception.code, "PLATFORM_UNSUPPORTED")
            self.assertIsNone(await self.db.get_plugin_installation("org.waveflow", "nmtv"))
            self.assertEqual(await self.db.list_plugin_scheme_ownership(), [])
        finally:
            for path in temporary_paths:
                path.unlink(missing_ok=True)

    async def test_manifest_artifact_signature_and_wrong_key_tamper_are_rejected(self):
        from official_plugin_distribution import OFFICIAL_RELEASE_ROOT, load_bundled_official_market
        from plugin_runtime import PluginError

        with tempfile.TemporaryDirectory() as directory:
            copied = Path(directory) / "distribution"
            shutil.copytree(OFFICIAL_RELEASE_ROOT, copied)
            _market, packages = load_bundled_official_market(copied)
            subsystem = await self._subsystem()
            subsystem.official_release_root = copied

            changed_manifest = json.loads(json.dumps(packages[0]))
            changed_manifest["plugin_manifest"]["display_name"] += " tampered"
            with self.assertRaises(PluginError) as manifest_error:
                await subsystem.install("org.waveflow/fjtv", [changed_manifest])
            self.assertEqual(manifest_error.exception.code, "ARTIFACT_SIGNATURE_INVALID")

            changed_signature = json.loads(json.dumps(packages[0]))
            changed_signature["manifest_signature"]["value"] = base64.b64encode(b"invalid").decode()
            with self.assertRaises(PluginError) as signature_error:
                await subsystem.install("org.waveflow/fjtv", [changed_signature])
            self.assertEqual(signature_error.exception.code, "ARTIFACT_SIGNATURE_INVALID")

            artifact_path = Path(packages[0]["artifact_references"][0]["_bundled_path"])
            artifact_path.write_bytes(artifact_path.read_bytes() + b"tamper")
            with self.assertRaises(PluginError) as artifact_error:
                load_bundled_official_market(copied)
            self.assertEqual(artifact_error.exception.code, "ARTIFACT_INTEGRITY_FAILED")

            dependency_copy = Path(directory) / "dependency-distribution"
            shutil.copytree(OFFICIAL_RELEASE_ROOT, dependency_copy)
            dependency_package = next(
                item for item in load_bundled_official_market(dependency_copy)[1]
                if item["plugin_manifest"]["plugin_id"] == "nmtv"
            )
            dependency_path = Path(dependency_package["dependency_references"][0]["_bundled_path"])
            dependency_path.write_bytes(dependency_path.read_bytes() + b"tamper")
            with self.assertRaises(PluginError) as dependency_error:
                load_bundled_official_market(dependency_copy)
            self.assertEqual(dependency_error.exception.code, "DEPENDENCY_ARTIFACT_INTEGRITY_FAILED")

        test_key = Ed25519PrivateKey.generate()
        test_public = base64.b64encode(test_key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw,
        )).decode()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key_path = root / "test-key.pem"
            key_path.write_bytes(test_key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
            ))
            trust_path = root / "test-trust.json"
            trust_path.write_text(json.dumps({"schema_version": 1, "publishers": [{
                "publisher_id": "org.waveflow", "keys": [{
                    "key_id": "test-key", "public_key": test_public, "enabled": True,
                }],
            }]}))
            from build_official_plugins import build_release
            from official_plugin_distribution import load_bundled_official_market
            build_release(signing_key=key_path, key_id="test-key", output=root / "release",
                          trust_path=trust_path, plugin_source_root=plugins_root())
            _market, test_packages = load_bundled_official_market(root / "release")
            await self.db.upsert_plugin_publisher_trust(
                publisher_id="org.waveflow", key_id="test-key", public_key=test_public,
                trust_level="official", enabled=True, description="must not override distribution trust",
            )
            subsystem = await self._subsystem()
            subsystem.official_release_root = root / "release"
            with self.assertRaises(PluginError) as wrong_key:
                await subsystem.install("org.waveflow/fjtv", test_packages)
            self.assertEqual(wrong_key.exception.code, "PLUGIN_UNTRUSTED")

    def test_official_catalog_rejects_incomplete_and_duplicate_identity_metadata(self):
        from official_plugin_distribution import OFFICIAL_RELEASE_ROOT, load_bundled_official_market
        from plugin_runtime import PluginError

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "distribution"
            shutil.copytree(OFFICIAL_RELEASE_ROOT, root)
            market_path = root / "market.json"
            market = json.loads(market_path.read_text())
            market["packages"][0]["artifact_references"] = []
            market_path.write_text(json.dumps(market))
            with self.assertRaises(PluginError) as incomplete:
                load_bundled_official_market(root)
            self.assertEqual(incomplete.exception.code, "ARTIFACT_INVALID")

            shutil.rmtree(root)
            shutil.copytree(OFFICIAL_RELEASE_ROOT, root)
            market_path = root / "market.json"
            market = json.loads(market_path.read_text())
            market["packages"][1]["id"] = market["packages"][0]["id"]
            market_path.write_text(json.dumps(market))
            with self.assertRaises(PluginError) as duplicate:
                load_bundled_official_market(root)
            self.assertEqual(duplicate.exception.code, "ARTIFACT_INVALID")

    def test_official_trust_metadata_rejects_duplicate_keys(self):
        from official_plugin_distribution import load_official_trust_rows
        from plugin_runtime import PluginError

        with tempfile.TemporaryDirectory() as directory:
            trust_path = Path(directory) / "trust.json"
            trust = json.loads((Path(__file__).parents[1] / "official_plugins" / "publisher-trust.json").read_text())
            trust["publishers"][0]["keys"].append(dict(trust["publishers"][0]["keys"][0]))
            trust_path.write_text(json.dumps(trust))
            with self.assertRaises(PluginError) as duplicate:
                load_official_trust_rows(trust_path)
            self.assertEqual(duplicate.exception.code, "PLUGIN_UNTRUSTED")

    async def test_signed_update_and_failed_provenance_keep_previous_active(self):
        from official_plugin_distribution import OFFICIAL_DISTRIBUTION_ROOT
        from plugin_capabilities import CapabilityGateway
        from plugin_market import PluginArtifactStore, PluginMarketService, current_platform, manifest_signature_payload
        from plugin_production import ProductionPluginSubsystem, ProductionTrustPolicy
        from plugin_runtime import PluginError, PluginRuntime, validate_manifest
        from provider_resolver import ProviderResolver
        from waveflow_plugin_cli import build_sdk_artifact

        key = Ed25519PrivateKey.generate()
        public = base64.b64encode(key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw,
        )).decode()
        policy = ProductionTrustPolicy([{
            "publisher_id": "org.waveflow", "key_id": "fixture-release-key", "public_key": public,
            "trust_level": "official", "enabled": 1, "require_manifest_signature": True,
        }])
        root = Path(self.tmp.name) / "signed-update"
        artifact = root / "jstv.pyz"
        artifact.parent.mkdir(parents=True)
        build_sdk_artifact(plugin_source("jstv") / "plugin.py", artifact)
        payload = artifact.read_bytes()
        os_name, arch = current_platform()

        def package(version: str, *, source: Path = artifact) -> dict:
            source_payload = source.read_bytes()
            source_digest = hashlib.sha256(source_payload).hexdigest()
            data = json.loads((plugin_source("jstv") / "manifest.json").read_text())
            data["version"] = version
            data["artifacts"] = [{
                "os": os_name, "arch": arch, "runtime": "python", "entrypoint": "jstv.pyz",
                "sha256": source_digest, "size_bytes": len(source_payload), "signature": {
                    "algorithm": "ed25519", "key_id": "fixture-release-key",
                    "value": base64.b64encode(key.sign(source_payload)).decode(),
                },
            }]
            manifest = validate_manifest(data)
            return {
                "schema_version": 1, "id": "official::jstv-plugin", "kind": "plugin_package",
                "package_type": "plugin_package", "version": version, "plugin_manifest": data,
                "manifest_signature": {
                    "algorithm": "ed25519", "key_id": "fixture-release-key",
                    "value": base64.b64encode(key.sign(manifest_signature_payload(manifest))).decode(),
                },
                "artifact_references": [{"sha256": source_digest, "local_path": str(source)}],
                "market_source": {"source_key": "official"},
            }

        runtime = PluginRuntime()
        service = PluginMarketService(
            runtime=runtime,
            store=PluginArtifactStore(root / "store", allowed_local_roots=[root]),
            trust_policy=policy, os_name=os_name, arch=arch,
        )
        subsystem = ProductionPluginSubsystem(
            service=service, trust_policy=policy, download_root=root / "downloads",
            http_client=self.client, provider_resolver=ProviderResolver(runtime=runtime),
            capability_gateway=CapabilityGateway(client=self.client),
        )
        try:
            await service.install_from_packages([package("1.0.0")], "org.waveflow/jstv")
            await subsystem.set_ownership("jstv", "plugin", "org.waveflow/jstv")
            await service.install_from_packages([package("1.1.0")], "org.waveflow/jstv")
            self.assertEqual(runtime.registry.route("jstv").manifest.version, "1.1.0")
            owner = next(item for item in await self.db.list_plugin_scheme_ownership()
                         if item["scheme"] == "jstv")
            self.assertEqual((owner["mode"], owner["plugin_identity"]),
                             ("plugin", "org.waveflow/jstv"))
            alternate = root / "jstv-alternate.pyz"
            alternate.write_bytes(payload + b"different-signed-artifact")
            with self.assertRaises(PluginError) as immutable_conflict:
                await service.install_from_packages(
                    [package("1.1.0", source=alternate)], "org.waveflow/jstv",
                )
            self.assertEqual(immutable_conflict.exception.code, "PLUGIN_INCOMPATIBLE")
            self.assertEqual(runtime.registry.route("jstv").manifest.version, "1.1.0")
            invalid = package("1.2.0")
            invalid["manifest_signature"] = package("1.1.0")["manifest_signature"]
            with self.assertRaises(PluginError) as rejected:
                await service.install_from_packages([invalid], "org.waveflow/jstv")
            self.assertEqual(rejected.exception.code, "ARTIFACT_SIGNATURE_INVALID")
            self.assertEqual(runtime.registry.route("jstv").manifest.version, "1.1.0")
            owner = next(item for item in await self.db.list_plugin_scheme_ownership()
                         if item["scheme"] == "jstv")
            self.assertEqual((owner["mode"], owner["plugin_identity"]),
                             ("plugin", "org.waveflow/jstv"))
        finally:
            await runtime.shutdown()


if __name__ == "__main__":
    unittest.main()
