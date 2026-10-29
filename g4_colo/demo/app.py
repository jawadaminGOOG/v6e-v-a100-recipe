"""Colo-at-scale demo app (FastAPI). Run from this directory:

    python -m uvicorn app:app --port 8088            # live cluster (local kubeconfig)
    DEMO_MOCK=1 python -m uvicorn app:app --port 8088  # synthetic state, no cluster
"""

from __future__ import annotations

import asyncio
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
app = FastAPI(title="G4 colo at scale demo")
app.mount("/static", StaticFiles(directory=STATIC), name="static")

backend = get_backend()
runner = ScenarioRunner(backend, time_scale=float(os.environ.get("DEMO_SCENARIO_TIME_SCALE", "1")))

# Coalesce concurrent /api/state polls (several tabs + scenario runner) to one fetch per second.
_cache: dict = {"t": 0.0, "state": None}
_lock = asyncio.Lock()


async def current_state() -> dict:
    async with _lock:
        if _cache["state"] is None or time.time() - _cache["t"] > 1.0:
            _cache["state"] = await backend.state()
            _cache["t"] = time.time()
        return _cache["state"]


def _invalidate():
    _cache["t"] = 0.0


async def _action(coro, what: str):
    try:
        res = await coro
    except Exception as e:  # noqa: BLE001
        log.exception("%s failed", what)
        raise HTTPException(status_code=502, detail=f"{what} failed: {type(e).__name__}: {e}") from e
    _invalidate()
    return {"ok": True, "result": res}


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/state")
async def api_state():
    try:
        st = await current_state()
    except Exception as e:  # noqa: BLE001
        log.exception("state failed")
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=502)
    return {**st, "scenario": runner.snapshot()}


@app.post("/api/gpu-rate")
async def api_gpu_rate(body: dict = Body(...)):
    rate = float(body.get("rate", 0))
    if not 0 <= rate <= 5000:
        raise HTTPException(400, "rate out of range")
    return await _action(backend.set_gpu_rate(rate), "gpu-rate")


@app.post("/api/demand")
async def api_demand(body: dict = Body(...)):
    d = {k: int(body[k]) for k in ("sandbox", "toolcall", "analytics") if k in body}
    if not d or any(v < 0 or v > 200 for v in d.values()):
        raise HTTPException(400, "demand must contain sandbox/toolcall/analytics in 0..200")
    return await _action(backend.set_demand(d), "demand")


@app.post("/api/guard")
async def api_guard(body: dict = Body(...)):
    return await _action(backend.set_guard(bool(body.get("enabled"))), "guard")


@app.post("/api/baseline")
async def api_baseline():
    return await _action(backend.baseline(), "baseline")


@app.get("/api/scenarios")
async def api_scenarios():
    return {k: {"title": v["title"], "steps": v["steps"]} for k, v in SCENARIOS.items()}


@app.get("/api/scenario/status")
async def api_scenario_status():
    return runner.snapshot()


@app.post("/api/scenario/cancel")
async def api_scenario_cancel():
    return {"cancelled": runner.cancel()}


@app.post("/api/scenario/{name}")
async def api_scenario(name: str):
    try:
        resolve_name(name)
        return runner.start(name)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from e
