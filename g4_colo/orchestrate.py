#!/usr/bin/env python3
"""g4-colo orchestrator. Runs on the workstation and drives the GKE cluster via kubectl.

Cell types
  vllm_solo        vLLM open-loop load only (SPEC pod absent)
  colo@L           vLLM open-loop load + SPEC rate on L vCPUs, same window
  spec_solo@L      SPEC rate on L vCPUs only (vLLM pod alive but idle)

Every cell records: kubelet CPU-manager state (cpusets), 1 s node telemetry,
vLLM /metrics before/after, per-request client CSVs, SPEC result files.

Usage
  orchestrate.py cell --kind colo --level 36 --rep 1
  orchestrate.py matrix --reps 5 [--resume]
"""
from __future__ import annotations

import argparse
import json
import random
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RESULTS = ROOT / "results"
NS = "colo"
CLIENTS = ["client-0", "client-1"]
PROCS_PER_CLIENT = 3
CFG = json.loads((ROOT / "experiment.json").read_text())


def sh(cmd: str, check=True, capture=True, timeout=None) -> str:
    r = subprocess.run(cmd, shell=True, text=True, capture_output=capture, timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError(f"cmd failed ({r.returncode}): {cmd}\n{r.stdout}\n{r.stderr}")
    return (r.stdout or "").strip()


def k(args: str, **kw) -> str:
    return sh(f"kubectl -n {NS} {args}", **kw)


def launch(args: str):
    """Fire-and-forget `kubectl exec` of a detached remote command.

    The remote command is setsid+nohup with all fds redirected, so the remote
    process survives the exec session; the local kubectl is reaped after 5 s.
    """
    p = subprocess.Popen(f"kubectl -n {NS} {args}", shell=True, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        p.wait(timeout=5)
    except subprocess.TimeoutExpired:
        p.kill()


def log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------- SPEC pod
def spec_manifest(name: str, ncpu: int, mem_gi: int, copies: int) -> str:
    benches = (ROOT / "spec" / "benchmarks.txt").read_text().split()
    return f"""
apiVersion: v1
kind: Pod
metadata: {{name: {name}, namespace: {NS}, labels: {{app: spec}}}}
spec:
  restartPolicy: Never
  nodeSelector: {{role: g4colo}}
  tolerations:
  - {{key: role, operator: Equal, value: g4colo, effect: NoSchedule}}
  - {{key: nvidia.com/gpu, operator: Exists, effect: NoSchedule}}
  containers:
  - name: spec
    image: ubuntu:24.04
    resources:
      requests: {{cpu: "{ncpu}", memory: {mem_gi}Gi}}
      limits: {{cpu: "{ncpu}", memory: {mem_gi}Gi}}
    command: ["bash", "-lc"]
    args:
    - |
      set -uo pipefail
      cat /sys/fs/cgroup/cpuset.cpus.effective > /tmp/cpuset
      echo CPUSET $(cat /tmp/cpuset)
      cd /work/spec && source shrc
      echo SPEC_START $(date +%s.%N)
      runcpu --config=g4colo --action=run --size=train --copies={copies} --iterations=1 \\
        --noreportable --deletework --define gcc_dir=/usr/local --output_format=txt,csv \\
        {' '.join(benches)} > /tmp/runcpu.log 2>&1
      rc=$?
      echo SPEC_END $(date +%s.%N) rc=$rc
      grep -E "Running|Success|Error|Run Complete|format: CSV|format: Text" /tmp/runcpu.log | tail -60
      mkdir -p /work/results/{name}
      cp /tmp/runcpu.log /tmp/cpuset /work/results/{name}/
      for f in $(grep -oE "/work/spec/result/CPU2026\\.[0-9]+\\.[a-z]+\\.(train|refrate|test)\\.(csv|txt)" /tmp/runcpu.log | sort -u); do cp $f /work/results/{name}/; done
      ls /work/results/{name}
      echo SPEC_DONE
    volumeMounts:
    - {{name: work, mountPath: /work}}
  volumes:
  - {{name: work, hostPath: {{path: /var/lib/g4colo, type: Directory}}}}
"""


def spec_start(name: str, level: int):
    ncpu = level
    mem = CFG["spec_mem_gi"][str(level)]
    k(f"delete pod {name} --ignore-not-found --wait=true")
    sh(f"kubectl apply -f - <<'EOF'\n{spec_manifest(name, ncpu, mem, copies=level)}\nEOF")
    k(f"wait --for=condition=Ready pod/{name} --timeout=300s")
    # wait until runcpu actually starts
    for _ in range(120):
        if "SPEC_START" in k(f"logs {name}", check=False):
            return
        time.sleep(2)
    raise RuntimeError("SPEC did not start")


def spec_wait(name: str, outdir: Path, timeout_s: int = 7200) -> str:
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        phase = k(f"get pod {name} -o jsonpath={{.status.phase}}", check=False)
        if phase in ("Succeeded", "Failed"):
            break
        time.sleep(15)
    logs = k(f"logs {name}", check=False)
    (outdir / "spec_pod.log").write_text(logs)
    # pull SPEC result files via the telemetry pod (hostPath view)
    files = k(f"exec telemetry -- ls /host/var/lib/g4colo/results/{name}", check=False).split()
    for f in files:
        sh(f"kubectl -n {NS} exec telemetry -- cat /host/var/lib/g4colo/results/{name}/{f} > {outdir / f}")
    k(f"delete pod {name} --ignore-not-found --wait=true")
    return logs


# ---------------------------------------------------------------- telemetry
def cpu_state(outdir: Path, tag: str):
    st = k("exec telemetry -- cat /host/var/lib/kubelet/cpu_manager_state")
    (outdir / f"cpu_manager_state.{tag}.json").write_text(st)
    pods = k("get pods -o wide")
    (outdir / f"pods.{tag}.txt").write_text(pods)


def telemetry_start(cell: str, dur: int):
    launch(f"exec telemetry -- bash -c 'mkdir -p /tmp/tel && setsid nohup python3 /tmp/sampler.py /tmp/tel/{cell}.csv {dur} </dev/null >/dev/null 2>&1 &'")


def telemetry_fetch(cell: str, outdir: Path):
    sh(f"kubectl -n {NS} exec telemetry -- cat /tmp/tel/{cell}.csv > {outdir / 'telemetry.csv'}", check=False)


def vllm_metrics(outdir: Path, tag: str):
    ip = k("get pod vllm -o jsonpath={.status.podIP}")
    txt = k(f"exec {CLIENTS[0]} -- python -c \"import urllib.request;print(urllib.request.urlopen('http://{ip}:8000/metrics').read().decode())\"", check=False)
    (outdir / f"vllm_metrics.{tag}.prom").write_text(txt)


# ---------------------------------------------------------------- load
def load_start(cell: str, duration: float, start_at: float):
    ip = k("get pod vllm -o jsonpath={.status.podIP}")
    total_rate = CFG["open_loop_rate"]
    nproc = len(CLIENTS) * PROCS_PER_CLIENT
    r = total_rate / nproc
    j = 0
    for c in CLIENTS:
        for p in range(PROCS_PER_CLIENT):
            label = f"{cell}.p{j}"
            seed = 1000 + j * 7919 + int(start_at) % 100000
            cmd = (f"cd /tmp && setsid nohup python colo_bench.py --input-file /tmp/synthetic_yelp.xlsx "
                   f"--output-dir /tmp/out/{cell} --url http://{ip}:8000/v1/completions "
                   f"--tokenizer {CFG['model']} --label {label} --mode open --rate {r:.4f} "
                   f"--duration {duration:.0f} --seed {seed} --start-at {start_at:.3f} "
                   f"--request-timeout 300 </dev/null > /tmp/out/{label}.log 2>&1 &")
            launch(f"exec {c} -- bash -c 'mkdir -p /tmp/out/{cell} && {cmd}'")
            j += 1


def load_wait(cell: str, outdir: Path, timeout_s: int):
    t0 = time.time()
    want = PROCS_PER_CLIENT
    while time.time() - t0 < timeout_s:
        done = 0
        for c in CLIENTS:
            n = k(f"exec {c} -- bash -c 'ls /tmp/out/{cell}/*.summary.json 2>/dev/null | wc -l'", check=False)
            done += int(n or 0)
        if done >= want * len(CLIENTS):
            break
        time.sleep(10)
    for c in CLIENTS:
        sh(f"kubectl -n {NS} exec {c} -- tar -C /tmp/out -cf - {cell} | tar -C {outdir.parent} -xf -", check=False)
        sh(f"kubectl -n {NS} exec {c} -- bash -c 'cat /tmp/out/{cell}.p*.log' >> {outdir / 'client_logs.txt'}", check=False)


# ---------------------------------------------------------------- cells
def run_cell(kind: str, level: int, rep: int) -> dict:
    cell = f"{kind}{'' if kind == 'vllm_solo' else '-L' + str(level)}-r{rep}"
    outdir = RESULTS / "cells" / cell
    if (outdir / "DONE").exists():
        log(f"skip {cell} (done)")
        return json.loads((outdir / "DONE").read_text())
    outdir.mkdir(parents=True, exist_ok=True)
    log(f"=== cell {cell}")
    meta = {"cell": cell, "kind": kind, "level": level, "rep": rep, "t_begin": time.time()}
    warm, win, tail = CFG["warmup_s"], CFG["window_s"], CFG["tail_s"]
    spec_name = f"spec-{cell}".lower().replace("_", "-")

    if kind == "vllm_solo":
        dur = warm + win + tail
        telemetry_start(cell, int(dur + 40))
        vllm_metrics(outdir, "pre")
        start_at = time.time() + 15
        load_start(cell, dur, start_at)
        cpu_state(outdir, "during")
        load_wait(cell, outdir, int(dur + 600))
        vllm_metrics(outdir, "post")
    elif kind == "colo":
        # SPEC first, then vLLM load for the whole SPEC run (+ margin): SPEC is
        # always co-located with load, and the vLLM analysis window
        # [warm, warm+win] is guaranteed to lie inside SPEC activity.
        spec_dur = CFG["spec_est_s"].get(str(level), 3600)
        dur = spec_dur + CFG["spec_margin_s"]
        telemetry_start(cell, int(dur + 120))
        vllm_metrics(outdir, "pre")
        spec_start(spec_name, level)
        cpu_state(outdir, "during")
        start_at = time.time() + 15
        load_start(cell, dur, start_at)
        spec_logs = spec_wait(spec_name, outdir)
        load_wait(cell, outdir, int(dur + 900))
        vllm_metrics(outdir, "post")
        meta["spec_rc"] = [l for l in spec_logs.splitlines() if l.startswith("SPEC_END")]
    elif kind == "spec_solo":
        spec_dur = CFG["spec_est_s"].get(str(level), 3600)
        telemetry_start(cell, int(spec_dur + CFG["spec_margin_s"] + 120))
        spec_start(spec_name, level)
        cpu_state(outdir, "during")
        spec_logs = spec_wait(spec_name, outdir)
        meta["spec_rc"] = [l for l in spec_logs.splitlines() if l.startswith("SPEC_END")]
    telemetry_fetch(cell, outdir)
    meta["t_end"] = time.time()
    (outdir / "DONE").write_text(json.dumps(meta, indent=2))
    log(f"=== done {cell} in {meta['t_end'] - meta['t_begin']:.0f}s")
    time.sleep(CFG["cooldown_s"])
    return meta


def schedule(reps: int, seed: int) -> list[tuple[str, int, int]]:
    """Interleaved blocks: each block contains every cell once, randomized order."""
    rng = random.Random(seed)
    levels = CFG["levels"]
    cells = [("vllm_solo", 0)] + [("colo", L) for L in levels] + [("spec_solo", L) for L in levels]
    out = []
    for r in range(1, reps + 1):
        blk = cells[:]
        rng.shuffle(blk)
        out += [(kd, L, r) for kd, L in blk]
    return out


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("cell")
    c.add_argument("--kind", required=True, choices=["vllm_solo", "colo", "spec_solo"])
    c.add_argument("--level", type=int, default=0)
    c.add_argument("--rep", type=int, default=0)
    m = sub.add_parser("matrix")
    m.add_argument("--reps", type=int, default=5)
    m.add_argument("--seed", type=int, default=20261027)
    a = ap.parse_args()
    RESULTS.mkdir(exist_ok=True)
    if a.cmd == "cell":
        run_cell(a.kind, a.level, a.rep)
    else:
        sched = schedule(a.reps, a.seed)
        (RESULTS / "schedule.json").write_text(json.dumps(sched, indent=1))
        for i, (kd, L, r) in enumerate(sched):
            log(f"[{i + 1}/{len(sched)}]")
            for attempt in range(2):
                try:
                    run_cell(kd, L, r)
                    break
                except Exception as e:  # noqa: BLE001
                    log(f"cell failed (attempt {attempt + 1}): {e}")
                    sh(f"kubectl -n {NS} delete pod -l app=spec --ignore-not-found", check=False)
                    time.sleep(60)


if __name__ == "__main__":
    sys.exit(main())
