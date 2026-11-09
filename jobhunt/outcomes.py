"""Confirming, dismissing and recording outcomes, plus the lessons view (M6).

Every write here follows the M4 rules: only the listed keys change, the rest of the
note stays byte-identical, writes are atomic, nothing is deleted. Closed outcomes
move the record to ``section: "Archive"``, the way the existing tracker files them.
"""

from __future__ import annotations

import json
import re

from . import store
from . import vault as vault_mod

CLOSED_STATUSES = ("Rejected", "Ghosted", "Withdrawn")
OUTCOME_KEYS = ("my_status", "next_step", "outcome_stage", "outcome_reason_category",
                "outcome_reason", "outcome_date", "updated", "summary", "section")


def _clean(value, limit: int | None = None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:limit] if limit else text


def _confirmed_next_step(status, stage, row, record, today) -> str | None:
    """What next_step says once the change is real.

    A confirmed outcome is closed business, so the suggestion's imperative ("mark as
    ghosted and move on") is replaced by a statement of what happened.
    """
    when = today.isoformat() if today is not None else ""
    if status == "Ghosted":
        from .vault import days_between
        reference = (record.get("outcome_date") or record.get("date_applied")) if record else None
        waiting = days_between(reference, today) if (reference and today) else None
        if waiting is None and row is not None:
            match = re.search(r"(\d+) days", str(row["excerpt"] or ""))
            waiting = int(match.group(1)) if match else None
        days_text = f"{waiting} days" if waiting is not None else "the waiting period"
        return f"Closed: no reply after {days_text} (marked ghosted {when})."
    if status == "Rejected":
        return f"Closed: rejected at {stage or 'unknown'} ({when})."
    return None          # test / interview / offer keep the classifier's next step


def write_status(tracker, record_file: str, my_status: str, *, outcome_stage=None,
                 outcome_reason_category=None, outcome_reason=None, next_step=None,
                 today=None) -> str:
    """Write an outcome to one record. Returns the record file it touched."""
    today = today or tracker.today()
    record = tracker.vault.record(record_file)
    if record is None:
        raise vault_mod.VaultError(f"no record {record_file}")

    updates = {"my_status": my_status, "updated": today.isoformat(),
               "outcome_date": today.isoformat()}
    if outcome_stage:
        updates["outcome_stage"] = outcome_stage
    if outcome_reason_category:
        updates["outcome_reason_category"] = outcome_reason_category
    if outcome_reason:
        updates["outcome_reason"] = _clean(outcome_reason, 300)
    if next_step:
        updates["next_step"] = _clean(next_step, 300)
    if my_status in CLOSED_STATUSES:
        updates["section"] = "Archive"
        if not updates.get("outcome_reason_category"):
            updates["outcome_reason_category"] = ("ghosted" if my_status == "Ghosted"
                                                 else "no_reason_given")

    merged = dict(record.fields)
    merged.update(updates)
    merged.setdefault("company", record.get("company"))
    merged.setdefault("program", record.get("program"))
    updates["summary"] = vault_mod.summary_line(vault_mod.Record(
        path=record.path, fields=merged, lines=record.lines))
    tracker.vault.update_keys(record, updates)
    tracker.reload()
    return record.rel


def confirm(conn, tracker, suggestion_id: int, overrides: dict | None = None,
            today=None) -> dict:
    """Apply a pending suggestion, with optional category/note overrides."""
    row = store.get_suggestion(conn, suggestion_id)
    if row is None:
        raise LookupError(f"no suggestion {suggestion_id}")
    if row["state"] != "pending":
        raise ValueError(f"suggestion {suggestion_id} was already {row['state']}")
    proposed = json.loads(row["proposed_json"] or "{}")
    overrides = overrides or {}
    category = overrides.get("outcome_reason_category") or proposed.get("outcome_reason_category")
    note = overrides.get("note")
    reason = note or (row["excerpt"] if row["type"] == "rejection" else None)
    status = proposed.get("my_status") or "Applied"
    record = tracker.vault.record(row["record_file"])
    next_step = (_confirmed_next_step(status, proposed.get("outcome_stage"), row, record, today)
                 or proposed.get("next_step"))
    record_file = write_status(
        tracker, row["record_file"], status,
        outcome_stage=proposed.get("outcome_stage"),
        outcome_reason_category=category,
        outcome_reason=reason,
        next_step=next_step,
        today=today,
    )
    store.set_suggestion_state(conn, suggestion_id, "confirmed")
    return {"ok": True, "record_file": record_file}


def dismiss(conn, suggestion_id: int) -> dict:
    row = store.get_suggestion(conn, suggestion_id)
    if row is None:
        raise LookupError(f"no suggestion {suggestion_id}")
    store.set_suggestion_state(conn, suggestion_id, "dismissed")
    return {"ok": True}


def suggestion_row(row, tracker=None) -> dict:
    proposed = json.loads(row["proposed_json"] or "{}")
    current = None
    if tracker is not None:
        record = next((r for r in tracker.records if r.rel == row["record_file"]), None)
        current = record.get("my_status") if record is not None else None
    return {
        "id": row["id"],
        "current_status": current,
        "record_file": row["record_file"],
        "company": row["company"],
        "program": row["program"],
        "type": row["type"],
        "proposed": {
            "my_status": proposed.get("my_status"),
            "outcome_stage": proposed.get("outcome_stage"),
            "outcome_reason_category": proposed.get("outcome_reason_category"),
            "next_step": proposed.get("next_step"),
        },
        "excerpt": row["excerpt"],
        "email": ({"from": row["email_from"], "subject": row["email_subject"],
                   "date": row["email_date"]} if row["email_from"] else None),
        "detected_at": row["detected_at"],
    }


# --------------------------------------------------------------------------- #
# lessons
# --------------------------------------------------------------------------- #

def _weakest_from_ikigai(text: str | None) -> str | None:
    """'L5 G4 N4 P3e' -> 'paid'."""
    if not text:
        return None
    values = {}
    for letter, value in re.findall(r"([LGNP])\s*(\d+|-)", str(text)):
        if value.isdigit():
            values[{"L": "love", "G": "good_at", "N": "needs", "P": "paid"}[letter]] = int(value)
    if not values:
        return None
    return min(values, key=values.get)


def lessons(tracker) -> dict:
    """Every Rejected or Ghosted record, plus the aggregates the board shows."""
    rows = []
    for record in tracker.records:
        status = str(record.get("my_status") or "")
        if status not in ("Rejected", "Ghosted"):
            continue
        category = record.get("outcome_reason_category")
        if not category:
            # a legacy row with no category still has one true reading
            category = "ghosted" if status == "Ghosted" else "no_reason_given"
        rows.append({
            "company": record.get("company"),
            "program": record.get("program"),
            "my_status": status,
            "outcome_stage": record.get("outcome_stage"),
            "outcome_reason_category": category,
            # Never fall back to the free-text note: it holds working notes ("GPA 2.5–3.0"),
            # and a lesson must not smuggle them into a prompt.
            "outcome_reason": record.get("outcome_reason"),
            "outcome_date": record.get("outcome_date"),
            "ikigai": record.get("ikigai"),
            "weakest_circle": _weakest_from_ikigai(record.get("ikigai")),
        })
    # Newest first; a record with no outcome_date sorts by its application date without
    # that date being *reported* as the outcome date.
    order = {row["company"]: row for row in rows}
    by_date = {}
    for record in tracker.records:
        if str(record.get("my_status")) in ("Rejected", "Ghosted"):
            by_date[record.get("company")] = str(record.get("outcome_date")
                                                or record.get("date_applied") or "")
    rows.sort(key=lambda row: by_date.get(row.get("company"), ""), reverse=True)

    def tally(key):
        counts: dict[str, int] = {}
        for row in rows:
            value = row.get(key)
            if value:
                counts[str(value)] = counts.get(str(value), 0) + 1
        return counts

    return {
        "lessons": rows,
        "by_stage": tally("outcome_stage"),
        "by_category": tally("outcome_reason_category"),
        "by_weakest_circle": tally("weakest_circle"),
    }


def recent_outcomes_block(tracker, limit: int = 10) -> str:
    """The "Recent outcomes" section appended to the scoring prompt."""
    rows = lessons(tracker)["lessons"][:limit]
    if not rows:
        return ""
    lines = ["", "## Recent outcomes (calibration only, as rubric.md describes)"]
    for row in rows:
        parts = [f"- {row['company'] or 'unknown'} — {row['program'] or 'unknown'}: "
                 f"{row['my_status']}"]
        if row.get("outcome_stage"):
            parts.append(f"at {row['outcome_stage']}")
        if row.get("outcome_reason_category"):
            parts.append(f"({row['outcome_reason_category']})")
        reason = (row.get("outcome_reason") or "").strip()
        if reason:
            parts.append(f"— {reason[:160]}")
        lines.append(" ".join(parts).replace(" — — ", " — "))
    return "\n".join(lines)
