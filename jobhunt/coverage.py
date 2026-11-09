"""Bounded employer recovery queries and honest, persistent coverage reporting.

A successful query means one public result page was inspected, never that every
vacancy was found. Company keywords are not verified employer-ID filters.
"""
from __future__ import annotations

import json
import math
import re
from datetime import datetime, timedelta, timezone

from . import store
from .http import FetchError, HostStopped, PoliteFetcher
from .sources import jobsdb, linkedin

BOARDS = {'linkedin': (linkedin.HOST, linkedin.PAGE_SIZE), 'jobsdb': ('th.jobsdb.com', jobsdb.PAGE_SIZE)}


def _stamp(now=None):
    if isinstance(now, str):
        return now
    return (now or datetime.now(timezone.utc)).isoformat()


def _date(value):
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError, AttributeError):
        return None


def _key(kind, source, target, query):
    return json.dumps([kind, source, target, query], ensure_ascii=False)


def ensure_schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS coverage_checks (
        check_key TEXT PRIMARY KEY, kind TEXT NOT NULL, source TEXT NOT NULL,
        target TEXT NOT NULL, query TEXT NOT NULL, status TEXT NOT NULL,
        last_attempt TEXT NOT NULL, last_success TEXT, found INTEGER NOT NULL,
        matched INTEGER NOT NULL, error TEXT, bounded INTEGER NOT NULL DEFAULT 1
    )''')
    conn.commit()


def _checks(conn):
    exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='coverage_checks'").fetchone()
    if not exists:
        return {}
    return {row['check_key']: dict(row) for row in conn.execute('SELECT * FROM coverage_checks')}


def _record(conn, row):
    conn.execute('''INSERT INTO coverage_checks
        (check_key,kind,source,target,query,status,last_attempt,last_success,found,matched,error,bounded)
        VALUES (:check_key,:kind,:source,:target,:query,:status,:last_attempt,:last_success,:found,:matched,:error,:bounded)
        ON CONFLICT(check_key) DO UPDATE SET status=excluded.status,
        last_attempt=excluded.last_attempt,
        last_success=COALESCE(excluded.last_success,coverage_checks.last_success),
        found=excluded.found,matched=excluded.matched,error=excluded.error,bounded=excluded.bounded''', row)
    conn.commit()


def targets(cfg):
    result = []
    seen = set()
    for entry in cfg.get('coverage.targets', []) or []:
        name = str(entry.get('name') or '').strip()
        if not name or name in seen:
            continue
        seen.add(name)
        queries = list(dict.fromkeys(str(q).strip() for q in entry.get('queries', [name]) if str(q).strip()))
        aliases = [name, *queries, *entry.get('aliases', [])]
        result.append({**entry, 'name': name, 'queries': queries,
                       'aliases': {store.normalise_text(a) for a in aliases}})
    return result


def _tasks(cfg, source):
    return [dict(check_key=_key('search', source, t['name'], q), kind='search',
                 source=source, target=t['name'], query=q, aliases=t['aliases'])
            for t in targets(cfg) for q in t['queries']]


def _requests(fetcher, host):
    return int(fetcher.stats().get(host, {}).get('requests', 0))


def _parse(source, text):
    if source == 'jobsdb':
        payload = json.loads(text)
        if not isinstance(payload, dict) or not isinstance(payload.get('data'), list):
            raise ValueError('missing JobsDB data array')
        if any(not isinstance(row, dict) for row in payload['data']):
            raise ValueError('malformed JobsDB result row')
        parsed = jobsdb.parse_search(payload)
        if payload['data'] and not parsed:
            raise ValueError('nonempty JobsDB payload yielded no job identities')
        return parsed
    parsed = linkedin.parse_cards(text)
    if parsed:
        return parsed
    # An empty guest fragment/list is a valid zero-result page. An HTML app shell
    # or damaged job cards are not evidence that the employer has no vacancies.
    if not text.strip() or re.search(r'<ul[^>]*>\s*</ul>', text, re.I) or 'no-results' in text.lower():
        return []
    raise ValueError('LinkedIn page has no recognizable jobs or empty-result marker')


def collect(cfg, conn, fetcher, source, *, search_cap, dry_run=False, now=None, log=None):
    """Recover one page per rotating company query, inside the source search cap."""
    if not cfg.get('coverage.enabled', False) or not cfg.get(f'sources.{source}', False) or source not in BOARDS:
        return [], []
    log = log or (lambda *a: None)
    if not dry_run:
        ensure_schema(conn)
    previous = _checks(conn)
    tasks = _tasks(cfg, source)
    # Stable ordering among never-tried checks; oldest actual attempts thereafter.
    tasks.sort(key=lambda t: (previous.get(t['check_key'], {}).get('last_attempt', ''), t['check_key']))
    host, page_size = BOARDS[source]
    allowance = max(0, int(cfg.get(f'coverage.{source}_requests', 0)))
    count = min(allowance, max(0, search_cap - _requests(fetcher, host)))
    stamp = _stamp(now)
    cutoff = (_date(stamp) or datetime.now(timezone.utc)) - timedelta(days=float(cfg.get('coverage.lookback_days', 30)))
    found, reports = [], []
    for task in tasks[:count]:
        if _requests(fetcher, host) >= search_cap or host in fetcher.stopped_hosts():
            break  # Unattempted checks stay pending, not falsely recorded as failed.
        row = {k: v for k, v in task.items() if k != 'aliases'}
        row.update(status='error', last_attempt=stamp, last_success=None,
                   found=0, matched=0, error=None, bounded=1)
        location = cfg.get('coverage.location', 'Thailand')
        url = (linkedin.search_url(task['query'], location, 0, cfg.get('coverage.lookback_days', 30))
               if source == 'linkedin' else jobsdb.search_url(task['query'], location, 1))
        try:
            response = fetcher.get(url, **({'accept': 'application/json'} if source == 'jobsdb' else {}))
            if not response.ok:
                raise ValueError(f'HTTP {response.status}')
            jobs = _parse(source, response.text)
            kept = [j for j in jobs if not _date(j.get('posted_at')) or _date(j['posted_at']) >= cutoff]
            found.extend(kept)
            row.update(status='partial' if len(jobs) >= page_size else ('ok' if jobs else 'empty'),
                       last_success=stamp, found=len(kept),
                       matched=sum(store.normalise_text(j.get('company')) in task['aliases'] for j in kept))
        except HostStopped as exc:
            row.update(status='blocked', error=str(exc))
        except (FetchError, ValueError, TypeError, AttributeError, KeyError) as exc:
            row['error'] = f'{type(exc).__name__}: {exc}'
        if not dry_run:
            _record(conn, row)
        reports.append(row)
        log(f"coverage {source} / {task['target']} / {task['query']}: {row['status']}, {row['matched']} employer matches")
    return found, reports


def record_direct(cfg, conn, reports, now=None):
    if not cfg.get('coverage.enabled', False):
        return
    ensure_schema(conn)
    stamp = _stamp(now)
    for item in reports:
        name, adapter = item['company'], item['adapter']
        status = item['status']
        if adapter == 'none':
            status = 'unsupported'
        elif adapter in ('watch', 'json_watch') and status == 'ok':
            status = 'watch'
        row = dict(check_key=_key('direct', adapter, name, ''), kind='direct', source=adapter,
                   target=name, query='', status=status, last_attempt=stamp,
                   last_success=stamp if status in ('ok', 'watch') else None,
                   found=item.get('postings', 0), matched=item.get('kept', 0),
                   error=item.get('error'), bounded=1)
        _record(conn, row)


def cadence(cfg):
    """Best-case rotation under default discovery budgets, not a schedule promise.

    Constructing a fetcher only reads configuration; no requests are issued.
    Custom discovery budgets, other host use and blocking can lengthen a cycle.
    """
    from .pipeline import search_budgets
    budgets = search_budgets(cfg, PoliteFetcher(cfg.profile))
    total = sum(len(t['queries']) for t in targets(cfg))
    result = {}
    for source in BOARDS:
        configured = max(0, int(cfg.get(f'coverage.{source}_requests', 0)))
        enabled = bool(cfg.get('coverage.enabled', False) and cfg.get(f'sources.{source}', False))
        effective = min(configured, budgets.get(source, 0)) if enabled else 0
        result[source] = dict(query_count=total, configured_requests=configured,
                              default_search_cap=budgets.get(source, 0),
                              effective_requests=effective,
                              minimum_runs_per_cycle=math.ceil(total / effective) if effective else None)
    return result


def report(cfg, conn, now=None):
    """Inspect state only. Never fetch or write tables, timestamps, or queue state."""
    stamp = _stamp(now)
    cutoff = (_date(stamp) or datetime.now(timezone.utc)) - timedelta(days=float(cfg.get('coverage.stale_after_days', 10)))
    checks = _checks(conn)
    enabled = bool(cfg.get('coverage.enabled', False))
    rows = []
    for source in BOARDS:
        for task in _tasks(cfg, source):
            old = checks.get(task['check_key'], {})
            status = old.get('status', 'never')
            success = _date(old.get('last_success'))
            disabled = not enabled or not cfg.get(f'sources.{source}', False)
            rows.append({**{k: v for k, v in task.items() if k != 'aliases'}, **old,
                         'status': 'disabled' if disabled else status,
                         'stale': bool(not disabled and (success is None or success < cutoff)),
                         'last_attempt': old.get('last_attempt'), 'last_success': old.get('last_success')})
    for item in cfg.get('companies', []):
        name, adapter = item['name'], item['adapter']
        old = checks.get(_key('direct', adapter, name, ''), {})
        status = old.get('status', 'unsupported' if adapter == 'none' else 'never')
        success = _date(old.get('last_success'))
        disabled = not enabled or not cfg.get('sources.companies', False)
        rows.append(dict(kind='direct', source=adapter, target=name, query='', **{
            'status': 'disabled' if disabled else status,
            'last_attempt': old.get('last_attempt'), 'last_success': old.get('last_success'),
            'stale': bool(not disabled and adapter != 'none' and (success is None or success < cutoff)),
            'found': old.get('found', 0), 'matched': old.get('matched', 0),
            'error': old.get('error'), 'bounded': True}))
    counts = {}
    for row in rows:
        counts[row['status']] = counts.get(row['status'], 0) + 1
    warnings = []
    never = counts.get('never', 0)
    failed = sum(counts.get(s, 0) for s in ('error', 'blocked'))
    stale = sum(r['stale'] and r['status'] not in ('never', 'error', 'blocked') for r in rows)
    if never: warnings.append(f'{never} configured checks have never run')
    if failed: warnings.append(f'{failed} checks last failed or were blocked')
    if stale: warnings.append(f'{stale} checks have no recent successful result')
    if counts.get('partial'): warnings.append(f"{counts['partial']} checks are partial or reached the first-page limit")
    if counts.get('unsupported'): warnings.append(f"{counts['unsupported']} configured direct entries have unsupported adapters")
    configured_direct = {item['name'] for item in cfg.get('companies', [])}
    board_only = [t['name'] for t in targets(cfg)
                  if not configured_direct.intersection([t['name'], *t.get('direct_names', [])])]
    if enabled and board_only:
        warnings.append(f'{len(board_only)} cross-search targets have no configured direct feed or watch')
    rotation = cadence(cfg)
    for source, cycle in rotation.items():
        if enabled and cfg.get(f'sources.{source}', False) and cycle['query_count']:
            runs = cycle['minimum_runs_per_cycle']
            if runs is None:
                warnings.append(f'{source} company checks cannot progress with zero recovery budget')
            elif runs > float(cfg.get('coverage.stale_after_days', 10)):
                warnings.append(f'{source} needs at least {runs} runs per company cycle; at one run/day '
                                'this exceeds the stale window (actual scheduling is not assumed)')
    industries = {}
    for target in targets(cfg):
        industry = target.get('industry', 'unclassified')
        industries[industry] = industries.get(industry, 0) + 1
    latest = conn.execute("SELECT MAX(finished_at) FROM runs WHERE command IN ('run', 'discover')").fetchone()[0] if conn.execute("SELECT 1 FROM sqlite_master WHERE name='runs'").fetchone() else None
    if enabled and latest is None:
        warnings.append('No discovery/run history recorded; automatic scheduling is not established')
    elif enabled and (_date(latest) is None or _date(latest) < cutoff):
        warnings.append('Latest recorded run is stale; check that discovery is still being invoked')
    observations = {'available': False}
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='job_sightings'").fetchone():
        multi = conn.execute('SELECT COUNT(*) FROM (SELECT job_id FROM job_sightings GROUP BY job_id HAVING COUNT(DISTINCT source)>1)').fetchone()[0]
        observations = {'available': True, 'multiple_source_jobs': multi}
    return dict(enabled=enabled, at=stamp, target_count=len(targets(cfg)), checks=rows,
                status_counts=counts, warnings=warnings, observations=observations,
                cadence=rotation, industry_target_counts=industries, board_only_targets=board_only,
                latest_recorded_run=latest,
                limitation='Bounded keyword checks; single-source jobs remain valid leads. No completeness or active-scheduler guarantee.')


def format_report(data):
    lines = [f"Coverage: {data['target_count']} employer targets; enabled={data['enabled']}",
             data['limitation'], f"Latest recorded run: {data['latest_recorded_run'] or 'none'}"]
    for source, cycle in data.get('cadence', {}).items():
        runs = cycle['minimum_runs_per_cycle']
        lines.append(f"{source}: {cycle['query_count']} query variants / "
                     f"{cycle['effective_requests']} company requests per default run; "
                     f"minimum cycle: {str(runs) + ' runs' if runs is not None else 'no progress'}")
    lines.extend('WARNING: ' + w for w in data['warnings'])
    for row in data['checks']:
        lines.append(f"{row['target']} | {row['source']} | {row['query'] or 'direct'} | "
                     f"{row['status']}{' / stale' if row['stale'] else ''} | "
                     f"last attempt: {row['last_attempt'] or 'never'} | "
                     f"last success: {row['last_success'] or 'never'}")
        if row.get('error'):
            lines.append('  ' + row['error'])
    return '\n'.join(lines)
