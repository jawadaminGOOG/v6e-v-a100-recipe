#!/usr/bin/env python3
"""Export static key frames for the Google Slides version (headless Chrome over CDP, 2x pixel density).

    python3 g4_colo/deck/export_frames.py          # needs google-chrome and the `websockets` package

Writes g4_colo/deck/keyframes/:
  slide-N.png                    every slide, final static frame (step slides at their last step, replays at their key frame)
  flow-step0..3.png              "How it works" diagram at each step (step 3 = final frame)
  hosts.png, components.png      "Inside the host" and appendix diagrams as single images
  substrate.png                  Agent Substrate diagram (flow + core maps) as a single image
  substrate-timeline.png         Agent Substrate: G4 scale-in timeline (slide 12)
  substrate-resume-cdf.png, substrate-busy.png, substrate-coremap.png   slide 13 charts
  replay-<g4|v6e>-<scenario>.png each replay panel at its key frame (busiest moment)
Charts are already single files in charts/ (SVG + PNG) from deck_charts.py.
"""
import asyncio
import base64
import json
import shutil
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

import websockets

DECK = Path(__file__).resolve().parent
OUT = DECK / "keyframes"
URL = f"file://{DECK / 'index.html'}?shot=1"
PORT = 9341


async def main():
    OUT.mkdir(exist_ok=True)
    prof = tempfile.mkdtemp(prefix="deckframes-")
    chrome = subprocess.Popen(["google-chrome", "--headless=new", f"--remote-debugging-port={PORT}", f"--user-data-dir={prof}",
                               "--no-sandbox", "--hide-scrollbars", "--window-size=1920,1080", "about:blank"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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

            async def shot(name, clip_sel=None):
                params = {"format": "png", "captureBeyondViewport": False}
                if clip_sel:
                    box = await js(f"(()=>{{const r=document.querySelector({json.dumps(clip_sel)}).getBoundingClientRect();"
                                   f"return {{x:r.x,y:r.y,width:r.width,height:r.height}}}})()")
                    pad = 12
                    params["clip"] = {"x": max(0, box["x"] - pad), "y": max(0, box["y"] - pad),
                                      "width": box["width"] + 2 * pad, "height": box["height"] + 2 * pad, "scale": 1}
                r = await cmd("Page.captureScreenshot", **params)
                (OUT / f"{name}.png").write_bytes(base64.b64decode(r["data"]))
                print("wrote", OUT / f"{name}.png")

            await cmd("Emulation.setDeviceMetricsOverride", width=1920, height=1080, deviceScaleFactor=2, mobile=False)
            await cmd("Page.enable")
            await cmd("Page.navigate", url=URL + "#slide=1")
            await asyncio.sleep(1.5)
            await js("document.fonts.ready.then(()=>true)")
            nslides = await js("document.querySelectorAll('.slide').length")
            for i in range(1, nslides + 1):
                await js(f"location.hash='slide={i}'")
                await asyncio.sleep(0.6)
                await shot(f"slide-{i}")
            idx = lambda sel: js(f"[...document.querySelectorAll('.slide')].findIndex(s=>s.matches({json.dumps(sel)}))+1")
            flow = await idx("[data-steps]")
            for k in range(4):
                await js(f"location.hash='slide={flow}&step={k}'")
                await asyncio.sleep(0.6)
                await shot(f"flow-step{k}", ".slide.is-active .flow-wrap")
            hosts = await js("[...document.querySelectorAll('.slide')].findIndex(s=>s.querySelector('#s2svg'))+1")
            await js(f"location.hash='slide={hosts}'"); await asyncio.sleep(0.5)
            await shot("hosts", ".slide.is-active .s2-wrap")
            await js(f"location.hash='slide={nslides}'"); await asyncio.sleep(0.5)
            await shot("components", ".slide.is-active .s3-wrap")
            sub = await js("[...document.querySelectorAll('.slide')].findIndex(s=>s.querySelector('#sbsvg'))+1")
            if sub:
                await js(f"location.hash='slide={sub}'"); await asyncio.sleep(0.5)
                await shot("substrate", ".slide.is-active .sb-wrap")
            for sel, name in (("#tlsvg", "substrate-timeline"), ("#cdfsvg", "substrate-resume-cdf"), ("#busysvg", "substrate-busy"), ("#cmsvg", "substrate-coremap")):
                k = await js(f"[...document.querySelectorAll('.slide')].findIndex(s=>s.querySelector('{sel}'))+1")
                if k:
                    await js(f"location.hash='slide={k}'"); await asyncio.sleep(0.5)
                    await shot(name, f".slide.is-active {sel}")
            # replays at their key frame, one per scenario
            for slide, acc in ((await idx("[data-replay=g4]"), "g4"), (await idx("[data-replay=v6e]"), "v6e")):
                await js(f"location.hash='slide={slide}'"); await asyncio.sleep(0.5)
                for scen in ("cpu-scale-out", "gpu-scale-out", "gpu-scale-in"):
                    ok = await js(f"(()=>{{const b=document.querySelector('#rp-{acc} .seg button[data-k=\"{scen}\"]');"
                                  f"if(!b||b.disabled)return false;b.click();return true}})()")
                    if not ok:
                        continue
                    await asyncio.sleep(0.6)
                    await shot(f"replay-{acc}-{scen}", f"#rp-{acc}")
    finally:
        chrome.terminate()
        shutil.rmtree(prof, ignore_errors=True)


asyncio.run(main())
