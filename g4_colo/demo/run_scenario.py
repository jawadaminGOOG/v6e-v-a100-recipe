#!/usr/bin/env python3
"""Run a demo scenario headlessly and write a JSON log of observations.

    python run_scenario.py gpu-scale-out --out /tmp/gpu-scale-out.json
    DEMO_MOCK=1 python run_scenario.py cpu-scale-out --time-scale 0.5
    python run_scenario.py --list

While the scenario runs, a metrics sample (state.summarize) is recorded every
--sample-s seconds into "samples", so the JSON is a time series as well as a
step log. Exit code 0 = scenario finished, 1 = failed/timed out.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from backend import get_backend  # noqa: E402
from scenarios import SCENARIOS, ScenarioRunner, resolve_name  # noqa: E402
from state import CLUSTER, summarize  # noqa: E402


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("scenario", nargs="?", help=f"one of {list(SCENARIOS)} (or 1-4)")
    ap.add_argument("--out", help="JSON output path (default /tmp/scenario-<name>-<ts>.json)")
    ap.add_argument("--poll-s", type=float, default=4.0, help="condition poll interval")
    ap.add_argument("--sample-s", type=float, default=10.0, help="metrics sampling interval")
    ap.add_argument("--time-scale", type=float, default=1.0, help="multiply wait/timeout durations")
    ap.add_argument("--list", action="store_true", help="list scenarios and exit")
    args = ap.parse_args()

    if args.list or not args.scenario:
        for k, v in SCENARIOS.items():
            print(f"{k:26s} {v['title']}")
            for i, s in enumerate(v["steps"], 1):
                print(f"    {i:2d}. {json.dumps(s)}")
        return 0

    name = resolve_name(args.scenario)
    backend = get_backend()
    runner = ScenarioRunner(backend, poll_s=args.poll_s, time_scale=args.time_scale)
    samples: list[dict] = []
    printed = 0

    async def sampler():
        nonlocal printed
        while True:
            try:
                samples.append(summarize(await backend.state()))
            except Exception as e:  # noqa: BLE001
                samples.append({"t": time.time(), "error": f"{type(e).__name__}: {e}"})
            while printed < len(runner.log):
                e = runner.log[printed]
                print(f"{e['time']} +{e['elapsed_s']:>6.0f}s {e['level']:5s} {e['msg']}", flush=True)
                printed += 1
            await asyncio.sleep(min(args.sample_s, 2.0) if printed < len(runner.log) else args.sample_s)

    stask = asyncio.create_task(sampler())
    result = await runner.run(name)
    stask.cancel()
    for e in runner.log[printed:]:
        print(f"{e['time']} +{e['elapsed_s']:>6.0f}s {e['level']:5s} {e['msg']}")

    out = Path(args.out or f"/tmp/scenario-{name}-{int(time.time())}.json")
    doc = {
        "scenario": name,
        "cluster": CLUSTER,
        "mode": backend.mode,
        "steps": SCENARIOS[name]["steps"],
        **result,
        "samples": samples,
    }
    out.write_text(json.dumps(doc, indent=2, default=str))
    print(f"wrote {out} ({result['status']})")
    return 0 if result["status"] == "done" else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
