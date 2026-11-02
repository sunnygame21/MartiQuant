"""Download Binance USDT-M 1m klines from the public archive (data.binance.vision).

The archive is a CDN, so long backtests don't spend fapi request weight (the IP is
shared with other live processes). One .npz per coin-month:

  data/klines_1m/<COIN>/<YYYY-MM>.npz   arrays: t (open_time ms), o, h, l, c, v

Each zip is verified against its published .CHECKSUM. Months before a coin's
listing simply 404 and are skipped. Re-running only fetches missing months.
The archive itself has holes (e.g. 2026-10-28 is absent from both the monthly and
daily files); internal gaps are filled from fapi klines, one paced request per 1500
bars.

Usage:
  python3 scripts/download_klines_archive.py --coins SOL,XRP --start 2023-11 --end 2026-10
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from martiquant.archive import DATA_DIR  # noqa: E402
from martiquant.market_data import fetch_klines_page  # noqa: E402

BASE = "https://data.binance.vision/data/futures/um/monthly/klines"
BAR_MS = 60_000
REST_GAP_SEC = 1.0  # fapi is shared with live processes on this IP; stay far below its limit
_rest_lock = threading.Lock()


def _month_bounds(month: str) -> tuple[int, int]:
    """[first bar, first bar of next month) open times in ms (UTC)."""
    y, m = map(int, month.split("-"))
    ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
    to_ms = lambda yy, mm: int(np.datetime64(f"{yy:04d}-{mm:02d}-01", "ms").astype(np.int64))
    return to_ms(y, m), to_ms(ny, nm)


def _fill_gaps(coin: str, rows: list[list[float]], month: str) -> tuple[list[list[float]], int]:
    """Fill holes inside the month (incl. its edges) from fapi klines.

    Edge holes in a listing/delisting month cost one empty request each.
    """
    lo, hi = _month_bounds(month)
    t = [lo - BAR_MS] + [int(r[0]) for r in rows] + [hi]
    filled = 0
    for a, b in zip(t, t[1:]):
        cursor = a + BAR_MS
        while cursor < b:
            with _rest_lock:
                raw = fetch_klines_page(f"{coin}USDT", "1m", start_ms=cursor, end_ms=b - 1, limit=1500)
                time.sleep(REST_GAP_SEC)
            if not raw:
                break  # genuine exchange outage: nothing to fill
            rows.extend([float(x) for x in k[:6]] for k in raw)
            filled += len(raw)
            cursor = int(raw[-1][0]) + BAR_MS
    rows.sort(key=lambda r: r[0])
    return rows, filled


def _get(url: str) -> bytes | None:
    req = urllib.request.Request(url, headers={"User-Agent": "martiquant/0.1"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if attempt == 3:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt == 3:
                raise
    return None


def _months(start: str, end: str) -> list[str]:
    y, m = map(int, start.split("-"))
    ey, em = map(int, end.split("-"))
    out = []
    while (y, m) <= (ey, em):
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def fetch_month(coin: str, month: str) -> str:
    out = DATA_DIR / coin / f"{month}.npz"
    if out.exists():
        return "cached"
    name = f"{coin}USDT-1m-{month}.zip"
    url = f"{BASE}/{coin}USDT/1m/{name}"
    blob = _get(url)
    if blob is None:
        return "missing"
    checksum = _get(url + ".CHECKSUM")
    if checksum is None or checksum.split()[0].decode() != hashlib.sha256(blob).hexdigest():
        raise RuntimeError(f"checksum mismatch for {name}")
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        text = z.read(z.namelist()[0]).decode()
    rows = [[float(x) for x in r[:6]]
            for r in csv.reader(io.StringIO(text)) if r and r[0].isdigit()]  # newer files have a header
    rows, filled = _fill_gaps(coin, rows, month)
    a = np.array(rows)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp.npz")
    np.savez(tmp, t=a[:, 0].astype(np.int64), o=a[:, 1], h=a[:, 2], l=a[:, 3], c=a[:, 4], v=a[:, 5])
    tmp.replace(out)
    return f"{len(rows)} bars" + (f" ({filled} filled from fapi)" if filled else "")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--coins", required=True, help="Comma-separated, e.g. SOL,XRP,ETH")
    ap.add_argument("--start", required=True, help="First month, YYYY-MM")
    ap.add_argument("--end", required=True, help="Last month, YYYY-MM (must be published)")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    jobs = [(c.strip().upper(), m) for c in args.coins.split(",") if c.strip()
            for m in _months(args.start, args.end)]
    with ThreadPoolExecutor(args.workers) as ex:
        for (coin, month), res in zip(jobs, ex.map(lambda j: fetch_month(*j), jobs)):
            print(f"{coin} {month}: {res}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
