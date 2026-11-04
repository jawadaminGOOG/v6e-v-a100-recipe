#!/usr/bin/env python3
"""Live check: customer DaemonSets on G4 nodes (components/customer-daemonset + colo-g4-guard isCustomerDaemonSet).

For each cluster:
  1. sample-node-agent (namespace colo-agents, labelled colo.gke.io/g4-daemonsets=allowed) has one Ready pod on
     every G4 and N4D node.
  2. In a namespace without the label: the same G4-tolerating DaemonSet gets no pod at all (a G4 toleration alone
     marks a pod as G4-bound, so it fails closed, FailedCreate from colo-g4-guard), while a DaemonSet without the G4
     tolerations still runs on every N4D node as before. The namespace is deleted.
  3. On G4 the agent pod holds no exclusive cores (it is not in the kubelet CPU manager state), so it shares only
     the node's shared cores, never vLLM's or backfill's.
Exit 0 = all pass.

    python3 daemonset_check.py [--clusters controller,native]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time

CTX = {"controller": "gke_northam-ce-mlai-tpu_us-west8_g4cluster-jawadamin-us-west8-0726",
       "native": "gke_northam-ce-mlai-tpu_us-west8_g4cluster-jawadamin-us-west8-1126"}
NEG_NS = "colo-agents-unlabelled"
FAILS: list[str] = []


def check(c, name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {c}: {name}  {detail}", flush=True)
    if not ok:
        FAILS.append(f"{c}: {name}")


def k(c, *a, check_=True, inp=None):
    r = subprocess.run(["kubectl", "--context", CTX[c], *a], capture_output=True, text=True, input=inp)
    if check_ and r.returncode:
        raise RuntimeError(r.stderr.strip()[:300])
    return r.stdout


def kj(c, *a):
    return json.loads(k(c, *a, "-o", "json"))


def ready(p):
    return any(x["type"] == "Ready" and x["status"] == "True" for x in p["status"].get("conditions", []))


def run(c):
    nodes = kj(c, "get", "nodes")["items"]
    role = {n["metadata"]["name"]: n["metadata"]["labels"].get("colo.role") for n in nodes}
    g4 = sorted(n for n, r in role.items() if r == "g4")
    cpu_nodes = sorted(n for n, r in role.items() if r in ("g4", "n4d"))

    # 1. labelled namespace: one Ready agent per G4 + N4D node
    pods = kj(c, "-n", "colo-agents", "get", "pods", "-l", "app=sample-node-agent")["items"]
    on = {p["spec"].get("nodeName"): p for p in pods if ready(p)}
    missing = [n for n in cpu_nodes if n not in on]
    check(c, "agent Ready on every G4 and N4D node", not missing,
          f"{len(on)}/{len(cpu_nodes)} nodes (G4: {sum(n in on for n in g4)}/{len(g4)}) missing={missing}")

    # 3. no exclusive cores for the agent on G4
    agents = {p["spec"]["nodeName"]: p for p in kj(c, "-n", "colo-system", "get", "pods", "-l", "app=colo-node-agent")["items"]
              if ready(p)}
    for n in g4:
        if n not in on or n not in agents:
            continue
        st = json.loads(k(c, "get", "--raw",
                          f"/api/v1/namespaces/colo-system/pods/{agents[n]['metadata']['name']}:8080/proxy/cpus"))
        ents = (st.get("cpu_manager_state") or {}).get("entries") or {}
        uid = on[n]["metadata"]["uid"]
        check(c, f"agent on {n[-4:]} uses shared cores only", uid not in ents,
              f"shared={st['cpu_manager_state'].get('defaultCpuSet')}")

    # 2. unlabelled namespace: no pod on G4
    k(c, "delete", "ns", NEG_NS, "--ignore-not-found", "--wait=true", "--timeout=60s", check_=False)
    src = kj(c, "-n", "colo-agents", "get", "ds", "sample-node-agent")
    ds = {"apiVersion": "apps/v1", "kind": "DaemonSet", "metadata": {"name": "unlabelled-agent", "namespace": NEG_NS},
          "spec": src["spec"]}
    k(c, "create", "ns", NEG_NS)
    try:
        k(c, "apply", "-f", "-", inp=json.dumps(ds))
        time.sleep(25)
        cpu_only = json.loads(json.dumps(ds))
        cpu_only["metadata"]["name"] = "unlabelled-cpu-only-agent"
        t = cpu_only["spec"]["template"]["spec"]
        t["tolerations"] = [x for x in t["tolerations"] if x.get("key") not in ("nvidia.com/gpu",)
                            and x.get("value") != "g4-gpu"]
        t["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"] = [
            {"matchExpressions": [{"key": "colo.role", "operator": "In", "values": ["n4d"]}]}]
        cpu_only["spec"]["selector"] = {"matchLabels": {"app": "cpu-only-agent"}}
        cpu_only["spec"]["template"]["metadata"] = {"labels": {"app": "cpu-only-agent"}}
        k(c, "apply", "-f", "-", inp=json.dumps(cpu_only))
        time.sleep(25)
        neg = kj(c, "-n", NEG_NS, "get", "pods")["items"]
        g4_tol = [p for p in neg if p["metadata"]["labels"].get("app") == "sample-node-agent"]
        plain = [p for p in neg if p["metadata"]["labels"].get("app") == "cpu-only-agent"
                 and role.get(p["spec"].get("nodeName")) == "n4d"]
        ev = k(c, "-n", NEG_NS, "get", "events", "--field-selector", "reason=FailedCreate",
               "-o", "custom-columns=M:.message", "--no-headers")
        denied = "colo-g4-guard" in ev
        check(c, "unlabelled namespace: G4-tolerating DaemonSet gets no pod anywhere (denied by colo-g4-guard)",
              not g4_tol and denied, f"pods={len(g4_tol)} denied={denied}")
        n4d = sum(r == "n4d" for r in role.values())
        check(c, "unlabelled namespace: DaemonSet without G4 tolerations runs on every N4D node (unchanged)",
              len(plain) == n4d, f"N4D pods={len(plain)}/{n4d}")
    finally:
        k(c, "delete", "ns", NEG_NS, "--wait=false", check_=False)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--clusters", default="controller,native")
    for c in ap.parse_args().clusters.split(","):
        run(c)
    print("ALL PASS" if not FAILS else f"FAILURES: {FAILS}")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
