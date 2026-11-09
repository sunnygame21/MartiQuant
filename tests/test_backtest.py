"""martiquant.backtest on synthetic bars: TP round, dd stop + halt + resume, basket drain."""

import unittest

import numpy as np

from martiquant.backtest import BacktestConfig, merge_bars, run
from martiquant.config import LadderParams, RiskLimits

PARAMS = LadderParams(name="T", ladder_drops_pct=(-10.0,), size_multipliers_usd=(2.0,),
                      tp_pct=1.0, first_entry_usd_default=100.0)
OFF = RiskLimits(max_level_any_coin_before_pause=99, max_coins_at_deep_level=99)


def bars(closes, lows=None, highs=None, t0=0):
    c = np.asarray(closes, dtype=float)
    return {"t": t0 + np.arange(len(c), dtype=np.int64) * 60_000,
            "h": np.asarray(highs if highs is not None else c, dtype=float),
            "l": np.asarray(lows if lows is not None else c, dtype=float), "c": c}


def cfg(**kw):
    base = dict(params=PARAMS, basket=("AAA",), risk=OFF, equity_usd=1000.0,
                first_entry_usd=100.0, maker_fee_rate=0.0, taker_fee_rate=0.0,
                stop_slippage=0.0)
    base.update(kw)
    return BacktestConfig(**base)


class BacktestTests(unittest.TestCase):
    def test_entry_then_take_profit(self):
        # bar0 posts entry at 100; bar1 fills it; bar2 high 101 hits TP (+1%)
        r = run(cfg(), merge_bars({"AAA": bars([100, 100, 100.5], highs=[100, 100, 101])}, ["AAA"]))
        self.assertEqual(r["rounds_closed"], 1)
        self.assertAlmostEqual(r["realized_price_pnl"], 1.0)
        self.assertEqual(r["rounds"][0]["exit"], "tp")

    def test_dd_stop_flattens_halts_and_resumes(self):
        # entry at 100 (1 coin of $100), add at 90 ($200): 3.222 coins, avg 93.1
        # at 60 the loss is ~ -107 on 1000 equity -> -10.7% < -5% stop
        n_halt = 3 * 1440  # 3 days of flat bars at 60
        closes = [100, 100, 90, 60] + [60] * n_halt
        r = run(cfg(dd_stop_pct=5.0, cooldown_days=1.0),
                merge_bars({"AAA": bars(closes)}, ["AAA"]))
        self.assertEqual(r["n_stops"], 1)
        stop = r["stops"][0]
        self.assertAlmostEqual(stop["eq_after"], 1000 + (100 / 100 + 200 / 90) * 60 - 300, places=6)
        self.assertEqual(r["rounds"][0]["exit"], "dd_stop")
        # halted for a day, then re-entered at 60 (a fresh round) and still holding it
        self.assertFalse(r["halted_at_end"])
        self.assertGreater(r["orders_placed"], 3)

    def test_halt_forever_without_cooldown(self):
        closes = [100, 100, 90, 60] + [60] * 100
        r = run(cfg(dd_stop_pct=5.0, cooldown_days=None), merge_bars({"AAA": bars(closes)}, ["AAA"]))
        self.assertTrue(r["halted_at_end"])
        self.assertEqual(r["open_upl"], 0.0)

    def test_dropped_coin_finishes_round_then_goes_quiet(self):
        a = bars([100, 100, 100, 100, 101, 101, 101], highs=[100, 100, 100, 100, 101, 101, 101])
        b = bars([10] * 7)
        # AAA in position from bar1; dropped at bar2; its TP still fills at bar4; never reopens
        r = run(cfg(basket=("AAA", "BBB"), rebalances=[(2 * 60_000, ("BBB",))]),
                merge_bars({"AAA": a, "BBB": b}, ["AAA", "BBB"]))
        a_rounds = [x for x in r["rounds"] if x["coin"] == "AAA"]
        self.assertEqual(len(a_rounds), 1)
        self.assertEqual(a_rounds[0]["exit"], "tp")
        self.assertEqual(r["open_upl"], 0.0)

    def test_delisted_coin_is_settled_and_not_reopened(self):
        closes = [100, 100, 95] + [95] * 50
        r = run(cfg(delist_ms={"AAA": 3 * 60_000}), merge_bars({"AAA": bars(closes)}, ["AAA"]))
        self.assertEqual([x["exit"] for x in r["rounds"]], ["delisted"])
        self.assertAlmostEqual(r["realized_price_pnl"], -5.0)

    def test_deep_stop_only_on_full_ladder_at_hour_close(self):
        # entry 100 (bar1), add at 90 fills (bar2) -> full ladder; then 70 for the rest of
        # the hour. Stop at 25% below first entry fires on the hour's last bar (index 59).
        closes = [100, 100, 90] + [70] * 57 + [70] * 120
        r = run(cfg(deep_stop_pct=25.0), merge_bars({"AAA": bars(closes)}, ["AAA"]))
        self.assertEqual(r["n_deep_stops"], 1)
        self.assertEqual(r["deep_stops"][0]["ts_ms"], 59 * 60_000)
        self.assertAlmostEqual(r["deep_stops"][0]["pnl"], (1 + 200 / 90) * 70 - 300)
        self.assertEqual([x["exit"] for x in r["rounds"]], ["deep_stop"])
        self.assertEqual(r["open_upl"], 0.0)  # cooldown: no new round in the remaining bars

    def test_deep_stop_ignores_partial_ladder(self):
        # 2-level ladder at -10%/-60%: at 70 only the first add filled, so no stop
        p = LadderParams(name="T2", ladder_drops_pct=(-10.0, -60.0), size_multipliers_usd=(2.0, 4.0),
                         tp_pct=1.0, first_entry_usd_default=100.0)
        closes = [100, 100, 90] + [70] * 120
        r = run(cfg(params=p, deep_stop_pct=25.0), merge_bars({"AAA": bars(closes)}, ["AAA"]))
        self.assertEqual(r["n_deep_stops"], 0)

    def test_entry_block_stops_new_rounds_in_a_collapse(self):
        # one bar per day, -2%/day; each day's high bounces 4% over the prior close, so
        # rounds open on one bar and take profit (+1%) on the next, all the way down.
        n = 60
        c = 100 * 0.98 ** np.arange(n)
        hi = np.r_[c[0], c[:-1] * 1.04]
        day = lambda closes, highs: {"t": np.arange(n, dtype=np.int64) * 86_400_000,
                                     "h": highs, "l": closes, "c": closes}
        free = run(cfg(), merge_bars({"AAA": day(c, hi)}, ["AAA"]))
        gated = run(cfg(entry_block_ret30_pct=30.0), merge_bars({"AAA": day(c, hi)}, ["AAA"]))
        last = lambda r: max(x["close_time_ms"] for x in r["rounds"]) // 86_400_000
        self.assertGreater(last(free), 55)
        # 30-day return first known on day 31 (closes of day 30 vs day 0: -45%)
        self.assertLessEqual(last(gated), 33)
        self.assertLess(gated["rounds_closed"], free["rounds_closed"])

    def test_merge_breaks_time_ties_by_basket_order(self):
        t, coins, *_ = merge_bars({"B": bars([1, 1]), "A": bars([1, 1])}, ["A", "B"])
        self.assertEqual(coins, ["A", "B", "A", "B"])
        self.assertEqual(t, [0, 0, 60_000, 60_000])


if __name__ == "__main__":
    unittest.main()
