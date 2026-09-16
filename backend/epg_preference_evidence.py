"""Acquisition and persistence for auditable EPG source preference evidence."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable
from urllib.parse import urlsplit, urlunsplit

import database as db
from epg_source_preference import (
    EpgSourcePreference,
    EpgSourcePreferenceResolution,
    EpgSourceState,
    resolve_epg_source_preference,
    sanitize_evidence,
)


class EpgPreferenceEvidenceError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _positive_id(value: object, field: str) -> int:
    if isinstance(value, bool):
        raise EpgPreferenceEvidenceError(
            'preference_invalid',
            f'{field} 必须是正整数',
        )
    try:
        normalized = int(value)
    except (TypeError, ValueError) as error:
        raise EpgPreferenceEvidenceError(
            'preference_invalid',
            f'{field} 必须是正整数',
        ) from error
    if normalized <= 0:
        raise EpgPreferenceEvidenceError(
            'preference_invalid',
            f'{field} 必须是正整数',
        )
    return normalized


def normalize_epg_source_url(value: str) -> str | None:
    """Normalize transport syntax while preserving path and complete query semantics."""
    try:
        parsed = urlsplit((value or '').strip())
        if parsed.scheme.lower() not in {'http', 'https'} or not parsed.hostname:
            return None
        if parsed.username or parsed.password:
            return None
        host = parsed.hostname.lower()
        port = parsed.port
    except ValueError:
        return None
    if ':' in host and not host.startswith('['):
        host = f'[{host}]'
    if port and not ((parsed.scheme.lower() == 'http' and port == 80) or (parsed.scheme.lower() == 'https' and port == 443)):
        host = f'{host}:{port}'
    path = parsed.path or '/'
    return urlunsplit((parsed.scheme.lower(), host, path, parsed.query, ''))


def _fingerprint(url: str) -> str | None:
    normalized = normalize_epg_source_url(url)
    return hashlib.sha256(normalized.encode()).hexdigest() if normalized else None


def _safe_hint_summary(url: str) -> str:
    normalized = normalize_epg_source_url(url)
    if not normalized:
        return 'invalid'
    parsed = urlsplit(normalized)
    query_keys = sorted({part.partition('=')[0][:48] for part in parsed.query.split('&') if part})
    path_digest = hashlib.sha256(parsed.path.encode()).hexdigest()[:12]
    summary = f'{parsed.scheme}://{parsed.netloc}/[path:{path_digest}]'
    if query_keys:
        summary += '?' + '&'.join(f'{key}=…' for key in query_keys[:12])
    return summary[:512]


@dataclass(frozen=True)
class SourceUrlResolution:
    status: str
    epg_source_id: int | None
    competing_source_ids: tuple[int, ...] = ()


def resolve_url_hint_to_sources(url: str, sources: Iterable[dict]) -> SourceUrlResolution:
    fingerprint = _fingerprint(url)
    if fingerprint is None:
        return SourceUrlResolution('unresolved', None)
    matches = sorted({int(source['id']) for source in sources if _fingerprint(str(source.get('url') or '')) == fingerprint})
    if not matches:
        return SourceUrlResolution('unresolved', None)
    if len(matches) > 1:
        return SourceUrlResolution('ambiguous_source', None, tuple(matches))
    return SourceUrlResolution('resolved', matches[0])


def _resolution_for_row(row: dict, sources: list[dict]) -> tuple[tuple[EpgSourcePreference, ...], str | None]:
    source_id = row['epg_source_id']
    if source_id is None:
        if row.get('resolution_status') == 'ambiguous_source':
            stored = {}
            try:
                stored = json.loads(row['evidence_json'] or '{}')
                source_ids = sorted({int(value) for value in stored.get('competing_source_ids', ()) if int(value) > 0})
            except (TypeError, ValueError, json.JSONDecodeError):
                source_ids = []
            preferences = tuple(
                EpgSourcePreference(
                    source_id,
                    str(row['origin']),
                    evidence=sanitize_evidence({'attribute_count': stored.get('attribute_count', 0)}),
                    subscription_id=int(row['subscription_id']) if row['subscription_id'] else None,
                )
                for source_id in source_ids
            )
            if len(preferences) > 1:
                return preferences, None
        return (), str(row.get('resolution_status') or 'unresolved')
    if row['origin'] == 'url_tvg':
        source = next((item for item in sources if int(item['id']) == int(source_id)), None)
        if source is None or _fingerprint(str(source.get('url') or '')) != row['hint_fingerprint']:
            return (), 'unresolved'
    evidence = sanitize_evidence(json.loads(row['evidence_json'] or '{}'))
    return (EpgSourcePreference(
        int(source_id), str(row['origin']), evidence=evidence,
        subscription_id=int(row['subscription_id']) if row['subscription_id'] else None,
        logical_channel_id=str(row['logical_channel_id'] or ''),
    ),), None


async def replace_subscription_url_tvg_evidence(subscription_id: int, hints: Iterable[tuple[str, str]]) -> dict:
    sources = await db.get_epg_sources()
    grouped: dict[str, dict] = {}
    for attribute, raw_url in hints:
        fingerprint = _fingerprint(raw_url)
        if not fingerprint:
            continue
        resolution = resolve_url_hint_to_sources(raw_url, sources)
        item = grouped.setdefault(fingerprint, {
            'summary': _safe_hint_summary(raw_url),
            'resolution': resolution,
            'attributes': set(),
        })
        item['attributes'].add(str(attribute))
    prepared = []
    for fingerprint, item in sorted(grouped.items()):
        evidence = {
            'attributes': ','.join(sorted(item['attributes'])),
            'attribute_count': len(item['attributes']),
        }
        resolution = item['resolution']
        if resolution.competing_source_ids:
            evidence['competing_source_ids'] = list(resolution.competing_source_ids[:16])
        prepared.append((
            fingerprint,
            item['summary'],
            resolution.status,
            resolution.epg_source_id,
            json.dumps(evidence, ensure_ascii=False, separators=(',', ':')),
        ))

    def _replace():
        conn = db._connect()
        try:
            now = datetime.now(timezone.utc).isoformat()
            with conn:
                conn.execute("DELETE FROM epg_source_preference_evidence WHERE subscription_id=? AND origin='url_tvg'", (subscription_id,))
                for fingerprint, summary, status, source_id, evidence_json in prepared:
                    conn.execute(
                        """INSERT INTO epg_source_preference_evidence(
                               subscription_id, origin, epg_source_id, hint_fingerprint, hint_summary,
                               resolution_status, evidence_json, created_at, updated_at
                           ) VALUES(?, 'url_tvg', ?, ?, ?, ?, ?, ?, ?)""",
                        (subscription_id, source_id, fingerprint, summary, status, evidence_json, now, now),
                    )
            return {'subscription_id': subscription_id, 'evidence_count': len(prepared)}
        finally:
            conn.close()
    return await asyncio.to_thread(_replace)


async def set_manual_source_preference(*, epg_source_id: int, subscription_id: int | None = None, logical_channel_id: str = '') -> None:
    def _set():
        conn = db._connect()
        try:
            now = datetime.now(timezone.utc).isoformat()
            with conn:
                if conn.execute('SELECT 1 FROM epg_sources WHERE id=?', (epg_source_id,)).fetchone() is None:
                    raise ValueError('EPG source does not exist')
                updated = conn.execute(
                    """UPDATE epg_source_preference_evidence
                       SET valid=1, current=1, updated_at=?
                       WHERE origin='manual' AND epg_source_id=?
                         AND COALESCE(subscription_id,-1)=COALESCE(?,-1)
                         AND logical_channel_id=?""",
                    (now, epg_source_id, subscription_id, logical_channel_id),
                )
                if updated.rowcount:
                    return
                conn.execute(
                    """INSERT INTO epg_source_preference_evidence(
                           subscription_id, logical_channel_id, origin, epg_source_id,
                           resolution_status, evidence_json, created_at, updated_at
                       ) VALUES(?, ?, 'manual', ?, 'manual', '{"explicit":true}', ?, ?)
                    """,
                    (subscription_id, logical_channel_id, epg_source_id, now, now),
                )
        finally:
            conn.close()
    await asyncio.to_thread(_set)


async def delete_manual_source_preference(*, epg_source_id: int, subscription_id: int | None = None, logical_channel_id: str = '') -> None:
    def _delete():
        conn = db._connect()
        try:
            with conn:
                conn.execute(
                    """DELETE FROM epg_source_preference_evidence
                       WHERE origin='manual' AND epg_source_id=? AND COALESCE(subscription_id,-1)=COALESCE(?,-1)
                         AND logical_channel_id=?""",
                    (epg_source_id, subscription_id, logical_channel_id),
                )
        finally:
            conn.close()
    await asyncio.to_thread(_delete)


async def set_subscription_manual_source_preference(
    *,
    subscription_id: int,
    epg_source_id: int,
) -> dict:
    """Replace one subscription-scoped manual preference atomically."""
    subscription_id = _positive_id(subscription_id, 'subscription_id')
    epg_source_id = _positive_id(epg_source_id, 'epg_source_id')

    def _set():
        conn = db._connect()
        try:
            conn.execute('BEGIN IMMEDIATE')
            if conn.execute(
                'SELECT 1 FROM subscriptions WHERE id=?',
                (subscription_id,),
            ).fetchone() is None:
                raise EpgPreferenceEvidenceError(
                    'preference_invalid',
                    '直播源订阅不存在',
                )
            if conn.execute(
                'SELECT 1 FROM epg_sources WHERE id=?',
                (epg_source_id,),
            ).fetchone() is None:
                raise EpgPreferenceEvidenceError(
                    'epg_source_not_found',
                    'EPG 来源不存在',
                )
            now = datetime.now(timezone.utc).isoformat()
            replaced_count = conn.execute(
                """
                DELETE FROM epg_source_preference_evidence
                WHERE origin='manual' AND subscription_id=?
                  AND logical_channel_id=''
                """,
                (subscription_id,),
            ).rowcount
            cursor = conn.execute(
                """
                INSERT INTO epg_source_preference_evidence(
                    subscription_id, logical_channel_id, origin,
                    epg_source_id, resolution_status, evidence_json,
                    created_at, updated_at
                ) VALUES(?, '', 'manual', ?, 'manual',
                         '{"explicit":true,"scope":"subscription"}', ?, ?)
                """,
                (subscription_id, epg_source_id, now, now),
            )
            row = conn.execute(
                """
                SELECT id, subscription_id, logical_channel_id, origin,
                       epg_source_id, resolution_status, valid, current,
                       created_at, updated_at
                FROM epg_source_preference_evidence WHERE id=?
                """,
                (cursor.lastrowid,),
            ).fetchone()
            conn.commit()
            return {
                **dict(row),
                'replaced_manual_count': int(replaced_count),
            }
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    return await asyncio.to_thread(_set)


async def clear_subscription_manual_source_preference(
    *,
    subscription_id: int,
) -> dict:
    """Clear only manual evidence; all derived evidence remains untouched."""
    subscription_id = _positive_id(subscription_id, 'subscription_id')

    def _clear():
        conn = db._connect()
        try:
            conn.execute('BEGIN IMMEDIATE')
            if conn.execute(
                'SELECT 1 FROM subscriptions WHERE id=?',
                (subscription_id,),
            ).fetchone() is None:
                raise EpgPreferenceEvidenceError(
                    'preference_invalid',
                    '直播源订阅不存在',
                )
            cleared_count = conn.execute(
                """
                DELETE FROM epg_source_preference_evidence
                WHERE origin='manual' AND subscription_id=?
                  AND logical_channel_id=''
                """,
                (subscription_id,),
            ).rowcount
            derived_count = int(conn.execute(
                """
                SELECT COUNT(*) FROM epg_source_preference_evidence
                WHERE origin<>'manual' AND subscription_id=?
                  AND valid=1 AND current=1
                """,
                (subscription_id,),
            ).fetchone()[0])
            conn.commit()
            return {
                'subscription_id': subscription_id,
                'cleared_manual_count': int(cleared_count),
                'preserved_derived_count': derived_count,
            }
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    return await asyncio.to_thread(_clear)


async def reconcile_url_tvg_source_resolutions() -> dict:
    """Re-resolve persisted URL fingerprints against the current source set.

    This does not need the original secret-bearing URL: endpoint identity is
    the persisted deterministic fingerprint. Manual evidence is deliberately
    excluded so deleting a referenced source remains auditable as
    ``missing_source`` instead of silently selecting another source.
    """

    def _reconcile():
        conn = db._connect()
        try:
            conn.execute('BEGIN IMMEDIATE')
            sources = [dict(row) for row in conn.execute(
                'SELECT id, url FROM epg_sources ORDER BY id'
            ).fetchall()]
            sources_by_fingerprint: dict[str, list[int]] = {}
            for source in sources:
                fingerprint = _fingerprint(str(source.get('url') or ''))
                if fingerprint:
                    sources_by_fingerprint.setdefault(fingerprint, []).append(
                        int(source['id'])
                    )

            rows = conn.execute(
                """
                SELECT * FROM epg_source_preference_evidence
                WHERE origin='url_tvg' AND valid=1 AND current=1
                ORDER BY id
                """
            ).fetchall()
            changed_count = 0
            counts = {'resolved': 0, 'unresolved': 0, 'ambiguous_source': 0}
            now = datetime.now(timezone.utc).isoformat()
            for row in rows:
                matches = sorted(set(sources_by_fingerprint.get(
                    str(row['hint_fingerprint'] or ''),
                    (),
                )))
                evidence = {}
                try:
                    decoded = json.loads(row['evidence_json'] or '{}')
                    if isinstance(decoded, dict):
                        evidence = decoded
                except json.JSONDecodeError:
                    evidence = {}
                evidence.pop('competing_source_ids', None)
                if len(matches) == 1:
                    status = 'resolved'
                    source_id = matches[0]
                elif len(matches) > 1:
                    status = 'ambiguous_source'
                    source_id = None
                    evidence['competing_source_ids'] = matches[:16]
                else:
                    status = 'unresolved'
                    source_id = None
                encoded = json.dumps(
                    evidence,
                    ensure_ascii=False,
                    separators=(',', ':'),
                    sort_keys=True,
                )
                counts[status] += 1
                if (
                    row['epg_source_id'] != source_id
                    or row['resolution_status'] != status
                    or row['evidence_json'] != encoded
                ):
                    conn.execute(
                        """
                        UPDATE epg_source_preference_evidence
                        SET epg_source_id=?, resolution_status=?,
                            evidence_json=?, updated_at=?
                        WHERE id=?
                        """,
                        (source_id, status, encoded, now, row['id']),
                    )
                    changed_count += 1
            conn.commit()
            return {
                'evidence_count': len(rows),
                'changed_count': changed_count,
                **counts,
            }
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    return await asyncio.to_thread(_reconcile)


@dataclass(frozen=True)
class PreferenceSnapshotItem:
    subscription_id: int | None
    logical_channel_id: str
    resolution: EpgSourcePreferenceResolution
    evidence_count: int


async def _load_preference_inputs() -> tuple[list[dict], list[dict]]:
    def _load():
        conn = db._connect()
        try:
            sources = [dict(row) for row in conn.execute('SELECT id, url, enabled, last_status FROM epg_sources').fetchall()]
            evidence = [dict(row) for row in conn.execute(
                "SELECT * FROM epg_source_preference_evidence WHERE valid=1 AND current=1 ORDER BY subscription_id, id"
            ).fetchall()]
            return sources, evidence
        finally:
            conn.close()
    return await asyncio.to_thread(_load)


async def build_epg_source_preference_snapshot() -> tuple[PreferenceSnapshotItem, ...]:
    """Load all sources/evidence in two queries, then resolve contexts in memory."""
    sources, rows = await _load_preference_inputs()
    states = [EpgSourceState(int(row['id']), bool(row['enabled']), str(row['last_status'] or '')) for row in sources]
    grouped: dict[tuple[int | None, str], list[EpgSourcePreference]] = {}
    invalid_contexts: set[tuple[int | None, str]] = set()
    for row in rows:
        subscription_id = int(row['subscription_id']) if row['subscription_id'] else None
        logical_channel_id = str(row['logical_channel_id'] or '')
        context = (subscription_id, logical_channel_id)
        preferences, invalid_reason = _resolution_for_row(row, sources)
        if invalid_reason:
            invalid_contexts.add(context)
        if preferences:
            grouped.setdefault(context, []).extend(preferences)
    result = []
    for subscription_id, logical_channel_id in sorted(grouped.keys() | invalid_contexts, key=lambda item: (item[0] or 0, item[1])):
        resolution = resolve_epg_source_preference(
            subscription_id=subscription_id or None,
            logical_channel_id=logical_channel_id,
            available_sources=states,
            preferences=grouped.get((subscription_id, logical_channel_id), ()),
        )
        if (subscription_id, logical_channel_id) in invalid_contexts and resolution.status == 'none':
            resolution = EpgSourcePreferenceResolution(None, 'missing_source')
        result.append(PreferenceSnapshotItem(
            subscription_id, logical_channel_id, resolution,
            len(grouped.get((subscription_id, logical_channel_id), ())) +
            int((subscription_id, logical_channel_id) in invalid_contexts),
        ))
    return tuple(result)


async def maintain_subscription_preference_evidence(subscription_id: int, hints: Iterable[tuple[str, str]]) -> bool:
    try:
        await replace_subscription_url_tvg_evidence(subscription_id, hints)
        return True
    except asyncio.CancelledError:
        raise
    except Exception:
        return False
