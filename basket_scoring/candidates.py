"""Candidate universe for basket scoring.

Rules for inclusion:
  - Top liquidity (24h volume > ~$50M)
  - Not a memecoin / short-lived narrative token
  - Not a wrapped / tokenized derivative

Hard gate applied in __main__: Binance USDT-M perpetual onboard date must be
older than MIN_ONBOARD_AGE_DAYS. Coins that pass the price screen but failed
one full market cycle on Binance get rejected — their "stable" look is a
data-length artifact, not a property of the coin.

BTC / ADA are included despite poor OKX in-sample results. The scoring system
must be allowed to disagree with that in-sample judgment on data grounds.
"""
from __future__ import annotations

import json
import logging
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

MIN_ONBOARD_AGE_DAYS = 730  # need >= 2 years of Binance USDT-M history


CURRENT_BASKET = (
    "SOL", "XRP", "ETH", "DOGE", "XLM",
    "SUI", "AVAX", "DOT", "BCH", "LINK",
)

ADDITIONAL_CANDIDATES = (
    "BTC", "ADA",
    "BNB", "TRX", "TON", "NEAR", "APT",
    "ATOM", "ARB", "OP", "FIL", "INJ",
)

ALL_CANDIDATES = CURRENT_BASKET + ADDITIONAL_CANDIDATES
BENCHMARK = "BTC"


_EXCHANGE_INFO_URL = "https://fapi.binance.com/fapi/v1/exchangeInfo"
_CACHE_PATH = Path("/tmp/martiquant_binance_onboard.json")


def fetch_onboard_dates() -> dict[str, int]:
    """Return {coin: onboard_date_ms} for all USDT-M perpetual symbols.

    Cached to disk; the exchangeInfo payload is ~2MB and changes rarely.
    """
    if _CACHE_PATH.exists():
        try:
            return {k: int(v) for k, v in json.loads(_CACHE_PATH.read_text()).items()}
        except Exception:
            logger.warning("onboard cache unreadable, refetching")

    req = urllib.request.Request(_EXCHANGE_INFO_URL,
                                 headers={"User-Agent": "martiquant/0.1"})
    with urllib.request.urlopen(req, timeout=30) as r:
        payload = json.loads(r.read())
    result: dict[str, int] = {}
    for sym in payload.get("symbols", []):
        if sym.get("contractType") != "PERPETUAL":
            continue
        if sym.get("quoteAsset") != "USDT":
            continue
        base = sym.get("baseAsset")
        onboard = sym.get("onboardDate") or 0
        if base and onboard:
            result[base] = int(onboard)
    _CACHE_PATH.write_text(json.dumps(result))
    return result
