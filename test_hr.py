"""HR 解析、四小时缓存和非阻塞接口回归；不请求真实站点。"""
import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import httpx
import requests
import server


SAMPLE = (server.APP_DIR / "myhr.html").read_text(encoding="utf-8")
EMPTY = '<div id="outer"><h1>H&amp;R记录</h1>没有记录</div>'
SITE = {"id": "school", "name": "校园", "base_url": "https://pt.btschool.club", "cookie": "secret-cookie", "enabled": False}


class HRTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.monitor = server.HRMonitor(Path(self.temp.name) / "hr.json")
        self.config = {"sites": [dict(SITE)]}

    def response(self, body):
        return Mock(status_code=200, text=body, apparent_encoding="utf-8")

    def session(self, bodies, passed_bodies=None):
        session = Mock()
        pages = iter(bodies)
        passed_pages = iter(passed_bodies or [EMPTY])
        def get(url, **kwargs):
            if url.endswith("details.php"):
                value = format(int(kwargs["params"]["id"]), "040x")
                return self.response(f"<b>Hash码:</b>&nbsp;{value}")
            return self.response(next(passed_pages if kwargs["params"]["hrtype"] == "B" else pages))
        session.get.side_effect = get
        session.__enter__ = Mock(return_value=session)
        session.__exit__ = Mock(return_value=False)
        return session

    def test_real_sample_and_empty_and_invalid(self):
        parsed = server.parse_hr_page(SAMPLE)
        self.assertEqual(parsed["pages"], 1)
        self.assertEqual(len(parsed["records"]), 2)
        self.assertEqual(parsed["records"][0]["torrent_id"], "337930")
        self.assertEqual(parsed["records"][0]["seed_remaining"], "20:00:00")
        self.assertEqual(server.parse_hr_page(EMPTY)["records"], [])
        for invalid in ('<form>登录</form>', '<h1>H&amp;R记录</h1>服务器异常'):
            with self.assertRaises(ValueError):
                server.parse_hr_page(invalid)

    def test_four_hour_cache_persistence_and_account_change(self):
        session = self.session([SAMPLE])
        with patch.object(server.requests, "Session", return_value=session), patch.object(server.time, "time", return_value=200000):
            self.monitor.refresh_due(self.config)
            self.monitor.refresh_due(self.config)
            self.assertEqual(session.get.call_count, 4)
            restored = server.HRMonitor(self.monitor.cache_path)
            restored.refresh_due(self.config)
            self.assertEqual(session.get.call_count, 4, "重启继续使用缓存")
            site = restored.snapshot(self.config)["sites"][0]
            self.assertEqual(site["pending"], 2)
            self.assertFalse(site["stale"])
            self.assertNotIn("secret-cookie", self.monitor.cache_path.read_text(encoding="utf-8"))
            self.config["sites"][0]["cookie"] = "changed"
            self.assertNotIn("updated_at", restored.snapshot(self.config)["sites"][0])
        session = self.session([EMPTY])
        with patch.object(server.requests, "Session", return_value=session), patch.object(server.time, "time", return_value=214400):
            self.monitor.refresh_due({"sites": [SITE]})
            self.assertEqual(session.get.call_count, 2)

    def test_failure_keeps_last_result_and_does_not_retry_frequently(self):
        key = self.monitor.key(SITE)
        self.monitor.cache[key] = {"updated_at": 1, "last_attempt_at": 1, "pending": 2, "failed": 0, "records": []}
        session = self.session([])
        session.get.side_effect = requests.Timeout("secret-cookie must not leak")
        with patch.object(server.requests, "Session", return_value=session):
            self.monitor.refresh_due(self.config)
            self.monitor.refresh_due(self.config)
        entry = self.monitor.snapshot(self.config)["sites"][0]
        self.assertEqual(entry["pending"], 2)
        self.assertTrue(entry["stale"])
        self.assertIn("连接失败", entry["error"])
        self.assertNotIn("secret-cookie", json.dumps(entry))
        self.assertEqual(session.get.call_count, 1)

    def test_pagination_fetches_pending_and_passed_only(self):
        page_one = SAMPLE.replace('var maxpage=0', 'var maxpage=1')
        page_two = SAMPLE.replace('337930', '123456').replace('337934', '123457')
        session = self.session([page_one, page_two])
        with patch.object(server.requests, "Session", return_value=session):
            self.monitor.refresh_due(self.config)
        entry = self.monitor.snapshot(self.config)["sites"][0]
        self.assertEqual(entry["pending"], 4)
        self.assertEqual(session.get.call_count, 7)
        hr_calls = [call for call in session.get.call_args_list if call.args[0].endswith("myhr.php")]
        self.assertEqual([call.kwargs["params"]["hrtype"] for call in hr_calls], ["A", "A", "B"])
        self.assertEqual(session.get.call_args_list[1].kwargs["params"], {"hrtype": "A", "page": 1})
        self.assertEqual(entry["records"][-1]["status"], "考察中")

    def test_missing_cookie_and_unrelated_sites(self):
        config = {"sites": [{**SITE, "cookie": ""}, {**SITE, "base_url": "https://other.example"}]}
        with patch.object(server.requests, "Session") as session:
            self.monitor.refresh_due(config)
            session.assert_not_called()
        entries = self.monitor.snapshot(config)["sites"]
        self.assertEqual(len(entries), 1)
        self.assertIn("Cookie", entries[0]["error"])

    def test_only_pending_names_are_marked_without_changing_cached_torrents(self):
        self.monitor.cache[self.monitor.key(SITE)] = {"last_attempt_at": 1, "records": [
            {"name": "Emergency S02E02 2026", "status": "考察中"},
            {"name": "Failed title", "status": "未达标"},
        ]}
        torrents = [{"name": "Emergency.S02E02.2026", "tracker": "https://tracker.btschool.club/announce"},
                    {"name": "Failed title"}, {"name": "Unrelated"},
                    {"name": "Emergency S02E02 2026", "tracker": "https://other.example/announce"}]
        marked = self.monitor.mark_torrents(torrents, self.config)
        self.assertEqual([t["hr_pending"] for t in marked], [True, False, False, False])
        self.assertTrue(all("hr_pending" not in t for t in torrents))
        self.assertTrue(all(not t["hr_pending"] for t in self.monitor.mark_torrents(torrents, {"sites": []})))

    def test_successful_empty_refresh_removes_pending_marker(self):
        self.monitor.cache[self.monitor.key(SITE)] = {"last_attempt_at": 1, "records": [
            {"name": "Old title", "status": "考察中"}]}
        with patch.object(server.requests, "Session", return_value=self.session([EMPTY])):
            self.monitor.refresh_due(self.config)
        self.assertFalse(self.monitor.mark_torrents([{"name": "Old title"}], self.config)[0]["hr_pending"])

    def test_hash_matches_different_titles_and_overrides_same_title(self):
        torrent_hash = "dffd35bde03bee6fe9dc7949c9f4d6975de98445"
        self.monitor.cache[self.monitor.key(SITE)] = {"last_attempt_at": 1, "records": [
            {"name": "Just Getting Started 2017 1080p BluRay x264-DRONES", "status": "考察中", "hash": torrent_hash}]}
        torrents = [{"name": "Just.Getting.Started.2017.1080p.BluRay.x264-DRONES.mkv", "hash": torrent_hash.upper()},
                    {"name": "Just Getting Started 2017 1080p BluRay x264-DRONES", "hash": "0" * 40}]
        self.assertEqual([t["hr_pending"] for t in self.monitor.mark_torrents(torrents, self.config)], [True, False])

    def test_existing_hashes_reused_next_refresh(self):
        with patch.object(server.requests, "Session", return_value=self.session([SAMPLE])):
            self.monitor.refresh_due(self.config)
        self.monitor.cache[self.monitor.key(SITE)]["last_attempt_at"] -= server.HR_REFRESH_SECONDS
        session = self.session([SAMPLE])
        with patch.object(server.requests, "Session", return_value=session):
            self.monitor.refresh_due(self.config)
        self.assertEqual(session.get.call_count, 2, "已缓存 Hash 不重复访问详情页")

    def test_pending_to_passed_reuses_hash_and_replaces_badge(self):
        with patch.object(server.requests, "Session", return_value=self.session([SAMPLE])):
            self.monitor.refresh_due(self.config)
        records = self.monitor.snapshot(self.config)["sites"][0]["records"]
        torrent = {"hash": records[0]["hash"], "name": "Downloader name differs"}
        self.assertEqual(self.monitor.mark_torrents([torrent], self.config)[0]["hr_status"], "考察中")
        self.monitor.cache[self.monitor.key(SITE)]["last_attempt_at"] -= server.HR_REFRESH_SECONDS
        session = self.session([EMPTY], [SAMPLE])
        with patch.object(server.requests, "Session", return_value=session):
            self.monitor.refresh_due(self.config)
        entry = self.monitor.snapshot(self.config)["sites"][0]
        self.assertEqual((entry["pending"], entry["passed"]), (0, 2))
        marked = self.monitor.mark_torrents([torrent], self.config)[0]
        self.assertEqual(marked["hr_status"], "已达标")
        self.assertFalse(marked["hr_pending"])
        self.assertEqual(session.get.call_count, 2, "状态变更沿用已知 Hash")

    def test_pending_takes_priority_over_passed_during_page_race(self):
        self.monitor.cache[self.monitor.key(SITE)] = {"last_attempt_at": 1, "records": [
            {"name": "Torrent", "hash": "a" * 40, "status": "考察中"},
            {"name": "Torrent", "hash": "a" * 40, "status": "已达标"}]}
        self.assertEqual(self.monitor.mark_torrents([{"hash": "a" * 40}], self.config)[0]["hr_status"], "考察中")


class HRResponsivenessTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_fetch_does_not_block_status_api(self):
        with tempfile.TemporaryDirectory() as temp:
            monitor = server.HRMonitor(Path(temp) / "hr.json")
            def slow_refresh(config):
                time.sleep(0.3)
            with patch.object(server, "hr_monitor", monitor), patch.object(monitor, "refresh_due", slow_refresh), \
                 patch.object(server.coordinator, "ensure_status_refresh"), \
                 patch.object(server.coordinator, "config", {"sites": [SITE]}):
                task = asyncio.create_task(monitor.loop())
                try:
                    await asyncio.sleep(0.02)
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
                        started = time.monotonic()
                        response = await client.get("/api/status")
                        self.assertLess(time.monotonic() - started, 0.2)
                        self.assertEqual(response.status_code, 200)
                        self.assertIn("torrents", response.json())
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    await asyncio.sleep(0.35)


if __name__ == "__main__":
    unittest.main()
