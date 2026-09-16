import copy
import importlib
import inspect
import sys
import unittest
from unittest import mock


class EpgMatcherTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog_module = importlib.import_module('epg_catalog')
        cls.matcher = importlib.import_module('epg_matcher')

    @staticmethod
    def _row(source_id, channel_id, names=(), *, enabled=True, status='success'):
        return {
            'source_id': source_id,
            'source_name': f'Source {source_id}',
            'source_enabled': int(enabled),
            'source_status': status,
            'channel_id': channel_id,
            'display_names': list(names),
            'normalized_names': [],
        }

    def _catalog(self, *rows):
        return self.catalog_module.build_epg_channel_catalog_from_rows(rows)

    @staticmethod
    def _hint(**overrides):
        value = {
            'logical_channel_id': 'lc-1',
            'canonical_key': 'cctv1',
            'display_name': 'CCTV1',
            'status': 'active',
            'member_channel_ids': [20, 10],
            'raw_tvg_ids': [],
            'raw_tvg_names': [],
            'raw_display_names': [],
        }
        value.update(overrides)
        return value

    def _binding(self, source_id, channel_id, *, locked=False, match_type='manual', confidence=100):
        return self.matcher.ExistingBindingSnapshot(
            target=self.catalog_module.EpgChannelIdentity(source_id, channel_id),
            locked=locked,
            status='matched',
            match_type=match_type,
            confidence=confidence,
            origin='manual',
        )

    def test_same_input_is_deterministic_does_not_mutate_and_has_no_io(self):
        hint = self._hint(raw_tvg_ids=['CCTV1'])
        original = copy.deepcopy(hint)
        catalog = self._catalog(self._row(1, 'CCTV1', ('CCTV One',)))
        database = importlib.import_module('database')
        with mock.patch.object(database, '_connect', side_effect=AssertionError('database access')):
            first = self.matcher.match_logical_channel(hint, catalog)
            second = self.matcher.match_logical_channel(hint, catalog)
        self.assertEqual(first, second)
        self.assertEqual(hint, original)
        source = inspect.getsource(self.matcher)
        self.assertNotIn('requests.', source)
        self.assertNotIn('urllib.', source)
        self.assertNotIn('channel_epg_map', source)
        self.assertNotIn('iptv_logical_channel_epg_bindings', source)

    def test_locked_valid_and_stale_binding_are_preserved(self):
        success = self._catalog(self._row(2, 'BOUND', ('Bound',)))
        decision = self.matcher.match_logical_channel(
            self._hint(raw_tvg_ids=['OTHER']), success,
            existing_binding=self._binding(2, 'BOUND', locked=True),
        )
        self.assertEqual(decision.status, 'locked_preserved')
        self.assertEqual(decision.selected_identity, self.catalog_module.EpgChannelIdentity(2, 'BOUND'))
        self.assertEqual(decision.existing_binding_action, 'preserve_locked')

        stale = self._catalog(self._row(2, 'BOUND', ('Bound',), status='stale'))
        decision = self.matcher.match_logical_channel(
            self._hint(), stale,
            existing_binding=self._binding(2, 'BOUND', locked=True),
        )
        self.assertEqual(decision.status, 'locked_preserved')
        self.assertEqual(decision.candidates[0].source_status, 'stale')
        self.assertFalse(decision.candidates[0].auto_applicable)

    def test_locked_or_unlocked_missing_target_never_falls_back_cross_source(self):
        catalog = self._catalog(self._row(9, 'SAME', ('Same',)))
        for locked in (True, False):
            decision = self.matcher.match_logical_channel(
                self._hint(raw_tvg_ids=['SAME']), catalog,
                existing_binding=self._binding(1, 'SAME', locked=locked),
            )
            self.assertEqual(decision.status, 'conflict')
            self.assertIsNone(decision.selected_identity)
            self.assertEqual(decision.existing_binding_action, 'orphan_target')

    def test_logical_conflicts_disable_all_diagnostic_candidates(self):
        catalog = self._catalog(self._row(1, 'CCTV1', ('CCTV1',)))
        for status in ('split_conflict', 'merge_conflict'):
            decision = self.matcher.match_logical_channel(
                self._hint(status=status, raw_tvg_ids=['CCTV1']), catalog
            )
            self.assertEqual(decision.status, 'conflict')
            self.assertIsNone(decision.selected_identity)
            self.assertTrue(decision.candidates)
            self.assertTrue(all(not candidate.auto_applicable for candidate in decision.candidates))

    def test_explicit_not_applicable_stops_but_unknown_continues(self):
        catalog = self._catalog(self._row(1, 'CCTV1', ('CCTV1',)))
        stopped = self.matcher.match_logical_channel(
            self._hint(raw_tvg_ids=['CCTV1']), catalog, applicability='not_applicable'
        )
        self.assertEqual(stopped.status, 'not_applicable')
        self.assertEqual(stopped.candidates, ())
        continued = self.matcher.match_logical_channel(
            self._hint(raw_tvg_ids=['CCTV1']), catalog, applicability='unknown'
        )
        self.assertEqual(continued.status, 'matched')

    def test_unique_exact_tvg_id_matches_and_member_variants_normalize(self):
        catalog = self._catalog(self._row(3, 'CCTV1', ('Wrong Name',)))
        decision = self.matcher.match_logical_channel(
            self._hint(raw_tvg_ids=['CCTV1', 'CCTV-1']), catalog
        )
        self.assertEqual(decision.status, 'matched')
        self.assertEqual(decision.match_type, 'exact_tvg_id')
        self.assertEqual(decision.confidence, 100)
        self.assertEqual(decision.selected_identity.source_id, 3)

    def test_conflicting_member_tvg_ids_return_conflict(self):
        catalog = self._catalog(
            self._row(1, 'CCTV1', ('CCTV1',)),
            self._row(1, 'CCTV2', ('CCTV2',)),
        )
        decision = self.matcher.match_logical_channel(
            self._hint(raw_tvg_ids=['CCTV1', 'CCTV2']), catalog
        )
        self.assertEqual(decision.status, 'conflict')
        self.assertIn('tvg_id_conflict', decision.hint_conflicts)
        self.assertTrue(all(not candidate.auto_applicable for candidate in decision.candidates))

    def test_same_id_across_sources_is_ambiguous_and_order_independent(self):
        rows = [self._row(2, 'CCTV1', ('CCTV1',)), self._row(1, 'CCTV1', ('CCTV1',))]
        first = self.matcher.match_logical_channel(
            self._hint(raw_tvg_ids=['CCTV1']), self._catalog(*rows)
        )
        second = self.matcher.match_logical_channel(
            self._hint(raw_tvg_ids=['CCTV1']), self._catalog(*reversed(rows))
        )
        self.assertEqual(first, second)
        self.assertEqual(first.status, 'ambiguous')
        self.assertIsNone(first.selected_identity)
        self.assertEqual([c.source_id for c in first.candidates[:2]], [1, 2])
        self.assertIn('multi_source_collision', first.reasons)

    def test_exact_tvg_id_beats_name_and_evidence_is_merged_per_identity(self):
        catalog = self._catalog(
            self._row(1, 'ID-WIN', ('Shared Name',)),
            self._row(2, 'NAME-WIN', ('Logical Name',)),
        )
        decision = self.matcher.match_logical_channel(
            self._hint(
                display_name='Logical Name', raw_tvg_ids=['ID-WIN'],
                raw_tvg_names=['Shared Name'], raw_display_names=['Shared Name'],
            ),
            catalog,
        )
        self.assertEqual(decision.status, 'matched')
        self.assertEqual(decision.selected_identity, self.catalog_module.EpgChannelIdentity(1, 'ID-WIN'))
        candidate = next(c for c in decision.candidates if c.identity == decision.selected_identity)
        self.assertEqual(candidate.match_type, 'exact_tvg_id')
        self.assertGreaterEqual(len(candidate.evidence), 3)
        self.assertEqual(len({c.identity for c in decision.candidates}), len(decision.candidates))

    def test_exact_tvg_name_raw_name_and_logical_name_rules(self):
        cases = [
            (self._hint(raw_tvg_names=['TV Guide Name'], display_name='No'), 'exact_tvg_name', 95, 'TV Guide Name'),
            (self._hint(raw_display_names=['Raw Name'], display_name='No'), 'exact_name', 90, 'Raw Name'),
            (self._hint(display_name='Logical Name'), 'exact_name', 90, 'Logical Name'),
        ]
        for hint, match_type, confidence, name in cases:
            with self.subTest(match_type=match_type, name=name):
                catalog = self._catalog(self._row(1, 'TARGET', (name,)))
                decision = self.matcher.match_logical_channel(hint, catalog)
                self.assertEqual(decision.status, 'matched')
                self.assertEqual(decision.match_type, match_type)
                self.assertEqual(decision.confidence, confidence)

    def test_multiple_exact_or_normalized_candidates_are_ambiguous(self):
        for hint in (
            self._hint(raw_tvg_names=['Shared']),
            self._hint(raw_display_names=['Shared']),
            self._hint(display_name='CCTV-1'),
        ):
            with self.subTest(hint=hint):
                catalog = self._catalog(
                    self._row(1, 'ONE', ('Shared', 'CCTV1')),
                    self._row(2, 'TWO', ('Shared', 'CCTV1')),
                )
                decision = self.matcher.match_logical_channel(hint, catalog)
                self.assertEqual(decision.status, 'ambiguous')
                self.assertIsNone(decision.selected_identity)

    def test_normalized_channel_id_normalized_name_alias_and_no_fuzzy(self):
        normalized_id_catalog = self._catalog(self._row(1, 'CCTV-1 HD', ('Other',)))
        normalized_id = self.matcher.match_logical_channel(
            self._hint(canonical_key='cctv1', display_name='No'), normalized_id_catalog
        )
        self.assertEqual((normalized_id.status, normalized_id.match_type), ('matched', 'normalized_channel_id'))

        normalized_name_catalog = self._catalog(self._row(1, 'X', ('CCTV-1 HD',)))
        normalized_name = self.matcher.match_logical_channel(
            self._hint(canonical_key='none', display_name='CCTV1'), normalized_name_catalog
        )
        self.assertEqual((normalized_name.status, normalized_name.match_type), ('matched', 'normalized_name'))

        alias_catalog = self._catalog(self._row(1, 'TARGET', ('Canonical Station',)))
        alias = self.matcher.match_logical_channel(
            self._hint(canonical_key='none', display_name='Station Alias'),
            alias_catalog,
            alias_resolver=lambda value: 'Canonical Station' if value == 'Station Alias' else value,
        )
        self.assertEqual((alias.status, alias.match_type), ('matched', 'alias'))

        fuzzy = self.matcher.match_logical_channel(
            self._hint(canonical_key='none', display_name='Canonical Statio'), alias_catalog
        )
        self.assertEqual(fuzzy.status, 'unmatched')

    def test_source_state_controls_only_new_auto_application(self):
        for status, enabled in (
            ('stale', True), ('failed', True), ('revision_discarded', True), ('success', False),
        ):
            with self.subTest(status=status, enabled=enabled):
                catalog = self._catalog(self._row(1, 'CCTV1', ('CCTV1',), enabled=enabled, status=status))
                decision = self.matcher.match_logical_channel(
                    self._hint(raw_tvg_ids=['CCTV1']), catalog
                )
                self.assertEqual(decision.status, 'unmatched')
                self.assertTrue(decision.candidates)
                self.assertFalse(decision.candidates[0].auto_applicable)
        success = self._catalog(self._row(1, 'CCTV1', ('CCTV1',), enabled=True, status='success'))
        self.assertEqual(
            self.matcher.match_logical_channel(self._hint(raw_tvg_ids=['CCTV1']), success).status,
            'matched',
        )

    def test_unlocked_existing_binding_is_preserved_and_never_auto_replaced(self):
        catalog = self._catalog(
            self._row(1, 'OLD', ('Old',)),
            self._row(2, 'NEW', ('New',)),
        )
        supported = self.matcher.match_logical_channel(
            self._hint(raw_tvg_ids=['OLD']), catalog,
            existing_binding=self._binding(1, 'OLD', locked=False),
        )
        self.assertEqual(supported.status, 'existing_preserved')
        self.assertEqual(supported.existing_binding_action, 'preserve')
        self.assertEqual(supported.selected_identity.source_id, 1)

        higher = self.matcher.match_logical_channel(
            self._hint(raw_tvg_ids=['NEW']), catalog,
            existing_binding=self._binding(1, 'OLD', locked=False, confidence=40),
        )
        self.assertEqual(higher.status, 'existing_preserved')
        self.assertEqual(higher.selected_identity, self.catalog_module.EpgChannelIdentity(1, 'OLD'))
        self.assertEqual(higher.existing_binding_action, 'review_recommended')
        self.assertTrue(all(not candidate.auto_applicable for candidate in higher.candidates))

        ambiguous = self.matcher.match_logical_channel(
            self._hint(raw_tvg_ids=['OLD']),
            self._catalog(self._row(1, 'OLD', ('Old',)), self._row(2, 'OLD', ('Old',))),
            existing_binding=self._binding(1, 'OLD', locked=False),
        )
        self.assertEqual(ambiguous.status, 'existing_preserved')
        self.assertIn('multiple_composite_candidates', ambiguous.reasons)
        self.assertEqual(ambiguous.existing_binding_action, 'review_recommended')

        conflict = self.matcher.match_logical_channel(
            self._hint(raw_tvg_ids=['OLD', 'NEW']), catalog,
            existing_binding=self._binding(1, 'OLD', locked=False),
        )
        self.assertEqual(conflict.status, 'conflict')
        self.assertEqual(conflict.selected_identity, self.catalog_module.EpgChannelIdentity(1, 'OLD'))
        self.assertEqual(conflict.existing_binding_action, 'review_recommended')

    def test_candidate_sort_confidence_types_evidence_and_legacy_compatibility(self):
        catalog = self._catalog(
            self._row(9, 'ID', ('Name',)),
            self._row(3, 'ID', ('Name',)),
        )
        decision = self.matcher.match_logical_channel(
            self._hint(raw_tvg_ids=['ID'], raw_tvg_names=['Name']), catalog
        )
        self.assertEqual(
            [candidate.stable_sort_key for candidate in decision.candidates],
            sorted(candidate.stable_sort_key for candidate in decision.candidates),
        )
        self.assertEqual(self.matcher.MATCH_RULES['exact_tvg_id'][1], 100)
        self.assertEqual(self.matcher.MATCH_RULES['alias'][1], 70)
        self.assertEqual(self.matcher.convert_legacy_match_type('raw_tvg_id'), 'exact_tvg_id')
        self.assertEqual(self.matcher.convert_legacy_match_type('canonical_key'), 'normalized_channel_id')
        self.assertEqual(self.matcher.convert_legacy_match_type('name_exact'), 'exact_name')
        self.assertEqual(self.matcher.convert_legacy_match_type('normalized'), 'normalized_name')
        self.assertEqual(self.matcher.convert_legacy_match_type('alias'), 'alias')
        self.assertEqual(self.matcher.convert_legacy_match_type('manual'), 'manual')
        serialized = repr(decision.as_dict()).lower()
        self.assertNotIn('url', serialized)
        self.assertNotIn('token', serialized)
        self.assertNotIn('programme', serialized)

        secret_catalog = self._catalog(self._row(1, 'secret', ('https://secret.example/feed?token=abc',)))
        secret = self.matcher.match_logical_channel(
            self._hint(display_name='https://secret.example/feed?token=abc'), secret_catalog
        )
        self.assertNotIn('secret.example', repr(secret.as_dict()))
        self.assertNotIn('token=abc', repr(secret.as_dict()))

    def test_diagnostics_count_all_domain_outcomes(self):
        success = self._catalog(self._row(1, 'ONE', ('One',)))
        stale = self._catalog(self._row(1, 'ONE', ('One',), status='stale'))
        ambiguous_catalog = self._catalog(
            self._row(1, 'ONE', ('One',)), self._row(2, 'ONE', ('One',))
        )
        decisions = [
            self.matcher.match_logical_channel(self._hint(raw_tvg_ids=['ONE']), success),
            self.matcher.match_logical_channel(self._hint(raw_tvg_ids=['ONE']), ambiguous_catalog),
            self.matcher.match_logical_channel(self._hint(raw_tvg_ids=['ONE']), stale),
            self.matcher.match_logical_channel(self._hint(raw_tvg_ids=['ONE', 'TWO']), success),
            self.matcher.match_logical_channel(self._hint(), success, applicability='not_applicable'),
            self.matcher.match_logical_channel(
                self._hint(), success, existing_binding=self._binding(1, 'ONE', locked=True)
            ),
            self.matcher.match_logical_channel(
                self._hint(raw_tvg_ids=['ONE']), success,
                existing_binding=self._binding(1, 'ONE', locked=False),
            ),
        ]
        summary = self.matcher.summarize_match_decisions(decisions)
        self.assertEqual(summary['total_logical_channels'], 7)
        self.assertEqual(summary['matched'], 1)
        self.assertEqual(summary['ambiguous'], 1)
        self.assertEqual(summary['unmatched'], 1)
        self.assertEqual(summary['conflict'], 1)
        self.assertEqual(summary['not_applicable'], 1)
        self.assertEqual(summary['locked_preserved'], 1)
        self.assertEqual(summary['existing_preserved'], 1)
        self.assertEqual(summary['stale_only_candidate_count'], 1)
        self.assertEqual(summary['multi_source_collision_count'], 1)
        self.assertEqual(summary['tvg_id_conflict_count'], 1)


if __name__ == '__main__':
    unittest.main()
