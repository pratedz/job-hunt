import unittest

from _util import paid, profile

from jobhunt import paid as paid_mod

# pinned: these tests assert exact anchor behaviour, not today's profile values
P = profile(**{"paid.anchors_thb_month": [30000, 40000, 50000, 65000]})


class StatedSalaries(unittest.TestCase):
    def test_thb_month_30_to_40k_scores_2(self):
        result = paid_mod.score(paid(30000, 40000, "THB", "month", True), P)
        self.assertEqual(result.score, 2)          # midpoint 35k reaches one anchor
        self.assertFalse(result.estimated)
        self.assertAlmostEqual(result.thb_month_mid, 35000)

    def test_thb_month_below_the_first_anchor_scores_1(self):
        result = paid_mod.score(paid(20000, 25000, "THB", "month", True), P)
        self.assertEqual(result.score, 1)

    def test_thb_month_at_the_top_anchor_scores_5(self):
        result = paid_mod.score(paid(65000, 65000, "THB", "month", True), P)
        self.assertEqual(result.score, 5)

    def test_single_figure_is_used_for_both_bounds(self):
        result = paid_mod.score(paid(50000, None, "THB", "month", True), P)
        self.assertAlmostEqual(result.thb_month_mid, 50000)
        self.assertEqual(result.score, 4)          # 50k reaches 30k/40k/50k anchors

    def test_annual_sgd_is_converted_and_divided_by_twelve(self):
        # SGD 48,000 a year = SGD 4,000 a month = THB 100,000 at the profile rate.
        result = paid_mod.score(paid(48000, 48000, "SGD", "year", True), P)
        self.assertAlmostEqual(result.thb_month_mid, 100000)
        self.assertEqual(result.score, 5)

    def test_annual_thb(self):
        result = paid_mod.score(paid(600000, 600000, "THB", "year", True), P)
        self.assertAlmostEqual(result.thb_month_mid, 50000)
        self.assertEqual(result.score, 4)

    def test_min_and_max_the_wrong_way_round_are_swapped(self):
        result = paid_mod.score(paid(45000, 35000, "THB", "month", True), P)
        self.assertAlmostEqual(result.thb_month_mid, 40000)

    def test_lowercase_currency_code_works(self):
        result = paid_mod.score(paid(4000, 4000, "sgd", "month", True), P)
        self.assertEqual(result.score, 5)


class EstimatesAndGaps(unittest.TestCase):
    def test_estimate_is_flagged(self):
        result = paid_mod.score(paid(30000, 45000, "THB", "month", False, "market rate"), P)
        self.assertTrue(result.estimated)
        self.assertEqual(result.score, 2)          # 37.5k
        self.assertIn("estimate", result.reason)

    def test_everything_null_gives_no_score(self):
        result = paid_mod.score(paid(), P)
        self.assertIsNone(result.score)
        self.assertFalse(result.estimated)
        self.assertIsNone(result.thb_month_mid)

    def test_missing_paid_object_gives_no_score(self):
        self.assertIsNone(paid_mod.score(None, P).score)

    def test_unknown_currency_gives_no_score(self):
        result = paid_mod.score(paid(30000, 40000, "MYR", "month", True), P)
        self.assertIsNone(result.score)
        self.assertIn("MYR", result.reason)

    def test_no_currency_gives_no_score(self):
        self.assertIsNone(paid_mod.score(paid(30000, 40000, None, "month", True), P).score)

    def test_missing_period_is_treated_as_monthly(self):
        result = paid_mod.score(paid(30000, 30000, "THB", None, True), P)
        self.assertAlmostEqual(result.thb_month_mid, 30000)

    def test_usd_and_hkd_rates_are_used(self):
        self.assertAlmostEqual(paid_mod.score(paid(2000, 2000, "USD", "month", True), P).thb_month_mid, 66000)
        self.assertAlmostEqual(paid_mod.score(paid(10000, 10000, "HKD", "month", True), P).thb_month_mid, 42000)


if __name__ == "__main__":
    unittest.main()
