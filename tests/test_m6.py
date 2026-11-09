"""M6: the status-email scan, ghosted prompts, confirm/dismiss, lessons."""

import json
import tempfile
import unittest
from collections import Counter
from dataclasses import replace
from datetime import date
from pathlib import Path

from _util import CFG
from _vault import TODAY, make_vault, record_text
from test_gmail import FakeImap

from jobhunt import board_api, outcomes, store
from jobhunt.llm import FakeLLM
from jobhunt.sources import gmail as gmail_source, status_mail
from jobhunt.tracker import Tracker


def mail(sender, subject, body, message_id):
    return (f"From: {sender}\r\nSubject: {subject}\r\nMessage-ID: {message_id}\r\n"
            f"Content-Type: text/plain; charset=utf-8\r\n\r\n{body}").encode("utf-8")


REJECTION = mail("Sea Limited Talent Acquisition <no-reply@sea.com>",
                 "Your application to Sea Limited", "Thank you for applying. We have decided "
                 "to move forward with other candidates.", "<rej-1@sea.com>")
INTERVIEW = mail("CP Axtra Careers <careers@cpaxtra.co.th>",
                 "Interview invitation - Future Leader Program",
                 "We would like to invite you to an interview on 14 Nov 2026.", "<inv-1@cpaxtra.co.th>")
TEST_INVITE = mail("Deutsche Bank Recruitment <noreply@db.com>",
                   "Online assessment", "Please complete the SOVA test by 14 Nov 2026.",
                   "<test-1@db.com>")
UNRELATED = mail("Bangkok Bank Marketing <news@bangkokbank.com>",
                 "A special offer for you", "Open a savings account today.",
                 "<news-1@bangkokbank.com>")
ALERT = mail("LinkedIn Job Alerts <jobalerts-noreply@linkedin.com>",
             "5 new jobs for you", "Management trainee roles in Bangkok", "<alert-1@linkedin.com>")


def classification(message_id, record_file, kind, stage, category, excerpt, next_step=""):
    return {"message_id": message_id, "record_file": record_file, "type": kind,
            "outcome_stage": stage, "reason_category": category, "excerpt": excerpt,
            "suggested_next_step": next_step}


class ScanCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = replace(CFG, data_root=Path(self.tmp.name))
        self.conn = store.connect_memory()
        self.vault = make_vault(self.tmp.name)
        records = self.vault.path
        (records / "Sea Limited — Global Management Associate.md").write_text(
            record_text("Sea Limited — Global Management Associate", company="Sea Limited",
                        program="Global Management Associate", my_status="Applied",
                        link="https://example.com/sea", date_applied="2026-10-24"), encoding="utf-8")
        (records / "CP Axtra — Future Leader Program.md").write_text(
            record_text("CP Axtra — Future Leader Program", company="CP Axtra",
                        program="Future Leader Program", my_status="Applied",
                        link="https://example.com/cpaxtra", date_applied="2026-10-30"), encoding="utf-8")
        (records / "Deutsche Bank — Graduate Programme.md").write_text(
            record_text("Deutsche Bank — Graduate Programme", company="Deutsche Bank",
                        program="Graduate Programme", my_status="Applied",
                        link="https://example.com/db", date_applied="2026-11-01"), encoding="utf-8")
        self.tracker = Tracker(self.vault, today=lambda: TODAY)

    def scan(self, messages, reply):
        stamp = "07-Nov-2026 09:12:00 +0700"
        imap = FakeImap([(str(index + 1).encode(), stamp, raw)
                         for index, raw in enumerate(messages)])
        reader = gmail_source.GmailReader(self.cfg, imap_factory=lambda host: imap,
                                          password="x", now=lambda: __import__(
                                              "datetime").datetime(2026, 11, 7, 12, 0,
                                                                   tzinfo=gmail_source.BANGKOK))
        llm = FakeLLM([reply])
        return status_mail.scan(self.cfg, self.tracker, self.conn, reader, llm), llm


class Scanning(ScanCase):
    def setUp(self):
        super().setUp()
        self.reply = {"emails": [
            classification("<rej-1@sea.com>", "61 Application Records/Sea Limited — Global Management Associate.md",
                           "rejection", "cv_screen", "experience", "move forward with other candidates"),
            classification("<inv-1@cpaxtra.co.th>", "61 Application Records/CP Axtra — Future Leader Program.md",
                           "interview_invite", "interview", "other", "interview on 14 Nov 2026",
                           "Confirm the interview slot"),
            classification("<test-1@db.com>", "61 Application Records/Deutsche Bank — Graduate Programme.md",
                           "test_invite", "online_test", "other", "SOVA test by 14 Nov 2026",
                           "Complete the SOVA test by 14 Nov"),
        ]}

    def test_three_employer_emails_become_three_suggestions(self):
        report, llm = self.scan([REJECTION, INTERVIEW, TEST_INVITE, UNRELATED, ALERT], self.reply)
        self.assertEqual(report["stored"], 3)
        self.assertEqual(len(store.pending_suggestions(self.conn)), 3)
        self.assertEqual(len(llm.calls), 1)

    def test_the_unrelated_email_never_reaches_the_model(self):
        _, llm = self.scan([REJECTION, INTERVIEW, TEST_INVITE, UNRELATED, ALERT], self.reply)
        prompt = llm.calls[0]["prompt"]
        self.assertNotIn("savings account", prompt)
        self.assertNotIn("5 new jobs for you", prompt)

    def test_the_suggestion_carries_the_email_and_the_proposal(self):
        self.scan([REJECTION, INTERVIEW, TEST_INVITE, UNRELATED, ALERT], self.reply)
        rows = [outcomes.suggestion_row(row) for row in store.pending_suggestions(self.conn)]
        rejection = next(row for row in rows if row["type"] == "rejection")
        self.assertEqual(rejection["proposed"]["my_status"], "Rejected")
        self.assertEqual(rejection["proposed"]["outcome_reason_category"], "experience")
        self.assertEqual(rejection["email"]["from"].split(" <")[0], "Sea Limited Talent Acquisition")
        test = next(row for row in rows if row["type"] == "test_invite")
        self.assertEqual(test["proposed"]["my_status"], "Test")
        self.assertIn("SOVA", test["proposed"]["next_step"])

    def test_a_duplicate_message_id_is_ignored_on_the_next_scan(self):
        self.scan([REJECTION, INTERVIEW, TEST_INVITE], self.reply)
        report, _ = self.scan([REJECTION, INTERVIEW, TEST_INVITE], self.reply)
        self.assertEqual(report["duplicates"], 3)
        self.assertEqual(report["stored"], 0)
        self.assertEqual(len(store.pending_suggestions(self.conn)), 3)


class Ghosted(ScanCase):
    def sea(self):
        return next(r for r in self.tracker.records if r.get("company") == "Sea Limited")

    def test_29_days_is_quiet_and_30_is_not(self):
        # move the Sea application back to 29 days before TODAY, then to 30
        self.vault.update_keys(self.sea(), {"date_applied": "2026-10-09"})
        self.tracker.reload()
        self.assertEqual(status_mail.ghosted_suggestions(self.cfg, self.tracker, self.conn), 0)
        self.vault.update_keys(self.sea(), {"date_applied": "2026-10-08"})  # 30 days
        self.tracker.reload()
        self.assertEqual(status_mail.ghosted_suggestions(self.cfg, self.tracker, self.conn), 1)
        row = store.pending_suggestions(self.conn)[0]
        self.assertEqual(row["type"], "ghosted")
        self.assertIn("No reply", row["excerpt"])

    def test_a_dismissed_ghosted_suggestion_comes_back_after_another_period(self):
        self.vault.update_keys(self.sea(), {"date_applied": "2026-10-08"})
        self.tracker.reload()
        status_mail.ghosted_suggestions(self.cfg, self.tracker, self.conn)
        outcome_id = store.pending_suggestions(self.conn)[0]["id"]
        outcomes.dismiss(self.conn, outcome_id)
        self.assertEqual(status_mail.ghosted_suggestions(self.cfg, self.tracker, self.conn), 0)
        self.tracker.today = lambda: date(2026, 12, 9)      # another 30 days on
        status_mail.ghosted_suggestions(self.cfg, self.tracker, self.conn)
        sea = [row for row in store.pending_suggestions(self.conn)
               if row["company"] == "Sea Limited"]
        self.assertEqual(len(sea), 1)
        self.assertTrue(sea[0]["message_id"].endswith(":2"))   # the next bucket


class ConfirmAndDismiss(ScanCase):
    def make_suggestion(self):
        store.add_suggestion(self.conn, {
            "message_id": "<rej-1@sea.com>",
            "record_file": "61 Application Records/Sea Limited — Global Management Associate.md",
            "company": "Sea Limited", "program": "Global Management Associate",
            "type": "rejection",
            "proposed": {"my_status": "Rejected", "outcome_stage": "cv_screen",
                         "outcome_reason_category": "experience",
                         "next_step": "No action needed."},
            "excerpt": "We have decided to move forward with other candidates.",
            "email_from": "Sea Limited <no-reply@sea.com>",
            "email_subject": "Your application", "email_date": "2026-11-06",
            "detected_at": "2026-11-07T00:00:00Z"})
        return store.pending_suggestions(self.conn)[0]["id"]

    def test_confirm_writes_exactly_the_listed_keys(self):
        suggestion_id = self.make_suggestion()
        record = self.tracker.vault.record(
            "61 Application Records/Sea Limited — Global Management Associate.md")
        before = record.path.read_text(encoding="utf-8").splitlines()
        outcomes.confirm(self.conn, self.tracker, suggestion_id,
                         {"note": "They said other candidates."}, today=TODAY)
        after = record.path.read_text(encoding="utf-8").splitlines()

        def keys(lines):
            return {line.split(":")[0].strip() for line in lines if ":" in line}

        changed = Counter(before) - Counter(after)
        added = Counter(after) - Counter(before)
        touched = {line.split(":")[0].strip() for line in list(changed) + list(added)}
        self.assertTrue(touched <= set(outcomes.OUTCOME_KEYS), touched)

        text = "\n".join(after)
        self.assertIn('my_status: "Rejected"', text)
        self.assertIn('section: "Archive"', text)
        self.assertIn('outcome_stage: "cv_screen"', text)
        self.assertIn('outcome_reason_category: "experience"', text)
        self.assertIn('outcome_reason: "They said other candidates."', text)
        self.assertIn('outcome_date: "2026-11-07"', text)
        self.assertEqual(store.get_suggestion(self.conn, suggestion_id)["state"], "confirmed")

    def test_confirm_can_override_the_category(self):
        suggestion_id = self.make_suggestion()
        outcomes.confirm(self.conn, self.tracker, suggestion_id,
                         {"outcome_reason_category": "role_filled"}, today=TODAY)
        record = self.tracker.vault.record(
            "61 Application Records/Sea Limited — Global Management Associate.md")
        self.assertEqual(record.get("outcome_reason_category"), "role_filled")

    def test_confirming_twice_is_refused(self):
        suggestion_id = self.make_suggestion()
        outcomes.confirm(self.conn, self.tracker, suggestion_id, {}, today=TODAY)
        with self.assertRaises(ValueError):
            outcomes.confirm(self.conn, self.tracker, suggestion_id, {}, today=TODAY)

    def test_dismiss_keeps_the_record_untouched_and_suppresses_the_message(self):
        suggestion_id = self.make_suggestion()
        record = self.tracker.vault.record(
            "61 Application Records/Sea Limited — Global Management Associate.md")
        before = record.path.read_text(encoding="utf-8")
        outcomes.dismiss(self.conn, suggestion_id)
        self.assertEqual(record.path.read_text(encoding="utf-8"), before)
        self.assertEqual(store.pending_suggestions(self.conn), [])
        self.assertEqual(store.suggestion_for_message(self.conn, "<rej-1@sea.com>")["state"],
                         "dismissed")


class ManualStatus(ScanCase):
    def test_the_manual_form_writes_the_same_shape(self):
        response = board_api.change_status(self.tracker, {
            "record_file": "61 Application Records/CP Axtra — Future Leader Program.md",
            "my_status": "Interview", "outcome_stage": "interview",
            "outcome_reason_category": "other", "next_step": "Call back on Monday"},
            today=TODAY)
        self.assertEqual(response, {"ok": True})
        record = self.tracker.vault.record(
            "61 Application Records/CP Axtra — Future Leader Program.md")
        self.assertEqual(record.get("my_status"), "Interview")
        self.assertEqual(record.get("outcome_stage"), "interview")
        self.assertEqual(record.get("next_step"), "Call back on Monday")

    def test_a_missing_field_is_a_400(self):
        with self.assertRaises(board_api.ApiError) as ctx:
            board_api.change_status(self.tracker, {"record_file": "x"}, today=TODAY)
        self.assertEqual(ctx.exception.status, 400)


class Lessons(ScanCase):
    def setUp(self):
        super().setUp()
        records = self.vault.path
        (records / "AlphaSights — Associate.md").write_text(
            record_text("AlphaSights — Associate", company="AlphaSights", program="Associate",
                        my_status="Rejected", link="https://example.com/alpha",
                        date_applied="2026-10-09",
                        extra=['note: "Closed — not passed (stated 1 Nov). Reason not given."']),
            encoding="utf-8")
        (records / "Shopee — Key Account Manager.md").write_text(
            record_text("Shopee — Key Account Manager", company="Shopee",
                        program="Key Account Manager", my_status="Rejected",
                        link="https://example.com/shopee", date_applied="2026-10-01",
                        extra=['outcome_stage: "cv_screen"',
                               'outcome_reason_category: "gpa_academic"',
                               'outcome_reason: "They screen on grades."',
                               'ikigai: "L5 G3 N4 P2e"']),
            encoding="utf-8")
        self.tracker.reload()

    def test_rows_include_the_existing_rejection_and_the_board_ones(self):
        view = outcomes.lessons(self.tracker)
        companies = [row["company"] for row in view["lessons"]]
        self.assertIn("AlphaSights", companies)
        self.assertIn("Shopee", companies)

    def test_aggregates(self):
        view = outcomes.lessons(self.tracker)
        self.assertEqual(view["by_stage"], {"cv_screen": 1})
        self.assertEqual(view["by_category"], {"gpa_academic": 1, "no_reason_given": 1})
        self.assertEqual(view["by_weakest_circle"], {"paid": 1})
        self.assertEqual({row["company"] for row in board_api.lessons(self.tracker)["lessons"]},
                         {"AlphaSights", "Shopee"})

    def test_the_note_field_never_reaches_a_lesson_or_the_prompt(self):
        note = next(r for r in self.tracker.records if r.get("company") == "AlphaSights")
        self.vault.update_keys(note, {"note": "Form answers: GPA 2.5-3.0, start July, visa needed."})
        self.tracker.reload()
        view = outcomes.lessons(self.tracker)
        alpha = next(row for row in view["lessons"] if row["company"] == "AlphaSights")
        self.assertIsNone(alpha["outcome_reason"])
        block = outcomes.recent_outcomes_block(self.tracker, 10)
        self.assertNotIn("GPA", block)
        self.assertNotIn("visa needed", block)

    def test_a_legacy_record_reports_no_outcome_date(self):
        alpha = next(row for row in outcomes.lessons(self.tracker)["lessons"]
                     if row["company"] == "AlphaSights")
        self.assertIsNone(alpha["outcome_date"])
        self.assertEqual(alpha["outcome_reason_category"], "no_reason_given")

    def test_the_scoring_prompt_gains_a_recent_outcomes_block(self):
        block = outcomes.recent_outcomes_block(self.tracker, 10)
        self.assertIn("Recent outcomes", block)
        self.assertIn("Shopee", block)
        from jobhunt import score as score_mod
        prompt = score_mod.build_prompt(self.cfg, [], block)
        self.assertIn("Recent outcomes", prompt)

    def test_without_outcomes_the_block_is_empty(self):
        empty = Tracker(make_vault(tempfile.mkdtemp()), today=lambda: TODAY)
        self.assertEqual(outcomes.recent_outcomes_block(empty), "")


class ClosedNextStep(ScanCase):
    def setUp(self):
        super().setUp()
        self.vault.update_keys(next(r for r in self.tracker.records
                                    if r.get("company") == "Sea Limited"),
                               {"date_applied": "2026-10-01"})
        self.tracker.reload()

    def suggestion(self, kind, proposed):
        store.add_suggestion(self.conn, {
            "message_id": f"<{kind}-1@x>", "record_file":
                "61 Application Records/Sea Limited — Global Management Associate.md",
            "company": "Sea Limited", "program": "Global Management Associate", "type": kind,
            "proposed": proposed, "excerpt": "No reply 37 days after applying (2026-10-01).",
            "detected_at": "2026-11-07T00:00:00Z"})
        return store.pending_suggestions(self.conn)[-1]["id"]

    def test_a_confirmed_ghosted_record_says_so_instead_of_an_imperative(self):
        suggestion_id = self.suggestion("ghosted", {
            "my_status": "Ghosted", "outcome_stage": "unknown",
            "outcome_reason_category": "ghosted",
            "next_step": "No reply in 37 days. Mark as ghosted and move on."})
        outcomes.confirm(self.conn, self.tracker, suggestion_id, today=TODAY)
        record = self.tracker.vault.record(
            "61 Application Records/Sea Limited — Global Management Associate.md")
        self.assertEqual(record.get("next_step"),
                         "Closed: no reply after 37 days (marked ghosted 2026-11-07).")

    def test_a_confirmed_rejection_names_the_stage(self):
        suggestion_id = self.suggestion("rejection", {
            "my_status": "Rejected", "outcome_stage": "cv_screen",
            "outcome_reason_category": "experience", "next_step": "No action needed."})
        outcomes.confirm(self.conn, self.tracker, suggestion_id, today=TODAY)
        record = self.tracker.vault.record(
            "61 Application Records/Sea Limited — Global Management Associate.md")
        self.assertEqual(record.get("next_step"),
                         "Closed: rejected at cv_screen (2026-11-07).")

    def test_an_interview_confirmation_keeps_the_classifier_next_step(self):
        suggestion_id = self.suggestion("interview_invite", {
            "my_status": "Interview", "outcome_stage": "interview",
            "outcome_reason_category": "other", "next_step": "Confirm the slot on 14 Nov"})
        outcomes.confirm(self.conn, self.tracker, suggestion_id, today=TODAY)
        record = self.tracker.vault.record(
            "61 Application Records/Sea Limited — Global Management Associate.md")
        self.assertEqual(record.get("next_step"), "Confirm the slot on 14 Nov")


class EveryScoringPathUsesLessons(unittest.TestCase):
    """The "Recent outcomes" block must reach score, run and the board's paste."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = replace(CFG, data_root=Path(self.tmp.name))
        self.vault = make_vault(self.tmp.name)
        (self.vault.path / "Shopee — KAM.md").write_text(
            record_text("Shopee — KAM", company="Shopee", program="KAM", my_status="Rejected",
                        link="https://example.com/shopee", date_applied="2026-10-01",
                        extra=['outcome_stage: "cv_screen"',
                               'outcome_reason_category: "gpa_academic"']), encoding="utf-8")
        self.tracker = Tracker(self.vault, today=lambda: TODAY)
        self.conn = store.connect_memory()

    def test_the_pipeline_run_scores_with_the_block(self):
        from _fake import FakeTransport
        from jobhunt import pipeline
        from jobhunt.http import PoliteFetcher
        store.add_job(self.conn, {"source": "manual", "title": "Management Trainee",
                                  "company": "Acme", "jd_text": "A programme."})
        fetcher = PoliteFetcher(self.cfg.profile, transport=FakeTransport({}))
        llm = FakeLLM([{"jobs": []}])
        pipeline.run(self.cfg, self.conn, fetcher, llm, sources=[],
                     outcomes=outcomes.recent_outcomes_block(self.tracker, 10))
        self.assertIn("Recent outcomes", llm.calls[0]["prompt"])
        self.assertIn("Shopee", llm.calls[0]["prompt"])

    def test_paste_scores_with_the_block(self):
        from _fake import FakeTransport
        from jobhunt.http import PoliteFetcher
        from _vault import scored_reply
        llm = FakeLLM([lambda prompt, ids: scored_reply(ids)])
        fetcher = PoliteFetcher(self.cfg.profile, transport=FakeTransport({}))
        board_api.paste(self.cfg, self.conn, self.tracker,
                        {"jd_text": "A rotational programme.", "title": "Pasted Trainee"},
                        llm, fetcher)
        self.assertIn("Recent outcomes", llm.calls[0]["prompt"])
        self.assertIn("Shopee", llm.calls[0]["prompt"])


class Stats(ScanCase):
    def test_suggestions_pending_is_counted(self):
        store.add_suggestion(self.conn, {
            "message_id": "<x@y>", "record_file": "61 Application Records/x.md",
            "type": "rejection", "proposed": {"my_status": "Rejected"},
            "detected_at": "2026-11-07T00:00:00Z"})
        self.assertEqual(board_api.stats(self.conn, self.tracker)["suggestions_pending"], 1)
        item = board_api.suggestions(self.conn, self.tracker)["items"][0]
        self.assertEqual(item["type"], "rejection")
        self.assertIsNone(item["current_status"])      # the record does not exist


if __name__ == "__main__":
    unittest.main()
