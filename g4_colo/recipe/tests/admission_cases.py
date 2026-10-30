#!/usr/bin/env python3
"""Server-side dry-run pods against the colo-g4-guard admission policy and the colo-svc quota.

Usage:  python3 admission_cases.py [--overlay ../overlays/demo] [--no-manifests]

Two groups of cases, each PASS when the API server's decision matches the expectation:
  1. Synthetic pods: the original wave-1 cases plus the G4-targeting bypasses from review finding 1.3
     (keyless `operator: Exists`, colo.role / gke-accelerator / compute-class selectors and affinity, nodeName,
     gVisor on G4 backfill, colo-system pods other than the node agent).
  2. Manifest pods: every Deployment / DaemonSet pod template rendered from the overlay must be admitted, so a
     tightened policy never blocks the recipe's own workloads.
  3. UPDATE cases (review N2): dry-run JSON patches on pods that already run (overflow, backfill, vLLM): appending a
     G4-matching toleration to a non-compliant pod is denied, harmless updates still pass.
  4. With --live-gated only: the gated-then-updated bypass. Creates ONE real scheduling-gated pause pod in colo-svc
     (it never schedules), dry-run patches it, then deletes it.
Except for --live-gated, nothing is created: every request uses --dry-run=server.
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


def first_pod(ns, selector):
    r = subprocess.run(["kubectl", "get", "pods", "-n", ns, "-l", selector, "--field-selector=status.phase=Running",
                        "-o", "jsonpath={.items[0].metadata.name}"], text=True, capture_output=True)
    return r.stdout.strip() or None


def patch_case(desc, ns, pod_name, ops, want):
    """A dry-run JSON patch (an UPDATE) of an existing pod."""
    return ("patch", desc, (ns, pod_name, ops), want)


TOL_APPEND = lambda tol: [{"op": "add", "path": "/spec/tolerations/-", "value": tol}]  # noqa: E731


def update_cases():
    out = []
    ov = first_pod("colo-svc", "colo.tier=overflow")
    bf = first_pod("colo-svc", "colo.tier=backfill")
    vl = first_pod("inference", "app=vllm")
    if ov:
        out.append(patch_case("UPDATE reject: append keyless Exists toleration to a running overflow pod",
                              "colo-svc", ov, TOL_APPEND(KEYLESS), False))
        out.append(patch_case("UPDATE reject: append g4-gpu toleration to a running overflow pod",
                              "colo-svc", ov, TOL_APPEND(G4_TOL), False))
        out.append(patch_case("UPDATE accept: label change on a running overflow pod",
                              "colo-svc", ov, [{"op": "add", "path": "/metadata/labels/colo.test", "value": "x"}], True))
    if bf:
        out.append(patch_case("UPDATE accept: pod-deletion-cost annotation on a running backfill pod", "colo-svc", bf,
                              [{"op": "add", "path": "/metadata/annotations/controller.kubernetes.io~1pod-deletion-cost",
                                "value": "-10"}], True))
        out.append(patch_case("UPDATE accept: append unrelated toleration to compliant backfill", "colo-svc", bf,
                              TOL_APPEND({"key": "example.com/x", "operator": "Exists", "effect": "NoSchedule"}), True))
    if vl:
        out.append(patch_case("UPDATE accept: append unrelated toleration to vLLM", "inference", vl,
                              TOL_APPEND({"key": "example.com/x", "operator": "Exists", "effect": "NoSchedule"}), True))
    if not (ov and bf and vl):
        print("NOTE: some UPDATE cases skipped (no Running overflow/backfill/vLLM pod found)")
    return out


GATED = pod("gated-bypass", "colo-svc", g4=False, cpu_req="500m", cpu_lim=None, mem_req="256Mi", mem_lim="256Mi",
            schedulingGates=[{"name": "colo.test/hold"}])
del GATED["spec"]["containers"][0]["resources"]["limits"]["cpu"]


def run_patch(ns, name, ops):
    return subprocess.run(["kubectl", "patch", "pod", "-n", ns, name, "--dry-run=server", "--type=json", "-p",
                           json.dumps(ops), "-o", "name"], text=True, capture_output=True)


def gated_cases():
    """Real gated pod (never schedules); returns patch cases and a cleanup function."""
    r = subprocess.run(["kubectl", "create", "-o", "name", "-f", "-"], input=json.dumps(GATED), text=True,
                       capture_output=True)
    if r.returncode != 0:
        print(f"FAIL  could not create the gated test pod: {(r.stderr or r.stdout).strip()[:200]}")
        return [], lambda: None
    bypass = [{"op": "add", "path": "/spec/tolerations/-", "value": KEYLESS},
              {"op": "add", "path": "/spec/nodeSelector", "value": {"colo.role": "g4"}}]
    cases = [
        patch_case("UPDATE reject: gated pod gains keyless Exists + nodeSelector colo.role=g4", "colo-svc",
                   "gated-bypass", bypass, False),
        patch_case("UPDATE reject: gated pod gains affinity compute-class=g4-gpu", "colo-svc", "gated-bypass",
                   [{"op": "add", "path": "/spec/affinity",
                     "value": affinity("cloud.google.com/compute-class", ["g4-gpu"])}], False),
        patch_case("UPDATE accept: gated pod drops its gate without G4 targeting", "colo-svc", "gated-bypass",
                   [{"op": "remove", "path": "/spec/schedulingGates"}], True),
    ]
    cleanup = lambda: subprocess.run(["kubectl", "delete", "pod", "-n", "colo-svc", "gated-bypass", "--wait=false"],  # noqa: E731
                                     capture_output=True)
    return cases, cleanup


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--overlay", default=str(HERE.parent / "overlays" / "demo"))
    ap.add_argument("--no-manifests", action="store_true")
    ap.add_argument("--live-gated", action="store_true",
                    help="create one real scheduling-gated pod in colo-svc for the N2 bypass case, then delete it")
    a = ap.parse_args()
    cases = [("create",) + c for c in CASES]
    if not a.no_manifests:
        cases += [("create",) + c for c in manifest_cases(a.overlay)]
    cases += update_cases()
    cleanup = lambda: None  # noqa: E731
    if a.live_gated:
        g, cleanup = gated_cases()
        cases += g
    ok = True
    fails = 0
    try:
        for kind, desc, obj, want in cases:
            if kind == "create":
                r = subprocess.run(["kubectl", "create", "--dry-run=server", "-o", "name", "-f", "-"],
                                   input=json.dumps(obj), text=True, capture_output=True)
            else:
                r = run_patch(*obj)
            got = r.returncode == 0
            text = (r.stderr or r.stdout).strip()
            msg = text.splitlines()[-1] if text else ""
            res = "PASS" if got == want else "FAIL"
            ok &= got == want
            fails += got != want
            print(f"{res}  {desc}  -> {'admitted' if got else 'denied'}  {msg[:200]}")
    finally:
        cleanup()
    print(f"{len(cases) - fails}/{len(cases)} PASS")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
