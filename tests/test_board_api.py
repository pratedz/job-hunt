import tempfile
import unittest
from datetime import date

from _fake import FakeTransport, fixture
from _util import CFG
from _vault import TODAY, make_vault, record_text, scored_reply, seeded_db

from jobhunt import board_api, store
from jobhunt.board_api import ApiError
from jobhunt.http import PoliteFetcher
from jobhunt.llm import FakeLLM, LLMError
from jobhunt.tracker import Tracker


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn, self.ids = seeded_db(self)
        self.vault = make_vault(self.tmp.name)
        self.tracker = Tracker(self.vault, today=lambda: TODAY)

    def jobs(self, tab="all", **kw):
        return board_api.list_jobs(self.conn, self.tracker, tab=tab, **kw)["jobs"]

    def ids_in(self, tab):
        return [job["id"] for job in self.jobs(tab)]


class Tabs(Base):
    def test_counts(self):
        tabs = board_api.stats(self.conn, self.tracker)["tabs"]
        self.assertEqual(tabs["all"], 5)          # CENTRE + HOLD + LOW only
        self.assertEqual(tabs["centre"], 2)
        self.assertEqual(tabs["hold"], 1)
        self.assertEqual(tabs["low"], 2)
        self.assertEqual(tabs["filtered"], 2)
        self.assertEqual(tabs["skipped"], 1)
        self.assertEqual(tabs["to_apply"], 1)      # the vault record, not a job
        self.assertEqual(tabs["applied"], 1)

    def test_each_tab_holds_the_right_jobs(self):
        self.assertEqual(set(self.ids_in("centre")), {self.ids["centre"], self.ids["manual"]})
        self.assertEqual(self.ids_in("hold"), [self.ids["hold"]])
        self.assertEqual(set(self.ids_in("low")), {self.ids["low"], self.ids["snippet"]})
        self.assertEqual(set(self.ids_in("filtered")),
                         {self.ids["filtered"], self.ids["prefiltered"]})
        self.assertEqual(self.ids_in("skipped"), [self.ids["skipped"]])

    def test_all_holds_only_the_live_verdicts(self):
        everything = set(self.ids_in("all"))
        self.assertEqual(everything, {self.ids["centre"], self.ids["hold"], self.ids["low"],
                                      self.ids["snippet"], self.ids["manual"]})
        for name in ("filtered", "prefiltered", "skipped", "unscored", "errored", "stale"):
            self.assertNotIn(self.ids[name], everything, name)

    def test_rank_follows_the_m1_order(self):
        rows = self.jobs("all")
        verdicts = [row["verdict"] for row in rows if row["verdict"]]
        self.assertEqual(verdicts, ["CENTRE", "CENTRE", "HOLD", "LOW", "LOW"])
        self.assertEqual([row["rank"] for row in rows if row["verdict"]], [1, 2, 3, 4, 5])
        self.assertTrue(all(row["rank"] is None for row in rows if row["verdict"] is None))

    def test_search_filters_on_company_or_title(self):
        rows = self.jobs("all", q="account")
        self.assertEqual([row["title"] for row in rows], ["Key Account Executive"])

    def test_an_unknown_tab_is_a_400(self):
        with self.assertRaises(ApiError) as ctx:
            board_api.list_jobs(self.conn, self.tracker, tab="everything")
        self.assertEqual(ctx.exception.status, 400)

    def test_paging(self):
        page = board_api.list_jobs(self.conn, self.tracker, tab="all", limit=3, offset=0)
        self.assertEqual(page["total"], 5)
        self.assertEqual(len(page["jobs"]), 3)


class StaleScores(Base):
    """A score made before the JD arrived is presented as waiting, not as a verdict."""

    def test_it_is_not_in_a_verdict_tab(self):
        self.assertNotIn(self.ids["stale"], self.ids_in("centre"))
        self.assertNotIn(self.ids["stale"], self.ids_in("low"))

    def test_it_is_in_no_tab_at_all(self):
        for tab in ("all", "centre", "hold", "low", "filtered", "skipped"):
            self.assertNotIn(self.ids["stale"], self.ids_in(tab), tab)

    def test_its_row_still_reports_no_verdict(self):
        detail = board_api.get_job(self.conn, self.tracker, self.ids["stale"])
        self.assertIsNone(detail["verdict"])
        self.assertEqual(detail["scores"], {"love": None, "good_at": None, "needs": None, "paid": None})

    def test_it_counts_as_waiting_to_be_scored(self):
        stats = board_api.stats(self.conn, self.tracker)
        self.assertEqual(stats["unscored"], 3)     # unscored + errored + stale
        self.assertEqual(stats["shortfall"], {"love": 1, "good_at": 1, "needs": 0, "paid": 0})

    def test_a_snippet_score_stays_visible(self):
        row = next(job for job in self.jobs("all") if job["id"] == self.ids["snippet"])
        self.assertEqual(row["verdict"], "LOW")
        self.assertEqual(row["scores"]["good_at"], 2)

    def test_a_pasted_job_stays_visible(self):
        row = next(job for job in self.jobs("all") if job["id"] == self.ids["manual"])
        self.assertEqual(row["verdict"], "CENTRE")

    def test_more_known_circles_rank_higher_inside_a_group(self):
        # three known circles must beat one known circle, whatever the score values
        first = store.add_job(self.conn, {
            "source": "manual", "title": "Hold with three", "company": "Acme",
            "url": "https://example.com/jobs/h3", "jd_text": "d"})[0]
        store.insert_score(self.conn, first, {
            "status": "scored", "verdict": "HOLD", "gate_result": "pass",
            "love": 4, "good_at": 4, "needs": 4, "paid": None, "jd_used": 1,
            "weakest_circle": "love", "weakest_score": 4, "mean_score": 4.0,
            "created_at": store.utc_now(), "scored_at": store.utc_now()})
        second = store.add_job(self.conn, {
            "source": "manual", "title": "Hold with five", "company": "Acme",
            "url": "https://example.com/jobs/h1", "jd_text": "d"})[0]
        store.insert_score(self.conn, second, {
            "status": "scored", "verdict": "HOLD", "gate_result": "pass",
            "love": 5, "good_at": None, "needs": None, "paid": None, "jd_used": 1,
            "weakest_circle": "love", "weakest_score": 5, "mean_score": 5.0,
            "created_at": store.utc_now(), "scored_at": store.utc_now()})
        order = [job["id"] for job in self.jobs("hold")]
        self.assertLess(order.index(first), order.index(second))

    def test_its_detail_shows_no_reasons(self):
        detail = board_api.get_job(self.conn, self.tracker, self.ids["stale"])
        self.assertIsNone(detail["verdict"])
        self.assertEqual(detail["reasons"], {})
        self.assertIsNone(detail["summary"])


class Detail(Base):
    def test_it_carries_reasons_facts_and_salary(self):
        detail = board_api.get_job(self.conn, self.tracker, self.ids["centre"])
        self.assertEqual(detail["reasons"]["love"], "love reason")
        self.assertEqual(detail["summary"], "A short summary.")
        self.assertEqual(detail["facts"]["job_type"], "graduate_programme")
        self.assertEqual(detail["salary"]["thb_month_mid"], 40000.0)
        self.assertFalse(detail["salary"]["stated"])
        self.assertIn("long description", detail["jd_text"])
        self.assertFalse(detail["jd_missing"])

    def test_a_prefiltered_job_shows_its_rule(self):
        detail = board_api.get_job(self.conn, self.tracker, self.ids["prefiltered"])
        self.assertEqual(detail["prefilter_reason"], "title_exclude: \\bsenior\\b")
        self.assertEqual(detail["gate"]["result"], None)

    def test_an_errored_job_shows_the_error(self):
        detail = board_api.get_job(self.conn, self.tracker, self.ids["errored"])
        self.assertIn("codex timeout", detail["error"])
        self.assertIsNone(detail["verdict"])

    def test_a_missing_job_is_a_404(self):
        with self.assertRaises(ApiError) as ctx:
            board_api.get_job(self.conn, self.tracker, 999)
        self.assertEqual(ctx.exception.status, 404)

    def test_row_and_detail_keys_match_the_contract(self):
        row = self.jobs("centre")[0]
        self.assertEqual(set(row), {
            "id", "rank", "company", "title", "location", "source", "url", "posted_at",
            "first_seen_at", "verdict", "status", "scores", "paid_estimated", "weakest",
            "mean", "gate", "prefilter_reason", "tracked", "error", "summary",
            "logo_url", "country", "apply_url", "apply_via"})
        detail = board_api.get_job(self.conn, self.tracker, row["id"])
        self.assertEqual(set(detail) - set(row),
                         {"reasons", "facts", "salary", "jd_text", "jd_missing",
                          "email_snippet"})

    def test_every_row_carries_the_summary_line(self):
        for row in self.jobs("all"):
            self.assertIn("summary", row)
            self.assertEqual(row["summary"], "A short summary.")

    def test_a_job_waiting_to_be_scored_has_no_summary(self):
        detail = board_api.get_job(self.conn, self.tracker, self.ids["stale"])
        self.assertIsNone(detail["summary"])
        self.assertNotIn(self.ids["stale"], self.ids_in("all"))


class SkipAndApply(Base):
    def test_skip_persists_and_moves_the_job(self):
        response = board_api.skip(self.conn, self.tracker, self.ids["low"], "skip")
        self.assertEqual(response, {"ok": True, "status": "skipped"})
        self.assertIn(self.ids["low"], self.ids_in("skipped"))
        self.assertNotIn(self.ids["low"], self.ids_in("all"))
        # a restart reads the same decisions row
        again = Tracker(self.vault, today=lambda: TODAY)
        self.assertEqual(again.status_for(store.board_job(self.conn, self.ids["low"]), "skip"),
                         "skipped")

    def test_unskip_restores_it(self):
        board_api.skip(self.conn, self.tracker, self.ids["low"], "skip")
        response = board_api.skip(self.conn, self.tracker, self.ids["low"], "unskip")
        self.assertEqual(response, {"ok": True, "status": "new"})
        self.assertIn(self.ids["low"], self.ids_in("low"))

    def test_apply_creates_one_tracker_record(self):
        response = board_api.apply_for_job(self.conn, self.tracker, self.ids["low"], today=TODAY)
        self.assertTrue(response["created"])
        self.assertFalse(response["already_tracked"])
        self.assertIsNone(response["cover_email"])
        self.assertTrue(response["record_file"].endswith("Acme — Key Account Executive.md"))
        self.assertIn("obsidian://open?vault=Career-Wiki", response["obsidian_url"])
        self.assertEqual(len(self.vault.records()), 4)
        self.assertNotIn(self.ids["low"], self.ids_in("all"))          # now in the tracker
        self.assertEqual(board_api.stats(self.conn, self.tracker)["tabs"]["to_apply"], 2)

    def test_applying_to_a_tracked_job_creates_nothing(self):
        job_id, _ = store.add_job(self.conn, {
            "source": "linkedin", "title": "Already tracked", "company": "Acme",
            "url": "https://example.com/jobs/1", "jd_text": "A description."})
        response = board_api.apply_for_job(self.conn, self.tracker, job_id, today=TODAY)
        self.assertFalse(response["created"])
        self.assertTrue(response["already_tracked"])
        self.assertEqual(len(self.vault.records()), 3)

    def test_submitted_updates_the_record(self):
        response = board_api.apply_for_job(self.conn, self.tracker, self.ids["low"], today=TODAY)
        done = board_api.mark_submitted(self.tracker, response["record_file"], today=TODAY)
        self.assertTrue(done["ok"])
        record = self.vault.record(response["record_file"])
        self.assertEqual(record.get("my_status"), "Applied")
        self.assertEqual(record.get("date_applied"), "2026-11-07")
        applied = board_api.applications(self.tracker, "applied")["applications"]
        self.assertIn("Acme", [row["company"] for row in applied])

    def test_submitted_needs_a_record_file(self):
        with self.assertRaises(ApiError) as ctx:
            board_api.mark_submitted(self.tracker, "")
        self.assertEqual(ctx.exception.status, 400)

    def test_submitted_on_an_unknown_record_is_a_404(self):
        with self.assertRaises(ApiError) as ctx:
            board_api.mark_submitted(self.tracker, "61 Application Records/Nope.md")
        self.assertEqual(ctx.exception.status, 404)


class Paste(Base):
    def setUp(self):
        super().setUp()
        self.transport = FakeTransport({
            "th.jobsdb.com/job/": (200, fixture("jobsdb_job_95340875.html"), {}),
            "linkedin.com": (404, "", {}),
        })
        self.fetcher = PoliteFetcher(CFG.profile, transport=self.transport,
                                     sleep=lambda s: None, now=lambda: 0.0)
        self.llm = FakeLLM([lambda prompt, ids: scored_reply(ids)])

    def paste(self, **body):
        return board_api.paste(CFG, self.conn, self.tracker, body, self.llm, self.fetcher,
                               today=TODAY)

    def test_pasted_text_is_scored_immediately(self):
        response = self.paste(jd_text="A long description of a graduate programme.",
                              title="Pasted Trainee", company="Pasted Co")
        self.assertEqual(response["state"], "scored")
        job = store.board_job(self.conn, response["job_id"])
        self.assertEqual(job["source"], "manual")
        self.assertEqual(job["verdict"], "CENTRE")
        self.assertEqual(len(self.llm.calls), 1)

    def test_a_pasted_url_gets_its_description_fetched(self):
        response = self.paste(url="https://th.jobsdb.com/job/95340875", title="Deliveree MT",
                              company="Deliveree")
        self.assertEqual(response["state"], "scored")
        job = store.get_job(self.conn, response["job_id"])
        self.assertIn("Deliveree", job["jd_text"])
        self.assertEqual(job["jd_missing"], 0)

    def test_an_off_track_title_is_prefiltered_without_a_model_call(self):
        response = self.paste(jd_text="Senior role.", title="Senior Key Account Manager")
        self.assertEqual(response["state"], "prefiltered")
        self.assertEqual(self.llm.calls, [])
        job = store.board_job(self.conn, response["job_id"])
        self.assertEqual(job["verdict"], "FILTERED")

    def test_a_model_failure_comes_back_as_state_error(self):
        self.llm = FakeLLM([LLMError("timeout", "codex timed out after 300s")])
        response = self.paste(jd_text="A description.", title="Trainee")
        self.assertEqual(response["state"], "error")
        self.assertIn("timed out", response["error"])

    def test_an_unreadable_page_says_what_to_do_instead(self):
        with self.assertRaises(ApiError) as ctx:
            self.paste(url="https://www.linkedin.com/jobs/view/1")
        self.assertEqual(ctx.exception.status, 502)
        self.assertIn("Paste the JD text instead", ctx.exception.message)

    def test_nothing_given_is_a_400(self):
        with self.assertRaises(ApiError) as ctx:
            self.paste()
        self.assertEqual(ctx.exception.status, 400)


class SiteChanges(Base):
    def test_unseen_items_are_listed_and_can_be_marked_seen(self):
        change_id = store.add_site_change(
            self.conn, "CP Axtra", "https://www.cpaxtra.com/en/careers",
            [{"href": "https://www.cpaxtra.com/en/careers/flp", "text": "Future Leader"}],
            detected_at="2026-11-07T00:02:00Z")
        items = board_api.site_changes(self.conn)["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["id"], change_id)
        self.assertEqual(items[0]["new_links"][0]["text"], "Future Leader")
        self.assertEqual(board_api.stats(self.conn, self.tracker)["site_changes_unseen"], 1)

        self.assertEqual(board_api.mark_site_change_seen(self.conn, change_id), {"ok": True})
        self.assertEqual(board_api.site_changes(self.conn)["items"], [])
        self.assertEqual(board_api.stats(self.conn, self.tracker)["site_changes_unseen"], 0)

    def test_marking_an_unknown_item_seen_is_a_404(self):
        with self.assertRaises(ApiError) as ctx:
            board_api.mark_site_change_seen(self.conn, 999)
        self.assertEqual(ctx.exception.status, 404)


class Stats(Base):
    def test_last_run_comes_from_the_newest_runs_row(self):
        self.assertIsNone(board_api.stats(self.conn, self.tracker)["last_run"])
        store.record_run(self.conn, "run", {
            "sources": {"linkedin": {"found": 10, "new": 4, "prefiltered": 1},
                        "jobsdb": {"found": 5, "new": 2, "prefiltered": 0}},
            "score": {"by_source": {"linkedin": {"scored": 3, "errors": 1},
                                    "jobsdb": {"scored": 2, "errors": 0}}},
        })
        last = board_api.stats(self.conn, self.tracker)["last_run"]
        self.assertEqual(set(last), {"at", "sources"})
        self.assertEqual(last["sources"]["linkedin"],
                         {"found": 10, "new": 4, "prefiltered": 1, "jd_fetched": 0,
                          "scored": 3, "errors": 1})
        self.assertEqual(last["sources"]["jobsdb"]["scored"], 2)

    def test_last_run_keeps_each_source_with_its_own_latest_numbers(self):
        store.record_run(self.conn, "discover", {"sources": {
            "linkedin": {"found": 9, "new": 3, "prefiltered": 1}}})
        store.record_run(self.conn, "discover", {"sources": {
            "jobsdb": {"found": 5, "new": 2, "prefiltered": 0}}})
        last = board_api.stats(self.conn, self.tracker)["last_run"]
        self.assertEqual(set(last["sources"]), {"linkedin", "jobsdb"})
        self.assertEqual(last["sources"]["linkedin"]["found"], 9)
        # a newer jobsdb run replaces only jobsdb
        store.record_run(self.conn, "discover", {"sources": {
            "jobsdb": {"found": 7, "new": 1, "prefiltered": 0}}})
        last = board_api.stats(self.conn, self.tracker)["last_run"]
        self.assertEqual(last["sources"]["jobsdb"]["found"], 7)
        self.assertEqual(last["sources"]["linkedin"]["found"], 9)

    def test_a_fetch_jd_run_reports_its_fetched_count(self):
        store.record_run(self.conn, "fetch-jd", {
            "by_source": {"linkedin": {"fetched": 17, "missing": 1}}})
        last = board_api.stats(self.conn, self.tracker)["last_run"]
        self.assertEqual(last["sources"]["linkedin"]["jd_fetched"], 17)

    def test_filtered_reasons_are_counted_and_trimmed(self):
        stats = board_api.stats(self.conn, self.tracker)
        reasons = [item["reason"] for item in stats["filtered_top_reasons"]]
        self.assertIn("Requires a minimum GPA of 3.00", reasons)
        self.assertTrue(all(";" not in reason for reason in reasons))

    def test_applications_lists_are_separated(self):
        to_apply = board_api.applications(self.tracker, "to_apply")["applications"]
        applied = board_api.applications(self.tracker, "applied")["applications"]
        self.assertEqual([row["my_status"] for row in to_apply], ["To apply"])
        self.assertEqual([row["my_status"] for row in applied], ["Applied"])
        with self.assertRaises(ApiError):
            board_api.applications(self.tracker, "everything")


if __name__ == "__main__":
    unittest.main()
