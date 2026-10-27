$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$archive = Join-Path $env:TEMP ('pt-release-' + [guid]::NewGuid().ToString('N') + '.tar.gz')
$files = @('server.py', 'metrics.py', 'account_status.py', 'security.py', 'security_admin.py', 'index.html', 'app.js', 'login.html', 'login.js', 'deploy')
& tar --exclude=__pycache__ --exclude='*.pyc' -czf $archive -C $root @files
if ($LASTEXITCODE -ne 0) { throw '打包失败' }
$encoded = [Convert]::ToBase64String([IO.File]::ReadAllBytes($archive))
$script = @'
#!/usr/bin/env bash
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo 'Run as root' >&2; exit 1; }
APP=/opt/pt-brush
[ "$(systemctl show pt-brush -p User --value)" = pt-brush ] || { echo '请先使用 deploy/apply-secure.sh 完成安全部署' >&2; exit 1; }
stage=$(mktemp -d /tmp/pt-update.XXXXXX)
trap 'rm -rf -- "$stage"' EXIT
base64 -d <<'PAYLOAD' | tar -xz -C "$stage"
__PAYLOAD__
PAYLOAD
"$APP/venv/bin/python" -m py_compile "$stage/server.py" "$stage/metrics.py" "$stage/account_status.py" "$stage/security.py"
backup="$APP/backups/$(date +%Y%m%d-%H%M%S)-update"
mkdir -p "$backup"
chmod 700 "$backup"
systemctl stop pt-brush
cp -a "$APP/data" "$APP/config.json" "$backup/"
files='server.py metrics.py account_status.py security.py security_admin.py index.html app.js login.html login.js deploy/backup.py'
for name in $files; do mkdir -p "$backup/$(dirname "$name")"; if [ -f "$APP/$name" ]; then cp -p "$APP/$name" "$backup/$name"; fi; done
rollback() {
    for name in $files; do if [ -f "$backup/$name" ]; then cp -p "$backup/$name" "$APP/$name"; else rm -f -- "$APP/$name"; fi; done
    systemctl restart pt-brush || true
    echo "更新失败，已恢复旧代码。备份: $backup" >&2
}
trap rollback ERR
for name in $files; do install -o root -g root -m 644 "$stage/$name" "$APP/$name"; done
systemctl restart pt-brush
ready=0
for attempt in $(seq 1 20); do
    if curl --noproxy '*' -fsS --max-time 3 -o /dev/null http://127.0.0.1:12300/login; then ready=1; break; fi
    sleep 1
done
[ "$ready" = 1 ]
[ "$(curl --noproxy '*' -sS --max-time 5 -o /dev/null -w '%{http_code}' http://127.0.0.1:12300/api/config)" = 401 ]
trap - ERR
echo "更新成功，配置和认证资料保持原状。备份: $backup"
'@
$script = $script.Replace('__PAYLOAD__', $encoded).Replace("`r`n", "`n")
[IO.File]::WriteAllText((Join-Path $root 'deploy_update.sh'), $script, [Text.UTF8Encoding]::new($false))
Remove-Item -LiteralPath $archive
Write-Output 'deploy_update.sh 已生成（包含完整前端与认证模块，不包含配置和密码）'
