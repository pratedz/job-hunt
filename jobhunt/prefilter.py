"""Cheap Python pre-filter, run before any codex call (WORKSPLIT constraint 3).

Rules live in ``[prefilter]`` of profile.toml. Two title lists, in this order:

* ``title_exclude_hard`` - off-track professions (engineer, nurse, teacher ...).
  Always filtered; an include override never cancels these, so "Graduate Software
  Engineer" is still filtered.
* ``title_exclude`` - seniority words. An ``include_override`` match on the title
  cancels these, so "Management Trainee" survives despite "manager".

Age: ``max_age_days`` (default 30) is the **only** age rule, applied only when the
posting date is known, because guessing it would silently drop jobs.
``posted_within_days`` / ``posted_within_days_first_run`` are search windows (what the
job sites are asked for) and never filter a stored job (user, 8 Nov 2026: a 4-day-old
TikTok Shop role was dropped by the 3-day window).

A hit is stored as ``FILTERED`` with ``prefilter: <rule>`` as the reason, and the
job never reaches codex.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone

BANGKOK = timezone(timedelta(hours=7))


def _compile_all(patterns) -> list[tuple[str, re.Pattern]]:
    return [(p, re.compile(p, re.IGNORECASE)) for p in patterns or []]


def title_hit(title: str | None, profile: dict) -> str | None:
    cfg = profile.get("prefilter", {})
    if not title:
        return None
    for pattern, regex in _compile_all(cfg.get("title_exclude_hard", [])):
        if regex.search(title):
            return f"title_exclude_hard: {pattern}"
    for pattern, regex in _compile_all(cfg.get("include_overrides", [])):
        if regex.search(title):
            return None
    for pattern, regex in _compile_all(cfg.get("title_exclude", [])):
        if regex.search(title):
            return f"title_exclude: {pattern}"
    return None


def parse_posted_at(value: str | None) -> datetime | None:
    """Accept ``YYYY-MM-DD`` or ISO 8601; a bare date is Bangkok midnight."""
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    try:
        if len(text) == 10:
            day = date.fromisoformat(text)
            return datetime.combine(day, time.min, tzinfo=BANGKOK)
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=BANGKOK)
    return parsed


DEFAULT_MAX_AGE_DAYS = 30


def date_hit(posted_at: str | None, profile: dict, now: datetime | None = None,
             first_run: bool = False, window_days: float | None = None) -> str | None:
    """Filter postings older than ``[prefilter].max_age_days``.

    ``first_run`` and ``window_days`` are accepted for old callers and ignored: the
    search window decides what is collected, never what is kept.
    """
    days = profile.get("prefilter", {}).get("max_age_days", DEFAULT_MAX_AGE_DAYS)
    if days is None:
        return None
    posted = parse_posted_at(posted_at)
    if posted is None:
        return None
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    if posted - now > timedelta(days=1):
        return None  # future date: not our business to filter
    if now - posted > timedelta(days=float(days)):
        return f"max_age_days: {float(days):g}d"
    return None


def check(job: dict, profile: dict, now: datetime | None = None,
          first_run: bool = False, window_days: float | None = None) -> str | None:
    """Return the rule label that pre-filters this job, or None."""
    hit = title_hit(job.get("title"), profile)
    if hit:
        return hit
    return date_hit(job.get("posted_at"), profile, now=now, first_run=first_run,
                    window_days=window_days)


# --------------------------------------------------------------------------- #
# queue priority (M12): on-track titles first in the JD-fetch and scoring queues
# --------------------------------------------------------------------------- #

#: Titles a fresh graduate in his tracks should see first (lead, M12). The profile's
#: include_overrides (trainee, graduate, entry, intern ...) count as well.
QUEUE_PRIORITY = (r"\b(key account|account manager|account executive|trainee|graduate|fresh|"
                  r"junior|associate|category|trade marketing)\b")


def priority_patterns(profile: dict) -> list[re.Pattern]:
    cfg = profile.get("prefilter", {})
    patterns = [cfg.get("queue_priority") or QUEUE_PRIORITY] + list(cfg.get("include_overrides", []))
    return [re.compile(pattern, re.IGNORECASE) for pattern in patterns]


def is_priority(title: str | None, patterns) -> bool:
    return bool(title) and any(regex.search(title) for regex in patterns)
