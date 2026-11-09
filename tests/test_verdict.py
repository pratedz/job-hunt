import unittest

from _util import job, memory_conn

from jobhunt import score as score_mod, store


def decide(love, good_at, needs, paid, gate="pass"):
    return score_mod.decide({"love": love, "good_at": good_at, "needs": needs, "paid": paid}, gate)


class Verdicts(unittest.TestCase):
    def test_all_four_at_least_three_is_centre(self):
        self.assertEqual(decide(3, 3, 3, 3)["verdict"], "CENTRE")

    def test_one_unknown_with_the_rest_high_is_hold(self):
        self.assertEqual(decide(5, 4, None, 4)["verdict"], "HOLD")

    def test_one_low_score_is_low_even_with_an_unknown(self):
        self.assertEqual(decide(2, None, 4, 4)["verdict"], "LOW")

    def test_all_unknown_is_hold_not_centre(self):
        result = decide(None, None, None, None)
        self.assertEqual(result["verdict"], "HOLD")
        self.assertIsNone(result["weakest_circle"])
        self.assertIsNone(result["mean_score"])

    def test_gate_fail_is_filtered_whatever_the_scores(self):
        self.assertEqual(decide(5, 5, 5, 5, gate="fail")["verdict"], "FILTERED")

    def test_gate_ask_does_not_filter(self):
        self.assertEqual(decide(5, 4, 3, 3, gate="ask")["verdict"], "CENTRE")


class RankingFields(unittest.TestCase):
    def test_weakest_is_the_lowest_known_circle(self):
        result = decide(5, 3, 4, 2)
        self.assertEqual(result["weakest_circle"], "paid")
        self.assertEqual(result["weakest_score"], 2)

    def test_weakest_tie_goes_to_the_first_circle_in_order(self):
        self.assertEqual(decide(3, 3, 4, 4)["weakest_circle"], "love")

    def test_unknowns_are_left_out_of_the_mean(self):
        self.assertAlmostEqual(decide(5, 5, None, None)["mean_score"], 5.0)
        self.assertAlmostEqual(decide(5, 4, 3, None)["mean_score"], 4.0)


class RankingOrder(unittest.TestCase):
    """The SQL ordering in store.RANKING_SQL, exercised through a real database."""

    def setUp(self):
        self.conn = memory_conn(self)
        self.ids = {}
        for name, title in [("centre", "Centre Job"), ("hold", "Hold Job"),
                            ("low", "Low Job"), ("filtered", "Filtered Job"),
                            ("all_null", "All Null Job")]:
            self.ids[name], _ = store.add_job(self.conn, job(title=title, company=name))

    def score(self, name, love, good_at, needs, paid, gate="pass"):
        record = score_mod.decide(
            {"love": love, "good_at": good_at, "needs": needs, "paid": paid}, gate)
        store.insert_score(self.conn, self.ids[name], {
            "status": "scored", "gate_result": gate, "verdict": record["verdict"],
            "love": love, "good_at": good_at, "needs": needs, "paid": paid,
            "weakest_circle": record["weakest_circle"],
            "weakest_score": record["weakest_score"], "mean_score": record["mean_score"],
            "created_at": store.utc_now(), "scored_at": store.utc_now(),
        })

    def test_group_order(self):
        self.score("low", 2, 5, 5, 5)
        self.score("centre", 3, 3, 3, 3)
        self.score("hold", 5, None, 4, 4)
        self.score("all_null", None, None, None, None)
        self.score("filtered", 5, 5, 5, 5, gate="fail")
        order = [row["company"] for row in store.ranked(self.conn)]
        self.assertEqual(order, ["centre", "hold", "all_null", "low", "filtered"])

    def test_within_centre_the_stronger_weakest_circle_comes_first(self):
        self.score("centre", 3, 3, 3, 3)
        self.score("low", 5, 5, 5, 5, gate="pass")
        self.score("hold", 4, 4, 4, 4)
        order = [row["company"] for row in store.ranked(self.conn) if row["verdict"] == "CENTRE"]
        self.assertEqual(order, ["low", "hold", "centre"])

    def test_error_rows_are_hidden_from_the_list(self):
        store.insert_score(self.conn, self.ids["centre"], {
            "status": "error", "error": "codex timed out", "created_at": store.utc_now()})
        self.assertEqual(store.ranked(self.conn), [])


if __name__ == "__main__":
    unittest.main()
