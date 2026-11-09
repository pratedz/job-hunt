"""Helpers for the board tests: temporary vaults, record text, seeded databases."""

from __future__ import annotations

import json
import shutil
from datetime import date
from pathlib import Path

from _util import CFG, memory_conn, reply, entry, facts, paid

from jobhunt import score as score_mod, store
from jobhunt.vault import Vault

TODAY = date(2026, 11, 7)


def record_text(title, company="Acme", program="Management Trainee", my_status="To apply",
                link="https://example.com/jobs/1", section="Apply now", date_applied=None,
                next_step="Submit via the apply link. Cover email draft: pending",
                do_by="2026-11-10", extra=None) -> str:
    lines = [
        "---",
        f'title: "{title}"',
        'type: "record"',
        'folder: "61 Application Records"',
        'status: "current"',
        'updated: "2026-11-01"',
        f'summary: "{company} — {program}: {my_status}, section {section}, do by {do_by}"',
        'database: "Applications"',
        f'link: "{link}"',
        'section: "%s"' % section,
        'do_by: "%s"' % do_by,
        f'my_status: "{my_status}"',
        f'next_step: "{next_step}"',
        f'program: "{program}"',
        f'company: "{company}"',
    ]
    if date_applied:
        lines.append(f'date_applied: "{date_applied}"')
    if extra:
        lines.extend(extra)
    lines += ['tags: ["type/record", "db/applications"]', "---", "",
              "Record in [[2027 Applications]] (Applications).", ""]
    return "\n".join(lines)


def make_vault(tmp) -> Vault:
    """A temporary vault with a couple of application records."""
    root = Path(tmp) / "Career-Wiki"
    records = root / "wiki" / "61 Application Records"
    records.mkdir(parents=True, exist_ok=True)
    (records / "Acme — Management Trainee.md").write_text(
        record_text("Acme — Management Trainee", link="https://example.com/jobs/1"), encoding="utf-8")
    (records / "Beta Corp — Analyst.md").write_text(
        record_text("Beta Corp — Analyst", company="Beta Corp", program="Analyst",
                    my_status="Applied", link="https://example.com/beta",
                    date_applied="2026-10-24"), encoding="utf-8")
    (records / "Old note with no status.md").write_text(
        record_text("Old note", company="Old Co", program="Old Job", my_status="To apply", link="")
        .replace('my_status: "To apply"\n', ""), encoding="utf-8")
    return Vault(root)


def copy_real_vault(tmp) -> Vault | None:
    """A copy of the real Career-Wiki, so tests can edit records without risk."""
    source = Path(CFG.get("vault.path"))
    if not (source / "wiki" / "61 Application Records").is_dir():
        return None
    target = Path(tmp) / "Career-Wiki"
    shutil.copytree(source / "wiki", target / "wiki")
    return Vault(target)


def seeded_db(test, path=None):
    """A database with one job per interesting state. Returns (conn, ids).

    With ``path`` it is a real file database (needed when a server reads it).
    """
    if path:
        conn = store.connect(path)
        test.addCleanup(conn.close)
    else:
        conn = memory_conn(test)
    ids = {}

    def add(name, title, company="Acme", url=None, verdict="derive", rule=None, error=None,
            circles=(5, 4, 4, 3), gate="pass", fails=(), questions=(), paid_obj=None,
            salary_label=None, jd_used=1, jd_missing=0, source="manual"):
        job_id, _ = store.add_job(conn, {
            "source": source, "title": title, "company": company,
            "location": "Bangkok, Thailand", "url": url or f"https://example.com/jobs/{title}",
            "jd_text": f"A {title} role with a long description.",
            "salary_text": salary_label,
        })
        if jd_missing:
            store.mark_jd_missing(conn, job_id, source)
        ids[name] = job_id
        if verdict == "NONE":
            return job_id      # never scored
        record = {"created_at": store.utc_now()}
        if rule:
            record.update({"status": "prefiltered", "prefilter_rule": rule, "verdict": "FILTERED",
                           "gate_reasons": [], "gate_questions": []})
        elif error:
            record.update({"status": "error", "error": error})
        else:
            computed = score_mod.decide(
                {"love": circles[0], "good_at": circles[1], "needs": circles[2], "paid": circles[3]},
                gate)
            record.update({
                "status": "scored",
                "verdict": computed["verdict"] if verdict == "derive" else verdict,
                "gate_result": gate,
                "gate_reasons": list(fails), "gate_questions": list(questions),
                "love": circles[0], "good_at": circles[1], "needs": circles[2], "paid": circles[3],
                "love_reason": "love reason", "good_at_reason": "good reason",
                "needs_reason": "needs reason", "paid_reason": "paid reason",
                "paid_estimated": 1 if paid_obj and not paid_obj.get("stated") else 0,
                "paid_thb_month_mid": 40000.0 if paid_obj else None,
                "paid_obj": paid_obj,
                "facts": facts(job_type="graduate_programme", location_city="Bangkok",
                               location_country="Thailand"),
                "summary": "A short summary.",
                "jd_used": jd_used,
                "created_at": store.utc_now(), "scored_at": store.utc_now(),
            })
            record["weakest_circle"] = computed["weakest_circle"]
            record["weakest_score"] = computed["weakest_score"]
            record["mean_score"] = computed["mean_score"]
        store.insert_score(conn, job_id, record)
        return job_id

    add("centre", "Graduate Programme", url="https://example.com/jobs/centre",
        paid_obj={"salary_min": 30000, "salary_max": 50000, "currency": "THB",
                  "period": "month", "stated": False, "reason": "estimate"})
    add("hold", "Rotational Associate", circles=(5, None, 4, 4))
    add("low", "Key Account Executive", circles=(2, 3, 4, 4))
    add("filtered", "Junior Trainee", gate="fail",
        fails=["Requires a minimum GPA of 3.00; your GPAX is 2.48 (transcript)."],
        questions=["Hiring immediately; you are available from 2027-06."])
    add("prefiltered", "Senior Analyst", rule="title_exclude: \\bsenior\\b")
    add("skipped", "Business Development Executive")
    add("unscored", "Fresh Graduate Programme", verdict="NONE")
    add("errored", "Strategy Analyst", verdict=None, error="codex timeout after 300s")
    # scored from a snippet after the fetch failed: visible, but weak evidence
    add("snippet", "Snippet Only Role", circles=(3, 2, 3, 3), jd_used=0, jd_missing=1,
        source="linkedin", url="https://example.com/jobs/snippet")
    # pasted by hand: no page to fetch, so a score without a JD is all there is
    add("manual", "Pasted Role", circles=(4, 4, 4, 4), jd_used=0, source="manual",
        url="https://example.com/jobs/pasted")
    # scored before its description arrived: must look like "waiting to be scored"
    add("stale", "Waiting For Rescore", circles=(5, 5, 5, 5), jd_used=0,
        source="linkedin", url="https://example.com/jobs/stale")
    store.set_decision(conn, ids["skipped"], "skip")
    return conn, ids


def scored_reply(job_ids):
    """A valid codex reply for the given ids."""
    return reply(*[entry(job_id, 5, 4, 4, pay=paid(30000, 50000, "THB", "month", False, "est"))
                   for job_id in job_ids])
