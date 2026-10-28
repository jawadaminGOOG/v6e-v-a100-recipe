#!/usr/bin/env python3
"""Per-second aggregates of the per-request client logs (committed to git; the raw
per-request files are ~600 MB and are kept out of git, see README)."""
import glob
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent
for d in sorted(glob.glob(str(ROOT / "results" / "cells" / "*"))):
    fs = glob.glob(d + "/*.requests.csv.gz")
    if not fs:
        continue
    df = pd.concat([pd.read_csv(f) for f in fs], ignore_index=True)
    df["sec"] = df.send_unix.astype(int)
    ok = df[df.ok == True]  # noqa: E712
    g = df.groupby("sec").agg(sent=("i", "size"), ok=("ok", "sum"))
    q = ok.groupby("sec").agg(ttft_p50=("ttft", "median"), ttft_p99=("ttft", lambda x: x.quantile(.99)),
                              itl_p50=("itl_mean", "median"), e2e_p50=("e2e", "median"),
                              e2e_p99=("e2e", lambda x: x.quantile(.99)), out_tokens=("completion_tokens", "sum"))
    g.join(q).reset_index().to_csv(Path(d) / "requests_per_second.csv.gz", index=False, float_format="%.5f")
    print(Path(d).name)
