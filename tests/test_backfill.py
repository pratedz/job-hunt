"""Paste-by-URL should learn the company and title from the posting page."""

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from _fake import FakeTransport, fixture
from _util import CFG, memory_conn, profile
from _vault import TODAY, make_vault, scored_reply

from jobhunt import backfill, board_api, cover, store
from jobhunt.http import PoliteFetcher
from jobhunt.llm import FakeLLM
from jobhunt.tracker import Tracker

JOBSDB_URL = "https://th.jobsdb.com/job/95340875"
LINKEDIN_URL = "https://www.linkedin.com/jobs/view/4472494924"


def env(routes):
    cfg = replace(CFG, data_root=Path(tempfile.mkdtemp()))
    clock_transport = FakeTransport(routes)
    fetcher = PoliteFetcher(cfg.profile, transport=clock_transport, sleep=lambda s: None,
                            now=lambda: 0.0)
    return cfg, fetcher, clock_transport


class Paste(unittest.TestCase):
    def setUp(self):
        self.conn = memory_conn(self)
        self.tracker = Tracker(make_vault(tempfile.mkdtemp()), today=lambda: TODAY)

    def paste(self, body, routes):
        cfg, fetcher, _transport = env(routes)
        llm = FakeLLM([lambda prompt, ids: scored_reply(ids)])
        return board_api.paste(cfg, self.conn, self.tracker, body, llm, fetcher), cfg

    def test_a_jobsdb_url_paste_takes_the_real_company_and_title(self):
        response, _ = self.paste({"url": JOBSDB_URL}, {
            "th.jobsdb.com/job/95340875": (200, fixture("jobsdb_job_95340875.html"), {})})
        job = store.get_job(self.conn, response["job_id"])
        self.assertEqual(job["company"], "Deliveree (Thailand) Limited")
        self.assertEqual(job["title"], "Fresh Graduate Management Trainee Program (MT)")
        self.assertNotEqual(job["company"], "th.jobsdb.com")
        self.assertIn("Deliveree", job["jd_text"])

    def test_a_linkedin_url_paste_reads_the_top_card(self):
        response, _ = self.paste({"url": LINKEDIN_URL}, {
            "jobPosting/4472494924": (200, fixture("linkedin_guest_jobposting.html"), {})})
        job = store.get_job(self.conn, response["job_id"])
        self.assertEqual(job["company"], "Aman")
        self.assertIn("IGNITE", job["title"])

    def test_a_company_or_title_the_user_typed_is_kept(self):
        response, _ = self.paste({"url": JOBSDB_URL, "company": "My Own Co",
                                  "title": "My Own Title"}, {
            "th.jobsdb.com/job/95340875": (200, fixture("jobsdb_job_95340875.html"), {})})
        job = store.get_job(self.conn, response["job_id"])
        self.assertEqual(job["company"], "My Own Co")
        self.assertEqual(job["title"], "My Own Title")


class Backfill(unittest.TestCase):
    def setUp(self):
        self.conn = memory_conn(self)

    def add(self, url, company, title, jd_text="A pasted description."):
        job_id, _ = store.add_job(self.conn, {"source": "manual", "url": url,
                                              "company": company, "title": title,
                                              "jd_text": jd_text})
        return job_id

    def test_the_placeholder_row_the_live_run_created_is_found(self):
        job_id = self.add(JOBSDB_URL, "th.jobsdb.com", f"Pasted job {JOBSDB_URL}")
        self.assertEqual([row["id"] for row in backfill.candidates(self.conn)], [job_id])

    def test_a_well_named_row_is_not_a_candidate(self):
        self.add(JOBSDB_URL, "Central Pattana", "GM Trainee Program 2027")
        self.assertEqual(backfill.candidates(self.conn), [])

    def test_dry_run_reports_without_writing(self):
        job_id = self.add(JOBSDB_URL, "th.jobsdb.com", f"Pasted job {JOBSDB_URL}")
        cfg, fetcher, _ = env({"th.jobsdb.com/job/95340875": (
            200, fixture("jobsdb_job_95340875.html"), {})})
        report = backfill.run(cfg, self.conn, fetcher=fetcher, dry_run=True)
        self.assertEqual(report["updated"], 1)
        self.assertEqual(report["changes"][0]["company"][1], "Deliveree (Thailand) Limited")
        job = store.get_job(self.conn, job_id)
        self.assertEqual(job["company"], "th.jobsdb.com")      # untouched

    def test_the_real_run_updates_company_and_title(self):
        job_id = self.add(JOBSDB_URL, "th.jobsdb.com", f"Pasted job {JOBSDB_URL}")
        cfg, fetcher, _ = env({"th.jobsdb.com/job/95340875": (
            200, fixture("jobsdb_job_95340875.html"), {})})
        report = backfill.run(cfg, self.conn, fetcher=fetcher)
        self.assertEqual(report["updated"], 1)
        job = store.get_job(self.conn, job_id)
        self.assertEqual(job["company"], "Deliveree (Thailand) Limited")
        self.assertEqual(job["title"], "Fresh Graduate Management Trainee Program (MT)")
        self.assertEqual(job["company_norm"], "deliveree thailand limited")

    def test_a_failed_page_is_reported_not_raised(self):
        self.add(JOBSDB_URL, "th.jobsdb.com", f"Pasted job {JOBSDB_URL}")
        cfg, fetcher, _ = env({"th.jobsdb.com": (404, "", {})})
        report = backfill.run(cfg, self.conn, fetcher=fetcher)
        self.assertEqual(report["updated"], 0)
        self.assertTrue(report["errors"])

    def test_it_also_fills_a_missing_description(self):
        job_id = self.add(JOBSDB_URL, "th.jobsdb.com", f"Pasted job {JOBSDB_URL}", jd_text=None)
        cfg, fetcher, _ = env({"th.jobsdb.com/job/95340875": (
            200, fixture("jobsdb_job_95340875.html"), {})})
        backfill.run(cfg, self.conn, fetcher=fetcher)
        job = store.get_job(self.conn, job_id)
        self.assertIn("Deliveree", job["jd_text"])
        self.assertEqual(job["jd_source"], "backfill")


class PromptsUseTheFixedValues(Backfill):
    """A pasted row that was backfilled scores with the real company name."""

    def test_the_cover_prompt_uses_the_company(self):
        job_id = self.add(JOBSDB_URL, "th.jobsdb.com", f"Pasted job {JOBSDB_URL}")
        cfg, fetcher, _ = env({"th.jobsdb.com/job/95340875": (
            200, fixture("jobsdb_job_95340875.html"), {})})
        backfill.run(cfg, self.conn, fetcher=fetcher)
        row = store.board_job(self.conn, job_id)
        prompt = cover.build_prompt(cfg, row, None)
        self.assertIn("company: Deliveree (Thailand) Limited", prompt)


if __name__ == "__main__":
    unittest.main()
