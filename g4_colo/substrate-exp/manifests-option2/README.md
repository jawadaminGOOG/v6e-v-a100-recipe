# Option 2: Workers outrank CPU backfill; Cluster Autoscaler evicts them with grace

Applied on top of `../manifests-native` on cluster 0726. **Apply file by file** (never `kubectl apply -k ../manifests*`:
it re-applies the WorkerPools and resets their replicas).

Priority order on every pool: vLLM `gpu-serving` (1e6) > Workers `substrate-worker` (-5, PreemptLowerPriority) >
sandbox/toolcall `colo-backfill` (-100, CA-expendable).

Why this lets a G4 node leave without losing actors:
1. Cluster Autoscaler builds its scale-down simulation from non-expendable pods only, so backfill cores look free.
2. `g4-workers` HPA (`maxReplicas: 60`) keeps G4 Workers = `min(G4 spare slots next to Ready vLLM, max(colo.gke.io/total-workers, ceil(busy / 0.8)))`.
   The user's Total Workers floor lives in annotation `colo.gke.io/total-workers` on `HPA substrate-exp/g4-workers`
   (UI "Total Workers" slider), so busy headroom claims spare G4 cores first when a 2nd G4 node scales up. When a vLLM pod
   terminates, its node's slots drop out of `g4-share` immediately, the extra Workers go, and the leaving node's Workers fit
   on the survivors in CA's simulation.
3. CA then EVICTS those Workers (grace honoured up to GKE's 10 min cap). Substrate drains the Worker and SIGTERMs its
   actors; the harness suspends them SYNCHRONOUSLY (api-guide.md "Eviction"). Rescheduled Workers preempt backfill.
4. `n4d-workers` (overflow HPA) runs `max(colo.gke.io/total-workers, ceil(busy / 0.8)) - G4 effective`.

| File | What |
|---|---|
| `priority.yaml` | `substrate-worker` -5 with `PreemptLowerPriority` (PriorityClass fields are immutable: delete + create) |
| `ksm-rbac.yaml` | exp-ksm reads pods, HPAs and CRDs (for custom-resource-state) |
| `ksm-args.json` | exp-ksm JSON patch: all namespaces, + node allocatable, pod requests, custom-resource-state for `colo.gke.io/total-workers` |
| `g4-share-metric.yaml` | AutoscalingMetric `substrate-g4-share`: `min(g4_capacity, max(total_workers, ceil(busy / 0.8)))` |
| `g4-workers-hpa.yaml` | HPA on WorkerPool g4-workers (`minReplicas: 0`, `maxReplicas: 60`, annotation `colo.gke.io/total-workers`) |
| `overflow-metric-patch.json` | overflow metric: floor term reads `colo.gke.io/total-workers` |
| `../actor/counter-evict-template.yaml` | actor that flags SIGTERM instead of exiting |
