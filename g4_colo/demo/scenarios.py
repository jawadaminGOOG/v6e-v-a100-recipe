"""Demo scenarios as data + a small async runner.

Each scenario is a list of steps (plain dicts) so the web app, run_scenario.py
and any other harness (e.g. recipe/tests/run_scenario.py) can share them.

Step kinds:
  {"do": "gpu_rate", "rate": 300}
  {"do": "demand", "sandbox": 4, "toolcall": 4, "analytics": 2}
  {"do": "guard", "enabled": true}
  {"do": "baseline"}
  {"do": "capture", "name": "n4d0", "metric": "n4d_nodes"}      # save a metric value
  {"do": "wait", "s": 60}
  {"do": "wait_until", "all": [["vllm_ready", ">=", 2]], "timeout_s": 900}
       # rhs may be a number or "$name" (captured) or "$name+1"
  {"do": "observe", "label": "steady"}                          # record a snapshot
Every step may carry a "note" shown in the step log.
Metric names are the keys of state.summarize().
"""

from __future__ import annotations

import asyncio
import operator
import time
from datetime import datetime, timezone
from typing import Any

from state import summarize

SCENARIOS: dict[str, dict[str, Any]] = {
    "gpu-scale-out": {
        "title": "GPU scale-out: 1200 rps scales vLLM 1->2, adds G4 #2, and packs 38/38 spare G4 slots (76 vCPUs)",
        "steps": [
            {"do": "guard", "enabled": True, "note": "latency guard on (only blocks memory-heavy, never integer)"},
            {"do": "demand", "sandbox": 19, "toolcall": 19, "analytics": 0, "note": "38 integer pods (76 vCPU): 19 fill G4 #1, 19 on N4D overflow"},
            {"do": "gpu_rate", "rate": 300, "note": "steady 300 rps on G4 #1"},
            {"do": "wait_until", "all": [["backfill_ready", ">=", 18]], "timeout_s": 120, "note": "G4 #1 spare cores 100% packed (19/19 slots = 38 vCPU)"},
            {"do": "observe", "label": "before"},
            {"do": "gpu_rate", "rate": 1200, "note": "spike GPU load to 1200 rps (> 700 rps/pod HPA target)"},
            {"do": "wait_until", "all": [["vllm_desired", ">=", 2]], "timeout_s": 60, "note": "vLLM scales 1 -> 2 replicas immediately"},
            {"do": "wait_until", "all": [["g4_nodes", ">=", 2]], "timeout_s": 300, "note": "GKE ComputeClass provisions 2nd G4 node"},
            {"do": "wait_until", "all": [["backfill_ready", ">=", 34]], "timeout_s": 180, "note": "19 overflow pods shift from N4D onto G4 #2 spare cores (76 vCPU reclaimed)"},
            {"do": "wait_until", "all": [["vllm_ready", ">=", 2]], "timeout_s": 600, "note": "2nd vLLM replica Ready and serving"},
            {"do": "observe", "label": "after"},
        ],
    },
    "gpu-scale-in": {
        "title": "GPU scale-in: 300 rps drops vLLM 2->1, shifts G4 #2 backfill to warm N4D, and releases G4 #2",
        "steps": [
            {"do": "demand", "sandbox": 19, "toolcall": 19, "analytics": 0, "note": "keep 38 integer pods (76 vCPU) active across scale-in"},
            {"do": "observe", "label": "before"},
            {"do": "gpu_rate", "rate": 300, "note": "drop GPU load to 300 rps (fits on 1 vLLM replica)"},
            {"do": "wait_until", "all": [["vllm_desired", "<=", 1]], "timeout_s": 60, "note": "vLLM scales 2 -> 1 immediately"},
            {"do": "wait_until", "all": [["g4_nodes", "<=", 1]], "timeout_s": 600, "note": "CA drains G4 #2 (1m consolidation delay); 19 pods shift to warm N4D"},
            {"do": "wait_until", "all": [["backfill_ready", ">=", 18], ["overflow_ready", ">=", 18]], "timeout_s": 120, "note": "G4 #1 stays 100% packed (19 pods) + 19 pods on N4D"},
            {"do": "observe", "label": "after"},
        ],
    },
    "cpu-scale-out": {
        "title": "CPU scale-out: G4 #1 stays 100% packed (38 vCPU) while excess CPU demand spills to N4D in <10s",
        "steps": [
            {"do": "gpu_rate", "rate": 300, "note": "steady 300 rps on 1 G4 node"},
            {"do": "demand", "sandbox": 10, "toolcall": 9, "analytics": 0, "note": "19 integer pods (38 vCPU) = 100% of G4 #1 spare slots"},
            {"do": "wait_until", "all": [["overflow_desired", "==", 0], ["backfill_ready", ">=", 18]], "timeout_s": 120, "note": "all 19 pods running on G4 spare cores (0 on N4D)"},
            {"do": "observe", "label": "fits-in-backfill"},
            {"do": "demand", "sandbox": 18, "toolcall": 18, "analytics": 0, "note": "spike CPU demand to 36 pods (72 vCPU > 38 G4 spare vCPU)"},
            {"do": "wait_until", "all": [["overflow_desired", ">=", 17]], "timeout_s": 30, "note": "controller keeps 19 pods on G4 and assigns 17 overflow pods to N4D"},
            {"do": "wait_until", "all": [["overflow_ready", ">=", 17]], "timeout_s": 180, "note": "all 17 overflow pods Ready on N4D (G4 node count unchanged at 1)"},
            {"do": "observe", "label": "spilled"},
            {"do": "demand", "sandbox": 10, "toolcall": 9, "analytics": 0, "note": "drop demand back to 19 pods (overflow shrinks first; G4 stays 100% packed)"},
            {"do": "wait_until", "all": [["overflow_desired", "==", 0], ["backfill_ready", ">=", 18]], "timeout_s": 60, "note": "N4D overflow back to 0; G4 #1 keeps all 19 pods (38 vCPU)"},
            {"do": "observe", "label": "after"},
        ],
    },
    "guard-integer-vs-memory": {
        "title": "Latency guard: 38 vCPU integer work is harmless; memory-heavy analytics trips guard -> evicted to N4D, G4 refills with integer",
        "steps": [
            {"do": "guard", "enabled": False, "note": "clear any prior node guard blocks"},
            {"do": "gpu_rate", "rate": 400, "note": "steady 400 rps on vLLM"},
            {"do": "demand", "sandbox": 10, "toolcall": 9, "analytics": 0, "note": "pack G4 with 19 integer pods (38 vCPU)"},
            {"do": "wait_until", "all": [["integer_backfill_ready", ">=", 18]], "timeout_s": 120, "note": "all 19 integer pods running on G4 spare cores"},
            {"do": "guard", "enabled": True, "note": "enable latency guard (threshold = 27 ms)"},
            {"do": "wait", "s": 12, "note": "hold 38 vCPU integer load: TTFT p99 stays ~20 ms (< 27 ms), 0 evictions"},
            {"do": "observe", "label": "integer-harmless"},
            {"do": "capture", "name": "ev0", "metric": "guard_evict_events"},
            {"do": "demand", "sandbox": 0, "toolcall": 0, "analytics": 16, "note": "switch G4 backfill to 16 memory-heavy analytics pods (3D stencil sweeps)"},
            {"do": "wait_until", "any": [["guard_evict_events", ">", "$ev0"], ["guard_blocks", ">=", 1]], "timeout_s": 120, "note": "memory-bus contention pushes TTFT p99 > 27 ms -> ColoGuardEvict fires!"},
            {"do": "observe", "label": "guard-fired"},
            {"do": "demand", "sandbox": 10, "toolcall": 9, "analytics": 16, "note": "refill G4 spare cores with 19 integer pods while analytics runs on N4D overflow"},
            {"do": "wait_until", "all": [["integer_backfill_ready", ">=", 18], ["analytics_overflow_ready", ">=", 12]], "timeout_s": 180, "note": "G4 100% packed with integer (38 vCPU) + analytics isolated on N4D"},
            {"do": "observe", "label": "after"},
        ],
    },
}
SCENARIO_ALIASES = {"1": "gpu-scale-out", "2": "gpu-scale-in", "3": "cpu-scale-out", "4": "guard-integer-vs-memory"}

OPS = {">=": operator.ge, ">": operator.gt, "<=": operator.le, "<": operator.lt, "==": operator.eq, "!=": operator.ne}


def resolve_name(name: str) -> str:
    name = SCENARIO_ALIASES.get(name, name)
    if name not in SCENARIOS:
        raise KeyError(f"unknown scenario {name!r}; choose from {list(SCENARIOS)}")
    return name


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, tz=timezone.utc).isoformat(timespec="seconds")


class ScenarioRunner:
    """Runs one scenario at a time against a backend; keeps a step log."""

    def __init__(self, backend, poll_s: float = 2.0, time_scale: float = 1.0):
        self.backend = backend
        self.poll_s = poll_s
        self.time_scale = time_scale
        self.task: asyncio.Task | None = None
        self.reset(None)

    def reset(self, name: str | None):
        self.name = name
        self.status = "idle" if name is None else "running"
        self.started = time.time()
        self.finished: float | None = None
        self.step_index = -1
        self.log: list[dict] = []
        self.observations: list[dict] = []
        self.vars: dict[str, Any] = {}

    def snapshot(self) -> dict:
        steps = SCENARIOS.get(self.name or "", {}).get("steps", [])
        cur_step = steps[self.step_index] if 0 <= self.step_index < len(steps) else None
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
            "log": self.log[-200:],
            "observations": self.observations,
        }

    def _log(self, msg: str, level: str = "info", **extra):
        t = time.time()
        self.log.append({"t": t, "time": _iso(t), "elapsed_s": round(t - self.started, 1), "level": level, "msg": msg, **extra})

    async def _metrics(self) -> dict:
        return summarize(await self.backend.state())

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

    def _check(self, m: dict, conds: list, mode: str) -> tuple[bool, list[str]]:
        res, desc = [], []
        for metric, op, rhs in conds:
            rv = self._rhs(rhs)
            lv = m.get(metric)
            ok = lv is not None and rv is not None and OPS[op](lv, rv)
            res.append(ok)
            desc.append(f"{metric}={lv} {op} {rv}")
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
            raise
        except Exception as e:  # noqa: BLE001
            self.status = "failed"
            self._log(f"step {self.step_index + 1} failed: {type(e).__name__}: {e}", "error")
        finally:
            self.finished = time.time()
        return self.snapshot()

    async def _run_step(self, i: int, step: dict):
        do = step["do"]
        note = f" - {step['note']}" if step.get("note") else ""
        b = self.backend
        prefix = f"[{i + 1}] "
        if do == "gpu_rate":
            await b.set_gpu_rate(step["rate"])
            self._log(f"{prefix}GPU request rate -> {step['rate']} rps{note}", step=i)
        elif do == "demand":
            d = {k: step[k] for k in ("sandbox", "toolcall", "analytics") if k in step}
            await b.set_demand(d)
            self._log(f"{prefix}demand -> {d}{note}", step=i)
        elif do == "guard":
            await b.set_guard(step["enabled"])
            self._log(f"{prefix}guard -> {'on' if step['enabled'] else 'off'}{note}", step=i)
        elif do == "baseline":
            r = await b.baseline()
            self._log(f"{prefix}baseline measured: {r}{note}", step=i)
        elif do == "capture":
            m = await self._metrics()
            self.vars[step["name"]] = m.get(step["metric"])
            self._log(f"{prefix}captured {step['name']} = {step['metric']} = {self.vars[step['name']]}{note}", step=i)
        elif do == "wait":
            s = step["s"] * self.time_scale
            self._log(f"{prefix}wait {s:.0f}s{note}", step=i)
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
            last_desc = []
            while True:
                try:
                    m = await self._metrics()
                except Exception as e:  # noqa: BLE001 - transient API errors must not kill a long scenario
                    self._log(f"{prefix}  state fetch failed ({type(e).__name__}: {e}); retrying", "warn", step=i)
                    m = {}
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
