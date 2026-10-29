# colo-controller

Fills spare CPU on G4 GPU nodes with CPU-service backfill pods, sends the rest of the demand to
N4D overflow, and guards vLLM TTFT by blocking and evicting backfill on nodes whose p99 regresses.

| File | Role |
|---|---|
| `colo_logic.py` | pure decisions: quantities, config, slots, allocation, scale plan, TTFT histograms, guard state machine |
| `controller.py` | I/O: k8s client, loadgen polling, reconcile loop, HTTP API |
| `tests/test_colo_logic.py` | unit tests for `colo_logic.py` |

## Loop (every `LOOP_S`=5 s, or immediately after `POST /config` / `POST /baseline`)
1. Read ConfigMap `colo-system/colo-config` key `config.json`; create it with defaults if missing.
   An invalid ConfigMap is logged and the last good config is kept.
2. List nodes and pods (cluster-wide, non-terminal). Poll `GET http://<pod-ip>:8080/stats` on every
   Ready `inference` pod with `app=loadgen` (2 s timeout; failures are skipped).
3. **Guard**, per `colo.role=g4` node: merge every loadgen replica's `ttft_hist` for vLLM pods on that
   node, p99 = upper edge of the 2 ms bucket holding the 99th percentile (overflow bucket -> 2000 ms).
   - healthy -> `block-memory-heavy` when p99 > `factor` x `baseline_ttft_p99_ms`: label
     `colo.block.memory-heavy=true`, delete memory-heavy backfill pods on the node.
   - still breaching `escalate_s` (60 s) later -> `block-all`: also `colo.block.integer=true`,
     delete integer backfill pods.
   - healthy for `cooldown_s` -> both labels removed.
   - fewer than `min_requests` (200) samples in the window, or no baseline: no transition; the healthy
     run restarts (no data is not evidence of health), the escalation timer is kept.
   - `guard.enabled=false`: all blocks released at once.
   Each transition creates a core/v1 Event on the Node (reason `ColoGuardEvict` / `ColoGuardRelease`,
   reportingComponent `colo-controller`, namespace `default`) and a log line. Labels are reconciled
   to the guard state every tick; blocked-class backfill pods found on a blocked node are deleted every
   tick (the affinity is IgnoredDuringExecution). On restart, guard state is rebuilt from node labels.
4. **Allocation**. Eligible nodes = Ready, schedulable, `colo.role=g4`, with a Ready `inference`
   `app=vllm` pod. Spare = allocatable - requests of every non-backfill pod on the node (CPU and
   memory; effective request = max(sum containers, max init) + overhead) - `reserve_cpu_per_node`.
   Backfill pods (2 CPU each; 4/2/6 Gi) are placed one at a time, services in order sandbox,
   toolcall, analytics, onto the unblocked node with most remaining spare CPU. overflow = demand - backfill.
5. **Safe scaling** per service (`colo-svc/<svc>-backfill`, `colo-svc/<svc>-overflow`): increases apply
   at once; a decrease of one tier is held until the other tier's ready pods cover the target total.
   Patch order: overflow first when the total grows, backfill first otherwise. Unchanged replicas are
   never patched. Missing Deployments are logged and skipped.
6. `colo-system/colo-buffer` replicas = `buffer_replicas`.

## Config (`colo-config` / `config.json`)
```json
{"demand": {"sandbox": 0, "toolcall": 0, "analytics": 0},
 "guard": {"enabled": true, "baseline_ttft_p99_ms": 60, "factor": 1.5, "cooldown_s": 600,
           "window_s": 30, "escalate_s": 60, "min_requests": 200},
 "buffer_replicas": 1, "reserve_cpu_per_node": 0}
```
`escalate_s`, `min_requests` and `reserve_cpu_per_node` are optional additions to the spec schema
(defaults = spec behaviour). `window_s` is passed to loadgen as `?window_s=`; the loadgen window it
reports back is what is used (shown as `loadgen_window_s` in `/state`).

## HTTP (:8080)
| Endpoint | |
|---|---|
| `GET /state` | `config`, `services.<svc>.{demand, backfill_target, overflow_target, backfill_desired, backfill_ready, overflow_desired, overflow_ready}`, `nodes[] {name, eligible, spare_cpu, slots, blocks, ttft_p99_ms, requests_in_window, guard_state, planned_backfill}`, `buffer`, `cluster_ttft_p99_ms`, `cluster_requests_in_window`, `baseline_ttft_p99_ms`, `threshold_ttft_p99_ms`, `events` (last 100 actions), `last_tick`, `last_error` |
| `POST /config` | JSON deep-merged into the config, validated (400 on error), written to the ConfigMap |
| `POST /baseline` | baseline = current cluster p99; 400 if the window holds < `min_requests` samples |
| `GET /metrics` | Prometheus gauges `colo_service_*`, `colo_node_*`, `colo_cluster_ttft_p99_ms`, `colo_baseline_ttft_p99_ms`, `colo_guard_enabled`, `colo_buffer_replicas`, `colo_last_tick_timestamp_seconds`, `colo_tick_errors` |
| `GET /healthz` | always 200 while the process serves; includes `last_tick`, `last_error` |

```sh
curl -s -XPOST localhost:8080/config -d '{"demand":{"sandbox":20,"analytics":6}}'
curl -s -XPOST localhost:8080/baseline
```

## Environment
`LOOP_S` (5), `PORT` (8080), `LOADGEN_PORT` (8080), `LOADGEN_TIMEOUT_S` (2), `LOG_LEVEL` (INFO),
`POD_NAME` (Event reportingInstance), `COLO_SVC_NAMESPACE`, `COLO_SYSTEM_NAMESPACE`,
`INFERENCE_NAMESPACE`, `CONFIGMAP_NAME`, `BUFFER_DEPLOYMENT`, `EVENT_NAMESPACE` (default).
Uses in-cluster config, falls back to `~/.kube/config`.

## RBAC
ClusterRole (bound to the controller ServiceAccount):
| API group | Resource | Verbs | Why |
|---|---|---|---|
| `""` | nodes | get, list, watch, patch | eligibility, allocatable, block labels |
| `""` | pods | get, list, watch | requests per node, vLLM/loadgen readiness and IPs (all namespaces incl. `inference`) |
| `""` | events | create | Node events (namespace `default`) |

Role in `colo-svc`: `apps` deployments get; `apps` deployments/scale get, patch; `""` pods delete.
Role in `colo-system`: `apps` deployments get; `apps` deployments/scale get, patch;
`""` configmaps get, create, update.
(Events can instead be a Role in `default` with `create` on events.)

Network: the controller pod must reach loadgen pods on :8080 (allow it in any NetworkPolicy on
`inference`).

## Limitations
- The allocation is a count plan. The scheduler places backfill pods itself; with one class blocked on
  one node, integer pods can land on the node the plan reserved for analytics, leaving some analytics
  backfill Pending. Pending backfill never triggers scale-up (priority -100) and the hold logic keeps
  overflow until backfill is ready, so capacity is not lost, but demand on G4 can be under-filled.
- A held decrease waits for the other tier's ready pods with no timeout; if N4D cannot scale, the
  held backfill pods stay Pending (harmless, visible in `/state`).

## Tests
```sh
python3 -m venv /tmp/colo-ctl-venv && /tmp/colo-ctl-venv/bin/pip install -r requirements.txt pytest
/tmp/colo-ctl-venv/bin/python -m pytest -q tests/
```
