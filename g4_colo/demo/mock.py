"""MOCK backend (DEMO_MOCK=1): a small time-compressed simulator of the colo
cluster that emits the *same raw formats* as the live fetcher (k8s objects
simplified to dicts, node-agent /cpus with a kubelet cpu_manager_state,
loadgen /stats histograms, controller /state). The real aggregation code
(state.build_state) then runs on top, so the UI and the scenarios exercise the
production code path without a cluster.

Behaviour (deliberately plausible, not calibrated):
  * HPA: desired vLLM = ceil(running / 100), running = rate * 0.15 s (Little).
  * Pending vLLM -> CA TriggeredScaleUp -> new G4 node Ready after G4_BOOT_S,
    vLLM Ready VLLM_BOOT_S later. Scale-in after HPA_DOWN_S, node removed
    after CA_DOWN_S (backfill pods are expendable, do not block).
  * Controller: 19 two-CPU backfill slots per eligible G4 node, rest overflow
    on N4D (7 slots/node) + warm buffer; N4D nodes boot in N4D_BOOT_S.
  * TTFT p99 per node grows with per-replica load and with the number of
    memory-heavy backfill pods on that node (integer pods barely matter).
  * Guard: block memory-heavy (evict) when p99 > factor x baseline, then
    integer after 60 s; release after cooldown.
"""

from __future__ import annotations

import json
import math
import os
import random
import string
import time
from datetime import datetime, timezone

from state import (CLUSTER, validate_worker_replicas, GUARD_NA_REASON, NATIVE_DEMAND_DEPLOYMENT, SERVICES, UnsupportedOnRecipe, accel_profile, actor_load_patch,
                   build_state)

G4_BOOT_S = 75
VLLM_BOOT_S = 45
N4D_BOOT_S = 40
HPA_DOWN_S = 60
CA_DOWN_S = 90
GUARD_COOLDOWN_S = 120
G4_ALLOC_CPU = 46.0
N4D_ALLOC_CPU = 15.0
G4_SLOTS = 19  # (46 - 8 vLLM) / 2
N4D_SLOTS = 7
BUCKET_MS = 2
NBUCKETS = 1001  # 2 ms to 2 s + overflow
WINDOW_S = 30
LOADGEN_REPLICAS = 4


def _sfx(n: int = 5) -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, tz=timezone.utc).isoformat()


def _lognormal_hist(total: int, p50: float, p99: float) -> list[int]:
    if total <= 0:
        return [0] * NBUCKETS
    mu = math.log(max(p50, 1.0))
    sigma = max(0.05, math.log(max(p99, p50 + 1) / max(p50, 1.0)) / 2.326)
    out = []
    prev = 0.0
    for i in range(NBUCKETS - 1):
        x = (i + 1) * BUCKET_MS
        cdf = 0.5 * (1 + math.erf((math.log(x) - mu) / (sigma * math.sqrt(2))))
        out.append(cdf - prev)
        prev = cdf
    out.append(1 - prev)
    counts = [int(round(p * total)) for p in out]
    return counts



# Measured on the live nodes (sysfs cache/index3/shared_cpu_list): physical cores per L3 domain.
MOCK_L3_CORES = {"g4": [8, 8, 8], "tpu-v6e": [8, 7, 7], "n4d": [8]}
MOCK_CPU_MODEL = {"g4": "AMD EPYC 9B45", "tpu-v6e": "AMD EPYC 9B14", "n4d": "AMD EPYC 9B45"}


def _mock_topology(ncpu: int, kind: str) -> dict:
    """Node-agent topology payload: siblings (c, c+ncpu/2), L3 domains as measured for this machine type."""
    half = ncpu // 2
    sizes = MOCK_L3_CORES.get(kind) if sum(MOCK_L3_CORES.get(kind, [])) == half else [8] * (half // 8) + ([half % 8] if half % 8 else [])
    llc_of, core = {}, 0
    for l, k in enumerate(sizes):
        for _ in range(k):
            llc_of[core] = l
            core += 1
    return {"model": MOCK_CPU_MODEL.get(kind), "l3_kib": 32768,
            "cpus": [{"id": c, "core": c % half, "thread": c // half, "llc": llc_of[c % half]} for c in range(ncpu)]}

class MockBackend:
    mode = "mock"
    recipe = "controller"
    POOL_TAG = "jawadamin-us-west8-1026"

    def __init__(self, cluster_id: str = "controller", cluster_info: dict | None = None):
        self.cluster_id = cluster_id
        self.cluster_info = cluster_info
        # Accelerator host to emulate (cluster_info["accelerator"]: g4 or tpu-v6e). Its vCPU count sizes the core map,
        # allocatable and backfill slots: g4-standard-48 -> 46 allocatable, 19 slots; ct6e-standard-1t -> 42, 17.
        self.accel = accel_profile(cluster_info)
        self.g4_half = self.accel["vcpu"] // 2
        self.g4_alloc = float(self.accel["vcpu"] - 2)
        self.g4_slots = int((self.g4_alloc - 8) // 2)
        now = time.time()
        self.t = now
        self.rate = 300.0
        self.eff_rate = 300.0
        self.demand = {"sandbox": 6, "toolcall": 6, "analytics": 2}
        self.guard = {"enabled": True, "baseline_ttft_p99_ms": 60.0, "factor": 1.5, "cooldown_s": GUARD_COOLDOWN_S, "window_s": 30, "escalate_s": 0}
        self.buffer_replicas = 1
        self.events: list[dict] = []
        self.guard_events: list[dict] = []
        self.nodes: dict[str, dict] = {}
        self._add_node("loadgen", "us-west8-a", now - 86400, ready=True)
        self._add_node("g4", "us-west8-a", now - 7200, ready=True)
        self._add_node("n4d", "us-west8-a", now - 3600, ready=True)
        self.vllm: list[dict] = []
        g4 = next(n for n in self.nodes.values() if n["role"] == "g4")
        self.vllm.append({"name": f"vllm-6f9c7d8b5-{_sfx()}", "uid": _sfx(32), "node": g4["name"], "ready_at": now - 7000})
        self.hpa_desired = 1
        self.hpa_min = 1
        self.fast_scale = os.environ.get("DEMO_FAST_SCALE", "1") == "1"
        self.fast_scale_forced = False
        self.hpa_low_since: float | None = None
        self.svc_pods: list[dict] = []  # {name, uid, app, tier, node|None, ready_at}
        self.buffer_pods: list[dict] = []
        self.loadgen_pods = [f"loadgen-7d4b9c6f8-{_sfx()}" for _ in range(LOADGEN_REPLICAS)]
        self.node_p99: dict[str, float] = {}
        self.node_p50: dict[str, float] = {}
        self.node_rps: dict[str, float] = {}
        self.node_ttft_rps: dict[str, float] = {}
        self.pending_g4_since: float | None = None
        self.pending_n4d_since: float | None = None
        self._step(now, force=True)
        for p in self.svc_pods + self.buffer_pods:  # start from a settled cluster
            if p["node"]:
                p["ready_at"] = now - 600

    # ------------------------------------------------------------ helpers
    def _add_node(self, role: str, zone: str, created: float, ready: bool = False, ready_at: float | None = None) -> dict:
        tpu = self.accel["id"] == "tpu-v6e"
        pool = self._pool(role)
        name = f"gke-{'v6ecluster' if tpu else 'g4cluster'}-{pool}-{self.POOL_TAG}-{_sfx(4)}-{_sfx(4)}"
        cc = {"g4": "g4-gpu", "n4d": "n4d-cpu"}.get(role)  # the TPU pool keeps the recipe's class identifiers
        labels = {
            "colo.role": role,
            "topology.kubernetes.io/zone": zone,
            "node.kubernetes.io/instance-type": {"g4": self.accel["machine"], "n4d": "n4d-standard-16", "loadgen": "e2-standard-8"}[role],
        }
        if cc:
            labels["cloud.google.com/compute-class"] = cc
        if tpu and role == "g4":
            labels["cloud.google.com/gke-tpu-accelerator"] = "tpu-v6e-slice"
            labels["cloud.google.com/gke-tpu-topology"] = "1x1"
        n = {
            "name": name,
            "role": role,
            "zone": zone,
            "created": created,
            "ready_at": created if ready else (ready_at or created + (G4_BOOT_S if role == "g4" else N4D_BOOT_S)),
            "labels": labels,
            "blocks": {},  # class -> since
            "breach_since": None,
            "healthy_since": None,
            "unneeded_since": None,
            "cores": {},  # core -> pod uid
        }
        self.nodes[name] = n
        return n

    def _pool(self, role: str) -> str:
        if role == "g4":
            return "v6etpu" if self.accel["id"] == "tpu-v6e" else "g4gpu"
        return {"n4d": "n4dcpu", "loadgen": "loadgen"}[role]

    def _event(self, reason: str, kind: str, obj: str, msg: str, ns: str | None = None, source: str = "cluster-autoscaler"):
        self.events.append({"time": _iso(self.t), "reason": reason, "kind": kind, "object": obj, "namespace": ns, "message": msg, "source": source, "count": 1})
        self.events = self.events[-200:]

    def _ready(self, n: dict) -> bool:
        return self.t >= n["ready_at"]

    def _free_core(self, node: dict) -> int | None:
        ncores = self.g4_half if node["role"] == "g4" else 8
        for c in range(1, ncores):
            if c not in node["cores"]:
                return c
        return None

    def _node_users(self, node: str) -> list[dict]:
        """Pods (simulator records with a "tier") that keep an N4D node from being scaled down."""
        return [p for p in self.svc_pods + self.buffer_pods if p["node"] == node]

    def _release(self, uid: str):
        for n in self.nodes.values():
            for c in [c for c, u in n["cores"].items() if u == uid]:
                del n["cores"][c]

    # ------------------------------------------------------------ simulation
    def _step(self, now: float, force: bool = False):
        dt = now - self.t
        if dt < 0.5 and not force:
            return
        self.t = now
        a = 1 - math.exp(-dt / 8.0)
        self.eff_rate += (self.rate - self.eff_rate) * a

        g4s = [n for n in self.nodes.values() if n["role"] == "g4"]
        n4ds = [n for n in self.nodes.values() if n["role"] == "n4d"]

        # --- HPA
        running = self.eff_rate * 0.15
        want = max(1, min(2, math.ceil(running / 100.0)))
        if want > self.hpa_desired:
            self.hpa_desired = want
            self.hpa_low_since = None
        elif want < self.hpa_desired:
            self.hpa_low_since = self.hpa_low_since or now
            if now - self.hpa_low_since >= HPA_DOWN_S:
                self.hpa_desired = want
                self.hpa_low_since = None
        else:
            self.hpa_low_since = None

        # --- vLLM placement
        while len(self.vllm) > self.hpa_desired:
            v = self.vllm.pop()
            self._release(v["uid"])
        while len(self.vllm) < self.hpa_desired:
            self.vllm.append({"name": f"vllm-6f9c7d8b5-{_sfx()}", "uid": _sfx(32), "node": None, "ready_at": None})
        for v in self.vllm:
            if v["node"] is None:
                used = {x["node"] for x in self.vllm if x["node"]}
                free = [n for n in g4s if self._ready(n) and n["name"] not in used]
                if free:
                    node = free[0]
                    v["node"] = node["name"]
                    v["ready_at"] = now + VLLM_BOOT_S
                    # vLLM is Guaranteed 8 CPU: take cores 1-4; preempt backfill there.
                    for c in range(1, 5):
                        uid = node["cores"].get(c)
                        if uid:
                            self._preempt(uid, v["name"])
                        node["cores"][c] = v["uid"]
                    self.pending_g4_since = None
                else:
                    booting = [n for n in g4s if not self._ready(n)]
                    if not booting and len(g4s) < 2:
                        self._event("TriggeredScaleUp", "Pod", v["name"], f"pod triggered scale-up: [{{{self._pool('g4')}-{self.POOL_TAG} 1->2 (max: 2)}}]", "inference")
                        self._add_node("g4", "us-west8-a", now)
                    elif not booting and self.pending_g4_since is None:
                        self.pending_g4_since = now
                        self._event("NotTriggerScaleUp", "Pod", v["name"], "pod didn't trigger scale-up: 1 max node group size reached", "inference")
        # vLLM pod cores on node (ensure)
        for v in self.vllm:
            if v["node"]:
                for c in range(1, 5):
                    self.nodes[v["node"]]["cores"][c] = v["uid"]

        # --- CA scale-down of G4 nodes without vLLM
        vnodes = {v["node"] for v in self.vllm if v["node"]}
        for n in list(g4s):
            if self._ready(n) and n["name"] not in vnodes and len(g4s) > 1:
                n["unneeded_since"] = n["unneeded_since"] or now
                if now - n["unneeded_since"] >= CA_DOWN_S:
                    self._event("ScaleDown", "Node", n["name"], "node removed by cluster autoscaler (only expendable backfill pods)", None)
                    for p in [p for p in self.svc_pods if p["node"] == n["name"]]:
                        self.svc_pods.remove(p)
                    del self.nodes[n["name"]]
                    g4s.remove(n)
            else:
                n["unneeded_since"] = None

        # --- placement of CPU services (controller here; the native mock overrides this)
        self._allocate(now)

        # --- N4D autoscaling
        n4ds = [n for n in self.nodes.values() if n["role"] == "n4d"]
        pending = [p for p in self.svc_pods + self.buffer_pods if p["tier"] in ("overflow", "buffer") and p["node"] is None]
        if pending and not any(not self._ready(n) for n in n4ds):
            need = math.ceil(len(pending) / N4D_SLOTS)
            for i in range(need):
                zone = "us-west8-a" if (len(n4ds) + i) % 2 == 0 else "us-west8-c"
                nn = self._add_node("n4d", zone, now)
                n4ds.append(nn)
            self._event("TriggeredScaleUp", "Pod", pending[0]["name"], f"pod triggered scale-up: [{{n4dcpu-{self.POOL_TAG} {len(n4ds) - need}->{len(n4ds)} (max: 10)}}]", "colo-svc")
        for n in list(n4ds):
            used = self._node_users(n["name"])
            only_buffer = all(p["tier"] == "buffer" for p in used)
            if self._ready(n) and (not used or (only_buffer and len(n4ds) > 1 and not pending)) and len(n4ds) > 1:
                n["unneeded_since"] = n["unneeded_since"] or now
                if now - n["unneeded_since"] >= CA_DOWN_S:
                    self._event("ScaleDown", "Node", n["name"], "node removed by cluster autoscaler: underutilized", None)
                    for p in used:
                        p["node"] = None
                    del self.nodes[n["name"]]
                    n4ds.remove(n)
            else:
                n["unneeded_since"] = None

        # --- latency model
        rps_per_v = {}
        rv = [v for v in self.vllm if v["ready_at"] and now >= v["ready_at"]]
        for v in rv:
            rps_per_v[v["node"]] = self.eff_rate / max(1, len(rv))
        self.node_p99.clear()
        self.node_p50.clear()
        self.node_rps.clear()
        for node_name, r in rps_per_v.items():
            n = self.nodes.get(node_name)
            if not n:
                continue
            mem = sum(1 for p in self.svc_pods if p["node"] == node_name and SERVICES[p["app"]] == "memory-heavy" and p["ready_at"] <= now)
            integ = sum(1 for p in self.svc_pods if p["node"] == node_name and SERVICES[p["app"]] == "integer" and p["ready_at"] <= now)
            u = min(r / 700.0, 1.4)
            base = 38 + 26 * u**3 + (300 * (u - 1.0) if u > 1.0 else 0)
            p99 = base * (1 + 0.075 * mem + 0.006 * integ) * random.uniform(0.96, 1.04)
            self.node_p99[node_name] = p99
            self.node_p50[node_name] = p99 * 0.42
            self.node_rps[node_name] = min(r, 760.0)

        # --- guard
        self._guard(now, g4s)

    def _allocate(self, now: float):
        """colo-controller: split demand into backfill (eligible G4 slots) and overflow (the rest)."""
        eligible = [self.nodes[v["node"]] for v in self.vllm if v["node"] and now >= v["ready_at"]]
        slots = {n["name"]: self.g4_slots for n in eligible}
        backfill_want = {}
        for svc, cls in SERVICES.items():
            want_bf = 0
            for n in eligible:
                if cls in n["blocks"]:
                    continue
                take = min(slots[n["name"]], self.demand[svc] - want_bf)
                slots[n["name"]] -= take
                want_bf += take
            backfill_want[svc] = want_bf
        for svc, cls in SERVICES.items():
            self._reconcile_backfill(svc, cls, backfill_want[svc], eligible, now)
            self._reconcile_overflow(svc, cls, self.demand[svc] - backfill_want[svc], now)
        self._reconcile_buffer(now)

    def _guard(self, now: float, g4s: list[dict]):
        thr = (self.guard.get("baseline_ttft_p99_ms") or 60) * self.guard.get("factor", 1.5)
        for n in g4s:
            if n["name"] not in self.nodes:
                continue
            p99 = self.node_p99.get(n["name"])
            if not self.guard.get("enabled") or p99 is None:
                n["breach_since"] = None
                continue
            if p99 > thr:
                n["healthy_since"] = None
                n["breach_since"] = n["breach_since"] or now
                if "memory-heavy" not in n["blocks"]:
                    self._block(n, "memory-heavy", p99, thr)
                # Same rule as the controller: escalate to integer only when escalate_s > 0
                # (the demo config ships escalate_s 0, so integer work is never blocked).
                elif (float(self.guard.get("escalate_s") or 0) > 0 and "integer" not in n["blocks"]
                      and now - n["blocks"]["memory-heavy"] >= float(self.guard["escalate_s"])
                      and now - n["breach_since"] >= float(self.guard["escalate_s"])):
                    self._block(n, "integer", p99, thr)
            else:
                n["breach_since"] = None
                if n["blocks"]:
                    n["healthy_since"] = n["healthy_since"] or now
                    if now - n["healthy_since"] >= self.guard.get("cooldown_s", GUARD_COOLDOWN_S):
                        for cls in list(n["blocks"]):
                            del n["blocks"][cls]
                            n["labels"].pop(f"colo.block.{cls}", None)
                            self._event("ColoGuardRelease", "Node", n["name"], f"p99 healthy for cooldown; unblocked {cls}", None, "colo-controller")
                            self.guard_events.append({"time": _iso(now), "node": n["name"], "action": "release", "class": cls})
                        n["healthy_since"] = None

    def _block(self, n: dict, cls: str, p99: float, thr: float):
        n["blocks"][cls] = self.t
        n["labels"][f"colo.block.{cls}"] = "true"
        victims = [p for p in self.svc_pods if p["node"] == n["name"] and SERVICES[p["app"]] == cls and p["tier"] == "backfill"]
        for p in victims:
            self._release(p["uid"])
            self.svc_pods.remove(p)
        self._event("ColoGuardEvict", "Node", n["name"], f"TTFT p99 {p99:.0f} ms > {thr:.0f} ms; blocked {cls}, evicted {len(victims)} backfill pods", None, "colo-controller")
        self.guard_events.append({"time": _iso(self.t), "node": n["name"], "action": "evict", "class": cls, "p99_ms": round(p99, 1), "evicted": len(victims)})
        self.guard_events = self.guard_events[-100:]

    def _preempt(self, uid: str, by: str):
        for p in list(self.svc_pods):
            if p["uid"] == uid:
                self.svc_pods.remove(p)
                self._release(uid)
                self._event("Preempted", "Pod", p["name"], f"Preempted by inference/{by} on node {p['node']}", "colo-svc", "default-scheduler")

    def _new_pod(self, app: str, tier: str) -> dict:
        h = {"sandbox": "5c8f7b9d4", "toolcall": "7b6d5f8c9", "analytics": "6d9c8b7f5", "colo-buffer": "84f9c6d7b"}[app]
        name = f"{app}-{tier}-{h}-{_sfx()}" if app != "colo-buffer" else f"colo-buffer-{h}-{_sfx()}"
        return {"name": name, "uid": _sfx(32), "app": app, "tier": tier, "node": None, "ready_at": None}

    def _reconcile_backfill(self, svc: str, cls: str, want: int, eligible: list[dict], now: float):
        mine = [p for p in self.svc_pods if p["app"] == svc and p["tier"] == "backfill"]
        while len(mine) > want:
            p = mine.pop()
            self._release(p["uid"])
            self.svc_pods.remove(p)
        while len(mine) < want:
            p = self._new_pod(svc, "backfill")
            for n in eligible:
                if cls in n["blocks"]:
                    continue
                c = self._free_core(n)
                if c is not None:
                    n["cores"][c] = p["uid"]
                    p["node"] = n["name"]
                    p["ready_at"] = now + 6
                    break
            if p["node"] is None:
                break  # no room; controller will put it in overflow next tick
            mine.append(p)
            self.svc_pods.append(p)

    def _place_n4d(self, p: dict, now: float) -> bool:
        for n in self.nodes.values():
            if n["role"] != "n4d" or not self._ready(n):
                continue
            c = self._free_core(n)
            if c is None:
                # preempt a buffer pod if this is overflow
                if p["tier"] == "overflow":
                    bp = next((b for b in self.buffer_pods if b["node"] == n["name"]), None)
                    if bp:
                        self._release(bp["uid"])
                        bp["node"] = None
                        self._event("Preempted", "Pod", bp["name"], f"Preempted by colo-svc/{p['name']} on node {n['name']}", "colo-svc", "default-scheduler")
                        c = self._free_core(n)
                if c is None:
                    continue
            n["cores"][c] = p["uid"]
            p["node"] = n["name"]
            p["ready_at"] = now + 5
            return True
        return False

    def _reconcile_overflow(self, svc: str, cls: str, want: int, now: float):
        mine = [p for p in self.svc_pods if p["app"] == svc and p["tier"] == "overflow"]
        while len(mine) > want:
            p = mine.pop()
            self._release(p["uid"])
            self.svc_pods.remove(p)
        while len(mine) < want:
            p = self._new_pod(svc, "overflow")
            mine.append(p)
            self.svc_pods.append(p)
        for p in mine:
            if p["node"] is None:
                self._place_n4d(p, now)

    def _reconcile_buffer(self, now: float):
        while len(self.buffer_pods) > self.buffer_replicas:
            p = self.buffer_pods.pop()
            self._release(p["uid"])
        while len(self.buffer_pods) < self.buffer_replicas:
            self.buffer_pods.append(self._new_pod("colo-buffer", "buffer"))
        for p in self.buffer_pods:
            if p["node"] is None:
                self._place_n4d(p, now)

    # ------------------------------------------------------------ raw output
    def raw(self) -> dict:
        now = time.time()
        self._step(now)
        nodes = []
        for n in self.nodes.values():
            alloc = {"g4": (self.g4_alloc, "176Gi" if self.accel["id"] == "g4" else "165Gi"), "n4d": (N4D_ALLOC_CPU, "58Gi"), "loadgen": (7.9, "28Gi")}[n["role"]]
            nodes.append(
                {
                    "name": n["name"],
                    "labels": dict(n["labels"]),
                    "allocatable": {"cpu": str(alloc[0]), "memory": alloc[1]},
                    "ready": self._ready(n),
                    "created": _iso(n["created"]),
                }
            )
        pods = []
        size = {"sandbox": (2, 4), "toolcall": (2, 2), "analytics": (2, 6)}
        for v in self.vllm:
            pods.append(self._pod(v["name"], v["uid"], "inference", {"app": "vllm"}, v["node"], v["ready_at"], 8, 64, "gpu-serving"))
        lg_node = next(n["name"] for n in self.nodes.values() if n["role"] == "loadgen")
        for lp in self.loadgen_pods:
            pods.append(self._pod(lp, _sfx(32), "inference", {"app": "loadgen"}, lg_node, now - 3600, 1, 2, None))
        for p in self.svc_pods:
            cpu, mem = size[p["app"]]
            labels = {"app": p["app"], "colo.tier": p["tier"], "colo.class": SERVICES[p["app"]]}
            pods.append(self._pod(p["name"], p["uid"], "colo-svc", labels, p["node"], p["ready_at"], cpu, mem, f"colo-{p['tier']}"))
        for p in self.buffer_pods:
            pods.append(self._pod(p["name"], p["uid"], "colo-system", {"app": "colo-buffer"}, p["node"], p["ready_at"], 2, 1, "colo-buffer"))
        pods.append(self._pod("colo-controller-5d7f8c9b6-k2x8q", _sfx(32), "colo-system", {"app": "colo-controller"}, lg_node, now - 3600, 0.2, 0.25, None))

        def dep(ns, name, replicas, ready):
            return {"namespace": ns, "name": name, "replicas": replicas, "ready": ready}

        ready_pod = lambda p: p["node"] is not None and p["ready_at"] is not None and now >= p["ready_at"]  # noqa: E731
        deployments = [
            dep("inference", "vllm", self.hpa_desired, sum(1 for v in self.vllm if ready_pod(v))),
            dep("inference", "loadgen", LOADGEN_REPLICAS, LOADGEN_REPLICAS),
            dep("colo-system", "colo-buffer", self.buffer_replicas, sum(1 for p in self.buffer_pods if ready_pod(p))),
        ]
        for svc in SERVICES:
            for tier in ("backfill", "overflow"):
                mine = [p for p in self.svc_pods if p["app"] == svc and p["tier"] == tier]
                deployments.append(dep("colo-svc", f"{svc}-{tier}", len(mine), sum(1 for p in mine if ready_pod(p))))
        running = self.eff_rate * 0.15
        rv = max(1, sum(1 for v in self.vllm if ready_pod(v)))
        hpas = [{"name": "vllm", "namespace": "inference", "target": "vllm", "min": self.hpa_min, "max": 2, "current": len(self.vllm), "desired": self.hpa_desired, "metric": f"{running / rv:.0f}"}]

        # node agents
        agents = {}
        for n in self.nodes.values():
            if n["role"] not in ("g4", "n4d") or not self._ready(n):
                continue
            half = self.g4_half if n["role"] == "g4" else 8
            entries: dict[str, dict[str, list[int]]] = {}
            for core, uid in n["cores"].items():
                entries.setdefault(uid, {}).setdefault("main", []).extend([core, core + half])
            uid_kind = {v["uid"]: "vllm" for v in self.vllm}
            uid_kind.update({p["uid"]: SERVICES[p["app"]] for p in self.svc_pods})
            uid_kind.update({p["uid"]: "buffer" for p in self.buffer_pods})
            vl = self.node_rps.get(n["name"], 0) / 700.0
            busy = []
            for c in range(half * 2):
                uid = n["cores"].get(c % half)
                k = uid_kind.get(uid)
                if k == "vllm":
                    b = 25 + 55 * vl
                elif k == "integer":
                    b = 92
                elif k == "memory-heavy":
                    b = 80
                elif k == "buffer":
                    b = 0.3
                else:
                    b = 6 + (10 if c % half == 0 else 0)
                busy.append(round(max(0, min(100, b + random.uniform(-5, 5))), 1))
            agents[n["name"]] = {
                "node": n["name"],
                "ncpu": half * 2,
                "cpu_manager_state": {
                    "policyName": "static",
                    "defaultCpuSet": ",".join(str(c) for c in range(half * 2) if (c % half) not in n["cores"]),
                    "entries": {u: {cn: ",".join(map(str, sorted(cs))) for cn, cs in d.items()} for u, d in entries.items()},
                    "checksum": 0,
                },
                "busy": busy,
                "topology": _mock_topology(half * 2, self.accel["id"] if n["role"] == "g4" else "n4d"),
                "mem_used_gib": round(10 + sum(64 if uid_kind.get(u) == "vllm" else 4 for u in set(n["cores"].values())) * 0.8, 1),
            }

        # loadgen stats: each replica sees 1/4 of the traffic
        loadgen = {}
        for lp in self.loadgen_pods:
            per_pod = {}
            for v in self.vllm:
                if not ready_pod(v):
                    continue
                r = self.node_rps.get(v["node"], 0) / LOADGEN_REPLICAS
                total = int(r * WINDOW_S)
                p99 = self.node_p99.get(v["node"], 50)
                p50 = self.node_p50.get(v["node"], 20)
                per_pod[v["name"]] = {
                    "node": v["node"],
                    "ttft_hist": _lognormal_hist(total, p50, p99),
                    "e2e_hist": _lognormal_hist(total, p50 * 6, p99 * 4),
                    "inflight": int(r * 0.15),
                    "ok": total,
                    "err": 0 if p99 < 500 else int(total * 0.01),
                }
            loadgen[lp] = {"rate": self.rate / LOADGEN_REPLICAS, "per_pod": per_pod, "window_s": WINDOW_S, "bucket_ms": BUCKET_MS}

        cfg = {"demand": dict(self.demand), "guard": dict(self.guard), "buffer_replicas": self.buffer_replicas}
        cstate = {
            "demand": dict(self.demand),
            "guard": dict(self.guard),
            "baseline_ttft_p99_ms": self.guard.get("baseline_ttft_p99_ms"),
            "nodes": {
                n["name"]: {"spare_cpu": self.g4_slots * 2, "blocks": sorted(n["blocks"]), "p99_ms": round(self.node_p99.get(n["name"], 0), 1)}
                for n in self.nodes.values()
                if n["role"] == "g4"
            },
            "guard_events": self.guard_events[-100:],
        }
        return {
            "nodes": nodes,
            "pods": pods,
            "deployments": deployments,
            "hpas": hpas,
            "events": list(reversed(self.events))[:50],
            "agents": agents,
            "loadgen": loadgen,
            "controller": {"state": cstate, "config": cfg},
            "configmap": cfg,
            "errors": [],
        }

    def _pod(self, name, uid, ns, labels, node, ready_at, cpu, mem, prio):
        now = self.t
        phase = "Running" if node else "Pending"
        return {
            "name": name,
            "namespace": ns,
            "uid": uid,
            "labels": labels,
            "node": node,
            "phase": phase,
            "ready": bool(node and ready_at is not None and now >= ready_at),
            "cpu_req": cpu,
            "mem_req_gib": mem,
            "priority_class": prio,
        }

    async def state(self) -> dict:
        return build_state(self.raw(), mode=self.mode, recipe=self.recipe, cluster=self.cluster_info)

    # ------------------------------------------------------------ actions
    async def set_gpu_rate(self, rate: float) -> dict:
        self.rate = float(rate)
        if self.fast_scale:
            self.hpa_min = 2 if self.rate > 700 else 1
            self.fast_scale_forced = self.hpa_min > 1
        return {p: {"rate": self.rate} for p in self.loadgen_pods}

    async def set_demand(self, demand: dict) -> dict:
        for k, v in demand.items():
            if k in self.demand:
                self.demand[k] = max(0, int(v))
        self._step(time.time(), force=True)
        return {"demand": self.demand}

    async def restore_hpa_min(self) -> bool:
        self.hpa_min = 1
        self.fast_scale_forced = False
        return True

    async def set_guard(self, enabled: bool) -> dict:
        self.guard["enabled"] = bool(enabled)
        return {"guard": self.guard}

    async def baseline(self) -> dict:
        vals = [v for v in self.node_p99.values()]
        if vals:
            self.guard["baseline_ttft_p99_ms"] = round(max(vals), 1)
        return {"baseline_ttft_p99_ms": self.guard["baseline_ttft_p99_ms"]}


# Native recipe (g4_colo/recipe-native/): HPA metric pipeline lag (GMP rule -> adapter -> HPA), compressed for the demo.
NATIVE_METRIC_LAG_S = float(os.environ.get("DEMO_MOCK_METRIC_LAG_S", "4"))
NATIVE_HPA_DOWN_WINDOW_S = float(os.environ.get("DEMO_MOCK_HPA_DOWN_S", "15"))
NATIVE_OVERFLOW_MIN = 1  # HPA cannot scale to 0 on GKE
NATIVE_OVERFLOW_MAX = 60


class NativeMockBackend(MockBackend):
    """Simulator for the controller-free recipe. Same raw formats as cluster.NativeLiveBackend:

      * <svc>-backfill holds the whole demand. A pod binds only to a G4 node that runs a vLLM pod (podAffinity)
        and has a free core; the rest stay Pending (priority -100, the cluster autoscaler ignores them).
      * <svc>-overflow is scaled by an HPA on colo_overflow_needed = demand - backfill Ready next to a Ready vLLM,
        seen NATIVE_METRIC_LAG_S late; scale-down waits NATIVE_HPA_DOWN_WINDOW_S. minReplicas 1.
      * memory-heavy (analytics) never backfills: analytics-overflow replicas = demand.
      * no colo-controller, no colo-config, no latency guard, no node labels.
    """

    recipe = "native"
    POOL_TAG = "jawadamin-us-west8-1126"

    def __init__(self, cluster_id: str = "native", cluster_info: dict | None = None):
        self.overflow_hist: dict[str, list[tuple[float, int]]] = {s: [] for s, c in SERVICES.items() if c == "integer"}
        self.hpa_overflow = {s: NATIVE_OVERFLOW_MIN for s in self.overflow_hist}
        self.hpa_metric: dict[str, int | None] = {s: None for s in self.overflow_hist}
        super().__init__(cluster_id=cluster_id, cluster_info=cluster_info)
        self.guard = {"enabled": None}
        # start settled: no metric lag on the first view
        for s in self.overflow_hist:
            needed = self._overflow_needed(s, time.time())
            self.overflow_hist[s] = [(time.time() - 600, needed)]
            self.hpa_metric[s] = needed
            self.hpa_overflow[s] = max(NATIVE_OVERFLOW_MIN, needed)

    # ------------------------------------------------------------ scheduling + HPAs
    def _vllm_hosts(self) -> list[dict]:
        """G4 nodes the backfill podAffinity accepts: any node with a vLLM pod bound to it."""
        return [self.nodes[v["node"]] for v in self.vllm if v["node"] and v["node"] in self.nodes]

    def _overflow_needed(self, svc: str, now: float) -> int:
        ready_vllm = {v["node"] for v in self.vllm if v["node"] and v["ready_at"] and now >= v["ready_at"]}
        eff = sum(1 for p in self.svc_pods if p["app"] == svc and p["tier"] == "backfill" and p["node"] in ready_vllm
                  and p["ready_at"] is not None and now >= p["ready_at"])
        return max(0, self.demand[svc] - eff)

    def _allocate(self, now: float):
        hosts = self._vllm_hosts()
        for svc, cls in SERVICES.items():
            if cls != "integer":
                continue
            mine = [p for p in self.svc_pods if p["app"] == svc and p["tier"] == "backfill"]
            want = self.demand[svc]
            while len(mine) > want:  # ReplicaSet scale-down deletes unscheduled pods first, then the newest
                victim = next((p for p in reversed(mine) if p["node"] is None), mine[-1])
                mine.remove(victim)
                self._release(victim["uid"])
                self.svc_pods.remove(victim)
            while len(mine) < want:
                p = self._new_pod(svc, "backfill")
                mine.append(p)
                self.svc_pods.append(p)
            for p in mine:
                if p["node"] is not None:
                    continue
                for n in hosts:
                    c = self._free_core(n)
                    if c is not None:
                        n["cores"][c] = p["uid"]
                        p["node"] = n["name"]
                        p["ready_at"] = now + 6
                        break
        # overflow HPAs on a lagged metric
        for svc in self.overflow_hist:
            hist = self.overflow_hist[svc]
            hist.append((now, self._overflow_needed(svc, now)))
            cut = now - NATIVE_METRIC_LAG_S - NATIVE_HPA_DOWN_WINDOW_S - 5
            while len(hist) > 1 and hist[1][0] < cut:
                hist.pop(0)
            seen = [v for t, v in hist if t <= now - NATIVE_METRIC_LAG_S]
            if seen:
                self.hpa_metric[svc] = seen[-1]
                window = [v for t, v in hist if now - NATIVE_METRIC_LAG_S - NATIVE_HPA_DOWN_WINDOW_S <= t <= now - NATIVE_METRIC_LAG_S]
                rec = max(window or [seen[-1]])  # up at once, down only after the stabilization window
                self.hpa_overflow[svc] = max(NATIVE_OVERFLOW_MIN, min(NATIVE_OVERFLOW_MAX, rec))
            self._reconcile_overflow(svc, "integer", self.hpa_overflow[svc], now)
        self._reconcile_overflow("analytics", "memory-heavy", self.demand["analytics"], now)
        self._reconcile_buffer(now)

    def _guard(self, now: float, g4s: list[dict]):
        return  # no latency guard: memory-heavy is banned from G4 by admission policy

    # ------------------------------------------------------------ raw output
    def raw(self) -> dict:
        r = super().raw()
        r["pods"] = [p for p in r["pods"] if (p.get("labels") or {}).get("app") != "colo-controller"]
        for p in r["pods"]:
            if (p.get("labels") or {}).get("app") == "colo-buffer":
                p["priority_class"] = "colo-buffer"
        r["deployments"] = [d for d in r["deployments"] if not (d["namespace"] == "colo-svc" and d["name"] == "analytics-backfill")]
        for svc in self.overflow_hist:
            r["hpas"].append({
                "name": f"{svc}-overflow", "namespace": "colo-svc", "target": f"{svc}-overflow",
                "min": NATIVE_OVERFLOW_MIN, "max": NATIVE_OVERFLOW_MAX,
                "current": sum(1 for p in self.svc_pods if p["app"] == svc and p["tier"] == "overflow"),
                "desired": self.hpa_overflow[svc],
                "metric": None if self.hpa_metric[svc] is None else str(self.hpa_metric[svc]),
            })
        r["controller"] = {"state": None, "config": None}
        r["configmap"] = None
        r["events"] = [e for e in r["events"] if not str(e.get("reason", "")).startswith("ColoGuard")]
        return r

    # ------------------------------------------------------------ actions
    async def set_demand(self, demand: dict) -> dict:
        for k, v in demand.items():
            if k in self.demand:
                self.demand[k] = max(0, int(v))
        self._step(time.time(), force=True)
        return {"scaled": [{"deployment": f"colo-svc/{NATIVE_DEMAND_DEPLOYMENT[k]}", "replicas": self.demand[k]}
                           for k in demand if k in NATIVE_DEMAND_DEPLOYMENT]}

    async def set_guard(self, enabled: bool) -> dict:
        raise UnsupportedOnRecipe(f"no latency guard on the native recipe: {GUARD_NA_REASON}")

    async def baseline(self) -> dict:
        raise UnsupportedOnRecipe(f"no TTFT baseline on the native recipe: {GUARD_NA_REASON}")


# Agent Substrate experiment on the controller cluster (recipe "substrate"). DEMO_MOCK_SUBSTRATE=absent renders the
# "not installed yet" empty state (no CRD, no pools, no actor-stats ConfigMap) offline.
SUB_WORKER_CPU = 2  # Guaranteed integer CPU per Worker pod (1 physical core), as on 0726
SUB_WORKER_MEM_GIB = 4
N4D_WORKERS_PER_NODE = 6
SUB_ACTOR_SLOTS = 1  # an awake (RUNNING) actor occupies its whole Worker; suspended actors take none
SUB_N4D_MAX = 10


class SubstrateMockBackend(MockBackend):
    """The controller simulator plus Agent Substrate Workers, emitting the raw `substrate` section of
    cluster.SubstrateLiveBackend:

      * g4-workers (WorkerPool, DEMO_MOCK_G4_WORKERS Total Workers, default 4): 2-vCPU / 4 GiB pods on G4 nodes running a Ready vLLM
        pod, in whatever cores colo backfill left free; the rest stay Pending.
      * n4d-workers: an HPA keeps 1 + (Pending g4-workers) replicas on N4D nodes with free cores.
      * actor-stats: ~12 actors per Running Worker, resume p99 about 40 ms on G4 and 55 ms on N4D, no lost actors.
    """

    recipe = "substrate"
    G4_WORKER_PRIORITY = "colo-backfill"

    def __init__(self, cluster_id: str = "substrate", cluster_info: dict | None = None):
        self.sub_absent = os.environ.get("DEMO_MOCK_SUBSTRATE", "") == "absent"
        # Option 2: Total Workers = HPA g4-workers maxReplicas; that HPA sets the G4 share (Workers that fit next to Ready
        # vLLM, outranking CPU backfill), the n4d-workers HPA runs the rest. DEMO_MOCK_NO_G4_HPA=1: the pre-option-2
        # cluster (no g4-workers HPA; Total Workers = WorkerPool g4-workers replicas, extras Pending).
        self.sub_total = int(os.environ.get("DEMO_MOCK_G4_WORKERS", "4"))
        self.g4_hpa_present = os.environ.get("DEMO_MOCK_NO_G4_HPA", "") != "1"
        self.sub_g4_desired = self.sub_total
        self.sub_workers: list[dict] = []  # {name, uid, pool, node|None, ready_at}
        self.sub_n4d_desired = 1
        # ConfigMap substrate-exp/actor-load as the actor-load generator reads it (POST /api/substrate/actors patches it).
        self.actor_load = {"total": 40, "awake": 2, "hold_s": 5.0}
        self.sub_counters = {"lost_actors": 0, "resumes_waited_no_room": 0, "memory_resets": 0, "failed_requests": 0}  # 1 on G4, 1 on N4D: settled at start
        # DEMO_MOCK_ATE_DEGRADED=1: atenet-router 0/1 ready (down) and ate-controller restarting, to see the red states.
        self.ate_degraded = os.environ.get("DEMO_MOCK_ATE_DEGRADED", "") == "1"
        super().__init__(cluster_id=cluster_id, cluster_info=cluster_info)
        t0 = time.time() - 5400
        self.ate_nodes = {kind: {"name": f"gke-g4cluster-ate-{kind.split('-')[-1]}-{self.POOL_TAG}-{_sfx(4)}-{_sfx(4)}",
                                 "created": t0, "kind": kind} for kind in ("ate-system-infra", "ate-postgres")}
        # As on 0726: (workload, node kind, cpu, owner kind, namespace); one entry per pod.
        A, PC = "ate-system", "podcertificate-controller-system"
        self.ate_pods = [
            ("ate-api-server", "ate-system-infra", 0.5, "Deployment", A), ("ate-api-server", "ate-system-infra", 0.5, "Deployment", A),
            ("ate-controller", "ate-system-infra", 0.25, "Deployment", A),
            ("atenet-router", "ate-system-infra", 0.25, "Deployment", A),
            ("atenet-egress", "ate-system-infra", 0.25, "Deployment", A),
            ("ate-idp-server", "ate-system-infra", 0.1, "Deployment", A), ("ate-idp-server", "ate-system-infra", 0.1, "Deployment", A),
            ("k8s-credential-provider", "ate-system-infra", 0.1, "Deployment", A),
            ("podcertificate-controller", "ate-system-infra", 0.1, "Deployment", PC),
            ("postgres", "ate-postgres", 1.0, "StatefulSet", A),
        ]
        self.ate_pod_names = [f"{w}-0" if owner == "StatefulSet" else f"{w}-{_sfx(9)}-{_sfx()}"
                              for w, _k, _c, owner, _ns in self.ate_pods]
        self.atelet_names: dict[str, str] = {}
        for w in self.sub_workers:  # start settled
            if w["node"]:
                w["ready_at"] = time.time() - 600

    def _free_cores(self, node: dict, k: int) -> list[int] | None:
        ncores = self.g4_half if node["role"] == "g4" else 8
        free = [c for c in range(1, ncores) if c not in node["cores"]]
        return free[:k] if len(free) >= k else None

    def _g4_capacity(self, hosts: list[dict]) -> int:
        """Workers that fit next to Ready vLLM when Workers outrank CPU backfill: cores not held by vLLM (or another
        non-backfill pod) on each host, in whole Workers."""
        backfill = {p["uid"] for p in self.svc_pods if p["tier"] == "backfill"} | {w["uid"] for w in self.sub_workers}
        cap = 0
        for n in hosts:
            usable = sum(1 for c in range(1, self.g4_half) if c not in n["cores"] or n["cores"][c] in backfill)
            cap += usable // (SUB_WORKER_CPU // 2)
        return cap

    def _preempt_backfill_for(self, n: dict, k: int) -> list[int] | None:
        """Option 2: a Worker (priority substrate-worker, -5) preempts CPU backfill (-100) on its host."""
        victims = [p for p in self.svc_pods if p["node"] == n["name"] and p["tier"] == "backfill"]
        while self._free_cores(n, k) is None and victims:
            p = victims.pop()
            self._release(p["uid"])
            self.svc_pods.remove(p)
            self._event("Preempted", "Pod", p["name"], f"Preempted by substrate-exp/g4-workers on node {n['name']}",
                        "colo-svc", "default-scheduler")
        return self._free_cores(n, k)

    def _place_worker(self, w: dict, nodes: list[dict], now: float) -> None:
        for n in nodes:
            cores = self._free_cores(n, SUB_WORKER_CPU // 2)
            if not cores and w["pool"] == "g4-workers" and self.g4_hpa_present:
                cores = self._preempt_backfill_for(n, SUB_WORKER_CPU // 2)
            if cores:
                for c in cores:
                    n["cores"][c] = w["uid"]
                w["node"] = n["name"]
                w["ready_at"] = now + 8
                return

    def _reconcile_pool(self, pool: str, want: int, nodes: list[dict], now: float) -> None:
        mine = [w for w in self.sub_workers if w["pool"] == pool]
        while len(mine) > want:
            victim = next((w for w in reversed(mine) if w["node"] is None), mine[-1])
            mine.remove(victim)
            self._release(victim["uid"])
            self.sub_workers.remove(victim)
        while len(mine) < want:
            w = {"name": f"{pool}-7c9d5b8f6-{_sfx()}", "uid": _sfx(32), "pool": pool, "node": None, "ready_at": None}
            mine.append(w)
            self.sub_workers.append(w)
        for w in mine:
            if w["node"] is None:
                self._place_worker(w, nodes, now)

    def _allocate(self, now: float):
        super()._allocate(now)
        if self.sub_absent:
            return
        for w in self.sub_workers:  # node removed by the autoscaler -> the pod is recreated Pending
            if w["node"] and w["node"] not in self.nodes:
                w["node"], w["ready_at"] = None, None
        hosts = [self.nodes[v["node"]] for v in self.vllm if v["node"] in self.nodes and v["ready_at"] and now >= v["ready_at"]]
        if self.g4_hpa_present:
            # g4-workers HPA: G4 share = Workers that fit next to Ready vLLM, capped by Total Workers (maxReplicas).
            self.sub_g4_desired = min(self.sub_total, self._g4_capacity(hosts))
        else:
            self.sub_g4_desired = self.sub_total
        self._reconcile_pool("g4-workers", self.sub_g4_desired, hosts, now)
        pending_g4 = sum(1 for w in self.sub_workers if w["pool"] == "g4-workers" and w["node"] is None)
        g4_cap = SUB_ACTOR_SLOTS * sum(1 for w in self.sub_workers if w["pool"] == "g4-workers" and w["node"])
        overflow = max(0, self.actor_load["awake"] - g4_cap)
        if self.g4_hpa_present:
            # n4d-workers HPA (option 2): Total − G4 share, plus busy headroom for awake actors beyond Total.
            rest = self.sub_total - self.sub_g4_desired
            self.sub_n4d_desired = max(0, min(self.n4d_max, rest + max(0, self.actor_load["awake"] - self.sub_total)))
        else:
            # pre-option-2: Pending g4-workers + Workers needed for awake actors beyond G4 capacity (at least 1).
            self.sub_n4d_desired = max(1, min(SUB_N4D_MAX, pending_g4 + max(1, math.ceil(overflow / SUB_ACTOR_SLOTS))))
        n4ds = [n for n in self.nodes.values() if n["role"] == "n4d" and self._ready(n)]
        self._reconcile_pool("n4d-workers", self.sub_n4d_desired, n4ds, now)
        # Pending n4d-workers -> the cluster autoscaler adds N4D nodes (about 6 Workers of 2 vCPU fit one n4d-standard-16).
        waiting = [w for w in self.sub_workers if w["pool"] == "n4d-workers" and w["node"] is None]
        all_n4d = [n for n in self.nodes.values() if n["role"] == "n4d"]
        if waiting and not any(not self._ready(n) for n in all_n4d):
            for i in range(math.ceil(len(waiting) / N4D_WORKERS_PER_NODE)):
                self._add_node("n4d", "us-west8-a" if (len(all_n4d) + i) % 2 == 0 else "us-west8-c", now)
            self._event("TriggeredScaleUp", "Pod", waiting[0]["name"],
                        f"pod triggered scale-up: [{{n4dcpu-{self.POOL_TAG} {len(all_n4d)}->"
                        f"{len(all_n4d) + math.ceil(len(waiting) / N4D_WORKERS_PER_NODE)} (max: 10)}}]", "substrate-exp")

    def _node_users(self, node: str) -> list[dict]:
        return super()._node_users(node) + [{**w, "tier": "worker"} for w in self.sub_workers if w["node"] == node]

    def raw(self) -> dict:
        r = super().raw()
        now = self.t
        if self.sub_absent:
            r["substrate"] = {"workerpools": None, "hpas": [], "actor_stats": None,
                              "ate_workloads": {"deployments": [], "statefulsets": [], "daemonsets": [],
                                                "podcert_deployments": []}, "notes": [
                "WorkerPools (workerpools.ate.dev/v1 in substrate-exp): not found (not installed yet)",
                "ConfigMap substrate-exp/actor-stats: not found (not installed yet)"]}
            return r
        ready = lambda w: w["node"] is not None and w["ready_at"] is not None and now >= w["ready_at"]  # noqa: E731
        for w in self.sub_workers:
            prio = self.G4_WORKER_PRIORITY if w["pool"] == "g4-workers" else None
            r["pods"].append(self._pod(w["name"], w["uid"], "substrate-exp", {"ate.dev/worker-pool": w["pool"], "app": "ate-worker"},
                                       w["node"], w["ready_at"], SUB_WORKER_CPU, SUB_WORKER_MEM_GIB, prio))
        # Busy % of Worker cores (agents emitted by the parent do not know the Worker uids).
        for w in self.sub_workers:
            ag = r["agents"].get(w["node"]) if w["node"] else None
            if not ag:
                continue
            half = ag["ncpu"] // 2
            for c, uid in self.nodes[w["node"]]["cores"].items():
                if uid == w["uid"]:
                    for cpu in (c, c + half):
                        # Counter actors sleep most of each hold: a Worker's CPUs stay near idle whether or not it
                        # hosts a RUNNING actor, which is why the map marks occupancy separately from busy %.
                        ag["busy"][cpu] = round(random.uniform(0.3, 3.0), 1)
        pools = []
        for pool, desired in (("g4-workers", self.sub_g4_desired), ("n4d-workers", self.sub_n4d_desired)):
            mine = [w for w in self.sub_workers if w["pool"] == pool]
            pools.append({"apiVersion": "ate.dev/v1", "kind": "WorkerPool",
                          "metadata": {"name": pool, "namespace": "substrate-exp", "labels": {"colo.gke.io/worker-tier": "agents"}},
                          "spec": {"replicas": desired, "workerImage": "ghcr.io/agent-substrate/worker:v1.0.0"},
                          "status": {"replicas": len(mine), "readyReplicas": sum(1 for w in mine if ready(w))}})
        n4d_now = sum(1 for w in self.sub_workers if w["pool"] == "n4d-workers")
        hpas = [{"name": "n4d-workers", "namespace": "substrate-exp", "target": "n4d-workers",
                 "min": 0 if self.g4_hpa_present else 1, "max": self.n4d_max if self.g4_hpa_present else SUB_N4D_MAX,
                 "current": n4d_now, "desired": self.sub_n4d_desired,
                 "metric": str(sum(1 for w in self.sub_workers if w["pool"] == "g4-workers" and w["node"] is None))}]
        if self.g4_hpa_present:
            hpas.append({"name": "g4-workers", "namespace": "substrate-exp", "target": "g4-workers", "min": 1,
                         "max": self.sub_total, "desired": self.sub_g4_desired,
                         "current": sum(1 for w in self.sub_workers if w["pool"] == "g4-workers"),
                         "metric": str(self.sub_g4_desired)})
        # Awake actors land on Ready g4-workers first (one per Worker), the rest on Ready n4d-workers; what fits
        # nowhere stays suspended until a Worker frees up.
        by_node: dict[str, int] = {}
        by_pool = {"g4-workers": 0, "n4d-workers": 0}
        left = self.actor_load["awake"]
        running_workers: list[str] = []
        for pool in ("g4-workers", "n4d-workers"):
            for w in self.sub_workers:
                if w["pool"] == pool and ready(w) and left > 0:
                    k = min(SUB_ACTOR_SLOTS, left)
                    left -= k
                    by_node[w["node"]] = by_node.get(w["node"], 0) + k
                    by_pool[pool] += k
                    running_workers.append(w["name"])
        running = sum(by_node.values())
        hold = self.actor_load["hold_s"]
        # Cumulative counters: every waiting awake actor was refused a resume once per stats tick (pool full); a resume
        # that lands on the other pool now and then restores on a different CPU feature set (memory reset, files kept).
        waiting = max(0, self.actor_load["awake"] - running)
        self.sub_counters["resumes_waited_no_room"] += waiting
        if by_pool["n4d-workers"] and by_pool["g4-workers"] and random.random() < 0.1:
            self.sub_counters["memory_resets"] += 1
        stats = {
            "updated": _iso(now),
            "source": "actor-load",
            "target_total": self.actor_load["total"],
            "target_awake": self.actor_load["awake"],
            "hold_s": hold,
            "actors_total": self.actor_load["total"],
            "actors_running": running,
            "actors_running_by_pool": by_pool,
            "running_workers": sorted(running_workers),
            "actors_by_node": by_node,
            "resumes_per_s": round(running / hold * random.uniform(0.9, 1.1), 1) if running else 0.0,
            "resume_p50_ms": round(14 * random.uniform(0.95, 1.05), 1),
            "resume_p99_ms": round(52 * random.uniform(0.95, 1.05), 1),
            "resume_p99_ms_by_pool": {"g4-workers": round(40 * random.uniform(0.95, 1.05), 1),
                                      "n4d-workers": round(55 * random.uniform(0.95, 1.05), 1)},
            **self.sub_counters,
        }
        r["substrate"] = {"workerpools": pools, "hpas": hpas, "actor_stats": json.dumps(stats), "notes": [],
                          "actor_load": {k: f"{v:g}" if isinstance(v, float) else str(v) for k, v in self.actor_load.items()},
                          "ate_workloads": self._ate_control_plane(r, now)}
        return r

    def _ate_control_plane(self, r: dict, now: float) -> dict:
        """Adds the two control-plane nodes, their ate-system pods and one atelet pod per G4/N4D node to `r`;
        returns the ate-system workload lists (same shape as cluster.SubstrateLiveBackend)."""
        for kind, n in self.ate_nodes.items():
            r["nodes"].append({"name": n["name"], "ready": True, "created": _iso(n["created"]),
                               "labels": {"ate.dev/workloadType": kind, "node.kubernetes.io/instance-type":
                                              "n4d-standard-4" if kind == "ate-system-infra" else "e2-standard-4",
                                          "topology.kubernetes.io/zone": "us-west8-a",
                                          "cloud.google.com/gke-nodepool": f"ate-{kind.split('-')[-1]}"},
                               "allocatable": {"cpu": "3.92", "memory": "13Gi"}})
        wls: dict[tuple[str, str, str], dict] = {}
        for (wname, kind, cpu, owner, ns), name in zip(self.ate_pods, self.ate_pod_names):
            down = self.ate_degraded and wname == "atenet-router"
            p = self._pod(name, _sfx(32), ns, {"app": wname}, self.ate_nodes[kind]["name"], None if down else now - 3000,
                          cpu, 0.5 if owner == "Deployment" else 4, None)
            p["owner_kind"] = "ReplicaSet" if owner == "Deployment" else "StatefulSet"
            p["restarts"] = 7 if self.ate_degraded and wname in ("atenet-router", "ate-controller") else 0
            r["pods"].append(p)
            wl = wls.setdefault((ns, owner, wname), {"name": wname, "replicas": 0, "ready": 0})
            wl["replicas"] += 1
            wl["ready"] += int(p["ready"])
        pick = lambda ns, owner: [w for (n, o, _w), w in wls.items() if n == ns and o == owner]  # noqa: E731
        deployments, statefulsets = pick("ate-system", "Deployment"), pick("ate-system", "StatefulSet")
        podcert = pick("podcertificate-controller-system", "Deployment")
        workers = [n for n in self.nodes.values() if n["role"] in ("g4", "n4d")]
        ready = 0
        for n in workers:
            name = self.atelet_names.setdefault(n["name"], f"atelet-{_sfx()}")
            p = self._pod(name, _sfx(32), "ate-system", {"app": "atelet"}, n["name"] if self._ready(n) else None,
                          n["ready_at"] + 10, 0.1, 0.25, None)
            p["owner_kind"], p["restarts"] = "DaemonSet", 0
            ready += int(p["ready"])
            r["pods"].append(p)
        daemonsets = [{"name": "atelet", "desired": len(workers), "ready": ready, "available": ready}]
        return {"deployments": deployments, "statefulsets": statefulsets, "daemonsets": daemonsets,
                "podcert_deployments": podcert}

    @property
    def n4d_max(self) -> int:
        return max(SUB_N4D_MAX, self.sub_total)

    async def set_g4_workers(self, body: dict) -> dict:
        n = validate_worker_replicas(body)  # ValueError -> 400, as live
        if self.g4_hpa_present:
            if n < 1:  # as live: an HPA cannot have maxReplicas below max(1, minReplicas)
                raise ValueError(f"total must be at least 1 while HPA substrate-exp/g4-workers exists (its minReplicas); got {n}")
            self.sub_total = n
            self._step(time.time(), force=True)
            return {"target": "hpa/substrate-exp/g4-workers", "mode": "hpa-max", "total": n}
        self.sub_total = n
        self._step(time.time(), force=True)
        return {"target": "workerpool/substrate-exp/g4-workers", "mode": "workerpool-replicas", "total": n}

    async def set_actor_load(self, body: dict) -> dict:
        spec, patch = actor_load_patch(self.actor_load, body)  # ValueError -> 400, as live
        self.actor_load = spec
        self._step(time.time(), force=True)
        return {"spec": spec, "patch": patch}


class SubstrateNativeMockBackend(SubstrateMockBackend, NativeMockBackend):
    """Agent Substrate on the native recipe (base_recipe "native"): NativeMockBackend's placement (backfill next to vLLM by
    podAffinity, overflow HPAs, no controller/guard) plus SubstrateMockBackend's Workers, actors and control plane.
    g4-workers go only to G4 nodes with a Ready vLLM pod (podAffinity), never keyed off a colo.active label."""

    recipe = "substrate"
    POOL_TAG = "jawadamin-us-west8-0726"
    G4_WORKER_PRIORITY = "substrate-worker"  # -5, bound next to vLLM by a MutatingAdmissionPolicy (podAffinity)

    def __init__(self, cluster_id: str = "substrate", cluster_info: dict | None = None):
        super().__init__(cluster_id=cluster_id, cluster_info=cluster_info)
        # build_state reads the base recipe from the cluster info; make it hold even when constructed bare.
        self.cluster_info = {**(cluster_info or CLUSTER), "base_recipe": "native"}
