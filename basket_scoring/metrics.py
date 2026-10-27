"""Compute 7 raw metrics per candidate from 1h klines over the lookback window.

Each coin → dict of raw numbers. No normalization here, just facts.

Metrics (direction = better when):
  liquidity_usd_daily        higher
  max_drawdown_pct           less negative (closer to 0)
  longest_underwater_days    smaller
  atr_pct_daily              higher (martingale wants volatility)
  corr_with_btc              lower (diversification)
  annual_return_pct          higher
  monthly_down_months        smaller
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable

import numpy as np

from martiquant.market_data import Bar


@dataclass
class RawMetrics:
    coin: str
    n_bars: int
    liquidity_usd_daily: float
    max_drawdown_pct: float
    longest_underwater_days: float
    atr_pct_daily: float
    corr_with_btc: float
    annual_return_pct: float
    monthly_down_months: int


def _hourly_arrays(bars: list[Bar]) -> tuple[np.ndarray, ...]:
    bars = sorted(bars, key=lambda b: b.open_time_ms)
    t = np.array([b.open_time_ms for b in bars], dtype=np.int64)
    o = np.array([b.open for b in bars], dtype=np.float64)
    h = np.array([b.high for b in bars], dtype=np.float64)
    l = np.array([b.low for b in bars], dtype=np.float64)
    c = np.array([b.close for b in bars], dtype=np.float64)
    v = np.array([b.volume for b in bars], dtype=np.float64)
    return t, o, h, l, c, v


def _daily_close(t_ms: np.ndarray, close: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    day = t_ms // 86_400_000
    uniq, idx = np.unique(day, return_index=True)
    last_idx = np.append(idx[1:] - 1, len(close) - 1)
    return uniq, close[last_idx]


def _max_drawdown_pct(daily_close: np.ndarray) -> float:
    peak = np.maximum.accumulate(daily_close)
    dd = daily_close / peak - 1.0
    return float(dd.min() * 100.0)


def _longest_underwater_days(daily_close: np.ndarray) -> float:
    peak = np.maximum.accumulate(daily_close)
    underwater = daily_close < peak
    longest = cur = 0
    for u in underwater:
        if u:
            cur += 1
            longest = max(longest, cur)
        else:
            cur = 0
    return float(longest)


def _liquidity_usd_daily(t_ms: np.ndarray, close: np.ndarray, volume: np.ndarray) -> float:
    day = t_ms // 86_400_000
    uniq_days = np.unique(day)
    if len(uniq_days) == 0:
        return 0.0
    usd_per_bar = close * volume
    total_usd = usd_per_bar.sum()
    n_days = len(uniq_days)
    return float(total_usd / n_days)


def _atr_pct_daily(t_ms: np.ndarray, high: np.ndarray, low: np.ndarray,
                   close: np.ndarray) -> float:
    day = t_ms // 86_400_000
    uniq_days, idx = np.unique(day, return_index=True)
    last_idx = np.append(idx[1:] - 1, len(close) - 1)
    highs = np.array([high[a:b + 1].max() for a, b in zip(idx, last_idx)])
    lows = np.array([low[a:b + 1].min() for a, b in zip(idx, last_idx)])
    closes = close[last_idx]
    prev_close = np.roll(closes, 1)
    prev_close[0] = closes[0]
    tr = np.maximum(highs - lows,
                    np.maximum(np.abs(highs - prev_close),
                               np.abs(lows - prev_close)))
    atr_pct = (tr / closes * 100.0)
    return float(np.mean(atr_pct))


def _daily_returns(t_ms: np.ndarray, close: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    days, closes = _daily_close(t_ms, close)
    rets = np.diff(closes) / closes[:-1]
    return days[1:], rets


def _corr_with_btc(t_ms: np.ndarray, close: np.ndarray,
                   btc_days: np.ndarray, btc_rets: np.ndarray) -> float:
    days, rets = _daily_returns(t_ms, close)
    btc_map = {d: r for d, r in zip(btc_days, btc_rets)}
    aligned_a = []
    aligned_b = []
    for d, r in zip(days, rets):
        if d in btc_map:
            aligned_a.append(r)
            aligned_b.append(btc_map[d])
    if len(aligned_a) < 30:
        return 0.0
    a = np.array(aligned_a)
    b = np.array(aligned_b)
    if a.std() == 0 or b.std() == 0:
        return 0.0
    return float(np.corrcoef(a, b)[0, 1])


def _annual_return_pct(t_ms: np.ndarray, close: np.ndarray) -> float:
    days, closes = _daily_close(t_ms, close)
    if len(closes) < 2:
        return 0.0
    total_return = closes[-1] / closes[0] - 1
    years = len(closes) / 365.0
    if years <= 0:
        return 0.0
    try:
        annual = (1 + total_return) ** (1 / years) - 1
    except (ValueError, OverflowError):
        return float(total_return * 100.0)
    if math.isnan(annual) or math.isinf(annual):
        return float(total_return * 100.0)
    return float(annual * 100.0)


def _monthly_down_months(t_ms: np.ndarray, close: np.ndarray) -> int:
    days, closes = _daily_close(t_ms, close)
    if len(closes) < 2:
        return 0
    month = (days // 30).astype(int)
    uniq, idx = np.unique(month, return_index=True)
    last_idx = np.append(idx[1:] - 1, len(closes) - 1)
    monthly_closes = closes[last_idx]
    rets = np.diff(monthly_closes) / monthly_closes[:-1]
    return int((rets < 0).sum())


def compute_metrics(coin: str, bars: list[Bar],
                    btc_days: np.ndarray, btc_rets: np.ndarray) -> RawMetrics:
    t, o, h, l, c, v = _hourly_arrays(bars)
    _, daily_c = _daily_close(t, c)
    return RawMetrics(
        coin=coin,
        n_bars=len(bars),
        liquidity_usd_daily=_liquidity_usd_daily(t, c, v),
        max_drawdown_pct=_max_drawdown_pct(daily_c),
        longest_underwater_days=_longest_underwater_days(daily_c),
        atr_pct_daily=_atr_pct_daily(t, h, l, c),
        corr_with_btc=_corr_with_btc(t, c, btc_days, btc_rets),
        annual_return_pct=_annual_return_pct(t, c),
        monthly_down_months=_monthly_down_months(t, c),
    )


def btc_daily_returns(bars: list[Bar]) -> tuple[np.ndarray, np.ndarray]:
    t, _, _, _, c, _ = _hourly_arrays(bars)
    return _daily_returns(t, c)
