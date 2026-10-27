#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PT-AutoBrush - 轻量级 PT 自动刷流与免费种子嗅探服务
针对 Linux aarch64 (Oracle Linux 8 UEK) 深度优化
包含：NexusPHP 站点 RSS 嗅探、Free/2xFree 双重状态校验、防爆盘多维防护(空间预扣/紧急制动/自适应水位)、
      SQLite 配置持久化与无损迁移、防封限速、qBittorrent 联动及 WebUI
"""

import os
import sys
import re
import json
import time
import shutil
import asyncio
import logging
import sqlite3
import threading
import hashlib
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Any
from pathlib import Path
from urllib.parse import urlparse, parse_qs
import html
import math

try:
    import requests
    from bs4 import BeautifulSoup
    import feedparser
    from fastapi import FastAPI, HTTPException, Request, BackgroundTasks, UploadFile, File, Form
    from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
    from fastapi.staticfiles import StaticFiles
    from starlette.middleware.gzip import GZipMiddleware
    import uvicorn
except ImportError:
    print("[ERROR] 缺少运行依赖，请在虚拟环境中安装: pip install fastapi uvicorn requests feedparser beautifulsoup4 python-multipart")
    sys.exit(1)

async def run_in_thread(func, *args):
    """跨 Python 版本的异步线程池执行器，彻底规避阻塞主事件循环"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, func, *args)

# ==================== 日志配置 ====================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger("pt-brush")

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"
CONFIG_DB_FILE = DATA_DIR / "config.db"
CONFIG_FILE = APP_DIR / "config.json"
DATA_DIR.mkdir(parents=True, exist_ok=True)
from metrics import MetricsStore
from account_status import AccountMonitor
metrics_store = MetricsStore(DATA_DIR / "metrics.db")
account_monitor = AccountMonitor(DATA_DIR / "account_status.json")

# 内存日志队列（保留最新 500 条用于 WebUI 显示）
MAX_LOGS = 500
system_logs: List[Dict[str, str]] = []

def record_log(tag: str, message: str, level: str = "info"):
    message = re.sub(r"(?i)([?&](?:passkey|authkey|token|apikey|api_key|key)=)[^&\s]+", r"\1[REDACTED]", str(message))
    now_str = datetime.now().strftime("%H:%M:%S")
    entry = {"time": now_str, "tag": tag.upper(), "msg": message, "level": level}
    system_logs.append(entry)
    if len(system_logs) > MAX_LOGS:
        system_logs.pop(0)
    if level in ("danger", "error"):
        logger.error(f"[{tag}] {message}")
    elif level == "warning":
        logger.warning(f"[{tag}] {message}")
    else:
        logger.info(f"[{tag}] {message}")

# ==================== 默认配置与 SQLite 持久化管理 ====================
DEFAULT_CONFIG = {
    "server": {
        "host": "0.0.0.0",
        "port": 12300,
        "token": ""
    },
    "downloader": {
        "url": "http://127.0.0.1:12301",
        "username": "admin",
        "password": "",
        "category": "pt-brush",
        "save_path": "/root/Downloads",
        "max_concurrent_downloads": 2,  # 限制同时处于下载状态的种子数，防多个大种写爆盘
        "single_upload_limit_mb": 85,   # 防封核心：单种上传限制 85MB/s
        "reannounce_delay_sec": 60      # 新种入列 60 秒后触发强制汇报
    },
    "rules": {
        "promotion_mode": "2xfree_and_free",  # "only_2xfree" | "2xfree_and_free" | "only_free"
        "min_free_remaining_min": 60,         # Free 剩余不足 60 分钟不下
        "min_size_gb": 2.0,                   # 单种最小 2 GB
        "max_size_gb": 50.0,                  # 单种最大 50 GB
        "max_seeders": 10,                    # 做种人数上限（首发抢传）
        "min_leechers": 2,                    # 下载人数下限
        "max_age_min": 30,                    # 发布时间窗口 30 分钟
        "avoid_hnr": True,                    # 必须跳过 H&R 种子
        "exclude_keywords": ["Complete", "BDMV", "iPad", "3D", "粤语"]
    },
    "cull": {
        "min_disk_free_gb": 15.0,             # 磁盘安全红线：剩余不足 15GB 熔断并清理
        "emergency_disk_free_gb": 5.0,         # 极端紧急制动红线：剩余不足 5GB 紧急暂停下载任务防系统崩溃
        "max_disk_used_percent": 88.0,         # 磁盘使用率红线：超过 88% 禁止推新并触发清理
        "pre_check_torrent_size": True,        # 入库前严格扣除待下空间预审，不足坚决不下
        "emergency_pause_downloads": True,     # 触碰极低水位时自动紧急暂停未完成下载任务
        "auto_resume_downloads": True,         # 空间腾挪恢复到安全水位后自动恢复下载
        "kill_expired_free": True,             # 下载中但 Free 已失效的紧急删除
        "target_ratio": 3.5,                   # 分享率达标退役
        "max_seed_time_hours": 24,             # 最长生存时间 24h
        "delete_files": True,                  # 物理删除源文件
        "hr_min_seed_hours": 20.0,             # H&R 强制保种时长（小时）
        "hr_min_ratio": 1.0,                   # H&R 免考核分享率
        "enable_score_cull": True,             # 启用基于种子价值评分的早筛淘汰机制
        "cull_score_threshold": 35.0,          # 低分淘汰门槛（低于此分且已达标优先退役）
        "score_grace_hours": 1.0               # 新种入库保护缓冲期（小时）
    },
    "sites": [
        {
            "id": "site_example",
            "name": "示例 NexusPHP 站点",
            "type": "nexusphp",
            "base_url": "https://pt.example.com",
            "rss_url": "",
            "cookie": "",
            "interval_sec": 60,
            "enabled": False,
            "avoid_hnr": True,
            "auto_checkin": True
        }
    ]
}

class ConfigManager:
    """基于 SQLite 的企业级配置持久化管理器，自带版本快照与历史自动备份"""
    def __init__(self, db_path: Path, legacy_json_path: Path):
        self.db_path = db_path
        self.legacy_json_path = legacy_json_path
        self._lock = threading.Lock()
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=15)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        return conn

    def _init_db(self):
        with self._lock:
            conn = self._get_connection()
            try:
                c = conn.cursor()
                c.execute("""
                    CREATE TABLE IF NOT EXISTS settings (
                        section TEXT PRIMARY KEY,
                        json_data TEXT NOT NULL,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                c.execute("""
                    CREATE TABLE IF NOT EXISTS sites (
                        id TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        type TEXT NOT NULL DEFAULT 'nexusphp',
                        base_url TEXT DEFAULT '',
                        rss_url TEXT DEFAULT '',
                        cookie TEXT DEFAULT '',
                        interval_sec INTEGER DEFAULT 60,
                        enabled INTEGER DEFAULT 1,
                        avoid_hnr INTEGER DEFAULT 1,
                        extra_json TEXT DEFAULT '{}',
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                c.execute("""
                    CREATE TABLE IF NOT EXISTS config_backups (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        reason TEXT DEFAULT '',
                        config_json TEXT NOT NULL
                    )
                """)
                conn.commit()

                # 检查数据库是否已有配置，若无则平滑迁移或初始化
                c.execute("SELECT COUNT(*) FROM settings")
                if c.fetchone()[0] == 0:
                    imported_cfg = None
                    if self.legacy_json_path.exists():
                        try:
                            with open(self.legacy_json_path, "r", encoding="utf-8") as f:
                                imported_cfg = json.load(f)
                            record_log("CONFIG", "检测到旧版 config.json，正在自动导入迁移至 SQLite 数据库...", "info")
                        except Exception as e:
                            record_log("CONFIG", f"读取旧版 config.json 异常: {e}", "warning")

                    if not imported_cfg:
                        imported_cfg = DEFAULT_CONFIG

                    self._save_internal(conn, imported_cfg, reason="首次启动：配置自动导入与 SQLite 初始化")
            finally:
                conn.close()

    def _save_internal(self, conn: sqlite3.Connection, cfg: dict, reason: str = "配置保存"):
        c = conn.cursor()
        # 1. 保存前自动备份当前配置快照
        c.execute("SELECT COUNT(*) FROM settings")
        if c.fetchone()[0] > 0:
            cur_cfg = self._load_internal(conn)
            c.execute(
                "INSERT INTO config_backups (reason, config_json) VALUES (?, ?)",
                (reason, json.dumps(cur_cfg, ensure_ascii=False))
            )
            # 保留最近 50 条历史备份，避免无限增大
            c.execute("""
                DELETE FROM config_backups WHERE id NOT IN (
                    SELECT id FROM config_backups ORDER BY id DESC LIMIT 50
                )
            """)

        # 2. 写入全局分节配置
        for sec in ["server", "downloader", "rules", "cull"]:
            sec_data = cfg.get(sec, DEFAULT_CONFIG.get(sec, {}))
            c.execute(
                "INSERT OR REPLACE INTO settings (section, json_data, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
                (sec, json.dumps(sec_data, ensure_ascii=False))
            )

        # 3. 写入站点配置
        if "sites" in cfg:
            c.execute("DELETE FROM sites")
            for s in cfg["sites"]:
                sid = s.get("id") or f"site_{int(time.time()*1000)}"
                name = s.get("name", "未命名站点")
                stype = s.get("type", "nexusphp")
                base_url = s.get("base_url", "")
                rss_url = s.get("rss_url", "")
                cookie = s.get("cookie", "")
                interval_sec = int(s.get("interval_sec", 60))
                enabled = 1 if s.get("enabled", True) else 0
                avoid_hnr = 1 if s.get("avoid_hnr", True) else 0

                std_keys = {"id", "name", "type", "base_url", "rss_url", "cookie", "interval_sec", "enabled", "avoid_hnr"}
                extra = {k: v for k, v in s.items() if k not in std_keys}

                c.execute("""
                    INSERT INTO sites (id, name, type, base_url, rss_url, cookie, interval_sec, enabled, avoid_hnr, extra_json, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """, (sid, name, stype, base_url, rss_url, cookie, interval_sec, enabled, avoid_hnr, json.dumps(extra, ensure_ascii=False)))

        conn.commit()

        # 4. 同步更新写出磁盘 config.json 快照（便于运维排查与第三方脚本无侵入读取）
        try:
            full_cfg = self._load_internal(conn)
            with open(self.legacy_json_path, "w", encoding="utf-8") as f:
                json.dump(full_cfg, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.debug(f"同步写入 config.json 快照异常: {e}")

    def _load_internal(self, conn: sqlite3.Connection) -> dict:
        c = conn.cursor()
        c.execute("SELECT section, json_data FROM settings")
        cfg = {}
        for sec, jdata in c.fetchall():
            try:
                cfg[sec] = json.loads(jdata)
            except Exception:
                cfg[sec] = {}

        # 增量补齐分节默认字段（兼容后续新功能更新，绝不冲掉已设置的值）
        for sec, def_sec in DEFAULT_CONFIG.items():
            if sec == "sites":
                continue
            if sec not in cfg or not isinstance(cfg[sec], dict):
                cfg[sec] = dict(def_sec)
            else:
                for k, v in def_sec.items():
                    if k not in cfg[sec]:
                        cfg[sec][k] = v

        # 加载站点
        c.execute("SELECT id, name, type, base_url, rss_url, cookie, interval_sec, enabled, avoid_hnr, extra_json FROM sites")
        sites = []
        for row in c.fetchall():
            s = {
                "id": row[0],
                "name": row[1],
                "type": row[2],
                "base_url": row[3],
                "rss_url": row[4],
                "cookie": row[5],
                "interval_sec": row[6],
                "enabled": bool(row[7]),
                "avoid_hnr": bool(row[8])
            }
            try:
                extra = json.loads(row[9] or "{}")
                s.update(extra)
            except Exception:
                pass
            if "auto_checkin" not in s:
                s["auto_checkin"] = True
            sites.append(s)

        cfg["sites"] = sites if sites else list(DEFAULT_CONFIG.get("sites", []))
        return cfg

    def load_config(self) -> dict:
        with self._lock:
            conn = self._get_connection()
            try:
                return self._load_internal(conn)
            finally:
                conn.close()

    def save_config(self, cfg: dict, reason: str = "更新配置"):
        with self._lock:
            conn = self._get_connection()
            try:
                self._save_internal(conn, cfg, reason=reason)
            finally:
                conn.close()

    def list_backups(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self._lock:
            conn = self._get_connection()
            try:
                c = conn.cursor()
                c.execute("SELECT id, created_at, reason FROM config_backups ORDER BY id DESC LIMIT ?", (limit,))
                return [{"id": r[0], "created_at": r[1], "reason": r[2]} for r in c.fetchall()]
            finally:
                conn.close()

    def restore_backup(self, backup_id: int) -> bool:
        with self._lock:
            conn = self._get_connection()
            try:
                c = conn.cursor()
                c.execute("SELECT config_json FROM config_backups WHERE id = ?", (backup_id,))
                row = c.fetchone()
                if not row:
                    return False
                cfg = json.loads(row[0])
                self._save_internal(conn, cfg, reason=f"从历史版本 #{backup_id} 还原")
                return True
            except Exception as e:
                record_log("CONFIG", f"恢复备份 #{backup_id} 失败: {e}", "danger")
                return False
            finally:
                conn.close()

# 全局配置管理器单例
config_mgr = ConfigManager(CONFIG_DB_FILE, CONFIG_FILE)

def load_config() -> dict:
    return config_mgr.load_config()

def save_config(cfg: dict, reason: str = "Web 界面配置修改"):
    config_mgr.save_config(cfg, reason=reason)

# ==================== 轻量级纯 Python Bencode 种子解析器 ====================
def parse_torrent_metadata(torrent_bytes: bytes) -> Dict[str, Any]:
    """
    零外部依赖安全解析 .torrent 元数据
    提取：种子名称、单文件/多文件总字节大小、GB 容量
    """
    try:
        def decode_value(b: bytes, idx: int):
            if idx >= len(b):
                raise ValueError("Unexpected EOF")
            char = b[idx:idx+1]
            if char == b'i':
                end = b.index(b'e', idx + 1)
                return int(b[idx+1:end]), end + 1
            elif char.isdigit():
                colon = b.index(b':', idx)
                length = int(b[idx:colon])
                start = colon + 1
                end = start + length
                return b[start:end], end
            elif char == b'l':
                items = []
                idx += 1
                while idx < len(b) and b[idx:idx+1] != b'e':
                    val, idx = decode_value(b, idx)
                    items.append(val)
                return items, idx + 1
            elif char == b'd':
                d = {}
                idx += 1
                while idx < len(b) and b[idx:idx+1] != b'e':
                    k, idx = decode_value(b, idx)
                    v, idx = decode_value(b, idx)
                    if isinstance(k, bytes):
                        k = k.decode('utf-8', errors='replace')
                    d[k] = v
                return d, idx + 1
            else:
                raise ValueError(f"Invalid bencode token at {idx}: {char}")

        root, _ = decode_value(torrent_bytes, 0)
        if not isinstance(root, dict):
            return {}
        info = root.get('info', {})
        if not isinstance(info, dict):
            return {}

        name = info.get('name', b'unknown')
        if isinstance(name, bytes):
            name = name.decode('utf-8', errors='replace')

        total_bytes = 0
        if 'length' in info and isinstance(info['length'], int):
            total_bytes = info['length']
        elif 'files' in info and isinstance(info['files'], list):
            for f in info['files']:
                if isinstance(f, dict) and 'length' in f:
                    total_bytes += int(f['length'])

        return {
            "name": name,
            "size_bytes": total_bytes,
            "size_gb": round(total_bytes / (1024**3), 2)
        }
    except Exception as e:
        logger.debug(f"解析 torrent 元数据异常: {e}")
        return {}

# ==================== qBittorrent WebAPI 客户端 ====================
class QBittorrentClient:
    def __init__(self, base_url: str, username: str = "", password: str = ""):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "PT-AutoBrush/1.0"})
        self.logged_in = False

    def login(self) -> bool:
        try:
            r = self.session.post(
                f"{self.base_url}/api/v2/auth/login",
                data={"username": self.username, "password": self.password},
                timeout=5
            )
            if r.status_code == 200 and ("Ok." in r.text or r.text.strip() == ""):
                self.logged_in = True
                return True
        except Exception as e:
            logger.debug(f"qBittorrent 登录错误: {e}")
        return False

    def request(self, method: str, endpoint: str, **kwargs) -> Optional[requests.Response]:
        url = f"{self.base_url}{endpoint}"
        kwargs.setdefault("timeout", 8)
        try:
            r = self.session.request(method, url, **kwargs)
            if r.status_code == 403:
                if self.login():
                    r = self.session.request(method, url, **kwargs)
            return r
        except Exception as e:
            logger.debug(f"qB 请求失败 {endpoint}: {e}")
            return None

    def test_connection(self) -> Dict[str, Any]:
        t0 = time.time()
        r = self.request("GET", "/api/v2/app/version")
        latency = int((time.time() - t0) * 1000)
        if r and r.status_code == 200:
            return {"ok": True, "version": r.text.strip(), "latency_ms": latency}
        return {"ok": False, "error": "无法连接或鉴权失败", "latency_ms": latency}

    def get_transfer_info(self) -> Dict[str, Any]:
        r = self.request("GET", "/api/v2/transfer/info")
        if r and r.status_code == 200:
            return r.json()
        return {"up_info_speed": 0, "dl_info_speed": 0, "up_info_data": 0, "dl_info_data": 0}

    def get_torrents(self, category: Optional[str] = None) -> List[Dict[str, Any]]:
        params = {}
        if category:
            params["category"] = category
        r = self.request("GET", "/api/v2/torrents/info", params=params)
        if r and r.status_code == 200:
            return r.json()
        return []

    def add_torrent(
        self,
        torrent_url: str = "",
        category: str = "pt-brush",
        upload_limit_b: int = 0,
        torrent_bytes: Optional[bytes] = None,
        filename: str = "download.torrent",
        paused: bool = False,
        savepath: str = ""
    ) -> bool:
        data = {
            "category": category,
            "autoTMM": "false",
            "paused": "true" if paused else "false"
        }
        if savepath:
            data["savepath"] = savepath
        if upload_limit_b > 0:
            data["upLimit"] = str(upload_limit_b)

        # 优先使用二进制文件直传（杜绝 qB 缺乏 Cookie 导致 403/302 无法下载种子文件问题）
        if torrent_bytes:
            files = {"torrents": (filename, torrent_bytes, "application/x-bittorrent")}
            r = self.request("POST", "/api/v2/torrents/add", data=data, files=files)
        else:
            data["urls"] = torrent_url
            r = self.request("POST", "/api/v2/torrents/add", data=data)

        if r and r.status_code == 200:
            try:
                rj = r.json()
                if isinstance(rj, dict) and "success_count" in rj:
                    return rj.get("success_count", 0) > 0
            except Exception:
                pass
            if "fail" in (r.text or "").lower():
                return False
            return True
        return False

    def delete_torrent(self, torrent_hash: str, delete_files: bool = True) -> bool:
        r = self.request("POST", "/api/v2/torrents/delete", data={
            "hashes": torrent_hash,
            "deleteFiles": "true" if delete_files else "false"
        })
        return bool(r and r.status_code == 200)

    def pause_torrents(self, hashes: List[str]) -> bool:
        if not hashes:
            return True
        r = self.request("POST", "/api/v2/torrents/pause", data={"hashes": "|".join(hashes)})
        return bool(r and r.status_code == 200)

    def resume_torrents(self, hashes: List[str]) -> bool:
        if not hashes:
            return True
        r = self.request("POST", "/api/v2/torrents/resume", data={"hashes": "|".join(hashes)})
        return bool(r and r.status_code == 200)

    def reannounce(self, torrent_hash: str) -> bool:
        r = self.request("POST", "/api/v2/torrents/reannounce", data={"hashes": torrent_hash})
        return bool(r and r.status_code == 200)

    def set_upload_limit(self, torrent_hash: str, limit_bytes: int) -> bool:
        r = self.request("POST", "/api/v2/torrents/setUploadLimit", data={
            "hashes": torrent_hash,
            "limit": str(limit_bytes)
        })
        return bool(r and r.status_code == 200)

# ==================== NexusPHP 嗅探与双重 Free 校验 ====================
class NexusPHPSniffer:
    def __init__(self, site_cfg: dict, rules: dict):
        self.cfg = site_cfg
        self.rules = rules
        self.session = requests.Session()
        headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:109.0) Gecko/20100101 Firefox/115.0",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        }
        if self.cfg.get("cookie"):
            headers["Cookie"] = self.cfg["cookie"]
        self.session.headers.update(headers)

    def fetch_rss_items(self) -> List[Dict[str, Any]]:
        rss_url = self.cfg.get("rss_url", "").strip()
        if not rss_url:
            return []
        try:
            r = self.session.get(rss_url, timeout=15)
            if r.status_code != 200:
                record_log("SNIFFER", f"[{self.cfg['name']}] RSS HTTP 状态异常: {r.status_code}", "warning")
                return []
            feed = feedparser.parse(r.content)
            items = []
            for entry in feed.entries:
                torrent_url = ""
                if hasattr(entry, "enclosures") and entry.enclosures:
                    torrent_url = entry.enclosures[0].get("href", "") or entry.enclosures[0].get("url", "")
                if not torrent_url and hasattr(entry, "link"):
                    torrent_url = entry.link
                if torrent_url:
                    torrent_url = html.unescape(torrent_url).replace("&amp;", "&")

                item_id = ""
                m = re.search(r"id=(\d+)", torrent_url or entry.get("link", ""))
                if m:
                    item_id = m.group(1)

                items.append({
                    "title": getattr(entry, "title", "Unknown"),
                    "link": getattr(entry, "link", ""),
                    "torrent_url": torrent_url,
                    "id": item_id,
                    "published": getattr(entry, "published", ""),
                    "description": getattr(entry, "description", "")
                })
            return items
        except Exception as e:
            record_log("SNIFFER", f"[{self.cfg['name']}] 解析 RSS 失败: {e}", "warning")
            return []

    def inspect_torrent_page(self, torrent_id: str, title: str) -> Dict[str, Any]:
        """进入 details.php 页面深度解析 Free 标签、倒计时与 H&R 标识"""
        base_url = self.cfg.get("base_url", "").rstrip("/")
        if not base_url or not torrent_id or not self.cfg.get("cookie"):
            return self._fallback_inspect(title)

        detail_url = f"{base_url}/details.php?id={torrent_id}&hit=1"
        try:
            r = self.session.get(detail_url, timeout=12)
            if r.status_code != 200:
                return {"valid": False, "reason": f"详情页状态码 {r.status_code}"}

            html_text = r.text
            if "login.php" in r.url or ("login.php" in html_text and ("take_login.php" in html_text or "class=\"login\"" in html_text)):
                return {"valid": False, "reason": "Cookie 已失效（站点返回登录页面）"}

            soup = BeautifulSoup(html_text, "html.parser")

            is_2xfree = bool(soup.find(class_=re.compile(r"pro_free2up|twoupfree|free2up", re.I))) or bool(soup.find("img", alt=re.compile(r"2x\s*free|2x免费", re.I))) or "2X Free" in html_text or "2X免费" in html_text
            is_free = is_2xfree or bool(soup.find(class_=re.compile(r"pro_free|free\b", re.I))) or bool(soup.find("img", alt=re.compile(r"^free|免费", re.I))) or "免费" in html_text

            promotion = "none"
            if is_2xfree:
                promotion = "2xfree"
            elif is_free:
                promotion = "free"

            has_hnr = bool(soup.find(class_=re.compile(r"hitandrun|hit_and_run|hnr", re.I))) or "hitandrun" in html_text.lower() or "H&R考核" in html_text or "Hit and Run" in html_text

            remaining_minutes = 9999
            time_match = re.search(r"(?:剩余|还有|Left|Remaining)\s*[:：]?\s*(?:(\d+)\s*(?:天|d|days?))?\s*(?:(\d+)\s*(?:小时|时|h|hours?))?\s*(?:(\d+)\s*(?:分|分钟|m|mins?))?", html_text, re.I)
            if time_match and (time_match.group(1) or time_match.group(2) or time_match.group(3)):
                days = int(time_match.group(1) or 0)
                hours = int(time_match.group(2) or 0)
                mins = int(time_match.group(3) or 0)
                calc_mins = days * 1440 + hours * 60 + mins
                if calc_mins > 0:
                    remaining_minutes = calc_mins

            size_gb = 0.0
            size_match = re.search(r"大小[：:\s]*(?:<[^>]+>)*\s*([0-9.]+)\s*(MB|GB|TB)", html_text, re.I)
            if size_match:
                val = float(size_match.group(1))
                unit = size_match.group(2).upper()
                if unit == "MB": size_gb = val / 1024.0
                elif unit == "GB": size_gb = val
                elif unit == "TB": size_gb = val * 1024.0

            seeders = 1
            leechers = 1
            peer_match = re.search(r"(\d+)\s*个做种者.*?(\d+)\s*个同伴", html_text)
            if peer_match:
                seeders = int(peer_match.group(1))
                leechers = int(peer_match.group(2))

            return {
                "valid": True,
                "promotion": promotion,
                "has_hnr": has_hnr,
                "remaining_minutes": remaining_minutes,
                "size_gb": size_gb,
                "seeders": seeders,
                "leechers": leechers
            }
        except Exception as e:
            return {"valid": False, "reason": f"深度页面解析异常: {e}"}

    def _fallback_inspect(self, title: str) -> Dict[str, Any]:
        promotion = "none"
        if re.search(r"\[(2X\s*Free|2X免费|2X)\]", title, re.I):
            promotion = "2xfree"
        elif re.search(r"\[(Free|免费)\]", title, re.I):
            promotion = "free"

        has_hnr = bool(re.search(r"\[H&R\]|Hit&Run", title, re.I))
        return {
            "valid": True,
            "promotion": promotion,
            "has_hnr": has_hnr,
            "remaining_minutes": 1440,
            "size_gb": 15.0,
            "seeders": 1,
            "leechers": 5
        }

    def evaluate_candidate(self, item: dict) -> Dict[str, Any]:
        """对候选种子应用全套规则过滤"""
        title = item.get("title", "")
        for kw in self.rules.get("exclude_keywords", []):
            if kw and kw.lower() in title.lower():
                return {"pass": False, "reason": f"包含排除关键词 [{kw}]"}

        info = self.inspect_torrent_page(item.get("id", ""), title)
        if not info.get("valid"):
            return {"pass": False, "reason": info.get("reason", "校验失败")}

        promo = info.get("promotion", "none")
        mode = self.rules.get("promotion_mode", "2xfree_and_free")
        if mode == "only_2xfree" and promo != "2xfree":
            return {"pass": False, "reason": f"非 2xFree 种子 (当前: {promo})"}
        if mode == "only_free" and promo != "free":
            return {"pass": False, "reason": f"非 Free 种子 (当前: {promo})"}
        if mode == "2xfree_and_free" and promo not in ("2xfree", "free"):
            return {"pass": False, "reason": "非免费种子 (严格拦截普通收费种)"}

        if self.rules.get("avoid_hnr", True) and info.get("has_hnr"):
            return {"pass": False, "reason": "包含 H&R 考核标记，自动规避"}

        min_rem = self.rules.get("min_free_remaining_min", 60)
        rem = info.get("remaining_minutes", 9999)
        if rem < min_rem:
            return {"pass": False, "reason": f"Free 剩余仅 {rem} 分钟，低于安全阈值 {min_rem} 分钟"}

        size_gb = info.get("size_gb", 0)
        min_size = self.rules.get("min_free_gb", self.rules.get("min_size_gb", 2.0))
        max_size = self.rules.get("max_size_gb", 50.0)
        if size_gb > 0:
            if size_gb < min_size:
                return {"pass": False, "reason": f"体积过小 ({size_gb:.1f}GB < {min_size}GB)"}
            if size_gb > max_size:
                return {"pass": False, "reason": f"体积过大 ({size_gb:.1f}GB > {max_size}GB)"}

        max_seeders = self.rules.get("max_seeders", 10)
        if info.get("seeders", 0) > max_seeders:
            return {"pass": False, "reason": f"做种人数超限 ({info['seeders']} > {max_seeders}，已过首发红利)"}

        return {"pass": True, "info": info}

# ==================== 智能种子价值评分与防爆盘安全引擎 ====================
def compute_torrent_score(torrent: dict, hr_config: dict, now: Optional[int] = None) -> Dict[str, Any]:
    """
    计算单个受管种子的综合价值评分 (0 ~ 100 分) 及考核达标状态。
    核心原则：淘汰前提是必须已达标！
    评分维度：
    1. 实时上传速率 (25分)：瞬时冲刺爆发力，速度越高评分越高
    2. 历史上行表现 (25分)：累计做种上传量与平均上传速率，保障稳定贡献功勋种不被误删
    3. 活跃同伴潜力 (20分)：做吸比及下载同伴数量越多，未来收益潜力越大
    4. 空间性价比 (15分)：体积适中占用少、产出分享率高
    5. 存活周期保护 (15分)：新种入库给予新手保护缓冲，老种逐渐衰减
    """
    if now is None:
        now = int(time.time())

    size_b = int(torrent.get("size", 0) or 0)
    size_gb = size_b / (1024 ** 3)
    upspeed_b = int(torrent.get("upspeed", 0) or 0)
    upspeed_mb = upspeed_b / (1024 * 1024)
    ratio = float(torrent.get("ratio", 0.0) or 0.0)
    progress = float(torrent.get("progress", 0.0) or 0.0)
    num_seeds = int(torrent.get("num_seeds", 0) or 0)
    num_leechs = int(torrent.get("num_leechs", 0) or 0)
    added_on = int(torrent.get("added_on", now) or now)
    age_hours = max(0.0, (now - added_on) / 3600.0)
    seeding_sec = int(torrent.get("seeding_time", 0) or 0)
    seeding_hours = max(0.0, seeding_sec / 3600.0)
    time_active_sec = int(torrent.get("time_active", 0) or 0)

    # 历史上行表现数据提取
    uploaded_b = int(torrent.get("uploaded", 0) or 0)
    if uploaded_b <= 0 and ratio > 0 and size_b > 0:
        uploaded_b = int(ratio * size_b)
    uploaded_gb = uploaded_b / (1024 ** 3)
    eff_seed_sec = max(seeding_sec, time_active_sec, 60)
    avg_upspeed_b = uploaded_b / eff_seed_sec if eff_seed_sec > 0 else 0
    avg_upspeed_mb = avg_upspeed_b / (1024 * 1024)

    # 1. 达标考核判定 (淘汰的前提：必须已达标！)
    hr_status = torrent.get("hr_status")
    hr_min_hours = float(hr_config.get("hr_min_seed_hours", hr_config.get("hr_protect_hours", 20.0)))
    hr_min_ratio = float(hr_config.get("hr_min_ratio", 1.0))

    state = str(torrent.get("state", "") or "")
    is_completed = (progress >= 1.0 or state in ("uploading", "seeding", "stalledUP", "forcedUP"))
    is_compliant = False
    compliance_reason = ""

    if not is_completed:
        is_compliant = False
        compliance_reason = f"未完成下载 (进度 {int(progress * 100)}%)"
    elif hr_status == "考察中":
        is_compliant = False
        compliance_reason = "站点 H&R 考察中 (强制保种保护)"
    elif hr_status == "已达标":
        is_compliant = True
        compliance_reason = "站点 H&R 官方确认已达标"
    else:
        # 普通种子或无官方标记：结合保种时长与分享率
        if ratio >= hr_min_ratio:
            is_compliant = True
            compliance_reason = f"分享率已达标 ({ratio:.2f} >= {hr_min_ratio})"
        elif seeding_hours >= hr_min_hours:
            is_compliant = True
            compliance_reason = f"做种满 {seeding_hours:.1f}h 达标 (>= {hr_min_hours}h)"
        else:
            is_compliant = False
            compliance_reason = f"考核保种中 ({seeding_hours:.1f}h/{hr_min_hours}h, 分享率 {ratio:.2f}/{hr_min_ratio})"

    # 2. 维度一：实时上传速率分 (0 ~ 25)
    if upspeed_mb <= 0:
        score_speed = 0.0
    else:
        score_speed = round(25.0 * (1.0 - math.exp(-upspeed_mb / 2.5)), 1)

    # 3. 维度二：历史上行表现分 (0 ~ 25)
    # 3.1 历史累计做种上传量 (0 ~ 13)
    history_vol = 13.0 * (1.0 - math.exp(-uploaded_gb / 8.0))
    # 3.2 历史平均做种上传速率 (0 ~ 12)
    history_avg = 12.0 * (1.0 - math.exp(-avg_upspeed_mb / 1.5))
    score_history = round(min(25.0, history_vol + history_avg), 1)

    # 4. 维度三：活跃同伴与做吸比潜力 (0 ~ 20)
    score_peers = 0.0
    if num_leechs > 0:
        leecher_part = min(12.0, num_leechs * 2.5)
        ratio_lp = num_leechs / max(1, num_seeds)
        if ratio_lp >= 1.0:
            comp_part = 8.0
        elif ratio_lp >= 0.3:
            comp_part = 5.0
        else:
            comp_part = 3.0
        score_peers = round(min(20.0, leecher_part + comp_part), 1)
    else:
        score_peers = 0.0

    # 5. 维度四：空间性价比与分享率产出 (0 ~ 15)
    if size_gb <= 5.0:
        size_part = 6.0
    elif size_gb <= 15.0:
        size_part = 4.0
    elif size_gb <= 30.0:
        size_part = 2.0
    else:
        size_part = 0.0

    if ratio >= 3.0:
        ratio_part = 9.0
    elif ratio >= 2.0:
        ratio_part = 7.0
    elif ratio >= 1.0:
        ratio_part = 5.0
    elif ratio >= 0.5:
        ratio_part = 3.0
    else:
        ratio_part = 1.0
    score_space = round(min(15.0, size_part + ratio_part), 1)

    # 6. 维度五：存活时效与入库新手保护缓冲 (0 ~ 15)
    if age_hours < 1.0:
        score_age = 15.0
    elif age_hours < 4.0:
        score_age = 10.0
    elif age_hours < 12.0:
        score_age = 5.0
    else:
        score_age = 0.0

    total_score = round(score_speed + score_history + score_peers + score_space + score_age, 1)
    total_score = max(0.0, min(100.0, total_score))

    if total_score >= 75:
        level = "hot"
        level_label = "优质热种"
    elif total_score >= 45:
        level = "normal"
        level_label = "稳定做种"
    else:
        level = "low"
        level_label = "低效冷种"

    summary_text = (
        f"综合评分: {total_score:.1f}分 ({level_label})\n"
        f"• 实时上行: {score_speed}/25分\n"
        f"• 历史上行: {score_history}/25分 (累计已传 {uploaded_gb:.2f}GB, 均速 {avg_upspeed_mb:.2f}MB/s)\n"
        f"• 同伴潜力: {score_peers}/20分\n"
        f"• 空间效率: {score_space}/15分\n"
        f"• 时效保护: {score_age}/15分\n"
        f"考核状态: {compliance_reason}"
    )

    return {
        "score": total_score,
        "level": level,
        "level_label": level_label,
        "is_compliant": is_compliant,
        "compliance_reason": compliance_reason,
        "breakdown": {
            "speed": score_speed,
            "history": score_history,
            "uploaded_gb": round(uploaded_gb, 2),
            "avg_upspeed_mb": round(avg_upspeed_mb, 2),
            "peers": score_peers,
            "space": score_space,
            "age": score_age
        },
        "summary": summary_text
    }


class DiskAndCullEngine:
    def __init__(self, qb: QBittorrentClient, config: dict):
        self.qb = qb
        self.config = config
        self.emergency_paused_hashes = set()

    def get_disk_status(self) -> Dict[str, Any]:
        save_path = self.config.get("downloader", {}).get("save_path", "/root/Downloads")
        check_path = save_path if os.path.exists(save_path) else "/"
        cull_cfg = self.config.get("cull", {})
        min_free_limit = float(cull_cfg.get("min_disk_free_gb", 15.0))
        emergency_limit = float(cull_cfg.get("emergency_disk_free_gb", 5.0))
        max_used_pct = float(cull_cfg.get("max_disk_used_percent", 88.0))

        try:
            stat = os.statvfs(check_path)
            total_b = stat.f_blocks * stat.f_frsize
            free_b = stat.f_bavail * stat.f_frsize
            used_b = total_b - free_b

            total_gb = round(total_b / (1024**3), 1)
            free_gb = round(free_b / (1024**3), 1)
            used_gb = round(used_b / (1024**3), 1)
            used_pct = round((used_b / total_b) * 100, 1) if total_b > 0 else 0

            is_emergency = free_gb <= emergency_limit or used_pct >= 95.0
            is_low = free_gb <= min_free_limit or used_pct >= max_used_pct or is_emergency

            return {
                "total_gb": total_gb,
                "free_gb": free_gb,
                "used_gb": used_gb,
                "used_pct": used_pct,
                "path": check_path,
                "is_low": is_low,
                "is_emergency": is_emergency,
                "min_disk_free_gb": min_free_limit,
                "emergency_disk_free_gb": emergency_limit,
                "max_disk_used_percent": max_used_pct
            }
        except Exception as e:
            return {
                "total_gb": 0, "free_gb": 0, "used_gb": 0, "used_pct": 0,
                "path": "/", "is_low": False, "is_emergency": False, "error": str(e)
            }

    def get_effective_free_gb(self, torrents: Optional[List[dict]] = None) -> float:
        """
        计算扣除未完成下载任务待写量后的【真实有效可用空间】
        彻底防止多个正在下载的任务并发写爆硬盘
        """
        disk = self.get_disk_status()
        current_free_gb = disk.get("free_gb", 0.0)
        if torrents is None:
            cat = self.config.get("downloader", {}).get("category", "pt-brush")
            torrents = self.qb.get_torrents(category=cat)

        pending_bytes = 0
        for t in torrents:
            progress = t.get("progress", 0.0)
            state = t.get("state", "")
            if progress < 1.0 and state in ("downloading", "stalledDL", "checkingDL", "metaDL", "forcedDL", "queuedDL"):
                size = t.get("total_size", t.get("size", 0))
                pending_bytes += max(0, int(size * (1.0 - progress)))

        pending_gb = pending_bytes / (1024**3)
        effective = max(0.0, current_free_gb - pending_gb)
        return round(effective, 2)

    def run_cull_cycle(self, force: bool = False) -> Dict[str, Any]:
        """
        多维自动防爆盘与低分早筛淘汰逻辑：
        【核心前置铁律】：淘汰的前提是种子必须【已达标】！未达标或 H&R 考察中种子享有绝对豁免权，严禁删除。
        1. 极端紧急制动：若触发紧急红线（<=5GB 或使用率>=95%），自动暂停所有下载任务阻断写盘
        2. 自动复苏：若空间脱离告警水位，自动恢复先前紧急制动的下载任务
        3. 常态淘汰检查：低分种子早点淘汰、分享率达标退役、超长做种寿命到期
        4. 空间压制淘汰：若空间依然低于红线，在已达标种子中按评分由低到高淘汰腾挪空间
        """
        disk = self.get_disk_status()
        cull_cfg = self.config.get("cull", {})
        cat = self.config.get("downloader", {}).get("category", "pt-brush")
        raw_torrents = self.qb.get_torrents(category=cat)
        deleted_count = 0
        freed_gb = 0.0

        if not raw_torrents:
            return {"deleted": 0, "freed_gb": 0.0, "reason": "无受管种子"}

        # 标记官方 H&R 考察/达标状态
        torrents = hr_monitor.mark_torrents(raw_torrents, self.config)

        now = int(time.time())
        hr_protect_hours = float(cull_cfg.get("hr_min_seed_hours", cull_cfg.get("hr_protect_hours", 20.0)))
        hr_min_ratio = float(cull_cfg.get("hr_min_ratio", 1.0))

        # ===== 【防爆盘防线 1：极端紧急制动刹车】 =====
        if disk["is_emergency"] and cull_cfg.get("emergency_pause_downloads", True):
            dl_tasks = [t for t in torrents if t.get("progress", 0.0) < 1.0 and t.get("state") in ("downloading", "stalledDL", "checkingDL")]
            to_pause = [t.get("hash") for t in dl_tasks if t.get("hash")]
            if to_pause:
                self.qb.pause_torrents(to_pause)
                self.emergency_paused_hashes.update(to_pause)
                record_log(
                    "DISK-EMERGENCY",
                    f"【防爆盘紧急制动】磁盘剩余仅 {disk['free_gb']}GB (使用率 {disk['used_pct']}%)，已紧急暂停 {len(to_pause)} 个下载中任务以防止系统宕机！",
                    "danger"
                )

        # ===== 【防爆盘防线 2：安全水位自动恢复复苏】 =====
        if not disk["is_low"] and cull_cfg.get("auto_resume_downloads", True) and self.emergency_paused_hashes:
            to_resume = list(self.emergency_paused_hashes)
            self.qb.resume_torrents(to_resume)
            self.emergency_paused_hashes.clear()
            record_log("DISK-GUARD", f"磁盘空间已恢复到安全水位 ({disk['free_gb']}GB)，已自动恢复先前制动的 {len(to_resume)} 个下载任务", "success")

        # ===== 【常态删种淘汰评估】 =====
        to_delete = []
        for t in torrents:
            thash = t.get("hash")
            name = t.get("name", "Unknown")
            upspeed = t.get("upspeed", 0)
            ratio = t.get("ratio", 0.0)
            size = t.get("size", 0)
            added_on = t.get("added_on", now)
            duration_hours = (now - added_on) / 3600.0

            # 核心铁律：淘汰前提是必须已达标！未达标种子拥有豁免权，绝不删除
            score_info = compute_torrent_score(t, cull_cfg, now)
            if not score_info["is_compliant"]:
                continue

            # 规则 1: 智能评分淘汰（低分种子早点淘汰，已达标为前提）
            enable_score_cull = cull_cfg.get("enable_score_cull", True)
            cull_threshold = float(cull_cfg.get("cull_score_threshold", 35.0))
            grace_hours = float(cull_cfg.get("score_grace_hours", 1.0))
            score = score_info["score"]

            if enable_score_cull and duration_hours >= grace_hours and score < cull_threshold and upspeed < 1024 * 1024:
                to_delete.append((thash, name, size, f"低分早筛淘汰 (综合评分 {score:.1f}分 < 淘汰线 {cull_threshold}分，{score_info['compliance_reason']})"))
                continue

            # 规则 2: 分享率超额达标退役
            target_ratio = cull_cfg.get("target_ratio", 3.5)
            if ratio >= target_ratio and upspeed < 2 * 1024 * 1024:
                to_delete.append((thash, name, size, f"分享率超额达标 ({ratio:.2f} >= {target_ratio}) 且已达标退役"))
                continue

            # 规则 3: 做种超过最长时间
            max_hours = max(cull_cfg.get("max_seed_time_hours", 24), hr_protect_hours)
            if duration_hours >= max_hours:
                to_delete.append((thash, name, size, f"做种满 {duration_hours:.1f}h 达到最长退役时间 (已达标)"))
                continue

        # 执行预定退役删除
        for thash, name, size, reason in to_delete:
            if self.qb.delete_torrent(thash, delete_files=cull_cfg.get("delete_files", True)):
                deleted_count += 1
                freed_gb += round(size / (1024**3), 2)
                record_log("AUTO-CULL", f"[退役清理] 删除种子 [{name[:35]}] -> 原因: {reason} (释放 {size/(1024**3):.2f}GB)", "info")

        # ===== 【防爆盘防线 3：空间偏低时的压制淘汰】 =====
        disk_now = self.get_disk_status()
        min_disk_free = cull_cfg.get("min_disk_free_gb", 15.0)
        target_safe_gb = min_disk_free + 2.0  # 多腾出 2GB 缓冲区
        max_used_pct = cull_cfg.get("max_disk_used_percent", 88.0)

        if disk_now["is_low"] or force:
            record_log("DISK-GUARD", f"磁盘空间告警 (当前剩余 {disk_now['free_gb']}GB, 使用率 {disk_now['used_pct']}%)，启动空间智能低分压制淘汰！", "warning")
            remaining_raw = [t for t in self.qb.get_torrents(category=cat) if t.get("hash") not in [x[0] for x in to_delete]]
            remaining_marked = hr_monitor.mark_torrents(remaining_raw, self.config)

            # 核心铁律：仅允许【已达标】的种子参与空间腾挪！未达标种子坚决不动
            eligible = []
            for t in remaining_marked:
                s_info = compute_torrent_score(t, cull_cfg, now)
                if s_info["is_compliant"]:
                    eligible.append((t, s_info))

            # 评分由低到高淘汰；相同分数下，体积大者优先淘汰以腾出最大空间
            eligible.sort(key=lambda item: (item[1]["score"], -item[0].get("size", 0)))

            for t, s_info in eligible:
                if disk_now["free_gb"] >= target_safe_gb and disk_now["used_pct"] < max_used_pct:
                    break
                thash = t.get("hash")
                name = t.get("name")
                size = t.get("size", 0)

                if self.qb.delete_torrent(thash, delete_files=cull_cfg.get("delete_files", True)):
                    deleted_count += 1
                    freed_gb += round(size / (1024**3), 2)
                    record_log("AUTO-CULL", f"[空间救急] 淘汰低分已达标种 [{name[:35]}] -> 评分: {s_info['score']:.1f}分 ({s_info['compliance_reason']}, 释放 {size/(1024**3):.2f}GB)", "danger")
                    disk_now = self.get_disk_status()

        return {"deleted": deleted_count, "freed_gb": round(freed_gb, 2)}

HR_REFRESH_SECONDS = 4 * 60 * 60


def parse_hr_page(html_text: str) -> dict:
    """只读解析 HR 表格；登录页及未知页面不能被误判成零条记录。"""
    soup = BeautifulSoup(html_text, "html.parser")
    heading = soup.find(["h1", "h2"], string=re.compile(r"H\s*&\s*R\s*记录"))
    if not heading:
        raise ValueError("无法读取 HR 记录，请检查站点 Cookie 是否有效")
    table = next((t for t in soup.find_all("table")
                  if [c.get_text(strip=True) for c in t.select("td.colhead")][:2]
                  == ["HR编号", "种子名称"]), None)
    # 部分 NexusPHP 在没有记录时不输出表格。
    if table is None:
        content = soup.select_one("#outer") or soup
        if re.search(r"没有.*(?:记录|种子)|暂无.*记录|无.*H\s*&\s*R.*记录", content.get_text(" ", strip=True)):
            return {"records": [], "pages": 1}
        raise ValueError("HR 页面结构异常，未找到记录表格")
    records = []
    for row in table.find_all("tr"):
        cells = row.find_all("td", recursive=False)
        if not cells or "rowfollow" not in cells[0].get("class", []):
            continue
        if len(cells) < 8:
            raise ValueError("HR 记录字段不完整")
        values = [c.get_text(" ", strip=True) for c in cells]
        link = cells[1].find("a", href=re.compile(r"details\.php\?"))
        torrent_id = parse_qs(urlparse(link["href"]).query).get("id", [""])[0] if link else ""
        records.append(dict(zip(
            ["id", "name", "uploaded", "downloaded", "ratio", "seed_remaining", "completed_at", "deadline_remaining"],
            values[:8]), torrent_id=torrent_id if torrent_id.isdigit() else ""))
    page_match = re.search(r"\bvar\s+maxpage\s*=\s*(\d+)", html_text)
    pages = int(page_match.group(1)) + 1 if page_match else 1
    for link in soup.find_all("a", href=True):
        query = parse_qs(urlparse(link["href"]).query)
        page = query.get("page", [""])[0]
        if page.isdigit() and "hrtype" in query:
            pages = max(pages, int(page) + 1)
    if pages > 50:
        raise ValueError("HR 记录超过 50 页，请前往站点查看")
    return {"records": records, "pages": pages}


class HRMonitor:
    def __init__(self, cache_path: Path):
        self.cache_path = cache_path
        self.lock = threading.Lock()
        try:
            self.cache = json.loads(cache_path.read_text(encoding="utf-8"))
            if not isinstance(self.cache, dict):
                self.cache = {}
            self.cache = {k: v for k, v in self.cache.items() if isinstance(v, dict)
                          and isinstance(v.get("last_attempt_at"), (int, float))}
        except (OSError, ValueError):
            self.cache = {}

    @staticmethod
    def sites(config):
        return [s for s in config.get("sites", [])
                if (urlparse(s.get("base_url", "")).hostname or "").lower() == "pt.btschool.club"]

    @staticmethod
    def key(site):
        # 配置/账号变更后立即失效；缓存不保存 Cookie。
        return hashlib.sha256(json.dumps(["hr-hash-v2", site.get("id"), site.get("base_url"), site.get("cookie")]).encode()).hexdigest()

    def snapshot(self, config):
        result = []
        with self.lock:
            for site in self.sites(config):
                entry = dict(self.cache.get(self.key(site), {}))
                entry.update(site_id=site.get("id"), site_name=site.get("name", "BTSCHOOL"),
                             url="https://pt.btschool.club/myhr.php")
                entry["stale"] = bool(entry.get("updated_at") and time.time() - entry["updated_at"] >= HR_REFRESH_SECONDS)
                result.append(entry)
        return {"refresh_interval_seconds": HR_REFRESH_SECONDS, "sites": result}

    @staticmethod
    def normalize_name(name):
        name = re.sub(r"\.torrent$", "", str(name), flags=re.I)
        return re.sub(r"[\W_]+", " ", name.casefold()).strip()

    def mark_torrents(self, torrents, config):
        records = [record for site in self.snapshot(config)["sites"]
                   for record in site.get("records", [])]
        hash_status, name_status = {}, {}
        # 分页抓取期间发生状态迁移时，以“考察中”优先，避免提前显示达标。
        for label in ("已达标", "考察中"):
            for record in records:
                if record.get("status") != label:
                    continue
                if record.get("hash"):
                    hash_status[record["hash"].lower()] = label
                else:
                    name = self.normalize_name(record.get("name", ""))
                    if name:
                        name_status[name] = label
        result = []
        for torrent in torrents:
            tracker = urlparse(torrent.get("tracker") or "").hostname
            same_site = not tracker or tracker == "btschool.club" or tracker.endswith(".btschool.club")
            status = (hash_status.get(str(torrent.get("hash", "")).lower())
                      or name_status.get(self.normalize_name(torrent.get("name", "")))) if same_site else None
            result.append({**torrent, "hr_status": status, "hr_pending": status == "考察中"})
        return result

    def refresh_due(self, config, force: bool = False):
        active_keys = set()
        changed = False
        for site in self.sites(config):
            key = self.key(site)
            active_keys.add(key)
            now = time.time()
            with self.lock:
                previous = dict(self.cache.get(key, {}))
            if not force and previous.get("fetch_version") == 3 and 0 <= now - previous.get("last_attempt_at", 0) < HR_REFRESH_SECONDS:
                continue
            entry = {**previous, "fetch_version": 3, "last_attempt_at": now, "next_refresh_at": now + HR_REFRESH_SECONDS}
            changed = True
            try:
                if not site.get("cookie", "").strip():
                    raise ValueError("请先在 PT 站点配置中填写 Cookie")
                category_records = {}
                with requests.Session() as session:
                    session.headers.update({"Cookie": site["cookie"], "User-Agent": "Mozilla/5.0 PT-AutoBrush"})
                    for hrtype, label in (("A", "考察中"), ("B", "已达标")):
                        page, pages = 0, 1
                        while page < pages:
                            response = session.get("https://pt.btschool.club/myhr.php",
                                                   params={"hrtype": hrtype, "page": page}, timeout=20,
                                                   allow_redirects=False)
                            if response.status_code != 200:
                                raise ValueError(f"HR 页面返回 HTTP {response.status_code}，请检查站点或 Cookie")
                            response.encoding = response.apparent_encoding or "utf-8"
                            parsed = parse_hr_page(response.text)
                            pages = max(pages, parsed["pages"])
                            for record in parsed["records"]:
                                category_records[(hrtype, record["id"])] = {**record, "status": label}
                            page += 1
                    known_hashes = {record.get("torrent_id"): record["hash"]
                                    for record in previous.get("records", []) if record.get("hash")}
                    missing_hashes = 0
                    for record in category_records.values():
                        torrent_id = record.get("torrent_id")
                        if torrent_id in known_hashes:
                            record["hash"] = known_hashes[torrent_id]
                            continue
                        try:
                            if not torrent_id:
                                raise ValueError("缺少种子编号")
                            details = session.get("https://pt.btschool.club/details.php",
                                                  params={"id": torrent_id, "hit": 0}, timeout=20,
                                                  allow_redirects=False)
                            if details.status_code != 200:
                                raise ValueError("详情页不可用")
                            details.encoding = "utf-8"
                            text = BeautifulSoup(details.text, "html.parser").get_text(" ", strip=True)
                            match = re.search(r"Hash\s*(?:码|code)?\s*[:：]\s*([a-fA-F0-9]{40})(?![a-fA-F0-9])", text, re.I)
                            if not match:
                                raise ValueError("未找到 Hash")
                            record["hash"] = match.group(1).lower()
                        except (requests.RequestException, ValueError):
                            missing_hashes += 1
                    if missing_hashes:
                        record_log("HR", f"{missing_hashes} 条 HR 记录暂未取得 Hash，保留名称匹配", "warning")
                entry.pop("failed", None)
                records = list(category_records.values())
                pending = sum(record["status"] == "考察中" for record in records)
                passed = sum(record["status"] == "已达标" for record in records)
                entry.update(updated_at=time.time(), pending=pending, passed=passed, records=records, error=None)
                record_log("HR", f"HR 记录已更新：考察中 {pending} 条、已达标 {passed} 条，下次更新约 4 小时后")
            except ValueError as exc:
                entry["error"] = str(exc)
            except requests.RequestException:
                entry["error"] = "HR 站点连接失败，将在下一刷新周期重试"
            except Exception:
                entry["error"] = "HR 状态更新失败，将在下一刷新周期重试"
            if entry.get("error"):
                record_log("HR", entry["error"], "warning")
            with self.lock:
                self.cache[key] = entry
        with self.lock:
            changed = changed or set(self.cache) != active_keys
            self.cache = {key: value for key, value in self.cache.items() if key in active_keys}
            payload = json.dumps(self.cache, ensure_ascii=False)
        if not changed:
            return
        try:
            temp_path = self.cache_path.with_suffix(".tmp")
            temp_path.write_text(payload, encoding="utf-8")
            os.replace(temp_path, self.cache_path)
        except OSError:
            logger.warning("HR 缓存写入失败，本次结果仍保留在内存中")

    def check_torrent_hr(self, torrent_hash: str, config: dict, qb_client=None, force: bool = True) -> dict:
        if force:
            self.refresh_due(config, force=True)
        records = [record for site in self.snapshot(config)["sites"]
                   for record in site.get("records", [])]
        thash = (torrent_hash or "").strip().lower()
        matched = None
        for record in records:
            if record.get("hash") and record["hash"].lower() == thash:
                matched = record
                break

        torrent_name = ""
        if not matched and qb_client:
            try:
                torrents = qb_client.get_torrents()
                target_t = next((t for t in torrents if (t.get("hash") or "").lower() == thash), None)
                if target_t:
                    torrent_name = target_t.get("name", "")
                    norm_target = self.normalize_name(torrent_name)
                    for record in records:
                        if self.normalize_name(record.get("name", "")) == norm_target:
                            matched = record
                            record["hash"] = thash
                            break
            except Exception as exc:
                logger.debug(f"检查单种达标时获取 qB 任务失败: {exc}")

        if matched:
            status = matched.get("status") or "已达标"
            msg = f"种子达标状态：【{status}】"
            if status == "考察中":
                rem_seed = matched.get("seed_remaining") or ""
                rem_time = matched.get("deadline_remaining") or ""
                extra = []
                if rem_seed:
                    extra.append(f"还需做种 {rem_seed}")
                if rem_time:
                    extra.append(f"倒计时 {rem_time}")
                if extra:
                    msg += f" ({', '.join(extra)})"
            return {
                "ok": True,
                "found": True,
                "hash": thash,
                "name": matched.get("name") or torrent_name,
                "hr_status": status,
                "hr_pending": (status == "考察中"),
                "record": matched,
                "message": msg
            }
        else:
            return {
                "ok": True,
                "found": False,
                "hash": thash,
                "name": torrent_name,
                "hr_status": "未列入HR",
                "hr_pending": False,
                "record": None,
                "message": "该种子未在站点 H&R 考察列表中（无需考核或普通种子）"
            }

    async def loop(self):
        while True:
            try:
                await run_in_thread(self.refresh_due, coordinator.config)
            except Exception:
                logger.warning("HR 后台刷新异常")
            await asyncio.sleep(60)


class SiteCheckinManager:
    """PT 站点每日自动签到与魔力领取管理器"""
    def __init__(self, cache_path: Path):
        self.cache_path = cache_path
        self.lock = threading.Lock()
        self.status: Dict[str, dict] = {}
        try:
            if self.cache_path.exists():
                data = json.loads(self.cache_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self.status = data
        except Exception:
            self.status = {}

    @staticmethod
    def today_str():
        tz_shanghai = timezone(timedelta(hours=8))
        return datetime.now(tz_shanghai).strftime("%Y-%m-%d")

    @staticmethod
    def extract_bonus(text: str) -> Optional[str]:
        if not text:
            return None
        patterns = [
            r"(?:今天签到您获得|签到成功.*?获得|获得|奖励|增加|赠送)\s*[:：]?\s*([\d,]+(?:\.\d+)?)\s*(?:点|个|颗)?\s*魔力",
            r"魔力值?\s*(?:增加|\+)\s*([\d,]+(?:\.\d+)?)",
            r"\+([\d,]+(?:\.\d+)?)\s*(?:点|个|颗)?\s*魔力",
            r"([\d,]+(?:\.\d+)?)\s*(?:点|个|颗)\s*魔力",
        ]
        for pat in patterns:
            m = re.search(pat, text, re.I)
            if m:
                return m.group(1).replace(",", "")
        return None

    def get_status(self) -> dict:
        with self.lock:
            return dict(self.status)

    def save(self):
        try:
            temp_path = self.cache_path.with_suffix(".tmp")
            temp_path.write_text(json.dumps(self.status, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temp_path, self.cache_path)
        except OSError:
            pass

    def _save_record(self, site_id: str, res: dict, existing_history: list):
        today = res.get("date")
        history = [dict(h) for h in existing_history if isinstance(h, dict)]
        found = False
        for item in history:
            if item.get("date") == today:
                found = True
                b = res.get("bonus_earned") or item.get("bonus_earned")
                item["time"] = res.get("time", time.time())
                item["status"] = res.get("status", "success")
                item["bonus_earned"] = b
                item["message"] = res.get("message", "")
                if b and "魔力" not in item["message"]:
                    item["message"] += f" (获得 +{b} 魔力)"
                break
        if not found and res.get("status") in ("success", "already_checked"):
            history.append({
                "date": today,
                "time": res.get("time", time.time()),
                "status": res.get("status"),
                "bonus_earned": res.get("bonus_earned"),
                "message": res.get("message")
            })
        history = history[-60:]
        res["history"] = history
        with self.lock:
            self.status[site_id] = res
            self.save()

    def checkin_site(self, site: dict, force: bool = False) -> dict:
        site_id = site.get("id") or "unknown"
        site_name = site.get("name") or "PT站点"
        cookie = (site.get("cookie") or "").strip()
        base_url = (site.get("base_url") or "").rstrip("/")
        today = self.today_str()

        with self.lock:
            existing = dict(self.status.get(site_id, {}))
            existing_history = list(existing.get("history", []))

        if not force and existing.get("date") == today and existing.get("status") in ("success", "already_checked"):
            return existing

        if not cookie or not base_url:
            res = {
                "site_id": site_id,
                "site_name": site_name,
                "date": today,
                "time": time.time(),
                "status": "failed",
                "message": "请先配置站点 Base URL 与 Cookie",
                "bonus_earned": None,
                "history": existing_history
            }
            with self.lock:
                self.status[site_id] = res
                self.save()
            return res

        try:
            session = requests.Session()
            session.headers.update({
                "Cookie": cookie,
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) PT-AutoBrush"
            })

            idx_resp = session.get(f"{base_url}/index.php", timeout=20, allow_redirects=False)
            if idx_resp.status_code == 302 or "login.php" in idx_resp.headers.get("Location", ""):
                raise ValueError("Cookie 已失效，站点跳转至登录页")
            idx_resp.encoding = "utf-8"
            soup = BeautifulSoup(idx_resp.text, "html.parser")

            info_block = soup.select_one("#info_block") or soup.find("a", href=re.compile(r"^logout\.php"))
            if not info_block:
                raise ValueError("未检测到登录状态，请检查站点 Cookie")

            # 1. 优先检测 BTSCHOOL 及 NexusPHP 首页 "action=addbonus" 每日签到
            addbonus_link = soup.find("a", href=re.compile(r"action=addbonus"))
            if addbonus_link:
                bonus_url = addbonus_link.get("href", "")
                if not bonus_url.startswith("http"):
                    bonus_url = f"{base_url}/{bonus_url.lstrip('/')}"
                bonus_resp = session.get(bonus_url, timeout=20)
                bonus_resp.encoding = bonus_resp.apparent_encoding or "utf-8"
                b_text = bonus_resp.text
                bonus = self.extract_bonus(b_text)
                msg = f"签到成功：获得 +{bonus} 点魔力值" if bonus else "签到成功"
                res = {
                    "site_id": site_id,
                    "site_name": site_name,
                    "date": today,
                    "time": time.time(),
                    "status": "success",
                    "bonus_earned": bonus,
                    "message": msg
                }
                record_log("CHECKIN", f"[{site_name}] {msg}", "success")
                self._save_record(site_id, res, existing_history)
                return res

            # 2. 尝试标准 NexusPHP attendance.php 接口
            att_resp = session.get(f"{base_url}/attendance.php", timeout=20)
            if att_resp.status_code == 200 and "404" not in att_resp.text[:500]:
                att_resp.encoding = att_resp.apparent_encoding or "utf-8"
                att_text = att_resp.text
                bonus = self.extract_bonus(att_text)
                m_days = re.search(r"连续签到\s*(\d+)\s*天", att_text)
                days = m_days.group(1) if m_days else None

                if "已经签到" in att_text or "请勿重复" in att_text or "明天再来" in att_text:
                    if not bonus and existing.get("date") == today and existing.get("bonus_earned"):
                        bonus = existing.get("bonus_earned")
                    msg = f"今日已签到 (获得 +{bonus} 魔力)" if bonus else "今日已签到（请勿重复刷新）"
                    res = {
                        "site_id": site_id,
                        "site_name": site_name,
                        "date": today,
                        "time": time.time(),
                        "status": "already_checked",
                        "bonus_earned": bonus,
                        "message": msg
                    }
                else:
                    msg_parts = ["签到成功"]
                    if days: msg_parts.append(f"连续 {days} 天")
                    if bonus: msg_parts.append(f"获得 +{bonus} 魔力")
                    res = {
                        "site_id": site_id,
                        "site_name": site_name,
                        "date": today,
                        "time": time.time(),
                        "status": "success",
                        "bonus_earned": bonus,
                        "message": " · ".join(msg_parts)
                    }
                record_log("CHECKIN", f"[{site_name}] {res['message']}", "success")
                self._save_record(site_id, res, existing_history)
                return res

            # 3. 首页已登录但无每日签到按钮，且 attendance 为 404，表明今日在 BTSCHOOL 已签到
            bonus = existing.get("bonus_earned") if existing.get("date") == today else None
            msg = f"今日已签到 (获得 +{bonus} 魔力)" if bonus else "今日已完成签到"
            res = {
                "site_id": site_id,
                "site_name": site_name,
                "date": today,
                "time": time.time(),
                "status": "already_checked",
                "bonus_earned": bonus,
                "message": msg
            }
            record_log("CHECKIN", f"[{site_name}] {res['message']}", "info")
            self._save_record(site_id, res, existing_history)
            return res

        except Exception as exc:
            err_msg = str(exc)
            bonus = existing.get("bonus_earned") if existing.get("date") == today else None
            res = {
                "site_id": site_id,
                "site_name": site_name,
                "date": today,
                "time": time.time(),
                "status": "failed",
                "bonus_earned": bonus,
                "message": f"签到失败: {err_msg}"
            }
            record_log("CHECKIN", f"[{site_name}] 签到失败: {err_msg}", "warning")
            self._save_record(site_id, res, existing_history)
            return res

    def checkin_all_due(self, config):
        today = self.today_str()
        for site in config.get("sites", []):
            if not site.get("enabled", True):
                continue
            if site.get("auto_checkin", True) is False:
                continue
            site_id = site.get("id")
            with self.lock:
                rec = self.status.get(site_id, {})
            if rec.get("date") == today and rec.get("status") in ("success", "already_checked"):
                continue
            self.checkin_site(site, force=False)


hr_monitor = HRMonitor(DATA_DIR / "hr_status.json")
site_checkin_mgr = SiteCheckinManager(DATA_DIR / "checkin_status.json")

# ==================== 调度器与主循环 ====================
class AutoBrushCoordinator:
    def __init__(self):
        self.config = load_config()
        self.qb = QBittorrentClient(
            self.config["downloader"]["url"],
            self.config["downloader"]["username"],
            self.config["downloader"]["password"]
        )
        self.cull_engine = DiskAndCullEngine(self.qb, self.config)
        self.is_running = bool(self.config.get("server", {}).get("engine_running", True))
        self.seen_torrent_ids = set()
        self.daily_stats = {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "uploaded_bytes": 0,
            "downloaded_bytes": 0,
            "snatched_count": 0
        }
        self.cached_status = None
        self.last_status_time = 0
        self.status_refresh_task = None
        self.last_status_attempt = float("-inf")
        self.status_cache_ttl = 4.0

    def ensure_status_refresh(self):
        now = time.monotonic()
        if self.status_refresh_task and not self.status_refresh_task.done():
            return
        if self.cached_status is not None and now - self.last_status_time < self.status_cache_ttl:
            return
        if now - self.last_status_attempt < self.status_cache_ttl:
            return
        self.last_status_attempt = now

        async def refresh():
            try:
                await run_in_thread(self.refresh_status_cache)
            except Exception as exc:
                logger.debug(f"刷新状态缓存异常: {exc}")

        self.status_refresh_task = asyncio.create_task(refresh())

    def refresh_status_cache(self) -> Dict[str, Any]:
        qb, cull_engine, config = self.qb, self.cull_engine, self.config
        try:
            disk = cull_engine.get_disk_status()
            cat = config.get("downloader", {}).get("category", "pt-brush")
            torrents = qb.get_torrents(category=cat)
            transfer = qb.get_transfer_info()
            try:
                source = hashlib.sha256(json.dumps([config.get("downloader", {}).get("url"), cat]).encode()).hexdigest()
                metrics_store.capture(qb, torrents, source)
            except Exception:
                logger.warning("报表采集失败，保留历史数据")
            today = metrics_store.today()

            dl_count = sum(1 for t in torrents if t.get("state") in ("downloading", "stalledDL", "checkingDL", "metaDL", "forcedDL", "queuedDL"))
            up_count = sum(1 for t in torrents if t.get("state") in ("uploading", "seeding", "stalledUP", "forcedUP"))

            effective_free_gb = cull_engine.get_effective_free_gb(torrents)
            disk["effective_free_gb"] = effective_free_gb

            status_obj = {
                "engine_running": self.is_running,
                "disk": disk,
                "transfer": {
                    "up_speed_mb": round(transfer.get("up_info_speed", 0) / (1024**2), 2),
                    "dl_speed_mb": round(transfer.get("dl_info_speed", 0) / (1024**2), 2),
                    "today_uploaded_gb": round(today["uploaded"] / (1024**3), 2),
                    "today_downloaded_gb": round(today["downloaded"] / (1024**3), 2)
                },
                "torrents_count": len(torrents),
                "downloading_count": dl_count,
                "seeding_count": up_count,
                "torrents": torrents,
                "limits": {"min_disk_free_gb": config.get("cull", {}).get("min_disk_free_gb", 15),
                           "single_upload_limit_mb": config.get("downloader", {}).get("single_upload_limit_mb", 85)}
            }

            if qb is self.qb:
                self.cached_status = status_obj
                self.last_status_time = time.monotonic()
            return status_obj
        except Exception as e:
            logger.debug(f"刷新状态缓存异常: {e}")
            if self.cached_status:
                return self.cached_status
            return {"engine_running": self.is_running, "disk": {}, "transfer": {}, "torrents_count": 0, "torrents": []}

    def reload(self):
        self.config = load_config()
        self.qb = QBittorrentClient(
            self.config["downloader"]["url"],
            self.config["downloader"]["username"],
            self.config["downloader"]["password"]
        )
        self.cull_engine = DiskAndCullEngine(self.qb, self.config)
        self.is_running = bool(self.config.get("server", {}).get("engine_running", self.is_running))
        self.cached_status = None
        self.last_status_time = 0
        self.last_status_attempt = float("-inf")
        record_log("SYSTEM", "配置已热加载更新 (SQLite)")

    async def main_loop(self):
        record_log("SYSTEM", "PT-AutoBrush 守护引擎已启动，已启用 SQLite 配置持久化与智能防爆盘安全系统", "info")
        while True:
            try:
                if self.is_running:
                    # 1. 磁盘与删种多维安全巡检
                    await run_in_thread(self.cull_engine.run_cull_cycle, False)

                    # 2. 检查磁盘剩余水位，若处于告警状态则暂停推新
                    disk = await run_in_thread(self.cull_engine.get_disk_status)
                    if disk.get("is_low"):
                        record_log(
                            "SNIFFER",
                            f"磁盘空间告警 (剩余 {disk.get('free_gb')}GB / 使用率 {disk.get('used_pct')}%)，自动暂停抓取新种以保护系统",
                            "warning"
                        )
                    else:
                        # 3. 轮询启用的站点
                        for site in self.config.get("sites", []):
                            if not site.get("enabled"):
                                continue
                            await run_in_thread(self.sniff_site, site)

                await asyncio.sleep(30)
            except Exception as e:
                record_log("ERROR", f"主调度异常: {e}", "danger")
                await asyncio.sleep(10)

    def sniff_site(self, site: dict):
        sniffer = NexusPHPSniffer(site, self.config["rules"])
        items = sniffer.fetch_rss_items()
        if not items:
            return

        cat = self.config.get("downloader", {}).get("category", "pt-brush")
        existing_torrents = self.qb.get_torrents(category=cat)
        existing_names = [t.get("name", "").lower() for t in existing_torrents]

        # 检查正在下载任务数量上限
        max_concurrent = int(self.config.get("downloader", {}).get("max_concurrent_downloads", 2))
        active_dl_count = sum(1 for t in existing_torrents if t.get("state") in ("downloading", "stalledDL", "checkingDL", "metaDL", "forcedDL", "queuedDL"))
        if active_dl_count >= max_concurrent:
            record_log("SNIFFER", f"[{site['name']}] 正在下载任务数已达上限 ({active_dl_count}/{max_concurrent})，排队等待完成", "info")
            return

        # 计算当前有效剩余磁盘空间
        cull_cfg = self.config.get("cull", {})
        min_free_limit = float(cull_cfg.get("min_disk_free_gb", 15.0))
        effective_free_gb = self.cull_engine.get_effective_free_gb(existing_torrents)

        for item in items[:10]:
            tid = item.get("id") or item.get("link")
            title = item.get("title", "")
            if not tid or tid in self.seen_torrent_ids:
                continue

            clean_title = re.sub(r"\[.*?\]", "", title).strip().lower()
            if any(clean_title and clean_title in name for name in existing_names) or title.lower() in existing_names:
                self.seen_torrent_ids.add(tid)
                continue

            eval_res = sniffer.evaluate_candidate(item)
            if not eval_res.get("pass"):
                self.seen_torrent_ids.add(tid)
                record_log("SKIP", f"[{site['name']}] 跳过: {title[:30]} -> 原因: {eval_res.get('reason')}", "warning")
                continue

            # ===== 【防爆盘防线 4：入库前体积预扣核验】 =====
            info = eval_res.get("info", {})
            cand_size_gb = float(info.get("size_gb", 0.0))
            if cull_cfg.get("pre_check_torrent_size", True) and cand_size_gb > 0:
                if cand_size_gb > (effective_free_gb - min_free_limit):
                    self.seen_torrent_ids.add(tid)
                    record_log(
                        "DISK-GUARD",
                        f"[{site['name']}] 防爆盘预扣拦截: 种子体积 {cand_size_gb:.1f}GB，当前有效剩余仅 {effective_free_gb:.1f}GB (需保留安全红线 {min_free_limit}GB)，坚决拒绝入库防打满！",
                        "danger"
                    )
                    continue

            # 命中！准备推送
            torrent_url = item.get("torrent_url") or item.get("link")
            if torrent_url:
                torrent_url = html.unescape(torrent_url).replace("&amp;", "&")
            limit_mb = int(self.config.get("downloader", {}).get("single_upload_limit_mb", 85))
            limit_b = limit_mb * 1024 * 1024
            save_path = self.config.get("downloader", {}).get("save_path", "/root/Downloads")

            torrent_bytes = None
            if torrent_url:
                try:
                    tr_resp = sniffer.session.get(torrent_url, timeout=15)
                    if tr_resp.status_code == 200 and (tr_resp.content.startswith(b"d8:announce") or len(tr_resp.content) > 200):
                        torrent_bytes = tr_resp.content
                        # 二次核对真实 torrent 文件中的大小
                        meta = parse_torrent_metadata(torrent_bytes)
                        real_size_gb = meta.get("size_gb", 0.0)
                        max_limit_gb = float(self.config.get("rules", {}).get("max_size_gb", 15.0))
                        if real_size_gb > 0 and real_size_gb > max_limit_gb:
                            self.seen_torrent_ids.add(tid)
                            record_log(
                                "DISK-GUARD",
                                f"[{site['name']}] 体积上限拦截: 种子真实大小为 {real_size_gb:.1f}GB 超出上限 {max_limit_gb:.1f}GB，拒绝推送！",
                                "danger"
                            )
                            continue
                        if real_size_gb > 0 and real_size_gb > (effective_free_gb - min_free_limit):
                            self.seen_torrent_ids.add(tid)
                            record_log(
                                "DISK-GUARD",
                                f"[{site['name']}] 防爆盘严格拦截: 解析种子真实大小为 {real_size_gb:.1f}GB，超出有效承载范围，拒绝推送！",
                                "danger"
                            )
                            continue
                except Exception as dl_err:
                    logger.debug(f"直接拉取种子文件异常: {dl_err}")

            success = self.qb.add_torrent(
                torrent_url,
                category=cat,
                upload_limit_b=limit_b,
                torrent_bytes=torrent_bytes,
                savepath=save_path
            )
            self.seen_torrent_ids.add(tid)
            if success:
                self.daily_stats["snatched_count"] += 1
                record_log(
                    "HIT-PUSH",
                    f"[{site['name']}] 命中 {info.get('promotion','Free')} 种子: {title[:40]} | 体积 {cand_size_gb:.1f}GB | 剩余 {info.get('remaining_minutes')}m | 推送成功 (限速 {limit_mb}MB/s)",
                    "success"
                )
                # 重新计算有效可用空间
                effective_free_gb = max(0.0, effective_free_gb - cand_size_gb)
            else:
                record_log("PUSH-FAIL", f"[{site['name']}] 推送 qB 失败或已存在: {title[:35]}", "warning")

coordinator = AutoBrushCoordinator()

# ==================== FastAPI 接口设计 ====================
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    main_task = asyncio.create_task(coordinator.main_loop())
    hr_task = asyncio.create_task(hr_monitor.loop())
    async def account_loop():
        while True:
            try:
                account_monitor.trigger(coordinator.config)
                await account_monitor.task
            except Exception:
                logger.warning("账号统计后台刷新异常")
            await asyncio.sleep(60)
    account_task = asyncio.create_task(account_loop())
    async def sample_metrics():
        while True:
            await run_in_thread(metrics_store.cleanup)
            await asyncio.sleep(60)
            coordinator.ensure_status_refresh()
    metrics_task = asyncio.create_task(sample_metrics())
    async def checkin_loop():
        await asyncio.sleep(5)
        while True:
            try:
                await run_in_thread(site_checkin_mgr.checkin_all_due, coordinator.config)
            except Exception as exc:
                logger.warning(f"站点自动签到后台异常: {exc}")
            await asyncio.sleep(1800)
    checkin_task = asyncio.create_task(checkin_loop())
    try:
        yield
    finally:
        main_task.cancel()
        hr_task.cancel()
        account_task.cancel()
        metrics_task.cancel()
        checkin_task.cancel()
        tasks = [main_task, hr_task, metrics_task, account_task, checkin_task]
        if coordinator.status_refresh_task:
            coordinator.status_refresh_task.cancel()
            tasks.append(coordinator.status_refresh_task)
        await asyncio.gather(*tasks, return_exceptions=True)

app = FastAPI(title="PT-AutoBrush WebUI Service", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=500)
from security import configure_security
configure_security(app, APP_DIR)

@app.middleware("http")
async def measure_response_time(request: Request, call_next):
    started = time.perf_counter()
    response = await call_next(request)
    response.headers["Server-Timing"] = f"app;dur={(time.perf_counter() - started) * 1000:.2f}"
    response.headers["X-PT-Build"] = "secure-console-20261009"
    response.headers["Cache-Control"] = "no-store"
    return response

@app.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse)
def serve_index():
    index_path = APP_DIR / "index.html"
    if index_path.exists():
        with open(index_path, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    return HTMLResponse("<h2>index.html 未找到</h2>")

@app.get("/api/reports")
async def get_reports(days: int = 7, hours: int = 24):
    return await run_in_thread(metrics_store.report, days, hours)


@app.get("/api/accounts")
async def get_accounts():
    snapshot = await run_in_thread(account_monitor.snapshot, coordinator.config)
    checkins = site_checkin_mgr.get_status()
    for site in snapshot.get("sites", []):
        sid = site.get("site_id")
        site["checkin"] = checkins.get(sid)
    return snapshot


@app.post("/api/accounts/refresh")
async def refresh_accounts():
    account_monitor.trigger(coordinator.config, force=True)
    snapshot = await run_in_thread(account_monitor.snapshot, coordinator.config)
    checkins = site_checkin_mgr.get_status()
    for site in snapshot.get("sites", []):
        sid = site.get("site_id")
        site["checkin"] = checkins.get(sid)
    return snapshot


@app.get("/api/sites/checkin_status")
async def get_checkin_status():
    return {"ok": True, "status": site_checkin_mgr.get_status()}


@app.post("/api/sites/{site_id}/checkin")
async def manual_site_checkin(site_id: str):
    site = next((s for s in coordinator.config.get("sites", []) if s.get("id") == site_id), None)
    if not site:
        raise HTTPException(status_code=404, detail="未找到对应站点")
    res = await run_in_thread(site_checkin_mgr.checkin_site, site, True)
    return {"ok": True, "result": res}


@app.post("/api/torrents/{torrent_hash}/check_hr")
async def check_single_torrent_hr(torrent_hash: str):
    res = await run_in_thread(hr_monitor.check_torrent_hr, torrent_hash, coordinator.config, coordinator.qb, True)
    if coordinator.cached_status and "torrents" in coordinator.cached_status:
        torrents = hr_monitor.mark_torrents(
            coordinator.cached_status["torrents"], coordinator.config
        )
        cull_cfg = coordinator.config.get("cull", {})
        scored = []
        for t in torrents:
            s_info = compute_torrent_score(t, cull_cfg)
            scored.append({
                **t,
                "score": s_info["score"],
                "score_level": s_info["level"],
                "score_label": s_info["level_label"],
                "is_compliant": s_info["is_compliant"],
                "compliance_reason": s_info["compliance_reason"],
                "score_breakdown": s_info["breakdown"],
                "score_summary": s_info["summary"]
            })
        coordinator.cached_status["torrents"] = scored
    return res


@app.get("/api/status")
async def get_status():
    coordinator.ensure_status_refresh()
    if coordinator.cached_status:
        torrents = hr_monitor.mark_torrents(coordinator.cached_status.get("torrents", []), coordinator.config)
        cull_cfg = coordinator.config.get("cull", {})
        scored_torrents = []
        for t in torrents:
            s_info = compute_torrent_score(t, cull_cfg)
            scored_torrents.append({
                **t,
                "score": s_info["score"],
                "score_level": s_info["level"],
                "score_label": s_info["level_label"],
                "is_compliant": s_info["is_compliant"],
                "compliance_reason": s_info["compliance_reason"],
                "score_breakdown": s_info["breakdown"],
                "score_summary": s_info["summary"]
            })
        return {**coordinator.cached_status, "engine_running": coordinator.is_running, "status_ready": True,
                "torrents": scored_torrents}
    return {
        "engine_running": coordinator.is_running,
        "status_ready": False,
        "disk": None,
        "transfer": None,
        "torrents_count": 0,
        "downloading_count": 0,
        "seeding_count": 0,
        "torrents": []
    }

@app.get("/app.js", include_in_schema=False)
async def serve_script():
    return FileResponse(APP_DIR / "app.js", media_type="application/javascript", headers={"Cache-Control": "no-store"})

@app.post("/api/engine/toggle")
async def toggle_engine():
    coordinator.is_running = not coordinator.is_running
    coordinator.config.setdefault("server", {})["engine_running"] = coordinator.is_running
    await run_in_thread(save_config, coordinator.config, "切换引擎启停状态 (engine_running)")
    status_str = "恢复运行" if coordinator.is_running else "已暂停"
    record_log("SYSTEM", f"用户切换引擎状态为: {status_str}")
    return {"ok": True, "running": coordinator.is_running}

@app.post("/api/engine/sniff")
async def trigger_sniff():
    record_log("MANUAL", "手动触发全站即时嗅探与巡检")
    for site in coordinator.config.get("sites", []):
        if site.get("enabled"):
            await run_in_thread(coordinator.sniff_site, site)
    return {"ok": True, "message": "嗅探完成"}

@app.post("/api/torrents/cull")
async def trigger_emergency_cull():
    res = await run_in_thread(coordinator.cull_engine.run_cull_cycle, True)
    return {"ok": True, "res": res}

@app.post("/api/torrents/reannounce")
async def reannounce_torrent(request: Request):
    data = await request.json()
    thash = data.get("hash")
    if thash:
        ok = await run_in_thread(coordinator.qb.reannounce, thash)
        return {"ok": ok}
    return {"ok": False}

@app.delete("/api/torrents/{torrent_hash}")
async def delete_torrent(torrent_hash: str):
    ok = await run_in_thread(coordinator.qb.delete_torrent, torrent_hash, True)
    if ok:
        record_log("AUTO-CULL", f"手动删除了种子 [{torrent_hash[:8]}]")
    return {"ok": ok}

@app.post("/api/torrents/upload")
async def upload_torrents(
    files: List[UploadFile] = File(...),
    category: str = Form("pt-brush"),
    paused: bool = Form(False),
    upload_limit_mb: int = Form(0),
    force: bool = Form(False)
):
    """
    手动上传 .torrent 种子文件接口：
    1. 自动解析种子元数据（文件名、单文件/多文件总大小）
    2. 防爆盘安全校验（若可用空间不足且未勾选暂停/强制，则阻断添加，保护 VPS 不被打满）
    3. 二进制直传推送至 qBittorrent
    """
    if not files:
        raise HTTPException(status_code=400, detail="未选择种子文件")

    cat = category.strip() or "pt-brush"
    limit_b = upload_limit_mb * 1024 * 1024 if upload_limit_mb > 0 else 0
    save_path = coordinator.config.get("downloader", {}).get("save_path", "/root/Downloads")
    cull_cfg = coordinator.config.get("cull", {})
    min_disk_free = float(cull_cfg.get("min_disk_free_gb", 15.0))

    results = []
    success_count = 0

    for f in files:
        filename = f.filename or "upload.torrent"
        try:
            content = await f.read()
            if not content:
                results.append({"filename": filename, "ok": False, "error": "文件内容为空"})
                continue

            # 解析种子元数据
            meta = parse_torrent_metadata(content)
            torrent_name = meta.get("name") or filename
            torrent_size_gb = float(meta.get("size_gb", 0.0))

            # 防爆盘预扣核验（若立即下载且未指定强制添加，则检查空间）
            if not paused and not force and torrent_size_gb > 0:
                effective_free = coordinator.cull_engine.get_effective_free_gb()
                if torrent_size_gb > (effective_free - min_disk_free):
                    err_msg = (
                        f"【防爆盘安全拦截】种子大小 {torrent_size_gb:.1f}GB，"
                        f"当前有效剩余仅 {effective_free:.1f}GB (系统需保留安全红线 {min_disk_free:.1f}GB)。"
                        f"为防 VPS 磁盘打满已阻止添加！请先清理空间，或勾选【暂停添加】/【强制添加】。"
                    )
                    results.append({"filename": filename, "ok": False, "error": err_msg})
                    record_log("DISK-GUARD", f"手动上传被拦截: {filename} ({torrent_size_gb:.1f}GB) -> {err_msg}", "warning")
                    continue

            ok = await run_in_thread(
                coordinator.qb.add_torrent,
                "",
                cat,
                limit_b,
                content,
                filename,
                paused,
                save_path
            )

            if ok:
                success_count += 1
                results.append({"filename": filename, "name": torrent_name, "size_gb": torrent_size_gb, "ok": True})
                limit_str = f"{upload_limit_mb}MB/s" if upload_limit_mb > 0 else "不限速"
                record_log(
                    "MANUAL",
                    f"手动上传种子成功: {torrent_name[:35]} (体积: {torrent_size_gb:.1f}GB, 分类: {cat}, 限速: {limit_str}, 暂停: {paused})",
                    "success"
                )
            else:
                results.append({"filename": filename, "ok": False, "error": "qBittorrent 添加失败（可能已存在相同任务）"})
                record_log("MANUAL", f"手动上传种子失败: {filename} (qB 返回失败)", "warning")
        except Exception as exc:
            results.append({"filename": filename, "ok": False, "error": str(exc)})
            record_log("MANUAL", f"手动上传种子异常: {filename} -> {exc}", "danger")

    if success_count > 0:
        await run_in_thread(coordinator.refresh_status_cache)

    return {
        "ok": success_count > 0,
        "total": len(files),
        "success_count": success_count,
        "results": results
    }

@app.post("/api/torrents/add_urls")
async def add_torrent_urls(request: Request):
    data = await request.json()
    urls_str = data.get("urls", "").strip()
    category = data.get("category", "pt-brush").strip() or "pt-brush"
    paused = bool(data.get("paused", False))
    upload_limit_mb = int(data.get("upload_limit_mb", 0))
    limit_b = upload_limit_mb * 1024 * 1024 if upload_limit_mb > 0 else 0
    save_path = coordinator.config.get("downloader", {}).get("save_path", "/root/Downloads")

    if not urls_str:
        raise HTTPException(status_code=400, detail="链接不能为空")

    urls = [u.strip() for u in urls_str.splitlines() if u.strip()]
    if not urls:
        raise HTTPException(status_code=400, detail="未解析到有效链接")

    success_count = 0
    results = []
    for u in urls:
        ok = await run_in_thread(
            coordinator.qb.add_torrent,
            u,
            category,
            limit_b,
            None,
            "download.torrent",
            paused,
            save_path
        )
        if ok:
            success_count += 1
            results.append({"url": u, "ok": True})
            record_log("MANUAL", f"手动添加链接成功: {u[:60]}...", "success")
        else:
            results.append({"url": u, "ok": False, "error": "添加失败"})
            record_log("MANUAL", f"手动添加链接失败: {u[:60]}...", "warning")

    if success_count > 0:
        await run_in_thread(coordinator.refresh_status_cache)

    return {"ok": success_count > 0, "total": len(urls), "success_count": success_count, "results": results}

@app.get("/api/config")
async def get_config():
    # The browser only needs to know that credentials exist; retain them server-side.
    cfg = json.loads(json.dumps(coordinator.config))
    for section, key in [("downloader", "password"), ("server", "token")]:
        if cfg.get(section, {}).get(key):
            cfg[section][key] = "__UNCHANGED__"
    for site in cfg.get("sites", []):
        for key in ("cookie", "rss_url"):
            if site.get(key):
                site[key] = "__UNCHANGED__"
    return cfg

@app.post("/api/config")
async def update_config(request: Request):
    new_cfg = await request.json()
    if not isinstance(new_cfg, dict):
        raise HTTPException(status_code=400, detail="配置必须是 JSON 对象")
    for section, key in [("downloader", "password"), ("server", "token")]:
        section_cfg = new_cfg.get(section)
        if section_cfg is not None and not isinstance(section_cfg, dict):
            raise HTTPException(status_code=400, detail="无效的配置分节")
        if section_cfg and section_cfg.get(key) == "__UNCHANGED__":
            section_cfg[key] = coordinator.config.get(section, {}).get(key, "")
    if "sites" in new_cfg:
        if not isinstance(new_cfg["sites"], list) or any(not isinstance(s, dict) for s in new_cfg["sites"]):
            raise HTTPException(status_code=400, detail="无效的站点列表")
        current_sites = {s["id"]: s for s in coordinator.config.get("sites", [])}
        for site in new_cfg["sites"]:
            current = current_sites.get(site.get("id"), {})
            for key in ("cookie", "rss_url"):
                if site.get(key) == "__UNCHANGED__":
                    site[key] = current.get(key, "")
            if "auto_checkin" not in site:
                site["auto_checkin"] = current.get("auto_checkin", True)
    await run_in_thread(save_config, new_cfg, "WebUI 配置保存")
    await run_in_thread(coordinator.reload)
    return {"ok": True}

@app.get("/api/config/backups")
async def get_config_backups():
    """查看配置在 SQLite 中的历史快照"""
    backups = await run_in_thread(config_mgr.list_backups)
    return {"ok": True, "backups": backups}

@app.post("/api/config/restore/{backup_id}")
async def restore_config_backup(backup_id: int):
    """从指定的 SQLite 历史快照版本中恢复配置"""
    ok = await run_in_thread(config_mgr.restore_backup, backup_id)
    if ok:
        await run_in_thread(coordinator.reload)
        return {"ok": True, "message": f"成功恢复版本 #{backup_id}"}
    raise HTTPException(status_code=404, detail="未找到该历史备份或恢复失败")

@app.get("/api/config/export")
async def export_config():
    """导出当前完整配置 JSON"""
    cfg = coordinator.config
    return JSONResponse(
        content=cfg,
        headers={"Content-Disposition": "attachment; filename=pt_brush_config_backup.json"}
    )

@app.get("/api/logs")
async def get_logs():
    return {"logs": list(system_logs)}

@app.post("/api/logs/clear")
async def clear_logs():
    system_logs.clear()
    return {"ok": True}

if __name__ == "__main__":
    cfg = load_config()
    port = int(os.environ.get("PT_BIND_PORT", cfg["server"].get("port", 12300)))
    host = os.environ.get("PT_BIND_HOST", cfg["server"].get("host", "0.0.0.0"))
    print(f"[*] PT-AutoBrush 正在启动 WebUI 服务: http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="warning")
