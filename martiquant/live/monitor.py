"""Health monitor for live paper instances (cron, every 2 minutes).

Runs outside the paper processes so it can report their death. For every tag that
run.sh remembers (runs/_live_logs/<tag>.cmd) it checks:

  crash_loop  run.sh watch relaunched it >= CRASH_LOOP_RESTARTS times in the last hour
  stalled     its newest state.json hasn't been written for > STALE_SEC
              (process dead and not coming back, or hung)
  no_bars     no coin has a processed bar newer than STALE_SEC
              (feed down and the REST backfill isn't closing the gap)
  delisted    a basket coin's perpetual has a settlement scheduled or past, or no
              longer exists (exchangeInfo, at most once per DELIST_CHECK_SEC). The
              feed keeps streaming flat zero-volume bars for a settled contract, so
              no_bars never fires for it and the paper sits on a dead position.
  backfill_failed / resume_skipped events appended since the last check

A check alerts once when it goes bad and once when it recovers; events alert each
time they appear. Alerts go to a Feishu bot when MARTIQUANT_FEISHU_WEBHOOK (and
optionally MARTIQUANT_FEISHU_SECRET for signed bots) is set in the environment or in
the repo's .env; otherwise they are only written to runs/_live_logs/monitor.log.

Usage:
  python3 -m martiquant.live.monitor
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from martiquant.market_data import LIVE_PERP_DELIVERY_MS, fetch_symbol_info

ROOT = Path(__file__).resolve().parent.parent.parent
LIVE_LOGS = ROOT / "runs" / "_live_logs"
STATE_FILE = LIVE_LOGS / "monitor_state.json"
LOG_FILE = LIVE_LOGS / "monitor.log"

STALE_SEC = 300
CRASH_LOOP_RESTARTS = 3
DELIST_CHECK_SEC = 3600  # delistings are announced days ahead; hourly is plenty
WATCH_LINE = re.compile(r"^\[watch (\S+)\] \[(\S+)\] dead, restarting")
ALERT_EVENTS = ("backfill_failed", "resume_skipped")


def _env(name: str) -> str | None:
    if os.environ.get(name):
        return os.environ[name]
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            k, _, v = line.partition("=")
            if k.strip() == name and v.strip():
                return v.strip().strip("'\"")
    return None


def _latest_run_dir(runs_dir: Path, tag: str) -> Path | None:
    dirs = sorted(runs_dir.glob(f"live_????????T??????Z_{tag}"))
    return dirs[-1] if dirs else None


def delist_problem(coins: list[str], info: dict[str, dict], now: float) -> str | None:
    """Describe basket coins whose perpetual is settling/settled or gone, else None."""
    problems = []
    for coin in coins:
        s = info.get(coin)
        if s is None:
            problems.append(f"{coin} has no USDT-M perpetual")
        elif s["delivery_ms"] < LIVE_PERP_DELIVERY_MS:
            day = datetime.fromtimestamp(s["delivery_ms"] / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            verb = "settled" if s["delivery_ms"] <= now * 1000 else "settles"
            problems.append(f"{coin} {verb} {day} ({s['status']})")
    return "; ".join(problems) or None


def _basket(run_dir: Path | None) -> list[str]:
    try:
        return list(json.loads((run_dir / "config.json").read_text())["basket"])
    except (TypeError, OSError, KeyError, json.JSONDecodeError):
        return []


def _recent_restarts(log: Path, tag: str, now: float) -> int:
    if not log.exists():
        return 0
    n = 0
    with log.open(errors="replace") as fh:
        for line in fh:
            m = WATCH_LINE.match(line)
            if m and m.group(2) == tag:
                t = datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                if now - t.timestamp() <= 3600:
                    n += 1
    return n


def check_tag(runs_dir: Path, tag: str, now: float, offsets: dict[str, int]
              ) -> tuple[dict[str, str | None], list[str]]:
    """Return ({check: problem text or None}, [new event alerts])."""
    live_logs = runs_dir / "_live_logs"
    checks: dict[str, str | None] = {}

    n = _recent_restarts(live_logs / f"{tag}.log", tag, now)
    checks["crash_loop"] = f"restarted {n}x in the last hour" if n >= CRASH_LOOP_RESTARTS else None

    run_dir = _latest_run_dir(runs_dir, tag)
    state = run_dir / "state.json" if run_dir else None
    if state is None or not state.exists():
        checks["stalled"] = "no state.json yet"
        checks["no_bars"] = None
        return checks, []

    age = now - state.stat().st_mtime
    checks["stalled"] = f"state.json not written for {age / 60:.0f} min" if age > STALE_SEC else None
    try:
        last_bar = json.loads(state.read_text()).get("last_bar_ms") or {}
    except (OSError, json.JSONDecodeError):
        last_bar = {}  # mid-replace read; the next run sees it
    bar_age = now - max(last_bar.values()) / 1000 - 60 if last_bar else 0.0
    checks["no_bars"] = (f"newest processed bar is {bar_age / 60:.0f} min old"
                         if bar_age > STALE_SEC else None)

    events: list[str] = []
    ev_path = run_dir / "events.jsonl"
    key = str(ev_path)
    if ev_path.exists():
        start = offsets.get(key, 0)
        with ev_path.open("rb") as fh:
            fh.seek(start)
            chunk = fh.read()
        complete = chunk[: chunk.rfind(b"\n") + 1]
        for line in complete.splitlines():
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("kind") in ALERT_EVENTS:
                detail = e.get("error") or e.get("reason") or ""
                events.append(f"{e['kind']} {detail}".strip())
        offsets[key] = start + len(complete)
    return checks, events


def send(text: str) -> str:
    url = _env("MARTIQUANT_FEISHU_WEBHOOK")
    if not url:
        return "no webhook"
    payload: dict = {"msg_type": "text", "content": {"text": text}}
    secret = _env("MARTIQUANT_FEISHU_SECRET")
    if secret:
        ts = str(int(time.time()))
        sign = hmac.new(f"{ts}\n{secret}".encode(), b"", hashlib.sha256).digest()
        payload.update(timestamp=ts, sign=base64.b64encode(sign).decode())
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            body = json.loads(r.read() or b"{}")
    except Exception as e:  # never let a failed push kill the check loop
        return f"push failed: {e!r}"
    code = body.get("code", body.get("StatusCode", 0))
    return "sent" if code == 0 else f"push rejected: {body}"


def run(runs_dir: Path = ROOT / "runs", now: float | None = None) -> list[str]:
    now = time.time() if now is None else now
    live_logs = runs_dir / "_live_logs"
    state_file = live_logs / STATE_FILE.name
    prev = json.loads(state_file.read_text()) if state_file.exists() else {}
    bad: dict[str, dict[str, str]] = prev.get("bad", {})
    offsets: dict[str, int] = prev.get("offsets", {})
    if not state_file.exists():  # first run: history before the monitor existed isn't news
        for cmd in live_logs.glob("*.cmd"):
            run_dir = _latest_run_dir(runs_dir, cmd.stem)
            if run_dir and (run_dir / "events.jsonl").exists():
                offsets[str(run_dir / "events.jsonl")] = (run_dir / "events.jsonl").stat().st_size

    info = None
    delist_checked_at = prev.get("delist_checked_at", 0)
    if now - delist_checked_at >= DELIST_CHECK_SEC:
        try:
            info = fetch_symbol_info()
            delist_checked_at = now
        except Exception:
            info = None  # retry next run; keep the last verdict meanwhile

    messages: list[str] = []
    for cmd in sorted(live_logs.glob("*.cmd")):
        tag = cmd.stem
        checks, events = check_tag(runs_dir, tag, now, offsets)
        was = bad.get(tag, {})
        checks["delisted"] = (delist_problem(_basket(_latest_run_dir(runs_dir, tag)), info, now)
                              if info is not None else was.get("delisted"))
        now_bad = {k: v for k, v in checks.items() if v}
        for k, v in now_bad.items():
            if k not in was:
                messages.append(f"❌ MartiQuant [{tag}] {k}: {v}")
        for k in was:
            if k not in now_bad:
                messages.append(f"✅ MartiQuant [{tag}] {k} recovered")
        messages += [f"⚠️ MartiQuant [{tag}] {e}" for e in events]
        bad[tag] = now_bad
    # tags that were stopped on purpose (run.sh stop removes .cmd) no longer count
    bad = {t: v for t, v in bad.items() if (live_logs / f"{t}.cmd").exists()}

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with (live_logs / LOG_FILE.name).open("a") as log:
        for msg in messages:
            log.write(f"[{stamp}] {msg} ({send(msg)})\n")
    tmp = state_file.with_suffix(".tmp")
    tmp.write_text(json.dumps({"bad": bad, "offsets": offsets,
                               "delist_checked_at": delist_checked_at}, indent=2))
    tmp.replace(state_file)
    return messages


if __name__ == "__main__":
    for m in run():
        print(m)
