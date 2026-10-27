"""Position accounting for a single round (open -> add* -> close).

Round PnL math is computed strictly from the fill sequence, matching OKX's
positions-history convention:
  price_pnl = sum over sells of sz * (sell_px - weighted_avg_entry_at_that_moment)
  fee       = sum of all order fees (negative = paid)
  realized  = price_pnl + fee + funding_fee

The replay harness uses this to independently reconstruct every round in
`okx-export/01_positions_history.csv` from the fills in `02_orders_filled.csv`.
A match to <$0.01 across all 7,176 rounds is the acceptance criterion for the
accounting layer, before any live wiring.
"""

from __future__ import annotations

from dataclasses import dataclass, field


EPS = 1e-9


@dataclass(frozen=True)
class Fill:
    """A single fill (one row in 02_orders_filled.csv), already normalized."""

    time: str  # ISO-8601 string; we only use it for ordering
    side: str  # "buy" | "sell"
    sz_coin: float  # positive, in coin units (not contracts)
    px: float  # fill price (avg_fill_px for aggregated partials)
    fee: float  # negative = paid, positive = received
    reduce_only: bool


@dataclass
class Round:
    """Accumulator for one open->close cycle."""

    coin: str
    fills: list[Fill] = field(default_factory=list)

    def add_fill(self, fill: Fill) -> None:
        self.fills.append(fill)

    def _ordered(self) -> list[Fill]:
        return sorted(self.fills, key=lambda f: f.time)

    def compute(self) -> dict:
        """Return a dict with price_pnl, fee, position/px aggregates, and max notional."""
        pos = 0.0  # coin
        cost = 0.0  # cost basis at weighted-avg entry (USD) = pos * wavg
        price_pnl = 0.0
        fee_sum = 0.0

        max_pos_usd = 0.0
        n_opens = 0
        n_closes = 0

        open_notional_sum = 0.0
        open_qty_sum = 0.0
        close_notional_sum = 0.0
        close_qty_sum = 0.0

        for f in self._ordered():
            fee_sum += f.fee
            if f.side == "buy":
                n_opens += 1
                open_notional_sum += f.sz_coin * f.px
                open_qty_sum += f.sz_coin
                cost += f.sz_coin * f.px
                pos += f.sz_coin
                # Max USD notional at weighted-avg entry (NOT at mark); this matches
                # OKX's max_position_usd convention from the positions-history export.
                if cost > max_pos_usd:
                    max_pos_usd = cost
            elif f.side == "sell":
                n_closes += 1
                close_notional_sum += f.sz_coin * f.px
                close_qty_sum += f.sz_coin
                wavg = cost / pos if pos > EPS else 0.0
                price_pnl += f.sz_coin * (f.px - wavg)
                cost -= f.sz_coin * wavg
                pos -= f.sz_coin
                if pos < EPS:
                    pos = 0.0
                    cost = 0.0
            else:
                raise ValueError(f"unknown side: {f.side!r}")

        open_avg_px = open_notional_sum / open_qty_sum if open_qty_sum > EPS else 0.0
        close_avg_px = close_notional_sum / close_qty_sum if close_qty_sum > EPS else 0.0

        return {
            "n_fills": len(self.fills),
            "n_opens": n_opens,
            "n_closes": n_closes,
            "open_avg_px": open_avg_px,
            "close_avg_px": close_avg_px,
            "max_position_usd": max_pos_usd,
            "price_pnl": price_pnl,
            "fee": fee_sum,
            "realized_pnl_ex_funding": price_pnl + fee_sum,
            "residual_position": pos,
        }
