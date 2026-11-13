#!/usr/bin/env python3
"""Data bundle for the Agent Substrate results slide (12), built from the raw logs of the native runs.

    python3 g4_colo/deck/substrate_data.py      # writes g4_colo/deck/js/substrate_data.js (window.SUBSTRATE_DATA)

Inputs (repo-relative, all from the final design on the native recipe):
  .agents/wave-4/logs/t6_opt2_sync/{timeline.txt,samples.jsonl,evacuations.jsonl,t6.log,scalein_start.txt,
                                    ca_scaledown_events.txt}                         G4 scale-in with 60 actors
  .agents/wave-4/logs/native_smoke2/{t5g.jsonl,t5n.jsonl}                            resume latency, G4 vs N4D
  .agents/wave-4/logs/native_smoke2/busy.txt                                         busy Workers up / down
  .agents/wave-4/logs/t2_native_cpuset_probe.txt                                     cpuset of every Worker
  .agents/wave-4/logs/smoke_native_opt2/smoke_results.json                           live UI smoke checks
The JS charts in js/deck.js draw only what is in this file; no value on that slide is typed by hand.
"""
from __future__ import annotations

import ast
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path

DECK = Path(__file__).resolve().parent
REPO = DECK.parent.parent
LOGS = REPO / ".agents" / "wave-4" / "logs"
DAY = "2026-11-12"


def ts(s: str) -> float:
    s = s.strip().rstrip("Z")
    if "T" not in s:
        s = f"{DAY}T{s}"
    s = re.sub(r"(\.\d{6})\d+", r"\1", s)
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp()


def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(math.ceil(q * len(xs))) - 1)]


def t6():
    d = LOGS / "t6_opt2_sync"
    t0 = ts((d / "scalein_start.txt").read_text().split()[0])
    log = (d / "t6.log").read_text()
    removed = ts(re.search(r"^(\S+) G4 node removed", log, re.M).group(1))
    m = re.search(r"^(\S+) after: crashed=(\d+) stats=(\{.*\})$", log, re.M)
    after_t, after = ts(m.group(1)), json.loads(m.group(3))
    marked = min(ts(x) for x in re.findall(r"^(\S+)\s+\S+\s+marked the node as toBeDeleted", (d / "ca_scaledown_events.txt").read_text(), re.M) if ts(x) >= t0)
    actors = []
    for line in (d / "timeline.txt").read_text().splitlines():
        mm = re.match(r"(\S+) g4nodes=(\d+) crashed=(\d+) hpa=(\d+) (\{.*\})", line)
        if not mm:
            continue
        st = ast.literal_eval(mm.group(5))
        bp = st.get("actors_running_by_pool", {})
        actors.append([round(ts(mm.group(1)) - t0, 1), bp.get("g4-workers", 0), bp.get("n4d-workers", 0), int(mm.group(3)), st.get("lost_actors", 0)])
    bp = after["actors_running_by_pool"]
    actors.append([round(after_t - t0, 1), bp.get("g4-workers", 0), bp.get("n4d-workers", 0), 0, after["lost_actors"]])
    workers = []
    for line in (d / "samples.jsonl").read_text().splitlines():
        s = json.loads(line)
        workers.append([round(ts(s["t"]) - t0, 1), s["g4-workers"]["ready"], s["n4d-workers"]["ready"], s["g4_nodes"], s["vllm_ready"]])
    ev = [json.loads(l) for l in (d / "evacuations.jsonl").read_text().splitlines() if l.strip()]
    ev = [[round(ts(e["t"]) - t0, 3), e["ms"], e["ok"]] for e in ev if ts(e["t"]) >= t0]
    return {
        "t0": datetime.fromtimestamp(t0, timezone.utc).strftime("%H:%M:%S"),
        "marked_s": round(marked - t0, 1), "removed_s": round(removed - t0, 1), "after_s": round(after_t - t0, 1),
        "actors": actors, "workers": workers, "evac": ev,
        "after": {k: after[k] for k in ("actors_total", "actors_running", "lost_actors", "failed_requests", "memory_resets",
                                        "resume_p99_ms", "evacuated", "evacuate_failed")} | {"by_pool": bp},
    }


def t5():
    out = {}
    for pool, f in (("g4", "t5g.jsonl"), ("n4d", "t5n.jsonl")):
        rows = [json.loads(l) for l in (LOGS / "native_smoke2" / f).read_text().splitlines() if l.strip()]
        last, e2e, fails = {}, [], 0
        for r in rows:
            if not r.get("ok", True):
                fails += 1
            if r["op"] == "resume" and r["ok"]:
                last[r["actor"]] = r["ms"]
            elif r["op"] == "post" and r["actor"] in last:
                e2e.append(round(last.pop(r["actor"]) + r["ms"], 1))
        e2e.sort()
        out[pool] = {"e2e": e2e, "n": len(e2e), "fails": fails, "p50": pct(e2e, .5), "p99": pct(e2e, .99)}
    out["ratio_p99"] = round(out["g4"]["p99"] / out["n4d"]["p99"], 2)
    return out


def busy():
    rows = []
    for line in (LOGS / "native_smoke2" / "busy.txt").read_text().splitlines():
        mm = re.match(r"(\S+) g4=(\d+) n4d=(\d+) n4d_ready=(\d+) n4dnodes=(\d+) (\{.*\})", line)
        if mm:
            st = ast.literal_eval(mm.group(6))
            rows.append([ts(mm.group(1)), int(mm.group(3)), int(mm.group(4)), int(mm.group(5)), st["actors_running"], st["lost_actors"], st["failed_requests"]])
    t0 = rows[0][0]
    # load on = the 4-min burst (awake 30) that starts with the first sample; "awake 0" at 19:56:21Z (result.md)
    load_off = ts("19:56:21")
    down = next(r[0] for r in rows if r[0] > load_off and r[1] <= rows[0][1])
    return {"rows": [[round(r[0] - t0, 1)] + r[1:] for r in rows], "load_off_s": round(load_off - t0, 1),
            "down_s": round(down - t0, 1), "lost": max(r[5] for r in rows), "failed": max(r[6] for r in rows),
            "start": datetime.fromtimestamp(t0, timezone.utc).strftime("%H:%M:%S")}


def cpuset():
    txt = (LOGS / "t2_native_cpuset_probe.txt").read_text()
    st = json.loads(txt.split("\n", 2)[1])
    owners = {}
    for ent in st["entries"].values():
        for name, cs in ent.items():
            if name in ("vllm", "worker"):
                owners.setdefault(name, []).append(cs)
    probed = re.findall(r"comm=(\S+) cpus=(\S+)", txt.split("== pod", 1)[1].split("== pod", 1)[0])
    worker_tasks = [c for comm, c in probed if comm != "pause"]
    return {"default": st["defaultCpuSet"], "vllm": owners.get("vllm", []), "workers": sorted(owners.get("worker", []), key=lambda c: int(c.split(",")[0])),
            "probe_worker": "18,42", "probe_tasks": len(worker_tasks), "probe_tasks_on_own": sum(1 for c in worker_tasks if c == "18,42")}


def smoke():
    r = json.loads((LOGS / "smoke_native_opt2" / "smoke_results.json").read_text())
    return {"ok": sum(1 for x in r if x["ok"]), "n": len(r)}


def main():
    data = {"t6": t6(), "t5": t5(), "busy": busy(), "cpuset": cpuset(), "smoke": smoke()}
    out = DECK / "js" / "substrate_data.js"
    out.write_text("window.SUBSTRATE_DATA = " + json.dumps(data, separators=(",", ":")) + ";\n")
    print("wrote", out, f"{out.stat().st_size / 1e3:.0f} kB")
    print(json.dumps({k: (v if k != "t5" else {p: {kk: vv for kk, vv in v[p].items() if kk != "e2e"} for p in ("g4", "n4d")} | {"ratio": v["ratio_p99"]})
                      for k, v in data.items() if k not in ("t6", "busy")}, indent=1))
    t = data["t6"]
    print("t6", t["t0"], t["marked_s"], t["removed_s"], t["after"], "evac", len(t["evac"]), min(e[1] for e in t["evac"]), max(e[1] for e in t["evac"]),
          max(e[0] for e in t["evac"]) - min(e[0] for e in t["evac"]))
    b = data["busy"]
    print("busy", b["start"], b["load_off_s"], b["down_s"], (b["down_s"] - b["load_off_s"]) / 60, max(r[1] for r in b["rows"]), b["lost"])


if __name__ == "__main__":
    main()
