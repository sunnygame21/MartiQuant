#!/usr/bin/env python3
"""Plot frozen holdout comparisons; plotting never changes the selected winner."""
from __future__ import annotations
import csv
import json
import sys
from pathlib import Path
from datetime import datetime, timezone
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from martiquant.research_engine import SearchConfig, load_cached_data, run_backtest


def main():
    out = Path(__file__).resolve().parents[1] / "runs/ladder_search_20261020"
    report = json.loads((out / "results.json").read_text())
    data = load_cached_data()
    start = len(data.times)*2//3
    labels = {
        report["winner_name"]: "Selected: quantity x1.5, TP 1%",
        "fixed_source_tp0.361": "Source parameters, matched budget",
        "cost_m1.0_rounded_tp1.0_lastNone": "Cost x1, rounded ladder, TP 1%",
        "fixed_tight_tp1.0": "Fixed amounts, tight ladder, TP 1%",
    }
    colors = ["#147d92", "#697686", "#d68a16", "#9664b1"]
    fig, axes = plt.subplots(1,2,figsize=(12,4.7),gridspec_kw={"width_ratios":[1.45,1]})
    checkpoints = sorted(set(range(start+2,len(data.times)+1,360)) | {len(data.times)})
    rows=[]
    names=[];returns=[];dds=[]
    for index, entry in enumerate(report["comparisons"]):
        cfg = SearchConfig(**entry["config"])
        values=[];dates=[]
        for end in checkpoints:
            metrics=run_backtest(data,cfg,start=start,end=end)
            ts=int(data.times[end-1]);value=float(metrics["ending_equity"])
            dates.append(datetime.fromtimestamp(ts/1000,timezone.utc))
            values.append(value)
            rows.append({"name":entry["name"],"timestamp_ms":ts,"equity":value})
        label=labels.get(entry["name"],entry["name"])
        axes[0].plot(dates,values,color=colors[index],label=label,linewidth=2 if index==0 else 1.4)
        names.append(label.replace(", ","\n"))
        returns.append(entry["test"]["net_pct"])
        dds.append(entry["test"]["max_dd_pct"])
    axes[0].set_title("Frozen 30-day holdout: fresh 10,000 USDT account",loc="left",weight="bold",fontsize=11)
    axes[0].set_ylabel("Equity, including open positions and estimated exit fee")
    axes[0].legend(loc="upper left",fontsize=8,frameon=False)
    axes[0].grid(alpha=.15)
    import matplotlib.dates as mdates
    axes[0].xaxis.set_major_locator(mdates.AutoDateLocator(minticks=4,maxticks=6))
    axes[0].xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
    x=np.arange(len(names))
    axes[1].bar(x-.18,returns,.34,label="Net return %",color="#147d92")
    axes[1].bar(x+.18,dds,.34,label="Maximum drawdown %",color="#da917a")
    axes[1].set_xticks(x,["Quantity x1.5\nTP 1%", "Source\nparameters", "Cost x1\nTP 1%", "Fixed amounts\ntight ladder"],fontsize=8)
    axes[1].set_title("Higher return also uses more risk",loc="left",weight="bold",fontsize=11)
    axes[1].legend(fontsize=8,frameon=False)
    axes[1].set_ylabel("Percent")
    axes[1].grid(axis="y",alpha=.15)
    for i,(r,d) in enumerate(zip(returns,dds)):
        axes[1].text(i-.18,r+.06,f"{r:.2f}",ha="center",fontsize=8)
        axes[1].text(i+.18,d+.06,f"{d:.2f}",ha="center",fontsize=8)
    axes[1].set_ylim(0,max(dds+returns)*1.25)
    fig.suptitle("MartiQuant | 425 candidate search",x=.07,ha="left",fontsize=17,weight="bold")
    fig.text(.07,.015,"45d training / 15d validation / 30d holdout. Equal 1,000 USDT per-coin ladder budget. Funding excluded. Research simulation.",fontsize=8,color="#58636f")
    fig.tight_layout(rect=(0,.045,1,.94))
    fig.savefig(out / "holdout_comparison.png",dpi=180,facecolor="white")
    with (out / "holdout_equity_6h.csv").open("w",newline="") as fh:
        w=csv.DictWriter(fh,fieldnames=["name","timestamp_ms","equity"])
        w.writeheader();w.writerows(rows)
    print(out / "holdout_comparison.png")


if __name__ == "__main__":
    main()
