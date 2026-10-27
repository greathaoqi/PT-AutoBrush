#!/usr/bin/env python3
"""Root-only online SQLite backups; retain the latest 14 archives."""
import os
import shutil
import sqlite3
import tarfile
import tempfile
from datetime import datetime
from pathlib import Path

root = Path("/opt/pt-brush")
destination = Path("/var/backups/pt-brush")
destination.mkdir(parents=True, exist_ok=True)
os.chmod(destination, 0o700)
with tempfile.TemporaryDirectory(prefix=".snapshot-", dir=destination) as temp:
    stage = Path(temp)
    (stage / "data").mkdir()
    for database in (root / "data").glob("*.db"):
        source = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        target = sqlite3.connect(stage / "data" / database.name)
        try:
            source.backup(target)
        finally:
            source.close()
            target.close()
    for name in ("server.py", "metrics.py", "account_status.py", "security.py", "security_admin.py", "index.html", "app.js", "login.html", "login.js", "config.json"):
        shutil.copy2(root / name, stage / name)
    shutil.copy2("/etc/pt-brush/security.json", stage / "security-credentials.json")
    shutil.copy2("/etc/systemd/system/pt-brush.service", stage / "pt-brush.service")
    shutil.copy2("/etc/nginx/conf.d/nginx-pt-brush.conf", stage / "nginx-pt-brush.conf")
    for relative in ("fail2ban.local", "filter.d/pt-login.conf", "jail.d/pt-security.local"):
        source = Path("/etc/fail2ban") / relative
        if source.exists():
            target = stage / "fail2ban" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    archive = destination / ("pt-backup-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".tar.gz")
    with tarfile.open(archive, "w:gz") as output:
        output.add(stage, arcname="pt-brush")
    os.chmod(archive, 0o600)
    with tarfile.open(archive, "r:gz") as check:
        for member in check.getmembers():
            if member.isfile():
                with check.extractfile(member) as saved:
                    if len(saved.read()) != member.size:
                        raise RuntimeError("Backup archive validation failed")
for old in sorted(destination.glob("pt-backup-*.tar.gz"), key=lambda p: p.stat().st_mtime, reverse=True)[14:]:
    if old.is_file() and not old.is_symlink():
        old.unlink()
print(f"Backup verified archive written: {archive}")
