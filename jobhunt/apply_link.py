"""Where Apply takes the user: the job's real application page (M10).

The board opens ``apply_url`` in a new tab; the user applies there directly and presses
Mark submitted. Nothing here fills in or submits anything (WORKSPLIT constraint 1).

Best destination first, from data already stored (no request is made here):

* a captured off-site link (``jobs.apply_url``: LinkedIn's ``externalApply`` target when
  the page shows one) -> ``company_site``;
* the same job found on the employer's own careers site by a company adapter (same
  company, same title) -> that page, ``company_site``;
* the adapter's own posting page: Greenhouse ``absolute_url`` (the form is on it), Lever
  ``hostedUrl/apply``, the Workday / SmartRecruiters / Sea posting page -> ``company_site``;
* LinkedIn ``/jobs/view/<id>/`` -> ``linkedin``; the JobsDB job page -> ``jobsdb``;
* a pasted URL: by its host.

**LinkedIn, as checked live on 8 Nov 2026:** a logged-out client can see *that* a job
applies off-site (the ``offsite-apply`` icon) but not *where*; neither the guest posting
nor the public job page carries the employer URL. So a LinkedIn job falls back to its
LinkedIn page, from which his own logged-in browser continues to the employer.
"""

from __future__ import annotations

import html
import re
import urllib.parse

from . import store

VIA = ("linkedin", "jobsdb", "company_site", "other")
COMPANY_SOURCES = {"greenhouse", "lever", "workday", "smartrecruiters", "company_site"}
ATS_HOSTS = ("greenhouse.io", "lever.co", "myworkdayjobs.com", "smartrecruiters.com",
             "career.sea.com", "ashbyhq.com", "workable.com", "successfactors.com",
             "taleo.net", "icims.com")
TRACKING = re.compile(r"^(utm_.*|trk|trkinfo|trackingid|refid|lipi|gclid|fbclid|mc_[ce]id|"
                      r"_hsenc|_hsmi|ref|src|source)$", re.IGNORECASE)
_APPLY_CODE = re.compile(r'<code[^>]*\bid="applyUrl"[^>]*>\s*<!--\s*"?(.*?)"?\s*-->',
                         re.IGNORECASE | re.DOTALL)
_EXTERNAL_HREF = re.compile(r'href="([^"]*externalApply[^"]*)"', re.IGNORECASE)
_OFFSITE = re.compile(r"offsite-apply|apply-link-offsite", re.IGNORECASE)


def _host(url: str | None) -> str:
    return (urllib.parse.urlsplit(url or "").hostname or "").casefold()


def http_url(url: str | None) -> str | None:
    """The URL if it is absolute http(s), else None."""
    url = (url or "").strip()
    parts = urllib.parse.urlsplit(url)
    return url if parts.scheme in ("http", "https") and parts.netloc else None


def strip_tracking(url: str | None) -> str | None:
    """Drop tracking parameters; keep everything a page needs (e.g. Greenhouse ``gh_jid``)."""
    url = http_url(url)
    if not url:
        return None
    parts = urllib.parse.urlsplit(url)
    query = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
             if not TRACKING.match(k)]
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query),
                                                  fragment=""))


def unwrap(url: str | None) -> str | None:
    """LinkedIn's redirect (``externalApply/…?url=<employer>``) -> the employer URL."""
    url = http_url(html.unescape(url or ""))
    if not url:
        return None
    if _host(url).endswith("linkedin.com"):
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        for key in ("url", "redirect", "dest"):
            for value in query.get(key, []):
                inner = http_url(value)
                if inner and not _host(inner).endswith("linkedin.com"):
                    return strip_tracking(inner)
        return None             # a LinkedIn URL that names no employer is not off-site
    return strip_tracking(url)


def linkedin_offsite(page_html: str) -> dict:
    """``{"offsite": bool, "apply_url": str | None}`` from a LinkedIn posting page."""
    text = page_html or ""
    target = None
    match = _APPLY_CODE.search(text)
    if match:
        target = unwrap(match.group(1))
    if target is None:
        for href in _EXTERNAL_HREF.findall(text):
            target = unwrap(href)
            if target:
                break
    return {"offsite": bool(target) or bool(_OFFSITE.search(text)), "apply_url": target}


def via_for_url(url: str | None) -> str:
    host = _host(url)
    if not host:
        return "other"
    if host.endswith("linkedin.com"):
        return "linkedin"
    if "jobsdb" in host or host.endswith("seek.com.au") or host.endswith("seek.com"):
        return "jobsdb"
    if any(host == ats or host.endswith("." + ats) for ats in ATS_HOSTS):
        return "company_site"
    return "other"


def _job_key(company, title, matcher) -> tuple | None:
    """Same employer and same title, across sources."""
    from . import logos

    title_key = store.normalise_text(title)
    if not title_key:
        return None
    domain = matcher.domain_for(company) if matcher else None
    company_key = ("@" + domain) if domain else logos.core_tokens(company)
    return (company_key, title_key) if company_key else None


class CompanySitePages:
    """The employer's own posting page for a job, found among the adapter jobs we store."""

    def __init__(self, conn, sources=None):
        from . import logos

        self.matcher = logos.CompanyMatcher(sources) if sources else None
        self.pages: dict[tuple, str] = {}
        placeholders = ",".join("?" * len(COMPANY_SOURCES))
        for row in conn.execute(
                f"SELECT company, title, source, url FROM jobs WHERE source IN ({placeholders})"
                " AND url IS NOT NULL ORDER BY id DESC", tuple(sorted(COMPANY_SOURCES))):
            key = _job_key(row["company"], row["title"], self.matcher)
            if key:
                self.pages.setdefault(key, own_page(row["source"], row["url"]))

    def find(self, company, title) -> str | None:
        key = _job_key(company, title, self.matcher)
        return self.pages.get(key) if key else None


def own_page(source: str | None, url: str | None) -> str | None:
    """The application page of a company-adapter job."""
    url = strip_tracking(url)
    if not url:
        return None
    if (source or "").casefold() == "lever" and not url.rstrip("/").endswith("/apply"):
        return url.rstrip("/") + "/apply"
    return url


def destination(row, pages: CompanySitePages | None = None) -> tuple[str | None, str]:
    """``(apply_url, apply_via)`` for one stored job row."""
    source = (row["source"] or "").casefold()
    captured = unwrap(_get(row, "apply_url"))
    if captured and via_for_url(captured) not in ("linkedin", "jobsdb"):
        return captured, "company_site"
    if source in COMPANY_SOURCES:
        page = own_page(source, row["url"])
        if page:
            return page, "company_site"
    if source in ("linkedin", "jobsdb") and pages is not None:
        page = pages.find(row["company"], row["title"])
        if page:
            return page, "company_site"
    if source == "linkedin" and row["source_job_id"]:
        return f"https://www.linkedin.com/jobs/view/{row['source_job_id']}/", "linkedin"
    if source == "jobsdb" and row["source_job_id"]:
        return f"https://th.jobsdb.com/job/{row['source_job_id']}", "jobsdb"
    url = strip_tracking(row["url"])
    if url:
        via = via_for_url(url)
        return url, via
    return None, "other"


def _get(row, key):
    try:
        return row[key]
    except (KeyError, IndexError):
        return None
