"""Deterministic correlated drawdown diagnostics for frozen research candidates.

These paths are hypothetical scenarios, not forecasts or historical evidence.
They intentionally include no rebound and do not enter the parameter selection.
"""
from __future__ import annotations

from dataclasses import replace
import numpy as np

from martiquant.config import DEFAULT_BASKET
from martiquant.research_engine import MarketData, SearchConfig, run_backtest


def _scenario(kind: str, coins: tuple[str, ...], deepest_drop_pct: float = -25.) -> MarketData:
    warmup = np.full(60, 100.0)
    if kind in ("smooth_down40", "smooth_down60"):
        terminal = 60.0 if kind == "smooth_down40" else 40.0
        path = np.concatenate((warmup, np.linspace(100., terminal, 1441)[1:],
                               np.full(720, terminal)))
        gap_at = -1
    elif kind == "gap40":
        path = np.concatenate((warmup, np.full(720, 60.0)))
        gap_at = len(warmup)
    elif kind == "gap40_after_ladder":
        # Build deep positions before an additional 40% overnight-style gap.
        # First reach one percentage point below this candidate's deepest level.
        # For -25% deepest level the total fall is 55.6% from initial entry.
        deep_price = max(.01, 100. + deepest_drop_pct - 1.)
        path = np.concatenate((warmup, np.linspace(100., deep_price, 1441)[1:],
                               np.full(720, deep_price * .60)))
        gap_at = len(warmup) + 1440
    else:
        raise ValueError(f"Unknown synthetic scenario: {kind}")
    opening = np.concatenate((path[:1], path[:-1]))
    if gap_at >= 0:
        opening[gap_at] = path[gap_at]
    high = np.maximum(opening, path)
    low = np.minimum(opening, path)
    arrays = [np.ascontiguousarray(np.repeat(v[:, None], len(coins), axis=1))
              for v in (opening, high, low, path)]
    times = 1784721960000 + np.arange(len(path), dtype=np.int64) * 60000
    return MarketData(times, *arrays, coins, {
        "synthetic": True, "scenario": kind, "fully_correlated": True,
        "rebound": False, "funding_rate": 0.,
    })


def run_stress(config: SearchConfig) -> dict:
    """Compare configured risk control with effectively disabled drawdown stop."""
    result = {}
    for name in ("smooth_down40", "smooth_down60", "gap40", "gap40_after_ladder"):
        data = _scenario(name, tuple(DEFAULT_BASKET.include), min(config.drops_pct))
        result[name] = {
            "configured_stop": run_backtest(data, config),
            "without_drawdown_stop": run_backtest(
                data, replace(config, hard_stop_pct=99., low_proxy=False)),
        }
    result["assumptions"] = {
        "coins": list(DEFAULT_BASKET.include),
        "warmup_minutes": 60, "smooth_decline_minutes": 1440,
        "flat_tail_minutes": 720,
        "gap40_after_ladder_total_decline_pct": (
            100. - max(.01, 100. + min(config.drops_pct) - 1.) * .60),
        "gap_limit_buys": "At resting limit, with no favorable gap improvement",
        "stop_gap_risk": "A stop threshold is not a guaranteed loss ceiling",
        "funding": "Not historical; excluded from simulator",
        "limitations": "No order book, liquidity, exchange lot sizes or liquidation engine",
    }
    return result


def data_regime_diagnostics(data: MarketData) -> dict:
    """Describe sample direction without altering any ranking or candidate."""
    returns = (data.close[-1] / data.close[0] - 1.) * 100.
    lows = (data.low.min(axis=0) / data.close[0] - 1.) * 100.
    return {
        "bars": int(len(data.times)), "start_ms": int(data.times[0]),
        "end_ms": int(data.times[-1]),
        "positive_return_coins": int(np.count_nonzero(returns > 0)),
        "coin_count": len(data.coins),
        "per_coin": {c: {"return_pct": float(returns[i]),
                          "lowest_vs_first_close_pct": float(lows[i])}
                     for i, c in enumerate(data.coins)},
        "caveat": "A rising sample cannot establish resilience across market regimes",
    }
