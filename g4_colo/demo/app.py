"""Colo-at-scale demo app (FastAPI). Run from this directory:

    python -m uvicorn app:app --port 8088            # live cluster (local kubeconfig)
    DEMO_MOCK=1 python -m uvicorn app:app --port 8088  # synthetic state, no cluster
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
import time
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

sys.path.insert(0, str(Path(__file__).resolve().parent))

from backend import get_backend  # noqa: E402
from scenarios import SCENARIOS, ScenarioRunner, resolve_name  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("demo")

STATIC = Path(__file__).resolve().parent / "static"
ERROR_LOG_INTERVAL_S = float(os.environ.get("DEMO_ERROR_LOG_INTERVAL_S", "30"))

backend = get_backend()

# Coalesce concurrent /api/state polls (several tabs + scenario runner) to one fetch per second.
_cache: dict = {"t": 0.0, "state": None}
_lock = asyncio.Lock()
_err_logged: dict[str, float] = {}


def _log_backend_errors(errors: list[str]) -> None:
    """WARNING-log each distinct backend error at most once per ERROR_LOG_INTERVAL_S.

    Lets an unattended soak (Cloud Run) be checked from Cloud Logging without opening the UI.
    """
    now = time.time()
    for e in errors or []:
        key = e[:160]
        if now - _err_logged.get(key, 0.0) >= ERROR_LOG_INTERVAL_S:
            _err_logged[key] = now
            log.warning("backend error: %s", e[:500])
    if len(_err_logged) > 500:  # bound memory if messages carry unique ids
        for k, t in sorted(_err_logged.items(), key=lambda kv: kv[1])[:250]:
            _err_logged.pop(k, None)


async def current_state() -> dict:
    async with _lock:
        if _cache["state"] is None or time.time() - _cache["t"] > 1.0:
            st = await backend.state()
            _log_backend_errors(st.get("errors") or [])
            _cache["state"] = st
            _cache["t"] = time.time()
        return _cache["state"]


def _invalidate():
    _cache["t"] = 0.0


async def _restore_accelerator():
    restore = getattr(backend, "restore_hpa_min", None)
    if restore is not None:
        await restore()
        _invalidate()


runner = ScenarioRunner(
    backend,
    time_scale=float(os.environ.get("DEMO_SCENARIO_TIME_SCALE", "1")),
    state_fn=current_state,
    on_action=_invalidate,
    on_finish=_restore_accelerator,
)


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    # Optional heartbeat (DEMO_HEARTBEAT_S > 0): fetch state on a timer so a hosted instance with no
    # open browser still exercises GKE auth and token refresh, and logs one line per beat for soak checks.
    beat_s = float(os.environ.get("DEMO_HEARTBEAT_S", "0"))
    beat_task = None
    if beat_s > 0:
        async def _beat():
            while True:
                try:
                    st = await current_state()
                    errs = st.get("errors") or []
                    auth = sum(1 for e in errs if "401" in e or "403" in e or "Forbidden" in e or "Unauthorized" in e)
                    log.info("heartbeat ok nodes=%d errors=%d auth_errors=%d",
                             len(st.get("nodes") or []), len(errs), auth)
                except Exception as e:  # noqa: BLE001
                    log.warning("heartbeat failed: %s: %s", type(e).__name__, e)
                await asyncio.sleep(beat_s)
        beat_task = asyncio.create_task(_beat())
    yield
    if beat_task:
        beat_task.cancel()
    # Never leave HPA minReplicas forced to 2 (a G4 node at ~$2/hr) behind a stopped instance.
    runner.cancel()
    try:
        await asyncio.wait_for(_restore_accelerator(), timeout=8)
    except Exception:  # noqa: BLE001
        log.exception("HPA restore on shutdown failed")


app = FastAPI(title="G4 colo at scale demo", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


async def _action(coro, what: str):
    try:
        res = await coro
    except Exception as e:  # noqa: BLE001
        log.exception("%s failed", what)
        raise HTTPException(status_code=502, detail=f"{what} failed: {type(e).__name__}: {e}") from e
    _invalidate()
    return {"ok": True, "result": res}


def _manual_allowed():
    if runner.running:
        raise HTTPException(409, f"scenario {runner.name} is running; stop it before changing controls")


def _demo_info(st: dict) -> dict:
    hpa = (st.get("vllm") or {}).get("hpa") or {}
    return {
        "fast_scale": bool(getattr(backend, "fast_scale", False)),
        # Read from the live HPA so a value left by another instance (or a crashed one) also shows.
        # The recipe ships minReplicas=1, so anything above 1 was forced by the demo accelerator.
        "accelerator_active": (hpa.get("min") or 1) > 1,
        "hpa_min": hpa.get("min"),
    }


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/state")
async def api_state():
    try:
        st = await current_state()
    except Exception as e:  # noqa: BLE001
        log.exception("state failed")
        return JSONResponse({"error": f"{type(e).__name__}: {e}", "kind": "backend"}, status_code=502)
    return {**st, "scenario": runner.snapshot(), "demo": _demo_info(st)}


@app.post("/api/gpu-rate")
async def api_gpu_rate(body: dict = Body(...)):
    _manual_allowed()
    rate = float(body.get("rate", 0))
    if not 0 <= rate <= 5000:
        raise HTTPException(400, "rate out of range")
    return await _action(backend.set_gpu_rate(rate), "gpu-rate")


@app.post("/api/demand")
async def api_demand(body: dict = Body(...)):
    _manual_allowed()
    d = {k: int(body[k]) for k in ("sandbox", "toolcall", "analytics") if k in body}
    if not d or any(v < 0 or v > 200 for v in d.values()):
        raise HTTPException(400, "demand must contain sandbox/toolcall/analytics in 0..200")
    return await _action(backend.set_demand(d), "demand")


@app.post("/api/guard")
async def api_guard(body: dict = Body(...)):
    _manual_allowed()
    return await _action(backend.set_guard(bool(body.get("enabled"))), "guard")


@app.post("/api/baseline")
async def api_baseline():
    _manual_allowed()
    return await _action(backend.baseline(), "baseline")


@app.get("/api/scenarios")
async def api_scenarios():
    return {k: {"title": v["title"], "steps": v["steps"]} for k, v in SCENARIOS.items()}


@app.get("/api/scenario/status")
async def api_scenario_status():
    return runner.snapshot()


@app.post("/api/scenario/cancel")
async def api_scenario_cancel():
    cancelled = runner.cancel()
    if not cancelled:  # nothing running; still make sure the accelerator is off
        await _restore_accelerator()
    return {"cancelled": cancelled}


@app.post("/api/scenario/{name}")
async def api_scenario(name: str):
    try:
        resolve_name(name)
        return runner.start(name)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from e
