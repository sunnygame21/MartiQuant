"""Synthetic execution/accounting checks independent of historical profitability.

Run with ``python3 -m unittest discover -s tests -v``; no pytest dependency.
"""

import unittest
import numpy as np

from martiquant.research_engine import (
    MarketData,
    SearchConfig,
    normalized_weights,
    run_backtest,
)


def market(rows, coins=("TEST",)):
    """Rows are (open, high, low, close), repeated over independent coins."""
    bars = np.asarray(rows, dtype=np.float64)
    fields = [np.repeat(bars[:, k, None], len(coins), axis=1) for k in range(4)]
    return MarketData(
        times=np.arange(len(rows), dtype=np.int64) * 60_000,
        open=fields[0], high=fields[1], low=fields[2], close=fields[3], coins=coins,
    )


class ResearchEngineTests(unittest.TestCase):
    def config(self, **kwargs):
        defaults = dict(
            mode="qty", multiplier=2.0, drops_pct=(-10.0, -20.0), tp_pct=1.0,
            coin_budget_fraction=0.5, initial_equity=1000.0,
            maker_fee=0.0, taker_fee=0.0, hard_stop_pct=100.0,
        )
        defaults.update(kwargs)
        return SearchConfig(**defaults)

    def assert_balance(self, result, initial=1000.0):
        self.assertAlmostEqual(
            initial + result["net_profit"],
            result["ending_cash"] + result["open_cost"] + result["open_pnl"],
            places=7,
        )
        self.assertGreaterEqual(result["ending_cash"], -1e-8)
        self.assertGreaterEqual(result["open_qty_total"], -1e-8)

    def test_quantity_and_cost_rules_use_equal_total_budget(self):
        qty = normalized_weights(self.config())
        cost = normalized_weights(self.config(mode="cost"))
        np.testing.assert_allclose(qty, np.array([1.0, 1.8, 4.8]) / 7.6)
        np.testing.assert_allclose(cost, np.array([1.0, 2.0, 6.0]) / 9.0)
        self.assertAlmostEqual(float(sum(qty)), 1.0)
        self.assertAlmostEqual(float(sum(cost)), 1.0)

    def test_fixed_baseline_keeps_original_allocation_proportions(self):
        weights = normalized_weights(self.config(mode="fixed", drops_pct=(-5,-10,-15,-25)))
        np.testing.assert_allclose(weights, np.array([1, 2.78, 10.46, 38.68, 80]) / 132.92)

    def test_one_bar_has_no_information_for_a_resting_entry(self):
        with self.assertRaises(ValueError):
            run_backtest(market([(100, 110, 60, 80)]), self.config())

    def test_gap_below_resting_entry_fills_at_conservative_limit(self):
        rows = [(100,100,100,100), (80,90,70,80)]
        result = run_backtest(market(rows), self.config())
        entry = 1000 * 0.5 * normalized_weights(self.config())[0]
        self.assertEqual(result["buy_count"], 1)
        self.assertAlmostEqual(result["open_cost"], entry)
        self.assertAlmostEqual(result["open_qty_total"], entry / 100)
        self.assert_balance(result)

    def test_wide_entry_bar_cannot_fill_future_adds_or_take_profit(self):
        result = run_backtest(market([(100,100,100,100), (100,120,60,100)]), self.config())
        self.assertEqual(result["buy_count"], 1)
        self.assertEqual(result["rounds"], 0)
        self.assertGreater(result["open_qty_total"], 0)
        self.assertAlmostEqual(result["net_profit"], 0)
        self.assert_balance(result)

    def test_wide_add_bar_cannot_take_profit_from_new_average(self):
        rows = [(100,100,100,100), (100,100,100,100), (100,100,80,90)]
        result = run_backtest(market(rows), self.config())
        self.assertEqual(result["buy_count"], 2)
        self.assertEqual(result["rounds"], 0)
        self.assertGreater(result["open_qty_total"], 0)
        self.assert_balance(result)

    def test_take_profit_closes_once_and_never_oversells(self):
        rows = [(100,100,100,100), (100,100,100,100), (100,102,100,102)]
        result = run_backtest(market(rows), self.config())
        entry = 1000 * 0.5 * normalized_weights(self.config())[0]
        self.assertEqual(result["buy_count"], 1)
        self.assertEqual(result["rounds"], 1)
        self.assertAlmostEqual(result["open_qty_total"], 0)
        self.assertAlmostEqual(result["net_profit"], entry * 0.01)
        self.assert_balance(result)

    def test_entry_and_exit_fees_reduce_realized_profit(self):
        rows = [(100,100,100,100), (100,100,100,100), (100,102,100,102)]
        cfg = self.config(maker_fee=0.002)
        result = run_backtest(market(rows), cfg)
        entry = 1000 * 0.5 / 1.002 * normalized_weights(cfg)[0]
        expected_fees = (entry + entry * 1.01) * 0.002
        self.assertAlmostEqual(result["fees"], expected_fees)
        self.assertAlmostEqual(result["net_profit"], entry * 0.01 - expected_fees)
        self.assert_balance(result)

    def test_unclosed_losses_are_included_in_final_equity(self):
        rows = [(100,100,100,100), (100,100,80,80)]
        result = run_backtest(market(rows), self.config())
        entry = 1000 * 0.5 * normalized_weights(self.config())[0]
        self.assertAlmostEqual(result["open_pnl"], -entry * 0.2)
        self.assertAlmostEqual(result["net_profit"], -entry * 0.2)
        self.assert_balance(result)

    def test_fees_cannot_make_cash_negative_at_full_budget(self):
        rows = [(100,100,100,100), (100,100,100,100), (90,90,90,90), (80,80,80,80)]
        cfg = self.config(coin_budget_fraction=1.0, maker_fee=0.002, taker_fee=0.004)
        result = run_backtest(market(rows), cfg)
        self.assertEqual(result["buy_count"], 3)
        self.assertLessEqual(result["peak_committed_usd"], 1000 + 1e-7)
        self.assert_balance(result)

    def test_multiple_coins_share_capital_without_borrowing(self):
        rows = [(100,100,100,100), (100,100,100,100), (90,90,90,90), (80,80,80,80)]
        result = run_backtest(
            market(rows, ("A", "B", "C")),
            self.config(coin_budget_fraction=1 / 3, maker_fee=0.002),
        )
        self.assertLessEqual(result["peak_committed_usd"], 1000 + 1e-7)
        self.assert_balance(result)

    def test_overallocated_portfolio_is_rejected(self):
        with self.assertRaises(ValueError):
            run_backtest(
                market([(100,100,100,100)] * 2, ("A", "B", "C")),
                self.config(coin_budget_fraction=0.5),
            )

    def test_hard_stop_liquidates_and_does_not_restart(self):
        rows = [(100,100,100,100), (100,100,100,100), (80,80,80,80), (70,70,70,70)]
        cfg = self.config(drops_pct=(-90,), multiplier=1, coin_budget_fraction=1, hard_stop_pct=5)
        result = run_backtest(market(rows), cfg)
        self.assertTrue(result["hard_stopped"])
        self.assertEqual(result["buy_count"], 1)
        self.assertAlmostEqual(result["open_qty_total"], 0)
        self.assertAlmostEqual(result["open_cost"], 0)
        self.assertLess(result["net_profit"], 0)
        self.assert_balance(result)

    def test_low_proxy_detects_flash_crash_hidden_by_close_marks(self):
        rows = [(100,100,100,100), (100,100,100,100), (100,100,50,100)]
        cfg = self.config(drops_pct=(-90,), multiplier=1, coin_budget_fraction=1, hard_stop_pct=5)
        close_result = run_backtest(market(rows), cfg)
        stress_result = run_backtest(market(rows), self.config(
            drops_pct=(-90,), multiplier=1, coin_budget_fraction=1,
            hard_stop_pct=5, low_proxy=True,
        ))
        self.assertFalse(close_result["hard_stopped"])
        self.assertTrue(stress_result["hard_stopped"])
        self.assertAlmostEqual(stress_result["open_qty_total"], 0)
        self.assertLess(stress_result["net_profit"], close_result["net_profit"])
        self.assert_balance(stress_result)

    def test_original_level_four_breaker_pauses_new_coin_after_three_adds(self):
        rows = [(p,p,p,p) for p in (100,100,95,90,85,85)]
        data = market(rows, ("A", "B"))
        # B gaps upward until A has reached entry plus three additions.
        # Only the final bar offers a B entry at its previous closing price.
        for field in (data.open, data.high, data.low, data.close):
            field[:, 1] = (100,110,120,130,140,140)
        cfg = self.config(drops_pct=(-5,-10,-15,-25), risk_breakers=True)
        result = run_backtest(data, cfg)
        self.assertEqual(result["buy_count"], 4)
        self.assert_balance(result)

    def test_slice_does_not_use_prices_before_its_start(self):
        data = market([(1000,1000,1000,1000), (100,100,100,100), (100,100,100,100)])
        sliced = run_backtest(data, self.config(), start=1)
        isolated = run_backtest(market([(100,100,100,100)] * 2), self.config())
        self.assertEqual(sliced["buy_count"], isolated["buy_count"])
        self.assertAlmostEqual(sliced["open_qty_total"], isolated["open_qty_total"])
        self.assertAlmostEqual(sliced["net_profit"], isolated["net_profit"])


if __name__ == "__main__":
    unittest.main()
