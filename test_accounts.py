"""Account parsing, refresh failure, daily snapshot and retention regression."""
import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from account_status import AccountMonitor, parse_account, parse_bonus
from metrics import day_of

SAMPLE = Path(__file__).with_name('myhr.html').read_text(encoding='utf-8')
BONUS = SAMPLE + '<p>你当前每小时能获取<b>21.008</b>个魔力值</p>'
SITE = {'id': 'school', 'name': 'BTSCHOOL', 'base_url': 'https://pt.btschool.club', 'cookie': 'secret-cookie'}


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.monitor = AccountMonitor(Path(self.temp.name) / 'account.json')
        self.config = {'sites': [dict(SITE)]}
        self.now = 1791777600

    def session(self, index=SAMPLE, bonus=BONUS):
        session = Mock()
        session.__enter__ = Mock(return_value=session)
        session.__exit__ = Mock(return_value=False)
        session.get.side_effect = lambda url, **kw: Mock(status_code=200, text=bonus if url.endswith('mybonus.php') else index)
        return session

    def test_header_assessment_and_bonus(self):
        stats = parse_account(SAMPLE)
        self.assertEqual(stats['username'], 'testuser')
        self.assertEqual(stats['uploaded'], '1.58 GB')
        self.assertEqual(stats['bonus'], '145.0')
        self.assertEqual((stats['seeding'], stats['leeching']), ('2', '1'))
        self.assertEqual(stats['connectable'], '是')
        self.assertEqual(stats['assessment']['deadline'], '2026-10-30 18:27:22')
        self.assertEqual(stats['assessment']['items'][0]['value'], '还需要 48.42 GB')
        self.assertEqual(parse_bonus(BONUS), '21.008')
        for parser in (parse_account, parse_bonus):
            with self.assertRaises(ValueError):
                parser('<form>登录</form>')
        with self.assertRaises(ValueError):
            parse_bonus(SAMPLE)

    def test_supplied_values_zeroes_and_infinite_ratio(self):
        sample = SAMPLE.replace('145.0', '2,446.4').replace('227.416', '66.265').replace('1.58 GB', '372.52 GB').replace('7.11 MB', '5.62 GB')
        stats = parse_account(sample)
        self.assertEqual((stats['bonus'], stats['ratio'], stats['uploaded'], stats['downloaded']), ('2,446.4', '66.265', '372.52 GB', '5.62 GB'))
        self.assertEqual(parse_account(sample.replace('66.265', 'Inf'))['ratio'], 'Inf')

    def test_two_hours_force_daily_once_restart_and_cookie_rotation(self):
        session = self.session()
        with patch('account_status.requests.Session', return_value=session), patch('account_status.time.time', return_value=self.now):
            self.monitor.refresh_due(self.config)
            self.monitor.refresh_due(self.config)
            self.assertEqual(session.get.call_count, 2)
            self.monitor.refresh_due(self.config, force=True)
            self.assertEqual(session.get.call_count, 4)
            snapshot = self.monitor.snapshot(self.config)['sites'][0]
            self.assertEqual(len(snapshot['daily_snapshots']), 1)
            self.assertFalse(snapshot['stale'])
            self.assertNotIn('secret-cookie', self.monitor.cache_path.read_text(encoding='utf-8'))
            restored = AccountMonitor(self.monitor.cache_path)
            restored.refresh_due(self.config)
            self.assertEqual(session.get.call_count, 4)
            self.config['sites'][0]['cookie'] = 'new-cookie'
            self.assertNotIn('stats', restored.snapshot(self.config)['sites'][0])
            restored.refresh_due(self.config)
            self.assertEqual(len(restored.snapshot(self.config)['sites'][0]['daily_snapshots']), 1)
        with patch('account_status.requests.Session', return_value=session), patch('account_status.time.time', return_value=self.now + 86400):
            restored.refresh_due(self.config)
            self.assertEqual(len(restored.snapshot(self.config)['sites'][0]['daily_snapshots']), 2)

    def test_failures_keep_cache_and_do_not_create_fake_daily_snapshot(self):
        session = self.session()
        with patch('account_status.requests.Session', return_value=session), patch('account_status.time.time', return_value=self.now):
            self.monitor.refresh_due(self.config)
        session.get.side_effect = requests.Timeout('secret-cookie should never leak')
        with patch('account_status.requests.Session', return_value=session), patch('account_status.time.time', return_value=self.now + 86400):
            self.monitor.refresh_due(self.config)
            result = self.monitor.snapshot(self.config)['sites'][0]
            self.assertEqual(result['stats']['uploaded'], '1.58 GB')
            self.assertEqual(result['bonus_per_hour'], '21.008')
            self.assertTrue(result['stale'])
            self.assertEqual(len(result['daily_snapshots']), 1)
            self.assertNotIn('secret-cookie', json.dumps(result))

    def test_daily_retention_boundary(self):
        with patch('account_status.requests.Session', return_value=self.session()):
            for offset in range(32):
                with patch('account_status.time.time', return_value=self.now + offset*86400):
                    self.monitor.refresh_due(self.config)
            with patch('account_status.time.time', return_value=self.now + 31*86400):
                history = self.monitor.snapshot(self.config)['sites'][0]['daily_snapshots']
                self.assertEqual(len(history), 30)
                self.assertEqual(history[-1]['day'], day_of(self.now+2*86400))

    def test_overlapping_refreshes_share_background_job(self):
        async def run():
            with patch('account_status.requests.Session', return_value=self.session()):
                self.monitor.trigger(self.config, True)
                original = self.monitor.task
                self.monitor.trigger(self.config, True)
                self.assertIs(self.monitor.task, original)
                await original
                self.assertFalse(self.monitor.snapshot(self.config)['refreshing'])
        asyncio.run(run())


if __name__ == '__main__':
    unittest.main()
