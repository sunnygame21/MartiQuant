"""CLI entry point for the live internal paper runner.

Usage:
  python3 -m martiquant.live --equity-usd 10000 --tag paper1
  python3 -m martiquant.live --equity-usd 10000 --no-circuit-breaker --tag nocb
  python3 -m martiquant.live --resume-from runs/live_20261022T100000Z

See docs/2026-10-22-live-paper-design.md §10.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from martiquant.config import (
    A_PERIOD,
    B_PERIOD,
    DEFAULT_BASKET,
    DEFAULT_RISK,
    BasketConfig,
)

from .runner import LiveRunner


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="MartiQuant live internal paper runner")
    ap.add_argument("--equity-usd", type=float, default=10_000.0)
    ap.add_argument("--period", choices=["A", "B"], default="B")
    ap.add_argument(
        "--source-equity-ref", type=float, default=207_000.0,
        help="Equity the B期 $320 first entry was tuned for (scaling base)",
    )
    ap.add_argument("--maker-fee-bps", type=float, default=2.0)
    ap.add_argument("--no-circuit-breaker", action="store_true")
    ap.add_argument("--include-btc-ada", action="store_true")
    ap.add_argument("--basket", default=None,
                    help="Comma-separated override, e.g. NEAR,BTC,BNB,INJ,ETH,TON,TRX,LINK,SOL,XRP. "
                         "Clears blacklist. Takes precedence over --include-btc-ada.")
    ap.add_argument("--tag", default="")
    ap.add_argument(
        "--runs-dir",
        default=str(Path(__file__).resolve().parent.parent.parent / "runs"),
    )
    ap.add_argument(
        "--resume-from", type=Path, default=None,
        help="Resume state from a previous run directory (uses its state.json)",
    )
    return ap.parse_args()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    args = parse_args()

    params = B_PERIOD if args.period == "B" else A_PERIOD
    first_entry_usd = args.equity_usd * (params.first_entry_usd_default / args.source_equity_ref)

    basket = DEFAULT_BASKET
    if args.basket:
        coins = tuple(c.strip().upper() for c in args.basket.split(",") if c.strip())
        basket = BasketConfig(include=coins, blacklist=())
    elif args.include_btc_ada:
        basket = BasketConfig(
            include=tuple(list(DEFAULT_BASKET.include) + ["BTC", "ADA"]),
            blacklist=(),
        )

    risk = DEFAULT_RISK
    if args.no_circuit_breaker:
        risk = replace(risk, max_level_any_coin_before_pause=99, max_coins_at_deep_level=99)

    now_iso = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    tag = f"_{args.tag}" if args.tag else ""
    run_dir = Path(args.runs_dir) / f"live_{now_iso}{tag}"
    run_dir.mkdir(parents=True, exist_ok=True)

    config_dump = {
        "run_ts_utc": now_iso,
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
        "resume_from": str(args.resume_from) if args.resume_from else None,
    }
    (run_dir / "config.json").write_text(json.dumps(config_dump, indent=2))

    print(f"Period: {params.name}  TP: +{params.tp_pct}%  ladder: {params.ladder_drops_pct}")
    print(f"Equity: ${args.equity_usd:,.0f}  → first entry: ${first_entry_usd:.2f}")
    print(f"Basket: {basket.include}  blacklist: {basket.blacklist}")
    print(f"Circuit breaker: {'OFF' if args.no_circuit_breaker else 'ON '}")
    print(f"Artifacts: {run_dir}")
    print()

    runner = LiveRunner(
        params=params,
        basket=basket,
        risk=risk,
        first_entry_usd=first_entry_usd,
        equity_usd=args.equity_usd,
        maker_fee_bps=args.maker_fee_bps,
        run_dir=run_dir,
        resume_from=args.resume_from,
    )

    try:
        asyncio.run(runner.run())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
