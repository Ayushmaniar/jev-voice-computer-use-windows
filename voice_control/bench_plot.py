"""Plot goal mode on the replicated benchmark next to the published steve8708/jev-browser-benchmark systems.

    python -m voice_control.bench_plot --label baseline --label final

For each label, the latest result of every task under that label is used. Cost per success is total Jev
cost over all attempted tasks divided by successes, as in the published report. The headline counts every task,
including the ones marked infeasible (a password, or text Jev would have to write); a second point leaves those out. Published points come from
goal-fixtures/bench/steve8708-results.json (MIT, github.com/steve8708/jev-browser-benchmark).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .bench_eval import RESULTS, ROOT, SKIPPED

STEVE = ROOT / "goal-fixtures" / "bench" / "steve8708-results.json"
OUT = ROOT / "logs"


def summarize(label: str, lane: str | None = None, split: str | None = None, feasible_only: bool = False) -> dict[str, Any]:
    """One label: the latest attempt at each task. "a+b": the latest attempt under each of labels a and b, pooled."""
    names = label.split("+")
    latest: dict[tuple, dict[str, Any]] = {}
    for line in RESULTS.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        if record.get("label") not in names or record.get("outcome") in SKIPPED:
            continue
        if lane and record["lane"] != lane or split and record["split"] != split:
            continue
        if feasible_only and record.get("infeasible"):
            continue
        latest[(record["task"], record["label"])] = record
    runs = list(latest.values())
    pending = [r["task"] for r in runs if r.get("passed") is None]
    if pending:
        raise SystemExit(f"{label}: external checks still pending for {sorted(set(pending))}; record them with --verdict")
    wins = sum(r["passed"] for r in runs)
    cost = sum(r.get("cost_usd", 0) for r in runs)
    return {"label": label, "successes": wins, "total": len(runs), "tasks": len({r["task"] for r in runs}), "rate": wins / len(runs) if runs else 0,
            "avg_seconds": sum(r["seconds"] for r in runs) / len(runs) if runs else 0,
            "cost_per_success_usd": cost / wins if wins else None, "total_cost_usd": cost}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", action="append", required=True, help="result label(s) to plot; the last is highlighted")
    parser.add_argument("--name", default="Jev voice control (goal mode)")
    args = parser.parse_args()
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    steve = json.loads(STEVE.read_text(encoding="utf-8"))["overall"]["results"]
    ours = [summarize(label) for label in args.label]
    feasible = summarize(args.label[-1], feasible_only=True)
    lanes = {label: {lane: summarize(label, lane) for lane in ("simple", "long", "native")} for label in args.label}

    fig, ax = plt.subplots(figsize=(11, 6.2), facecolor="#0f1115")
    ax.set_facecolor("#0f1115")
    colors = {"Luna": "#56b870", "Hybrid": "#8e7cc3", "Jev Ultrafast": "#e0675a", "Jev Browser": "#e0675a"}
    names = {"Jev Ultrafast": "Jev fast", "Jev Browser": "Jev"}
    for r in steve:
        ax.scatter(r["cost_per_success_usd"], r["rate"] * 100, s=60, color=colors[r["system"]], zorder=3)
        ax.annotate(names.get(r["system"], r["system"]), (r["cost_per_success_usd"], r["rate"] * 100), textcoords="offset points",
                    xytext=(-10, 10), color="#bbbbbb", fontsize=9, fontfamily="monospace")
    for i, r in enumerate(ours):
        final = i == len(ours) - 1
        ax.scatter(r["cost_per_success_usd"], r["rate"] * 100, s=140 if final else 70, marker="*" if final else "o",
                   color="#f2c14e" if final else "#8a7a4a", zorder=4, edgecolors="white" if final else "none")
        text = f"{args.name}{'' if final else ' - ' + r['label']}\n{r['successes']}/{r['total']} · ${r['cost_per_success_usd']:.4f}/success"
        ax.annotate(text, (r["cost_per_success_usd"], r["rate"] * 100), textcoords="offset points", xytext=(12, -14 if final else -30),
                    color="#f2c14e" if final else "#9a8a5a", fontsize=9, fontfamily="monospace")
    if feasible["total"] != ours[-1]["total"]:  # the same run without the tasks Jev cannot do at all
        final = ours[-1]
        ax.scatter(feasible["cost_per_success_usd"], feasible["rate"] * 100, s=70, marker="*", facecolors="none",
                   edgecolors="#f2c14e", zorder=4)
        ax.annotate("", (feasible["cost_per_success_usd"], feasible["rate"] * 100),
                    (final["cost_per_success_usd"], final["rate"] * 100),
                    arrowprops=dict(arrowstyle="-", color="#8a7a4a", linestyle=":"))
        ax.annotate(f"same run, only tasks Jev can do\n{feasible['successes']}/{feasible['total']} · "
                    f"${feasible['cost_per_success_usd']:.4f}/success", (feasible["cost_per_success_usd"], feasible["rate"] * 100),
                    textcoords="offset points", xytext=(-12, 6), ha="right", color="#9a8a5a", fontsize=8, fontfamily="monospace")
    ax.set_xscale("log")
    ax.set_xlim(0.03, 0.0004)  # right is cheaper, as in the published chart
    ax.set_ylim(0, 105)
    ax.set_xticks([0.03, 0.01, 0.003, 0.001, 0.0005])
    ax.set_xticklabels(["$0.03", "$0.01", "$0.003", "$0.001", "$0.0005"])
    ax.set_yticks([0, 25, 50, 75, 100])
    ax.set_yticklabels(["0%", "25%", "50%", "75%", "100%"])
    ax.grid(color="#2a2d33", linewidth=0.8)
    for spine in ax.spines.values():
        spine.set_color("#2a2d33")
    ax.tick_params(colors="#999999", labelsize=9)
    ax.set_xlabel("Cost per success · log scale · right is cheaper", color="#999999", fontfamily="monospace", fontsize=9)
    ax.set_ylabel("Success rate", color="#999999", fontfamily="monospace", fontsize=9)
    final = lanes[args.label[-1]]
    lane_text = "  ·  ".join(f"{lane} {v['successes']}/{v['total']}" for lane, v in final.items() if v["total"])
    base, held = summarize(args.label[0], split="holdout"), summarize(args.label[-1], split="holdout")
    if len(args.label) > 1 and held["total"]:  # the untuned half: the honest measure of how far the fixes carry
        lane_text += f"  ·  held-out tasks {base['rate']:.0%} → {held['rate']:.0%}"
    ax.set_title(f"{args.name} on a replication of the Jev computer-use benchmark\n{lane_text}", color="#dddddd",
                 fontsize=11, fontfamily="monospace", loc="left")
    fig.text(0.01, 0.01, f"Published points: steve8708/jev-browser-benchmark (overall, 51-57 tasks, macOS + browser). Ours: {ours[-1]['tasks']} tasks "
             f"rebuilt on Windows from the published lane descriptions\n(fixtures were not published), {ours[-1]['total']} attempts, including "
             "tasks Jev cannot do (a password, or text it must write). Real Spotify, Gmail, Drive, Google Flights,\nOpenTable and TodoMVC; "
             "local mocks for Notion, Figma, calendar and tracker. Baseline: the earlier 57-task set. Not the identical task set; compare with care.",
             color="#777777", fontsize=7.5, fontfamily="monospace")
    fig.tight_layout(rect=(0, 0.07, 1, 1))
    path = OUT / "bench-plot.png"
    fig.savefig(path, dpi=160, facecolor=fig.get_facecolor())
    (OUT / "bench-summary.json").write_text(json.dumps({"ours": ours, "feasible_only": feasible, "lanes": lanes}, indent=1), encoding="utf-8")
    print(path)
    for r in ours:
        print(json.dumps(r))


if __name__ == "__main__":
    main()
