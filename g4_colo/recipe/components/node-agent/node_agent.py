"""colo-node-agent: per-node CPU ownership and utilisation view.

GET /cpus    {"node", "ncpu", "cpu_manager_state", "busy": [% per CPU over the
              last ~2 s], "mem_used_gib", "mem_total_gib", "sample_window_s"}
GET /healthz {"ok": true}

Reads (read-only hostPath mounts):
  HOST_PROC     (default /host/proc)                 stat, meminfo
  KUBELET_DIR   (default /host/var/lib/kubelet)      cpu_manager_state
No hostPID needed: /proc/stat and /proc/meminfo are host-global.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST_PROC = os.environ.get("HOST_PROC", "/host/proc")
KUBELET_DIR = os.environ.get("KUBELET_DIR", "/host/var/lib/kubelet")
NODE_NAME = os.environ.get("NODE_NAME", os.uname().nodename)
PORT = int(os.environ.get("PORT", "8080"))
SAMPLE_INTERVAL_S = float(os.environ.get("SAMPLE_INTERVAL_S", "0.5"))
WINDOW_S = float(os.environ.get("WINDOW_S", "2"))


def parse_proc_stat(text: str) -> dict[int, tuple[int, int]]:
    """Return {cpu_id: (busy_jiffies, total_jiffies)} for each cpuN line.

    idle = idle + iowait; steal and guest are excluded from busy (guest is
    already counted in user/nice).
    """
    out: dict[int, tuple[int, int]] = {}
    for line in text.splitlines():
        if not line.startswith("cpu") or line.startswith("cpu "):
            continue
        f = line.split()
        name = f[0]
        if not name[3:].isdigit():
            continue
        v = [int(x) for x in f[1:]] + [0] * 10
        user, nice, system, idle, iowait, irq, softirq, steal = v[:8]
        idle_all = idle + iowait
        busy = user + nice + system + irq + softirq
        out[int(name[3:])] = (busy, busy + idle_all + steal)
    return out


def busy_pct(a: dict[int, tuple[int, int]], b: dict[int, tuple[int, int]]) -> list[float]:
    """Per-CPU busy % between two parse_proc_stat samples, indexed by CPU id."""
    if not b:
        return []
    n = max(b) + 1
    out = [0.0] * n
    for cpu, (busy1, tot1) in b.items():
        busy0, tot0 = a.get(cpu, (busy1, tot1))
        dt = tot1 - tot0
        out[cpu] = round(100.0 * (busy1 - busy0) / dt, 1) if dt > 0 else 0.0
    return out


def parse_meminfo(text: str) -> dict[str, int]:
    """Return meminfo fields in kB."""
    out = {}
    for line in text.splitlines():
        k, _, rest = line.partition(":")
        parts = rest.split()
        if parts and parts[0].isdigit():
            out[k.strip()] = int(parts[0])
    return out


def mem_used_gib(info: dict[str, int]) -> tuple[float, float]:
    total = info.get("MemTotal", 0)
    avail = info.get("MemAvailable", info.get("MemFree", 0))
    gib = 1024 * 1024
    return round((total - avail) / gib, 2), round(total / gib, 2)


def read(path: str) -> str:
    with open(path) as f:
        return f.read()


class Sampler(threading.Thread):
    """Samples /proc/stat every SAMPLE_INTERVAL_S and keeps WINDOW_S of history."""

    def __init__(self) -> None:
        super().__init__(daemon=True)
        maxlen = int(WINDOW_S / SAMPLE_INTERVAL_S) + 2
        self.samples: deque = deque(maxlen=maxlen)
        self.lock = threading.Lock()
        self.error = ""

    def run(self) -> None:
        while True:
            try:
                s = parse_proc_stat(read(os.path.join(HOST_PROC, "stat")))
                with self.lock:
                    self.samples.append((time.monotonic(), s))
                self.error = ""
            except OSError as e:
                self.error = str(e)
            time.sleep(SAMPLE_INTERVAL_S)

    def busy(self) -> tuple[list[float], float]:
        with self.lock:
            if len(self.samples) < 2:
                return [], 0.0
            t1, b = self.samples[-1]
            # oldest sample at least WINDOW_S old, else the oldest we have
            t0, a = self.samples[0]
            for t, s in self.samples:
                if t1 - t >= WINDOW_S:
                    t0, a = t, s
            return busy_pct(a, b), round(t1 - t0, 2)


SAMPLER = Sampler()


def cpus_payload() -> dict:
    try:
        cms = json.loads(read(os.path.join(KUBELET_DIR, "cpu_manager_state")))
    except (OSError, ValueError) as e:
        cms = {"error": str(e)}
    busy, window = SAMPLER.busy()
    try:
        used, total = mem_used_gib(parse_meminfo(read(os.path.join(HOST_PROC, "meminfo"))))
    except OSError:
        used, total = None, None
    ncpu = len(busy)
    if not ncpu:
        try:
            ncpu = len(parse_proc_stat(read(os.path.join(HOST_PROC, "stat"))))
        except OSError:
            ncpu = os.cpu_count() or 0
    return {
        "node": NODE_NAME,
        "ncpu": ncpu,
        "cpu_manager_state": cms,
        "busy": busy,
        "sample_window_s": window,
        "mem_used_gib": used,
        "mem_total_gib": total,
        "time": time.time(),
        "error": SAMPLER.error or None,
    }


class Handler(BaseHTTPRequestHandler):
    def _json(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/cpus":
            self._json(200, cpus_payload())
        elif path in ("/healthz", "/readyz"):
            self._json(200, {"ok": True})
        else:
            self._json(404, {"error": "not found"})

    def log_message(self, *_args) -> None:
        pass


def main() -> None:
    SAMPLER.start()
    srv = ThreadingHTTPServer(("", PORT), Handler)
    srv.daemon_threads = True
    print(f"colo-node-agent node={NODE_NAME} proc={HOST_PROC} kubelet={KUBELET_DIR} :{PORT}", flush=True)
    import signal

    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=srv.shutdown).start())
    srv.serve_forever()


if __name__ == "__main__":
    main()
