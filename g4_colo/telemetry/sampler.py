#!/usr/bin/env python3
"""1-second node sampler, run inside the privileged hostPID telemetry pod.

Writes one CSV row per second with: per-CPU busy% / steal%, node memory used and
page cache, and GPU util / power / SM clock / mem used / temperature / throttle
reasons. Usage: sampler.py OUT.csv DURATION_S
"""
import csv
import subprocess
import sys
import time

out, dur = sys.argv[1], float(sys.argv[2])
NV = "/usr/local/nvidia/bin/nvidia-smi"


def cpu_times():
    rows = {}
    with open("/proc/stat") as f:
        for line in f:
            if line.startswith("cpu") and line[3].isdigit():
                p = line.split()
                v = list(map(int, p[1:9]))  # user nice system idle iowait irq softirq steal
                rows[int(p[0][3:])] = v
    return rows


def meminfo():
    m = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, v = line.split(":")
            m[k] = int(v.split()[0])
    return m


def gpu():
    try:
        q = "utilization.gpu,power.draw,clocks.sm,memory.used,temperature.gpu,clocks_event_reasons.active"
        r = subprocess.run([NV, f"--query-gpu={q}", "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=2).stdout.strip().split(", ")
        return r
    except Exception:  # noqa: BLE001
        return [""] * 6


prev = cpu_times()
ncpu = len(prev)
hdr = ["t"] + [f"busy{c}" for c in range(ncpu)] + [f"steal{c}" for c in range(ncpu)] + \
      ["mem_used_gib", "page_cache_gib", "gpu_util", "gpu_power_w", "gpu_sm_mhz", "gpu_mem_mib", "gpu_temp_c", "gpu_throttle"]
t_end = time.time() + dur
with open(out, "w", newline="") as fh:
    w = csv.writer(fh)
    w.writerow(hdr)
    nxt = time.time() + 1
    while time.time() < t_end:
        time.sleep(max(0, nxt - time.time()))
        nxt += 1
        cur = cpu_times()
        busy, steal = [], []
        for c in range(ncpu):
            d = [a - b for a, b in zip(cur[c], prev[c])]
            tot = sum(d) or 1
            busy.append(round(100 * (tot - d[3] - d[4]) / tot, 1))
            steal.append(round(100 * d[7] / tot, 2))
        prev = cur
        m = meminfo()
        used = (m["MemTotal"] - m["MemAvailable"]) / 2**20
        cache = m.get("Cached", 0) / 2**20
        w.writerow([round(time.time(), 3)] + busy + steal + [round(used, 2), round(cache, 2)] + gpu())
        fh.flush()
