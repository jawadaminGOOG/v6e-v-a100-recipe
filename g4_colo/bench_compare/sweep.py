#!/usr/bin/env python3
"""Side-by-side performance sweep: controller recipe (g4_colo/recipe) vs native recipe (g4_colo/recipe-native).

Runs the same phases on both clusters at the same time (one thread per cluster) and samples both every 2 s.
Raw samples -> <out>/<cluster>.samples.jsonl, phase marks -> <out>/<cluster>.marks.json. plots.py draws the charts.

Phases (identical on both clusters):
  setup     vLLM pinned to 1 replica (HPA min=max=1), analytics 0, demand 0/0, 450 rps, warm-up
  backfill  TTFT vs backfill pods on the vLLM node: 0, 4, 8, 12, 16, 18 pods at 450 rps (90 s window each)
  load      TTFT vs offered load, solo (0 pods) and full (18 pods): 200, 450, 700, 1000, 1200 rps (60 s each)
  step      CPU demand step at 450 rps: 9/9 (fits G4) -> 20/20 (22 overflow) for 240 s -> 9/9 for 300 s
  gpu       GPU scale-out/in: HPA min 1 max 2, demand 18/18, 1200 rps until 2 vLLM Ready + 120 s, then 300 rps
            until a G4 node is removed (max 900 s)
  restore   puts back each cluster's pre-sweep demand, rate and HPA min/max (or the profile's "restore" values)

The numbers above are the G4 profile. `--clusters tpu` runs the same phases on the TPU v6e native cluster with
V6E_PROFILE (16 backfill slots, rates scaled to one v6e replica, forced HPA min 2 for the scale-out). `--cpu-1s`
adds <cluster>.cpus.jsonl: per-CPU busy % of the vLLM node every second (for heatmaps).

    python3 sweep.py --out ../../.agents/bench/<run> [--phases setup,backfill,load,step,gpu] [--clusters tpu --cpu-1s]
"""
from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

L3 = [set(range(0, 8)) | set(range(24, 32)), set(range(8, 16)) | set(range(32, 40)),
      set(range(16, 24)) | set(range(40, 48))]  # g4-standard-48 (EPYC 9B45): 3 CCX x 8 cores + SMT siblings
# ct6e-standard-1t (EPYC 9B14, 44 vCPU): 22 cores in L3 groups of 8/7/7, SMT sibling of cpu N is N+22 (sysfs).
L3_V6E = [set(range(0, 8)) | set(range(22, 30)), set(range(8, 15)) | set(range(30, 37)),
          set(range(15, 22)) | set(range(37, 44))]

# Per-cluster experiment profile. G4 values are the original sweep1 constants; the TPU v6e profile is scaled to the
# v6e host (16 backfill slots, not 18) and to one v6e vLLM replica's throughput (~330 completed rps, so the vLLM
# HPA's 700 rps/pod target can never be reached by load alone: gpu_mode "force" raises HPA minReplicas to 2 instead,
# exactly what the demo's fast-scale switch does).
G4_PROFILE = {"rate": 450, "backfill": (0, 4, 8, 12, 16, 18), "load_pods": (0, 18), "load_rates": (200, 450, 700, 1000, 1200),
              "step_lo": (9, 9), "step_hi": (20, 20), "gpu_demand": (18, 18), "gpu_hi": 1200, "gpu_lo": 300,
              "gpu_mode": "hpa", "warm_ms": 100, "l3": L3}
V6E_PROFILE = {"rate": 200, "backfill": (0, 4, 8, 12, 16), "load_pods": (0, 16), "load_rates": (50, 100, 200, 300, 400),
               "step_lo": (8, 8), "step_hi": (19, 19), "gpu_demand": (16, 16), "gpu_hi": 600, "gpu_lo": 300,
               "gpu_mode": "force", "warm_ms": 400, "l3": L3_V6E,
               # restore to the demo's steady state rather than whatever was live before the sweep
               "restore": {"demand": [10, 10, 2], "hpa": [1, 2], "rate": 300.0}}
CLUSTERS = {
    "controller": {"ctx": "gke_northam-ce-mlai-tpu_us-west8_g4cluster-jawadamin-us-west8-0726", "port": 8197,
                   "recipe": "controller", "profile": G4_PROFILE},
    "native": {"ctx": "gke_northam-ce-mlai-tpu_us-west8_g4cluster-jawadamin-us-west8-1126", "port": 8196,
               "recipe": "native", "profile": G4_PROFILE},
    "tpu": {"ctx": "gke_northam-ce-mlai-tpu_us-east5_v6ecluster-jawadamin-us-east5-1126", "port": 8198,
            "recipe": "native", "profile": V6E_PROFILE},
}
SVCS = ("sandbox", "toolcall", "analytics")
BUCKET_MS = 2


def log(name, msg):
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {name:10s} {msg}", flush=True)


def parse_cpuset(s):
    out = set()
    for part in (s or "").split(","):
        if not part:
            continue
        a, _, b = part.partition("-")
        out.update(range(int(a), int(b or a) + 1))
    return out


def ready(p):
    return any(c["type"] == "Ready" and c["status"] == "True" for c in p.get("status", {}).get("conditions", []))


def merge(hs):
    hs = [h[:1001] for h in hs if h]
    return [sum(x) for x in zip(*hs)] if hs else []


def pct(h, q):
    n = sum(h)
    if not n:
        return None
    target = math.ceil(q * n)
    c = 0
    for i, v in enumerate(h):
        c += v
        if c >= target:
            return (i + 1) * BUCKET_MS if i < 1000 else 2000.0
    return 2000.0


class Cluster:
    def __init__(self, name, out: Path):
        self.name, self.ctx, self.port = name, CLUSTERS[name]["ctx"], CLUSTERS[name]["port"]
        self.recipe = CLUSTERS[name]["recipe"]
        self.P = CLUSTERS[name]["profile"]
        self.cpu_fh = open(out / f"{name}.cpus.jsonl", "a")  # per-CPU busy % of the vLLM node(s) at 1 s
        self.proxy = subprocess.Popen(["kubectl", "--context", self.ctx, "proxy", "--port", str(self.port)],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(2)
        self.fh = open(out / f"{name}.samples.jsonl", "a")
        self.marks_path = out / f"{name}.marks.json"
        self.marks: list[dict] = []
        self.samples: list[dict] = []
        self.lock = threading.Lock()
        self.stop = False

    # ------------------------------------------------------------------ io
    def http(self, path, body=None, timeout=8.0, tries=4):
        for i in range(tries):
            try:
                return self._http(path, body, timeout)
            except urllib.error.HTTPError as e:
                if e.code < 500 or i == tries - 1:
                    raise
                time.sleep(2)

    def _http(self, path, body=None, timeout=8.0):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}",
                                     data=None if body is None else json.dumps(body).encode(),
                                     method="GET" if body is None else ("PATCH" if isinstance(body, tuple) else "POST"),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"null")

    def kubectl(self, *args, tries=6):
        # retried: GKE control planes return 503s for a few seconds at a time (seen on the v6e cluster)
        for i in range(tries):
            r = subprocess.run(["kubectl", "--context", self.ctx, *args], capture_output=True, text=True)
            if r.returncode == 0:
                return r.stdout
            log(self.name, f"kubectl {' '.join(args[:4])} failed ({i + 1}/{tries}): {r.stderr.strip()[:200]}")
            time.sleep(5)
        raise subprocess.CalledProcessError(r.returncode, r.args, r.stdout, r.stderr)

    def mark(self, phase, **kw):
        m = {"t": time.time(), "phase": phase, **kw}
        self.marks.append(m)
        json.dump(self.marks, open(self.marks_path, "w"), indent=1)
        log(self.name, f"mark {phase} {kw}")

    # ------------------------------------------------------------------ controls
    def loadgen_pods(self):
        return [p["metadata"]["name"] for p in self.http("/api/v1/namespaces/inference/pods?labelSelector=app%3Dloadgen")["items"]
                if ready(p)]

    def set_rate(self, rate):
        for n in self.loadgen_pods():
            self.http(f"/api/v1/namespaces/inference/pods/{n}:8080/proxy/rate", {"rate": float(rate)})
        log(self.name, f"rate {rate}")

    def get_rate(self):
        n = self.loadgen_pods()
        return self.http(f"/api/v1/namespaces/inference/pods/{n[0]}:8080/proxy/stats").get("rate_total") if n else 0.0

    def set_demand(self, sandbox, toolcall, analytics=0):
        if self.recipe == "controller":
            pod = [p["metadata"]["name"] for p in self.http(
                "/api/v1/namespaces/colo-system/pods?labelSelector=app%3Dcolo-controller")["items"] if ready(p)][0]
            self.http(f"/api/v1/namespaces/colo-system/pods/{pod}:8080/proxy/config",
                      {"demand": {"sandbox": sandbox, "toolcall": toolcall, "analytics": analytics}})
        else:
            for dep, n in (("sandbox-backfill", sandbox), ("toolcall-backfill", toolcall),
                           ("analytics-overflow", analytics)):
                self.kubectl("-n", "colo-svc", "scale", f"deploy/{dep}", f"--replicas={n}")
        log(self.name, f"demand {sandbox}/{toolcall}/{analytics}")

    def get_demand(self):
        if self.recipe == "controller":
            pod = [p["metadata"]["name"] for p in self.http(
                "/api/v1/namespaces/colo-system/pods?labelSelector=app%3Dcolo-controller")["items"] if ready(p)][0]
            d = self.http(f"/api/v1/namespaces/colo-system/pods/{pod}:8080/proxy/config")["demand"]
            return d["sandbox"], d["toolcall"], d.get("analytics", 0)
        deps = {d["metadata"]["name"]: d["spec"].get("replicas", 0)
                for d in self.http("/apis/apps/v1/namespaces/colo-svc/deployments")["items"]}
        return deps["sandbox-backfill"], deps["toolcall-backfill"], deps.get("analytics-overflow", 0)

    def set_hpa(self, mn, mx):
        self.kubectl("-n", "inference", "patch", "hpa", "vllm", "-p",
                     json.dumps({"spec": {"minReplicas": mn, "maxReplicas": mx}}))
        log(self.name, f"vllm hpa min={mn} max={mx}")

    def get_hpa(self):
        h = json.loads(self.kubectl("-n", "inference", "get", "hpa", "vllm", "-o", "json"))
        return h["spec"].get("minReplicas", 1), h["spec"]["maxReplicas"]

    # ------------------------------------------------------------------ sampling
    def snapshot(self):
        t = time.time()
        nodes = self.http("/api/v1/nodes")["items"]
        pods = self.http("/api/v1/pods?fieldSelector=status.phase!%3DSucceeded,status.phase!%3DFailed", timeout=12)["items"]
        role = {n["metadata"]["name"]: n["metadata"]["labels"].get("colo.role") for n in nodes}
        g4 = sorted(n for n, r in role.items() if r == "g4")
        n4d = sorted(n for n, r in role.items() if r == "n4d")
        vllm = [p for p in pods if p["metadata"]["namespace"] == "inference" and p["metadata"]["labels"].get("app") == "vllm"]
        vllm_ready = sorted({p["spec"].get("nodeName") for p in vllm
                             if ready(p) and not p["metadata"].get("deletionTimestamp")} - {None})
        svc = {}
        for s in SVCS:
            bf = [p for p in pods if p["metadata"]["namespace"] == "colo-svc" and p["metadata"]["labels"].get("app") == s
                  and p["metadata"]["labels"].get("colo.tier") == "backfill"]
            of = [p for p in pods if p["metadata"]["namespace"] == "colo-svc" and p["metadata"]["labels"].get("app") == s
                  and p["metadata"]["labels"].get("colo.tier") == "overflow"]
            svc[s] = {"bf_ready": sum(ready(p) for p in bf),
                      "bf_ready_on": {n: sum(1 for p in bf if ready(p) and p["spec"].get("nodeName") == n) for n in g4},
                      "bf_pending": sum(1 for p in bf if p["status"].get("phase") == "Pending"),
                      "of_ready": sum(ready(p) for p in of), "of_total": len(of)}
        # loadgen: merge per-vLLM-pod TTFT histograms across loadgen replicas
        per_vllm, rates, errs = {}, [], 0
        for p in pods:
            if p["metadata"]["namespace"] == "inference" and p["metadata"]["labels"].get("app") == "loadgen" and ready(p):
                try:
                    st = self.http(f"/api/v1/namespaces/inference/pods/{p['metadata']['name']}:8080/proxy/stats", timeout=4)
                except Exception:  # noqa: BLE001
                    continue
                rates.append(st.get("rate_total"))
                for vp, d in (st.get("per_pod") or {}).items():
                    e = per_vllm.setdefault(vp, {"node": d.get("node"), "hists": [], "ok": 0, "err": 0})
                    e["hists"].append(d.get("ttft_hist") or [])
                    e["ok"] += d.get("ok_window") or 0
                    e["err"] += d.get("err_window") or 0
                    errs += d.get("err_window") or 0
        lg = {}
        for vp, e in per_vllm.items():
            h = merge(e["hists"])
            lg[vp] = {"node": e["node"], "p50": pct(h, 0.5), "p90": pct(h, 0.9), "p99": pct(h, 0.99), "ok": e["ok"],
                      "err": e["err"], "hist": h}
        # node agents on G4: vLLM cpuset, L3 domains, busy %
        cpu = {}
        for p in pods:
            if p["metadata"]["namespace"] == "colo-system" and p["metadata"]["labels"].get("app") == "colo-node-agent" \
                    and p["spec"].get("nodeName") in g4 and ready(p):
                try:
                    d = self.http(f"/api/v1/namespaces/colo-system/pods/{p['metadata']['name']}:8080/proxy/cpus", timeout=4)
                except Exception:  # noqa: BLE001
                    continue
                ents = (d.get("cpu_manager_state") or {}).get("entries") or {}
                vset = set()
                for v in ents.values():
                    if "vllm" in v:
                        vset |= parse_cpuset(v["vllm"])
                busy = d.get("busy") or []
                cpu[p["spec"]["nodeName"]] = {
                    "vllm_cpus": sorted(vset), "vllm_l3": sum(1 for dom in self.P["l3"] if vset & dom),
                    "vllm_busy": round(sum(busy[i] for i in vset) / len(vset), 1) if vset and busy else None,
                    "all_busy": round(sum(busy) / len(busy), 1) if busy else None,
                    "shared": (d.get("cpu_manager_state") or {}).get("defaultCpuSet"),
                    "workers": sum(1 for v in ents.values() if "worker" in v)}
        return {"t": t, "g4": g4, "n4d": n4d, "vllm_total": len(vllm), "vllm_ready_nodes": vllm_ready, "svc": svc,
                "rates": rates, "errs": errs, "lg": lg, "cpu": cpu}

    def run(self):
        while not self.stop:
            t0 = time.time()
            try:
                s = self.snapshot()
                with self.lock:
                    self.samples.append(s)
                rec = dict(s)
                rec["lg"] = {k: {kk: vv for kk, vv in v.items() if kk != "hist"} for k, v in s["lg"].items()}
                self.fh.write(json.dumps(rec) + "\n")
                self.fh.flush()
            except Exception as e:  # noqa: BLE001
                log(self.name, f"sample error {type(e).__name__}: {e}")
            time.sleep(max(0.2, 2.0 - (time.time() - t0)))

    def run_hangwatch(self, out: Path):
        """Detect a vLLM pod that stops completing requests while load is offered (seen on vLLM-TPU): 3 consecutive
        2 s samples with 0 ok and >0 errors in the loadgen window. Marks "hang" with the container restart count and
        saves the vLLM log tail; marks "hang_end" when the pod (or its replacement) serves again."""
        hung: dict[str, float] = {}
        streak: dict[str, int] = {}
        seen = 0
        while not self.stop:
            time.sleep(2)
            with self.lock:
                new = self.samples[seen:]
                seen = len(self.samples)
            for smp in new:
                for vp, d in smp["lg"].items():
                    bad = (d.get("ok") or 0) == 0 and (d.get("err") or 0) > 0
                    streak[vp] = streak.get(vp, 0) + 1 if bad else 0
                    if streak[vp] == 3 and vp not in hung:
                        hung[vp] = smp["t"]
                        try:
                            pod = json.loads(self.kubectl("-n", "inference", "get", "pod", vp, "-o", "json", tries=2))
                            restarts = sum(c.get("restartCount", 0) for c in pod["status"].get("containerStatuses", []))
                            tail = self.kubectl("-n", "inference", "logs", vp, "--tail=300", "--timestamps", tries=2)
                        except Exception as e:  # noqa: BLE001
                            restarts, tail = None, f"log capture failed: {e}"
                        f = out / f"hang_{vp}_{int(smp['t'])}.log"
                        f.write_text(tail)
                        cpu = smp["cpu"].get(d.get("node") or "", {})
                        self.mark("hang", pod=vp, node=d.get("node"), since=smp["t"] - 4, restarts=restarts, log=f.name,
                                  bf_pods=sum(v["bf_ready_on"].get(d.get("node"), 0) for v in smp["svc"].values()),
                                  rate=(smp["rates"] or [None])[0], vllm_busy=cpu.get("vllm_busy"),
                                  all_busy=cpu.get("all_busy"))
                    elif vp in hung and (d.get("ok") or 0) > 0:
                        info = {}
                        try:
                            pod = json.loads(self.kubectl("-n", "inference", "get", "pod", vp, "-o", "json", tries=2))
                            cs = (pod["status"].get("containerStatuses") or [{}])[0]
                            info = {"restarts": cs.get("restartCount"),
                                    "last_terminated": ((cs.get("lastState") or {}).get("terminated") or {}).get("finishedAt"),
                                    "last_reason": ((cs.get("lastState") or {}).get("terminated") or {}).get("reason"),
                                    "started_at": ((cs.get("state") or {}).get("running") or {}).get("startedAt")}
                        except Exception:  # noqa: BLE001
                            pass
                        self.mark("hang_end", pod=vp, hung_s=round(smp["t"] - hung.pop(vp), 1), **info)
                # a hung pod replaced by a new one (liveness restart keeps the name; a delete does not)
                for vp in [v for v in hung if v not in smp["lg"]]:
                    self.mark("hang_end", pod=vp, hung_s=round(smp["t"] - hung.pop(vp), 1), reason="pod gone")

    def run_cpus(self):
        """1 s per-CPU busy % of every node running a Ready vLLM pod (node agent /cpus; its busy % is a 2 s window
        refreshed every 0.5 s). Also records the CPU-manager owner of each CPU so a heatmap can label vLLM rows."""
        while not self.stop:
            t0 = time.time()
            try:
                s = self.samples[-1] if self.samples else None
                nodes = set(s["vllm_ready_nodes"]) if s else set()
                pods = self.http("/api/v1/namespaces/colo-system/pods?labelSelector=app%3Dcolo-node-agent", timeout=4)["items"]
                for p in pods:
                    node = p["spec"].get("nodeName")
                    if node not in nodes or not ready(p):
                        continue
                    d = self.http(f"/api/v1/namespaces/colo-system/pods/{p['metadata']['name']}:8080/proxy/cpus", timeout=3)
                    ents = (d.get("cpu_manager_state") or {}).get("entries") or {}
                    excl = {}
                    for uid, conts in ents.items():
                        for cname, cs in conts.items():
                            for c in parse_cpuset(cs):
                                excl[c] = cname
                    self.cpu_fh.write(json.dumps({"t": round(t0, 2), "node": node, "busy": d.get("busy"),
                                                  "window_s": d.get("window_s"), "owner": excl,
                                                  "pods_bf": bf_on_vllm_node(s) if s else None}) + "\n")
                self.cpu_fh.flush()
            except Exception as e:  # noqa: BLE001
                log(self.name, f"cpu sample error {type(e).__name__}: {e}")
            time.sleep(max(0.05, 1.0 - (time.time() - t0)))

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
                log(self.name, f"ok: {what} ({time.time() - t0:.0f}s)")
                return time.time()
            time.sleep(1)
        log(self.name, f"TIMEOUT: {what}")
        return None

    def pooled(self, t0, t1):
        """TTFT percentiles from non-overlapping 30 s loadgen windows ending in [t0+30, t1] (wave-2 method)."""
        with self.lock:
            win = [s for s in self.samples if t0 <= s["t"] <= t1 + 1]
        picks, cur = [], t0 + 30
        while cur <= t1 + 1:
            cand = [s for s in win if s["t"] >= cur - 1]
            if not cand:
                break
            picks.append(cand[0])
            cur = cand[0]["t"] + 30
        h = merge([v["hist"] for s in picks for v in s["lg"].values()])
        return {"p50": pct(h, 0.5), "p90": pct(h, 0.9), "p99": pct(h, 0.99), "requests": sum(h), "windows": len(picks)}


def vllm_node(s):
    return s["vllm_ready_nodes"][0] if s["vllm_ready_nodes"] else None


def bf_on_vllm_node(s):
    n = vllm_node(s)
    return sum(v["bf_ready_on"].get(n, 0) for v in s["svc"].values()) if n else 0


def warm(C):
    ms = C.P["warm_ms"]
    return C.wait(lambda s: all((v["p99"] or 9999) < ms for x in C.samples[-30:] for v in x["lg"].values())
                  and len(C.samples) >= 30 and all(x["lg"] for x in C.samples[-30:]), 600, f"warm: 60 s of p99 < {ms} ms")


def phase_setup(C):
    C.mark("setup")
    C.set_hpa(1, 1)
    C.set_demand(0, 0, 0)
    C.set_rate(C.P["rate"])
    C.wait(lambda s: s["vllm_total"] == 1 and len(s["vllm_ready_nodes"]) == 1, 900, "1 vLLM Ready")
    warm(C)


def phase_backfill(C):
    C.set_rate(C.P["rate"])
    for n in C.P["backfill"]:
        C.set_demand((n + 1) // 2, n // 2, 0)
        C.wait(lambda s, n=n: bf_on_vllm_node(s) == n, 300, f"{n} backfill pods on the vLLM node")
        time.sleep(30)
        t0 = time.time()
        time.sleep(90)
        res = C.pooled(t0, time.time())
        s = C.last()
        cpu = s["cpu"].get(vllm_node(s) or "", {})
        C.mark("backfill", pods=bf_on_vllm_node(s), rate=C.P["rate"], t0=t0, t1=time.time(), ttft=res,
               vllm_l3=cpu.get("vllm_l3"), vllm_busy=cpu.get("vllm_busy"), all_busy=cpu.get("all_busy"))


def phase_load(C):
    for n in C.P["load_pods"]:
        C.set_demand((n + 1) // 2, n // 2, 0)
        C.wait(lambda s, n=n: bf_on_vllm_node(s) == n, 300, f"{n} backfill pods")
        for rate in C.P["load_rates"]:
            C.set_rate(rate)
            time.sleep(30)
            t0 = time.time()
            time.sleep(60)
            res = C.pooled(t0, time.time())
            s = C.last()
            cpu = s["cpu"].get(vllm_node(s) or "", {})
            C.mark("load", pods=n, rate=rate, t0=t0, t1=time.time(), ttft=res, errs=s["errs"],
                   vllm_busy=cpu.get("vllm_busy"))
        # past saturation the client-side backlog keeps TTFT pegged for a while: let it drain before moving on
        C.set_rate(C.P["rate"])
        warm(C)
    C.set_rate(C.P["rate"])


def covered(s, d):
    return all(s["svc"][k]["bf_ready"] + s["svc"][k]["of_ready"] >= v for k, v in d.items())


def phase_step(C):
    (lo_s, lo_t), (hi_s, hi_t) = C.P["step_lo"], C.P["step_hi"]
    C.set_rate(C.P["rate"])
    C.set_demand(lo_s, lo_t, 0)
    C.wait(lambda s: covered(s, {"sandbox": lo_s, "toolcall": lo_t}), 600, f"{lo_s}/{lo_t} covered")
    time.sleep(30)
    t_up = time.time()
    C.mark("step_up", demand=[hi_s, hi_t], demand_lo=[lo_s, lo_t])
    C.set_demand(hi_s, hi_t, 0)
    t_cov = C.wait(lambda s: covered(s, {"sandbox": hi_s, "toolcall": hi_t}), 600, f"{hi_s}/{hi_t} covered")
    time.sleep(max(0, 240 - (time.time() - t_up)))
    t_down = time.time()
    C.mark("step_down", demand=[lo_s, lo_t], covered_after_s=t_cov and round(t_cov - t_up, 1))
    C.set_demand(lo_s, lo_t, 0)
    t_drain = C.wait(lambda s: all(s["svc"][k]["of_total"] == 0 for k in ("sandbox", "toolcall")), 300,
                     "overflow drained")
    time.sleep(max(0, 300 - (time.time() - t_down)))
    C.mark("step_end", drained_after_s=t_drain and round(t_drain - t_down, 1))


def phase_gpu(C):
    ds, dt = C.P["gpu_demand"]
    force = C.P["gpu_mode"] == "force"
    C.set_hpa(1, 2)
    C.set_rate(C.P["rate"])
    C.set_demand(ds, dt, 0)
    C.wait(lambda s: len(s["vllm_ready_nodes"]) == 1 and covered(s, {"sandbox": ds, "toolcall": dt}), 900,
           f"1 vLLM, {ds}/{dt} covered")
    time.sleep(10)
    first = set(C.last()["g4"])
    t0 = time.time()
    C.mark("gpu_out", rate=C.P["gpu_hi"], demand=[ds, dt], mode=C.P["gpu_mode"])
    if force:  # the demo's fast-scale: HPA minReplicas 2 at the same moment as the load spike
        C.set_hpa(2, 2)
    C.set_rate(C.P["gpu_hi"])
    t_v2 = C.wait(lambda s: len(s["vllm_ready_nodes"]) >= 2, 1500, "2nd vLLM Ready")
    t_n2 = next((s["t"] for s in C.samples if s["t"] >= t0 and len(s["g4"]) > len(first)), None)
    time.sleep(C.P.get("gpu_hold_s", 120))
    t1 = time.time()
    C.mark("gpu_in", rate=C.P["gpu_lo"], vllm2_ready_after_s=t_v2 and round(t_v2 - t0, 1),
           node_added_after_s=t_n2 and round(t_n2 - t0, 1), new_nodes=sorted(set(C.last()["g4"]) - first))
    if force:
        C.set_hpa(1, 2)
    C.set_rate(C.P["gpu_lo"])
    g4_now = len(C.last()["g4"])
    t_gone = C.wait(lambda s: len(s["g4"]) < g4_now, 1200, "an accelerator node removed")
    time.sleep(30)
    C.mark("gpu_end", node_removed_after_s=t_gone and round(t_gone - t1, 1))


def phase_soak(C):
    """Long fixed-load windows to see whether vLLM-TPU hangs correlate with backfill: P["soak_pods"] x soak_s at
    soak_rate. TTFT pooled over the whole window; hangs are marked by run_hangwatch."""
    C.set_rate(C.P.get("soak_rate", 100))
    for n in C.P.get("soak_pods", (0, 16)):
        C.set_demand((n + 1) // 2, n // 2, 0)
        C.wait(lambda s, n=n: bf_on_vllm_node(s) == n, 300, f"{n} backfill pods")
        t0 = time.time()
        C.mark("soak_start", pods=n, rate=C.P.get("soak_rate", 100))
        time.sleep(C.P.get("soak_s", 600))
        res = C.pooled(t0, time.time())
        hangs = [m for m in C.marks if m["phase"] == "hang" and m["t"] >= t0]
        C.mark("soak", pods=n, rate=C.P.get("soak_rate", 100), t0=t0, t1=time.time(), ttft=res, hangs=len(hangs))
    C.set_rate(C.P["rate"])


PHASES = {"soak": phase_soak, "setup": phase_setup, "backfill": phase_backfill, "load": phase_load, "step": phase_step, "gpu": phase_gpu}


def drive(C, phases, saved):
    try:
        for ph in phases:
            log(C.name, f"== {ph}")
            PHASES[ph](C)
    except Exception as e:  # noqa: BLE001
        log(C.name, f"phase failed: {type(e).__name__}: {e}")
        C.mark("error", error=str(e))
    finally:
        try:
            C.set_demand(*saved["demand"])
            C.set_hpa(*saved["hpa"])
            C.set_rate(saved["rate"])
            C.mark("restored", **saved)
        except Exception as e:  # noqa: BLE001
            log(C.name, f"RESTORE FAILED: {e}  saved={saved}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--phases", default="setup,backfill,load,step,gpu")
    ap.add_argument("--clusters", default="controller,native", help=f"comma list of {sorted(CLUSTERS)}")
    ap.add_argument("--override", default="", help='JSON merged into every selected profile, e.g. {"load_pods": [16]}')
    ap.add_argument("--cpu-1s", action="store_true", help="also record per-CPU busy %% of the vLLM node at 1 s")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    cl = [Cluster(n, out) for n in args.clusters.split(",")]
    if args.override:
        for C in cl:
            C.P = {**C.P, **json.loads(args.override)}
            log(C.name, f"profile override {args.override}")
    saved = {}
    for C in cl:
        saved[C.name] = {"demand": list(C.get_demand()), "hpa": list(C.get_hpa()), "rate": C.get_rate()}
        log(C.name, f"saved pre-sweep state {saved[C.name]}")
    json.dump(saved, open(out / "pre_sweep_state.json", "w"), indent=1)
    samplers = [threading.Thread(target=C.run, daemon=True) for C in cl]
    samplers += [threading.Thread(target=C.run_hangwatch, args=(out,), daemon=True) for C in cl]
    if args.cpu_1s:
        samplers += [threading.Thread(target=C.run_cpus, daemon=True) for C in cl]
    for t in samplers:
        t.start()
    restore = {C.name: C.P.get("restore", saved[C.name]) for C in cl}
    drivers = [threading.Thread(target=drive, args=(C, args.phases.split(","), restore[C.name])) for C in cl]
    for t in drivers:
        t.start()
    for t in drivers:
        t.join()
    time.sleep(10)
    for C in cl:
        C.stop = True
        C.proxy.terminate()
    log("all", f"done -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
