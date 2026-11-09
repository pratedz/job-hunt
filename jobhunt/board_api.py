"""The board's JSON API (contract: pair/M4-api.md), independent of HTTP.

Everything here is a plain function over a connection, the tracker and the
config, so the whole API can be tested without opening a port. The HTTP layer in
``board_server.py`` only routes and serialises.
"""

from __future__ import annotations

import json
import re
import subprocess
import urllib.parse
from pathlib import Path
from datetime import date, datetime, timezone

from . import apply_link, cover as cover_mod, htmldom, outcomes as outcomes_mod
from . import prefilter, score as score_mod, store
from . import validate as schema_check

CIRCLES = ("love", "good_at", "needs", "paid")
ORDER = {"CENTRE": 0, "HOLD": 1, "LOW": 2, "FILTERED": 3}
TAB_VERDICT = {"centre": "CENTRE", "hold": "HOLD", "low": "LOW", "filtered": "FILTERED"}
TABS = ("all", "centre", "hold", "low", "filtered", "skipped")
REGIONS = ("all", "thailand", "abroad")
# Bangkok, its districts' usual spelling, and all 76 other provinces (common romanisations).
THAI_PLACES = (
    "thailand", "bangkok", "krung thep", "amnat charoen", "ang thong", "bueng kan", "buriram",
    "buri ram", "chachoengsao", "chai nat", "chainat", "chaiyaphum", "chanthaburi", "chiang mai",
    "chiang rai", "chonburi", "chon buri", "chumphon", "kalasin", "kamphaeng phet",
    "kanchanaburi", "khon kaen", "krabi", "lampang", "lamphun", "loei", "lopburi", "lop buri",
    "mae hong son", "maha sarakham", "mukdahan", "nakhon nayok", "nakhon pathom",
    "nakhon phanom", "nakhon ratchasima", "korat", "nakhon sawan", "nakhon si thammarat",
    "nan", "narathiwat", "nong bua lamphu", "nong khai", "nonthaburi", "pathum thani",
    "pattani", "phang nga", "phangnga", "phatthalung", "phayao", "phetchabun", "phetchaburi",
    "phichit", "phitsanulok", "phra nakhon si ayutthaya", "ayutthaya", "phrae", "phuket",
    "prachinburi", "prachin buri", "prachuap khiri khan", "ranong", "ratchaburi", "rayong",
    "roi et", "sa kaeo", "sakon nakhon", "samut prakan", "samut sakhon", "samut songkhram",
    "saraburi", "satun", "sing buri", "sisaket", "si sa ket", "songkhla", "hat yai",
    "sukhothai", "suphan buri", "suphanburi", "surat thani", "surin", "tak", "trang", "trat",
    "ubon ratchathani", "udon thani", "uthai thani", "uttaradit", "yala", "yasothon",
    "laem chabang", "sriracha", "si racha", "pattaya", "map ta phut", "eec",
)
# Abroad: a country name, or a city that names it on its own.
ABROAD_PLACES = (
    ("Singapore", ("singapore",)),
    ("Hong Kong", ("hong kong", "hongkong", "kowloon")),
    ("Malaysia", ("malaysia", "kuala lumpur", "penang", "johor", "selangor", "petaling jaya")),
    ("Vietnam", ("vietnam", "viet nam", "ho chi minh", "hanoi", "ha noi", "da nang", "saigon")),
    ("Indonesia", ("indonesia", "jakarta", "surabaya", "bandung", "bali")),
    ("Philippines", ("philippines", "manila", "makati", "taguig", "cebu")),
    ("Japan", ("japan", "tokyo", "osaka")),
    ("South Korea", ("korea", "seoul")),
    ("Taiwan", ("taiwan", "taipei")),
    ("China", ("china", "shanghai", "beijing", "shenzhen", "guangzhou")),
    ("Australia", ("australia", "sydney", "melbourne")),
    ("India", ("india", "bengaluru", "bangalore", "mumbai", "new delhi", "gurgaon", "hyderabad")),
    ("Cambodia", ("cambodia", "phnom penh")),
    ("Laos", ("laos", "lao pdr", "vientiane")),
    ("Myanmar", ("myanmar", "yangon")),
    ("United Arab Emirates", ("united arab emirates", "uae", "dubai", "abu dhabi")),
    ("United Kingdom", ("united kingdom", "london")),
    ("United States", ("united states", "usa", "new york", "san francisco")),
    ("France", ("france", "paris")),
    ("Germany", ("germany", "berlin", "munich")),
    ("Netherlands", ("netherlands", "amsterdam")),
    ("Switzerland", ("switzerland", "zurich", "geneva")),
)


def _place_pattern(names) -> re.Pattern:
    return re.compile(r"\b(?:" + "|".join(re.escape(name) for name in names) + r")\b")


THAI_PATTERN = _place_pattern(THAI_PLACES)
ABROAD_PATTERNS = tuple((country, _place_pattern(names)) for country, names in ABROAD_PLACES)
DETAIL_ONLY = ("reasons", "summary", "facts", "salary", "jd_text", "jd_missing", "email_snippet")


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def today_bangkok() -> date:
    from .sources.gmail import BANGKOK
    return datetime.now(timezone.utc).astimezone(BANGKOK).date()


def _json_list(value) -> list:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _json_obj(value) -> dict | None:
    if not value:
        return None
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


# --------------------------------------------------------------------------- #
# rows
# --------------------------------------------------------------------------- #

def is_stale(row) -> bool:
    """A score made before the job's description existed: it is waiting to be re-scored.

    Such a row is presented exactly like a job that has never been scored: no verdict,
    no circles, counted in ``stats.unscored``, and absent from the verdict tabs until
    the next ``score`` run replaces it.
    """
    if row["score_status"] != "scored" or row["jd_used"]:
        return False
    if row["jd_missing"]:
        return False            # fetch attempted and failed: the snippet is the evidence
    if (row["source"] or "").casefold() == "manual":
        return False            # pasted by hand: there is no page to fetch
    return True


def effective_verdict(row) -> str | None:
    return None if is_stale(row) else row["verdict"]


def logo_urls(conn, companies=None):
    """A `logos.LogoIndex` read once per request (None without a connection)."""
    from . import logos as logos_mod
    return logos_mod.LogoIndex(conn, companies) if conn is not None else None


def job_row(row, tracker, logos=None, pages=None) -> dict:
    status = tracker.status_for(row, row["decision"])
    stale = is_stale(row)
    weakest = None
    if row["weakest_circle"] and not stale:
        weakest = {"circle": row["weakest_circle"], "score": row["weakest_score"]}
    gate_result = None if stale else row["gate_result"]
    return {
        "id": row["id"],
        "rank": None,
        "company": row["company"],
        "title": row["title"],
        "location": row["location"],
        "source": row["source"],
        "url": row["url"],
        "posted_at": row["posted_at"],
        "first_seen_at": row["first_seen_at"],
        "verdict": None if stale else row["verdict"],
        "status": status,
        "scores": {circle: (None if stale else row[circle]) for circle in CIRCLES},
        "paid_estimated": False if stale else bool(row["paid_estimated"]),
        "weakest": weakest,
        "mean": None if stale else row["mean_score"],
        "gate": {"result": gate_result,
                 "fails": [] if stale else _json_list(row["gate_reasons_json"]),
                 "questions": [] if stale else _json_list(row["gate_questions_json"])},
        "prefilter_reason": row["prefilter_rule"] if row["score_status"] == "prefiltered" else None,
        "tracked": tracker.tracked_info(row),
        "error": row["error"] if row["score_status"] == "error" else None,
        # one line on why the job is or is not worth it (null while waiting to be scored)
        "summary": None if stale else row["summary"],
        "logo_url": logos.url(row["company"], row["company_norm"]) if logos else None,
        # the real application page (M10): Apply opens it, nothing is drafted
        **dict(zip(("apply_url", "apply_via"), apply_link.destination(row, pages))),
        "country": country_of(row),
    }


def company_pages(conn, companies=None):
    """Employer posting pages among the stored adapter jobs, read once per request."""
    return apply_link.CompanySitePages(conn, companies) if conn is not None else None


def job_detail(row, tracker, conn=None, companies=None) -> dict:
    detail = job_row(row, tracker, logo_urls(conn, companies), company_pages(conn, companies))
    stale = is_stale(row)
    if stale:
        # nothing here is trustworthy yet: the next score run replaces it
        detail.update({"reasons": {}, "summary": None, "facts": {}, "salary": None,
                       "jd_text": row["jd_text"], "jd_missing": bool(row["jd_missing"]),
                       "email_snippet": row["email_snippet"]})
        return detail
    paid = _json_obj(row["paid_json"]) or {}
    salary = None
    if paid or row["paid_thb_month_mid"] is not None:
        salary = {
            "min": paid.get("salary_min"),
            "max": paid.get("salary_max"),
            "currency": paid.get("currency"),
            "period": paid.get("period"),
            "stated": bool(paid.get("stated")),
            "thb_month_mid": row["paid_thb_month_mid"],
        }
    detail.update({
        "reasons": {circle: row[circle + "_reason"] for circle in ("love", "good_at", "needs")}
                   | {"paid": row["paid_reason"]},
        "summary": row["summary"],
        "facts": _json_obj(row["facts_json"]) or {},
        "salary": salary,
        "jd_text": row["jd_text"],
        "jd_missing": bool(row["jd_missing"]),
        "email_snippet": row["email_snippet"],
    })
    return detail


def country_of(row) -> str | None:
    """The scored country first; otherwise read the free-text location.

    Thai places are checked before abroad ones, so "Bangkok, Thailand" never matches a
    city name elsewhere; whole words only ("Indiana" is not India).
    """
    facts = _json_obj(row["facts_json"]) or {}
    country = str(facts.get("location_country") or "").strip()
    if country:
        return country
    location = " ".join(str(row["location"] or "").casefold().split())
    if not location:
        return None
    if THAI_PATTERN.search(location):
        return "Thailand"
    for name, pattern in ABROAD_PATTERNS:
        if pattern.search(location):
            return name
    return None


def region_of(country: str | None) -> str | None:
    """`thailand`, `abroad`, or None when the country is unknown."""
    if not country:
        return None
    return "thailand" if country.strip().casefold() in ("thailand", "th", "tha") else "abroad"


def in_region(row, region: str) -> bool:
    if region in (None, "", "all"):
        return True
    return region_of(country_of(row)) == region


def known_circles(row) -> int:
    return sum(1 for circle in CIRCLES if row[circle] is not None)


def _sort_key(row) -> tuple:
    verdict = row["verdict"]
    group = ORDER.get(verdict, 4)
    known = known_circles(row)
    weakest = -1 if row["weakest_score"] is None else row["weakest_score"]
    mean = -1 if row["mean_score"] is None else row["mean_score"]
    seen = str(row["first_seen_at"] or "")
    # More evidence beats less evidence inside a group: an unknown must never help.
    return (group, -known, -weakest, -mean, _desc(seen), -row["id"])


def _desc(text: str):
    """Sort newest first for an ISO timestamp, inside an ascending tuple sort."""
    return tuple(-ord(ch) for ch in text)


LIVE_VERDICTS = ("CENTRE", "HOLD", "LOW")


def _in_tab(status: str, verdict: str | None, tab: str) -> bool:
    if tab == "skipped":
        return status == "skipped"
    if status != "new":
        return False
    if tab == "all":
        # "All" means everything worth looking at: filtered and unscored rows have
        # their own tabs (or are waiting to be scored) and are not listed here.
        return verdict in LIVE_VERDICTS
    return verdict == TAB_VERDICT.get(tab)


def _matches(row, query: str) -> bool:
    if not query:
        return True
    needle = query.casefold()
    return needle in (row["company"] or "").casefold() or needle in (row["title"] or "").casefold()


# --------------------------------------------------------------------------- #
# GET endpoints
# --------------------------------------------------------------------------- #

def list_jobs(conn, tracker, tab: str = "all", q: str = "", limit: int = 100,
              offset: int = 0, region: str = "all", companies=None) -> dict:
    if tab not in TABS:
        raise ApiError(400, f"Unknown tab: {tab}")
    if region and region not in REGIONS:
        raise ApiError(400, f"Unknown region: {region}")
    rows = []
    for row in store.board_jobs(conn):
        status = tracker.status_for(row, row["decision"])
        if (_in_tab(status, effective_verdict(row), tab) and _matches(row, q)
                and in_region(row, region)):
            rows.append((row, status))
    rows.sort(key=lambda pair: _sort_key(pair[0]))
    logos = logo_urls(conn, companies)
    pages = company_pages(conn, companies)
    rank = 0
    out = []
    for row, _status in rows:
        item = job_row(row, tracker, logos, pages)
        if item["verdict"] is not None:
            rank += 1
            item["rank"] = rank
        out.append(item)
    return {"total": len(out), "jobs": out[offset:offset + limit]}


def get_job(conn, tracker, job_id: int, companies=None) -> dict:
    row = store.board_job(conn, job_id)
    if row is None:
        raise ApiError(404, f"No job {job_id}")
    return job_detail(row, tracker, conn, companies)


def stats(conn, tracker, region: str = "all", tab: str = "all") -> dict:
    """Counts for the board. ``region`` filters the tab counts and the shortfall bars;
    ``regions`` counts each region inside ``tab`` (a job tab; any other tab reads All)."""
    if region and region not in REGIONS:
        raise ApiError(400, f"Unknown region: {region}")
    region_tab = tab if tab in TABS else "all"
    rows = store.board_jobs(conn)
    statuses = {}
    for row in rows:
        statuses[row["id"]] = tracker.status_for(row, row["decision"])

    tabs = {tab: 0 for tab in TABS}
    verdicts = {row["id"]: effective_verdict(row) for row in rows}
    regions_of = {row["id"]: region_of(country_of(row)) for row in rows}
    for row in rows:
        if not in_region(row, region):
            continue
        for tab in TABS:
            if _in_tab(statuses[row["id"]], verdicts[row["id"]], tab):
                tabs[tab] += 1
    # Same definitions as the lists the UI shows, so a count never disagrees with its tab.
    tabs["to_apply"] = len(tracker.applications("to_apply"))
    tabs["applied"] = len(tracker.applications("applied"))
    tabs["lessons"] = len(outcomes_mod.lessons(tracker)["lessons"])

    live = [row for row in rows
            if in_region(row, region) and statuses[row["id"]] == "new"
            and verdicts[row["id"]] in ("CENTRE", "HOLD", "LOW")]
    shortfall = {circle: sum(1 for row in live
                             if row[circle] is not None and row[circle] < 3)
                 for circle in CIRCLES}

    reasons: dict[str, int] = {}
    for row in rows:
        if not in_region(row, region):
            continue
        if statuses[row["id"]] != "new" or verdicts[row["id"]] != "FILTERED":
            continue
        fails = _json_list(row["gate_reasons_json"]) or (
            [row["prefilter_rule"]] if row["prefilter_rule"] else [])
        for reason in fails:
            key = str(reason).split(";")[0].strip()
            if key:
                reasons[key] = reasons.get(key, 0) + 1
    top = sorted(reasons.items(), key=lambda item: (-item[1], item[0]))[:3]

    # how many jobs each region offers in the selected tab, whatever the filter is
    region_counts = {"all": 0, "thailand": 0, "abroad": 0}
    for row in rows:
        if not _in_tab(statuses[row["id"]], verdicts[row["id"]], region_tab):
            continue
        region_counts["all"] += 1
        where = regions_of[row["id"]]
        if where:
            region_counts[where] += 1

    return {
        "last_run": last_run(conn),
        "tabs": tabs,
        "regions": region_counts,
        "shortfall": shortfall,
        "filtered_top_reasons": [{"reason": reason, "count": count} for reason, count in top],
        "site_changes_unseen": len(store.site_changes(conn, unseen_only=True)),
        "suggestions_pending": store.count_pending_suggestions(conn),
        "unscored": sum(1 for row in rows if verdicts[row["id"]] is None),
    }


def visible_companies(conn, tracker) -> list[dict]:
    """Companies the board shows: CENTRE/HOLD/LOW jobs, To apply and Applied records.

    ``[{"company", "company_norm", "jobs"}]``, most important first (best verdict, then
    the most visible jobs). Records without a stored job carry an empty job list.
    """
    groups: dict[str, dict] = {}
    for row in store.board_jobs(conn):
        status = tracker.status_for(row, row["decision"])
        verdict = effective_verdict(row)
        if not (_in_tab(status, verdict, "all") or status in ("to_apply", "applied")):
            continue
        key = row["company_norm"] or store.normalise_text(row["company"])
        if not key:
            continue
        item = groups.setdefault(key, {"company": row["company"], "company_norm": key,
                                       "jobs": [], "best": 9})
        item["jobs"].append(row)
        item["best"] = min(item["best"], ORDER.get(verdict, 4) if status == "new" else -1)
    for status in ("to_apply", "applied"):
        for record in tracker.applications(status):
            key = store.normalise_text(record.get("company"))
            if key:
                item = groups.setdefault(key, {"company": record.get("company"),
                                               "company_norm": key, "jobs": [], "best": 9})
                item["best"] = -1
    ordered = sorted(groups.values(), key=lambda g: (g["best"], -len(g["jobs"]), g["company_norm"]))
    return [{k: v for k, v in item.items() if k != "best"} for item in ordered]


def surfaced_companies(conn, tracker) -> list[dict]:
    """Every company a feed tab or an application tab actually lists.

    Same shape as :func:`visible_companies`. Filtered and skipped rows are included,
    because those tabs show them. Thailand jobs on the main feed (CENTRE / HOLD / LOW)
    come first, then other live jobs and application records, then filtered and skipped.
    Within a band, a company that already has a posting logo URL comes first.
    """
    groups: dict[str, dict] = {}
    for row in store.board_jobs(conn):
        status = tracker.status_for(row, row["decision"])
        verdict = effective_verdict(row)
        tabs = [tab for tab in TABS if _in_tab(status, verdict, tab)]
        if not tabs and status not in ("to_apply", "applied"):
            continue
        from .logos import logo_storage_key
        key = logo_storage_key(row["company"], row["company_norm"])
        if not key:
            continue
        live = any(tab in ("all", "centre", "hold", "low") for tab in tabs)
        thai_live = live and region_of(country_of(row)) == "thailand"
        if status in ("to_apply", "applied"):
            band = 1
        elif thai_live:
            band = 0
        elif live:
            band = 1
        else:
            band = 2
        item = groups.setdefault(key, {"company": row["company"], "company_norm": key,
                                       "jobs": [], "band": band, "best": 9, "has_url": 0})
        item["jobs"].append(row)
        item["band"] = min(item["band"], band)
        item["best"] = min(item["best"], ORDER.get(verdict, 4))
        if row["logo_source_url"]:
            item["has_url"] = 1
    for status in ("to_apply", "applied"):
        for record in tracker.applications(status):
            from .logos import logo_storage_key
            key = logo_storage_key(record.get("company"))
            if not key:
                continue
            item = groups.setdefault(key, {"company": record.get("company"),
                                           "company_norm": key, "jobs": [],
                                           "band": 1, "best": 9, "has_url": 0})
            item["band"] = min(item["band"], 1)
    ordered = sorted(groups.values(), key=lambda g: (g["band"], -g["has_url"], g["best"],
                                                     -len(g["jobs"]), g["company_norm"]))
    return [{k: v for k, v in item.items() if k not in ("band", "best", "has_url")}
            for item in ordered]


def _source_counts(payload: dict) -> dict:
    """Per-source numbers from one run row, whatever command produced it."""
    out: dict[str, dict] = {}

    def slot(name):
        return out.setdefault(name, {"found": 0, "new": 0, "prefiltered": 0,
                                     "jd_fetched": 0, "scored": 0, "errors": 0})

    for name, counts in (payload.get("sources") or {}).items():
        entry = slot(name)
        entry["found"] = counts.get("found", 0)
        entry["new"] = counts.get("new", 0)
        entry["prefiltered"] = counts.get("prefiltered", 0)
        entry["scored"] = counts.get("scored", 0)
        entry["errors"] = counts.get("errors", 0)
        if counts.get("host_stopped"):
            entry["host_stopped"] = 1
    for name, counts in ((payload.get("jd") or {}).get("by_source") or {}).items():
        slot(name)["jd_fetched"] = counts.get("fetched", 0)
    score = payload.get("score") if isinstance(payload.get("score"), dict) else payload
    for name, counts in (score.get("by_source") or {}).items():
        entry = slot(name)
        entry["scored"] = counts.get("scored", 0)
        entry["errors"] = counts.get("errors", 0)
    if "score" not in payload:      # a fetch-jd run: by_source holds fetched/missing
        for name, counts in (payload.get("by_source") or {}).items():
            if "fetched" in counts:
                slot(name)["jd_fetched"] = counts.get("fetched", 0)
    return out


def last_run(conn) -> dict | None:
    """The latest run, plus each source's most recent numbers.

    A `discover --source jobsdb` followed by `discover --source linkedin` therefore
    shows both sources with their own counts.
    """
    rows = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 50").fetchall()
    if not rows:
        return None
    merged: dict[str, dict] = {}
    at = None
    for row in rows:
        try:
            payload = json.loads(row["stats_json"] or "{}")
        except ValueError:
            payload = {}
        at = at or (row["finished_at"] or row["started_at"])
        for name, counts in _source_counts(payload).items():
            merged.setdefault(name, counts)      # newest run of that source wins
    return {"at": at, "sources": merged}


def applications(tracker, status: str | None = None, conn=None, companies=None) -> dict:
    if status not in (None, "to_apply", "applied"):
        raise ApiError(400, f"Unknown status: {status}")
    rows = tracker.applications(status)
    logos = logo_urls(conn, companies)
    for row in rows:
        row["logo_url"] = logos.url(row.get("company")) if logos else None
    return {"applications": rows}


def suggestions(conn, tracker=None) -> dict:
    return {"items": [outcomes_mod.suggestion_row(row, tracker)
                      for row in store.pending_suggestions(conn)]}


def confirm_suggestion(conn, tracker, suggestion_id: int, body: dict | None = None,
                       today: date | None = None) -> dict:
    try:
        return outcomes_mod.confirm(conn, tracker, suggestion_id, body or {},
                                    today=today or today_bangkok())
    except LookupError as exc:
        raise ApiError(404, str(exc))
    except ValueError as exc:
        raise ApiError(409, str(exc))
    except Exception as exc:
        raise ApiError(400, str(exc))


def dismiss_suggestion(conn, suggestion_id: int) -> dict:
    try:
        return outcomes_mod.dismiss(conn, suggestion_id)
    except LookupError as exc:
        raise ApiError(404, str(exc))


def change_status(tracker, body: dict, today: date | None = None) -> dict:
    record_file = (body or {}).get("record_file")
    my_status = (body or {}).get("my_status")
    if not record_file or not my_status:
        raise ApiError(400, "record_file and my_status are required")
    try:
        outcomes_mod.write_status(
            tracker, record_file, my_status,
            outcome_stage=body.get("outcome_stage"),
            outcome_reason_category=body.get("outcome_reason_category"),
            outcome_reason=body.get("outcome_reason"),
            next_step=body.get("next_step"),
            today=today or today_bangkok(),
        )
    except Exception as exc:
        raise ApiError(400, str(exc))
    return {"ok": True}


def lessons(tracker) -> dict:
    return outcomes_mod.lessons(tracker)


def site_changes(conn) -> dict:
    items = []
    for row in store.site_changes(conn, unseen_only=True):
        links = _json_list(row["new_links_json"])
        items.append({
            "id": row["id"],
            "company": row["company"],
            "url": row["url"],
            "detected_at": row["detected_at"],
            "new_links": [link for link in links if isinstance(link, dict)],
        })
    return {"items": items}


def mark_site_change_seen(conn, change_id: int) -> dict:
    if not store.mark_site_change_seen(conn, change_id):
        raise ApiError(404, f"No site change {change_id}")
    return {"ok": True}


# --------------------------------------------------------------------------- #
# POST endpoints
# --------------------------------------------------------------------------- #

def skip(conn, tracker, job_id: int, action: str) -> dict:
    row = store.board_job(conn, job_id)
    if row is None:
        raise ApiError(404, f"No job {job_id}")
    if action == "skip":
        store.set_decision(conn, job_id, "skip")
        return {"ok": True, "status": "skipped"}
    store.set_decision(conn, job_id, "new")
    return {"ok": True, "status": "new"}


def apply_for_job(conn, tracker, job_id: int, today: date | None = None,
                  cfg=None, llm=None, companies=None) -> dict:
    """Add the job to the tracker as To apply and return its application page (M10).

    No model call: the board opens ``apply_url`` and the user applies there directly. A
    cover email is drafted only on demand (``POST /api/jobs/<id>/cover-email``).
    ``cfg`` and ``llm`` are accepted for compatibility and never used.
    """
    row = store.board_job(conn, job_id)
    if row is None:
        raise ApiError(404, f"No job {job_id}")
    apply_url, apply_via = apply_link.destination(row, company_pages(conn, companies))
    record, created = tracker.apply(row, row, today=today or today_bangkok(),
                                    apply_url=apply_url, apply_via=apply_via)
    if created:
        store.set_decision(conn, job_id, "apply")
    return {
        "ok": True,
        "created": created,
        "already_tracked": not created,
        "record_file": record.rel,
        "obsidian_url": record.obsidian_url(),
        "apply_url": apply_url,
        "apply_via": apply_via,
        "cover_email": None,
    }


OPEN_BIN = "/usr/bin/open"


def reveal_draft(conn, tracker, job_id: int, cfg, run=None) -> dict:
    """Reveal the latest draft in Finder. Never opens anything outside data/drafts/."""
    from . import cover as cover_mod

    row = store.board_job(conn, job_id)
    if row is None:
        raise ApiError(404, f"No job {job_id}")
    saved = cover_mod.latest(cfg, job_id)
    if saved is None:
        raise ApiError(404, f"No cover email draft for job {job_id}")
    path = cover_mod.absolute(cfg, saved.get("file") or saved["path"]).resolve()
    drafts = cover_mod.drafts_dir(cfg).resolve()
    if path != drafts and drafts not in path.parents:
        raise ApiError(400, "That draft is outside data/drafts/, so it will not be opened")
    if not path.is_file():
        raise ApiError(404, f"No cover email draft for job {job_id}")
    runner = run or subprocess.run
    runner([OPEN_BIN, "-R", str(path)], check=False)
    return {"ok": True}


DRAFT_MARK = " Cover email draft: "


def _point_next_step_at_draft(cfg, tracker, record, path: str) -> None:
    """``next_step`` keeps the apply instruction and gains the draft path (replaced on redraft)."""
    reference = path if path.startswith(f"{cfg.root.name}/") else f"{cfg.root.name}/{path}"
    updated = tracker.vault.record(record.rel)
    if updated is None:
        return
    current = str(updated.get("next_step") or "")
    base = current.split(DRAFT_MARK)[0].split("Cover email draft:")[0].rstrip()
    if not base:
        base = "Submit via the apply link."
    tracker.vault.update_keys(updated, {"next_step": f"{base}{DRAFT_MARK}{reference}"})
    tracker.reload()


def cover_email_for(conn, tracker, job_id: int, cfg, llm, regenerate: bool = False) -> dict:
    """Regenerate a draft (POST) or return the latest saved one (GET)."""
    row = store.board_job(conn, job_id)
    if row is None:
        raise ApiError(404, f"No job {job_id}")
    if regenerate:
        if llm is None or cfg is None:
            raise ApiError(503, "no model available for the cover email")
        try:
            draft = cover_mod.draft(cfg, row, row, llm)
        except Exception as exc:
            raise ApiError(502, f"Could not draft the cover email: {exc}")
        record = tracker.find_for_job(row) if tracker is not None else None
        if record is not None:      # a tracked job's next step names the draft
            _point_next_step_at_draft(cfg, tracker, record, draft["path"])
        return {"ok": True, "cover_email": {
            "subject": draft["subject"], "body": draft["body"], "path": draft["path"],
            "violations": draft["violations"], "facts_used": draft["facts_used"]}}
    saved = cover_mod.latest(cfg, job_id)
    if saved is None:
        raise ApiError(404, f"No cover email draft for job {job_id}")
    return {"ok": True, "cover_email": saved}


def mark_submitted(tracker, record_file: str, today: date | None = None) -> dict:
    if not record_file:
        raise ApiError(400, "record_file is required")
    try:
        tracker.mark_submitted(record_file, today=today or today_bangkok())
    except Exception as exc:  # missing record, path outside the records folder
        raise ApiError(404, str(exc))
    return {"ok": True}


def score_now(cfg, conn, tracker, job_id: int, llm, fetcher) -> dict:
    """"Score anyway" (M12): score one job now, past the pre-filter, like a paste.

    Its JD is fetched first when missing (same polite fetcher); a failed fetch marks the
    JD missing so the job is scored from its snippet. A current prefilter row is
    released first (history kept). Returns ``{"ok", "job_id", "state", "error"?}``.
    """
    from . import fetch_jd, reprefilter
    from .http import FetchError, HostStopped

    row = store.board_job(conn, job_id)
    if row is None:
        raise ApiError(404, f"No job {job_id}")
    current = store.current_score(conn, job_id)
    if current is not None and current["status"] == "prefiltered":
        reprefilter.release(conn, current["id"])
    if row["jd_text"] is None and not row["jd_missing"] and row["source"] in fetch_jd.SUPPORTED:
        try:
            result = fetch_jd.fetch_one(fetcher, row, cfg)
        except (HostStopped, FetchError) as exc:
            result = {"jd_text": "", "error": str(exc)}
        if result.get("jd_text"):
            store.set_jd(conn, job_id, result["jd_text"], result.get("jd_source") or row["source"])
            if result.get("apply_url") and not row["apply_url"]:
                store.set_apply_url(conn, job_id, result["apply_url"])
        else:
            store.mark_jd_missing(conn, job_id, row["source"])
    score_mod.run(cfg, conn, llm, job_ids=[job_id],
                  outcomes=outcomes_mod.recent_outcomes_block(
                      tracker, int(cfg.get("tracking.lessons_in_prompt", 10))))
    after = store.current_score(conn, job_id)
    if after is None:
        return {"ok": True, "job_id": job_id, "state": "error",
                "error": "the job could not be scored (no description and no snippet)"}
    if after["status"] == "error":
        return {"ok": True, "job_id": job_id, "state": "error", "error": after["error"]}
    if after["status"] == "prefiltered":
        return {"ok": True, "job_id": job_id, "state": "prefiltered"}
    return {"ok": True, "job_id": job_id, "state": "scored"}


# --------------------------------------------------------------------------- #
# paste
# --------------------------------------------------------------------------- #

def paste(cfg, conn, tracker, body: dict, llm, fetcher, today: date | None = None) -> dict:
    """Ingest one pasted job, fetch its JD when a URL was given, then score it."""
    from . import fetch_jd

    url = (body.get("url") or "").strip() or None
    jd_text = (body.get("jd_text") or "").strip() or None
    if not url and not jd_text:
        raise ApiError(400, "Give a job URL or paste the JD text.")

    title = (body.get("title") or "").strip() or None
    company = (body.get("company") or "").strip() or None
    if jd_text and not title:
        first_line = next((line.strip() for line in jd_text.splitlines() if line.strip()), "")
        title = first_line[:120] or "Pasted job"
    if not title:
        title = f"Pasted job {url}" if url else "Pasted job"

    job = {
        "source": "manual",
        "source_job_id": None,
        "url": url,
        "company": company or _host(url),
        "title": title,
        "location": None,
        "posted_at": None,
        "jd_text": jd_text,
        "email_snippet": None,
        "salary_text": None,
    }
    try:
        schema_check.validate(cfg.input_schema(), [job])
    except schema_check.SchemaError as exc:
        raise ApiError(400, str(exc))

    if url:
        posting = read_posting(fetcher, url)
        if not jd_text and posting.get("jd_text"):
            job["jd_text"] = posting["jd_text"]
        # A URL-only paste starts with the hostname and a placeholder title; the page
        # itself knows the real ones.
        if _is_placeholder_company(job["company"], url) and posting.get("company"):
            job["company"] = posting["company"]
        if _is_placeholder_title(job["title"], url) and posting.get("title"):
            job["title"] = posting["title"]

    hit = prefilter.check(job, cfg.profile)
    job_id, created = store.add_job(conn, job)
    if not created:
        job_id = job_id
    if hit:
        store.insert_score(conn, job_id, {
            "status": "prefiltered", "prefilter_rule": hit, "verdict": "FILTERED",
            "gate_reasons": [], "gate_questions": [], "created_at": store.utc_now()})
        return {"ok": True, "job_id": job_id, "state": "prefiltered"}

    score_mod.run(cfg, conn, llm, job_ids=[job_id],
                  outcomes=outcomes_mod.recent_outcomes_block(
                      tracker, int(cfg.get("tracking.lessons_in_prompt", 10))))
    row = store.board_job(conn, job_id)
    if row is None:
        raise ApiError(500, "the job vanished after scoring")
    if row["score_status"] == "error":
        return {"ok": True, "job_id": job_id, "state": "error", "error": row["error"]}
    return {"ok": True, "job_id": job_id, "state": "scored"}


def _host(url: str | None) -> str | None:
    host = urllib.parse.urlsplit(url or "").hostname or ""
    return host.casefold().removeprefix("www.") or None


def _is_placeholder_company(company: str | None, url: str | None) -> bool:
    if not company:
        return True
    host = _host(url)
    return bool(host) and company.casefold().rstrip("/") == host


def _is_placeholder_title(title: str | None, url: str | None) -> bool:
    if not title:
        return True
    text = title.strip().casefold()
    return text.startswith("pasted job") or text == (url or "").casefold() or text == _host(url)


def read_posting(fetcher, url: str) -> dict:
    """Fetch a public posting page and read its title, company and description."""
    from .http import FetchError, HostStopped
    from .sources import jobsdb, linkedin

    host = (urllib.parse.urlsplit(url).hostname or "").casefold()
    try:
        if host.endswith("linkedin.com"):
            job_id = store.extract_source_job_id("linkedin", url)
            if not job_id:
                raise ApiError(502, "that LinkedIn URL has no job id in it")
            response = fetcher.get(linkedin.posting_url(job_id))
            parsed = linkedin.parse_posting(response.text)
        elif host.endswith("jobsdb.com"):
            response = fetcher.get(url)
            parsed = jobsdb.parse_job_page(response.text)
        else:
            response = fetcher.get(url)
            parsed = {"jd_text": htmldom.to_text(response.text), "title": "", "company": ""}
    except (HostStopped, FetchError) as exc:
        raise ApiError(502, f"Could not fetch the job page ({exc}). Paste the JD text instead.")

    if not response.ok:
        raise ApiError(502, f"Could not fetch the job page (HTTP {response.status}). "
                            "Paste the JD text instead.")
    return {
        "jd_text": (parsed.get("jd_text") or "").strip(),
        "title": (parsed.get("title") or "").strip(),
        "company": (parsed.get("company") or "").strip(),
    }


def _fetch_description(fetcher, url: str) -> str:
    """Public page only, through the same polite fetcher as discovery."""
    from .http import FetchError, HostStopped
    from .sources import jobsdb, linkedin

    host = (urllib.parse.urlsplit(url).hostname or "").casefold()
    try:
        if host.endswith("linkedin.com"):
            job_id = store.extract_source_job_id("linkedin", url)
            if not job_id:
                raise ApiError(502, "that LinkedIn URL has no job id in it")
            response = fetcher.get(linkedin.posting_url(job_id))
            parsed = linkedin.parse_posting(response.text)
        elif host.endswith("jobsdb.com"):
            response = fetcher.get(url)
            parsed = jobsdb.parse_job_page(response.text)
        else:
            response = fetcher.get(url)
            parsed = {"jd_text": htmldom.to_text(response.text)}
    except (HostStopped, FetchError) as exc:
        raise ApiError(502, f"Could not fetch the job page ({exc}). Paste the JD text instead.")

    if not response.ok:
        raise ApiError(502, f"Could not fetch the job page (HTTP {response.status}). "
                            "Paste the JD text instead.")
    text = (parsed.get("jd_text") or "").strip()
    if not text:
        raise ApiError(502, "That page had no readable job description. Paste the JD text instead.")
    return text
