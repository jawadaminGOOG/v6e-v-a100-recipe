# Using the idle CPU on a g4-standard-48: GPU inference and CPU work on one node

**Question.** A `g4-standard-48` has 48 vCPU and 180 GB of memory next to one RTX PRO 6000 GPU. With the GPU
workload held to 8 vCPU / 64 GiB, what happens to it when the remaining vCPU run CPU work, and what happens to that
CPU work?

**Findings** (5 runs per scenario, change vs the GPU workload running alone):

| Co-located CPU work | vCPU used of 40 spare | TTFT p50 | TTFT p99 | End-to-end p99 | Output tokens/s | Failed requests |
|---|---|---|---|---|---|---|
| Compute-type (14 integer SPEC benchmarks) | 20 | +4.6% | +4.0% | +1.9% | −0.1% | 0 |
| Compute-type | 38 | +6.0% | +6.1% | +3.4% | 0.0% | 0 |
| Memory-bandwidth-heavy (fotonik3d, lbm) | 20 | +15.7% | +76.6% | +56.1% | +0.1% | 0 |
| Memory-bandwidth-heavy | 38 | +16.8% | +108.7% | +79.9% | 0.0% | 0 |

- Node CPU utilisation rises from **5.2%** (GPU workload only) to **79.8%** with 38 vCPU of co-located work. GPU
  utilisation stays at 98%.
- Co-located SPEC benchmarks run within ±1% of their solo runtime for 11 of 16 benchmarks at 38 vCPU. The largest
  change is +3.8% (`749.fotonik3d_r`).
- At saturating load, maximum throughput is unchanged alongside compute-type work (0.0% at 20 and 38 vCPU) and
  4-5% lower alongside memory-bandwidth-heavy work (−3.8% at 20 vCPU, −4.5% at 38 vCPU).
- Without co-location, giving the GPU workload all 48 vCPU instead of 8 shows no consistent capacity difference
  (single probes, within ±5%) and about 3% lower TTFT p50 (2 runs on a later day, not interleaved).
- CPU steal is 0.00% in every run.

This branch extends the G4 benchmark in [g4-readme.md](g4-readme.md) using the same workload, and follows the method
of [*The Idle Half of a GPU Pod*](https://medium.com/google-cloud/the-idle-half-of-a-gpu-pod-8ec3ffaddc4c) on a
`g4-standard-48`.

---

## 1. Setup

![g4-standard-48 layout](https://raw.githubusercontent.com/jawadaminGOOG/v6e-v-a100-recipe/g4-colo/g4_colo/diagrams/g4_layers.png)

| Item | Value |
|---|---|
| Machine | `g4-standard-48` on GKE (us-west8-a): AMD EPYC 9B45, 24 cores / 48 threads, 3 L3 domains, 180 GB, 1 × RTX PRO 6000 (96 GB) |
| Isolation | kubelet static CPU manager; every pod is Guaranteed QoS with integer CPUs, so each gets exclusive whole cores |
| GPU workload | vLLM v0.32.0 (`vllm/vllm-openai@sha256:befccf0e…`), `Qwen/Qwen3-1.7B-Base`, FP16, prefix caching, `--api-server-count=4`; pod limited to **8 vCPU / 64 GiB / 1 GPU** on CPUs `1-4,25-28` |
| CPU workload | SPEC CPU 2026 v1.0.1 rate, train size, the 16 benchmarks used in the article (GCC 14, `-O3 -march=x86-64-v3`) |
| Co-location levels | **50%**: 20 copies on 20 vCPU (CPUs `5-14,29-38`) · **95%**: 38 copies on 38 vCPU (CPUs `5-23,29-47`) |
| System | core 0 (CPUs `0,24`) stays in Kubernetes' shared pool for kubelet and daemons; 38 of 40 spare vCPU is the most a workload can claim |
| Load | same prompts as [g4-readme.md](g4-readme.md) (~600-token prompts, ~11 output tokens), streamed, sent at a fixed **625 requests/s**: 75% of the 832 RPS peak from short capacity probes, about 78% of the 801-805 RPS sustained over a full run |

## 2. Method

**Scenarios.** Five kinds of run, each repeated 5 times:

| Scenario | GPU workload | SPEC |
|---|---|---|
| GPU solo | load on | off |
| Co-located 50% | load on | 20 copies |
| Co-located 95% | load on | 38 copies |
| SPEC solo 50% / 95% | idle | 20 / 38 copies |

The 25 runs were interleaved in random order across 5 blocks, so drift over the 9-hour session spreads evenly across
scenarios. Placement was identical in every run and recorded from the kubelet's CPU-manager state.

**Fixed-rate load.** Requests go out on a fixed schedule regardless of response time, so any slowdown shows up as
higher latency rather than as fewer requests sent.

**Workload classes.** SPEC runs its benchmarks one after another: the 14 integer benchmarks first (about 12 minutes),
then the two memory-bandwidth-bound floating-point benchmarks `749.fotonik3d_r` and `782.lbm_r` (about 4-5 minutes).
Each co-located run is split at the start time SPEC logs for `749.fotonik3d_r`. GPU-workload requests are assigned to
a class by send time. Solo runs are cut at the same offsets into the run, so both groups cover the same time-in-run.

**What is measured.** At the client: time to first token (TTFT), inter-token latency (ITL: each request's mean gap
between streamed tokens; the table reports the median across requests), end-to-end latency, output tokens/s, failed
requests. On the node at 1 s: per-CPU utilisation and steal, memory, and GPU utilisation, power and clocks. From
SPEC: per-benchmark runtime.

**Statistics.** Each run contributes one number per metric. The change vs solo is mean(co-located) / mean(solo) − 1
over the 5 runs. The 95% interval shown is the wider of a percentile bootstrap and a Welch t interval.

**Run checks.** Every run had 0 failed requests, client schedule lag p99 ≈ 2 ms (the load generator was never the
bottleneck), all 16 SPEC benchmarks completed successfully, and the load ran past the end of SPEC. All 25 runs
met these checks; none were discarded.

## 3. Results

![Change vs solo](https://raw.githubusercontent.com/jawadaminGOOG/v6e-v-a100-recipe/g4-colo/g4_colo/results/plots/vllm_delta_summary.png)

*Change vs solo for each metric, with the memory-bandwidth-heavy class shown per benchmark (`749.fotonik3d_r`,
`782.lbm_r`). Error bars are 95% CIs.*

### GPU workload alongside compute-type co-located work (14 integer SPEC CPU 2026 benchmarks)

About 12 minutes of each run. Mean of 5 runs; brackets are the 95% CI of the change vs solo.

| Metric | Solo | Colo 50% (20 vCPU) | Colo 95% (38 vCPU) |
|---|---|---|---|
| TTFT p50 | 35.6 ms | 37.2 ms (+4.6% [+4.1, +5.1]) | 37.7 ms (+6.0% [+5.7, +6.2]) |
| TTFT p99 | 55.3 ms | 57.4 ms (+4.0% [+2.0, +6.0]) | 58.7 ms (+6.1% [+4.3, +8.0]) |
| Inter-token latency p50 | 11.7 ms | 11.8 ms (+0.8% [+0.3, +1.3]) | 12.0 ms (+1.9% [+1.6, +2.2]) |
| End-to-end p50 | 154.0 ms | 156.5 ms (+1.7% [+1.1, +2.2]) | 158.2 ms (+2.8% [+2.5, +3.0]) |
| End-to-end p99 | 219.9 ms | 223.7 ms (+1.9% [-0.5, +4.3]) | 227.3 ms (+3.4% [+1.4, +5.3]) |
| Output tokens/s | 6,888 | 6,875 (-0.1% [-0.3, +0.1]) | 6,887 (-0.0% [-0.1, +0.1]) |

Failed requests while co-located: 0.

### GPU workload alongside memory-bandwidth-heavy co-located work (749.fotonik3d_r, 782.lbm_r)

About 4 minutes of each run. Mean of 5 runs; brackets are the 95% CI of the change vs solo.

| Metric | Solo | Colo 50% (20 vCPU) | Colo 95% (38 vCPU) |
|---|---|---|---|
| TTFT p50 | 35.8 ms | 41.4 ms (+15.7% [+14.6, +16.8]) | 41.8 ms (+16.8% [+14.3, +19.3]) |
| TTFT p99 | 55.1 ms | 97.2 ms (+76.6% [+73.3, +79.9]) | 115.0 ms (+108.7% [+103.7, +113.7]) |
| Inter-token latency p50 | 11.8 ms | 13.0 ms (+10.2% [+9.1, +11.4]) | 13.2 ms (+11.8% [+10.1, +13.6]) |
| End-to-end p50 | 154.6 ms | 171.6 ms (+10.9% [+9.8, +12.1]) | 174.1 ms (+12.6% [+10.7, +14.4]) |
| End-to-end p99 | 220.4 ms | 342.8 ms (+56.1% [+52.1, +60.1]) | 396.5 ms (+79.9% [+76.3, +83.5]) |
| Output tokens/s | 6,914 | 6,917 (+0.1% [-0.3, +0.5]) | 6,913 (-0.0% [-0.2, +0.2]) |

Failed requests while co-located: 0.

### CPU workload: SPEC CPU 2026 rate runtime, co-located vs solo

| Benchmark | 50%: solo → colo (Δ, 95% CI) | 95%: solo → colo (Δ, 95% CI) |
|---|---|---|
| 706.stockfish_r | 50.0 s → 49.7 s (-0.77% [-3.24, +0.81]) | 52.4 s → 51.3 s (-2.04% [-3.72, -0.88]) |
| 707.ntest_r | 46.6 s → 48.5 s (+4.03% [+3.72, +4.33]) | 48.0 s → 49.2 s (+2.56% [+2.16, +2.96]) |
| 708.sqlite_r | 28.0 s → 28.1 s (+0.38% [+0.04, +0.72]) | 28.8 s → 29.0 s (+0.71% [+0.54, +0.86]) |
| 710.omnetpp_r | 25.1 s → 25.1 s (+0.15% [-0.53, +0.82]) | 25.9 s → 26.0 s (+0.57% [-0.10, +1.44]) |
| 714.cpython_r | 25.8 s → 25.8 s (+0.10% [-0.94, +1.16]) | 26.2 s → 26.1 s (-0.44% [-1.02, +0.08]) |
| 721.gcc_r | 39.0 s → 39.8 s (+2.05% [+1.73, +2.37]) | 40.3 s → 41.0 s (+1.79% [+1.60, +1.98]) |
| 723.llvm_r | 40.0 s → 40.4 s (+0.98% [+0.32, +1.50]) | 41.2 s → 41.6 s (+0.94% [+0.62, +1.29]) |
| 727.cppcheck_r | 64.9 s → 65.1 s (+0.26% [-0.52, +1.37]) | 67.3 s → 67.6 s (+0.48% [-0.00, +0.96]) |
| 729.abc_r | 51.6 s → 51.9 s (+0.69% [+0.02, +1.34]) | 53.6 s → 54.0 s (+0.79% [+0.52, +1.08]) |
| 734.vpr_r | 50.6 s → 50.7 s (+0.22% [-0.93, +1.36]) | 54.3 s → 54.5 s (+0.52% [-0.64, +1.65]) |
| 735.gem5_r | 36.5 s → 36.7 s (+0.49% [+0.15, +0.78]) | 37.3 s → 37.5 s (+0.31% [-0.21, +0.77]) |
| 749.fotonik3d_r | 111.7 s → 110.2 s (-1.33% [-2.66, +0.12]) | 128.1 s → 132.9 s (+3.76% [+2.38, +5.12]) |
| 750.sealcrypto_r | 57.0 s → 53.8 s (-5.62% [-6.49, -4.68]) | 57.1 s → 56.0 s (-2.01% [-4.09, +0.45]) |
| 753.ns3_r | 32.1 s → 31.7 s (-1.27% [-4.75, +2.15]) | 32.0 s → 32.1 s (+0.28% [-0.82, +1.17]) |
| 777.zstd_r | 97.5 s → 97.9 s (+0.42% [-0.06, +0.94]) | 103.2 s → 103.9 s (+0.62% [+0.27, +1.00]) |
| 782.lbm_r | 129.8 s → 127.9 s (-1.49% [-3.23, +0.32]) | 150.1 s → 150.2 s (+0.06% [-0.84, +0.94]) |

Estimated results (train size, not SPEC-reviewed). Positive Δ = slower when co-located with the GPU workload.

### What the node looks like

| Scenario | Node CPU busy (48 vCPU) | Memory used | GPU utilisation | Max CPU steal |
|---|---|---|---|---|
| GPU workload only | 5.2% | 10.9 GiB | 98% | 0.00% |
| Co-located 50% (20 vCPU) | 45.5% | 14.6 GiB | 98% | 0.00% |
| Co-located 95% (38 vCPU) | 79.8% | 18.3 GiB | 98% | 0.00% |

Measured at 1 s over the first 10 minutes of load (compute-type phase).



### Maximum throughput while co-located

Saturating load (500 requests in flight) for the whole SPEC run; completed requests/s, mean of 2 runs, change vs solo with 95% CI.

| SPEC running alongside | Solo | Colo 50% (20 vCPU) | Colo 95% (38 vCPU) |
|---|---|---|---|
| Compute-type (14 integer benchmarks) | 805 req/s | 805 (+0.0% [-0.1, +0.1]) | 805 (+0.0% [-0.1, +0.2]) |
| Memory-bandwidth-heavy (fotonik3d, lbm) | 806 req/s | 776 (-3.8% [-4.4, -3.2]) | 770 (-4.5% [-6.2, -2.9]) |

Failed requests: 0.

### GPU workload with all 48 vCPU (no co-location)

vLLM with no CPU or memory limit, so it can use every core on the node, compared with the 8 vCPU / 64 GiB slice.

| | 8 vCPU / 64 GiB | All 48 vCPU |
|---|---|---|
| Capacity at 250 requests in flight (1 short probe each) | 757 req/s | 766 req/s |
| Capacity at 500 requests in flight (1 short probe each) | 821 req/s | 860 req/s |
| Capacity at 750 requests in flight (1 short probe each) | 840 req/s | 833 req/s |
| TTFT p50 at 625 req/s | 35.7 ms (+2.7% [+1.6, +3.8]) | 34.8 ms |
| TTFT p99 at 625 req/s | 55.4 ms (+3.0% [+0.1, +5.9]) | 53.8 ms |
| Inter-token latency p50 at 625 req/s | 11.8 ms (+0.2% [-0.6, +0.9]) | 11.8 ms |
| End-to-end p50 at 625 req/s | 154.5 ms (+0.7% [-0.1, +1.5]) | 153.4 ms |
| End-to-end p99 at 625 req/s | 220.2 ms (+1.1% [-1.3, +3.5]) | 217.8 ms |
| Output tokens/s at 625 req/s | 6,914 | 6,908 |

Fixed-rate rows: 5 runs at 8 vCPU (the solo runs of the main matrix) vs 2 runs with all 48 vCPU (run on a later day with a new pod, not interleaved); percentage = 8 vCPU vs all 48 vCPU; the n = 2 intervals are rough.



![Capacity by benchmark](https://raw.githubusercontent.com/jawadaminGOOG/v6e-v-a100-recipe/g4-colo/g4_colo/results/plots/capacity_by_benchmark.png)

![8 vCPU vs 48 vCPU](https://raw.githubusercontent.com/jawadaminGOOG/v6e-v-a100-recipe/g4-colo/g4_colo/results/plots/golden_capacity.png)

### Latency distributions

![CDF compute](https://raw.githubusercontent.com/jawadaminGOOG/v6e-v-a100-recipe/g4-colo/g4_colo/results/plots/latency_cdf_compute.png)

![CDF bandwidth](https://raw.githubusercontent.com/jawadaminGOOG/v6e-v-a100-recipe/g4-colo/g4_colo/results/plots/latency_cdf_bandwidth.png)

*Cumulative distributions of all requests pooled across the 5 runs. Top row: every request. Bottom row: the slowest
10%. With compute-type co-located work, the distributions shift right by a few milliseconds. With
memory-bandwidth-heavy work, most requests shift moderately and the slowest fifth stretch out, which drives the p99
change.*

![SPEC impact](https://raw.githubusercontent.com/jawadaminGOOG/v6e-v-a100-recipe/g4-colo/g4_colo/results/plots/spec_impact_forest.png)

![Per-CPU busy, colo 95%](https://raw.githubusercontent.com/jawadaminGOOG/v6e-v-a100-recipe/g4-colo/g4_colo/results/plots/cpu_heatmap_colo-L38-r1.png)

*Per-CPU utilisation in a 95% run. Rows 1-4 and 25-28 are vLLM; the solid yellow blocks are SPEC benchmarks, with the
last two being `fotonik3d` and `lbm`. Gaps between blocks are SPEC setting up the next benchmark.*

![Per-CPU busy, solo](https://raw.githubusercontent.com/jawadaminGOOG/v6e-v-a100-recipe/g4-colo/g4_colo/results/plots/cpu_heatmap_vllm_solo-r1.png)

*The same node with only the GPU workload running.*

### What drives the differences

1. **Cores are exclusive; cache and memory bandwidth are shared.** vLLM's 4 cores are its own, but its L3 domain
   (cores 0-7) also holds SPEC cores 5-7, and memory bandwidth is shared by every core.
2. **Compute-type work stays mostly inside each core's caches**, so the GPU workload sees a few milliseconds more TTFT
   (35.6 → 37.7 ms at 38 vCPU) and almost no change in token generation.
3. **Memory-bandwidth-heavy work competes for the shared memory path.** vLLM's CPU-side request handling slows,
   which shows most in the tail: TTFT p99 goes from 55 to 97 ms at 20 vCPU and to 115 ms at 38 vCPU.
   Throughput is unchanged and no request fails.
4. **Within each class, tail latency grows with more co-located vCPU**, and the gap between classes is far larger
   than the gap between 20 and 38 vCPU.

## 4. Pricing input

The 2 vCPU that Kubernetes keeps for itself exist on any node, so pricing counts all 40 spare vCPU as reclaimable.

| | Value | Source |
|---|---|---|
| `g4-standard-48`, 3-year CUD | $1.98/hr | GCP pricing (us-central1; us-west8 is the same) |
| Spare 40 vCPU / 116 GiB priced as 9 × `n4d-standard-2` + 11 × `n4d-highcpu-2`, 3-year CUD | $0.697/hr | GCP pricing (us-central1; us-west8 is the same) |
| Co-location ratio used | 1.0 (40 / 40) | measured with compute-type co-located work at 38 / 40 vCPU |
| Effective GPU-node cost after crediting the reclaimed CPU: 1.98 − 1.0 × 0.697 | **$1.28/hr** | |

## 5. Why vLLM runs 4 API server processes

The first pilots used vLLM's default single API server process. That one process handles HTTP, tokenisation and
streaming for every request, and ran about 60% busy at the target load. With SPEC on 36-38 vCPU it reached 100%, the
request queue grew without bound, and latency rose to minutes with tens of thousands of failed requests. A solo
control of the same length stayed flat.

`--api-server-count=4` spreads that work over 4 processes inside the same 8 vCPU and raised capacity from 726 to 832
RPS. With it, the busiest vLLM CPU stays around 40-55% at 95% co-location. The single-process runs are kept in
`g4_colo/results/archive_single_frontend/` and are not used in any result above.

## 6. Scope

1. One model, one load point (75% of capacity), one machine shape, steady state. Bursts and autoscaling were not
   tested.
2. The all-48-vCPU vLLM pod has no CPU limit and shares CPUs with the kubelet and the telemetry sampler; its runs
   were made on a later day with a new pod, not interleaved with the 8-vCPU runs.
3. SPEC at train size uses little memory (about 18 GiB at 95%), so this measures co-located CPU work. Memory
   capacity is hard-limited per pod by cgroups; memory bandwidth, the shared part, is exercised by `fotonik3d` and `lbm`.
4. SPEC CPU 2026 results are estimates (train size, not SPEC-reviewed).
5. With `--api-server-count > 1`, vLLM disables its server-side stats logger, so latency is measured at the client.
6. Co-location shares microarchitecture (L3, branch predictors). Co-locate code you trust on both sides.

## 7. Reproduce

```bash
cd g4_colo
# infra: GKE node pool with infra/kubelet-config.yaml, namespace colo, k8s/telemetry.yaml, k8s/client.yaml,
#        k8s/spec-build.yaml (builds SPEC from the ISO), k8s/vllm-fp16-asc4.yaml
python3 orchestrate.py matrix --reps 5          # 25 interleaved runs, ~9 h
COLO_VARIANT=sanity python3 orchestrate.py matrix --cap --reps 2   # saturating-load runs, ~2.5 h
python3 analyze.py && python3 phases.py && python3 quantiles.py && python3 sanity.py && python3 plots.py && python3 report.py
python3 diagrams/g4_layers.py
```

| Path | Contents |
|---|---|
| `experiment.json` | every run parameter: rate, levels, windows, capacity probe |
| `results/cells/<run>/` | per run: client summaries, per-second request aggregates, telemetry, cpusets, SPEC result files and logs |
| `results/analysis/` | `vllm_phase_runs.csv` (one row per run and class), `vllm_phase_impact.csv`, `phase_summary.csv`, `latency_quantiles.csv`, `spec_impact.csv`, `cells.csv` |
| `results/sanity/` | saturating-load and all-48-vCPU results (`capacity_summary.csv`, `golden_capacity.csv`, `golden_latency.csv`) |
| `results/plots/` | all figures |

Per-request client logs (about 600 MB) are not in git; `phases.py` and `quantiles.py` need them and they are
available on request. All tables and figures can be regenerated from the committed `results/analysis/` files.

## Appendix A: cost of the 8-vCPU limit (single-process config)

Same image and node, vLLM with one API server process, 250 concurrent requests: 8 vCPU / 64 GiB reached 726 RPS (two
runs: 728.1, 723.8) vs 738 RPS with 40 vCPU / 150 GiB (739.3, 737.5), i.e. −1.7%. Not repeated for the 4-process
configuration.

## 8. Fleet Recipe and Live Demo (`g4_colo/recipe/` & `g4_colo/demo/`)

To run this co-location pattern across a multi-node GKE Standard cluster—with custom `ComputeClass` definitions (`g4-gpu` and `n4d-cpu`), expendable `PriorityClass` backfill (`value: -100`) so CPU demand never scales out GPU nodes, warm N4D overflow buffers (`value: -5`), admission/quota/network guardrails, and an automated TTFT p99 latency guard that evicts memory-bandwidth-heavy backfill to N4D—see:

- **[`g4_colo/recipe/README.md`](g4_colo/recipe/README.md):** Kustomize base and overlays (`overlays/demo`, `overlays/production-template`), controller, node-agent, and gate verification suite.
- **[`g4_colo/demo/README.md`](g4_colo/demo/README.md):** Local FastAPI + Chart.js dashboard showing per-core G4 CPU ownership, live TTFT p99 vs. baseline, backfill vs. overflow placement, and one-click scenario runners.
