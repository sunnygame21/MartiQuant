"""Persist scoring artifacts and a human-readable summary."""
from __future__ import annotations

import csv
from pathlib import Path

from .candidates import CURRENT_BASKET
from .score import METRIC_WEIGHTS, ScoredCoin


def write_scores_csv(path: Path, scored: list[ScoredCoin]) -> None:
    windows = sorted({d for s in scored for d in s.per_window}, reverse=True)
    fieldnames = ["rank", "coin", "total"]
    for d in windows:
        fieldnames.append(f"w{d}d_total")
    for d in windows:
        for name in METRIC_WEIGHTS:
            fieldnames.append(f"w{d}d_{name}")
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames)
        w.writeheader()
        for rank, s in enumerate(scored, 1):
            row = {"rank": rank, "coin": s.coin, "total": round(s.total, 2)}
            for d in windows:
                ws = s.per_window[d]
                row[f"w{d}d_total"] = round(ws.total, 2)
                for name in METRIC_WEIGHTS:
                    row[f"w{d}d_{name}"] = round(ws.components[name], 2)
            w.writerow(row)


def write_raw_metrics_csv(path: Path, scored: list[ScoredCoin]) -> None:
    windows = sorted({d for s in scored for d in s.per_window}, reverse=True)
    fieldnames = ["coin", "window_days"] + list(METRIC_WEIGHTS.keys())
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames)
        w.writeheader()
        for s in scored:
            for d in windows:
                ws = s.per_window[d]
                row = {"coin": s.coin, "window_days": d}
                for name in METRIC_WEIGHTS:
                    row[name] = round(ws.raw[name], 4)
                w.writerow(row)


def write_summary(
    path: Path,
    scored: list[ScoredCoin],
    rejected_young: dict[str, int],
    n_top: int = 10,
) -> None:
    windows = sorted({d for s in scored for d in s.per_window}, reverse=True)
    top = [s.coin for s in scored[:n_top]]
    added = [c for c in top if c not in CURRENT_BASKET]
    dropped = [c for c in CURRENT_BASKET if c not in top]

    lines = []
    lines.append("# Basket Scoring Report")
    lines.append("")
    lines.append(f"**Windows**: {windows} days (1h klines, equal percentile within each)")
    lines.append(f"**Candidates passed hard gate**: {len(scored)}  "
                 f"**Rejected (young listing)**: {len(rejected_young)}  "
                 f"**Basket size**: {n_top}")
    lines.append("")
    if rejected_young:
        lines.append("## Rejected by onboard-age hard gate")
        lines.append("")
        lines.append("| coin | listed days | reason |")
        lines.append("|---|---|---|")
        for coin, days in sorted(rejected_young.items(), key=lambda kv: kv[1]):
            lines.append(f"| {coin} | {days} | Binance USDT-M listing too young |")
        lines.append("")
    lines.append("## Recommended basket")
    lines.append("")
    lines.append(" / ".join(top))
    lines.append("")
    lines.append("## vs current basket")
    lines.append("")
    lines.append(f"- **Added** ({len(added)}): {', '.join(added) if added else '(none)'}")
    lines.append(f"- **Dropped** ({len(dropped)}): {', '.join(dropped) if dropped else '(none)'}")
    lines.append("")
    lines.append("## Full ranking (combined)")
    lines.append("")
    header = ["rank", "coin", "total"] + [f"{d}d" for d in windows]
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "|".join(["---"] * len(header)) + "|")
    for rank, s in enumerate(scored, 1):
        row = [str(rank), s.coin, f"{s.total:.1f}"]
        for d in windows:
            row.append(f"{s.per_window[d].total:.1f}")
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")
    for d in windows:
        lines.append(f"## Per-window ({d}d) component ranks")
        lines.append("")
        comp_header = ["coin"] + [n[:8] for n in METRIC_WEIGHTS]
        lines.append("| " + " | ".join(comp_header) + " |")
        lines.append("|" + "|".join(["---"] * len(comp_header)) + "|")
        for s in scored:
            ws = s.per_window[d]
            r = [s.coin] + [f"{ws.components[n]:.0f}" for n in METRIC_WEIGHTS]
            lines.append("| " + " | ".join(r) + " |")
        lines.append("")

    lines.append("## Weights")
    lines.append("")
    for name, (w, higher) in METRIC_WEIGHTS.items():
        direction = "higher better" if higher else "lower better"
        lines.append(f"- `{name}`: {w*100:.0f}% ({direction})")
    lines.append("- Window weights: 365d 50%, 180d 30%, 90d 20%")

    path.write_text("\n".join(lines) + "\n")
