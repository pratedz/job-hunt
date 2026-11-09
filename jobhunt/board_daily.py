"""Non-blocking daily-run control for the local board."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import threading

from . import daily

BANGKOK = timezone(timedelta(hours=7))


class DailyControl:
    def __init__(self, cfg, db_path, vault_path, runner=None, now=None):
        self.cfg, self.db_path, self.vault_path = cfg, db_path, vault_path
        self.runner = runner
        self.now = now or (lambda: datetime.now(BANGKOK))
        self.mutex = threading.RLock()
        self.running = False
        self.lines = []
        self.error = None
        self.thread = None

    def status(self):
        with self.mutex:
            try:
                summary = json.loads((self.cfg.data_dir / 'logs/latest.json').read_text())
            except (OSError, ValueError):
                summary = None
            lock = daily.RunLock(self.cfg.data_dir / 'run.lock')
            info = lock.read()
            external = bool(info and lock._alive(int(info.get('pid', -1))))
            today = self.now().astimezone(BANGKOK).date().isoformat()
            finished_today = False
            if summary and summary.get('finished_at'):
                try:
                    finished_today = datetime.fromisoformat(summary['finished_at']).astimezone(BANGKOK).date().isoformat() == today
                except (ValueError, TypeError):
                    pass
            problems = (summary or {}).get('problems', []) or (summary or {}).get('errors', [])
            successful = bool(finished_today and not problems and not (summary or {}).get('exit_code'))
            phase = 'running' if self.running or external else 'failed' if self.error else 'completed' if successful else 'partial' if finished_today else 'not_run'
            return {'phase': phase, 'today': today, 'summary': summary,
                    'error': self.error, 'lines': list(self.lines[-30:]),
                    'can_start': phase not in ('running', 'completed')}

    def start(self):
        with self.mutex:
            status = self.status()
            if not status['can_start']:
                return status
            self.running = True
            self.error = None
            self.lines = ['Starting daily update…']
            self.thread = threading.Thread(target=self._work, daemon=True, name='jobhunt-daily')
            self.thread.start()
            return self.status()

    def _log(self, *parts):
        with self.mutex:
            self.lines.append(' '.join(str(p) for p in parts))
            self.lines = self.lines[-30:]

    def _work(self):
        job = None
        try:
            if self.runner:
                self.runner(self._log)
            else:
                job = daily.build(self.cfg, db_path=self.db_path, vault_path=self.vault_path, log=self._log)
                result = job.run()
                if result.get('skipped'):
                    self._log(result['skipped'])
        except Exception as exc:
            with self.mutex:
                self.error = f'{type(exc).__name__}: {exc}'
        finally:
            if job:
                job.conn.close()
            with self.mutex:
                self.running = False
