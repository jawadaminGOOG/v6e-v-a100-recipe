"""Cut-off text audit: report every visible element whose text does not fit its box.

    python tools/cutoff.py "http://localhost:8088/?cluster=substrate" 1366x768 [--open] [--shot out.png] [--wait 8]

Checks, on the rendered page (headless Chrome via CDP, like tools/shot.py):
  * any element with visible text whose overflow is not `visible` (hidden / clip / auto / scroll), or that uses
    text-overflow: ellipsis or -webkit-line-clamp, and whose content is wider or taller than its box
    (scrollWidth > clientWidth or scrollHeight > clientHeight, 1 px tolerance);
  * any text node that spills past the box of its nearest clipping ancestor (catches text cut by a parent);
  * SVG <text> whose bounding box leaves its <svg> viewport.
`--open [all|drawers|rail]` opens <details> first (History, events, "How the numbers are computed"; the rail's
"Presets, scenarios, numbers"). Visually-hidden .sr-only helpers and scrolling containers (overflow auto/scroll) are not
cut-offs. Prints one line per
finding (selector · text) and exits 2 if anything is cut, so it can gate a change.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import subprocess
import sys
import tempfile
import time
import urllib.request

import websockets

AUDIT_JS = r"""
(() => {
  // Visually-hidden helpers (.sr-only: 1 px clip for screen readers) are clipped on purpose.
  const srOnly = (e) => !!e.closest('.sr-only');
  // Content of a closed <details> (other than its <summary>) is not rendered for the reader.
  const inClosed = (e) => { const d = e.closest('details:not([open])'); return !!d && !e.closest('summary'); };
  const vis = (e) => { const s = getComputedStyle(e); return e.getClientRects().length && s.visibility !== 'hidden' && s.display !== 'none' && !srOnly(e) && !inClosed(e); };
  const sel = (e) => {
    const part = (x) => x.tagName.toLowerCase() + (x.id ? '#' + x.id : '') +
      (typeof x.className === 'string' && x.className.trim() ? '.' + x.className.trim().split(/\s+/).slice(0, 2).join('.') : '');
    const out = []; let x = e;
    for (let i = 0; x && x.nodeType === 1 && i < 3; i++, x = x.parentElement) { out.unshift(part(x)); if (x.id) break; }
    return out.join(' > ');
  };
  const txt = (e) => (e.innerText || e.textContent || '').replace(/\s+/g, ' ').trim();
  // hidden / clip cut content; auto / scroll keep it reachable (a scrolling list is not a cut-off).
  const clips = (s) => ['hidden', 'clip'].includes(s.overflowX) || ['hidden', 'clip'].includes(s.overflowY);
  const scrolls = (s) => ['auto', 'scroll'].includes(s.overflowX) || ['auto', 'scroll'].includes(s.overflowY);
  const found = new Map();
  const add = (e, why, extra) => { const k = sel(e) + '|' + why; if (!found.has(k)) found.set(k, { sel: sel(e), why, text: txt(e).slice(0, 120), ...extra }); };
  // 1. boxes that clip or ellipsize and whose content overflows them
  for (const e of document.querySelectorAll('body *')) {
    if (!vis(e) || e.closest('svg') || !txt(e)) continue;
    const s = getComputedStyle(e);
    const ell = s.textOverflow === 'ellipsis' || (s.webkitLineClamp && s.webkitLineClamp !== 'none');
    if (!clips(s) && !ell) continue;
    // Per axis: an axis that scrolls (auto/scroll) keeps its content reachable; only hidden/clip (or ellipsis) cuts.
    const cutX = ell || ['hidden', 'clip'].includes(s.overflowX), cutY = ell || ['hidden', 'clip'].includes(s.overflowY);
    const w = cutX ? e.scrollWidth - e.clientWidth : 0, h = cutY ? e.scrollHeight - e.clientHeight : 0;
    if (w > 1 || h > 1) add(e, ell ? 'ellipsis' : 'overflow', { over_w: w, over_h: h });
  }
  // 2. text nodes that spill past their nearest clipping ancestor
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let t = walker.nextNode(); t; t = walker.nextNode()) {
    const p = t.parentElement; if (!p || !t.nodeValue.trim() || !vis(p) || p.closest('svg')) continue;
    let a = p; while (a && a !== document.body && !clips(getComputedStyle(a)) && !scrolls(getComputedStyle(a))) a = a.parentElement;
    if (!a || a === document.body || scrolls(getComputedStyle(a))) continue;
    const r = document.createRange(); r.selectNodeContents(t);
    const tb = r.getBoundingClientRect(), ab = a.getBoundingClientRect();
    if (!tb.width) continue;
    const out = Math.max(ab.left - tb.left, tb.right - ab.right, ab.top - tb.top, tb.bottom - ab.bottom);
    if (out > 1.5) add(p, 'cut by ' + sel(a), { out: Math.round(out) });
  }
  // 3. SVG text outside its svg
  for (const t of document.querySelectorAll('svg text')) {
    const svg = t.ownerSVGElement; if (!svg || !vis(svg)) continue;
    const tb = t.getBoundingClientRect(), sb = svg.getBoundingClientRect();
    if (tb.right - sb.right > 1 || sb.left - tb.left > 1 || tb.bottom - sb.bottom > 1 || sb.top - tb.top > 1) add(t, 'svg text outside svg', {});
  }
  return JSON.stringify([...found.values()]);
})()
"""


async def run(url: str, size: str, open_details: bool, shot: str | None, wait_s: float, port: int) -> list[dict]:
    zoom = 1.0
    if "@" in size:
        size, z = size.split("@", 1)
        zoom = float(z)
    w, h = (int(x) for x in size.lower().split("x"))
    prof = tempfile.mkdtemp(prefix="demo-cutoff-")
    proc = subprocess.Popen(
        ["google-chrome", "--headless=new", "--no-sandbox", "--disable-gpu", "--hide-scrollbars",
         f"--remote-debugging-port={port}", f"--user-data-dir={prof}", f"--window-size={w},{h}", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json"))
                page = next(t for t in tabs if t.get("type") == "page")
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.3)
        async with websockets.connect(page["webSocketDebuggerUrl"], max_size=50_000_000) as ws:
            n = [0]

            async def cdp(method, params=None):
                n[0] += 1
                await ws.send(json.dumps({"id": n[0], "method": method, "params": params or {}}))
                while True:
                    msg = json.loads(await ws.recv())
                    if msg.get("id") == n[0]:
                        return msg.get("result", {})

            await cdp("Emulation.setDeviceMetricsOverride", {"width": round(w / zoom), "height": round(h / zoom),
                                                             "deviceScaleFactor": zoom, "mobile": False})
            await cdp("Page.navigate", {"url": url})
            await asyncio.sleep(wait_s)
            if open_details:
                q = {"all": "details", "drawers": ".details > details", "rail": "details.more"}[open_details]
                await cdp("Runtime.evaluate", {"expression": f"document.querySelectorAll('{q}').forEach((d) => d.open = true)"})
                await asyncio.sleep(1.5)
            r = await cdp("Runtime.evaluate", {"expression": AUDIT_JS, "returnByValue": True})
            found = json.loads(r["result"]["value"])
            if shot:
                s = await cdp("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": bool(open_details)})
                if "data" not in s:
                    raise RuntimeError(f"screenshot failed: {s}")
                with open(shot, "wb") as f:
                    f.write(base64.b64decode(s["data"]))
        return found
    finally:
        proc.terminate()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url")
    ap.add_argument("size", help="WxH or WxH@zoom, e.g. 1366x768 or 1920x1080@1.35")
    ap.add_argument("--open", nargs="?", const="all", choices=["all", "drawers", "rail"],
                    help="open <details> first: all, the bottom drawers, or the rail's 'Presets, scenarios, numbers'")
    ap.add_argument("--shot", help="also save a screenshot here")
    ap.add_argument("--wait", type=float, default=8)
    ap.add_argument("--port", type=int, default=9334, help="Chrome remote-debugging port")
    a = ap.parse_args()
    found = asyncio.run(run(a.url, a.size, a.open, a.shot, a.wait, a.port))
    print(f"{a.size}{' open=' + a.open if a.open else ''} {a.url}: {len(found)} cut-off(s)")
    for f in found:
        extra = {k: v for k, v in f.items() if k not in ("sel", "why", "text")}
        print(f"  - [{f['why']}] {f['sel']} · \"{f['text']}\" {extra if extra else ''}")
    return 2 if found else 0


if __name__ == "__main__":
    sys.exit(main())
