#!/usr/bin/env python3
"""Streaming open-loop / closed-loop client for the g4-colo experiment.

Reuses the customer's prompt construction from benchmark.py unchanged (same
dataset, same guideline prefix, same chat template, same request body) and adds:

* streaming (`stream: true`) so TTFT, inter-token latency (ITL) and end-to-end
  latency (E2E) are measured per request;
* open-loop mode: Poisson arrivals at a fixed rate for a fixed duration,
  independent of server response time (a slowed server cannot hide its
  degradation by receiving less work);
* closed-loop mode: fixed concurrency, identical semantics to benchmark.py
  `--semaphores`, kept as a bridge to the earlier G4 results;
* per-request CSV + a JSON summary, plus schedule-lag accounting so a
  client-side bottleneck is detectable (and the run discarded) rather than
  silently mistaken for server slowdown.

Several processes can share one target rate: each draws an independent Poisson
stream at rate/N; the superposition is Poisson at the full rate.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys
import time
from pathlib import Path

import aiohttp
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import benchmark as cx  # noqa: E402  (customer client, unmodified)


STATE: dict = {}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--input-file", type=Path, required=True)
    p.add_argument("--output-dir", type=Path, required=True)
    p.add_argument("--url", required=True)
    p.add_argument("--tokenizer", default="Qwen/Qwen3-1.7B-Base")
    p.add_argument("--served-model-name", default=None)
    p.add_argument("--label", required=True, help="run label used in output filenames")
    p.add_argument("--mode", choices=["open", "closed"], required=True)
    p.add_argument("--rate", type=float, default=0.0, help="open-loop req/s for THIS process")
    p.add_argument("--duration", type=float, default=300.0, help="open-loop send window (s)")
    p.add_argument("--concurrency", type=int, default=250, help="closed-loop in-flight requests")
    p.add_argument("--num-messages", type=int, default=3000, help="closed-loop request count")
    p.add_argument("--warmup", type=float, default=0.0, help="open-loop: drop requests sent in the first N s")
    p.add_argument("--request-timeout", type=float, default=120.0)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--start-at", type=float, default=0.0, help="unix time to start (sync across pods)")
    return p.parse_args()


def build_prompts(args: argparse.Namespace, n: int) -> list[str]:
    from transformers import AutoTokenizer

    tok = AutoTokenizer.from_pretrained(args.tokenizer, trust_remote_code=True)
    random.seed(args.seed)
    df_len = len(pd.read_excel(args.input_file))
    prompts: list[str] = []
    seed = args.seed
    # benchmark.py samples without replacement, so draw in chunks of <= df_len.
    while len(prompts) < n:
        k = min(df_len, n - len(prompts))
        prompts += cx.generate_benchmark_prompts(args.input_file, tok, 1, k, seed)
        seed += 1
    return prompts


async def one_request(session, url, body, rec, t_sched):
    rec["t_sched"] = t_sched
    t0 = time.perf_counter()
    rec["t_send"] = t0
    tok_times: list[float] = []
    try:
        async with session.post(url, json=body) as resp:
            rec["status"] = resp.status
            if resp.status != 200:
                rec["error"] = (await resp.text())[:200]
                return
            async for raw in resp.content:
                line = raw.strip()
                if not line.startswith(b"data:"):
                    continue
                data = line[5:].strip()
                if data == b"[DONE]":
                    break
                obj = json.loads(data)
                if obj.get("usage"):
                    rec["prompt_tokens"] = obj["usage"].get("prompt_tokens")
                    rec["completion_tokens"] = obj["usage"].get("completion_tokens")
                ch = obj.get("choices") or []
                if ch and ch[0].get("text"):
                    tok_times.append(time.perf_counter())
        t1 = time.perf_counter()
        rec["e2e"] = t1 - t0
        if tok_times:
            rec["ttft"] = tok_times[0] - t0
            if len(tok_times) > 1:
                rec["itl_mean"] = (tok_times[-1] - tok_times[0]) / (len(tok_times) - 1)
            rec["n_chunks"] = len(tok_times)
        rec["ok"] = True
    except Exception as exc:  # noqa: BLE001
        rec["error"] = repr(exc)[:200]


def make_body(prompt: str, model: str) -> dict:
    body = cx.create_request(prompt, model)  # identical sampling params
    body["stream"] = True
    body["stream_options"] = {"include_usage": True}
    return body


async def run_open(args, prompts, model):
    rng = np.random.default_rng(args.seed)
    gaps = rng.exponential(1.0 / args.rate, size=int(args.rate * args.duration * 2) + 10)
    offsets = np.cumsum(gaps)
    offsets = offsets[offsets < args.duration]
    recs = [dict(i=i, ok=False) for i in range(len(offsets))]
    conn = aiohttp.TCPConnector(limit=0)
    timeout = aiohttp.ClientTimeout(total=args.request_timeout)
    tasks = []
    async with aiohttp.ClientSession(connector=conn, timeout=timeout) as s:
        if args.start_at:
            await asyncio.sleep(max(0.0, args.start_at - time.time()))
        base = time.perf_counter()
        STATE["base_unix"] = time.time()
        for i, off in enumerate(offsets):
            now = time.perf_counter() - base
            if off > now:
                await asyncio.sleep(off - now)
            body = make_body(prompts[i % len(prompts)], model)
            recs[i]["offset"] = float(off)
            tasks.append(asyncio.create_task(one_request(s, args.url, body, recs[i], base + off)))
        await asyncio.gather(*tasks)
    return recs, base


async def run_closed(args, prompts, model):
    sem = asyncio.Semaphore(args.concurrency)
    recs = [dict(i=i, ok=False) for i in range(len(prompts))]
    conn = aiohttp.TCPConnector(limit=args.concurrency)
    timeout = aiohttp.ClientTimeout(total=args.request_timeout)

    async def go(i, s, base):
        async with sem:
            await one_request(s, args.url, make_body(prompts[i], model), recs[i], time.perf_counter())
            recs[i]["offset"] = recs[i]["t_send"] - base

    async with aiohttp.ClientSession(connector=conn, timeout=timeout) as s:
        if args.start_at:
            await asyncio.sleep(max(0.0, args.start_at - time.time()))
        base = time.perf_counter()
        STATE["base_unix"] = time.time()
        await asyncio.gather(*(go(i, s, base) for i in range(len(prompts))))
    return recs, base


def summarize(args, df: pd.DataFrame, wall: float) -> dict:
    ok = df[df.ok == True]  # noqa: E712
    win = ok[ok.offset >= args.warmup] if args.mode == "open" else ok
    lag = (df.t_send - df.t_sched).dropna()

    def pct(col, q):
        v = win[col].dropna()
        return float(np.percentile(v, q)) if len(v) else None

    return {
        "label": args.label, "mode": args.mode, "rate": args.rate,
        "concurrency": args.concurrency if args.mode == "closed" else None,
        "n_sent": int(len(df)), "n_ok": int(len(ok)), "n_failed": int(len(df) - len(ok)),
        "n_window": int(len(win)), "wall_s": wall,
        "achieved_rps": len(ok) / wall if wall else None,
        "output_tok_s": float(ok.completion_tokens.sum()) / wall if wall else None,
        "ttft_p50": pct("ttft", 50), "ttft_p99": pct("ttft", 99),
        "itl_p50": pct("itl_mean", 50), "itl_p99": pct("itl_mean", 99),
        "e2e_p50": pct("e2e", 50), "e2e_p99": pct("e2e", 99),
        "avg_prompt_tokens": float(win.prompt_tokens.mean()) if len(win) else None,
        "avg_completion_tokens": float(win.completion_tokens.mean()) if len(win) else None,
        "sched_lag_p99_ms": float(np.percentile(lag, 99) * 1e3) if len(lag) else None,
        "sched_lag_max_ms": float(lag.max() * 1e3) if len(lag) else None,
        "host": os.uname().nodename,
    }


def main() -> None:
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    model = args.served_model_name or args.tokenizer
    n = int(args.rate * args.duration * 1.3) + 50 if args.mode == "open" else args.num_messages
    prompts = build_prompts(args, n)
    t_start = time.time()
    if args.mode == "open":
        recs, base = asyncio.run(run_open(args, prompts, model))
    else:
        recs, base = asyncio.run(run_closed(args, prompts, model))
    df = pd.DataFrame(recs)
    for c in ["ttft", "itl_mean", "e2e", "prompt_tokens", "completion_tokens", "t_send", "t_sched", "offset"]:
        if c not in df:
            df[c] = np.nan
    df["completion_tokens"] = df["completion_tokens"].fillna(0)
    wall = float((df.t_send + df.e2e.fillna(0)).max() - base) if len(df) else 0.0
    if args.mode == "open":
        wall = max(wall, args.duration)
    summ = summarize(args, df, wall)
    summ["t_start_unix"] = t_start
    summ["base_unix"] = STATE.get("base_unix")
    # wall-clock send/finish times so analysis can window requests against SPEC activity
    df["send_unix"] = summ["base_unix"] + df["offset"]
    df["done_unix"] = df["send_unix"] + df["e2e"]
    df.drop(columns=["t_send", "t_sched"]).to_csv(args.output_dir / f"{args.label}.requests.csv.gz", index=False)
    (args.output_dir / f"{args.label}.summary.json").write_text(json.dumps(summ, indent=2))
    print(json.dumps(summ))


if __name__ == "__main__":
    main()
