"""Single-admin authentication with revocable sessions and same-origin CSRF checks."""
import asyncio
import hashlib
import hmac
import json
import logging
import os
import secrets
import sqlite3
import threading
import time
from collections import defaultdict, deque
from contextlib import contextmanager
from pathlib import Path

from fastapi import Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

COOKIE = "__Host-pt_session"
SESSION_SECONDS = 8 * 60 * 60
audit = logging.getLogger("pt-security")


def password_record(username, password):
    if len(password) < 16:
        raise ValueError("管理员密码至少需要 16 个字符")
    salt = secrets.token_hex(32)
    iterations = 600000
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), iterations).hex()
    return {"username": username, "salt": salt, "iterations": iterations, "password_hash": digest}


class SessionStore:
    def __init__(self, credentials, database):
        self.credentials = json.loads(Path(credentials).read_text(encoding="utf-8"))
        self.version = hashlib.sha256(json.dumps(self.credentials, sort_keys=True).encode()).hexdigest()
        self.database = str(database)
        self.attempts = defaultdict(deque)
        self.lock = threading.Lock()
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, username TEXT, csrf TEXT, expires REAL, version TEXT)")
            db.execute("DELETE FROM sessions WHERE expires < ? OR version != ?", (time.time(), self.version))
        os.chmod(self.database, 0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.database, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def allowed_attempt(self, ip):
        now = time.time()
        with self.lock:
            # Bound memory even when many distinct addresses send login attempts.
            if len(self.attempts) > 10000:
                self.attempts = defaultdict(deque, {k: v for k, v in self.attempts.items() if v and v[-1] > now - 900})
            attempts = self.attempts[ip]
            while attempts and attempts[0] < now - 900:
                attempts.popleft()
            if len(attempts) >= 10:
                return False
            attempts.append(now)
            return True

    def verify_password(self, username, password):
        cfg = self.credentials
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(cfg["salt"]), cfg["iterations"]).hex()
        return hmac.compare_digest(digest, cfg["password_hash"]) and hmac.compare_digest(username.encode(), cfg["username"].encode())

    def create(self):
        token, csrf = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
        with self.connect() as db:
            db.execute("DELETE FROM sessions WHERE expires < ?", (time.time(),))
            db.execute("INSERT INTO sessions VALUES (?, ?, ?, ?, ?)",
                       (hashlib.sha256(token.encode()).hexdigest(), self.credentials["username"], csrf,
                        time.time() + SESSION_SECONDS, self.version))
        return token

    def get(self, token):
        if not token or len(token) > 256:
            return None
        with self.connect() as db:
            row = db.execute("SELECT username, csrf FROM sessions WHERE token = ? AND expires > ? AND version = ?",
                             (hashlib.sha256(token.encode()).hexdigest(), time.time(), self.version)).fetchone()
        return {"username": row[0], "csrf": row[1]} if row else None

    def revoke(self, token):
        with self.connect() as db:
            db.execute("DELETE FROM sessions WHERE token = ?", (hashlib.sha256(token.encode()).hexdigest(),))


def configure_security(app, app_dir, credentials_file=None, public_origin=None):
    credentials_file = credentials_file or os.environ.get("PT_SECURITY_FILE")
    if not credentials_file:
        return  # Development only; production systemd always supplies this setting.
    origin = (public_origin or os.environ["PT_PUBLIC_ORIGIN"]).rstrip("/")
    if not origin.startswith("https://"):
        raise ValueError("Production requires an HTTPS public origin")
    store = SessionStore(credentials_file, Path(app_dir) / "data" / "auth.db")

    @app.middleware("http")
    async def require_session(request: Request, call_next):
        path = request.url.path
        public = path in {"/login", "/login.js", "/api/auth/login"}
        session = await asyncio.to_thread(store.get, request.cookies.get(COOKIE))
        if not public and not session:
            if path == "/":
                return RedirectResponse("/login", status_code=303)
            return JSONResponse({"detail": "请先登录"}, status_code=401)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            if request.headers.get("origin") != origin:
                return JSONResponse({"detail": "禁止跨站请求"}, status_code=403)
            if path != "/api/auth/login":
                if not session or not hmac.compare_digest(request.headers.get("x-csrf-token", "").encode(), session["csrf"].encode()):
                    return JSONResponse({"detail": "CSRF 校验失败，请刷新页面"}, status_code=403)
        request.state.session = session
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        if session and request.method not in {"GET", "HEAD", "OPTIONS"}:
            audit.info("action user=%s method=%s path=%s result=%s", session["username"], request.method, path, response.status_code)
        return response

    @app.get("/login", include_in_schema=False)
    async def login_page():
        return FileResponse(Path(app_dir) / "login.html", headers={"Cache-Control": "no-store"})

    @app.get("/login.js", include_in_schema=False)
    async def login_script():
        return FileResponse(Path(app_dir) / "login.js", media_type="application/javascript")

    @app.post("/api/auth/login", include_in_schema=False)
    async def login(request: Request):
        ip = request.client.host if request.client else "unknown"
        if not store.allowed_attempt(ip):
            return JSONResponse({"detail": "尝试过多，请 15 分钟后重试"}, status_code=429, headers={"Retry-After": "900"})
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError):
            return JSONResponse({"detail": "无效请求"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"detail": "无效请求"}, status_code=400)
        username, password = body.get("username", ""), body.get("password", "")
        if not isinstance(username, str) or not isinstance(password, str) or len(username) > 128 or len(password) > 1024:
            return JSONResponse({"detail": "账号或密码错误"}, status_code=401)
        if not await asyncio.to_thread(store.verify_password, username, password):
            audit.warning("login_failed ip=%s", ip)
            return JSONResponse({"detail": "账号或密码错误"}, status_code=401)
        token = await asyncio.to_thread(store.create)
        audit.info("login_success ip=%s", ip)
        response = JSONResponse({"ok": True})
        response.set_cookie(COOKIE, token, max_age=SESSION_SECONDS, secure=True, httponly=True, samesite="strict", path="/")
        return response

    @app.get("/api/auth/session", include_in_schema=False)
    async def session_info(request: Request):
        return request.state.session

    @app.post("/api/auth/logout", include_in_schema=False)
    async def logout(request: Request):
        await asyncio.to_thread(store.revoke, request.cookies.get(COOKIE, ""))
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE, secure=True, httponly=True, samesite="strict", path="/")
        return response
