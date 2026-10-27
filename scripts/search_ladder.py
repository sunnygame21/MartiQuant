#!/usr/bin/env python3
"""Bounded, reproducible ladder search; holdout never participates in selection.

Run with a Python environment containing numpy and numba. No network or live trading.
The research engine is deliberately separate from the existing paper backtest.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from martiquant.research_engine import SearchConfig, load_cached_data, run_backtest


LADDERS = {
    "source": (-5.07, -9.13, -14.53, -25.30),
    "rounded": (-5.0, -10.0, -15.0, -25.0),
    "tight": (-3.0, -6.0, -10.0, -18.0),
    "smooth": (-4.0, -8.0, -14.0, -24.0),
    "wide": (-7.0, -13.0, -20.0, -30.0),
}
MULTIPLIERS = (1.0, 1.5, 2.0, 2.5, 2.93)
TAKE_PROFITS = (0.2, 0.361, 0.5, 0.674, 1.0)


def dump_json(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, default=str))


def net(result):
    return float(result["net_pct"])


def drawdown(result):
    return abs(float(result["max_dd_pct"]))


def score(result, days):
    # Daily return per drawdown, with a 1% floor to avoid tiny-DD denominator wins.
    if result.get("stopped", False) or drawdown(result) > 10 or net(result) <= 0:
        return -1e9
    return net(result) / days / max(drawdown(result), 1.0)


def configs():
    out = []
    for ladder, drops in LADDERS.items():
        for tp in TAKE_PROFITS:
            out.append((f"fixed_{ladder}_tp{tp}", SearchConfig(
                mode="fixed", drops_pct=drops, tp_pct=tp)))
            for mode in ("qty", "cost"):
                for multiplier in MULTIPLIERS:
                    # Compare regular recursion with the source-like last-leg reduction.
                    last_options = (None, 1.77) if multiplier > 1.77 else (None,)
                    for last in last_options:
                        name = f"{mode}_m{multiplier}_{ladder}_tp{tp}_last{last}"
                        out.append((name, SearchConfig(mode=mode, multiplier=multiplier,
                            drops_pct=drops, tp_pct=tp, last_multiplier=last)))
    return out


def csv_results(path, entries):
    rows = []
    for entry in entries:
        row = {"name": entry["name"], **entry["config"]}
        for split in ("train", "validation", "test", "full"):
            for key, value in entry.get(split, {}).items():
                if isinstance(value, (int, float, bool, str)):
                    row[f"{split}_{key}"] = value
        row["selection_score"] = entry.get("selection_score", "")
        rows.append(row)
    keys = list(dict.fromkeys(k for row in rows for k in row))
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=keys)
        writer.writeheader()
        writer.writerows(rows)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="runs/ladder_search_20261020")
    ap.add_argument("--shortlist", type=int, default=24)
    args = ap.parse_args()
    root = Path(__file__).resolve().parents[1]
    out = root / args.out
    out.mkdir(parents=True, exist_ok=True)
    data = load_cached_data()
    n = len(data.times)
    cut1, cut2 = n // 2, n * 2 // 3
    train_days = (data.times[cut1-1] - data.times[0] + 60_000) / 86_400_000
    val_days = (data.times[cut2-1] - data.times[cut1] + 60_000) / 86_400_000
    protocol = {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "search_space": {"ladders": LADDERS, "multipliers": MULTIPLIERS,
            "take_profits_pct": TAKE_PROFITS, "last_multiplier_options": [None, 1.77]},
        "split_indices": [0, cut1, cut2, n], "train_days": train_days,
        "validation_days": val_days,
        "selection": f"Train top{args.shortlist} plus family leaders -> maximize minimum of train/validation daily MTM return divided by max(maxDD,1pct). Require positive returns in both splits; no hard-stop or >10pct DD runs eligible. Holdout never selects.",
        "budget": "10000 USDT equity, 1000 maximum planned remaining entry-cost per coin; all full ladders normalized to this same budget; no borrowing",
        "limitations": ["90 days and one market regime", "historical funding not included",
            "no order book queue or partial fills", "independent splits start flat",
            "synthetic stress is scenario analysis, not empirical future probabilities"],
        "data": data.metadata,
    }
    dump_json(out / "protocol.json", protocol)
    candidates = configs()
    print(f"Search candidates: {len(candidates)}; split {cut1}/{cut2-cut1}/{n-cut2} bars", flush=True)
    entries = []
    t0 = time.monotonic()
    for i, (name, config) in enumerate(candidates):
        metrics = run_backtest(data, config, start=0, end=cut1)
        entries.append({"name": name, "config": asdict(config), "train": metrics,
            "train_score": score(metrics, train_days)})
        if (i+1) % 25 == 0 or i == 0:
            print(f"Train {i+1}/{len(candidates)}; {time.monotonic()-t0:.1f}s", flush=True)
    ranked = sorted(entries, key=lambda r:r["train_score"], reverse=True)
    selected = {r["name"]:r for r in ranked[:args.shortlist]}
    # Ensure each sizing family/multiplier is represented in validation.
    for mode in ("fixed", "qty", "cost"):
        for m in ((None,) if mode == "fixed" else MULTIPLIERS):
            family = [r for r in ranked if r["config"]["mode"] == mode
                and (m is None or r["config"]["multiplier"] == m)]
            if family:
                selected[family[0]["name"]] = family[0]
    baseline_name = "fixed_source_tp0.361"
    baseline = next(r for r in entries if r["name"] == baseline_name)
    selected[baseline_name] = baseline
    csv_results(out / "train_search.csv", entries)
    for entry in selected.values():
        c = SearchConfig(**entry["config"])
        entry["validation"] = run_backtest(data, c, start=cut1, end=cut2)
        entry["selection_score"] = min(entry["train_score"], score(entry["validation"], val_days))
    finalists = sorted(selected.values(), key=lambda r:r["selection_score"], reverse=True)
    if finalists[0]["selection_score"] <= -1e8:
        csv_results(out / "validation_search.csv", finalists)
        dump_json(out / "results.json", {"protocol": protocol,
            "winner_name": None, "reason": "No candidate meets train and validation criteria",
            "candidate_count": len(entries), "validation_count": len(selected)})
        print("No eligible profitable candidate; no winner selected.", flush=True)
        return
    # Freeze winner before executing any holdout.
    winner = finalists[0]
    dump_json(out / "frozen_selection.json", {
        "winner": winner, "baseline": baseline_name,
        "ranked_shortlist": finalists, "holdout_evaluated": False})
    print(f"Frozen winner: {winner['name']}", flush=True)
    # Freeze comparison set too: winner, baseline, and best member of each family.
    comparisons = {winner["name"]:winner, baseline_name:baseline}
    for mode in ("qty", "cost", "fixed"):
        for entry in finalists:
            if entry["config"]["mode"] == mode:
                comparisons[entry["name"]] = entry
                break
    for entry in comparisons.values():
        c = SearchConfig(**entry["config"])
        entry["test"] = run_backtest(data, c, start=cut2, end=n)
        entry["full"] = run_backtest(data, c)
        entry["fee_stress_test"] = run_backtest(data,
            replace(c, maker_fee=0.0006, taker_fee=0.0008), start=cut2, end=n)
        entry["low_proxy_test"] = run_backtest(data,
            replace(c, low_proxy=True), start=cut2, end=n)
        entry["breaker_test"] = run_backtest(data,
            replace(c, risk_breakers=True), start=cut2, end=n)
        print(f"Final {entry['name']}: test net={net(entry['test']):.4f}% DD={drawdown(entry['test']):.4f}%", flush=True)
    try:
        from scripts.research_diagnostics import run_stress
        for entry in comparisons.values():
            entry["stress"] = run_stress(SearchConfig(**entry["config"]))
    except ImportError as exc:
        print(f"Stress module unavailable: {exc}", flush=True)
    report = {"protocol": protocol, "winner_name": winner["name"],
        "candidate_count": len(entries), "validation_count": len(selected),
        "comparisons": list(comparisons.values()), "validation_ranking": finalists,
        "elapsed_seconds": time.monotonic()-t0}
    dump_json(out / "results.json", report)
    csv_results(out / "comparison.csv", list(comparisons.values()))
    csv_results(out / "validation_search.csv", finalists)
    print(f"Saved results: {out}", flush=True)


if __name__ == "__main__":
    main()
