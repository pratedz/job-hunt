"""Re-apply the profile rules to scores that already exist, without a model call.

When ``profile.toml`` changes (say the Paid anchors), every stored verdict can be
stale. The facts and the salary evidence are already in the database, so the gates,
the Paid circle, the verdict, the weakest circle and the mean can all be recomputed
here in pure Python. Reasons and summaries stay as the model wrote them; the old row
is superseded, never overwritten, so the history remains.
"""

from __future__ import annotations

import json

from . import gates as gates_mod
from . import paid as paid_mod
from . import score as score_mod
from . import store

COMPARE = ("verdict", "gate_result", "paid", "weakest_circle", "weakest_score",
           "mean_score", "paid_estimated", "paid_thb_month_mid")


def _json(value, default):
    try:
        return json.loads(value) if value else default
    except (TypeError, ValueError):
        return default


def scored_rows(conn) -> list:
    return conn.execute(store.BOARD_JOBS_SQL + " WHERE s.status = 'scored' "
                                               "ORDER BY j.id").fetchall()


def recompute(cfg, conn, dry_run: bool = False, log=None) -> dict:
    log = log or (lambda *a, **k: None)
    report = {"considered": 0, "changed": 0, "unchanged": 0, "paid_changed": 0,
              "verdict_changes": [], "errors": []}
    for row in scored_rows(conn):
        report["considered"] += 1
        try:
            facts = _json(row["facts_json"], {}) or {}
            gate = gates_mod.check(facts, cfg.profile)
            paid_result = paid_mod.score(_json(row["paid_json"], None), cfg.profile)
            scores = {"love": row["love"], "good_at": row["good_at"],
                      "needs": row["needs"], "paid": paid_result.score}
            decision = score_mod.decide(scores, gate["result"])
        except Exception as exc:
            report["errors"].append(f"job {row['id']}: {type(exc).__name__}: {exc}")
            continue

        new = {
            "verdict": decision["verdict"],
            "gate_result": gate["result"],
            "paid": paid_result.score,
            "weakest_circle": decision["weakest_circle"],
            "weakest_score": decision["weakest_score"],
            "mean_score": decision["mean_score"],
            "paid_estimated": 1 if paid_result.estimated else 0,
            "paid_thb_month_mid": paid_result.thb_month_mid,
            "gate_reasons": gate["reasons"],
            "gate_questions": gate["questions"],
            "paid_reason": paid_result.reason,
        }
        differs = {
            key: (row[key], new[key]) for key in COMPARE
            if (row[key] if row[key] is not None else None) != new[key]
        }
        if row["gate_reasons_json"] != store._dump(gate["reasons"]):
            differs["gate_reasons"] = (row["gate_reasons_json"], gate["reasons"])
        if row["gate_questions_json"] != store._dump(gate["questions"]):
            differs["gate_questions"] = (row["gate_questions_json"], gate["questions"])
        if not differs:
            report["unchanged"] += 1
            continue

        report["changed"] += 1
        if "paid" in differs:
            report["paid_changed"] += 1
        if differs.get("verdict"):
            report["verdict_changes"].append({
                "job_id": row["id"], "company": row["company"], "title": row["title"],
                "from": differs["verdict"][0], "to": differs["verdict"][1],
            })
            log(f"job {row['id']} {row['company']}: {differs['verdict'][0]} -> "
                f"{differs['verdict'][1]}")
        if dry_run:
            continue
        store.insert_score(conn, row["id"], {
            "status": "scored",
            "facts": facts,
            "gate_result": new["gate_result"],
            "gate_reasons": new["gate_reasons"],
            "gate_questions": new["gate_questions"],
            "love": row["love"], "good_at": row["good_at"], "needs": row["needs"],
            "paid": new["paid"],
            "love_reason": row["love_reason"],
            "good_at_reason": row["good_at_reason"],
            "needs_reason": row["needs_reason"],
            "paid_reason": new["paid_reason"],
            "paid_estimated": new["paid_estimated"],
            "paid_thb_month_mid": new["paid_thb_month_mid"],
            "paid_obj": _json(row["paid_json"], None),
            "verdict": new["verdict"],
            "weakest_circle": new["weakest_circle"],
            "weakest_score": new["weakest_score"],
            "mean_score": new["mean_score"],
            "summary": row["summary"],
            "llm_model": row["llm_model"],
            "prompt_sha256": row["prompt_sha256"],
            "jd_used": row["jd_used"],
            "created_at": store.utc_now(),
            "scored_at": store.utc_now(),
        })
    return report
