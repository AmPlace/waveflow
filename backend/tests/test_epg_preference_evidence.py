import asyncio
import importlib
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch


def _clear_modules():
    for name in (
        'database', 'epg_preference_evidence', 'epg_source_model',
        'epg_source_preference',
    ):
        sys.modules.pop(name, None)


class EpgPreferenceEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global db
        global parse_m3u_document
        global build_epg_source_preference_snapshot
        global delete_manual_source_preference
        global normalize_epg_source_url
        global replace_subscription_url_tvg_evidence
        global resolve_url_hint_to_sources
        global set_manual_source_preference

        cls.old_db_path = os.environ.get('WAVEFLOW_DB_PATH')
        cls.path = tempfile.NamedTemporaryFile(suffix='.db', delete=False).name
        os.environ['WAVEFLOW_DB_PATH'] = cls.path
        _clear_modules()
        db = importlib.import_module('database')
        parser_module = importlib.import_module('m3u8_parser')
        evidence_module = importlib.import_module('epg_preference_evidence')
        parse_m3u_document = parser_module.parse_m3u_document
        build_epg_source_preference_snapshot = (
            evidence_module.build_epg_source_preference_snapshot
        )
        delete_manual_source_preference = (
            evidence_module.delete_manual_source_preference
        )
        normalize_epg_source_url = evidence_module.normalize_epg_source_url
        replace_subscription_url_tvg_evidence = (
            evidence_module.replace_subscription_url_tvg_evidence
        )
        resolve_url_hint_to_sources = evidence_module.resolve_url_hint_to_sources
        set_manual_source_preference = evidence_module.set_manual_source_preference
        asyncio.run(db.initialize())

    @classmethod
    def tearDownClass(cls):
        os.unlink(cls.path)
        if cls.old_db_path is None:
            os.environ.pop('WAVEFLOW_DB_PATH', None)
        else:
            os.environ['WAVEFLOW_DB_PATH'] = cls.old_db_path
        _clear_modules()

    def setUp(self):
        conn = sqlite3.connect(self.path)
        conn.execute('PRAGMA foreign_keys=ON')
        for table in ('epg_source_preference_evidence', 'epg_programs', 'epg_channels', 'epg_sources', 'channels', 'subscriptions'):
            conn.execute(f'DELETE FROM {table}')
        conn.commit()
        conn.close()

    def test_header_parsing_all_aliases_and_conflicts(self):
        doc = parse_m3u_document(
            '#EXTM3U url-tvg="https://a.test/guide.xml?x=1" '
            'x-tvg-url="https://b.test/guide.xml" tvg-url="https://a.test/guide.xml?x=1"\n'
            '#EXTINF:-1,A\nhttp://stream.test/a\n'
        )
        self.assertEqual(len(doc.channels), 1)
        self.assertEqual({key for key, _ in doc.epg_url_hints}, {'url-tvg', 'tvg-url', 'x-tvg-url'})
        self.assertEqual(sum(url == 'https://a.test/guide.xml?x=1' for _, url in doc.epg_url_hints), 2)

    def test_malformed_or_missing_header_is_safe(self):
        self.assertEqual(parse_m3u_document('#EXTM3U\n#EXTINF:-1,A\nhttp://a\n').epg_url_hints, ())
        self.assertEqual(parse_m3u_document('#EXTM3U url-tvg="not a url"\n#EXTINF:-1,A\nhttp://a\n').epg_url_hints, ())

    def test_url_normalization_preserves_query_and_path(self):
        self.assertEqual(normalize_epg_source_url('HTTPS://Guide.Test:443/a?x=1&y=2'), 'https://guide.test/a?x=1&y=2')
        self.assertNotEqual(normalize_epg_source_url('https://guide.test/a?x=1'), normalize_epg_source_url('https://guide.test/a?x=2'))

    def test_token_query_is_identity_sensitive_but_not_persisted_as_secret(self):
        source_url = 'https://guide.test/xmltv?token=source-secret&signature=abc'
        hint_url = 'https://guide.test/xmltv?token=source-secret&signature=abc'
        other_url = 'https://guide.test/xmltv?token=other-secret&signature=abc'
        source = asyncio.run(db.add_epg_source('Guide', source_url))
        sub = asyncio.run(db.add_subscription('Sub', 'https://playlist.test/a'))

        self.assertEqual(resolve_url_hint_to_sources(hint_url, [{'id': source, 'url': source_url}]).epg_source_id, source)
        self.assertEqual(resolve_url_hint_to_sources(other_url, [{'id': source, 'url': source_url}]).status, 'unresolved')

        asyncio.run(replace_subscription_url_tvg_evidence(sub, (('url-tvg', hint_url),)))
        conn = sqlite3.connect(self.path)
        row = conn.execute(
            'SELECT hint_fingerprint, hint_summary, evidence_json FROM epg_source_preference_evidence WHERE subscription_id=?',
            (sub,),
        ).fetchone()
        conn.close()
        self.assertNotIn('source-secret', repr(row))
        self.assertNotIn('signature=abc', repr(row))
        self.assertNotIn('https://guide.test/xmltv', repr(row))
        self.assertEqual(len(row[0]), 64)

        snapshot = asyncio.run(build_epg_source_preference_snapshot())
        self.assertEqual(snapshot[0].resolution.preferred_source_id, source)
        self.assertNotIn('source-secret', repr(snapshot[0].resolution.as_dict()))

    def test_manual_source_delete_preserves_explicit_evidence_as_missing(self):
        source = asyncio.run(db.add_epg_source('Guide', 'https://guide.test/manual'))
        asyncio.run(set_manual_source_preference(epg_source_id=source, logical_channel_id='logical-manual'))
        conn = sqlite3.connect(self.path)
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM epg_source_preference_evidence WHERE origin='manual' AND epg_source_id=?",
                (source,),
            ).fetchone()[0],
            1,
        )
        conn.close()

        asyncio.run(db.delete_epg_source(source))
        conn = sqlite3.connect(self.path)
        row = conn.execute(
            "SELECT epg_source_id, origin FROM epg_source_preference_evidence WHERE origin='manual'"
        ).fetchone()
        conn.close()
        self.assertEqual(row, (source, 'manual'))

        snapshot = asyncio.run(build_epg_source_preference_snapshot())
        self.assertEqual(snapshot[0].resolution.status, 'missing_source')
        self.assertEqual(snapshot[0].resolution.preferred_source_id, source)

    def test_resolution_unique_missing_and_ambiguous(self):
        sources = [
            {'id': 1, 'url': 'https://guide.test/a?x=1'},
            {'id': 2, 'url': 'https://guide.test/a?x=2'},
        ]
        self.assertEqual(resolve_url_hint_to_sources('HTTPS://GUIDE.TEST:443/a?x=1', sources).epg_source_id, 1)
        self.assertEqual(resolve_url_hint_to_sources('https://none.test/a', sources).status, 'unresolved')
        self.assertEqual(resolve_url_hint_to_sources('https://guide.test/a?x=1', sources).status, 'resolved')
        ambiguous = resolve_url_hint_to_sources(
            'https://guide.test/a?x=1',
            [sources[0], {'id': 3, 'url': 'HTTPS://GUIDE.TEST:443/a?x=1'}],
        )
        self.assertEqual((ambiguous.status, ambiguous.competing_source_ids), ('ambiguous_source', (1, 3)))

    def test_subscription_evidence_replace_is_idempotent_and_snapshot_is_batch(self):
        source = asyncio.run(db.add_epg_source('Guide', 'https://guide.test/a?x=1'))
        sub = asyncio.run(db.add_subscription('Sub', 'https://playlist.test/a'))
        hints = (('url-tvg', 'https://guide.test/a?x=1'),)
        asyncio.run(replace_subscription_url_tvg_evidence(sub, hints))
        asyncio.run(replace_subscription_url_tvg_evidence(sub, hints))
        conn = sqlite3.connect(self.path)
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM epg_source_preference_evidence').fetchone()[0], 1)
        conn.execute("UPDATE epg_sources SET enabled=0, last_status='disabled' WHERE id=?", (source,))
        conn.commit()
        conn.close()
        snapshot = asyncio.run(build_epg_source_preference_snapshot())
        self.assertEqual(snapshot[0].resolution.status, 'stale_preference')

    def test_source_url_change_does_not_rebind_old_evidence(self):
        source = asyncio.run(db.add_epg_source('Guide', 'https://guide.test/old'))
        sub = asyncio.run(db.add_subscription('Sub', 'https://playlist.test/a'))
        asyncio.run(replace_subscription_url_tvg_evidence(sub, (('url-tvg', 'https://guide.test/old'),)))
        asyncio.run(db.update_epg_source(source, url='https://guide.test/new'))
        snapshot = asyncio.run(build_epg_source_preference_snapshot())
        self.assertEqual(snapshot[0].resolution.status, 'missing_source')
        self.assertIsNone(snapshot[0].resolution.preferred_source_id)

    def test_ambiguous_evidence_is_preserved_as_conflict(self):
        first = asyncio.run(db.add_epg_source('Guide A', 'https://guide.test/same'))
        second = asyncio.run(db.add_epg_source('Guide B', 'HTTPS://GUIDE.TEST:443/same'))
        sub = asyncio.run(db.add_subscription('Sub', 'https://playlist.test/a'))
        asyncio.run(replace_subscription_url_tvg_evidence(sub, (('url-tvg', 'https://guide.test/same'),)))
        snapshot = asyncio.run(build_epg_source_preference_snapshot())
        self.assertEqual(snapshot[0].resolution.status, 'conflict')
        self.assertIsNone(snapshot[0].resolution.preferred_source_id)
        self.assertEqual(
            {item.epg_source_id for item in snapshot[0].resolution.competing_preferences},
            {first, second},
        )

    def test_manual_same_context_is_idempotent_and_different_source_conflicts(self):
        first = asyncio.run(db.add_epg_source('Guide A', 'https://guide.test/a'))
        second = asyncio.run(db.add_epg_source('Guide B', 'https://guide.test/b'))
        asyncio.run(set_manual_source_preference(epg_source_id=first, logical_channel_id='logical-1'))
        asyncio.run(set_manual_source_preference(epg_source_id=first, logical_channel_id='logical-1'))
        asyncio.run(set_manual_source_preference(epg_source_id=second, logical_channel_id='logical-1'))
        snapshot = asyncio.run(build_epg_source_preference_snapshot())
        self.assertEqual(snapshot[0].resolution.status, 'conflict')
        self.assertEqual(snapshot[0].evidence_count, 2)
        asyncio.run(delete_manual_source_preference(epg_source_id=second, logical_channel_id='logical-1'))
        snapshot = asyncio.run(build_epg_source_preference_snapshot())
        self.assertEqual(snapshot[0].resolution.status, 'stale_preference')
        asyncio.run(delete_manual_source_preference(epg_source_id=second, logical_channel_id='logical-1'))

    def test_subscription_delete_cascades_derived_evidence(self):
        asyncio.run(db.add_epg_source('Guide', 'https://guide.test/a'))
        sub = asyncio.run(db.add_subscription('Sub', 'https://playlist.test/a'))
        asyncio.run(replace_subscription_url_tvg_evidence(sub, (('url-tvg', 'https://guide.test/a'),)))
        asyncio.run(db.delete_subscription(sub))
        conn = sqlite3.connect(self.path)
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM epg_source_preference_evidence').fetchone()[0], 0)
        conn.close()

    def test_source_delete_is_reported_without_rebinding(self):
        source = asyncio.run(db.add_epg_source('Guide', 'https://guide.test/a'))
        sub = asyncio.run(db.add_subscription('Sub', 'https://playlist.test/a'))
        asyncio.run(replace_subscription_url_tvg_evidence(sub, (('url-tvg', 'https://guide.test/a'),)))
        asyncio.run(db.delete_epg_source(source))
        snapshot = asyncio.run(build_epg_source_preference_snapshot())
        self.assertEqual(snapshot[0].resolution.status, 'missing_source')
        self.assertIsNone(snapshot[0].resolution.preferred_source_id)

    def test_refresh_changed_or_removed_hint_replaces_current_evidence(self):
        first = asyncio.run(db.add_epg_source('Guide A', 'https://guide.test/a'))
        second = asyncio.run(db.add_epg_source('Guide B', 'https://guide.test/b'))
        sub = asyncio.run(db.add_subscription('Sub', 'https://playlist.test/a'))
        asyncio.run(replace_subscription_url_tvg_evidence(sub, (('url-tvg', 'https://guide.test/a'),)))
        asyncio.run(replace_subscription_url_tvg_evidence(sub, (('x-tvg-url', 'https://guide.test/b'),)))
        conn = sqlite3.connect(self.path)
        rows = conn.execute(
            'SELECT epg_source_id FROM epg_source_preference_evidence WHERE subscription_id=?', (sub,)
        ).fetchall()
        self.assertEqual(rows, [(second,)])
        self.assertNotEqual(first, second)
        conn.close()
        asyncio.run(replace_subscription_url_tvg_evidence(sub, ()))
        conn = sqlite3.connect(self.path)
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM epg_source_preference_evidence').fetchone()[0], 0)
        conn.close()

    def test_manual_precedence_over_url_tvg(self):
        derived = asyncio.run(db.add_epg_source('Derived', 'https://guide.test/derived'))
        manual = asyncio.run(db.add_epg_source('Manual', 'https://guide.test/manual'))
        sub = asyncio.run(db.add_subscription('Sub', 'https://playlist.test/a'))
        conn = sqlite3.connect(self.path)
        conn.execute("UPDATE epg_sources SET last_status='success' WHERE id IN (?, ?)", (derived, manual))
        conn.commit()
        conn.close()
        asyncio.run(replace_subscription_url_tvg_evidence(sub, (('url-tvg', 'https://guide.test/derived'),)))
        asyncio.run(set_manual_source_preference(epg_source_id=manual, subscription_id=sub))
        snapshot = asyncio.run(build_epg_source_preference_snapshot())
        self.assertEqual(snapshot[0].resolution.status, 'preferred')
        self.assertEqual(snapshot[0].resolution.preferred_source_id, manual)
        self.assertEqual(snapshot[0].resolution.origin, 'manual')

    def test_snapshot_uses_one_batch_loader_for_many_contexts(self):
        import epg_preference_evidence as evidence_module

        sources = [{'id': 1, 'url': 'https://guide.test/a', 'enabled': 1, 'last_status': 'success'}]
        rows = [
            {
                'id': index,
                'subscription_id': index,
                'logical_channel_id': '',
                'origin': 'manual',
                'epg_source_id': 1,
                'hint_fingerprint': '',
                'resolution_status': 'manual',
                'evidence_json': '{}',
            }
            for index in range(1, 101)
        ]
        loader = AsyncMock(return_value=(sources, rows))
        with patch.object(evidence_module, '_load_preference_inputs', loader):
            snapshot = asyncio.run(build_epg_source_preference_snapshot())
        self.assertEqual(len(snapshot), 100)
        loader.assert_awaited_once_with()

    def test_maintenance_failure_isolated_and_cancellation_propagates(self):
        import epg_preference_evidence as evidence_module

        with patch.object(
            evidence_module, 'replace_subscription_url_tvg_evidence', AsyncMock(side_effect=RuntimeError('boom'))
        ):
            self.assertFalse(asyncio.run(evidence_module.maintain_subscription_preference_evidence(1, ())))
        with patch.object(
            evidence_module, 'replace_subscription_url_tvg_evidence', AsyncMock(side_effect=asyncio.CancelledError())
        ):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(evidence_module.maintain_subscription_preference_evidence(1, ()))


if __name__ == '__main__':
    unittest.main()
