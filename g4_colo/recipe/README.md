# G4 Co-Location at Scale on GKE — Production Recipe

This directory contains the Kubernetes plumbing to run CPU services on the spare cores of `g4-standard-48` GPU serving nodes across a fleet, without letting CPU demand scale out GPU nodes and with a guard on GPU inference tail latency.

It builds on the single-node co-location benchmark in [`../../g4-colo-readme.md`](../../g4-colo-readme.md), where an 8 vCPU / 64 GiB / 1× RTX PRO 6000 vLLM slice (`--api-server-count=4`) leaves **~39 allocatable vCPUs** per `g4-standard-48` node free for integer CPU services.

**Adopting it:** copy `overlays/production-template` and edit two things in it: `settings.yaml` (G4 reservation / Spot / on-demand and N4D pool names, model, gateway namespace, and the controller config with TTFT project, cluster name and demand) and `images:` in `kustomization.yaml` (registry, tag). The node pools come from the env vars of `infra/create-node-pools.sh`. kustomize `replacements` copy the settings into the manifests; nothing else needs editing. `base/` holds no project, registry or pool names: it uses the placeholder registry `colo-registry.invalid` and the pool names `your-g4-node-pool` / `your-n4d-node-pool`.

---

## 1. Architecture: Backfill + Overflow

Each co-located CPU service runs as **two Kubernetes Deployments** behind a single ClusterIP `Service` (`selector: {app: <svc>}`), balanced by `colo-controller`:

```
                       ┌─────────────────────────────────────────────────┐
                       │ g4-gpu ComputeClass (g4-standard-48 + RTX 6000) │
                       │  kubelet cpuManagerPolicy: static               │
   GPU traffic ──────► │  • vllm (8 CPU, 64Gi, 1 GPU, prio +1,000,000)   │
    (HPA 700 rps/pod)  │  • <svc>-backfill (2 CPU Guaranteed, prio -100) │ ◄── Only on nodes labelled colo.active=true;
                       └───────────────────────┬─────────────────────────┘     CA expendable (< -10),
                                               │                               never adds a G4 node
                            demand > G4 slots  │  or guard eviction
                                               ▼  (ColoGuardEvict)
                       ┌─────────────────────────────────────────────────┐
                       │ n4d-cpu ComputeClass (n4d-standard-16, multi-z) │
                       │  • <svc>-overflow (2 CPU Guaranteed, prio 0)    │ ◄── Triggers N4D scale-out
                       │  • colo-buffer (pause, 2 CPU, prio -5)          │ ◄── Warm N4D headroom
                       └─────────────────────────────────────────────────┘
```

### Why five primitives are required together

| Requirement | Kubernetes primitive | Manifest |
|---|---|---|
| **GPU cores stay physically isolated** | Kubelet `cpuManagerPolicy: static` + Guaranteed QoS + integer CPU requests (`cpu == limit`) | [`infra/kubelet-config.yaml`](infra/kubelet-config.yaml), [`base/vllm.yaml`](base/vllm.yaml) |
| **CPU demand never scales out GPU nodes** | `PriorityClass` `colo-backfill` (`value: -100`, below GKE Cluster Autoscaler's `-10` expendable cutoff) + `safe-to-evict: "true"` | [`base/cluster.yaml`](base/cluster.yaml), [`base/colo-services.yaml`](base/colo-services.yaml) |
| **Ordered GPU & CPU capacity tiers** | Custom `ComputeClass` `g4-gpu` (reservation → Spot → on-demand in production-template) and `n4d-cpu` (`whenUnsatisfiable: DoNotScaleUp`, `consolidationDelayMinutes: 1`, the GKE minimum) | [`base/cluster.yaml`](base/cluster.yaml), [`overlays/production-template/kustomization.yaml`](overlays/production-template/kustomization.yaml) |
| **CPU capacity kept during G4 scale-in** | Controller make-before-break dehydrate (section 3) + `colo-buffer` pause pods (`value: -5`, `terminationGracePeriodSeconds: 0`) holding warm N4D slots that `colo-overflow` (`0`) preempts | [`base/colo-services.yaml`](base/colo-services.yaml), [`base/controller.yaml`](base/controller.yaml) |
| **Cluster-enforced tenant guardrails** | `ValidatingAdmissionPolicy` `colo-g4-guard` + `ResourceQuota` + `NetworkPolicy` | [`base/guardrails.yaml`](base/guardrails.yaml) |

### `colo.active`: backfill only lands where vLLM runs

Backfill Deployments carry a required node affinity on `colo.active In ["true"]`. The controller sets `colo.active=true` on a `colo.role=g4` node when the node is Ready, not cordoned, has no `ToBeDeletedByClusterAutoscaler` taint, and has a non-terminating vLLM pod bound to it. The vLLM pod does not need to be Ready, so backfill packs while vLLM loads and compiles. A G4 node held only by `--min-nodes 1`, or one still booting, gets no backfill.

### Admission policy `colo-g4-guard`

A pod **targets G4** if it tolerates the `cloud.google.com/compute-class=g4-gpu:NoSchedule` taint (including a keyless `operator: Exists`), selects or requires (node affinity) `cloud.google.com/compute-class=g4-gpu`, `colo.role=g4` or `cloud.google.com/gke-accelerator`, or sets `spec.nodeName`. Such a pod must be either the GPU workload (namespace `inference`, GPU, Guaranteed integer CPU) or expendable backfill (`priorityClassName: colo-backfill`, no GPU, integer CPU, `requests == limits`), and must not use `runtimeClassName: gvisor` (section 2). The binding exempts only GKE system namespaces, whose DaemonSets tolerate every taint. In `colo-system` only pods running as service account `colo-node-agent` are exempt. If you run your own all-tolerating DaemonSets (logging, security agents), add their namespace to the binding. The policy matches pod CREATE and UPDATE. On UPDATE it is evaluated only when tolerations, nodeSelector, affinity or schedulingGates change, which closes the "create gated, then add a G4 toleration and selector, then drop the gate" bypass without blocking label or annotation updates.

---

## 2. Important Finding: GKE Sandbox (`gVisor`) vs. Static CPU Manager on GPU Nodes

During live validation on GKE Standard (`v1.35.8-gke.1796000`, `containerd 2.1.7`), we tested `runtimeClassName: gvisor` on Guaranteed integer-CPU backfill pods co-located with vLLM under `cpuManagerPolicy: static` (recorded in `.agents/wave-1/logs/p6_gvisor_cpuset_probe.json`):

- **Root cause:** Under CRI, kubelet creates the pod sandbox (`RunPodSandboxRequest`) in the **pod-level cgroup** (`cpuset.cpus = 0-47`) and writes exclusive `cpuset` allocations only to **container sub-cgroups** (`CreateContainerRequest`). Under `gvisor`, all application threads execute inside the `runsc-sandbox` sentry process in the pod-level cgroup (`0-47`), and gVisor's virtualized `cgroup2fs` exposes all 48 host CPUs.
- **Measured impact:** Even 1 `gvisor` pod (2 vCPU request, assigned `5,29` in `cpu_manager_state`) drove vLLM's exclusive cores (`1-4,25-28`) to **52–64% busy**; 9 `gvisor` pods pushed vLLM TTFT p99 from **20 ms → 50 ms (2.5×)**. Running the identical `sandbox` workload under default `runc` kept container processes strictly inside their assigned 2-core cpusets and held vLLM TTFT p99 at **20–22 ms (1.0–1.1×)**.
- **Recipe configuration:**
  - **`sandbox-backfill` (on shared G4 GPU nodes):** Uses default `runc` so kubelet exclusive cpusets are kernel-enforced, hardened with `runAsNonRoot: true`, `allowPrivilegeEscalation: false`, `capabilities.drop: ["ALL"]`, `seccompProfile: RuntimeDefault`, and `NetworkPolicy` blocking all egress except DNS and `toolcall`. The admission policy rejects gVisor on any G4-targeting pod.
  - **`sandbox-overflow` (on dedicated N4D CPU nodes):** Uses `runtimeClassName: gvisor` with `NPROC=2` (no co-located GPU serving loop on N4D).
  - **G4 node pool:** created **without** `--sandbox type=gvisor` ([`infra/create-node-pools.sh`](infra/create-node-pools.sh)), so G4 pods need no gVisor toleration. The demo cluster's G4 pool predates this change; `overlays/demo` adds the toleration back for it.

---

## 3. Controller loop, scale-in and latency guard (`colo-controller`)

The controller reconciles every 2 s (`LOOP_S`). Each loop it reads `colo-config`, lists nodes and pods, sets `colo.active`, runs the guard, splits each service's demand into backfill (G4 spare slots) and overflow (N4D), and scales `colo-buffer`. Full detail: [`components/controller/README.md`](components/controller/README.md).

### Spare slots and reserve
Spare CPU on a G4 node = allocatable − requests of every non-backfill pod − `reserve_cpu_per_node` (2 cores, one 2-vCPU slot left for the shared cpuset used by DaemonSets and node agents). Wave 1 measured **19 two-vCPU slots** per node with no reserve (`.agents/wave-1/logs/g1g3.verdict.json`); with the 2-core reserve the expected count is 18 (not measured).

### G4 scale-in: make-before-break dehydrate
When a G4 node loses its vLLM pod (HPA scale-in, rollout, crash), or is cordoned or marked for deletion by Cluster Autoscaler:
1. The controller removes `colo.active` at once, so no new backfill lands there, and drops the node from allocation, so overflow on N4D is scaled up first.
2. It deletes the node's remaining backfill pods, per service, only when ready overflow plus ready backfill on eligible nodes covers that service's demand, or after `dehydrate_grace_s` (180 s; longer than an N4D cold scale-up, measured ~95 s in wave-2 clause 5b), or at once if the node is cordoned or carries the Cluster Autoscaler deletion taint.
3. A vLLM `Recreate` rollout that brings vLLM back within the grace deletes nothing.
4. Draining backfill pods get `controller.kubernetes.io/pod-deletion-cost: "-1000"`, so when `<svc>-backfill` shrinks the ReplicaSet removes them before ready pods on healthy G4 nodes.

vLLM pods carry `cluster-autoscaler.kubernetes.io/safe-to-evict: "false"`. Backfill is expendable, so without it Cluster Autoscaler treats every G4 node as movable and may consolidate by evicting a serving vLLM pod onto another node; in wave-2 clause 5b that caused 138 s with no ready vLLM replica. G4 nodes are removed only after the HPA has removed their vLLM replica.

Within a service the scale plan never lowers one tier until the other tier's ready pods cover the target. Wave-2 clause 5b measured the 60 s grace this release replaced: the N1 ordering held (all 7 pods removed by the scale-down came from the drained node), but one service dipped for 34 s because two N4D nodes took ~95 s to come up; the default is now 180 s (`.agents/wave-2/logs/clause5.verdict.json`). The 180 s value is re-measured in the wave-2 final test pass. The wave-1 scale-in run kept sandbox ready ≥ demand in 100% of 213 samples with the buffer set to 15 replicas, but predates `colo.active` gating (`.agents/wave-1/logs/g1g3.verdict.json`).

### Latency guard
CPU cgroups bound core count and RAM capacity, **not** L3 cache or memory-bus bandwidth. Memory-bandwidth-heavy workloads (e.g., 3D stencil sweeps, large-array analytics) contend with CPU→GPU host transfers even on disjoint physical cores.

1. Each CPU service Deployment carries `colo.class: integer` (`sandbox`, `toolcall`) or `colo.class: memory-heavy` (`analytics`).
2. Each loop the controller takes per-node TTFT p99 over a 30 s window (`window_s`) from the configured TTFT source, on nodes that have a vLLM pod:
   - The guard only reacts when the breaching node has memory-heavy backfill. If p99 exceeds `factor × baseline_ttft_p99_ms` (1.5×, with at least 200 requests in the window) and memory-heavy backfill is present, it emits a `ColoGuardEvict` Event, labels the node `colo.block.memory-heavy=true` and deletes memory-heavy backfill pods on that node. A breach with only integer backfill (or none) is ignored.
   - Guard evictions are immediate (TTFT first). Overflow is raised in the same loop, so capacity for the evicted pods dips for one N4D pod start.
   - Backfill pods carry required node affinity `colo.block.<class> NotIn ["true"]`, so evicted pods do not return to a guarded node.
   - If p99 is still above threshold `escalate_s` after the memory-heavy block, the guard escalates to `colo.block.integer=true` and evicts integer backfill too. The recipe default is 60 s; `escalate_s: 0` disables escalation (integer backfill is then never evicted). The demo overlay uses 0.
   - After `cooldown_s` of healthy p99 (600 s by default, 45 s in the demo overlay), `ColoGuardRelease` clears the block labels.
3. **No signal fails safe.** With no baseline (`baseline_ttft_p99_ms: null`, the base and production default), too few samples, or a TTFT source that is down, stale or `none`, the guard makes no transition, blocks nothing and evicts nothing. `/state` reports `guard_state: "no_signal"` and `/metrics` reports `colo_guard_data_sufficient 0`. Alert on that metric. Set the baseline after a solo run with `POST /baseline` (same source as the guard) or in `colo-config`.

Measured in wave 1 (`.agents/wave-1/logs/g4.verdict.json`, baseline 18 ms): integer backfill kept p99 at 22 ms or below (threshold 27 ms) and the guard took no action; with memory-heavy backfill and the guard off, median p99 was 36 ms; with the guard on, it evicted within 7 s and p99 was back to 18 ms within 35 s.

### Production: TTFT source and IAM
- `overlays/demo` reads per-node TTFT histograms from `inference/loadgen` (`ttft_source.type: loadgen`).
- `overlays/production-template` reads `vllm:time_to_first_token_seconds` from Google Cloud Managed Service for Prometheus (`ttft_source.type: prometheus`), scraped by the `inference/vllm` PodMonitoring in `base/vllm.yaml` (keep it). Set `ttft_source.prometheus.project` and `ttft_source.prometheus.cluster` in the template's `settings.yaml` (`coloConfig`); `cluster` keeps other clusters' vLLM pods in the same project out of the p99 and the baseline. With `--api-server-count=4`, vLLM exposes one combined TTFT histogram per pod. Its buckets are coarse near 20–40 ms (`le` 0.02, 0.04), so p99 is interpolated; take the baseline from the same source.
- The controller's Kubernetes service account `colo-system/colo-controller` needs `roles/monitoring.viewer` through Workload Identity, and egress to `monitoring.googleapis.com:443`. `infra/create-node-pools.sh` grants it:

```bash
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')
gcloud projects add-iam-policy-binding "$PROJECT" --role=roles/monitoring.viewer --condition=None \
  --member="principal://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${PROJECT}.svc.id.goog/subject/ns/colo-system/sa/colo-controller"
```

### When the controller is down
Placement freezes: Deployment replica counts stay as they are, the guard stops, and `colo.active` labels persist. Two things still hold without the controller, because the cluster enforces them:
- **No backfill on a G4 node without vLLM.** Backfill pods carry a required `podAffinity` to `app=vllm` on the same node, so a stale `colo.active=true` label cannot attract them. Measured in wave-2 edge case E2: after vLLM left a node with the controller stopped, 17 replacement backfill pods stayed Pending instead of binding there (`.agents/wave-2/logs/final_E2_rerun.json`).
- **No GPU node is added for CPU work.** The `-100` priority and the admission policy still apply; E2 saw no G4 scale-up.

What you lose is capacity rebalancing. If Cluster Autoscaler removes the drained G4 node while the controller is down, its backfill pods go with it and nothing raises overflow. In E2, one service ran at 1 of 18 ready pods for about 4 minutes, until the controller returned and scaled overflow up within seconds. Keep controller outages short:
- A liveness probe on `/healthz` restarts a controller whose loop has not finished an iteration for 10 s (5 × `LOOP_S`, after a 60 s start-up grace).
- Alert on `time() - colo_last_tick_timestamp_seconds`.

---

## 4. Directory Layout

```
g4_colo/recipe/
├── infra/
│   ├── kubelet-config.yaml        # cpuManagerPolicy: static
│   └── create-node-pools.sh       # G4 GPU pool, multi-zone N4D CPU pool, Cloud NAT, Workload Identity (env vars required)
├── base/                          # Environment-neutral; placeholder registry and pool names; do not apply directly
│   ├── cluster.yaml               # Namespaces, PriorityClasses, g4-gpu & n4d-cpu ComputeClasses
│   ├── guardrails.yaml            # ValidatingAdmissionPolicy, ResourceQuota, NetworkPolicies
│   ├── custom-metrics-adapter.yaml# Stackdriver custom metrics adapter for Managed Prometheus HPA
│   ├── vllm.yaml                  # 8 vCPU / 64Gi / 1 GPU vLLM + PodMonitoring + 700 rps/pod HPA
│   ├── colo-services.yaml         # Generated backfill/overflow Deployments + colo-buffer + PodMonitoring
│   ├── gen_colo_services.py       # Generator for colo-services.yaml (edit this, then re-run it)
│   ├── controller.yaml            # colo-controller Deployment + RBAC + colo-config + colo-node-agent DaemonSet
│   └── loadgen.yaml               # Open-loop streaming Poisson load generator (demo only)
├── overlays/
│   ├── demo/                      # The wave-1/2 demo cluster: concrete registry, pools, demo guard settings
│   ├── production-template/       # Start here: no loadgen, Prometheus TTFT source, G4 reservation -> Spot -> on-demand
│   └── scenario-*/                # Demo scenarios on top of overlays/demo (demand and load only)
├── components/
│   ├── controller/                # Pure decision logic (colo_logic.py) + K8s reconcile loop (controller.py)
│   ├── node-agent/                # Read-only /var/lib/kubelet/cpu_manager_state + /proc/stat exporter
│   ├── worker/                    # Synthetic sandbox, toolcall, and memory-bandwidth analytics workers
│   ├── loadgen/                   # Streaming OpenAI client with per-pod 2 ms TTFT histograms
│   └── cloudbuild.yaml            # Builds and pushes the four component images (_REGISTRY required)
├── scripts/
│   └── check_readme.py            # Fails if this README's defaults differ from the manifests and code
└── tests/
    ├── admission_cases.py         # Server-side dry-run of the admission policy (incl. bypass cases) + quota
    ├── netpol_check.py            # Live verification of G4 exclusive cpusets + sandbox NetworkPolicy isolation
    └── gate.py                    # End-to-end autoscaling, CPU spill, scale-in, and latency-guard verifier
```

---

## 5. Deploy & Verify

### Step 1: Create node pools and IAM bindings
On a GKE Standard cluster (`>= 1.32`, Managed Prometheus enabled). The script refuses to run without these variables:

```bash
PROJECT=my-project CLUSTER=my-cluster REGION=us-central1 SUFFIX=prod \
G4_ZONES=us-central1-a N4D_ZONES=us-central1-a,us-central1-b \
  bash g4_colo/recipe/infra/create-node-pools.sh
```

It prints the pool names to copy into `overlays/<yours>/settings.yaml`. Set `CREATE_LOADGEN_POOL=1` only for the demo.

### Step 2: Build component images
```bash
gcloud builds submit g4_colo/recipe/components \
  --config g4_colo/recipe/components/cloudbuild.yaml \
  --project "$PROJECT" --region "$REGION" \
  --substitutions _REGISTRY="$REGION-docker.pkg.dev/$PROJECT/$REPOSITORY",_TAG=v0.2.4
```

Images are pinned (`v0.2.4`) with `imagePullPolicy: IfNotPresent`. vLLM uses the public `vllm/vllm-openai:v0.32.0`; map it to a mirror in `images:` if you have one.

### Step 3: Configure and apply an overlay
```bash
cp -r g4_colo/recipe/overlays/production-template g4_colo/recipe/overlays/prod
# edit overlays/prod/settings.yaml (pools, model, gateway namespace, coloConfig) and `images:` in overlays/prod/kustomization.yaml
kubectl kustomize g4_colo/recipe/overlays/prod      # review
kubectl apply -k g4_colo/recipe/overlays/prod

# Demo stack (includes synthetic loadgen):
kubectl apply -k g4_colo/recipe/overlays/demo
```

### Step 4: Verify guardrails and the README
```bash
python3 g4_colo/recipe/tests/admission_cases.py
python3 g4_colo/recipe/tests/netpol_check.py
python3 g4_colo/recipe/scripts/check_readme.py
```

### Sizing the quota (`max_total_cpu`)
Make-before-break briefly runs a moved pod in both tiers. Size `colo-svc-quota` `requests.cpu` to at least max total demand × 2 vCPU + one G4 node's backfill (18 pods × 2 vCPU = 36 vCPU). The recipe ships `requests.cpu: "180"` and `max_total_cpu: 144` (= 180 − 36), so the controller rejects any demand above 72 pods total (`POST /config` returns 400, an edited ConfigMap is ignored and the last good config kept) instead of letting ReplicaSets hit the quota. Raise both together.

### Sizing `buffer_replicas`
- **Cost floor:** with `buffer_replicas: 1` (the default), one `n4d-standard-16` node stays up 24×7 to hold the warm buffer pod. Set the buffer to 0 replicas for cost-first deployments, accepting that overflow then waits for Cluster Autoscaler to add an N4D node (not measured; the old 60–90 s estimate has no log behind it).
- **Absorbing a whole G4 node:** size the buffer to one G4 node's backfill (18 pods) so that a scale-in's overflow lands on warm N4D capacity. Wave 1 ran scale-in with the buffer at 15 replicas (see section 3). How fast a buffer pod is preempted and an overflow pod becomes Ready is not measured.

### vLLM compile and weight cache
`base/vllm.yaml` keeps Hugging Face weights and the vLLM/Inductor compile cache on hostPath `/var/lib/colo/hf`. This helps only when vLLM returns to a node it has already used (pod restart or reschedule). A new node added by the autoscaler starts cold: it downloads weights and compiles again, which is the main scale-out case. The speedup is not measured. For production, prefer a secondary boot disk, Hyperdisk ML or GCS FUSE for weights and cache.

### Measured scale-out timings (wave 1)
From `.agents/wave-1/logs/g1g3.verdict.json` (before `colo.active` gating and the 2 s loop): the HPA asked for a second vLLM replica 70.2 s after load rose from 300 to 1200 rps; backfill filled 17 of 19 slots on the new node 11 s after vLLM became Ready; the HPA removed the replica 110.6 s after load fell, and Cluster Autoscaler removed the extra G4 node 436 s later.

### Multi-zone G4 guidance
Where G4 capacity spans multiple zones in a region, list all G4 zones in `--node-locations` on your reservation, Spot, and on-demand G4 node pools and set `--location-policy=ANY` so Cluster Autoscaler places new `vllm` replicas into whichever zone has free G4 capacity. Both `<svc>-backfill` and `n4d-cpu` overflow operate across zones.

---

## 6. Defaults

Checked by [`scripts/check_readme.py`](scripts/check_readme.py) against `DEFAULT_CONFIG` in `components/controller/colo_logic.py`, `LOOP_S` in `controller.py`, the `colo-config` ConfigMap in `base/` and each overlay, and the manifests. "Code" is what the controller uses for a key left out of `colo-config`; "—" = not applicable.

<!-- defaults-table:start -->
| Setting | Code | base | demo | production-template |
|---|---|---|---|---|
| `loop period (s)` | 2 | 2 | 2 | 2 |
| `guard.enabled` | true | true | true | true |
| `guard.baseline_ttft_p99_ms` | 60 | null | 20 | null |
| `guard.factor` | 1.5 | 1.5 | 1.5 | 1.5 |
| `guard.window_s` | 30 | 30 | 30 | 30 |
| `guard.min_requests` | 200 | 200 | 200 | 200 |
| `guard.cooldown_s` | 600 | 600 | 45 | 600 |
| `guard.escalate_s` | 60 | 60 | 0 | 60 |
| `buffer_replicas` | 1 | 1 | 1 | 1 |
| `reserve_cpu_per_node` | 2 | 2 | 2 | 2 |
| `dehydrate_grace_s` | 180 | 180 | 180 | 180 |
| `max_total_cpu` | null | 144 | 144 | 144 |
| `ttft_source.type` | loadgen | loadgen | loadgen | prometheus |
| `colo-svc-quota requests.cpu` | — | 180 | 180 | 180 |
| `consolidationDelayMinutes g4-gpu` | — | 1 | 1 | 1 |
| `consolidationDelayMinutes n4d-cpu` | — | 1 | 1 | 1 |
| `vLLM HPA target (rps/replica)` | — | 700 | 700 | 700 |
| `vLLM HPA maxReplicas` | — | 2 | 2 | 8 |
<!-- defaults-table:end -->

The demo overlay's guard values are tuned for a presenter: a measured 20 ms baseline for Qwen3-1.7B on this shape, a 45 s cooldown so scenarios finish quickly, and escalation off so the demo shows memory-heavy eviction only.
