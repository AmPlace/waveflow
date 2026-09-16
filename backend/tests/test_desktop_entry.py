from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path

import httpx
from fastapi.testclient import TestClient


class DesktopEntryTest(unittest.TestCase):
    def setUp(self):
        self._old_environment = dict(os.environ)
        self._clear_modules()

    def tearDown(self):
        self._clear_modules()
        for key in list(os.environ):
            if key.startswith("WAVEFLOW_") or key == "RTSP_HLS_ROOT":
                os.environ.pop(key, None)
        os.environ.update(self._old_environment)

    @staticmethod
    def _clear_modules():
        for name in list(sys.modules):
            if name == "main" or name == "database" or name.startswith(("core", "security", "routers")):
                del sys.modules[name]

    def test_desktop_entry_owns_mode_origins_cookie_and_user_data(self):
        from desktop_entry import configure_desktop_environment

        with tempfile.TemporaryDirectory(prefix="waveflow-desktop-entry-") as temp:
            configure_desktop_environment(temp)

            self.assertEqual(os.environ["WAVEFLOW_MODE"], "desktop")
            self.assertEqual(os.environ["WAVEFLOW_ALLOWED_ORIGINS"], "null")
            self.assertEqual(os.environ["WAVEFLOW_SESSION_COOKIE_SECURE"], "0")
            self.assertEqual(os.environ["WAVEFLOW_DB_PATH"], str(Path(temp) / "waveflow.db"))
            self.assertEqual(os.environ["RTSP_HLS_ROOT"], str(Path(temp) / "rtsp_hls"))

    def test_desktop_renderer_origin_can_use_cookie_credentials(self):
        with tempfile.TemporaryDirectory(prefix="waveflow-desktop-cors-") as temp:
            os.environ["WAVEFLOW_DB_PATH"] = str(Path(temp) / "waveflow.db")
            os.environ["WAVEFLOW_MODE"] = "desktop"
            os.environ["WAVEFLOW_ALLOWED_ORIGINS"] = "null"
            os.environ["WAVEFLOW_PROXY_HANDLE_SECRET"] = "desktop-cors-test-secret"

            import database

            asyncio.run(database.initialize())
            from main import app

            client = TestClient(app)
            response = client.options(
                "/api/auth/desktop",
                headers={
                    "Origin": "null",
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "content-type",
                },
            )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["access-control-allow-origin"], "null")
            self.assertEqual(response.headers["access-control-allow-credentials"], "true")

    def test_desktop_bootstrap_secret_is_consumed_once(self):
        with tempfile.TemporaryDirectory(prefix="waveflow-desktop-replay-") as temp:
            os.environ["WAVEFLOW_DB_PATH"] = str(Path(temp) / "waveflow.db")
            os.environ["WAVEFLOW_MODE"] = "desktop"
            os.environ["WAVEFLOW_DESKTOP_SESSION"] = "desktop-replay-test-secret"
            os.environ["WAVEFLOW_PROXY_HANDLE_SECRET"] = "desktop-replay-proxy-secret"

            import database

            asyncio.run(database.initialize())
            from main import app

            async def exercise():
                transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 18765))
                async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
                    first = await client.post(
                        "/api/auth/desktop",
                        json={"desktop_session": "desktop-replay-test-secret"},
                    )
                    replay = await client.post(
                        "/api/auth/desktop",
                        json={"desktop_session": "desktop-replay-test-secret"},
                    )
                    return first, replay

            first, replay = asyncio.run(exercise())

            self.assertEqual(first.status_code, 200)
            self.assertEqual(replay.status_code, 401)


if __name__ == "__main__":
    unittest.main()
