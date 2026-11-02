"""Local paper-trading broker: simulates limit-order fills from OHLC bars.

Fill model (all orders assumed maker / post-only):
  - Buy  limit at px fills when the bar's low  <= px, at price px
  - Sell limit at px fills when the bar's high >= px, at price px
  - Fee = -px * qty * maker_fee_rate (Binance VIP0 futures maker = 0.02%)

This is a conservative-realistic model for the martingale ladder because:
  - Source strategy was 98.9% maker on exits / 79.9% maker on opens (verified)
  - 1-minute bars are granular enough for 5–25% ladder spacings; false fills from
    a bar touching an unrealistic micro-wick are rare at that spacing.

Known approximation: we don't model the first-entry "chase if not filled" taker
step. If the market moves straight up without touching our close-price limit,
the first entry just sits — mildly understating round count in strong trends.
"""

from __future__ import annotations

from dataclasses import dataclass

from .strategy import DesiredOrder


@dataclass
class LiveOrder:
    oid: int
    coin: str
    side: str
    px: float
    qty: float
    reduce_only: bool
    level_tag: int


@dataclass
class SimFill:
    coin: str
    side: str
    px: float
    qty: float
    fee: float
    ts: int  # bar open_time_ms
    level_tag: int


class PaperBroker:
    def __init__(self, maker_fee_rate: float = 0.0002) -> None:
        self.maker_fee_rate = maker_fee_rate
        self.open_orders: dict[int, LiveOrder] = {}
        self._next_id = 1
        self.n_placed = 0
        self.n_cancelled = 0

    def _key(self, coin: str, side: str, px: float, level_tag: int) -> tuple:
        # Round to 10 sig figs so float jitter doesn't cause phantom reconciles.
        return (coin, side, round(px, 10), level_tag)

    def reconcile(self, desired: list[DesiredOrder]) -> None:
        """Cancel orders not in `desired`; place orders not currently open."""
        want = {self._key(d.coin, d.side, d.px, d.level_tag): d for d in desired}
        have: dict[tuple, int] = {}
        for oid, o in self.open_orders.items():
            have[self._key(o.coin, o.side, o.px, o.level_tag)] = oid

        for k, oid in list(have.items()):
            if k not in want:
                del self.open_orders[oid]
                self.n_cancelled += 1

        for k, d in want.items():
            if k in have:
                continue
            self.open_orders[self._next_id] = LiveOrder(
                oid=self._next_id,
                coin=d.coin,
                side=d.side,
                px=d.px,
                qty=d.qty,
                reduce_only=d.reduce_only,
                level_tag=d.level_tag,
            )
            self._next_id += 1
            self.n_placed += 1

    def market_sell(self, coin: str, qty: float, mark_px: float, ts_ms: int,
                    taker_fee_rate: float, slippage: float) -> SimFill:
        """Simulated market sell of a whole position (risk stop, delisting).

        Fills at mark * (1 - slippage) as taker and cancels the coin's resting orders.
        level_tag -1 marks it as a forced exit rather than a take-profit.
        """
        for oid in [oid for oid, o in self.open_orders.items() if o.coin == coin]:
            del self.open_orders[oid]
            self.n_cancelled += 1
        px = mark_px * (1.0 - slippage)
        return SimFill(coin=coin, side="sell", px=px, qty=qty,
                       fee=-px * qty * taker_fee_rate, ts=ts_ms, level_tag=-1)

    def match_bar(self, coin: str, high: float, low: float, ts_ms: int) -> list[SimFill]:
        fills: list[SimFill] = []
        remove: list[int] = []
        for oid, o in self.open_orders.items():
            if o.coin != coin:
                continue
            hit = (o.side == "buy" and low <= o.px) or (o.side == "sell" and high >= o.px)
            if not hit:
                continue
            fee = -o.px * o.qty * self.maker_fee_rate
            fills.append(
                SimFill(
                    coin=o.coin,
                    side=o.side,
                    px=o.px,
                    qty=o.qty,
                    fee=fee,
                    ts=ts_ms,
                    level_tag=o.level_tag,
                )
            )
            remove.append(oid)
        for oid in remove:
            del self.open_orders[oid]
        return fills
