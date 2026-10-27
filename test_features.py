import unittest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient
import json
import time

from server import app, coordinator, hr_monitor, site_checkin_mgr

class NewFeaturesTest(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_accounts_endpoint_returns_cached_data_with_checkin(self):
        resp = self.client.get("/api/accounts")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("sites", data)
        self.assertIn("refresh_interval_seconds", data)
        self.assertEqual(data["refresh_interval_seconds"], 7200)

    def test_single_torrent_check_hr(self):
        with patch.object(hr_monitor, "refresh_due") as mock_refresh:
            mock_refresh.return_value = None
            resp = self.client.post("/api/torrents/abcdef1234567890abcdef1234567890abcdef12/check_hr")
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertTrue(data.get("ok"))
            self.assertIn("hr_status", data)
            self.assertIn("message", data)

    def test_site_checkin_status_and_trigger(self):
        # 1. Get checkin status
        resp = self.client.get("/api/sites/checkin_status")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data.get("ok"))
        self.assertIn("status", data)

        # 2. Trigger checkin on existing or mock site
        sites = coordinator.config.get("sites", [])
        if sites:
            site_id = sites[0]["id"]
            with patch.object(site_checkin_mgr, "checkin_site") as mock_ci:
                mock_ci.return_value = {
                    "site_id": site_id,
                    "site_name": sites[0].get("name", "BTSCHOOL"),
                    "date": "2026-10-14",
                    "status": "success",
                    "message": "签到成功：获得 33 点魔力值",
                    "bonus_earned": "33"
                }
                resp_post = self.client.post(f"/api/sites/{site_id}/checkin")
                self.assertEqual(resp_post.status_code, 200)
                post_data = resp_post.json()
                self.assertTrue(post_data.get("ok"))
                self.assertEqual(post_data["result"]["bonus_earned"], "33")

    def test_config_defaults_auto_checkin_to_true(self):
        for s in coordinator.config.get("sites", []):
            self.assertNotEqual(s.get("auto_checkin"), False, "auto_checkin should be True by default")

    def test_compute_torrent_score_and_compliance(self):
        from server import compute_torrent_score

        hr_cfg = {"hr_min_seed_hours": 20.0, "hr_min_ratio": 1.0}
        now = 1700000000

        # Case 1: Downloading torrent -> never compliant!
        t_dl = {
            "name": "Downloading Test",
            "progress": 0.45,
            "state": "downloading",
            "upspeed": 1000000,
            "size": 10 * (1024**3),
            "ratio": 0.1,
            "added_on": now - 3600,
            "seeding_time": 0
        }
        res_dl = compute_torrent_score(t_dl, hr_cfg, now)
        self.assertFalse(res_dl["is_compliant"])
        self.assertIn("未完成下载", res_dl["compliance_reason"])

        # Case 2: H&R 考察中 -> never compliant!
        t_hr = {
            "name": "HR Pending Test",
            "progress": 1.0,
            "state": "seeding",
            "upspeed": 0,
            "size": 20 * (1024**3),
            "ratio": 0.3,
            "added_on": now - 36000,
            "seeding_time": 36000,
            "hr_status": "考察中"
        }
        res_hr = compute_torrent_score(t_hr, hr_cfg, now)
        self.assertFalse(res_hr["is_compliant"])
        self.assertIn("考察中", res_hr["compliance_reason"])

        # Case 3: H&R 已达标 -> is_compliant is True
        t_passed = {
            "name": "HR Passed Test",
            "progress": 1.0,
            "state": "seeding",
            "upspeed": 0,
            "size": 35 * (1024**3),
            "ratio": 0.2,
            "added_on": now - 86400,
            "seeding_time": 86400,
            "hr_status": "已达标"
        }
        res_passed = compute_torrent_score(t_passed, hr_cfg, now)
        self.assertTrue(res_passed["is_compliant"])
        self.assertLess(res_passed["score"], 35.0, "Dead seed should have low score")
        self.assertEqual(res_passed["level"], "low")

        # Case 4: High speed hot seed -> high score (>75)
        t_hot = {
            "name": "Hot Seed Test",
            "progress": 1.0,
            "state": "seeding",
            "upspeed": 10 * 1024 * 1024, # 10 MB/s
            "num_seeds": 3,
            "num_leechs": 8,
            "size": 8 * (1024**3),
            "ratio": 2.5,
            "added_on": now - 1800, # 30 min ago
            "seeding_time": 1800,
            "hr_status": "已达标"
        }
        res_hot = compute_torrent_score(t_hot, hr_cfg, now)
        self.assertTrue(res_hot["is_compliant"])
        self.assertGreaterEqual(res_hot["score"], 75.0)
        self.assertEqual(res_hot["level"], "hot")

        # Case 5: High historical upload performance (even if momentarily 0 current speed)
        t_hist_strong = {
            "name": "Strong History Test",
            "progress": 1.0,
            "state": "seeding",
            "upspeed": 0, # momentarily idle
            "num_seeds": 10,
            "num_leechs": 2,
            "size": 10 * (1024**3),
            "uploaded": 45 * (1024**3), # 45 GB uploaded
            "ratio": 4.5,
            "added_on": now - 7200, # 2 hours
            "seeding_time": 7200,
            "hr_status": "已达标"
        }
        res_hist = compute_torrent_score(t_hist_strong, hr_cfg, now)
        self.assertTrue(res_hist["is_compliant"])
        self.assertGreaterEqual(res_hist["breakdown"]["history"], 20.0, "High historical upload should yield >= 20 history score")
        self.assertGreaterEqual(res_hist["score"], 45.0, "Strong historical performance seed should stay >= 45 and not be culled")

        # Case 6: Dead zombie seed with near-zero historical upload and 0 speed
        t_hist_weak = {
            "name": "Zombie History Test",
            "progress": 1.0,
            "state": "seeding",
            "upspeed": 0,
            "num_seeds": 60,
            "num_leechs": 0,
            "size": 30 * (1024**3),
            "uploaded": 50 * 1024 * 1024, # only 50 MB uploaded over 30h
            "ratio": 0.002,
            "added_on": now - 30 * 3600,
            "seeding_time": 30 * 3600,
            "hr_status": "已达标"
        }
        res_weak = compute_torrent_score(t_hist_weak, hr_cfg, now)
        self.assertTrue(res_weak["is_compliant"])
        self.assertLess(res_weak["breakdown"]["history"], 2.0)
        self.assertLess(res_weak["score"], 15.0, "Zombie seed should have extremely low score")
        self.assertEqual(res_weak["level"], "low")

    def test_cull_cycle_strictly_protects_non_compliant_torrents(self):
        from server import DiskAndCullEngine, compute_torrent_score

        mock_qb = MagicMock()
        mock_qb.delete_torrent.return_value = True

        cfg = {
            "downloader": {"category": "pt-brush", "save_path": "/tmp"},
            "cull": {
                "enable_score_cull": True,
                "cull_score_threshold": 35.0,
                "score_grace_hours": 1.0,
                "hr_min_seed_hours": 20.0,
                "hr_min_ratio": 1.0,
                "delete_files": True,
                "min_disk_free_gb": 15.0,
                "emergency_disk_free_gb": 5.0,
                "max_disk_used_percent": 88.0
            }
        }
        engine = DiskAndCullEngine(mock_qb, cfg)
        now = int(time.time())

        # Non-compliant low-score torrent (seeding only 5h, ratio 0.1, HR考察中)
        t_unpassed = {
            "hash": "unpassed_hash_1",
            "name": "Under Assessment",
            "progress": 1.0,
            "state": "seeding",
            "upspeed": 0,
            "num_seeds": 50,
            "num_leechs": 0,
            "size": 30 * (1024**3),
            "ratio": 0.1,
            "added_on": now - 5 * 3600,
            "seeding_time": 5 * 3600,
            "hr_status": "考察中"
        }
        # Compliant low-score torrent (seeding 25h, ratio 1.2, HR已达标, 0 speed)
        t_compliant_low = {
            "hash": "compliant_low_hash_2",
            "name": "Compliant Low Score",
            "progress": 1.0,
            "state": "seeding",
            "upspeed": 0,
            "num_seeds": 80,
            "num_leechs": 0,
            "size": 25 * (1024**3),
            "ratio": 1.2,
            "added_on": now - 25 * 3600,
            "seeding_time": 25 * 3600,
            "hr_status": "已达标"
        }

        mock_qb.get_torrents.return_value = [t_unpassed, t_compliant_low]

        # Mock disk as normal (not low)
        with patch.object(engine, "get_disk_status", return_value={"is_low": False, "is_emergency": False, "free_gb": 50.0, "used_pct": 50.0}):
            res = engine.run_cull_cycle(force=False)
            # The compliant low-score one should be deleted!
            # The unpassed one MUST NOT be deleted!
            deleted_hashes = [call[0][0] for call in mock_qb.delete_torrent.call_args_list]
            self.assertIn("compliant_low_hash_2", deleted_hashes)
            self.assertNotIn("unpassed_hash_1", deleted_hashes)

if __name__ == "__main__":
    unittest.main()
