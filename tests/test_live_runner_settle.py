"""Resuming with a coin dropped from the basket: settle it explicitly or refuse to start."""

import csv
import json
import tempfile
import unittest
from pathlib import Path

from martiquant.config import B_PERIOD, BasketConfig, RiskLimits
from martiquant.live import state as live_state
from martiquant.live.runner import SETTLE_FEE_RATE, LiveRunner
from martiquant.paper_broker import PaperBroker
from martiquant.strategy import LadderStrategy


class SettleDroppedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        old = self.root / "old"
        old.mkdir()
        strat = LadderStrategy(B_PERIOD, BasketConfig(include=("SOL", "TON")), RiskLimits(), 155.0)
        broker = PaperBroker()
        strat.on_fill("SOL", "buy", 100.0, 1.55, "60000")
        strat.on_fill("TON", "buy", 1.59, 97.5, "60000")
        broker.reconcile(strat.desired_orders({"SOL": 100.0, "TON": 1.59}))
        tracker = {c: {"open_time_ms": 60_000, "first_entry_px": px, "max_level": 1, "fee_paid": -0.03}
                   for c, px in (("SOL", 100.0), ("TON", 1.59))}
        live_state.dump(old / "state.json", strat, broker,
                        {"price_pnl": 10.0, "fee": -1.0, "peak_eq": 100_009.0, "worst_upl": 0.0,
                         "n_rounds_closed": 5, "n_fills": 11},
                        {"SOL": 101.0, "TON": 1.59}, {"SOL": 120_000, "TON": 120_000}, tracker)
        self.old = old

    def tearDown(self):
        self.tmp.cleanup()

    def runner(self, settle=()):
        return LiveRunner(B_PERIOD, BasketConfig(include=("SOL", "AVAX")), RiskLimits(), 155.0,
                          100_000.0, 2.0, self.root / "new", resume_from=self.old, settle=settle)

    def test_refuses_to_drop_open_round_silently(self):
        with self.assertRaises(SystemExit) as cm:
            self.runner()
        self.assertIn("--settle TON", str(cm.exception))

    def test_settle_closes_at_last_mark_and_logs_round(self):
        r = self.runner(settle=("TON",))
        self.assertNotIn("TON", r.strategy.states)
        self.assertNotIn("TON", r.mark)
        self.assertFalse(any(o.coin == "TON" for o in r.broker.open_orders.values()))
        self.assertTrue(r.strategy.states["SOL"].in_position)  # kept coin untouched
        self.assertIn("AVAX", r.strategy.states)  # new coin starts flat
        settle_fee = -1.59 * 97.5 * SETTLE_FEE_RATE
        self.assertAlmostEqual(r.totals["fee"], -1.0 + settle_fee)
        self.assertAlmostEqual(r.totals["price_pnl"], 10.0)  # closed at entry price
        self.assertEqual(r.totals["n_rounds_closed"], 6)
        rows = list(csv.DictReader(open(self.root / "new" / "rounds.csv")))
        self.assertEqual([x["coin"] for x in rows], ["TON"])
        self.assertAlmostEqual(float(rows[0]["realized_pnl"]), -0.03 + settle_fee)
        r.live_logger.close()

    def test_flat_dropped_coin_just_leaves(self):
        p = self.old / "state.json"
        payload = json.loads(p.read_text())
        payload["strategy_states"]["TON"].update(position_coin=0.0, cost_basis_usd=0.0, level=0,
                                                 first_entry_px=None, open_time=None)
        p.write_text(json.dumps(payload))
        r = self.runner()
        self.assertNotIn("TON", r.strategy.states)
        self.assertAlmostEqual(r.totals["fee"], -1.0)
        r.live_logger.close()


if __name__ == "__main__":
    unittest.main()
