"""recompute: re-apply profile rules to stored scores, without a model call."""

import json
import unittest
from dataclasses import replace
from pathlib import Path

from _util import CFG, memory_conn, profile

from jobhunt import gates, paid, recompute, store


def add_score(conn, job_id, **kw):
    base = {
        "status": "scored", "verdict": "CENTRE", "gate_result": "pass",
        "love": 4, "good_at": 4, "needs": 4, "paid": 3,
        "love_reason": "love reason", "good_at_reason": "good reason",
        "needs_reason": "needs reason", "paid_reason": "old paid reason",
        "paid_estimated": 1, "paid_thb_month_mid": 40000.0,
        "paid_obj": {"salary_min": 30000, "salary_max": 50000, "currency": "THB",
                     "period": "month", "stated": False, "reason": "estimate"},
        "facts": {"job_type": "graduate_programme", "location_city": "Bangkok",
                  "location_country": "Thailand", "remote": False},
        "summary": "A summary the model wrote.",
        "llm_model": "gpt-6.1-terra", "prompt_sha256": "abc", "jd_used": 1,
        "weakest_circle": "paid", "weakest_score": 3, "mean_score": 3.75,
        "created_at": store.utc_now(), "scored_at": store.utc_now(),
    }
    base.update(kw)
    store.insert_score(conn, job_id, base)


class Base(unittest.TestCase):
    def setUp(self):
        self.conn = memory_conn(self)
        self.job_id, _ = store.add_job(self.conn, {
            "source": "manual", "title": "Graduate Programme", "company": "Acme",
            "jd_text": "A programme."})
        self.cfg = replace(CFG, profile=profile(**{"paid.anchors_thb_month": [25000, 35000,
                                                                              45000, 55000]}))


class PaidAndVerdict(Base):
    def test_lower_anchors_raise_the_paid_circle_and_the_verdict(self):
        # mid 36,000: the old anchors [30,40,50,65] reached one, the new ones [25,35,45,55] two
        add_score(self.conn, self.job_id, paid=2, verdict="LOW", weakest_circle="paid",
                  weakest_score=2, mean_score=3.5,
                  paid_obj={"salary_min": 32000, "salary_max": 40000, "currency": "THB",
                            "period": "month", "stated": True, "reason": "stated"})
        report = recompute.recompute(self.cfg, self.conn)
        self.assertEqual(report["considered"], 1)
        self.assertEqual(report["changed"], 1)
        self.assertEqual(report["paid_changed"], 1)
        row = store.current_score(self.conn, self.job_id)
        self.assertEqual(row["paid"], 3)
        self.assertEqual(row["verdict"], "CENTRE")
        self.assertEqual(row["weakest_circle"], "paid")      # 4 4 4 3
        self.assertEqual(row["weakest_score"], 3)
        self.assertEqual(row["mean_score"], 3.75)

    def test_the_verdict_change_is_reported(self):
        add_score(self.conn, self.job_id, paid=2, verdict="LOW", weakest_circle="paid",
                  weakest_score=2, mean_score=3.5,
                  paid_obj={"salary_min": 32000, "salary_max": 40000, "currency": "THB",
                            "period": "month", "stated": True, "reason": "stated"})
        report = recompute.recompute(self.cfg, self.conn)
        self.assertEqual([(c["from"], c["to"]) for c in report["verdict_changes"]],
                         [("LOW", "CENTRE")])
        self.assertEqual(report["verdict_changes"][0]["job_id"], self.job_id)

    def test_a_low_salary_keeps_the_job_low(self):
        add_score(self.conn, self.job_id, paid=1, verdict="LOW", weakest_circle="paid",
                  weakest_score=1, mean_score=3.25,
                  paid_obj={"salary_min": 20000, "salary_max": 20000, "currency": "THB",
                            "period": "month", "stated": True, "reason": "stated"})
        recompute.recompute(self.cfg, self.conn)
        row = store.current_score(self.conn, self.job_id)
        self.assertEqual(row["verdict"], "LOW")
        self.assertEqual(row["paid"], 1)           # 20k is still under the first anchor

    def test_the_model_reasons_and_summary_survive(self):
        add_score(self.conn, self.job_id)
        recompute.recompute(self.cfg, self.conn)
        row = store.current_score(self.conn, self.job_id)
        self.assertEqual(row["love_reason"], "love reason")
        self.assertEqual(row["summary"], "A summary the model wrote.")
        self.assertEqual(row["llm_model"], "gpt-6.1-terra")
        self.assertEqual(row["prompt_sha256"], "abc")
        self.assertEqual(row["jd_used"], 1)

    def test_the_old_row_is_superseded_not_overwritten(self):
        add_score(self.conn, self.job_id)
        recompute.recompute(self.cfg, self.conn)
        rows = self.conn.execute("SELECT is_current, superseded_by FROM scores ORDER BY id").fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["is_current"], 0)
        self.assertIsNotNone(rows[0]["superseded_by"])


class GateChanges(Base):
    def test_a_gate_rule_change_is_applied(self):
        add_score(self.conn, self.job_id, facts={"min_years_experience": 5,
                                                 "job_type": "graduate_programme",
                                                 "location_country": "Thailand"},
                  verdict="CENTRE")
        report = recompute.recompute(self.cfg, self.conn)
        row = store.current_score(self.conn, self.job_id)
        self.assertEqual(row["gate_result"], "fail")
        self.assertEqual(row["verdict"], "FILTERED")
        self.assertEqual(report["verdict_changes"][0]["to"], "FILTERED")
        reasons = json.loads(row["gate_reasons_json"])
        self.assertIn("5 years", reasons[0])


class DryRunAndNoOps(Base):
    def test_a_dry_run_writes_nothing(self):
        add_score(self.conn, self.job_id)
        report = recompute.recompute(self.cfg, self.conn, dry_run=True)
        self.assertEqual(report["changed"], 1)
        rows = self.conn.execute("SELECT COUNT(*) FROM scores").fetchone()[0]
        self.assertEqual(rows, 1)
        self.assertEqual(store.current_score(self.conn, self.job_id)["paid"], 3)

    def test_a_second_run_changes_nothing(self):
        add_score(self.conn, self.job_id)
        recompute.recompute(self.cfg, self.conn)
        again = recompute.recompute(self.cfg, self.conn)
        self.assertEqual(again["changed"], 0)
        self.assertEqual(again["unchanged"], 1)

    def test_prefiltered_and_error_rows_are_left_alone(self):
        job_id, _ = store.add_job(self.conn, {"source": "jobsdb", "title": "Senior X",
                                              "company": "Beta"})
        store.insert_score(self.conn, job_id, {
            "status": "prefiltered", "prefilter_rule": "title_exclude: \\bsenior\\b",
            "verdict": "FILTERED", "created_at": store.utc_now()})
        error_id, _ = store.add_job(self.conn, {"source": "jobsdb", "title": "Broken",
                                                "company": "Beta"})
        store.insert_score(self.conn, error_id, {"status": "error", "error": "timeout",
                                                 "created_at": store.utc_now()})
        report = recompute.recompute(self.cfg, self.conn)
        self.assertEqual(report["considered"], 0)
        self.assertEqual(store.current_score(self.conn, job_id)["status"], "prefiltered")


if __name__ == "__main__":
    unittest.main()
