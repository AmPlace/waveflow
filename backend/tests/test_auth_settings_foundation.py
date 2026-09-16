from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from unittest import mock

import httpx
from fastapi.testclient import TestClient


SAFE_REQUEST_HEADERS = {"X-WaveFlow-Request": "1"}


class AuthSettingsFoundationTest(unittest.TestCase):
    def setUp(self):
        self._old_environment = dict(os.environ)
        for key in list(os.environ):
            if key.startswith("WAVEFLOW_"):
                os.environ.pop(key, None)
        self._tmpdir = tempfile.TemporaryDirectory(prefix="waveflow-auth-settings-")
        os.environ["WAVEFLOW_DB_PATH"] = os.path.join(self._tmpdir.name, "waveflow.db")
        os.environ["WAVEFLOW_PROXY_HANDLE_SECRET"] = "auth-settings-test-secret"
        self._clear_modules()

    def tearDown(self):
        self._clear_modules()
        self._tmpdir.cleanup()
        for key in list(os.environ):
            if key.startswith("WAVEFLOW_"):
                os.environ.pop(key, None)
        os.environ.update(self._old_environment)

    @staticmethod
    def _clear_modules():
        prefixes = ("security", "core", "routers")
        for name in list(sys.modules):
            if name == "database" or name == "main" or name.startswith(prefixes):
                del sys.modules[name]

    def _initialize_database(self):
        import database as db

        asyncio.run(db.initialize())
        return db

    def _client(self) -> TestClient:
        self._initialize_database()
        from main import app

        return TestClient(app, raise_server_exceptions=False)

    def _create_admin_session(self, token: str = "admin-session") -> str:
        from security import database as security_db
        from security.sessions import hash_token

        async def create():
            user = await security_db.create_admin_user("admin", "not-a-password-hash")
            await security_db.create_session(user["id"], hash_token(token))

        asyncio.run(create())
        return token

    def test_fresh_setup_status_initialize_and_repeat(self):
        client = self._client()

        before = client.get("/api/setup/status")
        self.assertEqual(before.status_code, 200)
        self.assertFalse(before.json()["initialized"])

        initialized = client.post(
            "/api/setup/initialize",
            json={"username": "admin", "password": "password-123"},
            headers=SAFE_REQUEST_HEADERS,
        )
        self.assertEqual(initialized.status_code, 200)
        self.assertEqual(initialized.json()["user"]["role"], "admin")
        self.assertIn("HttpOnly", initialized.headers["set-cookie"])

        after = client.get("/api/setup/status")
        self.assertTrue(after.json()["initialized"])
        repeated = client.post(
            "/api/setup/initialize",
            json={"username": "other-admin", "password": "password-123"},
            headers=SAFE_REQUEST_HEADERS,
        )
        self.assertEqual(repeated.status_code, 409)

    def test_setup_mutation_requires_request_marker_and_cors_allowlist(self):
        client = self._client()
        payload = {"username": "admin", "password": "password-123"}

        missing_marker = client.post("/api/setup/initialize", json=payload)
        self.assertEqual(missing_marker.status_code, 403)

        denied_preflight = client.options(
            "/api/setup/initialize",
            headers={
                "Origin": "https://untrusted.example",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type,x-waveflow-request",
            },
        )
        self.assertEqual(denied_preflight.status_code, 400)
        self.assertNotIn("access-control-allow-origin", denied_preflight.headers)

        self._clear_modules()
        os.environ["WAVEFLOW_ALLOWED_ORIGINS"] = "https://trusted.example"
        trusted_client = self._client()
        allowed_preflight = trusted_client.options(
            "/api/setup/initialize",
            headers={
                "Origin": "https://trusted.example",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type,x-waveflow-request",
            },
        )
        self.assertEqual(allowed_preflight.status_code, 200)
        self.assertEqual(
            allowed_preflight.headers.get("access-control-allow-origin"),
            "https://trusted.example",
        )
        initialized = trusted_client.post(
            "/api/setup/initialize",
            json=payload,
            headers={
                "Origin": "https://trusted.example",
                "X-WaveFlow-Request": "1",
            },
        )
        self.assertEqual(initialized.status_code, 200)

    def test_concurrent_first_setup_has_one_authority(self):
        self._initialize_database()
        from main import app

        def initialize(username):
            client = TestClient(app, raise_server_exceptions=False)
            response = client.post(
                "/api/setup/initialize",
                json={"username": username, "password": "password-123"},
                headers=SAFE_REQUEST_HEADERS,
            )
            return response.status_code

        with ThreadPoolExecutor(max_workers=2) as executor:
            statuses = list(executor.map(initialize, ("admin-one", "admin-two")))

        self.assertEqual(sorted(statuses), [200, 409])
        import database as db

        conn = db._connect()
        try:
            count = conn.execute("SELECT COUNT(*) FROM users WHERE role='admin'").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(count, 1)

    def test_setup_session_failure_leaves_recoverable_initialized_admin(self):
        client = self._client()
        import routers.setup as setup_router

        async def fail_session(*args, **kwargs):
            raise RuntimeError("simulated session creation failure")

        with mock.patch.object(setup_router, "create_login_session", new=fail_session):
            response = client.post(
                "/api/setup/initialize",
                json={"username": "admin", "password": "password-123"},
                headers=SAFE_REQUEST_HEADERS,
            )
        self.assertEqual(response.status_code, 500)
        self.assertTrue(client.get("/api/setup/status").json()["initialized"])

        login = client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "password-123"},
        )
        self.assertEqual(login.status_code, 200)

    def test_login_logout_expiry_and_malformed_cookie(self):
        client = self._client()
        client.post(
            "/api/setup/initialize",
            json={"username": "admin", "password": "password-123"},
            headers=SAFE_REQUEST_HEADERS,
        )

        self.assertEqual(client.get("/api/auth/me").status_code, 200)
        self.assertEqual(client.post("/api/auth/logout").status_code, 200)
        self.assertEqual(client.post("/api/auth/logout").status_code, 200)
        self.assertEqual(client.get("/api/auth/me").status_code, 401)

        import database as db
        from security import database as security_db
        from security.sessions import hash_token

        async def create_expired():
            user = await security_db.get_user_by_username("admin")
            await security_db.create_session(user["id"], hash_token("expired-token"))

        asyncio.run(create_expired())
        conn = db._connect()
        try:
            conn.execute(
                "UPDATE sessions SET expires_at=? WHERE token_hash=?",
                ((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(), hash_token("expired-token")),
            )
            conn.commit()
        finally:
            conn.close()
        client.cookies.set("waveflow_session", "expired-token")
        self.assertEqual(client.get("/api/auth/me").status_code, 401)
        client.cookies.set("waveflow_session", "malformed")
        self.assertEqual(client.get("/api/auth/me").status_code, 401)

    def test_session_survives_restart_and_logout_revocation_does_not(self):
        client = self._client()
        initialized = client.post(
            "/api/setup/initialize",
            json={"username": "admin", "password": "password-123"},
            headers=SAFE_REQUEST_HEADERS,
        )
        token = initialized.cookies.get("waveflow_session")
        self.assertTrue(token)

        self._clear_modules()
        restarted = self._client()
        restarted.cookies.set("waveflow_session", token)
        self.assertEqual(restarted.get("/api/auth/me").status_code, 200)
        self.assertEqual(restarted.post("/api/auth/logout").status_code, 200)

        self._clear_modules()
        after_logout = self._client()
        after_logout.cookies.set("waveflow_session", token)
        self.assertEqual(after_logout.get("/api/auth/me").status_code, 401)

    def test_session_database_expiry_uses_effective_setting(self):
        os.environ["WAVEFLOW_SESSION_MAX_AGE_DAYS"] = "3"
        client = self._client()
        response = client.post(
            "/api/setup/initialize",
            json={"username": "admin", "password": "password-123"},
            headers=SAFE_REQUEST_HEADERS,
        )
        self.assertEqual(response.status_code, 200)

        import database as db

        conn = db._connect()
        try:
            row = conn.execute("SELECT created_at, expires_at FROM sessions").fetchone()
        finally:
            conn.close()
        created = datetime.fromisoformat(row["created_at"])
        expires = datetime.fromisoformat(row["expires_at"])
        self.assertAlmostEqual((expires - created).total_seconds(), 3 * 86400, delta=3)
        self.assertIn("Max-Age=259200", response.headers["set-cookie"])

    def test_password_boundary_and_login_error_are_sanitized(self):
        from security.passwords import hash_password, verify_password

        password_hash = hash_password("password-123")
        self.assertTrue(password_hash.startswith("$argon2"))
        self.assertTrue(verify_password("password-123", password_hash))
        self.assertFalse(verify_password("wrong-password", password_hash))
        self.assertFalse(verify_password("", password_hash))

        client = self._client()
        client.post(
            "/api/setup/initialize",
            json={"username": "admin", "password": "password-123"},
            headers=SAFE_REQUEST_HEADERS,
        )
        unknown = client.post(
            "/api/auth/login",
            json={"username": "missing", "password": "password-123"},
        )
        wrong = client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "wrong-password"},
        )
        self.assertEqual((unknown.status_code, wrong.status_code), (401, 401))
        self.assertEqual(unknown.json()["detail"], wrong.json()["detail"])
        self.assertNotIn("password_hash", wrong.text)

    def test_anonymous_and_admin_route_boundaries(self):
        os.environ["WAVEFLOW_ANONYMOUS_BROWSE"] = "0"
        os.environ["WAVEFLOW_ANONYMOUS_PLAYBACK"] = "0"
        client = self._client()

        self.assertEqual(client.get("/api/radio/stations").status_code, 401)
        self.assertEqual(client.get("/api/iptv/channels").status_code, 401)
        self.assertEqual(client.get("/api/media/proxy/playlist/missing").status_code, 401)
        self.assertEqual(client.get("/api/admin/settings/security").status_code, 401)

        token = self._create_admin_session()
        client.cookies.set("waveflow_session", token)
        self.assertEqual(client.get("/api/admin/settings/security").status_code, 200)
        self.assertEqual(
            client.put("/api/admin/settings/security", json={}, headers={}).status_code,
            403,
        )
        self.assertEqual(
            client.get("/api/admin/subscriptions/not-an-int", headers={"X-WaveFlow-Request": "1"}).status_code,
            422,
        )

    def test_admin_auth_precedes_payload_validation_and_non_admin_is_rejected(self):
        client = self._client()
        malformed = client.put(
            "/api/admin/settings/security",
            content="{",
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(malformed.status_code, 422)
        self.assertEqual(
            client.put("/api/admin/settings/security", json={}).status_code,
            401,
        )

        import database as db
        from security import database as security_db
        from security.sessions import hash_token

        conn = db._connect()
        try:
            now = datetime.now(timezone.utc).isoformat()
            cursor = conn.execute(
                """
                INSERT INTO users(username, password_hash, role, created_at, updated_at)
                VALUES('viewer', 'hash', 'viewer', ?, ?)
                """,
                (now, now),
            )
            conn.commit()
            viewer_id = cursor.lastrowid
        finally:
            conn.close()
        asyncio.run(security_db.create_session(viewer_id, hash_token("viewer-session")))
        client.cookies.set("waveflow_session", "viewer-session")
        self.assertEqual(client.get("/api/admin/settings/security").status_code, 401)

        admin_token = self._create_admin_session("second-admin-session")
        client.cookies.set("waveflow_session", admin_token)
        missing_header = client.put(
            "/api/admin/settings/security",
            json={},
        )
        self.assertEqual(missing_header.status_code, 403)
        invalid_payload = client.put(
            "/api/admin/settings/security",
            json=[],
            headers={"X-WaveFlow-Request": "1"},
        )
        self.assertEqual(invalid_payload.status_code, 422)

    def test_admin_route_inventory_has_explicit_guard(self):
        self._initialize_database()
        from main import app
        from fastapi.routing import APIRoute

        guarded = []
        for route in app.routes:
            if not isinstance(route, APIRoute) or not route.path.startswith("/api/admin/"):
                continue

            def dependency_names(node):
                if node is None:
                    return []
                names = []
                call = getattr(node, "call", None)
                if call is not None:
                    names.append(getattr(call, "__name__", ""))
                for child in getattr(node, "dependencies", ()):
                    names.extend(dependency_names(child))
                return names

            names = dependency_names(route.dependant)
            guarded.append((route.path, sorted(route.methods), names))
            self.assertIn("require_admin", names, route.path)

        self.assertGreaterEqual(len(guarded), 35)

    def test_admin_handler_error_is_generic(self):
        client = self._client()
        token = self._create_admin_session()
        client.cookies.set("waveflow_session", token)
        import main

        async def fail():
            raise RuntimeError("/private/path and secret-token")

        with mock.patch.object(main.db, "get_subscriptions", new=fail):
            response = client.get("/api/admin/subscriptions")
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.text, "Internal Server Error")
        self.assertNotIn("private/path", response.text)
        self.assertNotIn("secret-token", response.text)

    def test_settings_precedence_and_restart_read(self):
        client = self._client()
        from core.settings_service import get_effective_settings, update_runtime_settings

        async def read():
            return await get_effective_settings()

        self.assertTrue(asyncio.run(read()).anonymous_browse)
        asyncio.run(update_runtime_settings({"anonymous_browse": False}, updated_by=None))
        self.assertFalse(asyncio.run(read()).anonymous_browse)

        self._clear_modules()
        import database as restarted_db
        from core.settings_service import get_effective_settings as restarted_effective

        asyncio.run(restarted_db.initialize())
        self.assertFalse(asyncio.run(restarted_effective()).anonymous_browse)

        os.environ["WAVEFLOW_ANONYMOUS_BROWSE"] = "1"
        from core.config import get_deployment_config

        get_deployment_config.cache_clear()
        self.assertTrue(asyncio.run(restarted_effective()).anonymous_browse)

    def test_settings_rejects_non_integer_values_without_persistence(self):
        self._initialize_database()
        from core.settings_service import SettingsValidationError, update_runtime_settings

        async def update(value):
            return await update_runtime_settings({"session_max_age_days": value}, updated_by=None)

        for value in (1.5, "14", None, True, 0, 366):
            with self.assertRaises(SettingsValidationError):
                asyncio.run(update(value))
        result = asyncio.run(update(14))
        self.assertEqual(result["settings"]["session_max_age_days"], 14)

    def test_settings_rejects_non_string_values_and_reports_hot_rtsp_limit(self):
        self._initialize_database()
        from core.settings_service import (
            SettingsValidationError,
            list_runtime_settings,
            update_runtime_settings,
        )

        for value in (None, 123, True, [], {}):
            with self.assertRaises(SettingsValidationError):
                asyncio.run(update_runtime_settings({"public_base_url": value}, updated_by=None))

        result = asyncio.run(
            update_runtime_settings({"public_base_url": "https://waveflow.example/"}, updated_by=None)
        )
        self.assertEqual(result["settings"]["public_base_url"], "https://waveflow.example")
        schema = asyncio.run(list_runtime_settings())["schema"]
        self.assertNotIn("restart_required", schema["rtsp_max_sessions"])

    def test_settings_read_failure_does_not_reopen_anonymous_access(self):
        self._initialize_database()
        import database as db
        from core.settings_service import get_effective_settings, get_effective_settings_sync

        asyncio.run(db.set_app_settings({"anonymous_browse": False, "anonymous_playback": False}))
        conn = db._connect()
        try:
            conn.execute("DROP TABLE app_settings")
            conn.commit()
        finally:
            conn.close()

        with self.assertRaises(sqlite3.OperationalError):
            asyncio.run(get_effective_settings())
        with self.assertRaises(sqlite3.OperationalError):
            get_effective_settings_sync()

    def test_settings_batch_write_is_atomic_on_database_failure(self):
        self._initialize_database()
        import database as db

        conn = db._connect()
        try:
            conn.execute(
                """
                CREATE TRIGGER fail_settings_insert
                BEFORE INSERT ON app_settings
                WHEN NEW.key='blocked_setting'
                BEGIN SELECT RAISE(ABORT, 'blocked setting'); END;
                """
            )
            conn.commit()
        finally:
            conn.close()

        async def write():
            await db.set_app_settings(
                {"anonymous_browse": False, "blocked_setting": True},
                updated_by=None,
            )

        with self.assertRaises(sqlite3.IntegrityError):
            asyncio.run(write())
        values = asyncio.run(db.get_app_settings())
        self.assertNotIn("anonymous_browse", values)

    def test_desktop_secret_endpoint_is_loopback_bound(self):
        os.environ["WAVEFLOW_MODE"] = "desktop"
        os.environ["WAVEFLOW_DESKTOP_SESSION"] = "desktop-test-secret"
        self._initialize_database()
        from main import app

        async def exercise():
            transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 18765))
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
                wrong = await client.post("/api/auth/desktop", json={"desktop_session": "wrong"})
                accepted = await client.post(
                    "/api/auth/desktop",
                    json={"desktop_session": "desktop-test-secret"},
                )
                me = await client.get("/api/auth/me")
                return wrong, accepted, me

        wrong, accepted, me = asyncio.run(exercise())
        self.assertEqual(wrong.status_code, 401)
        self.assertEqual(accepted.status_code, 200)
        self.assertTrue(accepted.json()["desktop"])
        self.assertEqual(me.status_code, 200)

    def test_runtime_mode_defaults_are_explicit(self):
        from core.config import build_effective_config, get_deployment_config, mode_defaults

        deployment = get_deployment_config()
        for mode in ("desktop", "nas", "public"):
            effective = build_effective_config(
                deployment.__class__(
                    **{**deployment.__dict__, "mode": mode},
                ),
            )
            defaults = mode_defaults(mode)
            self.assertEqual(effective.anonymous_browse, bool(defaults.anonymous_browse))
            self.assertEqual(effective.anonymous_playback, bool(defaults.anonymous_playback))


if __name__ == "__main__":
    unittest.main()
