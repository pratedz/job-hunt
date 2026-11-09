import unittest
from dataclasses import replace

from datetime import datetime, timezone

from _fake import FakeClock, FakeTransport, fixture, fixture_json, search_page
from _util import CFG, memory_conn, profile

from jobhunt import pipeline, prefilter, store
from jobhunt.http import PoliteFetcher


def env(routes, **overrides):
    overrides.setdefault("limits.min_seconds_between_requests", 0)
    overrides.setdefault("search.keywords", ["management trainee"])
    overrides.setdefault("search.linkedin_locations", ["Bangkok"])
    overrides.setdefault("search.jobsdb_locations", ["Bangkok"])
    overrides.setdefault("search.max_pages", 1)
    overrides.setdefault("prefilter.posted_within_days", 3650)
    overrides.setdefault("prefilter.posted_within_days_first_run", 3650)
    overrides.setdefault("prefilter.max_age_days", 3650)
    cfg = replace(CFG, profile=profile(**overrides))
    clock = FakeClock()
    transport = FakeTransport(routes)
    fetcher = PoliteFetcher(cfg.profile, transport=transport, sleep=clock.sleep, now=clock.now)
    return cfg, fetcher, transport


class OrderOfOperations(unittest.TestCase):
    """Collect -> store -> pre-filter -> fetch only survivors -> (score)."""

    def setUp(self):
        self.conn = memory_conn(self)
        page = search_page(("111", "Senior Key Account Manager"), ("222", "Management Trainee"))
        self.cfg, self.fetcher, self.transport = env({
            "seeMoreJobPostings": (200, page, {}),
            "jobPosting/222": (200, fixture("linkedin_guest_jobposting.html"), {}),
        })

    def test_a_prefiltered_job_is_stored_but_never_fetched(self):
        report = pipeline.discover(self.cfg, self.conn, self.fetcher, ["linkedin"])
        self.assertEqual(report["sources"]["linkedin"]["found"], 2)
        self.assertEqual(report["sources"]["linkedin"]["new"], 2)
        self.assertEqual(report["sources"]["linkedin"]["prefiltered"], 1)

        rows = {row["source_job_id"]: row for row in self.conn.execute("SELECT * FROM jobs")}
        self.assertIn("111", rows)
        self.assertIsNone(rows["111"]["jd_text"])          # stored, kept, unfetched
        self.assertIn("Aman Nai Lert", rows["222"]["jd_text"])

        self.assertFalse(any("jobPosting/111" in url for url in self.transport.urls))
        self.assertTrue(any("jobPosting/222" in url for url in self.transport.urls))

    def test_the_prefiltered_job_is_filtered_with_its_rule(self):
        pipeline.discover(self.cfg, self.conn, self.fetcher, ["linkedin"])
        row = self.conn.execute("SELECT * FROM jobs WHERE source_job_id = '111'").fetchone()
        score = store.current_score(self.conn, row["id"])
        self.assertEqual(score["status"], "prefiltered")
        self.assertEqual(score["verdict"], "FILTERED")
        self.assertIn("senior", score["prefilter_rule"])

    def test_the_fetch_step_runs_after_storing(self):
        report = pipeline.discover(self.cfg, self.conn, self.fetcher, ["linkedin"])
        self.assertEqual(report["jd"]["fetched"], 1)
        self.assertEqual(report["jd"]["by_source"]["linkedin"]["fetched"], 1)


class FullRunOnSamples(unittest.TestCase):
    def setUp(self):
        self.conn = memory_conn(self)
        self.cfg, self.fetcher, self.transport = env({
            "seeMoreJobPostings": (200, fixture("linkedin_guest_search.html"), {}),
            "jobsearch/v5/search": (200, fixture_json("jobsdb_search_v5.json"), {}),
            "jobPosting/": (200, fixture("linkedin_guest_jobposting.html"), {}),
            "th.jobsdb.com/job/": (200, fixture("jobsdb_job_95340875.html"), {}),
        })

    def test_both_sources_are_collected_and_stored(self):
        report = pipeline.discover(self.cfg, self.conn, self.fetcher, ["linkedin", "jobsdb"])
        self.assertEqual(report["sources"]["linkedin"]["found"], 10)
        self.assertEqual(report["sources"]["jobsdb"]["found"], 5)
        self.assertEqual(store.count_jobs(self.conn), 15)

    def test_prefiltered_jobs_are_exactly_the_ones_the_rules_catch(self):
        report = pipeline.discover(self.cfg, self.conn, self.fetcher, ["linkedin", "jobsdb"])
        expected = sum(
            1 for row in self.conn.execute("SELECT title FROM jobs")
            if prefilter.title_hit(row["title"], self.cfg.profile))
        self.assertEqual(report["sources"]["linkedin"]["prefiltered"]
                         + report["sources"]["jobsdb"]["prefiltered"], expected)

    def test_a_second_run_adds_nothing(self):
        pipeline.discover(self.cfg, self.conn, self.fetcher, ["linkedin", "jobsdb"])
        cfg, fetcher, transport = env({
            "seeMoreJobPostings": (200, fixture("linkedin_guest_search.html"), {}),
            "jobsearch/v5/search": (200, fixture_json("jobsdb_search_v5.json"), {}),
        })
        report = pipeline.discover(cfg, self.conn, fetcher, ["linkedin", "jobsdb"])
        self.assertEqual(report["sources"]["linkedin"]["new"], 0)
        self.assertEqual(report["sources"]["jobsdb"]["new"], 0)
        self.assertEqual(store.count_jobs(self.conn), 15)
        # nothing left to fetch either, so no posting request is made
        self.assertFalse(any("jobPosting/" in url for url in transport.urls))

    def test_dry_run_stores_nothing_and_fetches_nothing(self):
        report = pipeline.discover(self.cfg, self.conn, self.fetcher, ["linkedin", "jobsdb"],
                                   dry_run=True)
        self.assertEqual(report["sources"]["linkedin"]["found"], 10)
        self.assertEqual(report["sources"]["linkedin"]["new"], 0)
        self.assertEqual(store.count_jobs(self.conn), 0)
        self.assertFalse(any("jobPosting/" in url for url in self.transport.urls))
        self.assertEqual(report["jd"]["attempted"], 0)

    def test_the_per_host_cap_is_reported(self):
        cfg, fetcher, transport = env({
            "seeMoreJobPostings": (200, fixture("linkedin_guest_search.html"), {}),
        }, **{"limits.per_host": {"www.linkedin.com": 1}, "search.max_pages": 3})
        report = pipeline.discover(cfg, self.conn, fetcher, ["linkedin"])
        self.assertEqual(report["hosts"]["www.linkedin.com"]["requests"], 1)
        self.assertIn("www.linkedin.com", report["stopped_hosts"])


class FirstRunWindow(unittest.TestCase):
    """The bug from the live run: a 14-day first run was pre-filtered at 3 days."""

    NOW = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)

    def setUp(self):
        self.conn = memory_conn(self)
        self.page = search_page(
            ("111", "Management Trainee", "2026-10-28"),      # 10 days old
            ("222", "Business Development Executive", "2026-10-01"),   # 37 days old
        )
        self.cfg, self.fetcher, self.transport = env(
            {"seeMoreJobPostings": (200, self.page, {}),
             "jobPosting/": (200, fixture("linkedin_guest_jobposting.html"), {})},
            **{"prefilter.posted_within_days": 3, "prefilter.posted_within_days_first_run": 14,
               "prefilter.max_age_days": 30})

    def test_a_ten_day_old_job_survives_the_first_run(self):
        report = pipeline.discover(self.cfg, self.conn, self.fetcher, ["linkedin"], now=self.NOW)
        self.assertEqual(report["sources"]["linkedin"]["found"], 2)
        self.assertEqual(report["sources"]["linkedin"]["new"], 2)
        self.assertEqual(report["sources"]["linkedin"]["prefiltered"], 1)
        rows = {row["source_job_id"]: row for row in self.conn.execute("SELECT * FROM jobs")}
        self.assertIn("111", rows)          # kept: 10 days inside a 14-day window
        self.assertIn("222", rows)          # also stored, but older than max_age_days
        score = store.current_score(self.conn, rows["222"]["id"])
        self.assertEqual(score["prefilter_rule"], "max_age_days: 30d")
        # the surviving job was fetched, the filtered one was not
        self.assertTrue(any("jobPosting/111" in url for url in self.transport.urls))
        self.assertFalse(any("jobPosting/222" in url for url in self.transport.urls))

    def test_a_second_run_falls_back_to_the_daily_window(self):
        pipeline.discover(self.cfg, self.conn, self.fetcher, ["linkedin"], now=self.NOW)
        cfg, fetcher, transport = env(
            {"seeMoreJobPostings": (200, self.page, {})},
            **{"prefilter.posted_within_days": 3, "prefilter.posted_within_days_first_run": 14})
        report = pipeline.discover(cfg, self.conn, fetcher, ["linkedin"], now=self.NOW)
        self.assertFalse(report["sources"]["linkedin"]["first_run"])
        # nothing new is stored, and the 3-day rule now applies to the incoming copy
        self.assertEqual(report["sources"]["linkedin"]["new"], 0)
        self.assertEqual(report["sources"]["linkedin"]["duplicates"], 2)


class WindowsAndReporting(unittest.TestCase):
    def test_the_first_run_uses_the_wider_window(self):
        conn = memory_conn(self)
        cfg, fetcher, transport = env({}, **{"prefilter.posted_within_days": 3,
                                             "prefilter.posted_within_days_first_run": 14})
        days, first = pipeline.days_for_source(cfg, conn, "linkedin")
        self.assertEqual((days, first), (14.0, True))
        store.add_job(conn, {"source": "linkedin", "title": "X"})
        days, first = pipeline.days_for_source(cfg, conn, "linkedin")
        self.assertEqual((days, first), (3.0, False))

    def test_the_report_table_lists_each_source(self):
        conn = memory_conn(self)
        cfg, fetcher, transport = env({
            "seeMoreJobPostings": (200, search_page(("1", "Management Trainee")), {}),
            "jobsearch/v5/search": (200, {"data": []}, {}),
        })
        report = pipeline.discover(cfg, conn, fetcher, ["linkedin", "jobsdb"])
        text = pipeline.format_report(report)
        self.assertIn("linkedin", text)
        self.assertIn("jobsdb", text)
        self.assertIn("found   1", text)
        self.assertIn("first run", text)


if __name__ == "__main__":
    unittest.main()
