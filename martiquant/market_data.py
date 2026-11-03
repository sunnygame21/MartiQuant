"""Binance USDT-M futures public market data (no API key).

Public endpoints only — klines for backtest, bookTicker/markPrice WebSocket for
future live paper. REST base: fapi.binance.com. Rate limits are per-IP, generous
for klines (weight 2 per request, 2400/min limit).
"""

from __future__ import annotations

import http.client
import json
import pickle
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

BINANCE_FUTURES_REST = "https://fapi.binance.com"

# Disk cache for fetched bars so a 503 mid-run doesn't lose prior progress.
CACHE_DIR = Path("/tmp/martiquant_klines")


@dataclass
class Bar:
    coin: str
    open_time_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float


def _get_json(url: str, retries: int = 6, backoff: float = 2.0) -> list:
    last_err: Exception | None = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "martiquant/0.1"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except (
            urllib.error.URLError,
            urllib.error.HTTPError,
            TimeoutError,
            ConnectionError,
            http.client.RemoteDisconnected,
            http.client.BadStatusLine,
            socket.error,
        ) as e:
            last_err = e
            wait = backoff * (2 ** i)
            print(f"    [retry {i+1}/{retries}] {type(e).__name__} — sleeping {wait:.1f}s", flush=True)
            time.sleep(wait)
    raise RuntimeError(f"Binance fetch failed after {retries} retries: {last_err}")


def fetch_klines_page(
    symbol: str,
    interval: str = "1m",
    start_ms: int | None = None,
    end_ms: int | None = None,
    limit: int = 1500,
) -> list[list]:
    params = [f"symbol={symbol}", f"interval={interval}", f"limit={limit}"]
    if start_ms is not None:
        params.append(f"startTime={start_ms}")
    if end_ms is not None:
        params.append(f"endTime={end_ms}")
    url = f"{BINANCE_FUTURES_REST}/fapi/v1/klines?" + "&".join(params)
    return _get_json(url)


def fetch_symbol_info() -> dict[str, dict[str, int | str]]:
    """Return {coin: {onboard_ms, delivery_ms, status}} for USDT-M perpetuals.

    Fetched fresh on every call: a cached copy is exactly what hides a delisting.
    Live perpetuals carry a far-future deliveryDate (2100); a delisted or
    delisting-announced one carries its settlement time. Request weight 1.
    """
    payload = _get_json(f"{BINANCE_FUTURES_REST}/fapi/v1/exchangeInfo", retries=2)
    result: dict[str, dict[str, int | str]] = {}
    for sym in payload.get("symbols", []):
        if sym.get("contractType") != "PERPETUAL" or sym.get("quoteAsset") != "USDT":
            continue
        base = sym.get("baseAsset")
        if base and sym.get("onboardDate"):
            result[base] = {
                "onboard_ms": int(sym["onboardDate"]),
                "delivery_ms": int(sym.get("deliveryDate") or LIVE_PERP_DELIVERY_MS),
                "status": sym.get("status", ""),
            }
    return result


# Live perpetuals report deliveryDate 4133404800000 (2100-12-25); anything earlier is
# a scheduled or past settlement.
LIVE_PERP_DELIVERY_MS = 4_133_404_800_000

# 1m interval = 60_000 ms per bar
_INTERVAL_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000}


def _load_all_cached_bars(coin: str, interval: str) -> dict[int, Bar]:
    """Union every cached pkl file for (coin, interval) into one dict keyed by open_time_ms."""
    if not CACHE_DIR.exists():
        return {}
    pool: dict[int, Bar] = {}
    for p in CACHE_DIR.glob(f"{coin}_{interval}_*.pkl"):
        try:
            with p.open("rb") as f:
                for b in pickle.load(f):
                    pool.setdefault(b.open_time_ms, b)
        except Exception:
            continue
    return pool


def _find_gaps(cached_times: set[int], start_ms: int, end_ms: int, step: int) -> list[tuple[int, int]]:
    """Return [start,end] windows (inclusive-exclusive) that need fetching.

    We treat any bar whose open_time_ms falls in [start_ms, end_ms) as expected.
    Walk through expected timestamps; collapse consecutive missing ones into windows.
    Round each window out to the next 1500-bar page to minimize request count.
    """
    gaps: list[tuple[int, int]] = []
    cur = start_ms - (start_ms % step)  # align to interval
    gap_start: int | None = None
    while cur < end_ms:
        if cur not in cached_times:
            if gap_start is None:
                gap_start = cur
        else:
            if gap_start is not None:
                gaps.append((gap_start, cur))
                gap_start = None
        cur += step
    if gap_start is not None:
        gaps.append((gap_start, end_ms))
    return gaps


def fetch_bars(
    coin: str,
    start_ms: int,
    end_ms: int,
    interval: str = "1m",
    use_cache: bool = True,
) -> list[Bar]:
    """Fetch bars in [start_ms, end_ms), reusing any overlapping cache.

    Cache model: one pkl per (coin, interval, range) fetch, but on load we UNION
    all pkls for the (coin, interval). A 360d request after a 120d fetch only
    pays to download the uncovered prefix, not the whole range again.
    """
    symbol = f"{coin}USDT"
    step = _INTERVAL_MS.get(interval, 60_000)

    cached = _load_all_cached_bars(coin, interval) if use_cache else {}
    cached_times = set(cached.keys())
    gaps = _find_gaps(cached_times, start_ms, end_ms, step) if use_cache else [(start_ms, end_ms)]

    newly_fetched: list[Bar] = []
    for gap_start, gap_end in gaps:
        cursor = gap_start
        while cursor < gap_end:
            raw = fetch_klines_page(symbol, interval, start_ms=cursor, end_ms=gap_end, limit=1500)
            if not raw:
                break
            for k in raw:
                newly_fetched.append(
                    Bar(
                        coin=coin,
                        open_time_ms=int(k[0]),
                        open=float(k[1]),
                        high=float(k[2]),
                        low=float(k[3]),
                        close=float(k[4]),
                        volume=float(k[5]),
                    )
                )
            last_t = int(raw[-1][0])
            if last_t <= cursor:
                break
            cursor = last_t + step
            time.sleep(0.05)

    # Persist only the new slice, keyed by its own range (future unions pick it up).
    if use_cache and newly_fetched:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        hour = 3_600_000
        s = (newly_fetched[0].open_time_ms // hour) * hour
        e = ((newly_fetched[-1].open_time_ms + hour) // hour) * hour
        slice_path = CACHE_DIR / f"{coin}_{interval}_{s}_{e}.pkl"
        with slice_path.open("wb") as f:
            pickle.dump(newly_fetched, f)
        for b in newly_fetched:
            cached[b.open_time_ms] = b

    # Return bars in requested window, chronological.
    want = [b for t, b in cached.items() if start_ms <= t < end_ms] if use_cache else newly_fetched
    want.sort(key=lambda b: b.open_time_ms)
    return want
