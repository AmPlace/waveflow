"""Stable shadow identity for aggregated IPTV logical channels.

This module deliberately does not replace the production IPTV projection. It
uses the same persisted raw rows and normalization helper to build a durable,
diagnostic shadow model for the later EPG identity work.
"""

from datetime import datetime, timezone

import database as db
from m3u8_parser import channel_name_semantics, clean_channel_display_name, normalize_channel_name, _channel_alias


_GENERIC_CANDIDATES = frozenset({
    '新闻综合', '综合频道', '综合', '公共频道', '新闻频道', '地方频道',
})


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _display_name(raw_name: str) -> str:
    # Keep user/source spelling for the logical display name; qualifiers are
    # removed only from the matcher candidate and remain available on source
    # rows.  This preserves existing UI behavior for names such as 测试新闻台.
    display_name = clean_channel_display_name(raw_name)
    primary_name = _channel_alias.get_primary(display_name)
    if primary_name and primary_name != display_name:
        return primary_name
    return display_name


def _build_runtime_groups(raw_channels: list[dict]) -> list[dict]:
    groups: dict[str, dict] = {}
    for channel in raw_channels:
        channel_id = int(channel['id'])
        semantics = channel_name_semantics(channel['name'])
        candidate = str(semantics.get('canonical_candidate') or '').strip()
        if not candidate:
            continue
        stable = bool(
            str(channel.get('market_source_item_id') or '').strip()
            and not str(channel.get('market_source_item_id') or '').startswith('auto-')
        ) or bool(str(channel.get('tvg_id') or '').strip()) or (
            str(channel.get('source_type') or '').lower() == 'adapter'
            and '://' in str(channel.get('url') or '')
        )
        # Generic names carry insufficient cross-subscription evidence.  Keep
        # them subscription-local unless a stable provider/EPG identity exists.
        match_key = candidate
        if candidate in _GENERIC_CANDIDATES and not stable:
            match_key = f'{candidate}::subscription:{int(channel["subscription_id"])}'
        group = groups.setdefault(
            match_key,
            {
                'canonical_key': match_key,
                'display_name': _display_name(channel['name']),
                'channel_ids': [],
                'membership_reason': 'stable_identity' if stable else 'conservative_name',
                'membership_confidence': 100 if stable else 70,
            },
        )
        group['channel_ids'].append(channel_id)
    return list(groups.values())


async def get_iptv_runtime_channel_groups() -> list[dict]:
    """Return the current raw-channel projection using production ordering."""

    return _build_runtime_groups(await db.get_aggregated_channels())


async def sync_iptv_logical_channels() -> dict:
    started_at = _utc_now()
    raw_channels = await db.get_aggregated_channels()
    groups = _build_runtime_groups(raw_channels)
    sync_result = await db.sync_iptv_logical_channel_shadow_atomic(groups)
    finished_at = _utc_now()
    projection = await validate_iptv_logical_channel_projection(
        raw_channels=raw_channels,
        groups=groups,
    )
    return {
        'raw_channel_count': len(raw_channels),
        'target_group_count': len(groups),
        'created_logical_count': sync_result['created_logical_count'],
        'reused_logical_count': sync_result['reused_logical_count'],
        'updated_logical_count': sync_result['updated_logical_count'],
        'orphaned_logical_count': projection['orphaned_logical_count'],
        'created_member_count': sync_result['created_member_count'],
        'removed_member_count': sync_result['removed_member_count'],
        'split_conflicts': sync_result['split_conflicts'],
        'merge_conflicts': sync_result['merge_conflicts'],
        'projection_mismatches': projection['projection_mismatches'],
        'projection_mismatch_count': projection['projection_mismatch_count'],
        'started_at': started_at,
        'finished_at': finished_at,
    }


async def validate_iptv_logical_channel_projection(
    *,
    raw_channels: list[dict] | None = None,
    groups: list[dict] | None = None,
) -> dict:
    if raw_channels is None:
        raw_channels = await db.get_aggregated_channels()
    if groups is None:
        groups = _build_runtime_groups(raw_channels)

    logical_rows = await db.get_iptv_logical_channels()
    member_rows = await db.get_iptv_logical_channel_members()
    raw_ids = {int(channel['id']) for channel in raw_channels}
    runtime_by_key = {group['canonical_key']: group for group in groups}
    logical_by_id = {row['id']: row for row in logical_rows}
    shadow_members: dict[str, set[int]] = {row['id']: set() for row in logical_rows}
    memberships_by_channel: dict[int, list[str]] = {}
    for member in member_rows:
        channel_id = int(member['channel_id'])
        logical_id = member['logical_channel_id']
        shadow_members.setdefault(logical_id, set()).add(channel_id)
        memberships_by_channel.setdefault(channel_id, []).append(logical_id)

    shadow_by_key: dict[str, list[dict]] = {}
    for logical_id, member_ids in shadow_members.items():
        logical = logical_by_id.get(logical_id)
        if logical is None:
            continue
        shadow_by_key.setdefault(logical['canonical_key'], []).append({
            'logical_channel_id': logical_id,
            'display_name': logical['display_name'],
            'status': logical['status'],
            'channel_ids': sorted(member_ids),
        })

    missing_raw_channels = sorted(raw_ids - set(memberships_by_channel))
    duplicate_memberships = sorted(
        {
            channel_id: logical_ids
            for channel_id, logical_ids in memberships_by_channel.items()
            if len(logical_ids) > 1
        }.items()
    )
    runtime_without_shadow = sorted(
        key for key in runtime_by_key if key not in shadow_by_key
    )
    shadow_active_without_runtime = sorted(
        logical_id
        for logical_id, logical in logical_by_id.items()
        if logical['status'] == 'active' and logical['canonical_key'] not in runtime_by_key
    )
    member_count_mismatch = []
    display_name_mismatch = []
    canonical_key_mismatch = []
    for key, runtime_group in runtime_by_key.items():
        candidates = shadow_by_key.get(key, [])
        shadow_count = sum(len(candidate['channel_ids']) for candidate in candidates)
        if shadow_count != len(runtime_group['channel_ids']):
            member_count_mismatch.append({
                'canonical_key': key,
                'runtime_count': len(runtime_group['channel_ids']),
                'shadow_count': shadow_count,
            })
        if candidates and all(candidate['display_name'] != runtime_group['display_name'] for candidate in candidates):
            display_name_mismatch.append({
                'canonical_key': key,
                'runtime_display_name': runtime_group['display_name'],
                'shadow_display_names': sorted({candidate['display_name'] for candidate in candidates}),
            })
        if not candidates:
            canonical_key_mismatch.append({
                'canonical_key': key,
                'reason': 'runtime_group_missing_from_shadow',
            })

    projection_mismatches = {
        'missing_raw_channels': missing_raw_channels,
        'duplicate_memberships': [
            {'channel_id': channel_id, 'logical_channel_ids': logical_ids}
            for channel_id, logical_ids in duplicate_memberships
        ],
        'runtime_without_shadow': runtime_without_shadow,
        'shadow_active_without_runtime': shadow_active_without_runtime,
        'member_count_mismatch': member_count_mismatch,
        'display_name_mismatch': display_name_mismatch,
        'canonical_key_mismatch': canonical_key_mismatch,
    }
    mismatch_count = sum(len(items) for items in projection_mismatches.values())
    covered_ids = raw_ids & set(memberships_by_channel)
    raw_membership_coverage = len(covered_ids) / len(raw_ids) if raw_ids else 1.0

    return {
        'runtime_canonical_groups': runtime_by_key,
        'shadow_logical_groups': shadow_by_key,
        'missing_raw_channels': missing_raw_channels,
        'duplicate_memberships': projection_mismatches['duplicate_memberships'],
        'runtime_without_shadow': runtime_without_shadow,
        'shadow_active_without_runtime': shadow_active_without_runtime,
        'member_count_mismatch': member_count_mismatch,
        'display_name_mismatch': display_name_mismatch,
        'canonical_key_mismatch': canonical_key_mismatch,
        'projection_mismatches': projection_mismatches,
        'projection_mismatch_count': mismatch_count,
        'raw_membership_coverage': raw_membership_coverage,
        'source_count': len({int(channel['subscription_id']) for channel in raw_channels}),
        'raw_channel_count': len(raw_channels),
        'logical_channel_count': len(logical_rows),
        'orphaned_logical_count': sum(row['status'] == 'orphaned' for row in logical_rows),
    }


async def get_iptv_logical_channel_hints(logical_channel_id: str | None = None) -> list[dict]:
    """Return read-only raw hints for future source-aware EPG matching.

    This helper intentionally aggregates persisted membership metadata only. It
    does not inspect stream URLs, choose an EPG candidate, or create a binding.
    """

    rows = await db.get_iptv_logical_channel_hint_rows(logical_channel_id)
    grouped: dict[str, dict] = {}

    for row in rows:
        logical_id = str(row['logical_channel_id'])
        hint = grouped.setdefault(
            logical_id,
            {
                'logical_channel_id': logical_id,
                'canonical_key': str(row.get('canonical_key') or ''),
                'display_name': str(row.get('display_name') or ''),
                'status': str(row.get('logical_status') or ''),
                'member_channel_ids': [],
                'raw_tvg_ids': [],
                'raw_tvg_names': [],
                'raw_display_names': [],
                'variant_hints': [],
            },
        )
        member_id = row.get('member_channel_id')
        if member_id is None:
            continue
        member_id = int(member_id)
        hint['member_channel_ids'].append(member_id)
        for field, output_key in (
            ('raw_tvg_id', 'raw_tvg_ids'),
            ('raw_tvg_name', 'raw_tvg_names'),
            ('raw_name', 'raw_display_names'),
        ):
            value = str(row.get(field) or '').strip()
            if value and value not in hint[output_key]:
                hint[output_key].append(value)
        hint['variant_hints'].append({
            'channel_id': member_id,
            'membership_reason': str(row.get('membership_reason') or ''),
            'membership_confidence': int(row.get('membership_confidence') or 0),
            'variant_type': str(row.get('variant_type') or ''),
        })

    result = []
    for hint in grouped.values():
        hint['member_channel_ids'] = sorted(set(hint['member_channel_ids']))
        hint['variant_hints'] = sorted(hint['variant_hints'], key=lambda item: item['channel_id'])
        hint['has_conflicting_tvg_ids'] = len(hint['raw_tvg_ids']) > 1
        hint['has_membership_conflict'] = hint['status'] in {'split_conflict', 'merge_conflict'}
        result.append(hint)
    return result
