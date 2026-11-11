#!/usr/bin/env python3
"""Per-CPU busy % heatmap of the vLLM node during the backfill ladder, from sweep.py --cpu-1s output.

Same look as g4_colo/plots.py util_heatmap (viridis, 0-100 %, logical CPU on y, seconds on x), plus the ladder
steps on x and vLLM's exclusive CPUs / L3 groups on y. Also writes the matrix as telemetry-style CSV
(t,busy0..busyN), the format g4_colo/plots.py reads.

    g4_colo/.venv/bin/python cpu_heatmap.py <sweep dir> <cluster>    # e.g. .agents/bench/v6e_sweep1 tpu
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

D, NAME = Path(sys.argv[1]), sys.argv[2]
OUT = D / "charts"
OUT.mkdir(exist_ok=True)
marks = json.load(open(D / f"{NAME}.marks.json"))
bf = [m for m in marks if m["phase"] == "backfill"]
t_start = bf[0]["t0"] - 40
t_end = bf[-1]["t1"]
rows = [json.loads(l) for l in open(D / f"{NAME}.cpus.jsonl")]
rows = [r for r in rows if t_start <= r["t"] <= t_end and r.get("busy")]
node = max({r["node"] for r in rows}, key=lambda n: sum(r["node"] == n for r in rows))
rows = [r for r in rows if r["node"] == node]
busy = np.array([r["busy"] for r in rows]).T  # cpu x time
ts = np.array([r["t"] for r in rows]) - t_start
ncpu = busy.shape[0]
vllm = sorted({int(c) for r in rows for c, o in (r.get("owner") or {}).items() if o == "vllm"})

with open(D / f"{NAME}.backfill_telemetry.csv", "w", newline="") as fh:
    w = csv.writer(fh)
    w.writerow(["t", *[f"busy{i}" for i in range(ncpu)], "pods_bf"])
    for r in rows:
        w.writerow([r["t"], *r["busy"], r.get("pods_bf")])

fig, ax = plt.subplots(figsize=(12, 4.6))
im = ax.imshow(busy, aspect="auto", cmap="viridis", vmin=0, vmax=100, interpolation="nearest",
               extent=(ts[0], ts[-1], ncpu - 0.5, -0.5))
for m in bf:
    ax.axvline(m["t0"] - 30 - t_start, color="white", lw=0.8, ls=":")
    ax.text(m["t0"] - 28 - t_start, -1.2, f"{m['pods']} pods\np99 {m['ttft']['p99']} ms", fontsize=8, va="bottom")
half = ncpu // 2
ax.axhline(half - 0.5, color="white", lw=0.6)
ax.set_ylabel("logical CPU (SMT siblings: N and N+%d)" % half)
ax.set_xlabel("time (s)")
rng = lambda c: f"{c[0]}-{c[-1]}" if c == list(range(c[0], c[-1] + 1)) else ",".join(map(str, c))  # noqa: E731
lo, hi = [c for c in vllm if c < half], [c for c in vllm if c >= half]
ax.set_title(f"Per-CPU busy % at 1 s, {NAME} vLLM node during the backfill ladder "
             f"(vLLM cpus {rng(lo)},{rng(hi)})", pad=30)
fig.colorbar(im, ax=ax, label="busy %")
fig.tight_layout()
fig.savefig(OUT / f"cpu_heatmap_{NAME}_backfill.png", dpi=130)
print("wrote", OUT / f"cpu_heatmap_{NAME}_backfill.png", busy.shape, node)
