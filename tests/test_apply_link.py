"""M10: Apply goes to the real application page; no model call, no cover email."""

import json
import tempfile
import threading
import unittest
import urllib.request
from dataclasses import replace
from pathlib import Path

from _fake import fixture
from _util import CFG, memory_conn
from _vault import TODAY, make_vault, seeded_db

from jobhunt import apply_link, board_api, board_server, fetch_jd, score as score_mod, store
from jobhunt.sources import linkedin
from jobhunt.tracker import Tracker

EMPLOYER = "https://careers.acme.test/jobs/42?gh_jid=42"
OFFSITE_PAGE = f"""<html><body>
<code id="applyUrl" style="display: none"><!--"https://www.linkedin.com/jobs/view/externalApply/4486971271?url=https%3A%2F%2Fcareers%2Eacme%2Etest%2Fjobs%2F42%3Fgh_jid%3D42%26utm_source%3Dlinkedin&amp;urlHash=x"--></code>
<button class="sign-up-modal__outlet"><icon data-svg-class-name="apply-button__offsite-apply-icon-svg"></icon></button>
<div class="show-more-less-html__markup">A description.</div></body></html>"""


def row(source, url, source_job_id=None, company="Acme", title="Trainee", apply_url=None):
    return {"source": source, "url": url, "source_job_id": source_job_id, "company": company,
            "title": title, "apply_url": apply_url}


class Destinations(unittest.TestCase):
    def test_each_source_goes_to_its_application_page(self):
        cases = [
            (row("linkedin", "https://www.linkedin.com/jobs/view/4486971271", "4486971271"),
             ("https://www.linkedin.com/jobs/view/4486971271/", "linkedin")),
            (row("jobsdb", "https://th.jobsdb.com/job/95340875", "95340875"),
             ("https://th.jobsdb.com/job/95340875", "jobsdb")),
            (row("greenhouse", "https://job-boards.greenhouse.io/agoda/jobs/8358261"),
             ("https://job-boards.greenhouse.io/agoda/jobs/8358261", "company_site")),
            (row("lever", "https://jobs.lever.co/lalamove/007c4e8a"),
             ("https://jobs.lever.co/lalamove/007c4e8a/apply", "company_site")),
            (row("workday", "https://unilever.wd3.myworkdayjobs.com/U/job/X/SHE_R-1"),
             ("https://unilever.wd3.myworkdayjobs.com/U/job/X/SHE_R-1", "company_site")),
            (row("smartrecruiters", "https://jobs.smartrecruiters.com/Grab/744000125251544"),
             ("https://jobs.smartrecruiters.com/Grab/744000125251544", "company_site")),
            (row("company_site", "https://career.sea.com/position/J00022044"),
             ("https://career.sea.com/position/J00022044", "company_site")),
            (row("manual", "https://th.jobsdb.com/job/95212842"),
             ("https://th.jobsdb.com/job/95212842", "jobsdb")),
            (row("manual", "https://www.linkedin.com/jobs/view/1/?trk=abc"),
             ("https://www.linkedin.com/jobs/view/1/", "linkedin")),
            (row("manual", "https://boards.greenhouse.io/acme/jobs/1"),
             ("https://boards.greenhouse.io/acme/jobs/1", "company_site")),
            (row("manual", "https://acme.test/careers/1"), ("https://acme.test/careers/1", "other")),
            (row("manual", None), (None, "other")),
        ]
        for job, expected in cases:
            self.assertEqual(apply_link.destination(job), expected, job)

    def test_lever_apply_is_not_doubled(self):
        self.assertEqual(apply_link.own_page("lever", "https://jobs.lever.co/x/1/apply"),
                         "https://jobs.lever.co/x/1/apply")

    def test_only_http_urls(self):
        for url in ("javascript:alert(1)", "file:///etc/passwd", "data:text/html,x", "/relative"):
            self.assertEqual(apply_link.destination(row("manual", url)), (None, "other"), url)

    def test_tracking_parameters_are_stripped_and_needed_ones_kept(self):
        self.assertEqual(apply_link.strip_tracking(
            "https://boards.greenhouse.io/acme/jobs/1?gh_jid=1&utm_source=li&trk=x&refId=9#top"),
            "https://boards.greenhouse.io/acme/jobs/1?gh_jid=1")


class LinkedInOffsite(unittest.TestCase):
    def test_the_guest_sample_is_offsite_but_names_no_employer(self):
        """As checked live on 8 Nov 2026: guests see *that*, not *where*."""
        parsed = linkedin.parse_posting(fixture("linkedin_guest_jobposting.html"))
        self.assertTrue(parsed["apply_offsite"])
        self.assertIsNone(parsed["apply_url"])

    def test_an_apply_url_code_block_is_unwrapped_to_the_employer(self):
        parsed = linkedin.parse_posting(OFFSITE_PAGE)
        self.assertTrue(parsed["apply_offsite"])
        self.assertEqual(parsed["apply_url"], EMPLOYER)          # utm dropped, gh_jid kept

    def test_an_external_apply_href_is_unwrapped(self):
        page = ('<a href="https://www.linkedin.com/jobs/view/externalApply/1?url='
                'https%3A%2F%2Fjobs.acme.test%2Fapply%2F7&amp;trk=x">Apply</a>')
        self.assertEqual(apply_link.linkedin_offsite(page)["apply_url"],
                         "https://jobs.acme.test/apply/7")

    def test_a_linkedin_redirect_without_an_employer_is_not_offsite(self):
        self.assertIsNone(apply_link.unwrap("https://www.linkedin.com/jobs/view/externalApply/1"))
        self.assertIsNone(apply_link.unwrap(
            "https://www.linkedin.com/x?url=https%3A%2F%2Fwww.linkedin.com%2Flogin"))

    def test_the_jd_fetch_stores_the_captured_link(self):
        from _fake import FakeTransport
        from jobhunt.http import PoliteFetcher
        from _util import profile
        conn = memory_conn(self)
        job_id, _ = store.add_job(conn, {"source": "linkedin", "source_job_id": "4486971271",
                                         "url": "https://www.linkedin.com/jobs/view/4486971271",
                                         "title": "Trainee", "company": "Acme"})
        transport = FakeTransport({"jobPosting/4486971271": (200, OFFSITE_PAGE, {})})
        fetcher = PoliteFetcher(profile(**{"limits.min_seconds_between_requests": 0}),
                                transport=transport, sleep=lambda s: None, now=lambda: 0.0)
        fetch_jd.run(CFG, conn, fetcher)
        stored = store.board_job(conn, job_id)
        self.assertEqual(stored["apply_url"], EMPLOYER)
        self.assertEqual(apply_link.destination(stored), (EMPLOYER, "company_site"))


class CrossSource(unittest.TestCase):
    def test_a_linkedin_job_found_on_the_company_site_goes_there(self):
        conn = memory_conn(self)
        store.add_job(conn, {"source": "greenhouse", "source_job_id": "8358261",
                             "url": "https://job-boards.greenhouse.io/agoda/jobs/8358261",
                             "company": "Agoda", "title": "Graduate Programme 2027"})
        li, _ = store.add_job(conn, {"source": "linkedin", "source_job_id": "1",
                                     "url": "https://www.linkedin.com/jobs/view/1",
                                     "company": "Agoda (Thailand) Co., Ltd.",
                                     "title": "Graduate Programme 2027"})
        other, _ = store.add_job(conn, {"source": "linkedin", "source_job_id": "2",
                                        "url": "https://www.linkedin.com/jobs/view/2",
                                        "company": "Agoda", "title": "Data Analyst"})
        pages = apply_link.CompanySitePages(conn, [{"name": "Agoda", "domain": "agoda.com"}])
        self.assertEqual(apply_link.destination(store.board_job(conn, li), pages),
                         ("https://job-boards.greenhouse.io/agoda/jobs/8358261", "company_site"))
        self.assertEqual(apply_link.destination(store.board_job(conn, other), pages)[1], "linkedin")
        # whole tokens only, as for logos: "Agoda Services" is not "Agoda"
        services, _ = store.add_job(conn, {"source": "linkedin", "source_job_id": "3",
                                           "url": "https://www.linkedin.com/jobs/view/3",
                                           "company": "Agoda Services",
                                           "title": "Graduate Programme 2027"})
        self.assertEqual(apply_link.destination(store.board_job(conn, services), pages)[1],
                         "linkedin")


class ApplyEndpoint(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = replace(CFG, data_root=Path(self.tmp.name))
        self.vault = make_vault(self.tmp.name)
        self.db = Path(self.tmp.name) / "board.sqlite"
        self.conn, self.ids = seeded_db(self, path=self.db)
        self.job_id, _ = store.add_job(self.conn, {
            "source": "lever", "source_job_id": "abc", "company": "Lalamove",
            "title": "Management Trainee", "url": "https://jobs.lever.co/lalamove/abc",
            "jd_text": "A programme."})
        store.insert_score(self.conn, self.job_id, {
            "status": "scored", "verdict": "CENTRE", "gate_result": "pass", "love": 4,
            "good_at": 4, "needs": 4, "paid": 4, "jd_used": 1, "facts": {},
            "weakest_circle": "love", "weakest_score": 4, "mean_score": 4.0,
            "created_at": store.utc_now(), "scored_at": store.utc_now()})

    def test_rows_and_details_carry_the_destination(self):
        tracker = Tracker(self.vault, today=lambda: TODAY)
        row = next(r for r in board_api.list_jobs(self.conn, tracker)["jobs"]
                   if r["id"] == self.job_id)
        self.assertEqual((row["apply_url"], row["apply_via"]),
                         ("https://jobs.lever.co/lalamove/abc/apply", "company_site"))
        detail = board_api.get_job(self.conn, tracker, self.job_id)
        self.assertEqual(detail["apply_url"], row["apply_url"])

    def test_apply_over_http_makes_no_model_call_and_writes_the_record(self):
        def no_model():
            raise AssertionError("Apply asked for a model")
        server = board_server.create_server(self.cfg, db_path=self.db, vault_path=self.vault.root,
                                            port=0, llm_factory=no_model,
                                            fetcher_factory=lambda: None)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_address[1]}/api/jobs/{self.job_id}/apply",
            data=b"{}", method="POST",
            headers={"X-Requested-With": "jobhunt", "Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=10) as response:
            body = json.loads(response.read())
        self.assertEqual(set(body), {"ok", "created", "already_tracked", "record_file",
                                     "obsidian_url", "apply_url", "apply_via", "cover_email"})
        self.assertEqual(body["apply_url"], "https://jobs.lever.co/lalamove/abc/apply")
        self.assertEqual(body["apply_via"], "company_site")
        self.assertIsNone(body["cover_email"])
        record = Tracker(self.vault, today=lambda: TODAY).vault.record(body["record_file"])
        self.assertEqual(record.get("apply_link"), "https://jobs.lever.co/lalamove/abc/apply")
        self.assertEqual(record.get("link"), "https://jobs.lever.co/lalamove/abc")
        self.assertEqual(record.get("job_platform"), "Company career site")
        self.assertEqual(record.get("next_step"),
                         "Apply via the company site: https://jobs.lever.co/lalamove/abc/apply. "
                         "Then press Mark submitted.")
        self.assertEqual(list((self.cfg.data_dir / "drafts").glob("*"))
                         if (self.cfg.data_dir / "drafts").exists() else [], [])

    def test_an_already_tracked_job_still_returns_its_page(self):
        tracker = Tracker(self.vault, today=lambda: TODAY)
        board_api.apply_for_job(self.conn, tracker, self.job_id, today=TODAY)
        again = board_api.apply_for_job(self.conn, tracker, self.job_id, today=TODAY)
        self.assertTrue(again["already_tracked"])
        self.assertEqual(again["apply_url"], "https://jobs.lever.co/lalamove/abc/apply")

    def test_linkedin_and_jobsdb_records(self):
        tracker = Tracker(self.vault, today=lambda: TODAY)
        for source, url, job_ref, platform, where in (
                ("linkedin", "https://www.linkedin.com/jobs/view/77", "77", "LinkedIn", "LinkedIn"),
                ("jobsdb", "https://th.jobsdb.com/job/88", "88", "JobsDB", "JobsDB")):
            job_id, _ = store.add_job(self.conn, {"source": source, "source_job_id": job_ref,
                                                  "url": url, "company": f"Co {source}",
                                                  "title": "Trainee", "jd_text": "x"})
            response = board_api.apply_for_job(self.conn, tracker, job_id, today=TODAY)
            record = tracker.vault.record(response["record_file"])
            self.assertEqual(record.get("job_platform"), platform)
            self.assertTrue(record.get("next_step").startswith(f"Apply via {where}: "))
            self.assertEqual(record.get("apply_link"), response["apply_url"])


class ApplicationRowApplyLink(unittest.TestCase):
    """§M10 clarification: apply_link = the record's apply_link, else link, else null."""

    def test_apply_link_link_and_null(self):
        tmp = tempfile.mkdtemp()
        vault = make_vault(tmp)
        tracker = Tracker(vault, today=lambda: TODAY)
        cases = {"Both": ("https://apply.test/1", "https://post.test/1", "https://apply.test/1"),
                 "LinkOnly": ("", "https://post.test/2", "https://post.test/2"),
                 "Neither": ("", "", None)}
        for company, (apply_link, link, _expected) in cases.items():
            record = vault.new_record({
                "title": f"{company} — Role", "type": "record", "company": company,
                "program": "Role", "my_status": "To apply", "section": "Apply now",
                "do_by": "2026-11-20", "apply_link": apply_link, "link": link})
            vault.write_record(record)
        tracker.reload()
        rows = {row["company"]: row for row in
                board_api.applications(tracker, "to_apply")["applications"]}
        for company, (_a, _l, expected) in cases.items():
            self.assertEqual(rows[company]["apply_link"], expected, company)
        # every row carries the key, the existing records included
        self.assertTrue(all("apply_link" in row for row in rows.values()))

    def test_apply_writes_a_record_whose_row_points_at_the_apply_page(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        vault = make_vault(tmp.name)
        tracker = Tracker(vault, today=lambda: TODAY)
        conn = memory_conn(self)
        job_id, _ = store.add_job(conn, {"source": "lever", "source_job_id": "x",
                                         "company": "Lalamove", "title": "Trainee",
                                         "url": "https://jobs.lever.co/lalamove/x", "jd_text": "y"})
        board_api.apply_for_job(conn, tracker, job_id, today=TODAY)
        row = next(r for r in board_api.applications(tracker, "to_apply")["applications"]
                   if r["company"] == "Lalamove")
        self.assertEqual(row["apply_link"], "https://jobs.lever.co/lalamove/x/apply")
        self.assertEqual(row["link"], "https://jobs.lever.co/lalamove/x")


if __name__ == "__main__":
    unittest.main()
