"""colo-worker: synthetic CPU services for the G4 colo-at-scale recipe.

MODE selects the workload:
  sandbox    integer-type. Generates small Python "tool" snippets (text parsing,
             sorting lists of dicts, regex extraction, json round-trips, an
             in-memory sqlite query) and runs each in a fresh `python -c`
             subprocess with a timeout. Emulates agentic sandbox tool execution.
  toolcall   integer-type. Builds a 20-50 KB JSON request, parses it, runs
             regex + sha256 + zlib round-trips and builds a JSON response.
  analytics  memory-bandwidth heavy. numpy 3D 7-point stencil sweeps over
             float64 arrays (ARRAY_MB per process, default 1500 MB total working
             set), in the spirit of SPEC lbm / fotonik3d.

NPROC worker processes are spawned (default: CPUs in the cgroup cpuset).
Prometheus metrics are served on METRICS_PORT (default 9100) at /metrics:
  colo_worker_units_total{mode}  completed work units (counter)
  colo_worker_info{mode,nproc,...}
SIGTERM/SIGINT stop all workers; the process exits within ~5 s.
"""

from __future__ import annotations

import json
import math
import multiprocessing as mp
import os
import random
import re
import signal
import sqlite3  # noqa: F401  (imported so a missing module fails at startup)
import subprocess
import sys
import time
import hashlib
import zlib

MODES = ("sandbox", "toolcall", "analytics")

WORDS = (
    "alpha beta gamma delta order invoice customer shipment refund account "
    "status pending active closed region north south east west price total "
    "quantity item vendor ticket priority low high medium latency error ok "
    "retry timeout queue batch stream token model agent tool result value"
).split()


# --------------------------------------------------------------------------
# CPU detection
# --------------------------------------------------------------------------
def parse_cpuset(text: str) -> list[int]:
    """Parse a cpuset list like '0-3,8,10-11' into a sorted list of CPU ids."""
    cpus: set[int] = set()
    for part in text.strip().split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, hi = part.split("-", 1)
            cpus.update(range(int(lo), int(hi) + 1))
        else:
            cpus.add(int(part))
    return sorted(cpus)


def parse_cpu_max(text: str) -> int | None:
    """Parse cgroup v2 cpu.max ('max 100000' or '200000 100000') -> CPUs (ceil)."""
    parts = text.split()
    if len(parts) != 2 or parts[0] == "max":
        return None
    quota, period = int(parts[0]), int(parts[1])
    if quota <= 0 or period <= 0:
        return None
    return max(1, math.ceil(quota / period))


def detect_cpus(cgroup_root: str = "/sys/fs/cgroup") -> int:
    """CPUs available to this container.

    Primary source is cpuset.cpus.effective (exclusive CPUs under the static
    CPU manager). If the cpuset is the whole machine but a CFS quota is set
    (non-Guaranteed pod), the quota is used as an upper bound.
    """
    n = None
    try:
        with open(os.path.join(cgroup_root, "cpuset.cpus.effective")) as f:
            cpus = parse_cpuset(f.read())
            if cpus:
                n = len(cpus)
    except (OSError, ValueError):
        pass
    if n is None:
        n = os.cpu_count() or 1
    try:
        with open(os.path.join(cgroup_root, "cpu.max")) as f:
            q = parse_cpu_max(f.read())
            if q is not None:
                n = min(n, q)
    except (OSError, ValueError):
        pass
    return max(1, n)


# --------------------------------------------------------------------------
# sandbox mode
# --------------------------------------------------------------------------
def _rand_words(rng: random.Random, n: int) -> list[str]:
    return [rng.choice(WORDS) for _ in range(n)]


def make_snippet(rng: random.Random) -> str:
    """Return a small self-contained Python tool snippet (one of five kinds)."""
    kind = rng.randrange(5)
    seed = rng.randrange(1 << 30)
    n = rng.randint(200, 2000)
    if kind == 0:  # text parsing
        return (
            "import random,collections\n"
            f"r=random.Random({seed})\n"
            f"W={WORDS!r}\n"
            f"text='\\n'.join(' '.join(r.choice(W) for _ in range(12)) for _ in range({n}))\n"
            "c=collections.Counter(w for line in text.splitlines() for w in line.split())\n"
            "print(c.most_common(5))\n"
        )
    if kind == 1:  # sort list of dicts
        return (
            "import random\n"
            f"r=random.Random({seed})\n"
            f"rows=[{{'id':i,'price':r.random()*100,'qty':r.randint(1,50),'region':r.choice('NSEW')}} for i in range({n * 4})]\n"
            "rows.sort(key=lambda d:(d['region'],-d['price']*d['qty']))\n"
            "print(rows[0]['id'],sum(d['qty'] for d in rows))\n"
        )
    if kind == 2:  # regex extraction
        return (
            "import random,re\n"
            f"r=random.Random({seed})\n"
            "lines=[f'order-{r.randint(1000,99999)} user{r.randint(1,999)}@example.com "
            "amount={r.random()*500:.2f} ts=2026-0{r.randint(1,9)}-1{r.randint(0,9)}' "
            f"for _ in range({n})]\n"
            "rx=re.compile(r'order-(\\d+) (\\S+@\\S+) amount=([\\d.]+) ts=(\\S+)')\n"
            "m=[rx.match(l).groups() for l in lines]\n"
            "print(len(m),max(float(x[2]) for x in m))\n"
        )
    if kind == 3:  # json round-trip
        return (
            "import json,random\n"
            f"r=random.Random({seed})\n"
            f"doc={{'items':[{{'k':str(i),'v':[r.random() for _ in range(8)],'tags':['a','b','c']}} for i in range({n})]}}\n"
            "for _ in range(5):\n"
            "    doc=json.loads(json.dumps(doc))\n"
            "print(len(doc['items']))\n"
        )
    # in-memory sqlite query
    return (
        "import random,sqlite3\n"
        f"r=random.Random({seed})\n"
        "db=sqlite3.connect(':memory:')\n"
        "db.execute('create table t(id integer primary key,cust integer,amt real,region text)')\n"
        f"db.executemany('insert into t values(?,?,?,?)',[(i,r.randint(1,200),r.random()*100,r.choice('NSEW')) for i in range({n * 2})])\n"
        "print(db.execute('select region,count(*),sum(amt) from t where amt>10 group by region order by 3 desc').fetchall())\n"
        "print(db.execute('select cust,avg(amt) from t group by cust order by 2 desc limit 3').fetchall())\n"
    )


def sandbox_unit(rng: random.Random, timeout: float = 10.0) -> bool:
    code = make_snippet(rng)
    try:
        p = subprocess.run(
            [sys.executable, "-I", "-S", "-c", code],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
        )
        return p.returncode == 0
    except subprocess.TimeoutExpired:
        return False


# --------------------------------------------------------------------------
# toolcall mode
# --------------------------------------------------------------------------
_EMAIL_RX = re.compile(r"[\w.]+@[\w.]+")
_NUM_RX = re.compile(r"\b\d{3,}\b")


def toolcall_unit(rng: random.Random) -> int:
    """One request/response cycle. Returns the response size in bytes."""
    target = rng.randint(20_000, 50_000)
    msgs = []
    size = 0
    i = 0
    while size < target:
        text = " ".join(_rand_words(rng, rng.randint(10, 40)))
        m = {
            "role": rng.choice(("user", "assistant", "tool")),
            "id": i,
            "content": f"{text} contact user{rng.randint(1, 9999)}@example.com ref {rng.randint(100, 999999)}",
            "meta": {"ts": time.time(), "score": rng.random()},
        }
        msgs.append(m)
        size += len(m["content"]) + 60
        i += 1
    req = json.dumps({"tool": "lookup", "args": {"q": rng.choice(WORDS)}, "messages": msgs})
    parsed = json.loads(req)
    emails = 0
    nums = 0
    h = hashlib.sha256()
    for m in parsed["messages"]:
        c = m["content"]
        emails += len(_EMAIL_RX.findall(c))
        nums += len(_NUM_RX.findall(c))
        h.update(c.encode())
    blob = req.encode()
    comp = zlib.compress(blob, 6)
    if zlib.decompress(comp) != blob:
        raise RuntimeError("zlib round-trip mismatch")
    resp = json.dumps(
        {
            "digest": h.hexdigest(),
            "emails": emails,
            "numbers": nums,
            "ratio": len(comp) / len(blob),
            "n": len(parsed["messages"]),
        }
    )
    return len(resp)


# --------------------------------------------------------------------------
# analytics mode
# --------------------------------------------------------------------------
def stencil_dims(array_mb: float) -> int:
    """Edge length n of the two n^3 float64 grids that fill array_mb total."""
    per_array = array_mb * 1024 * 1024 / 2
    return max(16, int((per_array / 8) ** (1 / 3)))


class Stencil:
    """Double-buffered 3D 7-point Jacobi stencil over flat, contiguous views.

    The grids are n^3 float64 stored flat; neighbours are the +-1, +-n, +-n^2
    offsets (boundaries wrap, harmless for a synthetic load). Each sweep is 8
    in-place full-array numpy passes with no temporaries, so every pass
    streams the whole grid through DRAM with ~1 flop per 16-24 bytes moved:
    memory-bandwidth bound like lbm / fotonik3d. Measured on the dev box: a
    1500 MB grid pair runs ~2x slower per element than a cache-resident one.
    """

    C0 = 0.4  # centre weight
    C1 = 0.1  # neighbour weight (C0 + 6*C1 == 1 keeps values bounded)

    def __init__(self, n: int, seed: int = 0):
        import numpy as np

        self.np = np
        self.n = n
        rng = np.random.default_rng(seed)
        self.a = rng.random(n * n * n)
        self.b = self.a.copy()  # touches every page; boundary cells stay defined

    def sweep(self) -> None:
        np, a, b, n = self.np, self.a, self.b, self.n
        s = n * n + n + 1
        e = a.size - s
        out = b[s:e]
        np.multiply(a[s:e], self.C0 / self.C1, out=out)
        for off in (1, n, n * n):
            np.add(out, a[s - off:e - off], out=out)
            np.add(out, a[s + off:e + off], out=out)
        np.multiply(out, self.C1, out=out)
        self.a, self.b = b, a


# --------------------------------------------------------------------------
# worker processes
# --------------------------------------------------------------------------
def worker_main(mode: str, idx: int, counter, stop) -> None:
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    term = {"flag": False}  # plain flag: no mp locks inside a signal handler
    signal.signal(signal.SIGTERM, lambda *_: term.__setitem__("flag", True))
    rng = random.Random(os.getpid() ^ int(time.time() * 1e6))
    if mode == "analytics":
        array_mb = float(os.environ.get("ARRAY_MB", "1500"))
        st = Stencil(stencil_dims(array_mb), seed=idx)
        unit = st.sweep
    elif mode == "sandbox":
        timeout = float(os.environ.get("SANDBOX_TIMEOUT_S", "10"))
        unit = lambda: sandbox_unit(rng, timeout)  # noqa: E731
    else:
        unit = lambda: toolcall_unit(rng)  # noqa: E731
    while not term["flag"] and not stop.is_set():
        unit()
        with counter.get_lock():
            counter.value += 1


def serve_metrics(mode: str, nproc: int, counters, port: int) -> None:
    from prometheus_client import REGISTRY, start_http_server
    from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily

    class UnitsCollector:
        def collect(self):
            c = CounterMetricFamily(
                "colo_worker_units", "Completed work units", labels=["mode"]
            )
            c.add_metric([mode], float(sum(x.value for x in counters)))
            yield c
            g = GaugeMetricFamily(
                "colo_worker_info",
                "Worker configuration",
                labels=["mode", "nproc", "array_mb", "pod", "node"],
            )
            g.add_metric(
                [
                    mode,
                    str(nproc),
                    os.environ.get("ARRAY_MB", "1500") if mode == "analytics" else "",
                    os.environ.get("POD_NAME", os.uname().nodename),
                    os.environ.get("NODE_NAME", ""),
                ],
                1.0,
            )
            yield g
            p = GaugeMetricFamily("colo_worker_processes", "Live worker processes")
            p.add_metric([], float(nproc))
            yield p

    REGISTRY.register(UnitsCollector())
    start_http_server(port)


def main() -> int:
    mode = os.environ.get("MODE", "toolcall").strip().lower()
    if mode not in MODES:
        print(f"MODE must be one of {MODES}, got {mode!r}", file=sys.stderr)
        return 2
    nproc = int(os.environ.get("NPROC", "0") or 0) or detect_cpus()
    port = int(os.environ.get("METRICS_PORT", "9100"))

    ctx = mp.get_context("fork")
    stop = ctx.Event()
    counters = [ctx.Value("Q", 0) for _ in range(nproc)]
    procs = [
        ctx.Process(target=worker_main, args=(mode, i, counters[i], stop), daemon=True)
        for i in range(nproc)
    ]
    for p in procs:
        p.start()
    serve_metrics(mode, nproc, counters, port)
    print(f"colo-worker mode={mode} nproc={nproc} metrics=:{port}", flush=True)

    # The handler only flips a plain flag: calling mp.Event.set() from a signal
    # handler while the main thread sits in mp.Event.wait() can deadlock.
    shutdown = {"flag": False}

    def _shutdown(*_):
        shutdown["flag"] = True

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    last, last_t = 0, time.monotonic()
    while not shutdown["flag"]:
        time.sleep(0.2)
        if any(not p.is_alive() for p in procs):
            print("a worker process died; exiting", file=sys.stderr, flush=True)
            break
        now = time.monotonic()
        if now - last_t >= 30:
            total = sum(c.value for c in counters)
            print(f"units/s={(total - last) / (now - last_t):.2f}", flush=True)
            last, last_t = total, now
    stop.set()

    # Graceful stop: workers finish their current unit; analytics sweeps or
    # sandbox subprocesses may take a few seconds, so hard-stop at ~4 s.
    deadline = time.monotonic() + 4.0
    for p in procs:
        p.join(timeout=max(0.0, deadline - time.monotonic()))
    for p in procs:
        if p.is_alive():
            p.kill()
    for p in procs:
        p.join(timeout=0.5)
    return 0


if __name__ == "__main__":
    sys.exit(main())
