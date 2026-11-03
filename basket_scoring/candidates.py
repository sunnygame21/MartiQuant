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

from martiquant.market_data import fetch_symbol_info  # noqa: F401  (re-exported for callers)

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
