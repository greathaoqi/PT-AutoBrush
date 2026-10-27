"""回归测试：慢下载器不能阻塞状态/日志接口，不访问真实下载器或站点。"""
import asyncio
import time
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

import httpx
import server
from account_status import AccountMonitor


class ResponsivenessTests(unittest.IsolatedAsyncioTestCase):
    async def test_account_refresh_is_async_and_cached_reads_do_not_fetch(self):
        with tempfile.TemporaryDirectory() as temp:
            monitor = AccountMonitor(Path(temp) / 'accounts.json')
            calls = []
            def slow_fetch(config, force=False):
                calls.append(force)
                time.sleep(.3)
            with patch.object(server, 'account_monitor', monitor), patch.object(monitor, 'refresh_due', slow_fetch):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url='http://test') as client:
                    await client.get('/api/accounts')
                    self.assertEqual(calls, [])
                    started = time.monotonic()
                    response = await client.post('/api/accounts/refresh')
                    self.assertLess(time.monotonic()-started, .2)
                    self.assertTrue(response.json()['refreshing'])
                    await client.post('/api/accounts/refresh')
                    started = time.monotonic()
                    self.assertEqual((await client.get('/api/logs')).status_code, 200)
                    self.assertLess(time.monotonic()-started, .2)
                    await monitor.task
                    self.assertEqual(calls, [True])

    async def test_report_read_does_not_block_logs(self):
        def slow_report(days, hours):
            time.sleep(0.3)
            return {"daily": [], "samples": []}
        with patch.object(server.metrics_store, "report", slow_report):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
                report = asyncio.create_task(client.get("/api/reports?days=30&hours=24"))
                await asyncio.sleep(0.02)
                started = time.monotonic()
                logs = await client.get("/api/logs")
                self.assertLess(time.monotonic()-started, 0.2)
                self.assertEqual(logs.status_code, 200)
                self.assertEqual((await report).status_code, 200)

    async def test_scheduler_disk_io_does_not_block_logs(self):
        def slow_disk():
            time.sleep(0.3)
            return {"is_low": False}

        with patch.object(server.coordinator, "is_running", True), \
             patch.object(server.coordinator, "config", {"sites": []}), \
             patch.object(server.coordinator.cull_engine, "run_cull_cycle", lambda force: None), \
             patch.object(server.coordinator.cull_engine, "get_disk_status", slow_disk):
            task = asyncio.create_task(server.coordinator.main_loop())
            try:
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=server.app), base_url="http://test"
                ) as client:
                    await asyncio.sleep(0.02)
                    started = time.monotonic()
                    response = await client.get("/api/logs")
                    self.assertLess(time.monotonic() - started, 0.2)
                    self.assertEqual(response.status_code, 200)
                    self.assertIn("app;dur=", response.headers["server-timing"])
                    self.assertEqual(response.headers["x-pt-build"], "secure-console-20261009")
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await asyncio.sleep(0.35)

    async def test_cold_cache_and_single_background_refresh(self):
        calls = []

        def slow_refresh():
            calls.append(time.monotonic())
            time.sleep(0.3)
            server.coordinator.cached_status = {"engine_running": True, "torrents": []}
            server.coordinator.last_status_time = time.monotonic()

        async def idle_engine():
            await asyncio.Event().wait()

        with patch.object(server.coordinator, "cached_status", None), \
             patch.object(server.coordinator, "status_refresh_task", None), \
             patch.object(server.coordinator, "last_status_attempt", float("-inf")), \
             patch.object(server.coordinator, "refresh_status_cache", slow_refresh), \
             patch.object(server.account_monitor, "refresh_due", lambda *args: None), \
             patch.object(server.hr_monitor, "loop", idle_engine), \
             patch.object(server.coordinator, "main_loop", idle_engine):
            async with server.lifespan(server.app):
                await asyncio.sleep(0.02)
                self.assertEqual(calls, [], "启动时不预热，不访问状态接口就不刷新")
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=server.app), base_url="http://test"
                ) as client:
                    started = time.monotonic()
                    replies = await asyncio.gather(*[
                        client.get(path) for path in ["/api/status"] * 8 + ["/api/logs"]
                    ])
                    elapsed = time.monotonic() - started
                    self.assertLess(elapsed, 0.2)
                    self.assertTrue(all(r.status_code == 200 for r in replies))
                    self.assertTrue(all(not r.json()["status_ready"] for r in replies[:-1]))
                    self.assertEqual(len(calls), 1)
                    await asyncio.sleep(0.35)
                    self.assertTrue((await client.get("/api/status")).json()["status_ready"])
                    self.assertEqual(len(calls), 1)
                    print(f"9 requests during slow refresh: {elapsed * 1000:.1f} ms")
                    server.coordinator.last_status_time -= 10
                    server.coordinator.last_status_attempt -= 10
                    stale = await client.get("/api/status")
                    self.assertTrue(stale.json()["status_ready"])
                    await asyncio.sleep(0.02)
                    self.assertEqual(len(calls), 2, "缓存过期后按需刷新")
                    await asyncio.sleep(0.35)

    async def test_manual_sniff_does_not_block_logs(self):
        def slow_sniff(site):
            time.sleep(0.3)

        with patch.object(server.coordinator, "config", {"sites": [{"enabled": True}]}), \
             patch.object(server.coordinator, "sniff_site", slow_sniff):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=server.app), base_url="http://test"
            ) as client:
                sniff = asyncio.create_task(client.post("/api/engine/sniff"))
                await asyncio.sleep(0.02)
                started = time.monotonic()
                response = await client.get("/api/logs")
                self.assertLess(time.monotonic() - started, 0.2)
                self.assertEqual(response.status_code, 200)
                self.assertFalse(sniff.done())
                self.assertEqual((await sniff).status_code, 200)


if __name__ == "__main__":
    unittest.main()
