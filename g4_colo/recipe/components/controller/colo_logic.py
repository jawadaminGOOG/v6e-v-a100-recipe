"""Pure decision logic for the colo controller (no Kubernetes or network calls).

Everything in this module takes plain Python values and returns plain values, so
it can be unit tested without a cluster. `controller.py` owns all I/O.

Sections:
  * quantities and pod requests
  * config defaults, merge and validation
  * (a) spare-slot computation per eligible G4 node
  * (b) backfill / overflow allocation and the safe scale plan
  * (c) TTFT histogram merge and p99 (loadgen buckets and Prometheus buckets)
  * (d) per-node guard state machine
  * (e) per-node planning: eligibility, colo.active, make-before-break dehydrate
"""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Iterable, Mapping

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SERVICE_ORDER: tuple[str, ...] = ("sandbox", "toolcall", "analytics")
CLASS_INTEGER = "integer"
CLASS_MEMORY_HEAVY = "memory-heavy"
CLASSES: tuple[str, ...] = (CLASS_MEMORY_HEAVY, CLASS_INTEGER)
BLOCK_LABELS: dict[str, str] = {
    CLASS_MEMORY_HEAVY: "colo.block.memory-heavy",
    CLASS_INTEGER: "colo.block.integer",
}
DEFAULT_BUCKET_MS = 2


@dataclass(frozen=True)
class ServiceSpec:
    name: str
    cpu_m: int  # CPU request per pod, millicores
    mem_bytes: int  # memory request per pod, bytes
    cls: str  # colo.class


GI = 1024**3
SERVICES: dict[str, ServiceSpec] = {
    "sandbox": ServiceSpec("sandbox", 2000, 4 * GI, CLASS_INTEGER),
    "toolcall": ServiceSpec("toolcall", 2000, 2 * GI, CLASS_INTEGER),
    "analytics": ServiceSpec("analytics", 2000, 6 * GI, CLASS_MEMORY_HEAVY),
}

# ---------------------------------------------------------------------------
# Quantities
# ---------------------------------------------------------------------------

_MEM_SUFFIX = {
    "Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4, "Pi": 1024**5, "Ei": 1024**6,
    "k": 10**3, "K": 10**3, "M": 10**6, "G": 10**9, "T": 10**12, "P": 10**15, "E": 10**18,
}


def parse_cpu(q: str | int | float | None) -> int:
    """Kubernetes CPU quantity -> millicores (rounded up)."""
    if q is None or q == "":
        return 0
    if isinstance(q, (int, float)):
        return int(math.ceil(float(q) * 1000))
    s = str(q).strip()
    if s.endswith("m"):
        return int(math.ceil(float(s[:-1])))
    if s.endswith("n"):
        return int(math.ceil(float(s[:-1]) / 1e6))
    if s.endswith("u"):
        return int(math.ceil(float(s[:-1]) / 1e3))
    return int(math.ceil(float(s) * 1000))


def parse_mem(q: str | int | float | None) -> int:
    """Kubernetes memory quantity -> bytes."""
    if q is None or q == "":
        return 0
    if isinstance(q, (int, float)):
        return int(q)
    s = str(q).strip()
    for suf in ("Ki", "Mi", "Gi", "Ti", "Pi", "Ei"):
        if s.endswith(suf):
            return int(float(s[: -len(suf)]) * _MEM_SUFFIX[suf])
    if s.endswith("m"):  # milli-bytes, legal but odd
        return int(math.ceil(float(s[:-1]) / 1000))
    if s[-1] in _MEM_SUFFIX:
        return int(float(s[:-1]) * _MEM_SUFFIX[s[-1]])
    return int(float(s))


def pod_requests(
    containers: Iterable[Mapping[str, Any]],
    init_containers: Iterable[Mapping[str, Any]] = (),
    overhead: Mapping[str, Any] | None = None,
) -> tuple[int, int]:
    """Effective pod request (cpu_m, mem_bytes) as the scheduler computes it.

    Each container entry is its `resources.requests` mapping, e.g.
    {"cpu": "2", "memory": "4Gi"}. Effective = max(sum(app), max(init)) + overhead.
    Restartable (sidecar) init containers are treated as plain init containers.
    """
    cpu = sum(parse_cpu(c.get("cpu")) for c in containers)
    mem = sum(parse_mem(c.get("memory")) for c in containers)
    for ic in init_containers:
        cpu = max(cpu, parse_cpu(ic.get("cpu")))
        mem = max(mem, parse_mem(ic.get("memory")))
    if overhead:
        cpu += parse_cpu(overhead.get("cpu"))
        mem += parse_mem(overhead.get("memory"))
    return cpu, mem


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULT_CONFIG: dict[str, Any] = {
    "demand": {"sandbox": 0, "toolcall": 0, "analytics": 0},
    "guard": {
        "enabled": True,
        "baseline_ttft_p99_ms": 60,
        "factor": 1.5,
        "cooldown_s": 600,
        "window_s": 30,
        # Additions to the spec schema (optional keys, defaults = spec constants).
        "escalate_s": 60,
        "min_requests": 200,
    },
    "buffer_replicas": 1,
    # Addition: CPU (cores) kept free on each G4 node beyond non-backfill requests.
    # 2 keeps one 2-vCPU slot for the shared cpuset (DaemonSets, node agents).
    "reserve_cpu_per_node": 2,
    # Seconds a node may stay ineligible (e.g. vLLM Recreate rollout) before its
    # remaining backfill pods are deleted even if overflow is not ready yet.
    "dehydrate_grace_s": 60,
    # Where the guard reads per-node TTFT from: loadgen /stats (demo), Google
    # Managed Prometheus (production) or none (guard reports no_signal, never acts).
    "ttft_source": {
        "type": "loadgen",
        "prometheus": {
            "project": "",
            "location": "global",
            "query_window_s": 60,
            "metric": "vllm:time_to_first_token_seconds",
        },
    },
    # Optional upper bound on total requested CPU (cores) across all demand,
    # e.g. the colo-svc ResourceQuota minus headroom. null = no check.
    "max_total_cpu": None,
}

TTFT_SOURCE_TYPES: tuple[str, ...] = ("loadgen", "prometheus", "none")


def deep_merge(base: Mapping[str, Any], patch: Mapping[str, Any]) -> dict[str, Any]:
    """Recursive merge; dicts merge, everything else in `patch` replaces."""
    out = copy.deepcopy(dict(base))
    for k, v in patch.items():
        if isinstance(v, Mapping) and isinstance(out.get(k), Mapping):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def validate_config(cfg: Mapping[str, Any]) -> dict[str, Any]:
    """Return a normalised copy of `cfg` or raise ValueError."""
    c = deep_merge(DEFAULT_CONFIG, cfg)
    demand = c["demand"]
    if not isinstance(demand, Mapping):
        raise ValueError("demand must be an object")
    for svc in demand:
        if svc not in SERVICES:
            raise ValueError(f"unknown service in demand: {svc}")
    for svc in SERVICE_ORDER:
        v = demand.get(svc, 0)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0 or int(v) != v:
            raise ValueError(f"demand.{svc} must be a non-negative integer")
        demand[svc] = int(v)
    g = c["guard"]
    if not isinstance(g.get("enabled"), bool):
        raise ValueError("guard.enabled must be a boolean")
    b = g.get("baseline_ttft_p99_ms")
    if b is not None and (isinstance(b, bool) or not isinstance(b, (int, float)) or b <= 0):
        raise ValueError("guard.baseline_ttft_p99_ms must be > 0 or null")
    for key, lo in (("factor", 1.0), ("cooldown_s", 0), ("window_s", 1),
                    ("escalate_s", 0), ("min_requests", 1)):
        v = g.get(key)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v < lo:
            raise ValueError(f"guard.{key} must be a number >= {lo}")
    br = c["buffer_replicas"]
    if isinstance(br, bool) or not isinstance(br, int) or br < 0:
        raise ValueError("buffer_replicas must be a non-negative integer")
    r = c["reserve_cpu_per_node"]
    if isinstance(r, bool) or not isinstance(r, (int, float)) or r < 0:
        raise ValueError("reserve_cpu_per_node must be >= 0")
    d = c["dehydrate_grace_s"]
    if isinstance(d, bool) or not isinstance(d, (int, float)) or d < 0:
        raise ValueError("dehydrate_grace_s must be a number >= 0")
    ts = c["ttft_source"]
    if not isinstance(ts, Mapping) or ts.get("type") not in TTFT_SOURCE_TYPES:
        raise ValueError(f"ttft_source.type must be one of {', '.join(TTFT_SOURCE_TYPES)}")
    pr = ts.get("prometheus")
    if not isinstance(pr, Mapping):
        raise ValueError("ttft_source.prometheus must be an object")
    if ts["type"] == "prometheus" and not (isinstance(pr.get("project"), str) and pr["project"]):
        raise ValueError("ttft_source.prometheus.project is required when type is prometheus")
    for key in ("location", "metric"):
        if not isinstance(pr.get(key), str) or not pr[key]:
            raise ValueError(f"ttft_source.prometheus.{key} must be a non-empty string")
    w = pr.get("query_window_s")
    if isinstance(w, bool) or not isinstance(w, (int, float)) or w < 10:
        raise ValueError("ttft_source.prometheus.query_window_s must be a number >= 10")
    mx = c["max_total_cpu"]
    if mx is not None:
        if isinstance(mx, bool) or not isinstance(mx, (int, float)) or mx <= 0:
            raise ValueError("max_total_cpu must be > 0 or null")
        total = demand_cpu_cores(demand)
        if total > mx:
            raise ValueError(f"demand needs {total:g} CPU, above max_total_cpu {mx:g}")
    return c


def demand_cpu_cores(demand: Mapping[str, int],
                     services: Mapping[str, ServiceSpec] = SERVICES) -> float:
    """Total CPU (cores) requested if every demanded pod runs."""
    return sum(int(demand.get(s, 0)) * services[s].cpu_m for s in services) / 1000


# ---------------------------------------------------------------------------
# (a) Spare slots per node
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NodeCapacity:
    """One eligible G4 node.

    used_* are the summed requests of all non-backfill pods bound to the node
    (vLLM, DaemonSets, system pods, anything else). Memory may be left as None
    to make allocation CPU-only.
    """

    name: str
    alloc_cpu_m: int
    used_cpu_m: int
    blocks: frozenset[str] = frozenset()
    alloc_mem: int | None = None
    used_mem: int | None = None
    reserve_cpu_m: int = 0

    @property
    def spare_cpu_m(self) -> int:
        return max(0, self.alloc_cpu_m - self.used_cpu_m - self.reserve_cpu_m)

    @property
    def spare_mem(self) -> int | None:
        if self.alloc_mem is None or self.used_mem is None:
            return None
        return max(0, self.alloc_mem - self.used_mem)


def blocks_from_labels(labels: Mapping[str, str] | None) -> frozenset[str]:
    labels = labels or {}
    return frozenset(c for c, lbl in BLOCK_LABELS.items() if labels.get(lbl) == "true")


def _fits(spare_cpu: int, spare_mem: int | None, spec: ServiceSpec) -> int:
    n = spare_cpu // spec.cpu_m if spec.cpu_m > 0 else 0
    if spare_mem is not None and spec.mem_bytes > 0:
        n = min(n, spare_mem // spec.mem_bytes)
    return max(0, n)


def node_slots(
    node: NodeCapacity, services: Mapping[str, ServiceSpec] = SERVICES
) -> dict[str, int]:
    """Pods of each service that would fit alone on the node's spare capacity.

    A service whose class is blocked on the node gets 0. Slots are not additive
    across services: they share the same spare CPU.
    """
    return {
        name: 0 if spec.cls in node.blocks else _fits(node.spare_cpu_m, node.spare_mem, spec)
        for name, spec in services.items()
    }


# ---------------------------------------------------------------------------
# (b) Allocation and scale plan
# ---------------------------------------------------------------------------


@dataclass
class Allocation:
    backfill: dict[str, int]
    overflow: dict[str, int]
    placement: dict[str, dict[str, int]]  # node -> service -> pods
    leftover_cpu_m: dict[str, int]  # node -> spare CPU after placement


def allocate(
    demand: Mapping[str, int],
    nodes: Iterable[NodeCapacity],
    services: Mapping[str, ServiceSpec] = SERVICES,
    order: Iterable[str] = SERVICE_ORDER,
) -> Allocation:
    """Fill spare G4 capacity with backfill pods, service by service in `order`.

    Each pod goes to the unblocked node with the most remaining spare CPU (spread,
    like the scheduler's LeastAllocated scoring); ties break on node name.
    overflow = demand - backfill.
    """
    nodes = sorted(nodes, key=lambda n: n.name)
    cpu = {n.name: n.spare_cpu_m for n in nodes}
    mem = {n.name: n.spare_mem for n in nodes}
    blocks = {n.name: n.blocks for n in nodes}
    placement: dict[str, dict[str, int]] = {n.name: {} for n in nodes}
    backfill: dict[str, int] = {}
    overflow: dict[str, int] = {}
    for svc in order:
        spec = services[svc]
        want = max(0, int(demand.get(svc, 0)))
        got = 0
        while got < want:
            cands = [
                name for name in cpu
                if spec.cls not in blocks[name] and _fits(cpu[name], mem[name], spec) >= 1
            ]
            if not cands:
                break
            best = max(cands, key=lambda nm: (cpu[nm], [-ord(ch) for ch in nm]))
            cpu[best] -= spec.cpu_m
            if mem[best] is not None:
                mem[best] -= spec.mem_bytes
            placement[best][svc] = placement[best].get(svc, 0) + 1
            got += 1
        backfill[svc] = got
        overflow[svc] = want - got
    return Allocation(backfill, overflow, placement, cpu)


@dataclass(frozen=True)
class ScalePlan:
    backfill: int
    overflow: int
    order: tuple[str, str]  # patch order, e.g. ("overflow", "backfill")


def plan_scale(
    cur_bf: int, cur_ov: int, ready_bf: int, ready_ov: int, tgt_bf: int, tgt_ov: int
) -> ScalePlan:
    """One reconcile step from current to target replicas without dropping capacity.

    * Increases are applied immediately.
    * A decrease of one tier is held until the other tier's *ready* pods cover
      the target total (total ready never drops on purpose). When the target
      total itself shrinks, the hold releases at once.
    * A hold never keeps a tier above its own *ready* count: the hold exists to
      keep serving pods alive, not to recreate pods that vanished externally
      (dehydrate, eviction, preemption) and would only sit Pending.
    * Patch order: overflow first when the total grows, backfill first otherwise.
    """
    total = tgt_bf + tgt_ov
    new_ov = tgt_ov if tgt_ov >= cur_ov else max(tgt_ov, min(cur_ov, ready_ov, total - ready_bf))
    new_bf = tgt_bf if tgt_bf >= cur_bf else max(tgt_bf, min(cur_bf, ready_bf, total - ready_ov))
    growing = new_bf + new_ov > cur_bf + cur_ov
    order = ("overflow", "backfill") if growing else ("backfill", "overflow")
    return ScalePlan(new_bf, new_ov, order)


# ---------------------------------------------------------------------------
# (c) TTFT histograms
# ---------------------------------------------------------------------------


def merge_hists(hists: Iterable[list[int]]) -> list[int]:
    """Element-wise sum. All inputs must share one length (last = overflow)."""
    out: list[int] = []
    for h in hists:
        if not out:
            out = [int(x) for x in h]
            continue
        if len(h) != len(out):
            raise ValueError(f"histogram length mismatch: {len(h)} != {len(out)}")
        for i, x in enumerate(h):
            out[i] += int(x)
    return out


def hist_quantile(hist: list[int], q: float = 0.99, bucket_ms: float = DEFAULT_BUCKET_MS) -> float | None:
    """Quantile from a bucketed histogram.

    Bucket i (< last) covers [i*bucket_ms, (i+1)*bucket_ms); the value returned is
    the bucket's upper edge (conservative). The last bucket is overflow; if the
    quantile lands there the overflow lower edge (len-1)*bucket_ms is returned.
    """
    total = sum(hist)
    if total <= 0:
        return None
    rank = max(1, math.ceil(q * total - 1e-9))  # guard against 0.99*100 = 99.000...01
    cum = 0
    last = len(hist) - 1
    for i, c in enumerate(hist):
        cum += c
        if cum >= rank:
            return float(i * bucket_ms) if i == last else float((i + 1) * bucket_ms)
    return float(last * bucket_ms)


@dataclass
class TTFTSummary:
    p99_ms: float | None
    requests: int
    sufficient: bool


@dataclass
class TTFTView:
    nodes: dict[str, TTFTSummary]
    cluster: TTFTSummary
    window_s: float | None
    bucket_ms: float
    skipped: int  # per_pod entries dropped (bad shape / bucket mismatch)


def summarize_ttft(stats: Iterable[Mapping[str, Any]], min_requests: int = 200) -> TTFTView:
    """Merge loadgen /stats payloads into per-node and cluster TTFT p99.

    Each payload: {"per_pod": {pod: {"node": n, "ttft_hist": [...]}}, "bucket_ms": 2,
    "window_s": 30}. Several loadgen replicas report the same vLLM pod; all
    histograms for a node are summed. Nodes with fewer than `min_requests`
    samples in the window get p99 None (insufficient data).
    """
    bucket_ms: float | None = None
    window_s: float | None = None
    length: int | None = None
    per_node: dict[str, list[list[int]]] = {}
    all_hists: list[list[int]] = []
    skipped = 0
    for st in stats:
        b = st.get("bucket_ms", DEFAULT_BUCKET_MS)
        if bucket_ms is None:
            bucket_ms = b
        if window_s is None:
            window_s = st.get("window_s")
        for entry in (st.get("per_pod") or {}).values():
            h = entry.get("ttft_hist") if isinstance(entry, Mapping) else None
            if b != bucket_ms or not isinstance(h, list) or not h:
                skipped += 1
                continue
            if length is None:
                length = len(h)
            if len(h) != length:
                skipped += 1
                continue
            all_hists.append(h)
            node = entry.get("node")
            if node:
                per_node.setdefault(node, []).append(h)
    bucket_ms = bucket_ms or DEFAULT_BUCKET_MS

    def summ(hs: list[list[int]]) -> TTFTSummary:
        merged = merge_hists(hs) if hs else []
        n = sum(merged)
        ok = n >= min_requests
        return TTFTSummary(hist_quantile(merged, 0.99, bucket_ms) if ok else None, n, ok)

    return TTFTView(
        nodes={n: summ(hs) for n, hs in per_node.items()},
        cluster=summ(all_hists),
        window_s=window_s,
        bucket_ms=bucket_ms,
        skipped=skipped,
    )


def parse_prom_vector(payload: Mapping[str, Any]) -> list[tuple[dict[str, str], float]]:
    """Prometheus HTTP API instant-query JSON -> [(labels, value)].

    Raises ValueError when the response is not a successful vector result.
    Non-finite values (NaN from rate() on an idle series) become 0.
    """
    if not isinstance(payload, Mapping) or payload.get("status") != "success":
        err = payload.get("error") if isinstance(payload, Mapping) else None
        raise ValueError(f"prometheus query failed: {err or 'bad response'}")
    data = payload.get("data") or {}
    if data.get("resultType") != "vector":
        raise ValueError(f"prometheus resultType {data.get('resultType')!r}, want vector")
    out: list[tuple[dict[str, str], float]] = []
    for s in data.get("result") or []:
        try:
            v = float(s["value"][1])
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        out.append((dict(s.get("metric") or {}), v if math.isfinite(v) else 0.0))
    return out


def prom_bucket_quantile(buckets: Mapping[float, float], q: float) -> float | None:
    """Prometheus histogram_quantile() over cumulative buckets {le: count}.

    Same algorithm as PromQL: linear interpolation inside the bucket holding the
    rank; lower edge of the first bucket is 0; a rank in +Inf returns the
    largest finite upper bound. Returns None when the total is 0.
    """
    les = sorted(buckets)
    if not les or not math.isinf(les[-1]):
        return None
    total = buckets[les[-1]]
    if total <= 0:
        return None
    rank = q * total
    prev_le, prev_c = 0.0, 0.0
    for le in les:
        c = buckets[le]
        if c >= rank:
            if math.isinf(le):
                return prev_le
            if c == prev_c or le <= 0:
                return le
            return prev_le + (le - prev_le) * (rank - prev_c) / (c - prev_c)
        prev_le, prev_c = le, c
    return prev_le


def summarize_prom_ttft(
    samples: Iterable[tuple[Mapping[str, str], float]],
    pod_node: Mapping[str, str],
    window_s: float,
    min_requests: int = 200,
) -> TTFTView:
    """Per-node and cluster TTFT p99 from `sum by (pod, le)(rate(<metric>_bucket[w]))`.

    `samples` values are per-second rates of cumulative bucket counters; requests
    in the window = rate(+Inf) * window_s. Pods are mapped to nodes via `pod_node`
    (current pod list); pods not in it count toward the cluster only. Buckets of
    pods on the same node are summed before the quantile, as histogram_quantile
    over `sum by (node, le)` would.
    """
    per_pod: dict[str, dict[float, float]] = {}
    skipped = 0
    for labels, v in samples:
        pod, le = labels.get("pod"), labels.get("le")
        try:
            le_f = float(le)  # "+Inf" parses to inf
        except (TypeError, ValueError):
            skipped += 1
            continue
        if not pod:
            skipped += 1
            continue
        b = per_pod.setdefault(pod, {})
        b[le_f] = b.get(le_f, 0.0) + max(0.0, v)

    def merge(bs: Iterable[Mapping[float, float]]) -> dict[float, float]:
        out: dict[float, float] = {}
        for b in bs:
            for le, c in b.items():
                out[le] = out.get(le, 0.0) + c
        return out

    def summ(b: Mapping[float, float]) -> TTFTSummary:
        n = int(round(b.get(math.inf, 0.0) * window_s))
        ok = n >= min_requests
        p = prom_bucket_quantile(b, 0.99) if ok else None
        if p is None:
            ok = False
        return TTFTSummary(p * 1000 if p is not None else None, n, ok)

    by_node: dict[str, list[dict[float, float]]] = {}
    for pod, b in per_pod.items():
        node = pod_node.get(pod)
        if node:
            by_node.setdefault(node, []).append(b)
    return TTFTView(
        nodes={n: summ(merge(bs)) for n, bs in by_node.items()},
        cluster=summ(merge(per_pod.values())),
        window_s=window_s,
        bucket_ms=0.0,  # native Prometheus buckets, interpolated
        skipped=skipped,
    )


# ---------------------------------------------------------------------------
# (d) Guard state machine
# ---------------------------------------------------------------------------


class GuardState(str, Enum):
    HEALTHY = "healthy"
    BLOCK_MEMORY_HEAVY = "block-memory-heavy"
    BLOCK_ALL = "block-all"


_STATE_BLOCKS = {
    GuardState.HEALTHY: frozenset(),
    GuardState.BLOCK_MEMORY_HEAVY: frozenset({CLASS_MEMORY_HEAVY}),
    GuardState.BLOCK_ALL: frozenset({CLASS_MEMORY_HEAVY, CLASS_INTEGER}),
}


@dataclass(frozen=True)
class GuardConfig:
    enabled: bool = True
    baseline_ttft_p99_ms: float | None = 60.0
    factor: float = 1.5
    cooldown_s: float = 600.0
    escalate_s: float = 60.0
    min_requests: int = 200

    @classmethod
    def from_config(cls, cfg: Mapping[str, Any]) -> "GuardConfig":
        g = cfg.get("guard", {})
        d = DEFAULT_CONFIG["guard"]
        return cls(
            enabled=bool(g.get("enabled", d["enabled"])),
            baseline_ttft_p99_ms=g.get("baseline_ttft_p99_ms", d["baseline_ttft_p99_ms"]),
            factor=float(g.get("factor", d["factor"])),
            cooldown_s=float(g.get("cooldown_s", d["cooldown_s"])),
            escalate_s=float(g.get("escalate_s", d["escalate_s"])),
            min_requests=int(g.get("min_requests", d["min_requests"])),
        )

    @property
    def threshold_ms(self) -> float | None:
        if not self.baseline_ttft_p99_ms:
            return None
        return self.factor * float(self.baseline_ttft_p99_ms)


@dataclass(frozen=True)
class NodeGuard:
    state: GuardState = GuardState.HEALTHY
    breach_since: float | None = None  # first breaching sample in the current state
    healthy_since: float | None = None  # first healthy sample of the current healthy run
    changed_at: float | None = None

    @property
    def blocks(self) -> frozenset[str]:
        return _STATE_BLOCKS[self.state]


@dataclass(frozen=True)
class GuardAction:
    kind: str  # "block" | "release"
    classes: tuple[str, ...]  # classes newly blocked (evict) or released
    reason: str  # k8s Event reason
    message: str


def guard_from_labels(labels: Mapping[str, str] | None, now: float | None = None) -> NodeGuard:
    """Rebuild guard state from node labels (controller restart)."""
    b = blocks_from_labels(labels)
    if CLASS_INTEGER in b:
        return NodeGuard(GuardState.BLOCK_ALL, changed_at=now)
    if CLASS_MEMORY_HEAVY in b:
        return NodeGuard(GuardState.BLOCK_MEMORY_HEAVY, changed_at=now)
    return NodeGuard(changed_at=now)


def guard_step(
    g: NodeGuard,
    p99_ms: float | None,
    requests: int,
    now: float,
    cfg: GuardConfig,
    has_memory_heavy: bool = True,
) -> tuple[NodeGuard, list[GuardAction]]:
    """Advance one node's guard by one sample.

    healthy --(p99 > factor*baseline and has_memory_heavy)--> block-memory-heavy
    block-memory-heavy --(cfg.escalate_s > 0 and still breaching for >= escalate_s)--> block-all
    block-* --(healthy for >= cooldown_s)--> healthy (all labels released)

    Insufficient data (p99 None, requests < min_requests, or no baseline): no
    transition; the healthy run restarts (no data is not evidence of health),
    the breach timer is kept but escalation needs a breaching sample.
    Guard disabled: release everything immediately.
    """
    if not cfg.enabled:
        if g.state is GuardState.HEALTHY:
            return replace(g, breach_since=None, healthy_since=None), []
        released = tuple(sorted(g.blocks))
        return NodeGuard(changed_at=now), [GuardAction(
            "release", released, "ColoGuardRelease",
            f"guard disabled; released {', '.join(released)} backfill")]

    thr = cfg.threshold_ms
    if p99_ms is None or requests < cfg.min_requests or thr is None:
        return replace(g, healthy_since=None), []

    breaching = p99_ms > thr
    desc = f"TTFT p99 {p99_ms:.0f} ms vs threshold {thr:.0f} ms ({requests} req)"

    if g.state is GuardState.HEALTHY:
        if breaching and has_memory_heavy:
            return NodeGuard(GuardState.BLOCK_MEMORY_HEAVY, breach_since=now, changed_at=now), [
                GuardAction("block", (CLASS_MEMORY_HEAVY,), "ColoGuardEvict",
                            f"{desc}; blocking and evicting memory-heavy backfill")]
        return replace(g, breach_since=None, healthy_since=None), []

    if breaching:
        since = g.breach_since if g.breach_since is not None else now
        if g.state is GuardState.BLOCK_MEMORY_HEAVY and cfg.escalate_s > 0 and now - since >= cfg.escalate_s:
            return NodeGuard(GuardState.BLOCK_ALL, breach_since=now, changed_at=now), [
                GuardAction("block", (CLASS_INTEGER,), "ColoGuardEvict",
                            f"{desc} for {now - since:.0f}s after memory-heavy block; "
                            "blocking and evicting integer backfill")]
        return replace(g, breach_since=since, healthy_since=None), []

    # healthy sample while blocked
    hs = g.healthy_since if g.healthy_since is not None else now
    if now - hs >= cfg.cooldown_s:
        released = tuple(sorted(g.blocks))
        return NodeGuard(changed_at=now), [GuardAction(
            "release", released, "ColoGuardRelease",
            f"{desc}; healthy for {now - hs:.0f}s, releasing {', '.join(released)}")]
    return replace(g, breach_since=None, healthy_since=hs), []


GUARD_SIGNAL_OK = "ok"
GUARD_SIGNAL_NONE = "no_signal"
GUARD_SIGNAL_DISABLED = "disabled"


def guard_signal(summ: TTFTSummary | None, source_ok: bool, cfg: GuardConfig) -> str:
    """Whether the guard can act on a node this tick.

    "no_signal" means: TTFT source down, stale or `none`, too few samples, or no
    baseline. guard_step makes no transition in that case (no block, no evict).
    """
    if not cfg.enabled:
        return GUARD_SIGNAL_DISABLED
    if not source_ok or summ is None or not summ.sufficient or summ.p99_ms is None \
            or cfg.threshold_ms is None:
        return GUARD_SIGNAL_NONE
    return GUARD_SIGNAL_OK


# ---------------------------------------------------------------------------
# (e) Per-node planning
# ---------------------------------------------------------------------------

ACTIVE_LABEL = "colo.active"
CA_TAINT = "ToBeDeletedByClusterAutoscaler"
ROLE_VLLM = "vllm"
ROLE_BACKFILL = "backfill"
ROLE_OTHER = "other"


@dataclass(frozen=True)
class PodFacts:
    """What node planning needs to know about one pod bound to a node."""

    name: str
    role: str = ROLE_OTHER  # ROLE_VLLM | ROLE_BACKFILL | ROLE_OTHER
    service: str | None = None  # backfill: the `app` label (sandbox, toolcall, analytics)
    cls: str | None = None  # backfill: colo.class
    terminating: bool = False  # metadata.deletionTimestamp set
    ready: bool = False
    cpu_m: int = 0
    mem: int = 0


@dataclass(frozen=True)
class NodeFacts:
    name: str
    labels: Mapping[str, str] = field(default_factory=dict)
    ready: bool = True
    unschedulable: bool = False
    taint_keys: frozenset[str] = frozenset()
    alloc_cpu_m: int = 0
    alloc_mem: int = 0


@dataclass(frozen=True)
class Eligibility:
    eligible: bool
    has_vllm: bool
    reason: str  # "ok" | "not-ready" | "cordoned" | "ca-scale-down" | "no-vllm"


def node_eligibility(node: NodeFacts, pods: Iterable[PodFacts]) -> Eligibility:
    """Eligible = Ready, schedulable, no CA deletion taint, and a non-terminating
    vLLM pod bound to the node. The vLLM pod need not be Ready: backfill packs
    while vLLM loads and compiles (startup can take minutes)."""
    has_vllm = any(p.role == ROLE_VLLM and not p.terminating for p in pods)
    if not node.ready:
        reason = "not-ready"
    elif node.unschedulable:
        reason = "cordoned"
    elif CA_TAINT in node.taint_keys:
        reason = "ca-scale-down"
    elif not has_vllm:
        reason = "no-vllm"
    else:
        return Eligibility(True, True, "ok")
    return Eligibility(False, has_vllm, reason)


@dataclass(frozen=True)
class NodeTrack:
    """Controller memory per node across ticks (lost on restart, which only
    restarts the dehydrate grace timer: the conservative direction)."""

    ineligible_since: float | None = None


@dataclass
class NodePlan:
    name: str
    eligible: bool
    has_vllm: bool
    reason: str
    label_patch: dict[str, str | None]  # merge-patch for metadata.labels (None = remove)
    delete_pods: list[str]  # backfill pods to delete (dehydrate)
    drain_reason: str | None  # why delete_pods is non-empty
    draining: list[str]  # backfill pods kept for now on an ineligible node
    reset_guard: bool  # no vLLM: guard state -> healthy, block labels cleared, guard skipped
    track: NodeTrack


def ready_backfill_by_service(pods: Iterable[PodFacts]) -> dict[str, int]:
    out: dict[str, int] = {}
    for p in pods:
        if p.role == ROLE_BACKFILL and p.ready and not p.terminating and p.service:
            out[p.service] = out.get(p.service, 0) + 1
    return out


def drain_cover(
    demand: Mapping[str, int],
    overflow_ready: Mapping[str, int],
    ready_backfill_eligible: Mapping[str, int],
) -> dict[str, bool]:
    """Per service: True when ready overflow plus ready backfill on eligible
    nodes already covers demand, so backfill on ineligible nodes can go."""
    return {
        s: int(overflow_ready.get(s, 0)) + int(ready_backfill_eligible.get(s, 0))
        >= int(demand.get(s, 0))
        for s in set(demand) | set(overflow_ready) | set(ready_backfill_eligible)
    }


def plan_node(
    node: NodeFacts,
    pods: Iterable[PodFacts],
    now: float,
    track: NodeTrack,
    cover: Mapping[str, bool],
    dehydrate_grace_s: float,
) -> NodePlan:
    """Decide colo.active, guard reset, and dehydrate deletes for one G4 node.

    * Eligible: ensure colo.active=true; clear the ineligible timer.
    * Ineligible: remove colo.active at once (stops new backfill placements; the
      caller also drops the node from allocation so overflow scales up first).
      Its remaining backfill pods are deleted, per service, only when
        (a) cover[service] (overflow ready already covers that service), or
        (b) the node has been ineligible for >= dehydrate_grace_s, or
        (c) the node is cordoned or tainted ToBeDeletedByClusterAutoscaler.
      A vLLM Recreate rollout that returns within the grace therefore deletes
      nothing, and a GPU scale-in is make-before-break.
    * No vLLM on the node: reset the guard and remove colo.block.* labels.
    """
    pods = list(pods)
    el = node_eligibility(node, pods)
    labels = node.labels or {}
    patch: dict[str, str | None] = {}
    if el.eligible and labels.get(ACTIVE_LABEL) != "true":
        patch[ACTIVE_LABEL] = "true"
    elif not el.eligible and ACTIVE_LABEL in labels:
        patch[ACTIVE_LABEL] = None
    reset_guard = not el.has_vllm
    if reset_guard:
        for lbl in BLOCK_LABELS.values():
            if lbl in labels:
                patch[lbl] = None

    if el.eligible:
        return NodePlan(node.name, True, True, el.reason, patch, [], None, [], False, NodeTrack())

    since = track.ineligible_since if track.ineligible_since is not None else now
    bf = sorted((p for p in pods if p.role == ROLE_BACKFILL and not p.terminating),
                key=lambda p: p.name)
    deletes: list[str] = []
    reason: str | None = None
    if bf:
        if node.unschedulable:
            reason, deletes = "cordoned", [p.name for p in bf]
        elif CA_TAINT in node.taint_keys:
            reason, deletes = "ca-scale-down", [p.name for p in bf]
        elif now - since >= dehydrate_grace_s:
            reason, deletes = f"ineligible ({el.reason}) for {now - since:.0f}s", [p.name for p in bf]
        else:
            deletes = [p.name for p in bf if p.service and cover.get(p.service, False)]
            if deletes:
                reason = "overflow ready covers demand"
    keep = [p.name for p in bf if p.name not in set(deletes)]
    return NodePlan(node.name, False, el.has_vllm, el.reason, patch, deletes, reason, keep,
                    reset_guard, NodeTrack(since))


def node_capacity(
    node: NodeFacts,
    pods: Iterable[PodFacts],
    blocks: frozenset[str] = frozenset(),
    reserve_cpu_m: int = 0,
) -> NodeCapacity:
    """Spare capacity: allocatable minus every non-backfill pod's requests."""
    used_cpu = used_mem = 0
    for p in pods:
        if p.role != ROLE_BACKFILL:
            used_cpu += p.cpu_m
            used_mem += p.mem
    return NodeCapacity(node.name, node.alloc_cpu_m, used_cpu, blocks,
                        node.alloc_mem, used_mem, reserve_cpu_m)


def guard_label_patch(labels: Mapping[str, str], blocks: frozenset[str]) -> dict[str, str | None]:
    """Label merge-patch that makes colo.block.* match `blocks`."""
    patch: dict[str, str | None] = {}
    for cls, lbl in BLOCK_LABELS.items():
        if cls in blocks and labels.get(lbl) != "true":
            patch[lbl] = "true"
        elif cls not in blocks and lbl in labels:
            patch[lbl] = None
    return patch
