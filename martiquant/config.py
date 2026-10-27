"""Strategy parameters and exchange constants.

All numbers here are independently verified against the OKX CSV export in
/Users/zhangjiawei/repos/MartiQuant/okx-export (see analysis of 2026-10-20).
"""

from dataclasses import dataclass, field


# OKX contract face values (contracts -> coin units). The replay engine uses these
# to normalize sz_contracts in 02_orders_filled.csv into coin quantities.
# Binance USDT-M futures do NOT use contract face values — qty is already in coin units.
OKX_CONTRACT_FACE: dict[str, float] = {
    "SOL": 1,
    "XRP": 100,
    "ETH": 0.1,
    "BTC": 0.01,
    "DOGE": 1000,
    "XLM": 100,
    "SUI": 1,
    "AVAX": 1,
    "ADA": 100,
    "DOT": 1,
    "BCH": 0.1,
    "LINK": 1,
    "LTC": 1,
    "HBAR": 100,
    "SHIB": 1_000_000,
    "GRAM": 1,
}


@dataclass(frozen=True)
class LadderParams:
    """Parameters of a martingale ladder regime."""

    name: str
    # Price offset from first-entry price, as negative percents (e.g. -5.07 for the
    # first add-level 5.07% below first entry).
    ladder_drops_pct: tuple[float, ...]
    # USD-notional multiplier for each add level relative to the first entry USD.
    # Example: multipliers[0] = 2.78 means the first add is 2.78x the first entry's USD.
    size_multipliers_usd: tuple[float, ...]
    # Take-profit as percent above weighted-average entry.
    tp_pct: float
    # Suggested first-entry USD, before any dynamic equity scaling.
    first_entry_usd_default: float

    @property
    def n_levels(self) -> int:
        """Total open+add levels (first entry counts as 1)."""
        return 1 + len(self.ladder_drops_pct)

    def ladder_price(self, first_entry_px: float, level: int) -> float:
        """Return the limit price for add-level k (1-indexed, 0 = first entry).

        level=0 -> first_entry_px
        level=1..n -> first_entry_px * (1 + drop_k/100)
        """
        if level == 0:
            return first_entry_px
        return first_entry_px * (1.0 + self.ladder_drops_pct[level - 1] / 100.0)

    def level_notional_usd(self, first_entry_usd: float, level: int) -> float:
        """Return the USD notional for level k. level=0 -> first_entry_usd."""
        if level == 0:
            return first_entry_usd
        return first_entry_usd * self.size_multipliers_usd[level - 1]

    def tp_price(self, wavg_entry_px: float) -> float:
        return wavg_entry_px * (1.0 + self.tp_pct / 100.0)


# B 期 (2026-08 onwards) — current/verified parameters.
B_PERIOD = LadderParams(
    name="B",
    ladder_drops_pct=(-5.07, -9.13, -14.53, -25.30),
    size_multipliers_usd=(2.78, 10.46, 38.68, 80.0),
    tp_pct=0.361,
    first_entry_usd_default=320.0,
)


# A 期 (2025-12 to 2026-05) — historical, for reference / comparison backtests only.
A_PERIOD = LadderParams(
    name="A",
    ladder_drops_pct=(-3.13, -5.05, -7.00, -9.12, -11.68),
    size_multipliers_usd=(1.58, 4.07, 10.5, 27.0, 100.4),
    tp_pct=0.674,
    first_entry_usd_default=973.0,
)


@dataclass(frozen=True)
class BasketConfig:
    """Which coins to trade and which to blacklist."""

    include: tuple[str, ...]
    blacklist: tuple[str, ...] = ()

    def is_tradeable(self, coin: str) -> bool:
        return coin in self.include and coin not in self.blacklist


# Default basket for Binance paper trading. No blacklist: BTC/ADA's net loss in the
# OKX dataset came from manual de-risking, not the ladder rules, and excluding coins
# by realized historical PnL is hindsight bias. Coin selection uses ex-ante metrics
# (see basket_scoring/).
DEFAULT_BASKET = BasketConfig(
    include=("SOL", "XRP", "ETH", "DOGE", "XLM", "SUI", "AVAX", "DOT", "BCH", "LINK"),
)


@dataclass(frozen=True)
class RiskLimits:
    """Mechanical circuit-breaker rules. Source account had none — we add these."""

    # Pause new openings if any single coin fills level >= this
    max_level_any_coin_before_pause: int = 4
    # Pause new openings if this many coins are simultaneously at level >= deep_level
    max_coins_at_deep_level: int = 3
    deep_level: int = 3
    # Hard account-level DD% from peak equity -> flat everything
    max_account_dd_pct: float = 15.0


DEFAULT_RISK = RiskLimits()
