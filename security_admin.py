"""Run with sudo: python security_admin.py [--username admin]. Password is prompted, never an argument."""
import argparse
import getpass
import json
import os
from pathlib import Path
from security import password_record

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--username", default="admin")
    parser.add_argument("--file", default="/etc/pt-brush/security.json")
    args = parser.parse_args()
    password = getpass.getpass("新管理员密码（至少 16 位）: ")
    if password != getpass.getpass("再次输入: "):
        raise SystemExit("两次密码不一致")
    target = Path(args.file)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Update the existing inode so its restricted owner/group permissions stay intact.
    target.write_text(json.dumps(password_record(args.username, password)), encoding="utf-8")
    os.chmod(target, 0o640)
    print("密码已更新。重启 pt-brush 后生效，旧会话全部失效。")
