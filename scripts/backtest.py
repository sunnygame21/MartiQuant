"""Backtest the live paper strategy on archived 1m bars (martiquant.backtest).

Data: data/klines_1m (scripts/download_klines_archive.py). Same strategy, broker and
per-bar order as the live paper; adds the risk-control options under evaluation.

Usage:
  python3 scripts/backtest.py --days 720 --no-circuit-breaker --tag nocb
  python3 scripts/backtest.py --days 720 --no-circuit-breaker --dd-stop 5 --tag dd5
  python3 scripts/backtest.py --days 720 --no-circuit-breaker --max-adds 3 --tag adds3
  python3 scripts/backtest.py --days 720 --rebalances runs/walk_forward/<run>/schedule.json
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from martiquant import archive  # noqa: E402
from martiquant.backtest import DAY_MS, BacktestConfig, merge_bars, run  # noqa: E402
from martiquant.config import A_PERIOD, B_PERIOD, DEFAULT_BASKET, DEFAULT_RISK  # noqa: E402


ARTIFACTS = ("rounds", "stops", "deep_stops", "equity_daily", "equity_hourly")  # CSV, not summary


def _ms(s: str) -> int:
    if s.isdigit():  # raw epoch ms, e.g. to replay another run's exact window
        return int(s)
    return int(datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=720)
    ap.add_argument("--end", default=None, help="UTC date YYYY-MM-DD (exclusive); default = archive end")
    ap.add_argument("--start", default=None, help="UTC date YYYY-MM-DD; overrides --days")
    ap.add_argument("--equity-usd", type=float, default=100_000.0)
    ap.add_argument("--period", choices=["A", "B"], default="B")
    ap.add_argument("--source-equity-ref", type=float, default=207_000.0)
    ap.add_argument("--basket", default=",".join(DEFAULT_BASKET.include))
    ap.add_argument("--no-circuit-breaker", action="store_true")
    ap.add_argument("--max-adds", type=int, default=None,
                    help="Keep only the first N add levels of the ladder")
    ap.add_argument("--dd-stop", type=float, default=None,
                    help="Account MTM drawdown %% from peak that flattens and halts")
    ap.add_argument("--cooldown-days", default="7",
                    help="Halt length after a stop; 'none' = never resume")
    ap.add_argument("--deep-stop", type=float, default=None,
                    help="Sell a fully laddered coin when an hourly close is this %% below its "
                         "first entry; the coin opens no new round for --coin-cooldown-days")
    ap.add_argument("--coin-cooldown-days", type=float, default=7.0)
    ap.add_argument("--entry-block-ret30", type=float, default=None,
                    help="No new round for a coin while its 30-day return (daily closes up to "
                         "yesterday) is at or below -this %%")
    ap.add_argument("--maker-fee-bps", type=float, default=2.0)
    ap.add_argument("--taker-fee-bps", type=float, default=5.0)
    ap.add_argument("--stop-slippage-bps", type=float, default=20.0)
    ap.add_argument("--rebalances", type=Path, default=None,
                    help="JSON from scripts/walk_forward_basket.py: basket schedule + delistings")
    ap.add_argument("--tag", default="")
    ap.add_argument("--out-dir", type=Path,
                    default=Path(__file__).resolve().parent.parent / "runs" / "backtests")
    return ap.parse_args()


def main() -> int:
    args = parse_args()
    params = B_PERIOD if args.period == "B" else A_PERIOD
    if args.max_adds is not None:
        params = replace(params, name=f"{params.name}-adds{args.max_adds}",
                         ladder_drops_pct=params.ladder_drops_pct[:args.max_adds],
                         size_multipliers_usd=params.size_multipliers_usd[:args.max_adds])
    risk = DEFAULT_RISK
    if args.no_circuit_breaker:
        risk = replace(risk, max_level_any_coin_before_pause=99, max_coins_at_deep_level=99)

    end_ms = _ms(args.end) if args.end else archive.last_bar_ms("BTC") + 60_000
    start_ms = _ms(args.start) if args.start else end_ms - args.days * DAY_MS
    basket = tuple(c.strip().upper() for c in args.basket.split(",") if c.strip())
    rebalances, delist_ms = [], {}
    if args.rebalances:
        sched = json.loads(args.rebalances.read_text())
        rebalances = [(int(r["ts_ms"]), tuple(r["basket"])) for r in sched["rebalances"]]
        delist_ms = {c: int(t) for c, t in sched.get("delist_ms", {}).items()}
        first = [b for t, b in rebalances if t <= start_ms]
        basket = first[-1] if first else rebalances[0][1]
    coins = list(dict.fromkeys([*basket, *(c for _, b in rebalances for c in b)]))

    cfg = BacktestConfig(
        params=params, basket=basket, risk=risk, equity_usd=args.equity_usd,
        first_entry_usd=args.equity_usd * params.first_entry_usd_default / args.source_equity_ref,
        maker_fee_rate=args.maker_fee_bps / 1e4, taker_fee_rate=args.taker_fee_bps / 1e4,
        stop_slippage=args.stop_slippage_bps / 1e4, dd_stop_pct=args.dd_stop,
        cooldown_days=None if args.cooldown_days == "none" else float(args.cooldown_days),
        rebalances=rebalances, delist_ms=delist_ms,
        deep_stop_pct=args.deep_stop, coin_cooldown_days=args.coin_cooldown_days,
        entry_block_ret30_pct=args.entry_block_ret30,
    )

    t0 = time.time()
    data = {c: archive.load_coin(c, start_ms, end_ms) for c in coins}
    stream = merge_bars(data, coins)
    print(f"[{args.tag}] {len(stream[0]):,} bars, {len(coins)} coins, "
          f"{datetime.fromtimestamp(start_ms / 1000, timezone.utc):%F} -> "
          f"{datetime.fromtimestamp(end_ms / 1000, timezone.utc):%F}", flush=True)
    res = run(cfg, stream)
    summary = {k: v for k, v in res.items() if k not in ARTIFACTS}
    summary["runtime_sec"] = round(time.time() - t0, 1)

    now_iso = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = args.out_dir / (f"{now_iso}_{args.tag}" if args.tag else now_iso)
    run_dir.mkdir(parents=True, exist_ok=True)
    conf = {**vars(args), "rebalances": str(args.rebalances) if args.rebalances else None,
            "out_dir": str(args.out_dir), "start_ms": start_ms, "end_ms": end_ms,
            "coins": coins, "config": {**asdict(cfg), "params": asdict(params), "risk": asdict(risk)}}
    (run_dir / "config.json").write_text(json.dumps(conf, indent=2, default=list))
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    for name in ARTIFACTS:
        if res[name]:
            with (run_dir / f"{name}.csv").open("w", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=list(res[name][0]))
                w.writeheader()
                w.writerows(res[name])
    print(f"[{args.tag}] APR {summary['apr_pct_mtm']:+.2f}%  maxDD {summary['max_dd_pct']:.2f}%  "
          f"(hourly {summary['max_dd_hourly_pct']:.2f}%)  stops {summary['n_stops']}  "
          f"deep stops {summary['n_deep_stops']} (${summary['deep_stop_pnl_total']:+,.0f})  "
          f"rounds {summary['rounds_closed']:,}  "
          f"({summary['runtime_sec']:.0f}s) -> {run_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
