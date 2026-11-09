"""SQLite store for my job hunt.

Design rules
------------
* Timestamps are TEXT, ISO 8601 in UTC with a ``Z`` suffix. ``posted_at`` keeps
  whatever precision the source gave (it may be ``YYYY-MM-DD``).
* Dedupe lookup order at insert:
  1. ``(source, source_job_id)`` on ``jobs`` - extracted from the source's own URL
     shape when the input does not carry one (linkedin ``jobs/view/<id>`` and
     ``jobPosting/<id>``, jobsdb ``job/<id>``, greenhouse ``/jobs/<id>``).
  2. ``url_canonical`` on ``jobs`` - scheme + host (``www.`` dropped) + path,
     tracking parameters removed, trailing slash removed.
  3. ``job_sightings`` by that same exact identity (source + id, then canonical URL).
  4. ``company_norm`` + ``title_norm`` + ``location_norm``, falling back to
     ``company_norm`` + ``title_norm`` when the location is unknown.
     A same-source requisition id that differs from the row, or from a sighting
     of that source, does not merge.
  The primary ``jobs.source`` and ``jobs.source_job_id`` stay the first source.
  Another source is stored only as a sighting. ``dedupe_key`` is still one UNIQUE
  string. Re-seeing a job refreshes ``last_seen_at`` and fills fields that were
  empty (``jd_text``, ``posted_at``, ``company``, ``location``).
* A job is never deleted. Filtered and skipped jobs stay for the lessons view.
* Scores are append-only history. Exactly one row per job is ``is_current = 1``
  (partial unique index). Re-scoring inserts a new row, flips the old row to
  ``is_current = 0`` and points its ``superseded_by`` at the new row, in one
  transaction. The board and the CLI read current rows only; M6's lessons read
  the snapshot taken at Apply time (``decisions.score_snapshot_json``).
* ``status`` on a score row is ``prefiltered`` (no codex call, ``prefilter_rule``
  names the rule that hit), ``scored`` (circles present) or ``error`` (codex
  failed, timed out or returned schema-invalid JSON; ``error`` says why and the
  next ``score`` run picks the job up again). ``verdict`` is NULL only for
  ``error`` rows.
"""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 3

SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- One row per distinct job. Never deleted.
CREATE TABLE IF NOT EXISTS jobs (
    id              INTEGER PRIMARY KEY,
    dedupe_key      TEXT    NOT NULL UNIQUE,  -- 'src:<source>:<id>' | 'url:<canonical>' | 'ct:<company>|<title>|<location>'
    source          TEXT    NOT NULL,
    source_job_id   TEXT,
    url             TEXT,
    url_canonical   TEXT,
    company         TEXT,
    company_norm    TEXT,
    title           TEXT    NOT NULL,
    title_norm      TEXT    NOT NULL,
    location        TEXT,
    location_norm   TEXT,
    posted_at       TEXT,
    jd_text         TEXT,
    jd_source       TEXT,                     -- 'jd_file' | 'json' | 'pasted' | 'email_snippet' | 'page'
    jd_missing      INTEGER NOT NULL DEFAULT 0,
    email_snippet   TEXT,
    salary_text     TEXT,
    logo_source_url TEXT,                     -- where the company logo was advertised (M9)
    apply_url       TEXT,                     -- a captured off-site application page (M10)
    first_seen_at   TEXT    NOT NULL,
    last_seen_at    TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS jobs_source_id     ON jobs (source, source_job_id);
CREATE INDEX IF NOT EXISTS jobs_url_canonical ON jobs (url_canonical);
CREATE INDEX IF NOT EXISTS jobs_company_title ON jobs (company_norm, title_norm, location_norm);
CREATE INDEX IF NOT EXISTS jobs_posted_at     ON jobs (posted_at);

-- Append-only scoring history; one current row per job.
CREATE TABLE IF NOT EXISTS scores (
    id                    INTEGER PRIMARY KEY,
    job_id                INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    is_current            INTEGER NOT NULL DEFAULT 1,
    superseded_by         INTEGER REFERENCES scores(id),
    status                TEXT    NOT NULL,   -- 'prefiltered' | 'scored' | 'error'
    prefilter_rule        TEXT,
    facts_json            TEXT,
    gate_result           TEXT,               -- 'pass' | 'ask' | 'fail'
    gate_reasons_json     TEXT,
    gate_questions_json   TEXT,
    love                  INTEGER,            -- 1-5, NULL = unknown ("?")
    good_at               INTEGER,
    needs                 INTEGER,
    paid                  INTEGER,
    love_reason           TEXT,
    good_at_reason        TEXT,
    needs_reason          TEXT,
    paid_reason           TEXT,
    paid_estimated        INTEGER NOT NULL DEFAULT 0,
    paid_thb_month_mid    REAL,
    paid_json             TEXT,               -- the model's salary object (min/max/currency/period/stated)
    verdict               TEXT,               -- CENTRE | HOLD | LOW | FILTERED (NULL only for status='error')
    weakest_circle        TEXT,
    weakest_score         INTEGER,
    mean_score            REAL,
    summary               TEXT,
    llm_model             TEXT,
    prompt_sha256         TEXT,
    jd_used               INTEGER NOT NULL DEFAULT 0,  -- 1 when the JD text was available for this score
    error                 TEXT,
    created_at            TEXT    NOT NULL,
    scored_at             TEXT,               -- NULL for prefiltered/error rows
    CHECK (status = 'error' OR verdict IS NOT NULL)
);

CREATE UNIQUE INDEX IF NOT EXISTS scores_one_current ON scores (job_id) WHERE is_current = 1;
CREATE INDEX        IF NOT EXISTS scores_verdict   ON scores (verdict) WHERE is_current = 1;

-- Apply / Skip, one row per job. Apply also records the vault note it created.
CREATE TABLE IF NOT EXISTS decisions (
    job_id              INTEGER PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
    action              TEXT NOT NULL,        -- 'skip' | 'apply'
    note                TEXT,
    record_path         TEXT,                 -- vault note, set by M4 Apply
    score_snapshot_json TEXT,                 -- circles + verdict at apply time (M6 lessons)
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

-- A "careers page changed" item raised by the M3 watch adapter.
CREATE TABLE IF NOT EXISTS site_changes (
    id             INTEGER PRIMARY KEY,
    company        TEXT NOT NULL,
    url            TEXT NOT NULL,
    detected_at    TEXT NOT NULL,
    new_links_json TEXT,
    seen           INTEGER NOT NULL DEFAULT 0
);

-- The last seen hash of a career page (watch / json_watch adapters, M3).
CREATE TABLE IF NOT EXISTS site_watches (
    company    TEXT PRIMARY KEY,
    url        TEXT NOT NULL,
    hash       TEXT NOT NULL,
    links_json TEXT,
    checked_at TEXT NOT NULL
);

-- One row per company whose logo was fetched (M9). Keyed by the normalised name.
CREATE TABLE IF NOT EXISTS logos (
    company_norm TEXT PRIMARY KEY,
    file         TEXT,
    source_url   TEXT,
    fetched_at   TEXT,
    failed_at    TEXT,
    error        TEXT
);

-- Employer emails become suggested status changes (M6); deduped on the message id.
CREATE TABLE IF NOT EXISTS suggestions (
    id                      INTEGER PRIMARY KEY,
    message_id              TEXT NOT NULL UNIQUE,
    record_file             TEXT NOT NULL,
    company                 TEXT,
    program                 TEXT,
    type                    TEXT NOT NULL,
    proposed_json           TEXT,
    excerpt                 TEXT,
    email_from              TEXT,
    email_subject           TEXT,
    email_date              TEXT,
    detected_at             TEXT NOT NULL,
    state                   TEXT NOT NULL DEFAULT 'pending',  -- pending | confirmed | dismissed
    decided_at              TEXT
);

-- One row per command run, for the per-source counts in M2/M3 run reports.
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY,
    command     TEXT NOT NULL,                -- 'add' | 'score' | 'run'
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    stats_json  TEXT,
    error       TEXT
);

-- Every exact source identity seen for a stored job, including a deduped repeat.
-- The jobs row keeps the first source only. Coverage query state is not here.
CREATE TABLE IF NOT EXISTS job_sightings (
    id            INTEGER PRIMARY KEY,
    job_id        INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    source        TEXT    NOT NULL,
    source_job_id TEXT,
    url           TEXT,
    url_canonical TEXT,
    first_seen_at TEXT    NOT NULL,
    last_seen_at  TEXT    NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS job_sightings_source_id
    ON job_sightings (source, source_job_id)
    WHERE source_job_id IS NOT NULL AND source_job_id <> '';
-- Same canonical URL may be seen from two sources. Uniqueness is per source.
CREATE UNIQUE INDEX IF NOT EXISTS job_sightings_source_url
    ON job_sightings (source, url_canonical)
    WHERE url_canonical IS NOT NULL AND url_canonical <> '';
CREATE UNIQUE INDEX IF NOT EXISTS job_sightings_empty
    ON job_sightings (job_id, source)
    WHERE IFNULL(source_job_id, '') = '' AND IFNULL(url_canonical, '') = '';
CREATE INDEX IF NOT EXISTS job_sightings_by_url ON job_sightings (url_canonical);
CREATE INDEX IF NOT EXISTS job_sightings_job ON job_sightings (job_id);
"""

# A job may be scored when there is something to read: its JD, a fetch that was
# attempted and failed (score from the snippet, flagged jd_missing), or a manual
# add. A job whose score was made before its JD arrived is stale and comes back
# into the queue; jobs with a JD are scored before snippet-only jobs.
UNSCORED_SQL = """
SELECT j.*
  FROM jobs j
  LEFT JOIN scores s ON s.job_id = j.id AND s.is_current = 1
 WHERE (s.id IS NULL                                  -- never scored
        OR s.status = 'error'                          -- codex failed; retry next run
        OR (s.status = 'scored' AND s.jd_used = 0 AND j.jd_text IS NOT NULL))  -- stale
   AND (j.jd_text IS NOT NULL                          -- something to read
        OR j.jd_missing = 1
        OR j.source = 'manual')
 ORDER BY CASE WHEN j.jd_text IS NOT NULL THEN 0 ELSE 1 END,
          j.posted_at DESC NULLS LAST, j.id DESC
 LIMIT ?;
"""

RANKING_SQL = """
SELECT j.id, j.company, j.title, j.location, j.source, j.posted_at,
       s.verdict, s.love, s.good_at, s.needs, s.paid,
       s.weakest_circle, s.weakest_score, s.mean_score, s.gate_result,
       s.gate_questions_json, s.status, s.paid_estimated
  FROM jobs j
  JOIN scores s ON s.job_id = j.id AND s.is_current = 1
 WHERE s.status <> 'error'
 ORDER BY CASE s.verdict WHEN 'CENTRE' THEN 0 WHEN 'HOLD' THEN 1
                         WHEN 'LOW' THEN 2 ELSE 3 END,
          CASE WHEN s.weakest_score IS NULL THEN -1 ELSE s.weakest_score END DESC,
          CASE WHEN s.mean_score  IS NULL THEN -1 ELSE s.mean_score  END DESC,
          j.first_seen_at DESC, j.id DESC;
"""

TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "utm_id",
    "trk", "trackingid", "refid", "ref", "referer", "e", "lipi", "originalsubdomain",
    "position", "pagenum", "pagenumber", "currentjobid", "jobsearchid", "savedsearchid",
    "applytype", "alternatechannel", "src", "sourcesystem", "sponsored", "origin",
}


# --------------------------------------------------------------------------- #
# normalisation helpers
# --------------------------------------------------------------------------- #

def normalise_text(value: str | None) -> str | None:
    """casefold, strip accents, collapse everything that is not alphanumeric."""
    if value is None:
        return None
    text = unicodedata.normalize("NFKD", value.casefold())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^0-9a-z]+", " ", text)
    return re.sub(r"\s+", " ", text).strip() or None


def canonical_url(url: str | None) -> str | None:
    """scheme + host (www. dropped) + path + non-tracking query, no fragment."""
    if not url:
        return None
    try:
        parts = urllib.parse.urlsplit(url.strip())
    except ValueError:
        return None
    if not parts.scheme or not parts.netloc:
        return None
    host = parts.hostname or ""
    host = host.casefold()
    if host.startswith("www."):
        host = host[4:]
    path = parts.path.rstrip("/") or ""
    query = []
    for key, value in urllib.parse.parse_qsl(parts.query, keep_blank_values=True):
        if key.casefold() in TRACKING_PARAMS:
            continue
        query.append((key, value))
    query.sort()
    return urllib.parse.urlunsplit(
        (parts.scheme.casefold(), host, path, urllib.parse.urlencode(query), "")
    )


_ID_PATTERNS = {
    "linkedin": (r"/jobs/view/(\d+)", r"jobposting/(\d+)", r"[?&]currentjobid=(\d+)"),
    "jobsdb": (r"/job/(\d+)", r"/job/([0-9a-z-]+)"),
    "greenhouse": (r"/jobs/(\d+)",),
    "lever": (r"/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",),
    "smartrecruiters": (r"/(\d{9,})",),
}


def extract_source_job_id(source: str | None, url: str | None) -> str | None:
    """Pull the site's own job id out of a public URL."""
    if not url:
        return None
    lowered = url.casefold()
    for pattern in _ID_PATTERNS.get((source or "").casefold(), ()):
        match = re.search(pattern, lowered)
        if match:
            return match.group(1)
    return None


def dedupe_keys(job: dict) -> list[str]:
    """Candidate keys, most specific first. The first is stored."""
    keys = []
    source = (job.get("source") or "manual").casefold()
    job_id = job.get("source_job_id") or extract_source_job_id(source, job.get("url"))
    if job_id:
        keys.append(f"src:{source}:{str(job_id).strip()}")
    canon = canonical_url(job.get("url"))
    if canon:
        keys.append(f"url:{canon}")
    company = normalise_text(job.get("company"))
    title = normalise_text(job.get("title"))
    location = normalise_text(job.get("location"))
    if company and title:
        if location:
            # A known location is part of the key: the same title at the same company in
            # Bangkok and Singapore are different jobs.
            keys.append(f"ct:{company}|{title}|{location}")
        else:
            keys.append(f"ct:{company}|{title}")
    return keys


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- #
# connection
# --------------------------------------------------------------------------- #

def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    ensure_schema(conn)
    return conn


def connect_memory() -> sqlite3.Connection:
    """In-memory database for tests."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    ensure_schema(conn)
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    _migrate(conn)
    conn.execute(
        "INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns that newer versions expect, without touching existing rows."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(scores)")}
    if "paid_json" not in columns:
        conn.execute("ALTER TABLE scores ADD COLUMN paid_json TEXT")
    job_columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
    if "logo_source_url" not in job_columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN logo_source_url TEXT")
    if "apply_url" not in job_columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN apply_url TEXT")
    if "jd_used" not in columns:
        conn.execute("ALTER TABLE scores ADD COLUMN jd_used INTEGER NOT NULL DEFAULT 0")
        # Scores made before this column existed: the ones whose job already has a JD
        # were made with it, so they are not stale.
        conn.execute(
            """UPDATE scores SET jd_used = 1
                WHERE status = 'scored'
                  AND job_id IN (SELECT id FROM jobs WHERE jd_text IS NOT NULL)""")
    # The first draft named a global UNIQUE index job_sightings_url. A later
    # nonunique index may reuse that name; drop the unique one only.
    indexes = list(conn.execute("PRAGMA index_list('job_sightings')").fetchall())
    for index in indexes:
        if index["name"] == "job_sightings_url" and index["unique"]:
            conn.execute("DROP INDEX job_sightings_url")
            break
    _backfill_sightings(conn)
    conn.commit()


def _backfill_sightings(conn: sqlite3.Connection) -> None:
    """One sighting per existing job, from that job's own source and timestamps.

    Insert only. A second ``ensure_schema`` must not refresh ``last_seen_at`` or
    invent a source the row never had.
    """
    rows = conn.execute(
        """SELECT id, source, source_job_id, url, url_canonical, first_seen_at, last_seen_at
             FROM jobs"""
    ).fetchall()
    for row in rows:
        first_seen = row["first_seen_at"] or row["last_seen_at"]
        last_seen = row["last_seen_at"] or row["first_seen_at"]
        if not first_seen:
            continue
        already = conn.execute(
            """SELECT 1 FROM job_sightings
                WHERE job_id = ?
                  AND source = ?
                  AND IFNULL(source_job_id, '') = IFNULL(?, '')
                  AND IFNULL(url_canonical, '') = IFNULL(?, '')""",
            (row["id"], row["source"], row["source_job_id"], row["url_canonical"]),
        ).fetchone()
        if already:
            continue
        try:
            conn.execute(
                """INSERT INTO job_sightings
                       (job_id, source, source_job_id, url, url_canonical,
                        first_seen_at, last_seen_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    row["id"],
                    row["source"],
                    row["source_job_id"],
                    row["url"],
                    row["url_canonical"],
                    first_seen,
                    last_seen,
                ),
            )
        except sqlite3.IntegrityError:
            continue


# --------------------------------------------------------------------------- #
# jobs
# --------------------------------------------------------------------------- #

def _row_id(conn, key: str):
    row = conn.execute("SELECT id FROM jobs WHERE dedupe_key = ?", (key,)).fetchone()
    return row["id"] if row else None


def _identity(job: dict) -> tuple[str, str | None, str | None]:
    source = (job.get("source") or "manual").casefold()
    job_id = job.get("source_job_id") or extract_source_job_id(source, job.get("url"))
    job_id = str(job_id).strip() if job_id else None
    return source, job_id, canonical_url(job.get("url"))


def _sighting_job_id(conn: sqlite3.Connection, source: str, job_id: str | None,
                     canon: str | None) -> int | None:
    if job_id:
        row = conn.execute(
            "SELECT job_id FROM job_sightings WHERE source = ? AND source_job_id = ?",
            (source, job_id),
        ).fetchone()
        if row:
            return row["job_id"]
    if canon:
        row = conn.execute(
            "SELECT job_id FROM job_sightings WHERE url_canonical = ?",
            (canon,),
        ).fetchone()
        if row:
            return row["job_id"]
    return None


def _requisition_conflict(conn: sqlite3.Connection, row_id: int, source: str,
                          incoming_id: str | None) -> bool:
    """True when this title match would hide a different requisition of the same source."""
    if not incoming_id:
        return False
    row = conn.execute(
        "SELECT source, source_job_id FROM jobs WHERE id = ?", (row_id,)
    ).fetchone()
    if row is None:
        return True
    existing_id = (row["source_job_id"] or "").strip()
    if row["source"] == source and existing_id and existing_id != incoming_id:
        return True
    sights = conn.execute(
        """SELECT source_job_id FROM job_sightings
            WHERE job_id = ? AND source = ?
              AND source_job_id IS NOT NULL AND source_job_id <> ''""",
        (row_id, source),
    ).fetchall()
    return any(sight["source_job_id"] != incoming_id for sight in sights)


def find_job(conn: sqlite3.Connection, job: dict) -> int | None:
    """Dedupe lookup in the documented order."""
    source, job_id, canon = _identity(job)
    if job_id:
        row = conn.execute(
            "SELECT id FROM jobs WHERE source = ? AND source_job_id = ?",
            (source, job_id),
        ).fetchone()
        if row:
            return row["id"]
    if canon:
        row = conn.execute("SELECT id FROM jobs WHERE url_canonical = ?", (canon,)).fetchone()
        if row:
            return row["id"]
    seen = _sighting_job_id(conn, source, job_id, canon)
    if seen is not None:
        return seen
    company = normalise_text(job.get("company"))
    title = normalise_text(job.get("title"))
    location = normalise_text(job.get("location"))
    if company and title:
        if location:
            rows = conn.execute(
                """SELECT id FROM jobs
                    WHERE company_norm = ? AND title_norm = ? AND location_norm = ?
                    ORDER BY id""",
                (company, title, location),
            ).fetchall()
        else:
            # Only when the incoming job has no location may it match an existing row
            # by company and title alone.
            rows = conn.execute(
                """SELECT id FROM jobs
                    WHERE company_norm = ? AND title_norm = ?
                    ORDER BY id""",
                (company, title),
            ).fetchall()
        for row in rows:
            if not _requisition_conflict(conn, row["id"], source, job_id):
                return row["id"]
    return None


def record_sighting(conn: sqlite3.Connection, job_row_id: int, job: dict,
                    now: str) -> None:
    """Remember this exact source identity on ``job_row_id``. Does not commit."""
    source, job_id, canon = _identity(job)
    url = job.get("url")
    existing = None
    if job_id:
        existing = conn.execute(
            "SELECT id FROM job_sightings WHERE source = ? AND source_job_id = ?",
            (source, job_id),
        ).fetchone()
    if existing is None and canon:
        existing = conn.execute(
            "SELECT id FROM job_sightings WHERE source = ? AND url_canonical = ?",
            (source, canon),
        ).fetchone()
    if existing is None and not job_id and not canon:
        existing = conn.execute(
            """SELECT id FROM job_sightings
                WHERE job_id = ? AND source = ?
                  AND IFNULL(source_job_id, '') = ''
                  AND IFNULL(url_canonical, '') = ''""",
            (job_row_id, source),
        ).fetchone()
    if existing is not None:
        conn.execute(
            """UPDATE job_sightings
                  SET last_seen_at = ?,
                      url = COALESCE(url, ?),
                      url_canonical = COALESCE(url_canonical, ?),
                      source_job_id = COALESCE(NULLIF(source_job_id, ''), ?)
                WHERE id = ?""",
            (now, url, canon, job_id, existing["id"]),
        )
        return
    conn.execute(
        """INSERT INTO job_sightings
               (job_id, source, source_job_id, url, url_canonical, first_seen_at, last_seen_at)
           VALUES (?,?,?,?,?,?,?)""",
        (job_row_id, source, job_id, url, canon, now, now),
    )


def job_sightings(conn: sqlite3.Connection, job_id: int) -> list[dict]:
    """Exact identities stored for one job, oldest first."""
    rows = conn.execute(
        "SELECT * FROM job_sightings WHERE job_id = ? ORDER BY id",
        (job_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def add_job(conn: sqlite3.Connection, job: dict, now: str | None = None) -> tuple[int, bool]:
    """Insert or refresh one job. Returns ``(job_id, created)``."""
    now = now or utc_now()
    source, job_id, _canon = _identity(job)
    keys = dedupe_keys(job)

    existing = find_job(conn, job)
    if existing is not None:
        stored = conn.execute(
            "SELECT source, source_job_id FROM jobs WHERE id = ?", (existing,)
        ).fetchone()
        stored_id = (stored["source_job_id"] or "").strip()
        # Only the same source may fill an empty primary id. A LinkedIn id must
        # not land on a Workday or manual row: fetch dispatch uses that column.
        fill_id = job_id if stored["source"] == source and not stored_id else None
        conn.execute(
            """UPDATE jobs SET
                   last_seen_at = ?,
                   company      = COALESCE(company, ?),
                   location     = COALESCE(location, ?),
                   location_norm= COALESCE(location_norm, ?),
                   posted_at    = COALESCE(posted_at, ?),
                   jd_text      = COALESCE(jd_text, ?),
                   jd_source    = COALESCE(jd_source, ?),
                   jd_missing   = CASE WHEN COALESCE(jd_text, ?) IS NULL THEN jd_missing ELSE 0 END,
                   email_snippet= COALESCE(email_snippet, ?),
                   salary_text  = COALESCE(salary_text, ?),
                   source_job_id= COALESCE(source_job_id, ?),
                   logo_source_url = COALESCE(logo_source_url, ?)
             WHERE id = ?""",
            (
                now,
                job.get("company"),
                job.get("location"),
                normalise_text(job.get("location")),
                job.get("posted_at"),
                job.get("jd_text"),
                job.get("jd_source"),
                job.get("jd_text"),
                job.get("email_snippet"),
                job.get("salary_text"),
                fill_id,
                job.get("logo_source_url"),
                existing,
            ),
        )
        record_sighting(conn, existing, job, now)
        conn.commit()
        return existing, False

    key = keys[0] if keys else f"ct:{normalise_text(job.get('company')) or '?'}|{normalise_text(job.get('title')) or '?'}"
    cur = conn.execute(
        """INSERT INTO jobs (dedupe_key, source, source_job_id, url, url_canonical,
                             company, company_norm, title, title_norm, location, location_norm,
                             posted_at, jd_text, jd_source, jd_missing, email_snippet,
                             salary_text, logo_source_url, first_seen_at, last_seen_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            key,
            source,
            job_id,
            job.get("url"),
            canonical_url(job.get("url")),
            job.get("company"),
            normalise_text(job.get("company")),
            job.get("title"),
            normalise_text(job.get("title")),
            job.get("location"),
            normalise_text(job.get("location")),
            job.get("posted_at"),
            job.get("jd_text"),
            job.get("jd_source"),
            # jd_missing means "we tried to fetch the description and failed", so a
            # newly discovered job starts at 0 even when it has no JD yet: it is a
            # candidate for the fetch step, not a known gap.
            int(job.get("jd_missing", 0)),
            job.get("email_snippet"),
            job.get("salary_text"),
            job.get("logo_source_url"),
            now,
            now,
        ),
    )
    new_id = int(cur.lastrowid)
    record_sighting(conn, new_id, job, now)
    conn.commit()
    return new_id, True


def get_job(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()


def current_score(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM scores WHERE job_id = ? AND is_current = 1", (job_id,)
    ).fetchone()


def unscored_jobs(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    return conn.execute(UNSCORED_SQL, (limit,)).fetchall()


def ranked(conn: sqlite3.Connection, verdict: str | None = None) -> list[sqlite3.Row]:
    rows = conn.execute(RANKING_SQL).fetchall()
    if verdict:
        want = verdict.upper()
        rows = [r for r in rows if (r["verdict"] or "").upper() == want]
    return rows


# --------------------------------------------------------------------------- #
# scores
# --------------------------------------------------------------------------- #

def insert_score(conn: sqlite3.Connection, job_id: int, record: dict) -> int:
    """Insert a new current score row and supersede the previous one."""
    now = record.get("created_at") or utc_now()
    with conn:
        old = current_score(conn, job_id)
        if old is not None:
            # Clear the partial unique index before the new row claims it.
            conn.execute("UPDATE scores SET is_current = 0 WHERE id = ?", (old["id"],))
        cur = conn.execute(
            """INSERT INTO scores (job_id, is_current, status, prefilter_rule, facts_json,
                                   gate_result, gate_reasons_json, gate_questions_json,
                                   love, good_at, needs, paid,
                                   love_reason, good_at_reason, needs_reason, paid_reason,
                                   paid_estimated, paid_thb_month_mid, paid_json,
                                   verdict, weakest_circle, weakest_score, mean_score,
                                   summary, llm_model, prompt_sha256, jd_used, error, created_at, scored_at)
               VALUES (?,1,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                job_id,
                record.get("status", "scored"),
                record.get("prefilter_rule"),
                _dump(record.get("facts")),
                record.get("gate_result"),
                _dump(record.get("gate_reasons")),
                _dump(record.get("gate_questions")),
                record.get("love"),
                record.get("good_at"),
                record.get("needs"),
                record.get("paid"),
                record.get("love_reason"),
                record.get("good_at_reason"),
                record.get("needs_reason"),
                record.get("paid_reason"),
                1 if record.get("paid_estimated") else 0,
                record.get("paid_thb_month_mid"),
                _dump(record.get("paid_obj")),
                record.get("verdict"),
                record.get("weakest_circle"),
                record.get("weakest_score"),
                record.get("mean_score"),
                record.get("summary"),
                record.get("llm_model"),
                record.get("prompt_sha256"),
                1 if record.get("jd_used") else 0,
                record.get("error"),
                now,
                record.get("scored_at"),
            ),
        )
        new_id = int(cur.lastrowid)
        if old is not None:
            conn.execute(
                "UPDATE scores SET is_current = 0, superseded_by = ? WHERE id = ?",
                (new_id, old["id"]),
            )
    return new_id


def _dump(value):
    return None if value is None else json.dumps(value, ensure_ascii=False)


def load_json(value):
    return None if value is None else json.loads(value)


# --------------------------------------------------------------------------- #
# decisions and runs
# --------------------------------------------------------------------------- #

def set_decision(conn: sqlite3.Connection, job_id: int, action: str,
                 note: str | None = None, snapshot: dict | None = None,
                 now: str | None = None) -> None:
    now = now or utc_now()
    with conn:
        conn.execute(
            """INSERT INTO decisions (job_id, action, note, score_snapshot_json, created_at, updated_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(job_id) DO UPDATE SET action = excluded.action,
                                                note = excluded.note,
                                                score_snapshot_json = excluded.score_snapshot_json,
                                                updated_at = excluded.updated_at""",
            (job_id, action, note, _dump(snapshot), now, now),
        )


def get_decision(conn: sqlite3.Connection, job_id: int):
    return conn.execute("SELECT * FROM decisions WHERE job_id = ?", (job_id,)).fetchone()


def record_run(conn: sqlite3.Connection, command: str, stats: dict | None = None,
               started_at: str | None = None, error: str | None = None) -> int:
    now = utc_now()
    with conn:
        cur = conn.execute(
            "INSERT INTO runs (command, started_at, finished_at, stats_json, error) VALUES (?,?,?,?,?)",
            (command, started_at or now, now, _dump(stats), error),
        )
    return int(cur.lastrowid)


# --------------------------------------------------------------------------- #
# discovery support (M2)
# --------------------------------------------------------------------------- #

JD_CANDIDATES_SQL = """
SELECT j.*
  FROM jobs j
  LEFT JOIN scores s ON s.job_id = j.id AND s.is_current = 1
 WHERE j.jd_text IS NULL
   AND j.jd_missing = 0
   AND (s.id IS NULL OR s.status <> 'prefiltered')
 ORDER BY j.id
 LIMIT ?;
"""


def jd_candidates(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    """Jobs worth a JD fetch: no JD yet, not already known to be missing, not pre-filtered."""
    return conn.execute(JD_CANDIDATES_SQL, (limit,)).fetchall()


def set_jd(conn: sqlite3.Connection, job_id: int, text: str, source: str) -> None:
    with conn:
        conn.execute(
            "UPDATE jobs SET jd_text = ?, jd_source = ?, jd_missing = 0 WHERE id = ?",
            (text, source, job_id),
        )


def set_logo_source_url(conn: sqlite3.Connection, job_id: int, url: str) -> None:
    with conn:
        conn.execute("UPDATE jobs SET logo_source_url = ? WHERE id = ?", (url, job_id))


def set_apply_url(conn: sqlite3.Connection, job_id: int, url: str) -> None:
    with conn:
        conn.execute("UPDATE jobs SET apply_url = ? WHERE id = ?", (url, job_id))


def set_salary_text(conn: sqlite3.Connection, job_id: int, text: str) -> None:
    with conn:
        conn.execute("UPDATE jobs SET salary_text = ? WHERE id = ?", (text, job_id))


def mark_jd_missing(conn: sqlite3.Connection, job_id: int, source: str | None = None) -> None:
    with conn:
        conn.execute(
            "UPDATE jobs SET jd_missing = 1, jd_source = COALESCE(jd_source, ?) WHERE id = ?",
            (source, job_id),
        )


def update_job_fields(conn: sqlite3.Connection, job_id: int, **fields) -> None:
    if not fields:
        return
    columns = ", ".join(f"{name} = ?" for name in fields)
    with conn:
        conn.execute(f"UPDATE jobs SET {columns} WHERE id = ?",
                     (*fields.values(), job_id))


def manual_jobs(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Manual adds, with the raw url so callers can compare it to the company."""
    return conn.execute("SELECT * FROM jobs WHERE source = 'manual' ORDER BY id").fetchall()


def count_jobs(conn: sqlite3.Connection, source: str | None = None) -> int:
    if source:
        row = conn.execute("SELECT COUNT(*) FROM jobs WHERE source = ?", (source,)).fetchone()
    else:
        row = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()
    return int(row[0])


# --------------------------------------------------------------------------- #
# board support (M4)
# --------------------------------------------------------------------------- #

BOARD_JOBS_SQL = """
SELECT j.*,
       s.verdict, s.status AS score_status, s.love, s.good_at, s.needs, s.paid,
       s.paid_estimated, s.weakest_circle, s.weakest_score, s.mean_score,
       s.gate_result, s.gate_reasons_json, s.gate_questions_json, s.prefilter_rule,
       s.error, s.facts_json, s.summary, s.paid_json, s.paid_reason, s.jd_used,
       s.llm_model, s.prompt_sha256,
       s.paid_thb_month_mid,
       s.love_reason, s.good_at_reason, s.needs_reason,
       d.action AS decision
  FROM jobs j
  LEFT JOIN scores s ON s.job_id = j.id AND s.is_current = 1
  LEFT JOIN decisions d ON d.job_id = j.id
"""


def board_jobs(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(BOARD_JOBS_SQL).fetchall()


def board_job(conn: sqlite3.Connection, job_id: int):
    return conn.execute(BOARD_JOBS_SQL + " WHERE j.id = ?", (job_id,)).fetchone()


def latest_run(conn: sqlite3.Connection):
    return conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()


def add_site_change(conn: sqlite3.Connection, company: str, url: str, links: list,
                    detected_at: str | None = None) -> int:
    with conn:
        cur = conn.execute(
            "INSERT INTO site_changes (company, url, detected_at, new_links_json) VALUES (?,?,?,?)",
            (company, url, detected_at or utc_now(), _dump(links or [])),
        )
    return int(cur.lastrowid)


def site_changes(conn: sqlite3.Connection, unseen_only: bool = True) -> list[sqlite3.Row]:
    sql = "SELECT * FROM site_changes" + (" WHERE seen = 0" if unseen_only else "") + " ORDER BY id"
    return conn.execute(sql).fetchall()


def mark_site_change_seen(conn: sqlite3.Connection, change_id: int) -> bool:
    with conn:
        cur = conn.execute("UPDATE site_changes SET seen = 1 WHERE id = ?", (change_id,))
    return cur.rowcount > 0


def get_watch(conn: sqlite3.Connection, company: str):
    return conn.execute("SELECT * FROM site_watches WHERE company = ?", (company,)).fetchone()


def set_watch(conn: sqlite3.Connection, company: str, url: str, hash_value: str,
              links: list, checked_at: str) -> None:
    with conn:
        conn.execute(
            """INSERT INTO site_watches (company, url, hash, links_json, checked_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(company) DO UPDATE SET url = excluded.url, hash = excluded.hash,
                                                 links_json = excluded.links_json,
                                                 checked_at = excluded.checked_at""",
            (company, url, hash_value, _dump(links or []), checked_at),
        )


# --------------------------------------------------------------------------- #
# suggestions (M6)
# --------------------------------------------------------------------------- #

def add_suggestion(conn: sqlite3.Connection, suggestion: dict) -> int | None:
    """Insert unless the message is already known. Returns the new id, or None."""
    with conn:
        cur = conn.execute(
            """INSERT INTO suggestions (message_id, record_file, company, program, type,
                                        proposed_json, excerpt, email_from, email_subject,
                                        email_date, detected_at, state)
               VALUES (?,?,?,?,?,?,?,?,?,?,?, 'pending')
               ON CONFLICT(message_id) DO NOTHING""",
            (suggestion["message_id"], suggestion["record_file"], suggestion.get("company"),
             suggestion.get("program"), suggestion["type"], _dump(suggestion.get("proposed")),
             suggestion.get("excerpt"), suggestion.get("email_from"),
             suggestion.get("email_subject"), suggestion.get("email_date"),
             suggestion.get("detected_at") or utc_now()),
        )
    return int(cur.lastrowid) if cur.rowcount else None


def pending_suggestions(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM suggestions WHERE state = 'pending' ORDER BY id").fetchall()


def get_suggestion(conn: sqlite3.Connection, suggestion_id: int):
    return conn.execute("SELECT * FROM suggestions WHERE id = ?", (suggestion_id,)).fetchone()


def suggestion_for_message(conn: sqlite3.Connection, message_id: str):
    return conn.execute("SELECT * FROM suggestions WHERE message_id = ?", (message_id,)).fetchone()


def set_suggestion_state(conn: sqlite3.Connection, suggestion_id: int, state: str,
                         decided_at: str | None = None) -> bool:
    with conn:
        cur = conn.execute("UPDATE suggestions SET state = ?, decided_at = ? WHERE id = ?",
                           (state, decided_at or utc_now(), suggestion_id))
    return cur.rowcount > 0


def count_pending_suggestions(conn: sqlite3.Connection) -> int:
    return int(conn.execute(
        "SELECT COUNT(*) FROM suggestions WHERE state = 'pending'").fetchone()[0])


# --------------------------------------------------------------------------- #
# logos (M9)
# --------------------------------------------------------------------------- #

def logo_candidates(conn: sqlite3.Connection, limit: int = 500) -> list[sqlite3.Row]:
    """Distinct companies with a logo URL that have no cached logo yet."""
    return conn.execute(
        """SELECT j.company_norm, MIN(j.company) AS company, MIN(j.logo_source_url) AS url
             FROM jobs j
             LEFT JOIN logos l ON l.company_norm = j.company_norm
            WHERE j.logo_source_url IS NOT NULL AND j.company_norm IS NOT NULL
              AND (l.company_norm IS NULL
                   OR ((l.file IS NULL OR l.source_url LIKE 'link:%')
                       AND (l.failed_at IS NULL
                            OR l.failed_at < strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-7 days'))))
            GROUP BY j.company_norm
            ORDER BY COUNT(*) DESC, j.company_norm
            LIMIT ?""", (limit,)).fetchall()


def get_logo(conn: sqlite3.Connection, company_norm: str):
    return conn.execute("SELECT * FROM logos WHERE company_norm = ?", (company_norm,)).fetchone()


def set_logo(conn: sqlite3.Connection, company_norm: str, file: str | None, source_url: str | None,
             error: str | None = None) -> None:
    now = utc_now()
    with conn:
        conn.execute(
            """INSERT INTO logos (company_norm, file, source_url, fetched_at, failed_at, error)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(company_norm) DO UPDATE SET
                   file = excluded.file, source_url = excluded.source_url,
                   fetched_at = excluded.fetched_at, failed_at = excluded.failed_at,
                   error = excluded.error""",
            (company_norm, file, source_url, now if file else None, None if file else now, error),
        )


LINK_PREFIX = "link:"


def set_logo_link(conn: sqlite3.Connection, company_norm: str, file: str, target_key: str) -> None:
    """Point a company at another key's logo file (a matched company site icon).

    A link never replaces the company's own logo, and keeps any recorded failure so a
    failed own download still waits its week.
    """
    with conn:
        conn.execute(
            """INSERT INTO logos (company_norm, file, source_url, fetched_at)
               VALUES (?,?,?,?)
               ON CONFLICT(company_norm) DO UPDATE SET
                   file = excluded.file, source_url = excluded.source_url
               WHERE logos.file IS NULL OR logos.source_url LIKE 'link:%'""",
            (company_norm, file, LINK_PREFIX + target_key, utc_now()))


def record_logo_failure(conn: sqlite3.Connection, company_norm: str, source_url: str | None,
                        error: str) -> None:
    """A failed own download. A linked logo stays shown; the failure still waits 7 days."""
    row = get_logo(conn, company_norm)
    if row is not None and row["file"] and str(row["source_url"] or "").startswith(LINK_PREFIX):
        with conn:
            conn.execute("UPDATE logos SET failed_at = ?, error = ? WHERE company_norm = ?",
                         (utc_now(), error, company_norm))
        return
    set_logo(conn, company_norm, None, source_url, error=error)


def logo_map(conn: sqlite3.Connection) -> dict[str, str]:
    return {row["company_norm"]: row["file"] for row in
            conn.execute("SELECT company_norm, file FROM logos WHERE file IS NOT NULL")}
