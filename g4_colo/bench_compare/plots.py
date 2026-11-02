#!/usr/bin/env python3
"""Charts for sweep.py output: controller recipe vs native recipe.

    g4_colo/.venv/bin/python plots.py <sweep dir>     # writes <dir>/charts/*.png and <dir>/summary.md
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
NAMES = ("controller", "native")
COL = {"controller": "#1a3a6b", "native": "#1b9e77"}
LBL = {"controller": "With colo-controller (recipe/)", "native": "Native K8s only (recipe-native/)"}
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
ax.axhline(30, color="grey", ls=":", lw=1)
ax.text(0.2, 30.5, "1.5 x 20 ms budget", fontsize=7, color="grey")
ax.set(xlabel="Backfill pods on the vLLM node (2 vCPU each)", ylabel="TTFT (ms)",
       title="vLLM TTFT vs CPU backfill on the same G4 node (450 rps, 1 vLLM)\n"
             "L3×N: number of L3 cache domains (CCX) vLLM's 8 cores span")
ax.legend(fontsize=7)
save(fig, "1_ttft_vs_backfill.png")

# 2. TTFT vs load --------------------------------------------------------------------------------------------------
fig, ax = plt.subplots(figsize=(7, 4.2))
for n in NAMES:
    for pods, ls in ((0, "--"), (18, "-")):
        ms = [m for m in marks(n, "load") if m["pods"] == pods]
        if ms:
            ax.plot([m["rate"] for m in ms], [m["ttft"]["p99"] for m in ms], "o" + ls, color=COL[n],
                    label=f"{LBL[n]}, {'18 backfill pods' if pods else 'solo'}")
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
ax.set(xlabel="Backfill pods on the vLLM node", ylabel="CPU busy (%)", title="CPU utilisation on the G4 node (450 rps)")
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


fig, axes = plt.subplots(1, 2, figsize=(12, 4.2), sharey=True)
for ax, n in zip(axes, NAMES):
    tu, te = mark_t(n, "step_up"), mark_t(n, "step_end")
    if tu and te:
        td = mark_t(n, "step_down")
        timeline(ax, n, tu - 30, te, LBL[n], lambda s, td=td: 40 if s["t"] < td and s["t"] >= mark_t(n, "step_up") else 18)
        ax.axvline(td - tu + 30, color="black", ls=":", lw=1)
        summary[n]["step_covered_after_s"] = marks(n, "step_down")[0].get("covered_after_s")
        summary[n]["step_drained_after_s"] = marks(n, "step_end")[0].get("drained_after_s")
        # N4D node-minutes during the step
        w = window(n, tu, te)
        summary[n]["step_n4d_node_minutes"] = round(sum(len(s["n4d"]) * 2 for s in w) / 60, 1)
axes[0].legend(fontsize=7, loc="upper right")
fig.suptitle("CPU demand step 18 -> 40 pods -> 18 pods (450 rps)")
save(fig, "4_step_response.png")

# 5. GPU scale-out / scale-in -------------------------------------------------------------------------------------
fig, axes = plt.subplots(2, 2, figsize=(12, 7), sharex="col")
for col, n in enumerate(NAMES):
    t0, te = mark_t(n, "gpu_out"), mark_t(n, "gpu_end")
    if not (t0 and te):
        continue
    w = window(n, t0 - 20, te)
    x = [s["t"] - t0 for s in w]
    ax = axes[0][col]
    ax.plot(x, [tot(s, "bf_ready") for s in w], color=COL[n], label="backfill Ready (G4)")
    ax.plot(x, [tot(s, "of_ready") for s in w], color="#d95f02", label="overflow Ready (N4D)")
    ax.plot(x, [min(1.5, (tot(s, "bf_ready") + tot(s, "of_ready")) / 36) * 36 for s in w], color="#7570b3", ls="--",
            label="total Ready (demand 36)")
    ax.axhline(36, color="black", lw=0.8)
    ax.set(title=LBL[n], ylabel="pods")
    tin = mark_t(n, "gpu_in")
    for a in axes[:, col]:
        a.axvline(tin - t0, color="black", ls=":", lw=1)
    ax = axes[1][col]
    ax.step(x, [len(s["vllm_ready_nodes"]) for s in w], where="post", color="#1f78b4", label="vLLM Ready")
    ax.step(x, [len(s["g4"]) for s in w], where="post", color="#e31a1c", label="G4 nodes")
    ax.step(x, [len(s["n4d"]) for s in w], where="post", color="grey", label="N4D nodes")
    ax.set(xlabel="seconds since load 450 -> 1200 rps (dotted: 1200 -> 300 rps)", ylabel="count")
    gin = window(n, tin, te)
    ok = [all(s["svc"][k]["bf_ready"] + s["svc"][k]["of_ready"] >= 18 for k in ("sandbox", "toolcall")) for s in gin]
    summary[n]["scalein_ready_ge_demand_pct"] = round(100 * sum(ok) / len(ok), 1) if ok else None
    summary[n]["vllm2_ready_after_s"] = marks(n, "gpu_in")[0].get("vllm2_ready_after_s")
    summary[n]["node_removed_after_s"] = marks(n, "gpu_end")[0].get("node_removed_after_s")
    tv2 = next((s["t"] for s in window(n, t0, tin) if len(s["vllm_ready_nodes"]) >= 2), None)
    if tv2:
        new = [g for g in w[-1]["g4"]] if w else []
        fill = next((s["t"] - tv2 for s in window(n, tv2 - 300, tin)
                     if len(s["vllm_ready_nodes"]) >= 2 and tot(s, "bf_ready") >= 34), None)
        summary[n]["g4_fill_34_after_vllm2_ready_s"] = round(fill, 1) if fill is not None else None
axes[0][0].legend(fontsize=7)
axes[1][0].legend(fontsize=7)
fig.suptitle("GPU scale-out (vLLM 1 -> 2) and scale-in (2 -> 1), demand 18/18")
save(fig, "5_gpu_scale_out_in.png")

# 6. Summary bars --------------------------------------------------------------------------------------------------
keys = [("step_covered_after_s", "demand step up:\ncovered after (s)"),
        ("step_drained_after_s", "demand step down:\noverflow drained (s)"),
        ("vllm2_ready_after_s", "2nd vLLM Ready\nafter load up (s)"),
        ("node_removed_after_s", "G4 node removed\nafter load down (s)")]
fig, ax = plt.subplots(figsize=(9, 4))
w = 0.38
for i, n in enumerate(NAMES):
    vals = [summary[n].get(k) or 0 for k, _ in keys]
    bars = ax.bar([j + (i - 0.5) * w for j in range(len(keys))], vals, w, color=COL[n], label=LBL[n])
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
md = ["| metric | controller | native |", "|---|---|---|"]
md += [f"| {r} | {summary['controller'].get(r)} | {summary['native'].get(r)} |" for r in rows]
md += ["", "TTFT p99 by backfill pods (ms):", "", "| pods | controller | native |", "|---|---|---|"]
for p in (0, 4, 8, 12, 16, 18):
    md.append(f"| {p} | {summary['controller'].get('ttft_p99_by_pods', {}).get(p)} | "
              f"{summary['native'].get('ttft_p99_by_pods', {}).get(p)} |")
(D / "summary.md").write_text("\n".join(md) + "\n")
print((D / "summary.md").read_text())
