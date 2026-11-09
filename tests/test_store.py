import unittest

from _util import job, memory_conn

from jobhunt import store


class CanonicalUrl(unittest.TestCase):
    def test_tracking_parameters_are_removed(self):
        self.assertEqual(
            store.canonical_url("https://www.linkedin.com/jobs/view/4400000001/?trk=public_jobs&refId=abc&e=1"),
            "https://linkedin.com/jobs/view/4400000001")

    def test_www_and_trailing_slash_are_dropped(self):
        self.assertEqual(store.canonical_url("https://WWW.Example.com/jobs/7/"),
                         "https://example.com/jobs/7")

    def test_fragment_is_dropped(self):
        self.assertEqual(store.canonical_url("https://example.com/jobs/7#apply"),
                         "https://example.com/jobs/7")

    def test_meaningful_query_parameters_survive_in_a_stable_order(self):
        self.assertEqual(store.canonical_url("https://example.com/j?b=2&a=1"),
                         "https://example.com/j?a=1&b=2")

    def test_junk_gives_none(self):
        self.assertIsNone(store.canonical_url("not a url"))
        self.assertIsNone(store.canonical_url(None))


class SourceJobId(unittest.TestCase):
    def test_linkedin_view_and_guest_shapes(self):
        self.assertEqual(store.extract_source_job_id(
            "linkedin", "https://www.linkedin.com/jobs/view/4400000001/?trk=x"), "4400000001")
        self.assertEqual(store.extract_source_job_id(
            "linkedin", "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/4400000002"), "4400000002")

    def test_jobsdb_and_greenhouse(self):
        self.assertEqual(store.extract_source_job_id("jobsdb", "https://th.jobsdb.com/job/95340875"), "95340875")
        self.assertEqual(store.extract_source_job_id(
            "greenhouse", "https://job-boards.greenhouse.io/agoda/jobs/8194690"), "8194690")

    def test_unknown_source_gives_none(self):
        self.assertIsNone(store.extract_source_job_id("manual", "https://example.com/careers"))


class Dedupe(unittest.TestCase):
    def setUp(self):
        self.conn = memory_conn(self)

    def add(self, **kw):
        return store.add_job(self.conn, job(**kw))

    def test_same_source_job_id_is_one_job_even_with_different_urls(self):
        first, created_first = self.add(source="linkedin", source_job_id="1", url="https://www.linkedin.com/jobs/view/1")
        second, created_second = self.add(source="linkedin", source_job_id="1",
                                          url="https://www.linkedin.com/jobs/view/1/?trk=other")
        self.assertTrue(created_first)
        self.assertFalse(created_second)
        self.assertEqual(first, second)

    def test_id_extracted_from_the_url_dedupes_without_an_explicit_id(self):
        first, _ = self.add(source="linkedin", url="https://www.linkedin.com/jobs/view/2")
        second, created = self.add(source="linkedin", url="https://www.linkedin.com/jobs/view/2?refId=zz")
        self.assertFalse(created)
        self.assertEqual(first, second)

    def test_same_canonical_url_from_another_source_dedupes(self):
        first, _ = self.add(source="manual", title="A", company="Co", url="https://example.com/jobs/9?utm_source=x")
        second, created = self.add(source="company_site", title="A", company="Co", url="https://www.example.com/jobs/9/")
        self.assertFalse(created)
        self.assertEqual(first, second)

    def test_company_title_and_location_dedupe(self):
        first, _ = self.add(source="manual", title="Management Trainee", company="Acme",
                            location="Bangkok, Thailand")
        second, created = self.add(source="jobsdb", title="management trainee.", company="ACME",
                                   location="bangkok, thailand")
        self.assertFalse(created)
        self.assertEqual(first, second)

    def test_same_title_in_a_different_city_is_a_different_job(self):
        bangkok, _ = self.add(source="manual", title="Management Trainee", company="Acme",
                              location="Bangkok, Thailand")
        singapore, created = self.add(source="manual", title="Management Trainee", company="Acme",
                                      location="Singapore")
        self.assertTrue(created)
        self.assertNotEqual(bangkok, singapore)

    def test_unknown_location_falls_back_to_company_and_title(self):
        first, _ = self.add(source="manual", title="Management Trainee", company="Acme",
                            location="Bangkok")
        second, created = self.add(source="jobsdb", title="Management Trainee", company="Acme",
                                   location=None)
        self.assertFalse(created)
        self.assertEqual(first, second)

    def test_reseeing_fills_empty_fields_and_moves_last_seen(self):
        first, _ = self.add(source="linkedin", source_job_id="3", title="Analyst")
        before = store.get_job(self.conn, first)
        second, created = store.add_job(self.conn, job(
            source="linkedin", source_job_id="3", title="Analyst", jd_text="the JD",
            posted_at="2026-11-06"), now="2030-01-01T00:00:00Z")
        after = store.get_job(self.conn, second)
        self.assertFalse(created)
        self.assertEqual(first, second)
        self.assertEqual(after["jd_text"], "the JD")
        self.assertEqual(after["jd_missing"], 0)
        self.assertEqual(after["posted_at"], "2026-11-06")
        self.assertEqual(after["last_seen_at"], "2030-01-01T00:00:00Z")
        self.assertEqual(before["last_seen_at"], after["first_seen_at"])


class ScoreHistory(unittest.TestCase):
    def setUp(self):
        self.conn = memory_conn(self)
        self.job_id, _ = store.add_job(self.conn, job(title="Analyst", company="Acme"))

    def insert(self, verdict, **kw):
        return store.insert_score(self.conn, self.job_id, {
            "status": "scored", "verdict": verdict,
            "created_at": store.utc_now(), "scored_at": store.utc_now(), **kw})

    def test_rescoring_supersedes_instead_of_replacing(self):
        first = self.insert("HOLD")
        second = self.insert("CENTRE")
        rows = self.conn.execute("SELECT * FROM scores WHERE job_id = ? ORDER BY id", (self.job_id,)).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["is_current"], 0)
        self.assertEqual(rows[0]["superseded_by"], second)
        self.assertEqual(rows[1]["is_current"], 1)
        self.assertEqual(store.current_score(self.conn, self.job_id)["id"], second)
        self.assertNotEqual(first, second)

    def test_history_is_kept(self):
        self.insert("LOW")
        self.insert("CENTRE")
        verdicts = [r["verdict"] for r in self.conn.execute(
            "SELECT verdict FROM scores WHERE job_id = ? ORDER BY id", (self.job_id,))]
        self.assertEqual(verdicts, ["LOW", "CENTRE"])


class Unscored(unittest.TestCase):
    def setUp(self):
        self.conn = memory_conn(self)

    def test_prefiltered_and_scored_jobs_are_not_picked_up(self):
        fresh, _ = store.add_job(self.conn, job(title="Fresh", company="A"))
        prefiltered, _ = store.add_job(self.conn, job(title="Senior X", company="B"))
        store.insert_score(self.conn, prefiltered, {"status": "prefiltered", "verdict": "FILTERED",
                                                    "prefilter_rule": "title_exclude: \\bsenior\\b",
                                                    "created_at": store.utc_now()})
        scored, _ = store.add_job(self.conn, job(title="Scored", company="C"))
        store.insert_score(self.conn, scored, {"status": "scored", "verdict": "CENTRE",
                                               "created_at": store.utc_now(), "scored_at": store.utc_now()})
        ids = [row["id"] for row in store.unscored_jobs(self.conn, 50)]
        self.assertEqual(ids, [fresh])

    def test_an_errored_job_is_retried(self):
        job_id, _ = store.add_job(self.conn, job(title="Retry", company="D"))
        store.insert_score(self.conn, job_id, {"status": "error", "error": "timeout",
                                               "created_at": store.utc_now()})
        self.assertEqual([row["id"] for row in store.unscored_jobs(self.conn, 50)], [job_id])


class Decisions(unittest.TestCase):
    def test_apply_then_skip_updates_one_row(self):
        conn = memory_conn(self)
        job_id, _ = store.add_job(conn, job(title="Analyst", company="Acme"))
        store.set_decision(conn, job_id, "apply", note="first")
        store.set_decision(conn, job_id, "skip", note="changed my mind")
        row = store.get_decision(conn, job_id)
        self.assertEqual(row["action"], "skip")
        self.assertEqual(row["note"], "changed my mind")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
