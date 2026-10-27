#!/usr/bin/env bash
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo 'Run as root' >&2; exit 1; }
SOURCE=$(cd "$(dirname "$0")" && pwd)
if ! command -v fail2ban-client >/dev/null; then
    dnf install -y oracle-epel-release-el8
    dnf --enablerepo=ol8_developer_EPEL install -y fail2ban-server fail2ban-systemd
fi
backup="/opt/pt-brush/backups/$(date +%Y%m%d-%H%M%S)-fail2ban"
mkdir -p "$backup" /etc/fail2ban/filter.d /etc/fail2ban/jail.d
chmod 700 "$backup"
for path in filter.d/pt-login.conf jail.d/pt-security.local fail2ban.local; do
    if [ -f "/etc/fail2ban/$path" ]; then
        mkdir -p "$backup/$(dirname "$path")"
        cp -p "/etc/fail2ban/$path" "$backup/$path"
    fi
    install -o root -g root -m 644 "$SOURCE/fail2ban/$path" "/etc/fail2ban/$path"
done
fail2ban-client -t
systemctl enable --no-reload fail2ban
systemctl restart fail2ban
for attempt in $(seq 1 20); do
    if fail2ban-client ping >/dev/null 2>&1; then break; fi
    sleep 1
done
fail2ban-client status pt-login
fail2ban-client status sshd
for jail in pt-login sshd; do
    echo "JAIL=$jail"
    fail2ban-client get "$jail" maxretry
    fail2ban-client get "$jail" findtime
    fail2ban-client get "$jail" bantime
done
