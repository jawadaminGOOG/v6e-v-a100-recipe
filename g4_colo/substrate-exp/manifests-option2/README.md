# Option 2: Workers outrank CPU backfill; Cluster Autoscaler evicts them with grace

Applied on top of `../manifests-native` on cluster 0726. **Apply file by file** (never `kubectl apply -k ../manifests*`:
it re-applies the WorkerPools and resets their replicas).

Priority order on every pool: vLLM `gpu-serving` (1e6) > Workers `substrate-worker` (-5, PreemptLowerPriority) >
sandbox/toolcall `colo-backfill` (-100, CA-expendable).

Why this lets a G4 node leave without losing actors:
1. Cluster Autoscaler builds its scale-down simulation from non-expendable pods only, so backfill cores look free.
2. `g4-workers` HPA keeps G4 Workers <= what fits next to Ready vLLM (metric `g4-share`), capped by the user's total
   Worker count (HPA `maxReplicas`, the UI's "Total Workers" slider). When a vLLM pod terminates, the share drops,
   the extra Workers go, and the leaving node's Workers fit on the survivors in CA's simulation.
3. CA then EVICTS those Workers (grace honoured up to GKE's 10 min cap). Substrate drains the Worker and SIGTERMs its
   actors; the harness suspends them SYNCHRONOUSLY (api-guide.md "Eviction"). Rescheduled Workers preempt backfill.
4. `n4d-workers` (overflow HPA) runs total - G4 effective (+ busy headroom).

| File | What |
|---|---|
| `priority.yaml` | `substrate-worker` -5 with `PreemptLowerPriority` (PriorityClass fields are immutable: delete + create) |
| `ksm-rbac.yaml` | exp-ksm reads pods in every namespace and HPAs in substrate-exp |
| `ksm-args.json` | exp-ksm JSON patch: all namespaces, + node allocatable, pod requests, HPA max replicas |
| `g4-share-metric.yaml` | AutoscalingMetric `substrate-g4-share` |
| `g4-workers-hpa.yaml` | HPA on WorkerPool g4-workers; `maxReplicas` = total Workers |
| `overflow-metric-patch.json` | overflow metric: "g4 desired" term becomes the total (g4-workers HPA maxReplicas) |
| `../actor/counter-evict-template.yaml` | actor that flags SIGTERM instead of exiting |
