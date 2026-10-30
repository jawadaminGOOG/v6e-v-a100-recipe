"""Pure aggregation: turn a "raw" cluster snapshot into the /api/state document.

The raw snapshot is a plain dict produced either by the live cluster fetcher
(cluster.py) or by the simulator (mock.py), so the same code path is exercised
in both modes. Raw format (all keys optional except nodes/pods):

    nodes:       [{name, labels, allocatable:{cpu, memory}, ready, created}]
    pods:        [{name, namespace, uid, labels, node, phase, ready,
                   cpu_req, mem_req_gib, priority_class}]
    deployments: [{name, namespace, replicas, ready}]
    hpas:        [{name, namespace, target, min, max, current, desired, metric}]
    events:      [{time, reason, kind, object, namespace, message, source, count}]
    agents:      {node_name: <node-agent GET /cpus JSON>}
    loadgen:     {loadgen_pod: <loadgen GET /stats JSON>}
    controller:  {state: <GET /state JSON> | None, config: <GET /config JSON> | None}
    configmap:   <colo-config config.json parsed> | None
    errors:      [str]
"""

from __future__ import annotations

import math
import os
import time
from datetime import datetime, timezone
from typing import Any

CLUSTER = {
    "name": os.environ.get("DEMO_CLUSTER", "g4cluster-jawadamin-us-west8-0726"),
    "region": os.environ.get("DEMO_REGION", "us-west8"),
    "project": os.environ.get("DEMO_PROJECT", "northam-ce-mlai-tpu"),
}

# Prices (USD/hr). n4d-standard-16 = 16 vCPU + 64 GiB at 3-yr CUD unit prices.
PRICE_G4_HR = float(os.environ.get("DEMO_PRICE_G4_HR", "1.98"))
PRICE_VCPU_HR = float(os.environ.get("DEMO_PRICE_VCPU_HR", "0.0131"))
PRICE_GIB_HR = float(os.environ.get("DEMO_PRICE_GIB_HR", "0.00149"))
PRICE_N4D16_HR = float(
    os.environ.get("DEMO_PRICE_N4D16_HR", str(round(16 * PRICE_VCPU_HR + 64 * PRICE_GIB_HR, 4)))
)

SERVICES = {
    "sandbox": "integer",
    "toolcall": "integer",
    "analytics": "memory-heavy",
}
NAMESPACES = ("inference", "colo-svc", "colo-system")
WATCH_REASONS = {
    "TriggeredScaleUp",
    "NotTriggerScaleUp",
    "ScaleDown",
    "ScaleDownEmpty",
    "ScaledUpGroup",
    "ColoGuardEvict",
    "ColoGuardRelease",
    "Preempted",
}
# If true, each loadgen replica's /stats "rate" is already the cluster total.
LOADGEN_RATE_IS_TOTAL = os.environ.get("DEMO_LOADGEN_RATE_IS_TOTAL", "0") == "1"


# ---------------------------------------------------------------- parsing utils
def parse_cpuset(s: str | None) -> list[int]:
    """'0-3,24-27' -> [0,1,2,3,24,25,26,27]."""
    out: list[int] = []
    if not s:
        return out
    for part in str(s).split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out.extend(range(int(a), int(b) + 1))
        else:
            out.append(int(part))
    return out


def parse_cpu(q: Any) -> float:
    """k8s CPU quantity -> cores."""
    if q is None:
        return 0.0
    if isinstance(q, (int, float)):
        return float(q)
    q = str(q)
    if q.endswith("m"):
        return float(q[:-1]) / 1000.0
    if q.endswith("n"):
        return float(q[:-1]) / 1e9
    return float(q)


_MEM = {"Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40, "K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12, "k": 1e3}


def parse_mem_gib(q: Any) -> float:
    """k8s memory quantity -> GiB."""
    if q is None:
        return 0.0
    if isinstance(q, (int, float)):
        return float(q) / 2**30
    q = str(q)
    for suf in ("Ki", "Mi", "Gi", "Ti", "K", "M", "G", "T", "k"):
        if q.endswith(suf):
            return float(q[: -len(suf)]) * _MEM[suf] / 2**30
    return float(q) / 2**30


def hist_percentile(hist: list[int] | None, q: float, bucket_ms: float) -> float | None:
    """Percentile (upper bucket edge, ms) from a fixed-width histogram.

    The last bucket is the overflow bucket; it reports its lower edge.
    """
    if not hist:
        return None
    total = sum(hist)
    if total <= 0:
        return None
    target = q * total
    acc = 0
    for i, c in enumerate(hist):
        acc += c
        if acc >= target:
            if i == len(hist) - 1:
                return float(i * bucket_ms)
            return float((i + 1) * bucket_ms)
    return float(len(hist) * bucket_ms)


def add_hist(a: list[int] | None, b: list[int] | None) -> list[int]:
    a = list(a or [])
    b = b or []
    if len(b) > len(a):
        a.extend([0] * (len(b) - len(a)))
    for i, v in enumerate(b):
        a[i] += v
    return a


def _age_s(created: str | None, now: float) -> float | None:
    if not created:
        return None
    try:
        dt = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
        return max(0.0, now - dt.timestamp())
    except ValueError:
        return None


def node_role(labels: dict) -> str:
    role = labels.get("colo.role")
    if role:
        return role
    cc = labels.get("cloud.google.com/compute-class", "")
    if cc == "g4-gpu":
        return "g4"
    if cc == "n4d-cpu":
        return "n4d"
    return "other"


def pod_category(pod: dict) -> str:
    labels = pod.get("labels") or {}
    if labels.get("app") == "vllm":
        return "vllm"
    cls = labels.get("colo.class")
    if cls == "integer":
        return "integer"
    if cls == "memory-heavy":
        return "memory"
    if labels.get("app") == "colo-buffer" or pod.get("priority_class") == "colo-buffer":
        return "buffer"
    return "system"


def _cpu_topology(ncpu: int, cpu: int) -> tuple[int, int, int]:
    """(physical core, thread, llc) assuming Linux sibling layout cpu, cpu+ncpu/2
    and 8 physical cores per L3 domain (AMD EPYC 9B45 CCD)."""
    half = max(1, ncpu // 2)
    core = cpu % half
    thread = cpu // half
    return core, thread, core // 8


# ---------------------------------------------------------------- loadgen merge
def merge_loadgen(loadgen: dict[str, dict], pods_by_name: dict[str, dict]) -> dict:
    per_vllm: dict[str, dict] = {}
    rates: list[float] = []
    bucket_ms = 2.0
    window_s = 30.0
    for lg_pod, st in (loadgen or {}).items():
        if not isinstance(st, dict):
            continue
        bucket_ms = float(st.get("bucket_ms", bucket_ms))
        window_s = float(st.get("window_s", window_s))
        if "rate_total" in st:  # colo-loadgen reports the cluster-wide total
            rates.append(float(st["rate_total"]))
        elif "total_rate" in st:
            rates.append(float(st["total_rate"]))
        elif "rate" in st:
            rates.append(float(st["rate"]))
        for vpod, d in (st.get("per_pod") or {}).items():
            m = per_vllm.setdefault(
                vpod, {"node": d.get("node"), "ttft_hist": [], "e2e_hist": [], "inflight": 0, "ok": 0, "err": 0}
            )
            m["node"] = m["node"] or d.get("node") or (pods_by_name.get(vpod) or {}).get("node")
            m["ttft_hist"] = add_hist(m["ttft_hist"], d.get("ttft_hist"))
            m["e2e_hist"] = add_hist(m["e2e_hist"], d.get("e2e_hist"))
            for k in ("inflight", "ok", "err"):
                m[k] += int(d.get(k) or 0)

    if LOADGEN_RATE_IS_TOTAL or any(("total_rate" in (s or {})) or ("rate_total" in (s or {})) for s in (loadgen or {}).values()):
        target = max(rates) if rates else 0.0
    else:
        target = sum(rates)

    def summarize(ttft, e2e, inflight, ok, err):
        done = sum(e2e) if e2e else ok
        return {
            "ttft_p50_ms": hist_percentile(ttft, 0.50, bucket_ms),
            "ttft_p99_ms": hist_percentile(ttft, 0.99, bucket_ms),
            "e2e_p50_ms": hist_percentile(e2e, 0.50, bucket_ms),
            "e2e_p99_ms": hist_percentile(e2e, 0.99, bucket_ms),
            "achieved_rps": round(done / window_s, 2) if window_s else None,
            "inflight": inflight,
            "ok": ok,
            "err": err,
        }

    per_node: dict[str, dict] = {}
    node_acc: dict[str, dict] = {}
    all_t: list[int] = []
    all_e: list[int] = []
    tot = {"inflight": 0, "ok": 0, "err": 0}
    out_pods = {}
    for vpod, m in per_vllm.items():
        out_pods[vpod] = {"node": m["node"], **summarize(m["ttft_hist"], m["e2e_hist"], m["inflight"], m["ok"], m["err"])}
        acc = node_acc.setdefault(m["node"] or "?", {"t": [], "e": [], "inflight": 0, "ok": 0, "err": 0})
        acc["t"] = add_hist(acc["t"], m["ttft_hist"])
        acc["e"] = add_hist(acc["e"], m["e2e_hist"])
        all_t = add_hist(all_t, m["ttft_hist"])
        all_e = add_hist(all_e, m["e2e_hist"])
        for k in tot:
            acc[k] += m[k]
            tot[k] += m[k]
    for node, acc in node_acc.items():
        per_node[node] = summarize(acc["t"], acc["e"], acc["inflight"], acc["ok"], acc["err"])
    overall = summarize(all_t, all_e, tot["inflight"], tot["ok"], tot["err"])
    return {
        "replicas": len(loadgen or {}),
        "target_rps": round(target, 1),
        "window_s": window_s,
        "bucket_ms": bucket_ms,
        **overall,
        "per_node": per_node,
        "per_vllm_pod": out_pods,
    }


# ---------------------------------------------------------------- counterfactual + errors
# n4d-standard-16 allocatable on GKE (used when no N4D node is up to read it from).
N4D_ALLOC_CPU_DEFAULT = float(os.environ.get("DEMO_N4D_ALLOC_CPU", "15.89"))
N4D_ALLOC_MEM_GIB_DEFAULT = float(os.environ.get("DEMO_N4D_ALLOC_MEM_GIB", "57.0"))


def counterfactual_n4d(pods: list[dict], cap_cpu: float, cap_mem_gib: float, overhead_cpu: float = 0.0,
                       overhead_mem_gib: float = 0.0, price_hr: float = PRICE_N4D16_HR) -> dict:
    """N4D nodes needed to host `pods` (all running CPU pods) if they ran on N4D without colo.

    First-fit-decreasing bin packing of each pod's (cpu, mem) requests onto n4d-standard-16
    allocatable, minus the per-node daemonset overhead observed on real N4D nodes. A pod
    that alone exceeds one node still takes one node (never under-counts).
    """
    cpu_cap = max(0.1, cap_cpu - overhead_cpu)
    mem_cap = max(0.1, cap_mem_gib - overhead_mem_gib)
    bins: list[list[float]] = []  # [free_cpu, free_mem]
    items = sorted(((float(p.get("cpu_req") or 0.0), float(p.get("mem_req_gib") or 0.0)) for p in pods),
                   key=lambda x: (x[0] / cpu_cap + x[1] / mem_cap), reverse=True)
    for c, m in items:
        for b in bins:
            if b[0] + 1e-9 >= c and b[1] + 1e-9 >= m:
                b[0] -= c
                b[1] -= m
                break
        else:
            bins.append([max(0.0, cpu_cap - c), max(0.0, mem_cap - m)])
    n = len(bins)
    return {
        "pods": len(items),
        "vcpu": round(sum(c for c, _ in items), 2),
        "mem_gib": round(sum(m for _, m in items), 2),
        "n4d_nodes": n,
        "node_cpu": round(cpu_cap, 2),
        "node_mem_gib": round(mem_cap, 2),
        "price_n4d16_hr": price_hr,
        "per_hour": round(n * price_hr, 4),
        "method": "first-fit-decreasing pack of G4 backfill pod requests onto n4d-standard-16 allocatable",
    }


_AUTH_MARKERS = ("401", "403", "unauthorized", "forbidden", "invalid_grant", "reauth", "refresherror",
                 "default credentials", "token")
_NET_MARKERS = ("max retries", "connection refused", "newconnectionerror", "timed out", "timeout",
                "name or service not known", "temporary failure in name resolution", "connectionreseterror",
                "remotedisconnected", "network is unreachable")


def classify_errors(errors: list[str], have_nodes: bool) -> list[dict]:
    """Turn raw backend error strings into at most a few presenter-readable alerts."""
    out: list[dict] = []
    low = [(e, e.lower()) for e in errors or []]
    auth = [e for e, l in low if any(k in l for k in _AUTH_MARKERS) and ("http" in l or "status" in l or
                                                                       "unauthorized" in l or "forbidden" in l or
                                                                       "credential" in l or "token" in l)]
    net = [e for e, l in low if e not in auth and any(k in l for k in _NET_MARKERS)]
    ctrl = [e for e, l in low if e not in auth and e not in net and l.startswith("controller")]
    rest = [e for e, _ in low if e not in auth and e not in net and e not in ctrl]
    if auth:
        out.append({"kind": "auth", "severity": "error",
                    "title": "The GKE API rejected the demo's credentials",
                    "detail": "Cluster data is not updating. Check the demo service account's IAM and RBAC "
                              "(container.clusters.get + the colo-demo roles).",
                    "raw": auth[:3]})
    if net:
        out.append({"kind": "unreachable", "severity": "error" if not have_nodes else "warn",
                    "title": "Cluster unreachable" if not have_nodes else "Some cluster calls are timing out",
                    "detail": "The demo cannot reach the GKE API or an in-cluster service; numbers may be stale.",
                    "raw": net[:3]})
    if ctrl:
        out.append({"kind": "controller", "severity": "warn", "title": "Colo controller not reachable",
                    "detail": "Placement still runs in the cluster, but demand and guard changes cannot be applied.",
                    "raw": ctrl[:3]})
    if rest:
        out.append({"kind": "other", "severity": "warn", "title": f"{len(rest)} backend call(s) failed",
                    "detail": "Some panels may be incomplete.", "raw": rest[:3]})
    return out


# ---------------------------------------------------------------- main builder
def build_state(raw: dict, mode: str = "live") -> dict:
    now = time.time()
    errors = list(raw.get("errors") or [])
    pods = [p for p in raw.get("pods") or [] if p.get("phase") not in ("Succeeded", "Failed")]
    pods_by_uid = {p["uid"]: p for p in pods if p.get("uid")}
    pods_by_name = {p["name"]: p for p in pods}
    pods_by_node: dict[str, list[dict]] = {}
    for p in pods:
        if p.get("node"):
            pods_by_node.setdefault(p["node"], []).append(p)

    lg = merge_loadgen(raw.get("loadgen") or {}, pods_by_name)
    agents = raw.get("agents") or {}

    nodes_out = []
    counts = {"g4": 0, "n4d": 0, "loadgen": 0, "other": 0, "g4_ready": 0, "n4d_ready": 0}
    reclaimed_vcpu = 0.0
    reclaimed_mem = 0.0
    cost_g4 = 0.0
    cost_n4d = 0.0
    backfill_on_g4: list[dict] = []
    overflow_on_n4d: list[dict] = []
    g4_alloc = g4_vllm = g4_sys = g4_buffer = 0.0
    vllm_slices: list[float] = []
    overflow_pods = 0
    overflow_vcpu = 0.0
    n4d_caps: list[tuple[float, float, float, float]] = []  # (alloc_cpu, alloc_mem, ds_cpu, ds_mem)
    for n in sorted(raw.get("nodes") or [], key=lambda n: (node_role(n.get("labels") or {}), n.get("created") or "", n["name"])):
        labels = n.get("labels") or {}
        role = node_role(labels)
        counts[role] = counts.get(role, 0) + 1
        if n.get("ready"):
            counts[f"{role}_ready"] = counts.get(f"{role}_ready", 0) + 1
        if role == "g4":
            cost_g4 += PRICE_G4_HR
        elif role == "n4d":
            cost_n4d += PRICE_N4D16_HR
        alloc = n.get("allocatable") or {}
        node_pods = []
        for p in pods_by_node.get(n["name"], []):
            if p.get("namespace") not in ("inference", "colo-svc") and pod_category(p) != "buffer":
                # system / agent pods are not drawn as blocks (they live in the shared CPU pool);
                # the warm buffer (colo-system in the recipe) is drawn.
                continue
            pl = p.get("labels") or {}
            node_pods.append(
                {
                    "name": p["name"],
                    "ns": p.get("namespace"),
                    "app": pl.get("app"),
                    "tier": pl.get("colo.tier"),
                    "cat": pod_category(p),
                    "cpu": p.get("cpu_req", 0.0),
                    "mem_gib": round(p.get("mem_req_gib", 0.0), 2),
                    "phase": p.get("phase"),
                    "ready": bool(p.get("ready")),
                }
            )
            if role == "g4" and n.get("ready") and pl.get("colo.tier") == "backfill" and p.get("phase") == "Running":
                reclaimed_vcpu += p.get("cpu_req", 0.0)
                reclaimed_mem += p.get("mem_req_gib", 0.0)
                backfill_on_g4.append(p)
            if role == "n4d" and pl.get("colo.tier") == "overflow" and p.get("phase") == "Running":
                overflow_pods += 1
                overflow_vcpu += p.get("cpu_req", 0.0)
                overflow_on_n4d.append(p)

        # vCPU budget of this node: what the GPU slice, system pods and colo pods use.
        node_all = pods_by_node.get(n["name"], [])
        # System = daemonsets/agents; the warm buffer is preemptible capacity, not overhead.
        sys_pods = [p for p in node_all if p.get("namespace") not in ("inference", "colo-svc") and pod_category(p) != "buffer"]
        sys_cpu = sum(p.get("cpu_req", 0.0) for p in sys_pods)
        sys_mem = sum(p.get("mem_req_gib", 0.0) for p in sys_pods)
        if role == "g4" and n.get("ready"):
            g4_alloc += parse_cpu(alloc.get("cpu"))
            g4_sys += sys_cpu
            for p in node_all:
                cat = pod_category(p)
                if cat == "vllm":
                    g4_vllm += p.get("cpu_req", 0.0)
                    vllm_slices.append(p.get("cpu_req", 0.0))
                elif cat == "buffer":
                    g4_buffer += p.get("cpu_req", 0.0)
        elif role == "n4d" and n.get("ready"):
            n4d_caps.append((parse_cpu(alloc.get("cpu")), parse_mem_gib(alloc.get("memory")), sys_cpu, sys_mem))

        agent = agents.get(n["name"]) or {}
        ncpu = int(agent.get("ncpu") or round(parse_cpu(alloc.get("cpu_capacity") or 0)) or (48 if role == "g4" else 16 if role == "n4d" else 0))
        busy = agent.get("busy") or []
        cms = agent.get("cpu_manager_state") or {}
        owner: dict[int, dict] = {}
        for uid, containers in (cms.get("entries") or {}).items():
            p = pods_by_uid.get(uid)
            for cname, cset in (containers or {}).items():
                for c in parse_cpuset(cset):
                    if p:
                        pl = p.get("labels") or {}
                        owner[c] = {
                            "pod": p["name"],
                            "ns": p.get("namespace"),
                            "app": pl.get("app"),
                            "tier": pl.get("colo.tier"),
                            "container": cname,
                            "cat": pod_category(p),
                        }
                    else:
                        owner[c] = {"pod": uid[:8], "ns": None, "app": None, "tier": None, "container": cname, "cat": "system"}
        cpus = []
        if agent:
            for c in range(ncpu):
                core, thread, llc = _cpu_topology(ncpu, c)
                o = owner.get(c)
                cpus.append(
                    {
                        "id": c,
                        "core": core,
                        "thread": thread,
                        "llc": llc,
                        "cat": o["cat"] if o else "shared",
                        "owner": o,
                        "busy": round(float(busy[c]), 1) if c < len(busy) and busy[c] is not None else None,
                    }
                )
        blocks = sorted(k.split("colo.block.", 1)[1] for k, v in labels.items() if k.startswith("colo.block.") and str(v) == "true")
        lgn = lg["per_node"].get(n["name"]) or {}
        nodes_out.append(
            {
                "name": n["name"],
                "role": role,
                "zone": labels.get("topology.kubernetes.io/zone"),
                "compute_class": labels.get("cloud.google.com/compute-class"),
                "instance_type": labels.get("node.kubernetes.io/instance-type"),
                "ready": bool(n.get("ready")),
                "created": n.get("created"),
                "age_s": _age_s(n.get("created"), now),
                "alloc_cpu": parse_cpu(alloc.get("cpu")),
                "alloc_mem_gib": round(parse_mem_gib(alloc.get("memory")), 1),
                "ncpu": ncpu,
                "cpus": cpus,
                "agent_ok": bool(agent),
                "mem_used_gib": agent.get("mem_used_gib"),
                "pods": sorted(node_pods, key=lambda p: ({"vllm": 0, "integer": 1, "memory": 2, "buffer": 3}.get(p["cat"], 4), p["name"])),
                "blocks": blocks,
                "sys_cpu": round(sys_cpu, 2),  # daemonset/agent requests (shared pool, not exclusive)
                "ttft_p99_ms": lgn.get("ttft_p99_ms"),
                "ttft_p50_ms": lgn.get("ttft_p50_ms"),
                "achieved_rps": lgn.get("achieved_rps"),
            }
        )

    deps = {(d.get("namespace"), d["name"]): d for d in raw.get("deployments") or []}
    hpa = next((h for h in raw.get("hpas") or [] if h.get("target") == "vllm" or h.get("name") == "vllm"), None)
    vdep = deps.get(("inference", "vllm")) or {}
    vllm_pods = [p for p in pods if (p.get("labels") or {}).get("app") == "vllm"]
    vllm = {
        "desired": vdep.get("replicas") if vdep else (hpa or {}).get("desired"),
        "ready": vdep.get("ready") if vdep else sum(1 for p in vllm_pods if p.get("ready")),
        "pods": len(vllm_pods),
        "hpa": hpa,
    }

    cm = raw.get("configmap") or {}
    ctrl = raw.get("controller") or {}
    cstate = ctrl.get("state") if isinstance(ctrl.get("state"), dict) else None
    cconf = ctrl.get("config") if isinstance(ctrl.get("config"), dict) else None
    conf = cconf or (cstate or {}).get("config") or cm or {}
    demand = (cstate or {}).get("demand") or conf.get("demand") or {}
    guard = conf.get("guard") or {}
    baseline = None
    for src in (cstate or {}, guard):
        for k in ("baseline_ttft_p99_ms", "baseline"):
            v = src.get(k)
            if isinstance(v, dict):
                v = v.get("ttft_p99_ms") or v.get("value")
            if isinstance(v, (int, float)):
                baseline = float(v)
                break
        if baseline is not None:
            break
    guard_enabled = guard.get("enabled")
    if cstate and isinstance(cstate.get("guard"), dict) and "enabled" in cstate["guard"]:
        guard_enabled = cstate["guard"]["enabled"]

    services = {}
    for svc, cls in SERVICES.items():
        entry = {"class": cls, "demand": demand.get(svc)}
        for tier in ("backfill", "overflow"):
            d = deps.get(("colo-svc", f"{svc}-{tier}")) or {}
            tp = [p for p in pods if (p.get("labels") or {}).get("app") == svc and (p.get("labels") or {}).get("colo.tier") == tier]
            entry[tier] = {
                "desired": d.get("replicas", len(tp)) if d else len(tp),
                "ready": d.get("ready", sum(1 for p in tp if p.get("ready"))) if d else sum(1 for p in tp if p.get("ready")),
            }
        services[svc] = entry
    buf = deps.get(("colo-system", "colo-buffer")) or deps.get(("colo-svc", "colo-buffer")) or {}

    events = sorted(raw.get("events") or [], key=lambda e: e.get("time") or "", reverse=True)[:50]

    reclaimed_value = reclaimed_vcpu * PRICE_VCPU_HR + reclaimed_mem * PRICE_GIB_HR

    # Counterfactual "CPU on N4D, no colo": every CPU pod now running (backfill on G4 + overflow on N4D)
    # packed onto N4D. Savings = N4D nodes that world needs beyond the N4D nodes running today.
    cf_pods = backfill_on_g4 + overflow_on_n4d
    if n4d_caps:
        # Least daemonset overhead seen on a Ready N4D node (the buffer is excluded above).
        cap = min(n4d_caps, key=lambda c: (c[2], c[3]))
        cf = counterfactual_n4d(cf_pods, cap[0], cap[1], cap[2], cap[3])
    else:
        cf = counterfactual_n4d(cf_pods, N4D_ALLOC_CPU_DEFAULT, N4D_ALLOC_MEM_GIB_DEFAULT)
    n4d_now = counts.get("n4d", 0)
    saved_nodes = max(0, cf["n4d_nodes"] - n4d_now)
    cf["n4d_nodes_now"] = n4d_now
    cf["saved_nodes"] = saved_nodes
    cf["fleet_per_hour"] = round(cost_g4 + cf["n4d_nodes"] * PRICE_N4D16_HR, 4)
    cf["saved_per_hour"] = round(saved_nodes * PRICE_N4D16_HR, 4)
    cf["saved_pct"] = round(100.0 * cf["saved_per_hour"] / cf["fleet_per_hour"], 1) if cf["fleet_per_hour"] else 0.0
    cf["method"] = ("first-fit-decreasing pack of all running CPU pods (G4 backfill + N4D overflow) onto "
                    "n4d-standard-16 allocatable minus daemonset requests; saved = nodes needed - N4D nodes running")

    # Spare = what is left on Ready G4 nodes after the GPU slice and system pods; colo fills it.
    g4_spare = max(0.0, g4_alloc - g4_vllm - g4_sys)
    story = {
        "gpu_slice_vcpu": round(max(vllm_slices), 2) if vllm_slices else None,
        "gpu_slice_total_vcpu": round(g4_vllm, 2),
        "g4_alloc_vcpu": round(g4_alloc, 2),
        "g4_system_vcpu": round(g4_sys, 2),
        "g4_spare_vcpu": round(g4_spare, 2),
        "g4_buffer_vcpu": round(g4_buffer, 2),
        "reclaimed_vcpu": round(reclaimed_vcpu, 2),
        "reclaimed_pods": len(backfill_on_g4),
        "reclaimed_pct_of_spare": round(min(100.0, 100.0 * reclaimed_vcpu / g4_spare), 1) if g4_spare > 0 else None,
        "g4_idle_vcpu": round(max(0.0, g4_spare - reclaimed_vcpu - g4_buffer), 2),
        "overflow_pods": overflow_pods,
        "overflow_vcpu": round(overflow_vcpu, 2),
        "n4d_nodes": counts.get("n4d", 0),
        "saved_per_hour": cf["saved_per_hour"],
        "saved_pct": cf["saved_pct"],
        "counterfactual_n4d_nodes": cf["n4d_nodes"],
        "saved_nodes": saved_nodes,
    }
    out = {
        "ts": now,
        "mode": mode,
        "cluster": CLUSTER,
        "nodes": nodes_out,
        "counts": counts,
        "story": story,
        "alerts": classify_errors(errors, have_nodes=bool(raw.get("nodes"))),
        "vllm": vllm,
        "loadgen": lg,
        "services": services,
        "buffer": {"desired": buf.get("replicas"), "ready": buf.get("ready")},
        "controller": {
            "reachable": cstate is not None,
            "demand": demand,
            "guard_enabled": guard_enabled,
            "baseline_ttft_p99_ms": baseline,
            "factor": guard.get("factor", 1.5),
            "threshold_ms": (float((cstate or {})["threshold_ttft_p99_ms"]) if isinstance((cstate or {}).get("threshold_ttft_p99_ms"), (int, float))
                             else baseline * float(guard.get("factor", 1.5)) if baseline else None),
            "config": conf,
            "state": cstate,
        },
        "events": events,
        "cost": {
            "per_hour": round(cost_g4 + cost_n4d, 3),
            "g4_per_hour": round(cost_g4, 3),
            "n4d_per_hour": round(cost_n4d, 3),
            "reclaimed_vcpu": round(reclaimed_vcpu, 1),
            "reclaimed_mem_gib": round(reclaimed_mem, 1),
            "reclaimed_per_hour": round(reclaimed_value, 4),
            "counterfactual": cf,
            "saved_per_hour": cf["saved_per_hour"],
            "prices": {
                "g4_hr": PRICE_G4_HR,
                "n4d16_hr": PRICE_N4D16_HR,
                "vcpu_hr": PRICE_VCPU_HR,
                "gib_hr": PRICE_GIB_HR,
            },
        },
        "errors": errors,
    }
    # Additive field for the UI ("N of M G4 slots"); same source as the scenario text.
    try:
        story["g4_slots_per_node"] = _g4_slots_per_node(out)
    except Exception:  # noqa: BLE001 - never break /api/state for a display hint
        story["g4_slots_per_node"] = None
    return out


def _g4_slots_per_node(state: dict) -> int | None:
    """2-vCPU integer slots the controller actually places on one G4 node (used in scenario text).

    Prefer the controller's own per-node slot count (it subtracts reserve_cpu_per_node); fall back
    to spare vCPU minus the configured reserve when the controller state is unavailable.
    """
    ctrl = state.get("controller") or {}
    rows = ((ctrl.get("state") or {}).get("nodes")) or []
    slots = []
    for r in rows:
        sl = r.get("slots") if isinstance(r, dict) else None
        if isinstance(sl, dict) and r.get("eligible"):
            slots.append(int(sl.get("sandbox") or 0))
    if slots:
        return max(slots)
    spare = (state.get("story") or {}).get("g4_spare_vcpu")
    if not spare:
        return None
    nodes = max(1, state["counts"].get("g4_ready", 0) or 1)
    reserve = float(((ctrl.get("config") or {}).get("reserve_cpu_per_node")) or 0)
    return max(0, int((spare / nodes - reserve) // 2))


def summarize(state: dict) -> dict:
    """Flat metrics used by scenario conditions and the CLI observation log."""
    svc = state.get("services") or {}
    lg = state.get("loadgen") or {}
    ctrl = state.get("controller") or {}
    blocks = sum(len(n.get("blocks") or []) for n in state.get("nodes") or [])
    m = {
        "t": state.get("ts"),
        "target_rps": lg.get("target_rps"),
        "achieved_rps": lg.get("achieved_rps"),
        "ttft_p50_ms": lg.get("ttft_p50_ms"),
        "ttft_p99_ms": lg.get("ttft_p99_ms"),
        "err": lg.get("err"),
        "vllm_desired": (state.get("vllm") or {}).get("desired") or 0,
        "vllm_ready": (state.get("vllm") or {}).get("ready") or 0,
        "g4_nodes": state["counts"].get("g4", 0),
        "g4_ready": state["counts"].get("g4_ready", 0),
        "n4d_nodes": state["counts"].get("n4d", 0),
        "n4d_ready": state["counts"].get("n4d_ready", 0),
        "backfill_desired": sum(s["backfill"]["desired"] or 0 for s in svc.values()),
        "backfill_ready": sum(s["backfill"]["ready"] or 0 for s in svc.values()),
        "overflow_desired": sum(s["overflow"]["desired"] or 0 for s in svc.values()),
        "overflow_ready": sum(s["overflow"]["ready"] or 0 for s in svc.values()),
        "memory_backfill_ready": sum(s["backfill"]["ready"] or 0 for s in svc.values() if s["class"] == "memory-heavy"),
        "integer_backfill_ready": sum(s["backfill"]["ready"] or 0 for s in svc.values() if s["class"] == "integer"),
        "guard_blocks": blocks,
        "guard_enabled": ctrl.get("guard_enabled"),
        "baseline_ms": ctrl.get("baseline_ttft_p99_ms"),
        "threshold_ms": ctrl.get("threshold_ms"),
        "cost_per_hour": (state.get("cost") or {}).get("per_hour"),
        "reclaimed_vcpu": (state.get("cost") or {}).get("reclaimed_vcpu"),
        "guard_evict_events": sum(1 for e in state.get("events") or [] if e.get("reason") == "ColoGuardEvict"),
        "scale_up_events": sum(1 for e in state.get("events") or [] if e.get("reason") == "TriggeredScaleUp"),
        "controller_reachable": bool(ctrl.get("reachable")),
        "hpa_min": ((state.get("vllm") or {}).get("hpa") or {}).get("min"),
        "saved_per_hour": (state.get("story") or {}).get("saved_per_hour"),
        "g4_spare_vcpu": (state.get("story") or {}).get("g4_spare_vcpu"),
        # 2-vCPU integer pods that fit in one G4 node's spare (used for scenario note text).
        "g4_slots_per_node": _g4_slots_per_node(state),
    }
    for name, s in svc.items():
        m[f"{name}_backfill_ready"] = s["backfill"]["ready"] or 0
        m[f"{name}_overflow_ready"] = s["overflow"]["ready"] or 0
    return m


def finite(x: Any) -> bool:
    return isinstance(x, (int, float)) and math.isfinite(x)
