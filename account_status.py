"""BTSCHOOL account summary: read-only requests, two-hour persistent cache."""
import asyncio
import hashlib
import json
import os
import re
import threading
import time
import sqlite3
from contextlib import contextmanager
from metrics import day_of
from urllib.parse import urlparse, parse_qs

import requests
from bs4 import BeautifulSoup

REFRESH_SECONDS = 7200
BASE = 'https://pt.btschool.club'
NUMBER = r'[\d,]+(?:\.\d+)?'


def logged_in_soup(body):
    soup = BeautifulSoup(body, 'html.parser')
    header = soup.select_one('#info_block')
    if not header or not header.find('a', href=re.compile(r'^logout\.php(?:\?|$)')):
        raise ValueError('无法读取账号，请检查站点 Cookie 是否有效')
    return soup, header


def parse_account(body):
    soup, header = logged_in_soup(body)
    # Only the welcome cell; assessment and torrent stats must not replace totals.
    user = header.find('a', href=re.compile(r'^userdetails\.php\?'))
    if not user:
        raise ValueError('账号页结构异常，未找到用户名')
    cell = user.find_parent('td') or header
    for image in cell.find_all('img'):
        if image.get('alt') in ('Torrents seeding', 'Torrents leeching'):
            image.replace_with(' 做种数： ' if image['alt'] == 'Torrents seeding' else ' 下载数： ')
    text = cell.get_text(' ', strip=True)
    def field(pattern):
        match = re.search(pattern, text, re.I)
        return match.group(1).strip() if match else None
    stats = {
        'username': user.get_text(' ', strip=True),
        'user_id': parse_qs(urlparse(user['href']).query).get('id', [''])[0],
        'bonus': field(r'魔力值\s*\[\s*使用\s*\]\s*[:：]\s*(' + NUMBER + ')'),
        'invites': field(r'邀请\s*\[\s*发送\s*\]\s*[:：]\s*([\d+\s]+)'),
        'ratio': field(r'分享率\s*[:：]\s*(' + NUMBER + r'|∞|Inf(?:inite)?|无限)'),
        'hr': field(r'H\s*&\s*R\s*[:：]\s*([\d,]+\s*/\s*[\d,]+)'),
        'uploaded': field(r'上传量\s*[:：]\s*(' + NUMBER + r'\s*[KMGTPE]?i?B)'),
        'downloaded': field(r'下载量\s*[:：]\s*(' + NUMBER + r'\s*[KMGTPE]?i?B)'),
        'seeding': field(r'做种数\s*[:：]\s*(\d+)'),
        'leeching': field(r'下载数\s*[:：]\s*(\d+)'),
        'connectable': field(r'可连接\s*[:：]\s*(是|否|未知|Yes|No)'),
        'slots': field(r'连接数\s*[:：]\s*(无限制|无限|Unlimited|\d+)'),
    }
    if not stats['uploaded'] or not stats['downloaded']:
        raise ValueError('账号页结构异常，未找到上传量或下载量')
    # Keep the station's remaining/achieved wording; never infer a pass from absence.
    assessment = {'status': 'not_displayed', 'summary': '站点当前未展示新人考核', 'items': []}
    marker = soup.find(string=re.compile(r'离(?:新人|新手)考核结束还有'))
    if marker:
        banner = marker.find_parent('font') or marker.find_parent('td') or marker.parent
        banner_text = banner.get_text(' ', strip=True)
        remaining = re.search(r'考核结束还有\s*(.*?)\s*(?:上传量|下载量|魔力值)\s*[:：]', banner_text)
        deadline = banner.find(attrs={'title': re.compile(r'\d{4}-\d{2}-\d{2}')})
        items = []
        for label in ('上传量', '下载量', '魔力值', '做种时间', '分享率'):
            match = re.search(re.escape(label) + r'\s*[:：]\s*(.*?)(?=\s*(?:上传量|下载量|魔力值|做种时间|分享率)\s*[:：]|你也可以|$)', banner_text)
            if match:
                items.append({'label': label, 'value': match.group(1).strip()})
        assessment = {'status': 'active', 'summary': '新人考核进行中',
                      'remaining': remaining.group(1) if remaining else None,
                      'deadline': deadline.get('title') if deadline else None, 'items': items}
    stats['assessment'] = assessment
    return stats


def parse_bonus(body):
    soup, _ = logged_in_soup(body)
    text = soup.get_text(' ', strip=True)
    match = re.search(r'(?:你|您)\s*当前\s*每小时\s*(?:能|可|可以)?\s*(?:获取|获得)\s*(' + NUMBER + r')\s*个?\s*魔力值', text)
    if not match:
        raise ValueError('魔力页结构异常，未找到每小时魔力值')
    return match.group(1)


class AccountMonitor:
    def __init__(self, cache_path):
        self.cache_path = cache_path
        self.lock = threading.Lock()
        self.fetch_lock = threading.Lock()
        self.task = None
        self.task_force = False
        self.pending_force_config = None
        self.history_path = cache_path.with_name('account_snapshots.db')
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS snapshots (account TEXT, day TEXT, captured_at REAL, payload TEXT, PRIMARY KEY(account, day))')
        try:
            cache = json.loads(cache_path.read_text(encoding='utf-8'))
            self.cache = {k: v for k, v in cache.items() if isinstance(v, dict)} if isinstance(cache, dict) else {}
        except (OSError, ValueError):
            self.cache = {}

    @staticmethod
    def sites(config):
        return [s for s in config.get('sites', [])
                if (urlparse(s.get('base_url', '')).hostname or '').lower() == 'pt.btschool.club']

    @contextmanager
    def connect(self):
        db = sqlite3.connect(str(self.history_path), timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def key(site):
        return hashlib.sha256(json.dumps(['account-v1', site.get('id'), site.get('base_url'), site.get('cookie')]).encode()).hexdigest()

    @staticmethod
    def history_key(site, stats):
        return hashlib.sha256(json.dumps([site.get('id'), stats.get('user_id'), stats.get('username')]).encode()).hexdigest()

    def snapshot(self, config):
        history = self.history(config)
        with self.lock:
            result = []
            for site in self.sites(config):
                entry = dict(self.cache.get(self.key(site), {}))
                entry.update(site_id=site.get('id'), site_name=site.get('name', 'BTSCHOOL'))
                entry['stale'] = bool(entry.get('error') or not entry.get('updated_at') or time.time() - entry['updated_at'] >= REFRESH_SECONDS)
                entry['bonus_stale'] = bool(entry.get('bonus_error') or not entry.get('bonus_updated_at') or time.time() - entry['bonus_updated_at'] >= REFRESH_SECONDS)
                entry['daily_snapshots'] = history.get(self.key(site), [])
                result.append(entry)
        return {'sites': result, 'refresh_interval_seconds': REFRESH_SECONDS,
                'refreshing': bool(self.task and not self.task.done())}

    def history(self, config):
        with self.connect() as db:
            db.execute('DELETE FROM snapshots WHERE day < ?', (day_of(time.time() - 29 * 86400),))
            result = {}
            for site in self.sites(config):
                key = self.key(site)
                with self.lock:
                    stats = self.cache.get(key, {}).get('stats', {})
                if not stats:
                    continue
                rows = db.execute('SELECT day, captured_at, payload FROM snapshots WHERE account=? ORDER BY day DESC', (self.history_key(site, stats),))
                result[key] = [{'day': day, 'captured_at': ts, **json.loads(payload)} for day, ts, payload in rows]
            return result

    def save_daily(self, key, entry):
        now = time.time()
        with self.connect() as db:
            # First fully successful refresh of each Shanghai date is immutable.
            db.execute('INSERT OR IGNORE INTO snapshots VALUES (?,?,?,?)',
                       (key, day_of(now), now, json.dumps({'stats': entry['stats'], 'bonus_per_hour': entry['bonus_per_hour']}, ensure_ascii=False)))

    @staticmethod
    def page(session, path):
        response = session.get(BASE + path, timeout=(5, 15), allow_redirects=False)
        if response.status_code != 200:
            raise ValueError('站点返回 HTTP %s，请检查 Cookie 或稍后重试' % response.status_code)
        response.encoding = 'utf-8'
        return response.text

    def refresh_due(self, config, force=False):
        with self.fetch_lock:
            active = set()
            changed = False
            for site in self.sites(config):
                key = self.key(site)
                active.add(key)
                now = time.time()
                with self.lock:
                    entry = dict(self.cache.get(key, {}))
                if not force and 0 <= now - entry.get('last_attempt_at', 0) < REFRESH_SECONDS:
                    continue
                changed = True
                entry.update(last_attempt_at=now, next_refresh_at=now + REFRESH_SECONDS)
                if not site.get('cookie', '').strip():
                    entry.update(error='请先在 PT 站点配置中填写 Cookie', bonus_error='请先填写 Cookie')
                else:
                    with requests.Session() as session:
                        session.headers.update({'Cookie': site['cookie'], 'User-Agent': 'Mozilla/5.0 PT-AutoBrush'})
                        for path, parser, error_key in [('/index.php', parse_account, 'error'), ('/mybonus.php', parse_bonus, 'bonus_error')]:
                            try:
                                parsed = parser(self.page(session, path))
                                if error_key == 'error':
                                    entry.update(stats=parsed, updated_at=time.time(), error=None)
                                else:
                                    entry.update(bonus_per_hour=parsed, bonus_updated_at=time.time(), bonus_error=None)
                            except ValueError as exc:
                                entry[error_key] = str(exc)
                            except requests.RequestException:
                                entry[error_key] = '站点连接失败或超时，请稍后重试'
                            except Exception:
                                entry[error_key] = '站点数据读取失败，请稍后重试'
                with self.lock:
                    self.cache[key] = entry
                if not entry.get('error') and not entry.get('bonus_error'):
                    self.save_daily(self.history_key(site, entry['stats']), entry)
            with self.lock:
                changed = changed or set(self.cache) != active
                self.cache = {k: v for k, v in self.cache.items() if k in active}
                payload = json.dumps(self.cache, ensure_ascii=False)
            self.history(config)
            if changed:
                temp = self.cache_path.with_suffix('.tmp')
                try:
                    temp.write_text(payload, encoding='utf-8')
                    os.replace(temp, self.cache_path)
                except OSError:
                    pass  # In-memory result remains available if disk is full.

    def trigger(self, config, force=False):
        config = json.loads(json.dumps(config))
        if not self.task or self.task.done():
            # Freeze configuration so cookie edits cannot mix accounts mid-refresh.
            self.task_force = force
            self.pending_force_config = None
            self.task = asyncio.create_task(self.refresh_async(config, force))
        elif force and not self.task_force:
            self.pending_force_config = config

    async def refresh_async(self, config, force):
        await asyncio.get_running_loop().run_in_executor(None, self.refresh_due, config, force)
        if self.pending_force_config is not None:
            config = self.pending_force_config
            self.pending_force_config = None
            self.task_force = True
            await asyncio.get_running_loop().run_in_executor(None, self.refresh_due, config, True)

