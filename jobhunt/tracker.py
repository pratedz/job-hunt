"""The application tracker: the vault records, seen through the job database.

Read-only matching decides whether a job is already tracked; Apply creates one
record; Mark submitted edits four keys of one record. Nothing here deletes.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from . import vault as vault_mod
from .store import canonical_url, normalise_text

CIRCLES = ("love", "good_at", "needs", "paid")
TO_APPLY = "To apply"
APPLY_NOW = "Apply now"


def _today() -> date:
    return datetime.now(timezone.utc).astimezone().date()


class Tracker:
    def __init__(self, vault: vault_mod.Vault, today=None):
        self.vault = vault
        self.today = today or _today
        self.reload()

    # -- loading and matching ---------------------------------------------- #
    def reload(self) -> None:
        self.records = self.vault.records()
        self.by_url: dict[str, vault_mod.Record] = {}
        self.by_name: dict[tuple[str, str], vault_mod.Record] = {}
        for record in self.records:
            for key in ("link", "apply_link", "linkedin_link"):
                url = canonical_url(str(record.get(key) or ""))
                if url:
                    self.by_url.setdefault(url, record)
            company = normalise_text(str(record.get("company") or ""))
            program = normalise_text(str(record.get("program") or record.get("title") or ""))
            if company and program:
                self.by_name.setdefault((company, program), record)

    def find_for_job(self, job) -> vault_mod.Record | None:
        url = canonical_url(job["url"]) if job["url"] else None
        if url and url in self.by_url:
            return self.by_url[url]
        company = normalise_text(job["company"])
        title = normalise_text(job["title"])
        if company and title:
            return self.by_name.get((company, title))
        return None

    def status_for(self, job, decision: str | None) -> str:
        record = self.find_for_job(job)
        if record is not None:
            return "to_apply" if str(record.get("my_status")) == TO_APPLY else "applied"
        if decision == "skip":
            return "skipped"
        return "new"

    def tracked_info(self, job) -> dict | None:
        record = self.find_for_job(job)
        if record is None:
            return None
        return {"file": record.rel, "obsidian_url": record.obsidian_url(),
                "my_status": record.get("my_status")}

    # -- applications list -------------------------------------------------- #
    def applications(self, status: str | None = None) -> list[dict]:
        today = self.today()
        rows = []
        for record in self.records:
            my_status = record.get("my_status")
            if my_status is None:
                continue          # older records with no status are not board material
            if status == "to_apply":
                # "To apply" means he still has to act on it: queued in Apply now.
                if str(my_status) != TO_APPLY or str(record.get("section")) != APPLY_NOW:
                    continue
            if status == "applied" and str(my_status) == TO_APPLY:
                continue
            rows.append((record, {
                "record_file": record.rel,
                "obsidian_url": record.obsidian_url(),
                "company": record.get("company"),
                "program": record.get("program"),
                "my_status": my_status,
                "section": record.get("section"),
                "date_applied": record.get("date_applied"),
                "days_waiting": vault_mod.days_between(record.get("date_applied"), today),
                "next_step": record.get("next_step"),
                "jobhunt_id": record.get("jobhunt_id"),
                "link": record.get("link") or record.get("apply_link"),
                # §M10: where "Apply page" goes; an empty property counts as missing
                "apply_link": _text(record.get("apply_link")) or _text(record.get("link")),
                "outcome_stage": record.get("outcome_stage"),
                "outcome_reason_category": record.get("outcome_reason_category"),
                "outcome_reason": record.get("outcome_reason"),
                "outcome_date": record.get("outcome_date"),
            }))
        if status == "to_apply":
            # Sorted by his own target date (do_by, never the employer's deadline);
            # records without one go last.
            rows.sort(key=lambda pair: (str(pair[0].get("do_by") or "9999-99-99"),
                                        str(pair[0].get("company") or "")))
        elif status == "applied":
            rows.sort(key=lambda pair: str(pair[1].get("date_applied") or ""), reverse=True)
        return [row for _record, row in rows]

    # -- writing ------------------------------------------------------------ #
    def apply(self, job, score_row, today: date | None = None, apply_url: str | None = None,
              apply_via: str | None = None) -> tuple[vault_mod.Record, bool]:
        """Create the tracker record for one job. Returns (record, created).

        ``apply_url`` is the application page Apply opened (M10); ``link`` stays the
        posting. No cover email is mentioned: one is drafted only on demand.
        """
        existing = self.find_for_job(job)
        if existing is not None:
            return existing, False
        today = today or self.today()
        facts = _facts(score_row)
        scores = {circle: score_row[circle] if score_row else None for circle in CIRCLES}
        estimated = bool(score_row["paid_estimated"]) if score_row else False
        verdict = score_row["verdict"] if score_row else None
        gate = score_row["gate_result"] if score_row else None
        questions = _json_list(score_row["gate_questions_json"]) if score_row else []
        summary = score_row["summary"] if score_row else None
        title = vault_mod.record_title(job["company"], job["title"])
        apply_link = apply_url or job["url"] or ""
        platform = APPLY_PLATFORMS.get(apply_via or "") or \
            vault_mod.PLATFORMS.get((job["source"] or "").casefold(), "Other")
        where = APPLY_WHERE.get(apply_via or "", "the apply link")
        fields = {
            "title": title,
            "type": "record",
            "folder": vault_mod.RECORDS_DIR,
            "status": "current",
            "updated": today.isoformat(),
            "summary": f"{vault_mod.record_title(job['company'], job['title'])}: "
                       f"{TO_APPLY}, section Apply now, do by {vault_mod.do_by_date(today)}",
            "database": "Applications",
            "link": job["url"] or "",
            "apply_link": apply_link,
            "company": job["company"] or "Unknown company",
            "program": job["title"] or "Untitled role",
            "section": "Apply now",
            "my_status": TO_APPLY,
            "next_step": f"Apply via {where}: {apply_link}. Then press Mark submitted."
                         if apply_link else "Find the apply page, apply, then press Mark submitted.",
            "do_by": vault_mod.do_by_date(today),
            "note": _note(summary, questions, job, today),
            "job_platform": platform,
            "location": job["location"] or facts.get("location_city") or "",
            "job_type": vault_mod.JOB_TYPES.get(str(facts.get("job_type") or ""), "Full-time"),
            "ikigai": _ikigai(scores, estimated),
            "ikigai_verdict": verdict or "",
            "jobhunt_id": int(job["id"]),
            "tags": list(vault_mod.TAGS),
        }
        record = self.vault.new_record(fields)
        self.vault.write_record(record)
        self.reload()
        return record, True

    def mark_submitted(self, record_file: str, today: date | None = None) -> vault_mod.Record:
        today = today or self.today()
        record = self.vault.record(record_file)
        if record is None:
            raise vault_mod.VaultError(f"no record {record_file}")
        updates = {
            "my_status": "Applied",
            "date_applied": today.isoformat(),
            "updated": today.isoformat(),
            "next_step": f"Submitted {today.isoformat()}. Watch email (incl. spam) for the "
                         "test or interview invite.",
        }
        merged = dict(record.fields)
        merged.update(updates)
        merged.setdefault("company", record.get("company"))
        merged.setdefault("program", record.get("program"))
        updates["summary"] = vault_mod.summary_line(vault_mod.Record(
            path=record.path, fields=merged, lines=record.lines))
        self.vault.update_keys(record, updates)
        self.reload()
        return record


APPLY_PLATFORMS = {"linkedin": "LinkedIn", "jobsdb": "JobsDB",
                   "company_site": "Company career site"}
APPLY_WHERE = {"linkedin": "LinkedIn", "jobsdb": "JobsDB", "company_site": "the company site"}


def _text(value) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _facts(score_row) -> dict:
    if score_row is None or not score_row["facts_json"]:
        return {}
    import json
    try:
        return json.loads(score_row["facts_json"]) or {}
    except (TypeError, ValueError):
        return {}


def _json_list(value) -> list:
    import json
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _ikigai(scores: dict, estimated: bool) -> str:
    letters = {"love": "L", "good_at": "G", "needs": "N", "paid": "P"}
    parts = []
    for circle, letter in letters.items():
        value = scores.get(circle)
        if value is None:
            parts.append(f"{letter}?")
        elif circle == "paid" and estimated:
            parts.append(f"{letter}{value}e")
        else:
            parts.append(f"{letter}{value}")
    return " ".join(parts)


def _note(summary: str | None, questions: list, job, today: date) -> str:
    text = (summary or "Scored by my job hunt.").strip()
    if questions:
        text += " Gate questions: " + " ".join(str(q) for q in questions)
    found = job["posted_at"] or today.isoformat()
    return f"{text} Found by my job hunt ({job['source']}, {str(found)[:10]})."
