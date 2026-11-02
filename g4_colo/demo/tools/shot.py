"""Screenshot helper for mock-mode testing: keeps the page open for N seconds (so the
client-side charts accumulate history) and then captures a screenshot via CDP.

    python shot.py http://localhost:8088/ /tmp/demo_mock.png 150
    python shot.py "http://localhost:8091/?cluster=native" /tmp/native_1440.png 12 1440x900

With a WxH argument the viewport is set to exactly that size, only the viewport is captured, and the
one-pane-of-glass check is printed: page scroll size vs viewport (exit code 2 if the page would scroll).
"""

import asyncio
import base64
import json
import subprocess
import sys
import tempfile
import time
import urllib.request

import websockets


async def main(url: str, out: str, wait_s: float, size: str | None = None) -> int:
    prof = tempfile.mkdtemp(prefix="demo-shot-")
    w, h = (int(x) for x in size.lower().split("x")) if size else (1700, 2300)
    proc = subprocess.Popen(
        ["google-chrome", "--headless=new", "--no-sandbox", "--disable-gpu", "--hide-scrollbars",
         "--remote-debugging-port=9333", f"--user-data-dir={prof}", f"--window-size={w},{h}", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    rc = 0
    try:
        for _ in range(50):
            try:
                tabs = json.load(urllib.request.urlopen("http://127.0.0.1:9333/json"))
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

            if size:
                await cdp("Emulation.setDeviceMetricsOverride", {"width": w, "height": h, "deviceScaleFactor": 1, "mobile": False})
            await cdp("Page.navigate", {"url": url})
            await asyncio.sleep(wait_s)
            if size:
                r = await cdp("Runtime.evaluate", {"expression": "JSON.stringify({sw: document.documentElement.scrollWidth, sh: document.documentElement.scrollHeight, iw: innerWidth, ih: innerHeight, cluster: (new URLSearchParams(location.search)).get('cluster')})", "returnByValue": True})
                m = json.loads(r["result"]["value"])
                scroll = m["sh"] > m["ih"] or m["sw"] > m["iw"]
                print(f"{size}: scroll {m['sw']}x{m['sh']} viewport {m['iw']}x{m['ih']} cluster={m['cluster']} -> {'SCROLLS' if scroll else 'no scroll'}")
                rc = 2 if scroll else 0
            shot = await cdp("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": not size})
        with open(out, "wb") as f:
            f.write(base64.b64decode(shot["data"]))
        print("wrote", out)
    finally:
        proc.terminate()
    return rc


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1], sys.argv[2], float(sys.argv[3]) if len(sys.argv) > 3 else 10,
                              sys.argv[4] if len(sys.argv) > 4 else None)))
