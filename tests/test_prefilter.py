import unittest

from _util import job, profile

from jobhunt import prefilter

P = profile()
NOW = prefilter.parse_posted_at("2026-11-07T12:00:00+07:00")


class TitleRules(unittest.TestCase):
    def test_senior_is_filtered(self):
        self.assertEqual(prefilter.title_hit("Senior Key Account Manager - Modern Trade", P),
                         "title_exclude: \\bsenior\\b")

    def test_manager_alone_is_not_a_seniority_signal(self):
        """User, 8 Nov 2026: Key Account / Account Manager roles can take fresh grads."""
        for title in ("Key Account Manager", "Account Manager, FMCG", "Supervisor, Modern Trade",
                      "Category Manager - TikTok Shop", "Brand Manager"):
            self.assertIsNone(prefilter.title_hit(title, P), title)

    def test_clearly_senior_manager_titles_are_filtered(self):
        for title in ("Senior Manager, Commercial", "General Manager", "Country Manager Thailand",
                      "Regional Manager, SEA", "Assistant General Manager"):
            self.assertIsNotNone(prefilter.title_hit(title, P), title)

    def test_management_trainee_survives(self):
        self.assertIsNone(prefilter.title_hit("Management Trainee Program 2027", P))

    def test_fresh_graduate_survives_even_with_manager_in_the_title(self):
        self.assertIsNone(prefilter.title_hit("Fresh Graduate Management Trainee Program (MT)", P))

    def test_graduate_trainee_survives(self):
        self.assertIsNone(prefilter.title_hit("Graduate Trainee Program 2027", P))

    def test_software_engineer_is_filtered(self):
        self.assertIsNotNone(prefilter.title_hit("Software Engineer, Backend", P))

    def test_hard_excludes_survive_an_include_override(self):
        # "graduate" is an include override, but an off-track profession is filtered anyway.
        self.assertEqual(prefilter.title_hit("Graduate Software Engineer", P),
                         "title_exclude_hard: \\bsoftware engineer\\b")

    def test_hard_exclude_label_is_named_as_such(self):
        self.assertTrue(prefilter.title_hit("Staff Nurse", P).startswith("title_exclude_hard: "))

    def test_seniority_words_are_cancelled_by_an_override(self):
        self.assertIsNone(prefilter.title_hit("Junior Key Account Manager", P))
        self.assertIsNone(prefilter.title_hit("Entry Level Supervisor", P))

    def test_hard_excludes_are_checked_before_seniority(self):
        self.assertTrue(prefilter.title_hit("Senior Software Engineer", P)
                        .startswith("title_exclude_hard: "))

    def test_ordinary_commercial_titles_survive(self):
        for title in ("Associate Analyst, Marketing (Bangkok)", "Account Specialist, Homes",
                      "Accelerated Development Program (E-Commerce, Thailand) - 2027 Start",
                      "Business Development Executive"):
            self.assertIsNone(prefilter.title_hit(title, P), title)

    def test_empty_title_is_not_filtered(self):
        self.assertIsNone(prefilter.title_hit(None, P))


class DateRules(unittest.TestCase):
    """``max_age_days`` (30) is the only age rule; the search windows never filter."""

    def test_recent_posting_passes(self):
        self.assertIsNone(prefilter.date_hit("2026-11-06", P, now=NOW))

    def test_a_four_day_old_posting_is_kept(self):
        # the TikTok Shop FMCG role posted on 4 Nov, dropped by the old 3-day rule
        self.assertIsNone(prefilter.date_hit("2026-11-03", P, now=NOW))
        self.assertIsNone(prefilter.check(job(title="Category Strategist, FMCG (Thailand)",
                                              posted_at="2026-11-03"), P, now=NOW))

    def test_a_forty_day_old_posting_is_filtered(self):
        self.assertEqual(prefilter.date_hit("2026-09-28", P, now=NOW), "max_age_days: 30d")

    def test_the_search_windows_never_filter(self):
        narrow = profile(**{"prefilter.posted_within_days": 1,
                            "prefilter.posted_within_days_first_run": 1})
        self.assertIsNone(prefilter.date_hit("2026-10-20", narrow, now=NOW))
        self.assertIsNone(prefilter.date_hit("2026-10-20", P, now=NOW, window_days=3))
        self.assertIsNone(prefilter.date_hit("2026-10-20", P, now=NOW, first_run=False))

    def test_max_age_is_read_from_the_profile(self):
        self.assertEqual(P["prefilter"]["max_age_days"], 30)
        short = profile(**{"prefilter.max_age_days": 7})
        self.assertEqual(prefilter.date_hit("2026-10-28", short, now=NOW), "max_age_days: 7d")

    def test_unknown_date_is_never_filtered(self):
        self.assertIsNone(prefilter.date_hit(None, P, now=NOW))

    def test_garbage_date_is_never_filtered(self):
        self.assertIsNone(prefilter.date_hit("sometime last week", P, now=NOW))

    def test_future_date_is_not_filtered(self):
        self.assertIsNone(prefilter.date_hit("2027-01-01", P, now=NOW))

    def test_iso_datetime_with_offset_is_understood(self):
        self.assertIsNone(prefilter.date_hit("2026-11-07T09:00:00+07:00", P, now=NOW))


class Combined(unittest.TestCase):
    def test_title_hit_wins_over_date(self):
        self.assertEqual(
            prefilter.check(job(title="Senior Analyst", posted_at="2026-10-01"), P, now=NOW),
            "title_exclude: \\bsenior\\b")

    def test_clean_job_passes_both_rules(self):
        self.assertIsNone(prefilter.check(job(title="Management Trainee", posted_at="2026-11-06"),
                                          P, now=NOW))


if __name__ == "__main__":
    unittest.main()
