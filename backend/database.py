import asyncio
import sqlite3
import os
import json
import time
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

from epg_source_model import (
    BUILTIN_CHINA_EPG_PRESET,
    validate_builtin_key,
    validate_epg_source_name,
    validate_epg_source_origin,
    validate_epg_source_url,
)
from security.source_ids import source_revision_for


AUTOMATION_ERROR_MAX_LENGTH = 2048
EPG_ERROR_MAX_LENGTH = 1024
AUTOMATION_STATUSES = {
    'never_run',
    'running',
    'success',
    'partial',
    'failed',
    'cancelled',
    'interrupted',
}
AUTOMATION_FINAL_STATUSES = AUTOMATION_STATUSES - {'never_run', 'running'}
MARKET_VERSION_STATUSES = {'same', 'upgrade', 'downgrade', 'different', 'unknown'}
MARKET_UPDATE_FINAL_STATUSES = {'success', 'failed', 'cancelled', 'interrupted'}
MARKET_AUTOMATION_TASK_ID = 'market_auto_update'
MARKET_AUTOMATION_CONFLICT_GROUP = 'market'
MARKET_AUTOMATION_INTERVAL_SECONDS = 86400
RADIO_CATALOG_DEFAULT_STALE_GRACE_SECONDS = 24 * 60 * 60
DATABASE_BUSY_TIMEOUT_MS = 30_000
_INITIALIZE_LOCK = threading.Lock()

DB_PATH_RAW = (
    os.environ.get('WAVEFLOW_DB_PATH')
    or os.path.join(os.path.dirname(__file__), 'data', 'waveflow.db')
)
# 保留 import-time 默认值供现有调用方查看；每次连接仍会重新读取显式
# WAVEFLOW_DB_PATH。这样测试/嵌入式启动器在模块已被其他组件缓存后切换
# 隔离数据库，不会让旧模块继续写入先前或默认数据库。
if DB_PATH_RAW == ":memory:":
    DB_PATH = "file:waveflow?mode=memory&cache=shared"
else:
    DB_PATH = DB_PATH_RAW
_IMPORTED_DB_PATH = DB_PATH

_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS app_settings (
    key        TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by INTEGER
);

CREATE TABLE IF NOT EXISTS automation_task_config (
    task_id          TEXT PRIMARY KEY,
    conflict_group   TEXT NOT NULL,
    enabled          INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
    interval_seconds INTEGER NOT NULL CHECK(interval_seconds > 0),
    updated_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS automation_task_state (
    task_id          TEXT PRIMARY KEY,
    conflict_group   TEXT NOT NULL,
    task_type        TEXT DEFAULT '',
    run_token        TEXT DEFAULT '',
    last_started_at  TEXT DEFAULT '',
    last_finished_at TEXT DEFAULT '',
    last_status      TEXT NOT NULL DEFAULT 'never_run'
                     CHECK(last_status IN (
                         'never_run', 'running', 'success', 'partial',
                         'failed', 'cancelled', 'interrupted'
                     )),
    checked_count    INTEGER NOT NULL DEFAULT 0 CHECK(checked_count >= 0),
    updated_count    INTEGER NOT NULL DEFAULT 0 CHECK(updated_count >= 0),
    skipped_count    INTEGER NOT NULL DEFAULT 0 CHECK(skipped_count >= 0),
    failed_count     INTEGER NOT NULL DEFAULT 0 CHECK(failed_count >= 0),
    last_error       TEXT DEFAULT '',
    FOREIGN KEY (task_id) REFERENCES automation_task_config(task_id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_automation_running_conflict_group
ON automation_task_state(conflict_group)
WHERE last_status = 'running';

CREATE TABLE IF NOT EXISTS subscriptions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    title         TEXT NOT NULL,
    url           TEXT NOT NULL UNIQUE,
    channel_count INTEGER DEFAULT 0,
    valid         INTEGER DEFAULT 1,
    last_updated  TEXT DEFAULT '',
    created_at    TEXT NOT NULL,
    custom_ua     TEXT DEFAULT '',
    force_proxy   INTEGER DEFAULT 0,
    last_tested   TEXT DEFAULT '',
    refresh_generation INTEGER NOT NULL DEFAULT 0,
    last_attempt_at TEXT DEFAULT '',
    last_success_at TEXT DEFAULT '',
    last_refresh_status TEXT NOT NULL DEFAULT 'never',
    last_error TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS channels (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    subscription_id INTEGER NOT NULL,
    name            TEXT NOT NULL,
    url             TEXT NOT NULL,
    logo_url        TEXT DEFAULT '',
    group_name      TEXT DEFAULT '',
    tvg_id          TEXT DEFAULT '',
    tvg_name        TEXT DEFAULT '',
    is_working      INTEGER DEFAULT 0,
    latency_ms      REAL DEFAULT 0,
    last_tested     TEXT DEFAULT '',
    source_type     TEXT DEFAULT 'hls',
    youtube_video_id TEXT DEFAULT '',
    referer         TEXT DEFAULT '',
    custom_ua       TEXT DEFAULT '',
    force_proxy     INTEGER DEFAULT 0,
    probe_status    TEXT DEFAULT 'untested',
    live_status     TEXT DEFAULT 'unknown',
    probe_method    TEXT DEFAULT '',
    speed_mbps      REAL DEFAULT 0,
    resolution      TEXT DEFAULT '',
    fps             REAL DEFAULT 0,
    video_codec     TEXT DEFAULT '',
    audio_codec     TEXT DEFAULT '',
    requires_headers INTEGER DEFAULT 0,
    requires_proxy_declared INTEGER DEFAULT 0,
    proxy_required_hint INTEGER DEFAULT 0,
    last_success_at TEXT DEFAULT '',
    last_error      TEXT DEFAULT '',
    adapter_provider TEXT DEFAULT '',
    adapter_title   TEXT DEFAULT '',
    probe_meta_json TEXT DEFAULT '{}',
    market_package_id TEXT DEFAULT '',
    market_source_id TEXT DEFAULT '',
    market_channel_id TEXT DEFAULT '',
    market_source_item_id TEXT DEFAULT '',
    rtsp_timestamp_mode TEXT NOT NULL DEFAULT 'passthrough'
                        CHECK(rtsp_timestamp_mode IN ('passthrough', 'pts_from_dts')),
    FOREIGN KEY (subscription_id) REFERENCES subscriptions(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_channels_sub ON channels(subscription_id);
CREATE INDEX IF NOT EXISTS idx_channels_name ON channels(name);

CREATE TABLE IF NOT EXISTS iptv_logical_channels (
    id            TEXT PRIMARY KEY,
    canonical_key TEXT NOT NULL,
    display_name  TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'active'
                  CHECK(status IN ('active', 'orphaned', 'split_conflict', 'merge_conflict')),
    orphaned_at   TEXT DEFAULT NULL,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_iptv_logical_channels_key
ON iptv_logical_channels(canonical_key);
CREATE INDEX IF NOT EXISTS idx_iptv_logical_channels_status
ON iptv_logical_channels(status);

CREATE TABLE IF NOT EXISTS iptv_logical_channel_members (
    logical_channel_id    TEXT NOT NULL,
    channel_id            INTEGER NOT NULL UNIQUE,
    membership_reason     TEXT NOT NULL DEFAULT 'normalized_name',
    membership_confidence INTEGER NOT NULL DEFAULT 100
                          CHECK(membership_confidence BETWEEN 0 AND 100),
    variant_type          TEXT NOT NULL DEFAULT 'unknown'
                          CHECK(variant_type IN (
                              'unknown', 'standard', 'hd', '4k', 'delayed',
                              'region', 'international'
                          )),
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL,
    PRIMARY KEY(logical_channel_id, channel_id),
    FOREIGN KEY(logical_channel_id) REFERENCES iptv_logical_channels(id) ON DELETE CASCADE,
    FOREIGN KEY(channel_id) REFERENCES channels(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_iptv_logical_members_logical
ON iptv_logical_channel_members(logical_channel_id);
CREATE INDEX IF NOT EXISTS idx_iptv_logical_members_channel
ON iptv_logical_channel_members(channel_id);

CREATE TABLE IF NOT EXISTS iptv_logical_channel_epg_bindings (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    logical_channel_id  TEXT NOT NULL,
    epg_source_id       INTEGER NOT NULL,
    epg_channel_id      TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'matched'
                        CHECK(status IN (
                            'matched', 'ambiguous', 'unmatched',
                            'not_applicable', 'conflict', 'orphan_target'
                        )),
    match_type          TEXT NOT NULL DEFAULT '',
    confidence          INTEGER NOT NULL DEFAULT 0
                        CHECK(confidence BETWEEN 0 AND 100),
    locked              INTEGER NOT NULL DEFAULT 0
                        CHECK(locked IN (0, 1)),
    origin              TEXT NOT NULL DEFAULT 'manual'
                        CHECK(origin IN ('legacy_migrated', 'automatic', 'manual')),
    shadow_run_id       TEXT DEFAULT NULL,
    legacy_canonical_key TEXT NOT NULL DEFAULT '',
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    UNIQUE(logical_channel_id),
    FOREIGN KEY(logical_channel_id)
        REFERENCES iptv_logical_channels(id)
);
CREATE INDEX IF NOT EXISTS idx_iptv_logical_epg_bindings_target
ON iptv_logical_channel_epg_bindings(epg_source_id, epg_channel_id);
CREATE INDEX IF NOT EXISTS idx_iptv_logical_epg_bindings_status
ON iptv_logical_channel_epg_bindings(status);

CREATE TABLE IF NOT EXISTS iptv_logical_channel_epg_policies (
    logical_channel_id  TEXT PRIMARY KEY,
    mode                TEXT NOT NULL
                        CHECK(mode IN ('automatic', 'no_epg')),
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    FOREIGN KEY(logical_channel_id)
        REFERENCES iptv_logical_channels(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_iptv_logical_epg_policies_mode
ON iptv_logical_channel_epg_policies(mode);

CREATE TABLE IF NOT EXISTS epg_match_shadow_runs (
    run_id                       TEXT PRIMARY KEY,
    status                       TEXT NOT NULL
                                 CHECK(status IN (
                                     'running', 'success', 'partial',
                                     'failed', 'cancelled'
                                 )),
    started_at                   TEXT NOT NULL,
    finished_at                  TEXT DEFAULT '',
    logical_channel_count       INTEGER NOT NULL DEFAULT 0 CHECK(logical_channel_count >= 0),
    raw_member_count            INTEGER NOT NULL DEFAULT 0 CHECK(raw_member_count >= 0),
    epg_source_count            INTEGER NOT NULL DEFAULT 0 CHECK(epg_source_count >= 0),
    catalog_channel_count       INTEGER NOT NULL DEFAULT 0 CHECK(catalog_channel_count >= 0),
    existing_binding_count      INTEGER NOT NULL DEFAULT 0 CHECK(existing_binding_count >= 0),
    matched_count               INTEGER NOT NULL DEFAULT 0 CHECK(matched_count >= 0),
    ambiguous_count             INTEGER NOT NULL DEFAULT 0 CHECK(ambiguous_count >= 0),
    unmatched_count             INTEGER NOT NULL DEFAULT 0 CHECK(unmatched_count >= 0),
    conflict_count              INTEGER NOT NULL DEFAULT 0 CHECK(conflict_count >= 0),
    not_applicable_count        INTEGER NOT NULL DEFAULT 0 CHECK(not_applicable_count >= 0),
    locked_preserved_count      INTEGER NOT NULL DEFAULT 0 CHECK(locked_preserved_count >= 0),
    existing_preserved_count    INTEGER NOT NULL DEFAULT 0 CHECK(existing_preserved_count >= 0),
    stale_only_count            INTEGER NOT NULL DEFAULT 0 CHECK(stale_only_count >= 0),
    candidate_count             INTEGER NOT NULL DEFAULT 0 CHECK(candidate_count >= 0),
    source_revision_summary_json TEXT NOT NULL DEFAULT '[]',
    preference_snapshot_fingerprint TEXT NOT NULL DEFAULT '',
    error                       TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_epg_match_shadow_runs_status_finished
ON epg_match_shadow_runs(status, finished_at);

CREATE TABLE IF NOT EXISTS epg_match_shadow_decisions (
    run_id                     TEXT NOT NULL,
    logical_channel_id         TEXT NOT NULL,
    status                     TEXT NOT NULL,
    selected_epg_source_id     INTEGER,
    selected_epg_channel_id    TEXT,
    match_type                 TEXT NOT NULL DEFAULT '',
    confidence                 INTEGER NOT NULL DEFAULT 0 CHECK(confidence BETWEEN 0 AND 100),
    existing_binding_action    TEXT NOT NULL DEFAULT 'none',
    reasons_json               TEXT NOT NULL DEFAULT '[]',
    hint_conflicts_json        TEXT NOT NULL DEFAULT '[]',
    candidate_count            INTEGER NOT NULL DEFAULT 0 CHECK(candidate_count >= 0),
    error                      TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(run_id, logical_channel_id),
    FOREIGN KEY(run_id) REFERENCES epg_match_shadow_runs(run_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_epg_match_shadow_decisions_status
ON epg_match_shadow_decisions(run_id, status);
CREATE INDEX IF NOT EXISTS idx_epg_match_shadow_decisions_logical
ON epg_match_shadow_decisions(logical_channel_id, run_id);

CREATE TABLE IF NOT EXISTS epg_match_shadow_candidates (
    run_id              TEXT NOT NULL,
    logical_channel_id  TEXT NOT NULL,
    rank                INTEGER NOT NULL CHECK(rank > 0),
    epg_source_id       INTEGER NOT NULL,
    epg_channel_id      TEXT NOT NULL,
    match_type          TEXT NOT NULL DEFAULT '',
    confidence          INTEGER NOT NULL DEFAULT 0 CHECK(confidence BETWEEN 0 AND 100),
    source_status       TEXT NOT NULL DEFAULT '',
    source_enabled      INTEGER NOT NULL DEFAULT 0 CHECK(source_enabled IN (0, 1)),
    auto_applicable     INTEGER NOT NULL DEFAULT 0 CHECK(auto_applicable IN (0, 1)),
    evidence_json       TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY(run_id, logical_channel_id, rank),
    FOREIGN KEY(run_id) REFERENCES epg_match_shadow_runs(run_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_epg_match_shadow_candidates_identity
ON epg_match_shadow_candidates(run_id, epg_source_id, epg_channel_id);
CREATE INDEX IF NOT EXISTS idx_epg_match_shadow_candidates_logical
ON epg_match_shadow_candidates(logical_channel_id, run_id);

CREATE TABLE IF NOT EXISTS epg_sources (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    url             TEXT NOT NULL UNIQUE,
    enabled         INTEGER DEFAULT 1,
    revision        INTEGER NOT NULL DEFAULT 1,
    last_fetched_at TEXT DEFAULT '',
    last_attempt_at TEXT DEFAULT '',
    last_success_at TEXT DEFAULT '',
    last_status     TEXT DEFAULT '',
    last_error      TEXT DEFAULT '',
    channel_count   INTEGER NOT NULL DEFAULT 0,
    programme_count INTEGER NOT NULL DEFAULT 0,
    data_start_at   TEXT DEFAULT '',
    data_end_at     TEXT DEFAULT '',
    source_origin   TEXT NOT NULL DEFAULT 'custom'
                    CHECK(source_origin IN ('builtin', 'custom')),
    builtin_key     TEXT DEFAULT NULL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    CHECK(
        (source_origin='builtin' AND builtin_key IS NOT NULL AND builtin_key<>'')
        OR (source_origin='custom' AND builtin_key IS NULL)
    )
);

CREATE TABLE IF NOT EXISTS epg_source_preference_evidence (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    subscription_id     INTEGER,
    logical_channel_id  TEXT NOT NULL DEFAULT '',
    origin              TEXT NOT NULL CHECK(origin IN ('manual', 'subscription', 'market', 'url_tvg')),
    epg_source_id       INTEGER,
    hint_fingerprint    TEXT NOT NULL DEFAULT '',
    hint_summary        TEXT NOT NULL DEFAULT '',
    resolution_status   TEXT NOT NULL CHECK(resolution_status IN ('resolved', 'unresolved', 'ambiguous_source', 'manual')),
    evidence_json       TEXT NOT NULL DEFAULT '{}',
    valid               INTEGER NOT NULL DEFAULT 1 CHECK(valid IN (0, 1)),
    current             INTEGER NOT NULL DEFAULT 1 CHECK(current IN (0, 1)),
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    FOREIGN KEY(subscription_id) REFERENCES subscriptions(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_epg_preference_evidence_subscription
ON epg_source_preference_evidence(subscription_id, origin, current);
CREATE INDEX IF NOT EXISTS idx_epg_preference_evidence_source
ON epg_source_preference_evidence(epg_source_id, current);
CREATE INDEX IF NOT EXISTS idx_epg_preference_evidence_logical
ON epg_source_preference_evidence(logical_channel_id, origin);
CREATE UNIQUE INDEX IF NOT EXISTS idx_epg_preference_evidence_derived_unique
ON epg_source_preference_evidence(subscription_id, origin, hint_fingerprint, logical_channel_id)
WHERE origin <> 'manual';
CREATE UNIQUE INDEX IF NOT EXISTS idx_epg_preference_evidence_manual_unique
ON epg_source_preference_evidence(
    COALESCE(subscription_id, -1), logical_channel_id, epg_source_id
) WHERE origin = 'manual';

CREATE TABLE IF NOT EXISTS epg_channels (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id        INTEGER NOT NULL REFERENCES epg_sources(id) ON DELETE CASCADE,
    channel_id       TEXT NOT NULL,
    display_names    TEXT NOT NULL,
    normalized_names TEXT NOT NULL,
    UNIQUE(source_id, channel_id)
);
CREATE INDEX IF NOT EXISTS idx_epg_channels_src_ch ON epg_channels(source_id, channel_id);

CREATE TABLE IF NOT EXISTS epg_programs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id   INTEGER NOT NULL REFERENCES epg_sources(id) ON DELETE CASCADE,
    channel_id  TEXT NOT NULL,
    start       TEXT NOT NULL,
    stop        TEXT NOT NULL,
    title       TEXT NOT NULL,
    description TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_epg_programs_ch_time ON epg_programs(source_id, channel_id, start, stop);

CREATE TABLE IF NOT EXISTS market_packages_installed (
    package_id                TEXT PRIMARY KEY,
    market_url                TEXT DEFAULT '',
    installed_subscription_id INTEGER,
    installed_version         TEXT DEFAULT '',
    installed_at              TEXT NOT NULL,
    auto_update               INTEGER DEFAULT 0,
    metadata_json             TEXT DEFAULT '',
    last_checked_at           TEXT DEFAULT '',
    remote_version            TEXT DEFAULT '',
    version_status            TEXT DEFAULT 'unknown',
    last_update_started_at    TEXT DEFAULT '',
    last_update_finished_at   TEXT DEFAULT '',
    last_update_status        TEXT DEFAULT 'never_run',
    last_update_error         TEXT DEFAULT '',
    last_update_run_token     TEXT DEFAULT '',
    FOREIGN KEY (installed_subscription_id) REFERENCES subscriptions(id) ON DELETE SET NULL
);

-- Package-scoped visual assets are deliberately separate from channel/source
-- rows.  A package update replaces the active version's rows as one SQLite
-- transaction; the filesystem path is only an implementation detail of the
-- verified package asset store.
CREATE TABLE IF NOT EXISTS package_assets (
    package_id      TEXT NOT NULL,
    asset_id        TEXT NOT NULL,
    package_version TEXT NOT NULL,
    relative_path   TEXT NOT NULL,
    media_type      TEXT NOT NULL,
    sha256          TEXT NOT NULL,
    size_bytes      INTEGER NOT NULL CHECK(size_bytes > 0),
    stored_path     TEXT NOT NULL,
    state           TEXT NOT NULL DEFAULT 'active'
                    CHECK(state IN ('staged', 'active', 'retained')),
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    PRIMARY KEY(package_id, asset_id, package_version)
);
CREATE INDEX IF NOT EXISTS idx_package_assets_active
ON package_assets(package_id, package_version, state);

CREATE TABLE IF NOT EXISTS logical_channel_logo_bindings (
    logical_channel_id TEXT NOT NULL,
    package_id         TEXT NOT NULL,
    asset_id           TEXT NOT NULL,
    binding_type       TEXT NOT NULL
                       CHECK(binding_type IN ('content_package', 'logo_pack')),
    match_type         TEXT NOT NULL
                       CHECK(match_type IN ('stable_identity', 'exact', 'alias', 'normalized')),
    match_key          TEXT NOT NULL,
    priority           INTEGER NOT NULL DEFAULT 0,
    package_version    TEXT NOT NULL,
    enabled            INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
    lifecycle_state    TEXT NOT NULL DEFAULT 'active'
                       CHECK(lifecycle_state IN ('active', 'retained', 'revoked')),
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    PRIMARY KEY(logical_channel_id, package_id, asset_id, match_type),
    FOREIGN KEY(logical_channel_id) REFERENCES iptv_logical_channels(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_logical_logo_bindings_lookup
ON logical_channel_logo_bindings(logical_channel_id, enabled, lifecycle_state, priority);

CREATE TABLE IF NOT EXISTS market_sources (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_key      TEXT NOT NULL UNIQUE,
    name            TEXT NOT NULL,
    url             TEXT NOT NULL UNIQUE,
    enabled         INTEGER DEFAULT 1,
    allow_private   INTEGER DEFAULT 0,
    is_builtin      INTEGER DEFAULT 0,
    last_fetched_at TEXT DEFAULT '',
    last_status     TEXT DEFAULT '',
    last_error      TEXT DEFAULT '',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plugin_installations (
    publisher_id          TEXT NOT NULL,
    plugin_id             TEXT NOT NULL,
    installed_version     TEXT NOT NULL,
    active_version        TEXT DEFAULT '',
    candidate_version     TEXT DEFAULT '',
    enabled               INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
    trust_state           TEXT NOT NULL,
    trust_class           TEXT NOT NULL DEFAULT 'official'
                          CHECK(trust_class IN ('official', 'developer_local')),
    source_key            TEXT DEFAULT '',
    source_package_id     TEXT DEFAULT '',
    manifest_json         TEXT NOT NULL,
    manifest_sha256       TEXT NOT NULL,
    manifest_signature_json TEXT NOT NULL DEFAULT '{}',
    artifact_sha256       TEXT NOT NULL,
    artifact_path         TEXT NOT NULL,
    runtime_type          TEXT NOT NULL,
    entrypoint            TEXT NOT NULL,
    platform_os           TEXT NOT NULL,
    platform_arch         TEXT NOT NULL,
    lifecycle_state       TEXT NOT NULL DEFAULT 'installed',
    quarantined           INTEGER NOT NULL DEFAULT 0 CHECK(quarantined IN (0, 1)),
    last_activation_status TEXT DEFAULT '',
    last_error            TEXT DEFAULT '',
    retained_state_until  TEXT DEFAULT '',
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL,
    PRIMARY KEY (publisher_id, plugin_id)
);

CREATE TABLE IF NOT EXISTS plugin_artifacts (
    publisher_id      TEXT NOT NULL,
    plugin_id         TEXT NOT NULL,
    version           TEXT NOT NULL,
    platform_os       TEXT NOT NULL,
    platform_arch     TEXT NOT NULL,
    sha256            TEXT NOT NULL,
    path              TEXT NOT NULL,
    runtime_type      TEXT NOT NULL,
    entrypoint        TEXT NOT NULL,
    state             TEXT NOT NULL,
    source_key        TEXT DEFAULT '',
    source_package_id TEXT DEFAULT '',
    created_at        TEXT NOT NULL,
    PRIMARY KEY (publisher_id, plugin_id, version, platform_os, platform_arch, sha256)
);
CREATE INDEX IF NOT EXISTS idx_plugin_artifacts_state
ON plugin_artifacts(publisher_id, plugin_id, state);

CREATE TABLE IF NOT EXISTS plugin_dependency_artifacts (
    sha256        TEXT PRIMARY KEY,
    package_name  TEXT NOT NULL,
    version       TEXT NOT NULL,
    filename      TEXT NOT NULL,
    size_bytes    INTEGER NOT NULL,
    python_tag    TEXT NOT NULL,
    abi_tag       TEXT NOT NULL,
    platform_tag  TEXT NOT NULL,
    path          TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plugin_python_environments (
    publisher_id    TEXT NOT NULL,
    plugin_id       TEXT NOT NULL,
    plugin_version  TEXT NOT NULL,
    runtime_identity TEXT NOT NULL,
    lock_digest     TEXT NOT NULL,
    path            TEXT NOT NULL,
    state           TEXT NOT NULL CHECK(state IN ('candidate', 'active', 'retained')),
    dependency_count INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (publisher_id, plugin_id, plugin_version, runtime_identity, lock_digest)
);

CREATE TABLE IF NOT EXISTS plugin_environment_dependencies (
    publisher_id     TEXT NOT NULL,
    plugin_id        TEXT NOT NULL,
    plugin_version   TEXT NOT NULL,
    runtime_identity TEXT NOT NULL,
    lock_digest      TEXT NOT NULL,
    artifact_sha256  TEXT NOT NULL,
    PRIMARY KEY (publisher_id, plugin_id, plugin_version, runtime_identity, lock_digest, artifact_sha256)
);
CREATE INDEX IF NOT EXISTS idx_plugin_python_environments_state
ON plugin_python_environments(publisher_id, plugin_id, state);

CREATE TABLE IF NOT EXISTS plugin_publisher_trust (
    publisher_id TEXT NOT NULL,
    key_id       TEXT NOT NULL,
    public_key   TEXT NOT NULL,
    trust_level  TEXT NOT NULL CHECK(trust_level IN ('official', 'third_party')),
    enabled      INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
    description  TEXT DEFAULT '',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (publisher_id, key_id)
);

CREATE TABLE IF NOT EXISTS plugin_permission_approvals (
    publisher_id           TEXT NOT NULL,
    plugin_id              TEXT NOT NULL,
    permission_name       TEXT NOT NULL,
    permission_fingerprint TEXT NOT NULL,
    approved              INTEGER NOT NULL DEFAULT 0 CHECK(approved IN (0, 1)),
    approved_at            TEXT DEFAULT '',
    approved_by            TEXT DEFAULT '',
    revoked_at             TEXT DEFAULT '',
    revoked_by             TEXT DEFAULT '',
    manifest_version       TEXT DEFAULT '',
    updated_at             TEXT NOT NULL,
    PRIMARY KEY (publisher_id, plugin_id, permission_name, permission_fingerprint)
);

CREATE TABLE IF NOT EXISTS plugin_scheme_ownership (
    scheme       TEXT PRIMARY KEY,
    mode         TEXT NOT NULL CHECK(mode IN ('legacy', 'plugin', 'migration_test')),
    plugin_identity TEXT DEFAULT '',
    updated_at   TEXT NOT NULL
);

-- Radio is a separate domain.  A station is an explicit user-visible
-- container; its source identity is owner/provider/station based and never
-- inferred from a name, URL, frequency, or another IPTV row.
CREATE TABLE IF NOT EXISTS radio_stations (
    station_id             TEXT PRIMARY KEY,
    owner_identity         TEXT NOT NULL,
    provider_key           TEXT NOT NULL,
    provider_station_id    TEXT NOT NULL,
    name                   TEXT NOT NULL,
    logo_url               TEXT NOT NULL DEFAULT '',
    group_name             TEXT NOT NULL DEFAULT '',
    country                TEXT NOT NULL DEFAULT '',
    language               TEXT NOT NULL DEFAULT '',
    frequency              TEXT NOT NULL DEFAULT '',
    metadata_json          TEXT NOT NULL DEFAULT '{}',
    lifecycle_state        TEXT NOT NULL DEFAULT 'active'
                           CHECK(lifecycle_state IN ('active', 'stale', 'expired')),
    catalog_ttl_seconds    INTEGER NOT NULL DEFAULT 300 CHECK(catalog_ttl_seconds > 0),
    catalog_expires_at     REAL NOT NULL DEFAULT 0,
    last_catalog_success_at TEXT NOT NULL DEFAULT '',
    last_catalog_error     TEXT NOT NULL DEFAULT '',
    created_at             TEXT NOT NULL,
    updated_at             TEXT NOT NULL,
    UNIQUE(owner_identity, provider_key, provider_station_id)
);
CREATE INDEX IF NOT EXISTS idx_radio_stations_lifecycle
ON radio_stations(lifecycle_state, catalog_expires_at);

CREATE TABLE IF NOT EXISTS radio_station_sources (
    source_id              TEXT PRIMARY KEY,
    station_id             TEXT NOT NULL,
    owner_identity         TEXT NOT NULL,
    provider_key           TEXT NOT NULL,
    provider_station_id    TEXT NOT NULL,
    source_discriminator   TEXT NOT NULL DEFAULT '',
    reference_json         TEXT NOT NULL,
    source_revision        TEXT NOT NULL,
    explicit_priority      INTEGER NOT NULL DEFAULT 0,
    health_status          TEXT NOT NULL DEFAULT 'unknown'
                           CHECK(health_status IN ('unknown', 'healthy', 'unhealthy')),
    last_success_at        TEXT NOT NULL DEFAULT '',
    last_error             TEXT NOT NULL DEFAULT '',
    lifecycle_state        TEXT NOT NULL DEFAULT 'active'
                           CHECK(lifecycle_state IN ('active', 'stale', 'expired')),
    catalog_ttl_seconds    INTEGER NOT NULL DEFAULT 300 CHECK(catalog_ttl_seconds > 0),
    catalog_expires_at     REAL NOT NULL DEFAULT 0,
    resolve_expires_at     REAL NOT NULL DEFAULT 0,
    created_at             TEXT NOT NULL,
    updated_at             TEXT NOT NULL,
    UNIQUE(owner_identity, provider_key, provider_station_id, source_discriminator),
    UNIQUE(station_id, source_id),
    FOREIGN KEY(station_id) REFERENCES radio_stations(station_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_radio_station_sources_station
ON radio_station_sources(station_id, explicit_priority, source_id);
CREATE INDEX IF NOT EXISTS idx_radio_station_sources_lifecycle
ON radio_station_sources(lifecycle_state, catalog_expires_at);

CREATE TABLE IF NOT EXISTS radio_catalog_states (
    owner_identity          TEXT PRIMARY KEY,
    generation              INTEGER NOT NULL DEFAULT 0,
    published_generation    INTEGER NOT NULL DEFAULT 0,
    last_success_at         TEXT NOT NULL DEFAULT '',
    last_error              TEXT NOT NULL DEFAULT '',
    updated_at              TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_radio_catalog_states_generation
ON radio_catalog_states(owner_identity, generation);

-- Radio programme snapshots are intentionally separate from TV EPG tables.
-- They describe the current/provider-native programme view for one explicit
-- Radio source and never participate in TV binding or matching.
CREATE TABLE IF NOT EXISTS radio_programme_snapshots (
    source_id              TEXT PRIMARY KEY,
    station_id             TEXT NOT NULL,
    owner_identity         TEXT NOT NULL,
    provider_key           TEXT NOT NULL,
    provider_station_id    TEXT NOT NULL,
    source_revision        TEXT NOT NULL,
    revision               TEXT NOT NULL,
    programmes_json        TEXT NOT NULL,
    updated_at_unix        REAL NOT NULL,
    expires_at_unix        REAL NOT NULL,
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL,
    FOREIGN KEY(source_id) REFERENCES radio_station_sources(source_id) ON DELETE CASCADE,
    FOREIGN KEY(station_id) REFERENCES radio_stations(station_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_radio_programme_snapshots_expiry
ON radio_programme_snapshots(expires_at_unix);

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'admin',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash   TEXT NOT NULL UNIQUE,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at   TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    revoked_at   TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_sessions_token ON sessions(token_hash);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

CREATE TABLE IF NOT EXISTS media_credentials (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash   TEXT NOT NULL UNIQUE,
    name         TEXT NOT NULL,
    scopes_json  TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    expires_at   TEXT DEFAULT '',
    last_used_at TEXT DEFAULT '',
    revoked_at   TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_media_credentials_token ON media_credentials(token_hash);

-- 应用级密钥（用途隔离的对称根密钥）。
-- 仅持久化「无法从环境变量提供」时自动生成的回退值。
-- 不通过普通 app_settings 暴露，不允许通过设置面板修改。
CREATE TABLE IF NOT EXISTS app_secrets (
    name       TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def _current_db_path() -> str:
    # Preserve the narrow test/bootstrap seam that explicitly overrides the
    # module value for a one-off legacy-database migration.
    if DB_PATH != _IMPORTED_DB_PATH:
        return DB_PATH
    configured = os.environ.get('WAVEFLOW_DB_PATH')
    if configured is None:
        return DB_PATH
    if configured == ':memory:':
        return 'file:waveflow?mode=memory&cache=shared'
    return configured


def _connect() -> sqlite3.Connection:
    path = _current_db_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    conn = sqlite3.connect(path, timeout=DATABASE_BUSY_TIMEOUT_MS / 1000)
    try:
        conn.execute(f"PRAGMA busy_timeout={DATABASE_BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = sqlite3.Row
        return conn
    except BaseException:
        conn.close()
        raise


async def _await_thread_operation(operation):
    """Drain a SQLite worker before propagating cancellation."""
    task = asyncio.create_task(asyncio.to_thread(operation))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
        raise


def _execute_sql_script(conn: sqlite3.Connection, script: str) -> None:
    pending = ""
    for line in script.splitlines(keepends=True):
        pending += line
        if not sqlite3.complete_statement(pending):
            continue
        statement = pending.strip()
        pending = ""
        if statement:
            conn.execute(statement)
    if pending.strip():
        raise sqlite3.OperationalError("incomplete database schema statement")


def _add_column_if_missing(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    declaration: str,
) -> bool:
    columns = {
        str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if column in columns:
        return False
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
    return True


@contextmanager
def _initialization_transaction():
    with _INITIALIZE_LOCK:
        conn = _connect()
        try:
            _migrate_radio_station_sources_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()


def _run_initialization(callback) -> None:
    with _initialization_transaction() as conn:
        callback(conn)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_identifier(value, field: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f'{field} 必须是字符串')
    normalized = value.strip()
    if not normalized:
        raise ValueError(f'{field} 不能为空')
    return normalized


def _normalize_interval_seconds(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError('interval_seconds 必须是正整数')
    if value <= 0:
        raise ValueError('interval_seconds 必须大于 0')
    return value


def _normalize_enabled(value) -> int:
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, int) and value in {0, 1}:
        return value
    raise ValueError('enabled 必须是布尔值或 0/1')


def _normalize_timestamp(value: str | None, field: str) -> str:
    if value is None:
        return _utc_now()
    if not isinstance(value, str):
        raise TypeError(f'{field} 必须是 UTC ISO 时间字符串')
    normalized = value.strip()
    if not normalized:
        raise ValueError(f'{field} 不能为空')
    try:
        parsed = datetime.fromisoformat(normalized.replace('Z', '+00:00'))
    except ValueError as exc:
        raise ValueError(f'{field} 必须是有效的 ISO 时间字符串') from exc
    if parsed.tzinfo is None:
        raise ValueError(f'{field} 必须包含时区')
    return parsed.astimezone(timezone.utc).isoformat()


def _normalize_error(value: str | None) -> str:
    if value is None:
        return ''
    if not isinstance(value, str):
        raise TypeError('error 必须是字符串')
    return value.replace('\x00', '').strip()[:AUTOMATION_ERROR_MAX_LENGTH]


def _normalize_count(value, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f'{field} 必须是非负整数')
    if value < 0:
        raise ValueError(f'{field} 不能小于 0')
    return value


def _finalize_legacy_epg_map_upgrade(conn: sqlite3.Connection) -> dict[str, int]:
    """Migrate the old mapping table once, then remove it from the schema.

    This is intentionally kept in database initialization rather than the
    runtime maintenance module. The old table is therefore an upgrade input,
    never a normal production dependency.
    """
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='channel_epg_map'"
    ).fetchone()
    if table is None:
        return {'legacy_rows': 0, 'migrated_rows': 0, 'skipped_rows': 0}

    logical_rows = [dict(row) for row in conn.execute(
        'SELECT id, canonical_key, status FROM iptv_logical_channels'
    ).fetchall()]
    logical_by_key: dict[str, list[dict]] = {}
    for row in logical_rows:
        logical_by_key.setdefault(str(row['canonical_key'] or ''), []).append(row)
    targets = {
        (int(row['source_id']), str(row['channel_id']))
        for row in conn.execute('SELECT source_id, channel_id FROM epg_channels').fetchall()
    }
    no_epg = {
        str(row['logical_channel_id'])
        for row in conn.execute(
            "SELECT logical_channel_id FROM iptv_logical_channel_epg_policies WHERE mode='no_epg'"
        ).fetchall()
    }
    existing = {
        str(row['logical_channel_id'])
        for row in conn.execute(
            'SELECT logical_channel_id FROM iptv_logical_channel_epg_bindings'
        ).fetchall()
    }
    rows = [dict(row) for row in conn.execute(
        'SELECT * FROM channel_epg_map ORDER BY id'
    ).fetchall()]
    candidates: dict[str, list[dict]] = {}
    skipped = 0
    for row in rows:
        if str(row.get('match_status') or '').lower() != 'matched':
            skipped += 1
            continue
        source_id = row.get('epg_source_id')
        channel_id = row.get('epg_channel_id')
        if source_id is None or not str(channel_id or ''):
            skipped += 1
            continue
        try:
            target = (int(source_id), str(channel_id))
        except (TypeError, ValueError):
            skipped += 1
            continue
        logical = logical_by_key.get(str(row.get('canonical_key') or ''), [])
        active = [item for item in logical if item['status'] == 'active']
        if (
            len(active) != 1
            or any(item['status'] not in {'active', 'orphaned'} for item in logical)
            or target not in targets
            or active[0]['id'] in no_epg
            or active[0]['id'] in existing
        ):
            skipped += 1
            continue
        item = dict(row)
        item['logical_channel_id'] = str(active[0]['id'])
        item['target'] = target
        candidates.setdefault(item['logical_channel_id'], []).append(item)

    migrated = 0
    now = _utc_now()
    for logical_id, items in candidates.items():
        target_set = {item['target'] for item in items}
        if len(target_set) != 1:
            skipped += len(items)
            continue
        item = sorted(
            items,
            key=lambda value: (
                -int(bool(value.get('locked'))),
                -int(str(value.get('match_type') or '').lower() == 'manual'),
                -int(value.get('confidence') or 0),
                int(value['id']),
            ),
        )[0]
        origin = (
            'manual'
            if bool(item.get('locked')) or str(item.get('match_type') or '').lower() == 'manual'
            else 'legacy_migrated'
        )
        locked = int(bool(item.get('locked'))) if origin == 'manual' else 0
        conn.execute(
            """
            INSERT INTO iptv_logical_channel_epg_bindings(
                logical_channel_id, epg_source_id, epg_channel_id,
                status, match_type, confidence, locked, origin,
                legacy_canonical_key, created_at, updated_at
            ) VALUES(?, ?, ?, 'matched', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                logical_id, item['target'][0], item['target'][1],
                str(item.get('match_type') or ''),
                max(0, min(100, int(item.get('confidence') or 0))),
                locked, origin, str(item.get('canonical_key') or ''), now, now,
            ),
        )
        migrated += 1
    conn.execute('DROP TABLE channel_epg_map')
    return {'legacy_rows': len(rows), 'migrated_rows': migrated, 'skipped_rows': skipped}


def _migrate_plugin_permission_approval_key(conn: sqlite3.Connection) -> None:
    """Upgrade the pre-fingerprint permission approval primary key.

    Older production databases keyed an approval only by publisher, plugin,
    and permission name.  The current permission contract also persists the
    manifest-derived fingerprint, so the write path targets all four columns.
    SQLite cannot alter a table primary key in place; rebuild this small table
    inside the initialization transaction and preserve every existing row.
    """
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='plugin_permission_approvals'"
    ).fetchone()
    if table is None:
        return
    columns = conn.execute("PRAGMA table_info(plugin_permission_approvals)").fetchall()
    primary_key = tuple(
        row['name'] for row in sorted(columns, key=lambda value: int(value['pk']))
        if int(row['pk']) > 0
    )
    expected = (
        'publisher_id', 'plugin_id', 'permission_name', 'permission_fingerprint',
    )
    if primary_key == expected:
        return
    if primary_key != ('publisher_id', 'plugin_id', 'permission_name'):
        raise sqlite3.OperationalError(
            'plugin_permission_approvals has an unsupported primary key'
        )

    conn.execute("ALTER TABLE plugin_permission_approvals RENAME TO plugin_permission_approvals_legacy")
    conn.execute(
        """
        CREATE TABLE plugin_permission_approvals (
            publisher_id           TEXT NOT NULL,
            plugin_id              TEXT NOT NULL,
            permission_name        TEXT NOT NULL,
            permission_fingerprint TEXT NOT NULL,
            approved              INTEGER NOT NULL DEFAULT 0 CHECK(approved IN (0, 1)),
            approved_at            TEXT DEFAULT '',
            approved_by            TEXT DEFAULT '',
            revoked_at             TEXT DEFAULT '',
            revoked_by             TEXT DEFAULT '',
            manifest_version       TEXT DEFAULT '',
            updated_at             TEXT NOT NULL,
            PRIMARY KEY (publisher_id, plugin_id, permission_name, permission_fingerprint)
        )
        """
    )
    conn.execute(
        """
        INSERT INTO plugin_permission_approvals(
            publisher_id, plugin_id, permission_name, permission_fingerprint,
            approved, approved_at, approved_by, revoked_at, revoked_by,
            manifest_version, updated_at
        )
        SELECT
            publisher_id, plugin_id, permission_name, permission_fingerprint,
            approved, approved_at, approved_by, revoked_at, revoked_by,
            manifest_version, updated_at
        FROM plugin_permission_approvals_legacy
        """
    )
    conn.execute("DROP TABLE plugin_permission_approvals_legacy")


def _migrate_radio_station_sources_schema(conn: sqlite3.Connection) -> None:
    """Add explicit Radio source identity without collapsing old rows.

    The first Radio schema made provider/station the source identity.  A
    source discriminator is now optional, so the old three-column UNIQUE
    constraint must be rebuilt rather than merely adding a nullable column.
    Existing rows receive the empty discriminator and therefore retain their
    previous source IDs.
    """
    table = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='radio_station_sources'"
    ).fetchone()
    if table is None:
        return
    columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(radio_station_sources)").fetchall()
    }
    unique_sets: set[tuple[str, ...]] = set()
    for index in conn.execute("PRAGMA index_list(radio_station_sources)").fetchall():
        if not int(index[2]):
            continue
        unique_sets.add(tuple(
            str(row[2])
            for row in conn.execute(f"PRAGMA index_info({index[1]!r})").fetchall()
            if row[2] is not None
        ))
    expected = ('owner_identity', 'provider_key', 'provider_station_id', 'source_discriminator')
    if 'source_discriminator' in columns and expected in unique_sets:
        return

    if conn.in_transaction:
        raise sqlite3.OperationalError(
            'radio_station_sources migration requires an independent transaction'
        )
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("""
            CREATE TABLE radio_station_sources_new (
                source_id              TEXT PRIMARY KEY,
                station_id             TEXT NOT NULL,
                owner_identity         TEXT NOT NULL,
                provider_key           TEXT NOT NULL,
                provider_station_id    TEXT NOT NULL,
                source_discriminator   TEXT NOT NULL DEFAULT '',
                reference_json         TEXT NOT NULL,
                source_revision        TEXT NOT NULL,
                explicit_priority      INTEGER NOT NULL DEFAULT 0,
                health_status          TEXT NOT NULL DEFAULT 'unknown'
                                       CHECK(health_status IN ('unknown', 'healthy', 'unhealthy')),
                last_success_at        TEXT NOT NULL DEFAULT '',
                last_error             TEXT NOT NULL DEFAULT '',
                lifecycle_state        TEXT NOT NULL DEFAULT 'active'
                                       CHECK(lifecycle_state IN ('active', 'stale', 'expired')),
                catalog_ttl_seconds    INTEGER NOT NULL DEFAULT 300 CHECK(catalog_ttl_seconds > 0),
                catalog_expires_at     REAL NOT NULL DEFAULT 0,
                resolve_expires_at     REAL NOT NULL DEFAULT 0,
                created_at             TEXT NOT NULL,
                updated_at             TEXT NOT NULL,
                UNIQUE(owner_identity, provider_key, provider_station_id, source_discriminator),
                UNIQUE(station_id, source_id),
                FOREIGN KEY(station_id) REFERENCES radio_stations(station_id) ON DELETE CASCADE
            )
        """)
        discriminator_sql = "source_discriminator" if 'source_discriminator' in columns else "''"
        conn.execute(f"""
            INSERT INTO radio_station_sources_new(
                source_id, station_id, owner_identity, provider_key, provider_station_id,
                source_discriminator, reference_json, source_revision, explicit_priority,
                health_status, last_success_at, last_error, lifecycle_state,
                catalog_ttl_seconds, catalog_expires_at, resolve_expires_at,
                created_at, updated_at
            )
            SELECT source_id, station_id, owner_identity, provider_key, provider_station_id,
                   {discriminator_sql}, reference_json, source_revision, explicit_priority,
                   health_status, last_success_at, last_error, lifecycle_state,
                   catalog_ttl_seconds, catalog_expires_at, resolve_expires_at,
                   created_at, updated_at
            FROM radio_station_sources
        """)
        conn.execute("DROP TABLE radio_station_sources")
        conn.execute("ALTER TABLE radio_station_sources_new RENAME TO radio_station_sources")
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_radio_station_sources_station
            ON radio_station_sources(station_id, explicit_priority, source_id)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_radio_station_sources_lifecycle
            ON radio_station_sources(lifecycle_state, catalog_expires_at)
        """)
        conn.commit()
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


async def initialize():
    def _init(conn):
        _execute_sql_script(conn, _SCHEMA)
        _migrate_plugin_permission_approval_key(conn)
        _finalize_legacy_epg_map_upgrade(conn)
        # 兼容已有数据库：补充新字段
        for col, typ, default in [
            ('custom_ua', 'TEXT', "''"),
            ('force_proxy', 'INTEGER', '0'),
            ('last_tested', 'TEXT', "''"),
            ('refresh_generation', 'INTEGER NOT NULL', '0'),
            ('last_attempt_at', 'TEXT', "''"),
            ('last_success_at', 'TEXT', "''"),
            ('last_refresh_status', 'TEXT NOT NULL', "'never'"),
            ('last_error', 'TEXT', "''"),
        ]:
            _add_column_if_missing(
                conn, 'subscriptions', col, f'{typ} DEFAULT {default}'
            )
        conn.execute(
            """
            UPDATE subscriptions
            SET last_attempt_at=last_updated
            WHERE last_attempt_at='' AND last_updated<>''
            """
        )
        conn.execute(
            """
            UPDATE subscriptions
            SET last_success_at=last_updated
            WHERE last_success_at='' AND last_updated<>''
            """
        )
        conn.execute(
            """
            UPDATE subscriptions
            SET last_refresh_status=CASE WHEN valid=1 THEN 'success' ELSE 'failed' END
            WHERE last_refresh_status='never' AND last_updated<>''
            """
        )
        _add_column_if_missing(
            conn,
            'radio_programme_snapshots',
            'source_revision',
            "TEXT NOT NULL DEFAULT ''",
        )
        for col, typ, default in [
            ('source_type', 'TEXT', "'hls'"),
            ('youtube_video_id', 'TEXT', "''"),
            ('referer', 'TEXT', "''"),
            ('custom_ua', 'TEXT', "''"),
            ('force_proxy', 'INTEGER', '0'),
            ('probe_status', 'TEXT', "'untested'"),
            ('live_status', 'TEXT', "'unknown'"),
            ('probe_method', 'TEXT', "''"),
            ('speed_mbps', 'REAL', '0'),
            ('resolution', 'TEXT', "''"),
            ('fps', 'REAL', '0'),
            ('video_codec', 'TEXT', "''"),
            ('audio_codec', 'TEXT', "''"),
            ('requires_headers', 'INTEGER', '0'),
            ('requires_proxy_declared', 'INTEGER', '0'),
            ('proxy_required_hint', 'INTEGER', '0'),
            ('last_success_at', 'TEXT', "''"),
            ('last_error', 'TEXT', "''"),
            ('adapter_provider', 'TEXT', "''"),
            ('adapter_title', 'TEXT', "''"),
            ('probe_meta_json', 'TEXT', "'{}'"),
            ('market_package_id', 'TEXT', "''"),
            ('market_source_id', 'TEXT', "''"),
            ('market_channel_id', 'TEXT', "''"),
            ('market_source_item_id', 'TEXT', "''"),
            (
                'rtsp_timestamp_mode',
                "TEXT NOT NULL CHECK(rtsp_timestamp_mode IN ('passthrough', 'pts_from_dts'))",
                "'passthrough'",
            ),
        ]:
            _add_column_if_missing(conn, 'channels', col, f'{typ} DEFAULT {default}')
        conn.execute("CREATE INDEX IF NOT EXISTS idx_channels_market_pkg ON channels(market_package_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_channels_market_item ON channels(market_package_id, market_source_item_id)")
        _add_column_if_missing(
            conn, 'iptv_logical_channels', 'orphaned_at', 'TEXT DEFAULT NULL'
        )
        # Existing installations predate a reliable orphan transition time.
        # Give historical orphan rows one explicit upgrade-time baseline so
        # startup never treats them as immediately expired. Repeated startup
        # preserves that baseline.
        orphan_baseline = _utc_now()
        conn.execute(
            """
            UPDATE iptv_logical_channels
            SET orphaned_at=?
            WHERE status='orphaned' AND orphaned_at IS NULL
            """,
            (orphan_baseline,),
        )
        conn.execute(
            """
            UPDATE iptv_logical_channels
            SET orphaned_at=NULL
            WHERE status<>'orphaned' AND orphaned_at IS NOT NULL
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_iptv_logical_channels_orphaned_at
            ON iptv_logical_channels(status, orphaned_at)
            """
        )
        for col, typ, default in [
            ('revision', 'INTEGER', '1'),
            ('last_attempt_at', 'TEXT', "''"),
            ('last_success_at', 'TEXT', "''"),
            ('last_error', 'TEXT', "''"),
            ('channel_count', 'INTEGER', '0'),
            ('programme_count', 'INTEGER', '0'),
            ('data_start_at', 'TEXT', "''"),
            ('data_end_at', 'TEXT', "''"),
            ('source_origin', 'TEXT', "'custom'"),
            ('builtin_key', 'TEXT', 'NULL'),
        ]:
            _add_column_if_missing(conn, 'epg_sources', col, f'{typ} DEFAULT {default}')
        for col, typ, default in [
            ('manifest_signature_json', 'TEXT', "'{}'"),
            ('trust_class', 'TEXT', "'official'"),
        ]:
            _add_column_if_missing(
                conn,
                'plugin_installations',
                col,
                f'{typ} NOT NULL DEFAULT {default}',
            )
        conn.execute(
            """
            UPDATE plugin_installations
            SET trust_class='developer_local'
            WHERE trust_state='developer_local'
            """
        )
        # One-time development migration for the exact pre-5B default row.
        # Runtime product behavior never infers builtin identity from URL/name;
        # all later reads and writes use the persisted builtin_key.
        conn.execute(
            """
            UPDATE epg_sources
            SET source_origin='builtin', builtin_key=?
            WHERE source_origin='custom' AND builtin_key IS NULL
              AND name=? AND url=?
              AND NOT EXISTS (
                  SELECT 1 FROM epg_sources AS existing
                  WHERE existing.builtin_key=?
              )
            """,
            (
                BUILTIN_CHINA_EPG_PRESET.key,
                BUILTIN_CHINA_EPG_PRESET.name,
                BUILTIN_CHINA_EPG_PRESET.url,
                BUILTIN_CHINA_EPG_PRESET.key,
            ),
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_epg_sources_builtin_key
            ON epg_sources(builtin_key) WHERE builtin_key IS NOT NULL
            """
        )
        for col, typ, default in [
            ('shadow_run_id', 'TEXT', 'NULL'),
        ]:
            _add_column_if_missing(
                conn,
                'iptv_logical_channel_epg_bindings',
                col,
                f'{typ} DEFAULT {default}',
            )
        _add_column_if_missing(
            conn,
            'epg_match_shadow_runs',
            'preference_snapshot_fingerprint',
            "TEXT NOT NULL DEFAULT ''",
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_iptv_logical_epg_bindings_shadow_run ON iptv_logical_channel_epg_bindings(shadow_run_id)")
        conn.execute("UPDATE epg_sources SET revision=1 WHERE revision IS NULL OR revision < 1")
        conn.execute(
            """
            UPDATE epg_sources
            SET last_attempt_at=last_fetched_at
            WHERE last_attempt_at='' AND last_fetched_at<>''
            """
        )
        conn.execute(
            """
            UPDATE epg_sources
            SET last_success_at=last_fetched_at,
                last_status='success'
            WHERE last_success_at='' AND last_fetched_at<>'' AND last_status='ok'
            """
        )
        conn.execute(
            """
            UPDATE epg_sources
            SET channel_count=(
                    SELECT COUNT(*) FROM epg_channels WHERE epg_channels.source_id=epg_sources.id
                ),
                programme_count=(
                    SELECT COUNT(*) FROM epg_programs WHERE epg_programs.source_id=epg_sources.id
                ),
                data_start_at=COALESCE((
                    SELECT MIN(start) FROM epg_programs WHERE epg_programs.source_id=epg_sources.id
                ), ''),
                data_end_at=COALESCE((
                    SELECT MAX(stop) FROM epg_programs WHERE epg_programs.source_id=epg_sources.id
                ), '')
            WHERE channel_count=0 AND programme_count=0
            """
        )
        for col, typ, default in [
            ('source_key', 'TEXT', "''"),
            ('allow_private', 'INTEGER', '0'),
            ('is_builtin', 'INTEGER', '0'),
            ('last_fetched_at', 'TEXT', "''"),
            ('last_status', 'TEXT', "''"),
            ('last_error', 'TEXT', "''"),
            ('updated_at', 'TEXT', "''"),
        ]:
            _add_column_if_missing(conn, 'market_sources', col, f'{typ} DEFAULT {default}')
        for col, typ, default in [
            ('last_checked_at', 'TEXT', "''"),
            ('remote_version', 'TEXT', "''"),
            ('version_status', 'TEXT', "'unknown'"),
            ('last_update_started_at', 'TEXT', "''"),
            ('last_update_finished_at', 'TEXT', "''"),
            ('last_update_status', 'TEXT', "'never_run'"),
            ('last_update_error', 'TEXT', "''"),
            ('last_update_run_token', 'TEXT', "''"),
        ]:
            _add_column_if_missing(
                conn,
                'market_packages_installed',
                col,
                f'{typ} DEFAULT {default}',
            )
        now = _utc_now()
        conn.execute(
            """
            INSERT OR IGNORE INTO automation_task_config(
                task_id, conflict_group, enabled, interval_seconds, updated_at
            ) VALUES(?, ?, 1, ?, ?)
            """,
            (
                MARKET_AUTOMATION_TASK_ID,
                MARKET_AUTOMATION_CONFLICT_GROUP,
                MARKET_AUTOMATION_INTERVAL_SECONDS,
                now,
            ),
        )
        market_config = conn.execute(
            "SELECT conflict_group FROM automation_task_config WHERE task_id=?",
            (MARKET_AUTOMATION_TASK_ID,),
        ).fetchone()
        conn.execute(
            """
            INSERT OR IGNORE INTO automation_task_state(task_id, conflict_group, last_status)
            VALUES(?, ?, 'never_run')
            """,
            (MARKET_AUTOMATION_TASK_ID, market_config['conflict_group']),
        )
    await asyncio.to_thread(_run_initialization, _init)


# ── Settings ──

async def get_setting(key: str, default: str = '') -> str:
    def _get():
        conn = _connect()
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        conn.close()
        return row['value'] if row else default
    return await asyncio.to_thread(_get)


async def get_or_create_setting(key: str, value: str) -> str:
    """Atomically return one stable internal setting value.

    This is deliberately separate from ``set_setting``: callers that bind a
    durable external resource to this database must never overwrite a value
    selected by another process during concurrent startup.
    """
    def _get_or_create():
        conn = _connect()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)",
                (key, value),
            )
            row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            conn.commit()
            return str(row['value'])
        finally:
            conn.close()
    return await asyncio.to_thread(_get_or_create)

async def set_setting(key: str, value: str):
    def _set():
        conn = _connect()
        conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES(?, ?)", (key, value))
        conn.commit()
        conn.close()
    await asyncio.to_thread(_set)


async def get_app_settings() -> dict:
    def _get():
        conn = _connect()
        try:
            rows = conn.execute("SELECT key, value_json FROM app_settings").fetchall()
        finally:
            conn.close()
        result = {}
        for row in rows:
            try:
                result[row['key']] = json.loads(row['value_json'])
            except json.JSONDecodeError:
                continue
        return result
    return await asyncio.to_thread(_get)


async def set_app_settings(values: dict, updated_by: int | None = None) -> None:
    def _set():
        conn = _connect()
        try:
            now = datetime.now(timezone.utc).isoformat()
            with conn:
                for key, value in values.items():
                    conn.execute(
                        """
                        INSERT INTO app_settings(key, value_json, updated_at, updated_by)
                        VALUES(?, ?, ?, ?)
                        ON CONFLICT(key) DO UPDATE SET
                            value_json=excluded.value_json,
                            updated_at=excluded.updated_at,
                            updated_by=excluded.updated_by
                        """,
                        (key, json.dumps(value, ensure_ascii=False), now, updated_by),
                    )
        finally:
            conn.close()
    await asyncio.to_thread(_set)


# ── Automation task persistence ──

async def ensure_automation_task_config(
    task_id: str,
    conflict_group: str,
    enabled: bool | int,
    interval_seconds: int,
) -> dict:
    task_id = _normalize_identifier(task_id, 'task_id')
    conflict_group = _normalize_identifier(conflict_group, 'conflict_group')
    enabled_value = _normalize_enabled(enabled)
    interval_value = _normalize_interval_seconds(interval_seconds)

    def _ensure():
        conn = _connect()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO automation_task_config(
                        task_id, conflict_group, enabled, interval_seconds, updated_at
                    ) VALUES(?, ?, ?, ?, ?)
                    """,
                    (task_id, conflict_group, enabled_value, interval_value, _utc_now()),
                )
                config = conn.execute(
                    "SELECT * FROM automation_task_config WHERE task_id=?",
                    (task_id,),
                ).fetchone()
                conn.execute(
                    """
                    INSERT OR IGNORE INTO automation_task_state(task_id, conflict_group, last_status)
                    VALUES(?, ?, 'never_run')
                    """,
                    (task_id, config['conflict_group']),
                )
                return dict(config)
        finally:
            conn.close()

    return await asyncio.to_thread(_ensure)


async def get_automation_task_config(task_id: str) -> dict | None:
    task_id = _normalize_identifier(task_id, 'task_id')

    def _get():
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT * FROM automation_task_config WHERE task_id=?",
                (task_id,),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    return await asyncio.to_thread(_get)


async def list_automation_task_configs() -> list[dict]:
    def _list():
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT * FROM automation_task_config ORDER BY task_id"
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    return await asyncio.to_thread(_list)


async def update_automation_task_config(
    task_id: str,
    *,
    enabled: bool | int | None = None,
    interval_seconds: int | None = None,
) -> dict | None:
    task_id = _normalize_identifier(task_id, 'task_id')
    updates = {}
    if enabled is not None:
        updates['enabled'] = _normalize_enabled(enabled)
    if interval_seconds is not None:
        updates['interval_seconds'] = _normalize_interval_seconds(interval_seconds)
    if not updates:
        return await get_automation_task_config(task_id)
    updates['updated_at'] = _utc_now()

    def _update():
        conn = _connect()
        try:
            with conn:
                assignments = ', '.join(f'{key}=?' for key in updates)
                cursor = conn.execute(
                    f"UPDATE automation_task_config SET {assignments} WHERE task_id=?",
                    (*updates.values(), task_id),
                )
                if cursor.rowcount != 1:
                    return None
                row = conn.execute(
                    "SELECT * FROM automation_task_config WHERE task_id=?",
                    (task_id,),
                ).fetchone()
                return dict(row)
        finally:
            conn.close()

    return await asyncio.to_thread(_update)


async def delete_automation_task_config(task_id: str) -> bool:
    """Delete one persisted task and its latest state.

    Dynamic domain tasks use this only after their scheduler has stopped.  The
    state row is removed by the existing foreign-key cascade, so an orphaned
    source task cannot remain claimable after reconciliation.
    """
    task_id = _normalize_identifier(task_id, 'task_id')

    def _delete():
        conn = _connect()
        try:
            with conn:
                cursor = conn.execute(
                    "DELETE FROM automation_task_config WHERE task_id=?",
                    (task_id,),
                )
                return cursor.rowcount == 1
        finally:
            conn.close()

    return await asyncio.to_thread(_delete)


async def get_automation_task_state(task_id: str) -> dict | None:
    task_id = _normalize_identifier(task_id, 'task_id')

    def _get():
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT * FROM automation_task_state WHERE task_id=?",
                (task_id,),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    return await asyncio.to_thread(_get)


async def list_automation_task_states() -> list[dict]:
    """Return the latest state for all automation tasks in one read query."""

    def _list():
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT * FROM automation_task_state ORDER BY task_id"
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    return await asyncio.to_thread(_list)


async def get_automation_conflict_group_state(conflict_group: str) -> dict | None:
    conflict_group = _normalize_identifier(conflict_group, 'conflict_group')

    def _get():
        conn = _connect()
        try:
            row = conn.execute(
                """
                SELECT * FROM automation_task_state
                WHERE conflict_group=? AND last_status='running'
                ORDER BY last_started_at DESC
                LIMIT 1
                """,
                (conflict_group,),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    return await asyncio.to_thread(_get)


async def claim_automation_task(
    *,
    task_id: str,
    conflict_group: str,
    task_type: str,
    run_token: str,
    started_at: str | None = None,
) -> dict:
    task_id = _normalize_identifier(task_id, 'task_id')
    conflict_group = _normalize_identifier(conflict_group, 'conflict_group')
    task_type = _normalize_identifier(task_type, 'task_type')
    run_token = _normalize_identifier(run_token, 'run_token')
    started_at_value = _normalize_timestamp(started_at, 'started_at')

    def _claim():
        conn = _connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            config = conn.execute(
                "SELECT * FROM automation_task_config WHERE task_id=?",
                (task_id,),
            ).fetchone()
            if config is None:
                raise KeyError(f'未知自动任务: {task_id}')
            if config['conflict_group'] != conflict_group:
                raise ValueError('conflict_group 与任务配置不一致')

            current = conn.execute(
                """
                SELECT * FROM automation_task_state
                WHERE conflict_group=? AND last_status='running'
                LIMIT 1
                """,
                (conflict_group,),
            ).fetchone()
            if current is not None:
                conn.rollback()
                return {'claimed': False, 'current': dict(current)}

            try:
                conn.execute(
                    """
                    INSERT INTO automation_task_state(
                        task_id, conflict_group, task_type, run_token,
                        last_started_at, last_finished_at, last_status,
                        checked_count, updated_count, skipped_count, failed_count,
                        last_error
                    ) VALUES(?, ?, ?, ?, ?, '', 'running', 0, 0, 0, 0, '')
                    ON CONFLICT(task_id) DO UPDATE SET
                        conflict_group=excluded.conflict_group,
                        task_type=excluded.task_type,
                        run_token=excluded.run_token,
                        last_started_at=excluded.last_started_at,
                        last_finished_at='',
                        last_status='running',
                        checked_count=0,
                        updated_count=0,
                        skipped_count=0,
                        failed_count=0,
                        last_error=''
                    """,
                    (task_id, conflict_group, task_type, run_token, started_at_value),
                )
            except sqlite3.IntegrityError:
                current = conn.execute(
                    """
                    SELECT * FROM automation_task_state
                    WHERE conflict_group=? AND last_status='running'
                    LIMIT 1
                    """,
                    (conflict_group,),
                ).fetchone()
                conn.rollback()
                if current is not None:
                    return {'claimed': False, 'current': dict(current)}
                raise

            state = conn.execute(
                "SELECT * FROM automation_task_state WHERE task_id=?",
                (task_id,),
            ).fetchone()
            conn.commit()
            return {'claimed': True, 'state': dict(state)}
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    return await asyncio.to_thread(_claim)


async def update_automation_task_progress(
    task_id: str,
    run_token: str,
    *,
    checked_count: int | None = None,
    updated_count: int | None = None,
    skipped_count: int | None = None,
    failed_count: int | None = None,
    error: str | None = None,
) -> bool:
    task_id = _normalize_identifier(task_id, 'task_id')
    run_token = _normalize_identifier(run_token, 'run_token')
    updates = {}
    for field, value in (
        ('checked_count', checked_count),
        ('updated_count', updated_count),
        ('skipped_count', skipped_count),
        ('failed_count', failed_count),
    ):
        if value is not None:
            updates[field] = _normalize_count(value, field)
    if error is not None:
        updates['last_error'] = _normalize_error(error)
    if not updates:
        return False

    def _update():
        conn = _connect()
        try:
            with conn:
                assignments = ', '.join(f'{key}=?' for key in updates)
                cursor = conn.execute(
                    f"""
                    UPDATE automation_task_state SET {assignments}
                    WHERE task_id=? AND run_token=? AND last_status='running'
                    """,
                    (*updates.values(), task_id, run_token),
                )
                return cursor.rowcount == 1
        finally:
            conn.close()

    return await asyncio.to_thread(_update)


async def complete_automation_task(
    task_id: str,
    run_token: str,
    *,
    status: str,
    finished_at: str | None = None,
    checked_count: int | None = None,
    updated_count: int | None = None,
    skipped_count: int | None = None,
    failed_count: int | None = None,
    error: str | None = '',
) -> bool:
    task_id = _normalize_identifier(task_id, 'task_id')
    run_token = _normalize_identifier(run_token, 'run_token')
    if status not in AUTOMATION_FINAL_STATUSES:
        raise ValueError(f'非法自动任务完成状态: {status}')
    updates = {
        'last_finished_at': _normalize_timestamp(finished_at, 'finished_at'),
        'last_status': status,
        'last_error': _normalize_error(error),
    }
    for field, value in (
        ('checked_count', checked_count),
        ('updated_count', updated_count),
        ('skipped_count', skipped_count),
        ('failed_count', failed_count),
    ):
        if value is not None:
            updates[field] = _normalize_count(value, field)

    def _complete():
        conn = _connect()
        try:
            with conn:
                assignments = ', '.join(f'{key}=?' for key in updates)
                cursor = conn.execute(
                    f"""
                    UPDATE automation_task_state SET {assignments}
                    WHERE task_id=? AND run_token=? AND last_status='running'
                    """,
                    (*updates.values(), task_id, run_token),
                )
                return cursor.rowcount == 1
        finally:
            conn.close()

    return await asyncio.to_thread(_complete)


async def recover_interrupted_automation_tasks(
    *,
    finished_at: str | None = None,
    error: str | None = '服务启动时检测到上次自动任务运行被中断',
) -> int:
    finished_at_value = _normalize_timestamp(finished_at, 'finished_at')
    error_value = _normalize_error(error)

    def _recover():
        conn = _connect()
        try:
            with conn:
                cursor = conn.execute(
                    """
                    UPDATE automation_task_state SET
                        last_finished_at=?,
                        last_status='interrupted',
                        last_error=?
                    WHERE last_status='running'
                    """,
                    (finished_at_value, error_value),
                )
                return cursor.rowcount
        finally:
            conn.close()

    return await asyncio.to_thread(_recover)


# ── Subscriptions ──

class DuplicateSubscriptionError(Exception):
    pass


async def add_subscription(title: str, url: str, channel_count: int = 0, custom_ua: str = '', force_proxy: int = 0) -> int:
    def _add():
        conn = _connect()
        try:
            now = datetime.now(timezone.utc).isoformat()
            cur = conn.execute(
                "INSERT INTO subscriptions(title, url, channel_count, created_at, custom_ua, force_proxy) VALUES(?, ?, ?, ?, ?, ?)",
                (title, url, channel_count, now, custom_ua, force_proxy),
            )
            conn.commit()
            return cur.lastrowid
        except sqlite3.IntegrityError as exc:
            if 'subscriptions.url' in str(exc):
                raise DuplicateSubscriptionError(url) from exc
            raise
        finally:
            conn.close()
    return await asyncio.to_thread(_add)


async def get_subscriptions() -> list[dict]:
    def _get():
        conn = _connect()
        rows = conn.execute("SELECT * FROM subscriptions ORDER BY id DESC").fetchall()
        conn.close()
        return [dict(r) for r in rows]
    return await asyncio.to_thread(_get)


async def get_subscription(sub_id: int) -> dict | None:
    def _get():
        conn = _connect()
        row = conn.execute("SELECT * FROM subscriptions WHERE id=?", (sub_id,)).fetchone()
        conn.close()
        return dict(row) if row else None
    return await asyncio.to_thread(_get)


async def get_subscription_by_url(url: str) -> dict | None:
    def _get():
        conn = _connect()
        row = conn.execute("SELECT * FROM subscriptions WHERE url=?", (url,)).fetchone()
        conn.close()
        return dict(row) if row else None
    return await asyncio.to_thread(_get)


async def update_subscription(sub_id: int, **kwargs):
    if not kwargs:
        return

    def _update():
        conn = _connect()
        try:
            refresh_identity_changed = bool({'url', 'custom_ua'} & kwargs.keys())
            sets = [f"{key}=?" for key in kwargs]
            values = list(kwargs.values())
            if refresh_identity_changed:
                sets.extend([
                    "refresh_generation=refresh_generation+1",
                    "valid=0",
                    "last_refresh_status='config_changed'",
                    "last_error=''",
                ])
            with conn:
                conn.execute(
                    f"UPDATE subscriptions SET {', '.join(sets)} WHERE id=?",
                    (*values, sub_id),
                )
        finally:
            conn.close()
    return await asyncio.to_thread(_update)


class SubscriptionRefreshSuperseded(Exception):
    """A refresh completed after a newer refresh claimed the subscription."""


async def begin_subscription_refresh(sub_id: int) -> int:
    """Atomically claim the next refresh generation for one subscription."""
    def _begin():
        conn = _connect()
        try:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute(
                "SELECT refresh_generation FROM subscriptions WHERE id=?",
                (sub_id,),
            ).fetchone()
            if row is None:
                conn.rollback()
                raise ValueError('订阅不存在')
            generation = int(row['refresh_generation'] or 0) + 1
            attempted_at = datetime.now(timezone.utc).isoformat()
            conn.execute(
                """
                UPDATE subscriptions SET
                    refresh_generation=?,
                    last_attempt_at=?,
                    last_refresh_status='running',
                    last_error=''
                WHERE id=?
                """,
                (generation, attempted_at, sub_id),
            )
            conn.commit()
            return generation
        finally:
            conn.close()
    return await asyncio.to_thread(_begin)


async def mark_subscription_invalid_if_current(
    sub_id: int,
    generation: int,
    *,
    status: str = 'failed',
    error: str = '',
) -> bool:
    status_value = str(status or 'failed').strip()[:64] or 'failed'
    error_value = ' '.join(str(error or '').split())[:512]

    def _mark():
        conn = _connect()
        try:
            with conn:
                cursor = conn.execute(
                    """
                    UPDATE subscriptions SET
                        valid=0,
                        last_refresh_status=?,
                        last_error=?
                    WHERE id=? AND refresh_generation=?
                    """,
                    (status_value, error_value, sub_id, int(generation)),
                )
                return cursor.rowcount == 1
        finally:
            conn.close()
    return await asyncio.to_thread(_mark)


async def recover_interrupted_subscription_refreshes(
    *,
    error: str = '服务启动时检测到上次订阅刷新被中断',
) -> int:
    error_value = ' '.join(str(error or '').split())[:512]

    def _recover():
        conn = _connect()
        try:
            with conn:
                cursor = conn.execute(
                    """
                    UPDATE subscriptions SET
                        valid=0,
                        last_refresh_status='interrupted',
                        last_error=?
                    WHERE last_refresh_status='running'
                    """,
                    (error_value,),
                )
                return cursor.rowcount
        finally:
            conn.close()

    return await asyncio.to_thread(_recover)


async def delete_subscription(sub_id: int):
    def _delete():
        conn = _connect()
        conn.execute("DELETE FROM subscriptions WHERE id=?", (sub_id,))
        conn.commit()
        conn.close()
    await asyncio.to_thread(_delete)


# ── Channels ──

_CHANNEL_IDENTITY_FIELDS = (
    'url',
    'source_type',
    'custom_ua',
    'referer',
    'force_proxy',
    'requires_proxy_declared',
    'adapter_provider',
    'market_package_id',
    'market_source_id',
    'market_channel_id',
    'market_source_item_id',
)

_CHANNEL_CONFIG_FIELDS = (
    'name',
    'url',
    'logo_url',
    'group_name',
    'tvg_id',
    'tvg_name',
    'source_type',
    'youtube_video_id',
    'referer',
    'custom_ua',
    'force_proxy',
    'requires_headers',
    'requires_proxy_declared',
    'proxy_required_hint',
    'adapter_provider',
    'adapter_title',
    'market_package_id',
    'market_source_id',
    'market_channel_id',
    'market_source_item_id',
    'rtsp_timestamp_mode',
)

_CHANNEL_UPDATE_FIELDS = tuple(
    field for field in _CHANNEL_CONFIG_FIELDS
    if field not in {
        'youtube_video_id',
        'requires_headers',
        'proxy_required_hint',
        'adapter_title',
    }
)

_CHANNEL_BOOLEAN_FIELDS = {
    'force_proxy',
    'requires_headers',
    'requires_proxy_declared',
    'proxy_required_hint',
}


def _channel_bool(value) -> int:
    if isinstance(value, str):
        return 0 if value.strip().lower() in {'', '0', 'false', 'no', 'off', 'none', 'null'} else 1
    return 1 if value else 0


def _channel_config_value(channel: dict, field: str):
    if field in _CHANNEL_BOOLEAN_FIELDS:
        return _channel_bool(channel.get(field))
    if field == 'source_type':
        return str(channel.get(field) or 'hls').strip().lower()
    if field == 'adapter_provider':
        return str(channel.get(field) or channel.get('adapter') or '').strip().lower()
    if field == 'rtsp_timestamp_mode':
        from rtsp_playback import normalize_rtsp_timestamp_mode
        return normalize_rtsp_timestamp_mode(channel.get(field))
    return str(channel.get(field) or '').strip()


def _channel_identity_key(channel: dict) -> tuple:
    values = []
    for field in _CHANNEL_IDENTITY_FIELDS:
        value = _channel_config_value(channel, field)
        if field == 'market_source_item_id' and str(value).startswith('auto-'):
            value = ''
        values.append(value)
    return tuple(values)


def _channel_stable_identity_key(channel: dict) -> tuple | None:
    """Return a source continuity key that deliberately excludes URL.

    Stable market item/tvg ids and provider-owned adapter references are strong
    evidence.  Plain M3U names are only considered by the subscription-local
    single-candidate fallback in ``_sync_channels_conn``.
    """
    values = {field: _channel_config_value(channel, field) for field in _CHANNEL_CONFIG_FIELDS}
    market_item = values.get('market_source_item_id', '')
    if market_item and not market_item.startswith('auto-'):
        return ('market_item', values.get('market_package_id', ''), market_item)
    tvg_id = values.get('tvg_id', '')
    if tvg_id:
        return ('tvg_id', tvg_id.casefold())
    url = values.get('url', '')
    if values.get('source_type') == 'adapter' and '://' in url:
        return ('adapter_reference', values.get('adapter_provider', ''), url)
    return None


def _prepare_channels(channels: list[dict]) -> list[tuple[dict, dict]]:
    prepared = []
    for channel in channels:
        values = {field: _channel_config_value(channel, field) for field in _CHANNEL_CONFIG_FIELDS}
        if not values['name']:
            raise ValueError('channel name 不能为空')
        if not values['url']:
            raise ValueError('channel url 不能为空')
        prepared.append((channel, values))
    return prepared


def _sync_channels_conn(conn: sqlite3.Connection, sub_id: int, prepared: list[tuple[dict, dict]]) -> None:
    existing_rows = conn.execute(
        "SELECT * FROM channels WHERE subscription_id=? ORDER BY id",
        (sub_id,),
    ).fetchall()
    existing_by_key: dict[tuple, list[sqlite3.Row]] = {}
    existing_by_stable_key: dict[tuple, list[sqlite3.Row]] = {}
    existing_by_fallback_key: dict[tuple, list[sqlite3.Row]] = {}
    for row in existing_rows:
        row_dict = dict(row)
        existing_by_key.setdefault(_channel_identity_key(row_dict), []).append(row)
        stable = _channel_stable_identity_key(row_dict)
        if stable:
            existing_by_stable_key.setdefault(stable, []).append(row)
        else:
            from m3u8_parser import channel_name_semantics
            semantics = channel_name_semantics(row_dict.get('name', ''))
            fallback = (
                semantics['canonical_candidate'],
                row_dict.get('group_name', ''),
                row_dict.get('logo_url', ''),
            )
            existing_by_fallback_key.setdefault(fallback, []).append(row)

    retained_ids = []
    update_assignments = ', '.join(f"{field}=?" for field in _CHANNEL_UPDATE_FIELDS)
    insert_fields = ('subscription_id', *_CHANNEL_CONFIG_FIELDS)
    insert_columns = ', '.join(insert_fields)
    insert_placeholders = ', '.join('?' for _ in insert_fields)

    for original, values in prepared:
        matches = existing_by_key.get(_channel_identity_key(original)) or []
        if not matches:
            stable = _channel_stable_identity_key(original)
            if stable:
                matches = existing_by_stable_key.get(stable) or []
            else:
                from m3u8_parser import channel_name_semantics
                semantics = channel_name_semantics(values.get('name', ''))
                fallback = (
                    semantics['canonical_candidate'],
                    values.get('group_name', ''),
                    values.get('logo_url', ''),
                )
                candidates = existing_by_fallback_key.get(fallback) or []
                # Weak name evidence is accepted only for one-to-one
                # subscription-local continuity; it is never cross-source.
                matches = candidates if len(candidates) == 1 else []
        existing = matches.pop(0) if matches else None
        if existing is not None:
            row_id = int(existing['id'])
            update_values = tuple(values[field] for field in _CHANNEL_UPDATE_FIELDS)
            conn.execute(
                f"UPDATE channels SET {update_assignments} WHERE id=? AND subscription_id=?",
                (*update_values, row_id, sub_id),
            )
        else:
            config_values = tuple(values[field] for field in _CHANNEL_CONFIG_FIELDS)
            cursor = conn.execute(
                f"INSERT INTO channels({insert_columns}) VALUES({insert_placeholders})",
                (sub_id, *config_values),
            )
            row_id = int(cursor.lastrowid)
        retained_ids.append(row_id)

    if retained_ids:
        placeholders = ', '.join('?' for _ in retained_ids)
        conn.execute(
            f"DELETE FROM channels WHERE subscription_id=? AND id NOT IN ({placeholders})",
            (sub_id, *retained_ids),
        )
    else:
        conn.execute("DELETE FROM channels WHERE subscription_id=?", (sub_id,))

    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE subscriptions SET channel_count=?, last_updated=? WHERE id=?",
        (len(prepared), now, sub_id),
    )


async def add_channels_bulk(sub_id: int, channels: list[dict]):
    prepared = _prepare_channels(channels)

    def _add():
        conn = _connect()
        try:
            with conn:
                _sync_channels_conn(conn, sub_id, prepared)
        finally:
            conn.close()
    await asyncio.to_thread(_add)


async def add_subscription_with_channels(
    *,
    title: str,
    url: str,
    channels: list[dict],
    custom_ua: str = '',
    force_proxy: int = 0,
) -> int:
    """Create a subscription and its channels in one durable transaction."""
    prepared = _prepare_channels(channels)

    def _add():
        conn = _connect()
        try:
            with conn:
                now = datetime.now(timezone.utc).isoformat()
                cursor = conn.execute(
                    """
                    INSERT INTO subscriptions(
                        title, url, channel_count, created_at, custom_ua, force_proxy,
                        last_attempt_at, last_success_at, last_refresh_status, last_error
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'success', '')
                    """,
                    (
                        title, url, len(prepared), now, custom_ua, force_proxy,
                        now, now,
                    ),
                )
                sub_id = int(cursor.lastrowid)
                _sync_channels_conn(conn, sub_id, prepared)
                return sub_id
        except sqlite3.IntegrityError as exc:
            if 'subscriptions.url' in str(exc):
                raise DuplicateSubscriptionError(url) from exc
            raise
        finally:
            conn.close()

    return await asyncio.to_thread(_add)


async def replace_subscription_channels_atomic(
    sub_id: int,
    channels: list[dict],
    *,
    valid: int | None = None,
    expected_generation: int | None = None,
) -> None:
    """Replace channels and subscription refresh metadata atomically."""
    prepared = _prepare_channels(channels)

    def _replace():
        conn = _connect()
        try:
            with conn:
                if expected_generation is not None:
                    row = conn.execute(
                        "SELECT refresh_generation FROM subscriptions WHERE id=?",
                        (sub_id,),
                    ).fetchone()
                    if row is None or int(row['refresh_generation'] or 0) != int(expected_generation):
                        raise SubscriptionRefreshSuperseded(sub_id)
                _sync_channels_conn(conn, sub_id, prepared)
                if valid is not None:
                    if int(valid) == 1:
                        completed_at = datetime.now(timezone.utc).isoformat()
                        conn.execute(
                            """
                            UPDATE subscriptions SET
                                valid=1,
                                last_success_at=?,
                                last_refresh_status='success',
                                last_error=''
                            WHERE id=?
                            """,
                            (completed_at, sub_id),
                        )
                    else:
                        conn.execute(
                            "UPDATE subscriptions SET valid=? WHERE id=?",
                            (valid, sub_id),
                        )
        finally:
            conn.close()

    await asyncio.to_thread(_replace)


async def install_market_package_atomic(
    *,
    package_id: str,
    market_url: str,
    title: str,
    subscription_url: str,
    channels: list[dict],
    installed_version: str = '',
    metadata_json: str = '',
    custom_ua: str = '',
    force_proxy: int = 0,
    auto_update: int = 0,
    logo_assets: list[dict] | None = None,
    logo_bindings: list[dict] | None = None,
) -> int:
    if not channels:
        raise ValueError('没有可导入的频道源')
    prepared = _prepare_channels(channels)

    def _install():
        conn = _connect()
        try:
            with conn:
                install = conn.execute(
                    "SELECT * FROM market_packages_installed WHERE package_id=?",
                    (package_id,),
                ).fetchone()
                sub_id = int(install['installed_subscription_id']) if install and install['installed_subscription_id'] else 0
                subscription = conn.execute(
                    "SELECT * FROM subscriptions WHERE id=?",
                    (sub_id,),
                ).fetchone() if sub_id else None

                now = datetime.now(timezone.utc).isoformat()
                if subscription is None:
                    existing = conn.execute(
                        "SELECT id FROM subscriptions WHERE url=?",
                        (subscription_url,),
                    ).fetchone()
                    if existing:
                        raise DuplicateSubscriptionError(subscription_url)
                    cursor = conn.execute(
                        """
                        INSERT INTO subscriptions(
                            title, url, channel_count, valid, last_updated, created_at,
                            custom_ua, force_proxy, last_attempt_at, last_success_at,
                            last_refresh_status, last_error
                        ) VALUES(?, ?, ?, 1, ?, ?, ?, ?, ?, ?, 'success', '')
                        """,
                        (
                            title, subscription_url, len(channels), now, now,
                            custom_ua, force_proxy, now, now,
                        ),
                    )
                    sub_id = int(cursor.lastrowid)
                else:
                    conn.execute(
                        """
                        UPDATE subscriptions SET
                            title=?, url=?, channel_count=?, valid=1, last_updated=?,
                            custom_ua=?, force_proxy=?, last_attempt_at=?,
                            last_success_at=?, last_refresh_status='success', last_error=''
                        WHERE id=?
                        """,
                        (
                            title, subscription_url, len(channels), now,
                            custom_ua, force_proxy, now, now, sub_id,
                        ),
                    )

                _sync_channels_conn(conn, sub_id, prepared)
                conn.execute(
                    """
                    INSERT INTO market_packages_installed(
                        package_id, market_url, installed_subscription_id, installed_version,
                        installed_at, auto_update, metadata_json
                    ) VALUES(?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(package_id) DO UPDATE SET
                        market_url=excluded.market_url,
                        installed_subscription_id=excluded.installed_subscription_id,
                        installed_version=excluded.installed_version,
                        installed_at=excluded.installed_at,
                        auto_update=excluded.auto_update,
                        metadata_json=excluded.metadata_json
                    """,
                    (package_id, market_url, sub_id, installed_version, now, auto_update, metadata_json),
                )
                if logo_assets is not None or logo_bindings is not None:
                    _replace_package_logo_state_conn(
                        conn,
                        package_id,
                        installed_version,
                        logo_assets or [],
                        logo_bindings or [],
                        now=now,
                    )
                return sub_id
        finally:
            conn.close()

    return await asyncio.to_thread(_install)


async def install_logo_package_atomic(
    *,
    package_id: str,
    market_url: str,
    installed_version: str,
    metadata_json: str,
    assets: list[dict],
    bindings: list[dict],
    auto_update: int = 0,
) -> None:
    """Publish a logo-only package without creating subscription/channel rows."""
    def _install():
        conn = _connect()
        try:
            with conn:
                now = datetime.now(timezone.utc).isoformat()
                conn.execute(
                    "DELETE FROM logical_channel_logo_bindings WHERE package_id=?",
                    (package_id,),
                )
                conn.execute("DELETE FROM package_assets WHERE package_id=?", (package_id,))
                conn.execute(
                    """
                    INSERT INTO market_packages_installed(
                        package_id, market_url, installed_subscription_id,
                        installed_version, installed_at, auto_update, metadata_json
                    ) VALUES(?, ?, NULL, ?, ?, ?, ?)
                    ON CONFLICT(package_id) DO UPDATE SET
                        market_url=excluded.market_url,
                        installed_subscription_id=NULL,
                        installed_version=excluded.installed_version,
                        installed_at=excluded.installed_at,
                        auto_update=excluded.auto_update,
                        metadata_json=excluded.metadata_json
                    """,
                    (package_id, market_url, installed_version, now, auto_update, metadata_json),
                )
                _replace_package_logo_state_conn(
                    conn,
                    package_id,
                    installed_version,
                    assets,
                    bindings,
                    now=now,
                )
        finally:
            conn.close()
    await asyncio.to_thread(_install)


def _insert_package_logo_bindings_conn(
    conn: sqlite3.Connection,
    package_id: str,
    package_version: str,
    bindings: list[dict],
    *,
    now: str,
) -> None:
    for binding in bindings:
        conn.execute(
            """
            INSERT INTO logical_channel_logo_bindings(
                logical_channel_id, package_id, asset_id, binding_type,
                match_type, match_key, priority, package_version,
                enabled, lifecycle_state, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, 1, 'active', ?, ?)
            """,
            (
                binding["logical_channel_id"], package_id,
                binding["asset_id"], binding["binding_type"],
                binding["match_type"], binding["match_key"],
                int(binding.get("priority", 0)), package_version,
                now, now,
            ),
        )


def _replace_package_logo_state_conn(
    conn: sqlite3.Connection,
    package_id: str,
    package_version: str,
    assets: list[dict],
    bindings: list[dict],
    *,
    now: str,
) -> None:
    """Replace verified asset rows and bindings inside the caller's transaction."""
    conn.execute(
        "DELETE FROM logical_channel_logo_bindings WHERE package_id=?",
        (package_id,),
    )
    conn.execute("DELETE FROM package_assets WHERE package_id=?", (package_id,))
    for asset in assets:
        conn.execute(
            """
            INSERT INTO package_assets(
                package_id, asset_id, package_version, relative_path,
                media_type, sha256, size_bytes, stored_path, state,
                created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
            """,
            (
                package_id, asset["asset_id"], package_version,
                asset["relative_path"], asset["media_type"],
                asset["sha256"], int(asset["size_bytes"]),
                asset["stored_path"], now, now,
            ),
        )
    _insert_package_logo_bindings_conn(
        conn, package_id, package_version, bindings, now=now,
    )


async def replace_package_logo_bindings_atomic(
    package_id: str,
    package_version: str,
    bindings: list[dict],
) -> None:
    """Replace only bindings after logical-channel maintenance.

    Asset bytes and the installed package version are already published by the
    package transaction. A binding refresh must not delete verified assets.
    """
    def _replace():
        conn = _connect()
        try:
            with conn:
                now = datetime.now(timezone.utc).isoformat()
                conn.execute(
                    "DELETE FROM logical_channel_logo_bindings WHERE package_id=?",
                    (package_id,),
                )
                _insert_package_logo_bindings_conn(
                    conn, package_id, package_version, bindings, now=now,
                )
        finally:
            conn.close()
    await asyncio.to_thread(_replace)


async def uninstall_market_package_atomic(package_id: str) -> bool:
    def _uninstall():
        conn = _connect()
        try:
            with conn:
                install = conn.execute(
                    "SELECT installed_subscription_id FROM market_packages_installed WHERE package_id=?",
                    (package_id,),
                ).fetchone()
                if not install:
                    return False
                sub_id = install['installed_subscription_id']
                conn.execute("DELETE FROM market_packages_installed WHERE package_id=?", (package_id,))
                conn.execute("DELETE FROM logical_channel_logo_bindings WHERE package_id=?", (package_id,))
                conn.execute("DELETE FROM package_assets WHERE package_id=?", (package_id,))
                if sub_id:
                    subscription = conn.execute(
                        "SELECT url FROM subscriptions WHERE id=?",
                        (sub_id,),
                    ).fetchone()
                    # Only the synthetic subscription created by the Market
                    # installer is owned by this package. Retain any legacy
                    # or manually-associated user subscription.
                    if subscription and str(subscription['url'] or '') == f'market://{package_id}':
                        ownership = conn.execute(
                            """
                            SELECT SUM(
                                       CASE
                                           WHEN market_package_id<>'' AND market_package_id<>?
                                           THEN 1 ELSE 0
                                       END
                                   ) AS foreign_rows
                            FROM channels
                            WHERE subscription_id=?
                            """,
                            (package_id, sub_id),
                        ).fetchone()
                        if ownership and int(ownership['foreign_rows'] or 0) == 0:
                            conn.execute("DELETE FROM subscriptions WHERE id=?", (sub_id,))
                return True
        finally:
            conn.close()

    return await asyncio.to_thread(_uninstall)


def _apply_sub_fallbacks(rows: list[dict]) -> list[dict]:
    """频道级 custom_ua/force_proxy 为空时，用订阅级 sub_custom_ua/sub_force_proxy 兜底。"""
    for row in rows:
        if not (row.get('custom_ua') or '').strip():
            row['custom_ua'] = row.get('sub_custom_ua', '') or ''
        # force_proxy：channels.force_proxy=1 优先；否则取订阅级
        if not int(row.get('force_proxy') or 0):
            row['force_proxy'] = int(row.get('sub_force_proxy') or 0)
    return rows


async def get_channels(sub_id: int, group: str = '', search: str = '') -> list[dict]:
    """读频道列表；custom_ua/force_proxy 频道级优先，回退到订阅级。"""
    def _get():
        conn = _connect()
        # 同名列 sqlite3.Row → dict 会去重，所以订阅级用 sub_* 别名，
        # 在 Python 侧做"频道级空 → 用订阅级"的回退。
        query = (
            "SELECT c.*, "
            "s.custom_ua AS sub_custom_ua, "
            "s.force_proxy AS sub_force_proxy "
            "FROM channels c JOIN subscriptions s ON c.subscription_id=s.id "
            "WHERE c.subscription_id=?"
        )
        params: list = [sub_id]
        if group:
            query += " AND c.group_name=?"
            params.append(group)
        if search:
            query += " AND c.name LIKE ?"
            params.append(f"%{search}%")
        query += " ORDER BY c.id"
        rows = conn.execute(query, params).fetchall()
        conn.close()
        return _apply_sub_fallbacks([dict(r) for r in rows])
    return await asyncio.to_thread(_get)


async def get_channel_groups(sub_id: int) -> list[str]:
    def _get():
        conn = _connect()
        rows = conn.execute(
            "SELECT DISTINCT group_name FROM channels WHERE subscription_id=? AND group_name != '' ORDER BY group_name",
            (sub_id,),
        ).fetchall()
        conn.close()
        return [r['group_name'] for r in rows]
    return await asyncio.to_thread(_get)


async def get_aggregated_channels(group: str = '', search: str = '') -> list[dict]:
    """跨所有订阅源聚合频道：按清洗名去重，每个频道保留所有可用链接"""
    def _get():
        conn = _connect()
        query = """
            SELECT c.*, s.title as sub_title,
                   s.custom_ua AS sub_custom_ua,
                   s.force_proxy AS sub_force_proxy
            FROM channels c
            JOIN subscriptions s ON c.subscription_id = s.id
        """
        params: list = []
        where = []
        if group:
            where.append("c.group_name = ?")
            params.append(group)
        if search:
            where.append("c.name LIKE ?")
            params.append(f"%{search}%")
        if where:
            query += " WHERE " + " AND ".join(where)
        query += " ORDER BY c.name, c.is_working DESC, c.latency_ms ASC"
        rows = conn.execute(query, params).fetchall()
        conn.close()
        return _apply_sub_fallbacks([dict(r) for r in rows])
    return await asyncio.to_thread(_get)


async def get_iptv_logical_channel_projection() -> list[dict]:
    """Return the persisted logical-channel read projection with raw members.

    Production reads must use this membership projection rather than
    recomputing a name merge from raw rows.  Conflict rows are returned as
    explicit logical channels; orphan history is intentionally excluded.
    """
    def _get():
        conn = _connect()
        try:
            rows = conn.execute(
                """
                SELECT
                    lc.id AS logical_channel_id,
                    lc.canonical_key AS logical_canonical_key,
                    lc.display_name AS logical_display_name,
                    lc.status AS logical_status,
                    m.membership_reason,
                    m.membership_confidence,
                    m.variant_type,
                    c.*,
                    s.title AS sub_title,
                    s.custom_ua AS sub_custom_ua,
                    s.force_proxy AS sub_force_proxy
                FROM iptv_logical_channels AS lc
                JOIN iptv_logical_channel_members AS m
                    ON m.logical_channel_id=lc.id
                JOIN channels AS c ON c.id=m.channel_id
                JOIN subscriptions AS s ON s.id=c.subscription_id
                WHERE lc.status <> 'orphaned'
                ORDER BY lc.created_at, lc.id, c.id
                """
            ).fetchall()
            return _apply_sub_fallbacks([dict(row) for row in rows])
        finally:
            conn.close()
    return await asyncio.to_thread(_get)


def _new_iptv_logical_channel_id() -> str:
    return f'lc_{uuid.uuid4().hex}'


async def get_iptv_logical_channels() -> list[dict]:
    def _get():
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT * FROM iptv_logical_channels ORDER BY created_at, id"
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    return await asyncio.to_thread(_get)


async def get_iptv_logical_channel_members() -> list[dict]:
    def _get():
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT * FROM iptv_logical_channel_members ORDER BY logical_channel_id, channel_id"
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    return await asyncio.to_thread(_get)


async def get_iptv_logical_channel_hint_rows(logical_channel_id: str | None = None) -> list[dict]:
    """Read raw, non-secret membership fields for EPG-2B hint aggregation."""

    def _get():
        conn = _connect()
        try:
            query = """
                SELECT
                    lc.id AS logical_channel_id,
                    lc.canonical_key AS canonical_key,
                    lc.display_name AS display_name,
                    lc.status AS logical_status,
                    m.channel_id AS member_channel_id,
                    m.membership_reason AS membership_reason,
                    m.membership_confidence AS membership_confidence,
                    m.variant_type AS variant_type,
                    c.name AS raw_name,
                    c.tvg_id AS raw_tvg_id,
                    c.tvg_name AS raw_tvg_name
                FROM iptv_logical_channels AS lc
                LEFT JOIN iptv_logical_channel_members AS m
                    ON m.logical_channel_id = lc.id
                LEFT JOIN channels AS c ON c.id = m.channel_id
            """
            params: tuple[object, ...] = ()
            if logical_channel_id is not None:
                query += " WHERE lc.id=?"
                params = (logical_channel_id,)
            query += " ORDER BY lc.id, m.channel_id"
            rows = conn.execute(query, params).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    return await asyncio.to_thread(_get)


async def sync_iptv_logical_channel_shadow_atomic(groups: list[dict]) -> dict:
    """Atomically reconcile the persisted IPTV logical-channel shadow model.

    ``groups`` is a projection input produced from the current raw channel
    rows.  Membership uniqueness is enforced by SQLite so a partial write can
    never leave one raw channel attached to two logical channels.
    """

    if not isinstance(groups, list):
        raise TypeError('groups 必须是列表')

    def _sync():
        conn = _connect()
        now = _utc_now()
        created_logical_count = 0
        reused_logical_count = 0
        updated_logical_count = 0
        created_member_count = 0
        removed_member_count = 0
        split_conflicts: list[dict] = []
        merge_conflicts: list[dict] = []
        try:
            conn.execute('BEGIN IMMEDIATE')
            raw_ids = {
                int(row['id'])
                for row in conn.execute('SELECT id FROM channels').fetchall()
            }

            normalized_groups = []
            seen_group_keys = set()
            seen_channel_ids = set()
            for group in groups:
                if not isinstance(group, dict):
                    raise TypeError('logical channel group 必须是对象')
                canonical_key = str(group.get('canonical_key') or '').strip()
                display_name = str(group.get('display_name') or '').strip()
                channel_ids = [int(channel_id) for channel_id in (group.get('channel_ids') or [])]
                if not canonical_key or not display_name:
                    raise ValueError('logical channel group 缺少 canonical_key 或 display_name')
                if canonical_key in seen_group_keys:
                    raise ValueError(f'重复 logical canonical_key: {canonical_key}')
                seen_group_keys.add(canonical_key)
                current_channel_ids = [channel_id for channel_id in channel_ids if channel_id in raw_ids]
                cross_group_duplicates = set(current_channel_ids) & seen_channel_ids
                if cross_group_duplicates:
                    duplicate_ids = ', '.join(str(channel_id) for channel_id in sorted(cross_group_duplicates))
                    raise ValueError(f'同一 raw channel 不能属于多个 logical group: {duplicate_ids}')
                seen_channel_ids.update(current_channel_ids)
                if not current_channel_ids:
                    # A stale caller projection may contain only rows deleted
                    # after it was read. Do not materialize a new orphan row
                    # for such an empty target group.
                    continue
                normalized_groups.append({
                    'canonical_key': canonical_key,
                    'display_name': display_name,
                    'channel_ids': current_channel_ids,
                    'membership_reason': str(group.get('membership_reason') or 'normalized_name')[:64],
                    'membership_confidence': max(0, min(100, int(group.get('membership_confidence', 100)))),
                })

            # Foreign keys remove most stale rows automatically, but this
            # explicit cleanup also repairs databases created before the
            # shadow table existed.
            stale_cursor = conn.execute(
                """
                DELETE FROM iptv_logical_channel_members
                WHERE channel_id NOT IN (SELECT id FROM channels)
                """
            )
            removed_member_count += stale_cursor.rowcount

            logical_rows = conn.execute(
                "SELECT * FROM iptv_logical_channels ORDER BY created_at, id"
            ).fetchall()
            logical_by_id = {row['id']: dict(row) for row in logical_rows}
            member_rows = conn.execute(
                "SELECT * FROM iptv_logical_channel_members ORDER BY logical_channel_id, channel_id"
            ).fetchall()
            members_by_logical: dict[str, set[int]] = {logical_id: set() for logical_id in logical_by_id}
            logical_by_channel: dict[int, str] = {}
            for row in member_rows:
                channel_id = int(row['channel_id'])
                logical_id = row['logical_channel_id']
                members_by_logical.setdefault(logical_id, set()).add(channel_id)
                logical_by_channel[channel_id] = logical_id

            target_key_by_channel = {
                channel_id: group['canonical_key']
                for group in normalized_groups
                for channel_id in group['channel_ids']
            }
            # The input projection is authoritative for current membership. A
            # raw row omitted from every target group is no longer groupable
            # (for example, its name normalized to an empty key), so detach it
            # before evaluating split conflicts and orphan state.
            for channel_id, logical_id in list(logical_by_channel.items()):
                if channel_id in target_key_by_channel:
                    continue
                conn.execute(
                    'DELETE FROM iptv_logical_channel_members WHERE channel_id=?',
                    (channel_id,),
                )
                members_by_logical.setdefault(logical_id, set()).discard(channel_id)
                del logical_by_channel[channel_id]
                removed_member_count += 1

            logical_target_keys: dict[str, set[str]] = {}
            for logical_id, channel_ids in members_by_logical.items():
                logical_target_keys[logical_id] = {
                    target_key_by_channel[channel_id]
                    for channel_id in channel_ids
                    if channel_id in target_key_by_channel
                }
            split_ids = {
                logical_id
                for logical_id, target_keys in logical_target_keys.items()
                if len(target_keys) > 1
            }
            for logical_id in sorted(split_ids):
                row = logical_by_id[logical_id]
                split_conflicts.append({
                    'logical_channel_id': logical_id,
                    'canonical_key': row['canonical_key'],
                    'channel_ids': sorted(members_by_logical.get(logical_id, set())),
                    'target_keys': sorted(logical_target_keys[logical_id]),
                })

            handled_logical_ids: set[str] = set()
            touched_logical_ids: set[str] = set()

            def _create_logical(group: dict, status: str) -> str:
                nonlocal created_logical_count
                logical_id = _new_iptv_logical_channel_id()
                conn.execute(
                    """
                    INSERT INTO iptv_logical_channels(
                        id, canonical_key, display_name, status, created_at, updated_at
                    ) VALUES(?, ?, ?, ?, ?, ?)
                    """,
                    (logical_id, group['canonical_key'], group['display_name'], status, now, now),
                )
                logical_by_id[logical_id] = {
                    'id': logical_id,
                    'canonical_key': group['canonical_key'],
                    'display_name': group['display_name'],
                    'status': status,
                    'orphaned_at': None,
                }
                members_by_logical[logical_id] = set()
                created_logical_count += 1
                return logical_id

            def _update_logical(logical_id: str, group: dict, status: str) -> None:
                nonlocal updated_logical_count
                old = logical_by_id[logical_id]
                old_status = str(old['status'] or '')
                old_orphaned_at = old.get('orphaned_at')
                orphaned_at = (
                    old_orphaned_at
                    if status == 'orphaned' and old_status == 'orphaned' and old_orphaned_at
                    else now if status == 'orphaned'
                    else None
                )
                if (
                    old['canonical_key'] != group['canonical_key']
                    or old['display_name'] != group['display_name']
                    or old['status'] != status
                    or old_orphaned_at != orphaned_at
                ):
                    conn.execute(
                        """
                        UPDATE iptv_logical_channels
                        SET canonical_key=?, display_name=?, status=?,
                            orphaned_at=?, updated_at=?
                        WHERE id=?
                        """,
                        (
                            group['canonical_key'], group['display_name'],
                            status, orphaned_at, now, logical_id,
                        ),
                    )
                    logical_by_id[logical_id].update(
                        canonical_key=group['canonical_key'],
                        display_name=group['display_name'],
                        status=status,
                        orphaned_at=orphaned_at,
                    )
                    updated_logical_count += 1

            def _add_members(logical_id: str, channel_ids: list[int]) -> None:
                nonlocal created_member_count
                for channel_id in channel_ids:
                    group = next(item for item in normalized_groups if channel_id in item['channel_ids'])
                    conn.execute(
                        """
                        INSERT INTO iptv_logical_channel_members(
                            logical_channel_id, channel_id, membership_reason,
                            membership_confidence, variant_type, created_at, updated_at
                        ) VALUES(?, ?, ?, ?, 'unknown', ?, ?)
                        """,
                        (
                            logical_id, channel_id,
                            group['membership_reason'],
                            group['membership_confidence'],
                            now, now,
                        ),
                    )
                    members_by_logical.setdefault(logical_id, set()).add(channel_id)
                    logical_by_channel[channel_id] = logical_id
                    created_member_count += 1

            for group in normalized_groups:
                channel_ids = group['channel_ids']
                candidates = sorted({logical_by_channel[channel_id] for channel_id in channel_ids if channel_id in logical_by_channel})
                unassigned = [channel_id for channel_id in channel_ids if channel_id not in logical_by_channel]

                if len(candidates) > 1:
                    for logical_id in candidates:
                        _update_logical(logical_id, logical_by_id[logical_id], 'merge_conflict')
                        touched_logical_ids.add(logical_id)
                    merge_conflicts.append({
                        'canonical_key': group['canonical_key'],
                        'logical_channel_ids': candidates,
                        'channel_ids': sorted(channel_ids),
                    })
                    if unassigned:
                        new_id = _create_logical(group, 'merge_conflict')
                        _add_members(new_id, unassigned)
                        touched_logical_ids.add(new_id)
                    continue

                if len(candidates) == 1 and candidates[0] not in split_ids:
                    logical_id = candidates[0]
                    _update_logical(logical_id, group, 'active')
                    reused_logical_count += 1
                    handled_logical_ids.add(logical_id)
                    touched_logical_ids.add(logical_id)
                    if unassigned:
                        _add_members(logical_id, unassigned)
                    continue

                if len(candidates) == 1 and candidates[0] in split_ids:
                    logical_id = candidates[0]
                    _update_logical(logical_id, logical_by_id[logical_id], 'split_conflict')
                    touched_logical_ids.add(logical_id)
                    if unassigned:
                        new_id = _create_logical(group, 'split_conflict')
                        _add_members(new_id, unassigned)
                        touched_logical_ids.add(new_id)
                    continue

                new_id = _create_logical(group, 'active')
                _add_members(new_id, channel_ids)
                touched_logical_ids.add(new_id)

            for logical_id, row in logical_by_id.items():
                member_count = len(members_by_logical.get(logical_id, set()))
                if member_count == 0:
                    _update_logical(logical_id, row, 'orphaned')
                elif logical_id in split_ids and logical_id not in touched_logical_ids:
                    _update_logical(logical_id, row, 'split_conflict')

            conn.commit()
            return {
                'created_logical_count': created_logical_count,
                'reused_logical_count': reused_logical_count,
                'updated_logical_count': updated_logical_count,
                'created_member_count': created_member_count,
                'removed_member_count': removed_member_count,
                'split_conflicts': split_conflicts,
                'merge_conflicts': merge_conflicts,
            }
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    return await asyncio.to_thread(_sync)


# ── Market install state ──

async def get_market_install(package_id: str) -> dict | None:
    def _get():
        conn = _connect()
        row = conn.execute(
            "SELECT * FROM market_packages_installed WHERE package_id=?",
            (package_id,),
        ).fetchone()
        conn.close()
        return dict(row) if row else None
    return await asyncio.to_thread(_get)


async def get_package_assets(package_id: str, *, package_version: str = '', active_only: bool = True) -> list[dict]:
    """Return verified package asset metadata, never arbitrary filesystem paths."""
    def _get():
        conn = _connect()
        try:
            where = ["package_id=?"]
            params: list[Any] = [package_id]
            if package_version:
                where.append("package_version=?")
                params.append(package_version)
            if active_only:
                where.append("state='active'")
            rows = conn.execute(
                f"SELECT * FROM package_assets WHERE {' AND '.join(where)} ORDER BY asset_id",
                params,
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()
    return await asyncio.to_thread(_get)


async def get_referenced_package_asset_paths(paths: list[str]) -> set[str]:
    """Return stored paths still referenced by any package asset row."""
    values = list(dict.fromkeys(str(path) for path in paths if str(path)))
    if not values:
        return set()

    def _get() -> set[str]:
        conn = _connect()
        try:
            placeholders = ','.join('?' for _ in values)
            rows = conn.execute(
                f"""
                SELECT DISTINCT stored_path
                FROM package_assets
                WHERE stored_path IN ({placeholders})
                  AND state IN ('staged', 'active', 'retained')
                """,
                values,
            ).fetchall()
            return {str(row['stored_path']) for row in rows}
        finally:
            conn.close()

    return await asyncio.to_thread(_get)


async def get_active_package_asset(package_id: str, asset_id: str) -> dict | None:
    def _get():
        conn = _connect()
        try:
            row = conn.execute(
                """
                SELECT a.*, i.installed_version
                FROM package_assets AS a
                JOIN market_packages_installed AS i ON i.package_id=a.package_id
                WHERE a.package_id=? AND a.asset_id=?
                  AND a.package_version=i.installed_version
                  AND a.state='active'
                """,
                (package_id, asset_id),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()
    return await asyncio.to_thread(_get)


async def get_logical_channel_logo_bindings(logical_channel_id: str) -> list[dict]:
    def _get():
        conn = _connect()
        try:
            rows = conn.execute(
                """
                SELECT b.*, a.media_type, a.sha256, a.size_bytes, a.stored_path,
                       i.installed_version
                FROM logical_channel_logo_bindings AS b
                JOIN package_assets AS a
                  ON a.package_id=b.package_id
                 AND a.asset_id=b.asset_id
                 AND a.package_version=b.package_version
                 AND a.state='active'
                JOIN market_packages_installed AS i
                  ON i.package_id=b.package_id
                 AND i.installed_version=b.package_version
                WHERE b.logical_channel_id=?
                  AND b.enabled=1
                  AND b.lifecycle_state='active'
                ORDER BY CASE b.binding_type WHEN 'content_package' THEN 0 ELSE 1 END,
                         b.priority DESC,
                         CASE b.match_type
                           WHEN 'stable_identity' THEN 0
                           WHEN 'exact' THEN 1
                           WHEN 'alias' THEN 2
                           ELSE 3
                         END,
                         b.package_id ASC,
                         b.asset_id ASC
                """,
                (logical_channel_id,),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()
    return await asyncio.to_thread(_get)


async def get_logical_channel_logo_bindings_many(logical_channel_ids: list[str]) -> dict[str, list[dict]]:
    ids = list(dict.fromkeys(str(item) for item in logical_channel_ids if str(item)))
    if not ids:
        return {}

    def _get():
        conn = _connect()
        try:
            placeholders = ",".join("?" for _ in ids)
            rows = conn.execute(
                f"""
                SELECT b.*, a.media_type, a.sha256, a.size_bytes, a.stored_path,
                       i.installed_version
                FROM logical_channel_logo_bindings AS b
                JOIN package_assets AS a
                  ON a.package_id=b.package_id
                 AND a.asset_id=b.asset_id
                 AND a.package_version=b.package_version
                 AND a.state='active'
                JOIN market_packages_installed AS i
                  ON i.package_id=b.package_id
                 AND i.installed_version=b.package_version
                WHERE b.logical_channel_id IN ({placeholders})
                  AND b.enabled=1
                  AND b.lifecycle_state='active'
                ORDER BY b.logical_channel_id,
                         CASE b.binding_type WHEN 'content_package' THEN 0 ELSE 1 END,
                         b.priority DESC,
                         CASE b.match_type
                           WHEN 'stable_identity' THEN 0
                           WHEN 'exact' THEN 1
                           WHEN 'alias' THEN 2
                           ELSE 3
                         END,
                         b.package_id ASC,
                         b.asset_id ASC
                """,
                ids,
            ).fetchall()
            result: dict[str, list[dict]] = {item: [] for item in ids}
            for row in rows:
                result.setdefault(str(row['logical_channel_id']), []).append(dict(row))
            return result
        finally:
            conn.close()
    return await asyncio.to_thread(_get)


async def replace_package_logo_state(
    package_id: str,
    package_version: str,
    assets: list[dict],
    bindings: list[dict],
) -> None:
    """Atomically publish one package's verified assets and logical bindings."""
    def _replace():
        conn = _connect()
        try:
            with conn:
                now = datetime.now(timezone.utc).isoformat()
                _replace_package_logo_state_conn(
                    conn,
                    package_id,
                    package_version,
                    assets,
                    bindings,
                    now=now,
                )
        finally:
            conn.close()
    await asyncio.to_thread(_replace)


async def list_market_installs() -> list[dict]:
    def _list():
        conn = _connect()
        rows = conn.execute("SELECT * FROM market_packages_installed ORDER BY installed_at DESC").fetchall()
        conn.close()
        return [dict(r) for r in rows]
    return await asyncio.to_thread(_list)


async def update_market_install_check_state(
    package_id: str,
    *,
    checked_at: str | None = None,
    remote_version: str = '',
    version_status: str,
) -> bool:
    package_id = _normalize_identifier(package_id, 'package_id')
    if version_status not in MARKET_VERSION_STATUSES:
        raise ValueError(f'非法 Market 版本状态: {version_status}')
    if not isinstance(remote_version, str):
        raise TypeError('remote_version 必须是字符串')
    checked_at_value = _normalize_timestamp(checked_at, 'checked_at')
    remote_version_value = remote_version.strip()[:256]

    def _update():
        conn = _connect()
        try:
            with conn:
                cursor = conn.execute(
                    """
                    UPDATE market_packages_installed SET
                        last_checked_at=?,
                        remote_version=?,
                        version_status=?
                    WHERE package_id=?
                    """,
                    (checked_at_value, remote_version_value, version_status, package_id),
                )
                return cursor.rowcount == 1
        finally:
            conn.close()

    return await asyncio.to_thread(_update)


async def mark_market_install_update_started(
    package_id: str,
    *,
    run_token: str,
    started_at: str | None = None,
) -> bool:
    package_id = _normalize_identifier(package_id, 'package_id')
    run_token = _normalize_identifier(run_token, 'run_token')
    started_at_value = _normalize_timestamp(started_at, 'started_at')

    def _start():
        conn = _connect()
        try:
            with conn:
                cursor = conn.execute(
                    """
                    UPDATE market_packages_installed SET
                        last_update_started_at=?,
                        last_update_finished_at='',
                        last_update_status='running',
                        last_update_error='',
                        last_update_run_token=?
                    WHERE package_id=?
                    """,
                    (started_at_value, run_token, package_id),
                )
                return cursor.rowcount == 1
        finally:
            conn.close()

    return await asyncio.to_thread(_start)


async def complete_market_install_update(
    package_id: str,
    *,
    run_token: str,
    status: str,
    finished_at: str | None = None,
    error: str | None = '',
) -> bool:
    package_id = _normalize_identifier(package_id, 'package_id')
    run_token = _normalize_identifier(run_token, 'run_token')
    if status not in MARKET_UPDATE_FINAL_STATUSES:
        raise ValueError(f'非法 Market 更新完成状态: {status}')
    finished_at_value = _normalize_timestamp(finished_at, 'finished_at')
    error_value = _normalize_error(error)

    def _complete():
        conn = _connect()
        try:
            with conn:
                cursor = conn.execute(
                    """
                    UPDATE market_packages_installed SET
                        last_update_finished_at=?,
                        last_update_status=?,
                        last_update_error=?
                    WHERE package_id=?
                      AND last_update_run_token=?
                      AND last_update_status='running'
                    """,
                    (finished_at_value, status, error_value, package_id, run_token),
                )
                return cursor.rowcount == 1
        finally:
            conn.close()

    return await asyncio.to_thread(_complete)


async def upsert_market_install(
    package_id: str,
    market_url: str,
    installed_subscription_id: int,
    installed_version: str = '',
    metadata_json: str = '',
    auto_update: int = 0,
):
    def _upsert():
        conn = _connect()
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """
            INSERT INTO market_packages_installed(
                package_id, market_url, installed_subscription_id, installed_version,
                installed_at, auto_update, metadata_json
            )
            VALUES(?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(package_id) DO UPDATE SET
                market_url=excluded.market_url,
                installed_subscription_id=excluded.installed_subscription_id,
                installed_version=excluded.installed_version,
                installed_at=excluded.installed_at,
                auto_update=excluded.auto_update,
                metadata_json=excluded.metadata_json
            """,
            (package_id, market_url, installed_subscription_id, installed_version, now, auto_update, metadata_json),
        )
        conn.commit()
        conn.close()
    await asyncio.to_thread(_upsert)


async def update_market_install(package_id: str, **kwargs):
    allowed = {"auto_update"}
    values = {key: value for key, value in kwargs.items() if key in allowed}
    if not values:
        return

    def _update():
        conn = _connect()
        sets = ', '.join(f"{k}=?" for k in values)
        conn.execute(f"UPDATE market_packages_installed SET {sets} WHERE package_id=?", (*values.values(), package_id))
        conn.commit()
        conn.close()
    await asyncio.to_thread(_update)


async def delete_market_install(package_id: str):
    def _delete():
        conn = _connect()
        conn.execute("DELETE FROM market_packages_installed WHERE package_id=?", (package_id,))
        conn.commit()
        conn.close()
    await asyncio.to_thread(_delete)


# ── Radio domain persistence ──

async def begin_radio_catalog_refresh(owner_identity: str) -> int:
    """Reserve a monotonically increasing publication generation for an owner."""
    owner = _normalize_identifier(owner_identity, "owner_identity")

    def _begin() -> int:
        conn = _connect()
        try:
            with conn:
                now = _utc_now()
                conn.execute(
                    """
                    INSERT INTO radio_catalog_states(
                        owner_identity, generation, published_generation,
                        last_success_at, last_error, updated_at
                    ) VALUES(?, 0, 0, '', '', ?)
                    ON CONFLICT(owner_identity) DO NOTHING
                    """,
                    (owner, now),
                )
                conn.execute(
                    """
                    UPDATE radio_catalog_states
                    SET generation=generation + 1, updated_at=?
                    WHERE owner_identity=?
                    """,
                    (now, owner),
                )
                row = conn.execute(
                    "SELECT generation FROM radio_catalog_states WHERE owner_identity=?",
                    (owner,),
                ).fetchone()
                if row is None:
                    raise RuntimeError("Radio catalog generation state was not created")
                return int(row[0])
        finally:
            conn.close()

    return await asyncio.to_thread(_begin)

async def apply_radio_catalog(
    owner_identity: str,
    rows: list[dict],
    *,
    now_unix: float,
    stale_grace_seconds: int = RADIO_CATALOG_DEFAULT_STALE_GRACE_SECONDS,
    generation: int | None = None,
) -> dict:
    """Atomically publish one Plugin Radio catalog into its own tables.

    The caller supplies already-validated rows.  A successful refresh never
    deletes a previous source: items omitted by the latest catalog become
    stale and remain addressable until the bounded grace window expires.
    """
    owner = _normalize_identifier(owner_identity, "owner_identity")
    if not isinstance(rows, list):
        raise TypeError("rows must be a list")
    now = _utc_now()
    stale_until = float(now_unix) + max(1, int(stale_grace_seconds))

    def _apply() -> dict:
        conn = _connect()
        try:
            with conn:
                if generation is not None:
                    # This conditional write obtains the SQLite writer lock
                    # before any station mutation.  A late generation then
                    # observes rowcount=0 and cannot partially publish.
                    current = conn.execute(
                        """
                        UPDATE radio_catalog_states
                        SET published_generation=?, last_success_at=?, last_error='', updated_at=?
                        WHERE owner_identity=? AND generation=?
                        """,
                        (int(generation), now, now, owner, int(generation)),
                    )
                    if current.rowcount != 1:
                        return {
                            "owner_identity": owner, "status": "stale",
                            "published": 0, "stale": 0, "generation": int(generation),
                        }
                existing = {
                    str(row["source_id"]): row
                    for row in conn.execute(
                        "SELECT source_id, station_id FROM radio_station_sources WHERE owner_identity=?",
                        (owner,),
                    ).fetchall()
                }
                incoming_ids: set[str] = set()
                incoming_station_ids = {str(row.get("station_id") or "") for row in rows}
                for row in rows:
                    source_id = _normalize_identifier(row.get("source_id"), "source_id")
                    station_id = _normalize_identifier(row.get("station_id"), "station_id")
                    incoming_ids.add(source_id)
                    conn.execute(
                        """
                        INSERT INTO radio_stations(
                            station_id, owner_identity, provider_key, provider_station_id,
                            name, logo_url, group_name, country, language, frequency,
                            metadata_json, lifecycle_state, catalog_ttl_seconds,
                            catalog_expires_at, last_catalog_success_at, last_catalog_error,
                            created_at, updated_at
                        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, '', ?, ?)
                        ON CONFLICT(station_id) DO UPDATE SET
                            owner_identity=excluded.owner_identity,
                            provider_key=excluded.provider_key,
                            provider_station_id=excluded.provider_station_id,
                            name=excluded.name,
                            logo_url=excluded.logo_url,
                            group_name=excluded.group_name,
                            country=excluded.country,
                            language=excluded.language,
                            frequency=excluded.frequency,
                            metadata_json=excluded.metadata_json,
                            lifecycle_state='active',
                            catalog_ttl_seconds=excluded.catalog_ttl_seconds,
                            catalog_expires_at=excluded.catalog_expires_at,
                            last_catalog_success_at=excluded.last_catalog_success_at,
                            last_catalog_error='',
                            updated_at=excluded.updated_at
                        """,
                        (
                            station_id, owner, row["provider_key"], row["provider_station_id"],
                            row["name"], row.get("logo_url", ""), row.get("group_name", ""),
                            row.get("country", ""), row.get("language", ""), row.get("frequency", ""),
                            json.dumps(row.get("metadata") or {}, ensure_ascii=False, separators=(",", ":")),
                            int(row["ttl_seconds"]), float(row["catalog_expires_at"]), now, now, now,
                        ),
                    )
                    conn.execute(
                        """
                        INSERT INTO radio_station_sources(
                            source_id, station_id, owner_identity, provider_key,
                            provider_station_id, source_discriminator, reference_json, source_revision,
                            explicit_priority, health_status, last_success_at, last_error,
                            lifecycle_state, catalog_ttl_seconds, catalog_expires_at,
                            resolve_expires_at, created_at, updated_at
                        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 'unknown', '', '', 'active', ?, ?, 0, ?, ?)
                        ON CONFLICT(source_id) DO UPDATE SET
                            station_id=excluded.station_id,
                            owner_identity=excluded.owner_identity,
                            provider_key=excluded.provider_key,
                            provider_station_id=excluded.provider_station_id,
                            source_discriminator=excluded.source_discriminator,
                            reference_json=excluded.reference_json,
                            source_revision=excluded.source_revision,
                            explicit_priority=excluded.explicit_priority,
                            lifecycle_state='active',
                            catalog_ttl_seconds=excluded.catalog_ttl_seconds,
                            catalog_expires_at=excluded.catalog_expires_at,
                            last_error='',
                            updated_at=excluded.updated_at
                        """,
                        (
                            source_id, station_id, owner, row["provider_key"], row["provider_station_id"],
                            row.get("source_discriminator", ""),
                            json.dumps(row["reference"], ensure_ascii=False, separators=(",", ":")),
                            row["source_revision"], int(row.get("explicit_priority", 0)),
                            int(row["ttl_seconds"]), float(row["catalog_expires_at"]), now, now,
                        ),
                    )

                for source_id, previous in existing.items():
                    if source_id in incoming_ids:
                        continue
                    conn.execute(
                        """
                        UPDATE radio_station_sources
                        SET lifecycle_state='stale', catalog_expires_at=?, updated_at=?
                        WHERE source_id=? AND owner_identity=?
                        """,
                        (stale_until, now, source_id, owner),
                    )
                    if str(previous["station_id"]) not in incoming_station_ids:
                        conn.execute(
                            """
                            UPDATE radio_stations
                            SET lifecycle_state='stale', catalog_expires_at=?, updated_at=?
                            WHERE station_id=? AND owner_identity=?
                            """,
                            (stale_until, now, previous["station_id"], owner),
                        )
                conn.execute(
                    """
                    UPDATE radio_stations
                    SET last_catalog_success_at=?, last_catalog_error=''
                    WHERE owner_identity=?
                    """,
                    (now, owner),
                )
            return {
                "owner_identity": owner, "status": "success", "published": len(rows),
                "stale": len(set(existing) - incoming_ids),
                **({"generation": int(generation)} if generation is not None else {}),
            }
        finally:
            conn.close()

    return await asyncio.to_thread(_apply)


async def record_radio_catalog_failure(
    owner_identity: str, error: str, *, generation: int | None = None,
) -> None:
    owner = _normalize_identifier(owner_identity, "owner_identity")
    message = _normalize_error(error)

    def _record() -> None:
        conn = _connect()
        try:
            with conn:
                now = _utc_now()
                if generation is not None:
                    current = conn.execute(
                        """
                        UPDATE radio_catalog_states
                        SET last_error=?, updated_at=?
                        WHERE owner_identity=? AND generation=?
                        """,
                        (message, now, owner, int(generation)),
                    )
                    if current.rowcount != 1:
                        return
                else:
                    conn.execute(
                        """
                        UPDATE radio_catalog_states
                        SET last_error=?, updated_at=?
                        WHERE owner_identity=?
                        """,
                        (message, now, owner),
                    )
                conn.execute(
                    "UPDATE radio_stations SET last_catalog_error=?, updated_at=? WHERE owner_identity=?",
                    (message, now, owner),
                )
                conn.execute(
                    "UPDATE radio_station_sources SET last_error=?, updated_at=? WHERE owner_identity=?",
                    (message, now, owner),
                )
        finally:
            conn.close()

    await asyncio.to_thread(_record)


async def prune_radio_catalog(
    owner_identity: str = "", *, now_unix: float | None = None,
    stale_grace_seconds: int = RADIO_CATALOG_DEFAULT_STALE_GRACE_SECONDS,
) -> dict[str, int]:
    """Remove only expired Radio projections with no live source reference."""
    owner = owner_identity.strip() if isinstance(owner_identity, str) else ""
    now_value = float(now_unix if now_unix is not None else time.time())
    grace = max(1, int(stale_grace_seconds))

    def _prune() -> dict[str, int]:
        conn = _connect()
        try:
            with conn:
                owner_clause = "" if not owner else " AND owner_identity=?"
                owner_args = () if not owner else (owner,)
                snapshots = conn.execute(
                    "DELETE FROM radio_programme_snapshots WHERE expires_at_unix<=?" + owner_clause,
                    (now_value, *owner_args),
                ).rowcount
                sources = conn.execute(
                    """
                    DELETE FROM radio_station_sources
                    WHERE (lifecycle_state='expired'
                       OR (lifecycle_state='stale' AND catalog_expires_at<=?)
                       OR (lifecycle_state='active' AND catalog_expires_at + ? <= ?))
                    """ + (" AND owner_identity=?" if owner else ""),
                    (now_value, grace, now_value, *owner_args),
                ).rowcount
                stations = conn.execute(
                    """
                    DELETE FROM radio_stations
                    WHERE NOT EXISTS(
                        SELECT 1 FROM radio_station_sources r WHERE r.station_id=radio_stations.station_id
                    )
                    """ + (" AND owner_identity=?" if owner else ""),
                    owner_args,
                ).rowcount
            return {"snapshots": snapshots, "sources": sources, "stations": stations}
        finally:
            conn.close()

    return await asyncio.to_thread(_prune)


def _radio_lifecycle(
    row: dict,
    now_unix: float,
    *,
    stale_grace_seconds: int = RADIO_CATALOG_DEFAULT_STALE_GRACE_SECONDS,
) -> str:
    state = str(row.get("lifecycle_state") or "active")
    expiry = float(row.get("catalog_expires_at") or 0)
    if state == "stale" and expiry and now_unix > expiry:
        return "expired"
    if state == "active" and expiry and now_unix > expiry + max(1, int(stale_grace_seconds)):
        return "expired"
    if state == "active" and expiry and now_unix > expiry:
        return "stale"
    return state


def _radio_catalog_status(row: dict, station_state: str) -> str:
    published = int(row.get("catalog_published_generation") or 0)
    error = str(row.get("catalog_last_error") or "")
    if published <= 0:
        return "failed" if error else "never"
    if station_state == "expired":
        return "expired"
    if error:
        return "stale" if station_state == "stale" else "degraded"
    return "success"


async def list_radio_stations(
    *,
    owner_identity: str = "",
    include_expired: bool = False,
    now_unix: float | None = None,
    visible_owner_identities: set[str] | frozenset[str] | None = None,
) -> list[dict]:
    now_value = float(now_unix if now_unix is not None else time.time())

    def _list() -> list[dict]:
        conn = _connect()
        try:
            clauses = ["1=1"]
            values: list[Any] = []
            if owner_identity:
                clauses.append("s.owner_identity=?")
                values.append(owner_identity)
            if visible_owner_identities is not None:
                owners = sorted({str(item).strip() for item in visible_owner_identities if str(item).strip()})
                if not owners:
                    return []
                placeholders = ",".join("?" for _ in owners)
                clauses.append(f"s.owner_identity IN ({placeholders})")
                values.extend(owners)
            rows = conn.execute(
                """
                SELECT s.*, r.source_id, r.provider_key AS source_provider_key,
                       r.provider_station_id AS source_provider_station_id,
                       r.source_discriminator,
                       r.reference_json, r.source_revision, r.explicit_priority,
                       r.health_status, r.last_success_at, r.last_error AS source_last_error,
                       r.lifecycle_state AS source_lifecycle_state,
                       r.catalog_ttl_seconds AS source_catalog_ttl_seconds,
                       r.catalog_expires_at AS source_catalog_expires_at,
                       r.resolve_expires_at,
                       c.generation AS catalog_generation,
                       c.published_generation AS catalog_published_generation,
                       c.last_success_at AS catalog_last_success_at,
                       c.last_error AS catalog_last_error
                FROM radio_stations s
                LEFT JOIN radio_station_sources r ON r.station_id=s.station_id
                LEFT JOIN radio_catalog_states c ON c.owner_identity=s.owner_identity
                WHERE """ + " AND ".join(clauses) + "\n"
                "ORDER BY s.owner_identity, s.provider_key, s.provider_station_id, r.explicit_priority, r.source_id",
                values,
            ).fetchall()
            result: list[dict] = []
            by_station: dict[str, dict] = {}
            for raw in rows:
                row = dict(raw)
                station_id = str(row["station_id"])
                station = by_station.get(station_id)
                if station is None:
                    station = {
                        "station_id": station_id,
                        "owner_identity": row["owner_identity"],
                        "provider_key": row["provider_key"],
                        "provider_station_id": row["provider_station_id"],
                        "name": row["name"],
                        "logo_url": row["logo_url"],
                        "group_name": row["group_name"],
                        "country": row["country"],
                        "language": row["language"],
                        "frequency": row["frequency"],
                        "metadata": json.loads(row["metadata_json"] or "{}"),
                        "lifecycle_state": _radio_lifecycle(row, now_value),
                        "catalog_expires_at": row["catalog_expires_at"],
                        "catalog_generation": int(row["catalog_generation"] or 0),
                        "catalog_published_generation": int(row["catalog_published_generation"] or 0),
                        "catalog_last_success_at": row["catalog_last_success_at"] or "",
                        "catalog_last_error": row["catalog_last_error"] or "",
                        "catalog_status": "",
                        "sources": [],
                    }
                    by_station[station_id] = station
                    result.append(station)
                if row.get("source_id"):
                    source_state = _radio_lifecycle({
                        "lifecycle_state": row["source_lifecycle_state"],
                        "catalog_expires_at": row["source_catalog_expires_at"],
                    }, now_value)
                    if include_expired or source_state != "expired":
                        station["sources"].append({
                            "source_id": row["source_id"],
                            "owner_identity": row["owner_identity"],
                            "provider_key": row["source_provider_key"],
                            "provider_station_id": row["source_provider_station_id"],
                            "source_discriminator": row["source_discriminator"] or "",
                            "reference": json.loads(row["reference_json"] or "{}"),
                            "source_revision": row["source_revision"],
                            "explicit_priority": row["explicit_priority"],
                            "health_status": row["health_status"],
                            "last_success_at": row["last_success_at"],
                            "last_error": row["source_last_error"],
                            "lifecycle_state": source_state,
                            "catalog_expires_at": row["source_catalog_expires_at"],
                            "resolve_expires_at": row["resolve_expires_at"],
                        })
                station["catalog_status"] = _radio_catalog_status(row, station["lifecycle_state"])
            if not include_expired:
                result = [item for item in result if item["lifecycle_state"] != "expired" and item["sources"]]
            return result
        finally:
            conn.close()

    return await asyncio.to_thread(_list)


async def list_radio_catalog_states(
    *,
    owner_identities: set[str] | frozenset[str] | None = None,
    now_unix: float | None = None,
) -> list[dict]:
    """Return bounded provider-level catalog state without exposing run handles."""
    now_value = float(now_unix if now_unix is not None else time.time())

    def _list() -> list[dict]:
        conn = _connect()
        try:
            owners = None if owner_identities is None else sorted(
                {str(item).strip() for item in owner_identities if str(item).strip()}
            )
            if owners == []:
                return []
            clauses = ""
            values: list[Any] = []
            if owners is not None:
                clauses = " WHERE owner_identity IN (" + ",".join("?" for _ in owners) + ")"
                values.extend(owners)
            states = conn.execute(
                "SELECT * FROM radio_catalog_states" + clauses + " ORDER BY owner_identity",
                values,
            ).fetchall()
            result: list[dict] = []
            for raw in states:
                state = dict(raw)
                station_rows = conn.execute(
                    "SELECT lifecycle_state, catalog_expires_at FROM radio_stations WHERE owner_identity=?",
                    (state["owner_identity"],),
                ).fetchall()
                lifecycle_states = [
                    _radio_lifecycle(dict(row), now_value)
                    for row in station_rows
                ]
                if int(state.get("published_generation") or 0) <= 0:
                    status = "failed" if state.get("last_error") else "never"
                elif not lifecycle_states or all(item == "expired" for item in lifecycle_states):
                    status = "expired"
                elif state.get("last_error"):
                    status = "stale" if any(item == "stale" for item in lifecycle_states) else "degraded"
                else:
                    status = "success"
                result.append({
                    "owner_identity": state["owner_identity"],
                    "generation": int(state.get("generation") or 0),
                    "published_generation": int(state.get("published_generation") or 0),
                    "last_success_at": state.get("last_success_at") or "",
                    "last_error": state.get("last_error") or "",
                    "updated_at": state.get("updated_at") or "",
                    "status": status,
                    "station_count": sum(item != "expired" for item in lifecycle_states),
                })
            return result
        finally:
            conn.close()

    return await asyncio.to_thread(_list)


async def get_radio_station(
    station_id: str,
    *,
    include_expired: bool = False,
    now_unix: float | None = None,
    visible_owner_identities: set[str] | frozenset[str] | None = None,
) -> dict | None:
    rows = await list_radio_stations(
        include_expired=include_expired,
        now_unix=now_unix,
        visible_owner_identities=visible_owner_identities,
    )
    return next((row for row in rows if row["station_id"] == station_id), None)


async def get_radio_station_source(source_id: str, *, station_id: str = "", include_expired: bool = False, now_unix: float | None = None) -> dict | None:
    stations = await list_radio_stations(include_expired=include_expired, now_unix=now_unix)
    for station in stations:
        if station_id and station["station_id"] != station_id:
            continue
        for source in station["sources"]:
            if source["source_id"] == source_id:
                return {**source, "station_id": station["station_id"], "station_name": station["name"]}
    return None


async def update_radio_source_health(
    source_id: str,
    *,
    expected_source_revision: str | None = None,
    success: bool,
    error: str = "",
    resolve_expires_at: float = 0,
) -> None:
    message = _normalize_error(error)

    def _update() -> None:
        conn = _connect()
        try:
            with conn:
                now = _utc_now()
                where = "source_id=?"
                values: list[Any] = [
                    "healthy" if success else "unhealthy", now if success else "",
                    "" if success else message, float(resolve_expires_at) if success else 0,
                    now, source_id,
                ]
                if expected_source_revision is not None:
                    where += " AND source_revision=?"
                    values.append(str(expected_source_revision))
                conn.execute(
                    f"""
                    UPDATE radio_station_sources
                    SET health_status=?, last_success_at=?, last_error=?,
                        resolve_expires_at=?, updated_at=?
                    WHERE {where}
                    """,
                    values,
                )
        finally:
            conn.close()

    await asyncio.to_thread(_update)


async def upsert_radio_programme_snapshot(
    source: dict,
    snapshot: dict,
    *,
    updated_at_unix: float,
    ttl_seconds: int,
) -> dict:
    """Persist one validated Radio programme snapshot atomically.

    This table is deliberately not shared with the TV EPG projection.  The
    caller has already validated the Plugin payload and supplies the explicit
    source row that authorized the mutation.
    """
    source_id = _normalize_identifier(source.get("source_id"), "source_id")
    station_id = _normalize_identifier(source.get("station_id"), "station_id")
    owner_identity = _normalize_identifier(source.get("owner_identity"), "owner_identity")
    provider_key = _normalize_identifier(source.get("provider_key"), "provider_key")
    provider_station_id = _normalize_identifier(source.get("provider_station_id"), "provider_station_id")
    source_revision = _normalize_identifier(source.get("source_revision"), "source_revision")
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("programmes"), list):
        raise TypeError("snapshot must be a validated Radio programme object")
    if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int):
        raise TypeError("ttl_seconds must be an integer")
    ttl = max(1, min(ttl_seconds, 7 * 24 * 60 * 60))
    revision = _normalize_identifier(snapshot.get("revision"), "revision")
    programmes_json = json.dumps(
        snapshot["programmes"], ensure_ascii=False, separators=(",", ":"), allow_nan=False,
    )
    updated = float(updated_at_unix)
    expires = updated + ttl

    def _upsert() -> dict:
        conn = _connect()
        try:
            now = _utc_now()
            with conn:
                conn.execute(
                    """
                    INSERT INTO radio_programme_snapshots(
                        source_id, station_id, owner_identity, provider_key,
                        provider_station_id, source_revision, revision, programmes_json,
                        updated_at_unix, expires_at_unix, created_at, updated_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(source_id) DO UPDATE SET
                        station_id=excluded.station_id,
                        owner_identity=excluded.owner_identity,
                        provider_key=excluded.provider_key,
                        provider_station_id=excluded.provider_station_id,
                        source_revision=excluded.source_revision,
                        revision=excluded.revision,
                        programmes_json=excluded.programmes_json,
                        updated_at_unix=excluded.updated_at_unix,
                        expires_at_unix=excluded.expires_at_unix,
                        updated_at=excluded.updated_at
                    """,
                    (
                        source_id, station_id, owner_identity, provider_key,
                        provider_station_id, source_revision, revision, programmes_json,
                        updated, expires, now, now,
                    ),
                )
            return {
                "source_id": source_id,
                "station_id": station_id,
                "owner_identity": owner_identity,
                "provider_key": provider_key,
                "provider_station_id": provider_station_id,
                "source_revision": source_revision,
                "revision": revision,
                "programmes": json.loads(programmes_json),
                "updated_at_unix": updated,
                "expires_at_unix": expires,
            }
        finally:
            conn.close()

    return await asyncio.to_thread(_upsert)


async def get_radio_programme_snapshot(
    source_id: str,
    *,
    now_unix: float | None = None,
) -> dict | None:
    source_key = _normalize_identifier(source_id, "source_id")
    now = float(now_unix if now_unix is not None else time.time())

    def _get() -> dict | None:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT * FROM radio_programme_snapshots WHERE source_id=?",
                (source_key,),
            ).fetchone()
            if row is None:
                return None
            value = dict(row)
            if float(value.get("expires_at_unix") or 0) <= now:
                return None
            value["programmes"] = json.loads(value.pop("programmes_json") or "[]")
            return value
        finally:
            conn.close()

    return await asyncio.to_thread(_get)


# ── Plugin package persistence ──

async def get_plugin_installation(publisher_id: str, plugin_id: str) -> dict | None:
    def _get():
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT * FROM plugin_installations WHERE publisher_id=? AND plugin_id=?",
                (publisher_id, plugin_id),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()
    return await asyncio.to_thread(_get)


async def list_plugin_installations() -> list[dict]:
    def _list():
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT * FROM plugin_installations ORDER BY publisher_id, plugin_id"
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()
    return await asyncio.to_thread(_list)


async def get_plugin_permission_approval(
    publisher_id: str, plugin_id: str, permission_name: str, permission_fingerprint: str,
) -> dict | None:
    def _get():
        conn = _connect()
        try:
            row = conn.execute(
                """SELECT * FROM plugin_permission_approvals
                   WHERE publisher_id=? AND plugin_id=? AND permission_name=? AND permission_fingerprint=?""",
                (publisher_id, plugin_id, permission_name, permission_fingerprint),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()
    return await asyncio.to_thread(_get)


async def list_plugin_permission_approvals(publisher_id: str = "", plugin_id: str = "") -> list[dict]:
    def _list():
        conn = _connect()
        try:
            query = "SELECT * FROM plugin_permission_approvals"
            values: list[str] = []
            clauses = []
            if publisher_id:
                clauses.append("publisher_id=?")
                values.append(publisher_id)
            if plugin_id:
                clauses.append("plugin_id=?")
                values.append(plugin_id)
            if clauses:
                query += " WHERE " + " AND ".join(clauses)
            query += " ORDER BY publisher_id, plugin_id, permission_name"
            return [dict(row) for row in conn.execute(query, values).fetchall()]
        finally:
            conn.close()
    return await asyncio.to_thread(_list)


async def set_plugin_permission_approval(
    publisher_id: str, plugin_id: str, permission_name: str, permission_fingerprint: str,
    *, approved: bool, actor: str, manifest_version: str,
) -> dict:
    def _set():
        conn = _connect()
        try:
            with conn:
                now = _utc_now()
                conn.execute(
                    """INSERT INTO plugin_permission_approvals(
                           publisher_id, plugin_id, permission_name, permission_fingerprint,
                           approved, approved_at, approved_by, revoked_at, revoked_by,
                           manifest_version, updated_at)
                       VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(publisher_id, plugin_id, permission_name, permission_fingerprint) DO UPDATE SET
                           approved=excluded.approved,
                           approved_at=excluded.approved_at,
                           approved_by=excluded.approved_by,
                           revoked_at=excluded.revoked_at,
                           revoked_by=excluded.revoked_by,
                           manifest_version=excluded.manifest_version,
                           updated_at=excluded.updated_at""",
                    (publisher_id, plugin_id, permission_name, permission_fingerprint,
                     1 if approved else 0, now if approved else "", actor if approved else "",
                     "" if approved else now, "" if approved else actor, manifest_version, now),
                )
                row = conn.execute(
                    """SELECT * FROM plugin_permission_approvals
                       WHERE publisher_id=? AND plugin_id=? AND permission_name=? AND permission_fingerprint=?""",
                    (publisher_id, plugin_id, permission_name, permission_fingerprint),
                ).fetchone()
                return dict(row)
        finally:
            conn.close()
    return await asyncio.to_thread(_set)


async def list_plugin_artifacts(publisher_id: str, plugin_id: str, *, state: str = '') -> list[dict]:
    def _list():
        conn = _connect()
        try:
            if state:
                rows = conn.execute(
                    "SELECT * FROM plugin_artifacts WHERE publisher_id=? AND plugin_id=? AND state=? ORDER BY version, sha256",
                    (publisher_id, plugin_id, state),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM plugin_artifacts WHERE publisher_id=? AND plugin_id=? ORDER BY version, sha256",
                    (publisher_id, plugin_id),
                ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()
    return await asyncio.to_thread(_list)


async def list_plugin_artifact_references() -> list[str]:
    """Return every durable artifact path that the database still references.

    The installation row is the active authority, while ``plugin_artifacts``
    also retains candidate/previous-version rows during lifecycle transitions.
    Keeping both sets in one read prevents filesystem cleanup from treating a
    temporarily inconsistent projection as an orphan.
    """
    def _list() -> list[str]:
        conn = _connect()
        try:
            rows = conn.execute(
                """
                SELECT artifact_path AS path
                  FROM plugin_installations
                 WHERE artifact_path IS NOT NULL AND artifact_path != ''
                UNION
                SELECT path
                  FROM plugin_artifacts
                 WHERE path IS NOT NULL AND path != ''
                """
            ).fetchall()
            return [str(row["path"]) for row in rows]
        finally:
            conn.close()
    return await asyncio.to_thread(_list)


async def begin_plugin_candidate(
    *,
    publisher_id: str,
    plugin_id: str,
    version: str,
    trust_state: str,
    source_key: str,
    source_package_id: str,
    manifest_json: str,
    manifest_sha256: str,
    manifest_signature_json: str = '{}',
    artifact_sha256: str,
    artifact_path: str,
    runtime_type: str,
    entrypoint: str,
    platform_os: str,
    platform_arch: str,
    trust_class: str = '',
) -> dict:
    def _begin():
        conn = _connect()
        try:
            with conn:
                now = _utc_now()
                effective_trust_class = trust_class or (
                    'developer_local' if trust_state == 'developer_local' else 'official'
                )
                if effective_trust_class not in {'official', 'developer_local'}:
                    raise RuntimeError('invalid plugin trust class')
                existing = conn.execute(
                    "SELECT * FROM plugin_installations WHERE publisher_id=? AND plugin_id=?",
                    (publisher_id, plugin_id),
                ).fetchone()
                if existing and existing['candidate_version']:
                    raise RuntimeError('plugin candidate already exists')
                conn.execute(
                    """
                    INSERT INTO plugin_artifacts(
                        publisher_id, plugin_id, version, platform_os, platform_arch,
                        sha256, path, runtime_type, entrypoint, state,
                        source_key, source_package_id, created_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 'candidate', ?, ?, ?)
                    ON CONFLICT(publisher_id, plugin_id, version, platform_os, platform_arch, sha256)
                    DO UPDATE SET
                        path=excluded.path,
                        runtime_type=excluded.runtime_type,
                        entrypoint=excluded.entrypoint,
                        state='candidate',
                        source_key=excluded.source_key,
                        source_package_id=excluded.source_package_id
                    """,
                    (
                        publisher_id, plugin_id, version, platform_os, platform_arch,
                        artifact_sha256, artifact_path, runtime_type, entrypoint,
                        source_key, source_package_id, now,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO plugin_installations(
                        publisher_id, plugin_id, installed_version, active_version,
                        candidate_version, enabled, trust_state, source_key,
                        trust_class,
                        source_package_id, manifest_json, manifest_sha256,
                        manifest_signature_json, artifact_sha256, artifact_path, runtime_type, entrypoint,
                        platform_os, platform_arch, lifecycle_state, quarantined,
                        last_activation_status, last_error, created_at, updated_at
                    ) VALUES(?, ?, ?, '', ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                             'candidate', 0, 'pending', '', ?, ?)
                    ON CONFLICT(publisher_id, plugin_id) DO UPDATE SET
                        candidate_version=excluded.candidate_version,
                        trust_class=excluded.trust_class,
                        lifecycle_state='candidate',
                        last_activation_status='pending',
                        last_error='',
                        updated_at=excluded.updated_at
                    """,
                    (
                        publisher_id, plugin_id, version, version, trust_state,
                        source_key, effective_trust_class, source_package_id, manifest_json, manifest_sha256,
                        manifest_signature_json, artifact_sha256, artifact_path, runtime_type, entrypoint,
                        platform_os, platform_arch, now, now,
                    ),
                )
                row = conn.execute(
                    "SELECT * FROM plugin_installations WHERE publisher_id=? AND plugin_id=?",
                    (publisher_id, plugin_id),
                ).fetchone()
                return dict(row)
        finally:
            conn.close()
    return await asyncio.to_thread(_begin)


async def activate_plugin_candidate(
    *,
    publisher_id: str,
    plugin_id: str,
    candidate_version: str,
    trust_state: str,
    source_key: str,
    source_package_id: str,
    manifest_json: str,
    manifest_sha256: str,
    manifest_signature_json: str = '{}',
    artifact_sha256: str,
    artifact_path: str,
    runtime_type: str,
    entrypoint: str,
    platform_os: str,
    platform_arch: str,
    environment: dict | None = None,
    trust_class: str = '',
) -> bool:
    def _activate():
        conn = _connect()
        try:
            with conn:
                now = _utc_now()
                effective_trust_class = trust_class or (
                    'developer_local' if trust_state == 'developer_local' else 'official'
                )
                if effective_trust_class not in {'official', 'developer_local'}:
                    raise RuntimeError('invalid plugin trust class')
                cursor = conn.execute(
                    """
                    UPDATE plugin_installations SET
                        installed_version=?, active_version=?, candidate_version='',
                        enabled=1, trust_state=?, trust_class=?, source_key=?, source_package_id=?,
                        manifest_json=?, manifest_sha256=?, manifest_signature_json=?, artifact_sha256=?,
                        artifact_path=?, runtime_type=?, entrypoint=?, platform_os=?,
                        platform_arch=?, lifecycle_state='active', quarantined=0,
                        last_activation_status='success', last_error='', updated_at=?
                    WHERE publisher_id=? AND plugin_id=? AND candidate_version=?
                    """,
                    (
                        candidate_version, candidate_version, trust_state, effective_trust_class, source_key,
                        source_package_id, manifest_json, manifest_sha256,
                        manifest_signature_json, artifact_sha256, artifact_path, runtime_type, entrypoint,
                        platform_os, platform_arch, now, publisher_id, plugin_id,
                        candidate_version,
                    ),
                )
                if cursor.rowcount != 1:
                    return False
                if environment:
                    conn.execute(
                        "UPDATE plugin_python_environments SET state='retained', updated_at=? "
                        "WHERE publisher_id=? AND plugin_id=? AND state='active'",
                        (now, publisher_id, plugin_id),
                    )
                    env_cursor = conn.execute(
                        """UPDATE plugin_python_environments SET state='active', updated_at=?
                           WHERE publisher_id=? AND plugin_id=? AND plugin_version=?
                             AND runtime_identity=? AND lock_digest=? AND state='candidate'""",
                        (now, publisher_id, plugin_id, candidate_version,
                         environment['runtime_identity'], environment['lock_digest']),
                    )
                    if env_cursor.rowcount != 1:
                        raise RuntimeError('plugin environment candidate state changed')
                conn.execute(
                    """
                    UPDATE plugin_artifacts SET state='retained'
                    WHERE publisher_id=? AND plugin_id=? AND state='active'
                    """,
                    (publisher_id, plugin_id),
                )
                conn.execute(
                    """
                    UPDATE plugin_artifacts SET state='active', path=?
                    WHERE publisher_id=? AND plugin_id=? AND version=?
                      AND platform_os=? AND platform_arch=? AND sha256=?
                    """,
                    (
                        artifact_path, publisher_id, plugin_id, candidate_version,
                        platform_os, platform_arch, artifact_sha256,
                    ),
                )
                return True
        finally:
            conn.close()
    # Candidate activation is the durable commit boundary.  SQLite work keeps
    # running in a worker thread after coroutine cancellation, so retain and
    # await that worker before propagating cancellation; callers can then
    # reconcile against a final, not in-flight, durable outcome.
    commit_task = asyncio.create_task(
        asyncio.to_thread(_activate),
        name=f"plugin-candidate-commit:{publisher_id}/{plugin_id}:{candidate_version}",
    )

    async def settle() -> None:
        try:
            await commit_task
        except BaseException:
            return

    settled = asyncio.create_task(settle(), name=f"settle:{commit_task.get_name()}")
    cancelled = False
    while not settled.done():
        try:
            await asyncio.shield(settled)
        except asyncio.CancelledError:
            cancelled = True
            current = asyncio.current_task()
            if current is not None:
                current.uncancel()
    if cancelled:
        raise asyncio.CancelledError
    return commit_task.result()


async def fail_plugin_candidate(
    publisher_id: str,
    plugin_id: str,
    candidate_version: str,
    error: str,
    *,
    preserve_unavailable: bool = False,
) -> None:
    def _fail():
        conn = _connect()
        try:
            with conn:
                row = conn.execute(
                    "SELECT active_version FROM plugin_installations WHERE publisher_id=? AND plugin_id=?",
                    (publisher_id, plugin_id),
                ).fetchone()
                conn.execute(
                    """
                    DELETE FROM plugin_artifacts
                    WHERE publisher_id=? AND plugin_id=? AND version=? AND state='candidate'
                    """,
                    (publisher_id, plugin_id, candidate_version),
                )
                if not row:
                    return
                if row['active_version']:
                    conn.execute(
                        """
                        UPDATE plugin_installations SET
                            candidate_version='', lifecycle_state=?,
                            last_activation_status='failed', last_error=?, updated_at=?
                        WHERE publisher_id=? AND plugin_id=? AND candidate_version=?
                        """,
                        ("unavailable" if preserve_unavailable else "active", error[:1024], _utc_now(),
                         publisher_id, plugin_id, candidate_version),
                    )
                else:
                    conn.execute(
                        "DELETE FROM plugin_installations WHERE publisher_id=? AND plugin_id=?",
                        (publisher_id, plugin_id),
                    )
        finally:
            conn.close()
    await asyncio.to_thread(_fail)


async def set_plugin_enabled(
    publisher_id: str,
    plugin_id: str,
    enabled: bool,
    *,
    lifecycle_state: str,
    error: str = '',
) -> bool:
    def _set():
        conn = _connect()
        try:
            with conn:
                cursor = conn.execute(
                    """
                    UPDATE plugin_installations SET enabled=?, lifecycle_state=?,
                        last_error=?, updated_at=?
                    WHERE publisher_id=? AND plugin_id=?
                    """,
                    (1 if enabled else 0, lifecycle_state, error[:1024], _utc_now(), publisher_id, plugin_id),
                )
                return cursor.rowcount == 1
        finally:
            conn.close()
    return await asyncio.to_thread(_set)


async def delete_plugin_installation(publisher_id: str, plugin_id: str) -> bool:
    def _delete():
        conn = _connect()
        try:
            with conn:
                cursor = conn.execute(
                    "DELETE FROM plugin_installations WHERE publisher_id=? AND plugin_id=?",
                    (publisher_id, plugin_id),
                )
                conn.execute(
                    "DELETE FROM plugin_artifacts WHERE publisher_id=? AND plugin_id=?",
                    (publisher_id, plugin_id),
                )
                return cursor.rowcount == 1
        finally:
            conn.close()
    return await asyncio.to_thread(_delete)


async def delete_retained_plugin_artifacts(publisher_id: str, plugin_id: str) -> None:
    def _delete():
        conn = _connect()
        try:
            with conn:
                conn.execute(
                    "DELETE FROM plugin_artifacts WHERE publisher_id=? AND plugin_id=? AND state='retained'",
                    (publisher_id, plugin_id),
                )
        finally:
            conn.close()
    await asyncio.to_thread(_delete)


async def begin_plugin_python_environment(
    *, publisher_id: str, plugin_id: str, plugin_version: str, runtime_identity: str,
    lock_digest: str, path: str, dependencies: list[dict],
) -> None:
    def _begin():
        conn = _connect()
        try:
            with conn:
                now = _utc_now()
                for item in dependencies:
                    conn.execute(
                        """
                        INSERT INTO plugin_dependency_artifacts(
                            sha256, package_name, version, filename, size_bytes,
                            python_tag, abi_tag, platform_tag, path, created_at
                        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(sha256) DO UPDATE SET
                            package_name=excluded.package_name, version=excluded.version,
                            filename=excluded.filename, size_bytes=excluded.size_bytes,
                            python_tag=excluded.python_tag, abi_tag=excluded.abi_tag,
                            platform_tag=excluded.platform_tag, path=excluded.path
                        """,
                        (item['sha256'], item['name'], item['version'], item['filename'], item['size_bytes'],
                         item['python_tag'], item['abi_tag'], item['platform_tag'], item['path'], now),
                    )
                conn.execute(
                    """
                    INSERT INTO plugin_python_environments(
                        publisher_id, plugin_id, plugin_version, runtime_identity,
                        lock_digest, path, state, dependency_count, created_at, updated_at
                    ) VALUES(?, ?, ?, ?, ?, ?, 'candidate', ?, ?, ?)
                    ON CONFLICT(publisher_id, plugin_id, plugin_version, runtime_identity, lock_digest)
                    DO UPDATE SET path=excluded.path, state='candidate',
                                  dependency_count=excluded.dependency_count, updated_at=excluded.updated_at
                    """,
                    (publisher_id, plugin_id, plugin_version, runtime_identity, lock_digest,
                     path, len(dependencies), now, now),
                )
                conn.execute(
                    """DELETE FROM plugin_environment_dependencies
                       WHERE publisher_id=? AND plugin_id=? AND plugin_version=?
                         AND runtime_identity=? AND lock_digest=?""",
                    (publisher_id, plugin_id, plugin_version, runtime_identity, lock_digest),
                )
                for item in dependencies:
                    conn.execute(
                        """INSERT INTO plugin_environment_dependencies(
                               publisher_id, plugin_id, plugin_version, runtime_identity,
                               lock_digest, artifact_sha256
                           ) VALUES(?, ?, ?, ?, ?, ?)""",
                        (publisher_id, plugin_id, plugin_version, runtime_identity, lock_digest, item['sha256']),
                    )
        finally:
            conn.close()
    await asyncio.to_thread(_begin)


async def activate_plugin_python_environment(
    publisher_id: str, plugin_id: str, plugin_version: str, runtime_identity: str, lock_digest: str,
) -> bool:
    def _activate():
        conn = _connect()
        try:
            with conn:
                conn.execute(
                    "UPDATE plugin_python_environments SET state='retained', updated_at=? "
                    "WHERE publisher_id=? AND plugin_id=? AND state='active'",
                    (_utc_now(), publisher_id, plugin_id),
                )
                cursor = conn.execute(
                    """UPDATE plugin_python_environments SET state='active', updated_at=?
                       WHERE publisher_id=? AND plugin_id=? AND plugin_version=?
                         AND runtime_identity=? AND lock_digest=? AND state='candidate'""",
                    (_utc_now(), publisher_id, plugin_id, plugin_version, runtime_identity, lock_digest),
                )
                return cursor.rowcount == 1
        finally:
            conn.close()
    return await asyncio.to_thread(_activate)


async def fail_plugin_python_environment(
    publisher_id: str, plugin_id: str, plugin_version: str, runtime_identity: str, lock_digest: str,
) -> None:
    def _fail():
        conn = _connect()
        try:
            with conn:
                values = (publisher_id, plugin_id, plugin_version, runtime_identity, lock_digest)
                conn.execute(
                    """DELETE FROM plugin_environment_dependencies
                       WHERE publisher_id=? AND plugin_id=? AND plugin_version=?
                         AND runtime_identity=? AND lock_digest=?""", values)
                conn.execute(
                    """DELETE FROM plugin_python_environments
                       WHERE publisher_id=? AND plugin_id=? AND plugin_version=?
                         AND runtime_identity=? AND lock_digest=? AND state='candidate'""", values)
        finally:
            conn.close()
    await asyncio.to_thread(_fail)


async def list_plugin_python_environments(publisher_id: str, plugin_id: str) -> list[dict]:
    def _list():
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT * FROM plugin_python_environments WHERE publisher_id=? AND plugin_id=? "
                "ORDER BY plugin_version, lock_digest", (publisher_id, plugin_id)).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()
    return await asyncio.to_thread(_list)


async def delete_plugin_python_environments(publisher_id: str, plugin_id: str) -> list[str]:
    def _delete():
        conn = _connect()
        try:
            with conn:
                rows = conn.execute(
                    "SELECT path FROM plugin_python_environments WHERE publisher_id=? AND plugin_id=?",
                    (publisher_id, plugin_id)).fetchall()
                conn.execute("DELETE FROM plugin_environment_dependencies WHERE publisher_id=? AND plugin_id=?",
                             (publisher_id, plugin_id))
                conn.execute("DELETE FROM plugin_python_environments WHERE publisher_id=? AND plugin_id=?",
                             (publisher_id, plugin_id))
                return [str(row['path']) for row in rows]
        finally:
            conn.close()
    return await asyncio.to_thread(_delete)


async def list_plugin_dependency_artifacts() -> list[dict]:
    def _list():
        conn = _connect()
        try:
            rows = conn.execute("SELECT * FROM plugin_dependency_artifacts ORDER BY sha256").fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()
    return await asyncio.to_thread(_list)


async def list_plugin_publisher_trust() -> list[dict]:
    def _list():
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT * FROM plugin_publisher_trust ORDER BY publisher_id, key_id"
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()
    return await asyncio.to_thread(_list)


async def upsert_plugin_publisher_trust(
    *, publisher_id: str, key_id: str, public_key: str, trust_level: str,
    enabled: bool, description: str = '',
) -> dict:
    def _upsert():
        conn = _connect()
        try:
            with conn:
                now = _utc_now()
                conn.execute(
                    """
                    INSERT INTO plugin_publisher_trust(
                        publisher_id, key_id, public_key, trust_level, enabled,
                        description, created_at, updated_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(publisher_id, key_id) DO UPDATE SET
                        public_key=excluded.public_key,
                        trust_level=excluded.trust_level,
                        enabled=excluded.enabled,
                        description=excluded.description,
                        updated_at=excluded.updated_at
                    """,
                    (publisher_id, key_id, public_key, trust_level, 1 if enabled else 0,
                     description, now, now),
                )
                row = conn.execute(
                    "SELECT * FROM plugin_publisher_trust WHERE publisher_id=? AND key_id=?",
                    (publisher_id, key_id),
                ).fetchone()
                return dict(row)
        finally:
            conn.close()
    return await asyncio.to_thread(_upsert)


async def delete_plugin_publisher_trust(publisher_id: str, key_id: str) -> bool:
    def _delete():
        conn = _connect()
        try:
            with conn:
                cursor = conn.execute(
                    "DELETE FROM plugin_publisher_trust WHERE publisher_id=? AND key_id=?",
                    (publisher_id, key_id),
                )
                return cursor.rowcount == 1
        finally:
            conn.close()
    return await asyncio.to_thread(_delete)


async def list_plugin_scheme_ownership() -> list[dict]:
    def _list():
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT * FROM plugin_scheme_ownership ORDER BY scheme"
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()
    return await asyncio.to_thread(_list)


async def set_plugin_scheme_ownership(scheme: str, mode: str, plugin_identity: str = '') -> dict:
    def _set():
        conn = _connect()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO plugin_scheme_ownership(scheme, mode, plugin_identity, updated_at)
                    VALUES(?, ?, ?, ?)
                    ON CONFLICT(scheme) DO UPDATE SET
                        mode=excluded.mode,
                        plugin_identity=excluded.plugin_identity,
                        updated_at=excluded.updated_at
                    """,
                    (scheme, mode, plugin_identity, _utc_now()),
                )
                row = conn.execute(
                    "SELECT * FROM plugin_scheme_ownership WHERE scheme=?", (scheme,)
                ).fetchone()
                return dict(row)
        finally:
            conn.close()
    return await asyncio.to_thread(_set)


async def recover_quarantined_plugin(publisher_id: str, plugin_id: str) -> bool:
    def _recover():
        conn = _connect()
        try:
            with conn:
                cursor = conn.execute(
                    """
                    UPDATE plugin_installations SET quarantined=0, enabled=0,
                        lifecycle_state='disabled', last_error='', updated_at=?
                    WHERE publisher_id=? AND plugin_id=? AND quarantined=1
                    """,
                    (_utc_now(), publisher_id, plugin_id),
                )
                return cursor.rowcount == 1
        finally:
            conn.close()
    return await asyncio.to_thread(_recover)


# ── Market sources ──

async def list_market_sources() -> list[dict]:
    def _list():
        conn = _connect()
        rows = conn.execute("SELECT * FROM market_sources ORDER BY is_builtin DESC, id ASC").fetchall()
        conn.close()
        return [dict(r) for r in rows]
    return await asyncio.to_thread(_list)


async def get_market_source(source_id: int) -> dict | None:
    def _get():
        conn = _connect()
        row = conn.execute("SELECT * FROM market_sources WHERE id=?", (source_id,)).fetchone()
        conn.close()
        return dict(row) if row else None
    return await asyncio.to_thread(_get)


async def get_market_source_by_key(source_key: str) -> dict | None:
    def _get():
        conn = _connect()
        row = conn.execute("SELECT * FROM market_sources WHERE source_key=?", (source_key,)).fetchone()
        conn.close()
        return dict(row) if row else None
    return await asyncio.to_thread(_get)


async def upsert_market_source(
    *,
    source_key: str,
    name: str,
    url: str,
    enabled: int = 1,
    allow_private: int = 0,
    is_builtin: int = 0,
) -> int:
    def _upsert():
        conn = _connect()
        now = datetime.now(timezone.utc).isoformat()
        cur = conn.execute(
            """
            INSERT INTO market_sources(
                source_key, name, url, enabled, allow_private, is_builtin,
                created_at, updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_key) DO UPDATE SET
                name=excluded.name,
                url=excluded.url,
                enabled=excluded.enabled,
                allow_private=excluded.allow_private,
                is_builtin=excluded.is_builtin,
                updated_at=excluded.updated_at
            """,
            (source_key, name, url, enabled, allow_private, is_builtin, now, now),
        )
        conn.commit()
        row = conn.execute("SELECT id FROM market_sources WHERE source_key=?", (source_key,)).fetchone()
        conn.close()
        return int(row["id"] if row else cur.lastrowid)
    return await asyncio.to_thread(_upsert)


async def create_market_source(name: str, url: str, source_key: str, enabled: int = 1, allow_private: int = 0) -> int:
    def _create():
        conn = _connect()
        now = datetime.now(timezone.utc).isoformat()
        cur = conn.execute(
            """
            INSERT INTO market_sources(
                source_key, name, url, enabled, allow_private, is_builtin,
                created_at, updated_at
            )
            VALUES(?, ?, ?, ?, ?, 0, ?, ?)
            """,
            (source_key, name, url, enabled, allow_private, now, now),
        )
        conn.commit()
        source_id = cur.lastrowid
        conn.close()
        return source_id
    return await asyncio.to_thread(_create)


async def update_market_source(source_id: int, **kwargs):
    allowed = {"name", "url", "enabled", "allow_private", "last_fetched_at", "last_status", "last_error"}
    values = {key: value for key, value in kwargs.items() if key in allowed}
    if not values:
        return

    def _update():
        conn = _connect()
        values["updated_at"] = datetime.now(timezone.utc).isoformat()
        sets = ', '.join(f"{k}=?" for k in values)
        conn.execute(f"UPDATE market_sources SET {sets} WHERE id=?", (*values.values(), source_id))
        conn.commit()
        conn.close()
    await asyncio.to_thread(_update)


async def delete_market_source(source_id: int):
    def _delete():
        conn = _connect()
        row = conn.execute("SELECT is_builtin FROM market_sources WHERE id=?", (source_id,)).fetchone()
        if row and row["is_builtin"]:
            conn.close()
            raise ValueError("内置 Market 源不能删除")
        conn.execute("DELETE FROM market_sources WHERE id=?", (source_id,))
        conn.commit()
        conn.close()
    await asyncio.to_thread(_delete)


async def get_all_channel_groups() -> list[str]:
    def _get():
        conn = _connect()
        rows = conn.execute(
            "SELECT DISTINCT group_name FROM channels WHERE group_name != '' ORDER BY group_name"
        ).fetchall()
        conn.close()
        return [r['group_name'] for r in rows]
    return await asyncio.to_thread(_get)


async def update_channel_status(ch_id: int, is_working: int, latency_ms: float = 0):
    def _update():
        conn = _connect()
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE channels SET is_working=?, latency_ms=?, last_tested=? WHERE id=?",
            (is_working, latency_ms, now, ch_id),
        )
        conn.commit()
        conn.close()
    await asyncio.to_thread(_update)


async def update_channel_probe_result(
    ch_id: int,
    result: dict,
    *,
    expected_source_revision: str | None = None,
) -> bool:
    def _update():
        conn = _connect()
        try:
            conn.execute('BEGIN IMMEDIATE')
            if expected_source_revision is not None:
                row = conn.execute(
                    """
                    SELECT c.*, s.custom_ua AS sub_custom_ua,
                           s.force_proxy AS sub_force_proxy
                    FROM channels AS c
                    JOIN subscriptions AS s ON s.id=c.subscription_id
                    WHERE c.id=?
                    """,
                    (ch_id,),
                ).fetchone()
                if row is None:
                    conn.rollback()
                    return False
                current_source = dict(row)
                _apply_sub_fallbacks([current_source])
                if source_revision_for(current_source) != str(expected_source_revision):
                    conn.rollback()
                    return False

            now = datetime.now(timezone.utc).isoformat()
            probe_status = str(result.get('probe_status') or 'error')
            is_working = 1 if probe_status == 'online' else 0
            last_success_at = now if probe_status == 'online' else str(result.get('last_success_at') or '')
            cursor = conn.execute(
                """
                UPDATE channels SET
                    is_working=?,
                    latency_ms=?,
                    last_tested=?,
                    probe_status=?,
                    live_status=?,
                    probe_method=?,
                    speed_mbps=?,
                    resolution=?,
                    fps=?,
                    video_codec=?,
                    audio_codec=?,
                    requires_headers=?,
                    requires_proxy_declared=?,
                    proxy_required_hint=?,
                    last_success_at=COALESCE(NULLIF(?, ''), last_success_at),
                    last_error=?,
                    adapter_provider=?,
                    adapter_title=?,
                    youtube_video_id=COALESCE(NULLIF(?, ''), youtube_video_id),
                    probe_meta_json=?
                WHERE id=?
                """,
                (
                    is_working,
                    float(result.get('latency_ms') or 0),
                    now,
                    probe_status,
                    str(result.get('live_status') or 'unknown'),
                    str(result.get('probe_method') or ''),
                    float(result.get('speed_mbps') or 0),
                    str(result.get('resolution') or ''),
                    float(result.get('fps') or 0),
                    str(result.get('video_codec') or ''),
                    str(result.get('audio_codec') or ''),
                    1 if result.get('requires_headers') else 0,
                    1 if result.get('requires_proxy_declared') else 0,
                    1 if result.get('proxy_required_hint') else 0,
                    last_success_at,
                    str(result.get('last_error') or ''),
                    str(result.get('adapter_provider') or ''),
                    str(result.get('adapter_title') or ''),
                    str(result.get('youtube_video_id') or ''),
                    str(result.get('probe_meta_json') or '{}'),
                    ch_id,
                ),
            )
            conn.commit()
            return cursor.rowcount == 1
        finally:
            conn.close()
    return await asyncio.to_thread(_update)


async def reset_channel_statuses(sub_id: int):
    def _reset():
        conn = _connect()
        conn.execute(
            """
            UPDATE channels SET
                is_working=0,
                latency_ms=0,
                last_tested='',
                probe_status='untested',
                live_status='unknown',
                probe_method='',
                speed_mbps=0,
                resolution='',
                fps=0,
                video_codec='',
                audio_codec='',
                requires_headers=0,
                requires_proxy_declared=0,
                proxy_required_hint=0,
                adapter_provider='',
                adapter_title='',
                probe_meta_json='{}',
                last_error=''
            WHERE subscription_id=?
            """,
            (sub_id,),
        )
        conn.commit()
        conn.close()
    await asyncio.to_thread(_reset)


async def reset_channel_statuses_all():
    def _reset():
        conn = _connect()
        conn.execute(
            """
            UPDATE channels SET
                is_working=0,
                latency_ms=0,
                last_tested='',
                probe_status='untested',
                live_status='unknown',
                probe_method='',
                speed_mbps=0,
                resolution='',
                fps=0,
                video_codec='',
                audio_codec='',
                requires_headers=0,
                requires_proxy_declared=0,
                proxy_required_hint=0,
                adapter_provider='',
                adapter_title='',
                probe_meta_json='{}',
                last_error=''
            """
        )
        conn.commit()
        conn.close()
    await asyncio.to_thread(_reset)


# ── EPG ──

async def add_epg_source(
    name: str,
    url: str,
    *,
    enabled: bool | int = True,
    source_origin: str = 'custom',
    builtin_key: str | None = None,
) -> int:
    name = validate_epg_source_name(name)
    url = validate_epg_source_url(url)
    enabled_value = _normalize_enabled(enabled)
    source_origin = validate_epg_source_origin(source_origin)
    builtin_key = validate_builtin_key(
        builtin_key,
        required=source_origin == 'builtin',
    )
    if source_origin == 'custom' and builtin_key is not None:
        raise ValueError('custom EPG 来源不能包含 builtin_key')

    def _add():
        conn = _connect()
        try:
            now = datetime.now(timezone.utc).isoformat()
            cur = conn.execute(
                """
                INSERT INTO epg_sources(
                    name, url, enabled, source_origin, builtin_key,
                    created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    name, url, enabled_value, source_origin, builtin_key,
                    now, now,
                ),
            )
            conn.commit()
            return int(cur.lastrowid)
        finally:
            conn.close()
    return await asyncio.to_thread(_add)


async def ensure_builtin_epg_source(
    *,
    builtin_key: str,
    name: str,
    url: str,
) -> dict:
    """Ensure one code-managed preset while preserving enabled/name choices."""
    builtin_key = validate_builtin_key(builtin_key, required=True)
    name = validate_epg_source_name(name)
    url = validate_epg_source_url(url)

    def _ensure():
        conn = _connect()
        try:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute(
                "SELECT * FROM epg_sources WHERE builtin_key=?",
                (builtin_key,),
            ).fetchone()
            now = _utc_now()
            if row is None:
                cursor = conn.execute(
                    """
                    INSERT INTO epg_sources(
                        name, url, enabled, source_origin, builtin_key,
                        created_at, updated_at
                    ) VALUES(?, ?, 1, 'builtin', ?, ?, ?)
                    """,
                    (name, url, builtin_key, now, now),
                )
                source_id = int(cursor.lastrowid)
            else:
                source_id = int(row['id'])
                if row['source_origin'] != 'builtin':
                    raise ValueError('builtin_key 已被非 builtin EPG 来源占用')
                if str(row['url']) != url:
                    conn.execute(
                        """
                        UPDATE epg_sources
                        SET url=?, revision=revision+1,
                            last_status='revision_discarded', last_error='',
                            updated_at=?
                        WHERE id=?
                        """,
                        (url, now, source_id),
                    )
            current = conn.execute(
                "SELECT * FROM epg_sources WHERE id=?",
                (source_id,),
            ).fetchone()
            conn.commit()
            return dict(current)
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    return await asyncio.to_thread(_ensure)


async def get_epg_source(source_id: int) -> dict | None:
    def _get():
        conn = _connect()
        try:
            row = conn.execute("SELECT * FROM epg_sources WHERE id=?", (source_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()
    return await asyncio.to_thread(_get)


async def get_epg_sources() -> list[dict]:
    def _get():
        conn = _connect()
        rows = conn.execute("SELECT * FROM epg_sources ORDER BY id").fetchall()
        conn.close()
        return [dict(r) for r in rows]
    return await asyncio.to_thread(_get)


async def delete_epg_source(source_id: int):
    def _delete():
        conn = _connect()
        try:
            with conn:
                source = conn.execute(
                    "SELECT source_origin FROM epg_sources WHERE id=?",
                    (source_id,),
                ).fetchone()
                if source is not None and source['source_origin'] == 'builtin':
                    raise ValueError('builtin EPG 来源不能删除')
                conn.execute("DELETE FROM epg_sources WHERE id=?", (source_id,))
        finally:
            conn.close()
    await asyncio.to_thread(_delete)


async def update_epg_source(source_id: int, **kwargs):
    allowed = {'name', 'url', 'enabled'}
    unknown = set(kwargs) - allowed
    if unknown:
        raise ValueError(f"不支持的 EPG 来源字段: {', '.join(sorted(unknown))}")
    if not kwargs:
        return await get_epg_source(source_id)

    def _update():
        conn = _connect()
        try:
            with conn:
                current = conn.execute("SELECT * FROM epg_sources WHERE id=?", (source_id,)).fetchone()
                if current is None:
                    return None
                updates = dict(kwargs)
                if 'enabled' in updates:
                    updates['enabled'] = _normalize_enabled(updates['enabled'])
                if 'name' in updates:
                    updates['name'] = validate_epg_source_name(updates['name'])
                if 'url' in updates:
                    updates['url'] = validate_epg_source_url(updates['url'])
                    if (
                        current['source_origin'] == 'builtin'
                        and updates['url'] != current['url']
                    ):
                        raise ValueError('builtin EPG 来源 URL 由 WaveFlow 管理')
                identity_changed = any(
                    field in updates and updates[field] != current[field]
                    for field in ('url', 'enabled')
                )
                if identity_changed:
                    updates['revision'] = int(current['revision'] or 1) + 1
                    updates['last_status'] = (
                        'disabled' if updates.get('enabled', current['enabled']) == 0
                        else 'revision_discarded'
                    )
                    updates['last_error'] = ''
                updates['updated_at'] = _utc_now()
                sets = ', '.join(f"{key}=?" for key in updates)
                conn.execute(
                    f"UPDATE epg_sources SET {sets} WHERE id=?",
                    (*updates.values(), source_id),
                )
                row = conn.execute("SELECT * FROM epg_sources WHERE id=?", (source_id,)).fetchone()
                return dict(row)
        finally:
            conn.close()
    return await asyncio.to_thread(_update)


def _normalize_epg_error(value: str | None) -> str:
    if value is None:
        return ''
    if not isinstance(value, str):
        raise TypeError('EPG error 必须是字符串')
    return value.replace('\x00', '').strip()[:EPG_ERROR_MAX_LENGTH]


async def begin_epg_source_refresh(
    source_id: int,
    expected_revision: int,
    *,
    attempted_at: str | None = None,
) -> bool:
    attempted = _normalize_timestamp(attempted_at, 'attempted_at')

    def _begin():
        conn = _connect()
        try:
            with conn:
                cursor = conn.execute(
                    """
                    UPDATE epg_sources
                    SET last_attempt_at=?, last_status='running', last_error='', updated_at=?
                    WHERE id=? AND revision=? AND enabled=1
                    """,
                    (attempted, attempted, source_id, expected_revision),
                )
                return cursor.rowcount == 1
        finally:
            conn.close()

    return await asyncio.to_thread(_begin)


async def record_epg_source_disabled(
    source_id: int,
    expected_revision: int,
    *,
    attempted_at: str | None = None,
) -> bool:
    attempted = _normalize_timestamp(attempted_at, 'attempted_at')

    def _record():
        conn = _connect()
        try:
            with conn:
                cursor = conn.execute(
                    """
                    UPDATE epg_sources
                    SET last_attempt_at=?, last_status='disabled', last_error='', updated_at=?
                    WHERE id=? AND revision=? AND enabled=0
                    """,
                    (attempted, attempted, source_id, expected_revision),
                )
                return cursor.rowcount == 1
        finally:
            conn.close()

    return await asyncio.to_thread(_record)


async def record_epg_source_refresh_failure(
    source_id: int,
    expected_revision: int,
    *,
    status: str,
    attempted_at: str | None = None,
    error: str = '',
) -> bool:
    if status not in {'stale', 'failed', 'cancelled'}:
        raise ValueError('EPG 来源失败状态必须是 stale、failed 或 cancelled')
    attempted = _normalize_timestamp(attempted_at, 'attempted_at')
    normalized_error = _normalize_epg_error(error)

    def _record():
        conn = _connect()
        try:
            with conn:
                cursor = conn.execute(
                    """
                    UPDATE epg_sources
                    SET last_attempt_at=?, last_status=?, last_error=?, updated_at=?
                    WHERE id=? AND revision=?
                    """,
                    (
                        attempted,
                        status,
                        normalized_error,
                        _utc_now(),
                        source_id,
                        expected_revision,
                    ),
                )
                return cursor.rowcount == 1
        finally:
            conn.close()

    return await asyncio.to_thread(_record)


async def has_epg_dataset(source_id: int) -> bool:
    def _has():
        conn = _connect()
        try:
            row = conn.execute(
                """
                SELECT EXISTS(SELECT 1 FROM epg_channels WHERE source_id=? LIMIT 1) AS has_channels,
                       EXISTS(SELECT 1 FROM epg_programs WHERE source_id=? LIMIT 1) AS has_programmes
                """,
                (source_id, source_id),
            ).fetchone()
            return bool(row['has_channels'] and row['has_programmes'])
        finally:
            conn.close()

    return await asyncio.to_thread(_has)


async def replace_epg_dataset_atomic(
    source_id: int,
    expected_revision: int,
    channels: list[dict],
    programmes: list[dict],
    *,
    stats: dict,
) -> dict:
    channel_count = _normalize_count(stats.get('channel_count'), 'channel_count')
    programme_count = _normalize_count(stats.get('programme_count'), 'programme_count')
    if channel_count != len(channels) or programme_count != len(programmes):
        raise ValueError('EPG 数据集统计与实际记录数量不一致')
    data_start_at = _normalize_timestamp(stats.get('data_start_at'), 'data_start_at')
    data_end_at = _normalize_timestamp(stats.get('data_end_at'), 'data_end_at')
    finished_at = _normalize_timestamp(stats.get('finished_at'), 'finished_at')
    attempted_at = _normalize_timestamp(stats.get('attempted_at') or finished_at, 'attempted_at')

    def _replace():
        conn = _connect()
        try:
            conn.execute('BEGIN IMMEDIATE')
            source = conn.execute("SELECT * FROM epg_sources WHERE id=?", (source_id,)).fetchone()
            if source is None:
                conn.rollback()
                return {'committed': False, 'reason': 'revision_discarded', 'source': None}
            if int(source['revision'] or 1) != expected_revision:
                conn.rollback()
                return {'committed': False, 'reason': 'revision_discarded', 'source': dict(source)}
            if not source['enabled']:
                conn.rollback()
                return {'committed': False, 'reason': 'disabled', 'source': dict(source)}

            conn.execute("DELETE FROM epg_channels WHERE source_id=?", (source_id,))
            conn.execute("DELETE FROM epg_programs WHERE source_id=?", (source_id,))
            conn.executemany(
                "INSERT INTO epg_channels(source_id, channel_id, display_names, normalized_names) VALUES(?, ?, ?, ?)",
                [
                    (source_id, item['channel_id'], item['display_names'], item['normalized_names'])
                    for item in channels
                ],
            )
            conn.executemany(
                "INSERT INTO epg_programs(source_id, channel_id, start, stop, title, description) VALUES(?, ?, ?, ?, ?, ?)",
                [
                    (
                        source_id,
                        item['channel_id'],
                        item['start'],
                        item['stop'],
                        item['title'],
                        item.get('description', ''),
                    )
                    for item in programmes
                ],
            )
            cursor = conn.execute(
                """
                UPDATE epg_sources
                SET last_fetched_at=?, last_attempt_at=?, last_success_at=?,
                    last_status='success', last_error='', channel_count=?, programme_count=?,
                    data_start_at=?, data_end_at=?, updated_at=?
                WHERE id=? AND revision=? AND enabled=1
                """,
                (
                    finished_at,
                    attempted_at,
                    finished_at,
                    channel_count,
                    programme_count,
                    data_start_at,
                    data_end_at,
                    finished_at,
                    source_id,
                    expected_revision,
                ),
            )
            if cursor.rowcount != 1:
                conn.rollback()
                return {'committed': False, 'reason': 'revision_discarded', 'source': None}
            conn.commit()
            current = conn.execute("SELECT * FROM epg_sources WHERE id=?", (source_id,)).fetchone()
            return {'committed': True, 'reason': '', 'source': dict(current)}
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    return await _await_thread_operation(_replace)


async def replace_epg_channels(source_id: int, channels: list[dict]):
    def _replace():
        conn = _connect()
        conn.execute("DELETE FROM epg_channels WHERE source_id=?", (source_id,))
        conn.executemany(
            "INSERT INTO epg_channels(source_id, channel_id, display_names, normalized_names) VALUES(?, ?, ?, ?)",
            [(source_id, ch['channel_id'], ch['display_names'], ch['normalized_names']) for ch in channels],
        )
        conn.commit()
        conn.close()
    await asyncio.to_thread(_replace)


async def replace_epg_programs(source_id: int, programs: list[dict]):
    def _replace():
        conn = _connect()
        conn.execute("DELETE FROM epg_programs WHERE source_id=?", (source_id,))
        conn.executemany(
            "INSERT INTO epg_programs(source_id, channel_id, start, stop, title, description) VALUES(?, ?, ?, ?, ?, ?)",
            [(source_id, p['channel_id'], p['start'], p['stop'], p['title'], p.get('description', '')) for p in programs],
        )
        conn.commit()
        conn.close()
    await asyncio.to_thread(_replace)


async def list_epg_channel_catalog_rows() -> list[dict]:
    """Return safe source-aware EPG channel rows for the shadow catalog.

    The query deliberately omits source URLs and all other source
    configuration.  Ordering is explicit so callers do not depend on SQLite
    insertion order, while the composite source/channel identity remains the
    only lookup key.
    """

    def _list():
        conn = _connect()
        try:
            rows = conn.execute(
                """
                SELECT
                    s.id AS source_id,
                    s.name AS source_name,
                    s.enabled AS source_enabled,
                    s.last_status AS source_status,
                    c.channel_id AS channel_id,
                    c.display_names AS display_names,
                    c.normalized_names AS normalized_names
                FROM epg_channels AS c
                JOIN epg_sources AS s ON s.id = c.source_id
                ORDER BY s.id, c.channel_id, c.id
                """
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    return await asyncio.to_thread(_list)


async def get_epg_channel_catalog_row(source_id: int, channel_id: str) -> dict | None:
    """Read exactly one EPG channel by its composite source-aware identity."""

    def _get():
        conn = _connect()
        try:
            row = conn.execute(
                """
                SELECT
                    s.id AS source_id,
                    s.name AS source_name,
                    s.enabled AS source_enabled,
                    s.last_status AS source_status,
                    c.channel_id AS channel_id,
                    c.display_names AS display_names,
                    c.normalized_names AS normalized_names
                FROM epg_channels AS c
                JOIN epg_sources AS s ON s.id = c.source_id
                WHERE c.source_id=? AND c.channel_id=?
                """,
                (source_id, channel_id),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    return await asyncio.to_thread(_get)


async def epg_channel_identity_exists(source_id: int, channel_id: str) -> bool:
    """Check the exact ``(source_id, channel_id)`` identity without guessing."""

    def _exists():
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT 1 FROM epg_channels WHERE source_id=? AND channel_id=? LIMIT 1",
                (source_id, channel_id),
            ).fetchone()
            return row is not None
        finally:
            conn.close()

    return await asyncio.to_thread(_exists)


async def get_epg_channels(source_id: int) -> list[dict]:
    def _get():
        conn = _connect()
        rows = conn.execute("SELECT * FROM epg_channels WHERE source_id=?", (source_id,)).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    return await asyncio.to_thread(_get)


async def get_epg_programs(source_id: int, channel_id: str, start_after: str = '', start_before: str = '') -> list[dict]:
    def _get():
        conn = _connect()
        query = "SELECT * FROM epg_programs WHERE source_id=? AND channel_id=?"
        params: list = [source_id, channel_id]
        if start_after:
            query += " AND stop > ?"
            params.append(start_after)
        if start_before:
            query += " AND start <= ?"
            params.append(start_before)
        query += " ORDER BY start"
        rows = conn.execute(query, params).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    return await asyncio.to_thread(_get)


async def batch_get_current_programs(canonical_keys: list[str]) -> dict:
    """批量查当前节目：返回 {canonical_key: {current, next}} 或 {}。"""
    comparisons: dict[str, dict] = {}
    try:
        from epg_read_resolver import (
            emit_epg_read_diagnostic,
            emit_epg_read_resolver_error,
            resolve_epg_read_many,
        )
        comparisons = await resolve_epg_read_many(canonical_keys)
        for comparison in comparisons.values():
            emit_epg_read_diagnostic(comparison, context='batch-current')
    except Exception as exc:
        # Logical-only reads fail closed to no EPG; legacy mapping remains
        # available only to the isolated migration/bootstrap path.
        from epg_read_resolver import emit_epg_read_resolver_error
        emit_epg_read_resolver_error(context='batch-current', error=exc)
        comparisons = {}

    def _get():
        conn = _connect()
        try:
            now = datetime.now(timezone.utc).isoformat()
            keys = list(dict.fromkeys(str(key) for key in canonical_keys))
            if not keys:
                return {}

            targets: dict[str, tuple[int, str] | None] = {}
            for key in keys:
                comparison = comparisons.get(key)
                effective = comparison.get('effective_target') if comparison else None
                effective_source = str(comparison.get('effective_source') or '') if comparison else ''
                if effective and effective_source == 'logical':
                    targets[key] = (int(effective['source_id']), str(effective['channel_id']))
                else:
                    targets[key] = None

            target_pairs = sorted({target for target in targets.values() if target is not None})
            programs_by_target: dict[tuple[int, str], list[dict]] = {
                target: [] for target in target_pairs
            }
            # Keep each SQLite statement below the default variable limit while
            # remaining independent of the number of requested canonical keys.
            for offset in range(0, len(target_pairs), 400):
                chunk = target_pairs[offset:offset + 400]
                conditions = ' OR '.join('(source_id=? AND channel_id=?)' for _ in chunk)
                params: list[object] = [now]
                for source_id, channel_id in chunk:
                    params.extend((source_id, channel_id))
                rows = conn.execute(
                    f"""SELECT source_id, channel_id, title, start, stop, description
                        FROM epg_programs
                        WHERE stop > ? AND ({conditions})
                        ORDER BY source_id, channel_id, start""",
                    params,
                ).fetchall()
                for row in rows:
                    programs_by_target[(int(row['source_id']), str(row['channel_id']))].append(dict(row))

            result: dict[str, dict | None] = {}
            for key in canonical_keys:
                target = targets.get(str(key))
                if target is None:
                    result[key] = None
                    continue
                current = None
                next_prog = None
                for program in programs_by_target.get(target, []):
                    if current is None and program['start'] <= now < program['stop']:
                        current = {
                            field: program[field]
                            for field in ('title', 'start', 'stop', 'description')
                        }
                    if next_prog is None and program['start'] > now:
                        next_prog = {
                            field: program[field]
                            for field in ('title', 'start', 'stop')
                        }
                    if current is not None and next_prog is not None:
                        break
                result[key] = {'current': current, 'next': next_prog}
            return result
        finally:
            conn.close()

    return await asyncio.to_thread(_get)
