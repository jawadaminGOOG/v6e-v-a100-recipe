#!/usr/bin/env python3
"""N0 spike (a): end-to-end lag of the native control path.

Step: change <svc>-backfill replicas by DELTA (pods that cannot fit on G4 stay Pending), then poll every second:
  t_metric : colo_overflow_needed{service} in Managed Prometheus reflects the change
  t_hpa    : <svc>-overflow spec.replicas reflects it (HPA acted)
  t_ready  : <svc>-overflow ready replicas reach the new target
Repeats ROUNDS times alternating +DELTA / -DELTA. Writes JSON to argv[1].

    python3 lag_probe.py out.json [service] [delta] [rounds]
"""
import json
import subprocess
import sys
import time
import urllib.parse
import urllib.request

CTX = "gke_northam-ce-mlai-tpu_us-west8_g4cluster-jawadamin-us-west8-1126"
CLUSTER = "g4cluster-jawadamin-us-west8-1126"
PROJECT = "northam-ce-mlai-tpu"
OUT = sys.argv[1]
SVC = sys.argv[2] if len(sys.argv) > 2 else "toolcall"
DELTA = int(sys.argv[3]) if len(sys.argv) > 3 else 4
ROUNDS = int(sys.argv[4]) if len(sys.argv) > 4 else 4
TIMEOUT_S = 420


def token():
    return subprocess.run(["gcloud", "auth", "application-default", "print-access-token"],
                          capture_output=True, text=True, check=True).stdout.strip()


TOK = {"v": token(), "t": time.time()}


def metric():
    if time.time() - TOK["t"] > 1200:
        TOK.update(v=token(), t=time.time())
    q = f'colo_overflow_needed{{cluster="{CLUSTER}",service="{SVC}"}}'
    url = (f"https://monitoring.googleapis.com/v1/projects/{PROJECT}/location/global/prometheus/api/v1/query?"
           + urllib.parse.urlencode({"query": q}))
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {TOK['v']}"})
    r = json.load(urllib.request.urlopen(req, timeout=10))["data"]["result"]
    return float(r[0]["value"][1]) if r else None


def kget(name):
    out = subprocess.run(["kubectl", "--context", CTX, "-n", "colo-svc", "get", "deploy", name, "-o",
                          "jsonpath={.spec.replicas} {.status.readyReplicas}"],
                         capture_output=True, text=True, check=True).stdout.split()
    return int(out[0]), int(out[1]) if len(out) > 1 else 0


def scale(name, n):
    subprocess.run(["kubectl", "--context", CTX, "-n", "colo-svc", "scale", "deploy", name, f"--replicas={n}"],
                   capture_output=True, check=True)


def main():
    rounds = []
    for i in range(ROUNDS):
        bf, _ = kget(f"{SVC}-backfill")
        m0 = metric()
        of0, _ = kget(f"{SVC}-overflow")
        d = DELTA if i % 2 == 0 else -DELTA
        want_metric = (m0 or 0) + d
        want_of = max(1, int(want_metric))  # HPA: ceil(metric / 1), floored at minReplicas 1
        t0 = time.time()
        scale(f"{SVC}-backfill", bf + d)
        r = {"round": i, "delta": d, "metric_before": m0, "overflow_before": of0, "metric_target": want_metric,
             "overflow_target": want_of, "t_metric": None, "t_hpa": None, "t_ready": None}
        while time.time() - t0 < TIMEOUT_S and r["t_ready"] is None:
            now = time.time() - t0
            try:
                m = metric()
            except Exception as e:  # noqa: BLE001
                m = None
                print("metric error", e, flush=True)
            spec, ready = kget(f"{SVC}-overflow")
            if r["t_metric"] is None and m is not None and m == want_metric:
                r["t_metric"] = round(now, 1)
            if r["t_hpa"] is None and spec == want_of:
                r["t_hpa"] = round(now, 1)
            if r["t_hpa"] is not None and ready == want_of:
                r["t_ready"] = round(now, 1)
            time.sleep(1)
        print(json.dumps(r), flush=True)
        rounds.append(r)
        time.sleep(30)
    json.dump({"service": SVC, "delta": DELTA, "rounds": rounds}, open(OUT, "w"), indent=1)


if __name__ == "__main__":
    main()
