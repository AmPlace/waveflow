"""Read-only projections for the EPG management UI.

The management classification is deliberately built from logical channels,
logical bindings, source-aware catalog identities, preference evidence and
persisted matcher diagnostics. It never uses a legacy mapping authority. None
of the entry points run sync,
migration, matching, refresh or maintenance.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit, urlunsplit

import database as db
import epg
from epg_preference_evidence import _resolution_for_row
from epg_source_preference import (
    EpgSourcePreference,
    EpgSourcePreferenceResolution,
    EpgSourceState,
    resolve_logical_channel_source_preference,
    sanitize_evidence,
)
from epg_tasks import list_epg_automation_status


MATCHING_SCOPES = frozenset({
    "all", "bound", "unbound", "not_applicable", "needs_attention",
})
LOGICAL_SCOPES = frozenset({"active", "history", "all"})
DIAGNOSTIC_STATUSES = frozenset({
    "healthy_bound",
    "unmatched",
    "ambiguous",
    "logical_conflict",
    "split_conflict",
    "merge_conflict",
    "logical_orphan",
    "missing_target",
    "preference_conflict",
    "not_applicable",
})
CATALOG_AVAILABILITY = frozenset({"all", "enabled", "current"})
MAX_PAGE_SIZE = 100
MAX_CANDIDATES = 20
MAX_EVIDENCE_ITEMS = 8
MAX_DIAGNOSTIC_LENGTH = 512

_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(token|access_token|api[_-]?key|key|signature|sig|secret|password|"
    r"authorization|cookie)\s*[:=]\s*([^&\s,;]+)"
)
_URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class EpgManagementSnapshot:
    sources: tuple[dict[str, Any], ...]
    logical_channels: tuple[dict[str, Any], ...]
    members: tuple[dict[str, Any], ...]
    bindings: tuple[dict[str, Any], ...]
    policies: tuple[dict[str, Any], ...]
    catalog_channels: tuple[dict[str, Any], ...]
    decisions: tuple[dict[str, Any], ...]
    candidates: tuple[dict[str, Any], ...]
    preference_evidence: tuple[dict[str, Any], ...]


def _display_url(value: object) -> str:
    """Return a useful endpoint label without userinfo, query or fragment."""
    try:
        parsed = urlsplit(str(value or "").strip())
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return ""
        hostname = parsed.hostname
        if ":" in hostname and not hostname.startswith("["):
            hostname = f"[{hostname}]"
        netloc = hostname
        if parsed.port is not None:
            default_port = 80 if parsed.scheme.lower() == "http" else 443
            if parsed.port != default_port:
                netloc = f"{netloc}:{parsed.port}"
        return urlunsplit((parsed.scheme.lower(), netloc, parsed.path or "/", "", ""))[:512]
    except (TypeError, ValueError):
        return ""


def _safe_diagnostic(value: object, *, limit: int = MAX_DIAGNOSTIC_LENGTH) -> str:
    raw = str(value or "").replace("\x00", " ").strip()
    if not raw:
        return ""
    text = epg.sanitize_epg_error(raw)
    text = _URL_RE.sub(lambda match: _display_url(match.group(0)) or "[redacted-url]", text)
    text = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+\-/]+=*", "Bearer …", text)
    text = _SECRET_ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}=…", text)
    text = re.sub(r"\s+", " ", text)
    return text[:limit]


def _decode_names(value: object) -> tuple[str, ...]:
    if isinstance(value, (list, tuple)):
        values = value
    else:
        try:
            values = json.loads(str(value or "[]"))
        except (TypeError, ValueError, json.JSONDecodeError):
            values = []
    if not isinstance(values, list):
        return ()
    result = []
    for item in values:
        name = str(item or "").strip()
        if name and name not in result:
            result.append(name[:256])
    return tuple(result[:16])


def _source_health(source: Mapping[str, Any] | None) -> str:
    if source is None:
        return "failed"
    if not bool(source.get("enabled")):
        return "disabled"
    status = str(source.get("last_status") or "")
    if status == "success":
        return "healthy"
    if status == "stale":
        return "stale"
    return "failed"


def _source_projection(source: Mapping[str, Any], automation: Mapping[str, Any] | None) -> dict:
    automation = automation or {}
    last_run_status = str(automation.get("last_status") or "never_run")
    service_started = bool(automation.get("service_started"))
    task_enabled = bool(automation.get("enabled"))
    source_origin = str(source.get("source_origin") or "custom")
    is_builtin = source_origin == "builtin"
    return {
        "id": int(source["id"]),
        "name": str(source.get("name") or "")[:256],
        "enabled": bool(source.get("enabled")),
        "source_origin": source_origin,
        "builtin_key": str(source.get("builtin_key") or ""),
        "capabilities": {
            "can_edit_name": True,
            "can_edit_url": not is_builtin,
            "can_enable_disable": True,
            "can_refresh": True,
            "can_delete": not is_builtin,
        },
        "display_url": _display_url(source.get("url")),
        "data": {
            "channel_count": int(source.get("channel_count") or 0),
            "programme_count": int(source.get("programme_count") or 0),
            "coverage_start": str(source.get("data_start_at") or ""),
            "coverage_end": str(source.get("data_end_at") or ""),
        },
        "refresh": {
            "status": _source_health(source),
            "last_attempt": str(source.get("last_attempt_at") or ""),
            "last_success": str(source.get("last_success_at") or ""),
            "failure": _safe_diagnostic(source.get("last_error")),
        },
        "automation": {
            "scheduled": bool(source.get("enabled") and service_started and task_enabled),
            "running": last_run_status == "running",
            "next_run": str(automation.get("next_run_at") or ""),
            "last_run_status": last_run_status,
        },
    }


async def list_epg_source_statuses(automation_service=None) -> list[dict]:
    """Project current sources and bulk Automation state without reconciliation."""
    sources = await db.get_epg_sources()
    automation_by_source: dict[int, dict] = {}
    if automation_service is not None:
        try:
            rows = await list_epg_automation_status(
                automation_service,
                sources=sources,
            )
            for row in rows:
                item = dict(row)
                item["service_started"] = bool(automation_service.is_started)
                automation_by_source[int(item["source_id"])] = item
        except asyncio.CancelledError:
            raise
        except Exception:
            # Management reads remain available while Automation state is
            # temporarily unavailable; source data itself is authoritative.
            automation_by_source = {}
    return [
        _source_projection(source, automation_by_source.get(int(source["id"])))
        for source in sources
    ]


def _load_management_snapshot_sync(
    detail_logical_channel_id: str | None = None,
) -> EpgManagementSnapshot:
    conn = db._connect()
    try:
        conn.execute("BEGIN")
        sources = tuple(dict(row) for row in conn.execute(
            "SELECT * FROM epg_sources ORDER BY id"
        ).fetchall())
        logical_channels = tuple(dict(row) for row in conn.execute(
            "SELECT id, canonical_key, display_name, status FROM iptv_logical_channels ORDER BY display_name, id"
        ).fetchall())
        members = tuple(dict(row) for row in conn.execute(
            """
            SELECT m.logical_channel_id, m.channel_id, c.subscription_id
            FROM iptv_logical_channel_members AS m
            JOIN channels AS c ON c.id=m.channel_id
            ORDER BY m.logical_channel_id, m.channel_id
            """
        ).fetchall())
        bindings = tuple(dict(row) for row in conn.execute(
            """
            SELECT logical_channel_id, epg_source_id, epg_channel_id, status,
                   match_type, confidence, locked, origin
            FROM iptv_logical_channel_epg_bindings
            ORDER BY logical_channel_id
            """
        ).fetchall())
        policies = tuple(dict(row) for row in conn.execute(
            """
            SELECT logical_channel_id, mode, created_at, updated_at
            FROM iptv_logical_channel_epg_policies
            ORDER BY logical_channel_id
            """
        ).fetchall())
        catalog_channels = tuple(dict(row) for row in conn.execute(
            """
            SELECT c.source_id, c.channel_id, c.display_names
            FROM epg_channels AS c
            ORDER BY c.source_id, c.channel_id
            """
        ).fetchall())
        decisions = tuple(dict(row) for row in conn.execute(
            """
            WITH ranked AS (
                SELECT d.*, r.finished_at,
                       ROW_NUMBER() OVER (
                           PARTITION BY d.logical_channel_id
                           ORDER BY r.finished_at DESC, d.run_id DESC
                       ) AS row_rank
                FROM epg_match_shadow_decisions AS d
                JOIN epg_match_shadow_runs AS r ON r.run_id=d.run_id
                WHERE r.status IN ('success', 'partial')
            )
            SELECT * FROM ranked WHERE row_rank=1 ORDER BY logical_channel_id
            """
        ).fetchall())
        candidates: tuple[dict[str, Any], ...] = ()
        if detail_logical_channel_id:
            decision = next((
                row for row in decisions
                if str(row["logical_channel_id"]) == detail_logical_channel_id
            ), None)
            if decision is not None:
                candidates = tuple(dict(row) for row in conn.execute(
                    """
                    SELECT * FROM epg_match_shadow_candidates
                    WHERE run_id=? AND logical_channel_id=?
                    ORDER BY rank
                    """,
                    (decision["run_id"], detail_logical_channel_id),
                ).fetchall())
        preference_evidence = tuple(dict(row) for row in conn.execute(
            """
            SELECT * FROM epg_source_preference_evidence
            WHERE valid=1 AND current=1
            ORDER BY subscription_id, logical_channel_id, id
            """
        ).fetchall())
        conn.commit()
        return EpgManagementSnapshot(
            sources=sources,
            logical_channels=logical_channels,
            members=members,
            bindings=bindings,
            policies=policies,
            catalog_channels=catalog_channels,
            decisions=decisions,
            candidates=candidates,
            preference_evidence=preference_evidence,
        )
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


async def load_epg_management_snapshot(
    detail_logical_channel_id: str | None = None,
) -> EpgManagementSnapshot:
    return await asyncio.to_thread(
        _load_management_snapshot_sync,
        detail_logical_channel_id,
    )


def _build_preference_map(
    snapshot: EpgManagementSnapshot,
    subscription_ids_by_logical: Mapping[str, set[int]],
) -> dict[str, EpgSourcePreferenceResolution]:
    states = tuple(
        EpgSourceState(
            int(row["id"]),
            bool(row.get("enabled")),
            str(row.get("last_status") or ""),
        )
        for row in snapshot.sources
    )
    preferences: list[EpgSourcePreference] = []
    invalid_rows: list[dict[str, Any]] = []
    source_rows = [dict(row) for row in snapshot.sources]
    for row in snapshot.preference_evidence:
        try:
            resolved, invalid_reason = _resolution_for_row(dict(row), source_rows)
        except (TypeError, ValueError, json.JSONDecodeError):
            resolved, invalid_reason = (), "invalid"
        preferences.extend(resolved)
        if invalid_reason:
            invalid_rows.append(dict(row))

    result: dict[str, EpgSourcePreferenceResolution] = {}
    for logical in snapshot.logical_channels:
        logical_id = str(logical["id"])
        member_subscriptions = subscription_ids_by_logical.get(logical_id, set())
        resolution = resolve_logical_channel_source_preference(
            logical_channel_id=logical_id,
            subscription_ids=member_subscriptions,
            available_sources=states,
            preferences=preferences,
        )
        has_applicable_invalid = any(
            (not str(row.get("logical_channel_id") or "")
             or str(row.get("logical_channel_id")) == logical_id)
            and (
                row.get("subscription_id") is None
                or int(row["subscription_id"]) in member_subscriptions
            )
            for row in invalid_rows
        )
        if resolution.status == "none" and has_applicable_invalid:
            resolution = EpgSourcePreferenceResolution(None, "missing_source")
        result[logical_id] = resolution
    return result


def _preference_projection(
    resolution: EpgSourcePreferenceResolution,
    sources_by_id: Mapping[int, Mapping[str, Any]],
) -> dict:
    source = (
        sources_by_id.get(int(resolution.preferred_source_id))
        if resolution.preferred_source_id is not None
        else None
    )
    return {
        "status": resolution.status,
        "preferred_source_id": resolution.preferred_source_id,
        "preferred_source_name": str(source.get("name") or "") if source else "",
        "origin": resolution.origin or "",
        "conflict": resolution.status == "conflict",
    }


def _decision_status(decision: Mapping[str, Any] | None) -> str:
    return str(decision.get("status") or "") if decision else ""


def _classify_item(
    logical: Mapping[str, Any],
    binding: Mapping[str, Any] | None,
    target_exists: bool,
    decision: Mapping[str, Any] | None,
    preference: EpgSourcePreferenceResolution,
    policy_mode: str,
) -> tuple[str, str]:
    logical_status = str(logical.get("status") or "")
    if logical_status == "split_conflict":
        return "split_conflict", "频道成员发生拆分冲突，需要人工确认"
    if logical_status == "merge_conflict":
        return "merge_conflict", "多个逻辑频道发生合并冲突，需要人工确认"
    if logical_status == "orphaned":
        return "logical_orphan", "逻辑频道已无有效直播源成员"
    if policy_mode == "no_epg":
        return "not_applicable", "用户已明确停用该频道的节目单"
    if binding and (str(binding.get("status")) == "orphan_target" or not target_exists):
        return "missing_target", "当前绑定的 EPG 复合目标已不存在"
    if preference.status == "conflict":
        return "preference_conflict", "同优先级来源偏好指向多个 EPG 源"
    if binding and str(binding.get("status")) == "matched" and target_exists:
        return "healthy_bound", "已绑定可读取的 EPG 频道"

    decision_status = _decision_status(decision)
    binding_status = str(binding.get("status") or "") if binding else ""
    if decision_status == "ambiguous" or binding_status == "ambiguous":
        return "ambiguous", "存在多个等价候选，未自动选择"
    if decision_status in {"conflict", "error"} or binding_status == "conflict":
        return "logical_conflict", "当前匹配输入存在冲突"
    return "unmatched", "尚未建立 EPG 绑定"


def _binding_state(diagnostic_status: str) -> str:
    if diagnostic_status == "healthy_bound":
        return "bound"
    if diagnostic_status == "unmatched":
        return "unbound"
    if diagnostic_status == "not_applicable":
        return "not_applicable"
    return "needs_attention"


def _matching_items(snapshot: EpgManagementSnapshot) -> list[dict]:
    sources_by_id = {int(row["id"]): row for row in snapshot.sources}
    catalog_by_identity = {
        (int(row["source_id"]), str(row["channel_id"])): row
        for row in snapshot.catalog_channels
    }
    bindings_by_logical = {
        str(row["logical_channel_id"]): row for row in snapshot.bindings
    }
    decisions_by_logical = {
        str(row["logical_channel_id"]): row for row in snapshot.decisions
    }
    policies_by_logical = {
        str(row["logical_channel_id"]): row for row in snapshot.policies
    }
    members_by_logical: dict[str, list[dict[str, Any]]] = {}
    subscription_ids_by_logical: dict[str, set[int]] = {}
    for row in snapshot.members:
        logical_id = str(row["logical_channel_id"])
        members_by_logical.setdefault(logical_id, []).append(row)
        subscription_ids_by_logical.setdefault(logical_id, set()).add(
            int(row["subscription_id"])
        )
    preferences = _build_preference_map(snapshot, subscription_ids_by_logical)

    items = []
    for logical in snapshot.logical_channels:
        logical_id = str(logical["id"])
        binding = bindings_by_logical.get(logical_id)
        target = None
        source = None
        if binding:
            identity = (int(binding["epg_source_id"]), str(binding["epg_channel_id"]))
            target = catalog_by_identity.get(identity)
            source = sources_by_id.get(identity[0])
        preference = preferences.get(
            logical_id,
            EpgSourcePreferenceResolution(None, "none"),
        )
        policy = policies_by_logical.get(logical_id)
        policy_mode = str(policy.get("mode") or "") if policy else ""
        diagnostic_status, reason = _classify_item(
            logical,
            binding,
            target is not None,
            decisions_by_logical.get(logical_id),
            preference,
            policy_mode,
        )
        display_names = _decode_names(target.get("display_names")) if target else ()
        member_rows = members_by_logical.get(logical_id, [])
        binding_projection = {
            "status": _binding_state(diagnostic_status),
            "epg_source_id": int(binding["epg_source_id"]) if binding else None,
            "epg_source_name": str(source.get("name") or "") if source else "",
            "epg_channel_id": str(binding["epg_channel_id"]) if binding else "",
            "epg_channel_display_name": display_names[0] if display_names else "",
            "origin": str(binding.get("origin") or "") if binding else "",
            "locked": bool(binding.get("locked")) if binding else False,
            "match_type": str(binding.get("match_type") or "") if binding else "",
            "confidence": int(binding.get("confidence") or 0) if binding else 0,
        }
        items.append({
            "channel": {
                "logical_channel_id": logical_id,
                "display_name": str(logical.get("display_name") or "")[:256],
                "state": str(logical.get("status") or ""),
                "member_count": len(member_rows),
                "source_count": len(subscription_ids_by_logical.get(logical_id, set())),
            },
            "binding": binding_projection,
            "diagnostic": {
                "status": diagnostic_status,
                "reason": reason,
            },
            "preference": _preference_projection(preference, sources_by_id),
            "policy": {
                "mode": policy_mode or "automatic",
                "explicit": policy is not None,
                "updated_at": str(policy.get("updated_at") or "") if policy else "",
            },
        })
    return sorted(
        items,
        key=lambda item: (
            item["channel"]["display_name"].casefold(),
            item["channel"]["logical_channel_id"],
        ),
    )


async def get_epg_management_overview() -> dict:
    snapshot = await load_epg_management_snapshot()
    items = _matching_items(snapshot)
    active = [item for item in items if item["channel"]["state"] == "active"]
    source_counts = {status: 0 for status in ("healthy", "stale", "failed", "disabled")}
    for source in snapshot.sources:
        source_counts[_source_health(source)] += 1
    statuses = [item["diagnostic"]["status"] for item in items]
    active_binding_states = [item["binding"]["status"] for item in active]
    return {
        "sources": {"total": len(snapshot.sources), **source_counts},
        "logical_channels": {
            "total": len(active),
            "bound": active_binding_states.count("bound"),
            "unbound": active_binding_states.count("unbound"),
            "not_applicable": active_binding_states.count("not_applicable"),
            "needs_attention": active_binding_states.count("needs_attention"),
        },
        "diagnostics": {
            "ambiguous": statuses.count("ambiguous"),
            "conflict": sum(status in {"logical_conflict", "split_conflict", "merge_conflict"} for status in statuses),
            "split_conflict": statuses.count("split_conflict"),
            "merge_conflict": statuses.count("merge_conflict"),
            "orphan": statuses.count("logical_orphan"),
            "missing_target": statuses.count("missing_target"),
            "preference_conflict": statuses.count("preference_conflict"),
        },
    }


def _validate_pagination(page: int, page_size: int) -> tuple[int, int]:
    if isinstance(page, bool) or page < 1:
        raise ValueError("page 必须大于等于 1")
    if isinstance(page_size, bool) or not 1 <= page_size <= MAX_PAGE_SIZE:
        raise ValueError(f"page_size 必须在 1 到 {MAX_PAGE_SIZE} 之间")
    return page, page_size


async def list_epg_matching_channels(
    *,
    page: int = 1,
    page_size: int = 50,
    scope: str = "all",
    logical_scope: str = "active",
    diagnostic_status: str = "",
    source_id: int | None = None,
    text: str = "",
) -> dict:
    page, page_size = _validate_pagination(page, page_size)
    if scope not in MATCHING_SCOPES:
        raise ValueError("不支持的匹配状态筛选")
    if logical_scope not in LOGICAL_SCOPES:
        raise ValueError("不支持的逻辑频道范围")
    if diagnostic_status and diagnostic_status not in DIAGNOSTIC_STATUSES:
        raise ValueError("不支持的诊断状态筛选")
    if source_id is not None and (isinstance(source_id, bool) or source_id <= 0):
        raise ValueError("source_id 必须是正整数")

    snapshot = await load_epg_management_snapshot()
    items = _matching_items(snapshot)
    query = text.strip().casefold()[:256]
    filtered = []
    for item in items:
        logical_active = item["channel"]["state"] == "active"
        if logical_scope == "active" and not logical_active:
            continue
        if logical_scope == "history" and logical_active:
            continue
        if scope != "all" and item["binding"]["status"] != scope:
            continue
        if diagnostic_status and item["diagnostic"]["status"] != diagnostic_status:
            continue
        if source_id is not None and item["binding"]["epg_source_id"] != source_id:
            continue
        if query:
            haystack = "\n".join((
                item["channel"]["display_name"],
                item["binding"]["epg_source_name"],
                item["binding"]["epg_channel_id"],
                item["binding"]["epg_channel_display_name"],
            )).casefold()
            if query not in haystack:
                continue
        filtered.append(item)
    offset = (page - 1) * page_size
    return {
        "items": filtered[offset:offset + page_size],
        "page": page,
        "page_size": page_size,
        "total": len(filtered),
        "logical_scope": logical_scope,
    }


def _safe_json_list(value: object, *, limit: int = 16) -> list[str]:
    try:
        decoded = json.loads(str(value or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        decoded = []
    if not isinstance(decoded, list):
        return []
    return [
        safe
        for item in decoded[:limit]
        if (safe := _safe_diagnostic(item, limit=256))
    ]


def _candidate_evidence(value: object) -> list[dict[str, str | int | bool]]:
    try:
        decoded = json.loads(str(value or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        decoded = []
    values: Iterable[object] = decoded if isinstance(decoded, list) else (decoded,)
    result = []
    for item in values:
        safe = {
            key: _safe_diagnostic(value, limit=256) if isinstance(value, str) else value
            for key, value in sanitize_evidence(item)
        }
        if safe:
            result.append(safe)
        if len(result) >= MAX_EVIDENCE_ITEMS:
            break
    return result


async def get_epg_matching_detail(
    logical_channel_id: str,
    *,
    candidate_limit: int = 10,
) -> dict | None:
    logical_channel_id = str(logical_channel_id or "").strip()
    if not logical_channel_id:
        raise ValueError("logical_channel_id 不能为空")
    if isinstance(candidate_limit, bool) or not 1 <= candidate_limit <= MAX_CANDIDATES:
        raise ValueError(f"candidate_limit 必须在 1 到 {MAX_CANDIDATES} 之间")
    snapshot = await load_epg_management_snapshot(logical_channel_id)
    item = next((
        row for row in _matching_items(snapshot)
        if row["channel"]["logical_channel_id"] == logical_channel_id
    ), None)
    if item is None:
        return None

    logical = next(
        row for row in snapshot.logical_channels
        if str(row["id"]) == logical_channel_id
    )
    production_read = (
        await _production_read_projection(str(logical.get("canonical_key") or ""))
        if item["channel"]["state"] == "active"
        else {"status": "not_applicable"}
    )

    sources_by_id = {int(row["id"]): row for row in snapshot.sources}
    catalog_by_identity = {
        (int(row["source_id"]), str(row["channel_id"])): row
        for row in snapshot.catalog_channels
    }
    decision = next((
        row for row in snapshot.decisions
        if str(row["logical_channel_id"]) == logical_channel_id
    ), None)
    decision_projection = None
    if decision:
        selected_source_id = decision.get("selected_epg_source_id")
        selected_channel_id = decision.get("selected_epg_channel_id")
        selected_identity = None
        if selected_source_id is not None and selected_channel_id is not None:
            selected_identity = {
                "epg_source_id": int(selected_source_id),
                "epg_channel_id": str(selected_channel_id),
            }
        decision_projection = {
            "status": str(decision.get("status") or ""),
            "selected_identity": selected_identity,
            "match_type": str(decision.get("match_type") or ""),
            "confidence": int(decision.get("confidence") or 0),
            "reasons": _safe_json_list(decision.get("reasons_json")),
            "hint_conflicts": _safe_json_list(decision.get("hint_conflicts_json")),
            "candidate_count": int(decision.get("candidate_count") or 0),
            "error": _safe_diagnostic(decision.get("error")),
        }

    candidates = []
    for row in snapshot.candidates[:candidate_limit]:
        source_id = int(row["epg_source_id"])
        channel_id = str(row["epg_channel_id"])
        source = sources_by_id.get(source_id)
        channel = catalog_by_identity.get((source_id, channel_id))
        names = _decode_names(channel.get("display_names")) if channel else ()
        candidates.append({
            "rank": int(row["rank"]),
            "epg_source_id": source_id,
            "epg_source_name": str(source.get("name") or "") if source else "",
            "epg_channel_id": channel_id,
            "epg_channel_display_name": names[0] if names else "",
            "match_type": str(row.get("match_type") or ""),
            "confidence": int(row.get("confidence") or 0),
            "source_health": _source_health(source),
            "evidence": _candidate_evidence(row.get("evidence_json")),
        })

    binding_source = sources_by_id.get(item["binding"]["epg_source_id"])
    return {
        **item,
        "binding_source_health": _source_health(binding_source) if item["binding"]["epg_source_id"] else "",
        "latest_matcher_decision": decision_projection,
        "production_read": production_read,
        "candidates": candidates,
        "candidate_limit": candidate_limit,
    }


async def _production_read_projection(canonical_key: str) -> dict[str, str | bool]:
    """Report the current logical binding read authority."""
    try:
        from epg_read_resolver import resolve_epg_read

        resolution = await resolve_epg_read(canonical_key)
    except Exception:
        return {"status": "unavailable"}

    effective_source = str(resolution.get("effective_source") or "none")
    status = {
        "logical": "logical_binding",
    }.get(effective_source, "none")
    return {"status": status}


def _catalog_search_sync(
    *,
    page: int,
    page_size: int,
    text: str,
    source_id: int | None,
    availability: str,
) -> dict:
    where = []
    params: list[Any] = []
    if source_id is not None:
        where.append("s.id=?")
        params.append(source_id)
    if availability == "enabled":
        where.append("s.enabled=1")
    elif availability == "current":
        where.append("s.enabled=1 AND s.last_status='success'")
    query = text.strip().casefold()[:256]
    if query:
        where.append("(LOWER(c.channel_id) LIKE ? OR LOWER(c.display_names) LIKE ? OR LOWER(s.name) LIKE ?)")
        like = f"%{query}%"
        params.extend((like, like, like))
    where_sql = f" WHERE {' AND '.join(where)}" if where else ""
    conn = db._connect()
    try:
        conn.execute("BEGIN")
        total = int(conn.execute(
            "SELECT COUNT(*) FROM epg_channels AS c JOIN epg_sources AS s ON s.id=c.source_id" + where_sql,
            params,
        ).fetchone()[0])
        rows = conn.execute(
            """
            SELECT c.source_id, s.name AS source_name, s.enabled AS source_enabled,
                   s.last_status AS source_status, c.channel_id, c.display_names
            FROM epg_channels AS c
            JOIN epg_sources AS s ON s.id=c.source_id
            """ + where_sql + " ORDER BY LOWER(s.name), s.id, LOWER(c.channel_id), c.channel_id LIMIT ? OFFSET ?",
            (*params, page_size, (page - 1) * page_size),
        ).fetchall()
        conn.commit()
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()
    items = []
    for row in rows:
        item = dict(row)
        names = _decode_names(item.get("display_names"))
        source = {
            "enabled": item.get("source_enabled"),
            "last_status": item.get("source_status"),
        }
        items.append({
            "identity": {
                "epg_source_id": int(item["source_id"]),
                "epg_channel_id": str(item["channel_id"]),
            },
            "epg_source_id": int(item["source_id"]),
            "epg_source_name": str(item.get("source_name") or ""),
            "epg_channel_id": str(item["channel_id"]),
            "epg_channel_display_name": names[0] if names else "",
            "source_health": _source_health(source),
        })
    return {
        "items": items,
        "page": page,
        "page_size": page_size,
        "total": total,
    }


async def search_epg_catalog(
    *,
    page: int = 1,
    page_size: int = 50,
    text: str = "",
    source_id: int | None = None,
    availability: str = "all",
) -> dict:
    page, page_size = _validate_pagination(page, page_size)
    if source_id is not None and (isinstance(source_id, bool) or source_id <= 0):
        raise ValueError("source_id 必须是正整数")
    if availability not in CATALOG_AVAILABILITY:
        raise ValueError("不支持的 EPG source 可用性筛选")
    return await asyncio.to_thread(
        _catalog_search_sync,
        page=page,
        page_size=page_size,
        text=text,
        source_id=source_id,
        availability=availability,
    )


__all__ = [
    "CATALOG_AVAILABILITY",
    "DIAGNOSTIC_STATUSES",
    "LOGICAL_SCOPES",
    "MATCHING_SCOPES",
    "MAX_CANDIDATES",
    "MAX_PAGE_SIZE",
    "get_epg_management_overview",
    "get_epg_matching_detail",
    "list_epg_matching_channels",
    "list_epg_source_statuses",
    "load_epg_management_snapshot",
    "search_epg_catalog",
]
