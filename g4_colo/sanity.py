#!/usr/bin/env python3
"""Sanity runs: (1) max throughput under co-location, (2) GPU workload with all 48 vCPU.

(1) cells-sanity/cap-L{0,20,38}-r{1,2}: saturating closed-loop load (500 in flight)
    for the whole SPEC run. Completed requests/s and output tokens/s are computed
    per SPEC phase (integer / fotonik3d / lbm, from runcpu's benchmark start times).
    Solo runs (L0) are cut at the mean phase offsets of the co-located runs.
(2) golden_*: vLLM without a CPU limit (all 48 vCPU) - closed-loop capacity probes
    and fixed-rate 625 req/s runs, compared with the 8-vCPU solo runs.
Writes results/sanity/*.csv.
"""
import glob
import json
from pathlib import Path

import numpy as np
import pandas as pd

from phases import intervals, load_requests, spec_phases

ROOT = Path(__file__).resolve().parent
CS = ROOT / "results" / "cells-sanity"
OUT = ROOT / "results" / "sanity"


def phase_bounds(d: Path):
    ph = spec_phases(d, split=True)
    return {"integer": ph["compute"], "fotonik3d": ph["fotonik3d"], "lbm": ph["lbm"],
            "fotonik3d+lbm": (ph["fotonik3d"][0], ph["lbm"][1])}


def throughput(df, t0, t1):
    c = df[(df.ok == True) & (df.done_unix >= t0) & (df.done_unix < t1)]  # noqa: E712
    w = df[(df.send_unix >= t0) & (df.send_unix < t1) & (df.ok == True)]  # noqa: E712
    return {"rps": len(c) / (t1 - t0), "tok_s": c.completion_tokens.sum() / (t1 - t0),
            "ttft_p50": w.ttft.quantile(.5), "e2e_p50": w.e2e.quantile(.5), "failed": int((df.ok != True).sum())}  # noqa: E712


def capacity():
    rows, offs = [], {}
    cells = {}
    for d in sorted(CS.glob("cap-L*-r*")):
        m = json.loads((d / "DONE").read_text())
        cells[d.name] = (m, d)
    for name, (m, d) in cells.items():
        if m["level"] == 0:
            continue
        df, base = load_requests(d)
        for p, (t0, t1) in phase_bounds(d).items():
            offs.setdefault((m["level"], p), []).append((t0 - base, t1 - base))
            rows.append({"cell": name, "kind": "colo", "level": m["level"], "rep": m["rep"], "phase": p, **throughput(df, t0, t1)})
    mean_off = {k: tuple(np.mean(v, axis=0)) for k, v in offs.items()}
    for name, (m, d) in cells.items():
        if m["level"] != 0:
            continue
        df, base = load_requests(d)
        for (L, p), (o0, o1) in mean_off.items():
            rows.append({"cell": name, "kind": "solo", "level": L, "rep": m["rep"], "phase": p,
                         **throughput(df, base + o0, base + o1)})
    runs = pd.DataFrame(rows)
    runs.to_csv(OUT / "capacity_runs.csv", index=False)
    summ = []
    for (L, p), g in runs.groupby(["level", "phase"]):
        a, b = g[g.kind == "colo"].rps.to_numpy(float), g[g.kind == "solo"].rps.to_numpy(float)
        est, blo, bhi, tlo, thi = intervals(a, b)
        summ.append({"level": L, "phase": p, "solo_rps": b.mean(), "colo_rps": a.mean(), "delta_pct": est,
                     "lo": min(blo, tlo), "hi": max(bhi, thi), "n": len(a),
                     "solo_tok_s": g[g.kind == "solo"].tok_s.mean(), "colo_tok_s": g[g.kind == "colo"].tok_s.mean()})
    s = pd.DataFrame(summ)
    s.to_csv(OUT / "capacity_summary.csv", index=False)
    print(s.round(2).to_string())


def golden_latency():
    """Fixed-rate 625 req/s: all-48-vCPU vLLM (cells-golden) vs 8-vCPU solo runs of the main matrix,
    over the same 60-660 s window of each run."""
    from analyze import vllm_cell_metrics
    rows = []
    for grp, pat in [("8 vCPU / 64 GiB", "results/cells/vllm_solo-r[1-5]"), ("all 48 vCPU", "results/cells-golden/vllm_solo-r[1-5]")]:
        for d in sorted(glob.glob(str(ROOT / pat))):
            m = vllm_cell_metrics(Path(d))
            if m:
                m.pop("window")
                rows.append({"config": grp, "cell": Path(d).name, **m})
    if not rows:
        return
    r = pd.DataFrame(rows)
    r.to_csv(OUT / "golden_latency_runs.csv", index=False)
    a8, a48 = r[r.config == "8 vCPU / 64 GiB"], r[r.config == "all 48 vCPU"]
    if len(a48) < 2:
        return
    summ = []
    for met in ["ttft_p50", "ttft_p99", "itl_p50", "e2e_p50", "e2e_p99", "output_tok_s"]:
        est, blo, bhi, tlo, thi = intervals(a8[met].to_numpy(float), a48[met].to_numpy(float))
        summ.append({"metric": met, "all48_mean": a48[met].mean(), "cap8_mean": a8[met].mean(),
                     "cap8_vs_all48_pct": est, "lo": min(blo, tlo), "hi": max(bhi, thi),
                     "n_8": len(a8), "n_48": len(a48)})
    pd.DataFrame(summ).to_csv(OUT / "golden_latency.csv", index=False)
    print(pd.DataFrame(summ).round(4).to_string())


def golden():
    out = []
    for f in sorted(glob.glob(str(ROOT / "results" / "sanity" / "golden_probe_*.json"))):
        out.append(json.loads(Path(f).read_text()))
    if out:
        pd.DataFrame(out).to_csv(OUT / "golden_capacity.csv", index=False)
        print(pd.DataFrame(out).to_string())


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    capacity()
    golden()
    golden_latency()
