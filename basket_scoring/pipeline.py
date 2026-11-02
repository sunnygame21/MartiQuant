"""Gate candidates and score them as of a point in time.

Shared by the CLI (as_of = now) and the walk-forward backtest (as_of = each
rebalance date). Onboard and delivery dates from exchangeInfo are historical facts,
so the same gates replay correctly for past dates.
"""
from __future__ import annotations

from datetime import datetime, timezone

from martiquant.market_data import Bar

from .candidates import BENCHMARK, MIN_ONBOARD_AGE_DAYS
from .metrics import RawMetrics, btc_daily_returns, compute_metrics
from .score import DEFAULT_WINDOW_WEIGHTS, ScoredCoin, score_multi

DAY_MS = 86_400_000
DEFAULT_WINDOWS = (365, 180, 90)
# A window must have >= 90% of its hours present and >= 90% of them traded. Flat
# zero-volume stretches (halted or settled contracts) read as "no drawdown".
MIN_BAR_FRAC = 0.9
MIN_TRADED_FRAC = 0.9


def gate(candidates: tuple[str, ...], info: dict[str, dict], as_of_ms: int
         ) -> tuple[list[str], dict[str, str]]:
    """Return (approved coins, {rejected coin: reason})."""
    approved: list[str] = []
    rejected: dict[str, str] = {}
    for coin in candidates:
        s = info.get(coin)
        if s is None:
            rejected[coin] = "no USDT-M perpetual"
        elif s["delivery_ms"] <= as_of_ms:
            day = datetime.fromtimestamp(s["delivery_ms"] / 1000, timezone.utc).date()
            rejected[coin] = f"delisted (settled {day})"
        elif (age := (as_of_ms - s["onboard_ms"]) / DAY_MS) < MIN_ONBOARD_AGE_DAYS:
            rejected[coin] = f"listed {int(age)}d < {MIN_ONBOARD_AGE_DAYS}d"
        else:
            approved.append(coin)
    return approved, rejected


def score_as_of(
    coin_bars: dict[str, list[Bar]],
    approved: list[str],
    as_of_ms: int,
    windows: tuple[int, ...] = DEFAULT_WINDOWS,
) -> tuple[list[ScoredCoin], dict[int, list[RawMetrics]]]:
    """Score approved coins on 1h bars with open_time < as_of_ms.

    coin_bars must include BENCHMARK. Coins failing a window's data check are
    dropped from that window, and score_multi keeps only coins present in all.
    """
    metrics_by_window: dict[int, list[RawMetrics]] = {}
    for days in windows:
        win_start = as_of_ms - days * DAY_MS
        btc_win = [b for b in coin_bars[BENCHMARK] if win_start <= b.open_time_ms < as_of_ms]
        btc_days_ret, btc_rets = btc_daily_returns(btc_win)
        win_metrics = []
        for coin in approved:
            win_bars = [b for b in coin_bars.get(coin, ()) if win_start <= b.open_time_ms < as_of_ms]
            if len(win_bars) < 24 * days * MIN_BAR_FRAC:
                continue
            if sum(b.volume > 0 for b in win_bars) < len(win_bars) * MIN_TRADED_FRAC:
                continue
            win_metrics.append(compute_metrics(coin, win_bars, btc_days_ret, btc_rets))
        metrics_by_window[days] = win_metrics
    return score_multi(metrics_by_window, DEFAULT_WINDOW_WEIGHTS), metrics_by_window
