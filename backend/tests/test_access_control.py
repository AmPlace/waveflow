import asyncio
import os
import sys
import tempfile
import unittest

from fastapi.testclient import TestClient


class AccessControlTest(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        os.environ["WAVEFLOW_DB_PATH"] = os.path.join(self._tmpdir.name, "t.db")
        os.environ["WAVEFLOW_ANONYMOUS_BROWSE"] = "0"
        os.environ["WAVEFLOW_ANONYMOUS_PLAYBACK"] = "0"
        os.environ.setdefault("WAVEFLOW_PROXY_HANDLE_SECRET", "test-access-32bytes-key-here!!!")
        self._clear_modules()

    def tearDown(self):
        for key in list(os.environ):
            if key.startswith("WAVEFLOW_"):
                os.environ.pop(key, None)
        self._clear_modules()
        self._tmpdir.cleanup()

    def _clear_modules(self):
        for mod in list(sys.modules):
            if (
                mod == "main"
                or mod == "database"
                or mod == "routers"
                or mod.startswith("routers.")
                or mod == "security"
                or mod.startswith("security.")
                or mod.startswith("core")
            ):
                del sys.modules[mod]

    def _client(self) -> TestClient:
        import database as db

        asyncio.run(db.initialize())
        from main import app

        return TestClient(app)

    def _create_admin_session(self) -> str:
        from security import database as security_db
        from security.sessions import hash_token

        async def go():
            user = await security_db.create_admin_user("admin", "hash")
            token = "session-token"
            await security_db.create_session(user["id"], hash_token(token))
            return token

        return asyncio.run(go())

    def test_browse_and_playback_require_auth_when_anonymous_disabled(self):
        client = self._client()

        self.assertEqual(client.get("/api/radio/stations").status_code, 401)
        self.assertEqual(client.get("/api/iptv/channels").status_code, 401)
        self.assertEqual(client.get("/api/yunting/all").status_code, 404)
        self.assertEqual(
            client.get("/api/media/proxy/playlist/test-handle").status_code,
            401,
        )
        self.assertEqual(client.get("/api/iptv/subscription.m3u").status_code, 401)
        self.assertEqual(
            client.get("/api/iptv/smart/test-canonical-key.m3u8").status_code,
            401,
        )

    def test_admin_session_allows_browse_endpoint_when_anonymous_disabled(self):
        client = self._client()
        token = self._create_admin_session()
        client.cookies.set("waveflow_session", token)

        response = client.get("/api/iptv/channels")

        self.assertEqual(response.status_code, 200)
        self.assertIn("channels", response.json())


if __name__ == "__main__":
    unittest.main()
