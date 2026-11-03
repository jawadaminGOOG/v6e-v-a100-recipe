#!/usr/bin/env python3
"""README charts for the controller-vs-native comparison: one message per chart, title = the takeaway,
direct labels instead of legends, one colour per recipe everywhere.

    g4_colo/.venv/bin/python charts.py <sweep dir, e.g. .agents/bench/sweep1_merged> <out dir>
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402

D, OUT = Path(sys.argv[1]), Path(sys.argv[2])
OUT.mkdir(parents=True, exist_ok=True)

C = {"controller": "#2F4B8F", "native": "#11957A"}          # recipe colours, used everywhere
NAME = {"controller": "Controller recipe", "native": "Native recipe"}
G4_FILL = {"controller": "#B8C2D6", "native": "#B8C2D6"}   # CPU pods on G4 spare cores (free)
N4D = "#F0A04B"                                            # CPU pods on N4D (paid)
INK, MUTED, GRID = "#1D2433", "#6B7280", "#E5E7EB"

plt.rcParams.update({
    "font.family": "DejaVu Sans", "font.size": 12, "axes.edgecolor": "#9CA3AF", "axes.labelcolor": INK,
    "xtick.color": MUTED, "ytick.color": MUTED, "axes.spines.top": False, "axes.spines.right": False,
    "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.8, "axes.axisbelow": True,
    "figure.dpi": 150, "savefig.dpi": 150, "savefig.bbox": "tight", "savefig.pad_inches": 0.25,
})


def load(n):
    return ([json.loads(l) for l in open(D / f"{n}.samples.jsonl")], json.load(open(D / f"{n}.marks.json")))


DATA = {n: load(n) for n in ("controller", "native")}


def marks(n, phase):
    return [m for m in DATA[n][1] if m["phase"] == phase]


def mt(n, phase):
    return marks(n, phase)[0]["t"]


def window(n, t0, t1):
    return [s for s in DATA[n][0] if t0 <= s["t"] <= t1]


def tot(s, key):
    return sum(s["svc"][k][key] for k in ("sandbox", "toolcall"))


def titles(fig, title, subtitle):
    fig.text(0.0, 1.06, title, fontsize=16, fontweight="bold", color=INK, ha="left", va="bottom",
             transform=fig.axes[0].transAxes if len(fig.axes) == 1 else fig.transFigure)
    fig.text(0.0, 1.0, subtitle, fontsize=11.5, color=MUTED, ha="left", va="bottom",
             transform=fig.axes[0].transAxes if len(fig.axes) == 1 else fig.transFigure)


def save(fig, name):
    fig.savefig(OUT / name, facecolor="white")
    plt.close(fig)
    print("wrote", OUT / name)


# 1. Latency protection ------------------------------------------------------------------------------------------
fig, ax = plt.subplots(figsize=(9, 4.6))
ax.axhspan(0, 30, color="#ECFDF5", zorder=0)
ax.axhline(30, color="#10B981", lw=1.2, ls="--")
ax.text(0.3, 30.4, "TTFT budget: 1.5 × solo (30 ms)", color="#047857", fontsize=10.5, va="bottom")
for n, dy in (("controller", 0.7), ("native", -1.1)):
    ms = marks(n, "backfill")
    x, y = [m["pods"] for m in ms], [m["ttft"]["p99"] for m in ms]
    ax.plot(x, y, "-o", color=C[n], lw=2.6, ms=6, zorder=3)
    ax.annotate(f"{NAME[n]}  {y[-1]:.0f} ms", (x[-1], y[-1]), xytext=(10, dy * 10), textcoords="offset points",
                color=C[n], fontsize=11.5, fontweight="bold", va="center")
ax.annotate("solo: 20 ms", (0, 20), xytext=(6, -16), textcoords="offset points", color=MUTED, fontsize=10.5)
ax.set_xlim(-0.5, 23.5)
ax.set_ylim(15, 33)
ax.set_xticks([0, 4, 8, 12, 16, 18])
ax.set_xlabel("CPU pods sharing the vLLM node (2 vCPU each)")
ax.set_ylabel("vLLM TTFT p99 (ms)")
titles(fig, "Both recipes keep vLLM inside its latency budget",
       "TTFT p99 at 450 requests/s, 1 vLLM replica, as the G4 node fills with CPU pods. Same node shape, same moment.")
save(fig, "1_latency_protection.png")

# 2. Latency under load: two small multiples -----------------------------------------------------------------------
fig, axes = plt.subplots(1, 2, figsize=(11, 4.6), sharey=True)
fig.subplots_adjust(top=0.78, wspace=0.12)
for ax, pods, label in ((axes[0], 0, "vLLM alone on the node"), (axes[1], 18, "Node full: vLLM + 18 CPU pods")):
    for n in ("controller", "native"):
        ms = [m for m in marks(n, "load") if m["pods"] == pods]
        x, y = [m["rate"] for m in ms], [m["ttft"]["p99"] for m in ms]
        ax.plot(x, y, "-o", color=C[n], lw=2.6, ms=6)
        ax.annotate(f"{y[-1]:.0f}", (x[-1], y[-1]), xytext=(8, 0), textcoords="offset points", color=C[n],
                    fontsize=11, fontweight="bold", va="center")
    ax.set_title(label, loc="left", fontsize=12.5, color=INK, pad=8)
    ax.set_xlabel("Offered load (requests/s)")
    ax.set_xticks([200, 450, 700, 1000, 1200])
    ax.set_xlim(150, 1330)
axes[0].set_ylabel("TTFT p99 (ms)")
fig.text(0.0, 1.25, "Under heavy load, latency tracks the node, not the recipe", fontsize=16, fontweight="bold",
         color=INK, transform=axes[0].transAxes)
fig.text(0.0, 1.17, "TTFT p99 vs load, 1 vLLM replica. The controller cluster's vLLM cores happened to span two L3 caches, "
         "so it is ~10 ms slower at 1,000+ requests/s, even alone.", fontsize=11.5, color=MUTED, transform=axes[0].transAxes)
axes[1].legend(handles=[Patch(color=C["controller"], label=NAME["controller"]),
                        Patch(color=C["native"], label=NAME["native"])], frameon=False, loc="upper left", fontsize=11)
save(fig, "2_latency_under_load.png")


# 3 & 4. Where the CPU pods run over time (stacked area) ------------------------------------------------------------
def placement(ax, n, t0, t1, demand_fn, events, ylim):
    w = window(n, t0 - 20, t1)
    x = [s["t"] - t0 for s in w]
    g4 = [tot(s, "bf_ready") for s in w]
    n4 = [tot(s, "of_ready") for s in w]
    ax.stackplot(x, g4, n4, colors=[G4_FILL[n], N4D], lw=0, alpha=0.95)
    ax.plot(x, [demand_fn(s["t"]) for s in w], color=INK, lw=2, drawstyle="steps-post")
    for i, (tx, label) in enumerate(events):
        ax.axvline(tx, color=MUTED, lw=1, ls=":")
        ax.text(tx + 6, ylim * (0.97 - 0.1 * (i % 2)), label, color=INK, fontsize=10.5, va="top",
                bbox=dict(facecolor="white", edgecolor="none", alpha=0.8, pad=1))
    ax.set_ylim(0, ylim)
    ax.set_xlim(-20, x[-1])
    ax.set_ylabel("CPU pods Ready")
    ax.text(0.995, 0.04, NAME[n], transform=ax.transAxes, ha="right", fontsize=12.5, fontweight="bold", color=C[n])


fig, axes = plt.subplots(2, 1, figsize=(11, 6.6), sharex=True)
for ax, n in zip(axes, ("controller", "native")):
    tu, td, te = mt(n, "step_up"), mt(n, "step_down"), mt(n, "step_end")
    cov = marks(n, "step_down")[0]["covered_after_s"]
    dr = marks(n, "step_end")[0]["drained_after_s"]
    placement(ax, n, tu, min(te, td + 200), lambda t, tu=tu, td=td: 40 if tu <= t < td else 18,
              [(0, "demand 18 → 40"), (td - tu, "demand 40 → 18")], 46)
    ax.annotate(f"all 40 running after {cov:.0f} s", (cov, 40), xytext=(cov + 15, 28), color=INK, fontsize=11,
                arrowprops=dict(arrowstyle="->", color=INK, lw=1))
    ax.annotate(f"N4D empty {dr:.0f} s after the drop", (td - tu + dr, 18), xytext=(td - tu + dr + 15, 8),
                color=INK, fontsize=11, arrowprops=dict(arrowstyle="->", color=INK, lw=1))
axes[1].set_xlabel("seconds since demand rose")
fig.text(0.0, 1.24, "A CPU demand spike spills to N4D in under 90 s on both recipes", fontsize=16, fontweight="bold",
         color=INK, transform=axes[0].transAxes)
fig.text(0.0, 1.16, "CPU pods Ready over time. G4 holds what fits; the rest runs on N4D. Most of the wait is N4D nodes "
         "booting. The controller reacts faster: 18 s up, 26 s down.", fontsize=11.5, color=MUTED, transform=axes[0].transAxes)
LEG = [Patch(color="#B8C2D6", label="CPU pods on G4 spare cores (free)"), Patch(color=N4D, label="CPU pods on N4D (paid)"),
       plt.Line2D([], [], color=INK, lw=2, label="demand")]
axes[0].legend(handles=LEG, frameon=False, loc="lower left", ncol=3, fontsize=10.5, bbox_to_anchor=(0.0, 1.0))
save(fig, "3_cpu_demand_spike.png")

fig, axes = plt.subplots(2, 1, figsize=(11, 7.0))
fig.subplots_adjust(hspace=0.35)
for ax, n in zip(axes, ("controller", "native")):
    t0, tin, te = mt(n, "gpu_out"), mt(n, "gpu_in"), mt(n, "gpu_end")
    w = window(n, t0, te)
    tv2 = next(s["t"] for s in w if len(s["vllm_ready_nodes"]) >= 2) - t0
    tv1 = next((s["t"] for s in window(n, tin, te) if len(s["vllm_ready_nodes"]) < 2), tin) - t0
    gone = marks(n, "gpu_end")[0]["node_removed_after_s"] + (tin - t0)
    placement(ax, n, t0, te, lambda t: 36, [(tv2, "2nd vLLM Ready"), (tin - t0, "load drops"), (gone, "G4 node removed")], 58)
    pct_all = 100 * sum(tot(s, "bf_ready") + tot(s, "of_ready") >= 36 for s in w) / len(w)
    pct_in = 100 * sum(tot(s, "bf_ready") + tot(s, "of_ready") >= 36 for s in window(n, tin, te)) / len(window(n, tin, te))
    ax.text(0.01, 0.06, f"Ready ≥ demand: {pct_in:.0f} % during scale-in, {pct_all:.0f} % over the whole run",
            transform=ax.transAxes, color=INK, fontsize=11, bbox=dict(facecolor="white", edgecolor="none", alpha=0.85))
    ax.set_xlabel("seconds since LLM load rose from 450 to 1,200 requests/s")
NOTE = dict(fontsize=10, color="#92400E", bbox=dict(facecolor="#FFFBEB", edgecolor="#F59E0B", boxstyle="round,pad=0.4"))
axes[0].annotate("vLLM flapped 2 → 1 → 2 (15 s scale-down window):\nN4D briefly covered 7 pods. Fixed: 120 s window,\nretested with 0 drops.",
                 (320, 32), xytext=(470, 41), arrowprops=dict(arrowstyle="->", color="#B45309", lw=1), **NOTE)
axes[1].annotate("Same flap: one node's CPU pods were stranded,\nso N4D ran 18 extra pods until it ended\n(over-provisioned, nothing lost).",
                 (560, 50), xytext=(600, 14), arrowprops=dict(arrowstyle="->", color="#B45309", lw=1), **NOTE)
axes[0].legend(handles=LEG, frameon=False, loc="lower left", ncol=3, fontsize=10.5, bbox_to_anchor=(0.0, 1.0))
fig.text(0.0, 1.24, "GPU scale-in keeps every CPU pod running on both recipes", fontsize=16, fontweight="bold",
         color=INK, transform=axes[0].transAxes)
fig.text(0.0, 1.16, "36 CPU pods of demand. When vLLM adds a G4 node, CPU pods move onto it at once and N4D empties. "
         "When vLLM leaves, N4D takes over before the node is removed.", fontsize=11.5, color=MUTED, transform=axes[0].transAxes)
save(fig, "4_gpu_scale_out_in.png")

# 5. Scorecard: dumbbell of reaction times ------------------------------------------------------------------------
S = json.load(open(D / "summary.json"))
rows = [("CPU spike: all pods running", "step_covered_after_s"),
        ("CPU drop: N4D empty", "step_drained_after_s"),
        ("LLM spike: 2nd vLLM Ready", "vllm2_ready_after_s"),
        ("LLM drop: G4 node removed", "node_removed_after_s")]
fig, ax = plt.subplots(figsize=(10, 3.9))
for i, (label, key) in enumerate(rows):
    a, b = S["controller"][key], S["native"][key]
    y = len(rows) - 1 - i
    ax.plot([a, b], [y, y], color="#D1D5DB", lw=3, zorder=1)
    for n, v in (("controller", a), ("native", b)):
        ax.scatter([v], [y], s=110, color=C[n], zorder=3)
    lo, hi = (("controller", a), ("native", b)) if a <= b else (("native", b), ("controller", a))
    ax.text(lo[1] - 16, y, f"{lo[1]:.0f} s", ha="right", va="center", color=C[lo[0]], fontsize=11, fontweight="bold")
    ax.text(hi[1] + 16, y, f"{hi[1]:.0f} s", ha="left", va="center", color=C[hi[0]], fontsize=11, fontweight="bold")
ax.set_yticks(range(len(rows)), [r[0] for r in reversed(rows)], fontsize=11.5, color=INK)
ax.set_xlim(-60, 700)
ax.set_xlabel("seconds (lower is faster)")
ax.grid(axis="y", visible=False)
ax.legend(handles=[Patch(color=C["controller"], label=NAME["controller"]), Patch(color=C["native"], label=NAME["native"])],
          frameon=False, loc="upper right", fontsize=11)
titles(fig, "The controller reacts faster to CPU changes; GPU timings are the same",
       "Time from the trigger to the outcome. GPU timings are dominated by vLLM start-up and node removal, not the recipe.")
save(fig, "5_reaction_times.png")
