# G4 colo at scale: local demo app

A FastAPI backend and a static page (Chart.js from a CDN, no build step). It shows a GKE "colo at scale" cluster
live: the per-core ownership of each G4 node, N4D overflow nodes, vLLM and HPA scaling, CPU backfill vs. overflow,
the TTFT guard, cluster-autoscaler events and fleet cost. It also drives the cluster: GPU request rate, per-service
CPU demand, guard on/off, baseline, and four scripted scenarios.

## Prerequisites (live mode)

1. Authenticate with gcloud and get cluster credentials:
   ```bash
   gcloud auth login
   gcloud container clusters get-credentials g4cluster-jawadamin-us-west8-0726 \
     --region us-west8 --project northam-ce-mlai-tpu
   kubectl config current-context   # gke_northam-ce-mlai-tpu_us-west8_g4cluster-jawadamin-us-west8-0726
   ```
2. Deploy the recipe components (spec sections Workloads, Colo controller, Node agent): `inference/vllm` + HPA,
   `inference/loadgen` (port 8080), `colo-system/colo-controller` (port 8080), the
   `colo-system/colo-node-agent` DaemonSet (port 8080), and the `colo-svc` Deployments `<svc>-backfill|overflow`
   and `colo-buffer`.
3. Your identity needs RBAC for: get/list nodes, pods, events, deployments, HPAs and the `colo-config` ConfigMap,
   plus `create`/`get` on `pods/proxy` in `inference` and `colo-system`. The app reaches in-cluster HTTP services
   through the API-server pod proxy, so no port-forward is needed.

## Run

```bash
cd g4_colo/demo
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

python -m uvicorn app:app --port 8088              # live cluster, current kubeconfig context
DEMO_MOCK=1 python -m uvicorn app:app --port 8088  # synthetic cluster, no GKE needed
```

Open http://localhost:8088.

Environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `DEMO_MOCK` | `0` | `1` = built-in simulator (`mock.py`) instead of the cluster |
| `DEMO_KUBE_CONTEXT` | current | kubeconfig context to use |
| `DEMO_LOADGEN_RATE_IS_TOTAL` | `0` | `1` if each loadgen replica's `/stats.rate` is already the cluster total (default: per-replica share, summed) |
| `DEMO_SCENARIO_TIME_SCALE` | `1` | multiplies scenario `wait`/`timeout` durations |
| `DEMO_PRICE_G4_HR`, `DEMO_PRICE_VCPU_HR`, `DEMO_PRICE_GIB_HR`, `DEMO_PRICE_N4D16_HR` | 1.98, 0.0131, 0.00149, 16·vCPU+64·GiB | cost-ticker prices (USD/hr) |

## Scenarios from the command line

The step definitions in `scenarios.py` are data. The web app and the CLI run the same definitions:

```bash
python run_scenario.py --list
python run_scenario.py gpu-scale-out --out /tmp/gpu-scale-out.json
DEMO_MOCK=1 python run_scenario.py guard-integer-vs-memory
```

The JSON output contains the step log, labelled observations (`observe` steps) and a metric sample every
`--sample-s` seconds. Exit code 0 means the scenario finished; 1 means a step failed or timed out.

| Scenario | What it shows |
|---|---|
| `gpu-scale-out` (1) | rate 300 → baseline → rate 1200; the HPA goes to 2, CA adds a G4 node, and the 2nd replica becomes Ready |
| `gpu-scale-in` (2) | rate 300; the HPA drops to 1, then CA removes the G4 node. Backfill pods are expendable and do not block the removal. |
| `cpu-scale-out` (3) | small demand fits in G4 backfill; large demand spills to overflow, CA adds N4D nodes, then demand shrinks back |
| `guard-integer-vs-memory` (4) | integer backfill keeps p99 near baseline; memory-heavy backfill trips the guard (`ColoGuardEvict`) and moves to N4D |

## API

| Method | Path | Body / notes |
|---|---|---|
| GET | `/api/state` | aggregated snapshot: nodes + per-CPU owners, pods, vLLM/HPA, loadgen summary, services, controller, events, cost, `scenario` |
| POST | `/api/gpu-rate` | `{"rate": 600}`: sends `POST /rate {"rate": 600}` to every Ready loadgen pod |
| POST | `/api/demand` | `{"sandbox": n, "toolcall": n, "analytics": n}`: merged into the controller's `POST /config` |
| POST | `/api/guard` | `{"enabled": true}`: merged into the controller's `POST /config` |
| POST | `/api/baseline` | the controller's `POST /baseline` |
| POST | `/api/scenario/{name}` | `name` is a scenario name or `1`-`4`; runs asynchronously |
| GET | `/api/scenario/status` | current scenario step log + observations |
| POST | `/api/scenario/cancel` | cancels the running scenario |
| GET | `/api/scenarios` | scenario definitions |

## Files

| File | Role |
|---|---|
| `app.py` | FastAPI routes, 1 s state cache, scenario runner instance |
| `cluster.py` | live backend: kubernetes client reads plus pod-proxy calls (`/stats`, `/rate`, `/state`, `/config`, `/baseline`, `/cpus`) |
| `state.py` | pure aggregation of a raw snapshot into `/api/state` (CPU ownership, histogram merge, cost) |
| `mock.py` | time-compressed simulator that emits the same raw formats as `cluster.py` |
| `scenarios.py` | scenario step data + async runner |
| `run_scenario.py` | headless CLI, writes the JSON observation log |
| `static/` | `index.html`, `app.js`, `style.css` |
| `tools/shot.py` | headless-Chrome (CDP) screenshot after N s, e.g. `python tools/shot.py http://localhost:8088/ /tmp/demo_mock.png 150` (needs `websockets`, included with `uvicorn[standard]`) |

## How the data is read

* **CPU ownership**: the node agent's `/cpus` returns the raw kubelet `cpu_manager_state`. Each CPU in
  `entries[podUID][container]` is attributed to that pod. CPUs not in any entry are the shared pool
  (system and burstable pods, plus idle CPUs), drawn white with a fill equal to busy %. The G4 layout assumes Linux
  sibling numbering (CPU `c` and `c+24` are one physical core) and 8 cores per L3 domain (LLC 0-2). N4D nodes are
  drawn as pod blocks (one 2-CPU block per pod) with free slots and a per-CPU busy strip.
* **Latency**: loadgen histograms (`ttft_hist`, `e2e_hist`, `bucket_ms`-wide buckets, last bucket = overflow) are
  summed across loadgen replicas, then per vLLM pod, per node and overall. A percentile is the upper edge of its
  bucket. Achieved rps = Σ`e2e_hist` / `window_s`.
* **Replicas**: vLLM desired/ready come from the `inference/vllm` Deployment and its HPA. Backfill and overflow
  replicas come from the `colo-svc/<svc>-backfill|overflow` Deployments.
* **Cost**: G4 nodes × $1.98/hr + N4D nodes × (16 × $0.0131 + 64 × $0.00149) = $0.305/hr. The loadgen node is
  excluded. "Reclaimed" = CPU and memory requests of Running backfill pods on G4 nodes, valued at $0.0131/vCPU-hr
  and $0.00149/GiB-hr.
