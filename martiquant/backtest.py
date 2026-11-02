"""Event-driven backtest of LadderStrategy + PaperBroker on archived 1m bars.

Same per-bar loop as scripts/paper_backtest.py and the live runner: bars in
open_time order (ties in basket order); each bar → broker.match_bar →
strategy.on_fill → mark → reconcile on any fill or hour turn → MTM equity.

On top of that it evaluates the risk controls under study:

  dd_stop_pct    account MTM drawdown from peak at which every position is sold at
                 the bar close (taker fee + slippage), all orders cancelled, trading
                 halted. cooldown_days later it resumes with the stop's peak reset to
                 the equity then (stand-in for a manual restart); None = halt for good.
  rebalances     [(ms, basket)]: switch traded coins (walk-forward basket scoring).
                 Dropped coins finish their open round, then go quiet.
  delist_ms      {coin: ms}: an open position is settled at the bar close (taker fee,
                 no slippage) once the contract is delisted.

Reported max drawdown is always measured from the all-time equity peak; only the
stop rule uses the reset peak.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .config import BasketConfig, LadderParams, RiskLimits
from .paper_broker import PaperBroker, SimFill
from .strategy import LadderStrategy

DAY_MS = 86_400_000
HOUR_MS = 3_600_000


@dataclass
class BacktestConfig:
    params: LadderParams
    basket: tuple[str, ...]
    risk: RiskLimits
    equity_usd: float
    first_entry_usd: float
    maker_fee_rate: float = 0.0002
    taker_fee_rate: float = 0.0005
    stop_slippage: float = 0.002
    dd_stop_pct: float | None = None
    cooldown_days: float | None = 7.0
    rebalances: list[tuple[int, tuple[str, ...]]] = field(default_factory=list)
    delist_ms: dict[str, int] = field(default_factory=dict)


def merge_bars(data: dict[str, dict[str, np.ndarray]], order: list[str]
               ) -> tuple[list[int], list[str], list[float], list[float], list[float]]:
    """One stream sorted by open_time, ties broken by position in `order`."""
    coins = [c for c in order if c in data]
    t = np.concatenate([data[c]["t"] for c in coins])
    rank = np.concatenate([np.full(len(data[c]["t"]), i) for i, c in enumerate(coins)])
    idx = np.lexsort((rank, t))
    pick = lambda k: np.concatenate([data[c][k] for c in coins])[idx].tolist()
    return (t[idx].tolist(), [coins[i] for i in rank[idx].tolist()],
            pick("h"), pick("l"), pick("c"))


def run(cfg: BacktestConfig, stream: tuple[list, ...]) -> dict:
    t_all, coin_all, h_all, l_all, c_all = stream
    strat = LadderStrategy(cfg.params, BasketConfig(include=cfg.basket), cfg.risk,
                           cfg.first_entry_usd)
    broker = PaperBroker(maker_fee_rate=cfg.maker_fee_rate)
    rebalances = sorted(cfg.rebalances)
    next_rebalance = 0

    mark: dict[str, float] = {}
    upl_by: dict[str, float] = {}
    price_pnl = fee_total = 0.0
    peak = stop_peak = cfg.equity_usd
    max_dd = worst_upl = max_committed = 0.0
    hour_last = -1
    halted_until: float | None = None
    pause_ticks = n_bars = 0
    tracker: dict[str, dict] = {}
    rounds: list[dict] = []
    stops: list[dict] = []
    equity_daily: list[dict] = []
    last_day = -1

    def apply(f: SimFill, reason: str = "tp") -> None:
        nonlocal price_pnl, fee_total
        s = strat.states[f.coin]
        was_open = s.in_position
        pnl = strat.on_fill(f.coin, f.side, f.px, f.qty, str(f.ts))
        price_pnl += pnl
        fee_total += f.fee
        t = tracker.get(f.coin)
        if f.side == "buy":
            if not was_open:
                tracker[f.coin] = {"open_ts_ms": f.ts, "first_entry_px": f.px,
                                   "max_level": 1, "fee_paid": f.fee}
            elif t is not None:
                t["max_level"] = max(t["max_level"], s.level)
                t["fee_paid"] += f.fee
            return
        if t is not None:
            t["fee_paid"] += f.fee
            if not s.in_position:
                rounds.append({
                    "coin": f.coin, "open_time_ms": t["open_ts_ms"], "close_time_ms": f.ts,
                    "hold_hours": (f.ts - t["open_ts_ms"]) / HOUR_MS,
                    "first_entry_px": t["first_entry_px"], "close_px": f.px,
                    "max_level": t["max_level"], "price_pnl": pnl, "fee": t["fee_paid"],
                    "realized_pnl": pnl + t["fee_paid"], "exit": reason,
                })
                tracker.pop(f.coin, None)

    def force_close(coin: str, ts: int, slippage: float, reason: str) -> None:
        s = strat.states[coin]
        apply(broker.market_sell(coin, s.position_coin, mark[coin], ts,
                                 cfg.taker_fee_rate, slippage), reason)
        upl_by[coin] = 0.0

    for i in range(len(t_all)):
        ts, coin = t_all[i], coin_all[i]
        while next_rebalance < len(rebalances) and ts >= rebalances[next_rebalance][0]:
            strat.set_basket(rebalances[next_rebalance][1])
            hour_last = -1
            next_rebalance += 1
        s = strat.states.get(coin)
        if s is None:
            continue  # not in any basket yet
        n_bars += 1
        realized = cfg.equity_usd + price_pnl + fee_total

        if halted_until is not None and ts >= halted_until:
            halted_until = None
            stop_peak = realized + sum(upl_by.values())
            hour_last = -1
        halted = halted_until is not None

        fills = [] if halted else broker.match_bar(coin, h_all[i], l_all[i], ts)
        for f in fills:
            apply(f)
        close = c_all[i]
        mark[coin] = close

        delist = cfg.delist_ms.get(coin)
        if delist is not None and ts >= delist:
            if s.in_position:
                force_close(coin, ts, 0.0, "delisted")
            if coin in strat.basket.include:  # flat bars after settlement must not reopen
                strat.set_basket(tuple(c for c in strat.basket.include if c != coin))

        hour = ts // HOUR_MS
        if not halted and (fills or hour != hour_last):
            broker.reconcile(strat.desired_orders(mark))
            hour_last = hour

        upl_by[coin] = s.position_coin * (close - s.wavg_entry) if s.in_position else 0.0
        upl = sum(upl_by.values())
        worst_upl = min(worst_upl, upl)
        eq = cfg.equity_usd + price_pnl + fee_total + upl
        peak = max(peak, eq)
        stop_peak = max(stop_peak, eq)
        dd = (eq / peak - 1) * 100
        max_dd = min(max_dd, dd)
        max_committed = max(max_committed, sum(x.cost_basis_usd for x in strat.states.values()))
        if strat.should_pause_new_openings()[0]:
            pause_ticks += 1

        if (not halted and cfg.dd_stop_pct is not None
                and (eq / stop_peak - 1) * 100 <= -cfg.dd_stop_pct):
            open_coins = [c for c, x in strat.states.items() if x.in_position]
            for c in open_coins:
                force_close(c, ts, cfg.stop_slippage, "dd_stop")
            broker.n_cancelled += len(broker.open_orders)
            broker.open_orders.clear()
            eq_after = cfg.equity_usd + price_pnl + fee_total
            stops.append({"ts_ms": ts, "eq_before": eq, "eq_after": eq_after,
                          "stop_peak": stop_peak, "coins": len(open_coins)})
            halted_until = (float("inf") if cfg.cooldown_days is None
                            else ts + cfg.cooldown_days * DAY_MS)
            eq = eq_after

        day = ts // DAY_MS
        if day != last_day:
            equity_daily.append({"ts_ms": ts, "mtm_eq": round(eq, 4),
                                 "realized_eq": round(cfg.equity_usd + price_pnl + fee_total, 4),
                                 "upl": round(eq - cfg.equity_usd - price_pnl - fee_total, 4),
                                 "dd_pct_from_peak": round(dd, 4)})
            last_day = day

    final_upl = sum(upl_by.values())
    final_eq = cfg.equity_usd + price_pnl + fee_total + final_upl
    days = (t_all[-1] - t_all[0] + 60_000) / DAY_MS if t_all else 0.0
    return {
        "days": days,
        "final_eq_mtm": final_eq,
        "final_eq_realized": cfg.equity_usd + price_pnl + fee_total,
        "net_pct_mtm": (final_eq / cfg.equity_usd - 1) * 100,
        "apr_pct_mtm": (final_eq / cfg.equity_usd - 1) * 100 * 365 / days if days else 0.0,
        "realized_price_pnl": price_pnl,
        "fees": fee_total,
        "open_upl": final_upl,
        "worst_upl_during": worst_upl,
        "max_dd_pct": max_dd,
        "peak_eq": peak,
        "max_committed_usd": max_committed,
        "rounds_closed": len(rounds),
        "n_stops": len(stops),
        "stop_loss_total": sum(x["eq_after"] - x["stop_peak"] for x in stops),
        "halted_at_end": halted_until is not None,
        "orders_placed": broker.n_placed,
        "orders_cancelled": broker.n_cancelled,
        "risk_pause_pct": pause_ticks / n_bars * 100 if n_bars else 0.0,
        "rounds": rounds,
        "stops": stops,
        "equity_daily": equity_daily,
    }
