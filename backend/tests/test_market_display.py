"""Market V1 package-owned display contract tests."""

import json
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch


class MarketDisplayContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from market import _normalize_display  # noqa: F401
        except Exception as exc:
            raise unittest.SkipTest(f"market module unavailable: {exc}")

    def test_display_fields_are_projected_without_semantic_derivation(self):
        from market import _normalize_display

        display = {
            "subtitle": "Package subtitle",
            "summary": "Package summary",
            "identity": {
                "brand": "waveflow",
                "icon": {"type": "builtin", "name": "waveflow"},
            },
            "badge": {"text": "FX", "tone": "sky"},
        }
        self.assertEqual(_normalize_display(display), display)

    def test_display_tags_are_not_a_second_tag_source(self):
        from market import _normalize_display

        result = _normalize_display({
            "tags": [
                {"label": "zeta", "tone": "violet"},
                {"label": "央视", "tone": "red"},
                {"label": "zeta", "tone": "neutral"},
            ],
        })
        self.assertEqual(result, {})

    def test_invalid_display_members_are_dropped_safely(self):
        from market import _normalize_display

        result = _normalize_display({
            "subtitle": "<script>",
            "identity": {
                "brand": "移动",
                "icon": {"type": "image", "url": "http://insecure.example/icon.png"},
            },
            "badge": {"text": "<bad>", "tone": "rainbow"},
        })
        self.assertEqual(result, {})

    def test_explicit_https_image_icon_is_allowed(self):
        from market import _normalize_display

        result = _normalize_display({
            "identity": {"icon": {"type": "image", "url": "https://cdn.example/icon.png?v=1"}},
        })
        self.assertEqual(result, {
            "identity": {"icon": {"type": "image", "url": "https://cdn.example/icon.png?v=1"}},
        })

    def test_non_object_display_returns_empty_projection(self):
        from market import _normalize_display

        self.assertEqual(_normalize_display(None), {})
        self.assertEqual(_normalize_display("display"), {})
        self.assertEqual(_normalize_display([]), {})


class MarketPackageProjectionTest(unittest.TestCase):
    def test_name_and_display_are_preserved_by_package_normalization(self):
        from market import _normalize_package, _package_card

        package = _normalize_package({
            "id": "custom",
            "name": "Name with Plugin suffix",
            "kind": "playlist",
            "description": "Full description",
            "tags": ["custom"],
            "display": {
                "subtitle": "Explicit subtitle",
                "summary": "Explicit summary",
            },
        })
        self.assertEqual(package["name"], "Name with Plugin suffix")
        self.assertEqual(package["description"], "Full description")
        self.assertEqual(package["tags"], ["custom"])
        self.assertNotIn("tags", package["display"])
        self.assertEqual(_package_card(package)["display"], package["display"])

    def test_card_and_detail_tag_source_is_root_package_tags(self):
        from market import _normalize_package, _package_card

        package = _normalize_package({
            "id": "single-tag-source",
            "name": "Single Tag Source",
            "kind": "playlist",
            "tags": ["first", "second"],
            "display": {"subtitle": "No second tag list"},
        })
        card = _package_card(package)
        self.assertEqual(card["tags"], ["first", "second"])
        self.assertNotIn("tags", card["display"])

    def test_missing_display_uses_empty_projection_only(self):
        from market import _normalize_package

        package = _normalize_package({"id": "neutral", "name": "Neutral", "kind": "playlist"})
        self.assertEqual(package["display"], {})

    def test_market_index_requires_explicit_package_type(self):
        from market import MarketError, _validate_package_minimal

        with self.assertRaises(MarketError):
            _validate_package_minimal({
                "id": "missing-type",
                "name": "Missing type",
                "description": "Description",
                "kind": "playlist",
                "version": "1.0.0",
                "updated_at": "2026-08-23T00:00:00Z",
            }, context="market.json")

    def test_manifest_context_keeps_runtime_boundary_separate(self):
        from market import _validate_package_minimal

        _validate_package_minimal({"id": "manifest", "name": "Manifest", "kind": "playlist"}, context="manifest")

    def test_v1_metadata_projection_keeps_region_language_and_provider_distinct(self):
        from market import _normalize_package, _package_card, _package_search_haystack

        package = _normalize_package({
            "id": "multi-region",
            "name": "Explicit metadata",
            "description": "Description",
            "kind": "playlist",
            "regions": [{"country": "CN", "province": "福建", "city": None}, {"global": True}],
            "operators": ["中国移动"],
            "providers": ["YouTube"],
            "languages": ["zh-CN", "en-US"],
            "categories": ["体育"],
            "tags": ["作者标签", "未知标签"],
            "publisher": {"id": "org.example", "name": "Example"},
            "published_at": "2026-08-23T00:00:00Z",
            "compatibility": {"min_waveflow_version": "0.1.0"},
            "replacement": "new-package",
            "links": {"source": "https://example.com/source"},
            "catalog": {"sort_weight": 12, "featured": True},
        })
        card = _package_card(package)
        self.assertEqual(card["regions"][1], {"global": True})
        self.assertEqual(card["operators"], ["中国移动"])
        self.assertEqual(card["providers"], ["YouTube"])
        self.assertEqual(card["languages"], ["zh-CN", "en-US"])
        self.assertEqual(card["tags"], ["作者标签", "未知标签"])
        self.assertIn("youtube", _package_search_haystack(package))
        self.assertIn("org.example", _package_search_haystack(package))

    def test_v1_rejects_legacy_semantic_fields_and_non_operator_values(self):
        from market import MarketError, _normalize_package

        for field in ("region", "language", "provider"):
            with self.subTest(field=field), self.assertRaises(MarketError):
                _normalize_package({"id": field, "kind": "playlist", field: "legacy"})
        with self.assertRaises(MarketError):
            _normalize_package({"id": "bad-operator", "kind": "playlist", "operators": ["global"]})

    def test_official_distribution_packages_are_valid_v1_packages(self):
        from market import _normalize_package

        package_root = Path(__file__).parents[1] / "official_plugins" / "distribution" / "packages"
        paths = sorted(package_root.glob("*.json"))
        self.assertEqual(len(paths), 10)
        for path in paths:
            with self.subTest(package=path.name):
                raw = json.loads(path.read_text())
                package = _normalize_package(raw)
                self.assertEqual(package["kind"], "plugin_package")
                self.assertEqual(package["package_type"], "plugin_package")
                self.assertIsInstance(package["providers"], list)
                self.assertIsInstance(package["publisher"], dict)


class MarketSourceLoadingTest(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_root_tag_definitions_do_not_empty_market_source(self):
        import market

        payload = {
            "schema_version": 1,
            "tag_definitions": {"央视": {"priority": 100}},
            "tag_definitions_mode": "inherit",
            "packages": [{
                "id": "legacy-content",
                "name": "Legacy Content",
                "description": "Still loadable without legacy tag semantics.",
                "kind": "playlist",
                "package_type": "content_package",
                "version": "1.0.0",
                "updated_at": "2026-08-23T00:00:00Z",
            }],
        }
        source = {
            "id": 1,
            "name": "Legacy source",
            "url": "https://market.example/market.json",
            "source_key": "community",
            "enabled": 1,
            "allow_private": 0,
        }
        with patch.object(
            market,
            "safe_http_fetch",
            new=AsyncMock(return_value=(source["url"], json.dumps(payload), {})),
        ):
            _market, packages = await market._load_source_packages(source)

        self.assertEqual([item["id"] for item in packages], ["community::legacy-content"])
        self.assertEqual(packages[0]["display"], {})

    async def test_invalid_legacy_package_becomes_unsupported_item_without_emptying_source(self):
        import market

        payload = {
            "schema_version": 1,
            "packages": [{
                "id": "legacy-region",
                "name": "Legacy Region",
                "description": "Rejected package",
                "kind": "playlist",
                "package_type": "content_package",
                "version": "1.0.0",
                "updated_at": "2026-08-23T00:00:00Z",
                "region": {"country": "CN"},
            }, {
                "id": "valid",
                "name": "Valid",
                "description": "Valid package",
                "kind": "playlist",
                "package_type": "content_package",
                "version": "1.0.0",
                "updated_at": "2026-08-23T00:00:00Z",
            }],
        }
        source = {
            "id": 1,
            "name": "Community source",
            "url": "https://market.example/market.json",
            "source_key": "community",
            "enabled": 1,
            "allow_private": 0,
        }
        with patch.object(
            market,
            "safe_http_fetch",
            new=AsyncMock(return_value=(source["url"], json.dumps(payload), {})),
        ):
            _market, packages = await market._load_source_packages(source)

        self.assertEqual([item["id"] for item in packages], ["community::legacy-region", "community::valid"])
        self.assertFalse(packages[0]["supported_in_v1"])
        self.assertIn("旧字段", packages[0]["unsupported_reason"])
        self.assertTrue(packages[1]["supported_in_v1"])


if __name__ == "__main__":
    unittest.main()
