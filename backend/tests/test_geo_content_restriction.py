from __future__ import annotations

import asyncio
import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path


class GeoContentRestrictionRemovalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = os.environ.get("WAVEFLOW_DB_PATH")
        self.old_geo = os.environ.get("GEO_RESTRICT")
        self.old_regions = os.environ.get("GEO_BLOCKED_REGIONS")
        os.environ["WAVEFLOW_DB_PATH"] = str(Path(self.tmp.name) / "waveflow.db")
        # These legacy variables must not affect the product after removal.
        os.environ["GEO_RESTRICT"] = "1"
        os.environ["GEO_BLOCKED_REGIONS"] = "TW,CN,HK,MO"
        for name in ("main", "database"):
            sys.modules.pop(name, None)
        self.db = importlib.import_module("database")
        asyncio.run(self.db.initialize())
        self.main = importlib.import_module("main")

    def tearDown(self):
        for name in ("main", "database"):
            sys.modules.pop(name, None)
        if self.old_db is None:
            os.environ.pop("WAVEFLOW_DB_PATH", None)
        else:
            os.environ["WAVEFLOW_DB_PATH"] = self.old_db
        if self.old_geo is None:
            os.environ.pop("GEO_RESTRICT", None)
        else:
            os.environ["GEO_RESTRICT"] = self.old_geo
        if self.old_regions is None:
            os.environ.pop("GEO_BLOCKED_REGIONS", None)
        else:
            os.environ["GEO_BLOCKED_REGIONS"] = self.old_regions
        self.tmp.cleanup()

    def test_config_has_no_geo_visibility_policy(self):
        config = asyncio.run(self.main.get_config())
        self.assertNotIn("geoRestrict", config)
        self.assertNotIn("blockedRegions", config)

    def test_geo_visibility_caller_is_removed(self):
        self.assertFalse(hasattr(self.main, "_is_geo_blocked"))


if __name__ == "__main__":
    unittest.main()
