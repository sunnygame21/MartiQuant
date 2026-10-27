"""Structured logging for the live paper runner.

Three append-only sinks under one run directory:
  events.jsonl       - every fill, reconcile, disconnect, deep_level warning
  rounds.csv         - one row per closed round (schema matches paper_backtest)
  equity_hourly.csv  - one row per hourly equity snapshot

All writes are flushed immediately so a crash never loses more than one event.
Header rows are written only when the file is first created.
"""
from __future__ import annotations

import csv
import json
import logging
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


ROUND_FIELDS = (
    "coin",
    "open_time_ms",
    "close_time_ms",
    "hold_hours",
    "first_entry_px",
    "close_px",
    "max_level",
    "price_pnl",
    "fee",
    "realized_pnl",
)

EQUITY_FIELDS = (
    "ts_ms",
    "mtm_eq",
    "realized_eq",
    "upl",
    "dd_pct_from_peak",
    "open_coins",
    "open_rounds",
    "orders_open",
)


class LiveLogger:
    def __init__(self, run_dir: Path) -> None:
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)

        self.events_path = self.run_dir / "events.jsonl"
        self.rounds_path = self.run_dir / "rounds.csv"
        self.equity_path = self.run_dir / "equity_hourly.csv"

        self._events = self.events_path.open("a", buffering=1)
        self._rounds = self._open_csv(self.rounds_path, ROUND_FIELDS)
        self._equity = self._open_csv(self.equity_path, EQUITY_FIELDS)

        self.event("logger_init",
                   run_dir=str(self.run_dir),
                   rounds_existing=self._rounds_existing,
                   equity_existing=self._equity_existing)

    def _open_csv(self, path: Path, fields: tuple[str, ...]):
        existed = path.exists() and path.stat().st_size > 0
        fh = path.open("a", newline="", buffering=1)
        w = csv.DictWriter(fh, fieldnames=list(fields))
        if not existed:
            w.writeheader()
            fh.flush()
        if path.name.startswith("rounds"):
            self._rounds_existing = existed
        elif path.name.startswith("equity"):
            self._equity_existing = existed
        return (fh, w)

    def event(self, kind: str, **kw: Any) -> None:
        payload = {"ts_ms": int(time.time() * 1000), "kind": kind}
        payload.update(kw)
        try:
            self._events.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
        except Exception:
            logger.exception("event write failed: kind=%s", kind)

    def round(self, row: dict[str, Any]) -> None:
        fh, w = self._rounds
        try:
            w.writerow({k: row.get(k) for k in ROUND_FIELDS})
            fh.flush()
        except Exception:
            logger.exception("round write failed: coin=%s", row.get("coin"))

    def equity_snapshot(self, row: dict[str, Any]) -> None:
        fh, w = self._equity
        try:
            w.writerow({k: row.get(k) for k in EQUITY_FIELDS})
            fh.flush()
        except Exception:
            logger.exception("equity snapshot write failed")

    def close(self) -> None:
        for fh in (self._events, self._rounds[0], self._equity[0]):
            try:
                fh.flush()
                fh.close()
            except Exception:
                pass
