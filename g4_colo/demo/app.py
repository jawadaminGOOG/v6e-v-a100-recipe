"""Colo-at-scale demo app (FastAPI). Run from this directory:

    python -m uvicorn app:app --port 8088            # live cluster (local kubeconfig)
    DEMO_MOCK=1 python -m uvicorn app:app --port 8088  # synthetic state, no cluster (both recipes)
    DEMO_CLUSTERS='[...]' python -m uvicorn app:app --port 8088  # several clusters (see backend.py)

Every /api endpoint takes ?cluster=<id> (default: the first cluster); GET /api/clusters lists them.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
import time
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

sys.path.insert(0, str(Path(__file__).resolve().parent))

from backend import cluster_info, get_backends, get_cluster_specs  # noqa: E402
from scenarios import SCENARIOS, UNAVAILABLE_REASON, ScenarioRunner, resolve_name, scenarios_for  # noqa: E402
from state import GUARD_NA_REASON, UnsupportedOnRecipe  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("demo")

STATIC = Path(__file__).resolve().parent / "static"
ERROR_LOG_INTERVAL_S = float(os.environ.get("DEMO_ERROR_LOG_INTERVAL_S", "30"))
TIME_SCALE = float(os.environ.get("DEMO_SCENARIO_TIME_SCALE", "1"))

_err_logged: dict[str, float] = {}


def _log_backend_errors(errors: list[str], cluster_id: str | None = None) -> None:
    """WARNING-log each distinct backend error at most once per ERROR_LOG_INTERVAL_S.

    Lets an unattended soak (Cloud Run) be checked from Cloud Logging without opening the UI.
    """
    now = time.time()
    for e in errors or []:
        key = f"{cluster_id or ''}|{e[:160]}"
        if now - _err_logged.get(key, 0.0) >= ERROR_LOG_INTERVAL_S:
            _err_logged[key] = now
            if cluster_id:
                log.warning("backend error [%s]: %s", cluster_id, e[:500])
            else:
                log.warning("backend error: %s", e[:500])
    if len(_err_logged) > 500:  # bound memory if messages carry unique ids
        for k, t in sorted(_err_logged.items(), key=lambda kv: kv[1])[:250]:
            _err_logged.pop(k, None)


class ClusterCtx:
    """Everything that is per cluster: backend, 1 s state cache, and its own scenario runner (a scenario on
    one cluster never blocks controls or scenarios on the other)."""

    def __init__(self, spec: dict, backend):
        self.id = spec["id"]
        self.spec = spec
        self.info = cluster_info(spec)
        self.recipe = spec["recipe"]
        self.backend = backend
        self.scenarios = scenarios_for(self.recipe)
        # Coalesce concurrent /api/state polls (several tabs + scenario runner) to one fetch per second.
        self.cache: dict = {"t": 0.0, "state": None}
        self.lock = asyncio.Lock()
        self.runner = ScenarioRunner(
            backend,
            time_scale=TIME_SCALE,
            state_fn=self.current_state,
            on_action=self.invalidate,
            on_finish=self.restore_accelerator,
            scenarios=self.scenarios,
        )

    async def current_state(self) -> dict:
        async with self.lock:
            if self.cache["state"] is None or time.time() - self.cache["t"] > 1.0:
                st = await self.backend.state()
                _log_backend_errors(st.get("errors") or [], self.id if len(CTXS) > 1 else None)
                self.cache["state"] = st
                self.cache["t"] = time.time()
            return self.cache["state"]

    def invalidate(self):
        self.cache["t"] = 0.0

    async def restore_accelerator(self):
        restore = getattr(self.backend, "restore_hpa_min", None)
        if restore is not None:
            await restore()
            self.invalidate()


_backends = get_backends()
CTXS: dict[str, ClusterCtx] = {s["id"]: ClusterCtx(s, _backends[s["id"]]) for s in get_cluster_specs()}
DEFAULT_CLUSTER = next(iter(CTXS))

# Back-compat aliases (tests, single-cluster callers): the default cluster's backend, runner and cache.
backend = CTXS[DEFAULT_CLUSTER].backend
runner = CTXS[DEFAULT_CLUSTER].runner


def _ctx(cluster: str | None) -> ClusterCtx:
    cid = cluster or DEFAULT_CLUSTER
    ctx = CTXS.get(cid)
    if ctx is None:
        raise HTTPException(404, f"unknown cluster {cid!r}; choose from {list(CTXS)}")
    return ctx


async def current_state(cluster: str | None = None) -> dict:
    return await _ctx(cluster).current_state()


def _invalidate(cluster: str | None = None):
    _ctx(cluster).invalidate()


async def _restore_accelerator(cluster: str | None = None):
    await _ctx(cluster).restore_accelerator()


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    # Optional heartbeat (DEMO_HEARTBEAT_S > 0): fetch state on a timer so a hosted instance with no
    # open browser still exercises GKE auth and token refresh, and logs one line per beat for soak checks.
    beat_s = float(os.environ.get("DEMO_HEARTBEAT_S", "0"))
    beat_task = None
    if beat_s > 0:
        async def _beat():
            while True:
                for ctx in CTXS.values():
                    try:
                        st = await ctx.current_state()
                        errs = st.get("errors") or []
                        auth = sum(1 for e in errs if "401" in e or "403" in e or "Forbidden" in e or "Unauthorized" in e)
                        log.info("heartbeat ok cluster=%s nodes=%d errors=%d auth_errors=%d",
                                 ctx.id, len(st.get("nodes") or []), len(errs), auth)
                    except Exception as e:  # noqa: BLE001
                        log.warning("heartbeat failed cluster=%s: %s: %s", ctx.id, type(e).__name__, e)
                await asyncio.sleep(beat_s)
        beat_task = asyncio.create_task(_beat())
    yield
    if beat_task:
        beat_task.cancel()
    # Never leave HPA minReplicas forced to 2 (a G4 node at ~$2/hr) behind a stopped instance.
    for ctx in CTXS.values():
        ctx.runner.cancel()
    for ctx in CTXS.values():
        try:
            await asyncio.wait_for(ctx.restore_accelerator(), timeout=8)
        except Exception:  # noqa: BLE001
            log.exception("HPA restore on shutdown failed (cluster %s)", ctx.id)


app = FastAPI(title="G4 colo at scale demo", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


async def _action(ctx: ClusterCtx, coro, what: str):
    try:
        res = await coro
    except UnsupportedOnRecipe as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except Exception as e:  # noqa: BLE001
        log.exception("%s failed (cluster %s)", what, ctx.id)
        raise HTTPException(status_code=502, detail=f"{what} failed: {type(e).__name__}: {e}") from e
    ctx.invalidate()
    return {"ok": True, "result": res}


def _manual_allowed(ctx: ClusterCtx):
    if ctx.runner.running:
        raise HTTPException(409, f"scenario {ctx.runner.name} is running; stop it before changing controls")


def _guard_allowed(ctx: ClusterCtx, what: str):
    if ctx.recipe == "native":
        raise HTTPException(409, f"{what} is not available on cluster {ctx.id!r} (native recipe): {GUARD_NA_REASON}")


def _demo_info(ctx: ClusterCtx, st: dict) -> dict:
    hpa = (st.get("vllm") or {}).get("hpa") or {}
    return {
        "fast_scale": bool(getattr(ctx.backend, "fast_scale", False)),
        # Read from the live HPA so a value left by another instance (or a crashed one) also shows.
        # The recipe ships minReplicas=1, so anything above 1 was forced by the demo accelerator.
        "accelerator_active": (hpa.get("min") or 1) > 1,
        "hpa_min": hpa.get("min"),
        "scenarios_available": list(ctx.scenarios),
        "scenarios_unavailable": {n: UNAVAILABLE_REASON.get((ctx.recipe, n), "not available on this cluster")
                                  for n in SCENARIOS if n not in ctx.scenarios},
    }


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/clusters")
async def api_clusters():
    return {
        "default": DEFAULT_CLUSTER,
        "clusters": [{**c.info, "control": "HPA + scheduler" if c.recipe == "native" else "colo-controller",
                      "guard_available": c.recipe != "native",
                      "scenario_running": c.runner.running} for c in CTXS.values()],
    }


@app.get("/api/state")
async def api_state(cluster: str | None = Query(None)):
    ctx = _ctx(cluster)
    try:
        st = await ctx.current_state()
    except Exception as e:  # noqa: BLE001
        log.exception("state failed (cluster %s)", ctx.id)
        return JSONResponse({"error": f"{type(e).__name__}: {e}", "kind": "backend", "cluster_id": ctx.id}, status_code=502)
    return {**st, "cluster_id": ctx.id, "scenario": ctx.runner.snapshot(), "demo": _demo_info(ctx, st)}


@app.post("/api/gpu-rate")
async def api_gpu_rate(body: dict = Body(...), cluster: str | None = Query(None)):
    ctx = _ctx(cluster)
    _manual_allowed(ctx)
    rate = float(body.get("rate", 0))
    if not 0 <= rate <= 5000:
        raise HTTPException(400, "rate out of range")
    return await _action(ctx, ctx.backend.set_gpu_rate(rate), "gpu-rate")


@app.post("/api/demand")
async def api_demand(body: dict = Body(...), cluster: str | None = Query(None)):
    ctx = _ctx(cluster)
    _manual_allowed(ctx)
    d = {k: int(body[k]) for k in ("sandbox", "toolcall", "analytics") if k in body}
    if not d or any(v < 0 or v > 200 for v in d.values()):
        raise HTTPException(400, "demand must contain sandbox/toolcall/analytics in 0..200")
    return await _action(ctx, ctx.backend.set_demand(d), "demand")


@app.post("/api/guard")
async def api_guard(body: dict = Body(...), cluster: str | None = Query(None)):
    ctx = _ctx(cluster)
    _guard_allowed(ctx, "the latency guard")
    _manual_allowed(ctx)
    return await _action(ctx, ctx.backend.set_guard(bool(body.get("enabled"))), "guard")


@app.post("/api/baseline")
async def api_baseline(cluster: str | None = Query(None)):
    ctx = _ctx(cluster)
    _guard_allowed(ctx, "the TTFT baseline")
    _manual_allowed(ctx)
    return await _action(ctx, ctx.backend.baseline(), "baseline")


@app.get("/api/scenarios")
async def api_scenarios(cluster: str | None = Query(None)):
    ctx = _ctx(cluster)
    return {k: {"title": v["title"], "steps": v["steps"]} for k, v in ctx.scenarios.items()}


@app.get("/api/scenario/status")
async def api_scenario_status(cluster: str | None = Query(None)):
    return _ctx(cluster).runner.snapshot()


@app.post("/api/scenario/cancel")
async def api_scenario_cancel(cluster: str | None = Query(None)):
    ctx = _ctx(cluster)
    cancelled = ctx.runner.cancel()
    if not cancelled:  # nothing running; still make sure the accelerator is off
        await ctx.restore_accelerator()
    return {"cancelled": cancelled}


@app.post("/api/scenario/{name}")
async def api_scenario(name: str, cluster: str | None = Query(None)):
    ctx = _ctx(cluster)
    try:
        resolve_name(name, ctx.scenarios)
    except KeyError as e:
        try:  # a real scenario that this cluster's recipe does not have -> 409 with the reason
            full = resolve_name(name)
        except KeyError:
            raise HTTPException(404, str(e)) from e
        raise HTTPException(409, UNAVAILABLE_REASON.get((ctx.recipe, full), f"{full} is not available on cluster {ctx.id!r}")) from e
    try:
        return ctx.runner.start(name)
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from e
