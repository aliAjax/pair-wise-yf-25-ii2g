import unittest

from scoring import (
    DIMENSIONS,
    WEIGHTS,
    ScoreError,
    composite_score,
    dimension_averages,
    validate_dimension,
    validate_scores,
)


class ScoringTests(unittest.TestCase):
    def test_weights_cover_three_dimensions(self):
        self.assertEqual(set(DIMENSIONS), {"innovation", "rigor", "reproducibility"})
        self.assertEqual(WEIGHTS["innovation"], 0.40)
        self.assertEqual(WEIGHTS["rigor"], 0.35)
        self.assertEqual(WEIGHTS["reproducibility"], 0.25)
        self.assertAlmostEqual(sum(WEIGHTS.values()), 1.0)

    def test_validate_dimension_accepts_1_to_5_only(self):
        self.assertEqual(validate_dimension("innovation", 1), 1)
        self.assertEqual(validate_dimension("rigor", 5), 5)
        for bad in (0, 6, "3", 3.0, True, None):
            with self.assertRaises(ScoreError):
                validate_dimension("rigor", bad)
        with self.assertRaises(ScoreError):
            validate_dimension("unknown", 3)

    def test_validate_scores_requires_all_three_for_submission(self):
        full = {"innovation": 4, "rigor": 3, "reproducibility": 5}
        self.assertEqual(validate_scores(full), full)
        for partial in (
            {"rigor": 3, "reproducibility": 5},
            {"innovation": 4},
            {},
        ):
            with self.assertRaises(ScoreError) as ctx:
                validate_scores(partial)
            self.assertEqual(ctx.exception.code, "missing_dimension")
        with self.assertRaises(ScoreError):
            validate_scores([])

    def test_validate_scores_partial_allows_draft(self):
        self.assertEqual(validate_scores({"innovation": 2}, partial=True), {"innovation": 2})
        self.assertEqual(validate_scores({}, partial=True), {})
        with self.assertRaises(ScoreError):
            validate_scores({"innovation": 9}, partial=True)

    def test_composite_score_is_weighted_sum(self):
        # 4*0.4 + 3*0.35 + 5*0.25 = 1.6 + 1.05 + 1.25 = 3.9
        self.assertEqual(composite_score({"innovation": 4, "rigor": 3, "reproducibility": 5}), 3.9)
        self.assertEqual(composite_score({"innovation": 5, "rigor": 5, "reproducibility": 5}), 5.0)
        self.assertEqual(composite_score({"innovation": 1, "rigor": 1, "reproducibility": 1}), 1.0)

    def test_dimension_averages(self):
        avgs = dimension_averages([
            {"innovation": 5, "rigor": 3, "reproducibility": 1},
            {"innovation": 3, "rigor": 3, "reproducibility": 3},
        ])
        self.assertEqual(avgs, {"innovation": 4.0, "rigor": 3.0, "reproducibility": 2.0})
        self.assertIsNone(dimension_averages([]))


if __name__ == "__main__":
    unittest.main()
