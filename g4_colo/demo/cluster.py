"""Live cluster backend: reads the cluster through the local kubeconfig and reaches
in-cluster HTTP services (loadgen, controller, node agents) through the API
server pod proxy. All kubernetes-client calls are blocking, so they run in
worker threads and are fanned out with asyncio.gather.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from state import NAMESPACES, WATCH_REASONS, build_state, parse_cpu, parse_mem_gib

log = logging.getLogger("demo.cluster")

PROXY_TIMEOUT_S = 4


def _iso(ts: Any) -> str | None:
    if ts is None:
        return None
    try:
        return ts.isoformat()
    except AttributeError:
        return str(ts)


class LiveBackend:
    mode = "live"

    def __init__(self, context: str | None = None):
        from kubernetes import client, config

        try:
            config.load_kube_config(context=context)
        except Exception:  # noqa: BLE001 - fall back when running in a pod
            config.load_incluster_config()
        self.client = client
        self.core = client.CoreV1Api()
        self.apps = client.AppsV1Api()
        self.autoscaling = client.AutoscalingV2Api()

    # ------------------------------------------------------------ pod proxy
    def _proxy_sync(self, ns: str, pod: str, port: int, path: str, method: str = "GET", body: Any = None) -> Any:
        """HTTP request to <pod>:<port>/<path> via the API server pod proxy.

        Uses api_client.call_api with a pre-built resource path so the proxied
        path is not URL-encoded and a JSON body can be sent (the generated
        connect_post_* helpers take no body).
        """
        resource = f"/api/v1/namespaces/{ns}/pods/{pod}:{port}/proxy/{path.lstrip('/')}"
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        ac = self.core.api_client
        if hasattr(ac, "param_serialize"):  # kubernetes client >= 33 (new generator)
            m, url, hdrs, b, post = ac.param_serialize(
                method=method, resource_path=resource, header_params=headers, body=body, auth_settings=["BearerToken"]
            )
            r = ac.call_api(m, url, header_params=hdrs, body=b, post_params=post, _request_timeout=PROXY_TIMEOUT_S)
            data = r.read()
            status = r.status
        else:  # older clients
            resp = ac.call_api(
                resource,
                method,
                header_params=headers,
                body=body,
                auth_settings=["BearerToken"],
                _preload_content=False,
                _request_timeout=PROXY_TIMEOUT_S,
            )
            r = resp[0] if isinstance(resp, tuple) else resp
            data, status = r.data, r.status
        data = data.decode() if isinstance(data, (bytes, bytearray)) else data
        if status >= 400:
            raise RuntimeError(f"HTTP {status} from {ns}/{pod}:{port}/{path}: {str(data)[:200]}")
        try:
            return json.loads(data) if data else None
        except json.JSONDecodeError:
            return {"raw": data}

    async def proxy(self, ns: str, pod: str, port: int, path: str, method: str = "GET", body: Any = None) -> Any:
        return await asyncio.to_thread(self._proxy_sync, ns, pod, port, path, method, body)

    def _ready_pods(self, ns: str, selector: str) -> list[Any]:
        out = []
        for p in self.core.list_namespaced_pod(ns, label_selector=selector).items:
            if p.status.phase != "Running":
                continue
            conds = {c.type: c.status for c in (p.status.conditions or [])}
            if conds.get("Ready") == "True":
                out.append(p)
        return out

    # ------------------------------------------------------------ raw fetch
    def _nodes(self) -> list[dict]:
        out = []
        for n in self.core.list_node().items:
            conds = {c.type: c.status for c in (n.status.conditions or [])}
            out.append(
                {
                    "name": n.metadata.name,
                    "labels": n.metadata.labels or {},
                    "allocatable": dict(n.status.allocatable or {}),
                    "ready": conds.get("Ready") == "True" and not n.spec.unschedulable,
                    "created": _iso(n.metadata.creation_timestamp),
                }
            )
        return out

    def _pods(self) -> list[dict]:
        out = []
        for p in self.core.list_pod_for_all_namespaces().items:
            cpu = 0.0
            mem = 0.0
            for c in p.spec.containers or []:
                req = (c.resources.requests if c.resources else None) or {}
                cpu += parse_cpu(req.get("cpu"))
                mem += parse_mem_gib(req.get("memory"))
            conds = {c.type: c.status for c in (p.status.conditions or [])}
            out.append(
                {
                    "name": p.metadata.name,
                    "namespace": p.metadata.namespace,
                    "uid": p.metadata.uid,
                    "labels": p.metadata.labels or {},
                    "node": p.spec.node_name,
                    "phase": p.status.phase,
                    "ready": conds.get("Ready") == "True",
                    "cpu_req": round(cpu, 3),
                    "mem_req_gib": round(mem, 3),
                    "priority_class": p.spec.priority_class_name,
                    "deleting": p.metadata.deletion_timestamp is not None,
                }
            )
        return out

    def _deployments(self) -> list[dict]:
        out = []
        for ns in ("inference", "colo-svc"):
            for d in self.apps.list_namespaced_deployment(ns).items:
                out.append(
                    {
                        "name": d.metadata.name,
                        "namespace": ns,
                        "replicas": d.spec.replicas or 0,
                        "ready": d.status.ready_replicas or 0,
                    }
                )
        return out

    def _hpas(self) -> list[dict]:
        out = []
        for h in self.autoscaling.list_namespaced_horizontal_pod_autoscaler("inference").items:
            metric = None
            for m in h.status.current_metrics or []:
                if m.pods and m.pods.current:
                    metric = m.pods.current.average_value
                elif m.external and m.external.current:
                    metric = m.external.current.average_value or m.external.current.value
            out.append(
                {
                    "name": h.metadata.name,
                    "namespace": "inference",
                    "target": h.spec.scale_target_ref.name,
                    "min": h.spec.min_replicas,
                    "max": h.spec.max_replicas,
                    "current": h.status.current_replicas,
                    "desired": h.status.desired_replicas,
                    "metric": str(metric) if metric is not None else None,
                }
            )
        return out

    def _events(self) -> list[dict]:
        out = []
        for e in self.core.list_event_for_all_namespaces(limit=2000).items:
            if e.reason not in WATCH_REASONS:
                continue
            t = e.last_timestamp or e.event_time or e.first_timestamp or e.metadata.creation_timestamp
            out.append(
                {
                    "time": _iso(t),
                    "reason": e.reason,
                    "kind": e.involved_object.kind,
                    "object": e.involved_object.name,
                    "namespace": e.involved_object.namespace,
                    "message": (e.message or "")[:400],
                    "source": (e.source.component if e.source else None) or e.reporting_component,
                    "count": e.count,
                }
            )
        out.sort(key=lambda x: x["time"] or "", reverse=True)
        return out[:50]

    def _configmap(self) -> dict | None:
        try:
            cm = self.core.read_namespaced_config_map("colo-config", "colo-system")
            return json.loads((cm.data or {}).get("config.json") or "{}")
        except Exception:  # noqa: BLE001
            return None

    async def _gather_named(self, coros: dict[str, Any], errors: list[str], what: str) -> dict:
        names = list(coros)
        res = await asyncio.gather(*coros.values(), return_exceptions=True)
        out = {}
        for n, r in zip(names, res):
            if isinstance(r, Exception):
                errors.append(f"{what} {n}: {type(r).__name__}: {str(r)[:160]}")
            else:
                out[n] = r
        return out

    async def raw(self) -> dict:
        errors: list[str] = []
        t = asyncio.to_thread
        base = await self._gather_named(
            {
                "nodes": t(self._nodes),
                "pods": t(self._pods),
                "deployments": t(self._deployments),
                "hpas": t(self._hpas),
                "events": t(self._events),
                "configmap": t(self._configmap),
            },
            errors,
            "list",
        )
        pods = base.get("pods") or []

        def running(ns: str, app: str) -> list[dict]:
            return [p for p in pods if p["namespace"] == ns and p["labels"].get("app") == app and p["phase"] == "Running" and p["ready"]]

        lg = running("inference", "loadgen")
        agents = running("colo-system", "colo-node-agent")
        ctrl = running("colo-system", "colo-controller")
        lg_res = await self._gather_named({p["name"]: self.proxy("inference", p["name"], 8080, "stats") for p in lg}, errors, "loadgen")
        ag_res = await self._gather_named({p["node"]: self.proxy("colo-system", p["name"], 8080, "cpus") for p in agents}, errors, "agent")
        controller: dict = {"state": None, "config": None}
        if ctrl:
            c = await self._gather_named(
                {
                    "state": self.proxy("colo-system", ctrl[0]["name"], 8080, "state"),
                    "config": self.proxy("colo-system", ctrl[0]["name"], 8080, "config"),
                },
                errors,
                "controller",
            )
            controller.update(c)
        else:
            errors.append("controller: no Ready pod app=colo-controller in colo-system")
        return {
            **base,
            "loadgen": lg_res,
            "agents": ag_res,
            "controller": controller,
            "errors": errors,
        }

    async def state(self) -> dict:
        return build_state(await self.raw(), mode=self.mode)

    # ------------------------------------------------------------ actions
    async def _controller_pod(self) -> str:
        pods = await asyncio.to_thread(self._ready_pods, "colo-system", "app=colo-controller")
        if not pods:
            raise RuntimeError("no Ready colo-controller pod")
        return pods[0].metadata.name

    async def set_gpu_rate(self, rate: float) -> dict:
        pods = await asyncio.to_thread(self._ready_pods, "inference", "app=loadgen")
        if not pods:
            raise RuntimeError("no Ready loadgen pods")
        names = [p.metadata.name for p in pods]
        res = await asyncio.gather(
            *[self.proxy("inference", n, 8080, "rate", "POST", {"rate": float(rate)}) for n in names],
            return_exceptions=True,
        )
        return {n: (str(r) if isinstance(r, Exception) else r) for n, r in zip(names, res)}

    async def _post_config(self, patch: dict) -> Any:
        pod = await self._controller_pod()
        cur = await self.proxy("colo-system", pod, 8080, "config")
        cur = cur if isinstance(cur, dict) else {}
        for k, v in patch.items():
            if isinstance(v, dict) and isinstance(cur.get(k), dict):
                cur[k] = {**cur[k], **v}
            else:
                cur[k] = v
        return await self.proxy("colo-system", pod, 8080, "config", "POST", cur)

    async def set_demand(self, demand: dict) -> Any:
        return await self._post_config({"demand": {k: int(v) for k, v in demand.items()}})

    async def set_guard(self, enabled: bool) -> Any:
        return await self._post_config({"guard": {"enabled": bool(enabled)}})

    async def baseline(self) -> Any:
        pod = await self._controller_pod()
        return await self.proxy("colo-system", pod, 8080, "baseline", "POST", {})


__all__ = ["LiveBackend", "NAMESPACES"]
