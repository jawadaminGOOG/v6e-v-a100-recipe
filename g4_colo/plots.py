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
LN = {int(k): v for k, v in CFG["level_names"].items()}
NICE = {"ttft_p50": "TTFT p50", "ttft_p99": "TTFT p99", "itl_p50": "ITL p50", "itl_p99": "ITL p99",
        "e2e_p50": "E2E p50", "e2e_p99": "E2E p99", "output_tok_s": "Output tok/s", "achieved_rps": "RPS"}


def vllm_forest():
    imp = pd.read_csv(A / "vllm_impact.csv")
    mets = ["ttft_p50", "ttft_p99", "itl_p50", "e2e_p50", "e2e_p99", "output_tok_s"]
    fig, axes = plt.subplots(1, len(mets), figsize=(3.1 * len(mets), 3.6), sharey=True)
    levels = sorted(imp.level.unique())
    for ax, m in zip(axes, mets):
        s = imp[imp.metric == m].set_index("level").reindex(levels)
        y = np.arange(len(levels))
        ax.errorbar(s.delta_pct, y, xerr=[s.delta_pct - s.ci_lo, s.ci_hi - s.delta_pct], fmt="o", capsize=3)
        ax.axvline(0, color="k", lw=0.8)
        ax.set_title(NICE[m])
        ax.set_xlabel("colo vs solo (%)")
        ax.grid(alpha=.3)
    axes[0].set_yticks(np.arange(len(levels)))
    axes[0].set_yticklabels([f"{LN[L]}\n({L} vCPU)" for L in levels])
    fig.suptitle("vLLM (8 vCPU / 64 GiB slice) under SPEC co-location: mean delta and 95% bootstrap CI, n=5")
    fig.tight_layout()
    fig.savefig(P / "vllm_impact_forest.png", dpi=150)


def vllm_abs():
    c = pd.read_csv(A / "cells.csv")
    v = c[c.kind.isin(["vllm_solo", "colo"])].copy()
    v["lvl"] = np.where(v.kind == "vllm_solo", 0, v.level)
    order = [0] + sorted(CFG["levels"])
    labels = ["solo"] + [LN[L] for L in sorted(CFG["levels"])]
    mets = [("ttft_p50", 1e3, "ms"), ("itl_p50", 1e3, "ms"), ("e2e_p50", 1e3, "ms"), ("e2e_p99", 1e3, "ms"), ("output_tok_s", 1, "tok/s")]
    fig, axes = plt.subplots(1, len(mets), figsize=(3.3 * len(mets), 3.4))
    for ax, (m, sc, u) in zip(axes, mets):
        for i, L in enumerate(order):
            vals = v[v.lvl == L][m] * sc
            ax.scatter(np.full(len(vals), i) + np.random.uniform(-.08, .08, len(vals)), vals, s=14)
            ax.hlines(vals.mean(), i - .25, i + .25, color="k")
        ax.set_xticks(range(len(order)))
        ax.set_xticklabels(labels, rotation=45)
        ax.set_title(f"{NICE[m]} ({u})")
        ax.grid(alpha=.3)
    fig.suptitle("Per-rep values (dots) and mean (bar) by colo level")
    fig.tight_layout()
    fig.savefig(P / "vllm_abs_by_level.png", dpi=150)


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


if __name__ == "__main__":
    P.mkdir(parents=True, exist_ok=True)
    for fn in (vllm_forest, vllm_abs, spec_forest, util_heatmap, util_bars):
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            print(f"{fn.__name__}: {e}")
    print("plots in", P)
