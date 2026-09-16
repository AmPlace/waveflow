from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import sysconfig
import tempfile
import unittest
import zipfile
from email.parser import BytesParser
from pathlib import Path

from packaging.utils import parse_wheel_filename
from packaging.tags import Tag


ARTIFACT_ROOT = Path(__file__).parents[1] / "official_plugins" / "dependency_artifacts" / "pyexecjs"
SOURCE = ARTIFACT_ROOT / "source" / "PyExecJS-1.5.1.tar.gz"
WHEEL = ARTIFACT_ROOT / "pyexecjs-1.5.1-py3-none-any.whl"
SIX_WHEEL = ARTIFACT_ROOT / "six-1.17.0-py2.py3-none-any.whl"
PROVENANCE = ARTIFACT_ROOT / "provenance.json"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class OfficialDependencyArtifactTest(unittest.TestCase):
    def test_source_and_wheel_provenance_are_immutable_and_consistent(self):
        metadata = json.loads(PROVENANCE.read_text(encoding="utf-8"))
        source = metadata["source"]
        artifact = metadata["artifact"]
        self.assertEqual(SOURCE.name, source["filename"])
        self.assertEqual(_sha256(SOURCE), source["sha256"])
        self.assertEqual(SOURCE.stat().st_size, source["size_bytes"])
        self.assertEqual(WHEEL.name, artifact["filename"])
        self.assertEqual(_sha256(WHEEL), artifact["sha256"])
        self.assertEqual(WHEEL.stat().st_size, artifact["size_bytes"])
        self.assertEqual(metadata["build"]["network_during_build"], False)

        name, version, _build, tags = parse_wheel_filename(WHEEL.name)
        self.assertEqual((str(name), str(version)), ("pyexecjs", "1.5.1"))
        self.assertEqual({str(tag) for tag in tags}, {"py3-none-any"})
        self.assertEqual(_sha256(SIX_WHEEL), metadata["runtime_dependencies"][0]["sha256"])

        with zipfile.ZipFile(WHEEL) as archive:
            wheel_metadata = next(
                archive.read(path)
                for path in archive.namelist()
                if path.endswith(".dist-info/METADATA")
            )
            wheel_info = next(
                archive.read(path)
                for path in archive.namelist()
                if path.endswith(".dist-info/WHEEL")
            ).decode("utf-8")
        parsed = BytesParser().parsebytes(wheel_metadata)
        self.assertEqual(parsed["Name"], "PyExecJS")
        self.assertEqual(parsed["Version"], "1.5.1")
        self.assertIn("six>=1.10.0", parsed.get_all("Requires-Dist"))
        self.assertIn("Root-Is-Purelib: true", wheel_info)
        self.assertIn("Tag: py3-none-any", wheel_info)

    def test_py3_none_any_candidate_is_selected_for_cp314_and_cp311(self):
        from plugin_python_runtime import select_dependency_artifacts

        lock_item = {
            "name": "pyexecjs", "version": "1.5.1", "filename": WHEEL.name,
            "url": "https://official.waveflow.invalid/dependencies/pyexecjs/" + WHEEL.name,
            "sha256": _sha256(WHEEL), "size_bytes": WHEEL.stat().st_size,
            "python_tag": "py3", "abi_tag": "none", "platform_tag": "any",
        }
        lock = {"lock_version": 1, "artifacts": [lock_item]}
        for target in (
            {
                Tag("cp314", "cp314", "macosx_11_0_arm64"),
                Tag("py3", "none", "any"),
            },
            {
                Tag("cp311", "cp311", "manylinux2014_x86_64"),
                Tag("py3", "none", "any"),
            },
        ):
            self.assertEqual(select_dependency_artifacts(lock, supported_tags=target), (lock_item,))

    def test_clean_isolated_environment_imports_execjs_from_final_wheel(self):
        with tempfile.TemporaryDirectory(prefix="waveflow-pyexecjs-test-") as directory:
            root = Path(directory)
            environment = root / "venv"
            subprocess.run([sys.executable, "-m", "venv", str(environment)], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=60)
            python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            subprocess.run([
                str(python), "-m", "pip", "install", "--no-index", "--no-deps", str(WHEEL), str(SIX_WHEEL),
            ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=60)
            script = """
import importlib.metadata as metadata
import pathlib
import sys
import sysconfig
import execjs

prefix = pathlib.Path(sys.prefix).resolve()
stdlib_root = pathlib.Path(sysconfig.get_paths()['stdlib']).resolve().parent
assert sys.prefix != sys.base_prefix
assert metadata.version('PyExecJS') == '1.5.1'
assert pathlib.Path(execjs.__file__).resolve().is_relative_to(prefix)
assert all(path.is_relative_to(prefix) or path.is_relative_to(stdlib_root)
           for path in (pathlib.Path(value).resolve() for value in sys.path if value))
"""
            isolated_env = {"PATH": "/usr/bin:/bin", "PYTHONNOUSERSITE": "1"}
            subprocess.run([str(python), "-I", "-c", script], check=True, env=isolated_env,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)


if __name__ == "__main__":
    unittest.main()
