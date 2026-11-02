"""Normalize raw metrics to 0-100 percentile ranks, then weighted sum.

Multi-window: metrics are computed per lookback window (e.g. 365d / 180d / 90d).
We percentile-rank within each window separately, then combine with window
weights. A coin that scores high ONLY in the 365d window drops out — exactly
what we want to filter "lucky single window" survivorship.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .metrics import RawMetrics


METRIC_WEIGHTS = {
    "liquidity_usd_daily": (0.25, True),
    "max_drawdown_pct": (0.20, True),
    "longest_underwater_days": (0.20, False),
    "atr_pct_daily": (0.10, True),
    "corr_with_btc": (0.10, False),
    "annual_return_pct": (0.10, True),
    "monthly_down_months": (0.05, False),
}


DEFAULT_WINDOW_WEIGHTS = {365: 0.50, 180: 0.30, 90: 0.20}


@dataclass
class WindowScore:
    """Per-window per-coin percentile ranks and weighted sum."""
    days: int
    total: float
    components: dict[str, float]
    raw: dict[str, float]


@dataclass
class ScoredCoin:
    coin: str
    total: float                                   # weighted across windows
    per_window: dict[int, WindowScore]             # days → WindowScore


def _percentile_rank(values: np.ndarray, higher_is_better: bool) -> np.ndarray:
    """0-100 rank, 100 = best. Equal values share their mean position.

    Ties are common in the integer metrics (e.g. several coins underwater for the
    whole 365d window); breaking them by input order made scores order-dependent.
    """
    v = np.asarray(values, dtype=np.float64)
    if not higher_is_better:
        v = -v
    pos = np.empty(len(v), dtype=np.float64)
    pos[np.argsort(v, kind="stable")] = np.arange(len(v))
    for val in np.unique(v):
        tied = v == val
        pos[tied] = pos[tied].mean()
    return pos / max(len(v) - 1, 1) * 100.0


def _score_one_window(metrics: list[RawMetrics]) -> dict[str, WindowScore]:
    normed: dict[str, np.ndarray] = {}
    for name, (_, higher) in METRIC_WEIGHTS.items():
        vals = np.array([getattr(m, name) for m in metrics], dtype=np.float64)
        normed[name] = _percentile_rank(vals, higher)

    out: dict[str, WindowScore] = {}
    for i, m in enumerate(metrics):
        components = {name: float(normed[name][i]) for name in METRIC_WEIGHTS}
        total = sum(components[name] * weight
                    for name, (weight, _) in METRIC_WEIGHTS.items())
        raw = {name: float(getattr(m, name)) for name in METRIC_WEIGHTS}
        out[m.coin] = WindowScore(days=0, total=total, components=components, raw=raw)
    return out


def score_multi(
    metrics_by_window: dict[int, list[RawMetrics]],
    window_weights: dict[int, float] | None = None,
) -> list[ScoredCoin]:
    """metrics_by_window: {days: [RawMetrics for each coin in that window]}.

    Only coins that appear in EVERY window are returned (a coin with
    insufficient history in one of the windows is excluded).
    """
    if not metrics_by_window:
        return []
    weights = window_weights or DEFAULT_WINDOW_WEIGHTS
    weights = {d: w for d, w in weights.items() if d in metrics_by_window}
    total_w = sum(weights.values())
    if total_w <= 0:
        raise ValueError("no overlapping window weights")
    weights = {d: w / total_w for d, w in weights.items()}

    scored_per_window: dict[int, dict[str, WindowScore]] = {}
    coin_sets: list[set[str]] = []
    for days, mlist in metrics_by_window.items():
        s = _score_one_window(mlist)
        for ws in s.values():
            ws.days = days
        scored_per_window[days] = s
        coin_sets.append(set(s.keys()))
    common_coins = set.intersection(*coin_sets) if coin_sets else set()

    results: list[ScoredCoin] = []
    for coin in common_coins:
        per_window = {days: scored_per_window[days][coin]
                      for days in scored_per_window}
        total = sum(per_window[days].total * weights[days]
                    for days in weights)
        results.append(ScoredCoin(coin=coin, total=total, per_window=per_window))
    results.sort(key=lambda s: s.total, reverse=True)
    return results
