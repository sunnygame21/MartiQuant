"""CLI entry: hard-gate young listings → compute metrics across 365/180/90d
windows → percentile-rank within each window → combine → persist.

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

from martiquant.market_data import fetch_bars

from .candidates import (
    ALL_CANDIDATES,
    BENCHMARK,
    CURRENT_BASKET,
    MIN_ONBOARD_AGE_DAYS,
    fetch_onboard_dates,
)
from .metrics import btc_daily_returns, compute_metrics
from .report import write_raw_metrics_csv, write_scores_csv, write_summary
from .score import DEFAULT_WINDOW_WEIGHTS, score_multi


DEFAULT_WINDOWS = (365, 180, 90)


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Score basket candidates (multi-window)")
    ap.add_argument("--top", type=int, default=10,
                    help="Target basket size (default 10)")
    ap.add_argument("--interval", default="1h", choices=["1h", "1m"],
                    help="Kline interval (1h recommended)")
    ap.add_argument("--runs-dir",
                    default=str(Path(__file__).resolve().parent / "runs"),
                    help="Where to write artifacts")
    return ap.parse_args()


def _apply_onboard_gate(candidates: tuple[str, ...]) -> tuple[list[str], dict[str, int]]:
    """Return (approved coins, {rejected coin: listed days})."""
    now_ms = int(time.time() * 1000)
    try:
        onboard = fetch_onboard_dates()
    except Exception:
        print("WARN: could not fetch onboard dates; skipping age gate", file=sys.stderr)
        return list(candidates), {}
    approved: list[str] = []
    rejected: dict[str, int] = {}
    for coin in candidates:
        onboard_ms = onboard.get(coin, 0)
        if onboard_ms == 0:
            rejected[coin] = 0
            continue
        age_days = (now_ms - onboard_ms) / 86_400_000
        if age_days >= MIN_ONBOARD_AGE_DAYS:
            approved.append(coin)
        else:
            rejected[coin] = int(age_days)
    return approved, rejected


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s",
                        datefmt="%H:%M:%S")
    args = parse_args()

    print(f"Hard gate: Binance USDT-M listed >= {MIN_ONBOARD_AGE_DAYS} days")
    approved, rejected = _apply_onboard_gate(ALL_CANDIDATES)
    print(f"  approved: {len(approved)}  rejected: {len(rejected)} "
          f"({', '.join(f'{c}({d}d)' for c, d in rejected.items()) if rejected else '-'})")
    print()
    if BENCHMARK not in approved and BENCHMARK in ALL_CANDIDATES:
        approved.append(BENCHMARK)

    end_ms = int(time.time() * 1000)
    max_days = max(DEFAULT_WINDOWS)
    start_ms = end_ms - max_days * 86_400_000

    print(f"Fetching {BENCHMARK} (benchmark + candidate)...")
    btc_bars_all = fetch_bars(BENCHMARK, start_ms, end_ms, args.interval)
    print(f"  {len(btc_bars_all):,} bars")
    if len(btc_bars_all) < 100:
        print("ERROR: too few BTC bars", file=sys.stderr)
        return 1

    coin_bars: dict[str, list] = {BENCHMARK: btc_bars_all}
    for coin in approved:
        if coin == BENCHMARK:
            continue
        print(f"Fetching {coin}...", end="", flush=True)
        bars = fetch_bars(coin, start_ms, end_ms, args.interval)
        print(f" {len(bars):,} bars")
        if len(bars) >= 100:
            coin_bars[coin] = bars

    metrics_by_window: dict[int, list] = {}
    for days in DEFAULT_WINDOWS:
        win_start = end_ms - days * 86_400_000
        btc_win = [b for b in btc_bars_all if b.open_time_ms >= win_start]
        btc_days_ret, btc_rets = btc_daily_returns(btc_win)
        win_metrics = []
        for coin, bars in coin_bars.items():
            win_bars = [b for b in bars if b.open_time_ms >= win_start]
            if len(win_bars) < 24 * days * 0.9:
                continue
            if coin == BENCHMARK and len(approved) == 0:
                continue
            if coin not in approved and coin != BENCHMARK:
                continue
            win_metrics.append(
                compute_metrics(coin, win_bars, btc_days_ret, btc_rets)
            )
        if BENCHMARK not in approved:
            win_metrics = [m for m in win_metrics if m.coin != BENCHMARK]
        metrics_by_window[days] = win_metrics
        print(f"  window {days}d: {len(win_metrics)} coins passed data check")

    scored = score_multi(metrics_by_window, DEFAULT_WINDOW_WEIGHTS)

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
