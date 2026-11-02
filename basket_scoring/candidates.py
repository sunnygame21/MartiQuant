"""Candidate universe for basket scoring.

Rules for inclusion:
  - Top liquidity (24h volume > ~$50M)
  - Not a memecoin / short-lived narrative token
  - Not a wrapped / tokenized derivative

Hard gates applied in pipeline.gate(), evaluated as of the scoring date:
  - Binance USDT-M perpetual onboard date older than MIN_ONBOARD_AGE_DAYS. Coins
    that pass the price screen but failed one full market cycle on Binance get
    rejected — their "stable" look is a data-length artifact, not a property of
    the coin.
  - Not delisted: a perpetual that is settling/settled keeps a deliveryDate in the
    past and keeps printing flat zero-volume bars (TONUSDT since 2026-06-23), which
    otherwise score as "no drawdown, no BTC correlation".

BTC / ADA are included despite poor OKX in-sample results. The scoring system
must be allowed to disagree with that in-sample judgment on data grounds.
"""
from __future__ import annotations

import json
import urllib.request

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


def fetch_symbol_info() -> dict[str, dict[str, int | str]]:
    """Return {coin: {onboard_ms, delivery_ms, status}} for USDT-M perpetuals.

    Fetched fresh on every call: a cached copy is exactly what hides a delisting.
    Live perpetuals carry a far-future deliveryDate; delisted ones the settle time.
    """
    req = urllib.request.Request(_EXCHANGE_INFO_URL,
                                 headers={"User-Agent": "martiquant/0.1"})
    with urllib.request.urlopen(req, timeout=30) as r:
        payload = json.loads(r.read())
    result: dict[str, dict[str, int | str]] = {}
    for sym in payload.get("symbols", []):
        if sym.get("contractType") != "PERPETUAL" or sym.get("quoteAsset") != "USDT":
            continue
        base = sym.get("baseAsset")
        if base and sym.get("onboardDate"):
            result[base] = {
                "onboard_ms": int(sym["onboardDate"]),
                "delivery_ms": int(sym.get("deliveryDate") or 4_133_404_800_000),
                "status": sym.get("status", ""),
            }
    return result
