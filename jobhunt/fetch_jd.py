"""Fetch the full job description for stored jobs that do not have one.

Only jobs that survived the pre-filter are fetched: a title-only filter is free,
and a filtered job never needs a description (WORKSPLIT step 5). One failure never
kills the run: the job is flagged ``jd_missing``, keeps its search snippet, and is
scored from that.

A job whose fetch already failed stays flagged and is not retried on every run,
so a dead posting does not eat the request budget.
"""

from __future__ import annotations

import json
import re

from . import prefilter, store
from .http import FetchError, HostStopped
from .sources import jobsdb, jobthai, linkedin
from .sources.companies import adapters as company_adapters

FETCHERS = {
    linkedin.SOURCE: (linkedin.posting_url, linkedin.parse_posting, "linkedin_posting"),
    jobsdb.SOURCE: (jobsdb.job_url, jobsdb.parse_job_page, "jobsdb_job_page"),
    jobthai.SOURCE: (jobthai.job_url, jobthai.parse_detail, "jobthai_detail"),
}
# Sources whose description comes from a detail call rather than the list call.
DETAIL_SOURCES = {"workday", "smartrecruiters", "greenhouse"}
SUPPORTED = set(FETCHERS) | DETAIL_SOURCES


def smartrecruiters_company_id(cfg, company_name: str | None) -> str | None:
    for entry in (cfg.get("companies", []) if cfg else []):
        if str(entry.get("name", "")).casefold() == str(company_name or "").casefold():
            return entry.get("smartrecruiters_id") or entry.get("company")
    return None


def greenhouse_board(cfg, company_name: str | None) -> str | None:
    for entry in (cfg.get("companies", []) if cfg else []):
        if str(entry.get("name", "")).casefold() == str(company_name or "").casefold():
            return entry.get("board")
    return None


def _field(job, key):
    """``sqlite3.Row`` supports ``row[key]`` and has no ``get``."""
    try:
        return job[key]
    except (KeyError, IndexError, TypeError):
        return None


def _loreal(job) -> bool:
    url = company_adapters.loreal_job_url(_field(job, "url") or "")
    return _field(job, "source") == "company_site" and bool(url)


def supports(job) -> bool:
    return _field(job, "source") in SUPPORTED or _loreal(job)


def fetch_one(fetcher, job, cfg=None) -> dict:
    """Return ``{jd_text, jd_source}`` for one job, or ``{jd_text: ''}``."""
    source = _field(job, "source")
    if source == "workday" and _field(job, "url"):
        response = fetcher.get(company_adapters.workday_detail_url(_field(job, "url")),
                               accept="application/json")
        if not response.ok:
            return {"jd_text": "", "jd_source": None, "error": f"HTTP {response.status}"}
        parsed = company_adapters.parse_workday_detail(json.loads(response.text))
        return {"jd_text": (parsed.get("jd_text") or "").strip(), "jd_source": "workday_detail",
                "salary_text": None}
    if source == "greenhouse":
        board = greenhouse_board(cfg, _field(job, "company"))
        if not board:
            return {"jd_text": "", "jd_source": None,
                    "error": "no greenhouse board for this company in profile.toml"}
        response = fetcher.get(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs/"
                               f"{_field(job, 'source_job_id')}", accept="application/json")
        if not response.ok:
            return {"jd_text": "", "jd_source": None, "error": f"HTTP {response.status}"}
        payload = json.loads(response.text)
        import html as _html
        from . import htmldom
        text = htmldom.to_text(_html.unescape(payload.get("content") or ""))
        return {"jd_text": text.strip(), "jd_source": "greenhouse_detail", "salary_text": None}
    if source == "smartrecruiters":
        company_id = smartrecruiters_company_id(cfg, _field(job, "company"))
        if not company_id:
            return {"jd_text": "", "jd_source": None,
                    "error": "no smartrecruiters company id in profile.toml"}
        url = (f"https://api.smartrecruiters.com/v1/companies/{company_id}/postings/"
               f"{_field(job, 'source_job_id')}")
        response = fetcher.get(url, accept="application/json")
        if not response.ok:
            return {"jd_text": "", "jd_source": None, "error": f"HTTP {response.status}"}
        parsed = company_adapters.parse_smartrecruiters_detail(json.loads(response.text))
        return {"jd_text": (parsed.get("jd_text") or "").strip(),
                "jd_source": "smartrecruiters_detail", "salary_text": None}
    if _loreal(job):
        response = fetcher.get(company_adapters.loreal_job_url(_field(job, "url")))
        if not response.ok:
            return {"jd_text": "", "jd_source": None, "error": f"HTTP {response.status}"}
        parsed = company_adapters.parse_loreal_detail(response.text)
        return {"jd_text": (parsed.get("jd_text") or "").strip(),
                "jd_source": parsed.get("jd_source"),
                "posted_at": parsed.get("posted_at"),
                "salary_text": None}
    if source not in FETCHERS:
        return {"jd_text": "", "jd_source": None}
    url_builder, parser, source_name = FETCHERS[source]
    response = fetcher.get(url_builder(_field(job, "source_job_id")))
    if not response.ok:
        return {"jd_text": "", "jd_source": None, "error": f"HTTP {response.status}"}
    parsed = parser(response.text)
    return {
        "jd_text": (parsed.get("jd_text") or "").strip(),
        "jd_source": parsed.get("jd_source") or source_name,
        "posted_at": parsed.get("posted_at"),
        "salary_text": parsed.get("salary_text"),
        "logo_source_url": parsed.get("logo_source_url"),
        "apply_url": parsed.get("apply_url"),
    }


def _remember_posted(conn, job, result) -> None:
    """Fill a missing post date from a detail page. Never invent one."""
    posted = result.get("posted_at")
    if not isinstance(posted, str) or not posted.strip() or _field(job, "posted_at"):
        return
    conn.execute(
        "UPDATE jobs SET posted_at = ? WHERE id = ? AND posted_at IS NULL",
        (posted.strip(), _field(job, "id")),
    )
    conn.commit()


def _drop_if_stale(conn, cfg, job, result) -> str | None:
    """Apply the age rule once a detail page reveals a date the list did not have.

    An unknown list date is kept. That is not a claim that the posting is current.
    """
    posted = result.get("posted_at")
    if not isinstance(posted, str) or not posted.strip() or _field(job, "posted_at"):
        return None
    profile = getattr(cfg, "profile", None) or {}
    hit = prefilter.date_hit(posted.strip(), profile)
    if not hit:
        return None
    store.insert_score(conn, _field(job, "id"), {
        "status": "prefiltered",
        "prefilter_rule": hit,
        "verdict": "FILTERED",
        "gate_reasons": [],
        "gate_questions": [],
        "created_at": store.utc_now(),
    })
    return hit


SCAN_LIMIT = 5000


def priority_key(row, overrides) -> tuple:
    """Most promising first: on-track titles, then newest posting, then oldest find.

    A run has a small request budget, so the order of the queue decides which jobs
    get scored at all.
    """
    title = row["title"] or ""
    on_track = 0 if any(regex.search(title) for regex in overrides) else 1
    posted = prefilter.parse_posted_at(row["posted_at"])
    posted_key = -posted.timestamp() if posted else float("inf")   # newest first, unknown last
    return (on_track, posted_key, str(row["first_seen_at"] or ""), row["id"])


def order_candidates(rows, profile) -> list:
    """Priority titles (key account, trainee, graduate, category ... M12) first."""
    return sorted(rows, key=lambda row: priority_key(row, prefilter.priority_patterns(profile)))


def run(cfg, conn, fetcher, limit: int | None = None, log=None,
        sources: list[str] | None = None, company: str | None = None) -> dict:
    """Fetch JDs for stored jobs that have none. Returns the run's counts.

    ``sources`` narrows the work to some adapters (``["jobsdb"]``), and ``limit``
    caps how many descriptions one run asks for. The politeness rules in the
    fetcher still apply on top, so the per-host cap is the real ceiling.
    """
    log = log or (lambda *a, **k: None)
    candidates = store.jd_candidates(conn, SCAN_LIMIT)
    if sources:
        wanted = {s.casefold() for s in sources}
        candidates = [row for row in candidates if row["source"].casefold() in wanted]
    if company:
        needle = company.casefold()
        candidates = [row for row in candidates
                      if needle in str(row["company"] or "").casefold()]
    jobs = order_candidates(candidates, cfg.profile)
    jobs = jobs[:limit] if limit is not None else jobs
    report = {"attempted": 0, "fetched": 0, "missing": 0, "skipped": 0, "by_source": {},
              "jobs": []}
    stopped_sources: set[str] = set()

    for job in jobs:
        # a stopped host skips only its own source: a LinkedIn 429 must not starve JobsDB
        if not supports(job) or job["source"] in stopped_sources:
            report["skipped"] += 1
            continue
        report["attempted"] += 1
        try:
            result = fetch_one(fetcher, job, cfg)
        except HostStopped as exc:
            log(f"jd fetch: {job['source']} stopped, its other jobs wait:", exc)
            stopped_sources.add(job["source"])
            report["skipped"] += 1
            continue
        except FetchError as exc:
            # one transport failure (DNS, timeout) is this job's, not the host's
            log(f"jd fetch failed for job {job['id']} ({job['source']}):", exc)
            report["skipped"] += 1
            continue

        source_counts = report["by_source"].setdefault(job["source"], {"fetched": 0, "missing": 0})
        _remember_posted(conn, job, result)
        stale = _drop_if_stale(conn, cfg, job, result)
        if stale:
            log(f"job {job['id']} prefiltered after the detail date: {stale}")
        if result.get("jd_text"):
            store.set_jd(conn, job["id"], result["jd_text"], result["jd_source"] or job["source"])
            if result.get("salary_text") and not job["salary_text"]:
                store.set_salary_text(conn, job["id"], result["salary_text"])
            if result.get("logo_source_url") and not job["logo_source_url"]:
                store.set_logo_source_url(conn, job["id"], result["logo_source_url"])
            if result.get("apply_url") and not job["apply_url"]:
                store.set_apply_url(conn, job["id"], result["apply_url"])
            report["fetched"] += 1
            source_counts["fetched"] += 1
            report["jobs"].append({"job_id": job["id"], "source": job["source"], "ok": True})
        else:
            store.mark_jd_missing(conn, job["id"], result.get("jd_source") or job["source"])
            report["missing"] += 1
            source_counts["missing"] += 1
            report["jobs"].append({"job_id": job["id"], "source": job["source"], "ok": False,
                                   "error": result.get("error")})
            log(f"jd missing for job {job['id']} ({job['source']})")
    return report
