"""Live cluster backend: reads the cluster through the local kubeconfig and reaches
in-cluster HTTP services (loadgen, controller, node agents) through the API
server pod proxy. All kubernetes-client calls are blocking, so they run in
worker threads and are fanned out with asyncio.gather.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from typing import Any

from state import NAMESPACES, WATCH_REASONS, build_state, parse_cpu, parse_mem_gib

log = logging.getLogger("demo.cluster")

PROXY_TIMEOUT_S = 4
PODS_FIELD_SELECTOR = "status.phase!=Succeeded,status.phase!=Failed"
# High-volume reasons that are never shown; dropping them server-side keeps the list small.
_NOISY_REASONS = ("Scheduled", "Pulling", "Pulled", "Created", "Started", "Killing", "SuccessfulCreate",
                  "SuccessfulDelete", "ScalingReplicaSet", "FailedScheduling", "Unhealthy", "BackOff")
EVENTS_FIELD_SELECTOR = ",".join(f"reason!={r}" for r in _NOISY_REASONS)
EVENTS_LIMIT = 200
EVENTS_TTL_S = float(os.environ.get("DEMO_EVENTS_TTL_S", "10"))
# Demo accelerator: on a rate change, force HPA minReplicas/replicas so scale-out shows in seconds
# instead of waiting for the HPA metric window. Demo-only; production never patches the HPA.
FAST_SCALE = os.environ.get("DEMO_FAST_SCALE", "1") == "1"
FAST_SCALE_RPS = float(os.environ.get("DEMO_FAST_SCALE_RPS", "700"))


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

        self._adc_creds = None
        self._adc_cfg = None
        try:
            config.load_kube_config(context=context)
            self.incluster = False
        except Exception:  # noqa: BLE001 - fall back when running in a pod or Cloud Run
            try:
                config.load_incluster_config()
                self.incluster = True
            except Exception:  # noqa: BLE001 - Cloud Run / Google ADC mode
                self.incluster = False
                self._init_gke_adc(client)
        self.client = client
        self.core = client.CoreV1Api()
        self.apps = client.AppsV1Api()
        self.autoscaling = client.AutoscalingV2Api()
        self._pod_ips: dict[tuple[str, str], str] = {}
        self._events_cache: tuple[float, list[dict]] | None = None
        self.fast_scale = FAST_SCALE
        self.fast_scale_forced = False  # True while this process has HPA minReplicas > 1

    def _init_gke_adc(self, client: Any) -> None:
        import base64
        import os
        import google.auth

        endpoint = os.environ.get("GKE_ENDPOINT", "").strip()
        ca_b64 = os.environ.get("GKE_CA_CERT", "").strip()
        if not endpoint or not ca_b64:
            raise RuntimeError("No kubeconfig, in-cluster config, or GKE_ENDPOINT/GKE_CA_CERT found")
        creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        ca_path = "/tmp/gke-cluster-ca.crt"
        with open(ca_path, "wb") as f:
            f.write(base64.b64decode(ca_b64))
        cfg = client.Configuration()
        cfg.host = endpoint if endpoint.startswith("https://") else f"https://{endpoint}"
        cfg.ssl_ca_cert = ca_path
        self._adc_creds = creds
        self._adc_lock = threading.Lock()

        # set_default() deep-copies cfg, so refreshing a token on our own object never reaches the
        # ApiClients. The hook runs on every request against the copy actually in use.
        def _refresh_hook(c: Any) -> None:
            c.api_key = {"authorization": self._adc_token()}
            c.api_key_prefix = {"authorization": "Bearer"}

        cfg.refresh_api_key_hook = _refresh_hook
        _refresh_hook(cfg)
        self._adc_cfg = cfg
        client.Configuration.set_default(cfg)

    def _adc_token(self) -> str:
        import google.auth.transport.requests

        with self._adc_lock:
            # google-auth marks creds invalid ~3-5 min before expiry, so this refreshes ahead of time.
            if not self._adc_creds.valid or not self._adc_creds.token:
                self._adc_creds.refresh(google.auth.transport.requests.Request())
            return self._adc_creds.token

    def _ensure_adc(self) -> None:
        # Token refresh happens per request in refresh_api_key_hook; kept as a cheap pre-warm.
        if self._adc_creds is not None:
            self._adc_token()

    # ------------------------------------------------------------ pod proxy
    def _direct_http_sync(self, ip: str, port: int, path: str, method: str = "GET", body: Any = None) -> Any:
        import urllib.request

        url = f"http://{ip}:{port}/{path.lstrip('/')}"
        headers = {"Accept": "application/json"}
        data_bytes = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data_bytes = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data_bytes, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=PROXY_TIMEOUT_S) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else None

    def _proxy_sync(self, ns: str, pod: str, port: int, path: str, method: str = "GET", body: Any = None) -> Any:
        """HTTP request to <pod>:<port>/<path> (direct Pod IP when in-cluster, else API server pod proxy)."""
        if self.incluster:
            ip = self._pod_ips.get((ns, pod))
            if ip:
                try:
                    return self._direct_http_sync(ip, port, path, method, body)
                except Exception:  # noqa: BLE001 - fall back to apiserver proxy if pod IP changed
                    pass
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
        self._ensure_adc()
        out = []
        for p in self.core.list_namespaced_pod(ns, label_selector=selector).items:
            if p.status.phase != "Running":
                continue
            conds = {c.type: c.status for c in (p.status.conditions or [])}
            if conds.get("Ready") == "True":
                if p.status.pod_ip:
                    self._pod_ips[(p.metadata.namespace, p.metadata.name)] = p.status.pod_ip
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
        # Finished pods are never drawn; let the apiserver drop them.
        items = self.core.list_pod_for_all_namespaces(field_selector=PODS_FIELD_SELECTOR).items
        for p in items:
            if p.status and p.status.pod_ip:
                self._pod_ips[(p.metadata.namespace, p.metadata.name)] = p.status.pod_ip
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
        # Events change slowly relative to the 2 s poll: list at most every EVENTS_TTL_S, drop the
        # high-volume kubelet/scheduler reasons server-side, and cap the page size.
        now = time.monotonic()
        if self._events_cache is not None and now - self._events_cache[0] < EVENTS_TTL_S:
            return self._events_cache[1]
        out = []
        for e in self.core.list_event_for_all_namespaces(field_selector=EVENTS_FIELD_SELECTOR, limit=EVENTS_LIMIT).items:
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
        out = out[:50]
        self._events_cache = (now, out)
        return out

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
        await asyncio.to_thread(self._ensure_adc)
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
            # /state already carries the effective config, so one proxy call is enough.
            c = await self._gather_named(
                {"state": self.proxy("colo-system", ctrl[0]["name"], 8080, "state")},
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

    def _fast_scale_vllm(self, rate: float) -> None:
        if not self.fast_scale:
            return
        want = 2 if float(rate) > FAST_SCALE_RPS else 1
        try:
            self.autoscaling.patch_namespaced_horizontal_pod_autoscaler(
                "vllm", "inference", {"spec": {"minReplicas": want}}
            )
            self.fast_scale_forced = want > 1
        except Exception as e:  # noqa: BLE001
            log.warning("hpa patch failed: %s", e)
        try:
            self.apps.patch_namespaced_deployment_scale(
                "vllm", "inference", {"spec": {"replicas": want}}
            )
        except Exception as e:  # noqa: BLE001
            log.warning("deployment scale patch failed: %s", e)

    def _restore_hpa_min_sync(self) -> bool:
        """Put HPA minReplicas back to 1 if it is above 1 (left behind by the accelerator)."""
        try:
            h = self.autoscaling.read_namespaced_horizontal_pod_autoscaler("vllm", "inference")
            if (h.spec.min_replicas or 1) > 1:
                self.autoscaling.patch_namespaced_horizontal_pod_autoscaler(
                    "vllm", "inference", {"spec": {"minReplicas": 1}}
                )
                log.info("restored HPA vllm minReplicas %s -> 1", h.spec.min_replicas)
            self.fast_scale_forced = False
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("hpa minReplicas restore failed: %s", e)
            return False

    async def restore_hpa_min(self) -> bool:
        if not self.fast_scale:
            return True
        return await asyncio.to_thread(self._restore_hpa_min_sync)

    async def set_gpu_rate(self, rate: float) -> dict:
        pods = await asyncio.to_thread(self._ready_pods, "inference", "app=loadgen")
        if not pods:
            raise RuntimeError("no Ready loadgen pods")
        names = [p.metadata.name for p in pods]
        res, _ = await asyncio.gather(
            asyncio.gather(
                *[self.proxy("inference", n, 8080, "rate", "POST", {"rate": float(rate)}) for n in names],
                return_exceptions=True,
            ),
            asyncio.to_thread(self._fast_scale_vllm, float(rate)),
        )
        return {n: (str(r) if isinstance(r, Exception) else r) for n, r in zip(names, res)}

    async def _post_config(self, patch: dict) -> Any:
        # The controller deep-merges the POST body into its config, so send only the patch:
        # a read-modify-write here could overwrite a concurrent kubectl edit.
        pod = await self._controller_pod()
        return await self.proxy("colo-system", pod, 8080, "config", "POST", patch)

    async def set_demand(self, demand: dict) -> Any:
        return await self._post_config({"demand": {k: int(v) for k, v in demand.items()}})

    async def set_guard(self, enabled: bool) -> Any:
        return await self._post_config({"guard": {"enabled": bool(enabled)}})

    async def baseline(self) -> Any:
        pod = await self._controller_pod()
        return await self.proxy("colo-system", pod, 8080, "baseline", "POST", {})


__all__ = ["LiveBackend", "NAMESPACES"]
