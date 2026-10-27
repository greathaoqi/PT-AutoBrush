"""Authentication boundary tests use temporary databases and no real downloader."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path

import httpx
from fastapi import FastAPI

from security import COOKIE, SessionStore, configure_security, password_record


class SecurityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "data").mkdir()
        (self.root / "login.html").write_text("login", encoding="utf-8")
        (self.root / "login.js").write_text("", encoding="utf-8")
        self.credentials = self.root / "security.json"
        record = password_record("admin", "correct password long enough")
        self.credentials.write_text(json.dumps(record), encoding="utf-8")
        self.app = FastAPI(openapi_url=None)
        configure_security(self.app, self.root, self.credentials, "https://pt.example.com")
        self.executed = 0

        @self.app.get("/")
        async def index():
            return {"private": True}

        @self.app.get("/api/config")
        async def config():
            return {"secret": "must not be public"}

        @self.app.post("/api/change")
        async def change():
            self.executed += 1
            return {"ok": True}

        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="https://pt.example.com")

    async def asyncTearDown(self):
        await self.client.aclose()
        self.temp.cleanup()

    async def login(self):
        response = await self.client.post("/api/auth/login", headers={"Origin": "https://pt.example.com"},
                                          json={"username": "admin", "password": "correct password long enough"})
        self.assertEqual(response.status_code, 200)
        return response

    async def test_private_pages_and_api_require_authentication(self):
        self.assertEqual((await self.client.get("/")).status_code, 303)
        self.assertEqual((await self.client.get("/api/config")).status_code, 401)
        self.assertEqual((await self.client.post("/api/change")).status_code, 401)
        self.assertEqual(self.executed, 0)

    async def test_login_cookie_csrf_and_revocable_logout(self):
        response = await self.login()
        cookie = response.headers["set-cookie"]
        for flag in ("HttpOnly", "Secure", "SameSite=strict", "Max-Age=28800", "Path=/"):
            self.assertIn(flag, cookie)
        self.assertNotIn("Domain=", cookie)
        token = self.client.cookies.get(COOKIE)
        csrf = (await self.client.get("/api/auth/session")).json()["csrf"]
        self.assertEqual((await self.client.get("/api/config")).status_code, 200)
        self.assertEqual((await self.client.post("/api/change", headers={"Origin": "https://evil.example", "X-CSRF-Token": csrf})).status_code, 403)
        self.assertEqual((await self.client.post("/api/change", headers={"Origin": "https://pt.example.com"})).status_code, 403)
        headers = {"Origin": "https://pt.example.com", "X-CSRF-Token": csrf}
        self.assertEqual((await self.client.post("/api/change", headers=headers)).status_code, 200)
        self.assertEqual(self.executed, 1)
        self.assertEqual((await self.client.post("/api/auth/logout", headers=headers)).status_code, 200)
        self.client.cookies.set(COOKIE, token)
        self.assertEqual((await self.client.get("/api/config")).status_code, 401)

    async def test_bad_login_and_cross_origin_login(self):
        self.assertEqual((await self.client.post("/api/auth/login", json={})).status_code, 403)
        response = await self.client.post("/api/auth/login", headers={"Origin": "https://pt.example.com"},
                                          json={"username": "admin", "password": "wrong"})
        self.assertEqual(response.status_code, 401)
        self.assertNotIn("set-cookie", response.headers)

    async def test_sessions_expire_and_password_rotation_invalidates_them(self):
        await self.login()
        store = SessionStore(self.credentials, self.root / "data" / "auth.db")
        with store.connect() as db:
            db.execute("UPDATE sessions SET expires=0")
        self.assertEqual((await self.client.get("/api/config")).status_code, 401)
        token = store.create()
        self.credentials.write_text(json.dumps(password_record("admin", "a new sufficiently long password")), encoding="utf-8")
        rotated = SessionStore(self.credentials, self.root / "data" / "auth.db")
        self.assertIsNone(rotated.get(token))

    async def test_server_rate_limit(self):
        store = SessionStore(self.credentials, self.root / "data" / "auth.db")
        for _ in range(10):
            self.assertTrue(store.allowed_attempt("192.0.2.1"))
        self.assertFalse(store.allowed_attempt("192.0.2.1"))
        self.assertTrue(store.allowed_attempt("192.0.2.2"))


if __name__ == "__main__":
    unittest.main()
