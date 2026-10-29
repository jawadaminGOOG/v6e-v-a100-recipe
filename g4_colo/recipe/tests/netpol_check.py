#!/usr/bin/env python3
"""Gate 6: sandbox pods run on G4 with enforced exclusive cpusets (default runtime on G4 with
P6 reason recorded; gVisor preserved on N4D overflow), and NetworkPolicy isolates them.

Needs at least one sandbox-backfill pod and one toolcall pod Running (set demand >= 1 for both).
Checks, from inside a sandbox-backfill pod on G4:
  * the pod runs on a g4-gpu node,
  * its cgroup effective cpuset has exactly 2 exclusive CPUs disjoint from vLLM's cpuset,
  * the default runtime is used on G4 with the P6 gVisor-cpuset probe reason recorded in
    annotation `colo.gke.io/sandbox-runtime-reason` (while `sandbox-overflow` on N4D uses gVisor),
  * TCP connect to the vLLM Service (inference/vllm:8000) and vLLM pod IP is blocked,
  * TCP connect to a toolcall pod on :9100 succeeds.
Writes a JSON verdict to --out.
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "demo"))
from state import parse_cpuset  # noqa: E402

PROBE = r"""
import socket, sys
host, port = sys.argv[1], int(sys.argv[2])
s = socket.socket(); s.settimeout(4)
try:
    s.connect((host, port)); print("OPEN")
except Exception as e:
    print("BLOCKED", type(e).__name__)
"""


def kj(*args):
    return json.loads(subprocess.check_output(["kubectl", *args, "-o", "json"], text=True))


def sh(*args, check=False):
    p = subprocess.run(list(args), capture_output=True, text=True)
    if check and p.returncode:
        raise RuntimeError(p.stderr)
    return p.stdout.strip() + (("\n" + p.stderr.strip()) if p.stderr.strip() else "")


def running(selector):
    items = kj("get", "pods", "-n", "colo-svc", "-l", selector)["items"]
    return [p for p in items if p["status"].get("phase") == "Running"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(HERE.parents[2] / ".agents" / "wave-1" / "logs" / "gate6_netpol.verdict.json"))
    a = ap.parse_args()
    sb = running("app=sandbox,colo.tier=backfill")
    tc = running("app=toolcall")
    if not sb or not tc:
        print("need >=1 Running sandbox-backfill and toolcall pod", file=sys.stderr)
        return 2
    s, t = sb[0], tc[0]
    sname = s["metadata"]["name"]
    node = s["spec"]["nodeName"]
    node_cc = kj("get", "node", node)["metadata"]["labels"].get("cloud.google.com/compute-class")
    rc = s["spec"].get("runtimeClassName")
    ann = (s["metadata"].get("annotations") or {}).get("colo.gke.io/sandbox-runtime-reason", "")
    of_rc = kj("get", "deploy", "-n", "colo-svc", "sandbox-overflow")["spec"]["template"]["spec"].get("runtimeClassName")

    eff_cpuset_raw = sh("kubectl", "exec", "-n", "colo-svc", sname, "--", "cat", "/sys/fs/cgroup/cpuset.cpus.effective")
    sb_cpus = set(parse_cpuset(eff_cpuset_raw))
    vllm_pods = [p for p in kj("get", "pods", "-n", "inference", "-l", "app=vllm")["items"] if p["spec"].get("nodeName") == node]
    vllm_cpus = set()
    if vllm_pods:
        v_raw = sh("kubectl", "exec", "-n", "inference", vllm_pods[0]["metadata"]["name"], "--", "cat", "/sys/fs/cgroup/cpuset.cpus.effective")
        vllm_cpus = set(parse_cpuset(v_raw))

    vllm = sh("kubectl", "exec", "-n", "colo-svc", sname, "--", "python3", "-c", PROBE, "vllm.inference.svc.cluster.local", "8000")
    vllm_pod_ip = next((p["status"].get("podIP") for p in kj("get", "pods", "-n", "inference", "-l", "app=vllm")["items"]
                        if p["status"].get("podIP")), None)
    vllm_ip = sh("kubectl", "exec", "-n", "colo-svc", sname, "--", "python3", "-c", PROBE, vllm_pod_ip, "8000") if vllm_pod_ip else "n/a"
    tool = sh("kubectl", "exec", "-n", "colo-svc", sname, "--", "python3", "-c", PROBE, t["status"]["podIP"], "9100")

    checks = {
        "sandbox_on_g4": node_cc == "g4-gpu",
        "exclusive_cpuset_2_cores": len(sb_cpus) == 2,
        "cpuset_disjoint_from_vllm": bool(vllm_cpus) and not (sb_cpus & vllm_cpus),
        "default_runtime_with_p6_reason_recorded": rc is None and "gVisor" in ann and of_rc == "gvisor",
        "sandbox_to_vllm_service_blocked": vllm.startswith("BLOCKED"),
        "sandbox_to_vllm_pod_blocked": vllm_ip.startswith("BLOCKED"),
        "sandbox_to_toolcall_allowed": tool.startswith("OPEN"),
    }
    verdict = {
        "time": time.time(),
        "sandbox_pod": sname,
        "node": node,
        "node_compute_class": node_cc,
        "backfill_runtimeClassName": rc,
        "overflow_runtimeClassName": of_rc,
        "p6_runtime_reason_annotation": ann,
        "sandbox_effective_cpuset": sorted(sb_cpus),
        "vllm_effective_cpuset": sorted(vllm_cpus),
        "probe_vllm_service": vllm,
        "probe_vllm_pod": vllm_ip,
        "probe_toolcall": tool,
        "toolcall_pod": t["metadata"]["name"],
        "checks": checks,
        "pass": all(checks.values()),
    }
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(verdict, indent=2))
    print(json.dumps(verdict, indent=2))
    return 0 if verdict["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
