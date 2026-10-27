#!/usr/bin/env python3
"""Layer diagram of the g4-standard-48 node as used in the g4-colo experiment.

Topology facts are from `lscpu -e` and the kubelet CPU-manager state recorded in
every cell (results/cells/*/cpu_manager_state.*.json). Regenerate with:
    python3 diagrams/g4_layers.py
"""
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import FancyBboxPatch, Rectangle  # noqa: E402

OUT = Path(__file__).resolve().parent
C = {"sys": "#9aa0a6", "vllm": "#1a73e8", "spec": "#e8710a", "box": "#f8f9fa", "edge": "#dadce0",
     "text": "#202124", "sub": "#5f6368", "warn_bg": "#fce8e6", "warn": "#c5221f"}


def rbox(ax, x, y, w, h, fc=C["box"], ec=C["edge"], lw=1.2, r=0.012):
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle=f"round,pad=0,rounding_size={r}", fc=fc, ec=ec, lw=lw))


def owner(core):
    if core == 0:
        return "sys"
    if 1 <= core <= 4:
        return "vllm"
    return "spec"


fig = plt.figure(figsize=(16, 11.5))
ax = fig.add_axes([0, 0, 1, 1])
ax.set_xlim(0, 1)
ax.set_ylim(0, 1)
ax.axis("off")

ax.text(0.04, 0.955, "Google Cloud g4-standard-48: how the g4-colo experiment uses it", fontsize=22, weight="bold", color=C["text"])
ax.text(0.04, 0.925, "One VM · 1 socket / 1 NUMA node · 24 physical cores · 48 logical CPUs (SMT sibling = CPU + 24) · 180 GiB · 1 GPU",
        fontsize=12.5, color=C["sub"])

# Callout: cache boundary
rbox(ax, 0.62, 0.80, 0.34, 0.105, fc=C["warn_bg"], ec=C["warn"], lw=1.5)
ax.text(0.635, 0.880, "THE SHARED L3 BOUNDARY", fontsize=11.5, weight="bold", color=C["warn"])
ax.text(0.635, 0.812, "vLLM's 4 cores share L3 domain 0 with SPEC cores 5-7.\nExclusive cores are not exclusive cache. With one\nvLLM frontend this caused a latency collapse; with 4\nfrontends it costs a few ms (see results).",
        fontsize=10.5, color=C["text"], va="bottom")

# Socket
rbox(ax, 0.04, 0.44, 0.92, 0.35, fc="white")
ax.text(0.06, 0.758, "AMD EPYC 9B45 (Turin) · 24 physical cores · 48 threads · 3 L3 domains of 8 cores (16 CPUs), 32 MB each",
        fontsize=13, weight="bold", color=C["text"])

lx0, lw_, gap = 0.06, 0.285, 0.0175
for d in range(3):
    x = lx0 + d * (lw_ + gap)
    hl = d == 0
    rbox(ax, x, 0.555, lw_, 0.175, fc="#fff4ec" if hl else C["box"], ec=C["warn"] if hl else C["edge"], lw=2 if hl else 1.2)
    ax.text(x + lw_ / 2, 0.705, f"L3 domain {d}  ·  cores {d*8}-{d*8+7}", ha="center", fontsize=12, weight="bold",
            color=C["warn"] if hl else C["text"])
    for i in range(8):
        core = d * 8 + i
        cx = x + 0.012 + i * (lw_ - 0.024) / 8
        cw = (lw_ - 0.024) / 8 - 0.005
        o = owner(core)
        ax.add_patch(Rectangle((cx, 0.585), cw, 0.09, fc=C[o], ec="white", lw=1))
        ax.text(cx + cw / 2, 0.645, f"c{core}", ha="center", fontsize=9.5, color="white", weight="bold")
        ax.text(cx + cw / 2, 0.600, f"{core}\n{core+24}", ha="center", fontsize=7.5, color="white", linespacing=1.0)
        if 5 <= core <= 14:  # 50% level footprint
            ax.add_patch(Rectangle((cx, 0.570), cw, 0.008, fc="#5f6368", ec="none"))
ax.text(0.06, 0.538, "▬ grey underline = cores used at the 50% level (20 vCPU, CPUs 5-14 + 29-38). The 95% level uses all orange cores (38 vCPU, CPUs 5-23 + 29-47).",
        fontsize=9.5, color=C["sub"])

# Memory
rbox(ax, 0.06, 0.45, 0.88, 0.048, fc=C["box"])
tot = 180.0
segs = [("vLLM 64 GiB (cgroup limit)", 64, C["vllm"]), ("SPEC ≤100 GiB (95%) / 58 GiB (50%)", 100, C["spec"]),
        ("GKE ~16 GiB", 16, C["sys"])]
x = 0.07
for lbl, g, col in segs:
    w = 0.86 * g / tot
    ax.add_patch(Rectangle((x, 0.457), w, 0.033, fc=col, ec="white"))
    ax.text(x + w / 2, 0.4735, lbl, ha="center", va="center", fontsize=9.5, color="white", weight="bold")
    x += w
ax.text(0.06, 0.508, "180 GiB DRAM · memory bandwidth shared by all cores (cannot be partitioned in the VM)", ha="left", fontsize=10, color=C["sub"], weight="bold")

# PCIe + GPU
ax.plot([0.5, 0.5], [0.44, 0.425], color=C["edge"], lw=2)
rbox(ax, 0.42, 0.385, 0.16, 0.04, fc="white")
ax.text(0.5, 0.405, "PCIe", ha="center", va="center", fontsize=12, weight="bold")
ax.plot([0.5, 0.5], [0.385, 0.355], color=C["edge"], lw=2)
rbox(ax, 0.36, 0.285, 0.28, 0.07, fc="#e8f0fe", ec=C["vllm"], lw=1.6)
ax.text(0.5, 0.330, "NVIDIA RTX PRO 6000 Blackwell · 96 GB", ha="center", fontsize=12.5, weight="bold", color=C["text"])
ax.text(0.5, 0.302, "dedicated to vLLM (Qwen3-1.7B, FP16)", ha="center", fontsize=10.5, color=C["vllm"])

# Kubernetes layer
ax.text(0.04, 0.245, "Kubernetes layer (GKE, kubelet static CPU manager: Guaranteed QoS + integer CPUs → exclusive cpuset)", fontsize=13, weight="bold", color=C["text"])
pods = [
    (0.04, C["vllm"], "vllm pod", "8 vCPU / 64 GiB / 1 GPU = AWS g7e.2xlarge shape\ncpuset 1-4,25-28 · --api-server-count=4\nvLLM v0.32.0 (image pinned by digest)"),
    (0.35, C["spec"], "spec pod (the reclaimed workload)", "SPEC CPU 2026 rate, 16 benchmarks, train\n50%: 20 copies on 20 vCPU · 95%: 38 copies on 38 vCPU\ncpuset 5-14,29-38 or 5-23,29-47"),
    (0.66, C["sys"], "shared pool (core 0)", "kubelet, kube-proxy, logging, GPU device plugin,\nnetworking, 1 s telemetry sampler\nCPUs 0,24 (GKE never hands these out exclusively)"),
]
for x, col, title, body in pods:
    rbox(ax, x, 0.105, 0.30, 0.125, fc="white", ec=col, lw=2)
    ax.add_patch(Rectangle((x, 0.205), 0.30, 0.025, fc=col, ec=col))
    ax.text(x + 0.01, 0.2175, title, fontsize=11.5, weight="bold", color="white", va="center")
    ax.text(x + 0.01, 0.195, body, fontsize=10, color=C["text"], va="top", linespacing=1.35)

# Load generator
rbox(ax, 0.04, 0.025, 0.92, 0.06, fc=C["box"])
ax.text(0.055, 0.055, "Load: 2 client pods on separate e2-standard-8 nodes → open-loop Poisson 625 req/s (75% of measured 832 RPS capacity), streaming,\ncustomer prompts (synthetic_yelp, ~600 in / ~11 out tokens). Baseline = vLLM solo cells from the same interleaved run.",
        fontsize=10.5, color=C["text"], va="center")

# Legend
for i, (k, lbl) in enumerate([("vllm", "vLLM (GPU workload)"), ("spec", "SPEC (co-located CPU workload)"), ("sys", "GKE system")]):
    ax.add_patch(Rectangle((0.04 + i * 0.2, 0.892), 0.014, 0.014, fc=C[k]))
    ax.text(0.058 + i * 0.2, 0.899, lbl, fontsize=10.5, va="center", color=C["text"])

fig.savefig(OUT / "g4_layers.png", dpi=140)
fig.savefig(OUT / "g4_layers.svg")
print(OUT / "g4_layers.png")
