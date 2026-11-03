"""monitor.run: alerts once on a problem, once on recovery, events every time."""

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from martiquant.live import monitor

NOW = 1_800_000_000.0


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.runs = Path(self.tmp.name)
        self.logs = self.runs / "_live_logs"
        self.logs.mkdir()
        (self.logs / "p1.cmd").write_text("python -m martiquant.live --tag p1")
        self.run_dir = self.runs / "live_20261101T000000Z_p1"
        self.run_dir.mkdir()
        (self.run_dir / "events.jsonl").write_text("")
        self.env = mock.patch.dict(os.environ, {"MARTIQUANT_FEISHU_WEBHOOK": ""})
        self.env.start()
        self.no_dotenv = mock.patch.object(monitor, "ROOT", self.runs)
        self.no_dotenv.start()
        self.info = {"SOL": {"onboard_ms": 0, "delivery_ms": monitor.LIVE_PERP_DELIVERY_MS,
                             "status": "TRADING"}}
        self.fetch = mock.patch.object(monitor, "fetch_symbol_info", side_effect=lambda: self.info)
        self.fetch_mock = self.fetch.start()

    def tearDown(self):
        self.fetch.stop()
        self.no_dotenv.stop()
        self.env.stop()
        self.tmp.cleanup()

    def write_state(self, written_at: float, last_bar_s: float):
        p = self.run_dir / "state.json"
        p.write_text(json.dumps({"last_bar_ms": {"SOL": int(last_bar_s * 1000)}}))
        os.utime(p, (written_at, written_at))

    def test_healthy_is_silent(self):
        self.write_state(NOW - 30, NOW - 90)
        self.assertEqual(monitor.run(self.runs, NOW), [])

    def test_stall_alerts_once_then_recovers(self):
        self.write_state(NOW - 900, NOW - 960)
        first = monitor.run(self.runs, NOW)
        self.assertEqual(len(first), 2)  # stalled + no_bars
        self.assertTrue(all(m.startswith("❌") for m in first))
        self.assertEqual(monitor.run(self.runs, NOW + 120), [])
        self.write_state(NOW + 200, NOW + 150)
        recovered = monitor.run(self.runs, NOW + 240)
        self.assertEqual(sorted(recovered), ["✅ MartiQuant [p1] no_bars recovered",
                                             "✅ MartiQuant [p1] stalled recovered"])
        self.assertIn("(no webhook)", (self.logs / "monitor.log").read_text())

    def test_crash_loop(self):
        self.write_state(NOW - 30, NOW - 90)
        stamps = [time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - m * 60)) for m in (5, 15, 25)]
        (self.logs / "p1.log").write_text(
            "".join(f"[watch {s}] [p1] dead, restarting: cmd\n" for s in stamps))
        self.assertEqual(monitor.run(self.runs, NOW),
                         ["❌ MartiQuant [p1] crash_loop: restarted 3x in the last hour"])

    def test_first_run_skips_history(self):
        self.write_state(NOW - 30, NOW - 90)
        (self.run_dir / "events.jsonl").write_text('{"kind": "backfill_failed", "error": "old"}\n')
        self.assertEqual(monitor.run(self.runs, NOW), [])

    def test_new_events_alert_each_time_and_partial_line_waits(self):
        self.write_state(NOW - 30, NOW - 90)
        monitor.run(self.runs, NOW - 60)
        ev = self.run_dir / "events.jsonl"
        ev.write_text('{"kind": "fill"}\n{"kind": "backfill_failed", "error": "Timeout"}\n{"kind": "back')
        self.assertEqual(monitor.run(self.runs, NOW),
                         ["⚠️ MartiQuant [p1] backfill_failed Timeout"])
        with ev.open("a") as fh:
            fh.write('fill_failed", "error": "503"}\n')
        self.assertEqual(monitor.run(self.runs, NOW + 120),
                         ["⚠️ MartiQuant [p1] backfill_failed 503"])

    def set_basket(self, coins):
        (self.run_dir / "config.json").write_text(json.dumps({"basket": coins}))

    def test_delisted_coin_alerts_once_hourly_check_and_recovers(self):
        self.write_state(NOW - 30, NOW - 90)
        self.set_basket(["SOL", "TON"])
        self.info["TON"] = {"onboard_ms": 0, "delivery_ms": 1_782_205_200_000, "status": "SETTLING"}
        self.assertEqual(monitor.run(self.runs, NOW),
                         ["❌ MartiQuant [p1] delisted: TON settled 2026-06-23 09:00 UTC (SETTLING)"])
        # within the hour: no exchangeInfo call, verdict kept, no repeat
        self.assertEqual(monitor.run(self.runs, NOW + 120), [])
        self.assertEqual(self.fetch_mock.call_count, 1)
        # coin swapped out of the basket: recovers on the next hourly check
        self.set_basket(["SOL", "AVAX"])
        self.info["AVAX"] = self.info["SOL"]
        self.write_state(NOW + 3670, NOW + 3610)
        self.assertEqual(monitor.run(self.runs, NOW + 3700), ["✅ MartiQuant [p1] delisted recovered"])

    def test_announced_delisting_and_missing_symbol(self):
        self.write_state(NOW - 30, NOW - 90)
        self.set_basket(["SOL", "XYZ"])
        self.info["SOL"] = {"onboard_ms": 0, "delivery_ms": int((NOW + 3 * 86400) * 1000),
                            "status": "TRADING"}
        msg = monitor.run(self.runs, NOW)[0]
        self.assertIn("SOL settles", msg)
        self.assertIn("XYZ has no USDT-M perpetual", msg)

    def test_exchange_info_failure_keeps_last_verdict(self):
        self.write_state(NOW - 30, NOW - 90)
        self.set_basket(["TON"])
        self.info["TON"] = {"onboard_ms": 0, "delivery_ms": 1_782_205_200_000, "status": "SETTLING"}
        self.assertEqual(len(monitor.run(self.runs, NOW)), 1)
        self.fetch_mock.side_effect = RuntimeError("fapi down")
        self.write_state(NOW + 3670, NOW + 3610)
        self.assertEqual(monitor.run(self.runs, NOW + 3700), [])  # no false "recovered"
        state = json.loads((self.logs / "monitor_state.json").read_text())
        self.assertIn("delisted", state["bad"]["p1"])

    def test_stopped_tag_is_forgotten(self):
        self.write_state(NOW - 900, NOW - 960)
        monitor.run(self.runs, NOW)
        (self.logs / "p1.cmd").unlink()
        self.assertEqual(monitor.run(self.runs, NOW + 120), [])
        state = json.loads((self.logs / "monitor_state.json").read_text())
        self.assertEqual(state["bad"], {})


if __name__ == "__main__":
    unittest.main()
