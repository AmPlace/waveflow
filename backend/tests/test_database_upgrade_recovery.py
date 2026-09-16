from __future__ import annotations

import asyncio
import importlib
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock


NOW = "2026-08-18T00:00:00+00:00"

LEGACY_RADIO_SCHEMA = """
CREATE TABLE radio_stations (
    station_id TEXT PRIMARY KEY,
    owner_identity TEXT NOT NULL,
    provider_key TEXT NOT NULL,
    provider_station_id TEXT NOT NULL,
    name TEXT NOT NULL,
    logo_url TEXT NOT NULL DEFAULT '',
    group_name TEXT NOT NULL DEFAULT '',
    country TEXT NOT NULL DEFAULT '',
    language TEXT NOT NULL DEFAULT '',
    frequency TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    lifecycle_state TEXT NOT NULL DEFAULT 'active',
    catalog_ttl_seconds INTEGER NOT NULL DEFAULT 300,
    catalog_expires_at REAL NOT NULL DEFAULT 0,
    last_catalog_success_at TEXT NOT NULL DEFAULT '',
    last_catalog_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(owner_identity, provider_key, provider_station_id)
);
CREATE TABLE radio_station_sources (
    source_id TEXT PRIMARY KEY,
    station_id TEXT NOT NULL,
    owner_identity TEXT NOT NULL,
    provider_key TEXT NOT NULL,
    provider_station_id TEXT NOT NULL,
    reference_json TEXT NOT NULL,
    source_revision TEXT NOT NULL,
    explicit_priority INTEGER NOT NULL DEFAULT 0,
    health_status TEXT NOT NULL DEFAULT 'unknown',
    last_success_at TEXT NOT NULL DEFAULT '',
    last_error TEXT NOT NULL DEFAULT '',
    lifecycle_state TEXT NOT NULL DEFAULT 'active',
    catalog_ttl_seconds INTEGER NOT NULL DEFAULT 300,
    catalog_expires_at REAL NOT NULL DEFAULT 0,
    resolve_expires_at REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(owner_identity, provider_key, provider_station_id),
    UNIQUE(station_id, source_id),
    FOREIGN KEY(station_id) REFERENCES radio_stations(station_id) ON DELETE CASCADE
);
"""


def _clear_database_module() -> None:
    sys.modules.pop("database", None)


class _ConnectionProxy:
    def __init__(self, connection: sqlite3.Connection, hook=None):
        self.connection = connection
        self.hook = hook
        self.closed = False

    def execute(self, sql, parameters=()):
        if self.hook is not None:
            self.hook(sql)
        return self.connection.execute(sql, parameters)

    def executemany(self, sql, parameters):
        return self.connection.executemany(sql, parameters)

    def executescript(self, sql):
        return self.connection.executescript(sql)

    def commit(self):
        return self.connection.commit()

    def rollback(self):
        return self.connection.rollback()

    def close(self):
        self.closed = True
        return self.connection.close()

    @property
    def in_transaction(self):
        return self.connection.in_transaction


class DatabaseUpgradeRecoveryTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db_path = os.environ.get("WAVEFLOW_DB_PATH")
        self.db_path = os.path.join(self.tmp.name, "waveflow.db")
        os.environ["WAVEFLOW_DB_PATH"] = self.db_path
        _clear_database_module()
        self.db = importlib.import_module("database")

    async def asyncTearDown(self):
        if self.old_db_path is None:
            os.environ.pop("WAVEFLOW_DB_PATH", None)
        else:
            os.environ["WAVEFLOW_DB_PATH"] = self.old_db_path
        _clear_database_module()
        self.tmp.cleanup()

    @contextmanager
    def connect(self, path: str | None = None):
        connection = sqlite3.connect(path or self.db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def table_names(self) -> set[str]:
        with self.connect() as connection:
            return {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }

    def columns(self, table: str) -> set[str]:
        with self.connect() as connection:
            return {
                str(row[1])
                for row in connection.execute(f"PRAGMA table_info({table})")
            }

    def create_legacy_radio_fixture(self) -> None:
        with self.connect() as connection:
            connection.executescript(LEGACY_RADIO_SCHEMA)
            connection.execute(
                "INSERT INTO radio_stations(station_id, owner_identity, provider_key, "
                "provider_station_id, name, metadata_json, created_at, updated_at) "
                "VALUES('station-1', 'org.waveflow/fixture', 'fixture', 'remote-1', "
                "'Fixture Radio', '{\"region\":\"CN\"}', ?, ?)",
                (NOW, NOW),
            )
            connection.execute(
                "INSERT INTO radio_station_sources(source_id, station_id, owner_identity, "
                "provider_key, provider_station_id, reference_json, source_revision, "
                "created_at, updated_at) VALUES('source-1', 'station-1', "
                "'org.waveflow/fixture', 'fixture', 'remote-1', '{\"id\":1}', 'rev-7', ?, ?)",
                (NOW, NOW),
            )

    async def test_fresh_initialize_is_complete_idempotent_and_consistent(self):
        await self.db.initialize()
        required = {
            "settings",
            "subscriptions",
            "channels",
            "epg_sources",
            "market_packages_installed",
            "plugin_installations",
            "radio_stations",
            "automation_task_config",
            "users",
            "sessions",
        }
        self.assertTrue(required.issubset(self.table_names()))
        with self.connect() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM sqlite_master "
                    "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                ).fetchone()[0],
                37,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM sqlite_master "
                    "WHERE type='index' AND name NOT LIKE 'sqlite_%'"
                ).fetchone()[0],
                39,
            )
        self.assertNotIn("refresh_generation", self.columns("channels"))

        await self.db.set_setting("fixture", "preserved")
        await self.db.initialize()
        await self.db.initialize()
        self.assertEqual(await self.db.get_setting("fixture"), "preserved")

        with self.connect() as connection:
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)

    async def test_oldest_supported_schema_preserves_subscription_identity_and_data(self):
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE subscriptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    url TEXT NOT NULL UNIQUE,
                    channel_count INTEGER DEFAULT 0,
                    valid INTEGER DEFAULT 1,
                    last_updated TEXT DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE channels (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    subscription_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    url TEXT NOT NULL,
                    logo_url TEXT DEFAULT '',
                    group_name TEXT DEFAULT '',
                    tvg_id TEXT DEFAULT '',
                    tvg_name TEXT DEFAULT '',
                    is_working INTEGER DEFAULT -1,
                    latency_ms REAL DEFAULT 0,
                    last_tested TEXT DEFAULT '',
                    FOREIGN KEY(subscription_id) REFERENCES subscriptions(id) ON DELETE CASCADE
                );
                CREATE INDEX idx_channels_sub ON channels(subscription_id);
                CREATE INDEX idx_channels_name ON channels(name);
                """
            )
            connection.execute("INSERT INTO settings VALUES('theme', 'dark')")
            connection.execute(
                "INSERT INTO subscriptions(id, title, url, channel_count, created_at) "
                "VALUES(7, 'Legacy', 'https://fixture.invalid/list.m3u', 1, ?)",
                (NOW,),
            )
            connection.execute(
                "INSERT INTO channels(id, subscription_id, name, url, tvg_id) "
                "VALUES(11, 7, 'Legacy TV', 'https://fixture.invalid/live', 'legacy-tv')"
            )

        await self.db.initialize()
        await self.db.initialize()

        subscription = await self.db.get_subscription(7)
        channels = await self.db.get_channels(7)
        self.assertEqual(subscription["title"], "Legacy")
        self.assertEqual(subscription["refresh_generation"], 0)
        self.assertEqual(channels[0]["id"], 11)
        self.assertEqual(channels[0]["tvg_id"], "legacy-tv")
        self.assertEqual(channels[0]["probe_meta_json"], "{}")
        self.assertEqual(channels[0]["rtsp_timestamp_mode"], "passthrough")
        self.assertEqual(await self.db.get_setting("theme"), "dark")

    async def test_misplaced_refresh_generation_upgrade_repairs_subscription_table(self):
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE subscriptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL, url TEXT NOT NULL UNIQUE,
                    channel_count INTEGER DEFAULT 0, valid INTEGER DEFAULT 1,
                    last_updated TEXT DEFAULT '', created_at TEXT NOT NULL,
                    custom_ua TEXT DEFAULT '', force_proxy INTEGER DEFAULT 0,
                    last_tested TEXT DEFAULT ''
                );
                CREATE TABLE channels (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    subscription_id INTEGER NOT NULL, name TEXT NOT NULL, url TEXT NOT NULL,
                    logo_url TEXT DEFAULT '', group_name TEXT DEFAULT '', tvg_id TEXT DEFAULT '',
                    tvg_name TEXT DEFAULT '', is_working INTEGER DEFAULT 0,
                    latency_ms REAL DEFAULT 0, last_tested TEXT DEFAULT '',
                    refresh_generation INTEGER DEFAULT 0,
                    FOREIGN KEY(subscription_id) REFERENCES subscriptions(id) ON DELETE CASCADE
                );
                """
            )
            connection.execute(
                "INSERT INTO subscriptions(id, title, url, created_at) "
                "VALUES(4, 'Affected', 'https://fixture.invalid/affected.m3u', ?)",
                (NOW,),
            )
            connection.execute(
                "INSERT INTO channels(id, subscription_id, name, url, refresh_generation) "
                "VALUES(8, 4, 'Affected TV', 'https://fixture.invalid/live', 12)"
            )

        await self.db.initialize()
        generation = await self.db.begin_subscription_refresh(4)

        self.assertEqual(generation, 1)
        self.assertIn("refresh_generation", self.columns("subscriptions"))
        with self.connect() as connection:
            channel = connection.execute("SELECT * FROM channels WHERE id=8").fetchone()
        self.assertEqual(channel["refresh_generation"], 12)

    async def test_security_market_epg_and_automation_rows_survive_upgrade(self):
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE subscriptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL, url TEXT NOT NULL UNIQUE,
                    channel_count INTEGER DEFAULT 0, valid INTEGER DEFAULT 1,
                    last_updated TEXT DEFAULT '', created_at TEXT NOT NULL,
                    custom_ua TEXT DEFAULT '', force_proxy INTEGER DEFAULT 0,
                    last_tested TEXT DEFAULT ''
                );
                CREATE TABLE channels (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    subscription_id INTEGER NOT NULL, name TEXT NOT NULL, url TEXT NOT NULL,
                    logo_url TEXT DEFAULT '', group_name TEXT DEFAULT '', tvg_id TEXT DEFAULT '',
                    tvg_name TEXT DEFAULT '', is_working INTEGER DEFAULT 0,
                    latency_ms REAL DEFAULT 0, last_tested TEXT DEFAULT '',
                    FOREIGN KEY(subscription_id) REFERENCES subscriptions(id) ON DELETE CASCADE
                );
                CREATE TABLE epg_sources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                    url TEXT NOT NULL UNIQUE, enabled INTEGER DEFAULT 1,
                    last_fetched_at TEXT DEFAULT '', last_status TEXT DEFAULT '',
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE epg_channels (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_id INTEGER NOT NULL REFERENCES epg_sources(id) ON DELETE CASCADE,
                    channel_id TEXT NOT NULL, display_names TEXT NOT NULL,
                    normalized_names TEXT NOT NULL, UNIQUE(source_id, channel_id)
                );
                CREATE TABLE epg_programs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_id INTEGER NOT NULL REFERENCES epg_sources(id) ON DELETE CASCADE,
                    channel_id TEXT NOT NULL, start TEXT NOT NULL, stop TEXT NOT NULL,
                    title TEXT NOT NULL, description TEXT DEFAULT ''
                );
                CREATE TABLE market_packages_installed (
                    package_id TEXT PRIMARY KEY, market_url TEXT DEFAULT '',
                    installed_subscription_id INTEGER, installed_version TEXT DEFAULT '',
                    installed_at TEXT NOT NULL, auto_update INTEGER DEFAULT 0,
                    metadata_json TEXT DEFAULT ''
                );
                CREATE TABLE users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'admin',
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, token_hash TEXT NOT NULL UNIQUE,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL, revoked_at TEXT DEFAULT ''
                );
                CREATE TABLE automation_task_config (
                    task_id TEXT PRIMARY KEY, conflict_group TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1, interval_seconds INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE automation_task_state (
                    task_id TEXT PRIMARY KEY, conflict_group TEXT NOT NULL,
                    task_type TEXT DEFAULT '', run_token TEXT DEFAULT '',
                    last_started_at TEXT DEFAULT '', last_finished_at TEXT DEFAULT '',
                    last_status TEXT NOT NULL DEFAULT 'never_run',
                    checked_count INTEGER NOT NULL DEFAULT 0,
                    updated_count INTEGER NOT NULL DEFAULT 0,
                    skipped_count INTEGER NOT NULL DEFAULT 0,
                    failed_count INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT DEFAULT ''
                );
                """
            )
            connection.execute(
                "INSERT INTO epg_sources VALUES(3, 'Legacy EPG', 'https://fixture.invalid/epg.xml', "
                "1, ?, 'ok', ?, ?)",
                (NOW, NOW, NOW),
            )
            connection.execute(
                "INSERT INTO epg_channels(source_id, channel_id, display_names, normalized_names) "
                "VALUES(3, 'channel-3', '[\"Legacy TV\"]', '[\"legacy tv\"]')"
            )
            connection.execute(
                "INSERT INTO epg_programs(source_id, channel_id, start, stop, title) "
                "VALUES(3, 'channel-3', ?, '2026-08-18T01:00:00+00:00', 'Programme')",
                (NOW,),
            )
            connection.execute(
                "INSERT INTO market_packages_installed VALUES("
                "'org.waveflow/fixture', '', NULL, '1.2.3', ?, 1, ?)",
                (NOW, json.dumps({"state": "kept"})),
            )
            connection.execute(
                "INSERT INTO users VALUES(5, 'admin-fixture', 'fixture-hash', 'admin', ?, ?)",
                (NOW, NOW),
            )
            connection.execute(
                "INSERT INTO sessions VALUES(9, 'fixture-token-hash', 5, ?, "
                "'2027-08-18T00:00:00+00:00', ?, '')",
                (NOW, NOW),
            )
            connection.execute(
                "INSERT INTO automation_task_config VALUES('fixture-task', 'fixture', 0, 600, ?)",
                (NOW,),
            )
            connection.execute(
                "INSERT INTO automation_task_state(task_id, conflict_group, last_status, checked_count) "
                "VALUES('fixture-task', 'fixture', 'success', 4)"
            )

        await self.db.initialize()

        source = await self.db.get_epg_source(3)
        install = await self.db.get_market_install("org.waveflow/fixture")
        task = await self.db.get_automation_task_state("fixture-task")
        with self.connect() as connection:
            user = connection.execute("SELECT * FROM users WHERE id=5").fetchone()
            session = connection.execute("SELECT * FROM sessions WHERE id=9").fetchone()
        self.assertEqual(source["revision"], 1)
        self.assertEqual(source["last_status"], "success")
        self.assertEqual(source["programme_count"], 1)
        self.assertEqual(install["installed_version"], "1.2.3")
        self.assertEqual(json.loads(install["metadata_json"]), {"state": "kept"})
        self.assertEqual(task["checked_count"], 4)
        self.assertEqual(user["username"], "admin-fixture")
        self.assertEqual(session["user_id"], 5)

    async def test_radio_source_schema_rebuild_preserves_source_identity(self):
        self.create_legacy_radio_fixture()

        await self.db.initialize()
        await self.db.initialize()

        with self.connect() as connection:
            source = connection.execute(
                "SELECT * FROM radio_station_sources WHERE source_id='source-1'"
            ).fetchone()
            unique_indexes = [
                tuple(
                    row[2]
                    for row in connection.execute(f"PRAGMA index_info({index[1]!r})")
                )
                for index in connection.execute("PRAGMA index_list(radio_station_sources)")
                if index[2]
            ]
        self.assertEqual(source["station_id"], "station-1")
        self.assertEqual(source["source_revision"], "rev-7")
        self.assertEqual(json.loads(source["reference_json"]), {"id": 1})
        self.assertEqual(source["source_discriminator"], "")
        self.assertIn(
            ("owner_identity", "provider_key", "provider_station_id", "source_discriminator"),
            unique_indexes,
        )

    async def test_radio_source_rebuild_failure_rolls_back_and_retry_completes(self):
        self.create_legacy_radio_fixture()
        real_connect = self.db._connect
        created: list[_ConnectionProxy] = []

        def connect_proxy():
            def hook(sql: str) -> None:
                if sql.startswith(
                    "ALTER TABLE radio_station_sources_new RENAME TO radio_station_sources"
                ):
                    raise sqlite3.OperationalError("simulated radio rebuild failure")

            proxy = _ConnectionProxy(real_connect(), hook)
            created.append(proxy)
            return proxy

        with mock.patch.object(self.db, "_connect", connect_proxy):
            with self.assertRaisesRegex(sqlite3.OperationalError, "radio rebuild"):
                await self.db.initialize()

        self.assertTrue(created[0].closed)
        with self.connect() as connection:
            row = connection.execute(
                "SELECT source_id, source_revision FROM radio_station_sources"
            ).fetchone()
            temporary = connection.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='radio_station_sources_new'"
            ).fetchone()
        self.assertEqual(dict(row), {"source_id": "source-1", "source_revision": "rev-7"})
        self.assertIsNone(temporary)
        self.assertNotIn("source_discriminator", self.columns("radio_station_sources"))

        await self.db.initialize()
        self.assertIn("source_discriminator", self.columns("radio_station_sources"))

    async def test_schema_script_error_rolls_back_all_schema_changes(self):
        broken_schema = """
        CREATE TABLE first_step (id INTEGER PRIMARY KEY);
        CREATE TABLE broken_step (id INTEGER PRIMARY KEY, );
        """
        with mock.patch.object(self.db, "_SCHEMA", broken_schema):
            with self.assertRaises(sqlite3.OperationalError):
                await self.db.initialize()
        self.assertNotIn("first_step", self.table_names())

    async def test_add_column_operational_error_is_not_silently_accepted(self):
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE subscriptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL, url TEXT NOT NULL UNIQUE,
                    channel_count INTEGER DEFAULT 0, valid INTEGER DEFAULT 1,
                    last_updated TEXT DEFAULT '', created_at TEXT NOT NULL
                );
                CREATE TABLE channels (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    subscription_id INTEGER NOT NULL, name TEXT NOT NULL, url TEXT NOT NULL,
                    logo_url TEXT DEFAULT '', group_name TEXT DEFAULT '', tvg_id TEXT DEFAULT '',
                    tvg_name TEXT DEFAULT '', is_working INTEGER DEFAULT -1,
                    latency_ms REAL DEFAULT 0, last_tested TEXT DEFAULT ''
                );
                """
            )
        real_connect = self.db._connect
        created: list[_ConnectionProxy] = []

        def connect_proxy():
            def hook(sql: str) -> None:
                if sql.startswith("ALTER TABLE channels ADD COLUMN rtsp_timestamp_mode"):
                    raise sqlite3.OperationalError("simulated disk I/O error")

            proxy = _ConnectionProxy(real_connect(), hook)
            created.append(proxy)
            return proxy

        with mock.patch.object(self.db, "_connect", connect_proxy):
            with self.assertRaisesRegex(sqlite3.OperationalError, "disk I/O"):
                await self.db.initialize()
        self.assertTrue(created[0].closed)

    async def test_additive_upgrade_failure_rolls_back_and_retry_completes(self):
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE subscriptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
                    url TEXT NOT NULL UNIQUE, channel_count INTEGER DEFAULT 0,
                    valid INTEGER DEFAULT 1, last_updated TEXT DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE channels (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    subscription_id INTEGER NOT NULL, name TEXT NOT NULL, url TEXT NOT NULL,
                    logo_url TEXT DEFAULT '', group_name TEXT DEFAULT '', tvg_id TEXT DEFAULT '',
                    tvg_name TEXT DEFAULT '', is_working INTEGER DEFAULT -1,
                    latency_ms REAL DEFAULT 0, last_tested TEXT DEFAULT ''
                );
                """
            )

        real_connect = self.db._connect

        def connect_proxy():
            def hook(sql: str) -> None:
                if sql.lstrip().startswith("UPDATE iptv_logical_channels"):
                    raise sqlite3.OperationalError("simulated write failure")

            return _ConnectionProxy(real_connect(), hook)

        with mock.patch.object(self.db, "_connect", connect_proxy):
            with self.assertRaisesRegex(sqlite3.OperationalError, "write failure"):
                await self.db.initialize()

        self.assertNotIn("custom_ua", self.columns("subscriptions"))
        self.assertNotIn("source_type", self.columns("channels"))

        await self.db.initialize()
        self.assertIn("custom_ua", self.columns("subscriptions"))
        self.assertIn("source_type", self.columns("channels"))

    async def test_malformed_legacy_permission_schema_fails_closed_and_can_retry(self):
        with self.connect() as connection:
            connection.execute(
                """
                CREATE TABLE plugin_permission_approvals (
                    publisher_id TEXT PRIMARY KEY,
                    plugin_id TEXT NOT NULL,
                    permission_name TEXT NOT NULL,
                    permission_fingerprint TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "INSERT INTO plugin_permission_approvals VALUES("
                "'org.waveflow', 'fixture', 'network.direct', 'fingerprint', ?)",
                (NOW,),
            )

        with self.assertRaisesRegex(sqlite3.OperationalError, "unsupported primary key"):
            await self.db.initialize()
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM plugin_permission_approvals"
            ).fetchone()
        self.assertEqual(row["plugin_id"], "fixture")

        with self.connect() as connection:
            connection.execute("DROP TABLE plugin_permission_approvals")
        await self.db.initialize()
        self.assertIn("permission_fingerprint", self.columns("plugin_permission_approvals"))

    async def test_corrupt_database_fails_closed_without_replacement(self):
        corrupt = b"not-a-sqlite-database\x00fixture"
        Path(self.db_path).write_bytes(corrupt)

        with self.assertRaises(sqlite3.DatabaseError):
            await self.db.initialize()

        self.assertEqual(Path(self.db_path).read_bytes(), corrupt)

    def test_connect_closes_when_pragma_configuration_fails(self):
        real_connect = sqlite3.connect

        def hook(sql: str) -> None:
            if sql.startswith("PRAGMA journal_mode"):
                raise sqlite3.DatabaseError("simulated pragma failure")

        proxy = _ConnectionProxy(real_connect(self.db_path), hook)
        with mock.patch.object(self.db.sqlite3, "connect", return_value=proxy):
            with self.assertRaisesRegex(sqlite3.DatabaseError, "pragma failure"):
                self.db._connect()
        self.assertTrue(proxy.closed)

    async def test_concurrent_initialize_is_serialized_and_idempotent(self):
        await asyncio.gather(*(self.db.initialize() for _ in range(8)))
        await asyncio.gather(*(self.db.initialize() for _ in range(8)))
        with self.connect() as connection:
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM automation_task_config "
                    "WHERE task_id='market_auto_update'"
                ).fetchone()[0],
                1,
            )

    async def test_busy_writer_is_waited_for_and_initialization_recovers(self):
        await self.db.initialize()
        blocker = sqlite3.connect(self.db_path, check_same_thread=False)
        blocker.execute("BEGIN IMMEDIATE")
        blocker.execute("UPDATE settings SET value=value")

        def release() -> None:
            time.sleep(0.15)
            blocker.rollback()
            blocker.close()

        release_thread = threading.Thread(target=release)
        release_thread.start()
        try:
            await self.db.initialize()
        finally:
            release_thread.join()

        connection = self.db._connect()
        try:
            self.assertEqual(
                connection.execute("PRAGMA busy_timeout").fetchone()[0],
                self.db.DATABASE_BUSY_TIMEOUT_MS,
            )
        finally:
            connection.close()

    async def test_current_schema_requires_database_restore_for_legacy_epg_binary(self):
        await self.db.initialize()
        with self.connect() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM sqlite_master "
                    "WHERE type='table' AND name='channel_epg_map'"
                ).fetchone()[0],
                0,
            )
            with self.assertRaisesRegex(sqlite3.OperationalError, "no such table"):
                connection.execute("SELECT * FROM channel_epg_map").fetchall()


if __name__ == "__main__":
    unittest.main()
