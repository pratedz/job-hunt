import unittest

from _fake import FakeClock, FakeTransport, fixture
from _util import CFG, job, memory_conn, profile

from dataclasses import replace

from jobhunt import fetch_jd, store
from jobhunt.http import PoliteFetcher


def env(routes):
    cfg = replace(CFG, profile=profile(**{"limits.min_seconds_between_requests": 0}))
    clock = FakeClock()
    transport = FakeTransport(routes)
    fetcher = PoliteFetcher(cfg.profile, transport=transport, sleep=clock.sleep, now=clock.now)
    return cfg, fetcher, transport


class FetchDescriptions(unittest.TestCase):
    def setUp(self):
        self.conn = memory_conn(self)
        self.linkedin_id, _ = store.add_job(self.conn, job(
            source="linkedin", source_job_id="4472494924", title="Management Trainee",
            url="https://www.linkedin.com/jobs/view/4472494924"))
        self.jobsdb_id, _ = store.add_job(self.conn, job(
            source="jobsdb", source_job_id="95340875", title="Fresh Graduate Management Trainee",
            url="https://th.jobsdb.com/job/95340875"))

    def test_both_kinds_of_page_are_read(self):
        cfg, fetcher, transport = env({
            "jobPosting/4472494924": (200, fixture("linkedin_guest_jobposting.html"), {}),
            "th.jobsdb.com/job/95340875": (200, fixture("jobsdb_job_95340875.html"), {}),
        })
        report = fetch_jd.run(cfg, self.conn, fetcher)
        self.assertEqual(report["fetched"], 2)
        self.assertEqual(report["missing"], 0)
        linkedin_row = store.get_job(self.conn, self.linkedin_id)
        jobsdb_row = store.get_job(self.conn, self.jobsdb_id)
        self.assertIn("Aman Nai Lert", linkedin_row["jd_text"])
        self.assertEqual(linkedin_row["jd_source"], "linkedin_posting")
        self.assertIn("Deliveree", jobsdb_row["jd_text"])
        self.assertEqual(jobsdb_row["jd_source"], "jobAdDetails")
        self.assertEqual(linkedin_row["jd_missing"], 0)

    def test_a_page_without_a_description_is_flagged_and_not_retried(self):
        cfg, fetcher, transport = env({
            "jobPosting/4472494924": (200, fixture("linkedin_guest_jobposting.html"), {}),
            "th.jobsdb.com/job/95340875": (404, "gone", {}),
        })
        report = fetch_jd.run(cfg, self.conn, fetcher)
        self.assertEqual(report["fetched"], 1)
        self.assertEqual(report["missing"], 1)
        row = store.get_job(self.conn, self.jobsdb_id)
        self.assertEqual(row["jd_missing"], 1)
        self.assertIsNone(row["jd_text"])
        self.assertNotIn(self.jobsdb_id, [r["id"] for r in store.jd_candidates(self.conn, 10)])

    def test_a_stopped_host_leaves_the_rest_for_next_time(self):
        cfg, fetcher, transport = env({
            "jobPosting/4472494924": (429, "", {}),
            "th.jobsdb.com/job/95340875": (200, fixture("jobsdb_job_95340875.html"), {}),
        })
        report = fetch_jd.run(cfg, self.conn, fetcher)
        # M12: a LinkedIn stop no longer starves the other sources
        self.assertEqual(report["fetched"], 1)
        self.assertEqual(report["by_source"]["jobsdb"]["fetched"], 1)
        self.assertEqual(report["skipped"], 1)
        self.assertIn("www.linkedin.com", fetcher.stopped_hosts())
        # the LinkedIn job was not marked missing, so it comes back next run
        left = store.jd_candidates(self.conn, 10)
        self.assertEqual([row["source"] for row in left], ["linkedin"])

    def test_after_a_stop_no_more_requests_go_to_that_host(self):
        store.add_job(self.conn, job(source="linkedin", source_job_id="999", title="Trainee"))
        cfg, fetcher, transport = env({"jobPosting/": (429, "", {})})
        fetch_jd.run(cfg, self.conn, fetcher)
        self.assertEqual(sum(1 for url in transport.urls if "linkedin" in url), 1)

    def test_a_prefiltered_job_is_never_fetched(self):
        filtered, _ = store.add_job(self.conn, job(
            source="linkedin", source_job_id="111", title="Senior Key Account Manager"))
        store.insert_score(self.conn, filtered, {
            "status": "prefiltered", "prefilter_rule": "title_exclude: \\bsenior\\b",
            "verdict": "FILTERED", "created_at": store.utc_now()})
        cfg, fetcher, transport = env({
            "jobPosting/111": (200, "<html>should not happen</html>", {}),
            "jobPosting/4472494924": (200, fixture("linkedin_guest_jobposting.html"), {}),
            "th.jobsdb.com/job/95340875": (200, fixture("jobsdb_job_95340875.html"), {}),
        })
        fetch_jd.run(cfg, self.conn, fetcher)
        self.assertFalse(any("jobPosting/111" in url for url in transport.urls))

    def test_a_source_filter_fetches_only_that_source(self):
        cfg, fetcher, transport = env({
            "jobPosting/4472494924": (200, fixture("linkedin_guest_jobposting.html"), {}),
            "th.jobsdb.com/job/95340875": (200, fixture("jobsdb_job_95340875.html"), {}),
        })
        report = fetch_jd.run(cfg, self.conn, fetcher, sources=["jobsdb"])
        self.assertEqual(report["fetched"], 1)
        self.assertEqual(list(report["by_source"]), ["jobsdb"])
        self.assertFalse(any("jobPosting/" in url for url in transport.urls))
        self.assertIsNone(store.get_job(self.conn, self.linkedin_id)["jd_text"])

    def test_a_limit_caps_one_run_and_leaves_the_rest(self):
        cfg, fetcher, transport = env({
            "jobPosting/4472494924": (200, fixture("linkedin_guest_jobposting.html"), {}),
            "th.jobsdb.com/job/95340875": (200, fixture("jobsdb_job_95340875.html"), {}),
        })
        report = fetch_jd.run(cfg, self.conn, fetcher, limit=1)
        self.assertEqual(report["attempted"], 1)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(len(store.jd_candidates(self.conn, 100)), 1)   # one still waiting

    def test_an_unsupported_source_is_skipped_without_a_request(self):
        store.add_job(self.conn, job(source="manual", title="Pasted job", jd_text=None))
        cfg, fetcher, transport = env({})
        report = fetch_jd.run(cfg, self.conn, fetcher)
        self.assertEqual(report["attempted"], 2)   # the two supported ones
        self.assertEqual(report["skipped"], 1)
        self.assertFalse(any("manual" in url for url in transport.urls))


if __name__ == "__main__":
    unittest.main()
