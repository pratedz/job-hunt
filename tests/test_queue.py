"""Round-3 rules: score only what has something to read, and re-score when the JD lands."""

import unittest
from dataclasses import replace

from _fake import FakeClock, FakeTransport, fixture, search_page
from _util import CFG, entry, job, memory_conn, paid, profile, reply

from jobhunt import pipeline, score as score_mod, store
from jobhunt.http import PoliteFetcher
from jobhunt.llm import FakeLLM
from jobhunt.sources import linkedin


def add(conn, **kw):
    kw.setdefault("title", "Management Trainee")
    kw.setdefault("company", "Acme")
    job_id, _ = store.add_job(conn, job(**kw))
    return job_id


def score_row(verdict="CENTRE", **kw):
    base = {"status": "scored", "verdict": verdict, "gate_result": "pass",
            "love": 4, "good_at": 4, "needs": 4, "paid": 3,
            "created_at": store.utc_now(), "scored_at": store.utc_now()}
    base.update(kw)
    return base


class QueueEligibility(unittest.TestCase):
    def setUp(self):
        self.conn = memory_conn(self)

    def ids(self):
        return [row["id"] for row in store.unscored_jobs(self.conn, 100)]

    def test_a_job_with_no_description_is_not_scored(self):
        job_id = add(self.conn, source="jobsdb", jd_text=None)
        self.assertEqual(self.ids(), [])
        self.assertEqual(store.get_job(self.conn, job_id)["jd_missing"], 0)

    def test_a_job_whose_fetch_failed_is_scored_from_the_snippet(self):
        job_id = add(self.conn, source="jobsdb", jd_text=None, email_snippet="A short snippet")
        store.mark_jd_missing(self.conn, job_id)
        self.assertEqual(self.ids(), [job_id])

    def test_a_manual_job_is_scored_without_a_description(self):
        job_id = add(self.conn, source="manual", jd_text=None)
        self.assertEqual(self.ids(), [job_id])

    def test_a_job_with_a_description_is_scored(self):
        job_id = add(self.conn, source="jobsdb", jd_text="Full description")
        self.assertEqual(self.ids(), [job_id])

    def test_jobs_with_a_description_come_first(self):
        snippet = add(self.conn, source="jobsdb", title="Snippet job", jd_text=None,
                      email_snippet="snippet")
        store.mark_jd_missing(self.conn, snippet)
        full = add(self.conn, source="jobsdb", title="Full job", jd_text="Full description")
        self.assertEqual(self.ids()[:2], [full, snippet])


class StaleScores(unittest.TestCase):
    def setUp(self):
        self.conn = memory_conn(self)

    def test_a_score_made_without_a_jd_is_requeued_when_the_jd_arrives(self):
        job_id = add(self.conn, source="jobsdb", jd_text=None, email_snippet="snippet")
        store.mark_jd_missing(self.conn, job_id)
        store.insert_score(self.conn, job_id, score_row(jd_used=0))
        self.assertEqual(store.unscored_jobs(self.conn, 10), [])   # nothing new to do yet

        store.set_jd(self.conn, job_id, "The real description", "jobAdDetails")
        self.assertEqual([row["id"] for row in store.unscored_jobs(self.conn, 10)], [job_id])

    def test_a_score_made_with_the_jd_is_not_requeued(self):
        job_id = add(self.conn, source="jobsdb", jd_text="Description")
        store.insert_score(self.conn, job_id, score_row(jd_used=1))
        store.set_jd(self.conn, job_id, "Description, refreshed", "jobAdDetails")
        self.assertEqual(store.unscored_jobs(self.conn, 10), [])

    def test_an_errored_score_is_always_retried(self):
        job_id = add(self.conn, source="jobsdb", jd_text="Description")
        store.insert_score(self.conn, job_id, {"status": "error", "error": "timeout",
                                              "created_at": store.utc_now()})
        self.assertEqual([row["id"] for row in store.unscored_jobs(self.conn, 10)], [job_id])


class RescoreThroughThePipeline(unittest.TestCase):
    def setUp(self):
        self.conn = memory_conn(self)
        self.job_id = add(self.conn, source="jobsdb", jd_text=None, email_snippet="snippet")
        store.mark_jd_missing(self.conn, self.job_id)

    def answers(self, prompt, job_ids):
        return reply(*[entry(i, 3, 3, 3, pay=paid(30000, 40000, "THB", "month", True))
                       for i in job_ids])

    def test_the_score_records_whether_the_jd_was_used(self):
        score_mod.run(CFG, self.conn, FakeLLM([self.answers]))
        first = store.current_score(self.conn, self.job_id)
        self.assertEqual(first["jd_used"], 0)

        store.set_jd(self.conn, self.job_id, "The real description", "jobAdDetails")
        score_mod.run(CFG, self.conn, FakeLLM([self.answers]))
        history = self.conn.execute(
            "SELECT jd_used, is_current, superseded_by FROM scores WHERE job_id = ? ORDER BY id",
            (self.job_id,)).fetchall()
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["is_current"], 0)
        self.assertIsNotNone(history[0]["superseded_by"])
        self.assertEqual(history[1]["is_current"], 1)
        self.assertEqual(history[1]["jd_used"], 1)

    def test_a_job_without_a_description_never_reaches_the_model(self):
        other = add(self.conn, source="jobsdb", title="No JD yet", jd_text=None)
        llm = FakeLLM([self.answers])
        score_mod.run(CFG, self.conn, llm)
        asked = [job_id for call in llm.calls for job_id in call["job_ids"]]
        self.assertNotIn(str(other), asked)
        self.assertIn(str(self.job_id), asked)


class LinkedInPacing(unittest.TestCase):
    def test_the_linkedin_delay_comes_from_delay_per_host(self):
        p = PoliteFetcher(profile())
        self.assertEqual(p.spacing_for("www.linkedin.com"), 8.0)
        self.assertEqual(p.spacing_for("th.jobsdb.com"),
                         float(profile()["limits"]["min_seconds_between_requests"]))

    def test_the_linkedin_wait_is_eight_seconds(self):
        clock = FakeClock()
        transport = FakeTransport({"linkedin": (200, "ok", {})})
        p = PoliteFetcher(profile(), transport=transport, sleep=clock.sleep, now=clock.now)
        p.get("https://www.linkedin.com/a")
        p.get("https://www.linkedin.com/b")
        self.assertEqual(clock.sleeps, [8.0])

    def test_other_hosts_keep_the_normal_delay(self):
        clock = FakeClock()
        transport = FakeTransport({"jobsdb": (200, "ok", {})})
        p = PoliteFetcher(profile(), transport=transport, sleep=clock.sleep, now=clock.now)
        p.get("https://th.jobsdb.com/a")
        p.get("https://th.jobsdb.com/b")
        self.assertEqual(clock.sleeps, [3.0])

    def test_linkedin_stops_after_its_own_page_cap(self):
        cfg = replace(CFG, profile=profile(**{"search.linkedin_queries": [["kw", "Bangkok"]]}))
        cap = int(cfg.get("search.linkedin_max_pages"))
        self.assertLess(cap, int(cfg.get("search.max_pages")))   # its own, smaller cap
        transport = FakeTransport({"seeMoreJobPostings": (200, search_page(("1", "Trainee")), {})})
        clock = FakeClock()
        p = PoliteFetcher(cfg.profile, transport=transport, sleep=clock.sleep, now=clock.now)
        linkedin.collect(cfg, p, 14)
        self.assertEqual(len(transport.calls), cap)        # linkedin_max_pages, not max_pages
        self.assertEqual([url.split("start=")[1].split("&")[0] for url in transport.urls],
                         [str(page * 10) for page in range(cap)])

    def test_jobsdb_still_uses_max_pages(self):
        from jobhunt.sources import jobsdb
        cfg = replace(CFG, profile=profile(**{"search.max_pages": 2, "search.linkedin_max_pages": 3,
                                              "search.keywords": ["kw"],
                                              "search.jobsdb_locations": ["Bangkok"]}))
        transport = FakeTransport({"jobsearch/v5/search": (200, {"data": [{
            "id": "1", "title": "Trainee", "advertiser": {"description": "Co"},
            "locations": [{"label": "Bangkok"}], "listingDate": "2026-11-06T00:00:00Z"}]}, {})})
        clock = FakeClock()
        p = PoliteFetcher(cfg.profile, transport=transport, sleep=clock.sleep, now=clock.now)
        jobsdb.collect(cfg, p, 3650)
        self.assertEqual(len(transport.calls), 2)


if __name__ == "__main__":
    unittest.main()


class Round4(unittest.TestCase):
    """linkedin_queries, JD priority, and the search/JD budget split."""

    def config(self, **overrides):
        return replace(CFG, profile=profile(**overrides))

    def test_explicit_queries_replace_the_keyword_grid(self):
        cfg = self.config(**{"search.linkedin_queries": [["a", "X"], ["b", "Y"]],
                             "search.keywords": ["kw1", "kw2"],
                             "search.linkedin_locations": ["L1", "L2"]})
        self.assertEqual(linkedin.queries(cfg), [("a", "X"), ("b", "Y")])

    def test_without_queries_the_grid_is_used(self):
        cfg = self.config(**{"search.linkedin_queries": [],
                             "search.keywords": ["kw1", "kw2"],
                             "search.linkedin_locations": ["L1", "L2"]})
        self.assertEqual(linkedin.queries(cfg), [("kw1", "L1"), ("kw1", "L2"),
                                                 ("kw2", "L1"), ("kw2", "L2")])

    def test_collect_walks_the_query_pairs(self):
        cfg = self.config(**{"search.linkedin_queries": [["a", "X"], ["b", "Y"]],
                             "search.linkedin_max_pages": 1})
        transport = FakeTransport({"seeMoreJobPostings": (200, search_page(("1", "Trainee")), {})})
        clock = FakeClock()
        p = PoliteFetcher(cfg.profile, transport=transport, sleep=clock.sleep, now=clock.now)
        linkedin.collect(cfg, p, 14)
        self.assertEqual(len(transport.calls), 2)
        self.assertIn("keywords=a", transport.urls[0])
        self.assertIn("location=X", transport.urls[0])
        self.assertIn("keywords=b", transport.urls[1])

    def test_the_search_stops_at_its_budget(self):
        cfg = self.config(**{"search.linkedin_queries": [["a", "X"], ["b", "Y"]],
                             "search.linkedin_max_pages": 2})
        transport = FakeTransport({"seeMoreJobPostings": (200, search_page(("1", "Trainee")), {})})
        clock = FakeClock()
        p = PoliteFetcher(cfg.profile, transport=transport, sleep=clock.sleep, now=clock.now)
        linkedin.collect(cfg, p, 14, max_requests=1)
        self.assertEqual(len(transport.calls), 1)

    def test_searches_get_half_the_linkedin_cap(self):
        cfg = self.config(**{"limits.per_host": {"www.linkedin.com": 45}})
        p = PoliteFetcher(cfg.profile, transport=FakeTransport({}))
        self.assertEqual(pipeline.search_budgets(cfg, p), {"linkedin": 22})

    def test_the_jobsdb_collector_also_takes_a_budget(self):
        from jobhunt.sources import jobsdb
        cfg = self.config(**{"search.keywords": ["kw"], "search.jobsdb_locations": ["Bangkok"],
                             "search.max_pages": 3})
        transport = FakeTransport({"jobsearch/v5/search": (200, {"data": [{
            "id": "1", "title": "Trainee", "advertiser": {"description": "Co"},
            "locations": [{"label": "Bangkok"}], "listingDate": "2026-11-06T00:00:00Z"}]}, {})})
        clock = FakeClock()
        p = PoliteFetcher(cfg.profile, transport=transport, sleep=clock.sleep, now=clock.now)
        jobsdb.collect(cfg, p, 3650, max_requests=1)
        self.assertEqual(len(transport.calls), 1)


class JdPriority(unittest.TestCase):
    def setUp(self):
        self.conn = memory_conn(self)
        self.profile = profile()

    def add(self, title, posted_at=None, first_seen_at=None, **kw):
        job_id, _ = store.add_job(self.conn, job(**{"title": title, "posted_at": posted_at, **kw}))
        if first_seen_at:
            self.conn.execute("UPDATE jobs SET first_seen_at = ? WHERE id = ?",
                              (first_seen_at, job_id))
            self.conn.commit()
        return job_id

    def ordered(self):
        from jobhunt import fetch_jd
        rows = store.jd_candidates(self.conn, 100)
        return [row["id"] for row in fetch_jd.order_candidates(rows, self.profile)]

    def test_on_track_titles_come_first(self):
        plain = self.add("Account Specialist")
        grad = self.add("Graduate Programme")
        trainee = self.add("Management Trainee")
        order = self.ordered()
        self.assertLess(order.index(grad), order.index(plain))
        self.assertLess(order.index(trainee), order.index(plain))

    def test_then_newest_posted(self):
        old = self.add("Trainee old", posted_at="2026-10-01")
        new = self.add("Trainee new", posted_at="2026-11-06")
        undated = self.add("Trainee undated")
        order = self.ordered()
        self.assertLess(order.index(new), order.index(old))
        self.assertLess(order.index(old), order.index(undated))

    def test_then_oldest_first_seen(self):
        first = self.add("Trainee A", first_seen_at="2026-11-01T00:00:00Z")
        second = self.add("Trainee B", first_seen_at="2026-11-05T00:00:00Z")
        self.assertEqual(self.ordered(), [first, second])

    def test_fetch_jd_uses_the_priority_order(self):
        from jobhunt import fetch_jd
        self.add("Account Specialist", source="linkedin", source_job_id="1")
        trainee = self.add("Management Trainee", source="linkedin", source_job_id="2")
        transport = FakeTransport({"linkedin": (200, fixture("linkedin_guest_jobposting.html"), {})})
        clock = FakeClock()
        p = PoliteFetcher(self.profile, transport=transport, sleep=clock.sleep, now=clock.now)
        report = fetch_jd.run(CFG, self.conn, p, limit=1)
        self.assertEqual(report["attempted"], 1)
        fetched = store.get_job(self.conn, trainee)
        self.assertIn("Aman Nai Lert", fetched["jd_text"] or "")
