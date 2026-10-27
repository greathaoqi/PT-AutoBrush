"""Bind qBittorrent's management API to localhost without restarting torrent sessions."""
import json
from pathlib import Path
import requests

cfg = json.loads(Path("/opt/pt-brush/config.json").read_text())
base = "http://127.0.0.1:12301"
session = requests.Session()
session.trust_env = False
session.headers.update({"Referer": base + "/", "Origin": base})
preferences = session.get(base + "/api/v2/app/preferences", timeout=10)
if preferences.status_code == 403:
    auth = cfg.get("downloader", {})
    session.post(base + "/api/v2/auth/login", data={"username": auth.get("username", "admin"), "password": auth.get("password", "")}, timeout=10).raise_for_status()
    preferences = session.get(base + "/api/v2/app/preferences", timeout=10)
preferences.raise_for_status()
old_address = preferences.json()["web_ui_address"]
Path("/opt/pt-brush/backups/qb-webui-address-before.txt").write_text(str(old_address))
session.post(base + "/api/v2/app/setPreferences", data={"json": json.dumps({"web_ui_address": "127.0.0.1"})}, timeout=10).raise_for_status()
updated = session.get(base + "/api/v2/app/preferences", timeout=10)
updated.raise_for_status()
assert updated.json()["web_ui_address"] == "127.0.0.1", "qBittorrent address was not updated"
print("qBittorrent WebUI now binds to 127.0.0.1; torrent sessions preserved")
