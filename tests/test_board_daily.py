import json
import os
import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from _util import CFG
from jobhunt.board_daily import DailyControl
from jobhunt.daily import RunLock


class DailyControls(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cfg = replace(CFG, data_root=Path(tmp.name))
        self.now = datetime(2026, 11, 8, 1, tzinfo=timezone.utc)
        self.control = DailyControl(self.cfg, 'unused', 'unused', now=lambda: self.now)

    def summary(self, at='2026-11-08T00:30:00+00:00', problems=None):
        folder = self.cfg.data_dir / 'logs'
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'latest.json').write_text(json.dumps({'at': at, 'finished_at': at, 'problems': problems or [], 'scored': 2}))

    def test_missing_run_can_be_started_after_seven(self):
        self.assertEqual(self.control.status()['phase'], 'not_run')
        self.assertTrue(self.control.status()['can_start'])

    def test_completed_today_disables_repeat_without_launching(self):
        self.summary()
        self.control.runner = lambda log: self.fail('must not launch')
        self.assertEqual(self.control.start()['phase'], 'completed')
        self.assertIsNone(self.control.thread)

    def test_bangkok_date_used_instead_of_utc(self):
        self.summary('2026-11-07T18:00:00+00:00')
        self.assertEqual(self.control.status()['phase'], 'completed')
        self.summary('2026-11-07T16:00:00+00:00')
        self.assertEqual(self.control.status()['phase'], 'not_run')

    def test_partial_run_can_retry(self):
        self.summary(problems=['score: proxy unavailable'])
        self.assertEqual(self.control.status()['phase'], 'partial')
        self.assertTrue(self.control.status()['can_start'])

    def test_concurrent_clicks_launch_only_one_background_job(self):
        release, entered = threading.Event(), threading.Event()
        calls = []
        def run(log):
            calls.append(1)
            log('discover: start')
            entered.set()
            release.wait(3)
            self.summary()
        self.control.runner = run
        self.assertEqual(self.control.start()['phase'], 'running')
        self.assertTrue(entered.wait(1))
        for _ in range(4):
            self.assertEqual(self.control.start()['phase'], 'running')
        self.assertEqual(len(calls), 1)
        release.set()
        self.control.thread.join(3)
        self.assertEqual(self.control.status()['phase'], 'completed')
        self.assertIn('discover: start', self.control.status()['lines'])

    def test_external_scheduled_run_blocks_button(self):
        lock = RunLock(self.cfg.data_dir / 'run.lock', pid=os.getpid())
        lock.acquire()
        self.assertEqual(self.control.start()['phase'], 'running')
        self.assertIsNone(self.control.thread)

    def test_lock_acquisition_is_exclusive(self):
        lock = RunLock(self.cfg.data_dir / 'run.lock')
        lock.acquire()
        with self.assertRaises(FileExistsError):
            RunLock(lock.path).acquire()

    def test_crash_reenables_retry(self):
        def run(log):
            raise RuntimeError('offline')
        self.control.runner = run
        self.control.start()
        self.control.thread.join(3)
        self.assertEqual(self.control.status()['phase'], 'failed')
        self.assertTrue(self.control.status()['can_start'])
        self.assertIn('offline', self.control.status()['error'])
