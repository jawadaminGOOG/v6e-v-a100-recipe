# colo-controller

Fills spare CPU on G4 GPU nodes with CPU-service backfill pods, sends the rest of the demand to
N4D overflow, and guards vLLM TTFT by blocking and evicting backfill on nodes whose p99 regresses.

| File | Role |
|---|---|
| `colo_logic.py` | pure decisions: quantities, config, node eligibility and dehydrate plan, slots, allocation, scale plan, TTFT histograms (loadgen and Prometheus), guard state machine |
| `ttft_source.py` | TTFT source I/O: loadgen `/stats` or Google Managed Prometheus |
| `controller.py` | k8s I/O: reconcile loop, parallel writes, HTTP API |
| `tests/` | `test_colo_logic.py`, `test_node_plan.py`, `test_prometheus.py` (fake Prometheus API), `test_controller.py` (one tick against fake k8s APIs) |

## Loop (every `LOOP_S`=2 s, or immediately after `POST /config` / `POST /baseline`)
1. Read ConfigMap `colo-system/colo-config` key `config.json`; create it with defaults if missing.
   An invalid ConfigMap is logged and the last good config is kept.
2. In parallel: list nodes, list pods (cluster-wide, non-terminal), read the 6
   `colo-svc/<svc>-{backfill,overflow}` Deployments and `colo-system/colo-buffer`.
   TTFT comes from the last snapshot of the background poller (see [TTFT source](#ttft-source)); a
   slow or hung source never delays a tick. A snapshot older than `TTFT_STALE_S` counts as no signal.
3. **Node eligibility** (`colo.role=g4` nodes). Eligible = node Ready, not cordoned, no
   `ToBeDeletedByClusterAutoscaler` taint, and a non-terminating `inference` `app=vllm` pod bound to
   it. The vLLM pod does **not** need to be Ready: backfill packs while vLLM loads and compiles.
4. **Guard**, only on nodes with a vLLM pod:
   - healthy -> `block-memory-heavy` when p99 > `factor` x `baseline_ttft_p99_ms` **and** the node
     has memory-heavy backfill: label `colo.block.memory-heavy=true`, delete memory-heavy backfill
     pods on the node. A breach with only integer backfill (or none) is ignored.
   - `block-memory-heavy` still breaching `escalate_s` later -> `block-all`: also
     `colo.block.integer=true`, delete integer backfill pods. `escalate_s: 0` disables escalation:
     integer backfill is then never evicted by the guard.
   - blocked and healthy for `cooldown_s` -> both labels removed.
   - **No signal** (source `none`, down, stale, < `min_requests` samples, or baseline null): no
     transition, no block, no eviction. The healthy run restarts (no data is not evidence of
     health), so an existing block stays until data shows `cooldown_s` of health. `/state` shows
     `guard_state: "no_signal"`, `/metrics` `colo_guard_data_sufficient 0`.
   - `guard.enabled=false`: all blocks released at once.
   - Guard evictions are immediate (TTFT first); overflow is raised in the same tick and backfill
     capacity on that node is held only by the scale plan, so a guard eviction can dip capacity for
     one N4D pod start.
   Each transition creates a core/v1 Event on the Node (reason `ColoGuardEvict` / `ColoGuardRelease`,
   reportingComponent `colo-controller`, namespace `default`) and a log line. Labels are reconciled to
   the guard state every tick; blocked-class backfill pods on a blocked node are deleted every tick
   (the affinity is IgnoredDuringExecution). On restart, guard state is rebuilt from node labels.
   **Nodes without vLLM**: guard reset to healthy, `colo.block.*` labels removed, guard not evaluated.
5. **`colo.active` and dehydrate** (`colo_logic.plan_node`):
   - eligible node: label `colo.active=true`. Backfill Deployments require this label (node
     affinity), so backfill only schedules onto G4 nodes with a live vLLM pod.
   - ineligible node: `colo.active` removed **at once** (no new placements) and the node is dropped
     from allocation, so overflow scales up first. Its remaining backfill pods are deleted, per
     service, only when (a) ready overflow plus ready backfill on eligible nodes covers that
     service's demand, or (b) the node has been ineligible for `dehydrate_grace_s` (60 s), or (c) the
     node is cordoned or carries the CA deletion taint. GPU scale-in is therefore make-before-break,
     and a vLLM `Recreate` rollout that brings vLLM back within the grace deletes nothing.
   - The ineligible timer is in memory; a controller restart restarts the grace (the safe direction).
6. **Allocation** over eligible nodes. Spare = allocatable - requests of every non-backfill pod on
   the node (CPU and memory; effective request = max(sum containers, max init) + overhead) -
   `reserve_cpu_per_node` (2 cores: one 2-vCPU slot left for the shared cpuset used by DaemonSets and
   node agents). Backfill pods (2 CPU each; 4/2/6 Gi) are placed one at a time, services in order
   sandbox, toolcall, analytics, onto the unblocked node with most remaining spare CPU.
   overflow = demand - backfill. Per-node `planned_backfill` is advisory: the scheduler picks nodes.
7. **Safe scaling** per service: increases apply at once; a decrease of one tier is held until the
   other tier's ready pods cover the target total, and is never held above the tier's own ready count
   (pods deleted externally are not recreated just to sit Pending). Patch order: overflow first when
   the total grows, backfill first otherwise. Unchanged replicas are never patched. Missing
   Deployments are logged and skipped. `colo-system/colo-buffer` replicas = `buffer_replicas`.
8. **Writes** run in parallel within each phase, phases in order: node labels + Events, guard
   evictions, Deployment scales (ordered within a service), dehydrate deletes. Concurrency is capped
   at `K8S_WRITE_CONCURRENCY`. A failed write is logged, counted in `colo_write_errors`, and retried
   next tick.

## Config (`colo-config` / `config.json`)
Defaults (`DEFAULT_CONFIG` in `colo_logic.py`); any key may be left out:
```json
{"demand": {"sandbox": 0, "toolcall": 0, "analytics": 0},
 "guard": {"enabled": true, "baseline_ttft_p99_ms": 60, "factor": 1.5, "cooldown_s": 600,
           "window_s": 30, "escalate_s": 60, "min_requests": 200},
 "buffer_replicas": 1,
 "reserve_cpu_per_node": 2,
 "dehydrate_grace_s": 60,
 "ttft_source": {"type": "loadgen",
                 "prometheus": {"project": "", "location": "global", "query_window_s": 60,
                                "metric": "vllm:time_to_first_token_seconds"}},
 "max_total_cpu": null}
```
| Key | Meaning |
|---|---|
| `guard.baseline_ttft_p99_ms` | solo TTFT p99. `null` = guard inert (no_signal) until set; `POST /baseline` sets it from the current source |
| `guard.window_s` | loadgen `?window_s=` (loadgen source only) |
| `guard.escalate_s` | seconds of continued breach after the memory-heavy block before integer backfill is blocked; `0` = never |
| `guard.min_requests` | samples per node in the window below which the guard has no signal |
| `dehydrate_grace_s` | max seconds backfill stays on an ineligible node waiting for overflow |
| `ttft_source.type` | `loadgen` (demo), `prometheus` (production), `none` (guard always no_signal) |
| `ttft_source.prometheus.project` | required for `prometheus`: project of the Managed Prometheus data |
| `ttft_source.prometheus.query_window_s` | `rate()` window, >= 10; keep >= 4 x the PodMonitoring interval |
| `max_total_cpu` | optional: reject demand whose total request (pods x 2 CPU) exceeds it; set to the `colo-svc` ResourceQuota `requests.cpu` minus make-before-break headroom |

## TTFT source
- **`loadgen`**: `GET http://<pod-ip>:8080/stats?window_s=<guard.window_s>` on every Ready
  `inference` `app=loadgen` pod (2 s timeout), polled every `LOOP_S`. Histograms of all loadgen
  replicas for vLLM pods on a node are summed; p99 = upper edge of the 2 ms bucket holding the 99th
  percentile.
- **`prometheus`**: one instant query per `LOOP_S` to
  `https://monitoring.googleapis.com/v1/projects/<project>/location/<location>/prometheus/api/v1/query`
  with an OAuth token from Application Default Credentials (Workload Identity):
  `sum by (pod, le)(rate(vllm:time_to_first_token_seconds_bucket{namespace="inference"}[60s]))`.
  Pods are mapped to nodes from the current pod list; buckets of pods on one node are summed and the
  PromQL `histogram_quantile(0.99, ...)` algorithm (linear interpolation in the bucket) is applied per
  node and cluster-wide. Requests in window = rate(+Inf) x `query_window_s`. Requires a
  PodMonitoring that scrapes vLLM `:8000/metrics` with the `pod` target label.
  vLLM's default TTFT buckets are coarse around typical small-model baselines (`le` 0.01, 0.02,
  0.04, 0.06, 0.08, 0.1, 0.25 s), so p99 is an interpolated estimate; take the baseline with the
  same source (`POST /baseline`) so baseline and threshold share the bias.
- **`none`**: nothing is fetched; the guard reports no_signal and never blocks or evicts.

Any error (HTTP, auth, JSON, non-success status) leaves the guard in no_signal for that poll.

## HTTP (:8080)
| Endpoint | |
|---|---|
| `GET /state` | `config`, `services.<svc>.{demand, backfill_target, overflow_target, backfill_desired, backfill_ready, overflow_desired, overflow_ready}`, `nodes[] {name, eligible, ineligible_reason, has_vllm, spare_cpu, slots, blocks, ttft_p99_ms, requests_in_window, guard_state, guard_signal, draining_pods, ineligible_for_s, planned_backfill}`, `buffer`, `guard_state` (`ok`/`no_signal`/`disabled`), `guard_data_sufficient`, `ttft_source {type, ok, reason, age_s, window_s}`, `cluster_ttft_p99_ms`, `cluster_requests_in_window`, `baseline_ttft_p99_ms`, `threshold_ttft_p99_ms`, `loadgen_window_s`, `events` (last 100 actions), `last_tick`, `last_loop`, `last_error` |
| `GET /config` | current validated config |
| `POST /config` | JSON deep-merged into the config, validated (400 on error), written to the ConfigMap |
| `POST /baseline` | baseline = current cluster p99 from the configured source; 400 if no signal or < `min_requests` samples |
| `GET /metrics` | Prometheus gauges `colo_service_*`, `colo_node_*` (incl. `colo_node_draining_pods`, `colo_node_guard_data_sufficient`), `colo_guard_data_sufficient`, `colo_ttft_source_up`, `colo_ttft_source_age_seconds`, `colo_cluster_ttft_p99_ms`, `colo_baseline_ttft_p99_ms`, `colo_guard_enabled`, `colo_buffer_replicas`, `colo_last_tick_timestamp_seconds`, `colo_tick_errors`, `colo_write_errors` |
| `GET /healthz` | **liveness**: 503 when the loop has not finished an iteration (good or failed) for `LIVENESS_STALE_S` (5 x `LOOP_S` = 10 s), after a `STARTUP_GRACE_S` (60 s) start-up grace; 200 otherwise |
| `GET /readyz` | **readiness**: 200 whenever the HTTP server is up |

`colo_guard_data_sufficient` is 1 only when every G4 node with vLLM had enough data this tick; it is 0
with no vLLM node, with `ttft_source: none`, or with a null baseline. Alert when it stays 0 while
vLLM serves traffic: the guard is inert. Alert on `time() - colo_last_tick_timestamp_seconds` too:
repeated failed ticks keep `/healthz` green (the loop is alive) but stop reconciling.

Probes for the Deployment:
```yaml
readinessProbe: {httpGet: {path: /readyz, port: 8080}, periodSeconds: 5}
livenessProbe:
  httpGet: {path: /healthz, port: 8080}
  periodSeconds: 10
  failureThreshold: 3
```

```sh
curl -s -XPOST localhost:8080/config -d '{"demand":{"sandbox":20,"analytics":6}}'
curl -s -XPOST localhost:8080/baseline
```

## Environment
`LOOP_S` (2), `PORT` (8080), `LOADGEN_PORT` (8080), `LOADGEN_TIMEOUT_S` (2), `PROM_TIMEOUT_S` (10),
`PROM_ENDPOINT` (`https://monitoring.googleapis.com`), `TTFT_STALE_S` (max(15, 5 x `LOOP_S`)),
`LIVENESS_STALE_S` (5 x `LOOP_S`), `STARTUP_GRACE_S` (60), `K8S_WRITE_CONCURRENCY` (16),
`LOG_LEVEL` (INFO), `POD_NAME` (Event reportingInstance), `COLO_SVC_NAMESPACE` (colo-svc),
`COLO_SYSTEM_NAMESPACE` (colo-system), `INFERENCE_NAMESPACE` (inference), `CONFIGMAP_NAME`
(colo-config), `BUFFER_DEPLOYMENT` (colo-buffer), `EVENT_NAMESPACE` (default).
Uses in-cluster config, falls back to `~/.kube/config`.

## RBAC
ClusterRole (bound to the controller ServiceAccount):
| API group | Resource | Verbs | Why |
|---|---|---|---|
| `""` | nodes | get, list, watch, patch | eligibility, allocatable, taints, `colo.active` and block labels |
| `""` | pods | get, list, watch | requests per node, vLLM/loadgen presence and IPs (all namespaces incl. `inference`) |
| `""` | events | create | Node events (namespace `default`) |

Role in `colo-svc`: `apps` deployments get; `apps` deployments/scale get, patch; `""` pods delete.
Role in `colo-system`: `apps` deployments get; `apps` deployments/scale get, patch;
`""` configmaps get, create, update.
(Events can instead be a Role in `default` with `create` on events.)

`ttft_source: prometheus` also needs Google Cloud IAM: the KSA `colo-system/colo-controller` must be
able to read Managed Prometheus, e.g. with Workload Identity Federation:
```sh
gcloud projects add-iam-policy-binding <project> --role=roles/monitoring.viewer \
  --member="principal://iam.googleapis.com/projects/<project-number>/locations/global/workloadIdentityPools/<project>.svc.id.goog/subject/ns/colo-system/sa/colo-controller"
```

Network: with `loadgen`, the controller pod must reach loadgen pods on :8080 (allow it in any
NetworkPolicy on `inference`). With `prometheus`, it needs egress to `monitoring.googleapis.com:443`.

## When the controller is down
Labels persist: `colo.active` stays on a node after its vLLM leaves, so new backfill can still land
there; the guard stops; demand changes and the overflow/backfill split freeze. Alert on
`colo_last_tick_timestamp_seconds`. The liveness probe restarts a wedged loop.

## Limitations
- The allocation is a count plan. The scheduler places backfill pods itself; with one class blocked on
  one node, integer pods can land on the node the plan reserved for analytics, leaving some analytics
  backfill Pending. Pending backfill never triggers scale-up (priority -100) and the hold logic keeps
  overflow until backfill is ready, so capacity is not lost, but demand on G4 can be under-filled.
- When the backfill Deployment shrinks, the ReplicaSet chooses which pods to delete; it may pick a
  pod on an eligible node while the draining node's pods are deleted by dehydrate, causing a brief
  re-creation on an eligible node.
- A held decrease waits for the other tier's ready pods with no timeout; if N4D cannot scale, the
  held backfill pods stay (harmless, visible in `/state`). Dehydrate itself is bounded by
  `dehydrate_grace_s`.
- One vLLM pod per node is modelled (`has_vllm` is boolean).
- Single replica, no leader election.

## Tests
```sh
python3 -m venv /tmp/colo-ctl-venv && /tmp/colo-ctl-venv/bin/pip install -r requirements.txt pytest
/tmp/colo-ctl-venv/bin/python -m pytest -q tests/
```
Without the runtime dependencies, `test_controller.py` and the HTTP part of `test_prometheus.py`
are skipped; the pure tests still run.
