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
    substrate:   recipe "substrate" only, see build_substrate (WorkerPools, substrate-exp HPAs, actor-stats)
    errors:      [str]
"""

from __future__ import annotations

import json
import math
import os
import re
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
# ct6e-standard-1t (1 TPU v6e chip, 44 vCPU, 176 GiB): on-demand list price per chip-hour (Spot is about half).
PRICE_V6E_HR = float(os.environ.get("DEMO_PRICE_V6E_HR", "2.70"))

# ---------------------------------------------------------------- accelerator profiles
# The accelerator host of a cluster (the node that runs vLLM and lends its spare cores to CPU services). Internally
# that node keeps the role key "g4" (the recipe's class identifiers: compute-class g4-gpu, colo.role=g4) whatever the
# accelerator is; everything a person reads comes from the profile.
#   name:    selector / heading name       host: word in prose ("Reclaimed on G4")
#   tag:     node tile label ("G4 #1")     short: one-letter node id in lane tooltips ("G1")
#   unit:    accelerator unit ("GPU")      chip: the accelerator itself
#   machine: GKE machine type              vcpu: host vCPUs (fallback when no node agent reports)
#   price_hr: USD per node-hour (DEMO_PRICE_G4_HR / DEMO_PRICE_V6E_HR)
ACCELERATORS: dict[str, dict] = {
    "g4": {"id": "g4", "name": "G4 · RTX PRO 6000", "host": "G4", "tag": "G4", "short": "G", "unit": "GPU",
           "chip": "RTX PRO 6000", "machine": "g4-standard-48", "vcpu": 48, "price_hr": PRICE_G4_HR},
    "tpu-v6e": {"id": "tpu-v6e", "name": "TPU v6e", "host": "TPU v6e", "tag": "v6e", "short": "T", "unit": "TPU",
                "chip": "TPU v6e", "machine": "ct6e-standard-1t", "vcpu": 44, "price_hr": PRICE_V6E_HR},
}
DEFAULT_ACCELERATOR = "g4"


def accel_profile(accel: str | dict | None = None) -> dict:
    """Profile for an accelerator id (or a cluster dict carrying "accelerator"); unknown or missing -> G4."""
    if isinstance(accel, dict):
        accel = accel.get("accelerator")
    return dict(ACCELERATORS.get(accel or DEFAULT_ACCELERATOR) or ACCELERATORS[DEFAULT_ACCELERATOR])


_G4_TILE = re.compile(r"\bG4 #")
_G4 = re.compile(r"\bG4\b")
_GPU = re.compile(r"\bGPU(?=s?\b)")


def localize(text: Any, prof: dict | None) -> Any:
    """Rewrite G4/GPU wording of a display string for the cluster's accelerator. Identity on G4 clusters, so their
    text stays exactly as written. Case-sensitive: identifiers such as g4_nodes or g4-standard-48 are untouched.
    static/app.js has the same rules (function L)."""
    if not isinstance(text, str) or not text or not prof or prof.get("id") == DEFAULT_ACCELERATOR:
        return text
    t = text.replace("RTX PRO 6000", prof["chip"])
    t = _G4_TILE.sub(prof["tag"] + " #", t)
    t = _G4.sub(prof["host"], t)
    return _GPU.sub(prof["unit"], t)

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


# Agent Substrate (github.com/agent-substrate/substrate): the experiment's WorkerPools (ate.dev/v1), their Worker pods
# (label ate.dev/worker-pool=<pool>) and the n4d-workers HPA live in substrate-exp; ate-system holds only Substrate's
# control plane and the atelet DaemonSet (drawn like any other DaemonSet). Actor statistics come from the probe's
# ConfigMap substrate-exp/actor-stats (key stats.json).
SUBSTRATE_GROUP = "ate.dev"
SUBSTRATE_VERSION = "v1"
SUBSTRATE_POOL_LABEL = "ate.dev/worker-pool"
SUBSTRATE_NS = "substrate-exp"
SUBSTRATE_G4_POOL = "g4-workers"
SUBSTRATE_N4D_POOL = "n4d-workers"
SUBSTRATE_STATS_NS = "substrate-exp"
SUBSTRATE_STATS_CM = "actor-stats"
# Substrate control plane: namespace ate-system on dedicated nodes keyed by this label (pool names are not fixed).
ATE_SYSTEM_NS = "ate-system"
# The pod-certificate controller Substrate relies on runs outside ate-system (Deployment podcertificate-controller).
PODCERT_NS = "podcertificate-controller-system"
ATE_WORKLOAD_LABEL = "ate.dev/workloadType"
ATE_CP_NODE_KINDS = {"ate-system-infra": "infra", "ate-postgres": "postgres"}
# (key, label, name substrings) in match order: the pod-certificate controller must match before the generic controller.
ATE_COMPONENTS = (
    ("podcert", "podcert", ("podcertificate", "pod-certificate", "podcert")),
    ("api-server", "api-server", ("api-server", "apiserver")),
    ("controller", "controller", ("ate-controller",)),
    ("router", "router", ("router",)),
    ("egress", "egress", ("egress",)),
    ("idp", "idp", ("idp",)),
    ("postgres", "postgres", ("postgres",)),
    ("atelet", "atelet", ("atelet",)),
)
ATE_DISPLAY_ORDER = ("api-server", "controller", "router", "egress", "idp", "podcert", "postgres")

# Pod categories drawn on the node map although they live outside inference / colo-svc.
_DRAWN_EXTRA = ("buffer", "worker")

# Platform namespaces whose DaemonSets are part of GKE or of the recipe (drawn as "reserved", not as your agents).
_PLATFORM_NS_PREFIX = ("kube-", "gke-", "gmp-")
_PLATFORM_NS = {"colo-system", "inference", "colo-svc"}


def is_customer_daemonset(pod: dict) -> bool:
    """A DaemonSet pod you run (log shipper, security sensor...), as opposed to GKE's or the recipe's own agents."""
    ns = pod.get("namespace") or ""
    return pod.get("owner_kind") == "DaemonSet" and ns not in _PLATFORM_NS and not ns.startswith(_PLATFORM_NS_PREFIX)


def pod_category(pod: dict, workers: bool = False) -> str:
    """Drawing category of a pod. `workers` (substrate recipe only) gives Agent Substrate Worker pods their own
    category; elsewhere they stay "system" exactly as before, so the other tabs are unchanged."""
    labels = pod.get("labels") or {}
    if labels.get("app") == "vllm":
        return "vllm"
    if workers and labels.get(SUBSTRATE_POOL_LABEL):
        return "worker"  # Agent Substrate Worker pod (substrate-exp), drawn as "W"
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


# ---------------------------------------------------------------- recipes
# "controller": g4_colo/recipe/ (colo-controller in colo-system splits demand into backfill + overflow).
# "native":     g4_colo/recipe-native/ (no controller: <svc>-backfill replicas = demand, extra backfill pods stay
#               Pending at priority -100, and the <svc>-overflow HPAs cover them on N4D).
RECIPES = ("controller", "native", "substrate")
GUARD_NA_REASON = "memory-heavy services are kept off G4 by admission policy"
# Demand Deployment per service on the native recipe (memory-heavy services never backfill there).
NATIVE_DEMAND_DEPLOYMENT = {"sandbox": "sandbox-backfill", "toolcall": "toolcall-backfill", "analytics": "analytics-overflow"}
NATIVE_OVERFLOW_HPAS = ("sandbox-overflow", "toolcall-overflow")


class UnsupportedOnRecipe(RuntimeError):
    """The action has no equivalent on this cluster's recipe (e.g. the latency guard on the native recipe)."""


def _num(x: Any) -> float | None:
    """HPA metric quantity ('3', '1500m', 2.0) -> float."""
    if x is None:
        return None
    try:
        return round(parse_cpu(x), 3)
    except (TypeError, ValueError):
        return None


def _vllm_ready_nodes(pods: list[dict]) -> set[str]:
    return {p["node"] for p in pods if (p.get("labels") or {}).get("app") == "vllm" and p.get("node")
            and p.get("phase") == "Running" and p.get("ready")}


# ---------------------------------------------------------------- Agent Substrate (recipe "substrate")
# Raw format (all optional; produced by cluster.SubstrateLiveBackend and mock.SubstrateMockBackend):
#   substrate: {workerpools: [WorkerPool JSON as the API returns it] | None (CRD absent or unreadable),
#               hpas: [hpa dicts as in raw["hpas"], namespace substrate-exp] | None, actor_stats: <stats.json str or dict> | None,
#               notes: [str]  (why something is missing; shown in the panel, never as a cluster alert)}
def _int_or_none(x: Any) -> int | None:
    if isinstance(x, bool):
        return None
    if isinstance(x, (int, float)) and math.isfinite(x):
        return int(x)
    if isinstance(x, str):
        try:
            return int(float(x))
        except ValueError:
            return None
    return None


def _float_or_none(x: Any) -> float | None:
    if isinstance(x, bool):
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def parse_workerpool(obj: Any) -> dict | None:
    """One ate.dev/v1 WorkerPool (dict from the API) -> {name, namespace, desired, replicas, ready}. None if unusable."""
    if not isinstance(obj, dict):
        return None
    meta = obj.get("metadata") if isinstance(obj.get("metadata"), dict) else {}
    name = meta.get("name")
    if not name:
        return None
    spec = obj.get("spec") if isinstance(obj.get("spec"), dict) else {}
    status = obj.get("status") if isinstance(obj.get("status"), dict) else {}
    return {
        "name": str(name),
        "namespace": meta.get("namespace"),
        "desired": _int_or_none(spec.get("replicas")),
        "replicas": _int_or_none(status.get("replicas")),
        # readyReplicas is omitempty: absent with a status means 0 ready.
        "ready": _int_or_none(status.get("readyReplicas")) if "readyReplicas" in status else (0 if status else None),
    }


_STATS_FLOATS = ("resumes_per_s", "resume_p50_ms", "resume_p99_ms")
# Cumulative since the generator started: lost_actors = CRASHED (true loss); resumes_waited_no_room = Substrate refused
# a resume because the pool was full (the actor waits; expected under oversubscription); memory_resets = resumed without
# in-memory state (snapshot restored on a different CPU feature set; files kept); failed_requests = other failures.
_STATS_INTS = ("actors_total", "actors_running", "lost_actors", "resumes_waited_no_room", "memory_resets", "failed_requests")


def parse_actor_stats(raw: Any) -> tuple[dict | None, str | None]:
    """The probe's stats.json (str or already-parsed dict) -> (normalized stats, problem). Every field is optional;
    wrong-typed fields are dropped, not fatal. (None, reason) when absent or not a JSON object."""
    if raw is None or raw == "":
        return None, None
    data = raw
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            return None, f"actor-stats stats.json is not valid JSON ({e.msg})"
    if not isinstance(data, dict):
        return None, "actor-stats stats.json is not a JSON object"
    out: dict = {"updated": data.get("updated") if isinstance(data.get("updated"), str) else None}
    for k in _STATS_INTS:
        out[k] = _int_or_none(data.get(k))
    for k in _STATS_FLOATS:
        out[k] = _float_or_none(data.get(k))
    by_node = data.get("actors_by_node")
    out["actors_by_node"] = ({str(n): v for n, v in ((n, _int_or_none(c)) for n, c in by_node.items()) if v is not None}
                             if isinstance(by_node, dict) else None)
    by_pool = data.get("resume_p99_ms_by_pool")
    out["resume_p99_ms_by_pool"] = ({str(p): v for p, v in ((p, _float_or_none(x)) for p, x in by_pool.items()) if v is not None}
                                    if isinstance(by_pool, dict) else None)
    # actor-load generator extras (substrate-exp/actor-load): running actors per pool and the targets it is chasing.
    rbp = data.get("actors_running_by_pool")
    out["actors_running_by_pool"] = ({str(p): v for p, v in ((p, _int_or_none(x)) for p, x in rbp.items()) if v is not None}
                                     if isinstance(rbp, dict) else None)
    out["target_total"] = _int_or_none(data.get("target_total"))
    out["target_awake"] = _int_or_none(data.get("target_awake"))
    out["hold_s"] = _float_or_none(data.get("hold_s"))
    out["source"] = data.get("source") if isinstance(data.get("source"), str) else None
    # Worker pod names hosting a RUNNING actor right now (generator >= v0.1.2). Occupancy is not CPU: a counter actor
    # sleeps most of its hold, so the map marks these Workers explicitly. None = not reported (older generator).
    rw = data.get("running_workers")
    out["running_workers"] = sorted({str(x) for x in rw if isinstance(x, str) and x}) if isinstance(rw, list) else None
    return out, None


# ---------------------------------------------------------------- actor-load spec (ConfigMap substrate-exp/actor-load)
ACTOR_LOAD_CM = "actor-load"
ACTOR_LOAD_MAX_TOTAL = 200
ACTOR_LOAD_HOLD_S = (1.0, 300.0)
ACTOR_LOAD_DEFAULT_HOLD_S = 5.0


G4_WORKERS_MAX = 60  # "Warm Worker pool" slider: WorkerPool substrate-exp/g4-workers replicas


def validate_worker_replicas(body: Any) -> int:
    """POST /api/substrate/workers body {"total": 0..60} -> N ("Total Workers"). The old {"replicas": n} spelling is
    still accepted. ValueError with a readable reason."""
    if not isinstance(body, dict):
        raise ValueError("body must be a JSON object")
    unknown = sorted(set(body) - {"total", "replicas"})
    if unknown:
        raise ValueError(f"unknown field(s): {', '.join(unknown)}")
    if "total" in body and "replicas" in body:
        raise ValueError("set total (or the old replicas), not both")
    key = "total" if "total" in body else "replicas" if "replicas" in body else None
    if key is None:
        raise ValueError("set total")
    n = _strict_int(body[key], key)
    if not 0 <= n <= G4_WORKERS_MAX:
        raise ValueError(f"{key} must be 0..{G4_WORKERS_MAX}, got {n}")
    return n


def parse_actor_load(data: Any) -> dict:
    """ConfigMap data (string values) -> {exists, total, awake, hold_s}. Bad or missing values -> None."""
    if not isinstance(data, dict):
        return {"exists": False, "total": None, "awake": None, "hold_s": None}
    return {"exists": True, "total": _int_or_none(data.get("total")), "awake": _int_or_none(data.get("awake")),
            "hold_s": _float_or_none(data.get("hold_s"))}


def _strict_int(v: Any, name: str) -> int:
    if isinstance(v, bool) or v is None:
        raise ValueError(f"{name} must be an integer")
    if isinstance(v, float):
        if not v.is_integer():
            raise ValueError(f"{name} must be an integer")
        return int(v)
    try:
        return int(str(v).strip())
    except ValueError:
        raise ValueError(f"{name} must be an integer") from None


def actor_load_patch(current: dict | None, body: Any) -> tuple[dict, dict]:
    """Validate a POST /api/substrate/actors body against the current spec.

    body: any of total (0..200), awake (0..total), hold_s (1..300 s). Missing keys keep their current value.
    Returns (new spec {total, awake, hold_s}, merge patch for the ConfigMap: only the keys the body set, as strings).
    Raises ValueError with a presenter-readable message.
    """
    if not isinstance(body, dict):
        raise ValueError("body must be a JSON object")
    keys = [k for k in ("total", "awake", "hold_s") if k in body]
    if not keys:
        raise ValueError("set at least one of total, awake, hold_s")
    unknown = sorted(set(body) - {"total", "awake", "hold_s"})
    if unknown:
        raise ValueError(f"unknown field(s): {', '.join(unknown)}")
    cur = current or {}
    total = _strict_int(body["total"], "total") if "total" in body else cur.get("total")
    awake = _strict_int(body["awake"], "awake") if "awake" in body else cur.get("awake")
    hold = cur.get("hold_s")
    if "hold_s" in body:
        hold = _float_or_none(body["hold_s"]) if not isinstance(body["hold_s"], bool) else None
        if hold is None:
            raise ValueError("hold_s must be a number of seconds")
    total = 0 if total is None else total
    awake = 0 if awake is None else awake
    hold = ACTOR_LOAD_DEFAULT_HOLD_S if hold is None else hold
    if not 0 <= total <= ACTOR_LOAD_MAX_TOTAL:
        raise ValueError(f"total must be 0..{ACTOR_LOAD_MAX_TOTAL}, got {total}")
    if not 0 <= awake <= total:
        raise ValueError(f"awake must be 0..total ({total}), got {awake}")
    if not ACTOR_LOAD_HOLD_S[0] <= hold <= ACTOR_LOAD_HOLD_S[1]:
        raise ValueError(f"hold_s must be {ACTOR_LOAD_HOLD_S[0]:g}..{ACTOR_LOAD_HOLD_S[1]:g} s, got {hold:g}")
    spec = {"total": total, "awake": awake, "hold_s": hold}
    fmt = {"total": str(total), "awake": str(awake), "hold_s": f"{hold:g}"}
    return spec, {"data": {k: fmt[k] for k in keys}}


WORKER_PENDING_CPU = 2.0  # a Pending Worker is counted at 2 vCPU in the CPU budget (it will take that when it binds)


def cpu_budget(nodes_out: list[dict], pods: list[dict]) -> dict:
    """vCPUs the CPU-backfill sliders can still use once Workers are placed (substrate tab; a UI clamp only, preemption
    enforces it in-cluster): Σ allocatable CPU of Ready G4 + N4D nodes − Σ CPU requests of every non-backfill pod on
    them (vLLM, daemonsets, Workers ...) − 2 vCPU per Pending Worker. Backfill / overflow CPU-service pods and the
    preemptible warm buffer are not subtracted (they are what the sliders size). max_replicas per service =
    floor(vcpu_left / that service's per-replica CPU request)."""
    nodes = {n["name"] for n in nodes_out if n.get("role") in ("g4", "n4d") and n.get("ready")}
    alloc = sum(float(n.get("alloc_cpu") or 0.0) for n in nodes_out if n["name"] in nodes)
    used = 0.0
    pending_workers = 0
    per_replica: dict[str, float] = {}
    for p in pods:
        labels = p.get("labels") or {}
        if labels.get("app") in SERVICES and labels.get("colo.tier") in ("backfill", "overflow"):
            per_replica[labels["app"]] = max(per_replica.get(labels["app"], 0.0), float(p.get("cpu_req") or 0.0))
            continue
        if labels.get(SUBSTRATE_POOL_LABEL) and not p.get("node"):
            pending_workers += 1
            continue
        if p.get("node") not in nodes or pod_category(p) == "buffer":
            continue
        used += float(p.get("cpu_req") or 0.0)
    left = max(0.0, alloc - used - pending_workers * WORKER_PENDING_CPU)
    per = {svc: (per_replica.get(svc) or 2.0) for svc in SERVICES}
    return {"alloc_vcpu": round(alloc, 2), "non_backfill_vcpu": round(used, 2), "pending_workers": pending_workers,
            "pending_worker_vcpu": pending_workers * WORKER_PENDING_CPU, "vcpu_left": round(left, 2),
            "per_replica_vcpu": per, "max_replicas": {svc: int(left // c) if c > 0 else None for svc, c in per.items()}}


def mark_actor_workers(nodes_out: list[dict], running_workers: list[str] | None) -> None:
    """Set `actor` (True = hosts a RUNNING actor, False = warm idle Worker) on Worker CPU owners and Worker pod blocks.
    Left unset when the generator does not report running_workers, so the UI never claims idleness it cannot see."""
    if running_workers is None:
        return
    rs = set(running_workers)
    for n in nodes_out:
        for c in n.get("cpus") or []:
            if c.get("cat") == "worker" and c.get("owner"):
                c["owner"]["actor"] = c["owner"].get("pod") in rs
        for p in n.get("pods") or []:
            if p.get("cat") == "worker":
                p["actor"] = p.get("name") in rs


def ate_component(name: Any) -> str | None:
    """Control-plane component key of a workload or pod name (ATE_COMPONENTS), None if it is none of them."""
    low = str(name or "").lower()
    for key, _label, needles in ATE_COMPONENTS:
        if any(n in low for n in needles):
            return key
    return None


def _is_atelet(p: dict) -> bool:
    return p.get("namespace") == ATE_SYSTEM_NS and ((p.get("labels") or {}).get("app") == "atelet"
                                                   or ate_component(p.get("name")) == "atelet")


def _health(desired: int | None, ready: int | None) -> str:
    """ok | degraded | down | scaled-to-zero | unknown (the UI pairs each with an icon and words, not colour alone)."""
    if desired is None or ready is None:
        return "unknown"
    if desired <= 0:
        return "scaled-to-zero"
    if ready >= desired:
        return "ok"
    return "down" if ready <= 0 else "degraded"


def build_control_plane(raw_sub: Any, pods: list[dict], nodes: list[dict]) -> dict:
    """The /api/state `substrate.control_plane` section: Substrate's own nodes, pods and workload health.

    Raw (raw["substrate"]["ate_workloads"], all optional): {deployments: [{name, replicas, ready}] | None,
    statefulsets: [{name, replicas, ready}] | None, daemonsets: [{name, desired, ready, available}] | None,
    podcert_deployments: [{name, replicas, ready}] | None  (namespace PODCERT_NS)}.
    ate-system absent -> empty lists -> present False (an empty state, never an error).
    """
    raw_sub = raw_sub if isinstance(raw_sub, dict) else {}
    wl = raw_sub.get("ate_workloads") if isinstance(raw_sub.get("ate_workloads"), dict) else {}
    deps = [dict(d, kind="Deployment") for d in wl.get("deployments") or [] if isinstance(d, dict) and d.get("name")]
    # podcert is read from its own namespace only (PODCERT_NS), so a same-named workload elsewhere cannot shadow it.
    pc_deps = [dict(d, kind="Deployment") for d in wl.get("podcert_deployments") or [] if isinstance(d, dict) and d.get("name")]
    deps = [d for d in deps if ate_component(d["name"]) != "podcert"] + [d for d in pc_deps if ate_component(d["name"]) == "podcert"]
    sts = [dict(d, kind="StatefulSet") for d in wl.get("statefulsets") or [] if isinstance(d, dict) and d.get("name")]
    dss = [d for d in wl.get("daemonsets") or [] if isinstance(d, dict) and d.get("name")]
    ate_pods = [p for p in pods if p.get("namespace") == ATE_SYSTEM_NS]
    pc_pods = [p for p in pods if p.get("namespace") == PODCERT_NS]
    cp_pods = ate_pods + pc_pods  # tiles on control-plane nodes; component pod fallback

    def tile(p: dict) -> dict:
        comp = ate_component(p.get("name"))
        return {"name": p["name"], "component": comp, "letter": "P" if comp == "postgres" else "D" if comp == "atelet" else "C",
                "ready": bool(p.get("ready")), "phase": p.get("phase"), "restarts": p.get("restarts"),
                "cpu": p.get("cpu_req", 0.0)}

    cp_nodes = []
    kind_order = list(ATE_CP_NODE_KINDS)
    for n in sorted(nodes, key=lambda n: (kind_order.index(k) if (k := (n.get("labels") or {}).get(ATE_WORKLOAD_LABEL)) in kind_order
                                          else 99, n.get("name") or "")):
        kind = ATE_CP_NODE_KINDS.get((n.get("labels") or {}).get(ATE_WORKLOAD_LABEL))
        if not kind:
            continue
        labels = n.get("labels") or {}
        order = {k: i for i, k in enumerate(ATE_DISPLAY_ORDER + ("atelet",))}
        mine = sorted((p for p in cp_pods if p.get("node") == n["name"]),
                      key=lambda p: (order.get(ate_component(p["name"]), 99), p["name"]))
        cp_nodes.append({"name": n["name"], "kind": kind, "ready": bool(n.get("ready")),
                         "instance_type": labels.get("node.kubernetes.io/instance-type"),
                         "zone": labels.get("topology.kubernetes.io/zone"),
                         "pool": labels.get("cloud.google.com/gke-nodepool"),
                         "pods": [tile(p) for p in mine]})
    components = []
    labels_by_key = {k: lb for k, lb, _n in ATE_COMPONENTS}
    for key in ATE_DISPLAY_ORDER:
        label = labels_by_key[key]
        ws = [w for w in deps + sts if ate_component(w["name"]) == key]
        cp = [p for p in (pc_pods if key == "podcert" else ate_pods) if ate_component(p.get("name")) == key]
        if ws:
            desired = sum(_int_or_none(w.get("replicas")) or 0 for w in ws)
            ready = sum(_int_or_none(w.get("ready")) or 0 for w in ws)
            src = "/".join(sorted({w["kind"] for w in ws}))
        elif cp:
            desired, ready, src = len(cp), sum(1 for p in cp if p.get("ready")), "pods"
        else:
            desired = ready = src = None
        components.append({"key": key, "label": label, "found": src is not None, "source": src,
                           "workloads": [w["name"] for w in ws], "desired": desired, "ready": ready,
                           "restarts": sum(int(p.get("restarts") or 0) for p in cp) if cp else None,
                           "health": _health(desired, ready) if src else "missing"})
    ds = next((d for d in dss if ate_component(d["name"]) == "atelet"), None)
    atelet_pods = [p for p in ate_pods if _is_atelet(p)]
    if ds:
        atelet = {"found": True, "source": "DaemonSet", "name": ds["name"], "desired": _int_or_none(ds.get("desired")),
                  "ready": _int_or_none(ds.get("ready")), "available": _int_or_none(ds.get("available"))}
    elif atelet_pods:
        atelet = {"found": True, "source": "pods", "name": "atelet", "desired": len(atelet_pods),
                  "ready": sum(1 for p in atelet_pods if p.get("ready")), "available": None}
    else:
        atelet = {"found": False, "source": None, "name": None, "desired": None, "ready": None, "available": None}
    atelet["health"] = _health(atelet["desired"], atelet["ready"]) if atelet["found"] else "missing"
    atelet["restarts"] = sum(int(p.get("restarts") or 0) for p in atelet_pods) if atelet_pods else None
    present = bool(cp_nodes or ate_pods or deps or sts or dss)
    worst = [c["health"] for c in components if c["found"]] + ([atelet["health"]] if atelet["found"] else [])
    return {
        "present": present,
        "nodes": cp_nodes,
        "components": components,
        "atelet": atelet,
        "unscheduled": [tile(p) for p in cp_pods if not p.get("node")],
        "health": ("down" if "down" in worst else "degraded" if ("degraded" in worst or "scaled-to-zero" in worst)
                   else "ok" if worst else "unknown"),
    }


def build_substrate(raw_sub: Any, pods: list[dict], nodes_out: list[dict]) -> dict:
    """The /api/state `substrate` section. Missing CRD, pools, HPA or ConfigMap is an empty state, never an error."""
    raw_sub = raw_sub if isinstance(raw_sub, dict) else {}
    notes = [str(n) for n in raw_sub.get("notes") or []]
    role_of = {n["name"]: n["role"] for n in nodes_out}
    wps_raw = raw_sub.get("workerpools")
    installed = isinstance(wps_raw, list)
    wps = {}
    for o in wps_raw if installed else []:
        wp = parse_workerpool(o)
        if wp:
            wps[wp["name"]] = wp
    workers = [p for p in pods if (p.get("labels") or {}).get(SUBSTRATE_POOL_LABEL)]
    pool_names = list(dict.fromkeys([SUBSTRATE_G4_POOL, SUBSTRATE_N4D_POOL, *wps,
                                     *((p.get("labels") or {})[SUBSTRATE_POOL_LABEL] for p in workers)]))
    pools = {}
    for name in pool_names:
        mine = [p for p in workers if (p.get("labels") or {}).get(SUBSTRATE_POOL_LABEL) == name]
        wp = wps.get(name)
        if not wp and not mine:
            continue
        running = [p for p in mine if p.get("phase") == "Running"]
        pools[name] = {
            "exists": wp is not None,
            "desired": wp["desired"] if wp else None,
            "replicas": wp["replicas"] if wp else None,
            "ready": wp["ready"] if wp else None,
            "pods": len(mine),
            "pods_ready": sum(1 for p in mine if p.get("ready")),
            "pending": sum(1 for p in mine if p.get("phase") == "Pending"),
            "on_g4": sum(1 for p in running if role_of.get(p.get("node")) == "g4"),
            "on_n4d": sum(1 for p in running if role_of.get(p.get("node")) == "n4d"),
            "vcpu": round(sum(float(p.get("cpu_req") or 0.0) for p in running), 2),
        }
    def find_hpa(pool: str) -> dict | None:
        for h in hpas_raw if isinstance(hpas_raw, list) else []:
            if isinstance(h, dict) and pool in (h.get("target"), h.get("name")):
                return {"name": h.get("name"), "target": h.get("target"), "desired": h.get("desired"),
                        "current": h.get("current"), "min": h.get("min"), "max": h.get("max"),
                        "metric": _num(h.get("metric"))}
        return None

    hpas_raw = raw_sub.get("hpas")
    hpa = find_hpa(SUBSTRATE_N4D_POOL)
    # Option 2: a stock HPA on WorkerPool g4-workers sets the G4 share; its maxReplicas is the user's TOTAL Workers.
    g4_hpa = find_hpa(SUBSTRATE_G4_POOL)
    stats, problem = parse_actor_stats(raw_sub.get("actor_stats"))
    if problem:
        notes.append(problem)
    actors = None
    if stats is not None:
        g4 = n4d = other = None
        if stats.get("actors_by_node") is not None:
            g4 = n4d = other = 0
            for node, c in stats["actors_by_node"].items():
                r = role_of.get(node)
                if r == "g4":
                    g4 += c
                elif r == "n4d":
                    n4d += c
                else:
                    other += c
        by_pool = stats.get("resume_p99_ms_by_pool") or {}
        rbp = stats.get("actors_running_by_pool") or {}
        actors = {**stats, "actors_g4": g4, "actors_n4d": n4d, "actors_other": other,
                  "running_g4": rbp.get(SUBSTRATE_G4_POOL), "running_n4d": rbp.get(SUBSTRATE_N4D_POOL),
                  "resume_p99_g4_ms": by_pool.get(SUBSTRATE_G4_POOL), "resume_p99_n4d_ms": by_pool.get(SUBSTRATE_N4D_POOL),
                  "age_s": _age_s(stats.get("updated"), time.time())}
    g4p = pools.get(SUBSTRATE_G4_POOL) or {}
    return {
        "installed": installed,
        "pools": pools,
        "g4_pending": g4p.get("pending", 0),
        "workers_on_g4": sum(p["on_g4"] for p in pools.values()),
        "workers_on_n4d": sum(p["on_n4d"] for p in pools.values()),
        "n4d_hpa": hpa,
        "g4_hpa": g4_hpa,
        # Total Workers: the g4-workers HPA maxReplicas when it exists (option 2), else the WorkerPool replicas.
        "total_workers": (g4_hpa or {}).get("max") if g4_hpa else (pools.get(SUBSTRATE_G4_POOL) or {}).get("desired"),
        "total_workers_source": "hpa" if g4_hpa else ("workerpool" if pools.get(SUBSTRATE_G4_POOL) else None),
        "actors": actors,
        "has_data": bool(pools or hpa or actors),
        "actor_load": parse_actor_load(raw_sub.get("actor_load")),
        "notes": notes,
    }


# ---------------------------------------------------------------- main builder
def build_state(raw: dict, mode: str = "live", recipe: str = "controller", cluster: dict | None = None) -> dict:
    substrate = recipe == "substrate"
    # The substrate tab runs on top of a base recipe (cluster["base_recipe"]: "controller", the default, or "native"):
    # everything that is not Substrate (demand, eligibility, overflow HPAs, guard, control chip) follows that base.
    base_recipe = ((cluster or {}).get("base_recipe") or "controller") if substrate else None
    native = recipe == "native" or base_recipe == "native"
    # Accelerator host of this cluster (cluster["accelerator"], default G4): price, host vCPUs and display words.
    accel = accel_profile(cluster)
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
    vllm_nodes = _vllm_ready_nodes(pods)

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
        if substrate and labels.get(ATE_WORKLOAD_LABEL) in ATE_CP_NODE_KINDS:
            role = "ate-cp"  # Substrate control-plane node: drawn in its own group, never as N4D capacity
        counts[role] = counts.get(role, 0) + 1
        if n.get("ready"):
            counts[f"{role}_ready"] = counts.get(f"{role}_ready", 0) + 1
        if role == "g4":
            cost_g4 += accel["price_hr"]
        elif role == "n4d":
            cost_n4d += PRICE_N4D16_HR
        alloc = n.get("allocatable") or {}
        node_pods = []
        for p in pods_by_node.get(n["name"], []):
            if p.get("namespace") not in ("inference", "colo-svc") and pod_category(p, substrate) not in _DRAWN_EXTRA:
                # system / agent pods are not drawn as blocks (they live in the shared CPU pool);
                # the warm buffer (colo-system in the recipe) and Substrate Worker pods (substrate-exp) are drawn.
                continue
            pl = p.get("labels") or {}
            node_pods.append(
                {
                    "name": p["name"],
                    "ns": p.get("namespace"),
                    "app": pl.get("app"),
                    "tier": pl.get("colo.tier"),
                    "cat": pod_category(p, substrate),
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
            elif substrate and role == "g4" and n.get("ready") and pl.get(SUBSTRATE_POOL_LABEL) and p.get("phase") == "Running":
                # Substrate Workers on G4 spare cores are reclaimed capacity too (same backfill priority class).
                reclaimed_vcpu += p.get("cpu_req", 0.0)
                reclaimed_mem += p.get("mem_req_gib", 0.0)
                backfill_on_g4.append(p)
            if role == "n4d" and pl.get("colo.tier") == "overflow" and p.get("phase") == "Running":
                overflow_pods += 1
                overflow_vcpu += p.get("cpu_req", 0.0)
                overflow_on_n4d.append(p)
            elif substrate and role == "n4d" and pl.get(SUBSTRATE_POOL_LABEL) and p.get("phase") == "Running":
                # Substrate Workers on N4D (n4d-workers, the overflow share) count in the counterfactual like CPU
                # overflow: without colo every Worker would run on N4D. (Not in the CPU-service overflow story numbers.)
                overflow_on_n4d.append(p)

        # vCPU budget of this node: what the GPU slice, system pods and colo pods use.
        node_all = pods_by_node.get(n["name"], [])
        # System = daemonsets/agents; the warm buffer is preemptible capacity, not overhead.
        sys_pods = [p for p in node_all if p.get("namespace") not in ("inference", "colo-svc") and pod_category(p, substrate) not in _DRAWN_EXTRA]
        sys_cpu = sum(p.get("cpu_req", 0.0) for p in sys_pods)
        sys_mem = sum(p.get("mem_req_gib", 0.0) for p in sys_pods)
        agent_pods = [p for p in sys_pods if is_customer_daemonset(p)]
        agent_cpu = sum(p.get("cpu_req", 0.0) for p in agent_pods)
        if role == "g4" and n.get("ready"):
            g4_alloc += parse_cpu(alloc.get("cpu"))
            g4_sys += sys_cpu
            for p in node_all:
                cat = pod_category(p, substrate)
                if cat == "vllm":
                    g4_vllm += p.get("cpu_req", 0.0)
                    vllm_slices.append(p.get("cpu_req", 0.0))
                elif cat == "buffer":
                    g4_buffer += p.get("cpu_req", 0.0)
        elif role == "n4d" and n.get("ready"):
            n4d_caps.append((parse_cpu(alloc.get("cpu")), parse_mem_gib(alloc.get("memory")), sys_cpu, sys_mem))

        agent = agents.get(n["name"]) or {}
        ncpu = int(agent.get("ncpu") or round(parse_cpu(alloc.get("cpu_capacity") or 0)) or (accel["vcpu"] if role == "g4" else 16 if role == "n4d" else 0))
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
                            "cat": pod_category(p, substrate),
                        }
                    else:
                        owner[c] = {"pod": uid[:8], "ns": None, "app": None, "tier": None, "container": cname, "cat": "system"}
        cpus = []
        # Real topology from the node agent (sysfs) when it reports one; otherwise assume 8 cores per L3.
        topo = {t["id"]: (t["core"], t["thread"], t["llc"]) for t in ((agent.get("topology") or {}).get("cpus") or [])}
        if agent:
            for c in range(ncpu):
                core, thread, llc = topo.get(c) or _cpu_topology(ncpu, c)
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
        # Eligible for backfill: the controller marks it with colo.active=true; on the native recipe the scheduler's
        # podAffinity makes "runs a Ready vLLM pod" the rule (there is no node label).
        if role != "g4":
            eligible = None
        elif native or "colo.active" not in labels:
            eligible = n["name"] in vllm_nodes
        else:
            eligible = str(labels.get("colo.active")) == "true"
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
                "cpu_model": (agent.get("topology") or {}).get("model"),
                "l3_kib": (agent.get("topology") or {}).get("l3_kib"),
                "topology_source": "sysfs" if topo else "assumed",
                "alloc_mem_gib": round(parse_mem_gib(alloc.get("memory")), 1),
                "ncpu": ncpu,
                "cpus": cpus,
                "agent_ok": bool(agent),
                "mem_used_gib": agent.get("mem_used_gib"),
                "pods": sorted(node_pods, key=lambda p: ({"vllm": 0, "integer": 1, "memory": 2, "worker": 3, "buffer": 4}.get(p["cat"], 5), p["name"])),
                "blocks": blocks,
                "eligible": eligible,
                "sys_cpu": round(sys_cpu, 2),  # daemonset/agent requests (shared pool, not exclusive)
                # Of sys_cpu, what your own DaemonSets request (namespaces outside GKE and the recipe); drawn in its
                # own colour on the node map. Shared cores, never exclusive.
                "agent_cpu": round(agent_cpu, 2),
                "agents": [{"ns": p.get("namespace"), "name": p.get("name"), "cpu": p.get("cpu_req", 0.0),
                            "ready": bool(p.get("ready"))} for p in agent_pods],
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
    if native:
        # No controller and no colo-config: demand is the replicas of the Deployment that holds it.
        cstate, conf = None, {}
        demand = {svc: (deps.get(("colo-svc", dname)) or {}).get("replicas")
                  for svc, dname in NATIVE_DEMAND_DEPLOYMENT.items() if deps.get(("colo-svc", dname))}
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

    hpas_by_target = {(h.get("namespace"), h.get("target") or h.get("name")): h for h in raw.get("hpas") or []}
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
            if native and tier == "backfill":
                # Backfill pods with no G4 slot next to a vLLM pod stay Pending (priority -100, ignored by the
                # cluster autoscaler) by design; the overflow HPA covers them on N4D.
                entry[tier]["waiting"] = sum(1 for p in tp if p.get("phase") == "Pending" and not p.get("node"))
                entry[tier]["on_g4"] = sum(1 for p in tp if p.get("node") and p.get("phase") == "Running")
                entry[tier]["effective"] = sum(1 for p in tp if p.get("ready") and p.get("node") in vllm_nodes)
            if native and tier == "overflow":
                h = hpas_by_target.get(("colo-svc", f"{svc}-overflow"))
                entry[tier]["hpa"] = None if not h else {
                    "name": h.get("name"), "metric": _num(h.get("metric")), "desired": h.get("desired"),
                    "current": h.get("current"), "min": h.get("min"), "max": h.get("max"),
                }
        services[svc] = entry
    buf = deps.get(("colo-system", "colo-buffer")) or deps.get(("colo-svc", "colo-buffer")) or {}

    events = sorted(raw.get("events") or [], key=lambda e: e.get("time") or "", reverse=True)[:50]

    reclaimed_value = reclaimed_vcpu * PRICE_VCPU_HR + reclaimed_mem * PRICE_GIB_HR

    # Counterfactual "CPU on N4D, no colo": every CPU pod now running (backfill on G4 + overflow on N4D, plus Agent
    # Substrate Workers on G4 and N4D on the substrate tab, with their real requests) packed onto N4D. Savings = N4D nodes that world needs beyond the N4D nodes running today.
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
    cf["workers"] = sum(1 for p in cf_pods if (p.get("labels") or {}).get(SUBSTRATE_POOL_LABEL))
    cf["method"] = localize("first-fit-decreasing pack of all running CPU pods (G4 backfill + N4D overflow"
                            + (" + Agent Substrate Workers on G4 and N4D" if substrate else "") + ") onto "
                            "n4d-standard-16 allocatable minus daemonset requests; saved = nodes needed - N4D nodes "
                            "running", accel)

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
    if native:
        control = {
            "kind": "native",
            "label": "native K8s + Agent Substrate" if substrate else "HPA + scheduler",
            "detail": ("No controller: backfill replicas = demand; a backfill pod binds only next to a vLLM pod "
                       "(podAffinity); the overflow HPAs cover Pending backfill on N4D."
                       + (" Agent Substrate g4-workers also bind next to vLLM (podAffinity) at a low priority; "
                          "n4d-workers overflow via their HPA." if substrate else "")),
            "guard_available": False,
            "guard_reason": localize(GUARD_NA_REASON, accel),
            "eligible_g4": sum(1 for n in nodes_out if n["role"] == "g4" and n.get("eligible")),
            "backfill_waiting": sum((s["backfill"].get("waiting") or 0) for s in services.values()),
            "hpas": {svc: s["overflow"].get("hpa") for svc, s in services.items() if s["overflow"].get("hpa")},
        }
    elif substrate:
        control = {
            "kind": "controller",
            "label": "colo-controller + Agent Substrate",
            "detail": localize("colo-controller still splits demand into G4 backfill + N4D overflow and runs the TTFT "
                               "guard; Agent Substrate Workers (g4-workers on G4 spare cores, n4d-workers on N4D via "
                               "HPA) host suspended/resumed agent actors next to vLLM.", accel),
            "guard_available": True,
            "guard_reason": None,
        }
    else:
        control = {
            "kind": "controller",
            "label": "colo-controller",
            "detail": localize("colo-controller splits demand into G4 backfill + N4D overflow every 2 s and runs the "
                               "TTFT guard.", accel),
            "guard_available": True,
            "guard_reason": None,
        }
    out = {
        "ts": now,
        "mode": mode,
        "recipe": recipe,
        "base_recipe": base_recipe,
        "cluster": cluster or CLUSTER,
        # Display profile of the accelerator host (see ACCELERATORS); the UI takes every G4/GPU word from it.
        "accelerator": accel,
        "control": control,
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
                "g4_hr": accel["price_hr"],  # accelerator host per node-hour (key kept for compatibility)
                "accel_hr": accel["price_hr"],
                "n4d16_hr": PRICE_N4D16_HR,
                "vcpu_hr": PRICE_VCPU_HR,
                "gib_hr": PRICE_GIB_HR,
            },
        },
        "errors": errors,
    }
    if substrate:
        out["substrate"] = build_substrate(raw.get("substrate"), pods, nodes_out)
        out["substrate"]["control_plane"] = build_control_plane(raw.get("substrate"), pods, raw.get("nodes") or [])
        mark_actor_workers(nodes_out, (out["substrate"].get("actors") or {}).get("running_workers"))
        out["substrate"]["cpu_budget"] = cpu_budget(nodes_out, pods)
        atelets = {p.get("node"): p for p in pods if _is_atelet(p) and p.get("node")}
        for n in nodes_out:
            if n["role"] in ("g4", "n4d"):
                p = atelets.get(n["name"])
                n["atelet"] = None if not p else {"pod": p["name"], "ready": bool(p.get("ready")), "phase": p.get("phase"),
                                                  "restarts": p.get("restarts")}
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
        # Native recipe: backfill pods Pending for want of a G4 slot (always 0 on the controller recipe).
        "backfill_waiting": sum(s["backfill"].get("waiting") or 0 for s in svc.values()),
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
