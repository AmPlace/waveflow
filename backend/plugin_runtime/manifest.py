from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from packaging.version import InvalidVersion, Version

from .errors import PluginError


SEMVER_RE = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$")
RANGE_PART_RE = re.compile(r"^(<=|>=|<|>|=)(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-([0-9A-Za-z.-]+))?$")
IDENTITY_RE = re.compile(r"^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$")
PLUGIN_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?$")
# Canonical scheme grammar: 2-32 characters, lowercase alphanumeric plus
# ``+`` ``.`` ``-``.  The Python SDK enforces the same pattern when a Plugin
# registers a provider; the SDK ships standalone inside the ``.pyz`` artifact and
# cannot import this module, so the pattern is duplicated on purpose and a test
# asserts both copies stay identical.  Divergence here is what made a manifest
# pass ``validate`` and then crash at runtime.
SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]{1,31}$")
SCHEME_FORMAT = "2-32 chars, [a-z] followed by [a-z0-9+.-]"
API_VERSION = "1.0"
SUPPORTED_CONTRACTS = frozenset({"tv_provider", "tv_visual_provider", "radio_provider", "channel_catalog"})
SUPPORTED_PERMISSIONS = frozenset({
    "network", "secrets", "cache", "state", "filesystem", "runtime", "subprocess", "crypto", "media",
})
SUPPORTED_OS = frozenset({"linux", "macos", "windows"})
SUPPORTED_ARCH = frozenset({"x86_64", "arm64"})


def _version_tuple(value: str) -> tuple[int, int, int, str]:
    match = SEMVER_RE.fullmatch(value)
    if not match:
        raise ValueError(value)
    return int(match[1]), int(match[2]), int(match[3]), match[4] or ""


def _range_allows(version: str, expression: str) -> bool:
    current = _version_tuple(version)[:3]
    parts = expression.split()
    if not parts:
        return False
    for part in parts:
        match = RANGE_PART_RE.fullmatch(part)
        if not match:
            return False
        expected = (int(match[2]), int(match[3]), int(match[4]))
        op = match[1]
        if not {"<": current < expected, "<=": current <= expected, ">": current > expected,
                ">=": current >= expected, "=": current == expected}[op]:
            return False
    return True


@dataclass(frozen=True)
class ProviderContract:
    contract: str
    contract_version: str
    features: frozenset[str]
    schemes: frozenset[str] = frozenset()


@dataclass(frozen=True)
class PluginManifest:
    raw: dict[str, Any]
    publisher_id: str
    plugin_id: str
    display_name: str
    version: str
    plugin_api_version: str
    core_version_range: str
    provider_contracts: tuple[ProviderContract, ...]
    owned_schemes: tuple[tuple[str, str], ...]
    capabilities: frozenset[str]
    permissions: dict[str, Any]
    runtime: dict[str, Any]
    artifacts: tuple[dict[str, Any], ...]
    dependencies: tuple[dict[str, Any], ...]
    state_schema_version: int

    @property
    def identity(self) -> str:
        return f"{self.publisher_id}/{self.plugin_id}"


def _malformed(message: str) -> PluginError:
    return PluginError("INVALID_PLUGIN_RESPONSE", message, category="manifest")


def validate_manifest(data: Any, *, core_version: str = "0.1.0") -> PluginManifest:
    if not isinstance(data, dict):
        raise _malformed("Plugin manifest must be an object")
    required = {
        "manifest_version", "publisher_id", "plugin_id", "display_name", "version", "plugin_api_version",
        "core_version_range", "provider_contracts", "owned_schemes", "capabilities", "permissions", "runtime",
        "artifacts", "dependencies", "state_schema_version",
    }
    missing = required - data.keys()
    if missing:
        raise _malformed(f"Plugin manifest is missing required fields: {', '.join(sorted(missing))}")
    if data["manifest_version"] != 1:
        raise PluginError("PLUGIN_INCOMPATIBLE", "Unsupported plugin manifest version", category="compatibility")
    publisher = data["publisher_id"]
    plugin_id = data["plugin_id"]
    if not isinstance(publisher, str) or not IDENTITY_RE.fullmatch(publisher) or len(publisher) > 128:
        raise _malformed("Invalid publisher_id")
    if not isinstance(plugin_id, str) or not PLUGIN_ID_RE.fullmatch(plugin_id) or len(plugin_id) > 128:
        raise _malformed("Invalid plugin_id")
    if not isinstance(data["display_name"], str) or not 1 <= len(data["display_name"]) <= 200:
        raise _malformed("Invalid display_name")
    if not isinstance(data["version"], str) or not SEMVER_RE.fullmatch(data["version"]):
        raise _malformed(f"Invalid plugin SemVer (expected X.Y.Z): {data['version']!r}")
    if data["plugin_api_version"] != API_VERSION:
        raise PluginError("PLUGIN_INCOMPATIBLE", "Incompatible plugin API version", category="compatibility")
    expression = data["core_version_range"]
    try:
        allowed = isinstance(expression, str) and _range_allows(core_version, expression)
    except ValueError:
        allowed = False
    if not isinstance(expression, str) or not all(RANGE_PART_RE.fullmatch(p) for p in expression.split()):
        raise _malformed(
            f"Invalid Core version range: {expression!r} "
            "(expected space-separated parts like '>=0.1.0 <1.0.0')",
        )
    if not allowed:
        raise PluginError("PLUGIN_INCOMPATIBLE", "Plugin does not support this WaveFlow Core version", category="compatibility")

    contracts_raw = data["provider_contracts"]
    if not isinstance(contracts_raw, list) or not contracts_raw:
        raise _malformed("provider_contracts must not be empty")
    contracts: list[ProviderContract] = []
    contract_names: set[str] = set()
    visual_contract_schemes: list[set[str]] = []
    for item in contracts_raw:
        if not isinstance(item, dict) or item.get("contract") not in SUPPORTED_CONTRACTS or item.get("contract_version") != "1.0":
            raise PluginError("PLUGIN_INCOMPATIBLE", "Unsupported provider contract", category="compatibility")
        features = item.get("features")
        if not isinstance(features, list) or any(not isinstance(v, str) or not v for v in features):
            raise _malformed("Invalid provider contract features")
        if item["contract"] == "channel_catalog" and "discover" not in features:
            raise _malformed("Channel catalog contract must declare discover")
        if item["contract"] == "tv_visual_provider":
            if set(item) != {"contract", "contract_version", "features", "schemes"} or "metadata" not in features:
                raise _malformed("TV visual contract must declare metadata and schemes")
            schemes_value = item.get("schemes")
            if (not isinstance(schemes_value, list) or not schemes_value
                    or any(not isinstance(value, str) or not SCHEME_RE.fullmatch(value) for value in schemes_value)
                    or len(set(schemes_value)) != len(schemes_value)):
                raise _malformed("Invalid TV visual contract schemes")
            visual_contract_schemes.append(set(schemes_value))
        elif set(item) - {"contract", "contract_version", "features"}:
            raise _malformed("Provider contract contains unsupported fields")
        if item["contract"] in contract_names:
            raise _malformed("Duplicate provider contract")
        contract_names.add(item["contract"])
        contracts.append(ProviderContract(
            item["contract"], "1.0", frozenset(features),
            frozenset(item.get("schemes") or ()),
        ))

    schemes_raw = data["owned_schemes"]
    if not isinstance(schemes_raw, list):
        raise _malformed("owned_schemes must be an array")
    schemes: list[tuple[str, str]] = []
    seen: set[str] = set()
    for item in schemes_raw:
        if not isinstance(item, dict) or set(item) != {"scheme", "contract"}:
            raise _malformed(f"Invalid scheme declaration (expected object with only 'scheme' and 'contract'): {item!r}")
        scheme, contract = item["scheme"], item["contract"]
        if not isinstance(scheme, str) or not SCHEME_RE.fullmatch(scheme):
            raise _malformed(f"Invalid scheme declaration ({SCHEME_FORMAT}): {scheme!r}")
        if contract not in contract_names:
            raise _malformed(
                f"Scheme {scheme!r} references contract {contract!r}, which is not declared in provider_contracts "
                f"{sorted(contract_names)}",
            )
        if contract == "channel_catalog":
            raise _malformed("Channel catalog contract does not declare owned schemes")
        if scheme in seen:
            raise PluginError("SCHEME_CONFLICT", "Plugin declares the same scheme more than once", category="registry")
        seen.add(scheme)
        schemes.append((scheme, contract))

    tv_schemes = {scheme for scheme, contract in schemes if contract == "tv_provider"}
    for visual_schemes in visual_contract_schemes:
        if not visual_schemes.issubset(tv_schemes):
            raise PluginError(
                "SCHEME_CONFLICT", "TV visual metadata must belong to a TV-owned scheme", category="registry",
            )

    if "channel_catalog" in contract_names and "tv_provider" not in contract_names:
        raise PluginError(
            "PLUGIN_INCOMPATIBLE",
            "Channel catalog requires a TV provider contract",
            category="compatibility",
        )

    capabilities = data["capabilities"]
    if not isinstance(capabilities, list) or any(not isinstance(v, str) or not v for v in capabilities) or len(set(capabilities)) != len(capabilities):
        raise _malformed("Invalid capability declaration")
    permissions = data["permissions"]
    if (not isinstance(permissions, dict)
            or not set(permissions).issubset(SUPPORTED_PERMISSIONS)
            or any(not isinstance(v, (dict, bool, list)) for v in permissions.values())):
        raise _malformed("Invalid permission declaration")
    network = permissions.get("network")
    if network is not None:
        if (not isinstance(network, dict)
                or not set(network).issubset({"managed", "direct", "allow_http", "allowed_hosts", "allow_private"})
                or any(key in network and not isinstance(network[key], bool)
                       for key in ("managed", "direct", "allow_http", "allow_private"))
                or ("allowed_hosts" in network and
                    (not isinstance(network["allowed_hosts"], list)
                     or any(not isinstance(value, str) or not value for value in network["allowed_hosts"])) )):
            raise _malformed(
                "Invalid network permission declaration: allowed keys are "
                "managed, direct, allow_http, allowed_hosts, allow_private "
                f"(booleans except allowed_hosts); got {network!r}",
            )
        if network.get("allow_http") is True and network.get("managed") is not True:
            raise _malformed("Plain HTTP permission requires managed network")
    runtime = data["runtime"]
    if not isinstance(runtime, dict) or runtime.get("ipc") != "stdio_framed_json_v1":
        raise PluginError(
            "PLUGIN_INCOMPATIBLE",
            "Unsupported plugin runtime: ipc must be 'stdio_framed_json_v1', got "
            f"{(runtime or {}).get('ipc') if isinstance(runtime, dict) else runtime!r}",
            category="compatibility",
        )
    runtime_type = runtime.get("type")
    if runtime_type == "subprocess":
        if set(runtime) != {"type", "ipc"}:
            raise _malformed("Invalid subprocess runtime")
    elif runtime_type == "python":
        if set(runtime) != {"type", "ipc", "python_version_range", "entrypoint", "dependency_lock"}:
            raise _malformed("Invalid Python runtime specification")
        python_range = runtime.get("python_version_range")
        if (not isinstance(python_range, str) or not python_range
                or not all(RANGE_PART_RE.fullmatch(part) for part in python_range.split())):
            raise _malformed("Invalid Python version range")
        entrypoint = runtime.get("entrypoint")
        if (not isinstance(entrypoint, str) or not entrypoint or entrypoint.startswith(("/", "."))
                or ".." in Path(entrypoint).parts):
            raise _malformed("Invalid Python entrypoint")
        _validate_dependency_lock(runtime.get("dependency_lock"))
    else:
        raise PluginError(
            "PLUGIN_INCOMPATIBLE",
            f"Unsupported plugin runtime type: {runtime_type!r} (expected 'python' or 'subprocess')",
            category="compatibility",
        )
    artifacts = data["artifacts"]
    if not isinstance(artifacts, list) or not artifacts:
        raise _malformed("At least one plugin artifact is required")
    for artifact in artifacts:
        artifact_fields = {"os", "arch", "runtime", "entrypoint", "sha256", "size_bytes", "signature"}
        if not isinstance(artifact, dict) or set(artifact) != artifact_fields:
            raise _malformed(
                f"Invalid plugin artifact: expected exactly {sorted(artifact_fields)}, got "
                f"{sorted(artifact) if isinstance(artifact, dict) else artifact!r}",
            )
        signature = artifact.get("signature")
        if (artifact.get("os") not in SUPPORTED_OS or artifact.get("arch") not in SUPPORTED_ARCH
                or not isinstance(artifact.get("runtime"), str) or not artifact["runtime"]
                or not isinstance(artifact.get("entrypoint"), str) or not artifact["entrypoint"]
                or not re.fullmatch(r"[a-f0-9]{64}", str(artifact.get("sha256", "")))
                or not isinstance(artifact.get("size_bytes"), int) or isinstance(artifact["size_bytes"], bool)
                or artifact["size_bytes"] < 1 or not isinstance(signature, dict)
                or set(signature) != {"algorithm", "key_id", "value"}
                or signature.get("algorithm") != "ed25519"
                or not all(isinstance(signature.get(key), str) and signature[key] for key in ("key_id", "value"))):
            raise _malformed(
                f"Invalid plugin artifact for {artifact.get('os')!r}/{artifact.get('arch')!r}: "
                "os must be one of "
                f"{sorted(SUPPORTED_OS)}, arch one of {sorted(SUPPORTED_ARCH)}, sha256 a 64-char lowercase hex "
                "digest, size_bytes a positive integer, and signature an ed25519 "
                "{'algorithm', 'key_id', 'value'} object",
            )
    if runtime_type == "python" and any(artifact["entrypoint"] != runtime["entrypoint"] for artifact in artifacts):
        raise _malformed("Python runtime entrypoint does not match its artifact")
    dependencies = data["dependencies"]
    if not isinstance(dependencies, list):
        raise _malformed("Invalid plugin dependencies")
    for dependency in dependencies:
        if (not isinstance(dependency, dict) or not {"plugin", "version_range", "contract"}.issubset(dependency)
                or not isinstance(dependency["plugin"], str) or dependency["plugin"].count("/") != 1
                or dependency["contract"] not in SUPPORTED_CONTRACTS
                or not isinstance(dependency["version_range"], str)
                or not all(RANGE_PART_RE.fullmatch(part) for part in dependency["version_range"].split())):
            raise _malformed("Invalid plugin dependency")
    if not isinstance(data["state_schema_version"], int) or data["state_schema_version"] < 1:
        raise _malformed("Invalid state_schema_version")
    return PluginManifest(dict(data), publisher, plugin_id, data["display_name"], data["version"], API_VERSION,
                          expression, tuple(contracts), tuple(schemes), frozenset(capabilities), dict(permissions),
                          dict(runtime), tuple(dict(v) for v in artifacts), tuple(dict(v) for v in dependencies),
                          data["state_schema_version"])


def _validate_dependency_lock(value: Any) -> None:
    if not isinstance(value, dict) or set(value) != {"lock_version", "artifacts"} or value.get("lock_version") != 1:
        raise PluginError("DEPENDENCY_LOCK_INVALID", "Invalid Python dependency lock", category="dependency")
    artifacts = value.get("artifacts")
    if not isinstance(artifacts, list):
        raise PluginError("DEPENDENCY_LOCK_INVALID", "Invalid Python dependency lock artifacts", category="dependency")
    seen_candidates: set[tuple[str, str, str, str, str]] = set()
    seen_digests: set[str] = set()
    for item in artifacts:
        fields = {"name", "version", "filename", "url", "sha256", "size_bytes", "python_tag", "abi_tag", "platform_tag"}
        if not isinstance(item, dict) or set(item) != fields:
            raise PluginError("DEPENDENCY_LOCK_INVALID", "Invalid dependency artifact entry", category="dependency")
        name = str(item.get("name") or "")
        normalized = re.sub(r"[-_.]+", "-", name).lower()
        try:
            exact_version = Version(item.get("version")) if isinstance(item.get("version"), str) else None
        except InvalidVersion:
            exact_version = None
        if (name != normalized or not PLUGIN_ID_RE.fullmatch(name)
                or exact_version is None
                or not isinstance(item.get("filename"), str) or not item["filename"].endswith(".whl")
                or not isinstance(item.get("url"), str) or not item["url"].startswith("https://")
                or not re.fullmatch(r"[a-f0-9]{64}", str(item.get("sha256") or ""))
                or not isinstance(item.get("size_bytes"), int) or item["size_bytes"] < 1
                or any(not isinstance(item.get(key), str) or not item[key]
                       for key in ("python_tag", "abi_tag", "platform_tag"))):
            raise PluginError("DEPENDENCY_LOCK_INVALID", "Invalid dependency artifact metadata", category="dependency")
        candidate_key = (name, item["version"], item["python_tag"], item["abi_tag"], item["platform_tag"])
        if candidate_key in seen_candidates or item["sha256"] in seen_digests:
            raise PluginError("DEPENDENCY_LOCK_INVALID", "Duplicate dependency artifact", category="dependency")
        seen_candidates.add(candidate_key)
        seen_digests.add(item["sha256"])


def load_manifest(path: str | Path, *, core_version: str = "0.1.0") -> PluginManifest:
    try:
        with Path(path).open(encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise _malformed("Unable to read plugin manifest") from exc
    return validate_manifest(data, core_version=core_version)
