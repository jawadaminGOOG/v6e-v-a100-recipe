#!/usr/bin/env python3
"""Plots for g4-colo from results/analysis/*.csv and results/cells/*/telemetry.csv."""
from __future__ import annotations

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

ROOT = Path(__file__).resolve().parent
A = ROOT / "results" / "analysis"
P = ROOT / "results" / "plots"
CFG = json.loads((ROOT / "experiment.json").read_text())
LN = {int(k): v.split(" (")[0] for k, v in CFG["level_names"].items()}
NICE = {"ttft_p50": "TTFT p50", "ttft_p99": "TTFT p99", "itl_p50": "ITL p50", "itl_p99": "ITL p99",
        "e2e_p50": "E2E p50", "e2e_p99": "E2E p99", "output_tok_s": "Output tok/s", "achieved_rps": "RPS"}


COL = {"solo": "#1f1f1f", "colo 50%": "#e8a27f", "colo 95%": "#d4572a"}
CLASS = {"compute": "compute-type co-located work (14 integer SPEC benchmarks)",
         "bandwidth": "memory-bandwidth-heavy co-located work (749.fotonik3d_r, 782.lbm_r)"}
QN = {"ttft": "Time to first token (ms)", "itl_mean": "Inter-token latency (ms)", "e2e": "End-to-end latency (ms)"}


def latency_cdfs():
    """Cumulative distributions: full range (top) and the tail above p90 (bottom), one figure per workload class."""
    q = pd.read_csv(A / "latency_quantiles.csv")
    for phase in ["compute", "bandwidth"]:
        fig, axes = plt.subplots(2, 3, figsize=(16, 8.5), gridspec_kw={"height_ratios": [1, 1]})
        for j, met in enumerate(["ttft", "itl_mean", "e2e"]):
            for row, (lo, ttl) in enumerate([(0.0, "all requests"), (0.9, "tail (p90 to p99.9)")]):
                ax = axes[row, j]
                for L in sorted(q.level.unique()):
                    for scen in ["solo", f"colo {'50%' if L == 20 else '95%'}"]:
                        d = q[(q.phase == phase) & (q.level == L) & (q.scenario == scen) & (q.metric == met) & (q.q >= lo)]
                        if scen == "solo" and L != sorted(q.level.unique())[-1]:
                            continue  # one solo line (solo is cut at the 95% run offsets; the 50% cut is equivalent)
                        ax.plot(d.value_ms, d.q * 100, color=COL[scen], lw=2.2 if scen != "solo" else 2.6,
                                label=scen if (row == 0 and j == 0) else None)
                ax.set_xlabel(QN[met])
                ax.set_ylabel("% of requests at or below")
                ax.set_title(f"{QN[met].split(' (')[0]}: {ttl}", fontsize=11)
                ax.grid(alpha=.3)
                if lo:
                    ax.set_ylim(90, 100)
        h, l = axes[0, 0].get_legend_handles_labels()
        order = [l.index(x) for x in ["solo", "colo 50%", "colo 95%"] if x in l]
        fig.legend([h[i] for i in order], [l[i] for i in order], loc="upper right", ncol=3, frameon=False, fontsize=11)
        fig.suptitle(f"GPU workload latency distribution alongside {CLASS[phase]}", fontsize=13, x=0.01, ha="left")
        fig.tight_layout(rect=(0, 0, 1, 0.95))
        fig.savefig(P / f"latency_cdf_{phase}.png", dpi=140)
        plt.close(fig)


def delta_summary():
    """% change vs solo for every metric, across co-location level x workload class, with 95% CI (wider of bootstrap/Welch)."""
    imp = pd.read_csv(A / "vllm_phase_impact_split.csv")
    mets = ["ttft_p50", "ttft_p99", "itl_p50", "e2e_p50", "e2e_p99", "output_tok_s"]
    combos = [(L, p) for p in ["compute", "fotonik3d", "lbm"] for L in (20, 38)]
    names = {"compute": "integer", "fotonik3d": "fotonik3d", "lbm": "lbm"}
    lab = {(L, p): f"{'50%' if L == 20 else '95%'} · {names[p]}" for L, p in combos}
    cols = {(20, "compute"): "#c9c7c1", (38, "compute"): "#8f8d87", (20, "fotonik3d"): "#e8a27f",
            (38, "fotonik3d"): "#d4572a", (20, "lbm"): "#a9c4e8", (38, "lbm"): "#4a78b8"}
    fig, ax = plt.subplots(figsize=(15, 6))
    w = 0.135
    x = np.arange(len(mets))
    for k, cmb in enumerate(combos):
        d = imp[(imp.level == cmb[0]) & (imp.phase == cmb[1])].set_index("metric").reindex(mets)
        lo = np.minimum(d.boot_lo, d.welch_lo)
        hi = np.maximum(d.boot_hi, d.welch_hi)
        ax.bar(x + (k - 2.5) * w, d.delta_pct, w, color=cols[cmb], label=lab[cmb],
               yerr=[d.delta_pct - lo, hi - d.delta_pct], capsize=3, ecolor="#444")
        for xi, v, h in zip(x + (k - 2.5) * w, d.delta_pct, hi):
            ax.text(xi, (h + 1.5) if v >= 0 else -6.5, f"{v:+.0f}" if abs(v) >= 1 else f"{v:+.1f}", ha="center", fontsize=7.5)
    ax.axhline(0, color="k", lw=.8)
    ax.set_xticks(x)
    ax.set_xticklabels([NICE[m] for m in mets])
    ax.set_ylabel("change vs GPU workload alone (%)")
    ax.set_ylim(-8, 145)
    ax.legend(ncol=6, frameon=False, loc="upper center", bbox_to_anchor=(0.5, -0.08))
    ax.grid(axis="y", alpha=.3)
    ax.set_title("GPU workload change vs solo (%), by co-location level and the SPEC benchmarks running alongside (mean of 5 runs, 95% CI)")
    fig.tight_layout()
    fig.savefig(P / "vllm_delta_summary.png", dpi=140)
    plt.close(fig)


def spec_forest():
    s = pd.read_csv(A / "spec_impact.csv")
    if s.empty:
        return
    levels = sorted(s.level.unique())
    fig, axes = plt.subplots(1, len(levels), figsize=(3.2 * len(levels), 6), sharey=True)
    axes = np.atleast_1d(axes)
    benches = sorted(s.benchmark.unique())
    for ax, L in zip(axes, levels):
        d = s[s.level == L].set_index("benchmark").reindex(benches)
        y = np.arange(len(benches))
        ax.errorbar(d.delta_pct, y, xerr=[d.delta_pct - d.ci_lo, d.ci_hi - d.delta_pct], fmt="o", capsize=2)
        ax.axvline(0, color="k", lw=.8)
        ax.set_title(f"{LN[L]} ({L} copies)")
        ax.set_xlabel("runtime colo vs solo (%)")
        ax.grid(alpha=.3)
    axes[0].set_yticks(np.arange(len(benches)))
    axes[0].set_yticklabels(benches)
    fig.suptitle("SPEC CPU 2026 rate (train, est.) runtime with vLLM load vs alone: mean delta, 95% CI (higher = slower)")
    fig.tight_layout()
    fig.savefig(P / "spec_impact_forest.png", dpi=150)


def util_heatmap():
    cells = ROOT / "results" / "cells"
    for name in ["vllm_solo-r1", "colo-L20-r1", "colo-L38-r1"]:
        f = cells / name / "telemetry.csv"
        if not f.exists():
            continue
        t = pd.read_csv(f)
        busy = t[[c for c in t.columns if c.startswith("busy")]].to_numpy().T
        fig, ax = plt.subplots(figsize=(11, 4))
        im = ax.imshow(busy, aspect="auto", cmap="viridis", vmin=0, vmax=100, interpolation="nearest")
        ax.set_ylabel("logical CPU")
        ax.set_xlabel("time (s)")
        ax.set_title(f"Per-CPU busy % at 1 s, {name} (vLLM cpus 1-4,25-28; system 0,24)")
        fig.colorbar(im, ax=ax, label="busy %")
        fig.tight_layout()
        fig.savefig(P / f"cpu_heatmap_{name}.png", dpi=130)


def util_bars():
    c = pd.read_csv(A / "cells.csv")
    c["lvl"] = np.where(c.kind == "vllm_solo", 0, c.level)
    g = c[c.kind.isin(["vllm_solo", "colo"])].groupby("lvl")[["cpu_busy_pct", "mem_used_gib", "gpu_util"]].mean()
    fig, ax = plt.subplots(figsize=(7, 3.5))
    labels = ["solo" if L == 0 else LN[L] for L in g.index]
    ax.bar(labels, g.cpu_busy_pct)
    for i, v in enumerate(g.cpu_busy_pct):
        ax.text(i, v + 1, f"{v:.0f}%", ha="center")
    ax.set_ylabel("node CPU busy (% of 48 vCPU)")
    ax.set_ylim(0, 105)
    ax.set_title("Node CPU utilisation during the vLLM analysis window")
    fig.tight_layout()
    fig.savefig(P / "node_cpu_util_by_level.png", dpi=150)



def capacity_chart():
    """Max throughput (closed loop, 500 in flight) by SPEC benchmark running alongside."""
    f = ROOT / "results" / "sanity" / "capacity_summary.csv"
    if not f.exists():
        return
    c = pd.read_csv(f)
    phases = ["integer", "fotonik3d", "lbm"]
    fig, ax = plt.subplots(figsize=(10, 5))
    x = np.arange(len(phases))
    w = 0.26
    solo = c[c.level == 38].set_index("phase").reindex(phases).solo_rps
    ax.bar(x - w, solo, w, color="#1f1f1f", label="solo")
    for k, (L, col, nm) in enumerate([(20, "#e8a27f", "colo 50%"), (38, "#d4572a", "colo 95%")]):
        d = c[c.level == L].set_index("phase").reindex(phases)
        ax.bar(x + k * w, d.colo_rps, w, color=col, label=nm)
        for xi, v, dp in zip(x + k * w, d.colo_rps, d.delta_pct):
            ax.text(xi, v + 6, f"{dp:+.1f}%", ha="center", fontsize=9)
    ax.set_xticks(x)
    ax.set_xticklabels(["14 integer benchmarks", "749.fotonik3d_r", "782.lbm_r"])
    ax.set_ylabel("completed requests / s")
    ax.set_ylim(600, 860)
    ax.legend(frameon=False, ncol=3, loc="upper center", bbox_to_anchor=(0.5, -0.1))
    ax.grid(axis="y", alpha=.3)
    ax.set_title("GPU workload maximum throughput (500 requests in flight), by SPEC benchmark running alongside")
    fig.tight_layout()
    fig.savefig(P / "capacity_by_benchmark.png", dpi=140)
    plt.close(fig)


def golden_chart():
    """Closed-loop capacity: GPU workload limited to 8 vCPU vs no CPU limit (all 48 vCPU)."""
    f = ROOT / "results" / "sanity" / "golden_capacity.csv"
    if not f.exists():
        return
    g = pd.read_csv(f)
    g["cfg"] = g.label.str.startswith("g48").map({True: "all 48 vCPU", False: "8 vCPU / 64 GiB"})
    fig, ax = plt.subplots(figsize=(7.5, 4.2))
    for cfg, col in [("8 vCPU / 64 GiB", "#d4572a"), ("all 48 vCPU", "#1f1f1f")]:
        d = g[g.cfg == cfg].sort_values("concurrency")
        ax.plot(d.concurrency, d.rps, "o-", color=col, lw=2.2, label=cfg)
        for xc, v in zip(d.concurrency, d.rps):
            ax.text(xc, v + (25 if col == "#1f1f1f" else -55), f"{v:.0f}", ha="center", fontsize=9, color=col)
    ax.set_xlabel("requests in flight")
    ax.set_ylabel("completed requests / s")
    ax.set_xticks(sorted(g.concurrency.unique()))
    ax.set_ylim(0, 950)
    ax.legend(frameon=False, loc="lower right")
    ax.grid(alpha=.3)
    ax.set_title("GPU workload capacity, no co-location: 8-vCPU limit vs all 48 vCPU\n(one probe per point)", fontsize=11)
    fig.tight_layout()
    fig.savefig(P / "golden_capacity.png", dpi=140)
    plt.close(fig)

if __name__ == "__main__":
    P.mkdir(parents=True, exist_ok=True)
    for fn in (latency_cdfs, delta_summary, spec_forest, util_heatmap, util_bars, capacity_chart, golden_chart):
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            print(f"{fn.__name__}: {e}")
    print("plots in", P)
