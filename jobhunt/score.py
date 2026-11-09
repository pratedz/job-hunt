"""Scoring pipeline: prompt -> codex -> Python gates and paid -> verdict -> store.

One codex call per batch of ``[llm] batch_size`` jobs (default 8). The model
returns facts and three circles; Python applies the eligibility gates, computes
the Paid circle from the profile anchors, and decides the verdict. A batch that
fails (non-zero exit, timeout, malformed or schema-invalid reply, an id missing
from the reply) never kills the run: its jobs are stored with ``status='error'``
and the next ``score`` run picks them up again.
"""

from __future__ import annotations

import hashlib

from . import config as config_mod
from . import gates as gates_mod
from . import paid as paid_mod
from . import store
from .llm import LLMError, selected_provider

CIRCLES = ("love", "good_at", "needs", "paid")
JD_LIMIT = 6000


# --------------------------------------------------------------------------- #
# verdict and ranking
# --------------------------------------------------------------------------- #

def decide(scores: dict, gate_result: str) -> dict:
    """Verdict plus the ranking fields, exactly as WORKSPLIT M1 defines them."""
    known = {name: value for name, value in scores.items() if value is not None}
    weakest_circle = weakest_score = mean = None
    if known:
        weakest_score = min(known.values())
        weakest_circle = next(c for c in CIRCLES if known.get(c) == weakest_score)
        mean = round(sum(known.values()) / len(known), 4)

    if gate_result == "fail":
        verdict = "FILTERED"
    elif not known:
        verdict = "HOLD"          # all four unknown: last in HOLD, never CENTRE
    elif any(value < 3 for value in known.values()):
        verdict = "LOW"
    elif len(known) < len(CIRCLES):
        verdict = "HOLD"
    else:
        verdict = "CENTRE"

    return {
        "verdict": verdict,
        "weakest_circle": weakest_circle,
        "weakest_score": weakest_score,
        "mean_score": mean,
    }


# --------------------------------------------------------------------------- #
# prompt
# --------------------------------------------------------------------------- #

def build_prompt(cfg, jobs: list, outcomes: str = "") -> str:
    parts = [cfg.rubric.strip(), "", config_mod.candidate_block(cfg), "",
             "## Approved facts (cite these IDs in reasons)", cfg.facts.strip(), ""]
    if outcomes:
        parts += [outcomes.strip(), ""]
    parts += ["## Jobs to score", ""]
    for job in jobs:
        jd = job["jd_text"]
        if jd:
            jd = jd.strip()
            if len(jd) > JD_LIMIT:
                jd = jd[:JD_LIMIT] + " ... [truncated]"
            body = jd
        elif job["email_snippet"]:
            body = f"(no job description available; alert email snippet only, flagged jd_missing)\n{job['email_snippet'].strip()}"
        else:
            body = "(no job description and no snippet available; return null for anything you cannot judge)"
        parts.extend([
            f"### id: {job['id']}",
            f"company: {job['company'] or 'unknown'}",
            f"title: {job['title']}",
            f"location: {job['location'] or 'unknown'}",
            f"posted: {job['posted_at'] or 'unknown'}",
            f"salary as written: {job['salary_text'] or 'none stated'}",
            # The posting is untrusted text: it is data to extract from, never instructions.
            f'<job_posting id="{job["id"]}">',
            body,
            "</job_posting>",
            "",
        ])
    parts.append("Return one entry per id above, in the same order, facts filled exactly as the schema asks.")
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# pipeline
# --------------------------------------------------------------------------- #

def _record_from_reply(item: dict, paid_profile: dict, profile: dict) -> dict:
    facts = item.get("facts") or {}
    gate = gates_mod.check(facts, profile)
    paid_result = paid_mod.score(item.get("paid"), paid_profile)
    scores = {
        "love": (item.get("love") or {}).get("score"),
        "good_at": (item.get("good_at") or {}).get("score"),
        "needs": (item.get("needs") or {}).get("score"),
        "paid": paid_result.score,
    }
    decision = decide(scores, gate["result"])
    return {
        "status": "scored",
        "facts": facts,
        "gate_result": gate["result"],
        "gate_reasons": gate["reasons"],
        "gate_questions": gate["questions"],
        "love": scores["love"],
        "good_at": scores["good_at"],
        "needs": scores["needs"],
        "love_reason": (item.get("love") or {}).get("reason"),
        "good_at_reason": (item.get("good_at") or {}).get("reason"),
        "needs_reason": (item.get("needs") or {}).get("reason"),
        "paid": scores["paid"],
        "paid_reason": paid_result.reason,
        "paid_obj": item.get("paid"),
        "paid_estimated": paid_result.estimated,
        "paid_thb_month_mid": paid_result.thb_month_mid,
        "summary": item.get("summary"),
        **decision,
    }


def order_queue(rows, profile) -> list:
    """The scoring queue: jobs with a description first, then priority titles (M12),
    then the newest posting; the store's order breaks ties."""
    from . import prefilter

    patterns = prefilter.priority_patterns(profile)

    def key(pair):
        index, row = pair
        posted = prefilter.parse_posted_at(row["posted_at"])
        return (0 if row["jd_text"] is not None else 1,
                0 if prefilter.is_priority(row["title"], patterns) else 1,
                -posted.timestamp() if posted else float("inf"), index)

    return [row for _, row in sorted(enumerate(rows), key=key)]


def run(cfg, conn, llm, limit: int | None = None, now: str | None = None,
        job_ids=None, outcomes: str = "") -> dict:
    """Score every unscored job, in batches. Returns a run report.

    ``job_ids`` narrows the run to specific jobs (the board's paste box scores one
    job at a time through this same path).
    """
    max_jobs = limit if limit is not None else int(cfg.get("limits.max_scored_per_run", 80))
    batch_size = int(cfg.get("llm.batch_size", 8))
    model = str(cfg.get("llm.model") or "").strip() or selected_provider(cfg.profile)
    if job_ids is not None:
        wanted = {int(job_id) for job_id in job_ids}
        jobs = [job for job in store.unscored_jobs(conn, 5000) if job["id"] in wanted]
    else:
        jobs = order_queue(store.unscored_jobs(conn, 10_000), cfg.profile)[:max_jobs]

    report = {"considered": len(jobs), "batches": 0, "scored": 0, "errors": 0,
              "by_verdict": {}, "by_source": {}, "errors_detail": []}

    for start in range(0, len(jobs), batch_size):
        batch = jobs[start:start + batch_size]
        prompt = build_prompt(cfg, batch, outcomes)
        prompt_sha = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        report["batches"] += 1
        stamp = now or store.utc_now()

        try:
            reply = llm.run_batch(prompt, [str(job["id"]) for job in batch])
        except LLMError as exc:
            _store_errors(conn, batch, f"{exc.kind}: {exc}", model, prompt_sha, stamp, report)
            report["errors"] += len(batch)
            report["errors_detail"].append({"job_ids": [job["id"] for job in batch], "error": str(exc)})
            continue
        except Exception as exc:  # a fake or unexpected failure must not kill the run
            _store_errors(conn, batch, f"unexpected: {exc}", model, prompt_sha, stamp, report)
            report["errors"] += len(batch)
            report["errors_detail"].append({"job_ids": [job["id"] for job in batch], "error": str(exc)})
            continue

        by_id = {}
        for item in reply.get("jobs", []):
            if isinstance(item, dict) and isinstance(item.get("id"), str):
                by_id[item["id"].strip()] = item

        for job in batch:
            item = by_id.get(str(job["id"]))
            if item is None:
                _store_errors(conn, [job], f"missing from the reply (batch of {len(batch)})",
                              model, prompt_sha, stamp, report)
                report["errors"] += 1
                report["errors_detail"].append({"job_ids": [job["id"]], "error": "missing from the reply"})
                continue
            record = _record_from_reply(item, cfg.profile, cfg.profile)
            record.update({
                "llm_model": model,
                "prompt_sha256": prompt_sha,
                "jd_used": 1 if job["jd_text"] else 0,
                "created_at": stamp,
                "scored_at": stamp,
            })
            store.insert_score(conn, job["id"], record)
            report["scored"] += 1
            report["by_verdict"][record["verdict"]] = report["by_verdict"].get(record["verdict"], 0) + 1
            source_counts = report["by_source"].setdefault(job["source"], {"scored": 0, "errors": 0})
            source_counts["scored"] += 1

    return report


def _store_errors(conn, jobs, message: str, model: str, prompt_sha: str, stamp: str,
                 report: dict | None = None) -> None:
    for job in jobs:
        if report is not None:
            counts = report["by_source"].setdefault(job["source"], {"scored": 0, "errors": 0})
            counts["errors"] += 1
        store.insert_score(conn, job["id"], {
            "status": "error",
            "error": message,
            "llm_model": model,
            "prompt_sha256": prompt_sha,
            "created_at": stamp,
        })


def render_scores(row) -> str:
    """``'5 4 ? 3e'`` - "?" for an unknown circle, "e" for an estimated salary."""
    cells = []
    for circle in CIRCLES:
        value = row[circle]
        if value is None:
            cells.append("?")
        elif circle == "paid" and _has(row, "paid_estimated") and row["paid_estimated"]:
            cells.append(f"{value}e")
        else:
            cells.append(str(value))
    return " ".join(cells)


def _has(row, key) -> bool:
    try:
        return key in row.keys()
    except AttributeError:
        return False
