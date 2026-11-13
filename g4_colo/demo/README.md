# G4 colo at scale: local demo app

A FastAPI backend and a static page (no build step, no CDN: Chart.js and the IBM Plex Sans font are bundled in `static/`; the page still renders if the chart library fails). It shows a GKE "colo at scale" cluster
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

Open http://localhost:8088. Keys `1`-`4` start
the scenarios (after a confirm). The top strip states the story in three parts: vCPUs reclaimed on G4 |
$/hr saved vs running the same CPU pods on N4D without colo (counts free room on the N4D nodes already
running) | GPU serving (TTFT p99 vs limit, requests/s vs target, with sparklines). The left rail holds the
rate, per-service demand and guard controls; the per-L3 core map and service lanes fill the main panel.
History charts, events and the cost method are in expanders below the fold.

Environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `DEMO_MOCK` | `0` | `1` = built-in simulator (`mock.py`) instead of the cluster |
| `DEMO_CLUSTERS` | unset | JSON list of clusters for the header selector (see [Two clusters](#two-clusters)). Unset = one cluster from `DEMO_KUBE_CONTEXT` / `GKE_ENDPOINT` as before |
| `DEMO_KUBE_CONTEXT` | current | kubeconfig context to use (single-cluster mode) |
| `DEMO_LOADGEN_RATE_IS_TOTAL` | `0` | `1` if each loadgen replica's `/stats.rate` is already the cluster total (default: per-replica share, summed) |
| `DEMO_SCENARIO_TIME_SCALE` | `1` | multiplies scenario `wait`/`timeout` durations |
| `DEMO_PRICE_G4_HR`, `DEMO_PRICE_V6E_HR`, `DEMO_PRICE_VCPU_HR`, `DEMO_PRICE_GIB_HR`, `DEMO_PRICE_N4D16_HR` | 1.98, 2.70 (on-demand; Spot ≈ 1.35), 0.0131, 0.00149, 16·vCPU+64·GiB | cost-ticker prices (USD/hr) |
| `DEMO_N4D_ALLOC_CPU`, `DEMO_N4D_ALLOC_MEM_GIB` | 15.89, 57.0 | n4d-standard-16 allocatable for the all-N4D counterfactual when no N4D node is up (otherwise read from a live N4D node, minus its daemonset requests) |
| `DEMO_FAST_SCALE` | `1` | **demo accelerator, demo-only**: on a rate change, patch HPA `vllm` `minReplicas` (2 above `DEMO_FAST_SCALE_RPS`=700, else 1) and the Deployment scale so scale-out shows in seconds. `minReplicas` is reset to 1 when the rate drops, when any scenario ends or is cancelled, and on app shutdown. The UI shows a badge whenever the live HPA has `minReplicas > 1`. Set `0` for a production-like run where only the HPA scales vLLM. |
| `DEMO_EVENTS_TTL_S` | `10` | how often cluster events are re-listed (scoped by a reason field selector, limit 200) |
| `DEMO_ERROR_LOG_INTERVAL_S` | `30` | each distinct backend error is logged at WARNING at most this often (for unattended soaks in Cloud Logging) |

## Two clusters

One app shows both recipes behind a selector in the header (`?cluster=<id>` deep link; the choice is kept in
`localStorage`). Each cluster gets its own kubernetes `ApiClient`, 1 s state cache and scenario runner, so a
scenario on one cluster never locks the other.

| id | Cluster | Recipe | Control chip |
|---|---|---|---|
| `controller` | `g4cluster-jawadamin-us-west8-0726` | `g4_colo/recipe/` | Control: colo-controller |
| `native` | `g4cluster-jawadamin-us-west8-1126` | `g4_colo/recipe-native/` | Control: HPA + scheduler |
| `substrate` | `g4cluster-jawadamin-us-west8-0726` (same as `controller`) | `g4_colo/recipe/` + Agent Substrate experiment | Control: colo-controller + Agent Substrate |
| `tpu` | `v6ecluster-jawadamin-us-east5-1126` (TPU v6e, ct6e-standard-1t) | `g4_colo/recipe-native/` | Control: HPA + scheduler |

```bash
gcloud container clusters get-credentials g4cluster-jawadamin-us-west8-1126 --region us-west8 --project northam-ce-mlai-tpu
DEMO_CLUSTERS='[
 {"id":"controller","label":"With colo-controller","recipe":"controller","context":"gke_northam-ce-mlai-tpu_us-west8_g4cluster-jawadamin-us-west8-0726"},
 {"id":"native","label":"Native K8s only","recipe":"native","context":"gke_northam-ce-mlai-tpu_us-west8_g4cluster-jawadamin-us-west8-1126"},
 {"id":"tpu","label":"TPU v6e · native","recipe":"native","accelerator":"tpu-v6e","context":"gke_northam-ce-mlai-tpu_us-east5_v6ecluster-jawadamin-us-east5-1126","name":"v6ecluster-jawadamin-us-east5-1126"}
]' python -m uvicorn app:app --port 8088
DEMO_MOCK=1 python -m uvicorn app:app --port 8088   # both clusters simulated
```

* Entry fields: `id`, `label`, `recipe` (`controller`|`native`|`substrate`; substrate also takes `base_recipe`
  `controller`|`native`, see [Agent Substrate tab](#agent-substrate-tab)), and `context` **or** `endpoint` + `ca_cert`
  (base64 PEM, Cloud Run with ADC); optional `name`, `region`, `project`, and `accelerator` (`g4`, the default,
  or `tpu-v6e`). `accelerator` sets the accelerator-host price, its vCPU fallback (48 / 44) and every G4/GPU word
  the page shows (`state.ACCELERATORS`): on `tpu-v6e` the page says "TPU v6e", "v6e #1" and "TPU load". The TPU
  node pool keeps the recipe's class labels (`compute-class=g4-gpu`, `colo.role=g4`), so placement is unchanged.
  With `DEMO_MOCK=1` the simulator emulates the entry's accelerator. The first entry is the default.
  A `context` entry uses only that context (no fallback to another cluster's credentials). A cluster whose
  credentials fail to load shows an error banner; the other cluster keeps working.
* **What differs on `native`** (no colo-controller, no `colo-config`): demand = `spec.replicas` of
  `<svc>-backfill` (sandbox, toolcall) and `analytics-overflow` (analytics never backfills: admission policy),
  so `/api/demand` patches `deployments/scale`. A G4 node is eligible iff it runs a Ready vLLM pod. Backfill
  pods with no free G4 slot stay Pending at priority -100 by design; the lanes show them as "+N waiting → N4D"
  and the `sandbox-overflow` / `toolcall-overflow` HPA tag (`colo_overflow_needed` → replicas). Overflow keeps
  1 pod per service (HPA `minReplicas: 1`), so running can exceed demand by about 1 per service.
* Not on `native`: latency guard and baseline (`/api/guard`, `/api/baseline` return 409; the rail says why)
  and scenario 4. Scenarios 1-3 run a native variant (`NATIVE_SCENARIOS`) that uses only demand and GPU rate,
  with timeouts that allow for the GMP → HPA metric lag.
* **RBAC on both clusters** (the Cloud Run SA `colodemo-run@northam-ce-mlai-tpu.iam.gserviceaccount.com`; on
  native it also patches `deployments/scale` of the 3 demand Deployments and reads HPAs in `colo-svc`):
  ```bash
  for c in g4cluster-jawadamin-us-west8-0726 g4cluster-jawadamin-us-west8-1126; do
    kubectl --context gke_northam-ce-mlai-tpu_us-west8_$c apply -f k8s.yaml -l colo-demo/rbac=true
  done
  ```
  `-l colo-demo/rbac=true` applies only the ServiceAccount, roles and bindings, not the in-cluster demo
  Deployment/Service.
* **Cloud Run**: replace `GKE_ENDPOINT`/`GKE_CA_CERT` with one `DEMO_CLUSTERS` entry per cluster carrying
  `endpoint` and `ca_cert` (from `gcloud container clusters describe <c> --region us-west8
  --format='value(endpoint,masterAuth.clusterCaCertificate)'`). The SA needs `roles/container.clusterViewer`
  on the project (both clusters). The old single-cluster variables still work when `DEMO_CLUSTERS` is unset.

## Agent Substrate tab

Recipe `substrate` watches the Agent Substrate experiment (github.com/agent-substrate/substrate) on the controller
cluster. It reads everything the `controller` tab reads, plus (read-only):

| Object | Where | Shown as |
|---|---|---|
| Worker pods (label `ate.dev/worker-pool=<pool>`) | `substrate-exp` | **W** tiles on the node map: rose tint, letter W and a horizontal-stripe fill (never colour alone); legend "Substrate Worker" |
| WorkerPool `g4-workers` (`workerpools.ate.dev/v1`) | `substrate-exp` | ready / desired (`.status.readyReplicas` / `.spec.replicas`) and the Pending pod count ("no G4 room") |
| WorkerPool `n4d-workers` + its HPA (target `n4d-workers`) | `substrate-exp` | ready / desired, HPA desired (min–max) |
| ConfigMap `actor-stats`, key `stats.json` (written by the probe) | `substrate-exp` | actors running G4 · N4D (`actors_by_node` summed by node type), resumes/s, resume p99 G4 · N4D (`resume_p99_ms_by_pool`), lost actors (`lost_actors` = CRASHED, ✕ red when > 0), "waited for a Worker N" (`resumes_waited_no_room`, neutral: expected under oversubscription), "memory resets N" (`memory_resets`, amber ▲ when > 0: resumed on a different CPU feature set, files kept); all counters cumulative since actor-load started, `failed_requests` in the tooltip |

* **Control plane** (namespace `ate-system`). Nodes labelled `ate.dev/workloadType=ate-system-infra` or `=ate-postgres`
  (keyed off the label, not the pool name or machine type: each is a dedicated node, e.g. n4d-standard-4 and
  e2-standard-4 on 0726) are drawn as their own group, "Substrate control plane", below N4D and are never counted as
  worker capacity or colo cost. Each control-plane pod on them is a tile: **C** control plane, **P** postgres, **D** a
  DaemonSet pod. A NotReady pod gets a dashed red outline and a "!" after its letter; the tooltip has Ready/NotReady,
  phase and restart count. The panel's health strip shows ready/desired for api-server, controller, router, egress,
  idp (ate-system Deployments, matched by name), podcert (Deployment `podcertificate-controller` in namespace
  `podcertificate-controller-system`) and postgres (StatefulSet), plus atelet (DaemonSet ready/desired across nodes).
  Each chip has an icon and a word: ✓ ok, ▲ degraded / scaled to 0, ✕ down, – not found. Every G4/N4D node shows an
  atelet marker (✓ atelet / ✕ atelet / – no atelet); atelet still takes its D tile (DaemonSet convention; A is
  analytics). A component with no workload falls back to its pods; ate-system missing shows "Control plane: no data
  yet". Other ate-system pods (e.g. k8s-credential-provider) get a C tile but no chip.
* **Actor load controls** (the tab's only write to Substrate's side). An in-cluster generator (Deployment
  `substrate-exp/actor-load`) reads ConfigMap `substrate-exp/actor-load`: string keys `total` (actors that exist,
  0..200), `awake` (actors kept RUNNING at once, ≤ total) and `hold_s` (seconds each awake actor stays RUNNING per
  cycle, default 5). The left control rail has an "Agent Substrate actors" group (substrate tab only, same slider
  pattern as GPU load / CPU demand): "Agent actors (total)" and "Awake now" (awake is clamped to total), with presets
  0/10/40/100 and 0/4/16/32, and a readout "Running N / M awake ▲ waiting" with a G4 / N4D / waiting bar and a
  labelled key. Slider writes are debounced 1 s and sent to
  `POST /api/substrate/actors?cluster=<id>` `{total?, awake?, hold_s?}`, which validates the ranges against the current
  spec (400 with the reason), merge-patches only the keys given (`{"data": {"awake": "16"}}`) and returns
  `{spec, patch}`. 409 on any other recipe; if the ConfigMap is missing the call fails with "deploy the actor-load
  generator first" (the demo never creates it) and the controls show "no data yet".
  Target vs actual: `awake target → running: N G4 · M N4D` from the generator's stats (`target_awake`, `actors_running`,
  `actors_running_by_pool`), with a bar: G4 solid, N4D outlined, not-yet-running dashed amber plus "▲". The mock places
  awake actors one per Ready Worker (an awake actor occupies its whole Worker; suspended actors take none), g4-workers
  first, then n4d-workers; awake beyond Ready Workers shows as waiting (▲); its n4d-workers HPA asks for more
  Workers when actors overflow G4, and Pending n4d-workers trigger an N4D scale-up.
* **Total Workers** (rail slider, 0..60; "option 2": Workers outrank CPU backfill, vLLM 1e6 > substrate-worker -5 >
  colo-backfill -100). A stock HPA `substrate-exp/g4-workers` (scaleTargetRef WorkerPool g4-workers) sets the G4 share =
  Workers that fit next to Ready vLLM; its `spec.maxReplicas` is the TOTAL Worker count. The slider merge-patches that
  `maxReplicas` (its initial value is the HPA's maxReplicas; its minimum is max(1, HPA minReplicas), an HPA cannot go
  lower). Only when that HPA does not exist does it fall back to the old WorkerPool g4-workers `scale` patch (initial
  value = WorkerPool replicas). The n4d-workers HPA runs Total − G4 share (+ busy headroom).
  Readout: "Share: G4 N · N4D M · K pending" = WorkerPool g4-workers spec.replicas · n4d-workers spec.replicas · Workers
  with no node. `POST /api/substrate/workers?cluster=<id>` `{"total": n}` (old `{"replicas": n}` still accepted; 400
  outside 0..60 or below the HPA minimum, 409 on other recipes), debounced 1 s; returns `{target, mode, total}` with
  mode `hpa-max` or `workerpool-replicas`.
* **CPU-backfill clamp** (substrate tab): the sandbox / toolcall sliders max out at floor(vCPU left / per-replica CPU),
  where vCPU left = Σ allocatable CPU of Ready G4 + N4D nodes − Σ CPU requests of every non-backfill pod on them
  (vLLM, daemonsets, Workers) − 2 vCPU per Pending Worker. Shown as "N vCPU left after Workers" under CPU demand
  (`substrate.cpu_budget` in `/api/state`). A UI clamp only; preemption enforces it in the cluster. Other tabs keep max 20.
  Mock: `DEMO_MOCK_G4_WORKERS` (default 4) is the initial Total Workers; `DEMO_MOCK_NO_G4_HPA=1` simulates a cluster
  without the g4-workers HPA (pre-option-2: extras stay Pending).
* **Cost on this tab**: Running Workers count like CPU pods. g4-workers on Ready G4 nodes add to "Reclaimed on G4"
  (vCPU, GiB, $/hr), and both g4-workers on G4 and n4d-workers on N4D join the "Saved vs CPU on N4D" counterfactual
  (first-fit-decreasing pack of every running CPU pod, with real requests). Without this, Workers that preempt backfill
  under option 2 made the saving read $0.
* Every field is optional. A missing CRD, WorkerPool, HPA or ConfigMap (Substrate not installed yet, cluster
  mid-upgrade), a 403, or a malformed `stats.json` shows "no data yet" with the reason in a tooltip; it never raises a
  cluster alert. The atelet DaemonSet in `ate-system` is drawn like any other DaemonSet (D).
* g4-workers Running on Ready G4 nodes count toward "Reclaimed on G4" on this tab only. The `controller` tab draws
  Worker pods as before (reserved/system) and its numbers do not change.
* Controls (rate, demand, guard, baseline) drive the same colo-controller as the `controller` tab. Scenarios are
  disabled on this tab (409 with the reason): two runners must never drive one cluster, and the experiment should not
  be perturbed by surprise. Run them from the `controller` tab.
* Offline: `DEMO_MOCK=1` with a `substrate` entry simulates option 2: Total Workers 4 (2 vCPU / 4 GiB each, as on 0726), a
  g4-workers HPA whose G4 share is what fits next to vLLM (Workers preempt CPU backfill), and the n4d-workers HPA for
  the rest. `DEMO_MOCK_SUBSTRATE=absent` renders the "not installed" state; `DEMO_MOCK_G4_WORKERS` sets the initial
  Total Workers; `DEMO_MOCK_ATE_DEGRADED=1` makes atenet-router 0/1 (down)
  with restarts, to see the red states.
* RBAC: `k8s.yaml` objects labelled `colo-demo/rbac-substrate=true` (get/list/watch on `workerpools.ate.dev`, pods,
  configmaps and HPAs in `substrate-exp`; workerpools, pods, HPAs, deployments, statefulsets and daemonsets in
  `ate-system`; deployments and pods in `podcertificate-controller-system`; nodes are already cluster-wide read;
  the only writes: get/patch/update on ConfigMap `actor-load`, get/patch on HPA `g4-workers` and get/patch/update on
  `workerpools/scale` `g4-workers` in `substrate-exp`, all by resourceName).
  Apply on 0726 only, after those namespaces exist:
  `kubectl --context gke_northam-ce-mlai-tpu_us-west8_g4cluster-jawadamin-us-west8-0726 apply -f k8s.yaml -l colo-demo/rbac-substrate=true`.

**Base recipe.** The substrate tab runs on top of a base recipe, set by the entry field `base_recipe`:
`"controller"` (default, backward compatible: g4_colo/recipe/, colo-controller + colo-config) or `"native"`
(g4_colo/recipe-native/: no colo-controller or colo-config reads; CPU-demand sliders patch `deployments/scale` of the
backfill Deployments; overflow HPAs in colo-svc; backfill eligibility = a Ready vLLM pod on the node, never the
`colo.active` label; no latency guard / baseline (409); control chip "native K8s + Agent Substrate"). Everything
Substrate (Workers, actors, control plane, actor load) is the same on both bases. g4-workers are expected to bind next to
vLLM by podAffinity; the demo keys Worker placement off pods, not node labels. `base_recipe` is rejected on any other
recipe. The response of `/api/state` and `/api/clusters` carries `base_recipe`. Mock: `SubstrateNativeMockBackend`
(g4-workers at priority `substrate-worker`).

Cluster 0726 runs the native recipe now (since 2026-11-12). DEMO_CLUSTERS entries for it:

```json
{"id":"native0726","label":"Native K8s · 0726","recipe":"native","context":"gke_northam-ce-mlai-tpu_us-west8_g4cluster-jawadamin-us-west8-0726"},
{"id":"substrate","label":"G4 · Agent Substrate","recipe":"substrate","base_recipe":"native","context":"gke_northam-ce-mlai-tpu_us-west8_g4cluster-jawadamin-us-west8-0726"}
```

A `controller` entry for 0726 no longer works (no colo-controller there). Previous entry, for a cluster still on the
controller recipe (keep the `controller` entry; both use the same context):

```json
{"id":"substrate","label":"G4 · Agent Substrate","recipe":"substrate","context":"gke_northam-ce-mlai-tpu_us-west8_g4cluster-jawadamin-us-west8-0726"}
```

On Cloud Run use the same `endpoint` + `ca_cert` as the `controller` entry instead of `context`.

```bash
DEMO_MOCK=1 DEMO_CLUSTERS='[
 {"id":"controller","label":"With colo-controller","recipe":"controller","context":"gke_northam-ce-mlai-tpu_us-west8_g4cluster-jawadamin-us-west8-0726"},
 {"id":"substrate","label":"G4 · Agent Substrate","recipe":"substrate","context":"gke_northam-ce-mlai-tpu_us-west8_g4cluster-jawadamin-us-west8-0726"}
]' python -m uvicorn app:app --port 8088   # open http://localhost:8088/?cluster=substrate
```

## Scenarios from the command line

The step definitions in `scenarios.py` are data. The web app and the CLI run the same definitions:

```bash
python run_scenario.py --list
python run_scenario.py gpu-scale-out --out /tmp/gpu-scale-out.json
DEMO_MOCK=1 python run_scenario.py guard-integer-vs-memory
DEMO_MOCK=1 python run_scenario.py cpu-scale-out --cluster native   # any DEMO_CLUSTERS id
```

The JSON output contains the step log, labelled observations (`observe` steps) and a metric sample every
`--sample-s` seconds. Exit code 0 means the scenario finished; 1 means a step failed or timed out.

| Scenario | What it shows |
|---|---|
| `gpu-scale-out` (1) | rate 1200; vLLM goes to 2, CA adds a G4 node, CPU pods move from N4D onto its spare cores, and the 2nd replica becomes Ready |
| `gpu-scale-in` (2) | rate 300; the HPA drops to 1, then CA removes the G4 node. Backfill pods are expendable and do not block the removal. |
| `cpu-scale-out` (3) | small demand fits in G4 backfill; large demand spills to overflow on N4D (never a GPU node), then demand shrinks back |
| `guard-integer-vs-memory` (4) | integer backfill keeps p99 near baseline; memory-heavy backfill trips the guard (`ColoGuardEvict`) and moves to N4D |

## API

| Method | Path | Body / notes |
|---|---|---|
| GET | `/api/clusters` | `{default, clusters: [{id, label, recipe, name, region, project, control, guard_available}]}` |
| GET | `/api/state` | aggregated snapshot (adds `cluster_id`, `recipe`, `control`; native also `services.<svc>.backfill.waiting` and `services.<svc>.overflow.hpa`; substrate also `substrate` = `{installed, has_data, pools.<pool>.{desired, replicas, ready, pending, on_g4, on_n4d}, g4_pending, n4d_hpa, actors, notes, control_plane: {present, health, nodes[].pods[], components[], atelet, unscheduled}}` and `nodes[].atelet` on G4/N4D nodes): nodes + per-CPU owners, pods, vLLM/HPA, loadgen summary, services, controller, events, cost (incl. `cost.counterfactual`), `story` (hero numbers), `alerts` (classified backend errors), `demo` (accelerator state), `scenario` (incl. `wait` = current metric vs target + deadline) |
| POST | `/api/gpu-rate` | `{"rate": 600}`: sends `POST /rate {"rate": 600}` to every Ready loadgen pod |
| POST | `/api/demand` | `{"sandbox": n, "toolcall": n, "analytics": n}`: sent as a patch to the controller's `POST /config` (it deep-merges). 409 while a scenario runs |
| POST | `/api/guard` | `{"enabled": true}`: sent as a patch to the controller's `POST /config`. 409 while a scenario runs |
| POST | `/api/baseline` | the controller's `POST /baseline` |
| POST | `/api/scenario/{name}` | `name` is a scenario name or `1`-`4`; runs asynchronously |
| GET | `/api/scenario/status` | current scenario step log + observations |
| POST | `/api/scenario/cancel` | cancels the running scenario and resets HPA `minReplicas` to 1 |
| POST | `/api/substrate/workers` | substrate recipe only: `{"total": 0..60}` → merge-patch HPA `substrate-exp/g4-workers` spec.maxReplicas (fallback: WorkerPool g4-workers scale when the HPA is absent); returns `{target, mode, total}`; 400 bad range, 409 other recipe |
| POST | `/api/substrate/actors` | substrate recipe only: `{"total": 0..200, "awake": 0..total, "hold_s": 1..300}` (any subset) merge-patched into ConfigMap `substrate-exp/actor-load`; returns `{spec, patch}`; 400 bad range, 409 other recipe |
| GET | `/api/scenarios` | scenario definitions for that cluster's recipe |

Every endpoint takes `?cluster=<id>` (default: the first cluster; unknown id = 404). On `native`, `/api/demand`
patches `deployments/scale`, and `/api/guard`, `/api/baseline` and scenario 4 return 409.

## Files

| File | Role |
|---|---|
| `app.py` | FastAPI routes; per cluster: 1 s state cache and scenario runner |
| `backend.py` | `DEMO_CLUSTERS` parsing and one backend per cluster |
| `cluster.py` | live backends (`LiveBackend` controller recipe, `NativeLiveBackend` native recipe, `SubstrateLiveBackend` controller + Agent Substrate reads), each with its own `ApiClient`: kubernetes client reads plus pod-proxy calls (`/stats`, `/rate`, `/state`, `/config`, `/baseline`, `/cpus`) |
| `state.py` | pure aggregation of a raw snapshot into `/api/state` (CPU ownership, histogram merge, cost) |
| `mock.py` | time-compressed simulators (`MockBackend`, `NativeMockBackend`, `SubstrateMockBackend`) that emit the same raw formats as `cluster.py` |
| `scenarios.py` | scenario step data + async runner. Each scenario starts with a `require` step that fails fast with a plain message (e.g. scale-in needs 2 vLLM replicas on 2 G4 nodes); note text is templated from live metrics |
| `run_scenario.py` | headless CLI, writes the JSON observation log |
| `static/` | `index.html`, `app.js`, `style.css` |
| `tools/shot.py` | headless-Chrome (CDP) screenshot after N s, e.g. `python tools/shot.py http://localhost:8088/ /tmp/demo_mock.png 150` (needs `websockets`, included with `uvicorn[standard]`); add `WxH` (e.g. `1440x900`) to capture exactly that viewport and print the no-scroll check plus any clipped panel; `WxH@Z` emulates browser zoom (e.g. `1920x1080@1.35`) |
| `tools/cutoff.py` | cut-off text audit: every visible element whose text does not fit (overflow hidden / ellipsis / line-clamp, text spilling past a clipping parent, SVG text outside its svg), e.g. `python tools/cutoff.py "http://localhost:8088/?cluster=substrate" 1366x768 [--open drawers|rail] [--shot out.png]`; exit 2 if anything is cut |

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

## Hosting notes

* **In-cluster copy** (`k8s.yaml`): the Service is `ClusterIP` on purpose. The app has no auth of its own and can
  change the cluster, so it is not exposed to the VPC. Reach it with
  `kubectl -n colo-system port-forward svc/colo-demo 8088:80` and open http://localhost:8088.
* **Shareable copy**: the Cloud Run service behind IAP.

### Cloud Run

* Run **one instance** (`--max-instances=1`): the scenario runner, the state cache and the accelerator restore
  live in process memory, so a second instance would show "idle" mid-scenario and allow a second concurrent run.
* Run as a dedicated service account (not the default compute SA) and bind it with `k8s.yaml`: cluster-wide
  access is read-only (nodes, pods, events); pod proxy, HPA/Deployment scale and the `colo-config` read are
  namespaced Roles in `inference`, `colo-system` and `colo-svc`. The account also needs
  `roles/container.clusterViewer` to get cluster credentials.
* The UI detects an expired IAP session (redirect, 401/403, or HTML instead of JSON) and shows a
  "Session expired - Reload" banner; data older than 6 s shows a stale banner; backend errors (e.g. a 401 from
  the GKE API) appear as a classified banner and are WARNING-logged once per 30 s per distinct message.
