from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from plugin_runtime import PluginError


SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
PACKAGE_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$")


@dataclass(frozen=True)
class DependencyArtifact:
    package: str
    version: str
    platform: str
    python_abi: str
    sha256: str
    size_bytes: int

    @classmethod
    def parse(cls, value: Any) -> "DependencyArtifact":
        if not isinstance(value, dict) or set(value) != {
            "package", "version", "platform", "python_abi", "sha256", "size_bytes"
        }:
            raise _invalid("Invalid dependency artifact metadata")
        if (not isinstance(value["package"], str) or not PACKAGE_RE.fullmatch(value["package"])
                or not all(isinstance(value[key], str) and value[key] for key in ("version", "platform", "python_abi"))
                or not isinstance(value["sha256"], str) or not SHA256_RE.fullmatch(value["sha256"])
                or not isinstance(value["size_bytes"], int) or isinstance(value["size_bytes"], bool)
                or value["size_bytes"] < 1):
            raise _invalid("Invalid dependency artifact metadata")
        return cls(**value)

    @property
    def cache_key(self) -> str:
        metadata = json.dumps({
            "package": self.package.lower().replace("_", "-"), "version": self.version,
            "platform": self.platform, "python_abi": self.python_abi, "sha256": self.sha256,
        }, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(metadata).hexdigest()


@dataclass(frozen=True)
class PythonRuntimeSpec:
    python_version: str
    entrypoint: str
    lock_sha256: str
    artifacts: tuple[DependencyArtifact, ...]

    @classmethod
    def parse(cls, value: Any) -> "PythonRuntimeSpec":
        if not isinstance(value, dict) or set(value) != {"python_version", "entrypoint", "lock_sha256", "artifacts"}:
            raise _invalid("Invalid Python Plugin runtime metadata")
        artifacts = value["artifacts"]
        if (not isinstance(value["python_version"], str) or not value["python_version"]
                or not isinstance(value["entrypoint"], str) or not value["entrypoint"]
                or value["entrypoint"].startswith(("/", ".."))
                or not isinstance(value["lock_sha256"], str) or not SHA256_RE.fullmatch(value["lock_sha256"])
                or not isinstance(artifacts, list)):
            raise _invalid("Invalid Python Plugin runtime metadata")
        return cls(value["python_version"], value["entrypoint"], value["lock_sha256"],
                   tuple(DependencyArtifact.parse(item) for item in artifacts))


@dataclass(frozen=True)
class PluginEnvironmentPlan:
    plugin_identity: str
    lock_sha256: str
    environment_root: Path

    @classmethod
    def create(cls, plugin_identity: str, lock_sha256: str, roots: str | Path) -> "PluginEnvironmentPlan":
        if (plugin_identity.count("/") != 1 or not SHA256_RE.fullmatch(lock_sha256)
                or any(part in {"", ".", ".."} for part in plugin_identity.split("/"))):
            raise _invalid("Invalid Plugin environment ownership")
        namespace = plugin_identity.replace("/", ".")
        return cls(plugin_identity, lock_sha256, Path(roots).resolve() / namespace / lock_sha256)


class DependencyArtifactCache:
    """Core-owned immutable blob cache; it is not a shared Python environment."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    def path_for(self, artifact: DependencyArtifact) -> Path:
        return self.root / artifact.sha256[:2] / artifact.sha256

    def put_verified(self, source: str | Path, artifact: DependencyArtifact) -> Path:
        source_path = Path(source)
        digest = hashlib.sha256()
        size = 0
        with source_path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                size += len(chunk)
                digest.update(chunk)
        if size != artifact.size_bytes or digest.hexdigest() != artifact.sha256:
            raise PluginError("ARTIFACT_INTEGRITY_FAILED", "Dependency artifact integrity check failed",
                              category="artifact")
        target = self.path_for(artifact)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            return target
        fd, temp_name = tempfile.mkstemp(prefix="dependency-", dir=target.parent)
        os.close(fd)
        temp = Path(temp_name)
        try:
            shutil.copyfile(source_path, temp)
            try:
                os.replace(temp, target)
            except OSError:
                if not target.exists():
                    raise
            return target
        finally:
            temp.unlink(missing_ok=True)


def _invalid(message: str) -> PluginError:
    return PluginError("INVALID_PLUGIN_RESPONSE", message, category="dependency")
