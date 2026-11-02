#!/usr/bin/env python3
"""Native recipe live gate (wave N3), run against g4cluster-jawadamin-us-west8-1126.

Same scenarios and thresholds as recipe/ wave-2 clause 5, driven only through stock Kubernetes:
demand = `kubectl scale <svc>-backfill`, GPU load = loadgen /rate. Nothing here steers placement.

Phases (any subset, in order):
  c      TTFT protection: p99 solo (demand 0) vs with backfill (10/9), both at 450 rps.   pass: ratio <= 1.5
  noscale demand 40/40 for NOSCALE_S at 450 rps: G4 node count never rises; |running - demand| <= 2 per
         service within 120 s of the change and after.                                      pass: both
  a      GPU scale-out: demand 18/18, rate 1200 -> 2nd vLLM on a new G4 node.               pass: >= 90 % of the
         new node's backfill slots Ready <= 60 s after the 2nd vLLM is Ready (and its timeline vs recipe/: 4 s)
  b      GPU scale-in: rate 300 -> vLLM 2 -> 1 -> CA removes the node.                       pass: Ready pods
         (backfill + overflow) >= demand in >= 99 % of 2 s samples per service until the node is gone

Talks to the cluster through `kubectl --context CTX proxy --port 8198` (started here).
Samples -> .agents/native/logs/gate_native.samples.jsonl, verdict -> gate_native.verdict.json.

    python3 gate_native.py --phases c,noscale,a,b
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

CTX = "gke_northam-ce-mlai-tpu_us-west8_g4cluster-jawadamin-us-west8-1126"
LOGS = Path(__file__).resolve().parents[3] / ".agents" / "native" / "logs"
PROXY = "http://127.0.0.1:8198"
SVCS = ("sandbox", "toolcall")
NOSCALE_S = 900


def iso(t):
    return None if t is None else datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="seconds")


def log(msg):
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)


def http(path, body=None, timeout=6.0):
    req = urllib.request.Request(PROXY + path, data=None if body is None else json.dumps(body).encode(),
                                 method="GET" if body is None else "POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"null")


def kubectl(*args, check=True):
    return subprocess.run(["kubectl", "--context", CTX, *args], capture_output=True, text=True, check=check).stdout


def hist_p99(h):
    n = sum(h)
    if not n:
        return None
    acc = 0
    for i, v in enumerate(h):
        acc += v
        if acc >= 0.99 * n:
            return (i + 1) * 2.0 if i < 1000 else 2000.0  # same 2 ms buckets as recipe/ wave-2 clause5.py
    return 2000.0


def ready(p):
    return any(c["type"] == "Ready" and c["status"] == "True" for c in p["status"].get("conditions", []))


class Sampler:
    def __init__(self):
        LOGS.mkdir(parents=True, exist_ok=True)
        self.fh = open(LOGS / "gate_native.samples.jsonl", "a")
        self.samples = []
        self.lock = threading.Lock()
        self.stop = False

    def snapshot(self):
        t = time.time()
        nodes = json.loads(kubectl("get", "nodes", "-o", "json"))["items"]
        pods = http("/api/v1/pods?fieldSelector=status.phase!%3DSucceeded,status.phase!%3DFailed", timeout=10)["items"]
        deps = {d["metadata"]["name"]: d for d in http("/apis/apps/v1/namespaces/colo-svc/deployments")["items"]}
        g4 = [n["metadata"]["name"] for n in nodes if n["metadata"]["labels"].get("colo.role") == "g4"]
        vllm = [p for p in pods if p["metadata"]["namespace"] == "inference"
                and p["metadata"]["labels"].get("app") == "vllm"]
        vllm_ready_nodes = {p["spec"].get("nodeName") for p in vllm if ready(p)
                            and not p["metadata"].get("deletionTimestamp")}
        svc = {}
        for s in SVCS:
            bf = [p for p in pods if p["metadata"]["namespace"] == "colo-svc"
                  and p["metadata"]["labels"].get("app") == s and p["metadata"]["labels"].get("colo.tier") == "backfill"]
            of = [p for p in pods if p["metadata"]["namespace"] == "colo-svc"
                  and p["metadata"]["labels"].get("app") == s and p["metadata"]["labels"].get("colo.tier") == "overflow"]
            svc[s] = {
                "demand": deps[f"{s}-backfill"]["spec"].get("replicas", 0),
                "bf_ready": sum(1 for p in bf if ready(p)),
                "bf_ready_by_node": {n: sum(1 for p in bf if ready(p) and p["spec"].get("nodeName") == n) for n in g4},
                "bf_pending": sum(1 for p in bf if p["status"].get("phase") == "Pending"),
                "of_ready": sum(1 for p in of if ready(p)),
                "of_spec": deps[f"{s}-overflow"]["spec"].get("replicas", 0),
            }
        lg_hists, rates = [], []
        for p in pods:
            if p["metadata"]["namespace"] == "inference" and p["metadata"]["labels"].get("app") == "loadgen" and ready(p):
                try:
                    st = http(f"/api/v1/namespaces/inference/pods/{p['metadata']['name']}:8080/proxy/stats", timeout=4)
                    rates.append(st.get("rate_total"))
                    for d in (st.get("per_pod") or {}).values():
                        if d.get("ttft_hist"):
                            lg_hists.append(d["ttft_hist"][:1001])
                except Exception:  # noqa: BLE001
                    pass
        merged = [sum(x) for x in zip(*lg_hists)] if lg_hists else []  # same merge as wave-2 clause5.py
        return {"t": t, "g4_nodes": g4, "vllm_total": len(vllm), "vllm_ready_nodes": sorted(n for n in vllm_ready_nodes if n),
                "svc": svc, "loadgen": {"rates": rates, "p99_ms": hist_p99(merged), "n": sum(merged)}, "_hist": merged}

    def run(self, every=2.0):
        while not self.stop:
            t0 = time.time()
            try:
                s = self.snapshot()
                with self.lock:
                    self.samples.append(s)
                self.fh.write(json.dumps({k: v for k, v in s.items() if k != "_hist"}) + "\n")
                self.fh.flush()
            except Exception as e:  # noqa: BLE001
                log(f"sample error {type(e).__name__}: {e}")
            time.sleep(max(0.2, every - (time.time() - t0)))

    def last(self):
        while not self.samples:
            time.sleep(0.5)
        with self.lock:
            return self.samples[-1]

    def wait(self, pred, timeout, what):
        t0 = time.time()
        while time.time() - t0 < timeout:
            s = self.last()
            if s["t"] >= t0 and pred(s):
                log(f"ok: {what} ({time.time() - t0:.0f}s)")
                return time.time()
            time.sleep(1)
        log(f"TIMEOUT: {what}")
        return None

    def window(self, t0, t1=None):
        with self.lock:
            return [s for s in self.samples if s["t"] >= t0 and (t1 is None or s["t"] <= t1)]


def set_rate(S, rate):
    pods = json.loads(kubectl("-n", "inference", "get", "pods", "-l", "app=loadgen", "-o", "json"))["items"]
    for p in pods:
        if ready(p):
            http(f"/api/v1/namespaces/inference/pods/{p['metadata']['name']}:8080/proxy/rate", {"rate": rate})
    log(f"set loadgen rate {rate}")


def set_demand(sandbox, toolcall):
    kubectl("-n", "colo-svc", "scale", "deploy/sandbox-backfill", f"--replicas={sandbox}")
    kubectl("-n", "colo-svc", "scale", "deploy/toolcall-backfill", f"--replicas={toolcall}")
    log(f"set demand {sandbox}/{toolcall}")


def covered(s):
    return all(v["bf_ready"] + v["of_ready"] >= v["demand"] for v in s["svc"].values())


def pooled_p99(S, t0, t1):
    """Pool non-overlapping 30 s loadgen windows ending inside [t0+30, t1] (same method as wave-2 clause5.py)."""
    picks, cur = [], t0 + 30
    win = S.window(t0, t1 + 1)
    while cur <= t1 + 1:
        cand = [s for s in win if s["t"] >= cur - 1]
        if not cand:
            break
        picks.append(cand[0])
        cur = cand[0]["t"] + 30
    hs = [s["_hist"] for s in picks if s["_hist"]]
    h = [sum(x) for x in zip(*hs)] if hs else []
    return {"p99_ms": hist_p99(h), "requests": sum(h), "windows": len(picks)}


def phase_c(S, V):
    log("== phase c: TTFT protection")
    set_demand(0, 0)
    set_rate(S, 450)
    S.wait(lambda s: all(v["bf_ready"] == 0 for v in s["svc"].values()) and s["loadgen"]["rates"]
           and all(r == 450 for r in s["loadgen"]["rates"]), 300, "solo: no backfill, 450 rps")
    time.sleep(40)
    t0 = time.time()
    time.sleep(90)
    solo = pooled_p99(S, t0, time.time())
    set_demand(10, 9)
    S.wait(lambda s: sum(v["bf_ready"] for v in s["svc"].values()) >= 19, 300, "19 backfill pods Ready on G4")
    time.sleep(40)
    t1 = time.time()
    time.sleep(120)
    bw = pooled_p99(S, t1, time.time())
    ratio = (bw["p99_ms"] / solo["p99_ms"]) if solo["p99_ms"] and bw["p99_ms"] else None
    V["c"] = {"solo": solo, "with_backfill": bw, "ratio": ratio and round(ratio, 3),
              "backfill_pods": sum(v["bf_ready"] for v in S.last()["svc"].values()),
              "pass": ratio is not None and ratio <= 1.5}
    log(f"phase c: {json.dumps(V['c'])}")


def phase_noscale(S, V):
    log("== phase noscale: CPU demand never adds G4")
    set_rate(S, 450)
    g4_before = len(S.last()["g4_nodes"])
    t0 = time.time()
    set_demand(40, 40)
    t_cov = S.wait(covered, 600, "demand 40/40 covered (backfill + overflow Ready)")
    time.sleep(max(0, NOSCALE_S - (time.time() - t0)))
    win = S.window(t0)
    g4_max = max(len(s["g4_nodes"]) for s in win)
    after = [s for s in win if s["t"] >= t0 + 120]
    dev = [max(abs(v["bf_ready"] + v["of_ready"] - v["demand"]) for v in s["svc"].values()) for s in after]
    # Within 120 s is the plan's steady-state bound; N4D node provisioning can exceed it, so report both.
    after_cov = [s for s in win if t_cov and s["t"] >= t_cov]
    dev_cov = [max(abs(v["bf_ready"] + v["of_ready"] - v["demand"]) for v in s["svc"].values()) for s in after_cov]
    V["noscale"] = {"g4_before": g4_before, "g4_max": g4_max, "covered_after_s": t_cov and round(t_cov - t0, 1),
                    "max_dev_after_120s": max(dev) if dev else None,
                    "max_dev_after_covered": max(dev_cov) if dev_cov else None,
                    "pass_no_g4_scaleup": g4_max == g4_before,
                    "pass_steady_120s": bool(dev) and max(dev) <= 2}
    log(f"phase noscale: {json.dumps(V['noscale'])}")


def phase_a(S, V):
    log("== phase a: GPU scale-out")
    set_demand(18, 18)
    set_rate(S, 450)
    S.wait(lambda s: s["vllm_total"] == 1 and len(s["g4_nodes"]) == 1 and covered(s), 900, "steady 1 vLLM, covered")
    time.sleep(10)
    t0 = time.time()
    set_rate(S, 1200)
    first = set(S.last()["g4_nodes"])
    t_vready = S.wait(lambda s: len(s["vllm_ready_nodes"]) >= 2, 900, "2nd vLLM Ready")
    new = None
    if t_vready:
        new = next(n for n in S.last()["vllm_ready_nodes"] if n not in first)
    slots_new = None
    t_fill = None
    if new:
        # Slots = backfill pods the new node holds once settled; the controller recipe's 18-slot node is the reference.
        S.wait(lambda s: sum(v["bf_ready_by_node"].get(new, 0) for v in s["svc"].values()) >= 17, 300,
               "new G4 node >= 17 backfill Ready")
        win = S.window(t_vready)
        slots_new = max(sum(v["bf_ready_by_node"].get(new, 0) for v in s["svc"].values()) for s in win)
        for s in win:
            if sum(v["bf_ready_by_node"].get(new, 0) for v in s["svc"].values()) >= 0.9 * slots_new:
                t_fill = s["t"]
                break
    V["a"] = {"t_rate": iso(t0), "vllm2_ready_after_rate_s": t_vready and round(t_vready - t0, 1),
              "new_node": new, "new_node_slots": slots_new,
              "fill90_after_vllm2_ready_s": (t_fill and t_vready) and round(t_fill - t_vready, 1),
              "pass": bool(t_fill and t_vready and t_fill - t_vready <= 60)}
    log(f"phase a: {json.dumps(V['a'])}")


def phase_b(S, V):
    log("== phase b: GPU scale-in")
    S.wait(lambda s: len(s["vllm_ready_nodes"]) == 2 and covered(s), 900, "2 vLLM Ready, covered")
    time.sleep(20)
    g4_before = set(S.last()["g4_nodes"])
    t0 = time.time()
    set_rate(S, 300)
    t_gone = S.wait(lambda s: len(s["g4_nodes"]) < len(g4_before), 1800, "a G4 node removed by CA")
    time.sleep(20)
    win = S.window(t0)
    per = {}
    for svc in SVCS:
        ok = [s["svc"][svc]["bf_ready"] + s["svc"][svc]["of_ready"] >= s["svc"][svc]["demand"] for s in win]
        per[svc] = {"fraction_ready_ge_demand": round(sum(ok) / len(ok), 4) if ok else None, "n": len(ok),
                    "short_samples": len(ok) - sum(ok),
                    "min_ready": min(s["svc"][svc]["bf_ready"] + s["svc"][svc]["of_ready"] for s in win)}
    V["b"] = {"t_rate": iso(t0), "node_removed_after_s": t_gone and round(t_gone - t0, 1), "per_service": per,
              "pass": all(p["fraction_ready_ge_demand"] is not None and p["fraction_ready_ge_demand"] >= 0.99
                          for p in per.values())}
    log(f"phase b: {json.dumps(V['b'])}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phases", default="c,noscale,a,b")
    args = ap.parse_args()
    proxy = subprocess.Popen(["kubectl", "--context", CTX, "proxy", "--port", "8198"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(2)
    S = Sampler()
    th = threading.Thread(target=S.run, daemon=True)
    th.start()
    V = {"started": iso(time.time()), "cluster": CTX}
    out = LOGS / "gate_native.verdict.json"
    try:
        for ph in args.phases.split(","):
            {"c": phase_c, "noscale": phase_noscale, "a": phase_a, "b": phase_b}[ph](S, V)
            json.dump(V, open(out, "w"), indent=1)
    finally:
        S.stop = True
        set_rate(S, 450)
        set_demand(10, 9)
        V["finished"] = iso(time.time())
        json.dump(V, open(out, "w"), indent=1)
        proxy.terminate()
    log(f"verdict -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
