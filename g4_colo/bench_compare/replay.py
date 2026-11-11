#!/usr/bin/env python3
"""Record demo-app /api/state replays for the deck: drive one scenario on one cluster and save one lean state per line.

    python3 replay.py --cluster native --scenario steady      # 4 min of steady state
    python3 replay.py --cluster tpu --scenario cpu-spike      # CPU demand spike -> N4D overflow -> back
    python3 replay.py --cluster tpu --scenario llm-scale      # LLM load scale-out -> new accelerator host -> scale-in

Output: <out>/<cluster>_<scenario>.jsonl, one {"t", "phase", "state"} per line (~every --interval s), plus
<out>/<cluster>_<scenario>.events.json with the driver's actions. The demo app (g4_colo/demo) must be running with the
cluster in DEMO_CLUSTERS. Before recording, the cluster is settled at the scenario's starting point (not recorded).

Drivers:
  steady     demand 10/10/2, 300 rps, record 240 s.
  cpu-spike  settle at 9/9/0 and 300 rps, then the demo's own `cpu-scale-out` scenario (9/9 -> 18/18 -> 9/9).
  llm-scale  settle at 19/19/0 and 300 rps.
             G4: the demo's `gpu-scale-out` then `gpu-scale-in` scenarios (1200 rps, the demo's fast-scale).
             TPU v6e: one v6e vLLM replica completes ~330 rps, below the HPA's 700 rps/pod target, so the scale-out
             is driven the way the demo's fast-scale does it (HPA minReplicas 2) at 600 rps (what 2 replicas can
             serve), then 300 rps and minReplicas 1. Rate is set on the loadgen pods directly, because the demo's
             /api/gpu-rate would reset minReplicas for rates <= 700.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

CTX = {"native": "gke_northam-ce-mlai-tpu_us-west8_g4cluster-jawadamin-us-west8-1126",
       "tpu": "gke_northam-ce-mlai-tpu_us-east5_v6ecluster-jawadamin-us-east5-1126"}
TOP_KEEP = ("ts", "cluster_id", "recipe", "cluster", "accelerator", "counts", "story", "alerts", "vllm", "services",
            "buffer", "cost", "errors")
NODE_KEEP = ("name", "role", "instance_type", "ready", "created", "alloc_cpu", "ncpu", "cpu_model", "l3_kib",
             "mem_used_gib", "eligible", "ttft_p99_ms", "ttft_p50_ms", "achieved_rps")


def log(msg):
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)


def lean(st: dict) -> dict:
    out = {k: st.get(k) for k in TOP_KEEP}
    nodes = []
    for n in st.get("nodes") or []:
        m = {k: n.get(k) for k in NODE_KEEP}
        m["cpus"] = [{"id": c["id"], "core": c.get("core"), "thread": c.get("thread"), "llc": c.get("llc"),
                      "cat": c.get("cat"), "busy": c.get("busy"),
                      "owner": {"app": c["owner"].get("app"), "tier": c["owner"].get("tier")} if c.get("owner") else None}
                     for c in n.get("cpus") or []]
        m["n_pods"] = len(n.get("pods") or [])
        nodes.append(m)
    out["nodes"] = nodes
    lg = st.get("loadgen") or {}
    out["serving"] = {k: lg.get(k) for k in ("target_rps", "achieved_rps", "ttft_p50_ms", "ttft_p99_ms", "e2e_p99_ms",
                                              "inflight", "err", "window_s")}
    out["serving"]["per_node"] = {n: {k: v.get(k) for k in ("ttft_p50_ms", "ttft_p99_ms", "achieved_rps", "inflight")}
                                  for n, v in (lg.get("per_node") or {}).items()}
    sc = st.get("scenario") or {}
    out["scenario"] = {k: sc.get(k) for k in ("name", "status", "step_index", "n_steps", "current_note", "elapsed_s")}
    demo = st.get("demo") or {}
    out["demo"] = {k: demo.get(k) for k in ("hpa_min", "fast_scale", "accelerator_active")}
    return out


class Rec:
    def __init__(self, a):
        self.a, self.cl, self.api = a, a.cluster, a.api.rstrip("/")
        self.last: dict = {}
        self.phase = "settle"
        self.recording = False
        self.stop = False
        self.events: list[dict] = []
        out = Path(a.out)
        out.mkdir(parents=True, exist_ok=True)
        self.path = out / f"{self.cl}_{a.scenario}.jsonl"
        self.ev_path = out / f"{self.cl}_{a.scenario}.events.json"
        self.fh = None

    # ---------------------------------------------------------------- io
    def http(self, path, body=None, timeout=30):
        sep = "&" if "?" in path else "?"
        req = urllib.request.Request(f"{self.api}{path}{sep}cluster={self.cl}",
                                     data=None if body is None else json.dumps(body).encode(),
                                     method="GET" if body is None else "POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"null")

    def kubectl(self, *args):
        return subprocess.run(["kubectl", "--context", CTX[self.cl], *args], capture_output=True, text=True,
                              check=True).stdout

    def event(self, what, **kw):
        e = {"t": time.time(), "phase": self.phase, "what": what, **kw}
        self.events.append(e)
        json.dump(self.events, open(self.ev_path, "w"), indent=1)
        log(f"{self.cl} {what} {kw}")

    # ---------------------------------------------------------------- actions
    def demand(self, s, t, a):
        self.http("/api/demand", {"sandbox": s, "toolcall": t, "analytics": a})
        self.event("demand", demand=[s, t, a])

    def gpu_rate(self, rate):  # via the demo (its fast-scale sets HPA min 1 for rate <= 700)
        self.http("/api/gpu-rate", {"rate": rate})
        self.event("gpu_rate", rate=rate)

    def loadgen_rate(self, rate):  # straight to the loadgen pods: no HPA side effects
        pods = self.kubectl("-n", "inference", "get", "pods", "-l", "app=loadgen", "-o",
                            "jsonpath={.items[*].metadata.name}").split()
        for p in pods:
            subprocess.run(["kubectl", "--context", CTX[self.cl], "create", "--raw",
                            f"/api/v1/namespaces/inference/pods/{p}:8080/proxy/rate", "-f", "-"],
                           input=json.dumps({"rate": float(rate)}), capture_output=True, text=True, check=True)
        self.event("loadgen_rate", rate=rate)

    def hpa_min(self, mn):
        self.kubectl("-n", "inference", "patch", "hpa", "vllm", "-p", json.dumps({"spec": {"minReplicas": mn}}))
        self.event("hpa_min", min=mn)

    def scenario(self, name, timeout=1800):
        self.http(f"/api/scenario/{name}", {})
        self.event("scenario_start", name=name)
        t0 = time.time()
        time.sleep(4)
        while time.time() - t0 < timeout:
            sc = self.http("/api/scenario/status")
            if sc.get("status") not in ("running", "starting"):
                self.event("scenario_end", name=name, status=sc.get("status"), error=sc.get("error"),
                           elapsed_s=round(time.time() - t0, 1))
                return sc
            time.sleep(2)
        self.event("scenario_timeout", name=name)
        self.http("/api/scenario/cancel", {})
        return None

    # ---------------------------------------------------------------- sampling
    def run(self):
        while not self.stop:
            t0 = time.time()
            try:
                st = self.http("/api/state", timeout=20)
                if "error" in st and "nodes" not in st:
                    raise RuntimeError(st["error"])
                self.last = st
                if self.recording:
                    self.fh.write(json.dumps({"t": round(t0, 2), "phase": self.phase, "state": lean(st)},
                                             separators=(",", ":")) + "\n")
                    self.fh.flush()
            except Exception as e:  # noqa: BLE001
                log(f"sample error {type(e).__name__}: {e}")
            time.sleep(max(0.1, self.a.interval - (time.time() - t0)))

    def wait(self, pred, timeout, what):
        t0 = time.time()
        while time.time() - t0 < timeout:
            st = self.last
            if st and st.get("ts", 0) >= t0 and pred(st):
                self.event("ok", cond=what, after_s=round(time.time() - t0, 1))
                return True
            time.sleep(1)
        self.event("timeout", cond=what)
        return False

    def start_recording(self):
        self.fh = open(self.path, "w")
        self.recording = True
        self.event("record_start")


def settled(st, want_vllm=1):
    sv = st["services"]
    return (st["vllm"]["ready"] == want_vllm and st["vllm"]["desired"] == want_vllm and st["counts"]["g4"] == want_vllm
            and all(v["backfill"]["ready"] == v["backfill"]["desired"] - v["backfill"].get("waiting", 0) for v in sv.values())
            and all((v["overflow"]["ready"] or 0) == (v["overflow"]["desired"] or 0) for v in sv.values())
            and all((v["overflow"]["desired"] or 0) == v["backfill"].get("waiting", 0)
                    for v in sv.values() if v.get("class") == "integer"))


def bf_waiting(st):
    return sum(v["backfill"].get("waiting", 0) for v in st["services"].values())


def accel_nodes(st):
    return st["counts"]["g4"]


def scale_in(R: Rec, drop):
    """Load drops; wait for the HPA (120 s stabilization + metric lag) and the cluster autoscaler. The demo's
    gpu-scale-in scenario allows only 120 s for vLLM 2 -> 1, which the native G4 HPA overran once."""
    drop()
    R.wait(lambda s: s["vllm"]["desired"] <= 1, 600, "vLLM desired 1")
    R.wait(lambda s: accel_nodes(s) <= 1, 1500, "accelerator node removed")


def drive(R: Rec):
    a = R.a
    tpu = R.cl == "tpu"
    if a.scenario == "steady":
        R.demand(10, 10, 2)
        R.gpu_rate(300)
        R.wait(lambda s: settled(s), 900, "settled at 10/10/2, 300 rps")
        time.sleep(20)
        R.phase = "steady"
        R.start_recording()
        time.sleep(a.duration or 240)
    elif a.scenario == "cpu-spike":
        R.demand(9, 9, 0)
        R.gpu_rate(300)
        R.wait(lambda s: settled(s), 900, "settled at 9/9/0, 300 rps")
        time.sleep(20)
        R.phase = "before"
        R.start_recording()
        time.sleep(30)
        R.phase = "scenario"
        R.scenario("cpu-scale-out", 1200)
        R.phase = "after"
        time.sleep(30)
    elif a.scenario == "llm-scale-in":  # resume a llm-scale run whose scale-in did not finish (no settle)
        R.phase = "scale-in"
        R.start_recording()
        scale_in(R, (lambda: (R.loadgen_rate(300), R.hpa_min(1))) if tpu else (lambda: R.gpu_rate(300)))
        R.phase = "after"
        time.sleep(60)
    elif a.scenario == "llm-scale":
        R.demand(19, 19, 0)
        R.gpu_rate(300)
        R.wait(lambda s: settled(s), 1200, "settled at 19/19/0, 300 rps, 1 vLLM")
        time.sleep(20)
        R.phase = "before"
        R.start_recording()
        time.sleep(30)
        if not tpu:
            R.phase = "scale-out"
            R.scenario("gpu-scale-out", 1800)
            R.phase = "scaled-out"
            time.sleep(60)
            R.phase = "scale-in"
            scale_in(R, lambda: R.gpu_rate(300))
        else:
            R.phase = "scale-out"
            R.hpa_min(2)
            R.loadgen_rate(600)
            R.wait(lambda s: s["vllm"]["ready"] >= 2, 1800, "2nd vLLM Ready")
            R.wait(lambda s: bf_waiting(s) == 0 or sum(v["backfill"]["ready"] for v in s["services"].values()) >= 32,
                   300, "new accelerator host filled with backfill")
            R.phase = "scaled-out"
            time.sleep(60)
            R.phase = "scale-in"
            scale_in(R, lambda: (R.loadgen_rate(300), R.hpa_min(1)))
        R.phase = "after"
        time.sleep(60)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cluster", required=True, choices=sorted(CTX))
    ap.add_argument("--scenario", required=True, choices=("steady", "cpu-spike", "llm-scale", "llm-scale-in"))
    ap.add_argument("--out", default=str(Path(__file__).resolve().parents[2] / ".agents" / "bench" / "replays"))
    ap.add_argument("--api", default="http://127.0.0.1:8093")
    ap.add_argument("--interval", type=float, default=1.5)
    ap.add_argument("--duration", type=float, default=0, help="steady: seconds to record (default 240)")
    a = ap.parse_args()
    R = Rec(a)
    th = threading.Thread(target=R.run, daemon=True)
    th.start()
    try:
        drive(R)
    except Exception as e:  # noqa: BLE001
        R.event("error", error=f"{type(e).__name__}: {e}")
    finally:
        R.stop = True
        th.join(timeout=30)
        R.event("done", lines=sum(1 for _ in open(R.path)) if R.path.exists() else 0)


if __name__ == "__main__":
    main()
