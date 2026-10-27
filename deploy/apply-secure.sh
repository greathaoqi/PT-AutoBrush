#!/usr/bin/env bash
# Migrate an existing /opt/pt-brush installation. The certificate must exist first.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo 'Run as root' >&2; exit 1; }
SOURCE=$(cd "$(dirname "$0")/.." && pwd)
APP=/opt/pt-brush
test -s /etc/nginx/ssl/pt.example.com/fullchain.pem
test -s /etc/nginx/ssl/pt.example.com/key.pem
"$APP/venv/bin/python" -m py_compile "$SOURCE/server.py" "$SOURCE/metrics.py" "$SOURCE/account_status.py" "$SOURCE/security.py"
id pt-brush >/dev/null 2>&1 || useradd --system --user-group --home-dir "$APP" --shell /sbin/nologin pt-brush
BACKUP="$APP/backups/$(date +%Y%m%d-%H%M%S)-secure"
mkdir -p "$BACKUP"
chmod 700 "$APP/backups" "$BACKUP"
cp -a /etc/systemd/system/pt-brush.service "$BACKUP/pt-brush.service"
cp -a /etc/nginx/conf.d/nginx-pt-brush.conf "$BACKUP/nginx.conf"
systemctl stop pt-brush
for name in server.py metrics.py account_status.py index.html config.json data security.py app.js login.html login.js; do
    if [ -e "$APP/$name" ]; then cp -a "$APP/$name" "$BACKUP/"; fi
done
rollback() {
    for name in server.py metrics.py account_status.py index.html config.json security.py app.js login.html login.js; do
        if [ -e "$BACKUP/$name" ]; then cp -a "$BACKUP/$name" "$APP/"; fi
    done
    cp -a "$BACKUP/pt-brush.service" /etc/systemd/system/pt-brush.service
    cp -a "$BACKUP/nginx.conf" /etc/nginx/conf.d/nginx-pt-brush.conf
    systemctl daemon-reload
    systemctl restart pt-brush || true
    nginx -t && systemctl reload nginx
    echo "Deployment failed. Restored previous version. Backup: $BACKUP" >&2
}
trap rollback ERR
for name in server.py metrics.py account_status.py security.py security_admin.py index.html app.js login.html login.js; do
    install -o root -g root -m 644 "$SOURCE/$name" "$APP/$name"
done
mkdir -p "$APP/deploy" /etc/pt-brush
cp -a "$SOURCE/deploy/." "$APP/deploy/"
chown -R root:root "$APP/deploy"
chmod 755 "$APP" "$APP/deploy"
chown -R pt-brush:pt-brush "$APP/data"
chmod 700 "$APP/data"
find "$APP/data" -maxdepth 1 -type f -exec chmod 600 {} +
chown pt-brush:pt-brush "$APP/config.json"
chmod 600 "$APP/config.json"
chown root:pt-brush /etc/pt-brush
chmod 750 /etc/pt-brush
if [ ! -f /etc/pt-brush/security.json ]; then
    PYTHONPATH="$APP" "$APP/venv/bin/python" - <<'PY'
import json, os, secrets
from pathlib import Path
from security import password_record
password = secrets.token_urlsafe(24)
Path('/etc/pt-brush/security.json').write_text(json.dumps(password_record('admin', password)))
Path('/root/pt-admin-login.json').write_text(json.dumps({'url':'https://pt.example.com','username':'admin','password':password}))
os.chmod('/root/pt-admin-login.json', 0o600)
PY
fi
chown root:pt-brush /etc/pt-brush/security.json
chmod 640 /etc/pt-brush/security.json
install -m 644 "$SOURCE/deploy/nginx-pt-brush.conf" /etc/nginx/conf.d/nginx-pt-brush.conf
install -m 644 "$SOURCE/deploy/pt-brush.service" /etc/systemd/system/pt-brush.service
for name in pt-brush-backup.service pt-brush-backup.timer pt-brush-cert.service pt-brush-cert.timer pt-brush-firewall.service; do
    install -m 644 "$SOURCE/deploy/$name" "/etc/systemd/system/$name"
done
systemd-analyze verify /etc/systemd/system/pt-brush.service
nginx -t
systemctl daemon-reload
systemctl restart pt-brush
ready=0
for attempt in $(seq 1 20); do
    if curl --noproxy '*' -fsS --max-time 3 -o /dev/null http://127.0.0.1:12300/login; then ready=1; break; fi
    sleep 1
done
[ "$ready" = 1 ]
systemctl reload nginx
trap - ERR
systemctl enable pt-brush
# Protect qBittorrent's management port and the old direct console port without interrupting seeding.
"$APP/venv/bin/python" "$APP/deploy/harden-downloader.py"
if command -v iptables >/dev/null; then
    iptables -C INPUT ! -i lo -p tcp -m multiport --dports 12300,12301 -j REJECT --reject-with tcp-reset 2>/dev/null || \
        iptables -I INPUT 1 ! -i lo -p tcp -m multiport --dports 12300,12301 -j REJECT --reject-with tcp-reset
    iptables-save > /etc/sysconfig/iptables
fi
if command -v ip6tables >/dev/null; then
    ip6tables -C INPUT ! -i lo -p tcp -m multiport --dports 12300,12301 -j REJECT --reject-with tcp-reset 2>/dev/null || \
        ip6tables -I INPUT 1 ! -i lo -p tcp -m multiport --dports 12300,12301 -j REJECT --reject-with tcp-reset
    ip6tables-save > /etc/sysconfig/ip6tables
fi
# Private staging copies contain old site credentials.
if [ -d /root/pt ]; then chmod 700 /root/pt; fi
if [ -d /root/pt-backups ]; then chmod 700 /root/pt-backups; fi
trap - ERR
systemctl enable --no-reload pt-brush-backup.timer pt-brush-cert.timer pt-brush-firewall.service
systemctl start --no-block pt-brush-backup.timer pt-brush-cert.timer pt-brush-firewall.service
"$APP/venv/bin/python" "$APP/deploy/backup.py"
echo "DEPLOYED=https://pt.example.com"
echo "BACKUP=$BACKUP"
systemctl show pt-brush -p User -p ActiveState -p WorkingDirectory
systemctl list-timers pt-brush-backup.timer pt-brush-cert.timer --no-pager
