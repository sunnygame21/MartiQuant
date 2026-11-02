"""Scoring must not depend on input order; gates must reject young and delisted listings."""

import unittest

import numpy as np

from basket_scoring.pipeline import gate
from basket_scoring.score import _percentile_rank

DAY = 86_400_000


class PercentileRankTests(unittest.TestCase):
    def test_ties_share_rank_and_order_does_not_matter(self):
        vals = np.array([365.0, 120.0, 365.0, 365.0, 40.0])
        r = _percentile_rank(vals, higher_is_better=False)
        self.assertEqual(r[0], r[2])
        self.assertEqual(r[2], r[3])
        self.assertEqual(r[4], 100.0)
        perm = np.array([3, 0, 4, 2, 1])
        np.testing.assert_allclose(_percentile_rank(vals[perm], False), r[perm])

    def test_distinct_values_span_0_to_100(self):
        r = _percentile_rank(np.array([3.0, 1.0, 2.0]), higher_is_better=True)
        np.testing.assert_allclose(r, [100.0, 0.0, 50.0])


class GateTests(unittest.TestCase):
    def test_gates(self):
        now = 1_000 * DAY
        info = {
            "OLD": {"onboard_ms": 0, "delivery_ms": 10_000 * DAY},
            "YOUNG": {"onboard_ms": 500 * DAY, "delivery_ms": 10_000 * DAY},
            "DEAD": {"onboard_ms": 0, "delivery_ms": 900 * DAY},
        }
        approved, rejected = gate(("OLD", "YOUNG", "DEAD", "NONE"), info, now)
        self.assertEqual(approved, ["OLD"])
        self.assertEqual(set(rejected), {"YOUNG", "DEAD", "NONE"})
        self.assertIn("delisted", rejected["DEAD"])
        # as of a date before the settlement, the same coin is still eligible
        self.assertEqual(gate(("DEAD",), info, 800 * DAY)[0], ["DEAD"])


if __name__ == "__main__":
    unittest.main()
