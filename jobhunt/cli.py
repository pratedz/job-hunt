"""Command line: ``python3 -m jobhunt add|score|list|show``."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import config as config_mod
from . import coverage, fetch_jd, outcomes, pipeline, prefilter, score as score_mod, store
from .sources import gmail as gmail_source, status_mail
from .tracker import Tracker
from .vault import Vault
from . import validate as schema_check
from .http import HostStopped, PoliteFetcher
from . import llm as llm_mod
from .llm import FakeLLM
from .sources import gmail as gmail_source


def _load_config(args) -> config_mod.Config:
    return config_mod.load(
        root=args.root,
        profile_path=args.profile,
        rubric_path=args.rubric,
        facts_path=args.facts,
    )


_CONNECTIONS: list = []


def _open_db(cfg, args):
    """Open the database and remember it, so main() can always close it."""
    db = Path(args.db) if args.db else cfg.db_path
    conn = store.connect(db)
    _CONNECTIONS.append(conn)
    return conn


# --------------------------------------------------------------------------- #
# add
# --------------------------------------------------------------------------- #

def _ingest(cfg, conn, jobs: list[dict], now: str | None = None) -> dict:
    report = {"added": 0, "duplicates": 0, "prefiltered": 0, "filters": []}
    stamp = now or store.utc_now()
    for job in jobs:
        hit = prefilter.check(job, cfg.profile)
        job_id, created = store.add_job(conn, job, now=stamp)
        if not created:
            report["duplicates"] += 1
            continue
        report["added"] += 1
        if hit:
            report["prefiltered"] += 1
            report["filters"].append({"job_id": job_id, "title": job.get("title"), "rule": hit})
            store.insert_score(conn, job_id, {
                "status": "prefiltered",
                "prefilter_rule": hit,
                "verdict": "FILTERED",
                "gate_reasons": [],
                "gate_questions": [],
                "created_at": stamp,
            })
    return report


def cmd_add(cfg, args) -> int:
    conn = _open_db(cfg, args)

    if args.json:
        path = Path(args.json)
        try:
            jobs = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"error: cannot read {path}: {exc}", file=sys.stderr)
            return 2
        try:
            schema_check.validate(cfg.input_schema(), jobs)
        except schema_check.SchemaError as exc:
            print(f"error: {path} does not match schemas/job_input.schema.json: {exc}", file=sys.stderr)
            return 2
        report = _ingest(cfg, conn, jobs)
        print(f"add --json: {report['added']} added, {report['duplicates']} already known, "
              f"{report['prefiltered']} pre-filtered")
        for entry in report["filters"]:
            print(f"  prefilter {entry['job_id']}: {entry['rule']} - {entry['title']}")
        return 0

    if not args.url and not args.jd_file:
        print("error: pass --url, --jd-file or --json", file=sys.stderr)
        return 2
    if not args.title:
        print("error: --title is required for a single job (it is what the pre-filter matches)",
              file=sys.stderr)
        return 2

    job = {
        "source": args.source,
        "source_job_id": args.source_job_id,
        "url": args.url,
        "company": args.company,
        "title": args.title,
        "location": args.location,
        "posted_at": args.posted_at,
        "salary_text": args.salary_text,
        "email_snippet": args.email_snippet,
        "jd_text": None,
    }
    if args.jd_file:
        job["jd_text"] = Path(args.jd_file).read_text(encoding="utf-8")
    elif args.url:
        print("warning: no --jd-file, so there is no JD text; this job can only be scored as "
              "unknown. Fetching pages starts in M2.", file=sys.stderr)

    try:
        schema_check.validate(cfg.input_schema(), [job])
    except schema_check.SchemaError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if job["jd_text"]:
        job["jd_source"] = "jd_file"

    report = _ingest(cfg, conn, [job])
    print(f"add: {report['added']} added, {report['duplicates']} already known, "
          f"{report['prefiltered']} pre-filtered")
    for entry in report["filters"]:
        print(f"  prefilter {entry['job_id']}: {entry['rule']} - {entry['title']}")
    return 0


# --------------------------------------------------------------------------- #
# score
# --------------------------------------------------------------------------- #

def _fake_reply(prompt: str, job_ids: list[str]) -> dict:
    """Offline stand-in: everything unknown, so the pipeline can be exercised."""
    return {"jobs": [
        {
            "id": job_id,
            "facts": {
                "min_years_experience": None, "gpa_floor": None, "start_kind": "unknown",
                "start_date": None, "graduation_years": [], "degree_completed_required": None,
                "required_languages": [], "required_test_scores": [], "location_city": None,
                "location_country": None, "remote": False, "job_type": "unknown",
                "other_hard_requirements": [],
            },
            "love": {"score": None, "reason": "fake"},
            "good_at": {"score": None, "reason": "fake"},
            "needs": {"score": None, "reason": "fake"},
            "paid": {"salary_min": None, "salary_max": None, "currency": None,
                     "period": None, "stated": False, "reason": "fake"},
            "summary": "fake reply",
        }
        for job_id in job_ids
    ]}


def cmd_score(cfg, args) -> int:
    conn = _open_db(cfg, args)
    llm = FakeLLM([_fake_reply]) if args.fake else llm_mod.build(cfg.profile, cfg.schema_path)
    report = score_mod.run(cfg, conn, llm, limit=args.limit, outcomes=_outcomes_block(cfg, args))
    if report["considered"] == 0:
        print("score: nothing to do, every stored job already has a score")
        return 0
    print(f"score: {report['scored']} scored in {report['batches']} call(s), "
          f"{report['errors']} error(s), {report['considered']} considered")
    for verdict, count in sorted(report["by_verdict"].items()):
        print(f"  {verdict}: {count}")
    for entry in report["errors_detail"]:
        print(f"  error job {entry['job_ids']}: {entry['error']}")
    store.record_run(conn, "score", report)
    return 0 if report["errors"] == 0 else 1


# --------------------------------------------------------------------------- #
# discover / run
# --------------------------------------------------------------------------- #

def _fetcher(cfg) -> PoliteFetcher:
    def log_event(event):
        if event.get("event") == "wait":
            print(f"  waiting {event['seconds']}s before {event['host']}")
        elif event.get("event") == "host_stopped":
            print(f"  {event['host']} stopped: {event['reason']}", file=sys.stderr)
    return PoliteFetcher(cfg.profile, log=log_event)


def _logo_fetcher(cfg) -> PoliteFetcher:
    """The logo fetcher: image CDNs may take more requests than a search host.

    The copied limits apply only to this command. Daily search caps stay as configured.
    """
    import copy
    profile = copy.deepcopy(cfg.profile)
    limits = profile.setdefault("limits", {})
    per = dict(limits.get("per_host") or {})
    delay = dict(limits.get("delay_per_host") or {})
    for host, cap, wait in (
        ("image-service-cdn.seek.com.au", 500, 0.4),
        ("media.licdn.com", 150, 1.0),
    ):
        per[host] = max(int(per.get(host) or 0), cap)
        delay[host] = wait
    limits["per_host"] = per
    limits["delay_per_host"] = delay

    def log_event(event):
        if event.get("event") == "host_stopped":
            print(f"  {event['host']} stopped: {event['reason']}", file=sys.stderr)
    return PoliteFetcher(profile, log=log_event)


def _run_gmail(cfg, days):
    """The read-only IMAP shell. Alert parsing arrives with real samples."""
    reader = gmail_source.GmailReader(cfg, log=print)
    try:
        messages = reader.fetch_messages(days)
    except (gmail_source.GmailError, OSError) as exc:
        print(f"gmail: {exc}", file=sys.stderr)
        return None
    print(f"gmail: {len(messages)} alert messages found (parsing starts when samples exist)")
    return messages


def _tracker(cfg, args) -> Tracker:
    vault_path = getattr(args, "vault", None) or cfg.vault_path
    return Tracker(Vault(vault_path, cfg.get("vault.records_dir", "61 Application Records")))


def _outcomes_block(cfg, args) -> str:
    """The "Recent outcomes" section for the scoring prompt."""
    try:
        tracker = _tracker(cfg, args)
    except Exception:
        return ""
    return outcomes.recent_outcomes_block(tracker, int(cfg.get("tracking.lessons_in_prompt", 10)))


def cmd_track(cfg, args) -> int:
    conn = _open_db(cfg, args)
    tracker = _tracker(cfg, args)
    if args.scan:
        reader = gmail_source.GmailReader(cfg, log=print)
        llm = llm_mod.build(cfg.profile, cfg.root / "schemas" / "status_mail.schema.json")
        report = status_mail.scan(cfg, tracker, conn, reader, llm)
        print(f"status scan: {report['read']} read, {report['matched']} matched, "
              f"{report['stored']} suggestions, {report['duplicates']} duplicates, "
              f"{report.get('ghosted', 0)} ghosted")
        for error in report["errors"]:
            print(f"  error: {error}")
    ghosted = status_mail.ghosted_suggestions(cfg, tracker, conn)
    print(f"ghosted suggestions: {ghosted} new, "
          f"{store.count_pending_suggestions(conn)} pending overall")
    return 0


def _source_names(cfg, choice: str | None) -> list[str]:
    """`all` means every source enabled in [sources]; a name means just that one."""
    if choice and choice != "all":
        return [choice]
    names = []
    if cfg.get("sources.linkedin", True):
        names.append("linkedin")
    if cfg.get("sources.jobsdb", True):
        names.append("jobsdb")
    if cfg.get("sources.companies", False):
        names.append("companies")
    if cfg.get("sources.jobthai", False):
        names.append("jobthai")
    return names


def _discover(cfg, args) -> int:
    names = _source_names(cfg, args.source)
    if args.dry_run:
        # Inspect historical queue/watch state without migrating or mutating the
        # on-disk database. All schema upgrades occur only in this memory copy.
        import sqlite3
        db = Path(args.db) if args.db else cfg.db_path
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        if db.exists():
            original = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
            try:
                original.backup(conn)
            finally:
                original.close()
        store.ensure_schema(conn)
        _CONNECTIONS.append(conn)
    else:
        conn = _open_db(cfg, args)

    if "gmail" in names:
        first_run = store.count_jobs(conn) == 0
        days = cfg.get("gmail.days_first_run", 14) if first_run else cfg.get("gmail.days_daily", 3)
        _run_gmail(cfg, days)
        names = [n for n in names if n != "gmail"]
    if not names:
        return 0

    fetcher = _fetcher(cfg)
    report = pipeline.discover(cfg, conn, fetcher, names, dry_run=args.dry_run, log=print,
                               only_company=getattr(args, "company", None))
    print(pipeline.format_report(report))
    if not args.dry_run:
        store.record_run(conn, "discover", report)
    if args.dry_run:
        print("dry run: nothing was stored and no job descriptions were fetched")
    return 0


def cmd_discover(cfg, args) -> int:
    return _discover(cfg, args)


def cmd_run(cfg, args) -> int:
    """The whole daily job: health, discover, fetch, score, track, summary."""
    from . import daily

    job = daily.build(cfg, db_path=args.db, vault_path=getattr(args, "vault", None),
                      llm=None if getattr(args, "fake", False) else _default_llm(cfg),
                      log=print)
    summary = job.run()
    if summary.get("skipped"):
        print(f"run: {summary['skipped']}")
        return 0
    print(f"run: {summary.get('centre', 0)} in the centre, {summary.get('scored', 0)} scored, "
          f"{summary.get('suggestions_pending', 0)} suggestion(s) pending")
    for name, counts in (summary.get("sources") or {}).items():
        print(f"  {name}: found {counts['found']}, new {counts['new']}, "
              f"pre-filtered {counts['prefiltered']}, scored {counts['scored']}")
    for step in summary.get("skipped_steps", []):
        print(f"  skipped {step}")
    print(f"  log: {summary.get('log')}")
    if summary.get("exit_code"):
        return int(summary["exit_code"])
    return 0 if not summary.get("problems") else 1


def _default_llm(cfg):
    return llm_mod.build(cfg.profile, cfg.schema_path)


def cmd_logos(cfg, args) -> int:
    """Download company logos. ``--backfill`` covers every company the board shows."""
    from . import board_api, logos as logos_mod
    conn = _open_db(cfg, args)
    fetcher = _logo_fetcher(cfg)
    if args.retry_failed:
        with conn:
            cleared = conn.execute(
                "UPDATE logos SET failed_at = NULL WHERE file IS NULL").rowcount
        print(f"logos: cleared {cleared} remembered failures so this run can try again")
    if args.relink or args.check:
        tracker = _tracker(cfg, args)
        sources = logos_mod.logo_sources(cfg)
        if args.relink:
            report = logos_mod.relink(conn, sources, logos_mod.all_company_names(conn, tracker))
            print(f"logos relink (no requests): {report['checked']} companies checked, "
                  f"{report['linked']} linked, {report['updated']} updated, "
                  f"{report['own']} have their own logo, {report['unmatched']} without one")
        gaps = logos_mod.check(conn, tracker, sources)
        print(f"logos check: {len(gaps)} companies have a stored logo but show none")
        for gap in gaps:
            print(f"  {gap['company']}: {gap['jobs']} jobs, {gap['records']} records "
                  f"(file {gap['file']})")
        return 1 if gaps else 0
    if args.backfill:
        companies = board_api.surfaced_companies(conn, _tracker(cfg, args))
        report = logos_mod.backfill(cfg, conn, fetcher, companies, log=print,
                                    linkedin_budget=args.linkedin_budget)
        stored = report["stored"]
        share = (100.0 * report["with_logo"] / report["visible"]) if report["visible"] else 0.0
        print(f"logos backfill: {report['visible']} visible companies, "
              f"{report['with_logo']} with a real logo ({share:.0f}%); "
              f"already had {report['had_logo']}, new from a stored URL {stored['url']}, "
              f"from the company site {stored['domain']}, from a posting {stored['posting']}; "
              f"{report['deferred']} deferred (host capped or stopped)")
        if report["missing"]:
            print("no logo yet: " + ", ".join(str(name) for name in report["missing"]))
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    report = logos_mod.fetch_missing(cfg, conn, fetcher, limit=args.limit, log=print)
    domains = logos_mod.configured_domains(cfg)
    sites = logos_mod.fetch_domains(cfg, conn, fetcher, domains, limit=args.limit, log=print,
                                    assets=logos_mod.logo_assets(cfg))
    print(f"logos: {report['stored']} stored, {report['refused']} refused, "
          f"{report['deferred']} deferred (host capped or stopped), "
          f"{report['considered']} considered; company sites: {sites['stored']} stored, "
          f"{sites['refused']} refused, {sites['deferred']} deferred")
    return 0


def cmd_reprefilter(cfg, args) -> int:
    """Re-run today's pre-filter over pre-filtered jobs; release the ones that now pass."""
    from . import reprefilter as reprefilter_mod
    conn = _open_db(cfg, args)
    report = reprefilter_mod.run(cfg, conn, dry_run=args.dry_run)
    print(reprefilter_mod.format_report(report, dry_run=args.dry_run))
    return 0


def cmd_recompute(cfg, args) -> int:
    from . import recompute as recompute_mod
    conn = _open_db(cfg, args)
    report = recompute_mod.recompute(cfg, conn, dry_run=args.dry_run, log=print)
    print(f"recompute: {report['considered']} scored job(s) reviewed, "
          f"{report['changed']} {'would change' if args.dry_run else 'updated'}, "
          f"{report['unchanged']} unchanged, {report['paid_changed']} paid change(s)")
    for change in report["verdict_changes"]:
        print(f"  job {change['job_id']} {change['company']}: "
              f"{change['from']} -> {change['to']}")
    for error in report["errors"]:
        print(f"  error: {error}")
    if args.dry_run:
        print("dry run: nothing was written")
    return 0


def cmd_backfill(cfg, args) -> int:
    from . import backfill
    conn = _open_db(cfg, args)
    report = backfill.run(cfg, conn, fetcher=_fetcher(cfg), dry_run=args.dry_run,
                          limit=args.limit, log=print)
    print(f"backfill: {report['updated']} job(s) "
          f"{'would be ' if args.dry_run else ''}updated, {report['skipped']} unchanged, "
          f"{len(report['errors'])} error(s)")
    if args.dry_run:
        print("dry run: nothing was written")
    return 0


def cmd_cover(cfg, args) -> int:
    from . import cover as cover_mod
    conn = _open_db(cfg, args)
    row = store.board_job(conn, args.job_id)
    if row is None:
        print(f"error: no job {args.job_id}", file=sys.stderr)
        return 2
    candidate_name = cfg.get("candidate.name") or "Candidate"
    llm = FakeLLM([{
        "subject": "Application: " + (row["title"] or ""),
        "body": f"Dear Hiring Team,\n\n(a fake draft)\n\nBest regards,\n{candidate_name}",
        "language": "en", "facts_used": []}]) if args.fake else llm_mod.build(
            cfg.profile, cfg.cover_schema_path, effort="medium")
    try:
        draft = cover_mod.draft(cfg, row, row, llm)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"subject: {draft['subject']}")
    print(f"saved:   {draft['path']}")
    if draft["violations"]:
        print("violations:")
        for item in draft["violations"]:
            print(f"  - {item}")
    else:
        print("no violations")
    print()
    print(draft["body"])
    return 0


def cmd_board(cfg, args) -> int:
    from . import board_server
    board_server.serve(cfg, db_path=args.db, vault_path=args.vault,
                       open_browser=not args.no_open)
    return 0


def cmd_fetch_jd(cfg, args) -> int:
    conn = _open_db(cfg, args)
    fetcher = _fetcher(cfg)
    sources = None if args.source == "all" else [args.source]
    report = fetch_jd.run(cfg, conn, fetcher, limit=args.limit, log=print, sources=sources,
                          company=args.company)
    if not report["attempted"] and not report["skipped"]:
        print("fetch-jd: nothing to fetch, every stored job already has a description")
        return 0
    print(f"fetch-jd: {report['attempted']} attempted, {report['fetched']} fetched, "
          f"{report['missing']} missing, {report['skipped']} left for next run")
    for source, counts in sorted(report["by_source"].items()):
        print(f"  {source}: fetched {counts['fetched']}, missing {counts['missing']}")
    for host, stats in fetcher.stats().items():
        print(f"  {host}: {stats['requests']} requests"
              + (f", stopped: {stats['stopped']}" if stats.get("stopped") else ""))
    store.record_run(conn, "fetch-jd", report)
    left = len(store.jd_candidates(conn, fetch_jd.SCAN_LIMIT))
    print(f"  {left} job(s) still without a description")
    return 0


# --------------------------------------------------------------------------- #
# list / show
# --------------------------------------------------------------------------- #

def cmd_list(cfg, args) -> int:
    conn = _open_db(cfg, args)
    rows = store.ranked(conn, args.verdict)
    if not rows:
        print("no jobs yet; add some with `python3 -m jobhunt add`")
        return 0
    if args.json:
        out = []
        for rank, row in enumerate(rows, start=1):
            out.append({
                "rank": rank,
                "job_id": row["id"],
                "company": row["company"],
                "title": row["title"],
                "location": row["location"],
                "source": row["source"],
                "posted_at": row["posted_at"],
                "verdict": row["verdict"],
                "love": row["love"],
                "good_at": row["good_at"],
                "needs": row["needs"],
                "paid": row["paid"],
                "weakest_circle": row["weakest_circle"],
                "mean": row["mean_score"],
                "gate": row["gate_result"],
                "questions": store.load_json(row["gate_questions_json"]) or [],
            })
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    print(f"{'#':>3}  {'verdict':<8} {'L G N P':<8} {'company':<22} {'title':<46} gate")
    for rank, row in enumerate(rows, start=1):
        badge = row["gate_result"] or "-"
        questions = store.load_json(row["gate_questions_json"]) or []
        if questions:
            badge = f"{badge} ?{len(questions)}"
        print(f"{rank:>3}  {row['verdict']:<8} {score_mod.render_scores(row):<8} "
              f"{(row['company'] or '-')[:22]:<22} {(row['title'] or '-')[:46]:<46} {badge}")
    return 0


def cmd_show(cfg, args) -> int:
    conn = _open_db(cfg, args)
    job = store.get_job(conn, args.job_id)
    if job is None:
        print(f"error: no job {args.job_id}", file=sys.stderr)
        return 2
    row = store.current_score(conn, args.job_id)
    print(f"job {job['id']}: {job['title']}")
    print(f"company: {job['company'] or '-'}   location: {job['location'] or '-'}   source: {job['source']}"
          f"   posted: {job['posted_at'] or '-'}")
    print(f"url: {job['url'] or '-'}")
    print(f"seen: first {job['first_seen_at']}  last {job['last_seen_at']}"
          f"   jd_missing: {job['jd_missing']}")
    if row is None:
        print("not scored yet")
        return 0
    if row["status"] == "prefiltered":
        print(f"pre-filtered, no codex call: {row['prefilter_rule']}")
        return 0
    if row["status"] == "error":
        print(f"error: {row['error']}")
        return 0
    print(f"verdict: {row['verdict']}   weakest: {row['weakest_circle']} ({row['weakest_score']})"
          f"   mean: {row['mean_score']}")
    print(f"gate: {row['gate_result']}")
    for reason in store.load_json(row["gate_reasons_json"]) or []:
        print(f"  fail: {reason}")
    for question in store.load_json(row["gate_questions_json"]) or []:
        print(f"  ask:  {question}")
    for circle in score_mod.CIRCLES:
        value = row[circle]
        print(f"{circle:>8}: {'?' if value is None else value}  {row[circle + '_reason'] or ''}")
    if row["paid_estimated"]:
        print("paid: estimated (est.)")
    print(f"summary: {row['summary'] or '-'}")
    print(f"facts: {json.dumps(store.load_json(row['facts_json']), ensure_ascii=False)}")
    return 0


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #

def cmd_coverage(cfg, args):
    """Offline inspection; an existing database is opened read-only, never migrated."""
    import sqlite3
    db = Path(args.db) if args.db else cfg.db_path
    if db.exists():
        conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
    else:
        conn = store.connect_memory()
    try:
        data = coverage.report(cfg, conn)
        if args.job is not None:
            exists = conn.execute("SELECT 1 FROM sqlite_master WHERE name='job_sightings'").fetchone()
            data["job_id"] = args.job
            data["sightings"] = store.job_sightings(conn, args.job) if exists else []
        if args.json:
            print(json.dumps(data, ensure_ascii=False, indent=2))
        else:
            print(coverage.format_report(data))
            for item in data.get("sightings", []):
                print(json.dumps(dict(item), ensure_ascii=False))
        return 0
    finally:
        conn.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jobhunt", description="job hunt — local AI job search & Career Wiki")
    parser.add_argument("--root", help="project root (default: the job-hunt directory)")
    parser.add_argument("--db", help="sqlite path (default: data/jobhunt.sqlite)")
    parser.add_argument("--profile", help="profile.toml path")
    parser.add_argument("--rubric", help="rubric.md path")
    parser.add_argument("--facts", help="facts.md path")
    sub = parser.add_subparsers(dest="command", required=True)

    checks = sub.add_parser("coverage", help="offline employer/source coverage and job sightings")
    checks.add_argument("--json", action="store_true")
    checks.add_argument("--job", type=int, help="show source URLs seen for this stored job")
    checks.set_defaults(func=cmd_coverage)

    add = sub.add_parser("add", help="store jobs")
    add.add_argument("--json", help="a JSON array of jobs (schemas/job_input.schema.json)")
    add.add_argument("--url")
    add.add_argument("--jd-file")
    add.add_argument("--company")
    add.add_argument("--title")
    add.add_argument("--location")
    add.add_argument("--source", default="manual")
    add.add_argument("--source-job-id", dest="source_job_id")
    add.add_argument("--posted-at", dest="posted_at")
    add.add_argument("--salary-text", dest="salary_text")
    add.add_argument("--email-snippet", dest="email_snippet")
    add.set_defaults(func=cmd_add)

    discover = sub.add_parser("discover", help="find new jobs from LinkedIn, JobsDB, and JobThai")
    discover.add_argument("--source",
                          choices=["linkedin", "jobsdb", "jobthai", "companies", "gmail", "all"],
                          default="all")
    discover.add_argument("--company", help="only this target company (career sites)")
    discover.add_argument("--dry-run", action="store_true", dest="dry_run",
                          help="search but store nothing and fetch no descriptions")
    discover.set_defaults(func=cmd_discover)

    run = sub.add_parser("run", help="the daily job: discover, fetch, score, track")
    run.add_argument("--vault")
    run.add_argument("--fake", action="store_true", help="no model: scoring is skipped")
    run.set_defaults(func=cmd_run)

    logos = sub.add_parser("logos", help="download company logos for stored jobs")
    logos.add_argument("--limit", type=int, default=30)
    logos.add_argument("--backfill", action="store_true",
                       help="every company on a feed tab or an application tab, cheapest path first")
    logos.add_argument("--retry-failed", action="store_true",
                       help="try logo downloads again inside the 7-day failure wait")
    logos.add_argument("--linkedin-budget", type=int, default=45,
                       help="LinkedIn posting fetches allowed in a backfill")
    logos.add_argument("--vault", help="vault path for To apply / Applied (read only)")
    logos.add_argument("--json", action="store_true", help="print the full report")
    logos.add_argument("--relink", action="store_true",
                       help="attach stored site icons to every matching company (no requests)")
    logos.add_argument("--check", action="store_true",
                       help="list companies with a stored logo that the board does not show")
    logos.set_defaults(func=cmd_logos)

    recompute = sub.add_parser("recompute",
                               help="re-apply gates and Paid to stored scores (no model)")
    recompute.add_argument("--dry-run", action="store_true", dest="dry_run")
    recompute.set_defaults(func=cmd_recompute)

    reprefilter = sub.add_parser("reprefilter",
                                 help="re-run the pre-filter; release jobs that now pass (no model)")
    reprefilter.add_argument("--dry-run", action="store_true", dest="dry_run")
    reprefilter.set_defaults(func=cmd_reprefilter)

    backfill = sub.add_parser("backfill", help="fill company/title on URL-only pastes")
    backfill.add_argument("--dry-run", action="store_true", dest="dry_run")
    backfill.add_argument("--limit", type=int)
    backfill.set_defaults(func=cmd_backfill)

    track = sub.add_parser("track", help="scan employer emails and ghosted applications")
    track.add_argument("--scan", action="store_true", help="read the mailbox (read-only)")
    track.add_argument("--vault")
    track.set_defaults(func=cmd_track)

    cover = sub.add_parser("cover", help="draft a cover email for one job")
    cover.add_argument("job_id", type=int)
    cover.add_argument("--fake", action="store_true", help="no codex call")
    cover.set_defaults(func=cmd_cover)

    board = sub.add_parser("board", help="serve the local board on 127.0.0.1")
    board.add_argument("--vault", help="vault path (default: [vault] path from profile.toml)")
    board.add_argument("--no-open", action="store_true", dest="no_open",
                       help="do not open a browser")
    board.set_defaults(func=cmd_board)

    fetch = sub.add_parser("fetch-jd", help="fetch missing job descriptions only")
    fetch.add_argument("--source",
                       choices=["linkedin", "jobsdb", "jobthai", "greenhouse", "lever", "workday",
                                "smartrecruiters", "company_site", "all"], default="all")
    fetch.add_argument("--company", help="only this company's postings")
    fetch.add_argument("--limit", type=int, help="fetch at most N descriptions this run")
    fetch.set_defaults(func=cmd_fetch_jd)

    score = sub.add_parser("score", help="score every unscored job through codex")
    score.add_argument("--limit", type=int, help="score at most N jobs this run")
    score.add_argument("--fake", action="store_true", help="offline stand-in reply, no codex call")
    score.set_defaults(func=cmd_score)

    listing = sub.add_parser("list", help="ranked list")
    listing.add_argument("--verdict", help="CENTRE | HOLD | LOW | FILTERED")
    listing.add_argument("--json", action="store_true")
    listing.set_defaults(func=cmd_list)

    show = sub.add_parser("show", help="everything about one job")
    show.add_argument("job_id", type=int)
    show.set_defaults(func=cmd_show)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = _load_config(args)
    try:
        return args.func(cfg, args)
    finally:
        while _CONNECTIONS:
            _CONNECTIONS.pop().close()
