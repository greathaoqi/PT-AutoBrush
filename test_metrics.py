import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from metrics import MetricsStore, TZ


def timestamp(value):
    return datetime.fromisoformat(value).replace(tzinfo=TZ).timestamp()


def state(up, down, speed=100):
    return {"alltime_ul": up, "alltime_dl": down, "up_info_speed": speed, "dl_info_speed": 20}


class MetricsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "metrics.db"
        self.store = MetricsStore(self.path)
        self.t0 = timestamp("2026-10-12T12:00:00")

    def test_baseline_deltas_restart_and_counter_reset(self):
        self.store.record(self.t0, state(1000, 2000), [], "qb")
        report = self.store.report(now=self.t0)
        self.assertEqual(report["daily"][-1]["uploaded"], 0)
        self.assertIsNone(report["daily"][0]["uploaded"])
        self.store.record(self.t0+60, state(1200, 2300), [], "qb")
        self.store = MetricsStore(self.path)
        self.store.record(self.t0+120, state(1250, 2400), [], "qb")
        self.store.record(self.t0+180, state(10, 20), [], "qb")
        today = self.store.report(now=self.t0+180)["daily"][-1]
        self.assertEqual((today["uploaded"], today["downloaded"]), (260, 420))
        self.assertEqual(today["estimated"], 1)

    def test_cross_shanghai_midnight_preserves_total(self):
        before = timestamp("2026-10-12T23:59:30")
        self.store.record(before, state(1000, 2000), [], "qb")
        self.store.record(before+60, state(1201, 2301), [], "qb")
        daily = self.store.report(2, 24, before+60)["daily"]
        self.assertEqual(sum(row["uploaded"] for row in daily), 201)
        self.assertEqual(sum(row["downloaded"] for row in daily), 301)
        self.assertTrue(all(row["estimated"] for row in daily))
        self.assertEqual([row["day"] for row in daily], ["2026-10-12", "2026-10-13"])

    def test_initial_queue_not_new_then_completion_once_and_source_change(self):
        first = [{"hash": "existing", "progress": 1}, {"hash": "downloading", "progress": .5}]
        self.store.record(self.t0, state(1000, 2000), first, "qb")
        second = [{"hash": "existing", "progress": 1}, {"hash": "downloading", "progress": 1}, {"hash": "new", "progress": .2}]
        self.store.record(self.t0+60, state(1010, 2020), second, "qb")
        self.store.record(self.t0+120, state(1020, 2040), second, "qb")
        day = self.store.report(now=self.t0+120)["daily"][-1]
        self.assertEqual((day["added"], day["seeded"]), (1, 1))
        self.store.record(self.t0+180, state(100000, 200000), second, "other-downloader")
        day = self.store.report(now=self.t0+180)["daily"][-1]
        self.assertEqual(day["uploaded"], 20)
        self.assertEqual(day["added"], 1)

    def test_sampling_failure_is_not_a_zero_counter_and_minute_throttle(self):
        qb = Mock()
        qb.request.return_value = Mock(status_code=200, json=lambda: {"server_state": state(1000, 2000)})
        with patch("metrics.time.time", return_value=self.t0):
            self.store.capture(qb, [], "qb")
            self.store.capture(qb, [], "qb")
        self.assertEqual(qb.request.call_count, 1)
        qb.request.return_value = None
        with patch("metrics.time.time", return_value=self.t0+60):
            self.store.capture(qb, [], "qb")
        report = self.store.report(now=self.t0+60)
        self.assertEqual(report["updated_at"], self.t0)
        self.assertIsNotNone(report["error"])
        self.assertEqual(len(report["samples"]), 1)

    def test_aggregation_retains_peak_and_retention(self):
        for i in range(30):
            self.store.record(self.t0+i*60, state(1000+i, 2000+i, 900 if i == 3 else 100), [], "qb")
        report = self.store.report(7, 24, self.t0+30*60)
        self.assertEqual(report["peak_up"], 900)
        self.assertEqual(max(row["peak_up"] for row in report["samples"]), 900)
        self.assertLess(len(report["samples"]), 30)
        self.store.record(self.t0+31*86400, state(2000, 3000), [], "qb")
        self.assertEqual(len(self.store.report(now=self.t0+31*86400)["samples"]), 1)

    def test_exact_retention_boundaries_and_cleanup_without_capture(self):
        now = self.t0 + 40*86400
        with self.store.connect() as db:
            for offset in (29, 30, 31):
                from metrics import day_of
                db.execute('INSERT INTO daily(day) VALUES (?)', (day_of(now-offset*86400),))
            for offset in (86399, 86400, 86401):
                db.execute('INSERT INTO samples VALUES (?,?,?,?,?)', (int(now-offset), 1, 1, 1, 1))
        self.store.cleanup(now)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM daily').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM samples').fetchone()[0], 2)
        report = self.store.report(90, 168, now)
        self.assertEqual((report['days'], report['hours']), (30, 24))


if __name__ == "__main__":
    unittest.main()
