"""JobThai public search pages.

One HTML page per keyword. The job array is embedded JSON, not a documented API.
Descriptions come from the job page later, and only for titles that survive the
pre-filter. ``updatedAt`` on the list is an update stamp, so it is not stored as
``posted_at``.
"""

from __future__ import annotations

import json
import re
import urllib.parse

from ..http import FetchError, HostStopped
from .companies.adapters import jobposting_json, _role_text

SOURCE = "jobthai"
HOST = "www.jobthai.com"
SEARCH_URL = "https://www.jobthai.com/en/jobs?keyword={keyword}&orderBy=RELEVANCE_SEARCH"

DEFAULT_KEYWORDS = ("management trainee", "key account", "business development")
DEFAULT_LOCATIONS = (
    "Bangkok", "Nonthaburi", "Samut Prakan", "Pathum Thani",
)
DEFAULT_TITLE_INCLUDE = (
    "key account", "management trainee", "graduate", "business development",
    "e-commerce", "trade marketing",
)


def job_url(job_id: str) -> str:
    return f"https://www.jobthai.com/en/company/job/{job_id}"


def valid_job_url(url: str | None) -> bool:
    parts = urllib.parse.urlsplit(url or "")
    host = parts.netloc.casefold()
    if host not in {HOST, "jobthai.com"}:
        return False
    return re.fullmatch(r"/(en|th)/company/job/\d+", parts.path) is not None


def _text(value) -> str | None:
    """A real string field. Numbers, objects, and blanks are not titles or dates."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _place(item: dict) -> str | None:
    parts = []
    for key in ("district", "province"):
        node = item.get(key)
        name = _text(node.get("name")) if isinstance(node, dict) else None
        if name:
            parts.append(name)
    return ", ".join(parts) or None


def _row(item: dict) -> dict | None:
    job_id = _text(item.get("id"))
    if job_id is None and isinstance(item.get("id"), int) and not isinstance(item.get("id"), bool):
        job_id = str(item["id"])
    if not job_id or not job_id.isdigit():
        return None
    url = job_url(job_id)
    if not valid_job_url(url):
        return None
    posted = _text(item.get("postedAt")) or _text(item.get("datePosted"))
    updated = _text(item.get("updatedAt"))
    return {
        "source": SOURCE,
        "source_job_id": job_id,
        "url": url,
        "company": _text(item.get("companyName")),
        "title": _text(item.get("jobTitle")),
        "location": _place(item),
        "posted_at": posted,
        "jd_text": None,
        "salary_text": _text(item.get("salary")),
        "email_snippet": f"updated_at: {updated}" if updated and not posted else None,
    }


def parse_search(html_text: str) -> list[dict]:
    """Jobs from the embedded ``{"data":{"total":...}}`` object. Bad HTML yields none."""
    match = re.search(r'\{\s*"data"\s*:\s*\{\s*"total"\s*:', html_text or "")
    if not match:
        return []
    try:
        payload, _end = json.JSONDecoder().raw_decode(html_text[match.start():])
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, dict):
        return []
    outer = payload.get("data")
    if not isinstance(outer, dict):
        return []
    rows = outer.get("data")
    if not isinstance(rows, list):
        return []
    jobs = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        job = _row(item)
        if job:
            jobs.append(job)
    return jobs


def parse_detail(html_text: str) -> dict:
    posting = jobposting_json(html_text)
    text = _role_text(posting) if posting else ""
    if len(text) < 40:
        return {"jd_text": "", "posted_at": None, "jd_source": None}
    posted = _text(posting.get("datePosted"))
    return {"jd_text": text, "posted_at": posted, "jd_source": "jobthai_detail"}


def in_scope(job: dict, locations: list[str], titles: list[str]) -> bool:
    """Metro location and a commercial/trainee title. An unknown location is out."""
    location = (job.get("location") or "").casefold()
    if not location or not any(str(item).casefold() in location for item in locations):
        return False
    title = (job.get("title") or "").casefold()
    return any(str(pattern).casefold() in title for pattern in titles)


def _requests(fetcher) -> int:
    stats = getattr(fetcher, "stats", None)
    if not stats:
        return 0
    return int(stats().get(HOST, {}).get("requests", 0))


def _search_cap(cfg, fetcher, max_requests: int | None) -> int:
    """Search pages per run. A missing argument still uses the configured cap.

    CLI discover does not pass ``pipeline.search_budgets``. The cap is this
    fetcher's count for this run, and it stays at most half the host cap so a
    description fetch still fits.
    """
    if max_requests is None:
        raw = cfg.get("jobthai.max_requests", 3)
        try:
            configured = int(raw)
        except (TypeError, ValueError):
            configured = 3
    else:
        configured = int(max_requests)
    if configured <= 0:
        return 0
    host_cap = fetcher.cap_for(HOST) if hasattr(fetcher, "cap_for") else configured
    return min(configured, max(1, int(host_cap) // 2))


def collect(cfg, fetcher, days: float | None = None, log=None,
            max_requests: int | None = None, now=None) -> list[dict]:
    """Up to one search cap of keyword pages. ``days`` is unused: the page has no date query."""
    del days, now
    log = log or (lambda *a, **k: None)
    if not cfg.get("sources.jobthai", False):
        return []
    max_requests = _search_cap(cfg, fetcher, max_requests)
    keywords = list(cfg.get("jobthai.keywords", list(DEFAULT_KEYWORDS)) or [])
    locations = list(cfg.get("jobthai.locations", list(DEFAULT_LOCATIONS)) or [])
    titles = list(cfg.get("jobthai.title_include", list(DEFAULT_TITLE_INCLUDE)) or [])
    pages = int(cfg.get("jobthai.max_pages", 1) or 1)
    if pages > 1:
        log("jobthai: only page 1 is requested; a later page parameter is not verified")
    found: list[dict] = []
    seen: set[str] = set()
    # Count only this collect. Description fetches on the same fetcher are a later
    # step, and a later run must still get its own search cap.
    spent = _requests(fetcher)
    for keyword in keywords:
        if _requests(fetcher) - spent >= max_requests:
            log(f"jobthai search budget reached ({max_requests} requests)")
            break
        url = SEARCH_URL.format(keyword=urllib.parse.quote_plus(str(keyword)))
        try:
            response = fetcher.get(url)
        except (HostStopped, FetchError) as exc:
            log("jobthai stopped:", exc)
            break
        if not response.ok:
            log(f"jobthai search HTTP {response.status} for {keyword!r}")
            break
        page = parse_search(response.text)
        kept = 0
        for job in page:
            if job["source_job_id"] in seen or not in_scope(job, locations, titles):
                continue
            seen.add(job["source_job_id"])
            found.append(job)
            kept += 1
        log(f"jobthai {keyword!r}: {len(page)} on the page, {kept} in scope")
    return found
