"""M7: the daily job, its lock, its health checks, and the LaunchAgent files."""

import json
import os
import plistlib
import shutil
import subprocess
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from _fake import FakeClock, FakeTransport, fixture, search_page
from _util import CFG, memory_conn, profile
from _vault import TODAY, make_vault, scored_reply

from jobhunt import daily, store
from jobhunt.http import PoliteFetcher
from jobhunt.llm import FakeLLM
from jobhunt.tracker import Tracker

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 8, 7, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, start=NOW):
        self.time = start

    def __call__(self):
        return self.time

    def advance(self, **kwargs):
        self.time = self.time + timedelta(**kwargs)


def make_daily(**kwargs):
    tmp = tempfile.TemporaryDirectory()
    cfg = replace(CFG, data_root=Path(tmp.name), profile=profile(
        companies=[], **{"sources.gmail": False, "board.notify": False}))
    conn = memory_conn(kwargs.pop("test", None) or _DummyTest())
    vault = make_vault(tmp.name)
    tracker = Tracker(vault, today=lambda: TODAY)
    kwargs.setdefault("notify_runner", lambda *a, **k: None)
    kwargs.setdefault("probe", lambda host, port: True)
    kwargs.setdefault("keychain", lambda service, account: True)
    kwargs.setdefault("log", lambda *a: None)
    job = daily.Daily(cfg, conn, tracker, now=Clock(), **kwargs)
    return job, cfg, conn, tracker, tmp


class _DummyTest(unittest.TestCase):
    def runTest(self):
        pass


def fake_services(**kwargs):
    transport = FakeTransport({
        "seeMoreJobPostings": (200, search_page(("111", "Management Trainee")), {}),
        "jobPosting/111": (200, fixture("linkedin_guest_jobposting.html"), {}),
    })
    clock = FakeClock()
    fetcher = PoliteFetcher(profile(**{"limits.min_seconds_between_requests": 0}),
                            transport=transport, sleep=clock.sleep, now=clock.now)
    llm = FakeLLM([lambda prompt, job_ids: scored_reply(job_ids)])
    kwargs = {"fetcher": fetcher, "llm": llm}
    kwargs.update(kwargs)
    return kwargs


class RunOrder(unittest.TestCase):
    def setUp(self):
        services = fake_services()
        self.job, self.cfg, self.conn, self.tracker, self.tmp = make_daily(
            test=self, fetcher=services["fetcher"], llm=services["llm"])

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_steps_happen_in_order(self):
        summary = self.job.run()
        self.assertEqual(summary["steps"], ["discover", "fetch-jd", "score", "track", "logos", "logo-sites"])
        self.assertGreaterEqual(summary["scored"], 1)

    def test_latest_json_holds_the_summary(self):
        summary = self.job.run()
        latest = json.loads((self.cfg.data_dir / "logs" / "latest.json").read_text())
        self.assertEqual(set(latest) >= {"at", "sources", "scored", "centre"}, True)
        self.assertEqual(latest["at"], summary["at"])
        self.assertIn("linkedin", latest["sources"])

    def test_a_daily_log_line_per_step(self):
        self.job.run()
        day = self.job.now().date().isoformat()
        text = (self.cfg.data_dir / "logs" / f"run-{day}.log").read_text()
        for step in ("discover: start", "fetch-jd: start", "score: start", "track: start"):
            self.assertIn(step, text)

    def test_the_run_records_itself_for_the_board(self):
        self.job.run()
        last = store.latest_run(self.conn)
        self.assertEqual(last["command"], "run")
        self.assertIn("linkedin", json.loads(last["stats_json"])["sources"])


class Health(unittest.TestCase):
    def test_a_permission_error_asks_for_desktop_access(self):
        job, cfg, conn, tracker, tmp = make_daily(test=self, llm=FakeLLM([]))
        original = daily._readable

        def blocked(path):
            raise PermissionError("not allowed")

        daily._readable = blocked
        self.addCleanup(setattr, daily, "_readable", original)
        summary = job.run()
        self.assertEqual(summary["exit_code"], 2)
        self.assertTrue(summary["tcc_blocked"])
        self.assertIn("Files and Folders", summary["errors"][0])
        tmp.cleanup()

    def test_missing_selected_provider_cli_skips_scoring_but_still_discovers(self):
        services = fake_services()
        job, cfg, conn, tracker, tmp = make_daily(test=self, fetcher=services["fetcher"],
                                                  llm=services["llm"])
        job.cfg = replace(cfg, profile=profile(**{"llm.provider": "claude",
                                                  "llm.claude_path": "/nonexistent/claude"}))
        summary = job.run()
        self.assertIn("discover", summary["steps"])
        self.assertNotIn("score", summary["steps"])
        self.assertTrue(any("score" in step for step in summary["skipped_steps"]))
        tmp.cleanup()

    def test_a_missing_codex_binary_skips_scoring(self):
        services = fake_services()
        cfg_override = {"llm.codex_path": str(Path("/nonexistent/codex"))}
        job, cfg, conn, tracker, tmp = make_daily(test=self, fetcher=services["fetcher"],
                                                  llm=services["llm"])
        job.cfg = replace(cfg, profile=profile(**cfg_override))
        summary = job.run()
        self.assertNotIn("score", summary["steps"])
        tmp.cleanup()

    def test_no_keychain_item_skips_gmail_without_failing(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg = replace(CFG, data_root=Path(tmp.name), profile=profile(
            companies=[], **{"sources.gmail": True, "sources.linkedin": False,
                             "sources.jobsdb": False, "sources.companies": False}))
        self.addCleanup(lambda: None)
        conn = store.connect(Path(tmp.name) / "x.sqlite")
        self.addCleanup(conn.close)
        tracker = Tracker(make_vault(tmp.name), today=lambda: TODAY)
        health = daily.check_health(cfg, tracker.vault.root, probe=lambda h, p: True,
                                    keychain=lambda s, a: False)
        self.assertFalse(health["keychain"])
        self.assertTrue(any("Keychain" in note for note in health["notes"]))
        job = daily.Daily(cfg, conn, tracker, llm=None,
                          fetcher=PoliteFetcher(cfg.profile, transport=FakeTransport({}),
                                                sleep=FakeClock().sleep),
                          now=Clock(), probe=lambda h, p: True,
                          keychain=lambda s, a: False, notify_runner=lambda *a, **k: None,
                          log=lambda *a: None)
        summary = job.run()
        self.assertEqual(summary["exit_code"], 0)
        self.assertEqual(summary["errors"], [])
        self.assertNotIn("track: ", " ".join(summary["problems"]))


class Lock(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "run.lock"

    def lock(self, **kwargs):
        return daily.RunLock(self.path, now=lambda: NOW, **kwargs)

    def test_no_lock_means_no_block(self):
        self.assertIsNone(self.lock().blocked_by())

    def test_a_live_lock_blocks(self):
        self.lock(pid=os.getpid()).acquire()
        reason = self.lock(pid=os.getpid()).blocked_by()
        self.assertIn("already running", reason)

    def test_a_lock_older_than_three_hours_is_stale(self):
        self.lock(pid=os.getpid()).acquire()
        later = daily.RunLock(self.path, now=lambda: NOW + timedelta(hours=3, minutes=1))
        self.assertIsNone(later.blocked_by())
        self.assertFalse(self.path.exists())

    def test_a_dead_pid_is_stale(self):
        self.path.write_text(json.dumps({"pid": 999999, "started_at": NOW.isoformat()}))
        self.assertIsNone(self.lock().blocked_by())

    def test_a_second_run_exits_cleanly_when_locked(self):
        job, cfg, conn, tracker, tmp = make_daily(test=self, llm=FakeLLM([]))
        job.lock_probe = None
        lock = daily.RunLock(cfg.data_dir / "run.lock", now=job.now, pid=os.getpid())
        lock.acquire()
        summary = job.run()
        self.assertIn("already running", summary["skipped"])
        self.assertEqual(summary["scored"], 0)
        tmp.cleanup()


class TimeBudget(unittest.TestCase):
    def test_no_new_work_after_the_budget(self):
        services = fake_services()
        job, cfg, conn, tracker, tmp = make_daily(test=self, fetcher=services["fetcher"],
                                                  llm=services["llm"], budget_minutes=0)
        job.now = lambda: NOW
        summary = job.run()
        self.assertEqual(summary["steps"], [])
        self.assertTrue(any("time budget" in step for step in summary["skipped_steps"]))
        tmp.cleanup()


class Notification(unittest.TestCase):
    def test_the_command_is_built_not_run(self):
        argv = daily.notification_argv("3 new in the centre · 12 scored")
        self.assertEqual(argv[0], "/usr/bin/osascript")
        self.assertIn("display notification", argv[2])
        self.assertIn("3 new in the centre", argv[2])
        self.assertIn('with title "my job hunt"', argv[2])

    def test_notify_respects_the_config_flag(self):
        calls = []
        cfg = replace(CFG, profile=profile(**{"board.notify": True}))
        self.assertTrue(daily.notify(cfg, "hello", runner=lambda argv, **k: calls.append(argv)))
        self.assertEqual(calls[0][0], "/usr/bin/osascript")
        cfg_off = replace(CFG, profile=profile(**{"board.notify": False}))
        self.assertFalse(daily.notify(cfg_off, "hello", runner=lambda argv, **k: calls.append(argv)))
        self.assertEqual(len(calls), 1)

    def test_the_summary_text(self):
        text = daily.summary_text({"centre": 3, "scored": 12, "suggestions_pending": 1,
                                   "problems": []})
        self.assertEqual(text, "3 new in the centre · 12 scored · 1 status update to confirm")
        self.assertEqual(daily.summary_text({"problems": ["boom"], "centre": 0, "scored": 0}),
                         "run finished with problems: see log")


class LaunchAgentFiles(unittest.TestCase):
    def setUp(self):
        self.plist = ROOT / "launchd" / "com.jobhunt.daily.plist"

    def test_the_plist_is_valid(self):
        with open(self.plist, "rb") as handle:
            data = plistlib.load(handle)
        self.assertEqual(data["Label"], "com.jobhunt.daily")
        self.assertEqual(data["ProgramArguments"], ["/usr/bin/env", "python3", "-m", "jobhunt", "run"])
        self.assertEqual(data["WorkingDirectory"], "__PROJECT_DIR__")
        self.assertEqual(data["StartCalendarInterval"], {"Hour": 7, "Minute": 0})
        self.assertFalse(data["RunAtLoad"])
        self.assertTrue(data["StandardOutPath"].startswith("__HOME__/Library/Logs/"))
        self.assertNotIn("/Desktop", data["StandardOutPath"])   # outside TCC's reach
        self.assertIn("__HOME__/.local/bin", data["EnvironmentVariables"]["PATH"])

    def test_plutil_lint_passes(self):
        if shutil.which("plutil") is None:
            self.skipTest("plutil not available")
        result = subprocess.run(["plutil", "-lint", str(self.plist)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_the_scripts_are_executable_and_never_run_here(self):
        for name in ("install.sh", "uninstall.sh"):
            path = ROOT / "launchd" / name
            self.assertTrue(os.access(path, os.X_OK), name)
        self.assertIn("launchctl bootstrap",
                      (ROOT / "launchd" / "install.sh").read_text())
        self.assertIn("launchctl bootout", (ROOT / "launchd" / "uninstall.sh").read_text())
        self.assertIn("Already loaded", (ROOT / "launchd" / "install.sh").read_text())

    def test_the_board_launcher(self):
        path = ROOT / "My Job Hunt.command"
        self.assertTrue(os.access(path, os.X_OK))
        text = path.read_text()
        self.assertIn("exec python3 -m jobhunt board", text)
        self.assertIn("Ctrl+C", text)


if __name__ == "__main__":
    unittest.main()


class BuildWiring(unittest.TestCase):
    """The crash that would have failed every 07:00 run: build() plus cmd_run's llm=."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = replace(CFG, data_root=Path(self.tmp.name),
                           profile=profile(**{"sources.gmail": False}))
        self.vault = make_vault(self.tmp.name)
        self.db = Path(self.tmp.name) / "run.sqlite"

    def test_build_accepts_a_caller_supplied_model(self):
        from jobhunt import llm as llm_mod
        stub = FakeLLM([])
        original = llm_mod.CodexLLM
        llm_mod.CodexLLM = lambda *a, **k: stub
        self.addCleanup(setattr, llm_mod, "CodexLLM", original)
        job = daily.build(self.cfg, db_path=self.db, vault_path=self.vault.root, llm=stub)
        self.addCleanup(job.conn.close)
        self.assertIsInstance(job, daily.Daily)
        self.assertIs(job.llm, stub)

    def test_build_makes_its_own_models_when_none_are_given(self):
        from jobhunt import llm as llm_mod
        made = []

        class Stub:
            def __init__(self, *args, **kwargs):
                made.append(kwargs)

        original = llm_mod.CodexLLM
        llm_mod.CodexLLM = Stub
        self.addCleanup(setattr, llm_mod, "CodexLLM", original)
        job = daily.build(self.cfg, db_path=self.db, vault_path=self.vault.root)
        self.addCleanup(job.conn.close)
        self.assertIsInstance(job.llm, Stub)
        self.assertIsInstance(job.mail_llm, Stub)
        self.assertEqual(len(made), 2)

    def test_the_cli_builds_a_daily_job_without_fake(self):
        from jobhunt import cli, llm as llm_mod
        stub = FakeLLM([])
        original_llm = llm_mod.CodexLLM
        llm_mod.CodexLLM = lambda *a, **k: stub
        self.addCleanup(setattr, llm_mod, "CodexLLM", original_llm)
        job = daily.build(self.cfg, db_path=self.db, vault_path=self.vault.root,
                          llm=cli._default_llm(self.cfg))
        self.addCleanup(job.conn.close)
        self.assertIsInstance(job, daily.Daily)
        self.assertIs(job.llm, stub)

    def run_cli(self, *args, summary=None):
        import io
        from contextlib import redirect_stdout
        from jobhunt import cli

        class StubJob:
            def run(self):
                return summary or {"at": "2026-11-08T00:00:00+00:00", "sources": {},
                                   "centre": 2, "scored": 5, "suggestions_pending": 1,
                                   "problems": [], "skipped_steps": [], "steps": [],
                                   "log": "data/logs/run-2026-11-08.log"}

        stub = FakeLLM([])
        original = cli._default_llm
        cli._default_llm = lambda cfg: stub
        self.addCleanup(setattr, cli, "_default_llm", original)
        original_build = daily.build
        daily.build = lambda *a, **k: StubJob()
        self.addCleanup(setattr, daily, "build", original_build)
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(["--db", str(self.db), "run", *args])
        return code, out.getvalue()

    def test_the_cli_run_path_without_fake(self):
        code, out = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("2 in the centre, 5 scored, 1 suggestion(s) pending", out)

    def test_the_cli_run_path_with_fake(self):
        code, out = self.run_cli("--fake")
        self.assertEqual(code, 0)
        self.assertIn("run:", out)

    def test_a_run_with_problems_exits_one(self):
        code, _ = self.run_cli(summary={"at": "x", "sources": {}, "centre": 0, "scored": 0,
                                        "suggestions_pending": 0, "problems": ["boom"],
                                        "skipped_steps": [], "steps": [], "log": "l"})
        self.assertEqual(code, 1)

    def test_the_fake_path_takes_the_daily_job_to_the_end(self):
        """`run --fake` end to end: no network (empty sources), no model, real files."""
        cfg = replace(self.cfg, profile=profile(companies=[], **{"sources.linkedin": False,
                                                   "sources.jobsdb": False,
                                                   "sources.companies": False,
                                                   "sources.gmail": False}))
        job = daily.build(cfg, db_path=self.db, vault_path=self.vault.root, llm=None,
                          notify_runner=lambda *a, **k: None, probe=lambda h, p: True,
                          keychain=lambda s, a: True, log=lambda *a: None)
        self.addCleanup(job.conn.close)
        summary = job.run()
        self.assertEqual(summary["errors"], [])
        self.assertTrue((cfg.data_dir / "logs" / "latest.json").exists())
        self.assertIn("score: no model available this run", summary["skipped_steps"][0])
        # every web source off: not even a logo request (M9)
        self.assertIn("logos: no web source enabled", summary["skipped_steps"])
        self.assertNotIn("logos", summary["steps"])


class EndToEnd(unittest.TestCase):
    """No mocked steps: the real discover, fetch-jd and score functions, fake I/O only."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = replace(CFG, data_root=Path(self.tmp.name), profile=profile(
            companies=[], **{"sources.gmail": False, "board.notify": False,
                             "sources.linkedin": True, "sources.jobsdb": False,
                             "sources.companies": False,
                             "search.linkedin_queries": [["management trainee", "Bangkok"]],
                             "search.linkedin_max_pages": 1,
                             "limits.min_seconds_between_requests": 0,
                             "prefilter.posted_within_days": 3650,
                             "prefilter.posted_within_days_first_run": 3650}))
        self.conn = store.connect(Path(self.tmp.name) / "run.sqlite")
        self.addCleanup(self.conn.close)
        self.vault = make_vault(self.tmp.name)
        self.tracker = Tracker(self.vault, today=lambda: TODAY)
        self.transport = FakeTransport({
            # "Graduate Programme" is deliberately not a vault record's program, so the
            # job stays a new candidate rather than appearing as already tracked
            "seeMoreJobPostings": (200, search_page(("111", "Graduate Programme"),
                                                    ("222", "Senior Key Account Manager")), {}),
            "jobPosting/111": (200, fixture("linkedin_guest_jobposting.html"), {}),
        })
        clock = FakeClock()
        self.fetcher = PoliteFetcher(self.cfg.profile, transport=self.transport,
                                     sleep=clock.sleep, now=clock.now)
        self.llm = FakeLLM([lambda prompt, job_ids: scored_reply(job_ids)])

    def job(self, **kwargs):
        kwargs.setdefault("notify_runner", lambda *a, **k: None)
        kwargs.setdefault("probe", lambda host, port: True)
        kwargs.setdefault("keychain", lambda service, account: True)
        kwargs.setdefault("log", lambda *a: None)
        return daily.Daily(self.cfg, self.conn, self.tracker, fetcher=self.fetcher,
                           llm=self.llm, now=Clock(), **kwargs)

    def test_the_real_pipeline_runs_inside_the_daily_job(self):
        summary = self.job().run()
        self.assertEqual(summary["errors"], [])
        self.assertEqual(summary["steps"], ["discover", "fetch-jd", "score", "track", "logos", "logo-sites"])
        self.assertEqual(summary["sources"]["linkedin"]["found"], 2)
        self.assertEqual(summary["sources"]["linkedin"]["new"], 2)
        self.assertEqual(summary["sources"]["linkedin"]["prefiltered"], 1)   # the senior role
        self.assertGreaterEqual(summary["scored"], 1)
        self.assertEqual(summary["centre"], 1)

    def test_the_discover_counts_reach_latest_json_and_the_board(self):
        summary = self.job().run()
        latest = json.loads((self.cfg.data_dir / "logs" / "latest.json").read_text())
        self.assertEqual(latest["sources"]["linkedin"]["found"], 2)
        self.assertEqual(latest["sources"]["linkedin"]["new"], 2)
        from jobhunt import board_api
        stats = board_api.stats(self.conn, self.tracker)
        self.assertEqual(stats["last_run"]["sources"]["linkedin"]["found"], 2)
        self.assertEqual(stats["tabs"]["centre"], 1)

    def test_a_second_run_adds_no_duplicates(self):
        self.job().run()
        second = self.job().run()
        self.assertEqual(second["sources"]["linkedin"]["new"], 0)
        self.assertEqual(store.count_jobs(self.conn), 2)

    def test_a_two_argument_log_line_does_not_crash_the_step(self):
        """The live bug: `log("source stopped:", exc)` has two arguments."""
        self.transport.add("seeMoreJobPostings2", 999, "")
        self.transport.routes.insert(0, ("seeMoreJobPostings", (429, "slow down", {})))
        summary = self.job().run()
        self.assertEqual(summary["errors"], [])          # reported, not raised
        self.assertIn("discover", summary["steps"])
