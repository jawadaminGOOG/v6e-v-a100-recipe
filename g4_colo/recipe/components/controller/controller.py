"""colo-controller: backfills spare G4 CPU with CPU services and guards GPU TTFT.

Loop (every LOOP_S seconds):
  1. read ConfigMap colo-system/colo-config (create with defaults if missing)
  2. list nodes and pods; poll every Ready loadgen pod's /stats
  3. guard: per G4 node TTFT p99 vs factor x baseline -> block labels, evictions, Events
  4. allocate backfill per service from spare slots (respecting block labels);
     overflow = demand - backfill; patch Deployment scales safely
  5. patch colo-buffer replicas
HTTP on :8080: GET /state, POST /config, POST /baseline, GET /metrics, GET /healthz.

All decisions live in colo_logic.py; this file only does I/O.
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

NS_SVC = os.environ.get("COLO_SVC_NAMESPACE", "colo-svc")
NS_SYS = os.environ.get("COLO_SYSTEM_NAMESPACE", "colo-system")
NS_INF = os.environ.get("INFERENCE_NAMESPACE", "inference")
CM_NAME = os.environ.get("CONFIGMAP_NAME", "colo-config")
CM_KEY = "config.json"
BUFFER_DEPLOYMENT = os.environ.get("BUFFER_DEPLOYMENT", "colo-buffer")
EVENT_NAMESPACE = os.environ.get("EVENT_NAMESPACE", "default")  # Node is cluster-scoped
LOOP_S = float(os.environ.get("LOOP_S", "5"))
PORT = int(os.environ.get("PORT", "8080"))
LOADGEN_PORT = int(os.environ.get("LOADGEN_PORT", "8080"))
LOADGEN_TIMEOUT_S = float(os.environ.get("LOADGEN_TIMEOUT_S", "2"))
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


def _is_backfill(pod) -> bool:
    return pod.metadata.namespace == NS_SVC and _labels(pod).get("colo.tier") == "backfill"


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------


class Controller:
    def __init__(self) -> None:
        try:
            kconfig.load_incluster_config()
        except kconfig.ConfigException:
            kconfig.load_kube_config()
        self.core = client.CoreV1Api()
        self.apps = client.AppsV1Api()
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
        self.http: aiohttp.ClientSession | None = None

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

    # -- loadgen -----------------------------------------------------------

    async def _fetch_stats(self, ip: str, window_s: float) -> dict[str, Any] | None:
        url = f"http://{ip}:{LOADGEN_PORT}/stats"
        try:
            assert self.http is not None
            async with self.http.get(url, params={"window_s": str(int(window_s))}) as r:
                if r.status != 200:
                    log.warning("loadgen %s: HTTP %s", ip, r.status)
                    return None
                return await r.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
            log.warning("loadgen %s: %s", ip, e)
            return None

    async def poll_loadgen(self, pods, window_s: float) -> list[dict[str, Any]]:
        ips = [p.status.pod_ip for p in pods
               if p.metadata.namespace == NS_INF and _labels(p).get("app") == "loadgen"
               and p.status and p.status.pod_ip and _ready(p.status.conditions)]
        res = await asyncio.gather(*(self._fetch_stats(ip, window_s) for ip in ips))
        return [r for r in res if isinstance(r, dict)]

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

    # -- one reconcile pass --------------------------------------------------

    async def tick(self) -> None:
        async with self.cfg_lock:
            cfg = await asyncio.to_thread(self._read_config)
            self.config = cfg
        gcfg = L.GuardConfig.from_config(cfg)
        now = time.time()

        nodes_resp, pods_resp = await asyncio.gather(
            asyncio.to_thread(self.core.list_node),
            asyncio.to_thread(self.core.list_pod_for_all_namespaces,
                              field_selector="status.phase!=Succeeded,status.phase!=Failed"),
        )
        nodes = nodes_resp.items
        pods = pods_resp.items
        stats = await self.poll_loadgen(pods, cfg["guard"]["window_s"])
        ttft = L.summarize_ttft(stats, gcfg.min_requests)
        self.ttft = ttft
        if ttft.window_s is not None and ttft.window_s != cfg["guard"]["window_s"]:
            log.debug("loadgen window %ss differs from guard.window_s %ss",
                      ttft.window_s, cfg["guard"]["window_s"])

        g4 = [n for n in nodes if _labels(n).get("colo.role") == "g4"]
        pods_by_node: dict[str, list] = collections.defaultdict(list)
        for p in pods:
            if p.spec.node_name:
                pods_by_node[p.spec.node_name].append(p)

        # 3. guard (before allocation so slots reflect this tick's blocks)
        for n in g4:
            name = n.metadata.name
            labels = _labels(n)
            g = self.guards.get(name) or L.guard_from_labels(labels, now)
            summ = ttft.nodes.get(name)
            p99 = summ.p99_ms if summ else None
            reqs = summ.requests if summ else 0
            g, acts = L.guard_step(g, p99, reqs, now, gcfg)
            self.guards[name] = g
            for a in acts:
                self.record(a.reason, name, a.message, classes=list(a.classes))
                await asyncio.to_thread(self._node_event, name, n.metadata.uid, a.reason,
                                        a.message, "Evict" if a.kind == "block" else "Release")
            # reconcile labels to guard state
            want = g.blocks
            patch = {}
            for cls, lbl in L.BLOCK_LABELS.items():
                has = labels.get(lbl) == "true"
                if cls in want and not has:
                    patch[lbl] = "true"
                elif cls not in want and lbl in labels:
                    patch[lbl] = None
            if patch:
                await asyncio.to_thread(self._patch_node_labels, name, patch)
                self.record("labels", name, json.dumps(patch, sort_keys=True))
                for lbl, v in patch.items():
                    labels[lbl] = v  # local view
            # evict backfill pods of blocked classes (affinity is IgnoredDuringExecution)
            for p in pods_by_node.get(name, []):
                if _is_backfill(p) and _labels(p).get("colo.class") in want \
                        and not p.metadata.deletion_timestamp:
                    if await asyncio.to_thread(self._delete_pod, NS_SVC, p.metadata.name):
                        self.record("evict", name, f"deleted {NS_SVC}/{p.metadata.name} "
                                    f"(class {_labels(p).get('colo.class')})")
        for gone in set(self.guards) - {n.metadata.name for n in g4}:
            del self.guards[gone]

        # 1. eligible nodes and spare capacity
        reserve_m = L.parse_cpu(cfg.get("reserve_cpu_per_node", 0))
        caps: list[L.NodeCapacity] = []
        node_rows: list[dict[str, Any]] = []
        for n in g4:
            name = n.metadata.name
            npods = pods_by_node.get(name, [])
            has_vllm = any(p.metadata.namespace == NS_INF and _labels(p).get("app") == "vllm"
                           and p.status and _ready(p.status.conditions) for p in npods)
            eligible = _ready(n.status.conditions) and has_vllm \
                and not n.spec.unschedulable
            used_cpu = used_mem = 0
            for p in npods:
                if not _is_backfill(p):
                    c, m = _pod_req(p)
                    used_cpu += c
                    used_mem += m
            alloc = n.status.allocatable or {}
            g = self.guards.get(name, L.NodeGuard())
            cap = L.NodeCapacity(
                name=name,
                alloc_cpu_m=L.parse_cpu(alloc.get("cpu")),
                used_cpu_m=used_cpu,
                blocks=g.blocks,
                alloc_mem=L.parse_mem(alloc.get("memory")),
                used_mem=used_mem,
                reserve_cpu_m=reserve_m,
            )
            if eligible:
                caps.append(cap)
            summ = ttft.nodes.get(name)
            node_rows.append({
                "name": name,
                "eligible": eligible,
                "spare_cpu": round(cap.spare_cpu_m / 1000, 3) if eligible else 0,
                "slots": L.node_slots(cap) if eligible else {s: 0 for s in L.SERVICE_ORDER},
                "blocks": sorted(g.blocks),
                "ttft_p99_ms": summ.p99_ms if summ else None,
                "requests_in_window": summ.requests if summ else 0,
                "guard_state": g.state.value,
            })

        # 2. allocation + safe scale
        alloc_res = L.allocate(cfg["demand"], caps)
        for row in node_rows:
            row["planned_backfill"] = alloc_res.placement.get(row["name"], {})
        svc_rows: dict[str, dict[str, int]] = {}
        for svc in L.SERVICE_ORDER:
            bf_name, ov_name = f"{svc}-backfill", f"{svc}-overflow"
            bf, ov = await asyncio.gather(asyncio.to_thread(self._read_deploy, NS_SVC, bf_name),
                                          asyncio.to_thread(self._read_deploy, NS_SVC, ov_name))
            row = {"demand": cfg["demand"][svc],
                   "backfill_target": alloc_res.backfill[svc],
                   "overflow_target": alloc_res.overflow[svc]}
            if bf is None or ov is None:
                log.warning("deployment missing: %s/%s or %s; skipping", NS_SVC, bf_name, ov_name)
                row.update(backfill_desired=bf[0] if bf else 0, backfill_ready=bf[1] if bf else 0,
                           overflow_desired=ov[0] if ov else 0, overflow_ready=ov[1] if ov else 0)
                svc_rows[svc] = row
                continue
            plan = L.plan_scale(bf[0], ov[0], bf[1], ov[1],
                                alloc_res.backfill[svc], alloc_res.overflow[svc])
            new = {"backfill": (bf_name, bf[0], plan.backfill),
                   "overflow": (ov_name, ov[0], plan.overflow)}
            for tier in plan.order:
                dname, old, val = new[tier]
                if val != old:
                    await asyncio.to_thread(self._scale, NS_SVC, dname, val)
                    self.record("scale", f"{NS_SVC}/{dname}", f"{old} -> {val}")
            row.update(backfill_desired=plan.backfill, backfill_ready=bf[1],
                       overflow_desired=plan.overflow, overflow_ready=ov[1])
            svc_rows[svc] = row

        # 5. warm buffer
        buf = await asyncio.to_thread(self._read_deploy, NS_SYS, BUFFER_DEPLOYMENT)
        want_buf = int(cfg["buffer_replicas"])
        if buf is None:
            log.warning("deployment %s/%s missing", NS_SYS, BUFFER_DEPLOYMENT)
            self.buffer_state = {"desired": None, "ready": None}
        else:
            if buf[0] != want_buf:
                await asyncio.to_thread(self._scale, NS_SYS, BUFFER_DEPLOYMENT, want_buf)
                self.record("scale", f"{NS_SYS}/{BUFFER_DEPLOYMENT}", f"{buf[0]} -> {want_buf}")
            self.buffer_state = {"desired": want_buf, "ready": buf[1]}

        self.services_state = svc_rows
        self.nodes_state = node_rows
        self.last_tick = time.time()
        self.last_error = None

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception as e:  # keep looping; surface on /state and /metrics
                self.tick_errors += 1
                self.last_error = f"{type(e).__name__}: {e}"
                log.exception("tick failed")
            self.wake.clear()
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=LOOP_S)
            except asyncio.TimeoutError:
                pass

    # -- views -------------------------------------------------------------

    def cluster_p99(self) -> float | None:
        return self.ttft.cluster.p99_ms if self.ttft else None

    def state(self) -> dict[str, Any]:
        t = self.ttft
        return {
            "config": self.config,
            "services": self.services_state,
            "nodes": self.nodes_state,
            "buffer": self.buffer_state,
            "cluster_ttft_p99_ms": self.cluster_p99(),
            "cluster_requests_in_window": t.cluster.requests if t else 0,
            "baseline_ttft_p99_ms": self.config["guard"].get("baseline_ttft_p99_ms"),
            "threshold_ttft_p99_ms": L.GuardConfig.from_config(self.config).threshold_ms,
            "loadgen_window_s": t.window_s if t else None,
            "events": list(self.actions),
            "last_tick": self.last_tick,
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
        g("colo_node_slots", "backfill slots per service on node",
          [({"node": r["name"], "service": sv}, c) for r in n for sv, c in r["slots"].items()])
        g("colo_node_block", "1 if class is blocked on node",
          [({"node": r["name"], "class": c}, 1 if c in r["blocks"] else 0)
           for r in n for c in L.CLASSES])
        g("colo_node_ttft_p99_ms", "per-node TTFT p99 over loadgen window",
          [({"node": r["name"]}, r["ttft_p99_ms"]) for r in n])
        g("colo_node_requests_in_window", "TTFT samples in window",
          [({"node": r["name"]}, r["requests_in_window"]) for r in n])
        g("colo_node_guard_state", "1 for the node's current guard state",
          [({"node": r["name"], "state": st.value}, 1 if r["guard_state"] == st.value else 0)
           for r in n for st in L.GuardState])
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
        return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def make_app(ctl: Controller) -> web.Application:
    routes = web.RouteTableDef()

    @routes.get("/state")
    async def state(_: web.Request) -> web.Response:
        return web.json_response(ctl.state())

    @routes.get("/config")
    async def get_config(_: web.Request) -> web.Response:
        return web.json_response(ctl.state().get("config", {}))

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
        t = ctl.ttft
        if t is None or not t.cluster.sufficient or t.cluster.p99_ms is None:
            n = t.cluster.requests if t else 0
            need = ctl.config["guard"].get("min_requests", 200)
            return web.json_response(
                {"error": f"insufficient data: {n} requests in window, need {need}"}, status=400)
        new = await ctl.update_config({"guard": {"baseline_ttft_p99_ms": t.cluster.p99_ms}})
        return web.json_response({"baseline_ttft_p99_ms": t.cluster.p99_ms, "config": new})

    @routes.get("/metrics")
    async def metrics(_: web.Request) -> web.Response:
        return web.Response(text=ctl.metrics(), content_type="text/plain", charset="utf-8")

    @routes.get("/healthz")
    async def healthz(_: web.Request) -> web.Response:
        return web.json_response({"ok": True, "last_tick": ctl.last_tick,
                                  "last_error": ctl.last_error})

    app = web.Application()
    app.add_routes(routes)

    async def lifecycle(_: web.Application):
        ctl.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=LOADGEN_TIMEOUT_S))
        task = asyncio.create_task(ctl.run())
        yield
        task.cancel()
        await ctl.http.close()

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
