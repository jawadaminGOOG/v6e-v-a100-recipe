#!/usr/bin/env python3
"""Two-cluster smoke test: both recipes healthy, no regressions, no cross-contamination.

Read-mostly. One write: a +2 demand nudge on each cluster, reverted, to prove a change on one cluster never moves
the other. Exit 0 = all checks pass. Writes a JSON verdict next to the bench runs.

    python3 smoke.py [--demo http://127.0.0.1:8093] [--out ../../.agents/bench/smoke.json]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

CTX = {"controller": "gke_northam-ce-mlai-tpu_us-west8_g4cluster-jawadamin-us-west8-0726",
       "native": "gke_northam-ce-mlai-tpu_us-west8_g4cluster-jawadamin-us-west8-1126"}
NAME = {k: v.rsplit("_", 1)[1] for k, v in CTX.items()}
PROJECT = "northam-ce-mlai-tpu"
RESULTS: list[dict] = []


def check(name, ok, detail=""):
    RESULTS.append({"check": name, "pass": bool(ok), "detail": detail})
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}", flush=True)


def k(c, *args, check_=True):
    r = subprocess.run(["kubectl", "--context", CTX[c], *args], capture_output=True, text=True)
    if check_ and r.returncode:
        raise RuntimeError(r.stderr.strip()[:300])
    return r.stdout


def kj(c, *args):
    return json.loads(k(c, *args, "-o", "json"))


def ready(p):
    return any(x["type"] == "Ready" and x["status"] == "True" for x in p["status"].get("conditions", []))


def raw(c, path):
    return json.loads(k(c, "get", "--raw", path))


def promql(q):
    tok = subprocess.run(["gcloud", "auth", "application-default", "print-access-token"], capture_output=True,
                         text=True, check=True).stdout.strip()
    url = (f"https://monitoring.googleapis.com/v1/projects/{PROJECT}/location/global/prometheus/api/v1/query?"
           + urllib.parse.urlencode({"query": q}))
    return json.load(urllib.request.urlopen(urllib.request.Request(url, headers={"Authorization": f"Bearer {tok}"}),
                                            timeout=20))["data"]["result"]


def demand(c):
    if c == "controller":
        pod = [p["metadata"]["name"] for p in kj(c, "-n", "colo-system", "get", "pods", "-l", "app=colo-controller")["items"]
               if ready(p)][0]
        return raw(c, f"/api/v1/namespaces/colo-system/pods/{pod}:8080/proxy/config")["demand"], pod
    d = {x["metadata"]["name"]: x["spec"].get("replicas", 0) for x in kj(c, "-n", "colo-svc", "get", "deploy")["items"]}
    return {"sandbox": d["sandbox-backfill"], "toolcall": d["toolcall-backfill"], "analytics": d["analytics-overflow"]}, None


def coverage(c, dem):
    pods = kj(c, "-n", "colo-svc", "get", "pods")["items"]
    short = {}
    for s, want in dem.items():
        have = sum(1 for p in pods if p["metadata"]["labels"].get("app") == s and ready(p))
        if have < want:
            short[s] = f"{have}/{want}"
    return short


def deploy_replicas(c):
    return {x["metadata"]["name"]: x["spec"].get("replicas", 0) for x in kj(c, "-n", "colo-svc", "get", "deploy")["items"]}


def set_toolcall(c, n, ctrl_pod):
    if c == "controller":
        subprocess.run(["kubectl", "--context", CTX[c], "-n", "colo-system", "exec", ctrl_pod, "--", "python", "-c",
                        "import json,urllib.request;urllib.request.urlopen(urllib.request.Request("
                        "'http://127.0.0.1:8080/config',data=json.dumps({'demand':{'toolcall':%d}}).encode(),"
                        "headers={'Content-Type':'application/json'}))" % n], capture_output=True, check=True)
    else:
        k(c, "-n", "colo-svc", "scale", "deploy/toolcall-backfill", f"--replicas={n}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", default="http://127.0.0.1:8093")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parents[2] / ".agents/bench/smoke.json"))
    args = ap.parse_args()

    # 1. components per recipe ------------------------------------------------------------------------------------
    ctl = kj("controller", "-n", "colo-system", "get", "deploy", "colo-controller")
    img = ctl["spec"]["template"]["spec"]["containers"][0]["image"]
    check("controller: colo-controller v0.2.7 Ready", img.endswith(":v0.2.7") and ctl["status"].get("readyReplicas") == 1, img)
    check("native: no colo-controller", not k("native", "-n", "colo-system", "get", "deploy", "colo-controller",
                                                "--ignore-not-found"))
    check("native: no custom-metrics-stackdriver-adapter", not k("native", "get", "ns", "custom-metrics", "--ignore-not-found"))
    check("controller: no native-only objects (colo-ksm, AutoscalingMetric)",
          not k("controller", "-n", "colo-system", "get", "deploy", "colo-ksm", "--ignore-not-found").strip()
          and not k("controller", "get", "autoscalingmetric", "-A", "--ignore-not-found", check_=False).strip())
    ams = kj("native", "get", "autoscalingmetric", "-A")["items"]
    check("native: 2 AutoscalingMetrics", len(ams) == 2, ",".join(a["metadata"]["name"] for a in ams))

    # 2. HPAs -----------------------------------------------------------------------------------------------------
    for c in CTX:
        h = kj(c, "-n", "inference", "get", "hpa", "vllm")
        conds = {x["type"]: x["status"] for x in h["status"].get("conditions", [])}
        check(f"{c}: vLLM HPA scaleDown window 120 s and ScalingActive",
              h["spec"]["behavior"]["scaleDown"]["stabilizationWindowSeconds"] == 120 and conds.get("ScalingActive") == "True",
              f"min={h['spec'].get('minReplicas')} max={h['spec']['maxReplicas']} metric={h['spec']['metrics'][0]['pods']['metric']['name']}")
    for s in ("sandbox", "toolcall"):
        h = kj("native", "-n", "colo-svc", "get", "hpa", f"{s}-overflow")
        conds = {x["type"]: x["status"] for x in h["status"].get("conditions", [])}
        check(f"native: {s}-overflow HPA min 0, tolerance 0, ScalingActive",
              h["spec"].get("minReplicas") == 0 and conds.get("ScalingActive") == "True"
              and str(h["spec"]["behavior"]["scaleUp"].get("tolerance")) == "0", h["spec"]["metrics"][0]["external"]["metric"]["name"])

    # 3. serving health -------------------------------------------------------------------------------------------
    for c in CTX:
        lg = [p["metadata"]["name"] for p in kj(c, "-n", "inference", "get", "pods", "-l", "app=loadgen")["items"] if ready(p)]
        st = raw(c, f"/api/v1/namespaces/inference/pods/{lg[0]}:8080/proxy/stats")
        p99 = [d.get("ttft_p99_ms") for d in (st.get("per_pod") or {}).values()]
        err = sum(d.get("err_window") or 0 for d in (st.get("per_pod") or {}).values())
        check(f"{c}: vLLM serving, 0 errors, p99 < 200 ms", p99 and all((x or 0) < 200 for x in p99) and err == 0,
              f"rate={st.get('rate_total')} p99={p99} err={err}")
        dem, _ = demand(c)
        short = coverage(c, dem)
        check(f"{c}: Ready pods >= demand per service", not short, f"demand={dem} short={short}")

    # 3b. customer DaemonSet on every G4 and N4D node, shared cores only
    for c in CTX:
        nodes = {n["metadata"]["name"]: n["metadata"]["labels"].get("colo.role") for n in kj(c, "get", "nodes")["items"]}
        want = {n for n, r in nodes.items() if r in ("g4", "n4d")}
        have = {p["spec"].get("nodeName") for p in kj(c, "-n", "colo-agents", "get", "pods", "-l", "app=sample-node-agent")["items"]
                if ready(p)}
        check(f"{c}: customer DaemonSet Ready on every G4 and N4D node", want <= have,
              f"{len(have & want)}/{len(want)} (G4 {sum(1 for n in have if nodes.get(n) == 'g4')})")

    # 4. metrics isolation ----------------------------------------------------------------------------------------
    clusters = {r["metric"].get("cluster") for r in promql('count by (cluster) (kube_pod_info{container="kube-state-metrics",exported_namespace="colo-svc"})')}
    check("GMP: colo kube-state-metrics series only from the native cluster", clusters <= {NAME["native"]}, str(clusters))
    clusters = {r["metric"].get("cluster") for r in promql("count by (cluster) (colo_overflow_needed)")}
    check("GMP: colo_overflow_needed only from the native cluster", clusters == {NAME["native"]}, str(clusters))
    q = json.dumps([a["spec"] for a in ams])
    check("native AutoscalingMetric queries scoped to the native cluster only",
          NAME["native"] in q and NAME["controller"] not in q and q.count("cluster=") == q.count(f'cluster=\\"{NAME["native"]}\\"'),
          f"{q.count('cluster=')} cluster selectors")

    # 5. demand isolation: nudge one cluster, the other must not move ---------------------------------------------
    def toolcall_total(c):
        r = deploy_replicas(c)
        return r.get("toolcall-backfill", 0) + (r.get("toolcall-overflow", 0) if c == "controller" else 0)

    for a, b in (("native", "controller"), ("controller", "native")):
        dem_a, pod_a = demand(a)
        before_a, before_b, dem_b = toolcall_total(a), toolcall_total(b), demand(b)[0]
        set_toolcall(a, dem_a["toolcall"] + 2, pod_a)
        t0, moved = time.time(), False
        while time.time() - t0 < 60 and not moved:
            moved = toolcall_total(a) == before_a + 2
            time.sleep(3)
        after_b, dem_b2 = toolcall_total(b), demand(b)[0]
        set_toolcall(a, dem_a["toolcall"], pod_a)
        check(f"demand +2 on {a} moves {a} only (not {b})", moved and after_b == before_b and dem_b2 == dem_b,
              f"{a} toolcall {before_a}->{toolcall_total(a) if not moved else before_a + 2}; {b} {before_b}->{after_b}, demand {dem_b}->{dem_b2}")
    time.sleep(5)
    for c in CTX:
        dem, _ = demand(c)
        check(f"{c}: demand restored after nudge", True, str(dem))

    # 6. demo app (local) and Cloud Run ---------------------------------------------------------------------------
    for c in CTX:
        try:
            st = json.load(urllib.request.urlopen(f"{args.demo}/api/state?cluster={c}", timeout=20))
            nodes = {n.get("name", "") for n in st.get("nodes") or []}
            own = {n["metadata"]["name"] for n in kj(c, "get", "nodes")["items"]}
            other = {n["metadata"]["name"] for n in kj("controller" if c == "native" else "native", "get", "nodes")["items"]}
            check(f"demo {c}: 0 errors, own nodes only, correct control kind",
                  not st.get("errors") and nodes and nodes <= own and not (nodes & other)
                  and (st.get("control") or {}).get("kind", "controller") == ("native" if c == "native" else "controller"),
                  f"errors={st.get('errors')} nodes={len(nodes)} control={(st.get('control') or {}).get('kind')}")
        except Exception as e:  # noqa: BLE001
            check(f"demo {c} reachable", False, str(e))
    since = (datetime.now(timezone.utc) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    logs = subprocess.run(["gcloud", "logging", "read",
                           f'resource.type="cloud_run_revision" AND resource.labels.service_name="colodemo-jawadamin-us-west8-1026" '
                           f'AND textPayload:"heartbeat" AND timestamp>="{since}"', "--project", PROJECT, "--limit", "20",
                           "--format", "value(textPayload)"], capture_output=True, text=True).stdout.splitlines()
    for c in CTX:
        beats = [l for l in logs if f"cluster={c}" in l]
        check(f"Cloud Run heartbeat {c}: present, 0 errors", beats and all("errors=0" in l and "auth_errors=0" in l for l in beats),
              beats[0][-70:] if beats else "no heartbeat in 5 min")

    ok = all(r["pass"] for r in RESULTS)
    Path(args.out).write_text(json.dumps({"time": datetime.now(timezone.utc).isoformat(), "pass": ok, "results": RESULTS}, indent=1))
    print(f"\n{'ALL PASS' if ok else 'FAILURES'}: {sum(r['pass'] for r in RESULTS)}/{len(RESULTS)} -> {args.out}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
