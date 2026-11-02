"""Walk-forward basket schedule: rescore candidates every N months using only data
before each rebalance date, keep the top N coins. Feed the output to
scripts/backtest.py --rebalances to test the scoring rule out of sample.

Gates replay as of each date (listing age, not yet delisted); delisting times of
any coin that settles before --end go into delist_ms so the backtest closes the
position at settlement instead of marking it on flat post-delisting bars.

Usage:
  python3 scripts/walk_forward_basket.py --days 720 --every-months 3
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from basket_scoring.candidates import ALL_CANDIDATES, BENCHMARK, fetch_symbol_info  # noqa: E402
from basket_scoring.pipeline import DEFAULT_WINDOWS, gate, score_as_of  # noqa: E402
from martiquant import archive  # noqa: E402
from martiquant.backtest import DAY_MS  # noqa: E402


def _date(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d")


def _add_months(ms: int, months: int) -> int:
    """First day (00:00 UTC) of the month `months` after ms's month."""
    d = np.datetime64(_date(ms), "M") + np.timedelta64(months, "M")
    return int(np.datetime64(d, "ms").astype(np.int64))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=720)
    ap.add_argument("--every-months", type=int, default=3)
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--out-dir", type=Path, default=ROOT / "runs" / "walk_forward")
    args = ap.parse_args()

    end_ms = archive.last_bar_ms(BENCHMARK) + 60_000
    start_ms = end_ms - args.days * DAY_MS
    info = fetch_symbol_info()
    lookback = max(DEFAULT_WINDOWS) * DAY_MS
    bars = {c: archive.hourly_bars(c, start_ms - lookback, end_ms)
            for c in dict.fromkeys([BENCHMARK, *ALL_CANDIDATES]) if c in info}

    rebalances = []
    as_of = start_ms
    while as_of < end_ms:
        approved, rejected = gate(ALL_CANDIDATES, info, as_of)
        scored, _ = score_as_of(bars, approved, as_of)
        basket = [s.coin for s in scored[:args.top]]
        rebalances.append({"ts_ms": as_of, "date": _date(as_of), "basket": basket,
                           "rejected": rejected})
        print(f"{_date(as_of)}: {' '.join(basket)}  (rejected {len(rejected)}: "
              f"{', '.join(rejected) or '-'})", flush=True)
        as_of = _add_months(as_of, args.every_months)  # window start, then month boundaries

    delist_ms = {c: s["delivery_ms"] for c, s in info.items()
                 if c in ALL_CANDIDATES and s["delivery_ms"] < end_ms}
    out = args.out_dir / f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}_every{args.every_months}m"
    out.mkdir(parents=True, exist_ok=True)
    (out / "schedule.json").write_text(json.dumps(
        {"start_ms": start_ms, "end_ms": end_ms, "every_months": args.every_months,
         "top": args.top, "rebalances": rebalances, "delist_ms": delist_ms}, indent=2))
    print(f"-> {out / 'schedule.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
