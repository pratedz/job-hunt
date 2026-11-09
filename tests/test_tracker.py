import tempfile
import unittest
from datetime import date

from _vault import TODAY, make_vault, record_text, seeded_db

from jobhunt import store
from jobhunt.tracker import Tracker

RECORDS = "61 Application Records"


class Matching(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.vault = make_vault(self.tmp.name)
        self.tracker = Tracker(self.vault, today=lambda: TODAY)
        self.conn, self.ids = seeded_db(self)

    def job(self, job_id):
        return store.get_job(self.conn, job_id)

    def matched_job(self):
        """A job whose URL is the one the Acme record carries."""
        job_id, _ = store.add_job(self.conn, {
            "source": "linkedin", "title": "Already In The Vault", "company": "Acme",
            "url": "https://example.com/jobs/1", "jd_text": "A description."})
        return self.job(job_id)

    def test_matches_on_the_link_url(self):
        self.assertEqual(self.tracker.find_for_job(self.matched_job()).get("company"), "Acme")

    def test_matches_on_a_tracking_url_for_the_same_job(self):
        job_id, _ = store.add_job(self.conn, {
            "source": "linkedin", "title": "Something else", "company": "Other",
            "url": "https://example.com/jobs/1/?trk=public_jobs&refId=x"})
        record = self.tracker.find_for_job(store.get_job(self.conn, job_id))
        self.assertIsNotNone(record)
        self.assertEqual(record.get("program"), "Management Trainee")

    def test_matches_on_company_and_title(self):
        job_id, _ = store.add_job(self.conn, {
            "source": "jobsdb", "title": "Analyst", "company": "Beta Corp",
            "url": "https://elsewhere.test/1"})
        record = self.tracker.find_for_job(store.get_job(self.conn, job_id))
        self.assertEqual(record.get("my_status"), "Applied")

    def test_an_unrelated_job_is_not_tracked(self):
        self.assertIsNone(self.tracker.find_for_job(self.job(self.ids["low"])))
        self.assertEqual(self.tracker.status_for(self.job(self.ids["low"]), None), "new")

    def test_status_follows_the_record_and_the_decision(self):
        self.assertEqual(self.tracker.status_for(self.matched_job(), None), "to_apply")
        self.assertEqual(self.tracker.status_for(self.job(self.ids["low"]), "skip"), "skipped")

    def test_tracked_info_shape(self):
        info = self.tracker.tracked_info(self.matched_job())
        self.assertEqual(set(info), {"file", "obsidian_url", "my_status"})
        self.assertTrue(info["file"].startswith(RECORDS + "/"))
        self.assertIn("obsidian://open?vault=Career-Wiki", info["obsidian_url"])


class Applications(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.vault = make_vault(self.tmp.name)
        self.tracker = Tracker(self.vault, today=lambda: TODAY)

    def test_to_apply(self):
        rows = self.tracker.applications("to_apply")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["my_status"], "To apply")
        self.assertIsNone(rows[0]["date_applied"])
        self.assertIsNone(rows[0]["days_waiting"])

    def test_applied_rows_have_days_waiting_and_are_newest_first(self):
        (self.vault.path / "Gamma — Old.md").write_text(
            record_text("Gamma — Old", company="Gamma", program="Old", my_status="Rejected",
                        link="https://example.com/gamma", date_applied="2026-10-01"),
            encoding="utf-8")
        self.tracker.reload()
        rows = self.tracker.applications("applied")
        self.assertEqual([row["company"] for row in rows], ["Beta Corp", "Gamma"])
        self.assertEqual(rows[0]["days_waiting"], (TODAY - date(2026, 10, 24)).days)

    def test_only_apply_now_records_are_to_apply(self):
        (self.vault.path / "Blocked — Role.md").write_text(
            record_text("Blocked — Role", company="Blocked Co", program="Role",
                        my_status="To apply", section="Blocked", link="https://x.test/1",
                        do_by="2026-11-02"), encoding="utf-8")
        self.tracker.reload()
        companies = [row["company"] for row in self.tracker.applications("to_apply")]
        self.assertEqual(companies, ["Acme"])

    def test_to_apply_is_sorted_by_do_by(self):
        (self.vault.path / "Zeta — Soon.md").write_text(
            record_text("Zeta — Soon", company="Zeta", program="Soon", my_status="To apply",
                        section="Apply now", link="https://x.test/zeta", do_by="2026-11-02"),
            encoding="utf-8")
        (self.vault.path / "Alpha — Later.md").write_text(
            record_text("Alpha — Later", company="Alpha", program="Later", my_status="To apply",
                        section="Apply now", link="https://x.test/alpha", do_by="2026-11-20"),
            encoding="utf-8")
        (self.vault.path / "No date — Role.md").write_text(
            record_text("No date — Role", company="No date", program="Role", my_status="To apply",
                        section="Apply now", link="https://x.test/none", do_by="2026-11-01")
            .replace('do_by: "2026-11-01"\n', ""), encoding="utf-8")
        self.tracker.reload()
        companies = [row["company"] for row in self.tracker.applications("to_apply")]
        self.assertEqual(companies, ["Zeta", "Acme", "Alpha", "No date"])

    def test_records_without_a_status_are_ignored(self):
        self.assertNotIn("Old Co", [row["company"] for row in self.tracker.applications()])

    def test_row_shape(self):
        row = self.tracker.applications("to_apply")[0]
        self.assertEqual(set(row), {
            "record_file", "obsidian_url", "company", "program", "my_status", "section",
            "date_applied", "days_waiting", "next_step", "jobhunt_id", "link", "apply_link",
            "outcome_stage", "outcome_reason_category", "outcome_reason", "outcome_date"})


class Apply(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.vault = make_vault(self.tmp.name)
        self.tracker = Tracker(self.vault, today=lambda: TODAY)
        self.conn, self.ids = seeded_db(self)

    def apply(self, name):
        row = store.board_job(self.conn, self.ids[name])
        return self.tracker.apply(row, row, today=TODAY)

    def apply_matched(self):
        job_id, _ = store.add_job(self.conn, {
            "source": "linkedin", "title": "Already In The Vault", "company": "Acme",
            "url": "https://example.com/jobs/1", "jd_text": "A description."})
        row = store.board_job(self.conn, job_id)
        return self.tracker.apply(row, row, today=TODAY)

    def test_a_new_record_is_created_with_the_contract_fields(self):
        record, created = self.apply("low")
        self.assertTrue(created)
        self.assertEqual(record.file_name, "Acme — Key Account Executive.md")
        self.assertEqual(record.get("type"), "record")
        self.assertEqual(record.get("folder"), RECORDS)
        self.assertEqual(record.get("status"), "current")
        self.assertEqual(record.get("updated"), "2026-11-07")
        self.assertEqual(record.get("my_status"), "To apply")
        self.assertEqual(record.get("section"), "Apply now")
        self.assertEqual(record.get("database"), "Applications")
        self.assertEqual(record.get("do_by"), "2026-11-10")
        self.assertEqual(record.get("apply_link"), "https://example.com/jobs/Key Account Executive")
        self.assertEqual(record.get("company"), "Acme")
        self.assertEqual(record.get("program"), "Key Account Executive")
        self.assertEqual(record.get("jobhunt_id"), self.ids["low"])
        self.assertEqual(record.get("job_platform"), "Other")
        self.assertEqual(record.get("job_type"), "Graduate program")
        self.assertEqual(record.get("ikigai"), "L2 G3 N4 P4")
        self.assertEqual(record.get("ikigai_verdict"), "LOW")
        self.assertEqual(record.get("tags"), ["type/record", "db/applications"])
        self.assertNotIn("deadline", record.fields)          # the JD states none
        self.assertIn("Record in [[Applications]]", record.body)

    def test_the_note_carries_the_summary_and_the_questions(self):
        record, _ = self.apply("filtered")
        note = record.get("note")
        self.assertIn("A short summary.", note)
        self.assertIn("Gate questions:", note)
        self.assertIn("Found by my job hunt (manual", note)

    def test_an_estimated_salary_is_marked_in_the_ikigai_property(self):
        record, _ = self.apply("centre")
        self.assertEqual(record.get("ikigai"), "L5 G4 N4 P3e")

    def test_applying_to_a_tracked_job_creates_nothing(self):
        record, created = self.apply_matched()     # already in the vault by URL
        self.assertFalse(created)
        self.assertEqual(record.get("company"), "Acme")
        self.assertEqual(len(list(self.vault.path.glob("*.md"))), 3)

    def test_applying_twice_creates_one_record(self):
        first, created_first = self.apply("low")
        second, created_second = self.apply("low")
        self.assertTrue(created_first)
        self.assertFalse(created_second)
        self.assertEqual(first.path, second.path)
        self.assertEqual(len(list(self.vault.path.glob("*.md"))), 4)

    def test_the_record_is_valid_frontmatter_and_reads_back(self):
        record, _ = self.apply("low")
        again = self.tracker.vault.record(record.rel)
        self.assertEqual(again.get("jobhunt_id"), self.ids["low"])
        self.assertEqual(again.get("my_status"), "To apply")


class MarkSubmitted(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.vault = make_vault(self.tmp.name)
        self.tracker = Tracker(self.vault, today=lambda: TODAY)

    def test_it_sets_status_date_and_summary(self):
        record = self.tracker.vault.records()[0]
        before = record.path.read_text(encoding="utf-8").splitlines()
        self.tracker.mark_submitted(record.rel, today=TODAY)
        after = record.path.read_text(encoding="utf-8").splitlines()
        text = "\n".join(after)
        self.assertIn('my_status: "Applied"', text)
        self.assertIn('date_applied: "2026-11-07"', text)
        self.assertIn('updated: "2026-11-07"', text)
        self.assertIn('summary: "Acme — Management Trainee: Applied, section Apply now, do by 2026-11-10"', text)
        self.assertIn('next_step: "Submitted 2026-11-07. Watch email (incl. spam) for the test or interview request."'
                      .replace("request", "invite"), text)
        keys = {line.split(":")[0] for line in before}
        self.assertEqual(len(after), len(before) + (0 if "date_applied" in keys else 1))

    def test_other_lines_are_untouched(self):
        record = self.tracker.vault.records()[0]
        before = record.path.read_text(encoding="utf-8").splitlines()
        self.tracker.mark_submitted(record.rel, today=TODAY)
        after = record.path.read_text(encoding="utf-8").splitlines()
        edited = ("my_status:", "summary:", "updated:", "date_applied:", "next_step:")
        untouched_before = [line for line in before if not line.startswith(edited)]
        untouched_after = [line for line in after if not line.startswith(edited)]
        self.assertEqual(untouched_before, untouched_after)

    def test_it_moves_the_job_to_the_applied_list(self):
        record = self.tracker.vault.records()[0]
        self.tracker.mark_submitted(record.rel, today=TODAY)
        self.assertEqual(len(self.tracker.applications("to_apply")), 0)
        self.assertEqual([row["company"] for row in self.tracker.applications("applied")],
                         ["Acme", "Beta Corp"])

    def test_a_missing_record_raises(self):
        with self.assertRaises(Exception):
            self.tracker.mark_submitted("61 Application Records/Nope.md", today=TODAY)


if __name__ == "__main__":
    unittest.main()
