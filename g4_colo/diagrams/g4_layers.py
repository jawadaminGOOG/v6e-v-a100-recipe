#!/usr/bin/env python3
"""g4-standard-48 node diagram (style follows the g4-standard-384 figure in
"The Idle Half of a GPU Pod"). Topology from `lscpu -e`; core ownership from the
kubelet CPU-manager state recorded in every run. Regenerate:
    python3 diagrams/g4_layers.py
"""
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch, Rectangle  # noqa: E402

OUT = Path(__file__).resolve().parent
BG = "#f6f5f2"
CARD = "#ffffff"
EDGE = "#e3e1dc"
TILE = "#efeeeb"
INK = "#1f1f1f"
SUB = "#6b6b6b"
ACC = "#d4572a"        # accent orange-red (reference highlight)
ACC_BG = "#fbe9e1"
SPEC = "#9a9a96"       # co-located CPU work: mid grey
SYS = "#d3d2ce"        # system: light grey
plt.rcParams["font.family"] = "DejaVu Sans"


def card(ax, x, y, w, h, fc=CARD, ec=EDGE, lw=1.4, r=0.012):
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle=f"round,pad=0,rounding_size={r}", fc=fc, ec=ec, lw=lw))


def owner(core):
    return "sys" if core == 0 else ("gpu" if core <= 4 else "cpu")


fig = plt.figure(figsize=(15, 11.2), facecolor=BG)
ax = fig.add_axes([0, 0, 1, 1])
ax.set_facecolor(BG)
ax.set_xlim(0, 1)
ax.set_ylim(0, 1)
ax.axis("off")

ax.text(0.045, 0.935, "Google Cloud g4-standard-48", fontsize=30, weight="bold", color=INK)
ax.text(0.045, 0.898, "One VM · 1 socket · 24 physical cores · 48 logical CPUs · 1 GPU", fontsize=15, color=SUB)

# callout
card(ax, 0.60, 0.835, 0.355, 0.125, fc=ACC_BG, ec=ACC_BG)
ax.add_patch(Rectangle((0.60, 0.835), 0.006, 0.125, fc=ACC, ec=ACC))
ax.text(0.625, 0.927, "THE CACHE BOUNDARY", fontsize=13, weight="bold", color=ACC)
ax.text(0.625, 0.848, "The GPU workload and the co-located CPU\nwork run on different physical cores, but\nshare this one last-level cache.",
        fontsize=13, color=INK, va="bottom", linespacing=1.45)

# socket card
card(ax, 0.045, 0.385, 0.91, 0.425)
ax.text(0.07, 0.77, "AMD EPYC 9B45", fontsize=19, weight="bold", color=INK)
ax.text(0.07, 0.737, "24 physical cores · 48 threads · 3 LLC domains of 16 CPUs (32 MB each)", fontsize=13.5, color=SUB)

tw, tg, tx0, ty, th = 0.27, 0.0225, 0.07, 0.535, 0.17
for d in range(3):
    x = tx0 + d * (tw + tg)
    hl = d == 0
    card(ax, x, ty, tw, th, fc=CARD if hl else TILE, ec=ACC if hl else TILE, lw=2.4 if hl else 1)
    ax.text(x + tw / 2, ty + th - 0.04, f"LLC {d}", ha="center", fontsize=17, weight="bold", color=ACC if hl else INK)
    ax.text(x + tw / 2, ty + th - 0.067, f"cores {8*d}–{8*d+7}", ha="center", fontsize=11, color=SUB)
    sw = (tw - 0.03) / 8
    for i in range(8):
        core = 8 * d + i
        o = owner(core)
        col = {"gpu": ACC, "cpu": SPEC, "sys": SYS}[o]
        ax.add_patch(FancyBboxPatch((x + 0.015 + i * sw + 0.002, ty + 0.025), sw - 0.004, 0.05,
                                    boxstyle="round,pad=0,rounding_size=0.004", fc=col, ec="none"))

# arrow from callout to LLC 0
yr = 0.721  # route between the socket subtitle and the LLC tiles
ax.plot([0.78, 0.78, tx0 + tw / 2], [0.835, yr, yr], color=ACC, lw=1.6, solid_joinstyle="round")
ax.add_patch(FancyArrowPatch((tx0 + tw / 2, yr), (tx0 + tw / 2, ty + th + 0.002), arrowstyle="-|>", mutation_scale=16,
                             color=ACC, lw=1.6))

# memory bar
card(ax, 0.07, 0.41, 0.86, 0.085, fc=TILE, ec=TILE)
ax.text(0.5, 0.462, "180 GB · NUMA node 0", ha="center", fontsize=16, weight="bold", color=INK)
ax.text(0.5, 0.428, "CPUs 0–47 · memory bandwidth shared by every core", ha="center", fontsize=12, color=SUB)

# PCIe + GPU
card(ax, 0.045, 0.165, 0.91, 0.2, fc="#efeeea", ec="#efeeea")
ax.plot([0.5, 0.5], [0.385, 0.315], color="#bdbbb5", lw=1.4)
card(ax, 0.40, 0.27, 0.20, 0.045)
ax.text(0.5, 0.2925, "PCIe switch", ha="center", va="center", fontsize=15, weight="bold", color=INK)
ax.plot([0.5, 0.5], [0.27, 0.245], color="#bdbbb5", lw=1.4)
card(ax, 0.41, 0.18, 0.18, 0.065, ec=ACC, lw=2)
ax.text(0.5, 0.222, "GPU 0", ha="center", fontsize=16, weight="bold", color=INK)
ax.text(0.5, 0.193, "96 GB", ha="center", fontsize=13, color=SUB)
ax.text(0.5, 0.142, "1 × NVIDIA RTX PRO 6000 Blackwell Server Edition · 96 GB", ha="center", fontsize=13, color=SUB)

# how it is used
card(ax, 0.045, 0.035, 0.91, 0.09)
ax.text(0.07, 0.097, "How this experiment uses it", fontsize=14, weight="bold", color=INK)
items = [(ACC, "GPU workload", "4 cores (8 vCPU) · 64 GB · GPU 0 = AWS g7e.2xlarge shape"),
         (SPEC, "Co-located CPU work", "up to 19 cores (38 vCPU)"),
         (SYS, "System (Kubernetes)", "1 core (2 vCPU)")]
xs = [0.07, 0.50, 0.75]
for (col, t, b), x in zip(items, xs):
    ax.add_patch(FancyBboxPatch((x, 0.050), 0.016, 0.024, boxstyle="round,pad=0,rounding_size=0.003", fc=col, ec="none"))
    ax.text(x + 0.024, 0.0675, t, fontsize=12.5, weight="bold", color=INK, va="center")
    ax.text(x + 0.024, 0.0485, b, fontsize=11, color=SUB, va="center")

ax.add_patch(Rectangle((0.045, 0.008), 0.011, 0.014, fc=SYS, ec="none"))
ax.text(0.062, 0.015, "one physical core (2 SMT threads) · 3 LLC domains × 16 CPUs = 48 logical CPUs · 1 NUMA node · 180 GB total memory",
        fontsize=12, color=SUB, va="center")

fig.savefig(OUT / "g4_layers.png", dpi=140, facecolor=BG)
fig.savefig(OUT / "g4_layers.svg", facecolor=BG)
print(OUT / "g4_layers.png")
