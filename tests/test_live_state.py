"""state.load: replayable snapshots resume at any age, legacy ones only if fresh."""

import json
import tempfile
import time
import unittest
from pathlib import Path

from martiquant.config import B_PERIOD, BasketConfig, RiskLimits
from martiquant.live import state as live_state
from martiquant.paper_broker import PaperBroker
from martiquant.strategy import LadderStrategy


def fresh():
    strat = LadderStrategy(B_PERIOD, BasketConfig(include=("SOL", "XRP")), RiskLimits(), 155.0)
    return strat, PaperBroker()


class LoadAgeTests(unittest.TestCase):
    def dump(self, d: Path, age_sec: float, with_cursor: bool) -> Path:
        strat, broker = fresh()
        strat.on_fill("SOL", "buy", 100.0, 1.5, "0")
        p = d / "state.json"
        live_state.dump(p, strat, broker, {"price_pnl": 1.0}, {"SOL": 100.0, "XRP": 1.0},
                        {"SOL": 60_000, "XRP": 60_000} if with_cursor else {}, {})
        payload = json.loads(p.read_text())
        payload["ts_ms"] -= int(age_sec * 1000)
        if not with_cursor:
            payload.pop("last_bar_ms")
        p.write_text(json.dumps(payload))
        return p

    def test_week_old_snapshot_with_cursor_resumes(self):
        with tempfile.TemporaryDirectory() as d:
            p = self.dump(Path(d), 7 * 86_400, with_cursor=True)
            strat, broker = fresh()
            restored = live_state.load(p, strat, broker)
            self.assertIsNotNone(restored)
            self.assertEqual(strat.states["SOL"].level, 1)
            self.assertEqual(restored[2]["last_bar_ms"], {"SOL": 60_000, "XRP": 60_000})

    def test_old_legacy_snapshot_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            p = self.dump(Path(d), 2 * 3600, with_cursor=False)
            self.assertIsNone(live_state.load(p, *fresh()))

    def test_fresh_legacy_snapshot_still_loads(self):
        with tempfile.TemporaryDirectory() as d:
            p = self.dump(Path(d), 60, with_cursor=False)
            self.assertIsNotNone(live_state.load(p, *fresh()))


if __name__ == "__main__":
    unittest.main()
