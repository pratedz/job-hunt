"""Fill company and title on manual rows that were pasted as a bare URL.

A URL-only paste used to store the hostname as the company and "Pasted job <url>" as the
title. The posting page knows the real values. `python3 -m jobhunt backfill` reads them
back and updates those rows; `--dry-run` only reports what it would do.
"""

from __future__ import annotations

import urllib.parse

from . import board_api, store


def is_placeholder(row) -> bool:
    url = row["url"]
    host = urllib.parse.urlsplit(url or "").hostname or ""
    host = host.casefold().removeprefix("www.")
    company = str(row["company"] or "").casefold().rstrip("/")
    title = str(row["title"] or "").strip().casefold()
    company_is_host = bool(host) and company == host
    title_is_placeholder = (not title or title.startswith("pasted job")
                            or title == (url or "").casefold() or title == host)
    return bool(url) and (company_is_host or title_is_placeholder)


def candidates(conn) -> list:
    return [row for row in store.manual_jobs(conn) if is_placeholder(row)]


def run(cfg, conn, fetcher=None, dry_run: bool = False, limit: int | None = None,
        log=None) -> dict:
    """Update the placeholder rows. Returns a report of what changed."""
    log = log or (lambda *a, **k: None)
    from .http import PoliteFetcher

    fetcher = fetcher or PoliteFetcher(cfg.profile)
    rows = candidates(conn)
    if limit is not None:
        rows = rows[:limit]
    report = {"considered": len(rows), "updated": 0, "skipped": 0, "errors": [],
              "changes": []}
    for row in rows:
        try:
            posting = board_api.read_posting(fetcher, row["url"])
        except Exception as exc:
            report["errors"].append(f"job {row['id']}: {exc}")
            log(f"job {row['id']}: {exc}")
            continue
        fields = {}
        if posting.get("company") and board_api._is_placeholder_company(row["company"], row["url"]):
            fields["company"] = posting["company"]
            fields["company_norm"] = store.normalise_text(posting["company"])
        if posting.get("title") and board_api._is_placeholder_title(row["title"], row["url"]):
            fields["title"] = posting["title"]
            fields["title_norm"] = store.normalise_text(posting["title"])
        if not row["jd_text"] and posting.get("jd_text"):
            fields["jd_text"] = posting["jd_text"]
            fields["jd_source"] = "backfill"
            fields["jd_missing"] = 0
        if not fields:
            report["skipped"] += 1
            continue
        change = {"job_id": row["id"],
                  "company": [row["company"], fields.get("company", row["company"])],
                  "title": [row["title"], fields.get("title", row["title"])]}
        report["changes"].append(change)
        report["updated"] += 1
        log(f"job {row['id']}: {change['company'][0]} -> {change['company'][1]} | "
            f"{change['title'][0]} -> {change['title'][1]}")
        if not dry_run:
            store.update_job_fields(conn, row["id"], **fields)
    return report
