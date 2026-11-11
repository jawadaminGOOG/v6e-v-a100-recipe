#!/usr/bin/env python3
"""Charts and data bundle for the deck "Maximizing CPU Utilization on GCP GPU and TPU machines with GKE".

Every chart is re-rendered from raw data in the deck palette (palette.md), as SVG for the HTML deck and PNG for the
Google Slides export. G4 and TPU v6e charts come from the same functions, so axes, units and colours match.

    g4_colo/.venv/bin/python g4_colo/deck/deck_charts.py            # writes charts/*.svg|png and js/deck_data.js

Inputs (all optional; a missing input becomes a clearly marked placeholder, never invented data):
  G4 experiment     g4_colo/results/analysis/*.csv, g4_colo/results/cells/<cell>/telemetry.csv
  Native G4 sweep   .agents/bench/sweep1_merged/native.{samples.jsonl,marks.json}
  Native v6e sweep  .agents/bench/v6e_sweep1/<name>.{samples.jsonl,marks.json[,cpus.jsonl]}
  Replays           data/replays.json (trimmed scenario runs of the demo web app's engine, DEMO_MOCK=1); rebuilt from
                    DECK_MOCK_REPLAYS (raw /api/state jsonl) when that dir is set or data/replays.json is missing
  v6e charts        placeholders until DECK_V6E_FINAL=1 (sweep incl. the scale-out/in rerun is final)
  Pricing           $DECK_PRICING_DIR/pricing.json (+ g4_vs_aws.json, guidance.json)
"""
from __future__ import annotations

import csv
import glob
import io
import json
import math
import os
import re
import sys
from pathlib import Path

import logging

import matplotlib

matplotlib.use("Agg")
logging.getLogger("matplotlib.font_manager").setLevel(logging.ERROR)
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib import font_manager, ticker  # noqa: E402,F401
from matplotlib.colors import LinearSegmentedColormap, to_rgba  # noqa: E402

DECK = Path(__file__).resolve().parent
G4C = DECK.parent
REPO = G4C.parent
RES = G4C / "results"
BENCH = REPO / ".agents" / "bench"
OUT = DECK / "charts"
PRICING = Path(os.environ.get(
    "DECK_PRICING_DIR",
    "/usr/local/google/home/jawadamin/.gemini/jetski/brain/08874b37-6b8d-4f24-8961-0c853beadc43/scratch/deck/pricing"))
MOCK_REPLAYS = Path(os.environ.get(
    "DECK_MOCK_REPLAYS",
    "/usr/local/google/home/jawadamin/.gemini/jetski/brain/f45e12de-4596-4e31-9b64-1c0587e62170/scratch/mock2"))

# ---------------------------------------------------------------- palette (palette.md; nothing else is allowed)
PAL = {
    "blue": "#5A82B3",    # Olympic Blue
    "red": "#E2625E",     # Eugenia Red (A)
    "yellow": "#E2B540",  # Yellow Ocher
    "green": "#489B6E",   # Green
    "ink": "#111314",     # Black
    "warm": "#A1A39A",    # Warm Gray
    "ground": "#FFFDF7",  # ivory ground (spec)
}


def a(c, alpha):
    """An opacity mix of a palette colour (allowed by palette.md rule 1)."""
    return to_rgba(PAL[c], alpha)


INK2 = a("ink", 0.72)   # secondary text
GRID = a("warm", 0.35)
HEAT = LinearSegmentedColormap.from_list("deck_heat", [PAL["ground"], PAL["blue"], PAL["ink"]])

for f in ("Figtree-wght.ttf", "Caprasimo-Regular.ttf"):
    font_manager.fontManager.addfont(str(DECK / "fonts" / f))
plt.rcParams.update({
    "font.family": "Figtree", "font.size": 16, "svg.fonttype": "none",
    "axes.edgecolor": a("ink", 0.55), "axes.labelcolor": PAL["ink"], "axes.facecolor": "none",
    "figure.facecolor": "none", "savefig.facecolor": "none", "text.color": PAL["ink"],
    "xtick.color": INK2, "ytick.color": INK2, "xtick.labelcolor": INK2, "ytick.labelcolor": INK2,
    "axes.spines.top": False, "axes.spines.right": False, "axes.grid": True, "grid.color": GRID,
    "grid.linewidth": 0.8, "axes.axisbelow": True, "axes.labelsize": 17, "xtick.labelsize": 16,
    "ytick.labelsize": 16, "legend.frameon": False, "lines.solid_capstyle": "round",
})

SVGS: dict[str, str] = {}
PLACEHOLDERS: list[str] = []


def fig_px(w, h):
    return plt.figure(figsize=(w / 72, h / 72), dpi=72)


def save(fig, name, w, h):
    """SVG (text kept as text, sized by its viewBox) for the deck + 2x PNG for Slides."""
    OUT.mkdir(parents=True, exist_ok=True)
    buf = io.StringIO()
    fig.savefig(buf, format="svg", transparent=True)
    svg = buf.getvalue()
    svg = svg[svg.index("<svg"):]
    svg = re.sub(r'<svg([^>]*?) width="[^"]+" height="[^"]+"', r'<svg\1', svg, count=1)
    svg = re.sub(r"<metadata>.*?</metadata>", "", svg, flags=re.S)
    svg = svg.replace("font-family:'Figtree'", "font-family:'Figtree',sans-serif").replace("#000000", PAL["ink"])  # invisible marker edges
    (OUT / f"{name}.svg").write_text(svg)
    fig.savefig(OUT / f"{name}.png", dpi=144, facecolor=PAL["ground"])
    plt.close(fig)
    SVGS[name] = svg
    print("wrote", OUT / f"{name}.svg")


def placeholder_chart(name, w, h, label):
    fig = fig_px(w, h)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.axis("off")
    ax.add_patch(plt.Rectangle((0.01, 0.02), 0.98, 0.96, transform=ax.transAxes, fill=False,
                               ec=PAL["warm"], lw=2, ls=(0, (6, 5))))
    if h < 160:
        ax.text(0.5, 0.5, f"PLACEHOLDER · {label}", ha="center", va="center", fontsize=16, color=INK2,
                transform=ax.transAxes)
        save(fig, name, w, h)
        PLACEHOLDERS.append(f"{name}: {label}")
        return
    ax.text(0.5, 0.56, "PLACEHOLDER", ha="center", va="center", fontsize=24, fontweight="bold",
            color=PAL["warm"], transform=ax.transAxes)
    ax.text(0.5, 0.42, label, ha="center", va="center", fontsize=17, color=INK2, transform=ax.transAxes, wrap=True)
    save(fig, name, w, h)
    PLACEHOLDERS.append(f"{name}: {label}")


def read_csv(p):
    with open(p) as f:
        return list(csv.DictReader(f))


# ================================================================ A. G4 controlled experiment
L3_G4 = [list(range(0, 8)), list(range(8, 16)), list(range(16, 24))]   # physical cores per L3 (EPYC 9B45)
L3_V6E = [list(range(0, 8)), list(range(8, 15)), list(range(15, 22))]  # EPYC 9B14, 8/7/7


def cpu_rows(l3, ncores):
    """Row order for a per-CPU heatmap: L3 domain, then core, then SMT thread (sibling = cpu + ncores)."""
    rows = []
    for dom in l3:
        for c in dom:
            rows += [c, c + ncores]
    return rows


def heatmap_panel(ax, busy_by_cpu, times, l3, ncores, vllm, shared, label, bf=frozenset()):
    rows = cpu_rows(l3, ncores)
    m = [busy_by_cpu[c] for c in rows]
    ax.imshow(m, aspect="auto", cmap=HEAT, vmin=0, vmax=100, interpolation="nearest",
              extent=(times[0], times[-1], len(rows), 0))
    ax.grid(False)
    y = 0
    for i, dom in enumerate(l3):
        n = 2 * len(dom)
        if i:
            ax.axhline(y, color=PAL["ground"], lw=2.2)
        ax.text(-0.012, 1 - (y + n / 2) / len(rows), f"L3 {i}", transform=ax.transAxes, ha="right", va="center",
                fontsize=16, color=INK2, rotation=0)
        y += n
    for r, c in enumerate(rows):
        col = PAL["blue"] if c in vllm else (PAL["warm"] if c in shared else (PAL["green"] if c in bf else None))
        if col:
            ax.add_patch(plt.Rectangle((1.004, 1 - (r + 1) / len(rows)), 0.012, 1 / len(rows), transform=ax.transAxes,
                                       color=col, clip_on=False, lw=0))
    ax.set_yticks([])
    ax.spines[["left", "bottom"]].set_visible(False)
    ax.text(0.0, 1.03, label, transform=ax.transAxes, fontsize=18, fontweight="bold", va="bottom")


def load_telemetry(cell):
    f = RES / "cells" / cell / "telemetry.csv"
    rows = read_csv(f)
    t0 = float(rows[0]["t"])
    times = [(float(r["t"]) - t0) / 60 for r in rows]
    busy = {c: [float(r[f"busy{c}"] or 0) for r in rows] for c in range(48)}
    return times, busy


def exp_heatmap():
    w, h = 900, 540
    fig = fig_px(w, h)
    vllm, shared = {1, 2, 3, 4, 25, 26, 27, 28}, {0, 24}
    for k, (cell, lab) in enumerate((("vllm_solo-r1", "vLLM alone · node 5 % busy"),
                                     ("colo-L38-r1", "vLLM + 38 vCPU of SPEC CPU neighbours · node 80 % busy"))):
        ax = fig.add_axes([0.08, 0.565 - k * 0.475, 0.88, 0.355])
        times, busy = load_telemetry(cell)
        heatmap_panel(ax, busy, times, L3_G4, 24, vllm, shared, lab)
        ax.set_xlim(times[0], times[-1])
        if k:
            ax.set_xlabel("minutes into the run")
        else:
            ax.set_xticklabels([])
    save(fig, "exp_heatmap", w, h)


def exp_delta():
    w, h = 724, 290
    imp = read_csv(RES / "analysis" / "vllm_phase_impact_split.csv")
    mets = [("ttft_p50", "TTFT p50"), ("ttft_p99", "TTFT p99"), ("itl_p50", "ITL p50"), ("e2e_p99", "E2E p99"),
            ("output_tok_s", "tokens/s")]
    series = [("compute", "green", "integer"), ("lbm", "yellow", "lbm"), ("fotonik3d", "red", "fotonik3d")]
    fig = fig_px(w, h)
    ax = fig.add_axes([0.16, 0.17, 0.82, 0.7])
    ax.axvspan(-10, 10, color=a("yellow", 0.16), lw=0)
    ax.axvline(0, color=a("ink", 0.6), lw=1)
    for j, (ph, col, _) in enumerate(series):
        for lvl, alpha, dy in (("20", 0.45, -0.13), ("38", 1.0, 0.13)):
            for i, (m, _) in enumerate(mets):
                r = next(x for x in imp if x["level"] == lvl and x["phase"] == ph and x["metric"] == m)
                d = float(r["delta_pct"])
                lo = min(float(r["boot_lo"]), float(r["welch_lo"]))
                hi = max(float(r["boot_hi"]), float(r["welch_hi"]))
                y = len(mets) - 1 - i + (j - 1) * 0.26 + dy * 0.5
                ax.plot([lo, hi], [y, y], color=a(col, alpha), lw=2)
                ax.plot([d], [y], "o", ms=6, color=a(col, alpha), mec="none")
    ax.set_yticks(range(len(mets)), [n for _, n in reversed(mets)])
    ax.set_xlim(-12, 135)
    ax.set_xlabel("change vs vLLM alone (%)")
    ax.grid(axis="y", visible=False)
    ax.text(11, -0.45, "±10 % pass bar", fontsize=15.5, color=INK2, va="bottom")
    for k, (_, col, nm) in enumerate(series):
        ax.plot([0.42 + k * 0.17], [1.08], "o", transform=ax.transAxes, color=PAL[col], clip_on=False, ms=9)
        ax.text(0.44 + k * 0.17, 1.08, nm, transform=ax.transAxes, fontsize=16, va="center")
    ax.text(-0.19, 1.08, "solid 95 % · faint 50 %", transform=ax.transAxes, fontsize=15.5, color=INK2, va="center")
    save(fig, "exp_delta", w, h)


def exp_cdf():
    w, h = 724, 250
    q = read_csv(RES / "analysis" / "latency_quantiles.csv")
    fig = fig_px(w, h)
    for k, (ph, col, nm) in enumerate((("compute", "green", "integer neighbours"),
                                       ("bandwidth", "red", "memory-bandwidth neighbours"))):
        ax = fig.add_axes([0.11 + k * 0.47, 0.22, 0.4, 0.64])
        for scen, lvl, c, lw in (("solo", "38", PAL["ink"], 2.2), ("colo 50%", "20", a(col, 0.5), 2.2),
                                 ("colo 95%", "38", PAL[col], 2.6)):
            pts = sorted((float(r["q"]), float(r["value_ms"])) for r in q if r["phase"] == ph and r["level"] == lvl
                         and r["scenario"] == scen and r["metric"] == "ttft" and float(r["q"]) >= 0.9)
            ax.plot([v for _, v in pts], [qq * 100 for qq, _ in pts], color=c, lw=lw)
        ax.set_ylim(90, 100)
        ax.set_xlim(left=30)
        ax.set_title(nm, fontsize=16, loc="left", color=PAL["ink"])
        ax.set_xlabel("TTFT (ms)")
        if k == 0:
            ax.set_ylabel("% of requests ≤ x")
        else:
            ax.set_yticklabels([])
    save(fig, "exp_cdf", w, h)


def exp_node_util():
    w, h = 400, 230
    c = read_csv(RES / "analysis" / "cells.csv")
    vals = []
    for kind, lvl in (("vllm_solo", "0"), ("colo", "20"), ("colo", "38")):
        xs = [float(r["cpu_busy_pct"]) for r in c if r["kind"] == kind and r["level"] == lvl]
        vals.append(sum(xs) / len(xs))
    fig = fig_px(w, h)
    ax = fig.add_axes([0.25, 0.12, 0.68, 0.8])
    labels = ["vLLM alone", "50 % colo", "95 % colo"]
    cols = [PAL["warm"], a("green", 0.5), PAL["green"]]
    ax.barh(range(3)[::-1], vals, color=cols, height=0.62)
    for i, v in enumerate(vals):
        ax.text(v + 1.5, 2 - i, f"{v:.0f} %", va="center", fontsize=18, fontweight="bold")
    ax.set_yticks(range(3)[::-1], labels)
    ax.set_xlim(0, 100)
    ax.set_xticks([0, 25, 50, 75, 100], ["0", "25", "50", "75", "100 %"])
    ax.grid(axis="y", visible=False)
    save(fig, "exp_node_util", w, h)
    return [round(v, 1) for v in vals]


def exp_spec_forest():
    w, h = 400, 330
    s = [r for r in read_csv(RES / "analysis" / "spec_impact.csv") if r["level"] == "38"]
    s.sort(key=lambda r: float(r["delta_pct"]))
    fig = fig_px(w, h)
    ax = fig.add_axes([0.36, 0.13, 0.6, 0.84])
    ax.axvline(0, color=a("ink", 0.6), lw=1)
    for i, r in enumerate(s):
        mem = any(k in r["benchmark"] for k in ("fotonik", "lbm"))
        col = PAL["red"] if mem else PAL["green"]
        d, lo, hi = float(r["delta_pct"]), float(r["ci_lo"]), float(r["ci_hi"])
        ax.plot([lo, hi], [i, i], color=col, lw=2)
        ax.plot([d], [i], "o", color=col, ms=5)
    ax.set_yticks(range(len(s)), [r["benchmark"] for r in s], fontsize=15)
    ax.set_xlabel("SPEC runtime vs alone (%)", fontsize=16)
    ax.grid(axis="y", visible=False)
    save(fig, "exp_spec_forest", w, h)


# ================================================================ B/C. Native recipe sweeps (G4 and v6e, same code)
def load_sweep(d: Path, name: str | None = None):
    if not d.exists():
        return None
    cands = sorted(d.glob("*.samples.jsonl"))
    if name:
        cands = [p for p in cands if p.name.startswith(name + ".")]
    if not cands:
        return None
    base = cands[0].name.split(".")[0]
    samples = [json.loads(line) for line in open(d / f"{base}.samples.jsonl") if line.strip()]
    marks = json.load(open(d / f"{base}.marks.json")) if (d / f"{base}.marks.json").exists() else []
    cpus = None
    if (d / f"{base}.cpus.jsonl").exists():
        cpus = [json.loads(line) for line in open(d / f"{base}.cpus.jsonl") if line.strip()]
    return {"dir": d, "name": base, "samples": samples, "marks": marks, "cpus": cpus}


def marks_of(S, phase):
    return [m for m in S["marks"] if m["phase"] == phase]


def tot(s, key):
    return sum(s["svc"][k][key] for k in ("sandbox", "toolcall"))


def native_latency(S, key, title_alone):
    w, h = 1040, 290
    fig = fig_px(w, h)
    bf = marks_of(S, "backfill")
    ax = fig.add_axes([0.07, 0.18, 0.4, 0.68])
    x, y = [m["pods"] for m in bf], [m["ttft"]["p99"] for m in bf]
    solo = y[0]
    ax.axhspan(0, 1.5 * solo, color=a("yellow", 0.16), lw=0)
    ax.axhline(1.5 * solo, color=PAL["yellow"], lw=2, ls=(0, (5, 4)))
    ax.text(x[0], 1.5 * solo * 1.02, f"budget 1.5 × solo = {1.5 * solo:.0f} ms", fontsize=16, color=PAL["ink"],
            va="bottom")
    ax.plot(x, y, "-o", color=PAL["green"], lw=3, ms=7)
    ax.annotate(f"{y[-1]:.0f} ms", (x[-1], y[-1]), xytext=(8, -2), textcoords="offset points",
                fontsize=17, fontweight="bold", color=PAL["green"], va="center")
    ax.annotate(f"solo {solo:.0f} ms", (x[0], solo), xytext=(4, -12), textcoords="offset points", fontsize=16,
                color=INK2, ha="left", va="top")
    ax.set_ylim(0, max(1.5 * solo, max(y)) * 1.25)
    ax.set_xlim(min(x) - 1, max(x) + 4)
    ax.set_xticks(x)
    ax.set_xlabel("CPU pods next to vLLM (2 vCPU each)")
    ax.set_ylabel("TTFT p99 (ms)")
    ax.set_title(f"as the node fills, at {S['rate']} req/s", fontsize=17, loc="left")

    ld = list(marks_of(S, "load"))
    # the full-backfill point at the backfill phase's rate is the same condition; use it if the load ladder lacks it
    pmax = max([m["pods"] for m in ld] or [0])
    for m in bf:
        if m["pods"] == pmax and pmax and not any(x["pods"] == pmax and x["rate"] == m.get("rate", S["rate"]) for x in ld):
            ld.append({**m, "rate": m.get("rate", S["rate"])})
    ax2 = fig.add_axes([0.57, 0.18, 0.35, 0.68])
    pods = sorted({m["pods"] for m in ld})
    series = []
    for p, col, nm in ((pods[0], PAL["blue"], title_alone), (pods[-1], PAL["green"], f"+ {pods[-1]} CPU pods")):
        ms = sorted((m for m in ld if m["pods"] == p and (m.get("ttft") or {}).get("p99") is not None), key=lambda m: m["rate"])
        if ms:
            series.append((col, nm, [m["rate"] for m in ms], [m["ttft"]["p99"] for m in ms]))
    ok = [y for *_, ys in series for y in ys if y < 1000]  # >= 1000 ms = loadgen ceiling: the replica is saturated
    cap = max(ok) * 1.5 if ok else 1000
    sat_last = [ys[-1] >= 1000 for *_, ys in series]
    for k, (col, nm, xs, ys) in enumerate(series):
        last = f"{ys[-1]:.0f} ms" if ys[-1] < 1000 else ("" if all(sat_last) else f"≥ {ys[-1]:,.0f} ms, saturated")
        yc = [min(y, cap) for y in ys]
        ax2.plot(xs, yc, "-o", color=col, lw=3, ms=6)
        ax2.annotate(f"{nm}\n{last}".strip(), (xs[-1], yc[-1]), xytext=(8, 14 if k == 0 else -14) if all(sat_last) else (8, -22 if col == PAL["green"] else 0),
                     textcoords="offset points", ha="left", va="center", fontsize=16, fontweight="bold", color=col)
    if series and all(sat_last):
        x_ = series[0][2][-1]
        ax2.text(0.03, 0.97, f"at {x_:,.0f} req/s both saturate\n(≥ {series[0][3][-1]:,.0f} ms, axis capped)", transform=ax2.transAxes,
                 ha="left", va="top", fontsize=15, color=INK2)
    if any(y >= 1000 for *_, ys in series for y in ys):
        ax2.set_ylim(0, cap * 1.08)
    else:
        ax2.set_ylim(bottom=0)
    ax2.set_xlim(right=max(m["rate"] for m in ld) * 1.32)
    ax2.set_xlabel("offered load (requests/s)")
    ax2.set_title("as load rises", fontsize=17, loc="left")
    save(fig, f"{key}_latency", w, h)


def native_node_busy(S, key):
    w, h = 760 if not S.get("cpus") else 370, 200
    bf = marks_of(S, "backfill")
    fig = fig_px(w, h)
    ax = fig.add_axes([0.09 if w > 500 else 0.17, 0.22, 0.86 if w > 500 else 0.78, 0.68])
    x = [m["pods"] for m in bf]
    ax.plot(x, [m.get("all_busy") for m in bf], "-o", color=PAL["green"], lw=3, ms=6)
    ax.plot(x, [m.get("vllm_busy") for m in bf], "-o", color=PAL["blue"], lw=3, ms=6)
    ax.annotate(f"whole node {bf[-1].get('all_busy'):.0f} %", (x[-1], bf[-1]["all_busy"]), xytext=(-8, 8),
                textcoords="offset points", ha="right", va="bottom", fontsize=16, fontweight="bold",
                color=PAL["green"])
    ax.annotate(f"vLLM's 8 vCPU {bf[-1].get('vllm_busy'):.0f} %", (x[-1], bf[-1]["vllm_busy"]), xytext=(-8, -10),
                textcoords="offset points", ha="right", va="top", fontsize=16, fontweight="bold", color=PAL["blue"])
    ax.set_ylim(0, 100)
    ax.set_xticks(x)
    ax.set_yticks([0, 50, 100], ["0", "50", "100 %"])
    ax.set_xlabel("CPU pods next to vLLM")
    ax.set_ylabel("CPU busy")
    save(fig, f"{key}_nodebusy", w, h)


def placement(ax, S, t0, t1, demand_fn, events, ylim):
    w = [s for s in S["samples"] if t0 - 20 <= s["t"] <= t1]
    x = [s["t"] - t0 for s in w]
    ax.stackplot(x, [tot(s, "bf_ready") for s in w], [tot(s, "of_ready") for s in w],
                 colors=[a("green", 0.85), a("red", 0.85)], lw=0)
    ax.plot(x, [demand_fn(s["t"]) for s in w], color=PAL["ink"], lw=2, drawstyle="steps-post")
    xmax = x[-1] if x else 1
    for i, (tx, label) in enumerate(events):
        ax.axvline(tx, color=a("ink", 0.5), lw=1, ls=(0, (2, 3)))
        right = tx > 0.75 * xmax
        ax.text(tx + (-6 if right else 6), ylim * (0.97 - 0.11 * i), label, fontsize=15.5, va="top",
                ha="right" if right else "left", color=PAL["ink"])
    ax.set_ylim(0, ylim)
    ax.set_xlim(-20, x[-1])
    ax.set_ylabel("CPU pods Ready")


def native_spike(S, key):
    w, h = 510, 260
    fig = fig_px(w, h)
    ax = fig.add_axes([0.13, 0.2, 0.83, 0.72])
    up, dn, end = marks_of(S, "step_up")[0], marks_of(S, "step_down")[0], marks_of(S, "step_end")[0]
    tu, td, te = up["t"], dn["t"], end["t"]
    hi = sum(up.get("demand", [20, 20]))
    lo = sum(up.get("demand_lo", dn.get("demand", [9, 9])))
    placement(ax, S, tu, min(te, td + 200), lambda t: hi if tu <= t < td else lo,
              [(0, f"demand {lo} → {hi}"), (td - tu, f"{hi} → {lo}")], hi * 1.3)
    cov, dr = dn.get("covered_after_s"), end.get("drained_after_s")
    if cov:
        ax.annotate(f"all {hi} Ready\nafter {cov:.0f} s", (cov, hi), xytext=(cov + 12, hi * 0.45), fontsize=16,
                    arrowprops=dict(arrowstyle="-", color=PAL["ink"], lw=1))
    ax.set_xlabel("seconds since CPU demand rose")
    save(fig, f"{key}_spike", w, h)
    return cov, dr


def native_gpu(S, key):
    w, h = 510, 260
    fig = fig_px(w, h)
    ax = fig.add_axes([0.13, 0.2, 0.83, 0.72])
    out, gin, end = marks_of(S, "gpu_out")[0], marks_of(S, "gpu_in")[0], marks_of(S, "gpu_end")[0]
    t0, tin, te = out["t"], gin["t"], end["t"]
    dem = sum(out.get("demand", [18, 18]))
    win = [s for s in S["samples"] if t0 <= s["t"] <= te]
    v2 = gin.get("vllm2_ready_after_s") or next((s["t"] - t0 for s in win if len(s["vllm_ready_nodes"]) >= 2), None)
    gone = (end.get("node_removed_after_s") or 0) + (tin - t0)
    ev = [(v2, "2nd vLLM Ready"), (tin - t0, "load drops"), (gone, "node removed")]
    placement(ax, S, t0, te, lambda t: dem, [e for e in ev if e[0] is not None], dem * 1.6)
    wi = [s for s in S["samples"] if tin <= s["t"] <= te]
    pct = 100 * sum(tot(s, "bf_ready") + tot(s, "of_ready") >= dem for s in wi) / max(1, len(wi))
    ax.set_xlabel("seconds since LLM load rose")
    save(fig, f"{key}_gpu", w, h)
    return v2, end.get("node_removed_after_s"), round(pct, 1)


def native_cpu_heatmap(S, key, l3, ncores):
    w, h = 584, 118
    rows = [r for r in S["cpus"] if r.get("busy")]
    node = max({r["node"] for r in rows}, key=lambda n: sum(r["node"] == n for r in rows))
    rows = [r for r in rows if r["node"] == node]
    t0 = rows[0]["t"]
    times = [(r["t"] - t0) / 60 for r in rows]
    n = len(rows[0]["busy"])
    busy = {c: [float((r["busy"] or [0] * n)[c] or 0) for r in rows] for c in range(n)}
    owners = rows[-1].get("owner") or {}
    vllm = {int(c) for c, cn in owners.items() if "vllm" in str(cn) or cn == "main"}
    bfs = {int(c) for c, cn in owners.items() if int(c) not in vllm}
    fig = fig_px(w, h)
    ax = fig.add_axes([0.11, 0.2, 0.85, 0.77])
    heatmap_panel(ax, busy, times, l3, ncores, vllm, set(), "", bf=bfs)
    save(fig, f"{key}_heatmap", w, h)


def native_set(S, key, label_alone, l3, ncores, profile_rate):
    """All charts + KPIs for one native sweep. Returns the KPI dict (or None for a placeholder)."""
    if S is None:
        for nm, w, h in ((f"{key}_latency", 1040, 290), (f"{key}_nodebusy", 760, 200), (f"{key}_spike", 510, 260),
                         (f"{key}_gpu", 510, 260)):
            placeholder_chart(nm, w, h, "waiting for the final v6e sweep (incl. scale-out/in rerun)")
        return None
    S["rate"] = profile_rate
    src = str(S["dir"].relative_to(REPO))
    k = {"source": src, "partial": [], "heatmap": False}

    def attempt(fn, name, w, h, need):
        if not all(marks_of(S, p) for p in need) or (name == "latency" and len(marks_of(S, "backfill")) < 2):
            placeholder_chart(f"{key}_{name}", w, h, f"sweep phase '{need[-1]}' not finished yet ({src})")
            k["partial"].append(name)
            return None
        return fn()

    attempt(lambda: native_latency(S, key, label_alone) if {m["pods"] for m in marks_of(S, "load")} else
            placeholder_chart(f"{key}_latency", 1040, 290, f"sweep phase 'load' not finished yet ({src})"),
            "latency", 1040, 290, ["backfill", "load"])
    attempt(lambda: native_node_busy(S, key), "nodebusy", 760 if not S.get("cpus") else 370, 200, ["backfill"])
    r = attempt(lambda: native_spike(S, key), "spike", 510, 260, ["step_up", "step_down", "step_end"])
    if r:
        k["spike_covered_s"], k["spike_drained_s"] = r
    r = attempt(lambda: native_gpu(S, key), "gpu", 510, 260, ["gpu_out", "gpu_in", "gpu_end"])
    if r:
        k["vllm2_ready_s"], k["node_removed_s"], k["scalein_ready_pct"] = r
    if S.get("cpus"):
        native_cpu_heatmap(S, key, l3, ncores)
        k["heatmap"] = True
    bf = marks_of(S, "backfill")
    if len(bf) >= 2:
        k.update({"ttft_solo_ms": bf[0]["ttft"]["p99"], "ttft_full_ms": bf[-1]["ttft"]["p99"],
                  "full_pods": bf[-1]["pods"], "ttft_ratio": round(bf[-1]["ttft"]["p99"] / bf[0]["ttft"]["p99"], 2),
                  "node_busy_full": bf[-1].get("all_busy"), "node_busy_solo": bf[0].get("all_busy")})
    return k


# ================================================================ replays (trimmed /api/state frames)
TAGS = {"vllm": "V", "sandbox": "S", "toolcall": "T", "analytics": "A", "buffer": "B", "agent": "D"}
APP_CODE = {"free": 0, "system": 1, "vllm": 2, "sandbox": 3, "toolcall": 4, "analytics": 5, "buffer": 6,
            "agent": 7, "other": 8}
SVCS = ("sandbox", "toolcall", "analytics")


def cpu_kinds(n):
    """Python port of demo/static/app.js cpuKinds(): which app owns each vCPU tile."""
    ready = {p["name"]: p.get("ready") is not False for p in n.get("pods") or []}
    kinds = []
    for c in n["cpus"]:
        o, cat = c.get("owner"), c.get("cat")
        app = "free"
        if cat == "vllm":
            app = "vllm"
        elif cat == "buffer":
            app = "buffer"
        elif cat == "system":
            app = "system"
        elif cat in ("integer", "memory"):
            app = o["app"] if o and o.get("app") in SVCS else ("analytics" if cat == "memory" else "other")
        elif o:
            app = "other"
        kinds.append([app, bool(o and o.get("pod") and ready.get(o["pod"]) is False)])
    agent = n.get("agent_cpu") or 0
    reserved = max(0, round((n.get("ncpu") or 0) - (n.get("alloc_cpu") or n.get("ncpu") or 0))) + \
        round(max(0, (n.get("sys_cpu") or 0) - agent))
    agent_tiles = math.ceil(agent) if agent > 0 else 0
    free = sorted((i for i, k in enumerate(kinds) if k[0] == "free"),
                  key=lambda i: (n["cpus"][i]["core"], n["cpus"][i]["thread"]))
    for i in free:
        if reserved > 0:
            kinds[i][0] = "system"
            reserved -= 1
        elif agent_tiles > 0:
            kinds[i][0] = "agent"
            agent_tiles -= 1
        else:
            break
    return kinds


def trim_frame(s, t0):
    nodes = []
    order = {"g4": 0, "n4d": 1}
    ns = sorted((n for n in s.get("nodes") or [] if n.get("role") in order),
                key=lambda n: (order[n["role"]], -(n.get("age_s") or 0)))
    for n in ns:
        e = {"r": n["role"], "ok": bool(n.get("ready")), "ty": n.get("instance_type")}
        if n.get("cpus"):
            k = cpu_kinds(n)
            e["c"] = [[c["id"], c["core"], c["thread"], c["llc"], APP_CODE.get(k[i][0], 8),
                       int(round(float(c.get("busy") or 0))), 1 if k[i][1] else 0] for i, c in enumerate(n["cpus"])]
        else:
            e["n"] = n.get("ncpu") or 16
            e["p"] = [[APP_CODE.get(p.get("app") if p.get("app") in SVCS else ("vllm" if p.get("cat") == "vllm"
                       else "buffer" if p.get("cat") == "buffer" else "system"), 8), max(1, round(p.get("cpu") or 0)),
                       0 if p.get("ready") else 1] for p in n.get("pods") or []]
        nodes.append(e)
    lg, st, sv = s.get("loadgen") or {}, s.get("story") or {}, s.get("services") or {}
    bf = sum(((sv.get(k) or {}).get("backfill") or {}).get("ready") or 0 for k in SVCS)
    of = sum(((sv.get(k) or {}).get("overflow") or {}).get("ready") or 0 for k in SVCS)
    dem = sum((sv.get(k) or {}).get("demand") or 0 for k in SVCS)
    sc = s.get("scenario") or {}
    return {
        "t": round((s.get("ts") or s.get("t") or 0) - t0, 1), "nodes": nodes,
        "k": {"p99": lg.get("ttft_p99_ms"), "rps": lg.get("achieved_rps"), "tgt": lg.get("target_rps"),
              "vr": (s.get("vllm") or {}).get("ready"), "vd": (s.get("vllm") or {}).get("desired"),
              "bf": bf, "of": of, "dem": dem, "rec": st.get("reclaimed_vcpu"),
              "cost": (s.get("cost") or {}).get("per_hour")},
        "note": sc.get("current_note") or "",
        "sc": sc.get("name") or "",
    }


def compress(frames, idle_cap=1.0, gap_cap=2.0):
    """Skip idle stretches: frames whose key state did not change advance the playback clock by at most idle_cap
    seconds and runs of them are thinned; real gaps count at most gap_cap seconds. Writes frame['tc'] (playback
    seconds before the speed factor); frame['t'] keeps the recorded time."""
    def sig(f):
        k = f["k"]
        return (k.get("bf"), k.get("of"), k.get("vr"), k.get("vd"), k.get("dem"), len(f["nodes"]),
                tuple(n["ok"] for n in f["nodes"]))
    out, run = [], 0
    for i, f in enumerate(frames):
        same = bool(out) and sig(f) == sig(out[-1])
        run = run + 1 if same else 0
        if same and run % 2 and i != len(frames) - 1:
            continue  # keep every 2nd idle frame so busy % still moves
        if out:
            dt = f["t"] - out[-1]["t"]
            f["tc"] = round(out[-1]["tc"] + min(dt, idle_cap if same else gap_cap), 2)
        else:
            f["tc"] = 0.0
        out.append(f)
    return out


def load_replay(path: Path, max_frames=400, scenario=None):
    lines = [json.loads(x) for x in open(path) if x.strip()]
    lines = [x.get("state", x) if isinstance(x, dict) else x for x in lines]
    lines = [x for x in lines if isinstance(x, dict) and x.get("nodes")]
    if scenario:  # a combined recording (scale-out then scale-in): keep this scenario's stretch
        sel = [x for x in lines if (x.get("scenario") or {}).get("name") == scenario]
        lines = sel or lines
    if not lines:
        return None
    step = max(1, math.ceil(len(lines) / max_frames))
    lines = lines[::step]
    t0 = lines[0].get("ts") or lines[0].get("t") or 0
    title = next(((x.get("scenario") or {}).get("title") for x in lines if (x.get("scenario") or {}).get("title")), "")
    frames = compress([trim_frame(x, t0) for x in lines])
    return {"frames": frames, "title": title, "placeholder": bool(lines[0].get("_placeholder")),
            "source": str(path)}


REPLAY_SCENARIOS = [("cpu-scale-out", "CPU spike"), ("gpu-scale-out", "GPU scale-out"), ("gpu-scale-in", "GPU scale-in")]


def replays():
    """Scenario replays for slides 6 and 7: runs of the demo web app's scenario engine (DEMO_MOCK=1), trimmed and
    compressed. Stored in data/replays.json so the deck rebuilds without the raw recordings."""
    store = DECK / "data" / "replays.json"
    if store.exists() and not os.environ.get("DECK_MOCK_REPLAYS"):
        return json.load(open(store))
    out = {"g4": {}, "v6e": {}}
    for acc, mk in (("g4", "g4"), ("v6e", "tpu")):
        for sc, _ in REPLAY_SCENARIOS:
            cands = sorted(MOCK_REPLAYS.glob(f"{mk}_*{sc}*.jsonl")) or \
                (sorted(MOCK_REPLAYS.glob(f"{mk}_gpu-scale-out-in.jsonl")) if sc.startswith("gpu") else [])
            r = load_replay(cands[0], scenario=sc) if cands else None
            if r:
                r.pop("placeholder", None)
                r["source"] = "demo web app scenario engine (DEMO_MOCK=1)"
                if acc == "v6e":
                    for f in r["frames"]:
                        f["note"] = f["note"].replace("G4", "v6e")
                out[acc][sc] = r
            else:
                PLACEHOLDERS.append(f"replay {acc}/{sc}: no recording")
    store.parent.mkdir(exist_ok=True)
    store.write_text(json.dumps(out, separators=(",", ":")))
    return out


# ================================================================ pricing (slide 5)
def load_json(p: Path):
    try:
        return json.load(open(p))
    except Exception:  # noqa: BLE001
        return None


def pv(P, key):
    v = P.get(key)
    return v["value"] if isinstance(v, dict) else v


def pricing():
    """Slide 5 numbers from the pricing agent's files (scratch/deck/pricing/): pricing.json (every number with its
    source), g4_vs_aws.csv (sensitivity curve), guidance.csv (avoided N4D spend per host)."""
    P = load_json(PRICING / "pricing.json") or {}
    w, h = 832, 470
    sens_rows = read_csv(PRICING / "g4_vs_aws.csv") if (PRICING / "g4_vs_aws.csv").exists() else []
    out = {"src": "scratch/deck/pricing/ (pricing.json, g4_vs_aws.csv, guidance.csv)"}
    if sens_rows and P:
        x = [float(r["colo_pct"]) for r in sens_rows]
        y = [float(r["g4_effective_usd_hr"]) for r in sens_rows]
        aws = pv(P, "aws.g7e_2xlarge.us-east-1.3yr_ec2_instance_sp.no_upfront") or float(sens_rows[0]["aws_no_upfront_usd_hr"])
        be = 100 * pv(P, "result.break_even_colo.no_upfront")
        me = 100 * pv(P, "derived.g4.measured_colo_sheet_basis")
        ym = pv(P, "result.g4_effective_at_measured")
        fig = fig_px(w, h)
        ax = fig.add_axes([0.11, 0.15, 0.85, 0.8])
        ax.fill_between([be, 100], [aws, aws], [min(y) * 0.97] * 2, color=a("green", 0.10), lw=0)
        ax.plot(x, y, color=PAL["green"], lw=3.5)
        ax.axhline(aws, color=PAL["red"], lw=2.5, ls=(0, (6, 4)))
        ax.text(1, aws + 0.012, f"AWS g7e.2xlarge, 3-yr No Upfront  ${aws:.3f}/hr", va="bottom", fontsize=17)
        ax.text(3, y[0] + 0.004, f"G4 3-yr CUD  ${y[0]:.2f}/hr, nothing colocated", va="bottom", fontsize=17,
                color=PAL["green"], fontweight="bold")
        ax.axvline(be, color=a("ink", 0.55), lw=1.2, ls=(0, (2, 3)))
        ax.text(be - 1, 1.94, f"break-even\n{be:.1f} %", va="top", ha="right", fontsize=17)
        ax.plot([me], [ym], "o", ms=13, color=PAL["yellow"], mec=PAL["ink"], mew=1.5, zorder=5)
        ax.annotate(f"measured {me:.0f} %\n${ym:.3f}/hr", (me, ym), xytext=(-16, -12), textcoords="offset points",
                    ha="right", va="top", fontsize=18, fontweight="bold")
        ax.set_xlim(0, 100)
        ax.set_ylim(min(y) * 0.97, max(y) * 1.02)
        ax.set_xticks([0, 25, 50, 75, 100], ["0", "25", "50", "75", "100 %"])
        ax.yaxis.set_major_formatter(ticker.FuncFormatter(lambda v, _: f"${v:.2f}"))
        ax.set_xlabel("spare host vCPU put to work (colo %, of 40)")
        ax.set_ylabel("G4 effective $/hr")
        save(fig, "price_sensitivity", w, h)
        out["aws"] = {"aws_hr": aws, "break_even_pct": round(be, 1), "measured_pct": round(me, 1),
                      "g4_effective_measured": ym, "g4_3yr": y[0],
                      "saving_pct": pv(P, "result.savings_pct_vs_aws_noup_at_measured"),
                      "saving_hr": pv(P, "result.savings_vs_aws_noup_at_measured"),
                      "partial_hr": pv(P, "aws.g7e_2xlarge.us-east-1.3yr_ec2_instance_sp.partial_upfront"),
                      "all_hr": pv(P, "aws.g7e_2xlarge.us-east-1.3yr_ec2_instance_sp.all_upfront"),
                      "be_partial": 100 * pv(P, "result.break_even_colo.partial_upfront"),
                      "be_all": 100 * pv(P, "result.break_even_colo.all_upfront")}
    else:
        placeholder_chart("price_sensitivity", w, h, "pricing data not landed yet (scratch/deck/pricing/)")
    g = read_csv(PRICING / "guidance.csv") if (PRICING / "guidance.csv").exists() else []
    if g:
        table = {}
        for r in g:
            host = "g4" if r["host"].startswith("G4") else "v6e"
            table.setdefault(host, {}).setdefault(r["purchase_option"], {})[r["level"]] = {
                "pct": float(r["pct_of_spare"]), "vcpu": float(r["vcpu_used"]), "usd_mo": float(r["avoided_usd_month"]),
                "usd_hr": float(r["avoided_usd_hr"]), "pct_host": float(r["pct_of_host_price"]),
                "host_hr": float(r["host_usd_hr"])}
        out["guidance"] = table
    else:
        PLACEHOLDERS.append("guidance table: guidance.csv not landed yet")
    return out



# ================================================================ "what you get": g4-standard-48 vs AWS g7e.2xlarge
def what_you_get(J=None, name="what_you_get"):
    """Headline B visual from the pricing agent's what_you_get.json: two machine cards drawn to scale per resource
    (both machines share one scale per row; green = G4 host resources beyond the AWS shape, put to work by colo),
    and a waterfall: G4 3-yr price - extra vCPU value - extra RAM value = effective price, next to AWS 3-yr No Upfront."""
    J = J if J is not None else load_json(PRICING / "what_you_get.json")
    w, h = 832, 420
    if not J or "spec" not in J or "waterfall_3yr" not in J:
        placeholder_chart(name, w, h, "waiting for what_you_get.json (pricing agent)")
        return None
    from matplotlib.patches import FancyBboxPatch
    G, A = J["spec"]["g4-standard-48"], J["spec"]["g7e.2xlarge"]
    P = J["price_usd_hr"]
    W = J["waterfall_3yr"]
    rows = [("GPU memory", G["gpu_mem_gb"], A["gpu_mem_gb"], " GB", None),
            ("vCPU", G["vcpu"], A["vcpu"], "", None),
            ("Memory", G["ram_gib"], A["ram_gib"], " GiB", None),
            ("Network", G["network_gbps"], A["network_gbps"], " Gbps", None)]
    cards = [("g4-standard-48", f"${P['g4-standard-48']['3yr_cud']:.2f}/hr", "3-yr CUD · 1 × RTX PRO 6000", 0),
             ("AWS g7e.2xlarge", f"${P['g7e.2xlarge']['3yr_no_upfront']:.2f}/hr", "3-yr No Upfront · 1 × RTX PRO 6000", 1)]
    fig = fig_px(w, h)
    top = 0.44
    for title, price, basis, ci in cards:
        ax = fig.add_axes([0.0 if ci == 0 else 0.515, top, 0.485, 1 - top])
        ax.set_xlim(0, 1); ax.set_ylim(0, 1); ax.axis("off")
        ax.add_patch(FancyBboxPatch((0.01, 0.01), 0.98, 0.98, boxstyle="round,pad=0,rounding_size=0.05",
                                    fc=a("warm", 0.12), ec="none", transform=ax.transAxes))
        ax.text(0.05, 0.9, title, fontsize=18, fontweight="bold", va="center")
        ax.text(0.95, 0.9, price, fontsize=18, fontweight="bold", va="center", ha="right")
        ax.text(0.05, 0.79, basis, fontsize=15.5, color=INK2, va="center")
        for r, (lab, gv, av, unit, zero_txt) in enumerate(rows):
            y = 0.6 - r * 0.165
            v, other = (gv, av) if ci == 0 else (av, gv)
            mx = max(gv, av) or 1
            ax.text(0.05, y + 0.02, lab, fontsize=16, color=INK2, va="bottom")
            ax.text(0.95, y + 0.02, f"{v:,g}{unit}" if v or not zero_txt else zero_txt, fontsize=16.5,
                    fontweight="bold", va="bottom", ha="right")
            x0, xw, bh = 0.05, 0.9, 0.035
            ax.add_patch(plt.Rectangle((x0, y - bh), xw, bh, fc=a("warm", 0.25), ec="none"))
            base = min(v, other) if ci == 0 else v
            ax.add_patch(plt.Rectangle((x0, y - bh), xw * base / mx, bh, fc=PAL["blue"] if ci == 0 else PAL["warm"], ec="none"))
            if ci == 0 and v > other:
                ax.add_patch(plt.Rectangle((x0 + xw * other / mx, y - bh), xw * (v - other) / mx, bh, fc=PAL["green"], ec="none"))
    ax = fig.add_axes([0.3, 0.07, 0.68, top - 0.12])
    p0, dv, dr = W["g4_price"], W["minus_extra_vcpu_value"], W["minus_extra_ram_value"]
    eff, aws = W["g4_effective_aws_equivalent"], W["aws_price"]
    colo = int(round(100 * W.get("colo", J.get("measured_colo", 0.9))))
    bars = [("G4 3-yr CUD", 0, p0, PAL["warm"], 1, f"${p0:.2f}"),
            (f"− extra 40 vCPU at {colo} %", p0 + dv, p0, PAL["green"], 0.55, f"−${-dv:.2f}"),
            (f"− extra 116 GiB at {colo} %", p0 + dv + dr, p0 + dv, PAL["green"], 0.55, f"−${-dr:.2f}"),
            ("G4 effective", 0, eff, PAL["green"], 1, f"${eff:.2f}"),
            ("AWS 3-yr No Upfront", 0, aws, PAL["red"], 1, f"${aws:.2f}")]
    for i, (lab, lo, hi, col, al, txt) in enumerate(bars):
        y = len(bars) - 1 - i
        ax.barh(y, hi - lo, left=lo, height=0.64, color=col, alpha=al)
        ax.text(hi + 0.03, y, txt, va="center", fontsize=16.5, fontweight="bold")
    ax.text(eff + 0.03 + 0.42, 1, f"{W['g4_vs_aws_pct']:+.1f} % vs AWS", va="center", fontsize=15.5,
            fontweight="bold", color=PAL["green"])
    ax.set_yticks(range(len(bars))[::-1], [b[0] for b in bars], fontsize=16)
    ax.set_xlim(0, max(p0, aws) * 1.28)
    ax.xaxis.set_major_formatter(ticker.FuncFormatter(lambda v, _: f"${v:.1f}"))
    ax.tick_params(axis="x", labelsize=13.5)
    ax.tick_params(axis="y", length=0)
    ax.grid(axis="y", visible=False)
    ax.spines["left"].set_visible(False)
    save(fig, name, w, h)
    S = J.get("sensitivity_3yr", {})
    fn = f"Green = extra host resources valued at N4D 3-yr CUD, credited at {colo} %. "
    if "60%" in S and "100%" in S:
        fn += f"At 60 %: {S['60%']['g4_vs_aws_pct']:+.1f} %; at 100 %: {S['100%']['g4_vs_aws_pct']:+.1f} %. "
    pa, al_ = P["g7e.2xlarge"]["3yr_partial_upfront"], P["g7e.2xlarge"]["3yr_all_upfront"]
    fn += (f"Vs AWS Partial Upfront {100 * (eff / pa - 1):+.1f} %, All Upfront {100 * (eff / al_ - 1):+.1f} %.")
    return {"g4_effective": round(eff, 2), "aws": round(aws, 2), "pct": W["g4_vs_aws_pct"], "fn": fn,
            "footnotes": [f for f in J.get("footnotes", []) if "NVMe" not in f and "SSD" not in f]}

# ================================================================ main
def main():
    kpi = {}
    exp_heatmap()
    exp_delta()
    exp_cdf()
    kpi["exp_node_busy"] = exp_node_util()
    imp = read_csv(RES / "analysis" / "vllm_phase_impact_split.csv")
    pick = lambda ph, m: float(next(r["delta_pct"] for r in imp if r["level"] == "38" and r["phase"] == ph and r["metric"] == m))
    kpi["exp"] = {"ttft_p99_integer": round(pick("compute", "ttft_p99"), 1),
                  "ttft_p99_fotonik": round(pick("fotonik3d", "ttft_p99"), 1),
                  "ttft_p99_lbm": round(pick("lbm", "ttft_p99"), 1),
                  "tok_s_max_abs": round(max(abs(float(r["delta_pct"])) for r in imp if r["metric"] == "output_tok_s"), 1)}
    exp_spec_forest()

    g4 = load_sweep(BENCH / "sweep1_merged", "native")
    kpi["g4"] = native_set(g4, "g4", "vLLM alone", L3_G4, 24, 450)
    # v6e stays a placeholder until the sweep (incl. the scale-out/in rerun after the TPU fixes) is declared final
    v6 = load_sweep(BENCH / "v6e_sweep1") if os.environ.get("DECK_V6E_FINAL") == "1" else None
    kpi["v6e"] = native_set(v6, "v6e", "vLLM alone", L3_V6E, 22, 200)
    if v6 is None or not v6.get("cpus"):
        placeholder_chart("v6e_heatmap", 584, 118, "waiting for the final v6e sweep")

    if MOCK_REPLAYS.exists() and not (DECK / "data" / "replays.json").exists():
        os.environ["DECK_MOCK_REPLAYS"] = str(MOCK_REPLAYS)
    # Single source for the backfill sizing shown in text (slides 2, 3, 7, 8 and notes): the largest backfill step of each
    # sweep, 2 vCPU per pod; spare = host vCPU - vLLM's 8. Override the v6e cap with DECK_V6E_PODS if needed.
    v6_pods = int(os.environ.get("DECK_V6E_PODS") or (max([m["pods"] for m in marks_of(v6, "backfill")] or [16]) if v6 else 16))
    g4_pods = max([m["pods"] for m in marks_of(g4, "backfill")] or [18]) if g4 else 18
    kpi["cfg"] = {"g4_pods": g4_pods, "g4_reclaimed": 2 * g4_pods, "g4_spare": 40,
                  "g4_pct": round(100 * 2 * g4_pods / 40), "v6e_pods": v6_pods, "v6e_reclaimed": 2 * v6_pods,
                  "v6e_spare": 36, "v6e_pct": round(100 * 2 * v6_pods / 36)}
    pr = pricing()
    pr["what_you_get"] = what_you_get()
    data = {"svgs": SVGS, "kpi": kpi, "replays": replays(), "pricing": pr, "placeholders": PLACEHOLDERS}
    (DECK / "js").mkdir(exist_ok=True)
    (DECK / "js" / "deck_data.js").write_text(
        "/* generated by deck_charts.py - do not edit */\nwindow.DECK_DATA = " + json.dumps(data, separators=(",", ":")) + ";\n")
    print("wrote", DECK / "js" / "deck_data.js", f"{(DECK / 'js' / 'deck_data.js').stat().st_size / 1e6:.1f} MB")
    print("placeholders:", *PLACEHOLDERS, sep="\n  ")


if __name__ == "__main__":
    sys.exit(main())
