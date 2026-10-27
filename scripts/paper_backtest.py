#!/usr/bin/env python3
"""Local paper backtest: Binance public 1m klines → martiquant.strategy → PaperBroker.

No API key, no exchange account, no Testnet. Just:
  python3 scripts/paper_backtest.py --days 14 --equity-usd 10000
  python3 scripts/paper_backtest.py --days 90 --equity-usd 10000 --no-circuit-breaker
  python3 scripts/paper_backtest.py --days 90 --equity-usd 10000 --include-btc-ada

The run prints equity curve stats, per-coin PnL, and risk-break events.
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

from martiquant.config import (  # noqa: E402
    A_PERIOD,
    B_PERIOD,
    DEFAULT_BASKET,
    DEFAULT_RISK,
    BasketConfig,
    RiskLimits,
)
from martiquant.market_data import Bar, fetch_bars  # noqa: E402
from martiquant.paper_broker import PaperBroker  # noqa: E402
from martiquant.strategy import LadderStrategy  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14, help="History length in days")
    ap.add_argument("--equity-usd", type=float, default=10_000.0)
    ap.add_argument("--period", choices=["A", "B"], default="B")
    ap.add_argument(
        "--source-equity-ref",
        type=float,
        default=207_000.0,
        help="Equity the source B期 $320 first entry was tuned for; we scale from this.",
    )
    ap.add_argument(
        "--maker-fee-bps", type=float, default=2.0, help="Binance futures maker fee bps"
    )
    ap.add_argument(
        "--no-circuit-breaker",
        action="store_true",
        help="Disable the mechanical circuit-breaker (any-coin-L4 / ≥3-coins-L3). Stress test.",
    )
    ap.add_argument(
        "--include-btc-ada",
        action="store_true",
        help="Append BTC and ADA to the default basket (12 coins).",
    )
    ap.add_argument(
        "--tag",
        default="",
        help="Optional short label for the run directory (e.g. 'baseline', 'no-cb').",
    )
    ap.add_argument(
        "--runs-dir",
        default=str(Path(__file__).resolve().parent.parent / "runs"),
        help="Where to persist run artifacts (default: ./runs/).",
    )
    args = ap.parse_args()

    params = B_PERIOD if args.period == "B" else A_PERIOD
    first_entry_usd = args.equity_usd * (params.first_entry_usd_default / args.source_equity_ref)

    basket = DEFAULT_BASKET
    if args.include_btc_ada:
        basket = BasketConfig(
            include=tuple(list(DEFAULT_BASKET.include) + ["BTC", "ADA"]),
            blacklist=(),
        )

    risk = DEFAULT_RISK
    if args.no_circuit_breaker:
        # Set thresholds beyond the ladder depth so the breaker is effectively off.
        risk = replace(risk, max_level_any_coin_before_pause=99, max_coins_at_deep_level=99)

    print(f"Period: {params.name}  TP: +{params.tp_pct}%  ladder: {params.ladder_drops_pct}")
    print(f"Equity: ${args.equity_usd:,.0f}  → first entry: ${first_entry_usd:.2f}")
    print(f"Basket: {basket.include}  blacklist: {basket.blacklist}")
    print(f"Circuit breaker: {'OFF' if args.no_circuit_breaker else 'ON '}  "
          f"(max_level={risk.max_level_any_coin_before_pause}, "
          f"≥{risk.max_coins_at_deep_level}-coins-at-L{risk.deep_level})")
    print()

    strat = LadderStrategy(params, basket, risk, first_entry_usd)
    broker = PaperBroker(maker_fee_rate=args.maker_fee_bps / 1e4)

    end_ms = int(time.time() * 1000)
    start_ms = end_ms - args.days * 86_400_000

    print(f"Fetching {args.days}d 1m klines...")
    all_bars: list[Bar] = []
    for coin in basket.include:
        print(f"  {coin}...", end="", flush=True)
        bars = fetch_bars(coin, start_ms, end_ms, "1m")
        print(f" {len(bars):>7,} bars")
        all_bars.extend(bars)
    all_bars.sort(key=lambda b: b.open_time_ms)
    print(f"Total events: {len(all_bars):,}")
    print()

    mark: dict[str, float] = {}
    pnl_total = 0.0
    fee_total = 0.0
    per_coin_pnl = {c: 0.0 for c in basket.include}
    per_coin_fee = {c: 0.0 for c in basket.include}
    per_coin_rounds = {c: 0 for c in basket.include}
    per_coin_max_level = {c: 0 for c in basket.include}
    risk_pause_ticks = 0
    hour_last = -1

    peak_eq = args.equity_usd
    max_dd_pct = 0.0
    worst_upl = 0.0

    # Per-round log: each full close becomes one row.
    round_log: list[dict] = []
    # Daily equity sample (one row per UTC day for compact storage).
    equity_curve: list[dict] = []
    last_day = -1

    # Per-coin round in-progress tracker (to log when it closes).
    coin_round_tracker: dict[str, dict] = {}  # coin -> {open_ts, max_level, legs}

    for b in all_bars:
        fills = broker.match_bar(b.coin, b.high, b.low, b.open_time_ms)
        for f in fills:
            # Round open-tracking: on first fill (level 0 → 1), record start
            s_before = strat.states[f.coin]
            was_open = s_before.in_position
            pnl = strat.on_fill(f.coin, f.side, f.px, f.qty, str(f.ts))
            s_after = strat.states[f.coin]

            pnl_total += pnl
            fee_total += f.fee
            per_coin_pnl[f.coin] += pnl
            per_coin_fee[f.coin] += f.fee
            per_coin_max_level[f.coin] = max(per_coin_max_level[f.coin], s_after.level)

            if f.side == "buy":
                if not was_open:
                    coin_round_tracker[f.coin] = {
                        "open_ts_ms": f.ts,
                        "first_entry_px": f.px,
                        "max_level": 1,
                        "fee_paid": f.fee,
                    }
                else:
                    t = coin_round_tracker.get(f.coin)
                    if t is not None:
                        t["max_level"] = max(t["max_level"], s_after.level)
                        t["fee_paid"] += f.fee
            else:  # sell
                t = coin_round_tracker.get(f.coin)
                if t is not None:
                    t["fee_paid"] += f.fee
                if not s_after.in_position and t is not None:
                    # Round fully closed
                    per_coin_rounds[f.coin] += 1
                    round_log.append(
                        {
                            "coin": f.coin,
                            "open_time_ms": t["open_ts_ms"],
                            "close_time_ms": f.ts,
                            "hold_hours": (f.ts - t["open_ts_ms"]) / 3_600_000,
                            "first_entry_px": t["first_entry_px"],
                            "close_px": f.px,
                            "max_level": t["max_level"],
                            "price_pnl": pnl,
                            "fee": t["fee_paid"],
                            "realized_pnl": pnl + t["fee_paid"],
                        }
                    )
                    coin_round_tracker.pop(f.coin, None)

        mark[b.coin] = b.close

        hour = b.open_time_ms // 3_600_000
        if fills or hour != hour_last:
            desired = strat.desired_orders(mark)
            broker.reconcile(desired)
            hour_last = hour

        upl = 0.0
        for c, s in strat.states.items():
            m = mark.get(c)
            if s.in_position and m is not None:
                upl += s.position_coin * (m - s.wavg_entry)
        if upl < worst_upl:
            worst_upl = upl
        eq = args.equity_usd + pnl_total + fee_total + upl
        if eq > peak_eq:
            peak_eq = eq
        dd = (eq / peak_eq - 1) * 100
        if dd < max_dd_pct:
            max_dd_pct = dd

        paused, _ = strat.should_pause_new_openings()
        if paused:
            risk_pause_ticks += 1

        # Sample equity daily
        day = b.open_time_ms // 86_400_000
        if day != last_day:
            equity_curve.append(
                {
                    "ts_ms": b.open_time_ms,
                    "mtm_eq": round(eq, 4),
                    "realized_eq": round(args.equity_usd + pnl_total + fee_total, 4),
                    "upl": round(upl, 4),
                    "dd_pct_from_peak": round(dd, 4),
                }
            )
            last_day = day

    final_upl = 0.0
    final_pos_notional = 0.0
    for c, s in strat.states.items():
        m = mark.get(c)
        if s.in_position and m is not None:
            final_upl += s.position_coin * (m - s.wavg_entry)
            final_pos_notional += s.position_coin * m
    final_eq = args.equity_usd + pnl_total + fee_total + final_upl
    realized_only_eq = args.equity_usd + pnl_total + fee_total
    days = args.days

    print("=" * 76)
    print(f"  Equity (MTM)       : ${args.equity_usd:,.0f} → ${final_eq:,.2f}  "
          f"({(final_eq/args.equity_usd - 1)*100:+.3f}%)")
    print(f"  Equity (realized)  :  → ${realized_only_eq:,.2f}  "
          f"({(realized_only_eq/args.equity_usd - 1)*100:+.3f}%)")
    print(f"  Open position UPL  : ${final_upl:+,.2f}  "
          f"on ${final_pos_notional:,.0f} notional ({final_pos_notional/args.equity_usd*100:.1f}% of equity)")
    print(f"  Worst UPL (during): ${worst_upl:+,.2f}  "
          f"({worst_upl/args.equity_usd*100:+.2f}% of equity)")
    print(f"  Realized price PnL : ${pnl_total:+,.2f}")
    print(f"  Fees               : ${fee_total:+,.2f}")
    print(f"  Rounds closed      : {sum(per_coin_rounds.values())}")
    print(f"  Peak equity (MTM)  : ${peak_eq:,.2f}")
    print(f"  Max drawdown (MTM) : {max_dd_pct:.3f}%")
    ann = (final_eq / args.equity_usd - 1) * (365 / days) * 100
    print(f"  Annualized (naive) : {ann:+.2f}%")
    print(f"  Orders placed/cx   : {broker.n_placed:,} / {broker.n_cancelled:,}")
    print(f"  Risk-pause ticks   : {risk_pause_ticks:,}  "
          f"({risk_pause_ticks/len(all_bars)*100:.2f}% of time)")
    print()

    print("Per-coin:")
    print(f"  {'coin':<6} {'rounds':>7} {'px_pnl':>10} {'fee':>8} {'net':>10} "
          f"{'maxLvl':>7} {'open':>5}")
    for coin in basket.include:
        s = strat.states[coin]
        print(
            f"  {coin:<6} {per_coin_rounds[coin]:>7} "
            f"{per_coin_pnl[coin]:>+10.2f} {per_coin_fee[coin]:>+8.2f} "
            f"{per_coin_pnl[coin]+per_coin_fee[coin]:>+10.2f} "
            f"{per_coin_max_level[coin]:>7} {s.level:>5}"
        )

    # ------------------------------------------------------------------ persist
    now_iso = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    tag = f"_{args.tag}" if args.tag else ""
    run_dir = Path(args.runs_dir) / f"{now_iso}{tag}"
    run_dir.mkdir(parents=True, exist_ok=True)

    config = {
        "run_ts_utc": now_iso,
        "days": args.days,
        "equity_usd": args.equity_usd,
        "period": params.name,
        "ladder_drops_pct": list(params.ladder_drops_pct),
        "size_multipliers_usd": list(params.size_multipliers_usd),
        "tp_pct": params.tp_pct,
        "first_entry_usd": first_entry_usd,
        "source_equity_ref": args.source_equity_ref,
        "basket": list(basket.include),
        "blacklist": list(basket.blacklist),
        "maker_fee_bps": args.maker_fee_bps,
        "circuit_breaker_on": not args.no_circuit_breaker,
        "risk_max_level": risk.max_level_any_coin_before_pause,
        "risk_deep_level": risk.deep_level,
        "risk_max_coins_at_deep": risk.max_coins_at_deep_level,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "n_bars": len(all_bars),
    }

    summary = {
        "final_eq_mtm": final_eq,
        "final_eq_realized": realized_only_eq,
        "net_pct_mtm": (final_eq / args.equity_usd - 1) * 100,
        "net_pct_realized": (realized_only_eq / args.equity_usd - 1) * 100,
        "annualized_pct": ann,
        "realized_price_pnl": pnl_total,
        "fees": fee_total,
        "open_upl": final_upl,
        "worst_upl_during": worst_upl,
        "max_dd_pct": max_dd_pct,
        "peak_eq": peak_eq,
        "rounds_closed": sum(per_coin_rounds.values()),
        "orders_placed": broker.n_placed,
        "orders_cancelled": broker.n_cancelled,
        "risk_pause_pct": risk_pause_ticks / len(all_bars) * 100,
        "per_coin": {
            c: {
                "rounds": per_coin_rounds[c],
                "price_pnl": per_coin_pnl[c],
                "fee": per_coin_fee[c],
                "net": per_coin_pnl[c] + per_coin_fee[c],
                "max_level": per_coin_max_level[c],
                "open_level_at_end": strat.states[c].level,
            }
            for c in basket.include
        },
    }

    (run_dir / "config.json").write_text(json.dumps(config, indent=2))
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2))

    if round_log:
        with (run_dir / "rounds.csv").open("w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(round_log[0].keys()))
            w.writeheader()
            w.writerows(round_log)

    if equity_curve:
        with (run_dir / "equity_daily.csv").open("w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(equity_curve[0].keys()))
            w.writeheader()
            w.writerows(equity_curve)

    print()
    print(f"Artifacts written to: {run_dir}")
    print(f"  config.json       — run parameters")
    print(f"  summary.json      — top-line numbers + per-coin")
    print(f"  rounds.csv        — {len(round_log):,} closed rounds")
    print(f"  equity_daily.csv  — {len(equity_curve):,} daily samples")

    return 0


if __name__ == "__main__":
    sys.exit(main())
