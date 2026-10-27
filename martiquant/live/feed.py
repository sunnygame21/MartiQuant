"""Binance USDT-M Futures public WebSocket feed: kline_1m streams per coin.

One concurrent WS connection per coin. Only `k.x == true` (bar closed) events
are forwarded to the callback. No API key required.

Pattern borrowed from quant-analytics live/ws_feed.py:
  - exponential backoff reconnect (base 1s → cap 60s)
  - per-stream last_msg_time for health checks
  - 50 consecutive callback errors forces a reconnect
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Awaitable, Callable

import websockets

from martiquant.market_data import Bar

logger = logging.getLogger(__name__)

MARKET_BASE = "wss://fstream.binance.com/market/ws"

MAX_CONSECUTIVE_ERRORS = 50
PING_INTERVAL_SEC = 20
RECONNECT_BASE_SEC = 1.0
RECONNECT_CAP_SEC = 60.0


OnBar = Callable[[str, Bar], Awaitable[None]]


class WSFeed:
    def __init__(self, coins: list[str], on_bar: OnBar) -> None:
        self.coins = list(coins)
        self.on_bar = on_bar
        self._running = True
        self._ws_refs: list = []
        self.last_msg_time: float = 0.0
        self.last_msg_time_by_stream: dict[str, float] = {}

    async def start(self) -> None:
        tasks = [self._run_stream(coin) for coin in self.coins]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def stop(self) -> None:
        self._running = False
        for ws in self._ws_refs:
            try:
                await ws.close()
            except Exception:
                pass
        self._ws_refs.clear()

    async def _run_stream(self, coin: str) -> None:
        stream = f"{coin.lower()}usdt@kline_1m"
        url = f"{MARKET_BASE}/{stream}"
        delay = RECONNECT_BASE_SEC

        while self._running:
            ws = None
            try:
                ws = await websockets.connect(
                    url,
                    ping_interval=PING_INTERVAL_SEC,
                    ping_timeout=10,
                    close_timeout=5,
                )
                self._ws_refs.append(ws)
                logger.info("connected: %s", stream)
                delay = RECONNECT_BASE_SEC
                errs = 0

                async for raw in ws:
                    if not self._running:
                        break
                    now = time.time()
                    self.last_msg_time = now
                    self.last_msg_time_by_stream[stream] = now
                    try:
                        msg = json.loads(raw)
                        bar = _parse_kline_msg(coin, msg)
                        if bar is None:
                            continue
                        await self.on_bar(coin, bar)
                        errs = 0
                    except Exception:
                        errs += 1
                        if errs <= 3 or errs % 50 == 0:
                            logger.exception("callback error %s (consecutive=%d)",
                                             stream, errs)
                        if errs >= MAX_CONSECUTIVE_ERRORS:
                            logger.error("%s: %d consecutive errors, forcing reconnect",
                                         stream, errs)
                            break

            except (websockets.ConnectionClosed, ConnectionError, OSError) as e:
                if not self._running:
                    break
                logger.warning("%s disconnected: %s, reconnect in %.1fs",
                               stream, type(e).__name__, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, RECONNECT_CAP_SEC)
            except Exception:
                if not self._running:
                    break
                logger.exception("%s unexpected, reconnect in %.1fs", stream, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, RECONNECT_CAP_SEC)
            finally:
                if ws is not None:
                    if ws in self._ws_refs:
                        self._ws_refs.remove(ws)
                    try:
                        await ws.close()
                    except Exception:
                        pass


def _parse_kline_msg(coin: str, msg: dict) -> Bar | None:
    """Return a Bar iff this is a `k.x == true` (bar closed) kline event."""
    if msg.get("e") != "kline":
        return None
    k = msg.get("k") or {}
    if not k.get("x"):
        return None
    try:
        return Bar(
            coin=coin,
            open_time_ms=int(k["t"]),
            open=float(k["o"]),
            high=float(k["h"]),
            low=float(k["l"]),
            close=float(k["c"]),
            volume=float(k.get("v") or 0.0),
        )
    except (KeyError, TypeError, ValueError):
        logger.warning("malformed kline for %s: keys=%s", coin, list(k.keys()))
        return None
