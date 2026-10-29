# G4 Co-Location at Scale on GKE — Production Recipe

This directory contains the Kubernetes plumbing to run CPU services on the spare cores of `g4-standard-48` GPU serving nodes across a fleet without ever letting CPU demand scale out GPU nodes or degrade GPU inference tail latency.

It builds on the single-node co-location benchmark in [`../../g4-colo-readme.md`](../../g4-colo-readme.md), where an 8 vCPU / 64 GiB / 1× RTX PRO 6000 vLLM slice (`--api-server-count=4`) leaves **~39 allocatable vCPUs** per `g4-standard-48` node free for integer CPU services.

---

## 1. Architecture: Backfill + Overflow

Each co-located CPU service runs as **two Kubernetes Deployments** behind a single ClusterIP `Service` (`selector: {app: <svc>}`), balanced continuously by `colo-controller`:

```
                       ┌─────────────────────────────────────────────────┐
                       │ g4-gpu ComputeClass (g4-standard-48 + RTX 6000) │
                       │  kubelet cpuManagerPolicy: static               │
   GPU traffic ──────► │  • vllm (8 CPU, 64Gi, 1 GPU, prio +1,000,000)   │
    (HPA 700 rps/pod)  │  • <svc>-backfill (2 CPU Guaranteed, prio -100) │ ◄── Spare cores only;
                       └───────────────────────┬─────────────────────────┘     CA expendable (< -10),
                                               │                               NEVER adds a G4 node
                            demand > G4 slots  │  or TTFT p99 > 1.5× baseline
                                               ▼  (ColoGuardEvict)
                       ┌─────────────────────────────────────────────────┐
                       │ n4d-cpu ComputeClass (n4d-standard-16, multi-z) │
                       │  • <svc>-overflow (2 CPU Guaranteed, prio 0)    │ ◄── Triggers N4D scale-out
                       │  • colo-buffer (pause, 2 CPU, prio -5)          │ ◄── Warm N4D headroom
                       └─────────────────────────────────────────────────┘
```

### Why Five Primitives Are Required Together

| Requirement | Kubernetes Primitive | Manifest |
|---|---|---|
| **GPU cores stay physically isolated** | Kubelet `cpuManagerPolicy: static` + Guaranteed QoS + integer CPU requests (`cpu == limit`) | [`infra/kubelet-config.yaml`](infra/kubelet-config.yaml), [`base/vllm.yaml`](base/vllm.yaml) |
| **CPU demand never scales out GPU nodes** | `PriorityClass` `colo-backfill` (`value: -100`, below GKE Cluster Autoscaler's `-10` expendable cutoff) + `safe-to-evict: "true"` | [`base/cluster.yaml`](base/cluster.yaml), [`base/colo-services.yaml`](base/colo-services.yaml) |
| **Ordered GPU & CPU capacity tiers** | Custom `ComputeClass` `g4-gpu` (reservation → Spot → on-demand) and `n4d-cpu` (`whenUnsatisfiable: DoNotScaleUp`, `consolidationDelayMinutes: 5`) | [`base/cluster.yaml`](base/cluster.yaml), [`overlays/production-template/kustomization.yaml`](overlays/production-template/kustomization.yaml) |
| **Zero-drop G4 scale-in** | `colo-buffer` (`value: -5`, `terminationGracePeriodSeconds: 0`) holds warm N4D slots (`>= -10` triggers N4D scale-up; preempted in <1 s by `colo-overflow` at `0`) | [`base/colo-services.yaml`](base/colo-services.yaml), [`base/controller.yaml`](base/controller.yaml) |
| **Cluster-enforced tenant guardrails** | `ValidatingAdmissionPolicy` `colo-g4-guard` + `ResourceQuota` + `NetworkPolicy` | [`base/guardrails.yaml`](base/guardrails.yaml) |

---

## 2. Important Finding: GKE Sandbox (`gVisor`) vs. Static CPU Manager on GPU Nodes

During live validation on GKE Standard (`v1.35.8-gke.1796000`, `containerd 2.1.7`), we tested `runtimeClassName: gvisor` on Guaranteed integer-CPU backfill pods co-located with vLLM under `cpuManagerPolicy: static` (recorded in `.agents/wave-1/logs/p6_gvisor_cpuset_probe.json`):

- **Root cause:** Under CRI, kubelet creates the pod sandbox (`RunPodSandboxRequest`) in the **pod-level cgroup** (`cpuset.cpus = 0-47`) and writes exclusive `cpuset` allocations only to **container sub-cgroups** (`CreateContainerRequest`). Under `gvisor`, all application threads execute inside the `runsc-sandbox` sentry process in the pod-level cgroup (`0-47`), and gVisor's virtualized `cgroup2fs` exposes all 48 host CPUs.
- **Measured impact:** Even 1 `gvisor` pod (2 vCPU request, assigned `5,29` in `cpu_manager_state`) drove vLLM's exclusive cores (`1-4,25-28`) to **52–64% busy**; 9 `gvisor` pods pushed vLLM TTFT p99 from **20 ms → 50 ms (2.5×)**. Running the identical `sandbox` workload under default `runc` kept container processes strictly inside their assigned 2-core cpusets and held vLLM TTFT p99 at **20–22 ms (1.0–1.1×)**.
- **Recipe configuration:**
  - **`sandbox-backfill` (on shared G4 GPU nodes):** Uses default `runc` so kubelet exclusive cpusets are kernel-enforced, hardened with `runAsNonRoot: true`, `allowPrivilegeEscalation: false`, `capabilities.drop: ["ALL"]`, `seccompProfile: RuntimeDefault`, and `NetworkPolicy` blocking all egress except DNS and `toolcall`.
  - **`sandbox-overflow` (on dedicated N4D CPU nodes):** Uses `runtimeClassName: gvisor` with `NPROC=2` (no co-located GPU serving loop on N4D).

---

## 3. Latency Guard (`colo-controller`)

CPU cgroups bound core count and RAM capacity, **not** L3 cache or memory-bus bandwidth. Memory-bandwidth-heavy workloads (e.g., 3D stencil sweeps, large-array analytics) contend with CPU→GPU host transfers even on disjoint physical cores.

1. Each CPU service Deployment carries `colo.class: integer` (`sandbox`, `toolcall`) or `colo.class: memory-heavy` (`analytics`).
2. `colo-controller` merges sliding 30 s per-node TTFT histograms every 5 s:
   - If a G4 node's TTFT p99 exceeds `factor × baseline_ttft_p99_ms` (default `1.5×` with `>= 200` requests in window), it emits a Kubernetes `Event` (`reason: ColoGuardEvict`), labels the node `colo.block.memory-heavy=true`, deletes `memory-heavy` backfill pods on that node, and shifts their replicas to `n4d-cpu` overflow before shrinking backfill.
   - Backfill pods carry required node affinity `colo.block.<class> NotIn ["true"]`, preventing the scheduler from placing evicted pods back onto a guarded G4 node.
   - If TTFT p99 remains above threshold `escalate_s` (60 s) after evicting `memory-heavy`, the guard escalates to `colo.block.integer=true`.
   - After `cooldown_s` (600 s) of healthy TTFT p99, `ColoGuardRelease` clears the block label.

> **Production metric source:** In the demo overlay, per-pod TTFT histograms come from `inference/loadgen`. In production (where `loadgen` is omitted), point the guard at your inference gateway's per-backend streaming TTFT histogram or Prometheus `vllm:time_to_first_token_seconds_bucket` (when running `--api-server-count=1` or once upstream multi-API-server Prometheus histogram aggregation is enabled).

---

## 4. Directory Layout

```
g4_colo/recipe/
├── infra/
│   ├── kubelet-config.yaml        # cpuManagerPolicy: static
│   └── create-node-pools.sh       # G4 GPU pool, multi-zone N4D CPU pool, Cloud NAT, Workload Identity
├── base/
│   ├── cluster.yaml               # Namespaces, PriorityClasses, g4-gpu & n4d-cpu ComputeClasses
│   ├── guardrails.yaml            # ValidatingAdmissionPolicy, ResourceQuota, NetworkPolicies
│   ├── custom-metrics-adapter.yaml# Stackdriver custom metrics adapter for Managed Prometheus HPA
│   ├── vllm.yaml                  # 8 vCPU / 64Gi / 1 GPU vLLM + PodMonitoring + 700 rps/pod HPA
│   ├── colo-services.yaml         # Generated backfill/overflow Deployments + colo-buffer + PodMonitoring
│   ├── gen_colo_services.py       # Generator keeping backfill & overflow specs symmetric
│   ├── controller.yaml            # colo-controller Deployment + RBAC + ConfigMap + colo-node-agent DaemonSet
│   └── loadgen.yaml               # Open-loop streaming Poisson load generator (demo only)
├── overlays/
│   ├── demo/                      # Live demo overlay applied to g4cluster-jawadamin-us-west8-0726
│   └── production-template/       # Drops loadgen, enables Reservation -> Spot -> On-Demand G4 priority tiers
├── components/
│   ├── controller/                # Pure decision logic (colo_logic.py) + K8s reconcile loop (controller.py)
│   ├── node-agent/                # Read-only /var/lib/kubelet/cpu_manager_state + /proc/stat exporter
│   ├── worker/                    # Synthetic sandbox, toolcall, and memory-bandwidth analytics workers
│   ├── loadgen/                   # Streaming OpenAI client with per-pod 2 ms TTFT histograms
│   └── cloudbuild.yaml            # Builds and pushes all four component images
└── tests/
    ├── admission_cases.py         # Server-side dry-run verification of ValidatingAdmissionPolicy + ResourceQuota
    ├── netpol_check.py            # Live verification of G4 exclusive cpusets + sandbox NetworkPolicy isolation
    └── gate.py                    # End-to-end autoscaling, CPU spill, scale-in, and latency-guard verifier
```

---

## 5. Deploy & Verify

### Step 1: Create Node Pools and IAM Bindings
Edit project/cluster/zone variables in [`infra/create-node-pools.sh`](infra/create-node-pools.sh) and run once on a GKE Standard cluster (`>= 1.32` with Managed Prometheus enabled):

```bash
bash g4_colo/recipe/infra/create-node-pools.sh
```

### Step 2: Build Component Images
```bash
gcloud builds submit g4_colo/recipe/components \
  --config g4_colo/recipe/components/cloudbuild.yaml \
  --project "$PROJECT" \
  --substitutions _TAG=latest
```

### Step 3: Apply the Kustomize Overlay
```bash
# Demo stack (includes synthetic loadgen):
kubectl kustomize g4_colo/recipe/overlays/demo | head -n 30
kubectl apply -k g4_colo/recipe/overlays/demo

# Production template (after replacing pool/image names in overlays/production-template/kustomization.yaml):
kubectl kustomize g4_colo/recipe/overlays/production-template
```

### Step 4: Verify Guardrails & Live Behavior
```bash
python3 g4_colo/recipe/tests/admission_cases.py
python3 g4_colo/recipe/tests/netpol_check.py
```

### Sizing `buffer_replicas` for Zero-Drop G4 Scale-In
One `g4-standard-48` node with an 8-vCPU vLLM pod provides ~39 spare vCPUs (**19 two-vCPU backfill slots**).
- **Minimal cost (`buffer_replicas: 1`, default in `base/`):** Keeps 1 `n4d-standard-16` node warm (~7 two-vCPU slots); overflow above 7 pods waits ~60–90 s for Cluster Autoscaler to add N4D nodes.
- **Zero-drop `N-1` G4 scale-in (`buffer_replicas: 15`):** Keeps 30 vCPUs (~3 `n4d-standard-16` nodes = 21 two-vCPU slots) warm at `priority: -5`, absorbing the simultaneous drain of an entire G4 node's backfill pods in `< 10 s`.

### Multi-Zone G4 Guidance
Where G4 capacity spans multiple zones in a region, list all G4 zones in `--node-locations` on your reservation, Spot, and on-demand G4 node pools and set `--location-policy=ANY` so Cluster Autoscaler places new `vllm` replicas into whichever zone currently has free G4 capacity. Both `<svc>-backfill` and `n4d-cpu` overflow operate across zones automatically.
