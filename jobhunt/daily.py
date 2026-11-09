"""The daily job: health check → discover → fetch descriptions → score → track.

Everything here is written so it can be tested without a network, a model, a mailbox
or a Mac: the clock, the TCP probe, the Keychain lookup, the notifier and every
service are injectable. The LaunchAgent only calls ``python3 -m jobhunt run``.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config as config_mod
from . import fetch_jd, llm as llm_mod, logos as logos_mod, outcomes, pipeline, score as score_mod, store
from .http import PoliteFetcher
from .sources import gmail as gmail_source, status_mail
from .tracker import Tracker
from .vault import Vault

TCC_MESSAGE = ("macOS blocked access to ~/Desktop: give python3 Files and Folders → Desktop "
               "access (or Full Disk Access) in System Settings → Privacy & Security")
LOCK_STALE_HOURS = 3
DEFAULT_BUDGET_MINUTES = 45


class HealthError(RuntimeError):
    """The run cannot start at all (privacy block, unreadable project)."""


# --------------------------------------------------------------------------- #
# health
# --------------------------------------------------------------------------- #

def _readable(path: Path) -> None:
    if path.is_dir():
        next(path.iterdir(), None)
    else:
        path.read_text(encoding="utf-8")[:1]


def check_health(cfg, vault_path=None, probe=None, keychain=None) -> dict:
    """(a) files readable, (b) selected AI CLI installed, (c) optional Gmail Keychain item."""
    report = {"tcc_blocked": False, "llm": True, "codex": True, "keychain": True,
              "notes": []}
    profile_file = (cfg.profile_path if cfg.profile_path.is_file()
                    else cfg.root / "config" / "profile.example.toml")
    vault_root = Path(vault_path) if vault_path else cfg.vault_path
    try:
        vault_root.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        report["tcc_blocked"] = True
        report["notes"].append(TCC_MESSAGE)
        raise HealthError(TCC_MESSAGE)
    except OSError:
        pass

    for path in (profile_file, vault_root):
        try:
            _readable(Path(path))
        except PermissionError:
            report["tcc_blocked"] = True
            report["notes"].append(TCC_MESSAGE)
            raise HealthError(TCC_MESSAGE)
        except OSError as exc:
            report["notes"].append(f"cannot read {path}: {exc}")
            raise HealthError(f"cannot read {path}: {exc}")

    llm_ok, llm_note = llm_mod.check_provider_health(cfg.profile)
    report["llm"] = llm_ok
    report["codex"] = llm_ok
    if not llm_ok:
        report["notes"].append(f"{llm_note}; scoring is skipped this run")

    keychain = keychain or _keychain_present
    report["keychain"] = bool(keychain(cfg.get("gmail.keychain_service", "jobhunt-gmail"),
                                       cfg.get("gmail.account", "")))
    if not report["keychain"]:
        report["notes"].append("no Gmail app password in the Keychain; the mailbox is skipped")
    return report


def _tcp_probe(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _keychain_present(service: str, account: str) -> bool:
    try:
        completed = subprocess.run(
            ["/usr/bin/security", "find-generic-password", "-s", service, "-a", account],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


# --------------------------------------------------------------------------- #
# lock
# --------------------------------------------------------------------------- #

class RunLock:
    """`data/run.lock` with the pid and start time; stale after three hours or a dead pid."""

    def __init__(self, path: Path, now=None, pid=None, hours: float = LOCK_STALE_HOURS):
        self.path = Path(path)
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.pid = pid if pid is not None else os.getpid()
        self.hours = hours

    def _alive(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True

    def read(self) -> dict | None:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def blocked_by(self) -> str | None:
        """A reason when another live run holds the lock, else None (and clear stale locks)."""
        data = self.read()
        if not data:
            return None
        try:
            started = datetime.fromisoformat(str(data.get("started_at")))
        except ValueError:
            started = None
        if started is not None and self.now() - started > timedelta(hours=self.hours):
            self.release()
            return None
        if not self._alive(int(data.get("pid") or 0)):
            self.release()
            return None
        return f"already running (pid {data.get('pid')}, since {data.get('started_at')})"

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("x", encoding="utf-8") as handle:
            json.dump({"pid": self.pid, "started_at": self.now().isoformat()}, handle)

    def release(self) -> None:
        try:
            self.path.unlink()
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# notification
# --------------------------------------------------------------------------- #

def notification_argv(text: str) -> list[str]:
    return ["/usr/bin/osascript", "-e",
            f'display notification {json.dumps(text)} with title "my job hunt"']


def notify(cfg, text: str, runner=None) -> bool:
    if not cfg.get("board.notify", False):
        return False
    runner = runner or subprocess.run
    try:
        runner(notification_argv(text), stdin=subprocess.DEVNULL, capture_output=True,
               text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def summary_text(summary: dict) -> str:
    if summary.get("problems"):
        return "run finished with problems: see log"
    parts = [f"{summary['centre']} new in the centre", f"{summary['scored']} scored"]
    pending = summary.get("suggestions_pending", 0)
    if summary.get("coverage_warnings"):
        parts.append(f"{len(summary['coverage_warnings'])} coverage warnings; see log")
    if pending:
        parts.append(f"{pending} status update{'s' if pending != 1 else ''} to confirm")
    return " · ".join(parts)


# --------------------------------------------------------------------------- #
# the run
# --------------------------------------------------------------------------- #

class Daily:
    def __init__(self, cfg, conn, tracker, fetcher=None, llm=None, mail_llm=None, reader=None,
                 now=None, probe=None, keychain=None, notify_runner=None, log=print,
                 budget_minutes: int = DEFAULT_BUDGET_MINUTES, logo_budget: int = 30):
        self.cfg = cfg
        self.conn = conn
        self.tracker = tracker
        self.fetcher = fetcher or PoliteFetcher(cfg.profile)
        self.llm = llm
        self.mail_llm = mail_llm
        self.reader = reader
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.probe = probe
        self.keychain = keychain
        self.notify_runner = notify_runner
        self.log_lines: list[str] = []
        self.budget = timedelta(minutes=budget_minutes)
        self.logo_budget = logo_budget
        self._log_sink = log

    # -- helpers ------------------------------------------------------------ #
    def line(self, *parts) -> None:
        """A log callback for the pipeline: it is called with one or two arguments."""
        text = " ".join(str(part) for part in parts)
        stamp = self.now().strftime("%H:%M:%S")
        self.log_lines.append(f"{stamp} {text}")
        self._log_sink(text)

    def logs_dir(self) -> Path:
        return self.cfg.data_dir / "logs"

    def write_logs(self, summary: dict) -> Path:
        directory = self.logs_dir()
        directory.mkdir(parents=True, exist_ok=True)
        day = self.now().date().isoformat()
        log_path = directory / f"run-{day}.log"
        with open(log_path, "a", encoding="utf-8") as handle:
            for text in self.log_lines:
                handle.write(text + "\n")
        (directory / "latest.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                                               encoding="utf-8")
        return log_path

    # -- steps -------------------------------------------------------------- #
    def run(self) -> dict:
        started = self.now()
        deadline = started + self.budget
        lock = RunLock(self.cfg.data_dir / "run.lock", now=self.now)
        blocked = lock.blocked_by()
        if blocked:
            self.line(f"skipped: {blocked}")
            return {"ok": True, "skipped": blocked, "problems": [], "centre": 0, "scored": 0}
        try:
            lock.acquire()
        except FileExistsError:
            return {"ok": True, "skipped": "another daily run is starting", "problems": [], "centre": 0, "scored": 0}
        summary = {"at": started.isoformat(), "sources": {}, "centre": 0, "scored": 0,
                   "errors": [], "problems": [], "skipped_steps": [], "steps": [],
                   "suggestions_pending": 0, "jd_fetched": 0, "exit_code": 0}
        try:
            self._run_steps(summary, deadline)
        finally:
            lock.release()
        summary["finished_at"] = self.now().isoformat()
        for error in summary["errors"]:
            self.line(f"problem: {error}")
        summary["problems"] = list(summary["errors"]) + list(summary["skipped_steps"])
        summary["suggestions_pending"] = store.count_pending_suggestions(self.conn)
        summary["notification"] = notify(self.cfg, summary_text(summary), self.notify_runner)
        self.line(f"done: {summary_text(summary)}")
        summary["log"] = str(self.write_logs(summary))
        store.record_run(self.conn, "run", summary)
        return summary

    def _budget_left(self, deadline) -> bool:
        return self.now() < deadline

    def _step(self, summary, name, fn, deadline):
        if not self._budget_left(deadline):
            summary["skipped_steps"].append(f"{name}: time budget used up")
            self.line(f"{name}: skipped, time budget used up")
            return None
        self.line(f"{name}: start")
        try:
            result = fn()
        except Exception as exc:                     # one step must not kill the run
            summary["errors"].append(f"{name}: {type(exc).__name__}: {exc}")
            self.line(f"{name}: failed ({type(exc).__name__}: {exc})")
            return None
        summary["steps"].append(name)
        return result

    def _run_steps(self, summary, deadline) -> None:
        try:
            health = check_health(self.cfg, self.tracker.vault.root, probe=self.probe,
                                  keychain=self.keychain)
        except HealthError as exc:
            summary["errors"].append(str(exc))
            summary["tcc_blocked"] = "~/Desktop" in str(exc)
            summary["exit_code"] = 2
            return
        for note in health["notes"]:
            self.line(f"health: {note}")

        can_use_model = health["llm"]
        mail_on = bool(self.cfg.get("sources.gmail", False)) and health["keychain"]

        sources = []
        if self.cfg.get("sources.linkedin", True):
            sources.append("linkedin")
        if self.cfg.get("sources.jobsdb", True):
            sources.append("jobsdb")
        if self.cfg.get("sources.companies", False):
            sources.append("companies")
        if self.cfg.get("sources.jobthai", False):
            sources.append("jobthai")

        discovery = self._step(summary, "discover",
                               lambda: pipeline.discover(
                                   self.cfg, self.conn, self.fetcher, sources,
                                   budgets=pipeline.search_budgets(self.cfg, self.fetcher),
                                   log=self.line), deadline)
        if discovery:
            summary["sources"] = {name: {"found": counts.get("found", 0),
                                         "new": counts.get("new", 0),
                                         "prefiltered": counts.get("prefiltered", 0),
                                         "scored": 0, "errors": counts.get("errors", 0),
                                         "host_stopped": counts.get("host_stopped", 0),
                                         "partial": counts.get("partial", 0)}
                                  for name, counts in discovery["sources"].items()}
            summary["jd_fetched"] = discovery.get("jd", {}).get("fetched", 0)
            if "coverage" in discovery:
                summary["coverage"] = discovery["coverage"]
                summary["coverage_warnings"] = discovery["coverage"]["warnings"]
            for source, counts in summary["sources"].items():
                if counts["errors"]:
                    summary["errors"].append(f"{source}: discovery errors={counts['errors']}, stopped={counts['host_stopped']}")

        fetched = self._step(summary, "fetch-jd",
                             lambda: fetch_jd.run(self.cfg, self.conn, self.fetcher,
                                                  log=self.line), deadline)
        if fetched:
            summary["jd_fetched"] = max(summary["jd_fetched"], fetched.get("fetched", 0))

        if can_use_model and self.llm is not None:
            scored = self._step(summary, "score",
                                lambda: score_mod.run(
                                    self.cfg, self.conn, self.llm,
                                    outcomes=outcomes.recent_outcomes_block(
                                        self.tracker,
                                        int(self.cfg.get("tracking.lessons_in_prompt", 10)))),
                                deadline)
            if scored:
                summary["scored"] = scored.get("scored", 0)
                for name, counts in (scored.get("by_source") or {}).items():
                    entry = summary["sources"].setdefault(
                        name, {"found": 0, "new": 0, "prefiltered": 0, "scored": 0, "errors": counts.get("errors", 0),
                                         "host_stopped": counts.get("host_stopped", 0),
                                         "partial": counts.get("partial", 0)})
                    entry["scored"] = counts.get("scored", 0)
                    entry["errors"] = counts.get("errors", 0)
        else:
            summary["skipped_steps"].append("score: no model available this run")
            self.line("score: skipped, no model available this run")

        tracked = self._step(summary, "track",
                             lambda: self._track(mail_on), deadline)
        if tracked:
            summary["ghosted"] = tracked.get("ghosted", 0)
            summary["track"] = tracked

        # A run with every web source off (``run --fake``, tests) makes no request at all.
        if not sources:
            summary["skipped_steps"].append("logos: no web source enabled")
            self.line("logos: skipped, no web source enabled")
        else:
            # Last, so logos never take time from scoring, and the logos found on posting
            # pages by fetch-jd are downloaded in the same run. Logo hosts are separate from
            # the search hosts, but the fetcher's per-host caps apply all the same.
            logos = self._step(summary, "logos",
                               lambda: logos_mod.fetch_missing(self.cfg, self.conn, self.fetcher,
                                                               limit=self.logo_budget,
                                                               log=self.line),
                               deadline)
            if logos:
                summary["logos"] = logos
                # configured companies' own site icons; each is fetched once, failures weekly
                domains = logos_mod.configured_domains(self.cfg)
                left = max(self.logo_budget - logos.get("considered", 0), 0)
                sites = self._step(summary, "logo-sites",
                                   lambda: logos_mod.fetch_domains(self.cfg, self.conn, self.fetcher,
                                                                   domains, limit=left,
                                                                   log=self.line), deadline)
                if sites:
                    summary["logos"]["sites"] = sites
                try:            # no request; never fails the run
                    summary["logos"]["relink"] = logos_mod.relink(
                        self.conn, logos_mod.logo_sources(self.cfg),
                        logos_mod.all_company_names(self.conn, self.tracker))
                except Exception as exc:
                    self.line(f"logos: relink failed ({type(exc).__name__}: {exc})")

        summary["centre"] = self._centre_count()

    def _centre_count(self) -> int:
        try:
            from . import board_api
            return board_api.stats(self.conn, self.tracker)["tabs"]["centre"]
        except Exception:
            return 0

    def _track(self, mail_on: bool) -> dict:
        result = {"ghosted": status_mail.ghosted_suggestions(self.cfg, self.tracker, self.conn)}
        if not mail_on:
            return result
        reader = self.reader or gmail_source.GmailReader(self.cfg, log=self.line)
        mail_llm = self.mail_llm or self.llm
        if mail_llm is None:
            return result
        scan = status_mail.scan(self.cfg, self.tracker, self.conn, reader, mail_llm)
        result.update({"read": scan["read"], "matched": scan["matched"],
                       "stored": scan["stored"]})
        for error in scan["errors"]:
            self.line(f"track: {error}")
        return result


def build(cfg, db_path=None, vault_path=None, **kwargs) -> Daily:
    """Wire the real services for `python3 -m jobhunt run`."""
    db = Path(db_path) if db_path else cfg.db_path
    conn = store.connect(db)
    vault = Vault(vault_path or cfg.vault_path,
                  cfg.get("vault.records_dir", "61 Application Records"))
    tracker = Tracker(vault)
    # setdefault, not a keyword: the caller may already have supplied (or deliberately
    # nulled) the models, and passing llm= twice crashed every real 07:00 run.
    kwargs.setdefault("llm", llm_mod.build(cfg.profile, cfg.schema_path))
    kwargs.setdefault("mail_llm",
                      llm_mod.build(cfg.profile, cfg.root / "schemas" / "status_mail.schema.json"))
    return Daily(cfg, conn, tracker, **kwargs)
