"""One discovery run, in the order that saves requests and tokens:

1. collect from every enabled source,
2. dedupe and store,
3. pre-filter on the title (no JD needed),
4. fetch JDs only for the survivors,
5. score (``run`` only).

A source that stops (429/999/authwall) or fails contributes what it already found
and the run continues with the next one.
"""

from __future__ import annotations

from . import coverage, fetch_jd, prefilter, score as score_mod, store
from .sources import companies as company_sources
from .sources import jobsdb, jobthai, linkedin

SOURCES = {
    linkedin.SOURCE: linkedin,
    jobsdb.SOURCE: jobsdb,
    jobthai.SOURCE: jobthai,
    "companies": company_sources,
}
SOURCE_HOSTS = {
    linkedin.SOURCE: (linkedin.HOST,),
    jobsdb.SOURCE: ("th.jobsdb.com",),
    jobthai.SOURCE: (jobthai.HOST,),
    "companies": ("boards-api.greenhouse.io", "api.lever.co", "api.smartrecruiters.com",
                  "careers.loreal.com"),
}


def _days(cfg, first_run: bool) -> float:
    key = "prefilter.posted_within_days_first_run" if first_run else "prefilter.posted_within_days"
    return float(cfg.get(key, 3))


def days_for_source(cfg, conn, name: str) -> tuple[float, bool]:
    """The posting window, wider on the first run for a source that has nothing stored."""
    first = store.count_jobs(conn, name) == 0
    return _days(cfg, first), first


def search_budgets(cfg, fetcher) -> dict[str, int]:
    """Half of the LinkedIn cap goes to searches, so JD fetches always get some.

    LinkedIn's guest budget is small and shared by search and job pages; without a
    split, searching could spend the whole cap and no description would arrive.
    """
    cap = fetcher.cap_for(linkedin.HOST)
    budgets = {linkedin.SOURCE: max(1, cap // 2)}
    # JobThai search and job pages share www.jobthai.com. The search budget is the
    # configured keyword cap, and never more than half the host cap, so descriptions fit.
    # Omit the key while the source is off so an unchanged profile keeps the LinkedIn-only map.
    if cfg.get("sources.jobthai", False):
        jobthai_cap = fetcher.cap_for(jobthai.HOST)
        configured = int(cfg.get("jobthai.max_requests", 3) or 3)
        budgets[jobthai.SOURCE] = min(configured, max(1, jobthai_cap // 2))
    if cfg.get("coverage.enabled", False):
        budgets["jobsdb"] = max(1, fetcher.cap_for("th.jobsdb.com") // 2)
    return budgets


def discover(cfg, conn, fetcher, sources=None, dry_run: bool = False, log=None,
             fetch_descriptions: bool = True, now=None, budgets: dict | None = None,
             only_company: str | None = None) -> dict:
    log = log or (lambda *a, **k: None)
    # `None` means every source; an empty list means none (it used to mean every one,
    # which made an accidentally empty list run the whole discovery).
    requested = ["linkedin", "jobsdb", "jobthai", "companies"] if sources is None else sources
    names = [n for n in requested if n in SOURCES]
    report = {"sources": {}, "jd": {"attempted": 0, "fetched": 0, "missing": 0, "skipped": 0,
                                    "by_source": {}},
              "hosts": {}, "stopped_hosts": {}, "dry_run": dry_run}

    if budgets is None and cfg.get("coverage.enabled", False):
        budgets = search_budgets(cfg, fetcher)
    recovery_reports = []
    prepared: list[dict] = []
    report["companies"] = []
    for name in names:
        days, first_run = days_for_source(cfg, conn, name)
        counts = {"found": 0, "new": 0, "duplicates": 0, "prefiltered": 0,
                  "jd_fetched": 0, "host_stopped": 0, "scored": 0, "errors": 0,
                  "days": days, "first_run": first_run}
        log(f"{name}: last {days:g} days")
        log(f"{name}: searching ...")
        budget = (budgets or {}).get(name)
        try:
            if name == "companies":
                jobs, company_reports = company_sources.collect(cfg, conn, fetcher, log=log,
                                                                only=only_company, dry_run=dry_run)
                report["companies"] = company_reports
                if not dry_run:
                    coverage.record_direct(cfg, conn, company_reports, now=now)
            else:
                recovery, checks = ([], [])
                if name in coverage.BOARDS and not only_company:
                    cap = budget if budget is not None else fetcher.cap_for(coverage.BOARDS[name][0]) // 2
                    recovery, checks = coverage.collect(cfg, conn, fetcher, name,
                        search_cap=cap, dry_run=dry_run, now=now, log=log)
                    recovery_reports.extend(checks)
                    counts["errors"] += sum(c["status"] in ("error", "blocked") for c in checks)
                    counts["partial"] = sum(c["status"] == "partial" for c in checks)
                jobs = list(recovery)
                try:
                    jobs.extend(SOURCES[name].collect(cfg, fetcher, days, log=log, max_requests=budget))
                except Exception as exc:
                    counts["errors"] += 1
                    counts["error"] = f"{type(exc).__name__}: {exc}"
                    log(f"{name}: broad collector failed; retaining recovery jobs: {exc}")
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
            counts["errors"] = 1
            counts["error"] = reason
            log(f"{name}: collector failed, other sources continue: {reason}")
            report["sources"][name] = counts
            continue
        if name == "companies":
            counts["errors"] += sum(r["status"] == "error" for r in report["companies"])
            counts["partial"] = sum(r["status"] == "partial" for r in report["companies"])
        counts["found"] = len(jobs)
        log(f"{name}: {len(jobs)} jobs found")
        for job in jobs:
            job["posted_at"] = job.get("posted_at")
            prepared.append(job)
            # The date rule uses the very window this run collected with, so a
            # 14-day first run is not silently filtered down to 3 days.
            if dry_run:
                hit = prefilter.check(job, cfg.profile, now=now, window_days=days)
                if hit:
                    counts["prefiltered"] += 1
                continue
            hit = prefilter.check(job, cfg.profile, now=now, window_days=days)
            job_id, created = store.add_job(conn, job)
            if not created:
                counts["duplicates"] += 1
                continue
            counts["new"] += 1
            if hit:
                counts["prefiltered"] += 1
                store.insert_score(conn, job_id, {
                    "status": "prefiltered",
                    "prefilter_rule": hit,
                    "verdict": "FILTERED",
                    "gate_reasons": [],
                    "gate_questions": [],
                    "created_at": store.utc_now(),
                })
        report["sources"][name] = counts

    if not dry_run and fetch_descriptions:
        log("fetching job descriptions for the jobs that survived the pre-filter ...")
        report["jd"] = fetch_jd.run(cfg, conn, fetcher, log=log)
        log(f"job descriptions: {report['jd']['fetched']} fetched, "
            f"{report['jd']['missing']} missing, {report['jd']['skipped']} skipped")

    report["hosts"] = fetcher.stats()
    report["stopped_hosts"] = fetcher.stopped_hosts()
    for name, counts in report["sources"].items():
        counts["jd_fetched"] = (report.get("jd", {}).get("by_source", {}).get(name, {})
                                .get("fetched", 0))
        hosts = SOURCE_HOSTS.get(name, ())
        if any(host in report["stopped_hosts"] for host in hosts):
            counts["host_stopped"] = 1
    if cfg.get("coverage.enabled", False):
        report["coverage"] = coverage.report(cfg, conn, now=now)
        report["coverage_attempts"] = recovery_reports
        for warning in report["coverage"]["warnings"]:
            log("coverage warning:", warning)
    return report


def run(cfg, conn, fetcher, llm, sources=None, log=None, now=None,
        only_company: str | None = None, outcomes: str = "") -> dict:
    """``jobhunt run``: discover everything, then score what came in."""
    log = log or (lambda *a, **k: None)
    report = discover(cfg, conn, fetcher, sources=sources, log=log, now=now,
                      budgets=search_budgets(cfg, fetcher), only_company=only_company)
    report["score"] = score_mod.run(cfg, conn, llm, outcomes=outcomes)
    report["unscored_left"] = len(store.unscored_jobs(conn, 10_000))
    return report


def format_report(report: dict) -> str:
    """The per-source table the CLI prints."""
    lines = []
    for name, counts in report["sources"].items():
        jd = report.get("jd", {}).get("by_source", {}).get(name, {})
        lines.append(
            f"{name:<9} found {counts['found']:>3}  new {counts['new']:>3}  "
            f"pre-filtered {counts['prefiltered']:>3}  duplicates {counts['duplicates']:>3}  "
            f"JD fetched {jd.get('fetched', 0):>3}  JD missing {jd.get('missing', 0):>3}"
            f"   (last {counts['days']:g} days{', first run' if counts['first_run'] else ''})"
        )
    if report.get("companies"):
        lines.append(company_sources.format_reports(report["companies"]))
    stopped = report.get("stopped_hosts") or {}
    if stopped:
        lines.append("hosts stopped for this run: " + ", ".join(f"{h} ({why})" for h, why in stopped.items()))
    for host, stats in (report.get("hosts") or {}).items():
        lines.append(f"  {host}: {stats['requests']} requests"
                     + (f", stopped: {stats['stopped']}" if stats.get("stopped") else ""))
    return "\n".join(lines)
