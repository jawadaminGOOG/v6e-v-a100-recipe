#!/usr/bin/env python3
"""Quick per-cell validity + headline check. usage: check_cell.py CELLDIR [CELLDIR...]"""
import glob
import json
import sys
from pathlib import Path

import pandas as pd

for d in map(Path, sys.argv[1:]):
    m = json.loads((d / "DONE").read_text()) if (d / "DONE").exists() else {}
    out = [d.name]
    st = sorted(glob.glob(str(d / "cpu_manager_state.*.json")))
    if st:
        e = json.loads(Path(st[0]).read_text()).get("entries", {})
        out.append("cpus=" + ";".join(f"{k}:{v}" for x in e.values() for k, v in x.items()))
    sp = d / "spec_pod.log"
    spec_end = None
    if sp.exists():
        L = sp.read_text()
        se = [l for l in L.splitlines() if l.startswith("SPEC_END")]
        spec_end = float(se[0].split()[1]) if se else None
        nS = 0
        for f in glob.glob(str(d / "CPU2026.*.csv")):
            t = Path(f).read_text()
            blk = t.split('"Full Results Table"')[1].split('"Selected Results Table"')[0]
            nS += sum(1 for l in blk.splitlines() if l.count(",") > 5 and l.split(",")[5] == "S")
        out.append(f"spec_rc0={'rc=0' in L} spec_S={nS}/16")
    sums = [json.loads(Path(f).read_text()) for f in sorted(glob.glob(str(d / "*.summary.json")))]
    if sums:
        base = min(s["base_unix"] for s in sums)
        load_end = base + max(s["wall_s"] for s in sums)
        df = pd.concat([pd.read_csv(f) for f in glob.glob(str(d / "*.requests.csv.gz"))])
        w = df[(df.send_unix >= base + 60) & (df.send_unix < base + 660)]
        ok = w[w.ok == True]  # noqa: E712
        out.append(f"win_n={len(w)} fail={len(w) - len(ok)} ttft50={ok.ttft.median()*1e3:.1f}ms "
                   f"ttft99={ok.ttft.quantile(.99)*1e3:.1f}ms itl50={ok.itl_mean.median()*1e3:.2f}ms "
                   f"e2e50={ok.e2e.median()*1e3:.1f}ms e2e99={ok.e2e.quantile(.99)*1e3:.1f}ms "
                   f"lag99={max(s['sched_lag_p99_ms'] for s in sums):.1f}ms")
        allok = df[df.ok == True]  # noqa: E712
        out.append(f"whole_run_fail={len(df) - len(allok)} whole_ttft99={allok.ttft.quantile(.99)*1e3:.0f}ms")
        if spec_end:
            out.append(f"spec_end_before_load_end={spec_end < load_end} (margin {load_end - spec_end:.0f}s)")
    print(" | ".join(out))
