#!/usr/bin/env python3
"""Charts for sweep.py output: controller recipe vs native recipe.

    g4_colo/.venv/bin/python plots.py <sweep dir> [names]   # writes <dir>/charts/*.png and <dir>/summary.md
    names: comma list of clusters in the dir (default controller,native; the TPU v6e sweep is "tpu").
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

D = Path(sys.argv[1])
OUT = D / "charts"
OUT.mkdir(exist_ok=True)
NAMES = tuple(sys.argv[2].split(",")) if len(sys.argv) > 2 else ("controller", "native")
COL = {"controller": "#1a3a6b", "native": "#1b9e77", "tpu": "#7B3FA0"}
LBL = {"controller": "With colo-controller (recipe/)", "native": "Native K8s only (recipe-native/)",
       "tpu": "TPU v6e, native (recipe-native/overlays/tpu-v6e)"}
plt.rcParams.update({"font.size": 10, "axes.grid": True, "grid.alpha": 0.3, "axes.spines.top": False,
                     "axes.spines.right": False, "figure.dpi": 130})


def load(name):
    samples = [json.loads(l) for l in open(D / f"{name}.samples.jsonl")] if (D / f"{name}.samples.jsonl").exists() else []
    marks = json.load(open(D / f"{name}.marks.json")) if (D / f"{name}.marks.json").exists() else []
    return samples, marks


DATA = {n: load(n) for n in NAMES}


def marks(name, phase):
    return [m for m in DATA[name][1] if m["phase"] == phase]


def mark_t(name, phase):
    m = marks(name, phase)
    return m[0]["t"] if m else None


def window(name, t0, t1):
    return [s for s in DATA[name][0] if t0 <= s["t"] <= t1]


def tot(s, key, svcs=("sandbox", "toolcall")):
    return sum(s["svc"][k][key] for k in svcs)


def save(fig, fname):
    fig.tight_layout()
    fig.savefig(OUT / fname)
    plt.close(fig)
    print("wrote", OUT / fname)


summary = {n: {} for n in NAMES}


def step_levels(n):
    """(low, high) total sandbox+toolcall demand of the step phase; sweep1 marks predate the demand fields."""
    m = marks(n, "step_up")
    hi = sum(m[0].get("demand", [20, 20])) if m else 40
    lo = sum(m[0].get("demand_lo", [9, 9])) if m else 18
    return lo, hi


def gpu_demand(n):
    m = marks(n, "gpu_out")
    return m[0].get("demand", [18, 18]) if m else [18, 18]


def accel_slots(n):
    return 16 if n == "tpu" else 18

# 1. TTFT vs backfill pods ----------------------------------------------------------------------------------------
fig, ax = plt.subplots(figsize=(7, 4.2))
for n in NAMES:
    ms = marks(n, "backfill")
    if not ms:
        continue
    x = [m["pods"] for m in ms]
    ax.plot(x, [m["ttft"]["p99"] for m in ms], "o-", color=COL[n], label=f"{LBL[n]} p99")
    ax.plot(x, [m["ttft"]["p50"] for m in ms], "o--", color=COL[n], alpha=0.5, label=f"{LBL[n]} p50")
    for m in ms:
        if m.get("vllm_l3"):
            ax.annotate(f"L3×{m['vllm_l3']}", (m["pods"], m["ttft"]["p99"]), textcoords="offset points",
                        xytext=(0, 7 if n == "controller" else -13), ha="center", fontsize=7, color=COL[n])
    base = ms[0]["ttft"]["p99"]
    summary[n]["ttft_p99_by_pods"] = {m["pods"]: m["ttft"]["p99"] for m in ms}
    summary[n]["ttft_ratio_full"] = round(ms[-1]["ttft"]["p99"] / base, 2) if base else None
if "tpu" not in NAMES:
    ax.axhline(30, color="grey", ls=":", lw=1)
    ax.text(0.2, 30.5, "1.5 x 20 ms budget", fontsize=7, color="grey")
_rate = ", ".join(sorted({str(m.get("rate", 450)) for n in NAMES for m in marks(n, "backfill")}))
ax.set(xlabel="Backfill pods on the vLLM node (2 vCPU each)", ylabel="TTFT (ms)",
       title=f"vLLM TTFT vs CPU backfill on the same accelerator node ({_rate} rps, 1 vLLM)\n"
             "L3×N: number of L3 cache domains (CCX) vLLM's 8 cores span")
ax.legend(fontsize=7)
save(fig, "1_ttft_vs_backfill.png")

# 2. TTFT vs load --------------------------------------------------------------------------------------------------
fig, ax = plt.subplots(figsize=(7, 4.2))
for n in NAMES:
    for pods, ls in ((0, "--"), (accel_slots(n), "-")):
        ms = [m for m in marks(n, "load") if m["pods"] == pods]
        if ms:
            ax.plot([m["rate"] for m in ms], [m["ttft"]["p99"] for m in ms], "o" + ls, color=COL[n],
                    label=f"{LBL[n]}, {f'{pods} backfill pods' if pods else 'solo'}")
    summary[n]["ttft_p99_by_rate"] = {f"{m['pods']}pods@{m['rate']}": m["ttft"]["p99"] for m in marks(n, "load")}
ax.set(xlabel="Offered load (requests/s, 1 vLLM replica)", ylabel="TTFT p99 (ms)", yscale="log",
       title="TTFT p99 vs load: solo vs full backfill")
ax.legend(fontsize=7)
save(fig, "2_ttft_vs_load.png")

# 3. vLLM core busy vs backfill -----------------------------------------------------------------------------------
fig, ax = plt.subplots(figsize=(7, 4))
for n in NAMES:
    ms = [m for m in marks(n, "backfill") if m.get("vllm_busy") is not None]
    if ms:
        ax.plot([m["pods"] for m in ms], [m["vllm_busy"] for m in ms], "o-", color=COL[n], label=f"{LBL[n]} vLLM cores")
        ax.plot([m["pods"] for m in ms], [m["all_busy"] for m in ms], "s:", color=COL[n], alpha=0.6,
                label=f"{LBL[n]} whole node")
ax.set(xlabel="Backfill pods on the vLLM node", ylabel="CPU busy (%)", title=f"CPU utilisation on the accelerator node ({_rate} rps)")
ax.legend(fontsize=7)
save(fig, "3_cpu_busy_vs_backfill.png")


# 4. Demand step response -----------------------------------------------------------------------------------------
def timeline(axs, n, t0, t1, title, demand_fn):
    w = window(n, t0, t1)
    if not w:
        return
    x = [s["t"] - t0 for s in w]
    axs.plot(x, [demand_fn(s) for s in w], color="black", lw=1.2, label="demand")
    axs.plot(x, [tot(s, "bf_ready") for s in w], color=COL[n], label="backfill Ready (G4)")
    axs.plot(x, [tot(s, "of_ready") for s in w], color="#d95f02", label="overflow Ready (N4D)")
    axs.plot(x, [tot(s, "bf_ready") + tot(s, "of_ready") for s in w], color="#7570b3", ls="--", label="total Ready")
    a2 = axs.twinx()
    a2.step(x, [len(s["n4d"]) for s in w], color="grey", alpha=0.5, where="post")
    a2.set_ylabel("N4D nodes", color="grey")
    a2.set_ylim(0, 6)
    axs.set(title=title, xlabel="seconds since step", ylabel="pods")


fig, axes = plt.subplots(1, len(NAMES), figsize=(6 * len(NAMES), 4.2), sharey=True, squeeze=False)
axes = axes[0]
for ax, n in zip(axes, NAMES):
    tu, te = mark_t(n, "step_up"), mark_t(n, "step_end")
    if tu and te:
        td = mark_t(n, "step_down")
        lo, hi = step_levels(n)
        timeline(ax, n, tu - 30, te, LBL[n], lambda s, td=td, lo=lo, hi=hi: hi if td > s["t"] >= mark_t(n, "step_up") else lo)
        ax.axvline(td - tu + 30, color="black", ls=":", lw=1)
        summary[n]["step_covered_after_s"] = marks(n, "step_down")[0].get("covered_after_s")
        summary[n]["step_drained_after_s"] = marks(n, "step_end")[0].get("drained_after_s")
        # N4D node-minutes during the step
        w = window(n, tu, te)
        summary[n]["step_n4d_node_minutes"] = round(sum(len(s["n4d"]) * 2 for s in w) / 60, 1)
axes[0].legend(fontsize=7, loc="upper right")
fig.suptitle("CPU demand step: " + "; ".join(f"{n} {step_levels(n)[0]} -> {step_levels(n)[1]} -> {step_levels(n)[0]} pods" for n in NAMES))
save(fig, "4_step_response.png")

# 5. GPU scale-out / scale-in -------------------------------------------------------------------------------------
fig, axes = plt.subplots(2, len(NAMES), figsize=(6 * len(NAMES), 7), sharex="col", squeeze=False)
for col, n in enumerate(NAMES):
    gd = gpu_demand(n)
    dem = sum(gd)
    t0, te = mark_t(n, "gpu_out"), mark_t(n, "gpu_end")
    if not (t0 and te):
        continue
    w = window(n, t0 - 20, te)
    x = [s["t"] - t0 for s in w]
    ax = axes[0][col]
    ax.plot(x, [tot(s, "bf_ready") for s in w], color=COL[n], label="backfill Ready (G4)")
    ax.plot(x, [tot(s, "of_ready") for s in w], color="#d95f02", label="overflow Ready (N4D)")
    ax.plot(x, [min(1.5, (tot(s, "bf_ready") + tot(s, "of_ready")) / dem) * dem for s in w], color="#7570b3", ls="--",
            label=f"total Ready (demand {dem})")
    ax.axhline(dem, color="black", lw=0.8)
    ax.set(title=LBL[n], ylabel="pods")
    tin = mark_t(n, "gpu_in")
    for a in axes[:, col]:
        a.axvline(tin - t0, color="black", ls=":", lw=1)
    ax = axes[1][col]
    ax.step(x, [len(s["vllm_ready_nodes"]) for s in w], where="post", color="#1f78b4", label="vLLM Ready")
    ax.step(x, [len(s["g4"]) for s in w], where="post", color="#e31a1c", label="G4 nodes")
    ax.step(x, [len(s["n4d"]) for s in w], where="post", color="grey", label="N4D nodes")
    go, gi = marks(n, "gpu_out")[0], marks(n, "gpu_in")[0]
    ax.set(xlabel=f"seconds since load -> {go.get('rate', 1200)} rps"
                  f"{' + HPA min 2' if go.get('mode') == 'force' else ''} (dotted: -> {gi.get('rate', 300)} rps)",
           ylabel="count")
    gin = window(n, tin, te)
    ok = [all(s["svc"][k]["bf_ready"] + s["svc"][k]["of_ready"] >= v for k, v in zip(("sandbox", "toolcall"), gd)) for s in gin]
    summary[n]["scalein_ready_ge_demand_pct"] = round(100 * sum(ok) / len(ok), 1) if ok else None
    summary[n]["vllm2_ready_after_s"] = marks(n, "gpu_in")[0].get("vllm2_ready_after_s")
    summary[n]["node_removed_after_s"] = marks(n, "gpu_end")[0].get("node_removed_after_s")
    summary[n]["node_added_after_s"] = marks(n, "gpu_in")[0].get("node_added_after_s")
    tv2 = next((s["t"] for s in window(n, t0, tin) if len(s["vllm_ready_nodes"]) >= 2), None)
    if tv2:
        new = [g for g in w[-1]["g4"]] if w else []
        fill = next((s["t"] - tv2 for s in window(n, tv2 - 300, tin)
                     if len(s["vllm_ready_nodes"]) >= 2 and tot(s, "bf_ready") >= 2 * accel_slots(n) - 2), None)
        summary[n][f"g4_fill_{2 * accel_slots(n) - 2}_after_vllm2_ready_s"] = round(fill, 1) if fill is not None else None
axes[0][0].legend(fontsize=7)
axes[1][0].legend(fontsize=7)
fig.suptitle("Accelerator scale-out (vLLM 1 -> 2) and scale-in (2 -> 1); " + "; ".join(f"{n} demand {gpu_demand(n)[0]}/{gpu_demand(n)[1]}" for n in NAMES))
save(fig, "5_gpu_scale_out_in.png")

# 6. Summary bars --------------------------------------------------------------------------------------------------
keys = [("step_covered_after_s", "demand step up:\ncovered after (s)"),
        ("step_drained_after_s", "demand step down:\noverflow drained (s)"),
        ("vllm2_ready_after_s", "2nd vLLM Ready\nafter load up (s)"),
        ("node_removed_after_s", "G4 node removed\nafter load down (s)")]
fig, ax = plt.subplots(figsize=(9, 4))
w = 0.76 / len(NAMES)
for i, n in enumerate(NAMES):
    vals = [summary[n].get(k) or 0 for k, _ in keys]
    bars = ax.bar([j + (i - (len(NAMES) - 1) / 2) * w for j in range(len(keys))], vals, w, color=COL[n], label=LBL[n])
    for b, v in zip(bars, vals):
        ax.text(b.get_x() + b.get_width() / 2, v, f"{v:.0f}", ha="center", va="bottom", fontsize=8)
ax.set_xticks(range(len(keys)), [l for _, l in keys], fontsize=8)
ax.set(ylabel="seconds", title="Reaction times")
ax.legend(fontsize=8)
save(fig, "6_reaction_times.png")

# 7. Reclaimed vCPU and N4D usage over the whole sweep ------------------------------------------------------------
fig, axes = plt.subplots(2, 1, figsize=(12, 5.5), sharex=True)
t_start = min(DATA[n][0][0]["t"] for n in NAMES if DATA[n][0])
# Merged runs (merge.py): this chart covers only the simultaneous sweep, which ends where the first rerun starts.
t_end = min([m["t"] for n in NAMES for m in DATA[n][1] if m.get("source", "sweep1") != "sweep1"
             and m["phase"] in ("step_up", "gpu_out")] + [float("inf")]) - 120
for n in NAMES:
    s_ = [s for s in DATA[n][0] if s["t"] <= t_end]
    if not s_:
        continue
    x = [(s["t"] - t_start) / 60 for s in s_]
    axes[0].plot(x, [2 * tot(s, "bf_ready") for s in s_], color=COL[n], label=LBL[n])
    axes[1].step(x, [len(s["n4d"]) for s in s_], where="post", color=COL[n], label=LBL[n])
    summary[n]["sweep_n4d_node_minutes"] = round(sum(len(s["n4d"]) for s in s_) * 2 / 60, 1)
    summary[n]["sweep_reclaimed_vcpu_minutes"] = round(sum(2 * tot(s, "bf_ready") for s in s_) * 2 / 60, 1)
    summary[n]["max_reclaimed_vcpu"] = max(2 * tot(s, "bf_ready") for s in DATA[n][0])
    summary[n]["accel_node_hours"] = round(sum(len(s["g4"]) * (b["t"] - s["t"]) for s, b in zip(DATA[n][0], DATA[n][0][1:])) / 3600, 2)
for n in NAMES:
    for m in DATA[n][1]:
        if m["phase"] in ("backfill", "load") or m["t"] > t_end:
            continue
        axes[1].axvline((m["t"] - t_start) / 60, color=COL[n], alpha=0.25, lw=0.8)
axes[0].set(ylabel="vCPU used by backfill on G4",
            title="Reclaimed G4 vCPU and N4D nodes during the simultaneous sweep (both clusters share one N4D quota)")
axes[1].set(ylabel="N4D nodes", xlabel="minutes since sweep start")
axes[0].legend(fontsize=8)
save(fig, "7_reclaimed_vcpu_and_n4d.png")

json.dump(summary, open(D / "summary.json", "w"), indent=1)
rows = ["ttft_ratio_full", "step_covered_after_s", "step_drained_after_s", "step_n4d_node_minutes",
        "vllm2_ready_after_s", "g4_fill_34_after_vllm2_ready_s", "scalein_ready_ge_demand_pct", "node_removed_after_s",
        "sweep_n4d_node_minutes", "sweep_reclaimed_vcpu_minutes"]
rows += ["node_added_after_s", "max_reclaimed_vcpu", "accel_node_hours"]
md = ["| metric | " + " | ".join(NAMES) + " |", "|---" * (len(NAMES) + 1) + "|"]
md += [f"| {r} | " + " | ".join(str(summary[n].get(r)) for n in NAMES) + " |" for r in rows]
md += ["", "TTFT p99 by backfill pods (ms):", "", "| pods | " + " | ".join(NAMES) + " |", "|---" * (len(NAMES) + 1) + "|"]
for p in (0, 4, 8, 12, 16, 18):
    md.append(f"| {p} | " + " | ".join(str(summary[n].get("ttft_p99_by_pods", {}).get(p)) for n in NAMES) + " |")
(D / "summary.md").write_text("\n".join(md) + "\n")
print((D / "summary.md").read_text())
