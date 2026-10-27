"""Strategy decision engine for the martingale ladder.

The strategy is a pure function of (current coin states, mark prices). Given those,
it emits a *desired* set of resting orders. A broker (paper or live) is responsible
for reconciling its actual open orders to the desired set.

Level semantics:
  state.level == 0 : no position (round not open)
  state.level == 1 : first entry filled
  state.level == 2 : first add at ladder_drops[0] filled
  ...
  state.level == N+1 : full ladder (where N = len(ladder_drops_pct))

Risk-break convention:
  "task reached level k" means `state.level >= k`, i.e. k fills done.
  Doc wording "第 4 档" = level 4 = 3 adds filled (one add from full).
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import BasketConfig, LadderParams, RiskLimits


EPS = 1e-9


@dataclass
class CoinState:
    coin: str
    first_entry_px: float | None = None
    position_coin: float = 0.0
    cost_basis_usd: float = 0.0  # position_coin * weighted_avg_entry
    level: int = 0
    open_time: str | None = None

    @property
    def wavg_entry(self) -> float:
        return self.cost_basis_usd / self.position_coin if self.position_coin > EPS else 0.0

    @property
    def in_position(self) -> bool:
        return self.position_coin > EPS


@dataclass(frozen=True)
class DesiredOrder:
    """A single resting-order intent. Broker reconciles to the full set."""

    coin: str
    side: str  # 'buy' | 'sell'
    px: float
    qty: float  # coin units (NOT contracts; Binance USDT-M takes coin qty directly)
    reduce_only: bool
    level_tag: int  # 0 = TP, 1 = first entry, 2 = L1 add, ...


class LadderStrategy:
    def __init__(
        self,
        params: LadderParams,
        basket: BasketConfig,
        risk: RiskLimits,
        first_entry_usd: float,
    ) -> None:
        self.params = params
        self.basket = basket
        self.risk = risk
        self.first_entry_usd = first_entry_usd
        self.states: dict[str, CoinState] = {c: CoinState(c) for c in basket.include}

    # ------------------------------------------------------------------ risk

    def should_pause_new_openings(self) -> tuple[bool, str]:
        """Mechanical circuit-breaker. Source strategy had none; we add this.

        Returns (paused, reason).
        """
        deep_count = sum(1 for s in self.states.values() if s.level >= self.risk.deep_level)
        for coin, s in self.states.items():
            if s.level >= self.risk.max_level_any_coin_before_pause:
                return True, f"{coin} at level {s.level} (>= {self.risk.max_level_any_coin_before_pause})"
        if deep_count >= self.risk.max_coins_at_deep_level:
            return True, f"{deep_count} coins at level >= {self.risk.deep_level}"
        return False, ""

    # ------------------------------------------------------------- intent

    def desired_orders(self, mark_px: dict[str, float]) -> list[DesiredOrder]:
        paused, _ = self.should_pause_new_openings()
        orders: list[DesiredOrder] = []
        for coin, s in self.states.items():
            if not self.basket.is_tradeable(coin):
                continue
            px = mark_px.get(coin)
            if px is None or px <= 0:
                continue

            if not s.in_position:
                if paused:
                    continue
                qty = self.first_entry_usd / px
                orders.append(DesiredOrder(coin, "buy", px, qty, False, level_tag=1))
                continue

            # In a round: TP + remaining ladder legs
            assert s.first_entry_px is not None
            tp_px = self.params.tp_price(s.wavg_entry)
            orders.append(
                DesiredOrder(coin, "sell", tp_px, s.position_coin, True, level_tag=0)
            )
            # s.level == 1 => need to post adds at drops[0..3]
            # s.level == 2 => need to post adds at drops[1..3]
            # s.level == N+1 (full) => no more adds
            for k in range(s.level - 1, len(self.params.ladder_drops_pct)):
                add_px = self.params.ladder_price(s.first_entry_px, k + 1)
                add_notional = self.params.level_notional_usd(self.first_entry_usd, k + 1)
                add_qty = add_notional / add_px
                orders.append(
                    DesiredOrder(coin, "buy", add_px, add_qty, False, level_tag=k + 2)
                )
        return orders

    # --------------------------------------------------------------- fills

    def on_fill(self, coin: str, side: str, px: float, qty: float, ts: str) -> float:
        """Apply a fill to coin state. Returns realized price PnL (0 on opens)."""
        s = self.states[coin]
        if side == "buy":
            if not s.in_position:
                s.first_entry_px = px
                s.open_time = ts
            s.cost_basis_usd += qty * px
            s.position_coin += qty
            s.level += 1
            return 0.0

        # sell
        wavg = s.wavg_entry
        pnl = qty * (px - wavg)
        s.cost_basis_usd -= qty * wavg
        s.position_coin -= qty
        if s.position_coin < 1e-6:
            s.first_entry_px = None
            s.open_time = None
            s.position_coin = 0.0
            s.cost_basis_usd = 0.0
            s.level = 0
        return pnl
