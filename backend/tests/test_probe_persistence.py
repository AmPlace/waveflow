import os
import tempfile
import unittest


class ProbePersistenceRevisionTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import database

        self.database = database
        self.previous_db = os.environ.get("WAVEFLOW_DB_PATH")
        self.tmpdir = tempfile.TemporaryDirectory(prefix="waveflow-probe-persistence-")
        os.environ["WAVEFLOW_DB_PATH"] = os.path.join(self.tmpdir.name, "waveflow.db")
        await self.database.initialize()

    async def asyncTearDown(self):
        if self.previous_db is None:
            os.environ.pop("WAVEFLOW_DB_PATH", None)
        else:
            os.environ["WAVEFLOW_DB_PATH"] = self.previous_db
        self.tmpdir.cleanup()

    async def test_late_probe_result_cannot_update_reused_channel_row(self):
        from security.source_ids import source_revision_for

        sub_id = await self.database.add_subscription(
            "probe", "https://example.test/list.m3u", custom_ua="Subscription-UA"
        )
        await self.database.add_channels_bulk(sub_id, [{
            "name": "Channel",
            "url": "https://old.example/live.m3u8",
            "source_type": "hls",
        }])
        old_row = (await self.database.get_channels(sub_id))[0]
        old_revision = source_revision_for(old_row)

        generation = await self.database.begin_subscription_refresh(sub_id)
        await self.database.replace_subscription_channels_atomic(
            sub_id,
            [{
                "name": "Channel",
                "url": "https://new.example/live.m3u8",
                "source_type": "hls",
            }],
            expected_generation=generation,
        )
        current_row = (await self.database.get_channels(sub_id))[0]
        self.assertEqual(current_row["id"], old_row["id"])
        self.assertNotEqual(source_revision_for(current_row), old_revision)

        accepted = await self.database.update_channel_probe_result(
            current_row["id"],
            {"probe_status": "online", "live_status": "live", "last_error": "old result"},
            expected_source_revision=old_revision,
        )

        self.assertFalse(accepted)
        after = (await self.database.get_channels(sub_id))[0]
        self.assertEqual(after["url"], "https://new.example/live.m3u8")
        self.assertEqual(after["probe_status"], "untested")
        self.assertEqual(after["last_error"], "")

        accepted = await self.database.update_channel_probe_result(
            after["id"],
            {"probe_status": "online", "live_status": "live", "last_error": ""},
            expected_source_revision=source_revision_for(after),
        )
        self.assertTrue(accepted)
        self.assertEqual((await self.database.get_channels(sub_id))[0]["probe_status"], "online")

    async def test_probe_result_does_not_resurrect_deleted_source(self):
        from security.source_ids import source_revision_for

        sub_id = await self.database.add_subscription(
            "probe", "https://delete.example/list.m3u"
        )
        await self.database.add_channels_bulk(sub_id, [{
            "name": "Deleted",
            "url": "https://delete.example/live.m3u8",
            "source_type": "hls",
        }])
        row = (await self.database.get_channels(sub_id))[0]
        revision = source_revision_for(row)
        await self.database.delete_subscription(sub_id)

        accepted = await self.database.update_channel_probe_result(
            row["id"],
            {"probe_status": "online", "live_status": "live"},
            expected_source_revision=revision,
        )

        self.assertFalse(accepted)
        self.assertEqual(await self.database.get_channels(sub_id), [])


if __name__ == "__main__":
    unittest.main()
