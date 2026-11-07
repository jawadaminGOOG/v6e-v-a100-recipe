#!/usr/bin/env python3
"""Fail if the native recipe runs any of our own code in the control path.

Renders each overlay with `kubectl kustomize` and checks every workload:
  1. Our images (built from g4_colo/recipe/components) may only be the workload itself (colo-worker) or, in the
     demo overlay, demo traffic and read-only telemetry (colo-loadgen, colo-node-agent). colo-controller, or any
     other image of ours, fails.
  2. No ServiceAccount used by one of our images may write to Deployments, scale subresources, Nodes, Pods,
     HPAs or ClusterRules, so even an allowed image cannot steer placement or replica counts.

    python3 g4_colo/recipe-native/scripts/check_no_custom_code.py      # exit 0 = pass
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
OVERLAYS = {
    "demo": {"colo-worker", "colo-loadgen", "colo-node-agent"},
    "production-template": {"colo-worker"},
    "tpu-v6e": {"colo-worker", "colo-loadgen", "colo-node-agent"},
}
OUR_IMAGE_MARKERS = ("colo-registry.invalid/", "/colo-")
WORKLOAD_KINDS = {"Deployment", "DaemonSet", "StatefulSet", "Job", "CronJob", "Pod", "ReplicaSet"}
WRITE_VERBS = {"create", "update", "patch", "delete", "deletecollection", "*"}
CONTROL_RESOURCES = {"deployments", "deployments/scale", "replicasets", "replicasets/scale", "nodes", "pods",
                     "pods/eviction", "horizontalpodautoscalers", "clusterrules", "rules", "*"}


def render(overlay: str) -> list[dict]:
    out = subprocess.run(["kubectl", "kustomize", str(ROOT / "overlays" / overlay)],
                         check=True, capture_output=True, text=True).stdout
    return [d for d in yaml.safe_load_all(out) if d]


def pod_spec(obj: dict) -> dict:
    spec = obj.get("spec", {})
    if obj["kind"] == "CronJob":
        return spec["jobTemplate"]["spec"]["template"]["spec"]
    if obj["kind"] == "Pod":
        return spec
    return spec["template"]["spec"]


def our_image_name(image: str) -> str | None:
    if not any(m in image for m in OUR_IMAGE_MARKERS):
        return None
    return image.rsplit("/", 1)[-1].split(":", 1)[0]


def check(overlay: str, allowed: set[str]) -> list[str]:
    docs = render(overlay)
    errors: list[str] = []
    our_sas: set[tuple[str, str]] = set()
    for d in docs:
        if d["kind"] not in WORKLOAD_KINDS:
            continue
        ps = pod_spec(d)
        ns = d["metadata"].get("namespace", "default")
        for c in ps.get("initContainers", []) + ps.get("containers", []):
            name = our_image_name(c["image"])
            if name is None:
                continue
            our_sas.add((ns, ps.get("serviceAccountName", "default")))
            if name not in allowed:
                errors.append(f"{overlay}: {d['kind']}/{d['metadata']['name']} runs our image {name!r} "
                              f"(allowed here: {sorted(allowed)})")
    roles = {(d["kind"], d["metadata"].get("namespace"), d["metadata"]["name"]): d for d in docs
             if d["kind"] in ("Role", "ClusterRole")}
    for d in docs:
        if d["kind"] not in ("RoleBinding", "ClusterRoleBinding"):
            continue
        subjects = {(s.get("namespace"), s["name"]) for s in d.get("subjects", []) if s["kind"] == "ServiceAccount"}
        if not subjects & our_sas:
            continue
        ref = d["roleRef"]
        role_ns = d["metadata"].get("namespace") if ref["kind"] == "Role" else None
        role = roles.get((ref["kind"], role_ns, ref["name"]))
        for rule in (role or {}).get("rules", []):
            res = set(rule.get("resources", [])) & CONTROL_RESOURCES
            verbs = set(rule.get("verbs", [])) & WRITE_VERBS
            if res and verbs:
                errors.append(f"{overlay}: {d['kind']}/{d['metadata']['name']} gives {sorted(subjects & our_sas)} "
                              f"{sorted(verbs)} on {sorted(res)}")
    print(f"{overlay}: {len(docs)} objects, our service accounts {sorted(our_sas)}, "
          f"{'FAIL' if errors else 'ok'}")
    return errors


def main() -> int:
    errors = [e for ov, allowed in OVERLAYS.items() for e in check(ov, allowed)]
    for e in errors:
        print("  " + e)
    print("PASS: no custom code in the control path" if not errors else f"FAIL: {len(errors)} problem(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
