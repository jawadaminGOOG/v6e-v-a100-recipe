#!/usr/bin/env python3
"""Merge sweep1 with the single-cluster reruns into one directory for plots.py.

sweep1 ran both clusters at the same time. Both draw N4D nodes from one regional quota (CPUS_PER_VM_FAMILY, 91 N4D
vCPUs in us-west8), so whichever cluster scaled N4D second was starved: native's demand step and the controller's
GPU scale-in in sweep1 measure quota contention, not the recipe. Those phases were rerun one cluster at a time:
  native:     step + gpu  -> sweep1_native_rerun
  controller: gpu         -> sweep1_controller_rerun
The backfill and load phases (no N4D scaling) come from sweep1, measured at the same moment on both clusters.

    python3 merge.py <bench dir>     # writes <bench dir>/sweep1_merged/
"""
import json
import shutil
import sys
from pathlib import Path

B = Path(sys.argv[1])
OUT = B / "sweep1_merged"
OUT.mkdir(exist_ok=True)
PLAN = {"controller": ("sweep1_controller_rerun", {"gpu_out", "gpu_in", "gpu_end"}),
        "native": ("sweep1_native_rerun", {"step_up", "step_down", "step_end", "gpu_out", "gpu_in", "gpu_end"})}
for name, (rerun, phases) in PLAN.items():
    base_marks = json.load(open(B / "sweep1" / f"{name}.marks.json"))
    re_marks = json.load(open(B / rerun / f"{name}.marks.json"))
    marks = [m for m in base_marks if m["phase"] not in phases] + [m for m in re_marks if m["phase"] in phases]
    marks.sort(key=lambda m: m["t"])
    for m in marks:
        m["source"] = rerun if m["phase"] in phases else "sweep1"
    json.dump(marks, open(OUT / f"{name}.marks.json", "w"), indent=1)
    with open(OUT / f"{name}.samples.jsonl", "w") as fh:
        for src in ("sweep1", rerun):
            fh.write(open(B / src / f"{name}.samples.jsonl").read())
shutil.copy(B / "sweep1" / "pre_sweep_state.json", OUT / "pre_sweep_state.json")
(OUT / "SOURCES.md").write_text(__doc__)
print("wrote", OUT)
