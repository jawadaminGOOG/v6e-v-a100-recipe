#!/usr/bin/env python3
"""GPU-workload impact by co-located workload class.

SPEC CPU 2026 runs its benchmarks one after another: the 14 integer benchmarks
first, then the 2 memory-bandwidth-bound FP benchmarks (749.fotonik3d_r,
782.lbm_r). Each colo run is therefore split into two phases using runcpu's own
benchmark start times:

  compute   : first integer benchmark start  -> 749.fotonik3d_r start
  bandwidth : 749.fotonik3d_r start          -> SPEC end

vLLM requests are assigned to a phase by send time. Solo runs have no SPEC, so
each solo run is cut at the same load offsets (mean phase offsets of the colo
runs at that level), which keeps time-in-run identical between groups.

Statistics: one number per run per metric; effect = mean(colo)/mean(solo) - 1.
Two 95% intervals are computed: percentile bootstrap (20k) and Welch t on the
difference of means scaled by the solo mean. Reported intervals use the wider
(more conservative) bound of the two.

phase_summary.csv reports, per level and class, the worst latency upper bound,
the throughput lower bound, failed requests and phase length.
"""
from __future__ import annotations

import glob
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

ROOT = Path(__file__).resolve().parent
CELLS = ROOT / "results" / "cells"
OUT = ROOT / "results" / "analysis"
CFG = json.loads((ROOT / "experiment.json").read_text())
RNG = np.random.default_rng(12345)
LAT = ["ttft_p50", "ttft_p99", "itl_p50", "e2e_p50", "e2e_p99"]
FP_FIRST = "749.fotonik3d_r"


def load_requests(d: Path):
    sums = [json.loads(Path(f).read_text()) for f in glob.glob(str(d / "*.summary.json"))]
    df = pd.concat([pd.read_csv(f) for f in glob.glob(str(d / "*.requests.csv.gz"))], ignore_index=True)
    return df, min(s["base_unix"] for s in sums)


def spec_phases(d: Path) -> dict:
    """Unix times of first integer benchmark start, FP start, SPEC end, from SPEC's own logs."""
    log = (d / "runcpu.log").read_text()
    starts = {m.group(1): datetime.strptime(m.group(2), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
              for m in re.finditer(r"Running (\d{3}\.\w+) train base \S+ \(\d+ copies\) \[([\d\- :]+)\]", log)}
    end = float(re.search(r"SPEC_END (\S+)", (d / "spec_pod.log").read_text()).group(1))
    t_int = min(v for k, v in starts.items() if k != FP_FIRST and k != "782.lbm_r")
    return {"compute": (t_int, starts[FP_FIRST]), "bandwidth": (starts[FP_FIRST], end)}


def metrics(df: pd.DataFrame, t0: float, t1: float) -> dict:
    w = df[(df.send_unix >= t0) & (df.send_unix < t1)]
    ok = w[w.ok == True]  # noqa: E712
    comp = df[(df.ok == True) & (df.done_unix >= t0) & (df.done_unix < t1)]  # noqa: E712
    return {"n": len(w), "n_failed": int(len(w) - len(ok)),
            "ttft_p50": ok.ttft.quantile(.5), "ttft_p99": ok.ttft.quantile(.99),
            "itl_p50": ok.itl_mean.quantile(.5), "e2e_p50": ok.e2e.quantile(.5), "e2e_p99": ok.e2e.quantile(.99),
            "output_tok_s": comp.completion_tokens.sum() / (t1 - t0)}


def intervals(a: np.ndarray, b: np.ndarray, n=20000):
    est = (a.mean() / b.mean() - 1) * 100
    ia, ib = RNG.integers(0, len(a), (n, len(a))), RNG.integers(0, len(b), (n, len(b)))
    bs = (a[ia].mean(1) / b[ib].mean(1) - 1) * 100
    va, vb = a.var(ddof=1) / len(a), b.var(ddof=1) / len(b)
    se = np.sqrt(va + vb)
    dof = (va + vb) ** 2 / (va ** 2 / (len(a) - 1) + vb ** 2 / (len(b) - 1)) if se > 0 else 1
    half = stats.t.ppf(0.975, dof) * se / b.mean() * 100
    return est, float(np.percentile(bs, 2.5)), float(np.percentile(bs, 97.5)), est - half, est + half


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    runs = {}
    for d in sorted(CELLS.iterdir()):
        m = json.loads((d / "DONE").read_text()) if (d / "DONE").exists() else None
        if m and m["rep"] > 0 and m["kind"] in ("colo", "vllm_solo"):
            runs[m["cell"]] = (m, d)

    rows = []
    offsets = {}
    for cell, (m, d) in runs.items():
        if m["kind"] != "colo":
            continue
        df, base = load_requests(d)
        ph = spec_phases(d)
        for p, (t0, t1) in ph.items():
            offsets.setdefault((m["level"], p), []).append((t0 - base, t1 - base))
            rows.append({"cell": cell, "kind": "colo", "level": m["level"], "rep": m["rep"], "phase": p,
                         "start_off": t0 - base, "end_off": t1 - base, **metrics(df, t0, t1)})
    mean_off = {k: tuple(np.mean(v, axis=0)) for k, v in offsets.items()}
    for cell, (m, d) in runs.items():
        if m["kind"] != "vllm_solo":
            continue
        df, base = load_requests(d)
        for (L, p), (o0, o1) in mean_off.items():
            rows.append({"cell": cell, "kind": "solo", "level": L, "rep": m["rep"], "phase": p,
                         "start_off": o0, "end_off": o1, **metrics(df, base + o0, base + o1)})
    per_run = pd.DataFrame(rows)
    per_run.to_csv(OUT / "vllm_phase_runs.csv", index=False)

    imp, verdict = [], []
    for (L, p), g in per_run.groupby(["level", "phase"]):
        c, s = g[g.kind == "colo"], g[g.kind == "solo"]
        lat_hi, thr_lo = [], None
        for met in LAT + ["output_tok_s"]:
            a, b = c[met].to_numpy(float), s[met].to_numpy(float)
            est, blo, bhi, tlo, thi = intervals(a, b)
            imp.append({"level": L, "phase": p, "metric": met, "solo_mean": b.mean(), "colo_mean": a.mean(),
                        "delta_pct": est, "boot_lo": blo, "boot_hi": bhi, "welch_lo": tlo, "welch_hi": thi,
                        "n_solo": len(b), "n_colo": len(a)})
            if met == "output_tok_s":
                thr_lo = min(blo, tlo)
            else:
                lat_hi.append(max(bhi, thi))
        fails = int(c.n_failed.sum())
        verdict.append({"level": L, "phase": p, "spare_vcpu": L, "worst_latency_upper_pct": max(lat_hi),
                        "throughput_lower_pct": thr_lo, "colo_failed_requests": fails,
                        "phase_seconds": float(c.end_off.mean() - c.start_off.mean())})
    pd.DataFrame(imp).to_csv(OUT / "vllm_phase_impact.csv", index=False)
    v = pd.DataFrame(verdict)
    v.to_csv(OUT / "phase_summary.csv", index=False)
    print(v.round(2).to_string())


if __name__ == "__main__":
    main()
