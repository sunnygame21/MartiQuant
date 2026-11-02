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

    def tearDown(self):
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

    def test_stopped_tag_is_forgotten(self):
        self.write_state(NOW - 900, NOW - 960)
        monitor.run(self.runs, NOW)
        (self.logs / "p1.cmd").unlink()
        self.assertEqual(monitor.run(self.runs, NOW + 120), [])
        state = json.loads((self.logs / "monitor_state.json").read_text())
        self.assertEqual(state["bad"], {})


if __name__ == "__main__":
    unittest.main()
