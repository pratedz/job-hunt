"""M3: the company career-site adapters, on the lead's real sample responses."""

import json
import re
import unittest
from dataclasses import replace

from _fake import FakeClock, FakeTransport
from _util import CFG, memory_conn, profile

from jobhunt import fetch_jd, pipeline, store
from jobhunt.http import PoliteFetcher
from jobhunt.sources import companies
from jobhunt.sources.companies import adapters

SAMPLES = "tests/fixtures/companies"


def sample(name):
    with open(f"{SAMPLES}/{name}", encoding="utf-8") as handle:
        return json.load(handle)


def sample_text(name):
    with open(f"{SAMPLES}/{name}", encoding="utf-8") as handle:
        return handle.read()


def config(**overrides):
    base = {"companies": [], "companies_filter": {"keep": ["Thailand", "Bangkok", "Singapore"]}}
    base.update(overrides)
    return replace(CFG, profile=profile(**base))


def env(routes, **overrides):
    cfg = config(**overrides)
    clock = FakeClock()
    transport = FakeTransport(routes)
    fetcher = PoliteFetcher(cfg.profile, transport=transport, sleep=clock.sleep, now=clock.now)
    return cfg, fetcher, transport


class Greenhouse(unittest.TestCase):
    def setUp(self):
        self.jobs = adapters.parse_greenhouse(sample("greenhouse_alphasights.json"), "AlphaSights")

    def test_every_posting_is_listed(self):
        payload = sample("greenhouse_alphasights.json")
        self.assertEqual(len(self.jobs), len(payload["jobs"]))

    def test_the_first_posting(self):
        first = self.jobs[0]
        self.assertEqual(first["source"], "greenhouse")
        self.assertEqual(first["company"], "AlphaSights")
        self.assertTrue(first["source_job_id"])
        self.assertTrue(first["url"].startswith("https://"))
        self.assertTrue(first["title"])

    def test_the_description_comes_from_the_list_call_when_present(self):
        # the captured sample was fetched without ?content=true, so it has none
        self.assertTrue(all(job["jd_text"] is None for job in self.jobs))
        with_content = {"jobs": [dict(sample("greenhouse_alphasights.json")["jobs"][0],
                                      content="&lt;p&gt;Rotational programme&lt;/p&gt;")]}
        parsed = adapters.parse_greenhouse(with_content, "AlphaSights")
        self.assertIn("Rotational programme", parsed[0]["jd_text"])

    def test_an_empty_payload(self):
        self.assertEqual(adapters.parse_greenhouse({"jobs": []}, "X"), [])


class Lever(unittest.TestCase):
    def setUp(self):
        self.jobs = adapters.parse_lever(sample("lever_lalamove.json"), "Lalamove")

    def test_postings(self):
        payload = sample("lever_lalamove.json")
        self.assertEqual(len(self.jobs), len(payload))

    def test_fields(self):
        first = self.jobs[0]
        self.assertEqual(first["source"], "lever")
        self.assertEqual(first["url"], sample("lever_lalamove.json")[0]["hostedUrl"])
        self.assertTrue(re.match(r"\d{4}-\d{2}-\d{2}$", first["posted_at"]))
        self.assertIn("Lalamove", first["jd_text"])

    def test_location_comes_from_categories(self):
        self.assertTrue(any(job["location"] for job in self.jobs))


class Workday(unittest.TestCase):
    def setUp(self):
        self.entry = {"name": "P&G", "adapter": "workday", "tenant": "pg", "wd_host": "wd3",
                      "site": "PG_External", "search_text": "Thailand"}
        self.jobs = adapters.parse_workday_list(sample("workday_pg_list.json"), self.entry)

    def test_postings(self):
        self.assertEqual(len(self.jobs), len(sample("workday_pg_list.json")["jobPostings"]))

    def test_url_is_built_from_the_external_path(self):
        first = self.jobs[0]
        self.assertTrue(first["url"].startswith("https://pg.wd3.myworkdayjobs.com/PG_External/job/"))
        self.assertEqual(first["source"], "workday")

    def test_relative_posted_text_becomes_a_date(self):
        from datetime import datetime, timezone
        now = datetime(2026, 11, 7, tzinfo=timezone.utc)
        self.assertEqual(adapters.parse_posted_on("Posted 3 Days Ago", now), "2026-11-04")
        self.assertEqual(adapters.parse_posted_on("Posted Today", now), "2026-11-07")
        self.assertEqual(adapters.parse_posted_on("Posted Yesterday", now), "2026-11-06")
        self.assertEqual(adapters.parse_posted_on("Posted 30+ Days Ago", now), "2026-10-08")
        self.assertIsNone(adapters.parse_posted_on("sometime", now))
        self.assertIsNone(adapters.parse_posted_on(None, now))

    def test_the_detail_endpoint_is_derived_from_the_posting_url(self):
        url = self.jobs[0]["url"]
        detail = adapters.workday_detail_url(url)
        self.assertIn("/wday/cxs/pg/PG_External/job/", detail)

    def test_the_detail_description_is_parsed(self):
        parsed = adapters.parse_workday_detail(sample("workday_pg_detail.json"))
        self.assertGreater(len(parsed["jd_text"]), 100)

    def test_the_list_is_paginated(self):
        cfg, fetcher, transport = env({"wday/cxs": (200, sample("workday_pg_list.json"), {})},
                                      companies=[self.entry])
        jobs = adapters.workday(self.entry, fetcher)
        self.assertTrue(jobs)
        calls = [call for call in transport.calls if "wday" in call["url"]]
        self.assertTrue(all(call["method"] == "POST" for call in calls))
        self.assertGreaterEqual(len(calls), 1)


class SmartRecruiters(unittest.TestCase):
    def test_the_list(self):
        jobs = adapters.parse_smartrecruiters_list(sample("smartrecruiters_minor_th.json"), "Minor")
        self.assertEqual(len(jobs), 3)
        self.assertTrue(jobs[0]["location"])
        self.assertEqual(jobs[0]["posted_at"], "2025-04-01")

    def test_the_detail(self):
        parsed = adapters.parse_smartrecruiters_detail(sample("smartrecruiters_minor_detail.json"))
        self.assertGreater(len(parsed["jd_text"]), 50)

    def test_the_jd_is_fetched_through_fetch_jd(self):
        conn = memory_conn(self)
        job_id, _ = store.add_job(conn, {
            "source": "smartrecruiters", "source_job_id": "744000051123232",
            "title": "Spa Reception", "company": "Minor International",
            "url": "https://jobs.smartrecruiters.com/MinorInternational/744000051232"})
        cfg = config(companies=[{"name": "Minor International", "adapter": "smartrecruiters",
                                 "smartrecruiters_id": "MinorInternational"}])
        _, fetcher, transport = env({"api.smartrecruiters.com": (
            200, sample("smartrecruiters_minor_detail.json"), {})}, companies=cfg.get("companies"))
        fetch_jd.run(cfg, conn, fetcher)
        self.assertIn("api.smartrecruiters.com", transport.urls[0])
        self.assertTrue(store.get_job(conn, job_id)["jd_text"])


class Sea(unittest.TestCase):
    def test_postings_and_descriptions(self):
        jobs = adapters.parse_sea(sample("json_watch_sea.json"), "Sea", "https://career.sea.com/position/{id}")
        self.assertEqual(len(jobs), len(sample("json_watch_sea.json")["data"]["job_list"]))
        self.assertEqual(jobs[0]["source"], "company_site")
        self.assertEqual(jobs[0]["location"], "Bangkok, Thailand")
        self.assertIn("IT support", jobs[0]["jd_text"])
        self.assertTrue(jobs[0]["source_job_id"])


class LocationFilter(unittest.TestCase):
    def test_keep_list(self):
        keep = ["Thailand", "Bangkok", "Singapore"]
        self.assertTrue(companies.keep_location("Bangkok, Thailand", keep))
        self.assertTrue(companies.keep_location("Singapore", keep))
        self.assertFalse(companies.keep_location("London, United Kingdom", keep))

    def test_unknown_location_is_kept(self):
        self.assertTrue(companies.keep_location(None, ["Thailand"]))

    def test_an_empty_keep_list_keeps_everything(self):
        self.assertTrue(companies.keep_location("London", []))


class Watch(unittest.TestCase):
    def setUp(self):
        self.conn = memory_conn(self)
        # the pattern profile.toml uses for CP Axtra: "/jobs?/" matches /job/ and /jobs/
        self.entry = {"name": "CP Axtra", "adapter": "watch",
                      "url": "https://www.cpaxtracareers.com/en/jobs", "link_pattern": "/jobs?/"}
        self.page = sample_text("watch_cpaxtra_jobs.html")

    def run_watch(self, page, stamp="2026-11-07T00:00:00Z"):
        cfg, fetcher, transport = env({"cpaxtra": (200, page, {})},
                                      companies=[self.entry])
        jobs, reports = companies.collect(cfg, self.conn, fetcher, now=stamp)
        return jobs, reports

    def test_the_first_run_only_records_a_baseline(self):
        jobs, reports = self.run_watch(self.page)
        self.assertEqual(jobs, [])
        self.assertEqual(len(store.site_changes(self.conn)), 0)
        self.assertIsNotNone(store.get_watch(self.conn, "CP Axtra"))
        self.assertGreater(reports[0]["postings"], 0)

    def test_a_token_only_change_is_not_a_change(self):
        self.run_watch(self.page)
        before = store.get_watch(self.conn, "CP Axtra")["hash"]
        mutated = re.sub(r"csrf[_-]?token=[A-Za-z0-9]+", "csrf-token=DIFFERENTTOKEN", self.page)
        if mutated == self.page:      # no token in the sample: inject one
            mutated = self.page.replace("<body", '<body data-token="abc123"', 1)
        self.run_watch(mutated, stamp="2026-11-07T01:00:00Z")
        after = store.get_watch(self.conn, "CP Axtra")["hash"]
        self.assertEqual(before, after)
        self.assertEqual(len(store.site_changes(self.conn)), 0)

    def test_a_new_link_raises_one_board_item(self):
        self.run_watch(self.page)
        mutated = self.page.replace(
            "</body>", '<a href="https://www.cpaxtracareers.com/en/jobs/future-leader-99">Future Leader 99</a></body>')
        jobs, reports = self.run_watch(mutated, stamp="2026-11-07T02:00:00Z")
        self.assertTrue(reports[0]["changed"])
        items = store.site_changes(self.conn)
        self.assertEqual(len(items), 1)
        links = json.loads(items[0]["new_links_json"])
        self.assertEqual([link["text"] for link in links], ["Future Leader 99"])
        # a second identical run adds nothing
        self.run_watch(mutated, stamp="2026-11-07T03:00:00Z")
        self.assertEqual(len(store.site_changes(self.conn)), 1)


class JsonWatch(unittest.TestCase):
    def setUp(self):
        self.conn = memory_conn(self)
        self.entry = {"name": "Marriott", "adapter": "json_watch",
                      "url": "https://careers.marriott.com/api/search",
                      "list_path": "items.0.requisitionList", "id_key": "Id", "title_key": "Title"}
        self.payload = json.dumps(sample("json_watch_marriott.json"))

    def test_items_are_extracted(self):
        items = adapters.json_watch_items(sample("json_watch_marriott.json"), self.entry)
        self.assertTrue(items)
        self.assertTrue(all(item["text"] for item in items))

    def test_a_baseline_then_a_change(self):
        cfg, fetcher, transport = env({"marriott": (200, self.payload, {})},
                                      companies=[self.entry])
        companies.collect(cfg, self.conn, fetcher, now="2026-11-07T00:00:00Z")
        self.assertEqual(len(store.site_changes(self.conn)), 0)

        payload = sample("json_watch_marriott.json")
        payload["items"][0]["requisitionList"].append({"Id": "NEW-1", "Title": "A brand new role"})
        cfg, fetcher, _ = env({"marriott": (200, json.dumps(payload), {})},
                              companies=[self.entry])
        _, reports = companies.collect(cfg, self.conn, fetcher, now="2026-11-07T01:00:00Z")
        self.assertTrue(reports[0]["changed"])
        links = json.loads(store.site_changes(self.conn)[0]["new_links_json"])
        self.assertEqual([link["text"] for link in links], ["A brand new role"])


class Isolation(unittest.TestCase):
    def test_one_failing_company_does_not_stop_the_others(self):
        conn = memory_conn(self)
        cfg, fetcher, transport = env({
            "boards/broken": (500, "boom", {}),
            "boards-api.greenhouse.io": (200, sample("greenhouse_alphasights.json"), {}),
        }, companies=[
            {"name": "Broken", "adapter": "greenhouse", "board": "broken"},
            {"name": "AlphaSights", "adapter": "greenhouse", "board": "alphasights"},
            {"name": "Ignored", "adapter": "none"},
        ], companies_filter={"keep": ["Hong Kong", "Thailand", "Singapore"]})
        jobs, reports = companies.collect(cfg, conn, fetcher)
        by_name = {report["company"]: report for report in reports}
        self.assertEqual(by_name["Broken"]["status"], "error")
        self.assertTrue(by_name["Broken"]["error"])
        self.assertEqual(by_name["AlphaSights"]["status"], "ok")
        self.assertTrue(jobs)
        self.assertEqual(by_name["Ignored"]["status"], "skipped")

    def test_only_narrows_to_one_company(self):
        conn = memory_conn(self)
        cfg, fetcher, transport = env({"boards-api.greenhouse.io": (200, sample("greenhouse_alphasights.json"), {})},
                                      companies=[{"name": "AlphaSights", "adapter": "greenhouse",
                                                  "board": "alphasights"},
                                                 {"name": "Other", "adapter": "greenhouse",
                                                  "board": "other"}])
        _, reports = companies.collect(cfg, conn, fetcher, only="alpha")
        self.assertEqual([report["company"] for report in reports], ["AlphaSights"])


class ThroughThePipeline(unittest.TestCase):
    def test_company_jobs_are_stored_and_pre_filtered(self):
        conn = memory_conn(self)
        cfg, fetcher, transport = env({"boards-api.greenhouse.io": (
            200, sample("greenhouse_alphasights.json"), {})},
            companies=[{"name": "AlphaSights", "adapter": "greenhouse", "board": "alphasights"}],
            companies_filter={"keep": ["Hong Kong", "Thailand", "Singapore"]},
            **{"prefilter.posted_within_days": 3650,
               "prefilter.posted_within_days_first_run": 3650})
        report = pipeline.discover(cfg, conn, fetcher, ["companies"], fetch_descriptions=False)
        self.assertTrue(report["companies"])
        self.assertGreater(report["sources"]["companies"]["found"], 0)
        self.assertGreater(store.count_jobs(conn), 0)
        # the location filter ran: the sample has a London/Tokyo posting too
        self.assertGreater(report["companies"][0]["postings"],
                           report["companies"][0]["kept"])


if __name__ == "__main__":
    unittest.main()


class EmptyBaseline(unittest.TestCase):
    """A page with no links must not become a baseline, or the next run raises falsely."""

    def setUp(self):
        self.conn = memory_conn(self)
        self.entry = {"name": "True", "adapter": "watch",
                      "url": "https://careers.true.th/sitemap.xml", "link_pattern": "/job/"}

    def run_watch(self, page, stamp):
        cfg, fetcher, transport = env({"careers.true.th": (200, page, {})},
                                      companies=[self.entry])
        jobs, reports = companies.collect(cfg, self.conn, fetcher, now=stamp)
        return jobs, reports

    def test_an_empty_page_records_no_baseline(self):
        jobs, reports = self.run_watch("<html><body>no jobs</body></html>", "2026-11-08T00:00:00Z")
        self.assertEqual(reports[0]["status"], "no links")
        self.assertIsNone(store.get_watch(self.conn, "True"))
        self.assertEqual(store.site_changes(self.conn), [])

    def test_a_legacy_empty_baseline_does_not_raise_a_change(self):
        store.set_watch(self.conn, "True", self.entry["url"], "oldhash", [], "2026-11-01T00:00:00Z")
        page = ('<?xml version="1.0"?><urlset><url><loc>https://careers.true.th/jobs/16670</loc>'
                '</url><url><loc>https://careers.true.th/jobs/16671</loc></url></urlset>')
        jobs, reports = self.run_watch(page, "2026-11-08T00:00:00Z")
        self.assertFalse(reports[0]["changed"])
        self.assertEqual(store.site_changes(self.conn), [])
        self.assertTrue(store.get_watch(self.conn, "True")["links_json"] != "[]")
