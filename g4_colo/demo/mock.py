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

import math
import os
import random
import string
import time
from datetime import datetime, timezone

from state import SERVICES, build_state

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


class MockBackend:
    mode = "mock"

    def __init__(self):
        now = time.time()
        self.t = now
        self.rate = 300.0
        self.eff_rate = 300.0
        self.demand = {"sandbox": 6, "toolcall": 6, "analytics": 2}
        self.guard = {"enabled": True, "baseline_ttft_p99_ms": 60.0, "factor": 1.5, "cooldown_s": GUARD_COOLDOWN_S, "window_s": 30}
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
        pool = {"g4": "g4gpu", "n4d": "n4dcpu", "loadgen": "loadgen"}[role]
        name = f"gke-g4cluster-{pool}-jawadamin-us-west8-1026-{_sfx(4)}-{_sfx(4)}"
        cc = {"g4": "g4-gpu", "n4d": "n4d-cpu"}.get(role)
        labels = {
            "colo.role": role,
            "topology.kubernetes.io/zone": zone,
            "node.kubernetes.io/instance-type": {"g4": "g4-standard-48", "n4d": "n4d-standard-16", "loadgen": "e2-standard-8"}[role],
        }
        if cc:
            labels["cloud.google.com/compute-class"] = cc
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

    def _event(self, reason: str, kind: str, obj: str, msg: str, ns: str | None = None, source: str = "cluster-autoscaler"):
        self.events.append({"time": _iso(self.t), "reason": reason, "kind": kind, "object": obj, "namespace": ns, "message": msg, "source": source, "count": 1})
        self.events = self.events[-200:]

    def _ready(self, n: dict) -> bool:
        return self.t >= n["ready_at"]

    def _free_core(self, node: dict) -> int | None:
        ncores = 24 if node["role"] == "g4" else 8
        for c in range(1, ncores):
            if c not in node["cores"]:
                return c
        return None

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
                        self._event("TriggeredScaleUp", "Pod", v["name"], "pod triggered scale-up: [{g4gpu-jawadamin-us-west8-1026 1->2 (max: 2)}]", "inference")
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

        # --- controller allocation
        eligible = [self.nodes[v["node"]] for v in self.vllm if v["node"] and now >= v["ready_at"]]
        slots = {n["name"]: G4_SLOTS for n in eligible}
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

        # --- N4D autoscaling
        n4ds = [n for n in self.nodes.values() if n["role"] == "n4d"]
        pending = [p for p in self.svc_pods + self.buffer_pods if p["tier"] in ("overflow", "buffer") and p["node"] is None]
        if pending and not any(not self._ready(n) for n in n4ds):
            need = math.ceil(len(pending) / N4D_SLOTS)
            for i in range(need):
                zone = "us-west8-a" if (len(n4ds) + i) % 2 == 0 else "us-west8-c"
                nn = self._add_node("n4d", zone, now)
                n4ds.append(nn)
            self._event("TriggeredScaleUp", "Pod", pending[0]["name"], f"pod triggered scale-up: [{{n4dcpu-jawadamin-us-west8-1026 {len(n4ds) - need}->{len(n4ds)} (max: 10)}}]", "colo-svc")
        for n in list(n4ds):
            used = [p for p in self.svc_pods + self.buffer_pods if p["node"] == n["name"]]
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
                elif "integer" not in n["blocks"] and now - n["blocks"]["memory-heavy"] >= 60 and now - n["breach_since"] >= 60:
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
            alloc = {"g4": (G4_ALLOC_CPU, "176Gi"), "n4d": (N4D_ALLOC_CPU, "58Gi"), "loadgen": (7.9, "28Gi")}[n["role"]]
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
            half = 24 if n["role"] == "g4" else 8
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
                n["name"]: {"spare_cpu": G4_SLOTS * 2, "blocks": sorted(n["blocks"]), "p99_ms": round(self.node_p99.get(n["name"], 0), 1)}
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
        return build_state(self.raw(), mode=self.mode)

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
