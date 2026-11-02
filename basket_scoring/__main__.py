"""CLI entry: hard-gate candidates (young or delisted listings) → compute metrics
across 365/180/90d windows → percentile-rank within each window → combine → persist.

Usage:
  python3 -m basket_scoring
  python3 -m basket_scoring --top 10
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from martiquant import archive
from martiquant.market_data import fetch_bars

from .candidates import (
    ALL_CANDIDATES,
    BENCHMARK,
    CURRENT_BASKET,
    MIN_ONBOARD_AGE_DAYS,
    fetch_symbol_info,
)
from .pipeline import DEFAULT_WINDOWS, gate, score_as_of
from .report import write_raw_metrics_csv, write_scores_csv, write_summary


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Score basket candidates (multi-window)")
    ap.add_argument("--top", type=int, default=10,
                    help="Target basket size (default 10)")
    ap.add_argument("--interval", default="1h", choices=["1h", "1m"],
                    help="Kline interval (1h recommended)")
    ap.add_argument("--source", default="api", choices=["api", "archive"],
                    help="archive: 1h bars aggregated from data/klines_1m (no fapi weight), "
                         "scored as of the newest archived bar")
    ap.add_argument("--runs-dir",
                    default=str(Path(__file__).resolve().parent / "runs"),
                    help="Where to write artifacts")
    return ap.parse_args()


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s",
                        datefmt="%H:%M:%S")
    args = parse_args()

    end_ms = (archive.last_bar_ms(BENCHMARK) + 60_000 if args.source == "archive"
              else int(time.time() * 1000))
    print(f"Hard gates: Binance USDT-M listed >= {MIN_ONBOARD_AGE_DAYS} days, not delisted")
    try:
        info = fetch_symbol_info()
    except Exception as e:
        print(f"ERROR: exchangeInfo unavailable, refusing to score without gates: {e!r}",
              file=sys.stderr)
        return 1
    approved, rejected = gate(ALL_CANDIDATES, info, end_ms)
    print(f"  approved: {len(approved)}  rejected: {len(rejected)} "
          f"({', '.join(f'{c} ({r})' for c, r in rejected.items()) if rejected else '-'})")
    print()

    start_ms = end_ms - max(DEFAULT_WINDOWS) * 86_400_000
    coin_bars: dict[str, list] = {}
    for coin in dict.fromkeys([BENCHMARK, *approved]):
        print(f"Fetching {coin}...", end="", flush=True)
        coin_bars[coin] = (archive.hourly_bars(coin, start_ms, end_ms) if args.source == "archive"
                           else fetch_bars(coin, start_ms, end_ms, args.interval))
        print(f" {len(coin_bars[coin]):,} bars")
    if len(coin_bars[BENCHMARK]) < 100:
        print("ERROR: too few BTC bars", file=sys.stderr)
        return 1

    scored, metrics_by_window = score_as_of(coin_bars, approved, end_ms)
    for days, mlist in metrics_by_window.items():
        print(f"  window {days}d: {len(mlist)} coins passed data check")

    now_iso = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = Path(args.runs_dir) / now_iso
    run_dir.mkdir(parents=True, exist_ok=True)

    write_scores_csv(run_dir / "scores.csv", scored)
    write_raw_metrics_csv(run_dir / "raw_metrics.csv", scored)
    write_summary(run_dir / "summary.md", scored, rejected, args.top)

    print()
    print(f"Top {args.top}:")
    for rank, s in enumerate(scored[:args.top], 1):
        tag = " (current)" if s.coin in CURRENT_BASKET else " (NEW)"
        per_win = " ".join(
            f"{d}d={s.per_window[d].total:>4.1f}"
            for d in sorted(s.per_window, reverse=True)
        )
        print(f"  {rank:>2}. {s.coin:<6} {s.total:>5.1f}  [{per_win}]{tag}")
    print()
    print(f"Artifacts: {run_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
