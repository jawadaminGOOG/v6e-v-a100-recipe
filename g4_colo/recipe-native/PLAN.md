# Plan: `g4_colo/recipe-native/`: G4 co-location without a custom controller

**Goal.** A second recipe that reaches the same outcome as `g4_colo/recipe/` (spare G4 vCPU backfill, N4D overflow, no G4 scale-up for CPU demand, safe scale-in, TTFT protection) using only core Kubernetes and GKE features: ComputeClass (CCC), PriorityClass, affinity, HPA, Google Managed Prometheus (GMP) rules, and ValidatingAdmissionPolicy. The same demo UI shows both clusters behind a selector.

**Definition of "no custom code".** No process we write sits in the control path. Everything that decides replica counts or placement is YAML or PromQL that stock GKE components evaluate. `loadgen` and `node-agent` stay, because they are demo traffic and read-only telemetry, not control.

---

## 1. Controller function → native replacement

| # | What `colo-controller` does today | Native replacement in `recipe-native` | What you give up |
|---|---|---|---|
| 1 | Sets `colo.active=true` on G4 nodes running vLLM | Required `podAffinity` to `app=vllm` (already in `recipe/`). Cluster Autoscaler runs the same scheduler predicates in its simulation, so a fresh G4 node, which has no vLLM pod yet, never fits a backfill pod. CPU demand can't add G4 nodes. `PriorityClass -100` stays as a second lock. | Nothing |
| 2 | Splits demand into `backfill + overflow = demand` | **Backfill HPA** targets `colo_demand_replicas{service}`. Pods beyond the free G4 slots stay `Pending`, which is harmless at priority -100 because CA ignores them. **Overflow HPA** uses an External metric from a GMP recording rule: `demand - effective_ready_backfill`. | Pending backfill pods are visible in `kubectl`. HPA `minReplicas >= 1`, so each overflow Deployment keeps 1 pod on N4D. |
| 3 | Moves pods back from N4D when a new G4 node joins | Pending backfill pods bind as soon as vLLM lands on the new node (podAffinity becomes satisfiable). `effective_ready_backfill` rises, so the overflow HPA scales down after its stabilization window. | Overflow shrinks after the HPA stabilization window (60 s planned), not in 2 s |
| 4 | Make-before-break when G4 scales in | `effective_ready_backfill` counts only backfill pods on a node that also runs a Ready vLLM pod (PromQL join on `kube_pod_info{node}`). When vLLM leaves G4 #2, overflow scales up immediately. The node stays up for `consolidationDelayMinutes: 3` on the `g4-gpu` CCC, so G4 #2 keeps serving while N4D warms. | Overlap time is fixed by the CCC delay, not by readiness. Costs about 2 extra G4-minutes per scale-in. |
| 5 | TTFT p99 guard: blocks and evicts memory-heavy pods per node | **Default:** VAP bans `colo.class=memory-heavy` from G4, so `analytics` always runs on N4D. **Opt-in:** the `analytics-backfill` HPA target is `slots × (no TTFT breach in the last 10 min)` as a PromQL rule. A breach shrinks analytics-backfill to its floor. | Granularity is per cluster, not per node. The floor is 1 pod (`minReplicas`). No Kubernetes Events. |
| 6 | Keeps the `colo-buffer` warm N4D pause pod | The same Deployment with fixed `replicas: 1` (GKE's documented overprovisioning pattern) | Nothing |
| 7 | `runc` on G4, gVisor on N4D | Two Deployments per service, same as `recipe/` | Nothing |
| 8 | Admission guardrails | The same `colo-g4-guard` VAP, plus the memory-heavy ban | Nothing |

**Demand signal.** The demo sliders set a `colo_demand_replicas{service}` gauge served by `loadgen`, which GMP scrapes. A production cluster replaces it with one PromQL rule: `ceil(sum(rate(requests_total[1m])) / rps_per_pod)`.

---

## 2. Folder layout (mirrors `recipe/`)

```
g4_colo/recipe-native/
  README.md                 # how it works, comparison table, trade-offs
  PLAN.md                   # this plan
  base/
    cluster.yaml            # namespaces, PriorityClasses, CCCs (g4 consolidationDelay 3m)
    colo-services.yaml      # generated: backfill/overflow Deployments, Services, buffer (replicas: 1)
    gen_colo_services.py    # generator (build-time only, like recipe/)
    hpa.yaml                # generated: 2 HPAs per service
    rules.yaml              # GMP Rules: effective_ready_backfill, overflow_needed, analytics_allowed
    kube-state-metrics.yaml # GMP managed KSM package config (kube_pod_info for the node join)
    guardrails.yaml         # VAP from recipe/ + memory-heavy ban
    vllm.yaml, loadgen.yaml, custom-metrics-adapter.yaml   # synced copies, CI diff-checked against recipe/
  overlays/{demo,production-template}/
  infra/create-cluster.sh   # new cluster + G4/N4D/loadgen pools
  scripts/check_no_custom_code.py   # fails if any control-path image is ours
  tests/                    # admission cases + live gate scripts
```

Kustomize won't load files outside a kustomization root, so shared manifests are copied. `check_sync.py` fails CI if they drift from `recipe/`.

---

## 3. Demo UI: one app, two clusters

1. **Backend.** `CLUSTERS` env holds a JSON list `[{id, label, context|endpoint, recipe: controller|native}]`. `ClusterBackend` gets a `NativeBackend` sibling with the same `raw()`/`state()` contract:
   - reads HPAs, Deployments, and pods instead of the controller's `/state`
   - `set_demand` → `loadgen /demand`
   - `set_guard` and `baseline` → patch the GMP `Rules` object
2. **UI.** A cluster selector in the header (`?cluster=native` deep link). Same one-pane layout, plus a chip: `Control: colo-controller` or `Control: HPA + scheduler`.
3. **Unchanged.** Node map, L3 domains, lanes, sliders, and scenarios. Scenarios run against whichever cluster is selected.
4. **Cloud Run.** One service. The `colodemo-run` SA gets the same RBAC on both clusters.

---

## 4. Waves and falsifiable gates

| Wave | Work | Est. |
|---|---|---|
| **N0 spike** | On the new cluster, measure two things before writing the full recipe. **(a)** GMP → adapter → HPA metric lag. If p50 > 60 s, fall back to KEDA's Prometheus scaler, an add-on rather than custom code, and record that. **(b)** Confirm CA refuses a G4 scale-up for a podAffinity-to-vLLM pod at priority 0 and at -100. | 2 h |
| **N1 manifests** | Generator, HPAs, Rules, VAP, overlays, `check_no_custom_code.py`, `check_sync.py` | 3 h |
| **N2 demo** | `NativeBackend`, selector, mock mode for both, tests, redeploy to Cloud Run | 3 h |
| **N3 live gate** | Same scenarios as recipe Wave 2 clause 5, run on both clusters | 4 h |

**N3 gate.** GREEN only if every clause passes:

1. `check_no_custom_code.py` exits 0: no image of ours in the control path.
2. TTFT p99 with 18 backfill pods ≤ 1.5× solo. The controller recipe measured 1.10×.
3. G4 never scales for CPU demand: demand 200, 0 G4 scale-ups over 15 min.
4. Scale-out: ≥ 90 % of the new G4 node's slots filled ≤ 60 s after vLLM is Ready. The controller recipe filled 94 % in 4 s. Both numbers get reported.
5. Scale-in 2→1: running pods ≥ demand in ≥ 99 % of samples. The controller recipe held 100 %.
6. Steady state: |running − demand| ≤ 2 per service within 120 s of a slider change.
7. The UI switches clusters with zero scrolling at 1920×1080, 1680×1050, 1536×864, and 1440×900. Cloud Run is updated.
8. The README has a measured side-by-side table: controller vs native.

---

## 5. Risks

1. **Metric lag.** GMP to Cloud Monitoring to the adapter can take 60–120 s. That makes the overflow reaction slower than the controller's 2 s loop. N0 measures it. KEDA is the fallback.
2. **Scale-to-zero.** HPA can't go to 0 on GKE (`HPAScaleToZero` is alpha), so each overflow Deployment keeps 1 pod on N4D. The buffer already keeps an N4D node up, so the extra cost is close to zero.
3. **Pending-pod noise.** Excess backfill pods show `Pending`, and ResourceQuota counts them. Quota is sized for `demand_max`.

---

## 6. Needs your approval before N0

> [!IMPORTANT]
> New cluster `g4cluster-jawadamin-us-west8-1126` (us-west8, `northam-ce-mlai-tpu`). It runs 1–2 G4 nodes plus N4D and loadgen pools while the demo stays up, which roughly doubles current demo spend. The alternative is a second set of node pools in the existing cluster. That is cheaper, but both recipes would share one VAP and one CA, so the comparison would be less clean.
