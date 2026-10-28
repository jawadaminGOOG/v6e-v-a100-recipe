#!/usr/bin/env python3
"""Latency distribution quantiles per scenario, pooled across reps.

Writes results/analysis/latency_quantiles.csv (committed), from which plots.py
draws the cumulative-distribution charts. Needs the raw per-request logs.
Scenarios: solo, colo 50% / 95%, each split by co-located workload class
(compute phase vs memory-bandwidth phase, see phases.py); solo runs are cut at
the same load offsets as the matching colo phase.
"""
import json
from pathlib import Path

import numpy as np
import pandas as pd

from phases import CELLS, OUT, load_requests

Q = np.concatenate([np.arange(0.0, 0.9, 0.01), np.arange(0.9, 0.99, 0.002), np.arange(0.99, 0.9995, 0.0005)])


def main():
    runs = pd.read_csv(OUT / "vllm_phase_runs.csv")
    cache = {}
    pools = {}
    for _, r in runs.iterrows():
        if r.cell not in cache:
            cache[r.cell] = load_requests(CELLS / r.cell)
        df, base = cache[r.cell]
        w = df[(df.send_unix >= base + r.start_off) & (df.send_unix < base + r.end_off) & (df.ok == True)]  # noqa: E712
        scen = "solo" if r.kind == "solo" else f"colo {'50%' if r.level == 20 else '95%'}"
        key = (r.phase, int(r.level), scen)
        pools.setdefault(key, []).append(w[["ttft", "itl_mean", "e2e"]])
    rows = []
    for (phase, L, scen), parts in pools.items():
        d = pd.concat(parts)
        for met in ["ttft", "itl_mean", "e2e"]:
            v = d[met].dropna().to_numpy()
            for q, x in zip(Q, np.quantile(v, Q)):
                rows.append({"phase": phase, "level": L, "scenario": scen, "metric": met, "q": round(q, 4),
                             "value_ms": x * 1e3, "n": len(v)})
    pd.DataFrame(rows).to_csv(OUT / "latency_quantiles.csv", index=False, float_format="%.4f")
    print("wrote", OUT / "latency_quantiles.csv", len(rows))


if __name__ == "__main__":
    main()
