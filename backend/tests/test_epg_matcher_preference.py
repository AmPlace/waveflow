import unittest

from epg_catalog import EpgChannelIdentity, build_epg_channel_catalog_from_rows
from epg_matcher import ExistingBindingSnapshot, match_logical_channel
from epg_source_preference import (
    EpgSourcePreference,
    EpgSourcePreferenceResolution,
    EpgSourceState,
    resolve_logical_channel_source_preference,
)


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


def _hint(**overrides):
    value = {
        'logical_channel_id': 'logical-1',
        'canonical_key': 'station',
        'display_name': 'Station',
        'status': 'active',
        'member_channel_ids': [2, 1],
        'subscription_ids': [20, 10],
        'raw_tvg_ids': [],
        'raw_tvg_names': [],
        'raw_display_names': [],
    }
    value.update(overrides)
    return value


def _preferred(source_id, origin='manual'):
    return EpgSourcePreferenceResolution(source_id, 'preferred', origin=origin)


class EpgMatcherPreferenceTests(unittest.TestCase):
    def test_none_and_unrelated_preference_keep_original_decisions(self):
        catalog = build_epg_channel_catalog_from_rows([_row(1, 'ONE')])
        hint = _hint(raw_tvg_ids=['ONE'])
        original = match_logical_channel(hint, catalog)
        self.assertEqual(original, match_logical_channel(hint, catalog, preference=None))
        self.assertEqual(original, match_logical_channel(hint, catalog, preference=_preferred(2)))

    def test_preference_never_overrides_stronger_tier(self):
        catalog = build_epg_channel_catalog_from_rows([
            _row(1, 'EXACT', ('Other',)),
            _row(2, 'WEAKER', ('Station',)),
        ])
        decision = match_logical_channel(
            _hint(raw_tvg_ids=['EXACT']), catalog, preference=_preferred(2)
        )
        self.assertEqual(decision.selected_identity, EpgChannelIdentity(1, 'EXACT'))
        self.assertEqual((decision.match_type, decision.confidence), ('exact_tvg_id', 100))
        self.assertNotIn('preference_applied', decision.reasons)

    def test_same_tier_cross_source_ambiguity_can_be_disambiguated(self):
        rows = [_row(2, 'SAME'), _row(1, 'SAME')]
        hint = _hint(raw_tvg_ids=['SAME'])
        for preferred_source in (1, 2):
            with self.subTest(source=preferred_source):
                decision = match_logical_channel(
                    hint, build_epg_channel_catalog_from_rows(rows),
                    preference=_preferred(preferred_source, 'url_tvg'),
                )
                self.assertEqual(decision.status, 'matched')
                self.assertEqual(decision.selected_identity, EpgChannelIdentity(preferred_source, 'SAME'))
                self.assertEqual((decision.match_type, decision.confidence), ('exact_tvg_id', 100))
                self.assertIn('preference_applied', decision.reasons)

    def test_preferred_source_absent_or_non_unique_keeps_ambiguous(self):
        absent = build_epg_channel_catalog_from_rows([_row(1, 'SAME'), _row(2, 'SAME')])
        decision = match_logical_channel(
            _hint(raw_tvg_ids=['SAME']), absent, preference=_preferred(3)
        )
        self.assertEqual(decision.status, 'ambiguous')

        duplicate = build_epg_channel_catalog_from_rows([
            _row(1, 'A', ('Shared',)), _row(1, 'B', ('Shared',)), _row(2, 'C', ('Shared',)),
        ])
        decision = match_logical_channel(
            _hint(raw_tvg_names=['Shared']), duplicate, preference=_preferred(1)
        )
        self.assertEqual(decision.status, 'ambiguous')

    def test_non_preferred_resolutions_and_unavailable_source_do_not_disambiguate(self):
        catalog = build_epg_channel_catalog_from_rows([
            _row(1, 'SAME', status='failed'), _row(2, 'SAME')
        ])
        for resolution in (
            EpgSourcePreferenceResolution(None, 'none'),
            EpgSourcePreferenceResolution(None, 'conflict'),
            EpgSourcePreferenceResolution(1, 'stale_preference'),
            EpgSourcePreferenceResolution(3, 'missing_source'),
            _preferred(1),
        ):
            with self.subTest(status=resolution.status):
                self.assertEqual(
                    match_logical_channel(
                        _hint(raw_tvg_ids=['SAME']), catalog, preference=resolution
                    ).status,
                    'ambiguous',
                )

    def test_existing_binding_is_preserved_regardless_of_preference(self):
        catalog = build_epg_channel_catalog_from_rows([_row(1, 'OLD'), _row(2, 'NEW')])
        binding = ExistingBindingSnapshot(EpgChannelIdentity(1, 'OLD'), False, match_type='manual', confidence=100)
        decision = match_logical_channel(
            _hint(raw_tvg_ids=['NEW']), catalog,
            existing_binding=binding, preference=_preferred(2),
        )
        self.assertEqual(decision.status, 'existing_preserved')
        self.assertEqual(decision.selected_identity, EpgChannelIdentity(1, 'OLD'))

    def test_cross_subscription_aggregation_is_deterministic(self):
        sources = [EpgSourceState(1, True, 'success'), EpgSourceState(2, True, 'success')]
        same = [
            EpgSourcePreference(1, 'url_tvg', subscription_id=20),
            EpgSourcePreference(1, 'url_tvg', subscription_id=10),
        ]
        first = resolve_logical_channel_source_preference(
            logical_channel_id='logical-1', subscription_ids=[20, 10],
            available_sources=sources, preferences=same,
        )
        second = resolve_logical_channel_source_preference(
            logical_channel_id='logical-1', subscription_ids=[10, 20],
            available_sources=reversed(sources), preferences=reversed(same),
        )
        self.assertEqual(first, second)
        self.assertEqual((first.status, first.preferred_source_id), ('preferred', 1))

        conflict = resolve_logical_channel_source_preference(
            logical_channel_id='logical-1', subscription_ids=[10, 20],
            available_sources=sources,
            preferences=[
                EpgSourcePreference(1, 'url_tvg', subscription_id=10),
                EpgSourcePreference(2, 'url_tvg', subscription_id=20),
            ],
        )
        self.assertEqual(conflict.status, 'conflict')


if __name__ == '__main__':
    unittest.main()
