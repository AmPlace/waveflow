import copy
import sys
import unittest

sys.path.insert(0, "/Users/dxg114/code/wavebypass/backend")

from epg_matcher import match_logical_channel
from epg_catalog import build_epg_channel_catalog_from_rows
from epg_source_preference import (
    EpgSourcePreference,
    EpgSourceState,
    resolve_epg_source_preference,
    sanitize_evidence,
)


class EpgSourcePreferenceTests(unittest.TestCase):
    def source(self, source_id=1, *, enabled=True, status="success"):
        return EpgSourceState(source_id, enabled=enabled, last_status=status)

    def preference(self, source_id=1, origin="manual", **kwargs):
        return EpgSourcePreference(
            source_id,
            origin,
            evidence=kwargs.pop("evidence", (("proof", "explicit"),)),
            **kwargs,
        )

    def test_no_preference(self):
        result = resolve_epg_source_preference(available_sources=[self.source()])
        self.assertEqual(result.status, "none")
        self.assertIsNone(result.preferred_source_id)

    def test_each_supported_origin(self):
        for origin in ("manual", "subscription", "market", "url_tvg"):
            with self.subTest(origin=origin):
                result = resolve_epg_source_preference(
                    available_sources=[self.source()], preferences=[self.preference(origin=origin)]
                )
                self.assertEqual((result.status, result.origin), ("preferred", origin))

    def test_precedence_is_deterministic(self):
        preferences = [
            self.preference(1, "url_tvg"),
            self.preference(2, "manual"),
            self.preference(3, "subscription"),
        ]
        first = resolve_epg_source_preference(
            available_sources=[self.source(1), self.source(2), self.source(3)], preferences=preferences
        )
        second = resolve_epg_source_preference(
            available_sources=[self.source(3), self.source(1), self.source(2)], preferences=reversed(preferences)
        )
        self.assertEqual(first.as_dict(), second.as_dict())
        self.assertEqual(first.preferred_source_id, 2)

    def test_same_level_same_source_dedupes(self):
        item = self.preference(1, "subscription", evidence=(("proof", "explicit"),))
        second = self.preference(1, "subscription", evidence=(("proof", "derived"),))
        result = resolve_epg_source_preference(
            available_sources=[self.source()], preferences=[item, copy.deepcopy(second)]
        )
        self.assertEqual(result.status, "preferred")
        self.assertEqual(len(result.competing_preferences), 1)
        self.assertEqual(set(result.evidence), {("proof", "derived"), ("proof", "explicit")})

    def test_same_level_different_sources_conflict(self):
        result = resolve_epg_source_preference(
            available_sources=[self.source(1), self.source(2)],
            preferences=[self.preference(1, "subscription"), self.preference(2, "market")],
        )
        self.assertEqual(result.status, "conflict")
        self.assertIsNone(result.preferred_source_id)

    def test_missing_and_unavailable_sources_are_not_selected(self):
        missing = resolve_epg_source_preference(preferences=[self.preference(9)])
        self.assertEqual((missing.status, missing.preferred_source_id), ("missing_source", 9))
        for enabled, status in ((True, "stale"), (True, "failed"), (False, "success"), (False, "disabled")):
            with self.subTest(enabled=enabled, status=status):
                result = resolve_epg_source_preference(
                    available_sources=[self.source(enabled=enabled, status=status)],
                    preferences=[self.preference()],
                )
                self.assertEqual(result.status, "stale_preference")

    def test_context_filters_preferences(self):
        result = resolve_epg_source_preference(
            logical_channel_id="lc-1",
            subscription_id=4,
            available_sources=[self.source(1), self.source(2)],
            preferences=[
                self.preference(1, logical_channel_id="other"),
                self.preference(2, subscription_id=4),
            ],
        )
        self.assertEqual(result.preferred_source_id, 2)

    def test_sanitize_evidence_is_bounded_and_secret_free(self):
        evidence = {"URL": "https://private.test/x", "token": "secret", "match": "exact"}
        sanitized = sanitize_evidence(evidence)
        self.assertEqual(sanitized, (("match", "exact"),))
        self.assertLessEqual(len(sanitize_evidence({str(i): i for i in range(100)})), 16)

    def test_matcher_import_and_result_contract_are_unchanged(self):
        hint = {
            "logical_channel_id": "lc-1", "canonical_key": "cctv1", "display_name": "CCTV1",
            "raw_member_count": 1, "raw_tvg_ids": ["CCTV1"], "raw_tvg_names": ["CCTV1"],
            "raw_display_names": ["CCTV1"], "member_names": ["CCTV1"], "status": "active",
        }
        catalog = build_epg_channel_catalog_from_rows([
            {
                "source_id": 1, "source_name": "Test", "source_enabled": 1,
                "source_status": "success", "channel_id": "CCTV1",
                "display_names": '["CCTV1"]', "normalized_names": '["cctv1"]',
            }
        ])
        result = match_logical_channel(hint, catalog, existing_binding=None)
        self.assertIn(result.status, {"matched", "ambiguous", "unmatched", "conflict"})


if __name__ == "__main__":
    unittest.main()
