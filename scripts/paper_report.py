"""Status report for the live paper instances (read-only).

Each paper is a chain of run directories (every restart resumes the previous one
and writes a new runs/live_<ts>_<tag>/). The chain is followed backwards through
config.json "resume_from", so abandoned or smoke-test runs are never counted.

  realized  = price P&L + fees, from the newest state.json (fees are negative)
  APR       = realized / starting equity / days running * 365
  max DD    = deepest fall of MTM equity from its running peak, over all equity
              snapshots in the chain (one per minute)

Usage:
  python3 scripts/paper_report.py                # all instances run.sh knows
  python3 scripts/paper_report.py paper-cb-100k  # one instance
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
RUNS = ROOT / "runs"
TZ = ZoneInfo("Asia/Shanghai")


def _t(ms: float) -> str:
    return datetime.fromtimestamp(ms / 1000, TZ).strftime("%m-%d %H:%M")


def chain(tag: str) -> list[Path]:
    """Run directories of one paper, oldest first, linked by resume_from."""
    dirs = sorted(RUNS.glob(f"live_????????T??????Z_{tag}"))
    if not dirs:
        return []
    out, cur = [], dirs[-1]
    while cur is not None and cur.exists():
        out.append(cur)
        prev = json.loads((cur / "config.json").read_text()).get("resume_from")
        cur = (ROOT / prev) if prev else None
    return out[::-1]


def breaker_state(dirs: list[Path]) -> str:
    for d in reversed(dirs):
        last = None
        with (d / "events.jsonl").open() as fh:
            for line in fh:
                if '"circuit_' in line:
                    last = json.loads(line)
        if last:
            if last["kind"] == "circuit_pause":
                return f"paused since {_t(last['bar_ms'])} ({last['reason']})"
            return f"running (last resumed {_t(last['bar_ms'])})"
    return "running (never paused)"


def report(tag: str) -> None:
    dirs = chain(tag)
    if not dirs:
        print(f"{tag}: no runs")
        return
    cfg = json.loads((dirs[-1] / "config.json").read_text())
    equity0 = cfg["equity_usd"]
    st = json.loads((dirs[-1] / "state.json").read_text())
    tot = st["totals"]
    first_ms = json.loads((dirs[0] / "events.jsonl").open().readline())["ts_ms"]
    eq = pd.concat([pd.read_csv(d / "equity_hourly.csv") for d in dirs
                    if (d / "equity_hourly.csv").exists()]).sort_values("ts_ms").reset_index(drop=True)
    days = (time.time() * 1000 - first_ms) / 86_400_000
    realized = tot["price_pnl"] + tot["fee"]
    peak = eq.mtm_eq.cummax()
    dd = (eq.mtm_eq / peak - 1) * 100
    i = dd.idxmin() if len(dd) else None
    last = eq.iloc[-1]

    breaker = ("off" if not cfg.get("circuit_breaker_on", True)
               else breaker_state(dirs))
    print(f"\n== {tag}  (as of {_t(st['ts_ms'])}, {days:.1f} days, breaker {breaker})")
    print(f"   coins: {' '.join(cfg['basket'])}")
    print(f"   realized ${realized:+,.2f}  (price ${tot['price_pnl']:+,.2f}, fees ${tot['fee']:,.2f})  "
          f"rounds {tot['n_rounds_closed']}  APR {realized / equity0 / days * 365 * 100:.1f}%")
    print(f"   equity ${last.mtm_eq:,.2f}  float ${last.upl:+,.2f}  "
          f"max DD {dd.iloc[i]:.2f}% (${eq.mtm_eq.iloc[i] - peak.iloc[i]:,.0f}) at {_t(eq.ts_ms.iloc[i])}"
          if i is not None else "")
    rows = []
    for c, s in st["strategy_states"].items():
        if s["position_coin"] <= 1e-9:
            continue
        m = st["mark"][c]
        rows.append((s["position_coin"] * m - s["cost_basis_usd"], c, s["level"] - 1,
                     s["cost_basis_usd"], (m / s["first_entry_px"] - 1) * 100))
    if rows:
        print(f"   open: {len(rows)} coins, ${sum(r[3] for r in rows):,.0f} invested")
        for u, c, adds, cost, vs in sorted(rows):
            print(f"     {c:5s} adds {adds}  invested ${cost:>8,.0f}  vs first {vs:+6.1f}%  float ${u:+9,.2f}")


def main() -> int:
    tags = sys.argv[1:] or sorted(p.stem for p in (RUNS / "_live_logs").glob("*.cmd"))
    for tag in tags:
        report(tag)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
