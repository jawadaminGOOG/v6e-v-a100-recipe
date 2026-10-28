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
    san = sanity_section()
    (A / "report_section.md").write_text(sec)
    tpl = (ROOT / "README.template.md").read_text()
    (ROOT.parent / "g4-colo-readme.md").write_text(tpl.replace("RESULTS_SECTION", sec).replace("SANITY_SECTION", san))
    print(sec)



def sanity_section():
    S = ROOT / "results" / "sanity"
    o = []
    c = pd.read_csv(S / "capacity_summary.csv")
    o.append("### Maximum throughput while co-located\n")
    o.append("Saturating load (500 requests in flight) for the whole SPEC run; completed requests/s, mean of 2 runs, "
             "change vs solo with 95% CI.\n")
    o.append("| SPEC running alongside | Solo | Colo 50% (20 vCPU) | Colo 95% (38 vCPU) |")
    o.append("|---|---|---|---|")
    for ph, nm in [("integer", "Compute-type (14 integer benchmarks)"), ("fotonik3d+lbm", "Memory-bandwidth-heavy (fotonik3d, lbm)")]:
        a, b = c[(c.phase == ph) & (c.level == 20)].iloc[0], c[(c.phase == ph) & (c.level == 38)].iloc[0]
        o.append(f"| {nm} | {b.solo_rps:.0f} req/s | {a.colo_rps:.0f} ({a.delta_pct:+.1f}% [{a.lo:+.1f}, {a.hi:+.1f}]) | "
                 f"{b.colo_rps:.0f} ({b.delta_pct:+.1f}% [{b.lo:+.1f}, {b.hi:+.1f}]) |")
    runs = pd.read_csv(S / "capacity_runs.csv")
    o.append(f"\nFailed requests: {int(runs.groupby('cell').failed.first().sum())}.\n")
    g = pd.read_csv(S / "golden_capacity.csv")
    gl = pd.read_csv(S / "golden_latency.csv").set_index("metric")
    o.append("### GPU workload with all 48 vCPU (no co-location)\n")
    o.append("vLLM with no CPU or memory limit, so it can use every core on the node, compared with the 8 vCPU / 64 GiB slice.\n")
    o.append("| | 8 vCPU / 64 GiB | All 48 vCPU |")
    o.append("|---|---|---|")
    for conc in sorted(g.concurrency.unique()):
        r8 = g[(g.concurrency == conc) & g.label.str.startswith("g8")].rps.iloc[0]
        r48 = g[(g.concurrency == conc) & g.label.str.startswith("g48")].rps.iloc[0]
        o.append(f"| Capacity at {conc} requests in flight (1 short probe each) | {r8:.0f} req/s | {r48:.0f} req/s |")
    for m in ["ttft_p50", "ttft_p99", "itl_p50", "e2e_p50", "e2e_p99"]:
        r = gl.loc[m]
        o.append(f"| {NICE[m]} at 625 req/s | {r.cap8_mean * 1e3:.1f} ms ({r.cap8_vs_all48_pct:+.1f}% [{r.lo:+.1f}, {r.hi:+.1f}]) | {r.all48_mean * 1e3:.1f} ms |")
    r = gl.loc["output_tok_s"]
    o.append(f"| Output tokens/s at 625 req/s | {r.cap8_mean:,.0f} | {r.all48_mean:,.0f} |")
    o.append(f"\nFixed-rate rows: {int(r.n_8)} runs at 8 vCPU (the solo runs of the main matrix) vs {int(r.n_48)} runs with all 48 vCPU "
             "(run on a later day with a new pod, not interleaved); percentage = 8 vCPU vs all 48 vCPU; the n = 2 intervals are rough.\n")
    return "\n".join(o) + "\n"

if __name__ == "__main__":
    main()
