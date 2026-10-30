"""colo-controller: backfills spare G4 CPU with CPU services and guards GPU TTFT.

Loop (every LOOP_S seconds, or at once after POST /config or /baseline):
  1. read ConfigMap colo-system/colo-config (create with defaults if missing)
  2. read nodes, pods and the backfill/overflow/buffer Deployments in parallel;
     take the last TTFT snapshot (polled by a background task, see ttft_source.py)
  3. guard, per G4 node with a vLLM pod: TTFT p99 vs factor x baseline -> block
     labels, evictions, Events; no signal -> no action; no vLLM -> guard reset
  4. per node (colo_logic.plan_node): colo.active on eligible nodes; on ineligible
     nodes remove colo.active at once and delete backfill only when overflow is
     ready, after dehydrate_grace_s, or when the node is cordoned / CA-tainted
  5. allocate backfill per service over eligible nodes; overflow = demand -
     backfill; patch Deployment scales without dropping ready capacity
  6. patch colo-buffer replicas
Writes run in parallel per phase (labels+events, evictions, scales, dehydrate).
HTTP on :8080: GET /state, GET/POST /config, POST /baseline, GET /metrics,
GET /healthz (liveness), GET /readyz.

All decisions live in colo_logic.py; this file and ttft_source.py only do I/O.
"""

from __future__ import annotations

import asyncio
import collections
import datetime as dt
import json
import logging
import os
import socket
import time
from typing import Any

import aiohttp
from aiohttp import web
from kubernetes import client
from kubernetes import config as kconfig
from kubernetes.client.rest import ApiException

import colo_logic as L
import ttft_source as TS

NS_SVC = os.environ.get("COLO_SVC_NAMESPACE", "colo-svc")
NS_SYS = os.environ.get("COLO_SYSTEM_NAMESPACE", "colo-system")
NS_INF = os.environ.get("INFERENCE_NAMESPACE", "inference")
CM_NAME = os.environ.get("CONFIGMAP_NAME", "colo-config")
CM_KEY = "config.json"
BUFFER_DEPLOYMENT = os.environ.get("BUFFER_DEPLOYMENT", "colo-buffer")
EVENT_NAMESPACE = os.environ.get("EVENT_NAMESPACE", "default")  # Node is cluster-scoped
LOOP_S = float(os.environ.get("LOOP_S", "2"))
PORT = int(os.environ.get("PORT", "8080"))
LOADGEN_PORT = int(os.environ.get("LOADGEN_PORT", "8080"))
LOADGEN_TIMEOUT_S = float(os.environ.get("LOADGEN_TIMEOUT_S", "2"))
PROM_TIMEOUT_S = float(os.environ.get("PROM_TIMEOUT_S", "10"))
PROM_ENDPOINT = os.environ.get("PROM_ENDPOINT", TS.GMP_ENDPOINT)
# TTFT snapshot older than this is treated as no signal.
TTFT_STALE_S = float(os.environ.get("TTFT_STALE_S", str(max(15.0, 5 * LOOP_S))))
# /healthz fails when the loop has not finished an iteration for this long.
LIVENESS_STALE_S = float(os.environ.get("LIVENESS_STALE_S", str(5 * LOOP_S)))
STARTUP_GRACE_S = float(os.environ.get("STARTUP_GRACE_S", "60"))
K8S_WRITE_CONCURRENCY = int(os.environ.get("K8S_WRITE_CONCURRENCY", "16"))
COMPONENT = "colo-controller"
INSTANCE = os.environ.get("POD_NAME", socket.gethostname())

log = logging.getLogger(COMPONENT)


# ---------------------------------------------------------------------------
# k8s object helpers
# ---------------------------------------------------------------------------


def _ready(conditions) -> bool:
    return any(c.type == "Ready" and c.status == "True" for c in (conditions or []))


def _labels(obj) -> dict[str, str]:
    return dict(obj.metadata.labels or {})


def _pod_req(pod) -> tuple[int, int]:
    spec = pod.spec
    cs = [(c.resources.requests or {}) if c.resources else {} for c in spec.containers or []]
    ics = [(c.resources.requests or {}) if c.resources else {} for c in spec.init_containers or []]
    return L.pod_requests(cs, ics, spec.overhead or None)


def _pod_facts(pod) -> L.PodFacts:
    lb = _labels(pod)
    ns = pod.metadata.namespace
    if ns == NS_INF and lb.get("app") == "vllm":
        role = L.ROLE_VLLM
    elif ns == NS_SVC and lb.get("colo.tier") == "backfill":
        role = L.ROLE_BACKFILL
    else:
        role = L.ROLE_OTHER
    cpu, mem = _pod_req(pod)
    return L.PodFacts(
        name=pod.metadata.name,
        role=role,
        service=lb.get("app") if role == L.ROLE_BACKFILL else None,
        cls=lb.get("colo.class"),
        terminating=bool(pod.metadata.deletion_timestamp),
        ready=bool(pod.status) and _ready(pod.status.conditions),
        cpu_m=cpu,
        mem=mem,
    )


def _node_facts(node) -> L.NodeFacts:
    alloc = (node.status.allocatable if node.status else None) or {}
    return L.NodeFacts(
        name=node.metadata.name,
        labels=_labels(node),
        ready=bool(node.status) and _ready(node.status.conditions),
        unschedulable=bool(node.spec and node.spec.unschedulable),
        taint_keys=frozenset(t.key for t in ((node.spec.taints if node.spec else None) or [])),
        alloc_cpu_m=L.parse_cpu(alloc.get("cpu")),
        alloc_mem=L.parse_mem(alloc.get("memory")),
    )


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------


class Controller:
    def __init__(self, core: Any = None, apps: Any = None, token_fn: Any = None) -> None:
        if core is None or apps is None:
            try:
                kconfig.load_incluster_config()
            except kconfig.ConfigException:
                kconfig.load_kube_config()
            kc = client.Configuration.get_default_copy()
            kc.connection_pool_maxsize = max(kc.connection_pool_maxsize or 4,
                                             K8S_WRITE_CONCURRENCY + 4)
            client.Configuration.set_default(kc)
        self.core = core if core is not None else client.CoreV1Api()
        self.apps = apps if apps is not None else client.AppsV1Api()
        self.config: dict[str, Any] = L.validate_config({})
        self.guards: dict[str, L.NodeGuard] = {}
        self.actions: collections.deque[dict[str, Any]] = collections.deque(maxlen=100)
        self.ttft: L.TTFTView | None = None
        self.services_state: dict[str, dict[str, int]] = {}
        self.nodes_state: list[dict[str, Any]] = []
        self.buffer_state: dict[str, int | None] = {}
        self.last_tick: float = 0.0
        self.last_error: str | None = None
        self.tick_errors = 0
        self.wake = asyncio.Event()
        self.cfg_lock = asyncio.Lock()
        self.http: aiohttp.ClientSession | None = None  # loadgen
        self.prom_http: aiohttp.ClientSession | None = None
        self.token_fn = token_fn or TS.AdcToken()
        self.io_sem = asyncio.Semaphore(K8S_WRITE_CONCURRENCY)
        self.tracks: dict[str, L.NodeTrack] = {}
        self.loadgen_ips: list[str] = []
        self.ttft_snap: TS.TTFTSnapshot | None = None
        self.ttft_source_ok = False
        self.ttft_source_why: str | None = "not polled yet"
        self.guard_sufficient = False
        self.write_errors = 0
        self.started_at = time.time()
        self.last_loop: float = 0.0

    # -- bookkeeping -------------------------------------------------------

    def record(self, kind: str, target: str, message: str, **extra: Any) -> None:
        entry = {"ts": _now_iso(), "kind": kind, "target": target, "message": message, **extra}
        self.actions.append(entry)
        log.info("%s %s: %s", kind, target, message)

    # -- config ------------------------------------------------------------

    def _read_config(self) -> dict[str, Any]:
        try:
            cm = self.core.read_namespaced_config_map(CM_NAME, NS_SYS)
        except ApiException as e:
            if e.status != 404:
                raise
            body = client.V1ConfigMap(
                metadata=client.V1ObjectMeta(name=CM_NAME, namespace=NS_SYS,
                                             labels={"app": COMPONENT}),
                data={CM_KEY: json.dumps(L.DEFAULT_CONFIG, indent=2)})
            self.core.create_namespaced_config_map(NS_SYS, body)
            self.record("config-created", f"{NS_SYS}/{CM_NAME}", "created with defaults")
            return L.validate_config({})
        raw = (cm.data or {}).get(CM_KEY) or "{}"
        try:
            return L.validate_config(json.loads(raw))
        except (ValueError, TypeError) as e:  # json.JSONDecodeError is a ValueError
            log.error("invalid %s/%s: %s; keeping last good config", CM_NAME, CM_KEY, e)
            self.last_error = f"invalid config: {e}"
            return self.config

    def _write_config(self, cfg: dict[str, Any]) -> None:
        text = json.dumps(cfg, indent=2)
        for _ in range(3):
            try:
                cm = self.core.read_namespaced_config_map(CM_NAME, NS_SYS)
            except ApiException as e:
                if e.status != 404:
                    raise
                body = client.V1ConfigMap(
                    metadata=client.V1ObjectMeta(name=CM_NAME, namespace=NS_SYS,
                                                 labels={"app": COMPONENT}),
                    data={CM_KEY: text})
                self.core.create_namespaced_config_map(NS_SYS, body)
                return
            cm.data = dict(cm.data or {})
            cm.data[CM_KEY] = text
            try:
                self.core.replace_namespaced_config_map(CM_NAME, NS_SYS, cm)
                return
            except ApiException as e:
                if e.status != 409:  # resourceVersion conflict -> retry
                    raise
        raise RuntimeError("configmap update conflicted 3 times")

    async def update_config(self, patch: dict[str, Any]) -> dict[str, Any]:
        async with self.cfg_lock:
            current = await asyncio.to_thread(self._read_config)
            new = L.validate_config(L.deep_merge(current, patch))
            await asyncio.to_thread(self._write_config, new)
            self.config = new
        self.record("config-updated", f"{NS_SYS}/{CM_NAME}", json.dumps(patch, sort_keys=True))
        self.wake.set()
        return new

    # -- k8s writes (sync; run via to_thread) --------------------------------

    def _patch_node_labels(self, node: str, labels: dict[str, str | None]) -> None:
        self.core.patch_node(node, {"metadata": {"labels": labels}})

    def _delete_pod(self, ns: str, name: str) -> bool:
        try:
            self.core.delete_namespaced_pod(name, ns, grace_period_seconds=5)
            return True
        except ApiException as e:
            if e.status == 404:
                return False
            raise

    def _node_event(self, node_name: str, node_uid: str | None, reason: str, message: str,
                    action: str) -> None:
        now = dt.datetime.now(dt.timezone.utc)
        ev = client.CoreV1Event(
            metadata=client.V1ObjectMeta(generate_name=f"{node_name}.colo-",
                                         namespace=EVENT_NAMESPACE),
            involved_object=client.V1ObjectReference(kind="Node", name=node_name, uid=node_uid,
                                                     api_version="v1"),
            reason=reason,
            message=message[:1000],
            type="Warning" if reason == "ColoGuardEvict" else "Normal",
            action=action,
            source=client.V1EventSource(component=COMPONENT),
            reporting_component=COMPONENT,
            reporting_instance=INSTANCE,
            first_timestamp=now,
            last_timestamp=now,
            count=1,
        )
        try:
            self.core.create_namespaced_event(EVENT_NAMESPACE, ev)
        except ApiException as e:
            log.warning("event for node %s failed: %s", node_name, e.reason)

    def _read_deploy(self, ns: str, name: str) -> tuple[int, int] | None:
        try:
            d = self.apps.read_namespaced_deployment(name, ns)
        except ApiException as e:
            if e.status == 404:
                return None
            raise
        return int(d.spec.replicas or 0), int((d.status and d.status.ready_replicas) or 0)

    def _scale(self, ns: str, name: str, replicas: int) -> None:
        self.apps.patch_namespaced_deployment_scale(name, ns, {"spec": {"replicas": replicas}})

    # -- TTFT source (background) ---------------------------------------------

    async def poll_ttft(self) -> None:
        """Refresh self.ttft_snap every LOOP_S, off the reconcile critical path."""
        while True:
            try:
                self.ttft_snap = await TS.poll_once(
                    self.config,
                    loadgen_session=self.http, prom_session=self.prom_http,
                    loadgen_ips=list(self.loadgen_ips), loadgen_port=LOADGEN_PORT,
                    namespace=NS_INF, token_fn=self.token_fn, prom_endpoint=PROM_ENDPOINT)
            except Exception:  # poll_once does not raise; belt and braces
                log.exception("ttft poll failed")
            await asyncio.sleep(LOOP_S)

    # -- bounded parallel writes -------------------------------------------

    async def _io(self, fn, *args, **kwargs):
        async with self.io_sem:
            return await asyncio.to_thread(fn, *args, **kwargs)

    async def _gather_writes(self, what: str, coros: list) -> None:
        if not coros:
            return
        res = await asyncio.gather(*coros, return_exceptions=True)
        errs = [r for r in res if isinstance(r, BaseException)]
        for e in errs:
            log.error("%s failed: %s: %s", what, type(e).__name__, e)
        if errs:
            self.write_errors += len(errs)
            self.last_error = f"{what}: {len(errs)} failed, e.g. {type(errs[0]).__name__}: {errs[0]}"

    async def _patch_labels_rec(self, node: str, patch: dict[str, str | None], why: str) -> None:
        await self._io(self._patch_node_labels, node, patch)
        self.record("labels", node, f"{json.dumps(patch, sort_keys=True)} ({why})")

    async def _delete_rec(self, node: str, pod: str, kind: str, why: str) -> None:
        if await self._io(self._delete_pod, NS_SVC, pod):
            self.record(kind, node, f"deleted {NS_SVC}/{pod} ({why})")

    async def _event(self, node: str, uid: str | None, a: L.GuardAction) -> None:
        await self._io(self._node_event, node, uid, a.reason, a.message,
                       "Evict" if a.kind == "block" else "Release")

    async def _scale_service(self, svc: str, bf: tuple[int, int], ov: tuple[int, int],
                             plan: L.ScalePlan) -> None:
        new = {"backfill": (f"{svc}-backfill", bf[0], plan.backfill),
               "overflow": (f"{svc}-overflow", ov[0], plan.overflow)}
        for tier in plan.order:  # order matters within a service
            dname, old, val = new[tier]
            if val != old:
                await self._io(self._scale, NS_SVC, dname, val)
                self.record("scale", f"{NS_SVC}/{dname}", f"{old} -> {val}")

    async def _scale_buffer(self, old: int, want: int) -> None:
        await self._io(self._scale, NS_SYS, BUFFER_DEPLOYMENT, want)
        self.record("scale", f"{NS_SYS}/{BUFFER_DEPLOYMENT}", f"{old} -> {want}")

    # -- one reconcile pass --------------------------------------------------

    async def tick(self) -> None:
        async with self.cfg_lock:
            cfg = await asyncio.to_thread(self._read_config)
            self.config = cfg
        gcfg = L.GuardConfig.from_config(cfg)
        demand = cfg["demand"]
        grace = float(cfg["dehydrate_grace_s"])
        reserve_m = L.parse_cpu(cfg.get("reserve_cpu_per_node", 0))

        # -- reads, all in parallel -------------------------------------------
        deploy_names = [(NS_SVC, f"{s}-{t}") for s in L.SERVICE_ORDER for t in ("backfill", "overflow")]
        res = await asyncio.gather(
            self._io(self.core.list_node),
            self._io(self.core.list_pod_for_all_namespaces,
                     field_selector="status.phase!=Succeeded,status.phase!=Failed"),
            self._io(self._read_deploy, NS_SYS, BUFFER_DEPLOYMENT),
            *(self._io(self._read_deploy, ns, nm) for ns, nm in deploy_names),
        )
        nodes, pods, buf = res[0].items, res[1].items, res[2]
        deploys = dict(zip((nm for _, nm in deploy_names), res[3:]))
        now = time.time()

        self.loadgen_ips = [p.status.pod_ip for p in pods
                            if p.metadata.namespace == NS_INF and _labels(p).get("app") == "loadgen"
                            and p.status and p.status.pod_ip and _ready(p.status.conditions)]
        vllm_pod_node = {p.metadata.name: p.spec.node_name for p in pods
                         if p.metadata.namespace == NS_INF and _labels(p).get("app") == "vllm"
                         and p.spec.node_name}

        ttft, source_ok, source_why = TS.snapshot_view(
            self.ttft_snap, cfg, vllm_pod_node, gcfg.min_requests, now, TTFT_STALE_S)
        self.ttft, self.ttft_source_ok, self.ttft_source_why = ttft, source_ok, source_why

        g4 = sorted((n for n in nodes if _labels(n).get("colo.role") == "g4"),
                    key=lambda n: n.metadata.name)
        facts: dict[str, list[L.PodFacts]] = collections.defaultdict(list)
        for p in pods:
            if p.spec.node_name:
                facts[p.spec.node_name].append(_pod_facts(p))
        nfacts = {n.metadata.name: _node_facts(n) for n in g4}
        uids = {n.metadata.name: n.metadata.uid for n in g4}

        # -- guard, only on nodes with a vLLM pod ------------------------------
        label_patches: dict[str, dict[str, str | None]] = collections.defaultdict(dict)
        event_coros: list = []
        evict_coros: list = []
        signals: dict[str, str] = {}
        elig = {nm: L.node_eligibility(nf, facts[nm]) for nm, nf in nfacts.items()}
        for nm, nf in nfacts.items():
            if not elig[nm].has_vllm:
                # 4.4: no vLLM -> guard reset (labels cleared via plan_node), not evaluated
                g = self.guards.get(nm)
                if g is None or g.state is not L.GuardState.HEALTHY:
                    self.guards[nm] = L.NodeGuard(changed_at=now)
                signals[nm] = "idle"
                continue
            g = self.guards.get(nm) or L.guard_from_labels(nf.labels, now)
            summ = ttft.nodes.get(nm) if source_ok else None
            signals[nm] = L.guard_signal(summ, source_ok, gcfg)
            p99 = summ.p99_ms if summ else None
            reqs = summ.requests if summ else 0
            has_mem_bf = any(p.role == L.ROLE_BACKFILL and p.cls == L.CLASS_MEMORY_HEAVY
                             and not p.terminating for p in facts[nm])
            g, acts = L.guard_step(g, p99, reqs, now, gcfg, has_memory_heavy=has_mem_bf)
            self.guards[nm] = g
            for a in acts:
                self.record(a.reason, nm, a.message, classes=list(a.classes))
                event_coros.append(self._event(nm, uids[nm], a))
            label_patches[nm].update(L.guard_label_patch(nf.labels, g.blocks))
            # blocked-class backfill is evicted every tick (affinity is IgnoredDuringExecution)
            for p in facts[nm]:
                if p.role == L.ROLE_BACKFILL and p.cls in g.blocks and not p.terminating:
                    evict_coros.append(self._delete_rec(nm, p.name, "evict", f"class {p.cls} blocked"))
        for gone in set(self.guards) - set(nfacts):
            del self.guards[gone]

        # -- per-node plan: colo.active, make-before-break dehydrate ------------
        ready_bf_eligible: dict[str, int] = {}
        for nm in nfacts:
            if elig[nm].eligible:
                blocks = self.guards[nm].blocks
                for s, c in L.ready_backfill_by_service(
                        p for p in facts[nm] if p.cls not in blocks).items():
                    ready_bf_eligible[s] = ready_bf_eligible.get(s, 0) + c
        overflow_ready = {s: (deploys[f"{s}-overflow"] or (0, 0))[1] for s in L.SERVICE_ORDER}
        cover = L.drain_cover(demand, overflow_ready, ready_bf_eligible)

        caps: list[L.NodeCapacity] = []
        node_rows: list[dict[str, Any]] = []
        dehydrate_coros: list = []
        for nm, nf in nfacts.items():
            plan = L.plan_node(nf, facts[nm], now, self.tracks.get(nm, L.NodeTrack()), cover, grace)
            self.tracks[nm] = plan.track
            if L.ACTIVE_LABEL in plan.label_patch:
                self.record("active" if plan.eligible else "dehydrate", nm,
                            "colo.active=true (vllm present)" if plan.eligible
                            else f"removed colo.active ({plan.reason})")
            label_patches[nm].update(plan.label_patch)
            for pod in plan.delete_pods:
                dehydrate_coros.append(self._delete_rec(nm, pod, "dehydrate-pod", plan.drain_reason or ""))
            g = self.guards.get(nm, L.NodeGuard())
            cap = L.node_capacity(nf, facts[nm], g.blocks, reserve_m)
            if plan.eligible:
                caps.append(cap)
            summ = ttft.nodes.get(nm)
            sig = signals.get(nm, "idle")
            node_rows.append({
                "name": nm,
                "eligible": plan.eligible,
                "ineligible_reason": None if plan.eligible else plan.reason,
                "has_vllm": plan.has_vllm,
                "spare_cpu": round(cap.spare_cpu_m / 1000, 3) if plan.eligible else 0,
                "slots": L.node_slots(cap) if plan.eligible else {s: 0 for s in L.SERVICE_ORDER},
                "blocks": sorted(g.blocks),
                "ttft_p99_ms": summ.p99_ms if summ else None,
                "requests_in_window": summ.requests if summ else 0,
                "guard_state": (L.GUARD_SIGNAL_NONE if sig == L.GUARD_SIGNAL_NONE
                                and g.state is L.GuardState.HEALTHY else g.state.value),
                "guard_signal": sig,
                "draining_pods": len(plan.draining),
                "ineligible_for_s": (round(now - plan.track.ineligible_since, 1)
                                     if plan.track.ineligible_since is not None else None),
            })
        for gone in set(self.tracks) - set(nfacts):
            del self.tracks[gone]

        # -- allocation + safe scale plan --------------------------------------
        alloc_res = L.allocate(demand, caps)
        for row in node_rows:
            row["planned_backfill"] = alloc_res.placement.get(row["name"], {})
        svc_rows: dict[str, dict[str, int]] = {}
        scale_coros: list = []
        for svc in L.SERVICE_ORDER:
            bf, ov = deploys[f"{svc}-backfill"], deploys[f"{svc}-overflow"]
            row = {"demand": demand[svc],
                   "backfill_target": alloc_res.backfill[svc],
                   "overflow_target": alloc_res.overflow[svc]}
            if bf is None or ov is None:
                log.warning("deployment missing: %s/%s-backfill or -overflow; skipping", NS_SVC, svc)
                row.update(backfill_desired=bf[0] if bf else 0, backfill_ready=bf[1] if bf else 0,
                           overflow_desired=ov[0] if ov else 0, overflow_ready=ov[1] if ov else 0)
                svc_rows[svc] = row
                continue
            plan = L.plan_scale(bf[0], ov[0], bf[1], ov[1],
                                alloc_res.backfill[svc], alloc_res.overflow[svc])
            scale_coros.append(self._scale_service(svc, bf, ov, plan))
            row.update(backfill_desired=plan.backfill, backfill_ready=bf[1],
                       overflow_desired=plan.overflow, overflow_ready=ov[1])
            svc_rows[svc] = row
        want_buf = int(cfg["buffer_replicas"])
        if buf is None:
            log.warning("deployment %s/%s missing", NS_SYS, BUFFER_DEPLOYMENT)
            self.buffer_state = {"desired": None, "ready": None}
        else:
            if buf[0] != want_buf:
                scale_coros.append(self._scale_buffer(buf[0], want_buf))
            self.buffer_state = {"desired": want_buf, "ready": buf[1]}

        # -- writes: labels+events, then evictions, then scale, then dehydrate --
        # Labels first so a blocked/inactive node takes no new backfill before pods
        # are deleted; scale before dehydrate so a lowered backfill target is in
        # place before its pods go (fewer Pending re-creations).
        await self._gather_writes("label/event", [
            self._patch_labels_rec(nm, p, "reconcile") for nm, p in label_patches.items() if p
        ] + event_coros)
        await self._gather_writes("guard evict", evict_coros)
        await self._gather_writes("scale", scale_coros)
        await self._gather_writes("dehydrate", dehydrate_coros)

        evaluated = [s for s in signals.values() if s != "idle"]
        self.guard_sufficient = bool(evaluated) and all(s == L.GUARD_SIGNAL_OK for s in evaluated)
        self.services_state = svc_rows
        self.nodes_state = node_rows
        self.last_tick = time.time()

    async def run(self) -> None:
        while True:
            errors_before = self.write_errors
            try:
                await self.tick()
                if self.write_errors == errors_before:
                    self.last_error = None
            except Exception as e:  # keep looping; surface on /state and /metrics
                self.tick_errors += 1
                self.last_error = f"{type(e).__name__}: {e}"
                log.exception("tick failed")
            self.last_loop = time.time()
            self.wake.clear()
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=LOOP_S)
            except asyncio.TimeoutError:
                pass

    def live(self, now: float | None = None) -> tuple[bool, str]:
        """Liveness: the loop must have completed an iteration (good or failed)
        within LIVENESS_STALE_S, after STARTUP_GRACE_S from process start."""
        now = time.time() if now is None else now
        ref = self.last_loop or self.started_at
        if self.last_loop == 0 and now - self.started_at <= STARTUP_GRACE_S:
            return True, "starting"
        if now - ref > LIVENESS_STALE_S:
            return False, f"reconcile loop stalled: last iteration {now - ref:.0f}s ago"
        return True, "ok"

    # -- views -------------------------------------------------------------

    def cluster_p99(self) -> float | None:
        if not self.ttft_source_ok or self.ttft is None:
            return None
        return self.ttft.cluster.p99_ms

    def guard_overall(self) -> str:
        if not self.config["guard"]["enabled"]:
            return L.GUARD_SIGNAL_DISABLED
        return L.GUARD_SIGNAL_OK if self.guard_sufficient else L.GUARD_SIGNAL_NONE

    def source_state(self, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        snap = self.ttft_snap
        return {
            "type": self.config["ttft_source"]["type"],
            "ok": self.ttft_source_ok,
            "reason": self.ttft_source_why,
            "age_s": round(now - snap.fetched_at, 1) if snap and snap.fetched_at else None,
            "window_s": self.ttft.window_s if self.ttft and self.ttft_source_ok else None,
        }

    def state(self) -> dict[str, Any]:
        t = self.ttft if self.ttft_source_ok else None
        return {
            "config": self.config,
            "services": self.services_state,
            "nodes": self.nodes_state,
            "buffer": self.buffer_state,
            "guard_state": self.guard_overall(),  # ok | no_signal | disabled
            "guard_data_sufficient": self.guard_sufficient,
            "ttft_source": self.source_state(),
            "cluster_ttft_p99_ms": self.cluster_p99(),
            "cluster_requests_in_window": t.cluster.requests if t else 0,
            "baseline_ttft_p99_ms": self.config["guard"].get("baseline_ttft_p99_ms"),
            "threshold_ttft_p99_ms": L.GuardConfig.from_config(self.config).threshold_ms,
            "loadgen_window_s": t.window_s if t else None,  # kept for the demo UI
            "events": list(self.actions),
            "last_tick": self.last_tick,
            "last_loop": self.last_loop,
            "last_error": self.last_error,
        }

    def metrics(self) -> str:
        out: list[str] = []

        def g(name: str, help_: str, samples: list[tuple[dict[str, str], float | None]]) -> None:
            out.append(f"# HELP {name} {help_}")
            out.append(f"# TYPE {name} gauge")
            for labels, v in samples:
                if v is None:
                    continue
                lbl = ",".join(f'{k}="{str(val).replace(chr(34), "")}"'
                               for k, val in labels.items())
                out.append(f"{name}{{{lbl}}} {float(v)}" if lbl else f"{name} {float(v)}")

        s = self.services_state
        for key, help_ in (("demand", "configured demand (pods)"),
                           ("backfill_desired", "backfill Deployment replicas"),
                           ("backfill_ready", "backfill ready pods"),
                           ("overflow_desired", "overflow Deployment replicas"),
                           ("overflow_ready", "overflow ready pods")):
            g(f"colo_service_{key}", help_, [({"service": k}, v.get(key)) for k, v in s.items()])
        n = self.nodes_state
        g("colo_node_spare_cpu", "spare CPU cores for backfill",
          [({"node": r["name"]}, r["spare_cpu"]) for r in n])
        g("colo_node_eligible", "1 if node is eligible for backfill",
          [({"node": r["name"]}, 1 if r["eligible"] else 0) for r in n])
        g("colo_node_draining_pods", "backfill pods kept on an ineligible node until overflow is ready",
          [({"node": r["name"]}, r["draining_pods"]) for r in n])
        g("colo_node_slots", "backfill slots per service on node",
          [({"node": r["name"], "service": sv}, c) for r in n for sv, c in r["slots"].items()])
        g("colo_node_block", "1 if class is blocked on node",
          [({"node": r["name"], "class": c}, 1 if c in r["blocks"] else 0)
           for r in n for c in L.CLASSES])
        g("colo_node_ttft_p99_ms", "per-node TTFT p99 over the source window",
          [({"node": r["name"]}, r["ttft_p99_ms"]) for r in n])
        g("colo_node_requests_in_window", "TTFT samples in window",
          [({"node": r["name"]}, r["requests_in_window"]) for r in n])
        states = [st.value for st in L.GuardState] + [L.GUARD_SIGNAL_NONE]
        g("colo_node_guard_state", "1 for the node's current guard state",
          [({"node": r["name"], "state": st}, 1 if r["guard_state"] == st else 0)
           for r in n for st in states])
        g("colo_node_guard_data_sufficient", "1 if the guard had enough TTFT data for the node",
          [({"node": r["name"]}, 1 if r["guard_signal"] == L.GUARD_SIGNAL_OK else 0)
           for r in n if r["guard_signal"] != "idle"])
        g("colo_guard_data_sufficient",
          "1 if every G4 node with vLLM had enough TTFT data this tick; 0 = guard inert",
          [({}, 1 if self.guard_sufficient else 0)])
        snap = self.ttft_snap
        g("colo_ttft_source_up", "1 if the configured TTFT source answered and is fresh",
          [({"type": self.config["ttft_source"]["type"]}, 1 if self.ttft_source_ok else 0)])
        g("colo_ttft_source_age_seconds", "age of the last TTFT fetch",
          [({}, time.time() - snap.fetched_at if snap and snap.fetched_at else None)])
        g("colo_cluster_ttft_p99_ms", "cluster TTFT p99", [({}, self.cluster_p99())])
        g("colo_baseline_ttft_p99_ms", "guard baseline",
          [({}, self.config["guard"].get("baseline_ttft_p99_ms"))])
        g("colo_guard_enabled", "1 if guard enabled",
          [({}, 1 if self.config["guard"]["enabled"] else 0)])
        g("colo_buffer_replicas", "configured warm buffer replicas",
          [({}, self.config["buffer_replicas"])])
        g("colo_last_tick_timestamp_seconds", "unix time of last good tick",
          [({}, self.last_tick or None)])
        g("colo_tick_errors", "failed ticks since start", [({}, self.tick_errors)])
        g("colo_write_errors", "failed k8s writes since start", [({}, self.write_errors)])
        return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def make_app(ctl: Controller, start_loops: bool = True) -> web.Application:
    routes = web.RouteTableDef()

    @routes.get("/state")
    async def state(_: web.Request) -> web.Response:
        return web.json_response(ctl.state())

    @routes.get("/config")
    async def get_config(_: web.Request) -> web.Response:
        return web.json_response(ctl.config)

    @routes.post("/config")
    async def post_config(req: web.Request) -> web.Response:
        try:
            patch = await req.json()
            if not isinstance(patch, dict):
                raise ValueError("body must be a JSON object")
            new = await ctl.update_config(patch)
        except (ValueError, TypeError) as e:
            return web.json_response({"error": str(e)}, status=400)
        return web.json_response(new)

    @routes.post("/baseline")
    async def post_baseline(_: web.Request) -> web.Response:
        t = ctl.ttft if ctl.ttft_source_ok else None
        if t is None or not t.cluster.sufficient or t.cluster.p99_ms is None:
            n = t.cluster.requests if t else 0
            need = ctl.config["guard"].get("min_requests", 200)
            why = "" if ctl.ttft_source_ok else f" (ttft source: {ctl.ttft_source_why})"
            return web.json_response(
                {"error": f"insufficient data: {n} requests in window, need {need}{why}"},
                status=400)
        p99 = round(t.cluster.p99_ms, 3)
        new = await ctl.update_config({"guard": {"baseline_ttft_p99_ms": p99}})
        return web.json_response({"baseline_ttft_p99_ms": p99, "config": new})

    @routes.get("/metrics")
    async def metrics(_: web.Request) -> web.Response:
        return web.Response(text=ctl.metrics(), content_type="text/plain", charset="utf-8")

    @routes.get("/healthz")
    async def healthz(_: web.Request) -> web.Response:
        ok, why = ctl.live()
        return web.json_response({"ok": ok, "reason": why, "last_loop": ctl.last_loop,
                                  "last_tick": ctl.last_tick, "last_error": ctl.last_error},
                                 status=200 if ok else 503)

    @routes.get("/readyz")
    async def readyz(_: web.Request) -> web.Response:
        return web.json_response({"ok": True, "last_tick": ctl.last_tick})

    app = web.Application()
    app.add_routes(routes)

    async def lifecycle(_: web.Application):
        ctl.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=LOADGEN_TIMEOUT_S))
        ctl.prom_http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=PROM_TIMEOUT_S))
        tasks = [asyncio.create_task(ctl.run()), asyncio.create_task(ctl.poll_ttft())] \
            if start_loops else []
        yield
        for t in tasks:
            t.cancel()
        await ctl.http.close()
        await ctl.prom_http.close()

    app.cleanup_ctx.append(lifecycle)
    return app


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    ctl = Controller()
    web.run_app(make_app(ctl), port=PORT, access_log=None)


if __name__ == "__main__":
    main()
