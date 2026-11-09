"""A fake network: no sockets, recorded headers, scripted responses."""

from __future__ import annotations

import json
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def fixture_json(name: str) -> dict:
    return json.loads(fixture(name))


class FakeTransport:
    """Routes URLs to canned responses. ``routes`` maps a substring to a response."""

    def __init__(self, routes=None, default=(404, "not found", {})):
        self.routes = list((routes or {}).items())
        self.default = default
        self.calls: list[dict] = []

    def add(self, needle: str, status=200, body="", final_url=None, headers=None):
        self.routes.append((needle, (status, body, headers or {}, final_url)))

    def __call__(self, url, headers, method="GET", data=None, timeout=None):
        self.calls.append({"url": url, "headers": dict(headers), "method": method,
                           "data": data, "timeout": timeout})
        for needle, response in self.routes:
            if needle in url:
                status, body, extra_headers, final_url = (list(response) + [None, None, None, None])[:4]
                if isinstance(body, (dict, list)):
                    body = json.dumps(body)
                payload = body if isinstance(body, bytes) else str(body).encode("utf-8")
                return status, final_url or url, extra_headers or {}, payload
        status, body, extra_headers = (list(self.default) + [None, None, None])[:3]
        payload = body if isinstance(body, bytes) else str(body).encode("utf-8")
        return status, url, extra_headers or {}, payload

    @property
    def urls(self) -> list[str]:
        return [call["url"] for call in self.calls]


class FakeClock:
    """A clock and a sleep that only advance when the code asks."""

    def __init__(self, start: float = 1000.0):
        self.time = start
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.time

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(round(seconds, 3))
        self.time += seconds


def quiet(*args, **kwargs):
    """A logger that records nothing."""
    return None


def search_page(*cards) -> str:
    """A minimal LinkedIn search page: (job id, title[, posted date])."""
    items = []
    for card in cards:
        job_id, title = card[0], card[1]
        posted = card[2] if len(card) > 2 else "2026-11-06"
        items.append(f"""
        <li><div class="base-card base-search-card job-search-card"
                 data-entity-urn="urn:li:jobPosting:{job_id}">
          <a class="base-card__full-link" href="https://th.linkedin.com/jobs/view/x-{job_id}?trk=x"></a>
          <div class="base-search-card__info">
            <h3 class="base-search-card__title">{title}</h3>
            <h4 class="base-search-card__subtitle"><a class="hidden-nested-link">Acme</a></h4>
            <div class="base-search-card__metadata">
              <span class="job-search-card__location">Bangkok, Thailand</span>
              <time class="job-search-card__listdate" datetime="{posted}">today</time>
            </div>
          </div>
        </div></li>""")
    return "<html><body><ul>" + "".join(items) + "</ul></body></html>"
