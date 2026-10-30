#!/usr/bin/env python3
"""Wave-1 gate checks for the colo-at-scale recipe, run against the live cluster.

Usage (from g4_colo/recipe/tests, demo venv):  python gate.py g1g3|g2|g4|g6|g7 [--out DIR]

Uses the demo app's live backend (same data path as the UI) and samples cluster state every 5 s to a JSONL file,
then evaluates the gate clauses and writes a verdict JSON. Each check prints PASS/FAIL lines.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "demo"))
from cluster import LiveBackend  # noqa: E402
from state import build_state, parse_cpuset, summarize  # noqa: E402

# GKE truncates MIG names to 16 chars of nodepool name in TriggeredScaleUp events. Defaults to the demo pool.
G4_POOL = os.environ.get("G4_POOL_PREFIX", "g4gpu-jawadamin")


def now() -> float:
    return time.time()


def log(msg: str):
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)


def kubectl_json(*args) -> dict:
    return json.loads(subprocess.run(["kubectl", *args, "-o", "json"], capture_output=True, text=True, check=True).stdout)


class Run:
    def __init__(self, name: str, out: Path):
        self.be = LiveBackend()
        self.name = name
        self.out = out
        out.mkdir(parents=True, exist_ok=True)
        self.samples: list[dict] = []
        self.cpuset_violations: list[dict] = []
        self.marks: dict[str, float] = {}
        self.checks: list[dict] = []
        self._stop = False
        self._fh = open(out / f"{name}.samples.jsonl", "a")

    async def snapshot(self) -> tuple[dict, dict, dict]:
        raw = await self.be.raw()
        st = build_state(raw)
        return raw, st, summarize(st)

    async def sampler(self, every: float = 5.0):
        while not self._stop:
            t0 = now()
            try:
                raw, st, s = await self.snapshot()
                s["t"] = t0
                s["per_node"] = {n: {"ttft_p99_ms": v.get("ttft_p99_ms"), "n": v.get("ok")}
                                 for n, v in (st.get("loadgen") or {}).get("per_node", {}).items()}
                s["services"] = {k: {"demand": v.get("demand"), "bf_ready": v["backfill"]["ready"],
                                     "of_ready": v["overflow"]["ready"], "bf_desired": v["backfill"]["desired"],
                                     "of_desired": v["overflow"]["desired"]}
                                 for k, v in (st.get("services") or {}).items()}
                s["g4_node_names"] = sorted(n["name"] for n in st.get("nodes", []) if n.get("role") == "g4")
                self.check_cpusets(raw, t0)
                self.samples.append(s)
                self._fh.write(json.dumps(s, default=str) + "\n")
                self._fh.flush()
            except Exception as e:  # noqa: BLE001
                log(f"sample error: {type(e).__name__}: {e}")
            await asyncio.sleep(max(0.5, every - (now() - t0)))

    def check_cpusets(self, raw: dict, t: float):
        """vLLM's exclusive cpuset must be disjoint from every other pod's on every G4 node."""
        pods = {p.get("uid"): p for p in raw.get("pods") or []}
        for node, a in (raw.get("agents") or {}).items():
            cms = (a or {}).get("cpu_manager_state") or {}
            entries = cms.get("entries") or {}
            owned = []
            for uid, ctrs in entries.items():
                for ctr, cs in ctrs.items():
                    owned.append(((pods.get(uid) or {}).get("app") or uid, set(parse_cpuset(cs))))
            vllm = [cs for app, cs in owned if app == "vllm"]
            others = [(app, cs) for app, cs in owned if app != "vllm"]
            for v in vllm:
                for app, cs in others:
                    if v & cs:
                        self.cpuset_violations.append({"t": t, "node": node, "app": app, "overlap": sorted(v & cs)})
                if v & set(parse_cpuset(cms.get("defaultCpuSet"))):
                    self.cpuset_violations.append({"t": t, "node": node, "app": "shared-pool", "overlap": True})

    async def wait_until(self, pred, timeout: float, what: str, every: float = 5.0) -> float | None:
        t0 = now()
        while now() - t0 < timeout:
            if self.samples and pred(self.samples[-1]):
                log(f"met: {what} after {now() - t0:.0f}s")
                return now()
            await asyncio.sleep(every)
        log(f"TIMEOUT: {what} ({timeout:.0f}s)")
        return None

    def check(self, clause: str, desc: str, ok: bool, evidence: dict):
        self.checks.append({"clause": clause, "check": desc, "pass": bool(ok), "evidence": evidence})
        log(f"{'PASS' if ok else 'FAIL'}  [{clause}] {desc}  {json.dumps(evidence, default=str)[:300]}")

    def finish(self):
        self._stop = True
        self._fh.close()
        verdict = {"run": self.name, "marks": self.marks, "checks": self.checks,
                   "cpuset_violations": self.cpuset_violations[:50], "n_samples": len(self.samples),
                   "all_pass": all(c["pass"] for c in self.checks)}
        (self.out / f"{self.name}.verdict.json").write_text(json.dumps(verdict, indent=2, default=str))
        log(f"verdict: {'ALL PASS' if verdict['all_pass'] else 'SOME FAIL'} -> {self.out / (self.name + '.verdict.json')}")
        return verdict


def events(reason: str | None = None) -> list[dict]:
    ev = kubectl_json("get", "events", "-A", "--field-selector", f"reason={reason}" if reason else "")["items"]
    return ev


def ca_scaleups_since(t0: float) -> list[dict]:
    out = []
    for e in events("TriggeredScaleUp"):
        ts = e.get("lastTimestamp") or e.get("eventTime") or (e.get("metadata") or {}).get("creationTimestamp")
        try:
            tt = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        except Exception:  # noqa: BLE001
            continue
        if tt >= t0 - 5:
            io = e.get("involvedObject") or {}
            out.append({"ns": io.get("namespace"), "pod": io.get("name"), "msg": e.get("message"), "t": tt})
    return out


def pod_ready_time(ns: str, name: str) -> float | None:
    p = kubectl_json("get", "pod", "-n", ns, name)
    for c in (p.get("status") or {}).get("conditions") or []:
        if c["type"] == "Ready" and c["status"] == "True":
            return datetime.fromisoformat(c["lastTransitionTime"].replace("Z", "+00:00")).timestamp()
    return None


def backfill_on_node(node: str) -> int:
    pods = kubectl_json("get", "pods", "-n", "colo-svc", "-l", "colo.tier=backfill", "--field-selector",
                        f"spec.nodeName={node}")["items"]
    return sum(1 for p in pods if any(c["type"] == "Ready" and c["status"] == "True"
                                      for c in (p.get("status") or {}).get("conditions") or []))


def node_spare_slots(node: str, pod_cpu: int = 2) -> int:
    n = kubectl_json("get", "node", node)
    alloc = float(n["status"]["allocatable"]["cpu"].rstrip("m")) / (1000 if n["status"]["allocatable"]["cpu"].endswith("m") else 1)
    pods = kubectl_json("get", "pods", "-A", "--field-selector", f"spec.nodeName={node}")["items"]
    used = 0.0
    for p in pods:
        if (p["metadata"].get("labels") or {}).get("colo.tier") == "backfill":
            continue
        if p["status"].get("phase") in ("Succeeded", "Failed"):
            continue
        for c in p["spec"]["containers"]:
            r = ((c.get("resources") or {}).get("requests") or {}).get("cpu", "0")
            used += float(r[:-1]) / 1000 if r.endswith("m") else float(r)
    return int((alloc - used) // pod_cpu)


# ----------------------------------------------------------------------------------------------- G1 + G3
async def g1g3(run: Run):
    be = run.be
    samp = asyncio.create_task(run.sampler())
    # Size warm N4D buffer (15 x 2 vCPU = 30 vCPU across 3 N4D nodes) to cover 1 G4 node's backfill domain.
    await be._post_config(
        {"buffer_replicas": 15, "guard": {"enabled": False}, "demand": {"sandbox": 18, "toolcall": 18, "analytics": 0}}
    )
    await be.set_gpu_rate(300)
    await asyncio.sleep(10)
    await run.wait_until(
        lambda s: s["vllm_ready"] >= 1 and s["g4_nodes"] == 1 and s["backfill_ready"] >= 18 and s["overflow_ready"] >= 16,
        900,
        "steady: 1 vLLM, 1 G4, backfill + warm N4D overflow filled",
    )
    g4_before = run.samples[-1]["g4_node_names"]
    run.marks["scale_out_start"] = now()
    log("G1: raising GPU rate to 1200 rps")
    await be.set_gpu_rate(1200)
    t_desired = await run.wait_until(lambda s: s["vllm_desired"] >= 2, 420, "HPA desired >= 2")
    t_g4 = await run.wait_until(lambda s: s["g4_nodes"] >= 2, 900, "second G4 node registered")
    t_ready = await run.wait_until(lambda s: s["vllm_ready"] >= 2, 1500, "second vLLM Ready")
    new_nodes = sorted(set(run.samples[-1]["g4_node_names"]) - set(g4_before))
    new_node = new_nodes[0] if new_nodes else None
    vpods = kubectl_json("get", "pods", "-n", "inference", "-l", "app=vllm")["items"]
    vnew = [p for p in vpods if p["spec"].get("nodeName") == new_node]
    t_vready = pod_ready_time("inference", vnew[0]["metadata"]["name"]) if vnew else None
    # backfill on the new node within 120 s of its vLLM pod becoming Ready
    t_fill, filled, slots = None, 0, None
    if new_node and t_vready:
        slots = node_spare_slots(new_node)
        target = max(1, int(0.8 * slots))
        while now() - t_vready < 300:
            filled = backfill_on_node(new_node)
            if filled >= target:
                t_fill = now()
                break
            await asyncio.sleep(5)
    sups = ca_scaleups_since(run.marks["scale_out_start"])
    g4_sups = [e for e in sups if G4_POOL in (e["msg"] or "")]
    run.check("1", "HPA adds a vLLM replica", t_desired is not None, {"t_desired_s": t_desired and t_desired - run.marks["scale_out_start"]})
    run.check("1", "ComputeClass adds exactly one G4 node", t_g4 is not None and run.samples[-1]["g4_nodes"] == 2,
              {"new_nodes": new_nodes, "g4_scaleup_events": [(e["ns"], e["pod"]) for e in g4_sups]})
    run.check("1", "only the vLLM pod triggered the G4 scale-up", all(e["ns"] == "inference" for e in g4_sups) and len(g4_sups) >= 1,
              {"events": g4_sups})
    run.check("1", "backfill occupies >=80% of new node's spare slots within 120 s of vLLM Ready",
              t_fill is not None and t_vready is not None and t_fill - t_vready <= 120,
              {"new_node": new_node, "slots": slots, "filled": filled, "seconds": t_fill and t_vready and round(t_fill - t_vready)})
    await asyncio.sleep(60)
    # ---------------- G3: scale in
    run.marks["scale_in_start"] = now()
    log("G3: dropping GPU rate to 300 rps")
    await be.set_gpu_rate(300)
    t_v1 = await run.wait_until(lambda s: s["vllm_desired"] <= 1, 900, "HPA desired <= 1")
    t_n1 = await run.wait_until(lambda s: s["g4_nodes"] <= 1, 1800, "extra G4 node removed")
    run.check("3", "HPA removes the extra replica", t_v1 is not None, {"s": t_v1 and t_v1 - run.marks["scale_in_start"]})
    run.check("3", "CA removes the extra G4 node within 15 min of the replica going away",
              t_n1 is not None and t_v1 is not None and t_n1 - t_v1 <= 900,
              {"node_removed_after_replica_s": t_n1 and t_v1 and round(t_n1 - t_v1)})
    await asyncio.sleep(120)
    # service availability over the whole run (from scale-out start)
    win = [s for s in run.samples if s["t"] >= run.marks["scale_out_start"]]
    for svc in ("sandbox", "toolcall"):
        short, episodes, cur = 0, [], None
        for s in win:
            v = s["services"].get(svc) or {}
            ready = (v.get("bf_ready") or 0) + (v.get("of_ready") or 0)
            dem = v.get("demand") or 0
            if ready < dem:
                short += 1
                cur = cur or s["t"]
            elif cur:
                episodes.append(s["t"] - cur)
                cur = None
        if cur:
            episodes.append(win[-1]["t"] - cur)
        frac = 1 - short / max(1, len(win))
        run.check("3", f"{svc}: ready >= demand for >=95% of samples", frac >= 0.95, {"fraction": round(frac, 4), "n": len(win)})
        run.check("3", f"{svc}: every shortfall recovers within 60 s", all(e <= 60 for e in episodes),
                  {"episodes_s": [round(e) for e in episodes]})
    run.check("1", "vLLM cpuset disjoint from all other pods at every sample", not run.cpuset_violations,
              {"violations": run.cpuset_violations[:5], "samples": len(run.samples)})
    await be._post_config({"buffer_replicas": 1, "demand": {"sandbox": 0, "toolcall": 0, "analytics": 0}})
    samp.cancel()


# ----------------------------------------------------------------------------------------------- G2
async def g2(run: Run):
    be = run.be
    samp = asyncio.create_task(run.sampler())
    await be.set_guard(False)
    await be.set_gpu_rate(300)
    await be.set_demand({"sandbox": 0, "toolcall": 0, "analytics": 0})
    await run.wait_until(
        lambda s: s["vllm_ready"] >= 1 and s["g4_nodes"] == 1 and s["vllm_desired"] == 1 and s["n4d_nodes"] <= 1,
        1800,
        "1 G4, 1 vLLM, <=1 N4D",
    )
    await asyncio.sleep(30)
    g4_0, n4d_0 = run.samples[-1]["g4_nodes"], run.samples[-1]["n4d_nodes"]
    run.marks["cpu_scale_out_start"] = now()
    log("G2: raising CPU demand beyond G4 spare")
    await be.set_demand({"sandbox": 16, "toolcall": 16, "analytics": 0})
    t_of = await run.wait_until(lambda s: s["overflow_desired"] > 0, 120, "overflow assigned")
    t_n4d = await run.wait_until(lambda s: s["n4d_nodes"] > max(n4d_0, 1), 900, "N4D nodes added")
    t_all = await run.wait_until(lambda s: s["overflow_ready"] >= s["overflow_desired"] > 0, 1200, "all overflow Ready")
    win = [s for s in run.samples if s["t"] >= run.marks["cpu_scale_out_start"]]
    sups = ca_scaleups_since(run.marks["cpu_scale_out_start"])
    g4_sups = [e for e in sups if G4_POOL in (e["msg"] or "")]
    run.check("2", "CPU demand beyond G4 spare adds N4D nodes", t_n4d is not None,
              {"n4d_before": n4d_0, "n4d_after": run.samples[-1]["n4d_nodes"]})
    run.check("2", "all overflow replicas become Ready", t_all is not None, {})
    run.check("2", "G4 node count unchanged for the whole run", all(s["g4_nodes"] == g4_0 for s in win),
              {"g4_counts": sorted({s["g4_nodes"] for s in win})})
    run.check("2", "no CA scale-up event names the G4 pool", not g4_sups, {"events": g4_sups[:5]})
    # negative control: N4D overflow disabled (controller paused, overflow 0), backfill over-requested
    log("G2 negative control: pausing controller, over-requesting backfill, overflow at 0")
    await be.set_demand({"sandbox": 0, "toolcall": 0, "analytics": 0})
    await asyncio.sleep(30)
    subprocess.run(["kubectl", "-n", "colo-system", "scale", "deploy/colo-controller", "--replicas=0"], check=True)
    for d in ("sandbox-overflow", "toolcall-overflow", "analytics-overflow"):
        subprocess.run(["kubectl", "-n", "colo-svc", "scale", f"deploy/{d}", "--replicas=0"], check=True)
    subprocess.run(["kubectl", "-n", "colo-system", "scale", "deploy/colo-buffer", "--replicas=0"], check=True)
    await asyncio.sleep(60)
    nodes0 = len(kubectl_json("get", "nodes")["items"])
    run.marks["neg_start"] = now()
    subprocess.run(["kubectl", "-n", "colo-svc", "scale", "deploy/sandbox-backfill", "--replicas=40"], check=True)
    await asyncio.sleep(420)
    pend = [p for p in kubectl_json("get", "pods", "-n", "colo-svc", "-l", "app=sandbox,colo.tier=backfill")["items"]
            if p["status"].get("phase") == "Pending"]
    nodes1 = len(kubectl_json("get", "nodes")["items"])
    neg_sups = ca_scaleups_since(run.marks["neg_start"])
    run.check("2", "negative control: over-requested backfill pods stay Pending", len(pend) > 0, {"pending": len(pend)})
    run.check("2", "negative control: no node of any kind added", nodes1 <= nodes0 and not neg_sups,
              {"nodes_before": nodes0, "nodes_after": nodes1, "scaleups": neg_sups[:5]})
    subprocess.run(["kubectl", "-n", "colo-svc", "scale", "deploy/sandbox-backfill", "--replicas=0"], check=True)
    subprocess.run(["kubectl", "-n", "colo-system", "scale", "deploy/colo-controller", "--replicas=1"], check=True)
    samp.cancel()


# ----------------------------------------------------------------------------------------------- G4
async def g4(run: Run, hold_s: int = 240):
    be = run.be
    samp = asyncio.create_task(run.sampler())
    await be.set_guard(False)
    await be.set_gpu_rate(400)
    await be.set_demand({"sandbox": 0, "toolcall": 0, "analytics": 0})
    await run.wait_until(lambda s: s["backfill_ready"] == 0 and s["vllm_ready"] >= 1 and s["vllm_desired"] == 1, 900, "no backfill, 1 vLLM")
    await asyncio.sleep(90)
    r = await be.baseline()
    base = (r or {}).get("baseline_ttft_p99_ms")
    run.marks["baseline_ms"] = base
    log(f"baseline TTFT p99 = {base} ms")
    thr, ok_back = 1.5 * base, 1.15 * base

    def p99(s):
        return s.get("ttft_p99_ms") or 0

    # (a) integer-type, guard on
    await be.set_guard(True)
    await be.set_demand({"sandbox": 9, "toolcall": 9, "analytics": 0})
    await run.wait_until(lambda s: s["integer_backfill_ready"] >= 16, 600, "integer backfill Ready")
    await asyncio.sleep(40)
    run.marks["a_start"] = now()
    ev0 = len(events("ColoGuardEvict"))
    await asyncio.sleep(hold_s)
    win = [s for s in run.samples if s["t"] >= run.marks["a_start"]]
    ev1 = len(events("ColoGuardEvict"))
    run.check("4a", "integer-type: TTFT p99 stays below 1.5x baseline", max(p99(s) for s in win) < thr,
              {"max_p99_ms": max(p99(s) for s in win), "threshold_ms": thr})
    run.check("4a", "integer-type: guard takes no action and pods stay on G4",
              ev1 == ev0 and min(s["integer_backfill_ready"] for s in win) >= 16,
              {"evict_events": ev1 - ev0, "min_integer_backfill": min(s["integer_backfill_ready"] for s in win)})
    # (b-control) memory-heavy, guard OFF
    await be.set_guard(False)
    await be.set_demand({"sandbox": 0, "toolcall": 0, "analytics": 16})
    await run.wait_until(lambda s: s["memory_backfill_ready"] >= 12, 600, "memory-heavy backfill Ready")
    await asyncio.sleep(40)
    run.marks["ctrl_start"] = now()
    await asyncio.sleep(hold_s)
    win = [s for s in run.samples if s["t"] >= run.marks["ctrl_start"]]
    frac_above = sum(1 for s in win if p99(s) > thr) / max(1, len(win))
    run.check("4b", "negative control (guard off): TTFT p99 stays above 1.5x baseline", frac_above >= 0.9,
              {"fraction_above": round(frac_above, 3), "median_p99": sorted(p99(s) for s in win)[len(win) // 2], "threshold_ms": thr})
    # (b) memory-heavy, guard ON
    run.marks["guard_on"] = now()
    ev0 = len(events("ColoGuardEvict"))
    await be.set_guard(True)
    t_ev = None
    t0 = now()
    while now() - t0 < 180:
        if len(events("ColoGuardEvict")) > ev0:
            t_ev = now()
            break
        await asyncio.sleep(5)
    t_rec = await run.wait_until(lambda s: p99(s) <= ok_back and s["memory_backfill_ready"] == 0, 300, "p99 back within +15% of baseline")
    await run.wait_until(
        lambda s: (s["services"].get("analytics") or {}).get("of_ready", 0) >= 12,
        300,
        "evicted analytics replicas Ready on N4D",
    )
    await asyncio.sleep(15)
    last = run.samples[-1]
    run.check("4b", "guard evicts memory-heavy backfill when p99 > 1.5x baseline", t_ev is not None,
              {"seconds_to_evict": t_ev and round(t_ev - run.marks["guard_on"])})
    run.check("4b", "evicted replicas come back as overflow on N4D",
              (last["services"].get("analytics") or {}).get("of_ready", 0) >= 12,
              {"analytics": last["services"].get("analytics")})
    run.check("4b", "TTFT p99 within +15% of baseline within 180 s of eviction",
              t_rec is not None and t_ev is not None and t_rec - t_ev <= 180,
              {"seconds": t_rec and t_ev and round(t_rec - t_ev), "p99_ms": p99(last), "limit_ms": ok_back})
    await be.set_demand({"sandbox": 0, "toolcall": 0, "analytics": 0})
    samp.cancel()


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("which", choices=["g1g3", "g2", "g4"])
    ap.add_argument("--out", default=str(HERE.parents[2] / ".agents" / "wave-1" / "logs"))
    a = ap.parse_args()
    run = Run(a.which, Path(a.out))
    try:
        await {"g1g3": g1g3, "g2": g2, "g4": g4}[a.which](run)
    finally:
        run.finish()


if __name__ == "__main__":
    asyncio.run(main())
