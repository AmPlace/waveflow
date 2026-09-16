from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sys
import sysconfig
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable

from packaging import tags

from plugin_runtime import PluginError, PluginManifest
from plugin_runtime.manifest import _range_allows


MAX_DEPENDENCY_ARTIFACT_BYTES = 64 * 1024 * 1024
INSTALL_TIMEOUT_SECONDS = 120.0


def python_runtime_identity(executable: str = sys.executable) -> str:
    return f"cpython-{sys.version_info.major}.{sys.version_info.minor}-{sys.implementation.cache_tag}-{sysconfig.get_platform()}"


def dependency_lock_digest(lock: dict[str, Any]) -> str:
    encoded = json.dumps(lock, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _artifact_tag(item: dict[str, Any]) -> tags.Tag:
    return tags.Tag(item["python_tag"], item["abi_tag"], item["platform_tag"])


def validate_python_runtime(manifest: PluginManifest) -> tuple[dict[str, Any], str]:
    runtime = manifest.runtime
    if runtime.get("type") != "python":
        raise PluginError("PLUGIN_INCOMPATIBLE", "Plugin is not a managed Python runtime", category="runtime")
    current = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    if not _range_allows(current, runtime["python_version_range"]):
        raise PluginError("PYTHON_RUNTIME_UNSUPPORTED", "Python runtime version is unsupported", category="runtime")
    select_dependency_artifacts(runtime["dependency_lock"])
    return runtime["dependency_lock"], dependency_lock_digest(runtime["dependency_lock"])


def select_dependency_artifacts(lock: dict[str, Any], *, supported_tags: Iterable[tags.Tag] | None = None,
                                ) -> tuple[dict[str, Any], ...]:
    """Select one exact wheel candidate per locked package for a runtime.

    Production callers use the current interpreter's tags.  Tests and release
    tooling may provide a deterministic target tag set to validate a Docker
    or other published target without pretending that target is executing on
    the current host.
    """
    supported = set(tags.sys_tags() if supported_tags is None else supported_tags)
    by_package: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for item in lock["artifacts"]:
        by_package.setdefault((item["name"], item["version"]), []).append(item)
    selected = []
    for candidates in by_package.values():
        compatible = [item for item in candidates if _artifact_tag(item) in supported]
        if len(compatible) != 1:
            raise PluginError("DEPENDENCY_PLATFORM_UNSUPPORTED",
                              "Dependency lock must select exactly one compatible wheel per package",
                              category="dependency")
        selected.append(compatible[0])
    return tuple(selected)


@dataclass(frozen=True)
class PreparedPythonEnvironment:
    path: Path
    python: Path
    lock_digest: str
    runtime_identity: str
    dependencies: tuple[dict[str, Any], ...]


class PythonEnvironmentManager:
    def __init__(self, root: str | Path, *, python_executable: str = sys.executable):
        self.root = Path(root).resolve()
        self.python_executable = str(Path(python_executable).resolve())
        self.dependencies_root = self.root / "dependencies" / "sha256"
        self.environments_root = self.root / "environments"
        self.staging_root = self.root / "staging"
        for path in (self.dependencies_root, self.environments_root, self.staging_root):
            path.mkdir(parents=True, exist_ok=True)
        self._artifact_locks: dict[str, asyncio.Lock] = {}

    def environment_path(self, manifest: PluginManifest, lock_digest: str) -> Path:
        runtime_key = hashlib.sha256(python_runtime_identity(self.python_executable).encode()).hexdigest()[:16]
        return (self.environments_root / manifest.publisher_id / manifest.plugin_id /
                manifest.version / runtime_key / lock_digest)

    async def prepare(
        self, manifest: PluginManifest, references: dict[str, Path],
        *, fetch: Callable[[dict[str, Any], Path], Awaitable[Path]] | None = None,
    ) -> PreparedPythonEnvironment:
        lock, lock_digest = validate_python_runtime(manifest)
        wheels = []
        selected = select_dependency_artifacts(lock)
        for item in selected:
            wheels.append(await self._cache_artifact(item, references.get(item["sha256"]), fetch=fetch))
        target = self.environment_path(manifest, lock_digest)
        if target.exists():
            return self.verify(manifest, target)
        staging = self.staging_root / f"env-{uuid.uuid4().hex}"
        try:
            # Keep the bundled Desktop runtime immutable.  Without -B, the
            # sidecar can write stdlib bytecode into the signed app bundle
            # while creating a Plugin environment, invalidating its sealed
            # resources before the first Plugin starts.
            await self._run((self.python_executable, "-B", "-m", "venv", str(staging)))
            python = self._env_python(staging)
            if wheels:
                await self._run((str(python), "-B", "-m", "pip", "install", "--no-index", "--no-deps",
                                 "--disable-pip-version-check", *map(str, wheels)))
            marker = {"plugin": manifest.identity, "version": manifest.version,
                      "runtime_identity": python_runtime_identity(self.python_executable),
                      "lock_digest": lock_digest, "dependencies": selected}
            (staging / "waveflow-environment.json").write_text(
                json.dumps(marker, sort_keys=True, separators=(",", ":")), encoding="utf-8")
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.replace(staging, target)
            except OSError:
                if not target.exists():
                    raise
            return self.verify(manifest, target)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise

    async def _cache_artifact(self, item: dict[str, Any], local: Path | None,
                              *, fetch: Callable[[dict[str, Any], Path], Awaitable[Path]] | None) -> Path:
        digest = item["sha256"]
        directory = self.dependencies_root / digest[:2] / digest
        target = directory / item["filename"]
        lock = self._artifact_locks.setdefault(digest, asyncio.Lock())
        async with lock:
            metadata_path = directory / "metadata.json"
            expected_metadata = {key: item[key] for key in (
                "name", "version", "filename", "sha256", "size_bytes", "python_tag", "abi_tag", "platform_tag")}
            if metadata_path.is_file():
                try:
                    existing_metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    raise PluginError("DEPENDENCY_ARTIFACT_INTEGRITY_FAILED", "Dependency cache metadata is invalid", category="dependency") from exc
                if existing_metadata != expected_metadata:
                    raise PluginError("DEPENDENCY_ARTIFACT_INTEGRITY_FAILED", "Dependency digest metadata conflicts", category="dependency")
            if target.is_file():
                self._verify_file(target, item)
                return target
            target.parent.mkdir(parents=True, exist_ok=True)
            temp = target.parent / f"staging-{uuid.uuid4().hex}.whl"
            try:
                if local is not None:
                    await asyncio.to_thread(shutil.copyfile, local, temp)
                elif fetch is not None:
                    downloaded = await fetch(item, target.parent)
                    await asyncio.to_thread(shutil.move, downloaded, temp)
                else:
                    raise PluginError("DEPENDENCY_ARTIFACT_NOT_FOUND", "Dependency artifact is unavailable", category="dependency")
                self._verify_file(temp, item)
                os.replace(temp, target)
                metadata_path.write_text(json.dumps(expected_metadata, sort_keys=True), encoding="utf-8")
                return target
            finally:
                temp.unlink(missing_ok=True)

    @staticmethod
    def _verify_file(path: Path, item: dict[str, Any]) -> None:
        if not path.is_file() or path.stat().st_size != item["size_bytes"]:
            raise PluginError("DEPENDENCY_ARTIFACT_INTEGRITY_FAILED", "Dependency artifact integrity check failed", category="dependency")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != item["sha256"]:
            raise PluginError("DEPENDENCY_ARTIFACT_INTEGRITY_FAILED", "Dependency artifact integrity check failed", category="dependency")

    def verify(self, manifest: PluginManifest, path: Path) -> PreparedPythonEnvironment:
        lock, lock_digest = validate_python_runtime(manifest)
        marker_path = path / "waveflow-environment.json"
        python = self._env_python(path)
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PluginError("PLUGIN_ENVIRONMENT_INVALID", "Plugin environment metadata is invalid", category="runtime") from exc
        expected = (manifest.identity, manifest.version, python_runtime_identity(self.python_executable), lock_digest)
        actual = (marker.get("plugin"), marker.get("version"), marker.get("runtime_identity"), marker.get("lock_digest"))
        if actual != expected or not python.is_file():
            raise PluginError("PLUGIN_ENVIRONMENT_INVALID", "Plugin environment identity is invalid", category="runtime")
        dependencies = marker.get("dependencies")
        if not isinstance(dependencies, list):
            raise PluginError("PLUGIN_ENVIRONMENT_INVALID", "Plugin environment dependencies are invalid", category="runtime")
        selected = select_dependency_artifacts(lock)
        if dependencies != list(selected):
            raise PluginError("PLUGIN_ENVIRONMENT_INVALID", "Plugin environment dependency plan is invalid", category="runtime")
        return PreparedPythonEnvironment(path, python, lock_digest, expected[2], selected)

    async def rebuild(self, manifest: PluginManifest, references: dict[str, Path], **kwargs) -> PreparedPythonEnvironment:
        _lock, digest = validate_python_runtime(manifest)
        shutil.rmtree(self.environment_path(manifest, digest), ignore_errors=True)
        return await self.prepare(manifest, references, **kwargs)

    def remove_environment(self, path: str | Path) -> None:
        resolved = Path(path).resolve()
        if resolved.is_relative_to(self.environments_root):
            shutil.rmtree(resolved, ignore_errors=True)

    def cache_objects(self) -> list[Path]:
        return list(self.dependencies_root.glob("*/*/*.whl"))

    def cleanup_unreferenced_cache(self, live_digests: set[str]) -> int:
        removed = 0
        for directory in self.dependencies_root.glob("*/*"):
            if directory.is_dir() and directory.name not in live_digests:
                shutil.rmtree(directory, ignore_errors=True)
                removed += 1
        return removed

    async def _run(self, argv: tuple[str, ...]) -> None:
        environment = os.environ.copy()
        # venv bootstraps pip in a child interpreter which does not inherit
        # the parent's -B flag.  Propagate the policy explicitly so a frozen
        # Desktop runtime never receives generated bytecode in its signed
        # application bundle.
        if self._runtime_is_controlled_sidecar():
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
        try:
            process = await asyncio.create_subprocess_exec(
                *argv, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
                env=environment,
            )
            _out, stderr = await asyncio.wait_for(process.communicate(), INSTALL_TIMEOUT_SECONDS)
        except asyncio.TimeoutError as exc:
            if 'process' in locals() and process.returncode is None:
                process.kill()
                await process.wait()
            raise PluginError("DEPENDENCY_INSTALL_FAILED", "Plugin environment installation timed out", category="dependency") from exc
        if process.returncode:
            raise PluginError("DEPENDENCY_INSTALL_FAILED", "Plugin environment installation failed", category="dependency",
                              internal_diagnostics={"stderr": stderr.decode(errors="replace")[:2048]})

    def _runtime_is_controlled_sidecar(self) -> bool:
        """Whether the interpreter lives in a bundled ``python-runtime`` tree."""
        try:
            executable = Path(self.python_executable).resolve()
            return any(parent.name == "python-runtime" for parent in executable.parents)
        except OSError:
            return False

    @staticmethod
    def _env_python(path: Path) -> Path:
        return path / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
