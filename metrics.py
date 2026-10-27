"""Persistent downloader counters and managed-torrent lifecycle statistics."""
import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager

TZ = timezone(timedelta(hours=8))


def day_of(timestamp):
    return datetime.fromtimestamp(timestamp, TZ).strftime("%Y-%m-%d")


class MetricsStore:
    def __init__(self, path):
        self.path = str(path)
        self.lock = threading.Lock()
        self.last_attempt = float("-inf")
        self.error = None
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS daily (
                    day TEXT PRIMARY KEY, uploaded INTEGER DEFAULT 0, downloaded INTEGER DEFAULT 0,
                    added INTEGER DEFAULT 0, seeded INTEGER DEFAULT 0, estimated INTEGER DEFAULT 0);
                CREATE TABLE IF NOT EXISTS seen (source TEXT, hash TEXT, seeded INTEGER,
                    PRIMARY KEY (source, hash));
                CREATE TABLE IF NOT EXISTS samples (ts INTEGER PRIMARY KEY, up REAL, down REAL,
                    seeding INTEGER, downloading INTEGER);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def capture(self, qb, torrents, source):
        now = time.time()
        with self.lock:
            if now - self.last_attempt < 60:
                return
            self.last_attempt = now
        response = qb.request("GET", "/api/v2/sync/maindata", params={"rid": 0})
        if response is None or response.status_code != 200:
            self.error = "下载器统计暂时不可用"
            return
        state = response.json().get("server_state", {})
        required = ("alltime_ul", "alltime_dl", "up_info_speed", "dl_info_speed")
        if not all(key in state for key in required):
            self.error = "下载器未返回累计流量，暂无法采集报表"
            return
        self.record(now, state, torrents, source)
        self.error = None

    def record(self, now, state, torrents, source):
        """First observation is a baseline, never imported as today's traffic."""
        up, down = int(state["alltime_ul"]), int(state["alltime_dl"])
        if min(up, down) < 0:
            raise ValueError("Invalid cumulative counters")
        with self.lock, self.connect() as db:
            row = db.execute("SELECT value FROM state WHERE key='last'").fetchone()
            previous = json.loads(row[0]) if row else None
            started = db.execute("SELECT value FROM state WHERE key='started'").fetchone()
            if not started:
                db.execute("INSERT INTO state VALUES ('started', ?)", (str(now),))
            day = day_of(now)
            db.execute("INSERT OR IGNORE INTO daily(day) VALUES (?)", (day,))
            same_source = previous and previous["source"] == source
            if same_source and now > previous["ts"]:
                delta_up = up - previous["up"] if up >= previous["up"] else up
                delta_down = down - previous["down"] if down >= previous["down"] else down
                # Split across Shanghai midnight; gaps cannot be attributed more precisely.
                cursor, end = previous["ts"], now
                assigned_up = assigned_down = 0
                while cursor < end:
                    midnight = (datetime.fromtimestamp(cursor, TZ).replace(hour=0, minute=0, second=0, microsecond=0)
                                + timedelta(days=1)).timestamp()
                    segment_end = min(end, midnight)
                    fraction = (segment_end - previous["ts"]) / (end - previous["ts"])
                    target_up, target_down = round(delta_up * fraction), round(delta_down * fraction)
                    segment_day = day_of(cursor)
                    db.execute("INSERT OR IGNORE INTO daily(day) VALUES (?)", (segment_day,))
                    estimated = int(day_of(previous["ts"]) != day or end - previous["ts"] > 120
                                    or up < previous["up"] or down < previous["down"])
                    db.execute("UPDATE daily SET uploaded=uploaded+?, downloaded=downloaded+?, estimated=MAX(estimated,?) WHERE day=?",
                               (target_up-assigned_up, target_down-assigned_down, estimated, segment_day))
                    assigned_up, assigned_down = target_up, target_down
                    cursor = segment_end
            for torrent in torrents:
                thash = torrent.get("hash")
                if not thash:
                    continue
                completed = float(torrent.get("progress", 0)) >= 1
                old = db.execute("SELECT seeded FROM seen WHERE source=? AND hash=?", (source, thash)).fetchone()
                if old is None:
                    db.execute("INSERT INTO seen VALUES (?,?,?)", (source, thash, int(completed)))
                    if same_source:
                        db.execute("UPDATE daily SET added=added+1, seeded=seeded+? WHERE day=?", (int(completed), day))
                elif completed and not old[0]:
                    db.execute("UPDATE seen SET seeded=1 WHERE source=? AND hash=?", (source, thash))
                    db.execute("UPDATE daily SET seeded=seeded+1 WHERE day=?", (day,))
            db.execute("INSERT OR REPLACE INTO state VALUES ('last', ?)",
                       (json.dumps({"ts": now, "up": up, "down": down, "source": source}),))
            downloading = sum(float(t.get("progress", 0)) < 1 for t in torrents)
            db.execute("INSERT OR REPLACE INTO samples VALUES (?,?,?,?,?)",
                       (int(now), max(0, state["up_info_speed"]), max(0, state["dl_info_speed"]),
                        len(torrents)-downloading, downloading))
            db.execute("DELETE FROM samples WHERE ts < ?", (now - 86400,))
            db.execute("DELETE FROM daily WHERE day < ?", (day_of(now - 29*86400),))

    def today(self):
        with self.connect() as db:
            row = db.execute("SELECT * FROM daily WHERE day=?", (day_of(time.time()),)).fetchone()
            return dict(row) if row else {"uploaded": 0, "downloaded": 0, "added": 0, "seeded": 0}

    def cleanup(self, now=None):
        now = time.time() if now is None else now
        with self.connect() as db:
            db.execute("DELETE FROM samples WHERE ts < ?", (now - 86400,))
            db.execute("DELETE FROM daily WHERE day < ?", (day_of(now - 29*86400),))

    def report(self, days=7, hours=24, now=None):
        now = time.time() if now is None else now
        days, hours = max(1, min(days, 30)), max(1, min(hours, 24))
        self.cleanup(now)
        with self.connect() as db:
            started = db.execute("SELECT value FROM state WHERE key='started'").fetchone()
            last = db.execute("SELECT value FROM state WHERE key='last'").fetchone()
            start_day = day_of(now-(days-1)*86400)
            rows = {r["day"]: dict(r) for r in db.execute("SELECT * FROM daily WHERE day>=? ORDER BY day", (start_day,))}
            daily = []
            for offset in range(days-1, -1, -1):
                day = day_of(now-offset*86400)
                daily.append(rows.get(day, {"day": day, "uploaded": None, "downloaded": None,
                                           "added": None, "seeded": None, "estimated": 0}))
            samples = [dict(r) for r in db.execute("SELECT * FROM samples WHERE ts>=? ORDER BY ts", (now-hours*3600,))]
            # Preserve spikes when reducing long ranges; return per-bucket average and maximum.
            grouped = []
            bucket_size = max(60, hours*3600//480)
            buckets = {}
            for sample in samples:
                buckets.setdefault(sample["ts"]//bucket_size, []).append(sample)
            for bucket in buckets.values():
                grouped.append({"ts": bucket[-1]["ts"], "up": sum(r["up"] for r in bucket)/len(bucket),
                                "down": sum(r["down"] for r in bucket)/len(bucket),
                                "peak_up": max(r["up"] for r in bucket), "seeding": bucket[-1]["seeding"],
                                "downloading": bucket[-1]["downloading"]})
            latest = json.loads(last[0])["ts"] if last else None
            return {"days": days, "hours": hours, "daily": daily, "samples": grouped,
                    "started_at": float(started[0]) if started else None, "updated_at": latest,
                    "stale": latest is None or now-latest > 180, "error": self.error,
                    "peak_up": max((r["up"] for r in samples), default=0),
                    "average_up": sum(r["up"] for r in samples)/len(samples) if samples else 0,
                    "sample_interval_seconds": 60, "timezone": "Asia/Shanghai"}
