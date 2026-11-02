"""Snapshot / restore of strategy + broker + running totals.

Design (per 2026-10-22-live-paper-design.md §6.4):
  - dumped every 60s during normal running
  - loaded only on explicit --resume-from (run.sh watch passes the tag's newest one)
  - snapshots with per-coin last_bar_ms load at any age: the runner replays the gap
    from REST, so a resume equals an uninterrupted run. Older snapshots without it
    can't be replayed and are only trusted if younger than max_age_sec.
  - append-only logs (events.jsonl / rounds.csv) are crash-safe independently
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from martiquant.paper_broker import LiveOrder, PaperBroker
from martiquant.strategy import CoinState, LadderStrategy

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1


def dump(path: Path, strategy: LadderStrategy, broker: PaperBroker,
         totals: dict[str, float], mark: dict[str, float],
         last_bar_ms: dict[str, int], round_tracker: dict[str, dict]) -> None:
    payload = {
        "schema": SCHEMA_VERSION,
        "ts_ms": int(time.time() * 1000),
        "strategy_states": {c: asdict(s) for c, s in strategy.states.items()},
        "broker": {
            "next_id": broker._next_id,
            "n_placed": broker.n_placed,
            "n_cancelled": broker.n_cancelled,
            "open_orders": {str(oid): asdict(o) for oid, o in broker.open_orders.items()},
        },
        "totals": totals,
        "mark": mark,
        "last_bar_ms": last_bar_ms,
        "round_tracker": round_tracker,
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str))
    tmp.replace(path)


def load(path: Path, strategy: LadderStrategy, broker: PaperBroker,
         max_age_sec: int = 3600) -> tuple[dict[str, float], dict[str, float], dict] | None:
    """Restore state into strategy + broker.

    Returns (totals, mark, extra) or None if stale. `extra` holds last_bar_ms and
    round_tracker; snapshots written before those keys existed restore them as empty.
    """
    if not path.exists():
        logger.info("state.json not found at %s", path)
        return None
    try:
        payload = json.loads(path.read_text())
    except Exception:
        logger.exception("state.json unreadable, treating as cold start")
        return None

    if payload.get("schema") != SCHEMA_VERSION:
        logger.warning("state.json schema mismatch (want %d got %s), cold start",
                       SCHEMA_VERSION, payload.get("schema"))
        return None

    age_sec = (time.time() * 1000 - payload["ts_ms"]) / 1000
    replayable = set(payload.get("last_bar_ms", {})) >= set(payload["strategy_states"])
    if age_sec > max_age_sec and not replayable:
        logger.warning("state.json too old (%.0fs > %ds) and has no last_bar_ms to replay "
                       "the gap, cold start", age_sec, max_age_sec)
        return None

    for coin, s_dict in payload["strategy_states"].items():
        if coin in strategy.states:
            strategy.states[coin] = CoinState(**s_dict)

    broker._next_id = payload["broker"]["next_id"]
    broker.n_placed = payload["broker"]["n_placed"]
    broker.n_cancelled = payload["broker"]["n_cancelled"]
    broker.open_orders = {
        int(oid): LiveOrder(**o_dict)
        for oid, o_dict in payload["broker"]["open_orders"].items()
    }

    totals = dict(payload["totals"])
    mark = dict(payload["mark"])
    extra = {
        "last_bar_ms": {c: int(t) for c, t in payload.get("last_bar_ms", {}).items()},
        "round_tracker": dict(payload.get("round_tracker", {})),
    }
    logger.info("state restored, age=%.0fs, open_orders=%d, open_coins=%d",
                age_sec, len(broker.open_orders),
                sum(1 for s in strategy.states.values() if s.in_position))
    return totals, mark, extra
