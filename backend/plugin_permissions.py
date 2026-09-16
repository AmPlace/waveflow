from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

import database as db
from plugin_runtime import PluginError, PluginManifest


MANAGED_HTTP_PERMISSION = "network.managed_http"
HIGH_RISK_PERMISSIONS = frozenset({"network.direct", MANAGED_HTTP_PERMISSION})


@dataclass(frozen=True)
class PermissionRequest:
    name: str
    risk: str
    fingerprint: str
    value: Any


def requested_permissions(manifest: PluginManifest) -> tuple[PermissionRequest, ...]:
    network = manifest.permissions.get("network")
    requests: list[PermissionRequest] = []
    if isinstance(network, dict) and network.get("managed") is True:
        requests.append(_request("network.managed", "standard", True))
    if isinstance(network, dict) and network.get("direct") is True:
        requests.append(_request("network.direct", "high", True))
    if isinstance(network, dict) and network.get("allow_http") is True:
        requests.append(_request(MANAGED_HTTP_PERMISSION, "high", True))
    return tuple(requests)


def _request(name: str, risk: str, value: Any) -> PermissionRequest:
    encoded = json.dumps({"name": name, "value": value}, sort_keys=True, separators=(",", ":")).encode()
    return PermissionRequest(name, risk, hashlib.sha256(encoded).hexdigest(), value)


async def permission_projection(manifest: PluginManifest) -> dict[str, Any]:
    requests = requested_permissions(manifest)
    approvals = await db.list_plugin_permission_approvals(manifest.publisher_id, manifest.plugin_id)
    approved_keys = {
        (row["permission_name"], row["permission_fingerprint"])
        for row in approvals if row.get("approved")
    }
    requested = [_public(item) for item in requests]
    approved = [_public(item) for item in requests if (item.name, item.fingerprint) in approved_keys
                or item.name not in HIGH_RISK_PERMISSIONS]
    pending = [_public(item) for item in requests if item.name in HIGH_RISK_PERMISSIONS
               and (item.name, item.fingerprint) not in approved_keys]
    return {"requested": requested, "approved": approved, "pending": pending,
            "risk": {item.name: item.risk for item in requests}}


async def require_high_risk_approvals(manifest: PluginManifest, *, record_pending: bool = True) -> None:
    pending = []
    for item in requested_permissions(manifest):
        if item.name not in HIGH_RISK_PERMISSIONS:
            continue
        row = await db.get_plugin_permission_approval(
            manifest.publisher_id, manifest.plugin_id, item.name, item.fingerprint)
        if not row or not row.get("approved"):
            if record_pending and row is None:
                await db.set_plugin_permission_approval(
                    manifest.publisher_id, manifest.plugin_id, item.name, item.fingerprint,
                    approved=False, actor="system", manifest_version=manifest.version)
            pending.append(item.name)
    if pending:
        raise PluginError(
            "PERMISSION_APPROVAL_REQUIRED", "Plugin requires approval for a high-risk permission",
            category="permission", details={"permissions": sorted(pending)},
        )


def _public(item: PermissionRequest) -> dict[str, Any]:
    return {"name": item.name, "risk": item.risk}
