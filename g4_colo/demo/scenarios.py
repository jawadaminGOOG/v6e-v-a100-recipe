"""Demo scenarios as data + a small async runner.

Each scenario is a list of steps (plain dicts) so the web app, run_scenario.py
and any other harness (e.g. recipe/tests/run_scenario.py) can share them.

Step kinds:
  {"do": "require", "all": [["g4_nodes", "<=", 1]], "msg": "run scenario 2 first"}
       # precondition: checked once, fails the scenario at once with `msg` if not met
  {"do": "gpu_rate", "rate": 300}
  {"do": "demand", "sandbox": 4, "toolcall": 4, "analytics": 2}
  {"do": "guard", "enabled": true}
  {"do": "baseline"}
  {"do": "capture", "name": "n4d0", "metric": "n4d_nodes"}      # save a metric value
  {"do": "wait", "s": 60}
  {"do": "wait_until", "all": [["vllm_ready", ">=", 2]], "timeout_s": 900}
       # rhs may be a number or "$name" (captured) or "$name+1"
  {"do": "observe", "label": "steady"}                          # record a snapshot
Every step may carry a "note" shown in the step log. Notes are templates filled
from live metrics when the step runs: "{threshold_ms:.0f} ms" -> "27 ms".
Metric names are the keys of state.summarize().
"""

from __future__ import annotations

import asyncio
import logging
import operator
import re
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from state import summarize

log = logging.getLogger("demo.scenarios")

_CTRL = ["controller_reachable", "==", True]

SCENARIOS: dict[str, dict[str, Any]] = {
    "gpu-scale-out": {
        "title": "GPU scale-out: a load spike adds a 2nd G4 node, and its spare vCPUs fill with CPU services",
        "steps": [
            {"do": "require", "all": [_CTRL, ["g4_nodes", "<=", 1], ["vllm_desired", "<=", 1]],
             "msg": "Needs 1 G4 node running 1 vLLM replica. The cluster is already scaled out: run scenario 2 (GPU scale-in) first."},
            {"do": "guard", "enabled": True, "note": "latency guard on (it only blocks memory-heavy work, never integer)"},
            {"do": "demand", "sandbox": 19, "toolcall": 19, "analytics": 0,
             "note": "38 integer pods (76 vCPU); one G4 node has {g4_slots_per_node} spare 2-vCPU slots, the rest wait on N4D"},
            {"do": "observe", "label": "before"},
            {"do": "gpu_rate", "rate": 1200, "note": "spike GPU load to 1200 rps (above the 700 rps per replica HPA target)"},
            {"do": "wait_until", "all": [["vllm_desired", ">=", 2]], "timeout_s": 180, "note": "vLLM scales 1 -> 2 replicas"},
            {"do": "wait_until", "all": [["g4_nodes", ">=", 2]], "timeout_s": 360, "note": "GKE ComputeClass provisions a 2nd G4 node"},
            {"do": "wait_until", "all": [["backfill_ready", ">=", 34]], "timeout_s": 240,
             "note": "CPU pods move from N4D onto the new G4 node's spare cores"},
            {"do": "wait_until", "all": [["vllm_ready", ">=", 2]], "timeout_s": 600, "note": "2nd vLLM replica Ready and serving"},
            {"do": "observe", "label": "after"},
        ],
    },
    "gpu-scale-in": {
        "title": "GPU scale-in: load drops, the 2nd G4 node is released, and its CPU pods move to warm N4D without losing capacity",
        "steps": [
            {"do": "require", "all": [_CTRL, ["vllm_ready", ">=", 2], ["g4_nodes", ">=", 2]],
             "msg": "Needs 2 vLLM replicas on 2 G4 nodes. Run scenario 1 (GPU scale-out) first."},
            {"do": "demand", "sandbox": 19, "toolcall": 19, "analytics": 0, "note": "keep 38 integer pods (76 vCPU) active across scale-in"},
            {"do": "observe", "label": "before"},
            {"do": "gpu_rate", "rate": 300, "note": "drop GPU load to 300 rps (fits on 1 vLLM replica)"},
            {"do": "wait_until", "all": [["vllm_desired", "<=", 1]], "timeout_s": 60, "note": "vLLM scales 2 -> 1"},
            {"do": "wait_until", "all": [["g4_nodes", "<=", 1]], "timeout_s": 600,
             "note": "cluster autoscaler drains the 2nd G4 node; its CPU pods shift to warm N4D"},
            {"do": "wait_until", "all": [["backfill_ready", ">=", 18], ["overflow_ready", ">=", 18]], "timeout_s": 120,
             "note": "G4 #1 stays packed ({g4_slots_per_node} slots) and the rest run on N4D"},
            {"do": "observe", "label": "after"},
        ],
    },
    "cpu-scale-out": {
        "title": "CPU scale-out: G4 stays packed while extra CPU demand spills to N4D in seconds, never adding a GPU node",
        "steps": [
            {"do": "require", "all": [_CTRL, ["g4_ready", ">=", 1], ["g4_nodes", "<=", 1]],
             "msg": "Needs exactly 1 Ready G4 node. Run scenario 2 (GPU scale-in) first, or wait for the 2nd G4 node to drain."},
            {"do": "gpu_rate", "rate": 300, "note": "steady 300 rps on 1 G4 node"},
            {"do": "demand", "sandbox": 9, "toolcall": 9, "analytics": 0,
             "note": "18 integer pods (36 vCPU) against {g4_slots_per_node} spare slots on G4 #1"},
            {"do": "wait_until", "all": [["overflow_desired", "==", 0], ["backfill_ready", ">=", 18]], "timeout_s": 120,
             "note": "all pods run on G4 spare cores (0 on N4D)"},
            {"do": "observe", "label": "fits-in-backfill"},
            {"do": "demand", "sandbox": 18, "toolcall": 18, "analytics": 0, "note": "spike CPU demand to 36 pods (72 vCPU)"},
            {"do": "wait_until", "all": [["overflow_desired", ">=", 17]], "timeout_s": 30,
             "note": "controller keeps G4 packed and assigns the excess to N4D"},
            {"do": "wait_until", "all": [["overflow_ready", ">=", 17]], "timeout_s": 180,
             "note": "overflow pods Ready on N4D; G4 node count unchanged"},
            {"do": "observe", "label": "spilled"},
            {"do": "demand", "sandbox": 9, "toolcall": 9, "analytics": 0, "note": "drop demand back to 18 pods (overflow shrinks first)"},
            {"do": "wait_until", "all": [["overflow_desired", "==", 0], ["backfill_ready", ">=", 18]], "timeout_s": 60,
             "note": "N4D overflow back to 0; G4 #1 keeps its pods"},
            {"do": "observe", "label": "after"},
        ],
    },
    "guard-integer-vs-memory": {
        "title": "Latency guard: integer work is harmless; memory-heavy analytics trips the guard and is moved to N4D",
        "steps": [
            {"do": "require", "all": [_CTRL, ["baseline_ms", ">", 0]],
             "msg": "No TTFT baseline yet. Open Manual controls and press 'Measure baseline' at steady load first."},
            {"do": "guard", "enabled": False, "note": "clear any prior node guard blocks"},
            {"do": "gpu_rate", "rate": 400, "note": "steady 400 rps on vLLM"},
            {"do": "demand", "sandbox": 10, "toolcall": 9, "analytics": 0, "note": "pack G4 with 19 integer pods (38 vCPU)"},
            {"do": "wait_until", "all": [["integer_backfill_ready", ">=", 18]], "timeout_s": 120, "note": "integer pods running on G4 spare cores"},
            {"do": "guard", "enabled": True,
             "note": "enable latency guard (threshold {threshold_ms:.0f} ms = baseline {baseline_ms:.0f} ms x factor)"},
            {"do": "wait", "s": 12, "note": "hold integer load: TTFT p99 {ttft_p99_ms:.0f} ms vs {threshold_ms:.0f} ms threshold, no evictions"},
            {"do": "observe", "label": "integer-harmless"},
            {"do": "capture", "name": "ev0", "metric": "guard_evict_events"},
            {"do": "demand", "sandbox": 0, "toolcall": 0, "analytics": 16, "note": "switch G4 backfill to 16 memory-heavy analytics pods"},
            {"do": "wait_until", "any": [["guard_evict_events", ">", "$ev0"], ["guard_blocks", ">=", 1]], "timeout_s": 120,
             "note": "memory-bus contention pushes TTFT p99 above {threshold_ms:.0f} ms -> guard evicts"},
            {"do": "observe", "label": "guard-fired"},
            {"do": "demand", "sandbox": 10, "toolcall": 9, "analytics": 16,
             "note": "refill G4 spare cores with integer pods while analytics runs on N4D"},
            {"do": "wait_until", "all": [["integer_backfill_ready", ">=", 18], ["analytics_overflow_ready", ">=", 12]], "timeout_s": 180,
             "note": "G4 packed with integer work, analytics isolated on N4D"},
            {"do": "observe", "label": "after"},
        ],
    },
}
SCENARIO_ALIASES = {"1": "gpu-scale-out", "2": "gpu-scale-in", "3": "cpu-scale-out", "4": "guard-integer-vs-memory"}

OPS = {">=": operator.ge, ">": operator.gt, "<=": operator.le, "<": operator.lt, "==": operator.eq, "!=": operator.ne}

# Plain-language names for the scenario banner ("current vs target").
METRIC_LABELS = {
    "vllm_desired": "vLLM replicas wanted",
    "vllm_ready": "vLLM replicas ready",
    "g4_nodes": "G4 nodes",
    "g4_ready": "G4 nodes ready",
    "backfill_ready": "CPU pods on G4",
    "overflow_ready": "CPU pods on N4D",
    "overflow_desired": "CPU pods assigned to N4D",
    "integer_backfill_ready": "integer pods on G4",
    "analytics_overflow_ready": "analytics pods on N4D",
    "guard_evict_events": "guard evictions",
    "guard_blocks": "guard blocks",
    "controller_reachable": "controller reachable",
    "baseline_ms": "TTFT baseline (ms)",
}

_TPL = re.compile(r"\{(\w+)(?::([^}]*))?\}")


class PreconditionError(RuntimeError):
    pass


def resolve_name(name: str) -> str:
    name = SCENARIO_ALIASES.get(name, name)
    if name not in SCENARIOS:
        raise KeyError(f"unknown scenario {name!r}; choose from {list(SCENARIOS)}")
    return name


def render_note(note: str | None, metrics: dict) -> str:
    """Fill {metric[:fmt]} placeholders from live metrics; unknown or missing values become '?'."""
    if not note:
        return ""

    def sub(mo: re.Match) -> str:
        v = metrics.get(mo.group(1))
        if v is None:
            return "?"
        try:
            return format(v, mo.group(2) or "")
        except (TypeError, ValueError):
            return str(v)

    return _TPL.sub(sub, note)


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, tz=timezone.utc).isoformat(timespec="seconds")


class ScenarioRunner:
    """Runs one scenario at a time against a backend; keeps a step log.

    state_fn: coroutine returning the /api/state document. The web app passes its 1 s cache so a
        running scenario adds no apiserver load; the CLI falls back to backend.state().
    on_action: called after every mutating step (the app uses it to invalidate its cache).
    on_finish: awaited when a scenario ends for any reason (the app restores HPA minReplicas).
    """

    def __init__(self, backend, poll_s: float = 2.0, time_scale: float = 1.0,
                 state_fn: Callable[[], Awaitable[dict]] | None = None,
                 on_action: Callable[[], None] | None = None,
                 on_finish: Callable[[], Awaitable[Any]] | None = None):
        self.backend = backend
        self.poll_s = poll_s
        self.time_scale = time_scale
        self.state_fn = state_fn or backend.state
        self.on_action = on_action
        self.on_finish = on_finish
        self.task: asyncio.Task | None = None
        self.reset(None)

    def reset(self, name: str | None):
        self._rate_before = None
        self.name = name
        self.status = "idle" if name is None else "running"
        self.started = time.time()
        self.finished: float | None = None
        self.step_index = -1
        self.log: list[dict] = []
        self.observations: list[dict] = []
        self.vars: dict[str, Any] = {}
        self.wait: dict | None = None
        self.error: str | None = None
        self.note: str = ""

    def snapshot(self) -> dict:
        steps = SCENARIOS.get(self.name or "", {}).get("steps", [])
        cur_step = steps[self.step_index] if 0 <= self.step_index < len(steps) else None
        wait = None
        if self.wait and self.status == "running":
            wait = {**self.wait, "remaining_s": round(max(0.0, self.wait["deadline"] - time.time()), 0)}
        return {
            "name": self.name,
            "title": SCENARIOS.get(self.name or "", {}).get("title"),
            "status": self.status,
            "started": self.started,
            "finished": self.finished,
            "elapsed_s": round((self.finished or time.time()) - self.started, 1) if self.name else 0,
            "step_index": self.step_index,
            "n_steps": len(steps),
            "current_step": cur_step,
            "current_note": self.note,
            "wait": wait,
            "error": self.error,
            "log": self.log[-200:],
            "observations": self.observations,
        }

    def _log(self, msg: str, level: str = "info", **extra):
        t = time.time()
        self.log.append({"t": t, "time": _iso(t), "elapsed_s": round(t - self.started, 1), "level": level, "msg": msg, **extra})

    async def _metrics(self) -> dict:
        return summarize(await self.state_fn())

    def _acted(self):
        if self.on_action:
            self.on_action()

    def _rhs(self, v: Any) -> Any:
        if isinstance(v, str) and v.startswith("$"):
            expr = v[1:]
            for sign in ("+", "-"):
                if sign in expr:
                    k, d = expr.split(sign, 1)
                    base = self.vars.get(k, 0) or 0
                    return base + float(d) if sign == "+" else base - float(d)
            return self.vars.get(expr)
        return v

    def _eval(self, m: dict, conds: list) -> list[dict]:
        out = []
        for metric, op, rhs in conds:
            rv = self._rhs(rhs)
            lv = m.get(metric)
            try:
                ok = lv is not None and rv is not None and OPS[op](lv, rv)
            except TypeError:
                ok = False
            out.append({"metric": metric, "label": METRIC_LABELS.get(metric, metric), "value": lv, "op": op, "target": rv, "ok": bool(ok)})
        return out

    def _check(self, m: dict, conds: list, mode: str) -> tuple[bool, list[str]]:
        ev = self._eval(m, conds)
        res = [e["ok"] for e in ev]
        desc = [f"{e['metric']}={e['value']} {e['op']} {e['target']}" for e in ev]
        return (all(res) if mode == "all" else any(res)), desc

    async def run(self, name: str) -> dict:
        name = resolve_name(name)
        self.reset(name)
        steps = SCENARIOS[name]["steps"]
        self._log(f"scenario {name}: {SCENARIOS[name]['title']}")
        try:
            for i, step in enumerate(steps):
                self.step_index = i
                await self._run_step(i, step)
            self.status = "done"
            self._log("scenario finished", "ok")
        except asyncio.CancelledError:
            self.status = "cancelled"
            self._log("scenario cancelled", "warn")
            # A cancelled scale-out must not leave the load spike running (it would still add a G4 node).
            if self._rate_before is not None:
                try:
                    await self.backend.set_gpu_rate(self._rate_before)
                    self._log(f"GPU rate restored to {self._rate_before:.0f} rps", "warn")
                except Exception:  # noqa: BLE001
                    log.exception("rate restore on cancel failed")
            raise
        except PreconditionError as e:
            self.status = "failed"
            self.error = str(e)
            self._log(f"not started: {e}", "error")
        except Exception as e:  # noqa: BLE001
            self.status = "failed"
            self.error = f"step {self.step_index + 1}: {e}"
            self._log(f"step {self.step_index + 1} failed: {type(e).__name__}: {e}", "error")
        finally:
            self.finished = time.time()
            self.wait = None
            if self.on_finish:
                try:
                    await self.on_finish()
                except Exception:  # noqa: BLE001
                    log.exception("scenario on_finish hook failed")
        return self.snapshot()

    async def _safe_metrics(self) -> dict:
        try:
            return await self._metrics()
        except Exception:  # noqa: BLE001 - note text must never fail a step
            return {}

    async def _run_step(self, i: int, step: dict):
        do = step["do"]
        self.wait = None
        b = self.backend
        prefix = f"[{i + 1}] "
        self.note = render_note(step.get("note"), await self._safe_metrics()) if step.get("note") else ""
        note = f" - {self.note}" if self.note else ""
        if do == "require":
            mode = "any" if "any" in step else "all"
            m = await self._metrics()
            ev = self._eval(m, step.get(mode) or [])
            ok = all(e["ok"] for e in ev) if mode == "all" else any(e["ok"] for e in ev)
            if not ok:
                failed = ", ".join(f"{e['label']} is {e['value']} (needs {e['op']} {e['target']})" for e in ev if not e["ok"])
                raise PreconditionError(f"{step.get('msg') or 'precondition not met'} [{failed}]")
            self._log(f"{prefix}preconditions met", "ok", step=i)
        elif do == "gpu_rate":
            if self._rate_before is None:
                self._rate_before = (await self._safe_metrics()).get("target_rps")
            await b.set_gpu_rate(step["rate"])
            self._acted()
            self._log(f"{prefix}GPU request rate -> {step['rate']} rps{note}", step=i)
        elif do == "demand":
            d = {k: step[k] for k in ("sandbox", "toolcall", "analytics") if k in step}
            await b.set_demand(d)
            self._acted()
            self._log(f"{prefix}demand -> {d}{note}", step=i)
        elif do == "guard":
            await b.set_guard(step["enabled"])
            self._acted()
            self._log(f"{prefix}guard -> {'on' if step['enabled'] else 'off'}{note}", step=i)
        elif do == "baseline":
            r = await b.baseline()
            self._acted()
            self._log(f"{prefix}baseline measured: {r}{note}", step=i)
        elif do == "capture":
            m = await self._metrics()
            self.vars[step["name"]] = m.get(step["metric"])
            self._log(f"{prefix}captured {step['name']} = {step['metric']} = {self.vars[step['name']]}{note}", step=i)
        elif do == "wait":
            s = step["s"] * self.time_scale
            self._log(f"{prefix}wait {s:.0f}s{note}", step=i)
            t0 = time.time()
            self.wait = {"kind": "wait", "started": t0, "timeout_s": s, "deadline": t0 + s, "mode": "all", "conds": []}
            await asyncio.sleep(s)
        elif do == "observe":
            m = await self._metrics()
            self.observations.append({"label": step["label"], "time": _iso(time.time()), "metrics": m})
            self._log(
                f"{prefix}observe '{step['label']}': rps {m['achieved_rps']}/{m['target_rps']}, p99 {m['ttft_p99_ms']} ms, "
                f"vLLM {m['vllm_ready']}/{m['vllm_desired']}, G4 {m['g4_nodes']}, N4D {m['n4d_nodes']}, "
                f"backfill {m['backfill_ready']}, overflow {m['overflow_ready']}, blocks {m['guard_blocks']}{note}",
                "ok",
                step=i,
                metrics=m,
            )
        elif do == "wait_until":
            for var, metric in (step.get("capture_before") or {}).items():
                self.vars[var] = (await self._metrics()).get(metric)
            mode = "any" if "any" in step else "all"
            conds = step.get(mode) or []
            timeout = step.get("timeout_s", 600) * self.time_scale
            t0 = time.time()
            self._log(f"{prefix}wait until {mode} {conds} (timeout {timeout:.0f}s){note}", step=i)
            self.wait = {"kind": "wait_until", "started": t0, "timeout_s": timeout, "deadline": t0 + timeout, "mode": mode, "conds": []}
            last_desc = []
            while True:
                try:
                    m = await self._metrics()
                except Exception as e:  # noqa: BLE001 - transient API errors must not kill a long scenario
                    self._log(f"{prefix}  state fetch failed ({type(e).__name__}: {e}); retrying", "warn", step=i)
                    m = {}
                self.wait["conds"] = self._eval(m, conds)
                ok, last_desc = self._check(m, conds, mode)
                if ok:
                    self._log(f"{prefix}  met after {time.time() - t0:.0f}s: {', '.join(last_desc)}", "ok", step=i)
                    return
                if time.time() - t0 > timeout:
                    raise TimeoutError(f"condition not met in {timeout:.0f}s: {', '.join(last_desc)}")
                await asyncio.sleep(self.poll_s)
        else:
            raise ValueError(f"unknown step kind {do!r}")

    def start(self, name: str) -> dict:
        name = resolve_name(name)
        if self.task and not self.task.done():
            raise RuntimeError(f"scenario {self.name} already running")
        self.reset(name)
        self.task = asyncio.create_task(self.run(name))
        return self.snapshot()

    def cancel(self) -> bool:
        if self.task and not self.task.done():
            self.task.cancel()
            return True
        return False

    @property
    def running(self) -> bool:
        return bool(self.task and not self.task.done())
