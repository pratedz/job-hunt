import json
import unittest
from datetime import datetime, timezone

from dataclasses import replace

from _fake import FakeClock, FakeTransport, fixture, fixture_json, search_page
from _util import CFG, profile

from jobhunt.http import HostStopped, PoliteFetcher
from jobhunt.sources import jobsdb, linkedin


def make_env(routes, **overrides):
    """A cfg and a fetcher that share the same overridden profile."""
    overrides.setdefault("limits.min_seconds_between_requests", 0)
    cfg = replace(CFG, profile=profile(**overrides))
    clock = FakeClock()
    transport = FakeTransport(routes)
    fetcher = PoliteFetcher(cfg.profile, transport=transport, sleep=clock.sleep, now=clock.now)
    return cfg, fetcher, transport


class LinkedInParsing(unittest.TestCase):
    def setUp(self):
        self.cards = linkedin.parse_cards(fixture("linkedin_guest_search.html"))

    def test_the_sample_has_ten_cards(self):
        self.assertEqual(len(self.cards), 10)

    def test_the_first_card(self):
        first = self.cards[0]
        self.assertEqual(first["source"], "linkedin")
        self.assertEqual(first["source_job_id"], "4472494924")
        self.assertIn("IGNITE", first["title"])
        self.assertEqual(first["company"], "Aman")
        self.assertIn("Bangkok", first["location"])
        self.assertEqual(first["posted_at"], "2026-10-19")
        self.assertEqual(first["url"], "https://www.linkedin.com/jobs/view/4472494924")

    def test_every_card_has_an_id_and_a_title(self):
        for card in self.cards:
            self.assertTrue(card["source_job_id"].isdigit())
            self.assertTrue(card["title"])

    def test_an_empty_page_gives_no_cards(self):
        self.assertEqual(linkedin.parse_cards("<html><body><ul></ul></body></html>"), [])

    def test_a_login_wall_page_gives_no_cards(self):
        self.assertEqual(linkedin.parse_cards("<html><body>Sign in to continue</body></html>"), [])


class LinkedInPosting(unittest.TestCase):
    def setUp(self):
        self.posting = linkedin.parse_posting(fixture("linkedin_guest_jobposting.html"))

    def test_the_jd_is_extracted(self):
        self.assertGreater(len(self.posting["jd_text"]), 500)
        self.assertIn("Aman Nai Lert", self.posting["jd_text"])

    def test_the_top_card_is_read(self):
        self.assertIn("IGNITE", self.posting["title"])
        self.assertEqual(self.posting["company"], "Aman")
        self.assertIn("Bangkok", self.posting["location"])

    def test_the_criteria_are_listed(self):
        self.assertTrue(any("Employment type" in item for item in self.posting["criteria"]))

    def test_paragraph_breaks_are_kept(self):
        self.assertIn("\n", self.posting["jd_text"])


class LinkedInUrls(unittest.TestCase):
    def test_pagination_steps_by_ten(self):
        first = linkedin.search_url("management trainee", "Bangkok, Thailand", 0, 3)
        second = linkedin.search_url("management trainee", "Bangkok, Thailand", 10, 3)
        self.assertIn("start=0", first)
        self.assertIn("start=10", second)

    def test_the_posted_time_filter_is_in_seconds(self):
        self.assertIn("f_TPR=r259200", linkedin.search_url("kw", "loc", 0, 3))
        self.assertIn("f_TPR=r1209600", linkedin.search_url("kw", "loc", 0, 14))

    def test_the_keyword_is_url_encoded(self):
        self.assertIn("keywords=management+trainee",
                      linkedin.search_url("management trainee", "Bangkok", 0, 3))

    def test_urls(self):
        self.assertEqual(linkedin.posting_url("123"),
                         "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/123")
        self.assertEqual(linkedin.job_url("123"), "https://www.linkedin.com/jobs/view/123")


class LinkedInCollect(unittest.TestCase):
    def test_pages_are_walked_and_deduped(self):
        cfg, p, transport = make_env({"seeMoreJobPostings": (200, fixture("linkedin_guest_search.html"), {})},
                                    **{"search.linkedin_queries": [["management trainee", "Bangkok"]],
                                       "search.linkedin_max_pages": 2,
                                       "search.max_pages": 5})
        jobs = linkedin.collect(cfg, p, 3)
        self.assertEqual(len(jobs), 10)          # the same page twice, deduped by id
        self.assertEqual(len(transport.calls), 2)

    def test_an_empty_page_stops_the_walk(self):
        cfg, p, transport = make_env({
            "start=10": (200, "<html></html>", {}),
            "seeMoreJobPostings": (200, fixture("linkedin_guest_search.html"), {}),
        }, **{"search.linkedin_queries": [["kw", "Bangkok"]],
              "search.linkedin_max_pages": 5})
        linkedin.collect(cfg, p, 3)
        self.assertEqual(len(transport.calls), 2)   # page 1, then the empty page 2

    def test_max_pages_is_respected(self):
        cfg, p, transport = make_env({"seeMoreJobPostings": (200, fixture("linkedin_guest_search.html"), {})},
                                    **{"search.linkedin_queries": [["kw", "Bangkok"]],
                                       "search.linkedin_max_pages": 3,
                                       "search.max_pages": 5})
        linkedin.collect(cfg, p, 3)
        self.assertEqual(len(transport.calls), 3)

    def test_a_stopped_host_ends_the_walk_without_raising(self):
        cfg, p, transport = make_env({"seeMoreJobPostings": (999, "no", {})},
                                    **{"search.linkedin_queries": [["a", "Bangkok"], ["b", "Bangkok"]],
                                       "search.max_pages": 3})
        jobs = linkedin.collect(cfg, p, 3)
        self.assertEqual(jobs, [])
        self.assertEqual(len(transport.calls), 1)
        self.assertIn("www.linkedin.com", p.stopped_hosts())


class JobsDBParsing(unittest.TestCase):
    def setUp(self):
        self.jobs = jobsdb.parse_search(fixture_json("jobsdb_search_v5.json"))

    def test_the_sample_has_jobs(self):
        self.assertEqual(len(self.jobs), 5)

    def test_the_first_job(self):
        first = self.jobs[0]
        self.assertEqual(first["source"], "jobsdb")
        self.assertEqual(first["source_job_id"], "95744952")
        self.assertEqual(first["title"], "Management Trainee")
        self.assertEqual(first["company"], "Bangkok Bank Public Company Limited")
        self.assertIn("Bangkok", first["location"])
        self.assertEqual(first["url"], "https://th.jobsdb.com/job/95744952")
        self.assertEqual(first["posted_at"], "2026-11-06T09:52:21Z")

    def test_the_teaser_becomes_the_snippet(self):
        self.assertTrue(self.jobs[0]["email_snippet"])

    def test_a_salary_label_is_kept(self):
        jobs = jobsdb.parse_search({"data": [{
            "id": "1", "title": "Trainee", "advertiser": {"description": "Co"},
            "locations": [{"label": "Bangkok"}], "salaryLabel": "THB 30,000-40,000 per month",
            "listingDate": "2026-11-06T00:00:00Z",
        }]})
        self.assertEqual(jobs[0]["salary_text"], "THB 30,000-40,000 per month")

    def test_an_empty_payload_gives_no_jobs(self):
        self.assertEqual(jobsdb.parse_search({"data": []}), [])

    def test_listing_dates_parse_as_utc(self):
        stamp = jobsdb.listing_datetime("2026-11-06T09:52:21Z")
        self.assertEqual(stamp, datetime(2026, 11, 6, 9, 52, 21, tzinfo=timezone.utc))
        self.assertIsNone(jobsdb.listing_datetime("not a date"))


class JobsDBJobPage(unittest.TestCase):
    def test_the_description_comes_from_the_page(self):
        parsed = jobsdb.parse_job_page(fixture("jobsdb_job_95340875.html"))
        self.assertEqual(parsed["jd_source"], "jobAdDetails")
        self.assertGreater(len(parsed["jd_text"]), 500)
        self.assertIn("Deliveree", parsed["jd_text"])
        self.assertIn("\n", parsed["jd_text"])

    def test_ld_json_is_preferred_when_present(self):
        html = """<html><head><script type="application/ld+json">
        {"@graph": [{"@type": "JobPosting", "title": "Management Trainee",
                     "hiringOrganization": {"name": "Acme"},
                     "datePosted": "2026-11-01",
                     "description": "<p>Rotational programme.</p><ul><li>Sales</li><li>Ops</li></ul>",
                     "baseSalary": {"currency": "THB", "value": {"minValue": 30000,
                                     "maxValue": 40000, "unitText": "MONTH"}}}]}
        </script></head><body><div data-automation="jobAdDetails">fallback</div></body></html>"""
        parsed = jobsdb.parse_job_page(html)
        self.assertEqual(parsed["jd_source"], "ld+json")
        self.assertIn("Rotational programme", parsed["jd_text"])
        self.assertIn("Sales", parsed["jd_text"])
        self.assertEqual(parsed["title"], "Management Trainee")
        self.assertEqual(parsed["company"], "Acme")
        self.assertIn("THB 30,000-40,000 per month", parsed["salary_text"])

    def test_a_page_without_a_description_gives_empty_text(self):
        self.assertEqual(jobsdb.parse_job_page("<html><body>nothing</body></html>")["jd_text"], "")


class JobsDBCollect(unittest.TestCase):
    def setUp(self):
        self.payload = fixture_json("jobsdb_search_v5.json")

    def test_pages_are_walked_and_jobs_in_window_kept(self):
        cfg, p, transport = make_env({"jobsearch/v5/search": (200, self.payload, {})},
                                    **{"search.keywords": ["kw"], "search.jobsdb_locations": ["Bangkok"],
                                       "search.max_pages": 2})
        jobs = jobsdb.collect(cfg, p, 30,
                              now=datetime(2026, 11, 7, 12, tzinfo=timezone.utc))
        self.assertEqual(len(jobs), 5)
        self.assertEqual(len(transport.calls), 2)

    def test_a_short_window_keeps_only_the_recent_jobs(self):
        cfg, p, transport = make_env({"jobsearch/v5/search": (200, self.payload, {})},
                                     **{"search.keywords": ["kw"],
                                        "search.jobsdb_locations": ["Bangkok"],
                                        "search.max_pages": 1})
        jobs = jobsdb.collect(cfg, p, 1.5,
                              now=datetime(2026, 11, 7, 12, tzinfo=timezone.utc))
        self.assertEqual(len(jobs), 2)   # only the two from 2026-11-06

    def test_jobs_outside_the_window_are_dropped(self):
        cfg, p, transport = make_env({"jobsearch/v5/search": (200, self.payload, {})},
                                    **{"search.keywords": ["kw"], "search.jobsdb_locations": ["Bangkok"],
                                       "search.max_pages": 1})
        jobs = jobsdb.collect(cfg, p, 0.1,
                              now=datetime(2026, 11, 7, 12, tzinfo=timezone.utc))
        self.assertLess(len(jobs), 5)

    def test_an_empty_page_stops_the_walk(self):
        cfg, p, transport = make_env({"page=2": (200, {"data": []}, {}),
                                     "jobsearch/v5/search": (200, self.payload, {})},
                                    **{"search.keywords": ["kw"], "search.jobsdb_locations": ["Bangkok"],
                                       "search.max_pages": 5})
        jobsdb.collect(cfg, p, 30,
                       now=datetime(2026, 11, 7, 12, tzinfo=timezone.utc))
        self.assertEqual(len(transport.calls), 2)

    def test_a_stopped_host_ends_the_walk(self):
        cfg, p, transport = make_env({"jobsearch/v5/search": (429, "", {})},
                                    **{"search.keywords": ["a", "b"],
                                       "search.jobsdb_locations": ["Bangkok"], "search.max_pages": 3})
        self.assertEqual(jobsdb.collect(cfg, p, 3), [])
        self.assertEqual(len(transport.calls), 1)


if __name__ == "__main__":
    unittest.main()
