"""LinkedIn public guest endpoints: search cards, and one posting per job.

Both endpoints were verified without login on 7 Nov 2026 (WORKSPLIT constraint 2):

* search: ``https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search``
  returns **10 cards per request**, so pages step by ``start=0,10,20,...``;
* posting: ``https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/<id>`` returns
  the full description in ``.show-more-less-html__markup``.

No login, no cookies, no session. When the host stops (429/999/authwall) the
fetcher refuses further calls and this module simply returns what it has.
"""

from __future__ import annotations

import re
import urllib.parse

from .. import htmldom
from ..http import FetchError, HostStopped

SEARCH_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
POSTING_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"
PAGE_SIZE = 10
SOURCE = "linkedin"
HOST = "www.linkedin.com"

_URN = re.compile(r"urn:li:jobPosting:(\d+)")
LOGO_PREFIX = "https://media.licdn.com/"


def company_logo(image) -> str | None:
    """The company logo URL of one ``img``, or None.

    LinkedIn lazy-loads the logo from ``data-delayed-url`` and keeps a grey placeholder
    on ``static.licdn.com`` (``data-ghost-url``, sometimes ``src``). Only a
    ``media.licdn.com`` company-logo image counts; placeholders and face piles do not.
    """
    if image is None:
        return None
    for attribute in ("data-delayed-url", "src"):
        candidate = (image.get(attribute) or "").strip()
        if candidate.startswith(LOGO_PREFIX) and "company-logo" in candidate:
            return candidate
    return None


def job_url(job_id: str) -> str:
    return f"https://www.linkedin.com/jobs/view/{job_id}"


def posting_url(job_id: str) -> str:
    return POSTING_URL.format(job_id=job_id)


def search_url(keyword: str, location: str, start: int, days: float | None) -> str:
    params = {
        "keywords": keyword,
        "location": location,
        "start": str(start),
    }
    if days:
        params["f_TPR"] = f"r{int(float(days) * 86400)}"
    return f"{SEARCH_URL}?{urllib.parse.urlencode(params, quote_via=urllib.parse.quote_plus)}"


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #

def parse_cards(html: str) -> list[dict]:
    """Job cards from one search page, in page order."""
    root = htmldom.parse(html)
    jobs: list[dict] = []
    seen: set[str] = set()
    for card in root.find_all("div", class_="job-search-card"):
        match = _URN.search(card.get("data-entity-urn", "") or "")
        if not match:
            continue
        job_id = match.group(1)
        if job_id in seen:
            continue
        seen.add(job_id)
        title = card.first_text("h3", "base-search-card__title")
        company = card.first_text("h4", "base-search-card__subtitle")
        location = card.first_text("span", "job-search-card__location")
        logo = next((url for url in map(company_logo, card.find_all("img")) if url), None)
        posted = None
        time_node = card.find("time")
        if time_node is not None:
            posted = time_node.get("datetime") or None
        jobs.append({
            "source": SOURCE,
            "source_job_id": job_id,
            "url": job_url(job_id),
            "title": title,
            "company": company or None,
            "location": location or None,
            "posted_at": posted,
            "logo_source_url": logo,
        })
    return jobs


def parse_posting(html: str) -> dict:
    """Title, company, location, criteria and JD text from one posting page."""
    root = htmldom.parse(html)
    body = root.find("div", class_="show-more-less-html__markup")
    jd_text = body.to_text() if body is not None else ""

    company_node = root.find("a", class_="topcard__org-name-link")
    if company_node is None:
        company_node = root.find("span", class_="topcard__flavor")
    location = ""
    bullet = root.find("span", class_="topcard__flavor--bullet")
    if bullet is not None:
        location = bullet.text()

    criteria = []
    for item in root.find_all("li", class_="description__job-criteria-item"):
        label = item.first_text("h3")
        value = item.first_text("span")
        if label and value:
            criteria.append(f"{label}: {value}")

    card = root.find("div", class_="top-card-layout__card") or root
    logo = next((url for url in map(company_logo, card.find_all("img")) if url), None)

    from ..apply_link import linkedin_offsite
    apply = linkedin_offsite(html)

    return {
        "apply_url": apply["apply_url"],      # the employer's page, when LinkedIn shows it
        "apply_offsite": apply["offsite"],
        "title": root.first_text("h2", "top-card-layout__title"),
        "company": company_node.text() if company_node is not None else "",
        "location": location,
        "criteria": criteria,
        "jd_text": jd_text,
        "logo_source_url": logo,
    }


# --------------------------------------------------------------------------- #
# collection
# --------------------------------------------------------------------------- #

def queries(cfg) -> list[tuple[str, str]]:
    """The keyword/location pairs to search.

    ``[search].linkedin_queries`` wins when present (a list of [keyword, location]
    pairs); otherwise every ``keywords`` × ``linkedin_locations`` combination.
    """
    explicit = cfg.get("search.linkedin_queries") or []
    pairs = []
    for entry in explicit:
        if isinstance(entry, (list, tuple)) and len(entry) == 2 and all(entry):
            pairs.append((str(entry[0]), str(entry[1])))
    if pairs:
        return pairs
    return [(keyword, location)
            for keyword in cfg.get("search.keywords", [])
            for location in cfg.get("search.linkedin_locations", [])]


def _requests_to_host(fetcher) -> int:
    stats = getattr(fetcher, "stats", None)
    if not stats:
        return 0
    return int(stats().get(HOST, {}).get("requests", 0))


def collect(cfg, fetcher, days: float | None, log=None,
            max_requests: int | None = None) -> list[dict]:
    """Search every query pair, up to ``max_pages`` pages of 10 cards.

    ``max_requests`` stops the search part early, so a run keeps LinkedIn budget for
    the JD fetches (the host cap is shared by both).
    """
    log = log or (lambda *a, **k: None)
    max_pages = int(cfg.get("search.linkedin_max_pages", cfg.get("search.max_pages", 5)))
    found: list[dict] = []
    seen: set[str] = set()
    stopped = False

    for keyword, location in queries(cfg):
        for page in range(max_pages):
            if max_requests is not None and _requests_to_host(fetcher) >= max_requests:
                log(f"linkedin search budget reached ({max_requests} requests); "
                    "the rest goes to job descriptions")
                stopped = True
                break
            url = search_url(keyword, location, page * PAGE_SIZE, days)
            try:
                response = fetcher.get(url)
            except (HostStopped, FetchError) as exc:
                log("linkedin stopped:", exc)
                stopped = True
                break
            if not response.ok:
                log(f"linkedin search {response.status} for {keyword!r} page {page + 1}")
                break
            cards = parse_cards(response.text)
            if not cards:
                break
            for card in cards:
                if card["source_job_id"] in seen:
                    continue
                seen.add(card["source_job_id"])
                found.append(card)
            log(f"linkedin {keyword!r} / {location!r} page {page + 1}: {len(cards)} cards")
        if stopped:
            break
    return found
