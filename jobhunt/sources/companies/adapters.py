"""One parser per career-site adapter (M3). Each returns job dicts in the shape
``schemas/job_input.schema.json`` accepts, or a change record for the watch adapters.

The list calls are cheap and are what discovery uses; the expensive per-posting
description calls live in :mod:`jobhunt.fetch_jd` so they only run for jobs that
survive the pre-filter.
"""

from __future__ import annotations

import html
import json
import re
import urllib.parse
from datetime import datetime, timedelta, timezone

from ... import htmldom
from ...http import FetchError, HostStopped

SOURCE_FOR_ADAPTER = {
    "greenhouse": "greenhouse",
    "lever": "lever",
    "workday": "workday",
    "smartrecruiters": "smartrecruiters",
    "sea_ats": "company_site",
    "loreal": "company_site",
    "watch": "company_site",
    "json_watch": "company_site",
}

DEFAULT_LINK_PATTERN = (r"(/job/|/jobs/|/careers?/|/positions?/|/openings?/|/vacanc|"
                        r"/career/|job-detail|/requisition)")


def source_of(adapter: str) -> str:
    return SOURCE_FOR_ADAPTER.get(adapter, "company_site")


def _job(name, adapter, job_id, title, url, location=None, posted_at=None,
         jd_text=None, salary_text=None, snippet=None, logo_source_url=None) -> dict:
    return {
        "logo_source_url": logo_source_url,
        "source": source_of(adapter),
        "source_job_id": str(job_id) if job_id is not None else None,
        "url": url,
        "company": name,
        "title": title,
        "location": location,
        "posted_at": posted_at,
        "jd_text": jd_text,
        "salary_text": salary_text,
        "email_snippet": snippet,
    }


# --------------------------------------------------------------------------- #
# greenhouse
# --------------------------------------------------------------------------- #

def parse_greenhouse(payload: dict, name: str) -> list[dict]:
    jobs = []
    for item in payload.get("jobs") or []:
        content = item.get("content") or ""
        jd = htmldom.to_text(html.unescape(content)) if content else None
        jobs.append(_job(
            name, "greenhouse", item.get("id"), item.get("title"),
            item.get("absolute_url"),
            location=(item.get("location") or {}).get("name"),
            posted_at=(item.get("updated_at") or "")[:10] or None,
            jd_text=jd,
        ))
    return jobs


def greenhouse(entry: dict, fetcher) -> list[dict]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{entry['board']}/jobs?content=true"
    response = fetcher.get(url, accept="application/json")
    return parse_greenhouse(json.loads(response.text), entry["name"])


# --------------------------------------------------------------------------- #
# lever
# --------------------------------------------------------------------------- #

def _lever_text(item: dict) -> str:
    parts = [item.get("descriptionPlain") or htmldom.to_text(item.get("description") or "")]
    for block in item.get("lists") or []:
        parts.append(str(block.get("text") or ""))
        parts.append(htmldom.to_text(block.get("content") or ""))
    parts.append(item.get("additionalPlain") or "")
    return "\n".join(part.strip() for part in parts if part and part.strip())


def parse_lever(payload, name: str) -> list[dict]:
    jobs = []
    for item in payload or []:
        created = item.get("createdAt")
        posted = None
        if isinstance(created, (int, float)):
            posted = datetime.fromtimestamp(created / 1000, timezone.utc).date().isoformat()
        jobs.append(_job(
            name, "lever", item.get("id"), item.get("text"), item.get("hostedUrl"),
            location=(item.get("categories") or {}).get("location"),
            posted_at=posted,
            jd_text=_lever_text(item) or None,
        ))
    return jobs


def lever(entry: dict, fetcher) -> list[dict]:
    url = f"https://api.lever.co/v0/postings/{entry['company']}?mode=json"
    response = fetcher.get(url, accept="application/json")
    return parse_lever(json.loads(response.text), entry["name"])


# --------------------------------------------------------------------------- #
# workday
# --------------------------------------------------------------------------- #

_AGO = re.compile(r"(\d+)\s*\+?\s*(day|week|month)s?\s+ago", re.IGNORECASE)


def parse_posted_on(text: str | None, now: datetime | None = None) -> str | None:
    """Workday says "Posted 3 Days Ago"; turn that into a date."""
    if not text:
        return None
    now = now or datetime.now(timezone.utc)
    lowered = str(text).casefold()
    if "today" in lowered:
        return now.date().isoformat()
    if "yesterday" in lowered:
        return (now - timedelta(days=1)).date().isoformat()
    match = _AGO.search(lowered)
    if not match:
        return None
    amount, unit = int(match.group(1)), match.group(2).casefold()
    days = {"day": 1, "week": 7, "month": 30}[unit] * amount
    return (now - timedelta(days=days)).date().isoformat()


def workday_base(entry: dict) -> str:
    return f"https://{entry['tenant']}.{entry['wd_host']}.myworkdayjobs.com"


def workday_url(entry: dict, external_path: str) -> str:
    return f"{workday_base(entry)}/{entry['site']}{external_path}"


def parse_workday_list(payload: dict, entry: dict, now: datetime | None = None) -> list[dict]:
    jobs = []
    for item in payload.get("jobPostings") or []:
        path = item.get("externalPath") or ""
        if not path:
            continue
        jobs.append(_job(
            entry["name"], "workday", path, item.get("title"),
            workday_url(entry, path),
            location=item.get("locationsText"), posted_at=parse_posted_on(item.get("postedOn"), now),
        ))
    return jobs


def workday(entry: dict, fetcher, max_pages: int = 5) -> list[dict]:
    jobs: list[dict] = []
    offset = 0
    for _page in range(max_pages):
        body = {"appliedFacets": entry.get("applied_facets") or {}, "limit": 20, "offset": offset}
        if entry.get("search_text"):
            body["searchText"] = entry["search_text"]
        url = f"{workday_base(entry)}/wday/cxs/{entry['tenant']}/{entry['site']}/jobs"
        response = fetcher.post(url, body)
        payload = json.loads(response.text)
        page = parse_workday_list(payload, entry)
        jobs.extend(page)
        total = int(payload.get("total") or 0)
        offset += 20
        if not page or offset >= total:
            break
    return jobs


def workday_detail_url(job_url: str) -> str:
    """Detail endpoint for a posting URL, derived from the URL itself."""
    match = re.match(r"(https://([^.]+)\.([^/]+))/([^/]+)(/.*)$", job_url or "")
    if not match:
        return job_url
    base, tenant, _host, site, path = match.groups()
    return f"{base}/wday/cxs/{tenant}/{site}{path}"


def parse_workday_detail(payload: dict) -> dict:
    info = (payload or {}).get("jobPostingInfo") or {}
    return {
        "jd_text": htmldom.to_text(info.get("jobDescription") or ""),
        "posted_at": (info.get("startDate") or None),
        "location": info.get("location"),
    }


# --------------------------------------------------------------------------- #
# smartrecruiters
# --------------------------------------------------------------------------- #

def parse_smartrecruiters_list(payload: dict, name: str) -> list[dict]:
    jobs = []
    for item in payload.get("content") or []:
        location = item.get("location") or {}
        jobs.append(_job(
            name, "smartrecruiters", item.get("id"), item.get("name"),
            f"https://jobs.smartrecruiters.com/{item.get('company', {}).get('identifier', '')}/"
            f"{item.get('id')}",
            location=location.get("fullLocation") or location.get("city"),
            posted_at=(item.get("releasedDate") or "")[:10] or None,
        ))
    return jobs


def smartrecruiters(entry: dict, fetcher) -> list[dict]:
    company_id = entry.get("smartrecruiters_id") or entry.get("company")
    url = f"https://api.smartrecruiters.com/v1/companies/{company_id}/postings"
    if entry.get("country"):
        url += f"?country={entry['country']}"
    response = fetcher.get(url, accept="application/json")
    return parse_smartrecruiters_list(json.loads(response.text), entry["name"])


def parse_smartrecruiters_detail(payload: dict) -> dict:
    sections = ((payload or {}).get("jobAd") or {}).get("sections") or {}
    parts = []
    for key in ("companyDescription", "jobDescription", "qualifications", "additionalInformation"):
        section = sections.get(key) or {}
        text = section.get("text")
        if text:
            parts.append(htmldom.to_text(text))
    return {"jd_text": "\n\n".join(part.strip() for part in parts if part.strip())}


# --------------------------------------------------------------------------- #
# sea_ats (Sea / Shopee)
# --------------------------------------------------------------------------- #

SEA_CITY_NAMES = {"31": "Bangkok, Thailand"}


def parse_sea(payload: dict, name: str, url_template: str) -> list[dict]:
    data = (payload or {}).get("data") or {}
    jobs = []
    for item in data.get("job_list") or []:
        if not isinstance(item, dict):
            continue
        job_id = item.get("job_id") or item.get("id")
        if isinstance(job_id, bool) or not isinstance(job_id, (str, int)):
            continue
        job_id = str(job_id).strip()
        if not job_id:
            continue
        description = item.get("job_description") if isinstance(item.get("job_description"), str) else ""
        requirements = item.get("requirements") if isinstance(item.get("requirements"), str) else ""
        jd = htmldom.to_text(description + "\n" + requirements) if (description or requirements) else None
        location = item.get("url") and None
        city = item.get("city_id")
        if city is not None and isinstance(city, (str, int)) and not isinstance(city, bool):
            location = SEA_CITY_NAMES.get(str(city)) or (
                item.get("city_name") if isinstance(item.get("city_name"), str) else None)
        title = item.get("job_name") if isinstance(item.get("job_name"), str) else None
        posted = item.get("posted_at") if isinstance(item.get("posted_at"), str) else None
        if posted is None and isinstance(item.get("create_time"), str):
            posted = item.get("create_time")
        url = url_template.format(id=job_id) if url_template and "{id}" in url_template else None
        jobs.append(_job(
            name, "sea_ats", job_id, title,
            url,
            location=location,
            posted_at=posted,
            jd_text=jd,
        ))
    return jobs


def _with_query_param(url: str, key: str, value: str) -> str:
    """Set one query key, replacing an existing one instead of appending a second."""
    parts = urllib.parse.urlsplit(url)
    pairs = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
             if k != key]
    pairs.append((key, value))
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path,
                                    urllib.parse.urlencode(pairs), ""))


def sea_job_template(entry: dict) -> str:
    """Public job URL template. A Shopee list host does not inherit Sea's template."""
    configured = (entry.get("job_url") or "").strip()
    if configured:
        return configured
    host = urllib.parse.urlsplit(entry.get("url") or "").netloc.casefold()
    if host == "career.sea.com":
        return "https://career.sea.com/position/{id}"
    return ""


class _PartialJobs(list):
    """Jobs collected so far. ``partial`` is set when a later page could not be read."""

    partial: str | None = None


def _stop_with(jobs: _PartialJobs, reason: str):
    """Keep rows already accepted. The first page still raises so the company is isolated."""
    if jobs:
        jobs.partial = reason
        return jobs
    raise RuntimeError(reason)


def sea_ats(entry: dict, fetcher) -> list[dict]:
    template = sea_job_template(entry)
    jobs: _PartialJobs = _PartialJobs()
    seen: set[str] = set()
    offset = 0
    for _page in range(int(entry.get("max_pages", 3))):
        url = _with_query_param(entry["url"], "offset", str(offset))
        try:
            response = fetcher.get(url, accept="application/json")
        except (HostStopped, FetchError) as exc:
            # The first page has nothing to keep. A later page must not throw away
            # the rows already accepted; the caller reports the list as partial.
            return _stop_with(jobs, f"{type(exc).__name__}: {exc}")
        if not response.ok:
            return _stop_with(jobs, f"HTTP {response.status}")
        try:
            payload = json.loads(response.text)
        except json.JSONDecodeError as exc:
            return _stop_with(jobs, f"malformed JSON: {exc}")
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict) or not isinstance(data.get("job_list"), list):
            return _stop_with(jobs, "malformed Sea payload")
        rows = data.get("job_list") or []
        if rows and not any(isinstance(item, dict) for item in rows):
            return _stop_with(jobs, "non-dict job_list items")
        page = parse_sea(payload, entry["name"], template)
        fresh = []
        for job in page:
            job_id = str(job.get("source_job_id") or "")
            if not job_id or job_id in seen:
                continue
            seen.add(job_id)
            fresh.append(job)
        if not fresh:
            break
        jobs.extend(fresh)
        try:
            total = int(data.get("total_count") or 0)
        except (TypeError, ValueError):
            jobs.partial = f"bad total_count: {data.get('total_count')!r}"
            break
        offset += len(page) or 1
        if offset >= total:
            break
    return jobs


# --------------------------------------------------------------------------- #
# watch / json_watch
# --------------------------------------------------------------------------- #

def _looks_like_xml(text: str, url: str | None = None) -> bool:
    head = (text or "")[:400].lstrip().casefold()
    if head.startswith("<?xml") or "<urlset" in head or "<sitemapindex" in head:
        return True
    return bool(url and str(url).casefold().split("?")[0].endswith(".xml"))


def sitemap_links(xml_text: str, pattern: str | None = None) -> list[dict]:
    """A sitemap lists its pages in <loc> elements, not in anchors."""
    locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", xml_text or "", re.IGNORECASE)
    regex = re.compile(pattern, re.IGNORECASE) if pattern else None
    chosen = [loc for loc in locs if regex is None or regex.search(loc)]
    if not chosen:
        # the configured pattern matched nothing here (e.g. "/job/" against "/jobs/…"),
        # so fall back to the generic job-page pattern rather than reporting "no links"
        generic = re.compile(DEFAULT_LINK_PATTERN, re.IGNORECASE)
        chosen = [loc for loc in locs if generic.search(loc)]
    links = []
    seen = set()
    for loc in chosen:
        text = loc.rstrip("/").rsplit("/", 1)[-1].replace("-", " ").strip() or loc
        if loc in seen:
            continue
        seen.add(loc)
        links.append({"href": loc, "text": text[:200]})
    links.sort(key=lambda link: link["href"])
    return links


def extract_links(html_text: str, pattern: str | None = None, url: str | None = None) -> list[dict]:
    if _looks_like_xml(html_text, url):
        return sitemap_links(html_text, pattern)
    root = htmldom.parse(html_text)
    regex = re.compile(pattern or DEFAULT_LINK_PATTERN, re.IGNORECASE)
    seen = set()
    links = []
    for anchor in root.find_all("a"):
        href = (anchor.get("href") or "").strip()
        if not href or href.startswith("#") or not regex.search(href):
            continue
        text = anchor.text() or href
        key = (href, text)
        if key in seen:
            continue
        seen.add(key)
        links.append({"href": href, "text": text[:200]})
    links.sort(key=lambda link: link["href"])
    return links


def _dig(payload, dotted: str):
    node = payload
    for part in (dotted or "").split("."):
        if not part:
            continue
        if isinstance(node, list):
            try:
                node = node[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(node, dict):
            node = node.get(part)
        else:
            return None
    return node


def json_watch_items(payload: dict, entry: dict) -> list[dict]:
    rows = _dig(payload, entry.get("list_path") or "") or []
    if isinstance(rows, dict):
        rows = [rows]
    id_key = entry.get("id_key") or "id"
    title_key = entry.get("title_key") or "title"
    items = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        title = row.get(title_key) or row.get("Title") or ""
        if not title:
            continue
        items.append({"href": entry["url"], "text": str(title)[:200],
                      "id": str(row.get(id_key) or title)})
    items.sort(key=lambda item: item["id"])
    return items


# --------------------------------------------------------------------------- #
# L'Oreal (one public search page, then a detail page per surviving job)
# --------------------------------------------------------------------------- #

_LOREAL_JOB = re.compile(r"^/en/job/([^/]+)/.+/(\d+)$")
# The anchor repeats the city, then the region, then the country, after the title.
_LOREAL_PLACE = re.compile(r"\s+([^,]+),\s+\1,\s+Thailand\s*$", re.IGNORECASE)
_LD_JSON = re.compile(
    r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)


def jobposting_json(html_text: str) -> dict | None:
    """A schema.org JobPosting, from a raw JSON-LD script or JobThai's escaped copy."""
    for match in _LD_JSON.finditer(html_text or ""):
        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and data.get("@type") == "JobPosting":
            return data
    match = re.search(
        r'"type":"application/ld\+json","children":("(?:\\.|[^"\\])*")',
        html_text or "",
    )
    if not match:
        return None
    try:
        data = json.loads(json.loads(match.group(1)))
    except json.JSONDecodeError:
        return None
    if isinstance(data, dict) and data.get("@type") == "JobPosting":
        return data
    return None


def _role_text(posting: dict) -> str:
    """Role description and qualifications. Company blurbs and page chrome stay out."""
    parts = []
    for key in ("description", "qualifications"):
        raw = posting.get(key) or ""
        if not isinstance(raw, str) or not raw.strip():
            continue
        text = htmldom.to_text(raw) if "<" in raw else raw.strip()
        text = text.strip()
        if text:
            parts.append(text)
    return "\n\n".join(parts).strip()


def loreal_job_url(href: str) -> str | None:
    """Canonical L'Oréal job URL, or None for nav, foreign hosts, and bad paths.

    A relative ``/en/job/...`` path is accepted. An absolute link is accepted only
    when it is HTTPS on ``careers.loreal.com``. The path is never copied onto that
    host from somewhere else.
    """
    raw = (href or "").strip()
    if not raw or raw.startswith(("#", "javascript:", "mailto:")):
        return None
    parts = urllib.parse.urlsplit(raw)
    if parts.scheme or parts.netloc:
        host = (parts.hostname or "").casefold()
        if parts.scheme.casefold() != "https" or host != "careers.loreal.com":
            return None
    path = parts.path or ""
    match = _LOREAL_JOB.match(path)
    if not match:
        return None
    return urllib.parse.urlunsplit(("https", "careers.loreal.com", path, "", ""))


def parse_loreal_list(html_text: str, name: str) -> list[dict]:
    """Job anchors only. A nav link such as /en/jobs is not a posting."""
    root = htmldom.parse(html_text or "")
    jobs = []
    seen: set[str] = set()
    for anchor in root.find_all("a"):
        url = loreal_job_url(anchor.get("href") or "")
        if not url:
            continue
        match = _LOREAL_JOB.match(urllib.parse.urlsplit(url).path)
        if not match:
            continue
        city, job_id = match.group(1), match.group(2)
        if job_id in seen:
            continue
        seen.add(job_id)
        label = re.sub(r"\s+", " ", anchor.text() or "").strip()
        title = _LOREAL_PLACE.sub("", label).strip() or label
        if not title:
            continue
        place = city.replace("-", " ").strip()
        location = f"{place.title()}, Thailand" if place else None
        jobs.append(_job(name, "loreal", f"loreal:{job_id}", title, url, location=location))
    return jobs


def parse_loreal_detail(html_text: str) -> dict:
    posting = jobposting_json(html_text)
    text = _role_text(posting) if posting else ""
    # A title, a nav line, or an empty shell is not a description.
    if len(text) < 40:
        return {"jd_text": "", "posted_at": None, "jd_source": None}
    raw = posting.get("datePosted")
    posted = raw.strip()[:10] if isinstance(raw, str) and raw.strip() else None
    return {"jd_text": text, "posted_at": posted, "jd_source": "loreal_detail"}


def loreal(entry: dict, fetcher) -> list[dict]:
    response = fetcher.get(entry["url"])
    if not response.ok:
        raise RuntimeError(f"HTTP {response.status}")
    return parse_loreal_list(response.text, entry["name"])
