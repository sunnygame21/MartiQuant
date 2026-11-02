"""Local 1m kline store built by scripts/download_klines_archive.py.

Layout: data/klines_1m/<COIN>/<YYYY-MM>.npz with arrays t (open_time ms), o, h, l, c, v.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from .market_data import Bar

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "klines_1m"
HOUR_MS = 3_600_000


def load_coin(coin: str, start_ms: int, end_ms: int) -> dict[str, np.ndarray]:
    """Concatenate a coin's bars with open_time in [start_ms, end_ms)."""
    parts = [np.load(p) for p in sorted((DATA_DIR / coin).glob("*.npz"))]
    if not parts:
        raise FileNotFoundError(f"no archive data for {coin} under {DATA_DIR / coin}")
    out = {k: np.concatenate([p[k] for p in parts]) for k in ("t", "o", "h", "l", "c", "v")}
    order = np.argsort(out["t"], kind="stable")
    keep = (out["t"][order] >= start_ms) & (out["t"][order] < end_ms)
    return {k: v[order][keep] for k, v in out.items()}


def last_bar_ms(coin: str) -> int:
    """Open time of the newest archived bar for the coin."""
    newest = sorted((DATA_DIR / coin).glob("*.npz"))[-1]
    return int(np.load(newest)["t"].max())


def hourly_bars(coin: str, start_ms: int, end_ms: int) -> list[Bar]:
    """1h OHLCV aggregated from archived 1m bars, as fetch_bars(..., "1h") would return."""
    d = load_coin(coin, start_ms, end_ms)
    if len(d["t"]) == 0:
        return []
    hour = d["t"] // HOUR_MS
    idx = np.flatnonzero(np.r_[True, hour[1:] != hour[:-1]])
    last = np.r_[idx[1:] - 1, len(hour) - 1]
    high = np.maximum.reduceat(d["h"], idx)
    low = np.minimum.reduceat(d["l"], idx)
    vol = np.add.reduceat(d["v"], idx)
    return [Bar(coin, int(hour[i] * HOUR_MS), float(d["o"][i]), float(h), float(lo),
                float(d["c"][j]), float(v))
            for i, j, h, lo, v in zip(idx, last, high, low, vol)]
