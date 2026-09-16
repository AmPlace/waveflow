import asyncio
import importlib
import os
import sqlite3
import sys
import tempfile
import threading
import unittest


def _clear_database_module():
    sys.modules.pop('database', None)


CHANNELS_OLD = [{
    'channel_id': 'old',
    'display_names': '["Old"]',
    'normalized_names': '["old"]',
}]
PROGRAMMES_OLD = [{
    'channel_id': 'old',
    'start': '2026-08-06T00:00:00+00:00',
    'stop': '2026-08-06T01:00:00+00:00',
    'title': 'Old programme',
    'description': '',
}]


class EpgDatabaseTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.old_db_path = os.environ.get('WAVEFLOW_DB_PATH')
        self.db_path = os.path.join(self.tmpdir.name, 'waveflow.db')
        os.environ['WAVEFLOW_DB_PATH'] = self.db_path
        _clear_database_module()
        self.db = importlib.import_module('database')
        await self.db.initialize()
        self.source_id = await self.db.add_epg_source('Test', 'https://example.test/epg.xml')
        self.source = await self.db.get_epg_source(self.source_id)
        await self.db.replace_epg_dataset_atomic(
            self.source_id,
            self.source['revision'],
            CHANNELS_OLD,
            PROGRAMMES_OLD,
            stats=self._stats(1, 1),
        )

    async def asyncTearDown(self):
        if self.old_db_path is None:
            os.environ.pop('WAVEFLOW_DB_PATH', None)
        else:
            os.environ['WAVEFLOW_DB_PATH'] = self.old_db_path
        _clear_database_module()
        self.tmpdir.cleanup()

    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        return conn

    def _stats(self, channels, programmes):
        return {
            'channel_count': channels,
            'programme_count': programmes,
            'data_start_at': '2026-08-06T00:00:00+00:00',
            'data_end_at': '2026-08-06T01:00:00+00:00',
            'finished_at': '2026-08-06T00:05:00+00:00',
        }

    async def _assert_old_dataset(self):
        channels = await self.db.get_epg_channels(self.source_id)
        programmes = await self.db.get_epg_programs(self.source_id, 'old')
        self.assertEqual([row['channel_id'] for row in channels], ['old'])
        self.assertEqual([row['title'] for row in programmes], ['Old programme'])

    async def test_schema_migration_adds_source_refresh_fields_idempotently(self):
        await self.db.initialize()
        await self.db.initialize()
        conn = self._connect()
        try:
            columns = {row['name'] for row in conn.execute('PRAGMA table_info(epg_sources)')}
        finally:
            conn.close()
        self.assertTrue({
            'revision', 'last_attempt_at', 'last_success_at', 'last_error',
            'channel_count', 'programme_count', 'data_start_at', 'data_end_at',
        }.issubset(columns))

    async def test_final_legacy_map_upgrade_is_active_first_transactional_and_idempotent(self):
        now = '2026-08-08T00:00:00+00:00'
        conn = self._connect()
        try:
            conn.execute(
                """
                CREATE TABLE channel_epg_map (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    canonical_key TEXT NOT NULL UNIQUE,
                    epg_source_id INTEGER,
                    epg_channel_id TEXT,
                    match_type TEXT DEFAULT '',
                    confidence INTEGER DEFAULT 0,
                    match_status TEXT DEFAULT 'unmatched',
                    match_detail TEXT DEFAULT '',
                    locked INTEGER DEFAULT 0,
                    updated_at TEXT NOT NULL
                );
                """
            )
            conn.executemany(
                """
                INSERT INTO iptv_logical_channels
                    (id, canonical_key, display_name, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    ('active-eligible', 'eligible', 'Eligible', 'active', now, now),
                    ('orphan-eligible', 'eligible', 'Old Eligible', 'orphaned', now, now),
                    ('orphan-only', 'orphan-only', 'Orphan Only', 'orphaned', now, now),
                    ('active-conflict-a', 'conflict', 'Conflict A', 'active', now, now),
                    ('active-conflict-b', 'conflict', 'Conflict B', 'active', now, now),
                    ('active-missing', 'missing', 'Missing Target', 'active', now, now),
                ],
            )
            conn.executemany(
                """
                INSERT INTO channel_epg_map
                    (canonical_key, epg_source_id, epg_channel_id, match_type,
                     confidence, match_status, locked, updated_at)
                VALUES (?, ?, ?, 'exact_tvg_id', 100, 'matched', 0, ?)
                """,
                [
                    ('eligible', self.source_id, 'old', now),
                    ('orphan-only', self.source_id, 'old', now),
                    ('conflict', self.source_id, 'old', now),
                    ('missing', self.source_id, 'does-not-exist', now),
                ],
            )
            conn.commit()
        finally:
            conn.close()

        await self.db.initialize()
        conn = self._connect()
        try:
            table_count = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE type='table' AND name='channel_epg_map'"
            ).fetchone()[0]
            bindings = [dict(row) for row in conn.execute(
                'SELECT * FROM iptv_logical_channel_epg_bindings '
                'ORDER BY logical_channel_id'
            ).fetchall()]
            integrity = conn.execute('PRAGMA integrity_check').fetchone()[0]
        finally:
            conn.close()

        self.assertEqual(table_count, 0)
        self.assertEqual(integrity, 'ok')
        self.assertEqual([row['logical_channel_id'] for row in bindings], ['active-eligible'])
        self.assertEqual(bindings[0]['epg_source_id'], self.source_id)
        self.assertEqual(bindings[0]['epg_channel_id'], 'old')
        self.assertEqual(bindings[0]['origin'], 'legacy_migrated')
        self.assertNotIn('orphan-eligible', {
            row['logical_channel_id'] for row in bindings
        })

        await self.db.initialize()
        conn = self._connect()
        try:
            count = conn.execute(
                'SELECT COUNT(*) FROM iptv_logical_channel_epg_bindings'
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(count, 1)

    async def test_legacy_database_upgrade_preserves_epg_rows_and_backfills_state(self):
        legacy_path = os.path.join(self.tmpdir.name, 'legacy.db')
        conn = sqlite3.connect(legacy_path)
        conn.executescript(
            """
            CREATE TABLE epg_sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                url TEXT NOT NULL UNIQUE,
                enabled INTEGER DEFAULT 1,
                last_fetched_at TEXT DEFAULT '',
                last_status TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE epg_channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id INTEGER NOT NULL REFERENCES epg_sources(id) ON DELETE CASCADE,
                channel_id TEXT NOT NULL,
                display_names TEXT NOT NULL,
                normalized_names TEXT NOT NULL,
                UNIQUE(source_id, channel_id)
            );
            CREATE TABLE epg_programs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id INTEGER NOT NULL REFERENCES epg_sources(id) ON DELETE CASCADE,
                channel_id TEXT NOT NULL,
                start TEXT NOT NULL,
                stop TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT DEFAULT ''
            );
            """
        )
        conn.execute(
            "INSERT INTO epg_sources VALUES(1, 'Legacy', 'https://example.test/legacy.xml', 1, ?, 'ok', ?, ?)",
            ('2026-08-05T00:00:00+00:00', '2026-08-01T00:00:00+00:00', '2026-08-05T00:00:00+00:00'),
        )
        conn.execute(
            "INSERT INTO epg_channels(source_id, channel_id, display_names, normalized_names) VALUES(1, 'legacy', '[\"Legacy\"]', '[\"legacy\"]')"
        )
        conn.execute(
            "INSERT INTO epg_programs(source_id, channel_id, start, stop, title) VALUES(1, 'legacy', ?, ?, 'Legacy programme')",
            ('2026-08-05T00:00:00+00:00', '2026-08-05T01:00:00+00:00'),
        )
        conn.commit()
        conn.close()

        current_path = self.db.DB_PATH
        self.db.DB_PATH = legacy_path
        try:
            await self.db.initialize()
            await self.db.initialize()
            source = await self.db.get_epg_source(1)
            self.assertEqual(source['revision'], 1)
            self.assertEqual(source['last_status'], 'success')
            self.assertEqual(source['last_success_at'], '2026-08-05T00:00:00+00:00')
            self.assertEqual(source['channel_count'], 1)
            self.assertEqual(source['programme_count'], 1)
            self.assertEqual([row['channel_id'] for row in await self.db.get_epg_channels(1)], ['legacy'])
            self.assertEqual(
                [row['title'] for row in await self.db.get_epg_programs(1, 'legacy')],
                ['Legacy programme'],
            )
        finally:
            self.db.DB_PATH = current_path

    async def test_atomic_replace_updates_both_tables_and_success_state(self):
        channels = [{
            'channel_id': 'new',
            'display_names': '["New"]',
            'normalized_names': '["new"]',
        }]
        programmes = [{
            'channel_id': 'new',
            'start': '2026-08-07T00:00:00+00:00',
            'stop': '2026-08-07T01:00:00+00:00',
            'title': 'New programme',
            'description': '',
        }]
        committed = await self.db.replace_epg_dataset_atomic(
            self.source_id,
            self.source['revision'],
            channels,
            programmes,
            stats={
                'channel_count': 1,
                'programme_count': 1,
                'data_start_at': programmes[0]['start'],
                'data_end_at': programmes[0]['stop'],
                'finished_at': '2026-08-07T00:05:00+00:00',
            },
        )
        self.assertTrue(committed['committed'])
        self.assertEqual([row['channel_id'] for row in await self.db.get_epg_channels(self.source_id)], ['new'])
        self.assertEqual([row['title'] for row in await self.db.get_epg_programs(self.source_id, 'new')], ['New programme'])
        source = await self.db.get_epg_source(self.source_id)
        self.assertEqual(source['last_status'], 'success')
        self.assertEqual(source['channel_count'], 1)
        self.assertEqual(source['programme_count'], 1)
        self.assertEqual(source['last_success_at'], '2026-08-07T00:05:00+00:00')

    async def test_atomic_replace_drains_worker_before_propagating_cancellation(self):
        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        original_connect = self.db._connect

        def connect_with_commit_barrier():
            connection = original_connect()

            def trace(statement):
                if 'UPDATE epg_sources' in statement and 'last_fetched_at' in statement:
                    entered.set()
                    release.wait(5)
                    finished.set()

            connection.set_trace_callback(trace)
            return connection

        self.db._connect = connect_with_commit_barrier
        new_channel = {
            'channel_id': 'new',
            'display_names': '["New"]',
            'normalized_names': '["new"]',
        }
        new_programme = {**PROGRAMMES_OLD[0], 'channel_id': 'new', 'title': 'New programme'}
        task = asyncio.create_task(self.db.replace_epg_dataset_atomic(
            self.source_id,
            self.source['revision'],
            [new_channel],
            [new_programme],
            stats=self._stats(1, 1),
        ))
        try:
            await asyncio.wait_for(asyncio.to_thread(entered.wait, 5), 6)
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            await asyncio.wait_for(asyncio.to_thread(finished.wait, 5), 6)
        finally:
            release.set()
            self.db._connect = original_connect

        self.assertEqual(
            [row['channel_id'] for row in await self.db.get_epg_channels(self.source_id)],
            ['new'],
        )
        self.assertEqual(
            [row['title'] for row in await self.db.get_epg_programs(self.source_id, 'new')],
            ['New programme'],
        )

    async def test_duplicate_channel_insert_rolls_back_old_dataset(self):
        duplicate = {
            'channel_id': 'dup',
            'display_names': '["Dup"]',
            'normalized_names': '["dup"]',
        }
        with self.assertRaises(sqlite3.IntegrityError):
            await self.db.replace_epg_dataset_atomic(
                self.source_id,
                self.source['revision'],
                [duplicate, duplicate],
                PROGRAMMES_OLD,
                stats=self._stats(2, 1),
            )
        await self._assert_old_dataset()

    async def test_programme_insert_failure_rolls_back_channels_and_programmes(self):
        conn = self._connect()
        try:
            conn.execute("CREATE TRIGGER fail_epg_programme BEFORE INSERT ON epg_programs BEGIN SELECT RAISE(ABORT, 'programme failure'); END")
            conn.commit()
        finally:
            conn.close()
        with self.assertRaises(sqlite3.IntegrityError):
            await self.db.replace_epg_dataset_atomic(
                self.source_id,
                self.source['revision'],
                [{
                    'channel_id': 'new',
                    'display_names': '["New"]',
                    'normalized_names': '["new"]',
                }],
                [{**PROGRAMMES_OLD[0], 'channel_id': 'new'}],
                stats=self._stats(1, 1),
            )
        await self._assert_old_dataset()

    async def test_source_state_failure_rolls_back_dataset(self):
        conn = self._connect()
        try:
            conn.execute("CREATE TRIGGER fail_epg_source_state BEFORE UPDATE OF last_success_at ON epg_sources BEGIN SELECT RAISE(ABORT, 'state failure'); END")
            conn.commit()
        finally:
            conn.close()
        with self.assertRaises(sqlite3.IntegrityError):
            await self.db.replace_epg_dataset_atomic(
                self.source_id,
                self.source['revision'],
                [{
                    'channel_id': 'new',
                    'display_names': '["New"]',
                    'normalized_names': '["new"]',
                }],
                [{**PROGRAMMES_OLD[0], 'channel_id': 'new'}],
                stats=self._stats(1, 1),
            )
        await self._assert_old_dataset()

    async def test_wrong_revision_and_disabled_source_do_not_replace_dataset(self):
        wrong = await self.db.replace_epg_dataset_atomic(
            self.source_id,
            self.source['revision'] + 1,
            CHANNELS_OLD,
            PROGRAMMES_OLD,
            stats=self._stats(1, 1),
        )
        self.assertFalse(wrong['committed'])
        self.assertEqual(wrong['reason'], 'revision_discarded')

        await self.db.update_epg_source(self.source_id, enabled=0)
        disabled_source = await self.db.get_epg_source(self.source_id)
        disabled = await self.db.replace_epg_dataset_atomic(
            self.source_id,
            disabled_source['revision'],
            CHANNELS_OLD,
            PROGRAMMES_OLD,
            stats=self._stats(1, 1),
        )
        self.assertFalse(disabled['committed'])
        self.assertEqual(disabled['reason'], 'disabled')
        await self._assert_old_dataset()

    async def test_request_identity_changes_increment_revision(self):
        original = await self.db.get_epg_source(self.source_id)
        await self.db.update_epg_source(self.source_id, name='Renamed')
        renamed = await self.db.get_epg_source(self.source_id)
        self.assertEqual(renamed['revision'], original['revision'])
        await self.db.update_epg_source(self.source_id, url='https://example.test/new.xml')
        changed = await self.db.get_epg_source(self.source_id)
        self.assertEqual(changed['revision'], original['revision'] + 1)
        self.assertEqual(changed['last_status'], 'revision_discarded')
        await self.db.update_epg_source(self.source_id, enabled=0)
        disabled = await self.db.get_epg_source(self.source_id)
        self.assertEqual(disabled['revision'], original['revision'] + 2)
        self.assertEqual(disabled['last_status'], 'disabled')

    async def test_failure_state_preserves_last_success_and_counts(self):
        before = await self.db.get_epg_source(self.source_id)
        updated = await self.db.record_epg_source_refresh_failure(
            self.source_id,
            before['revision'],
            status='stale',
            attempted_at='2026-08-06T02:00:00+00:00',
            error='timeout',
        )
        self.assertTrue(updated)
        after = await self.db.get_epg_source(self.source_id)
        self.assertEqual(after['last_status'], 'stale')
        self.assertEqual(after['last_error'], 'timeout')
        self.assertEqual(after['last_success_at'], before['last_success_at'])
        self.assertEqual(after['channel_count'], before['channel_count'])
        self.assertEqual(after['programme_count'], before['programme_count'])

    async def test_programme_range_uses_half_open_stop_boundary(self):
        programmes = [
            {
                'channel_id': 'old',
                'start': '2026-08-05T00:00:00+00:00',
                'stop': '2026-08-05T01:00:00+00:00',
                'title': 'Before boundary',
                'description': '',
            },
            {
                'channel_id': 'old',
                'start': '2026-08-05T01:00:00+00:00',
                'stop': '2026-08-05T02:00:00+00:00',
                'title': 'At boundary',
                'description': '',
            },
        ]
        await self.db.replace_epg_dataset_atomic(
            self.source_id,
            self.source['revision'],
            CHANNELS_OLD,
            programmes,
            stats=self._stats(1, 2),
        )

        rows = await self.db.get_epg_programs(
            self.source_id,
            'old',
            start_after='2026-08-05T01:00:00+00:00',
        )
        self.assertEqual([row['title'] for row in rows], ['At boundary'])


if __name__ == '__main__':
    unittest.main()
