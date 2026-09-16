from __future__ import annotations

import importlib
import os
import sqlite3
import sys
import tempfile
import unittest


class PluginPermissionSchemaMigrationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db_path = os.environ.get("WAVEFLOW_DB_PATH")
        self.db_path = os.path.join(self.tmp.name, "waveflow.db")
        os.environ["WAVEFLOW_DB_PATH"] = self.db_path
        sys.modules.pop("database", None)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE plugin_permission_approvals (
                    publisher_id TEXT NOT NULL,
                    plugin_id TEXT NOT NULL,
                    permission_name TEXT NOT NULL,
                    permission_fingerprint TEXT NOT NULL,
                    approved INTEGER NOT NULL DEFAULT 0,
                    approved_at TEXT DEFAULT '',
                    approved_by TEXT DEFAULT '',
                    revoked_at TEXT DEFAULT '',
                    revoked_by TEXT DEFAULT '',
                    manifest_version TEXT DEFAULT '',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (publisher_id, plugin_id, permission_name)
                )
                """
            )
            conn.execute(
                """
                INSERT INTO plugin_permission_approvals(
                    publisher_id, plugin_id, permission_name, permission_fingerprint,
                    approved, manifest_version, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                ("org.waveflow", "legacy", "network.direct", "old-fingerprint", 1, "1.0.0", "now"),
            )

    async def asyncTearDown(self):
        if self.old_db_path is None:
            os.environ.pop("WAVEFLOW_DB_PATH", None)
        else:
            os.environ["WAVEFLOW_DB_PATH"] = self.old_db_path
        sys.modules.pop("database", None)
        self.tmp.cleanup()

    def _primary_key(self) -> tuple[str, ...]:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute("PRAGMA table_info(plugin_permission_approvals)").fetchall()
        return tuple(row[1] for row in sorted(rows, key=lambda row: row[5]) if row[5])

    async def test_initialize_rebuilds_legacy_key_and_preserves_fingerprint_rows(self):
        db = importlib.import_module("database")
        await db.initialize()

        self.assertEqual(
            self._primary_key(),
            ("publisher_id", "plugin_id", "permission_name", "permission_fingerprint"),
        )
        self.assertEqual(len(await db.list_plugin_permission_approvals()), 1)

        await db.set_plugin_permission_approval(
            "org.waveflow", "legacy", "network.direct", "new-fingerprint",
            approved=True, actor="admin", manifest_version="2.0.0",
        )
        rows = await db.list_plugin_permission_approvals("org.waveflow", "legacy")
        self.assertEqual(
            {row["permission_fingerprint"] for row in rows},
            {"old-fingerprint", "new-fingerprint"},
        )

        await db.set_plugin_permission_approval(
            "org.waveflow", "legacy", "network.direct", "new-fingerprint",
            approved=False, actor="admin", manifest_version="2.0.0",
        )
        rows = await db.list_plugin_permission_approvals("org.waveflow", "legacy")
        updated = next(row for row in rows if row["permission_fingerprint"] == "new-fingerprint")
        self.assertEqual(updated["approved"], 0)

        await db.initialize()
        with sqlite3.connect(self.db_path) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE name='plugin_permission_approvals_legacy'"
                ).fetchone()[0],
                0,
            )


if __name__ == "__main__":
    unittest.main()
