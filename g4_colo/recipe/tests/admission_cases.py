#!/usr/bin/env python3
"""Gate clause 5: server-side dry-run pods against the colo-g4-guard admission policy and the colo-svc quota."""
import json, subprocess, sys

G4 = {"tolerations": [{"key": "cloud.google.com/compute-class", "operator": "Equal", "value": "g4-gpu", "effect": "NoSchedule"}],
      "nodeSelector": {"cloud.google.com/compute-class": "g4-gpu"}}

def pod(name, ns, prio=None, cpu_req="2", cpu_lim="2", mem_req="1Gi", mem_lim="1Gi", gpu=False, g4=True):
    res = {"requests": {"cpu": cpu_req, "memory": mem_req}, "limits": {"cpu": cpu_lim, "memory": mem_lim}}
    if gpu:
        res["requests"]["nvidia.com/gpu"] = 1; res["limits"]["nvidia.com/gpu"] = 1
    spec = {"containers": [{"name": "c", "image": "registry.k8s.io/pause:3.10", "resources": res}]}
    if g4: spec.update(json.loads(json.dumps(G4)))
    if prio: spec["priorityClassName"] = prio
    return {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": name, "namespace": ns}, "spec": spec}

CASES = [
    ("reject: non-expendable priority on G4", pod("t1", "colo-svc", prio="colo-overflow"), False),
    ("reject: no priority class on G4", pod("t2", "colo-svc"), False),
    ("reject: fractional CPU", pod("t3", "colo-svc", prio="colo-backfill", cpu_req="1500m", cpu_lim="1500m"), False),
    ("reject: request != limit", pod("t4", "colo-svc", prio="colo-backfill", cpu_req="2", cpu_lim="4"), False),
    ("reject: GPU request outside inference", pod("t5", "colo-svc", prio="colo-backfill", gpu=True), False),
    ("accept: compliant backfill", pod("t6", "colo-svc", prio="colo-backfill"), True),
    ("accept: GPU workload in inference", pod("t7", "inference", prio="gpu-serving", cpu_req="8", cpu_lim="8", mem_req="64Gi", mem_lim="64Gi", gpu=True), True),
    ("accept: non-G4 pod unaffected", pod("t8", "colo-svc", prio="colo-overflow", g4=False), True),
    ("reject: quota (CPU above namespace cap)", pod("t9", "colo-svc", prio="colo-overflow", cpu_req="200", cpu_lim="200", g4=False), False),
]

ok = True
for desc, obj, want in CASES:
    r = subprocess.run(["kubectl", "create", "--dry-run=server", "-o", "name", "-f", "-"], input=json.dumps(obj),
                       text=True, capture_output=True)
    got = r.returncode == 0
    msg = (r.stderr or r.stdout).strip().splitlines()[-1] if (r.stderr or r.stdout) else ""
    res = "PASS" if got == want else "FAIL"
    ok &= got == want
    print(f"{res}  {desc}  -> {'admitted' if got else 'denied'}  {msg[:160]}")
sys.exit(0 if ok else 1)
