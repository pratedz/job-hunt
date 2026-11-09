import unittest

from _util import facts, profile

from jobhunt import gates

P = profile()


def run(**kw):
    return gates.check(facts(**kw), P)


def outcome(result, rule):
    for hit in result["hits"]:
        if hit["rule"] == rule:
            return hit["outcome"]
    raise AssertionError(f"no hit for {rule}: {result['hits']}")


class ExperienceRule(unittest.TestCase):
    def test_not_stated_passes(self):
        self.assertEqual(run(min_years_experience=None)["hits"][0]["outcome"], "pass")

    def test_zero_years_passes(self):
        self.assertEqual(outcome(run(min_years_experience=0), "experience"), "pass")

    def test_one_year_asks(self):
        result = run(min_years_experience=1)
        self.assertEqual(outcome(result, "experience"), "ask")
        self.assertEqual(result["result"], "ask")
        self.assertTrue(any("fresh graduate" in q for q in result["questions"]))

    def test_two_years_fails_with_the_number(self):
        result = run(min_years_experience=2)
        self.assertEqual(outcome(result, "experience"), "fail")
        self.assertEqual(result["result"], "fail")
        self.assertIn("2 years", result["reasons"][0])

    def test_five_years_fails(self):
        self.assertEqual(outcome(run(min_years_experience=5), "experience"), "fail")


class GpaRule(unittest.TestCase):
    def test_not_stated_passes(self):
        self.assertEqual(outcome(run(gpa_floor=None), "gpa"), "pass")

    def test_floor_equal_to_his_gpa_passes(self):
        self.assertEqual(outcome(run(gpa_floor=2.48), "gpa"), "pass")

    def test_floor_below_his_gpa_passes(self):
        self.assertEqual(outcome(run(gpa_floor=2.00), "gpa"), "pass")

    def test_floor_above_his_gpa_fails_and_quotes_both_numbers(self):
        result = run(gpa_floor=3.0)
        self.assertEqual(outcome(result, "gpa"), "fail")
        self.assertIn("3.00", result["reasons"][0])
        self.assertIn("2.48", result["reasons"][0])

    def test_floor_just_above_fails(self):
        self.assertEqual(outcome(run(gpa_floor=2.49), "gpa"), "fail")


class StartRule(unittest.TestCase):
    def test_immediate_asks(self):
        result = run(start_kind="immediate")
        self.assertEqual(outcome(result, "start"), "ask")
        self.assertTrue(any("2027-06" in q for q in result["questions"]))

    def test_fixed_date_on_the_available_month_passes(self):
        self.assertEqual(outcome(run(start_kind="fixed_date", start_date="2027-06"), "start"), "pass")

    def test_fixed_date_before_available_fails(self):
        result = run(start_kind="fixed_date", start_date="2027-05")
        self.assertEqual(outcome(result, "start"), "fail")
        self.assertIn("2027-05", result["reasons"][0])

    def test_fixed_date_within_two_months_after_passes(self):
        self.assertEqual(outcome(run(start_kind="fixed_date", start_date="2027-08",
                                     location_country="Thailand"), "start"), "pass")

    def test_fixed_date_more_than_two_months_after_asks(self):
        result = run(start_kind="fixed_date", start_date="2027-09", location_country="Thailand")
        self.assertEqual(outcome(result, "start"), "ask")
        self.assertTrue(any("wait that long" in q for q in result["questions"]))

    def test_abroad_uses_the_later_available_month(self):
        # August 2027 is fine for a Singapore role (available from 2027-07).
        self.assertEqual(outcome(run(start_kind="fixed_date", start_date="2027-08",
                                     location_country="Singapore"), "start"), "pass")

    def test_abroad_fixed_date_six_months_late_asks(self):
        self.assertEqual(outcome(run(start_kind="fixed_date", start_date="2027-10",
                                     location_country="Singapore"), "start"), "ask")

    def test_fixed_date_without_a_date_is_treated_as_unknown(self):
        self.assertEqual(outcome(run(start_kind="fixed_date", start_date=None), "start"), "pass")

    def test_flexible_passes(self):
        self.assertEqual(outcome(run(start_kind="flexible"), "start"), "pass")

    def test_unknown_passes(self):
        self.assertEqual(outcome(run(start_kind="unknown"), "start"), "pass")

    def test_bad_month_string_is_ignored(self):
        self.assertEqual(outcome(run(start_kind="fixed_date", start_date="June 2027"), "start"), "pass")


class GraduationRule(unittest.TestCase):
    def test_empty_passes(self):
        self.assertEqual(outcome(run(graduation_years=[]), "graduation"), "pass")

    def test_includes_2027_passes(self):
        self.assertEqual(outcome(run(graduation_years=[2026, 2027]), "graduation"), "pass")

    def test_excludes_2027_fails(self):
        result = run(graduation_years=[2025, 2026])
        self.assertEqual(outcome(result, "graduation"), "fail")
        self.assertIn("2027", result["reasons"][0])


class DegreeRule(unittest.TestCase):
    def test_not_required_passes(self):
        self.assertEqual(outcome(run(degree_completed_required=False), "degree"), "pass")

    def test_unknown_passes(self):
        self.assertEqual(outcome(run(degree_completed_required=None), "degree"), "pass")

    def test_required_fails(self):
        self.assertEqual(outcome(run(degree_completed_required=True), "degree"), "fail")


class LanguageRule(unittest.TestCase):
    def test_thai_and_english_pass(self):
        self.assertEqual(outcome(run(required_languages=["Thai", "English"]), "language"), "pass")

    def test_case_insensitive(self):
        self.assertEqual(outcome(run(required_languages=["thai", "ENGLISH"]), "language"), "pass")

    def test_other_required_language_fails(self):
        result = run(required_languages=["Chinese"])
        self.assertEqual(outcome(result, "language"), "fail")
        self.assertIn("Chinese", result["reasons"][0])

    def test_required_test_score_asks(self):
        result = run(required_test_scores=["TOEIC 600"])
        self.assertEqual(outcome(result, "language"), "ask")
        self.assertIn("TOEIC 600", result["questions"][0])


class LocationRule(unittest.TestCase):
    def test_bangkok_passes(self):
        self.assertEqual(outcome(run(location_city="Bangkok", location_country="Thailand"), "location"), "pass")

    def test_bangkok_metro_city_passes(self):
        self.assertEqual(outcome(run(location_city="Nonthaburi", location_country="Thailand"), "location"), "pass")

    def test_singapore_passes(self):
        self.assertEqual(outcome(run(location_country="Singapore"), "location"), "pass")

    def test_hong_kong_asks(self):
        result = run(location_city="Hong Kong", location_country="Hong Kong")
        self.assertEqual(outcome(result, "location"), "ask")
        self.assertIn("Hong Kong", result["questions"][0])

    def test_upcountry_thailand_asks(self):
        self.assertEqual(outcome(run(location_city="Chiang Mai", location_country="Thailand"), "location"), "ask")

    def test_thailand_without_a_city_passes(self):
        self.assertEqual(outcome(run(location_country="Thailand"), "location"), "pass")

    def test_nothing_stated_passes(self):
        self.assertEqual(outcome(run(), "location"), "pass")

    def test_remote_anywhere_passes(self):
        self.assertEqual(outcome(run(location_country="Japan", remote=True), "location"), "pass")


class JobTypeRule(unittest.TestCase):
    def test_graduate_programme_passes(self):
        self.assertEqual(outcome(run(job_type="graduate_programme"), "job_type"), "pass")

    def test_full_time_passes(self):
        self.assertEqual(outcome(run(job_type="full_time"), "job_type"), "pass")

    def test_unknown_passes(self):
        self.assertEqual(outcome(run(job_type="unknown"), "job_type"), "pass")

    def test_internship_asks(self):
        self.assertEqual(outcome(run(job_type="internship"), "job_type"), "ask")

    def test_contract_asks(self):
        self.assertEqual(outcome(run(job_type="contract"), "job_type"), "ask")

    def test_part_time_fails(self):
        self.assertEqual(outcome(run(job_type="part_time"), "job_type"), "fail")


class OtherRule(unittest.TestCase):
    def test_none_passes(self):
        self.assertEqual(outcome(run(other_hard_requirements=[]), "other"), "pass")

    def test_each_item_becomes_a_question(self):
        result = run(other_hard_requirements=["A driving licence", "Relocation to Rayong"])
        self.assertEqual(outcome(result, "other"), "ask")
        self.assertEqual(len([q for q in result["questions"] if "requires" in q]), 2)

    def test_a_trailing_period_does_not_produce_a_double_period(self):
        result = run(other_hard_requirements=["Thai nationality required."])
        question = result["questions"][0]
        self.assertNotIn("..", question)
        self.assertTrue(question.endswith("Do you meet it?"))
        self.assertIn("Thai nationality required. Do you meet it?", question)


class WorstOutcomeWins(unittest.TestCase):
    def test_fail_beats_ask_and_keeps_both_fail_reasons(self):
        # Deliveree shape: GPA 2.70 plus degree completed required, immediate start.
        result = run(gpa_floor=2.70, degree_completed_required=True, start_kind="immediate")
        self.assertEqual(result["result"], "fail")
        self.assertEqual(len(result["reasons"]), 2)
        self.assertEqual(len(result["questions"]), 1)  # the immediate-start question survives

    def test_ask_beats_pass(self):
        result = run(start_kind="immediate", location_country="Hong Kong")
        self.assertEqual(result["result"], "ask")
        self.assertEqual(len(result["questions"]), 2)

    def test_clean_job_passes_with_no_messages(self):
        result = run(start_kind="fixed_date", start_date="2027-06", location_city="Bangkok",
                     location_country="Thailand", job_type="graduate_programme",
                     min_years_experience=0, graduation_years=[2027],
                     required_languages=["Thai", "English"])
        self.assertEqual(result["result"], "pass")
        self.assertEqual(result["reasons"], [])
        self.assertEqual(result["questions"], [])


if __name__ == "__main__":
    unittest.main()
