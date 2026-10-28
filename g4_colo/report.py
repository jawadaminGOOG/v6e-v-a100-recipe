#!/usr/bin/env python3
"""Builds the results section of g4-colo-readme.md from results/analysis/*.csv and renders the README.

Every number in the results section is generated here from the per-run data, so
analyze.py + phases.py + quantiles.py + plots.py + report.py reproduce it.
"""
import json
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent
A = ROOT / "results" / "analysis"
CFG = json.loads((ROOT / "experiment.json").read_text())
LVL = {20: "50% (20 vCPU)", 38: "95% (38 vCPU)"}
NICE = {"ttft_p50": "TTFT p50", "ttft_p99": "TTFT p99", "itl_p50": "Inter-token latency p50",
        "e2e_p50": "End-to-end p50", "e2e_p99": "End-to-end p99", "output_tok_s": "Output tokens/s"}
CLASS = {"compute": "Compute-type co-located work (14 integer SPEC CPU 2026 benchmarks)",
         "bandwidth": "Memory-bandwidth-heavy co-located work (749.fotonik3d_r, 782.lbm_r)"}


def val(m, v):
    return f"{v:,.0f}" if m == "output_tok_s" else f"{v * 1e3:.1f} ms"


def main():
    imp = pd.read_csv(A / "vllm_phase_impact.csv")
    ph = pd.read_csv(A / "phase_summary.csv")
    sp = pd.read_csv(A / "spec_impact.csv")
    cells = pd.read_csv(A / "cells.csv")
    out = []
    for phase in ["compute", "bandwidth"]:
        secs = ph[ph.phase == phase].phase_seconds.mean()
        out.append(f"### GPU workload alongside {CLASS[phase][0].lower() + CLASS[phase][1:]}\n")
        out.append(f"About {secs / 60:.0f} minutes of each run. Mean of 5 runs; brackets are the 95% CI of the change vs solo.\n")
        out.append("| Metric | Solo | " + " | ".join(f"Colo {LVL[L]}" for L in (20, 38)) + " |")
        out.append("|---|---|---|---|")
        for m in ["ttft_p50", "ttft_p99", "itl_p50", "e2e_p50", "e2e_p99", "output_tok_s"]:
            s = imp[(imp.phase == phase) & (imp.metric == m)]
            row = f"| {NICE[m]} | {val(m, s[s.level == 38].solo_mean.iloc[0])} |"
            for L in (20, 38):
                r = s[s.level == L].iloc[0]
                lo, hi = min(r.boot_lo, r.welch_lo), max(r.boot_hi, r.welch_hi)
                row += f" {val(m, r.colo_mean)} ({r.delta_pct:+.1f}% [{lo:+.1f}, {hi:+.1f}]) |"
            out.append(row)
        f = ph[ph.phase == phase].colo_failed_requests.sum()
        out.append(f"\nFailed requests while co-located: {int(f)}.\n")

    out.append("### CPU workload: SPEC CPU 2026 rate runtime, co-located vs solo\n")
    out.append("| Benchmark | 50%: solo → colo (Δ, 95% CI) | 95%: solo → colo (Δ, 95% CI) |")
    out.append("|---|---|---|")
    for b in sorted(sp.benchmark.unique()):
        row = f"| {b} |"
        for L in (20, 38):
            r = sp[(sp.benchmark == b) & (sp.level == L)].iloc[0]
            row += f" {r.solo_s:.1f} s → {r.colo_s:.1f} s ({r.delta_pct:+.2f}% [{r.ci_lo:+.2f}, {r.ci_hi:+.2f}]) |"
        out.append(row)
    out.append("\nEstimated results (train size, not SPEC-reviewed). Positive Δ = slower when co-located with the GPU workload.\n")

    u = cells[cells.kind.isin(["vllm_solo", "colo"])].copy()
    u["s"] = u.apply(lambda r: "GPU workload only" if r.kind == "vllm_solo" else f"Co-located {LVL[int(r.level)]}", axis=1)
    g = u.groupby("s")[["cpu_busy_pct", "mem_used_gib", "gpu_util", "steal_pct_max"]].mean()
    out.append("### What the node looks like\n")
    out.append("| Scenario | Node CPU busy (48 vCPU) | Memory used | GPU utilisation | Max CPU steal |")
    out.append("|---|---|---|---|---|")
    for k in ["GPU workload only", f"Co-located {LVL[20]}", f"Co-located {LVL[38]}"]:
        r = g.loc[k]
        out.append(f"| {k} | {r.cpu_busy_pct:.1f}% | {r.mem_used_gib:.1f} GiB | {r.gpu_util:.0f}% | {r.steal_pct_max:.2f}% |")
    out.append("\nMeasured at 1 s over the first 10 minutes of load (compute-type phase).\n")
    sec = "\n".join(out) + "\n"
    (A / "report_section.md").write_text(sec)
    tpl = (ROOT / "README.template.md").read_text()
    (ROOT.parent / "g4-colo-readme.md").write_text(tpl.replace("RESULTS_SECTION", sec))
    print(sec)


if __name__ == "__main__":
    main()
