"""Target company career sites (M3).

Each company in ``[[companies]]`` of ``profile.toml`` names an adapter. API adapters
list postings; watch adapters only compare a hash of the job links on a page (or of
the (id, title) pairs in a JSON feed) and raise a board item when it changes.

One failing company is reported and the run continues. The location filter drops
postings outside Thailand/Singapore before they are stored, so the board is not
flooded with London roles.
"""

from __future__ import annotations

import hashlib
import json

from ... import store
from . import adapters

API_ADAPTERS = {
    "greenhouse": adapters.greenhouse,
    "lever": adapters.lever,
    "workday": adapters.workday,
    "smartrecruiters": adapters.smartrecruiters,
    "sea_ats": adapters.sea_ats,
    "loreal": adapters.loreal,
}
WATCH_ADAPTERS = ("watch", "json_watch")


def entries(cfg) -> list[dict]:
    return [dict(entry) for entry in cfg.get("companies", []) if entry.get("adapter")]


def keep_location(location: str | None, keep: list[str]) -> bool:
    """Keep postings whose location is in the allow list; unknown locations pass."""
    if not keep:
        return True
    if not location:
        return True
    text = str(location).casefold()
    return any(str(item).casefold() in text for item in keep)


def _hash(links: list[dict]) -> str:
    payload = json.dumps([[link.get("href", ""), link.get("text", "")] for link in links],
                         ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _record_change(conn, entry: dict, links: list[dict], log, now) -> bool:
    """Baseline on the first run; a board item when the set of links changes."""
    if not links:
        # An empty page is not a baseline: storing it would turn the next working
        # run into a false "everything changed".
        log(f"{entry['name']}: no links found, no baseline recorded")
        return False
    current = _hash(links)
    previous = store.get_watch(conn, entry["name"])
    store.set_watch(conn, entry["name"], entry["url"], current, links, now)
    if previous is None or not store.load_json(previous["links_json"]):
        # first run, or an old broken baseline with no links
        log(f"{entry['name']}: baseline recorded ({len(links)} links)")
        return False
    if previous["hash"] == current:
        return False
    known = {(str(link.get("href")), str(link.get("text")))
             for link in store.load_json(previous["links_json"]) or []}
    fresh = [link for link in links
             if (str(link.get("href")), str(link.get("text"))) not in known][:20]
    store.add_site_change(conn, entry["name"], entry["url"], fresh, detected_at=now)
    log(f"{entry['name']}: careers page changed ({len(fresh)} new links)")
    return True


def collect(cfg, conn, fetcher, log=None, only=None, now=None, dry_run=False) -> tuple[list[dict], list[dict]]:
    """Return (jobs, per-company report). Watch items are written to the store."""
    log = log or (lambda *a, **k: None)
    keep = list(cfg.get("companies_filter.keep", []) or [])
    jobs: list[dict] = []
    reports: list[dict] = []
    stamp = now or store.utc_now()

    for entry in entries(cfg):
        name, adapter = entry["name"], entry["adapter"].casefold()
        if only and only.casefold() not in name.casefold():
            continue
        report = {"company": name, "adapter": adapter, "status": "ok", "postings": 0,
                  "kept": 0, "dropped": 0, "changed": False, "error": None}
        try:
            if adapter == "none":
                report["status"] = "skipped"
                reports.append(report)
                continue
            if adapter in API_ADAPTERS:
                found = API_ADAPTERS[adapter](entry, fetcher)
                partial = getattr(found, "partial", None)
                if partial:
                    report["status"] = "partial"
                    report["error"] = partial
                    log(f"{name}: partial list, {len(found)} postings kept: {partial}")
                report["postings"] = len(found)
                kept = [job for job in found if keep_location(job.get("location"), keep)]
                logo = str(entry.get("logo_url") or "").strip()
                if logo:            # optional, set by the lead per company (M9)
                    for job in kept:
                        job["logo_source_url"] = job.get("logo_source_url") or logo
                report["kept"] = len(kept)
                report["dropped"] = len(found) - len(kept)
                jobs.extend(kept)
            elif adapter == "watch":
                response = fetcher.get(entry["url"])
                if not response.ok:
                    raise RuntimeError(f"HTTP {response.status}")
                links = adapters.extract_links(response.text, entry.get("link_pattern"),
                                               url=entry["url"])
                report["postings"] = len(links)
                report["kept"] = len(links)
                report["changed"] = False if dry_run else _record_change(conn, entry, links, log, stamp)
                if not links:
                    report["status"] = "no links"
            elif adapter == "json_watch":
                response = fetcher.get(entry["url"], accept="application/json")
                if not response.ok:
                    raise RuntimeError(f"HTTP {response.status}")
                items = adapters.json_watch_items(json.loads(response.text), entry)
                report["postings"] = len(items)
                report["kept"] = len(items)
                report["changed"] = False if dry_run else _record_change(conn, entry, items, log, stamp)
                if not items:
                    report["status"] = "no links"
            else:
                report["status"] = "unknown adapter"
        except Exception as exc:  # one company must never stop the run
            report["status"] = "error"
            report["error"] = f"{type(exc).__name__}: {exc}"
            log(f"{name}: {report['error']}")
        reports.append(report)
        log(f"{name} ({adapter}): {report['status']}, {report['postings']} postings, "
            f"{report['kept']} kept")
    return jobs, reports


def format_reports(reports: list[dict]) -> str:
    lines = []
    for report in reports:
        detail = (f"{report['adapter']:<15} {report['status']:<10} "
                  f"listed {report['postings']:>3}  kept {report['kept']:>3}  "
                  f"dropped {report['dropped']:>3}")
        if report["changed"]:
            detail += "  CHANGED"
        if report["error"]:
            detail += f"  {report['error']}"
        lines.append(f"{report['company']:<32} {detail}")
    return "\n".join(lines)
