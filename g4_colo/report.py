#!/usr/bin/env python3
"""Builds the results section of g4-colo-readme.md from results/analysis/*.csv.

Everything numeric in the README results section is generated here from the raw
cell data, so re-running analyze.py + plots.py + report.py reproduces it.
"""
import json
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent
A = ROOT / "results" / "analysis"
CFG = json.loads((ROOT / "experiment.json").read_text())
LN = {int(k): v.split(" (")[0] for k, v in CFG["level_names"].items()}  # short labels: "50%", "95%"
NICE = {"ttft_p50": "TTFT p50", "ttft_p99": "TTFT p99", "itl_p50": "ITL p50", "itl_p99": "ITL p99",
        "e2e_p50": "E2E latency p50", "e2e_p99": "E2E latency p99", "output_tok_s": "Output tokens/s",
        "achieved_rps": "Requests/s"}
UNIT = {"output_tok_s": ("", 1, "{:,.0f}"), "achieved_rps": ("", 1, "{:,.1f}")}


def fmt(m, v):
    if m in UNIT:
        return UNIT[m][2].format(v)
    return f"{v * 1e3:.1f} ms"


def main():
    imp = pd.read_csv(A / "vllm_impact.csv")
    lp = pd.read_csv(A / "level_pass.csv")
    sp = pd.read_csv(A / "spec_impact.csv")
    cells = pd.read_csv(A / "cells.csv")
    out = []

    levels = sorted(imp.level.unique())
    out.append("### GPU workload: co-located vs solo\n")
    hdr = "| Metric | Solo | " + " | ".join(f"Colo {LN[L]}" for L in levels) + " |"
    out.append(hdr)
    out.append("|" + "---|" * (2 + len(levels)))
    for m in ["ttft_p50", "ttft_p99", "itl_p50", "e2e_p50", "e2e_p99", "output_tok_s"]:
        s = imp[imp.metric == m]
        row = f"| {NICE[m]} | {fmt(m, s.solo_mean.iloc[0])} |"
        for L in levels:
            r = s[s.level == L].iloc[0]
            row += f" {fmt(m, r.colo_mean)} ({r.delta_pct:+.1f}% [{r.ci_lo:+.1f}, {r.ci_hi:+.1f}]) |"
        out.append(row)
    nf = cells[cells.kind.isin(["vllm_solo", "colo"])].groupby("kind").n_failed.sum()
    n_s = int(imp.n_solo.iloc[0]); n_c = int(imp.n_colo.min())
    out.append(f"\nMean of {n_s} solo / {n_c} colo reps; brackets = 95% bootstrap CI of the change vs solo. Failed requests: "
               f"solo {int(nf.get('vllm_solo', 0))}, colo {int(nf.get('colo', 0))}.\n")

    out.append("### Verdict per colo level\n")
    out.append("| Colo level | Spare vCPU used | Pass (≤ +10% latency, ≥ −2% throughput, 0 failures) | Strict (CI includes 0) |")
    out.append("|---|---|---|---|")
    for _, r in lp.iterrows():
        out.append(f"| {LN[int(r.level)]} | {int(r.level)} of 40 | {'✅ PASS' if r.pass_tolerance else '❌ FAIL'} | "
                   f"{'yes' if r.pass_strict_ci0 else 'no'} |")
    best = lp[lp.pass_tolerance]
    head = int(best.level.max()) if len(best) else None
    out.append("")
    if head:
        out.append(f"**Headline colo ratio: {head}/40 = {head / 40:.0%} of the spare vCPU.**\n")

    out.append("### CPU workload: SPEC CPU 2026 rate runtime, co-located vs solo\n")
    out.append("| Benchmark | " + " | ".join(f"{LN[L]}: solo s → colo s (Δ, 95% CI)" for L in sorted(sp.level.unique())) + " |")
    out.append("|" + "---|" * (1 + sp.level.nunique()))
    for b in sorted(sp.benchmark.unique()):
        row = f"| {b} |"
        for L in sorted(sp.level.unique()):
            r = sp[(sp.benchmark == b) & (sp.level == L)]
            if len(r):
                r = r.iloc[0]
                row += f" {r.solo_s:.1f} → {r.colo_s:.1f} ({r.delta_pct:+.2f}% [{r.ci_lo:+.2f}, {r.ci_hi:+.2f}]) |"
            else:
                row += " n/a |"
        out.append(row)
    out.append("\nEstimated SPEC CPU 2026 results (train size, not SPEC-reviewed). Positive Δ = slower when co-located.\n")

    u = cells[cells.kind.isin(["vllm_solo", "colo"])].copy()
    u["lvl"] = u.apply(lambda r: "solo" if r.kind == "vllm_solo" else LN[int(r.level)], axis=1)
    g = u.groupby("lvl")[["cpu_busy_pct", "mem_used_gib", "gpu_util", "steal_pct_max"]].mean()
    out.append("### What the node looks like\n")
    out.append("| Scenario | Node CPU busy (48 vCPU) | Memory used | GPU util | Max CPU steal |")
    out.append("|---|---|---|---|---|")
    for k in ["solo"] + [LN[L] for L in levels]:
        if k in g.index:
            r = g.loc[k]
            out.append(f"| {k} | {r.cpu_busy_pct:.1f}% | {r.mem_used_gib:.1f} GiB | {r.gpu_util:.0f}% | {r.steal_pct_max:.2f}% |")
    (A / "report_section.md").write_text("\n".join(out) + "\n")
    print("\n".join(out))


if __name__ == "__main__":
    main()


def render_readme():
    """Insert the generated results section into README.template.md -> ../g4-colo-readme.md."""
    sec = (A / "report_section.md").read_text()
    tpl = (ROOT / "README.template.md").read_text()
    (ROOT.parent / "g4-colo-readme.md").write_text(tpl.replace("RESULTS_SECTION", sec))


render_readme()
