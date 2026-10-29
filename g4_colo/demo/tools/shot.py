"""Screenshot helper for mock-mode testing: keeps the page open for N seconds (so the
client-side charts accumulate history) and then captures a full screenshot via CDP.

    python shot.py http://localhost:8088/ /tmp/demo_mock.png 150
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


async def main(url: str, out: str, wait_s: float):
    prof = tempfile.mkdtemp(prefix="demo-shot-")
    proc = subprocess.Popen(
        ["google-chrome", "--headless=new", "--no-sandbox", "--disable-gpu", "--hide-scrollbars",
         "--remote-debugging-port=9333", f"--user-data-dir={prof}", "--window-size=1700,2300", url],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                tabs = json.load(urllib.request.urlopen("http://127.0.0.1:9333/json"))
                page = next(t for t in tabs if t.get("type") == "page")
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.3)
        await asyncio.sleep(wait_s)
        async with websockets.connect(page["webSocketDebuggerUrl"], max_size=50_000_000) as ws:
            await ws.send(json.dumps({"id": 1, "method": "Page.captureScreenshot", "params": {"format": "png", "captureBeyondViewport": True}}))
            while True:
                msg = json.loads(await ws.recv())
                if msg.get("id") == 1:
                    break
        with open(out, "wb") as f:
            f.write(base64.b64decode(msg["result"]["data"]))
        print("wrote", out)
    finally:
        proc.terminate()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2], float(sys.argv[3]) if len(sys.argv) > 3 else 10))
