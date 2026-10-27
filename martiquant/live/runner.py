"""LiveRunner: asyncio orchestration of feed + strategy + broker + logger + state.

Reads 1m closed bars from WSFeed, drives PaperBroker.match_bar + LadderStrategy
exactly like paper_backtest.py — just streaming instead of looping.

See docs/2026-10-22-live-paper-design.md §3 and §7 for the full flow.
"""
from __future__ import annotations

import asyncio
import logging
import signal
import time
from pathlib import Path

from martiquant.config import BasketConfig, LadderParams, RiskLimits
from martiquant.market_data import Bar
from martiquant.paper_broker import PaperBroker
from martiquant.strategy import LadderStrategy

from .feed import WSFeed
from .logger import LiveLogger
from . import state as live_state

logger = logging.getLogger(__name__)


class LiveRunner:
    def __init__(
        self,
        params: LadderParams,
        basket: BasketConfig,
        risk: RiskLimits,
        first_entry_usd: float,
        equity_usd: float,
        maker_fee_bps: float,
        run_dir: Path,
        resume_from: Path | None = None,
    ) -> None:
        self.params = params
        self.basket = basket
        self.risk = risk
        self.equity_usd = equity_usd
        self.run_dir = run_dir

        self.strategy = LadderStrategy(params, basket, risk, first_entry_usd)
        self.broker = PaperBroker(maker_fee_rate=maker_fee_bps / 1e4)
        self.live_logger = LiveLogger(run_dir)

        self.mark: dict[str, float] = {}
        self.totals = {
            "price_pnl": 0.0,
            "fee": 0.0,
            "peak_eq": equity_usd,
            "worst_upl": 0.0,
            "n_rounds_closed": 0,
            "n_fills": 0,
        }
        self.hour_last = -1

        self._round_tracker: dict[str, dict] = {}

        self.feed = WSFeed(list(basket.include), self.on_bar)
        self._stopping = False
        self._lock = asyncio.Lock()

        if resume_from is not None:
            restored = live_state.load(resume_from / "state.json",
                                       self.strategy, self.broker)
            if restored is not None:
                self.totals, self.mark = restored
                self.live_logger.event("resumed", from_dir=str(resume_from),
                                       totals=self.totals,
                                       open_coins=sum(1 for s in self.strategy.states.values()
                                                      if s.in_position),
                                       open_orders=len(self.broker.open_orders))
            else:
                self.live_logger.event("resume_skipped", from_dir=str(resume_from),
                                       reason="stale_or_missing")

    async def on_bar(self, coin: str, bar: Bar) -> None:
        async with self._lock:
            fills = self.broker.match_bar(coin, bar.high, bar.low, bar.open_time_ms)

            for f in fills:
                was_open = self.strategy.states[coin].in_position
                if f.side == "buy" and not was_open:
                    self._round_tracker[coin] = {
                        "open_time_ms": f.ts,
                        "first_entry_px": f.px,
                        "max_level": 1,
                        "fee_paid": f.fee,
                    }

                pnl = self.strategy.on_fill(coin, f.side, f.px, f.qty, str(f.ts))
                s_after = self.strategy.states[coin]
                self.totals["price_pnl"] += pnl
                self.totals["fee"] += f.fee
                self.totals["n_fills"] += 1

                t = self._round_tracker.get(coin)
                if t is not None and f.side == "buy" and was_open:
                    t["max_level"] = max(t["max_level"], s_after.level)
                    t["fee_paid"] += f.fee
                if t is not None and f.side == "sell":
                    t["fee_paid"] += f.fee

                self.live_logger.event(
                    "fill", coin=coin, side=f.side, px=f.px, qty=f.qty,
                    fee=f.fee, pnl=pnl, level=s_after.level,
                    level_tag=f.level_tag,
                )

                self._maybe_deep_level_warn(coin, s_after.level)

                if f.side == "sell" and not s_after.in_position and t is not None:
                    self.live_logger.round({
                        "coin": coin,
                        "open_time_ms": t["open_time_ms"],
                        "close_time_ms": f.ts,
                        "hold_hours": (f.ts - t["open_time_ms"]) / 3_600_000,
                        "first_entry_px": t["first_entry_px"],
                        "close_px": f.px,
                        "max_level": t["max_level"],
                        "price_pnl": pnl,
                        "fee": t["fee_paid"],
                        "realized_pnl": pnl + t["fee_paid"],
                    })
                    self.totals["n_rounds_closed"] += 1
                    self._round_tracker.pop(coin, None)

            self.mark[coin] = bar.close
            hour_now = bar.open_time_ms // 3_600_000

            if fills or hour_now != self.hour_last:
                n_cx0 = self.broker.n_cancelled
                n_pl0 = self.broker.n_placed
                desired = self.strategy.desired_orders(self.mark)
                self.broker.reconcile(desired)
                self.live_logger.event(
                    "reconcile",
                    hour=hour_now,
                    reason=("fill" if fills else "hour_turn"),
                    n_desired=len(desired),
                    n_placed=self.broker.n_placed - n_pl0,
                    n_cancelled=self.broker.n_cancelled - n_cx0,
                )
                self.hour_last = hour_now

            paused, reason = self.strategy.should_pause_new_openings()
            if paused:
                self.live_logger.event("circuit_pause", reason=reason)

    def _maybe_deep_level_warn(self, coin: str, level: int) -> None:
        if level >= 4:
            self.live_logger.event("l4_reached", coin=coin, level=level)
        deep = [c for c, s in self.strategy.states.items() if s.level >= 3]
        if len(deep) >= 3:
            self.live_logger.event("multi_deep", coins=deep, n=len(deep))
        if len(deep) >= 8:
            self.live_logger.event(
                "margin_warn", n_deep=len(deep),
                note="≥8 coins deep; real account would be near 2x margin cap",
            )

    def _compute_equity(self) -> dict:
        upl = 0.0
        open_coins = 0
        for c, s in self.strategy.states.items():
            m = self.mark.get(c)
            if s.in_position and m is not None:
                upl += s.position_coin * (m - s.wavg_entry)
                open_coins += 1
        realized_eq = self.equity_usd + self.totals["price_pnl"] + self.totals["fee"]
        mtm_eq = realized_eq + upl
        if mtm_eq > self.totals["peak_eq"]:
            self.totals["peak_eq"] = mtm_eq
        if upl < self.totals["worst_upl"]:
            self.totals["worst_upl"] = upl
        peak = self.totals["peak_eq"]
        dd = (mtm_eq / peak - 1) * 100 if peak > 0 else 0.0
        return {
            "ts_ms": int(time.time() * 1000),
            "mtm_eq": round(mtm_eq, 4),
            "realized_eq": round(realized_eq, 4),
            "upl": round(upl, 4),
            "dd_pct_from_peak": round(dd, 4),
            "open_coins": open_coins,
            "open_rounds": sum(1 for s in self.strategy.states.values() if s.in_position),
            "orders_open": len(self.broker.open_orders),
        }

    async def _state_dump_loop(self) -> None:
        while not self._stopping:
            await asyncio.sleep(60)
            async with self._lock:
                try:
                    live_state.dump(
                        self.run_dir / "state.json",
                        self.strategy, self.broker, self.totals, self.mark,
                    )
                    snap = self._compute_equity()
                    self.live_logger.equity_snapshot(snap)
                except Exception:
                    logger.exception("state dump loop error")

    async def _hourly_reconcile_fallback(self) -> None:
        """Even if no bars arrive (full WS blackout), nudge reconcile once per hour."""
        while not self._stopping:
            await asyncio.sleep(3600)
            if not self.mark:
                continue
            async with self._lock:
                try:
                    desired = self.strategy.desired_orders(self.mark)
                    n_cx0 = self.broker.n_cancelled
                    n_pl0 = self.broker.n_placed
                    self.broker.reconcile(desired)
                    self.live_logger.event(
                        "reconcile_fallback",
                        n_desired=len(desired),
                        n_placed=self.broker.n_placed - n_pl0,
                        n_cancelled=self.broker.n_cancelled - n_cx0,
                    )
                except Exception:
                    logger.exception("hourly fallback error")

    async def _health_loop(self) -> None:
        while not self._stopping:
            await asyncio.sleep(300)
            now = time.time()
            stale = []
            for stream, t in self.feed.last_msg_time_by_stream.items():
                if now - t > 60:
                    stale.append({"stream": stream, "silent_sec": int(now - t)})
            if stale:
                self.live_logger.event("health_stale_streams", streams=stale)
                logger.warning("stale streams: %s", stale)

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, lambda: asyncio.create_task(self._shutdown()))
            except NotImplementedError:
                pass

        self.live_logger.event("startup",
                               params=self.params.name,
                               basket=list(self.basket.include),
                               equity_usd=self.equity_usd,
                               circuit_breaker_on=self.risk.max_level_any_coin_before_pause < 99)

        tasks = [
            asyncio.create_task(self.feed.start(), name="feed"),
            asyncio.create_task(self._state_dump_loop(), name="state_dump"),
            asyncio.create_task(self._hourly_reconcile_fallback(), name="hourly_fallback"),
            asyncio.create_task(self._health_loop(), name="health"),
        ]
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            pass

    async def _shutdown(self) -> None:
        if self._stopping:
            return
        self._stopping = True
        logger.info("shutdown requested")
        self.live_logger.event("shutdown_begin")
        await self.feed.stop()
        async with self._lock:
            try:
                live_state.dump(
                    self.run_dir / "state.json",
                    self.strategy, self.broker, self.totals, self.mark,
                )
                self.live_logger.equity_snapshot(self._compute_equity())
            except Exception:
                logger.exception("final dump failed")
        self.live_logger.event("shutdown_done", totals=self.totals)
        self.live_logger.close()
        for t in asyncio.all_tasks():
            if t is not asyncio.current_task():
                t.cancel()
