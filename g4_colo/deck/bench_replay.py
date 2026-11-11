#!/usr/bin/env python3
"""Replay playback benchmark in a real (headless) Chrome over CDP.

    python3 g4_colo/deck/bench_replay.py [--out <dir>] [--quick]

For every replay slide x scenario it:
  1. plays at Fit to the end and times it (wall clock),
  2. plays 5 s at 1x, 4x and 8x,
logging requestAnimationFrame frame times, Long Tasks (PerformanceObserver 'longtask', > 50 ms), and the player's scrub
position every 100 ms (a stall = position did not advance over a 100 ms sample while playing). It also times scenario
and slide switches, checks that players pause when their slide is hidden and restart from 0 when it is shown again, and
saves a screenshot sequence (1 per second) of one Fit playback. Results: <out>/bench_replay.json (+ printed summary).
"""
import argparse
import asyncio
import base64
import json
import shutil
import statistics
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

import websockets

DECK = Path(__file__).resolve().parent
PORT = 9353

INSTR = r"""
(() => {
  if (window.__bench) return true;
  const B = window.__bench = { rec: false, ft: [], lt: [], last: 0 };
  new PerformanceObserver((l) => { for (const e of l.getEntries()) if (B.rec) B.lt.push(e.duration); })
    .observe({ type: "longtask", buffered: false });
  const loop = (t) => { if (B.rec && B.last) B.ft.push(t - B.last); B.last = t; requestAnimationFrame(loop); };
  requestAnimationFrame(loop);
  return true;
})()
"""


def pct(xs, q):
    if not xs:
        return None
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(q * (len(xs) - 1) + 0.5))], 2)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(DECK / "bench"))
    ap.add_argument("--quick", action="store_true", help="2 s per speed instead of 5 s")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    prof = tempfile.mkdtemp(prefix="deckbench-")
    chrome = subprocess.Popen(["google-chrome", "--headless=new", f"--remote-debugging-port={PORT}", f"--user-data-dir={prof}",
                               "--no-sandbox", "--hide-scrollbars", "--window-size=1920,1080", "about:blank"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    res = {"method": "headless Chrome (CDP), 1920x1080; rAF frame deltas, PerformanceObserver longtask, scrub sampled every 100 ms",
           "runs": [], "switches": [], "visibility": []}
    try:
        for _ in range(100):
            try:
                tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json"))
                ws_url = next(t["webSocketDebuggerUrl"] for t in tabs if t["type"] == "page")
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.1)
        async with websockets.connect(ws_url, max_size=None) as ws:
            n = 0

            async def cmd(method, **params):
                nonlocal n
                n += 1
                await ws.send(json.dumps({"id": n, "method": method, "params": params}))
                while True:
                    r = json.loads(await ws.recv())
                    if r.get("id") == n:
                        return r.get("result", {})

            async def js(expr):
                r = await cmd("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True)
                return r.get("result", {}).get("value")

            await cmd("Page.enable")
            await cmd("Page.navigate", url=f"file://{DECK / 'index.html'}#slide=1")
            await asyncio.sleep(2.0)
            await js(INSTR)
            slides = await js("[...document.querySelectorAll('.slide')].map(s=>s.dataset.replay||'')")
            reps = [(i + 1, a) for i, a in enumerate(slides) if a]

            async def go(slide):
                t = await js(f"(()=>{{window.__bench.rec=true;window.__bench.lt=[];const t0=performance.now();"
                             f"location.hash='slide={slide}';window.dispatchEvent(new HashChangeEvent('hashchange'));"
                             f"return performance.now()-t0}})()")
                await asyncio.sleep(0.4)
                lt = await js("(()=>{const l=window.__bench.lt;window.__bench.rec=false;return l})()")
                return t, lt

            async def sample(acc, max_s, until_end):
                await js("window.__bench.ft=[];window.__bench.lt=[];window.__bench.rec=true")
                pos, t0 = [], time.time()
                while True:
                    st = await js(f"(()=>{{const p=window.DeckPlayers['{acc}'];return [p.pos,p.playing,p.dur]}})()")
                    pos.append((time.time() - t0, st[0], st[1]))
                    if (until_end and not st[1]) or time.time() - t0 > max_s:
                        break
                    await asyncio.sleep(0.1)
                b = await js("(()=>{const b=window.__bench;b.rec=false;return {ft:b.ft,lt:b.lt}})()")
                playing = [p for p in pos if p[2]]
                stalls = sum(1 for a_, b_ in zip(playing, playing[1:]) if b_[1] <= a_[1])
                return {"wall_s": round(pos[-1][0], 2), "frames": len(b["ft"]), "ft_p50_ms": pct(b["ft"], 0.5),
                        "ft_p95_ms": pct(b["ft"], 0.95), "ft_max_ms": round(max(b["ft"]), 1) if b["ft"] else None,
                        "longtask_max_ms": round(max(b["lt"]), 1) if b["lt"] else 0, "longtasks": len(b["lt"]),
                        "scrub_samples": len(playing), "scrub_stalls": stalls,
                        "pos_start": round(playing[0][1], 2) if playing else None,
                        "pos_end": round(pos[-1][1], 2), "dur": round(st[2], 2)}

            per_speed = 2 if args.quick else 5
            for slide, acc in reps:
                t, lt = await go(slide)
                res["switches"].append({"what": f"slide -> {slide} ({acc})", "sync_ms": round(t, 1),
                                        "longtask_max_ms": round(max(lt), 1) if lt else 0})
                scen = await js(f"[...document.querySelectorAll('#rp-{acc} .seg button')].filter(b=>!b.disabled).map(b=>b.dataset.k)")
                for sc in scen:
                    t = await js(f"(()=>{{window.__bench.rec=true;window.__bench.lt=[];const t0=performance.now();"
                                 f"document.querySelector('#rp-{acc} .seg button[data-k=\"{sc}\"]').click();return performance.now()-t0}})()")
                    await asyncio.sleep(0.3)
                    lt = await js("(()=>{const l=window.__bench.lt;window.__bench.rec=false;return l})()")
                    res["switches"].append({"what": f"{acc} scenario -> {sc}", "sync_ms": round(t, 1),
                                            "longtask_max_ms": round(max(lt), 1) if lt else 0})
                    for sp in ("fit", "1", "4", "8"):
                        await js(f"(()=>{{const p=window.DeckPlayers['{acc}'];"
                                 f"document.querySelector('#rp-{acc} .rp-speed button[data-k=\"{sp}\"]').click();p.play(true)}})()")
                        r = await sample(acc, 60 if sp == "fit" else per_speed, sp == "fit")
                        r.update({"slide": slide, "acc": acc, "scenario": sc, "speed": sp})
                        res["runs"].append(r)
                        print(json.dumps(r))
            # hidden slides pause; returning restarts from 0
            g4slide = reps[0][0]
            await go(g4slide)
            await asyncio.sleep(1.0)
            before = await js("window.DeckPlayers['g4'].pos")
            await go(1)
            st = await js("Object.fromEntries(Object.entries(window.DeckPlayers).map(([k,p])=>[k,p.playing]))")
            await asyncio.sleep(0.5)
            await go(g4slide)
            after = await js("[window.DeckPlayers['g4'].pos, window.DeckPlayers['g4'].playing]")
            res["visibility"] = {"pos_before_leaving": round(before, 2), "playing_on_slide_1": st,
                                 "pos_after_return_0_4s": round(after[0], 2), "playing_after_return": after[1]}
            # screenshot sequence: G4 scale-out at Fit
            seq = out / "replay_seq"
            seq.mkdir(exist_ok=True)
            await js("(()=>{const p=window.DeckPlayers['g4'];document.querySelector('#rp-g4 .seg button[data-k=\"gpu-scale-out\"]').click();"
                     "document.querySelector('#rp-g4 .rp-speed button[data-k=\"fit\"]').click();p.play(true)})()")
            for k in range(8):
                r = await cmd("Page.captureScreenshot", format="png",
                              clip={"x": 1196, "y": 306, "width": 608, "height": 680, "scale": 1})
                (seq / f"g4-scale-out-{k:02d}s.png").write_bytes(base64.b64decode(r["data"]))
                await asyncio.sleep(1.0)
    finally:
        chrome.terminate()
        shutil.rmtree(prof, ignore_errors=True)
    runs = res["runs"]
    allp95 = [r["ft_p95_ms"] for r in runs if r["ft_p95_ms"]]
    res["summary"] = {
        "ft_p50_ms_median": statistics.median([r["ft_p50_ms"] for r in runs if r["ft_p50_ms"]]),
        "ft_p95_ms_max": max(allp95),
        "longtask_max_ms": max(r["longtask_max_ms"] for r in runs),
        "scrub_stalls_total": sum(r["scrub_stalls"] for r in runs),
        "fit_wall_s": {f'{r["acc"]}/{r["scenario"]}': r["wall_s"] for r in runs if r["speed"] == "fit"},
        "switch_sync_ms_max": max(s["sync_ms"] for s in res["switches"]),
        "switch_longtask_max_ms": max(s["longtask_max_ms"] for s in res["switches"]),
    }
    (out / "bench_replay.json").write_text(json.dumps(res, indent=1))
    print(json.dumps(res["summary"], indent=1))
    print(json.dumps(res["visibility"]))


asyncio.run(main())
