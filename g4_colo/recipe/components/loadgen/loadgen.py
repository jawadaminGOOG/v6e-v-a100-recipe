"""colo-loadgen: open-loop Poisson load generator for vLLM pods.

Each replica:
  * discovers Ready vLLM pods (label VLLM_LABEL in VLLM_NAMESPACE) and the
    number of Ready loadgen replicas (REPLICAS_LABEL) via the Kubernetes API
    every DISCOVERY_INTERVAL_S (2 s);
  * sends streaming /v1/completions requests with Poisson arrivals at
    rate_total / replicas, round-robin across the discovered pods;
  * keeps per-pod rolling 30 s TTFT / E2E histograms (2 ms buckets to 2 s plus
    overflow) and serves them on :8080.

HTTP API (:8080)
  GET  /stats            JSON, see build_stats()
  POST /rate {"rate":x}  set the *total* rate across all replicas (rps)
  GET  /rate[?rate=x]    read or set the total rate
  GET  /metrics          Prometheus
  GET  /healthz

Prompts are a generic toxicity-classification instruction (~550 tokens) plus a
synthetic message assembled from word lists and a unique id/timestamp. No
customer data, no tokenizer.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import socket
import time
from dataclasses import dataclass, field

import aiohttp
from aiohttp import web
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

from hist import RollingHistogram, percentile
from prompts import build_prompt

log = logging.getLogger("loadgen")

WINDOW_S = 30
BUCKET_MS = 2.0
MAX_MS = 2000.0
SA_NS_FILE = "/var/run/secrets/kubernetes.io/serviceaccount/namespace"

PROM_BUCKETS = (
    0.005, 0.01, 0.015, 0.02, 0.025, 0.03, 0.035, 0.04, 0.05, 0.06, 0.075,
    0.09, 0.1, 0.125, 0.15, 0.2, 0.3, 0.5, 0.75, 1.0, 2.0, 5.0,
)
M_TTFT = Histogram(
    "loadgen_ttft_seconds", "Time to first token", ["pod", "node"], buckets=PROM_BUCKETS
)
M_E2E = Histogram(
    "loadgen_e2e_seconds", "End-to-end request latency", ["pod", "node"], buckets=PROM_BUCKETS
)
M_INFLIGHT = Gauge("loadgen_inflight", "Requests in flight", ["pod"])
M_REQS = Counter("loadgen_requests_total", "Completed requests by result code", ["code"])
M_RATE = Gauge("loadgen_rate_rps", "Configured arrival rate", ["scope"])
M_TARGETS = Gauge("loadgen_targets", "Ready vLLM pods discovered")
M_REPLICAS = Gauge("loadgen_replicas", "Loadgen replicas the total rate is divided by")


def env(name: str, default: str) -> str:
    v = os.environ.get(name)
    return default if v is None or v == "" else v


@dataclass(frozen=True)
class Target:
    name: str
    ip: str
    node: str


@dataclass
class PodStats:
    node: str
    ttft: RollingHistogram = field(default_factory=lambda: RollingHistogram(WINDOW_S, BUCKET_MS, MAX_MS))
    e2e: RollingHistogram = field(default_factory=lambda: RollingHistogram(WINDOW_S, BUCKET_MS, MAX_MS))
    inflight: int = 0
    ok: int = 0
    err: int = 0
    # per-second ring of [sec, ok, err] for windowed counts
    ring: list = field(default_factory=lambda: [[-1, 0, 0] for _ in range(WINDOW_S)])

    def tick(self, now: float, ok: bool) -> None:
        sec = int(now)
        slot = self.ring[sec % WINDOW_S]
        if slot[0] != sec:
            slot[0], slot[1], slot[2] = sec, 0, 0
        slot[1 if ok else 2] += 1

    def window_counts(self, now: float) -> tuple[int, int]:
        sec = int(now)
        ok = err = 0
        for s, o, e in self.ring:
            if sec - WINDOW_S < s <= sec:
                ok += o
                err += e
        return ok, err


class LoadGen:
    def __init__(self) -> None:
        self.vllm_ns = env("VLLM_NAMESPACE", "inference")
        self.vllm_label = env("VLLM_LABEL", "app=vllm")
        self.vllm_port = int(env("VLLM_PORT", "8000"))
        self.model = env("MODEL", "Qwen/Qwen3-1.7B-Base")
        self.replicas_label = env("REPLICAS_LABEL", "app=loadgen")
        self.max_tokens = int(env("MAX_TOKENS", "16"))
        self.max_inflight = int(env("MAX_INFLIGHT", "4000"))
        self.req_timeout = float(env("REQUEST_TIMEOUT_S", "60"))
        self.discovery_interval = float(env("DISCOVERY_INTERVAL_S", "2"))
        self.replicas_override = int(env("REPLICAS", "0"))
        # Local/dev: VLLM_TARGETS="http://127.0.0.1:8000,http://host2:8000" skips k8s.
        self.static_targets = [t for t in env("VLLM_TARGETS", "").split(",") if t]
        self.replica = env("POD_NAME", socket.gethostname())
        self.namespace = env("POD_NAMESPACE", "") or self._sa_namespace() or self.vllm_ns

        self.rate_total = float(env("INITIAL_RATE", "0"))
        self.replicas = max(1, self.replicas_override or 1)
        self.rate_gen = 0
        self.targets: list[Target] = []
        self.stats: dict[str, PodStats] = {}
        self.rr = 0
        self.inflight_total = 0
        self.tasks: set[asyncio.Task] = set()
        self.session: aiohttp.ClientSession | None = None
        self.core_api = None
        self.discovery_ok = False
        self.started = time.time()
        self._update_rate_metrics()

    # ------------------------------------------------------------------ rate
    @staticmethod
    def _sa_namespace() -> str:
        try:
            with open(SA_NS_FILE) as f:
                return f.read().strip()
        except OSError:
            return ""

    @property
    def rate_local(self) -> float:
        return self.rate_total / max(1, self.replicas)

    def set_rate(self, total: float) -> None:
        if total < 0:
            raise ValueError("rate must be >= 0")
        self.rate_total = float(total)
        self.rate_gen += 1
        self._update_rate_metrics()
        log.info("rate_total=%.3f replicas=%d rate_local=%.3f", self.rate_total, self.replicas, self.rate_local)

    def _update_rate_metrics(self) -> None:
        M_RATE.labels("total").set(self.rate_total)
        M_RATE.labels("local").set(self.rate_local)
        M_REPLICAS.set(self.replicas)

    # ------------------------------------------------------------- discovery
    def _init_k8s(self) -> None:
        from kubernetes import client, config

        try:
            config.load_incluster_config()
        except config.ConfigException:
            config.load_kube_config()
        self.core_api = client.CoreV1Api()

    @staticmethod
    def _ready(pod) -> bool:
        if pod.metadata.deletion_timestamp is not None:
            return False
        if not pod.status or pod.status.phase != "Running" or not pod.status.pod_ip:
            return False
        for c in pod.status.conditions or []:
            if c.type == "Ready":
                return c.status == "True"
        return False

    def _discover_sync(self) -> tuple[list[Target], int]:
        pods = self.core_api.list_namespaced_pod(
            self.vllm_ns, label_selector=self.vllm_label, _request_timeout=5
        )
        targets = sorted(
            (
                Target(p.metadata.name, p.status.pod_ip, p.spec.node_name or "")
                for p in pods.items
                if self._ready(p)
            ),
            key=lambda t: t.name,
        )
        replicas = self.replicas_override
        if not replicas:
            lg = self.core_api.list_namespaced_pod(
                self.namespace, label_selector=self.replicas_label, _request_timeout=5
            )
            replicas = sum(1 for p in lg.items if self._ready(p))
        return targets, max(1, replicas)

    async def discovery_loop(self) -> None:
        if self.static_targets:
            self.targets = [
                Target(u.split("://", 1)[-1], u, "local") for u in self.static_targets
            ]
            self.discovery_ok = True
            M_TARGETS.set(len(self.targets))
            return
        await asyncio.to_thread(self._init_k8s)
        while True:
            try:
                targets, replicas = await asyncio.to_thread(self._discover_sync)
                if targets != self.targets:
                    log.info("targets: %s", [(t.name, t.ip, t.node) for t in targets])
                if replicas != self.replicas:
                    log.info("replicas %d -> %d", self.replicas, replicas)
                self.targets = targets
                self.replicas = replicas
                self.discovery_ok = True
                self._update_rate_metrics()
                M_TARGETS.set(len(targets))
            except Exception as e:  # keep last known targets
                log.warning("discovery failed: %s", e)
            await asyncio.sleep(self.discovery_interval)

    def _url(self, t: Target) -> str:
        if t.ip.startswith("http"):
            return t.ip.rstrip("/") + "/v1/completions"
        return f"http://{t.ip}:{self.vllm_port}/v1/completions"

    # -------------------------------------------------------------- requests
    def _stats_for(self, t: Target) -> PodStats:
        st = self.stats.get(t.name)
        if st is None:
            st = self.stats[t.name] = PodStats(node=t.node)
        elif st.node != t.node:
            st.node = t.node
        return st

    def _body(self, rng: random.Random) -> bytes:
        return json.dumps(
            {
                "model": self.model,
                "prompt": build_prompt(rng),
                "max_tokens": self.max_tokens,
                "temperature": 0,
                "stream": True,
                "stream_options": {"include_usage": True},
            }
        ).encode()

    async def one_request(self, t: Target, body: bytes) -> None:
        st = self._stats_for(t)
        st.inflight += 1
        self.inflight_total += 1
        g = M_INFLIGHT.labels(t.name)
        g.inc()
        t0 = time.perf_counter()
        ttft = None
        code = "200"
        done = False
        try:
            async with self.session.post(
                self._url(t), data=body, headers={"Content-Type": "application/json"}
            ) as resp:
                if resp.status != 200:
                    code = str(resp.status)
                    await resp.read()
                else:
                    async for line in resp.content:
                        if not line.startswith(b"data:"):
                            continue
                        if b"[DONE]" in line:
                            done = True
                            break
                        if ttft is None and b'"choices":[]' not in line:
                            ttft = time.perf_counter() - t0
                    if not done:
                        code = "incomplete"
        except asyncio.TimeoutError:
            code = "timeout"
        except aiohttp.ClientError:
            code = "conn_error"
        except asyncio.CancelledError:
            code = "cancelled"
            raise
        except Exception:  # noqa: BLE001
            log.exception("request failed")
            code = "exception"
        finally:
            e2e = time.perf_counter() - t0
            now = time.time()
            st.inflight -= 1
            self.inflight_total -= 1
            g.dec()
            M_REQS.labels(code).inc()
            ok = code == "200" and ttft is not None
            if ok:
                st.ok += 1
                st.ttft.record(ttft * 1000.0, now)
                st.e2e.record(e2e * 1000.0, now)
                M_TTFT.labels(t.name, t.node).observe(ttft)
                M_E2E.labels(t.name, t.node).observe(e2e)
            else:
                st.err += 1
            st.tick(now, ok)

    def _launch(self, rng: random.Random) -> None:
        targets = self.targets
        if not targets:
            return
        if self.inflight_total >= self.max_inflight:
            M_REQS.labels("dropped").inc()
            return
        t = targets[self.rr % len(targets)]
        self.rr += 1
        task = asyncio.create_task(self.one_request(t, self._body(rng)))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def arrival_loop(self) -> None:
        loop = asyncio.get_running_loop()
        rng = random.Random()
        while True:
            rate = self.rate_local
            if rate <= 0 or not self.targets:
                await asyncio.sleep(0.05)
                continue
            gen, replicas = self.rate_gen, self.replicas
            next_t = loop.time()
            # Inner loop runs until the rate or replica count changes.
            while gen == self.rate_gen and replicas == self.replicas and self.targets:
                next_t += rng.expovariate(rate)
                delay = next_t - loop.time()
                changed = False
                while delay > 0:
                    await asyncio.sleep(min(delay, 0.1))
                    if gen != self.rate_gen or replicas != self.replicas:
                        changed = True
                        break
                    delay = next_t - loop.time()
                if changed:
                    break
                if delay < -1.0:  # event loop fell >1 s behind: drop backlog
                    next_t = loop.time()
                self._launch(rng)

    # ----------------------------------------------------------------- stats
    def build_stats(self) -> dict:
        now = time.time()
        live = {t.name for t in self.targets}
        per_pod = {}
        for name in list(self.stats):
            st = self.stats[name]
            ok_w, err_w = st.window_counts(now)
            if name not in live and st.inflight == 0 and ok_w == 0 and err_w == 0:
                del self.stats[name]  # gone and aged out of the window
                continue
            ttft_h = st.ttft.snapshot(now)
            e2e_h = st.e2e.snapshot(now)
            per_pod[name] = {
                "node": st.node,
                "ready": name in live,
                "ttft_hist": ttft_h,
                "e2e_hist": e2e_h,
                "inflight": st.inflight,
                "ok": st.ok,
                "err": st.err,
                "ok_window": ok_w,
                "err_window": err_w,
                "ttft_p50_ms": percentile(ttft_h, 50, BUCKET_MS),
                "ttft_p99_ms": percentile(ttft_h, 99, BUCKET_MS),
                "e2e_p99_ms": percentile(e2e_h, 99, BUCKET_MS),
            }
        return {
            "replica": self.replica,
            "rate": self.rate_total,
            "rate_total": self.rate_total,
            "rate_local": self.rate_local,
            "replicas": self.replicas,
            "targets": len(self.targets),
            "inflight": self.inflight_total,
            "per_pod": per_pod,
            "window_s": WINDOW_S,
            "bucket_ms": BUCKET_MS,
            "max_ms": MAX_MS,
            "time": now,
        }

    # ------------------------------------------------------------------ http
    async def h_stats(self, _req: web.Request) -> web.Response:
        return web.json_response(self.build_stats())

    async def h_rate(self, req: web.Request) -> web.Response:
        try:
            if req.method == "POST":
                body = await req.json()
                if "rate" in body:
                    self.set_rate(float(body["rate"]))
            elif "rate" in req.query:
                self.set_rate(float(req.query["rate"]))
        except (ValueError, TypeError, json.JSONDecodeError) as e:
            return web.json_response({"error": str(e)}, status=400)
        return web.json_response(
            {"rate": self.rate_total, "rate_total": self.rate_total,
             "rate_local": self.rate_local, "replicas": self.replicas}
        )

    async def h_metrics(self, _req: web.Request) -> web.Response:
        return web.Response(body=generate_latest(), headers={"Content-Type": CONTENT_TYPE_LATEST})

    async def h_health(self, _req: web.Request) -> web.Response:
        return web.json_response({"ok": True, "discovery_ok": self.discovery_ok})

    def app(self) -> web.Application:
        app = web.Application()
        app.add_routes(
            [
                web.get("/stats", self.h_stats),
                web.post("/rate", self.h_rate),
                web.get("/rate", self.h_rate),
                web.get("/metrics", self.h_metrics),
                web.get("/healthz", self.h_health),
            ]
        )
        app.on_startup.append(self._on_startup)
        app.on_cleanup.append(self._on_cleanup)
        return app

    async def _on_startup(self, app: web.Application) -> None:
        self.session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(limit=0, ttl_dns_cache=300, keepalive_timeout=60),
            timeout=aiohttp.ClientTimeout(total=self.req_timeout, sock_connect=5),
        )
        app["bg"] = [
            asyncio.create_task(self.discovery_loop()),
            asyncio.create_task(self.arrival_loop()),
        ]
        log.info(
            "loadgen %s up: vllm=%s/%s port=%d model=%s initial_rate=%.2f",
            self.replica, self.vllm_ns, self.vllm_label, self.vllm_port, self.model, self.rate_total,
        )

    async def _on_cleanup(self, app: web.Application) -> None:
        for t in app["bg"]:
            t.cancel()
        for t in list(self.tasks):
            t.cancel()
        await asyncio.gather(*app["bg"], *self.tasks, return_exceptions=True)
        if self.session:
            await self.session.close()


def main() -> None:
    logging.basicConfig(level=env("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    try:
        import uvloop  # type: ignore

        loop = uvloop.new_event_loop()
    except ImportError:
        loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    lg = LoadGen()
    web.run_app(
        lg.app(), port=int(env("PORT", "8080")), access_log=None, shutdown_timeout=3, loop=loop
    )


if __name__ == "__main__":
    main()
