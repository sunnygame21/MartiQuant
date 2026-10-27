#!/usr/bin/env python3
"""Explain a frozen selection by changing one named input; never reselect winner."""
from __future__ import annotations
import json
import sys
from dataclasses import asdict, replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from martiquant.research_engine import SearchConfig, load_cached_data, run_backtest


def main():
    root = Path(__file__).resolve().parents[1]
    out = root / "runs/ladder_search_20261020"
    original = json.loads((out / "results.json").read_text())
    winner = next(r for r in original["comparisons"] if r["name"] == original["winner_name"])
    c = SearchConfig(**winner["config"])
    data = load_cached_data()
    a, b = len(data.times)//2, len(data.times)*2//3
    alternatives = [
        ("same_ladder_qty_2", replace(c, multiplier=2.)),
        ("same_ladder_qty_2.93", replace(c, multiplier=2.93)),
        ("same_ladder_cost_2", replace(c, mode="cost", multiplier=2.)),
        ("same_ladder_tp_0.361", replace(c, tp_pct=.361)),
    ]
    rows = []
    for name, cfg in alternatives:
        r = {"name": name, "config": asdict(cfg)}
        for key, start, end in (("train",0,a),("validation",a,b),("test",b,len(data.times)),
                                ("full",0,len(data.times))):
            r[key] = run_backtest(data,cfg,start=start,end=end)
        rows.append(r)
        print(name, "test", r["test"]["net_pct"], "DD", r["test"]["max_dd_pct"])
    payload = {"purpose": "Post-selection explanatory ablations; fixed changes named by the user discussion. These results never reselect the frozen winner.",
               "frozen_winner": original["winner_name"], "ablations": rows}
    (out / "ablation_results.json").write_text(json.dumps(payload,indent=2))


if __name__ == "__main__":
    main()
