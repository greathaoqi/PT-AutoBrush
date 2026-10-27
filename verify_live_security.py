"""Explicitly invoked production smoke test. Uses credentials from a file, never logs them."""
import json
import os
import socket
from pathlib import Path
import requests

credentials = json.loads(Path(os.environ["PT_TEST_CREDENTIALS"]).read_text(encoding="utf-8"))
base = os.environ.get("PT_TEST_BASE_URL", "https://pt.example.com").rstrip("/")
host_ip = os.environ.get("PT_TEST_HOST_IP", "127.0.0.1")
client = requests.Session()
client.trust_env = False


def check(label, method, path, expected, **kwargs):
    response = client.request(method, base + path, timeout=20, allow_redirects=False, **kwargs)
    assert response.status_code == expected, f"{label}: HTTP {response.status_code}, expected {expected}"
    print(f"PASS {label}: HTTP {response.status_code}")
    return response


http_base = base.replace("https://", "http://")
redirect = client.get(http_base + "/", timeout=15, allow_redirects=False)
assert redirect.status_code == 308 and redirect.headers["location"].startswith(base)
print("PASS HTTP redirects to HTTPS")
login_page = check("valid TLS and login page", "GET", "/login", 200)
assert "script-src 'self'" in login_page.headers["Content-Security-Policy"]
assert "Strict-Transport-Security" in login_page.headers
check("private home redirects to login", "GET", "/", 303)
check("configuration requires login", "GET", "/api/config", 401)
check("write API requires login", "POST", "/api/logs/clear", 401)
check("cross-origin login rejected", "POST", "/api/auth/login", 403, json={})
response = check("admin login", "POST", "/api/auth/login", 200,
                 headers={"Origin": base}, json={"username": credentials["username"], "password": credentials["password"]})
for flag in ("Secure", "HttpOnly", "SameSite=strict"):
    assert flag in response.headers["set-cookie"]
session = check("session and CSRF token", "GET", "/api/auth/session", 200).json()
check("authenticated dashboard", "GET", "/", 200)
check("external application script", "GET", "/app.js", 200)
check("downloader status", "GET", "/api/status", 200)
cfg = check("masked configuration", "GET", "/api/config", 200).json()
for section, key in (("downloader", "password"), ("server", "token")):
    assert cfg.get(section, {}).get(key, "") in ("", "__UNCHANGED__")
for site in cfg.get("sites", []):
    for key in ("cookie", "rss_url"):
        assert site.get(key, "") in ("", "__UNCHANGED__")
check("export blocked without login", "GET", "/data/config.db", 404)
check("cross-origin mutation rejected", "POST", "/api/logs/clear", 403,
      headers={"Origin": "https://evil.example", "X-CSRF-Token": session["csrf"]})
check("mutation without CSRF rejected", "POST", "/api/logs/clear", 403, headers={"Origin": base})
cookie = client.cookies.get("__Host-pt_session")
check("logout", "POST", "/api/auth/logout", 200, headers={"Origin": base, "X-CSRF-Token": session["csrf"]})
check("revoked cookie cannot be replayed", "GET", "/api/config", 401, headers={"Cookie": "__Host-pt_session=" + cookie})
for port in (12300, 12301):
    try:
        connection = socket.create_connection((host_ip, port), timeout=5)
    except OSError:
        print(f"PASS direct management port {port} blocked")
    else:
        connection.close()
        raise AssertionError(f"Management port {port} is publicly reachable")
print("ALL LIVE SECURITY CHECKS PASSED")
