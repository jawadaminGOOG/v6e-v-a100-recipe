#!/usr/bin/env python3
"""Merge the TPU v6e sweep attempts in .agents/bench/v6e_sweep1 into tpu.marks.json for plots.py / charts.py.

Every attempt appended to the same tpu.samples.jsonl / tpu.cpus.jsonl (timestamps never overlap); only the marks
file was rewritten per attempt, so the old ones were moved to attempt<N>/. Marks kept:
  attempt2   setup, backfill ladder (0-16 pods), load ladder solo (0 pods, 50-400 rps)
  v6e_sweep2 load ladder with 16 backfill pods, 50-400 rps, clean (phase-2 rerun 22:12-22:20Z); attempt3's partial
             ladder (50/100 rps before the engine hung) is the fallback
  (top dir)  the final run: step and gpu phases, restore
attempt1 died on a GKE control-plane 503 during the backfill ladder (its 0/4/8-pod points were remeasured).

    python3 merge_v6e.py .agents/bench/v6e_sweep1          # reads final/tpu.marks.json -> writes tpu.marks.json
"""
import json
import shutil
import sys
from pathlib import Path

D = Path(sys.argv[1])
final_src = D / "final" / "tpu.marks.json"
if not final_src.exists():  # first run: park the final attempt's marks next to the others
    final_src.parent.mkdir(exist_ok=True)
    shutil.copy(D / "tpu.marks.json", final_src)
    for f in ("sweep.log", "pre_sweep_state.json"):
        if (D / f).exists():
            shutil.copy(D / f, D / "final" / f)
a2 = json.load(open(D / "attempt2" / "tpu.marks.json"))
a3 = json.load(open(D / "attempt3" / "tpu.marks.json"))
fin = json.load(open(final_src))
marks = [dict(m, source="attempt2") for m in a2 if m["phase"] in ("setup", "backfill")
         or (m["phase"] == "load" and m["pods"] == 0)]
# Clean 16-pod load ladder (50-400 rps, 0 errors) from the phase-2 rerun, v6e_sweep2 (22:12-22:20Z). Used instead of
# attempt3's partial ladder when present.
s2 = D.parent / "v6e_sweep2" / "tpu.marks.json"
l16 = [m for m in json.load(open(s2)) if m["phase"] == "load" and m["pods"] == 16] if s2.exists() else []
if l16 and all(m["ttft"]["requests"] and not m.get("errs") for m in l16):
    marks += [dict(m, source="v6e_sweep2") for m in l16]
else:
    marks += [dict(m, source="attempt3") for m in a3 if m["phase"] == "load" and m["pods"] == 16
              and m["ttft"]["requests"] and not m.get("errs")]
marks += [dict(m, source="final") for m in fin if m["phase"] not in ("setup", "backfill", "load")]
marks.sort(key=lambda m: m["t"])
json.dump(marks, open(D / "tpu.marks.json", "w"), indent=1)
(D / "SOURCES.md").write_text(__doc__)
print("wrote", D / "tpu.marks.json", len(marks), "marks")
