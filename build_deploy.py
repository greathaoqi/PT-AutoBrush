import os
import sys
import tarfile
import base64
import io
from pathlib import Path
import requests

ROOT = Path(__file__).resolve().parent

# Files to package
FILES_TO_PACK = [
    "server.py",
    "metrics.py",
    "account_status.py",
    "security.py",
    "security_admin.py",
    "index.html",
    "app.js",
    "login.html",
    "login.js"
]

def create_tar_gz_b64() -> str:
    tar_buf = io.BytesIO()
    with tarfile.open(fileobj=tar_buf, mode="w:gz") as tar:
        for fname in FILES_TO_PACK:
            fpath = ROOT / fname
            if fpath.exists():
                tar.add(fpath, arcname=fname)
        deploy_dir = ROOT / "deploy"
        if deploy_dir.exists():
            for item in deploy_dir.rglob("*"):
                if item.is_file() and "__pycache__" not in item.parts and not item.name.endswith(".pyc"):
                    rel = item.relative_to(ROOT)
                    tar.add(item, arcname=str(rel).replace("\\", "/"))
    tar_bytes = tar_buf.getvalue()
    return base64.b64encode(tar_bytes).decode("ascii")

def generate_deploy_script() -> str:
    payload_b64 = create_tar_gz_b64()
    script = f'''#!/usr/bin/env bash
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: Please run as root." >&2
    exit 1
fi

APP="/opt/pt-brush"
if [ ! -d "$APP" ]; then
    echo "ERROR: 目录 $APP 不存在，请确认部署路径。" >&2
    exit 1
fi

echo "[*] 开始 PT-AutoBrush 增量无缝更新..."

stage=$(mktemp -d /tmp/pt-update.XXXXXX)
cleanup() {{
    rm -rf -- "$stage"
}}
trap cleanup EXIT

echo "[*] 解压更新载荷..."
base64 -d <<'EOF_PAYLOAD' | tar -xz -C "$stage"
{payload_b64}
EOF_PAYLOAD

if [ -x "$APP/venv/bin/python" ]; then
    PY_BIN="$APP/venv/bin/python"
elif [ -x "$APP/.venv/bin/python" ]; then
    PY_BIN="$APP/.venv/bin/python"
else
    PY_BIN="python3"
fi
"$PY_BIN" -m py_compile "$stage/server.py" "$stage/metrics.py" "$stage/account_status.py" "$stage/security.py"

backup="$APP/backups/$(date +%Y%m%d-%H%M%S)-update"
mkdir -p "$backup"
chmod 700 "$backup"

echo "[*] 正在备份现有数据与配置至: $backup"
if [ -d "$APP/data" ]; then
    cp -a "$APP/data" "$backup/"
fi
if [ -f "$APP/config.json" ]; then
    cp -p "$APP/config.json" "$backup/"
fi

echo "[*] 正在暂停 pt-brush 服务..."
systemctl stop pt-brush || true

files="server.py metrics.py account_status.py security.py security_admin.py index.html app.js login.html login.js"
for name in $files; do
    if [ -f "$APP/$name" ]; then
        cp -p "$APP/$name" "$backup/$name"
    fi
done

rollback() {{
    echo "[-] 服务启动遇到异常，输出最近 25 行 journalctl 日志以供排查:" >&2
    journalctl -u pt-brush -n 25 --no-pager >&2 || true
    echo "[-] 正在执行自动回滚..." >&2
    for name in $files; do
        if [ -f "$backup/$name" ]; then
            cp -p "$backup/$name" "$APP/$name"
        fi
    done
    RUN_USER=$(systemctl show pt-brush -p User --value 2>/dev/null || echo "pt-brush")
    if id "$RUN_USER" >/dev/null 2>&1; then
        chown -R "$RUN_USER:$RUN_USER" "$APP/data" 2>/dev/null || true
    fi
    systemctl restart pt-brush || true
    echo "[-] 已回滚至升级前状态。备份目录: $backup" >&2
}}
trap rollback ERR

echo "[*] 正在安装新版代码与前端资源..."
for name in $files; do
    if [ -f "$stage/$name" ]; then
        install -o root -g root -m 644 "$stage/$name" "$APP/$name"
    fi
done

if [ -d "$stage/deploy" ]; then
    mkdir -p "$APP/deploy"
    cp -rf "$stage/deploy/"* "$APP/deploy/" 2>/dev/null || true
fi

RUN_USER=$(systemctl show pt-brush -p User --value 2>/dev/null || echo "")
if [ -z "$RUN_USER" ]; then
    RUN_USER="pt-brush"
fi

if id "$RUN_USER" >/dev/null 2>&1; then
    chown -R "$RUN_USER:$RUN_USER" "$APP/data"
    chmod 700 "$APP/data"
    find "$APP/data" -maxdepth 1 -type f -exec chmod 600 {{}} + 2>/dev/null || true
    if [ -f "$APP/config.json" ]; then
        chown "$RUN_USER:$RUN_USER" "$APP/config.json"
        chmod 600 "$APP/config.json"
    fi
else
    chown -R root:root "$APP/data" || true
fi

echo "[*] 正在重启 pt-brush 服务..."
systemctl restart pt-brush

echo "[*] 正在进行服务就绪巡检..."
ready=0
for attempt in $(seq 1 30); do
    code=$(curl --noproxy '*' -s --max-time 3 -o /dev/null -w "%{{http_code}}" http://127.0.0.1:12300/ 2>/dev/null || echo "000")
    if [ "$code" -ge 200 ] 2>/dev/null && [ "$code" -lt 500 ] 2>/dev/null; then
        ready=1
        break
    fi
    sleep 1
done

if [ "$ready" -ne 1 ]; then
    echo "[-] 服务启动超时 (HTTP 状态码: $code)，触发回滚" >&2
    exit 1
fi

trap - ERR
echo "[+] PT-AutoBrush 更新成功！配置、账号及 SQLite 数据均已完整保留。"
echo "[+] 备份快照路径: $backup"
'''
    # Strictly ensure LF
    script = script.replace("\r\n", "\n")
    return script

def upload_to_dpaste(content: str) -> str:
    # dpaste.org API
    data = {
        "content": content,
        "syntax": "bash",
        "expiry_days": 14,
        "title": "pt-brush-update"
    }
    for attempt in range(1, 4):
        try:
            print(f"[*] 尝试上传至 dpaste.org (第 {attempt} 次)...")
            resp = requests.post("https://dpaste.org/api/", data=data, timeout=60)
            if resp.status_code == 200:
                url = resp.text.strip().strip('"')
                if not url.endswith("/raw"):
                    url = f"{url}/raw"
                return url
        except Exception as e:
            print(f"[-] 第 {attempt} 次上传异常: {e}")
            if attempt == 3:
                raise
    raise RuntimeError("dpaste upload failed after 3 attempts")

def main():
    script = generate_deploy_script()
    deploy_path = ROOT / "deploy_update.sh"
    with open(deploy_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(script)
    print(f"[OK] 生成 deploy_update.sh 成功，大小: {len(script)} 字节")
    
    print("[*] 正在上传至 dpaste.org ...")
    raw_url = upload_to_dpaste(script)
    print(f"[OK] 上传成功！Raw URL: {raw_url}")
    print(f"[*] 一键更新指令: curl -fsSL {raw_url} | bash")

if __name__ == "__main__":
    main()
