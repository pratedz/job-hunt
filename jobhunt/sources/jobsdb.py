"""JobsDB (SEEK) public search API and job pages, no login.

* search: ``https://th.jobsdb.com/api/jobsearch/v5/search`` returns JSON
  (``data[]`` with ``id``, ``title``, ``advertiser.description``, ``listingDate``,
  ``locations``, ``salaryLabel``, ``teaser``, ``bulletPoints``), 30 per page;
* job page: ``https://th.jobsdb.com/job/<id>`` carries an ``application/ld+json``
  JobPosting when available, otherwise the description in
  ``[data-automation="jobAdDetails"]``.

Thai text is normal and is kept as it is.
"""

from __future__ import annotations

import re

import json
import urllib.parse
from datetime import datetime, timedelta, timezone

from .. import htmldom
from ..http import FetchError, HostStopped

SEARCH_URL = "https://th.jobsdb.com/api/jobsearch/v5/search"
JOB_URL = "https://th.jobsdb.com/job/{job_id}"
SOURCE = "jobsdb"
PAGE_SIZE = 30


def job_url(job_id: str) -> str:
    return JOB_URL.format(job_id=job_id)


def search_url(keyword: str, location: str, page: int, page_size: int = PAGE_SIZE) -> str:
    params = {
        "siteKey": "TH-Main",
        "sourcesystem": "houston",
        "keywords": keyword,
        "where": location,
        "page": str(page),
        "pageSize": str(page_size),
        "locale": "en-TH",
    }
    return f"{SEARCH_URL}?{urllib.parse.urlencode(params, quote_via=urllib.parse.quote_plus)}"


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #

def _teaser(entry: dict) -> str | None:
    parts = []
    if entry.get("teaser"):
        parts.append(str(entry["teaser"]).strip())
    for bullet in entry.get("bulletPoints") or []:
        if bullet:
            parts.append(f"- {str(bullet).strip()}")
    text = "\n".join(part for part in parts if part)
    return text or None


def parse_search(payload: dict) -> list[dict]:
    jobs = []
    for entry in payload.get("data") or []:
        job_id = entry.get("id")
        if not job_id:
            continue
        advertiser = entry.get("advertiser") or {}
        locations = entry.get("locations") or [{}]
        salary = (entry.get("salaryLabel") or "").strip() or None
        branding = entry.get("branding") or {}
        logo = (branding.get("serpLogoUrl") or entry.get("advertiserLogo")
                or (advertiser.get("logo")) or "")
        logo = str(logo).strip() or None
        jobs.append({
            "source": SOURCE,
            "source_job_id": str(job_id),
            "url": job_url(job_id),
            "title": (entry.get("title") or "").strip(),
            "company": (advertiser.get("description") or entry.get("companyName") or "").strip() or None,
            "location": (locations[0].get("label") or "").strip() or None,
            "posted_at": entry.get("listingDate") or None,
            "salary_text": salary,
            "logo_source_url": logo,
            "email_snippet": _teaser(entry),
        })
    return jobs


def listing_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _salary_from_ldjson(node: dict) -> tuple[str | None, dict]:
    salary = node.get("baseSalary") or {}
    value = salary.get("value") or {}
    if isinstance(value, dict):
        low, high = value.get("minValue"), value.get("maxValue")
        unit = (value.get("unitText") or "").upper()
        amount = value.get("value")
        if low is None and amount is not None:
            low = high = amount
        if low is not None:
            currency = salary.get("currency") or "THB"
            period = "month" if unit in ("MONTH", "") else "year" if unit == "YEAR" else unit.lower()
            if high is not None and high != low:
                text = f"{currency} {low:,.0f}-{high:,.0f} per {period}"
            else:
                text = f"{currency} {low:,.0f} per {period}"
            return text, {"source": "ld+json"}
    return None, {}


def _ldjson_jobposting(root) -> dict | None:
    for script in root.find_all("script"):
        if (script.get("type") or "").strip().lower() != "application/ld+json":
            continue
        raw = "".join(str(c) for c in script.children if not isinstance(c, htmldom.Node))
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        candidates = []
        if isinstance(data, dict) and isinstance(data.get("@graph"), list):
            candidates = data["@graph"]
        elif isinstance(data, list):
            candidates = data
        else:
            candidates = [data]
        for node in candidates:
            if isinstance(node, dict) and node.get("@type") in ("JobPosting", ["JobPosting"]):
                return node
    return None


# The page's embedded state: the company profile's branding logo, or the job ad's.
_STATE_LOGO = re.compile(
    r'"(?:branding":\{"__typename":"Branding","logo|'
    r'logo":\{"__typename":"JobProductBrandingImage","url)"'
    r':"(https:(?:\\u002F|/){2}(?:image-service-cdn\.seek\.com\.au|'
    r'bx-branding-gateway\.cloud\.seek\.com\.au)[^"\\]*(?:\\u002F[^"\\]*)*)"')


def _state_logo(html: str) -> str | None:
    """The company logo from the page's embedded JSON (ld+json rarely has one)."""
    match = _STATE_LOGO.search(html or "")
    return match.group(1).replace("\\u002F", "/") if match else None


def parse_job_page(html: str) -> dict:
    """JD text (ld+json first, then jobAdDetails), plus title/company/salary when present."""
    root = htmldom.parse(html)
    result = {"jd_text": "", "jd_source": None, "title": "", "company": "",
              "salary_text": None, "posted_at": None, "logo_source_url": None}

    posting = _ldjson_jobposting(root)
    if posting:
        description = posting.get("description") or ""
        result["jd_text"] = htmldom.to_text(description) if description else ""
        result["jd_source"] = "ld+json"
        result["title"] = (posting.get("title") or "").strip()
        org = posting.get("hiringOrganization") or {}
        result["company"] = (org.get("name") if isinstance(org, dict) else "") or ""
        result["posted_at"] = posting.get("datePosted")
        organization = posting.get("hiringOrganization") or {}
        if isinstance(organization, dict) and organization.get("logo"):
            result["logo_source_url"] = str(organization["logo"])
        result["salary_text"], _ = _salary_from_ldjson(posting)

    if not result["logo_source_url"]:
        result["logo_source_url"] = _state_logo(html)

    if not result["jd_text"]:
        node = root.find(attrs={"data-automation": "jobAdDetails"})
        if node is not None:
            result["jd_text"] = node.to_text()
            result["jd_source"] = "jobAdDetails"

    # The page also carries the title and the advertiser outside ld+json.
    if not result["title"]:
        heading = root.find("h1")
        if heading is not None:
            result["title"] = heading.text()
    if not result["company"]:
        advertiser = root.find(attrs={"data-automation": "advertiser-name"})
        if advertiser is not None:
            result["company"] = advertiser.text()
    return result


# --------------------------------------------------------------------------- #
# collection
# --------------------------------------------------------------------------- #

def _requests_to_host(fetcher) -> int:
    stats = getattr(fetcher, "stats", None)
    if not stats:
        return 0
    return int(stats().get("th.jobsdb.com", {}).get("requests", 0))


def collect(cfg, fetcher, days: float | None, log=None, now: datetime | None = None,
            max_requests: int | None = None) -> list[dict]:
    """Search every keyword × location, up to ``max_pages`` pages of 30."""
    log = log or (lambda *a, **k: None)
    keywords = cfg.get("search.keywords", [])
    locations = cfg.get("search.jobsdb_locations", [])
    max_pages = int(cfg.get("search.max_pages", 5))
    cutoff = None
    if days:
        cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=float(days))
    found: list[dict] = []
    seen: set[str] = set()
    stopped = False

    for keyword in keywords:
        for location in locations:
            for page in range(1, max_pages + 1):
                if max_requests is not None and _requests_to_host(fetcher) >= max_requests:
                    log(f"jobsdb search budget reached ({max_requests} requests)")
                    stopped = True
                    break
                url = search_url(keyword, location, page)
                try:
                    response = fetcher.get(url, accept="application/json")
                except (HostStopped, FetchError) as exc:
                    log("jobsdb stopped:", exc)
                    stopped = True
                    break
                if not response.ok:
                    log(f"jobsdb search {response.status} for {keyword!r} page {page}")
                    break
                try:
                    payload = json.loads(response.text)
                except json.JSONDecodeError:
                    log(f"jobsdb search page {page} for {keyword!r} was not JSON")
                    break
                page_jobs = parse_search(payload)
                if not page_jobs:
                    break
                kept = 0
                for job in page_jobs:
                    posted = listing_datetime(job.get("posted_at"))
                    if cutoff and posted and posted < cutoff:
                        continue
                    kept += 1
                    if job["source_job_id"] in seen:
                        continue
                    seen.add(job["source_job_id"])
                    found.append(job)
                log(f"jobsdb {keyword!r} / {location!r} page {page}: {kept} in window")
                if cutoff and kept == 0:
                    break
            if stopped:
                break
        if stopped:
            break
    return found
