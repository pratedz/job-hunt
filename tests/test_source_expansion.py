"""M2: JobThai, L'Oreal import, and Shopee/Sea paging. Temp DB only."""

import json
import unittest
from dataclasses import replace
from pathlib import Path

from _fake import FakeClock, FakeTransport
from _util import CFG, entry, memory_conn, profile, reply

from jobhunt import fetch_jd, pipeline, score as score_mod, store
from jobhunt.http import PoliteFetcher
from jobhunt.llm import FakeLLM
from jobhunt.sources import companies, jobthai
from jobhunt.sources.companies import adapters

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "source_expansion"


def text(name: str) -> str:
    with open(f"{FIXTURES}/{name}", encoding="utf-8") as handle:
        return handle.read()


def cfg(**overrides):
    return replace(CFG, profile=profile(**overrides))


def fetcher(routes, profile_overrides=None):
    data = profile(**(profile_overrides or {}))
    clock = FakeClock()
    transport = FakeTransport(routes)
    return PoliteFetcher(data, transport=transport, sleep=clock.sleep, now=clock.now), transport


def sea_page(jobs, total):
    return {"code": 0, "data": {"total_count": total, "job_list": jobs}}


def sea_job(job_id, title="Livestream Affiliate Key Account Management (Shopee)"):
    return {"job_id": job_id, "job_name": title, "city_id": 31,
            "job_description": "<p>Sell through Shopee livestream.</p>", "requirements": ""}


class JobThaiParse(unittest.TestCase):
    def test_list_keeps_the_update_stamp_off_posted_at(self):
        jobs = jobthai.parse_search(text("jobthai-key-account-page.html"))
        by_id = {job["source_job_id"]: job for job in jobs}
        self.assertEqual(set(by_id), {"1913343", "1971890", "1924574"})
        bangkok = by_id["1924574"]
        self.assertEqual(bangkok["source"], "jobthai")
        self.assertEqual(bangkok["title"], "Key Account E-commerce")
        self.assertEqual(bangkok["location"], "Saphan Sung, Bangkok")
        self.assertIsNone(bangkok["posted_at"])
        self.assertEqual(bangkok["email_snippet"], "updated_at: 2026-11-06T04:35:40.000Z")
        self.assertEqual(bangkok["url"], "https://www.jobthai.com/en/company/job/1924574")
        self.assertTrue(jobthai.valid_job_url(bangkok["url"]))
        self.assertFalse(jobthai.valid_job_url("https://example.com/en/company/job/1924574"))
        self.assertIsNone(bangkok["jd_text"])

    def test_scope_drops_rayong_and_keeps_bangkok_key_account(self):
        jobs = jobthai.parse_search(text("jobthai-key-account-page.html"))
        kept = [job for job in jobs if jobthai.in_scope(
            job, ["Bangkok", "Nonthaburi"], ["key account"])]
        self.assertEqual(sorted(job["source_job_id"] for job in kept), ["1913343", "1924574"])

    def test_detail_is_the_role_not_the_nav(self):
        parsed = jobthai.parse_detail(text("jobthai-1924574.html"))
        self.assertIn("E-commerce", parsed["jd_text"])
        self.assertIn("Lazada", parsed["jd_text"])
        self.assertIn("Marketplace", parsed["jd_text"])
        self.assertNotIn("Cookie Policy", parsed["jd_text"])
        self.assertEqual(parsed["posted_at"], "2026-11-06T04:35:40.000Z")
        self.assertEqual(jobthai.parse_detail("<html><nav>Cookie Policy</nav></html>")["jd_text"], "")

    def test_a_non_string_detail_date_is_ignored(self):
        description = "This is a long enough key account description for a Bangkok commercial role."
        for raw in ({"@type": "Date"}, ["2026-01-01"], 20260101, "", "   "):
            html = "<script type=\"application/ld+json\">" + json.dumps({
                "@type": "JobPosting", "title": "Key Account",
                "datePosted": raw, "description": description,
            }) + "</script>"
            parsed = jobthai.parse_detail(html)
            self.assertIsNone(parsed["posted_at"])
            self.assertIn("key account", parsed["jd_text"].casefold())
            loreal = adapters.parse_loreal_detail(html)
            self.assertIsNone(loreal["posted_at"])
            self.assertIn("key account", loreal["jd_text"].casefold())

    def test_a_broken_page_is_empty(self):
        self.assertEqual(jobthai.parse_search("<html>no jobs</html>"), [])
        self.assertEqual(jobthai.parse_search('{"data":'), [])

    def test_malformed_shapes_do_not_raise(self):
        for payload in (
            {"data": {"total": 1}},
            {"data": {"total": 1, "data": "nope"}},
            {"data": {"total": 1, "data": [1, "x", {"id": {"bad": 1}}]}},
        ):
            self.assertEqual(jobthai.parse_search(json.dumps(payload)), [])
        weird = {"data": {"total": 1, "data": [{
            "id": 12, "jobTitle": {"en": "Key Account"}, "district": "Bangkok",
            "province": {"name": ["Bangkok"]}, "salary": {"min": 1}, "updatedAt": 20261106,
        }]}}
        jobs = jobthai.parse_search(json.dumps(weird))
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["source_job_id"], "12")
        self.assertIsNone(jobs[0]["title"])
        self.assertIsNone(jobs[0]["location"])
        self.assertIsNone(jobs[0]["posted_at"])
        self.assertIsNone(jobs[0]["email_snippet"])


class JobThaiDiscover(unittest.TestCase):
    def setUp(self):
        self.conn = memory_conn(self)
        self.profile_overrides = {
            "sources.jobthai": True,
            "jobthai.keywords": ["key account"],
            "jobthai.max_pages": 1,
            "jobthai.max_requests": 3,
            "jobthai.locations": ["Bangkok"],
            "jobthai.title_include": ["key account"],
            "companies": [],
        }
        self.cfg = cfg(**self.profile_overrides)
        routes = {
            "/company/job/1924574": (200, text("jobthai-1924574.html"), {}),
            "keyword=": (200, text("jobthai-key-account-page.html"), {}),
        }
        self.fetcher, self.transport = fetcher(routes, self.profile_overrides)

    def test_discover_then_detail_is_eligible_to_score(self):
        report = pipeline.discover(self.cfg, self.conn, self.fetcher, ["jobthai"])
        self.assertEqual(report["sources"]["jobthai"]["found"], 2)
        self.assertEqual(report["sources"]["jobthai"]["new"], 2)
        row = store.get_job(self.conn, 1)
        # ids are not ordered; find the e-commerce card
        rows = store.unscored_jobs(self.conn, 10)
        titled = {r["title"]: r for r in self.conn.execute("SELECT * FROM jobs")}
        job = titled["Key Account E-commerce"]
        self.assertEqual(job["source_job_id"], "1924574")
        self.assertNotIn("jobthai:", job["source_job_id"])
        self.assertIn("E-commerce", job["jd_text"])
        self.assertEqual(job["posted_at"], "2026-11-06T04:35:40.000Z")
        self.assertIn(job["id"], {r["id"] for r in rows})
        self.assertIsNone(store.current_score(self.conn, job["id"]))
        self.assertGreater(report["jd"]["fetched"], 0)

    def test_a_discovered_job_is_scored_by_a_fake_model(self):
        pipeline.discover(self.cfg, self.conn, self.fetcher, ["jobthai"])
        job = next(row for row in self.conn.execute("SELECT * FROM jobs")
                   if row["title"] == "Key Account E-commerce")
        self.assertTrue(job["jd_text"])
        report = score_mod.run(self.cfg, self.conn, FakeLLM([
            lambda prompt, ids: reply(*[entry(i) for i in ids])
        ]))
        self.assertGreaterEqual(report["scored"], 1)
        self.assertGreaterEqual(report["by_source"]["jobthai"]["scored"], 1)
        score = store.current_score(self.conn, job["id"])
        self.assertEqual(score["status"], "scored")
        self.assertEqual(score["jd_used"], 1)
        self.assertNotIn(job["id"], {row["id"] for row in store.unscored_jobs(self.conn, 20)})

    def test_a_second_collect_is_a_duplicate(self):
        pipeline.discover(self.cfg, self.conn, self.fetcher, ["jobthai"])
        again = pipeline.discover(self.cfg, self.conn, self.fetcher, ["jobthai"])
        self.assertEqual(again["sources"]["jobthai"]["duplicates"], 2)
        self.assertEqual(again["sources"]["jobthai"]["new"], 0)
        self.assertEqual(store.count_jobs(self.conn, "jobthai"), 2)

    def test_search_budget_is_below_the_host_cap(self):
        budgets = pipeline.search_budgets(self.cfg, self.fetcher)
        self.assertEqual(budgets["jobthai"], 3)
        self.assertLess(budgets["jobthai"], self.fetcher.cap_for("www.jobthai.com"))

    def test_a_blocked_host_does_not_raise(self):
        blocked, _transport = fetcher(
            {"keyword=": (429, "too many", {})}, self.profile_overrides)
        report = pipeline.discover(self.cfg, self.conn, blocked, ["jobthai"])
        self.assertEqual(report["sources"]["jobthai"]["found"], 0)
        self.assertEqual(report["sources"]["jobthai"]["host_stopped"], 1)

    def test_discover_without_a_passed_budget_stops_at_max_requests(self):
        overrides = {
            "sources.jobthai": True,
            "jobthai.keywords": ["a", "b", "c", "d"],
            "jobthai.max_requests": 3,
            "jobthai.locations": ["Bangkok"],
            "jobthai.title_include": ["key account"],
            "companies": [],
        }
        polite, transport = fetcher(
            {"keyword=": (200, text("jobthai-key-account-page.html"), {})}, overrides)
        pipeline.discover(cfg(**overrides), memory_conn(self), polite, ["jobthai"],
                          fetch_descriptions=False)
        self.assertEqual(len(transport.urls), 3)

    def test_a_collector_error_is_recorded_and_the_next_source_runs(self):
        conn = memory_conn(self)
        sea_body = json.loads((ROOT / "tests/fixtures/companies/json_watch_sea.json").read_text())
        overrides = {
            "sources.jobthai": True,
            "companies": [{
                "name": "Sea", "adapter": "sea_ats",
                "url": "https://career.sea.com/api/user/job/list?limit=20&offset=0&city_ids=31",
            }],
            "companies_filter.keep": ["Thailand", "Bangkok"],
        }
        polite, _transport = fetcher({"career.sea.com": (200, sea_body, {})}, overrides)
        real = jobthai.collect

        def boom(*args, **kwargs):
            raise TypeError("data was a list")

        jobthai.collect = boom
        self.addCleanup(setattr, jobthai, "collect", real)
        report = pipeline.discover(cfg(**overrides), conn, polite, ["jobthai", "companies"],
                                   fetch_descriptions=False)
        self.assertEqual(report["sources"]["jobthai"]["errors"], 1)
        self.assertIn("TypeError", report["sources"]["jobthai"]["error"])
        self.assertEqual(report["sources"]["jobthai"]["found"], 0)
        self.assertEqual(report["companies"][0]["status"], "ok")
        self.assertEqual(report["companies"][0]["kept"], 1)


class Loreal(unittest.TestCase):
    def test_list_is_job_anchors_only(self):
        jobs = adapters.parse_loreal_list(text("loreal-thailand-search.html"), "L'Oreal")
        self.assertEqual(len(jobs), 8)
        ids = {job["source_job_id"] for job in jobs}
        self.assertIn("loreal:46578729152", ids)
        self.assertTrue(all(job["source"] == "company_site" for job in jobs))
        self.assertTrue(all(job["source_job_id"].startswith("loreal:") for job in jobs))
        self.assertTrue(all(not job["source_job_id"].startswith("company_site:") for job in jobs))
        self.assertTrue(all(job["jd_text"] is None for job in jobs))
        assistant = next(job for job in jobs if job["source_job_id"] == "loreal:46578729152")
        self.assertEqual(assistant["title"], "Assistant E-Commerce Manager - Tiktok")
        self.assertEqual(assistant["location"], "Bangkok, Thailand")
        self.assertTrue(assistant["url"].startswith("https://careers.loreal.com/en/job/"))

    def test_foreign_and_malformed_links_are_not_rewritten(self):
        html = """
        <a href="https://evil.example/en/job/bangkok/stolen/3456/999">Stolen Bangkok, Bangkok, Thailand</a>
        <a href="http://careers.loreal.com/en/job/bangkok/insecure/3456/333">Insecure Bangkok, Bangkok, Thailand</a>
        <a href="https://careers.loreal.com.evil.example/en/job/bangkok/lookalike/3456/444">Lookalike Bangkok, Bangkok, Thailand</a>
        <a href="/en/job/not-a-job">Broken</a>
        <a href="/en/jobs">Jobs</a>
        <a href="/en/job/bangkok/assistant-e-commerce-manager/3456/111">Assistant E-Commerce Manager Bangkok, Bangkok, Thailand</a>
        <a href="https://careers.loreal.com/en/job/bangkok/real-role/3456/222">Real Role Bangkok, Bangkok, Thailand</a>
        """
        jobs = adapters.parse_loreal_list(html, "L'Oreal")
        self.assertEqual([job["url"] for job in jobs], [
            "https://careers.loreal.com/en/job/bangkok/assistant-e-commerce-manager/3456/111",
            "https://careers.loreal.com/en/job/bangkok/real-role/3456/222",
        ])
        self.assertFalse(fetch_jd._loreal({
            "source": "company_site",
            "url": "https://evil.example/en/job/bangkok/stolen/3456/999",
        }))
        self.assertTrue(fetch_jd._loreal({
            "source": "company_site",
            "url": "https://careers.loreal.com/en/job/bangkok/real-role/3456/222",
        }))

    def test_detail_is_the_posting_body(self):
        parsed = adapters.parse_loreal_detail(text("loreal-46578729152.html"))
        self.assertIn("About the job", parsed["jd_text"])
        self.assertNotIn("What our people say", parsed["jd_text"])
        self.assertEqual(parsed["posted_at"], "2026-11-03")
        self.assertEqual(adapters.parse_loreal_detail("<html><h1>Assistant</h1></html>")["jd_text"], "")

    def test_discover_imports_and_fetches_only_a_real_description(self):
        conn = memory_conn(self)
        overrides = {
            "sources.companies": True,
            "companies": [{
                "name": "L'Oreal",
                "adapter": "loreal",
                "url": "https://careers.loreal.com/en/search-jobs/Thailand/3456/2/1605651/15x5/101/50/2",
            }],
            "companies_filter.keep": ["Thailand", "Bangkok"],
        }
        config = cfg(**overrides)
        polite, transport = fetcher({
            "46578729152": (200, text("loreal-46578729152.html"), {}),
            "search-jobs": (200, text("loreal-thailand-search.html"), {}),
        }, overrides)
        report = pipeline.discover(config, conn, polite, ["companies"], fetch_descriptions=True)
        self.assertEqual(report["companies"][0]["status"], "ok")
        self.assertEqual(report["companies"][0]["kept"], 8)
        titles = {row["title"]: row for row in conn.execute("SELECT * FROM jobs")}
        senior = titles["Senior E-Key Account Manager"]
        self.assertEqual(store.current_score(conn, senior["id"])["status"], "prefiltered")
        self.assertIsNone(senior["jd_text"])
        assistant = titles["Assistant E-Commerce Manager - Tiktok"]
        self.assertIn("About the job", assistant["jd_text"])
        self.assertEqual(assistant["jd_source"], "loreal_detail")
        self.assertEqual(assistant["posted_at"], "2026-11-03")
        self.assertIn(assistant["id"], {row["id"] for row in store.unscored_jobs(conn, 20)})
        self.assertTrue(any("search-jobs" in url for url in transport.urls))


class SeaPaging(unittest.TestCase):
    def test_offset_replaces_instead_of_duplicating(self):
        entry = {
            "name": "Shopee",
            "adapter": "sea_ats",
            "url": "https://ats.workatsea.com/ats/api/v1/user/job/list/?limit=1&offset=0&city_ids=31",
            "max_pages": 2,
            "job_url": "https://careers.shopee.co.th/job-detail/{id}/1",
        }
        pages = {
            "offset=0": (200, sea_page([sea_job("J1")], 2), {}),
            "offset=1": (200, sea_page([sea_job("J2", "Strategy")], 2), {}),
        }
        polite, transport = fetcher(pages)
        jobs = adapters.sea_ats(entry, polite)
        self.assertEqual([job["source_job_id"] for job in jobs], ["J1", "J2"])
        self.assertTrue(all("offset=0&offset=" not in url and "offset=0&amp;" not in url
                            for url in transport.urls))
        self.assertIn("offset=0", transport.urls[0])
        self.assertIn("offset=1", transport.urls[1])
        self.assertTrue(jobs[0]["url"].endswith("/J1/1"))

    def test_a_repeated_page_stops(self):
        body = sea_page([sea_job("J1")], 5)
        polite, transport = fetcher({"workatsea.com": (200, body, {})})
        entry = {
            "name": "Shopee", "url": "https://ats.workatsea.com/ats/api/v1/user/job/list/?limit=1&offset=0",
            "max_pages": 5,
        }
        jobs = adapters.sea_ats(entry, polite)
        self.assertEqual(len(jobs), 1)
        self.assertIsNone(jobs[0]["url"])
        self.assertLess(len(transport.urls), 5)

    def test_a_later_failure_keeps_the_first_page(self):
        polite, _transport = fetcher({
            "offset=0": (200, sea_page([sea_job("J1")], 2), {}),
            "offset=1": (500, "nope", {}),
        })
        entry = {
            "name": "Shopee",
            "url": "https://ats.workatsea.com/ats/api/v1/user/job/list/?limit=1&offset=0",
            "max_pages": 3,
        }
        jobs = adapters.sea_ats(entry, polite)
        self.assertEqual([job["source_job_id"] for job in jobs], ["J1"])
        self.assertIn("HTTP 500", jobs.partial)

    def _shopee(self):
        return {
            "name": "Shopee", "adapter": "sea_ats",
            "url": "https://ats.workatsea.com/ats/api/v1/user/job/list/?limit=1&offset=0&city_ids=31",
            "max_pages": 3,
        }

    def test_a_later_block_is_a_partial_list(self):
        overrides = {"companies": [self._shopee()], "companies_filter.keep": ["Bangkok", "Thailand"]}
        polite, transport = fetcher({
            "offset=0": (200, sea_page([sea_job("J1")], 2), {}),
            "offset=1": (429, "slow down", {}),
        }, overrides)
        jobs, reports = companies.collect(cfg(**overrides), memory_conn(self), polite,
                                          log=lambda *a, **k: None)
        self.assertEqual([job["source_job_id"] for job in jobs], ["J1"])
        self.assertEqual(reports[0]["status"], "partial")
        self.assertIn("HostStopped", reports[0]["error"])
        self.assertEqual(len(transport.urls), 2)

    def test_a_later_malformed_page_is_partial(self):
        overrides = {"companies": [self._shopee()], "companies_filter.keep": ["Bangkok"]}
        polite, _transport = fetcher({
            "offset=0": (200, sea_page([sea_job("J1")], 2), {}),
            "offset=1": (200, "{", {}),
        }, overrides)
        jobs, reports = companies.collect(cfg(**overrides), memory_conn(self), polite,
                                          log=lambda *a, **k: None)
        self.assertEqual([job["source_job_id"] for job in jobs], ["J1"])
        self.assertEqual(reports[0]["status"], "partial")
        self.assertIn("malformed JSON", reports[0]["error"])

    def test_a_bad_total_and_a_non_dict_item_keep_accepted_rows(self):
        mixed = {"code": 0, "data": {"total_count": 1, "job_list": [sea_job("J1"), "nope", 3]}}
        polite, _transport = fetcher({"offset=0": (200, mixed, {})})
        jobs = adapters.sea_ats(self._shopee(), polite)
        self.assertEqual([job["source_job_id"] for job in jobs], ["J1"])
        self.assertIsNone(jobs.partial)
        bad_total = sea_page([sea_job("J1")], 2)
        bad_total["data"]["total_count"] = "many"
        polite, transport = fetcher({"offset=0": (200, bad_total, {})})
        jobs = adapters.sea_ats({**self._shopee(), "max_pages": 3}, polite)
        self.assertEqual([job["source_job_id"] for job in jobs], ["J1"])
        self.assertIn("bad total_count", jobs.partial)
        self.assertEqual(len(transport.urls), 1)
        junk = {"code": 0, "data": {"total_count": 2, "job_list": ["nope"]}}
        polite, _transport = fetcher({
            "offset=0": (200, sea_page([sea_job("J1")], 2), {}),
            "offset=1": (200, junk, {}),
        })
        jobs = adapters.sea_ats(self._shopee(), polite)
        self.assertEqual([job["source_job_id"] for job in jobs], ["J1"])
        self.assertIn("non-dict", jobs.partial)

    def test_a_bad_first_page_is_isolated_from_the_next_company(self):
        conn = memory_conn(self)
        sea_body = json.loads((ROOT / "tests/fixtures/companies/json_watch_sea.json").read_text(encoding="utf-8"))
        overrides = {
            "companies": [
                {"name": "Shopee", "adapter": "sea_ats",
                 "url": "https://ats.workatsea.com/ats/api/v1/user/job/list/?limit=20&offset=0"},
                {"name": "Sea", "adapter": "sea_ats",
                 "url": "https://career.sea.com/api/user/job/list?external_entity_id=3&limit=20&offset=0&city_ids=31"},
            ],
            "companies_filter.keep": ["Thailand", "Bangkok"],
        }
        polite, _transport = fetcher({
            "workatsea.com": (200, "{", {}),
            "career.sea.com": (200, sea_body, {}),
        }, overrides)
        jobs, reports = companies.collect(cfg(**overrides), conn, polite, log=lambda *a, **k: None)
        self.assertEqual(reports[0]["status"], "error")
        self.assertEqual(reports[1]["status"], "ok")
        self.assertEqual(reports[1]["kept"], 1)
        self.assertEqual(jobs[0]["url"], "https://career.sea.com/position/J02116669")
        self.assertEqual(jobs[0]["company"], "Sea")


class FetchDetailMissing(unittest.TestCase):
    def test_a_short_page_is_marked_missing_and_not_stored_as_a_description(self):
        conn = memory_conn(self)
        job_id, _created = store.add_job(conn, {
            "source": "jobthai", "source_job_id": "1", "title": "Key Account",
            "company": "Example", "location": "Bangkok",
            "url": "https://www.jobthai.com/en/company/job/1",
        })
        polite, _transport = fetcher({
            "/company/job/1": (200, "<html><nav>Cookie Policy</nav></html>", {}),
        })
        report = fetch_jd.run(cfg(), conn, polite)
        row = store.get_job(conn, job_id)
        self.assertEqual(report["missing"], 1)
        self.assertIsNone(row["jd_text"])
        self.assertEqual(row["jd_missing"], 1)

    def test_supports_reads_a_sqlite_row_without_get(self):
        conn = memory_conn(self)
        store.add_job(conn, {
            "source": "jobsdb", "source_job_id": "95744952",
            "title": "Management Trainee", "company": "Example", "location": "Bangkok",
            "url": "https://th.jobsdb.com/job/95744952",
        })
        row = store.jd_candidates(conn, 5)[0]
        with self.assertRaises(AttributeError):
            row.get("source")
        self.assertTrue(fetch_jd.supports(row))
        page = """<script type="application/ld+json">
        {"@type":"JobPosting","title":"Management Trainee",
         "description":"A long enough trainee description for a commercial graduate role in Bangkok retail."}
        </script>"""
        polite, _transport = fetcher({"/job/95744952": (200, page, {})})
        report = fetch_jd.run(cfg(), conn, polite)
        self.assertEqual(report["fetched"], 1)

    def test_a_dict_date_does_not_abort_the_jd_run(self):
        description = ("This is a long enough key account description for a Bangkok "
                       "commercial role and a graduate hire.")
        html = "<script type=\"application/ld+json\">" + json.dumps({
            "@type": "JobPosting", "datePosted": {"@type": "Date"}, "description": description,
        }) + "</script>"
        conn = memory_conn(self)
        job_id, _created = store.add_job(conn, {
            "source": "jobthai", "source_job_id": "99", "title": "Key Account",
            "company": "Example", "location": "Bangkok",
            "url": "https://www.jobthai.com/en/company/job/99",
        })
        report = fetch_jd.run(cfg(), conn, fetcher({"/company/job/99": (200, html, {})})[0])
        self.assertEqual(report["fetched"], 1)
        self.assertEqual(report["skipped"], 0)
        self.assertIsNone(store.get_job(conn, job_id)["posted_at"])
        self.assertIn("key account", store.get_job(conn, job_id)["jd_text"].casefold())

    def test_an_old_detail_date_is_not_left_scorable(self):
        description = ("This role sells to key accounts across Bangkok retail chains and "
                       "needs a graduate who can run a book of business for a full year.")
        pages = {
            "jobthai": (
                "<script type=\"application/ld+json\">"
                "{\"@type\":\"JobPosting\",\"title\":\"Key Account\",\"datePosted\":\"2026-01-01\","
                f"\"description\":\"{description}\"}}</script>"
            ),
            "loreal": (
                "<script type=\"application/ld+json\">"
                "{\"@type\":\"JobPosting\",\"title\":\"Assistant\","
                "\"datePosted\":\"2026-01-01\","
                f"\"description\":\"<p>About the job. {description}</p>\"}}</script>"
            ),
        }
        cases = (
            ("jobthai", "90", "https://www.jobthai.com/en/company/job/90", "/company/job/90", pages["jobthai"]),
            ("company_site", "loreal:90",
             "https://careers.loreal.com/en/job/bangkok/assistant-e-commerce-manager/3456/90",
             "/en/job/bangkok/assistant-e-commerce-manager/3456/90", pages["loreal"]),
        )
        conn = memory_conn(self)
        routes = {}
        ids = []
        for source, source_id, url, needle, body in cases:
            job_id, _created = store.add_job(conn, {
                "source": source, "source_job_id": source_id, "title": "Key Account",
                "company": "Example", "location": "Bangkok", "url": url,
            })
            ids.append(job_id)
            routes[needle] = (200, body, {})
        fetch_jd.run(cfg(), conn, fetcher(routes)[0])
        for job_id in ids:
            row = store.get_job(conn, job_id)
            self.assertEqual(row["posted_at"], "2026-01-01")
            self.assertIn("key accounts", row["jd_text"])
            score = store.current_score(conn, job_id)
            self.assertEqual(score["status"], "prefiltered")
            self.assertTrue(score["prefilter_rule"].startswith("max_age_days"))
        unscored = {row["id"] for row in store.unscored_jobs(conn, 20)}
        self.assertTrue(unscored.isdisjoint(ids))


class ProfileDefault(unittest.TestCase):
    def test_jobthai_stays_off_unless_the_test_opts_in(self):
        self.assertTrue(CFG.profile["sources"]["jobthai"])
        copied = profile()
        self.assertFalse(copied["sources"]["jobthai"])
        self.assertTrue(profile(**{"sources.jobthai": True})["sources"]["jobthai"])
        self.assertFalse(profile(**{"sources.jobthai": False})["sources"]["jobthai"])
        self.assertTrue(CFG.profile["sources"]["jobthai"])


if __name__ == "__main__":
    unittest.main()
