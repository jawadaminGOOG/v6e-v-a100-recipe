#!/usr/bin/env python3
"""Analysis for g4-colo: turns results/cells/* into tables, plots and the headline ratio.

Statistics
  * Unit of replication = one cell (rep). Each metric is reduced to one number
    per cell (e.g. TTFT p50 over all requests in the analysis window).
  * Effect = mean(colo reps) / mean(solo reps) - 1, in %.
  * 95% CI = percentile bootstrap (20k resamples, reps resampled with
    replacement independently within each group).
  * PASS for a level = every guarded vLLM metric's CI includes 0 (the
    article's bar), i.e. no statistically detectable degradation.
  * Minimum detectable effect (MDE, 80% power, alpha .05, two-sided)
    reported per metric so "no effect" is read against resolution.
"""
from __future__ import annotations

import glob
import io
import json
import re
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
CELLS = ROOT / "results" / "cells"
OUT = ROOT / "results" / "analysis"
CFG = json.loads((ROOT / "experiment.json").read_text())
RNG = np.random.default_rng(12345)
GUARDED = ["ttft_p50", "ttft_p99", "itl_p50", "e2e_p50", "e2e_p99", "output_tok_s"]
LOWER_IS_BETTER = {"ttft_p50": True, "ttft_p99": True, "itl_p50": True, "itl_p99": True,
                   "e2e_p50": True, "e2e_p99": True, "output_tok_s": False, "achieved_rps": False}


def cell_meta(d: Path) -> dict | None:
    f = d / "DONE"
    return json.loads(f.read_text()) if f.exists() else None


# ------------------------------------------------------------------ vLLM side
def vllm_cell_metrics(d: Path) -> dict | None:
    files = sorted(glob.glob(str(d / "*.requests.csv.gz")))
    sums = [json.loads(Path(f).read_text()) for f in sorted(glob.glob(str(d / "*.summary.json")))]
    if not files or not sums:
        return None
    df = pd.concat([pd.read_csv(f) for f in files], ignore_index=True)
    base = min(s["base_unix"] for s in sums)
    t0, t1 = base + CFG["warmup_s"], base + CFG["warmup_s"] + CFG["window_s"]
    w = df[(df.send_unix >= t0) & (df.send_unix < t1)]
    ok = w[w.ok == True]  # noqa: E712
    comp = df[(df.ok == True) & (df.done_unix >= t0) & (df.done_unix < t1)]  # noqa: E712
    return {
        "n_window": len(w), "n_failed": int(len(w) - len(ok)),
        "ttft_p50": ok.ttft.quantile(.5), "ttft_p99": ok.ttft.quantile(.99),
        "itl_p50": ok.itl_mean.quantile(.5), "itl_p99": ok.itl_mean.quantile(.99),
        "e2e_p50": ok.e2e.quantile(.5), "e2e_p99": ok.e2e.quantile(.99),
        "output_tok_s": comp.completion_tokens.sum() / CFG["window_s"],
        "achieved_rps": len(comp) / CFG["window_s"],
        "avg_completion_tokens": ok.completion_tokens.mean(),
        "sched_lag_p99_ms": max(s["sched_lag_p99_ms"] or 0 for s in sums),
        "window": (t0, t1),
    }


def boot_ci(a: np.ndarray, b: np.ndarray, n=20000) -> tuple[float, float, float]:
    """% change of mean(a) vs mean(b) with percentile bootstrap CI."""
    est = (a.mean() / b.mean() - 1) * 100
    ia = RNG.integers(0, len(a), (n, len(a)))
    ib = RNG.integers(0, len(b), (n, len(b)))
    bs = (a[ia].mean(1) / b[ib].mean(1) - 1) * 100
    return est, float(np.percentile(bs, 2.5)), float(np.percentile(bs, 97.5))


def mde_pct(a: np.ndarray, b: np.ndarray) -> float:
    sp = np.sqrt(((len(a) - 1) * a.var(ddof=1) + (len(b) - 1) * b.var(ddof=1)) / (len(a) + len(b) - 2))
    return float(2.8 * sp * np.sqrt(1 / len(a) + 1 / len(b)) / b.mean() * 100)


# ------------------------------------------------------------------ SPEC side
def spec_cell_runtimes(d: Path) -> dict[str, float]:
    """Per-benchmark base run time (s) from the runcpu CSV in a cell."""
    out = {}
    for f in glob.glob(str(d / "CPU2026.*.csv")):
        txt = Path(f).read_text(errors="replace")
        m = re.search(r'"?Full Results Table"?\n(.*?)\n\n', txt, re.S)
        block = m.group(1) if m else txt
        try:
            t = pd.read_csv(io.StringIO(block))
        except Exception:  # noqa: BLE001
            continue
        cols = {c.lower(): c for c in t.columns}
        bcol = cols.get("benchmark")
        rcol = next((cols[c] for c in cols if "base run time" in c), None)
        if not bcol or not rcol:
            continue
        for _, r in t.iterrows():
            try:
                v = float(r[rcol])
            except (TypeError, ValueError):
                continue
            if v > 0 and isinstance(r[bcol], str) and r[bcol][:3].isdigit():
                out[r[bcol]] = v
    return out


def spec_valid(d: Path) -> bool:
    log = (d / "spec_pod.log").read_text() if (d / "spec_pod.log").exists() else ""
    return "rc=0" in log


# ------------------------------------------------------------------ telemetry
def telemetry_summary(d: Path, win=None) -> dict:
    f = d / "telemetry.csv"
    if not f.exists():
        return {}
    t = pd.read_csv(f)
    if win:
        t = t[(t.t >= win[0]) & (t.t < win[1])]
    busy = t[[c for c in t.columns if c.startswith("busy")]]
    steal = t[[c for c in t.columns if c.startswith("steal")]]
    return {
        "cpu_busy_pct": float(busy.mean(axis=1).mean()),
        "steal_pct_max": float(steal.max().max()),
        "mem_used_gib": float(t.mem_used_gib.mean()),
        "gpu_util": float(pd.to_numeric(t.gpu_util, errors="coerce").mean()),
        "gpu_power_w": float(pd.to_numeric(t.gpu_power_w, errors="coerce").mean()),
    }


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    rows, spec_rows = [], []
    for d in sorted(CELLS.iterdir()):
        m = cell_meta(d)
        if not m:
            continue
        r = {"cell": m["cell"], "kind": m["kind"], "level": m["level"], "rep": m["rep"]}
        if m["kind"] in ("vllm_solo", "colo"):
            v = vllm_cell_metrics(d)
            if v:
                win = v.pop("window")
                r.update(v)
                r.update(telemetry_summary(d, win))
        else:
            r.update(telemetry_summary(d))
        if m["kind"] in ("colo", "spec_solo"):
            r["spec_valid"] = spec_valid(d)
            for b, s in spec_cell_runtimes(d).items():
                spec_rows.append({"cell": m["cell"], "kind": m["kind"], "level": m["level"], "rep": m["rep"],
                                  "benchmark": b, "runtime_s": s, "valid": r["spec_valid"]})
        rows.append(r)
    cells = pd.DataFrame(rows)
    cells.to_csv(OUT / "cells.csv", index=False)
    spec = pd.DataFrame(spec_rows)
    spec.to_csv(OUT / "spec_runtimes.csv", index=False)

    # validity
    v = cells[cells.kind.isin(["vllm_solo", "colo"])].copy()
    v["valid"] = (v.n_failed == 0) & (v.sched_lag_p99_ms < 50)
    if "spec_valid" in v:
        v.loc[v.kind == "colo", "valid"] &= v.loc[v.kind == "colo", "spec_valid"].fillna(False).astype(bool)
    v[["cell", "valid", "n_failed", "sched_lag_p99_ms"]].to_csv(OUT / "validity_vllm.csv", index=False)
    v = v[v.valid]
    solo = v[v.kind == "vllm_solo"]

    # vLLM impact table
    imp = []
    for L in CFG["levels"]:
        c = v[(v.kind == "colo") & (v.level == L)]
        if len(c) < 2 or len(solo) < 2:
            continue
        for met in GUARDED + ["itl_p99", "achieved_rps"]:
            a, b = c[met].to_numpy(float), solo[met].to_numpy(float)
            est, lo, hi = boot_ci(a, b)
            imp.append({"level": L, "metric": met, "solo_mean": b.mean(), "colo_mean": a.mean(),
                        "delta_pct": est, "ci_lo": lo, "ci_hi": hi, "mde_pct": mde_pct(a, b),
                        "n_solo": len(b), "n_colo": len(a),
                        "ci_includes_0": lo <= 0 <= hi,
                        "degraded": (lo > 0) if LOWER_IS_BETTER[met] else (hi < 0)})
    imp = pd.DataFrame(imp)
    imp.to_csv(OUT / "vllm_impact.csv", index=False)

    # SPEC impact
    simp = []
    if len(spec):
        sv = spec[spec.valid]
        for L in CFG["levels"]:
            for bname, g in sv[sv.level == L].groupby("benchmark"):
                a = g[g.kind == "colo"].runtime_s.to_numpy(float)
                b = g[g.kind == "spec_solo"].runtime_s.to_numpy(float)
                if len(a) < 2 or len(b) < 2:
                    continue
                est, lo, hi = boot_ci(a, b)
                simp.append({"level": L, "benchmark": bname, "solo_s": b.mean(), "colo_s": a.mean(),
                             "delta_pct": est, "ci_lo": lo, "ci_hi": hi, "n_solo": len(b), "n_colo": len(a)})
    simp = pd.DataFrame(simp)
    simp.to_csv(OUT / "spec_impact.csv", index=False)

    # headline
    passing = []
    for L in CFG["levels"]:
        s = imp[(imp.level == L) & (imp.metric.isin(GUARDED))] if len(imp) else imp
        if len(s) == len(GUARDED):
            passing.append({"level": L, "pass_strict_ci0": bool(s.ci_includes_0.all()),
                            "pass_no_degradation": bool((~s.degraded).all()),
                            "worst_upper_ci_pct": float(np.where(s.metric == "output_tok_s", -s.ci_lo, s.ci_hi).max())})
    passing = pd.DataFrame(passing)
    passing.to_csv(OUT / "level_pass.csv", index=False)
    print(passing.to_string() if len(passing) else "no levels yet")
    print(imp.round(3).to_string() if len(imp) else "")
    print(simp.round(3).to_string() if len(simp) else "")


if __name__ == "__main__":
    main()
