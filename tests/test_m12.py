"""M12: the pre-filter fix, reprefilter, queue priority and "Score anyway"."""

import json
import tempfile
import threading
import unittest
import urllib.request
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from _fake import FakeTransport
from _util import CFG, entry, memory_conn, profile, reply
from _vault import make_vault

from jobhunt import board_server, fetch_jd, prefilter, reprefilter, score as score_mod, store
from jobhunt.http import PoliteFetcher
from jobhunt.llm import FakeLLM

NOW = datetime.now(timezone.utc)


def days_ago(days: int) -> str:
    return (NOW - timedelta(days=days)).date().isoformat()


def add_prefiltered(conn, title, rule, posted=None, source="linkedin", ref=None):
    job_id, _ = store.add_job(conn, {"source": source, "source_job_id": ref or title,
                                     "title": title, "company": "Acme", "posted_at": posted,
                                     "url": f"https://www.linkedin.com/jobs/view/{ref or 1}"})
    store.insert_score(conn, job_id, {"status": "prefiltered", "prefilter_rule": rule,
                                      "verdict": "FILTERED", "gate_reasons": [],
                                      "gate_questions": [], "created_at": store.utc_now()})
    return job_id


class Reprefilter(unittest.TestCase):
    def setUp(self):
        self.conn = memory_conn(self)
        m, s = "title_exclude: \\bmanager\\b", "title_exclude: \\bsupervisor\\b"
        self.ids = {
            "kam": add_prefiltered(self.conn, "Key Account Manager", m, days_ago(2), ref="1"),
            "am": add_prefiltered(self.conn, "Account Manager, FMCG", m, days_ago(2), ref="2"),
            "sup": add_prefiltered(self.conn, "Supervisor, Modern Trade", s, days_ago(2), ref="3"),
            "tiktok": add_prefiltered(self.conn, "Category Strategist, FMCG (Thailand)",
                                      "posted_within_days: 3d", days_ago(4), ref="4"),
            "senior": add_prefiltered(self.conn, "Senior Manager, Sales",
                                      "title_exclude: \\bsenior\\b", days_ago(2), ref="5"),
            "gm": add_prefiltered(self.conn, "General Manager", m, days_ago(2), ref="6"),
            "old": add_prefiltered(self.conn, "Brand Executive", "posted_within_days: 3d",
                                   days_ago(40), ref="7"),
            "nurse": add_prefiltered(self.conn, "Staff Nurse",
                                     "title_exclude_hard: \\bnurse\\b", days_ago(1), ref="8"),
        }
        self.released = {"kam", "am", "sup", "tiktok"}

    def current(self, name):
        return store.current_score(self.conn, self.ids[name])

    def test_it_releases_exactly_what_the_new_rules_allow(self):
        report = reprefilter.run(CFG, self.conn)
        self.assertEqual(set(report["released_ids"]), {self.ids[n] for n in self.released})
        self.assertEqual((report["checked"], report["released"], report["still"]), (8, 4, 4))
        for name in self.released:
            self.assertIsNone(self.current(name), name)           # unscored again
        self.assertEqual(self.current("senior")["prefilter_rule"], "title_exclude: \\bsenior\\b")
        self.assertTrue(self.current("gm")["prefilter_rule"].startswith("title_exclude: \\b(senior"))
        self.assertEqual(self.current("old")["prefilter_rule"], "max_age_days: 30d")
        self.assertEqual(report["relabelled"], 2)                 # gm and old: new reasons
        self.assertEqual(report["by_rule"]["title_exclude: \\bmanager\\b"],
                         {"released": 2, "still": 1})

    def test_history_is_kept(self):
        reprefilter.run(CFG, self.conn)
        rows = self.conn.execute("SELECT status, is_current FROM scores WHERE job_id = ?",
                                 (self.ids["kam"],)).fetchall()
        self.assertEqual([(r["status"], r["is_current"]) for r in rows], [("prefiltered", 0)])

    def test_released_jobs_join_the_queues(self):
        reprefilter.run(CFG, self.conn)
        waiting = {row["id"] for row in store.jd_candidates(self.conn, 100)}
        self.assertEqual(waiting, {self.ids[n] for n in self.released})
        store.set_jd(self.conn, self.ids["kam"], "A key account role.", "linkedin_posting")
        self.assertIn(self.ids["kam"], [r["id"] for r in store.unscored_jobs(self.conn, 100)])

    def test_dry_run_writes_nothing_and_a_second_run_releases_nothing(self):
        before = self.conn.execute("SELECT COUNT(*), SUM(is_current) FROM scores").fetchone()
        dry = reprefilter.run(CFG, self.conn, dry_run=True)
        self.assertEqual(dry["released"], 4)
        self.assertEqual(tuple(self.conn.execute(
            "SELECT COUNT(*), SUM(is_current) FROM scores").fetchone()), tuple(before))
        reprefilter.run(CFG, self.conn)
        again = reprefilter.run(CFG, self.conn)
        self.assertEqual((again["released"], again["relabelled"]), (0, 0))

    def test_the_report_names_each_old_rule(self):
        text = reprefilter.format_report(reprefilter.run(CFG, self.conn))
        self.assertIn("4 released", text)
        self.assertIn("posted_within_days: 3d", text)


class QueuePriority(unittest.TestCase):
    TITLES = ("Operations Analyst", "Key Account Executive", "Marketing Specialist",
              "Category Associate", "Trade Marketing Officer", "Management Trainee")

    def rows(self, conn):
        for index, title in enumerate(self.TITLES):
            store.add_job(conn, {"source": "linkedin", "source_job_id": str(index),
                                 "title": title, "company": "Acme",
                                 "posted_at": days_ago(index)})     # newer = earlier in list
        return store.jd_candidates(conn, 100)

    def test_the_jd_fetch_queue_puts_priority_titles_first(self):
        conn = memory_conn(self)
        order = [row["title"] for row in fetch_jd.order_candidates(self.rows(conn), CFG.profile)]
        self.assertEqual(order[:4], ["Key Account Executive", "Category Associate",
                                     "Trade Marketing Officer", "Management Trainee"])
        self.assertEqual(set(order[4:]), {"Operations Analyst", "Marketing Specialist"})

    def test_the_scoring_queue_puts_priority_titles_first(self):
        conn = memory_conn(self)
        for row in self.rows(conn):
            store.set_jd(conn, row["id"], "A role.", "linkedin_posting")
        order = [row["title"] for row in
                 score_mod.order_queue(store.unscored_jobs(conn, 100), CFG.profile)]
        self.assertEqual(order[:4], ["Key Account Executive", "Category Associate",
                                     "Trade Marketing Officer", "Management Trainee"])

    def test_a_job_with_a_description_still_comes_before_one_without(self):
        conn = memory_conn(self)
        plain, _ = store.add_job(conn, {"source": "manual", "title": "Operations Analyst",
                                        "company": "A", "jd_text": "x"})
        kam, _ = store.add_job(conn, {"source": "manual", "title": "Key Account Manager",
                                      "company": "B"})
        order = [row["id"] for row in score_mod.order_queue(store.unscored_jobs(conn, 10),
                                                            CFG.profile)]
        self.assertEqual(order, [plain, kam])

    def test_the_scoring_run_respects_the_cap_after_ordering(self):
        conn = memory_conn(self)
        for row in self.rows(conn):
            store.set_jd(conn, row["id"], "A role.", "linkedin_posting")
        cfg = replace(CFG, profile=profile(**{"limits.max_scored_per_run": 2}))
        llm = FakeLLM([lambda prompt, ids: reply(*[entry(i) for i in ids])])
        score_mod.run(cfg, conn, llm)
        scored = {row["title"] for row in conn.execute(
            "SELECT j.title FROM jobs j JOIN scores s ON s.job_id = j.id AND s.is_current = 1")}
        self.assertEqual(scored, {"Key Account Executive", "Category Associate"})


class ScoreAnyway(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = replace(CFG, data_root=Path(self.tmp.name))
        self.db = Path(self.tmp.name) / "board.sqlite"
        conn = store.connect(self.db)
        self.job = add_prefiltered(conn, "Senior Key Account Manager",
                                   "title_exclude: \\bsenior\\b", days_ago(1), ref="4486971271")
        conn.close()
        self.transport = FakeTransport({"jobPosting/4486971271": (
            200, '<div class="show-more-less-html__markup">A key account role in FMCG.</div>', {})})
        self.llm = FakeLLM([lambda prompt, ids: reply(*[entry(i, 4, 4, 4) for i in ids])])
        fetcher = PoliteFetcher(profile(**{"limits.min_seconds_between_requests": 0}),
                                transport=self.transport, sleep=lambda s: None, now=lambda: 0.0)
        self.server = board_server.create_server(
            self.cfg, db_path=self.db, vault_path=make_vault(self.tmp.name).root, port=0,
            llm_factory=lambda: self.llm, fetcher_factory=lambda: fetcher)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def post(self, path):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.server.server_address[1]}{path}", data=b"{}", method="POST",
            headers={"X-Requested-With": "jobhunt", "Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read())

    def test_a_prefiltered_job_is_fetched_and_scored_now(self):
        body = self.post(f"/api/jobs/{self.job}/score")
        self.assertEqual(body, {"ok": True, "job_id": self.job, "state": "scored"})
        self.assertTrue(any("jobPosting/4486971271" in url for url in self.transport.urls))
        self.assertEqual(len(self.llm.calls), 1)
        self.assertEqual(self.llm.calls[0]["job_ids"], [str(self.job)])
        conn = store.connect(self.db)
        self.addCleanup(conn.close)
        current = store.current_score(conn, self.job)
        self.assertEqual(current["status"], "scored")
        self.assertIn("key account role", store.get_job(conn, self.job)["jd_text"])
        history = conn.execute("SELECT status FROM scores WHERE job_id = ? ORDER BY id",
                               (self.job,)).fetchall()
        self.assertEqual([row["status"] for row in history], ["prefiltered", "scored"])

    def test_an_unknown_job_is_a_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post("/api/jobs/99999/score")
        self.assertEqual(ctx.exception.code, 404)


import urllib.error  # noqa: E402

if __name__ == "__main__":
    unittest.main()
