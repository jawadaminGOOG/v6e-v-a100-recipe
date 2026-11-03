#!/usr/bin/env python3
"""Live retests for the two fixes from the 2 Nov sweep. Restores each cluster's state at the end.

  flap   (native cluster): vLLM HPA scaleDown window 120 s. Demand 9/9 (fits one G4 node, no N4D needed),
         HPA min 1 max 2, 450 -> 1200 rps. PASS if, once the 2nd vLLM is Ready, the HPA never drops back to 1
         replica during the next 240 s at 1200 rps.
  starve (controller cluster): controller keeps stranded backfill while overflow is not Ready (v0.2.5+).
         Demand 18/18 on 2 vLLM, N4D pool capped at its current size (overflow cannot start), then
         1200 -> 300 rps so vLLM scales 2 -> 1. PASS if Ready >= demand per service in >= 99 % of samples from
         the scale-in until the Cluster Autoscaler marks the drained G4 node for deletion, and the controller
         still holds the stranded pods past the 180 s grace.

    python3 retest_fixes.py --out ../../.agents/bench/retest [--tests flap,starve]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

CTX = {"controller": "gke_northam-ce-mlai-tpu_us-west8_g4cluster-jawadamin-us-west8-0726",
       "native": "gke_northam-ce-mlai-tpu_us-west8_g4cluster-jawadamin-us-west8-1126"}
N4D_POOL = {"controller": "n4dcpu-jawadamin-us-west8-1026"}
PROJECT, REGION = "northam-ce-mlai-tpu", "us-west8"
VERDICT: dict = {}


def log(c, m):
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {c:10s} {m}", flush=True)


def k(c, *a, check=True):
    r = subprocess.run(["kubectl", "--context", CTX[c], *a], capture_output=True, text=True)
    if check and r.returncode:
        raise RuntimeError(r.stderr.strip()[:300])
    return r.stdout


def kj(c, *a):
    return json.loads(k(c, *a, "-o", "json"))


def ready(p):
    return any(x["type"] == "Ready" and x["status"] == "True" for x in p.get("status", {}).get("conditions", []))


def ctrl_pod():
    return [p["metadata"]["name"] for p in kj("controller", "-n", "colo-system", "get", "pods", "-l", "app=colo-controller")["items"]
            if ready(p)][0]


def ctrl_post(path, body):
    code = ("import json,urllib.request;print(urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8080%s',"
            "data=json.dumps(%r).encode(),headers={'Content-Type':'application/json'})).read().decode())" % (path, body))
    return json.loads(k("controller", "-n", "colo-system", "exec", ctrl_pod(), "--", "python", "-c", code))


def ctrl_get(path):
    code = "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8080%s').read().decode())" % path
    return json.loads(k("controller", "-n", "colo-system", "exec", ctrl_pod(), "--", "python", "-c", code))


def set_rate(c, rate):
    for p in kj(c, "-n", "inference", "get", "pods", "-l", "app=loadgen")["items"]:
        if ready(p):
            subprocess.run(["kubectl", "--context", CTX[c], "-n", "inference", "exec", p["metadata"]["name"], "--",
                            "python", "-c", "import json,urllib.request;urllib.request.urlopen(urllib.request.Request("
                            "'http://127.0.0.1:8080/rate',data=json.dumps({'rate':%s}).encode(),"
                            "headers={'Content-Type':'application/json'}))" % float(rate)], capture_output=True, check=True)
    log(c, f"rate {rate}")


def get_rate(c):
    p = [p["metadata"]["name"] for p in kj(c, "-n", "inference", "get", "pods", "-l", "app=loadgen")["items"] if ready(p)][0]
    return json.loads(k(c, "get", "--raw", f"/api/v1/namespaces/inference/pods/{p}:8080/proxy/stats"))["rate_total"]


def set_hpa(c, mn, mx):
    k(c, "-n", "inference", "patch", "hpa", "vllm", "-p", json.dumps({"spec": {"minReplicas": mn, "maxReplicas": mx}}))
    log(c, f"vllm hpa min={mn} max={mx}")


def get_hpa(c):
    h = kj(c, "-n", "inference", "get", "hpa", "vllm")
    return h["spec"].get("minReplicas", 1), h["spec"]["maxReplicas"]


def demand(c):
    if c == "controller":
        return ctrl_get("/config")["demand"]
    d = {x["metadata"]["name"]: x["spec"].get("replicas", 0) for x in kj(c, "-n", "colo-svc", "get", "deploy")["items"]}
    return {"sandbox": d["sandbox-backfill"], "toolcall": d["toolcall-backfill"], "analytics": d["analytics-overflow"]}


def set_demand(c, d):
    if c == "controller":
        ctrl_post("/config", {"demand": d})
    else:
        for dep, key in (("sandbox-backfill", "sandbox"), ("toolcall-backfill", "toolcall"), ("analytics-overflow", "analytics")):
            k(c, "-n", "colo-svc", "scale", f"deploy/{dep}", f"--replicas={d[key]}")
    log(c, f"demand {d}")


def snapshot(c):
    pods = kj(c, "get", "pods", "-A", "--field-selector", "status.phase!=Succeeded,status.phase!=Failed")["items"]
    nodes = kj(c, "get", "nodes", "-l", "colo.role=g4")["items"]
    vllm = [p for p in pods if p["metadata"]["namespace"] == "inference" and p["metadata"]["labels"].get("app") == "vllm"]
    svc = {}
    for s in ("sandbox", "toolcall"):
        mine = [p for p in pods if p["metadata"]["namespace"] == "colo-svc" and p["metadata"]["labels"].get("app") == s]
        svc[s] = {"ready": sum(ready(p) for p in mine),
                  "bf_ready": sum(ready(p) for p in mine if p["metadata"]["labels"].get("colo.tier") == "backfill"),
                  "of_pending": sum(1 for p in mine if p["metadata"]["labels"].get("colo.tier") == "overflow"
                                    and p["status"].get("phase") == "Pending")}
    return {"t": time.time(), "vllm_ready": sum(1 for p in vllm if ready(p) and not p["metadata"].get("deletionTimestamp")),
            "g4": [n["metadata"]["name"] for n in nodes],
            "ca_marked": [n["metadata"]["name"] for n in nodes
                          if any(t["key"] in ("ToBeDeletedByClusterAutoscaler",) for t in n["spec"].get("taints", []))],
            "svc": svc}


def wait(c, pred, timeout, what):
    t0 = time.time()
    while time.time() - t0 < timeout:
        s = snapshot(c)
        if pred(s):
            log(c, f"ok: {what} ({time.time() - t0:.0f}s)")
            return s
        time.sleep(4)
    log(c, f"TIMEOUT: {what}")
    return None


def test_flap(out):
    c = "native"
    saved = {"demand": demand(c), "hpa": get_hpa(c), "rate": get_rate(c)}
    log(c, f"saved {saved}")
    samples, res = [], {"saved": saved}
    try:
        set_demand(c, {"sandbox": 9, "toolcall": 9, "analytics": 0})
        set_hpa(c, 1, 2)
        set_rate(c, 450)
        wait(c, lambda s: s["vllm_ready"] == 1, 600, "1 vLLM")
        set_rate(c, 1200)
        t_up = time.time()
        s2 = wait(c, lambda s: s["vllm_ready"] >= 2, 900, "2nd vLLM Ready")
        res["vllm2_ready_after_s"] = s2 and round(s2["t"] - t_up, 1)
        t0 = time.time()
        while time.time() - t0 < 240:
            s = snapshot(c)
            samples.append(s)
            time.sleep(4)
        drops = [s for s in samples if s["vllm_ready"] < 2]
        events = k(c, "-n", "inference", "get", "events", "--field-selector", "involvedObject.name=vllm,reason=SuccessfulRescale",
                   "-o", "custom-columns=T:.lastTimestamp,M:.message", "--no-headers")
        downs = [l for l in events.splitlines() if "New size: 1" in l
                 and datetime.fromisoformat(l.split()[0].replace("Z", "+00:00")).timestamp() >= t_up]
        res.update(samples=len(samples), samples_below_2=len(drops), scale_down_events_since_scale_out=downs,
                   pass_=bool(s2) and not drops and not downs)
    finally:
        set_rate(c, saved["rate"])
        set_hpa(c, *saved["hpa"])
        set_demand(c, saved["demand"])
    VERDICT["flap"] = res
    log(c, f"flap verdict: {'PASS' if res.get('pass_') else 'FAIL'} {json.dumps({k_: v for k_, v in res.items() if k_ != 'saved'})}")


def n4d_pool_max(c):
    out = subprocess.run(["gcloud", "container", "node-pools", "describe", N4D_POOL[c], "--cluster",
                          CTX[c].rsplit("_", 1)[1], "--region", REGION, "--project", PROJECT, "--format",
                          "value(autoscaling.totalMaxNodeCount,autoscaling.totalMinNodeCount)"],
                         capture_output=True, text=True, check=True).stdout.split()
    return int(out[0]), int(out[1]) if len(out) > 1 and out[1] else 0


def set_n4d_max(c, mx, mn=0):
    subprocess.run(["gcloud", "container", "node-pools", "update", N4D_POOL[c], "--cluster", CTX[c].rsplit("_", 1)[1],
                    "--region", REGION, "--project", PROJECT, "--enable-autoscaling", f"--total-min-nodes={mn}",
                    f"--total-max-nodes={mx}", "--quiet"], capture_output=True, check=True)
    log(c, f"N4D pool total max nodes = {mx}")


def test_starve(out):
    c = "controller"
    saved = {"demand": demand(c), "hpa": get_hpa(c), "rate": get_rate(c), "n4d": n4d_pool_max(c)}
    log(c, f"saved {saved}")
    samples, res = [], {"saved": saved}
    try:
        set_demand(c, {"sandbox": 18, "toolcall": 18, "analytics": 0})
        set_hpa(c, 2, 2)
        set_rate(c, 1200)
        wait(c, lambda s: s["vllm_ready"] == 2 and all(v["bf_ready"] >= 18 for v in s["svc"].values()), 900,
             "2 vLLM, 36 backfill Ready")
        n4d_now = len(kj(c, "get", "nodes", "-l", "colo.role=n4d")["items"])
        set_n4d_max(c, max(1, n4d_now), 0)
        set_hpa(c, 1, 2)
        set_rate(c, 300)
        t0 = time.time()
        res["n4d_capped_at"] = n4d_now
        marked = None
        while time.time() - t0 < 1500:
            s = snapshot(c)
            samples.append(s)
            if s["ca_marked"] or len(s["g4"]) < 2:
                marked = s["t"]
                break
            time.sleep(4)
        scale_in = next((s["t"] for s in samples if s["vllm_ready"] < 2), None)
        win = [s for s in samples if scale_in and s["t"] >= scale_in]
        ok = [all(v["ready"] >= 18 for v in s["svc"].values()) for s in win]
        pend = [s for s in win if any(v["of_pending"] for v in s["svc"].values())]
        events = ctrl_get("/state").get("events", [])
        kept = [e for e in events if "kept services whose overflow is not Ready" in json.dumps(e)]
        res.update(scale_in_at=scale_in, ca_marked_after_s=(marked and scale_in) and round(marked - scale_in, 1),
                   samples=len(win), ready_ge_demand_pct=round(100 * sum(ok) / len(ok), 1) if ok else None,
                   samples_with_overflow_pending=len(pend), held_past_grace=bool(kept) or (
                       bool(win) and win[-1]["t"] - scale_in > 180 and all(o for o in ok)),
                   min_ready={s_: min(x["svc"][s_]["ready"] for x in win) for s_ in ("sandbox", "toolcall")} if win else None)
        res["pass_"] = bool(ok) and sum(ok) / len(ok) >= 0.99 and bool(pend)
    finally:
        set_n4d_max(c, *saved["n4d"])
        set_rate(c, saved["rate"])
        set_hpa(c, *saved["hpa"])
        set_demand(c, saved["demand"])
    VERDICT["starve"] = res
    log(c, f"starve verdict: {'PASS' if res.get('pass_') else 'FAIL'} {json.dumps({k_: v for k_, v in res.items() if k_ != 'saved'})}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--tests", default="flap,starve")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    th = [threading.Thread(target={"flap": test_flap, "starve": test_starve}[t], args=(out,)) for t in a.tests.split(",")]
    for t in th:
        t.start()
    for t in th:
        t.join()
    json.dump(VERDICT, open(out / "retest_verdict.json", "w"), indent=1, default=str)
    ok = all(v.get("pass_") for v in VERDICT.values()) and len(VERDICT) == len(th)
    print("RETEST", "PASS" if ok else "FAIL", "->", out / "retest_verdict.json")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
