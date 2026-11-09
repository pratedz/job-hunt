"""Re-run the current pre-filter over jobs that an older rule pre-filtered (M12).

A job that now passes is **released**: its prefilter score row stops being current
(the row stays in history), so the job is unscored again and joins the JD-fetch and
scoring queues, where priority titles go first. A job that is still filtered keeps a
prefilter row, re-labelled with today's rule when the reason changed. No request, no
model call. Gate-filtered jobs (scored, then FILTERED by a gate) are not touched.
"""

from __future__ import annotations

from . import prefilter, store

PREFILTERED_SQL = """
SELECT j.id, j.title, j.posted_at, s.id AS score_id, s.prefilter_rule
  FROM jobs j
  JOIN scores s ON s.job_id = j.id AND s.is_current = 1
 WHERE s.status = 'prefiltered'
 ORDER BY j.id
"""


def run(cfg, conn, dry_run: bool = False, now=None) -> dict:
    """Returns ``{"checked", "released", "still", "relabelled", "by_rule": {old rule:
    {"released", "still"}}, "released_ids": [...]}``."""
    report = {"checked": 0, "released": 0, "still": 0, "relabelled": 0, "by_rule": {},
              "released_ids": []}
    for row in conn.execute(PREFILTERED_SQL).fetchall():
        report["checked"] += 1
        old = row["prefilter_rule"] or "?"
        counts = report["by_rule"].setdefault(old, {"released": 0, "still": 0})
        hit = prefilter.check({"title": row["title"], "posted_at": row["posted_at"]},
                              cfg.profile, now=now)
        if hit is None:
            counts["released"] += 1
            report["released"] += 1
            report["released_ids"].append(row["id"])
            if not dry_run:
                release(conn, row["score_id"])
            continue
        counts["still"] += 1
        report["still"] += 1
        if hit != old:
            report["relabelled"] += 1
            if not dry_run:
                store.insert_score(conn, row["id"], {
                    "status": "prefiltered", "prefilter_rule": hit, "verdict": "FILTERED",
                    "gate_reasons": [], "gate_questions": [], "created_at": store.utc_now()})
    return report


def release(conn, score_id: int) -> None:
    """The prefilter row leaves the current view; history keeps it."""
    with conn:
        conn.execute("UPDATE scores SET is_current = 0 WHERE id = ? AND status = 'prefiltered'",
                     (score_id,))


def format_report(report: dict, dry_run: bool = False) -> str:
    lines = [f"reprefilter{' (dry run, nothing written)' if dry_run else ''}: "
             f"{report['checked']} pre-filtered jobs checked, {report['released']} released, "
             f"{report['still']} still filtered ({report['relabelled']} with a new reason)"]
    width = max((len(rule) for rule in report["by_rule"]), default=10)
    for rule, counts in sorted(report["by_rule"].items(),
                               key=lambda item: (-item[1]["released"], item[0])):
        lines.append(f"  {rule:<{width}}  released {counts['released']:>4}  "
                     f"still {counts['still']:>4}")
    return "\n".join(lines)
