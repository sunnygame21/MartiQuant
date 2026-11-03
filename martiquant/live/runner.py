"""LiveRunner: asyncio orchestration of feed + strategy + broker + logger + state.

Reads 1m closed bars from WSFeed, drives PaperBroker.match_bar + LadderStrategy
exactly like paper_backtest.py — just streaming instead of looping.

Gap handling: each coin remembers the open_time of its last processed bar. Duplicate
or stale bars are dropped. When a bar arrives after a gap (WS reconnect, host sleep,
resume), the missing bars for *all* coins are fetched via REST and replayed in
paper_backtest order (stable sort by open_time, ties in basket order) before the
new bar is processed.

See docs/2026-10-22-live-paper-design.md §3 and §7 for the full flow.
"""
from __future__ import annotations

import asyncio
import logging
import signal
import time
from pathlib import Path

from martiquant.config import BasketConfig, LadderParams, RiskLimits
from martiquant.market_data import Bar, fetch_bars
from martiquant.paper_broker import PaperBroker, SimFill
from martiquant.strategy import LadderStrategy

from .feed import WSFeed
from .logger import LiveLogger
from . import state as live_state

logger = logging.getLogger(__name__)

BAR_MS = 60_000
SETTLE_FEE_RATE = 0.0005  # Binance VIP0 taker; same as backtest delisting settlement


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
        settle: tuple[str, ...] = (),
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
        self.last_bar_ms: dict[str, int] = {}
        self._paused_last = False

        self.feed = WSFeed(list(basket.include), self.on_bar)
        self._stopping = False
        self._lock = asyncio.Lock()

        if resume_from is not None:
            restored = live_state.load(resume_from / "state.json",
                                       self.strategy, self.broker)
            if restored is not None:
                self.totals, self.mark, extra = restored
                self.last_bar_ms = extra["last_bar_ms"]
                self._round_tracker = extra["round_tracker"]
                self._settle_dropped(settle)
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
            last = self.last_bar_ms.get(coin)
            if last is not None and bar.open_time_ms <= last:
                return
            if last is not None and bar.open_time_ms > last + BAR_MS:
                await self._catch_up(bar.open_time_ms)
            self._process_bar(coin, bar)

    async def _catch_up(self, until_ms: int) -> None:
        """Fetch and replay every coin's missing bars with open_time < until_ms."""
        gaps = {
            c: last + BAR_MS
            for c in self.basket.include
            if (last := self.last_bar_ms.get(c)) is not None and last + BAR_MS < until_ms
        }
        bars: list[Bar] = []
        n_by_coin: dict[str, int] = {}
        try:
            for c, start_ms in gaps.items():
                got = await asyncio.to_thread(fetch_bars, c, start_ms, until_ms, "1m", False)
                got = [b for b in got if start_ms <= b.open_time_ms < until_ms]
                n_by_coin[c] = len(got)
                bars.extend(got)
        except Exception as e:
            logger.exception("backfill fetch failed")
            self.live_logger.event("backfill_failed", until_ms=until_ms,
                                   gaps_from_ms=gaps, error=repr(e))
            return
        bars.sort(key=lambda b: b.open_time_ms)
        for b in bars:
            if b.open_time_ms > self.last_bar_ms[b.coin]:
                self._process_bar(b.coin, b)
        self.live_logger.event(
            "backfill",
            until_ms=until_ms,
            gap_min={c: (until_ms - s) // BAR_MS for c, s in gaps.items()},
            n_bars=n_by_coin,
        )

    def _settle_dropped(self, settle: tuple[str, ...]) -> None:
        """Handle coins in the resumed snapshot that are no longer in --basket.

        A flat one just leaves. An open round is closed at its last mark (taker fee,
        like an exchange delisting settlement) only if the coin is named in `settle`;
        otherwise refuse to start rather than silently drop the position and its P&L.
        """
        dropped = [c for c in self.strategy.states if c not in self.basket.include]
        stuck = [c for c in dropped if self.strategy.states[c].in_position and c not in settle]
        if stuck:
            raise SystemExit(f"open rounds in {stuck} but they are not in --basket; "
                             f"add them back or pass --settle {','.join(stuck)}")
        for coin in dropped:
            s = self.strategy.states[coin]
            if s.in_position:
                ts = self.last_bar_ms.get(coin, int(time.time() * 1000))
                f = self.broker.market_sell(coin, s.position_coin, self.mark[coin], ts,
                                            SETTLE_FEE_RATE, 0.0)
                self._apply_fill(coin, f)
                self.live_logger.event("settled", coin=coin, px=f.px, qty=f.qty, fee=f.fee)
            for oid in [oid for oid, o in self.broker.open_orders.items() if o.coin == coin]:
                del self.broker.open_orders[oid]
                self.broker.n_cancelled += 1
            del self.strategy.states[coin]
            self.mark.pop(coin, None)
            self.last_bar_ms.pop(coin, None)
            self._round_tracker.pop(coin, None)

    def _apply_fill(self, coin: str, f: SimFill) -> None:
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
            level_tag=f.level_tag, bar_ms=f.ts,
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

    def _process_bar(self, coin: str, bar: Bar) -> None:
        fills = self.broker.match_bar(coin, bar.high, bar.low, bar.open_time_ms)
        for f in fills:
            self._apply_fill(coin, f)

        self.mark[coin] = bar.close
        self.last_bar_ms[coin] = bar.open_time_ms
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
        if paused != self._paused_last:
            if paused:
                self.live_logger.event("circuit_pause", reason=reason, bar_ms=bar.open_time_ms)
            else:
                self.live_logger.event("circuit_resume", bar_ms=bar.open_time_ms)
            self._paused_last = paused

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
                        self.last_bar_ms, self._round_tracker,
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
                    self.last_bar_ms, self._round_tracker,
                )
                self.live_logger.equity_snapshot(self._compute_equity())
            except Exception:
                logger.exception("final dump failed")
        self.live_logger.event("shutdown_done", totals=self.totals)
        self.live_logger.close()
        for t in asyncio.all_tasks():
            if t is not asyncio.current_task():
                t.cancel()
