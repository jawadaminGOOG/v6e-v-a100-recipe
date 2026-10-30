#!/usr/bin/env python3
"""Server-side dry-run pods against the colo-g4-guard admission policy and the colo-svc quota.

Usage:  python3 admission_cases.py [--overlay ../overlays/demo] [--no-manifests]

Two groups of cases, each PASS when the API server's decision matches the expectation:
  1. Synthetic pods: the original wave-1 cases plus the G4-targeting bypasses from review finding 1.3
     (keyless `operator: Exists`, colo.role / gke-accelerator / compute-class selectors and affinity, nodeName,
     gVisor on G4 backfill, colo-system pods other than the node agent).
  2. Manifest pods: every Deployment / DaemonSet pod template rendered from the overlay must be admitted, so a
     tightened policy never blocks the recipe's own workloads.
Nothing is created: every request uses `kubectl create --dry-run=server`.
"""
import argparse
import copy
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
G4_TOL = {"key": "cloud.google.com/compute-class", "operator": "Equal", "value": "g4-gpu", "effect": "NoSchedule"}
G4 = {"tolerations": [G4_TOL], "nodeSelector": {"cloud.google.com/compute-class": "g4-gpu"}}
KEYLESS = {"operator": "Exists"}


def affinity(key, values, op="In"):
    expr = {"key": key, "operator": op}
    if values is not None:
        expr["values"] = values
    return {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {
        "nodeSelectorTerms": [{"matchExpressions": [expr]}]}}}


def pod(name, ns, prio=None, cpu_req="2", cpu_lim="2", mem_req="1Gi", mem_lim="1Gi", gpu=False, g4=True, **spec_extra):
    res = {"requests": {"cpu": cpu_req, "memory": mem_req}, "limits": {"cpu": cpu_lim, "memory": mem_lim}}
    if gpu:
        res["requests"]["nvidia.com/gpu"] = 1
        res["limits"]["nvidia.com/gpu"] = 1
    spec = {"containers": [{"name": "c", "image": "registry.k8s.io/pause:3.10", "resources": res}]}
    if g4:
        spec.update(copy.deepcopy(G4))
    if prio:
        spec["priorityClassName"] = prio
    spec.update(spec_extra)
    return {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": name, "namespace": ns}, "spec": spec}


def g4_node_name():
    r = subprocess.run(["kubectl", "get", "nodes", "-l", "colo.role=g4", "-o", "jsonpath={.items[0].metadata.name}"],
                       text=True, capture_output=True)
    return r.stdout.strip() or "g4-node-placeholder"


NODE = g4_node_name()
BURST = dict(cpu_req="500m", cpu_lim="1", mem_req="256Mi", mem_lim="512Mi")

CASES = [
    # --- wave-1 cases (compute-class toleration + nodeSelector) ---
    ("reject: non-expendable priority on G4", pod("t1", "colo-svc", prio="colo-overflow"), False),
    ("reject: no priority class on G4", pod("t2", "colo-svc"), False),
    ("reject: fractional CPU", pod("t3", "colo-svc", prio="colo-backfill", cpu_req="1500m", cpu_lim="1500m"), False),
    ("reject: request != limit", pod("t4", "colo-svc", prio="colo-backfill", cpu_req="2", cpu_lim="4"), False),
    ("reject: GPU request outside inference", pod("t5", "colo-svc", prio="colo-backfill", gpu=True), False),
    ("accept: compliant backfill", pod("t6", "colo-svc", prio="colo-backfill"), True),
    ("accept: GPU workload in inference", pod("t7", "inference", prio="gpu-serving", cpu_req="8", cpu_lim="8",
                                              mem_req="64Gi", mem_lim="64Gi", gpu=True), True),
    ("accept: non-G4 pod unaffected", pod("t8", "colo-svc", prio="colo-overflow", g4=False), True),
    ("reject: quota (CPU above namespace cap)", pod("t9", "colo-svc", prio="colo-overflow", cpu_req="200",
                                                     cpu_lim="200", g4=False), False),
    # --- finding 1.3 bypasses: G4 targeting without the compute-class toleration/selector pair ---
    ("reject: keyless Exists toleration + affinity colo.role=g4 (burstable)",
     pod("b1", "colo-svc", prio="colo-overflow", g4=False, tolerations=[KEYLESS],
         affinity=affinity("colo.role", ["g4"]), **BURST), False),
    ("reject: keyless Exists toleration + nodeName (burstable)",
     pod("b2", "colo-svc", g4=False, tolerations=[KEYLESS], nodeName=NODE, **BURST), False),
    ("reject: keyless Exists toleration only (burstable)",
     pod("b3", "colo-svc", g4=False, tolerations=[KEYLESS], **BURST), False),
    ("reject: compute-class toleration operator Exists (no value), non-expendable",
     pod("b4", "colo-svc", prio="colo-overflow", g4=False,
         tolerations=[{"key": "cloud.google.com/compute-class", "operator": "Exists"}]), False),
    ("reject: nodeSelector colo.role=g4, non-expendable",
     pod("b5", "colo-svc", prio="colo-overflow", g4=False, nodeSelector={"colo.role": "g4"}), False),
    ("reject: nodeSelector cloud.google.com/gke-accelerator, non-expendable",
     pod("b6", "colo-svc", prio="colo-overflow", g4=False,
         nodeSelector={"cloud.google.com/gke-accelerator": "nvidia-rtx-pro-6000"}), False),
    ("reject: affinity compute-class In [g4-gpu], non-expendable",
     pod("b7", "colo-svc", prio="colo-overflow", g4=False,
         affinity=affinity("cloud.google.com/compute-class", ["g4-gpu"])), False),
    ("reject: affinity gke-accelerator Exists, non-expendable",
     pod("b8", "colo-svc", prio="colo-overflow", g4=False,
         affinity=affinity("cloud.google.com/gke-accelerator", None, op="Exists")), False),
    ("reject: nodeName set, non-expendable",
     pod("b9", "colo-svc", prio="colo-overflow", g4=False, nodeName=NODE), False),
    ("reject: compliant backfill but runtimeClassName gvisor on G4 (cpuset bypass)",
     pod("b10", "colo-svc", prio="colo-backfill", runtimeClassName="gvisor"), False),
    ("reject: colo-system pod (not node agent) with keyless Exists, burstable",
     pod("b11", "colo-system", g4=False, tolerations=[KEYLESS], **BURST), False),
    ("reject: colo-system pod with node-agent SA but GPU request",
     pod("b12", "colo-system", g4=False, gpu=True, tolerations=[KEYLESS], serviceAccountName="colo-node-agent"),
     False),
    # --- things the tighter policy must still admit ---
    ("accept: node agent (colo-system, SA colo-node-agent) tolerating everything",
     pod("a1", "colo-system", g4=False, tolerations=[KEYLESS], serviceAccountName="colo-node-agent",
         affinity=affinity("colo.role", ["g4", "n4d"]), **BURST), True),
    ("accept: keyless Exists limited to NoExecute does not tolerate the G4 NoSchedule taint",
     pod("a2", "colo-svc", g4=False, tolerations=[{"operator": "Exists", "effect": "NoExecute"}], **BURST), True),
    ("accept: compliant backfill with keyless Exists toleration",
     pod("a3", "colo-svc", prio="colo-backfill", g4=False, tolerations=[KEYLESS]), True),
    ("accept: gVisor overflow on N4D (not G4)",
     pod("a4", "colo-svc", prio="colo-overflow", g4=False, runtimeClassName="gvisor",
         nodeSelector={"cloud.google.com/compute-class": "n4d-cpu"},
         tolerations=[{"key": "cloud.google.com/compute-class", "operator": "Equal", "value": "n4d-cpu",
                       "effect": "NoSchedule"},
                      {"key": "sandbox.gke.io/runtime", "operator": "Equal", "value": "gvisor",
                       "effect": "NoSchedule"}]), True),
]


def manifest_cases(overlay):
    """Pod templates from the rendered overlay, wrapped as Pods; every one must be admitted."""
    import yaml  # PyYAML; only needed for this group
    out = subprocess.run(["kubectl", "kustomize", str(overlay)], text=True, capture_output=True, check=True).stdout
    cases = []
    for d in yaml.safe_load_all(out):
        if not d or d.get("kind") not in ("Deployment", "DaemonSet"):
            continue
        tmpl = d["spec"]["template"]
        meta = {"name": f"dryrun-{d['metadata']['name']}"[:63], "namespace": d["metadata"]["namespace"],
                "labels": tmpl.get("metadata", {}).get("labels", {})}
        p = {"apiVersion": "v1", "kind": "Pod", "metadata": meta, "spec": tmpl["spec"]}
        cases.append((f"accept: manifest {d['kind']} {meta['namespace']}/{d['metadata']['name']}", p, True))
    return cases


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--overlay", default=str(HERE.parent / "overlays" / "demo"))
    ap.add_argument("--no-manifests", action="store_true")
    a = ap.parse_args()
    cases = list(CASES)
    if not a.no_manifests:
        cases += manifest_cases(a.overlay)
    ok = True
    fails = 0
    for desc, obj, want in cases:
        r = subprocess.run(["kubectl", "create", "--dry-run=server", "-o", "name", "-f", "-"], input=json.dumps(obj),
                           text=True, capture_output=True)
        got = r.returncode == 0
        text = (r.stderr or r.stdout).strip()
        msg = text.splitlines()[-1] if text else ""
        res = "PASS" if got == want else "FAIL"
        ok &= got == want
        fails += got != want
        print(f"{res}  {desc}  -> {'admitted' if got else 'denied'}  {msg[:200]}")
    print(f"{len(cases) - fails}/{len(cases)} PASS")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
