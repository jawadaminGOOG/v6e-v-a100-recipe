# g4-standard-48 co-location: GPU workload on an 8-vCPU slice, CPU work on the rest

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
- CPU steal is 0.00% in every run.

This branch extends the G4 benchmark in [g4-readme.md](g4-readme.md) using the same workload, and follows the method
of [*The Idle Half of a GPU Pod*](https://medium.com/google-cloud/the-idle-half-of-a-gpu-pod-8ec3ffaddc4c) on a
`g4-standard-48`.

---

## 1. Setup

![g4-standard-48 layout](g4_colo/diagrams/g4_layers.png)

| Item | Value |
|---|---|
| Machine | `g4-standard-48` on GKE (us-west8-a): AMD EPYC 9B45, 24 cores / 48 threads, 3 L3 domains, 180 GB, 1 × RTX PRO 6000 (96 GB) |
| Isolation | kubelet static CPU manager; every pod is Guaranteed QoS with integer CPUs, so each gets exclusive whole cores |
| GPU workload | vLLM v0.32.0 (`vllm/vllm-openai@sha256:befccf0e…`), `Qwen/Qwen3-1.7B-Base`, FP16, prefix caching, `--api-server-count=4`; pod limited to **8 vCPU / 64 GiB / 1 GPU** on CPUs `1-4,25-28` |
| CPU workload | SPEC CPU 2026 v1.0.1 rate, train size, the 16 benchmarks used in the article (GCC 14, `-O3 -march=x86-64-v3`) |
| Co-location levels | **50%**: 20 copies on 20 vCPU (CPUs `5-14,29-38`) · **95%**: 38 copies on 38 vCPU (CPUs `5-23,29-47`) |
| System | core 0 (CPUs `0,24`) stays in Kubernetes' shared pool for kubelet and daemons; 38 of 40 spare vCPU is the most a workload can claim |
| Load | same prompts as [g4-readme.md](g4-readme.md) (~600-token prompts, ~11 output tokens), streamed, sent at a fixed **625 requests/s** = 75% of the measured 832 RPS capacity |

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

![Change vs solo](g4_colo/results/plots/vllm_delta_summary.png)

RESULTS_SECTION

### Latency distributions

![CDF compute](g4_colo/results/plots/latency_cdf_compute.png)

![CDF bandwidth](g4_colo/results/plots/latency_cdf_bandwidth.png)

*Cumulative distributions of all requests pooled across the 5 runs. Top row: every request. Bottom row: the slowest
10%. With compute-type co-located work, the distributions shift right by a few milliseconds. With
memory-bandwidth-heavy work, most requests shift moderately and the slowest fifth stretch out, which drives the p99
change.*

![SPEC impact](g4_colo/results/plots/spec_impact_forest.png)

![Per-CPU busy, colo 95%](g4_colo/results/plots/cpu_heatmap_colo-L38-r1.png)

*Per-CPU utilisation in a 95% run. Rows 1-4 and 25-28 are vLLM; the solid yellow blocks are SPEC benchmarks, with the
last two being `fotonik3d` and `lbm`. Gaps between blocks are SPEC setting up the next benchmark.*

![Per-CPU busy, solo](g4_colo/results/plots/cpu_heatmap_vllm_solo-r1.png)

*The same node with only the GPU workload running.*

### What drives the differences

1. **Cores are exclusive; cache and memory bandwidth are shared.** vLLM's 4 cores are its own, but its L3 domain
   (cores 0-7) also holds SPEC cores 5-7, and memory bandwidth is shared by every core.
2. **Compute-type work stays mostly inside each core's caches**, so the GPU workload sees a few milliseconds more TTFT
   (35.6 → 37.7 ms at 38 vCPU) and almost no change in token generation.
3. **Memory-bandwidth-heavy work saturates the shared memory path.** vLLM's CPU-side request handling waits longer
   for memory, which shows most in the tail: TTFT p99 goes from 55 to 97 ms at 20 vCPU and to 115 ms at 38 vCPU.
   Throughput is unchanged and no request fails.
4. **Within each class, more co-located vCPU means a larger effect**, and the gap between classes is far larger than
   the gap between 20 and 38 vCPU.

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
2. SPEC at train size uses little memory (about 18 GiB at 95%), so this measures co-located CPU work. Memory
   capacity is hard-limited per pod by cgroups; memory bandwidth, the shared part, is exercised by `fotonik3d` and `lbm`.
3. SPEC CPU 2026 results are estimates (train size, not SPEC-reviewed).
4. With `--api-server-count > 1`, vLLM disables its server-side stats logger, so latency is measured at the client.
5. Co-location shares microarchitecture (L3, branch predictors). Co-locate code you trust on both sides.

## 7. Reproduce

```bash
cd g4_colo
# infra: GKE node pool with infra/kubelet-config.yaml, namespace colo, k8s/telemetry.yaml, k8s/client.yaml,
#        k8s/spec-build.yaml (builds SPEC from the ISO), k8s/vllm-fp16-asc4.yaml
python3 orchestrate.py matrix --reps 5          # 25 interleaved runs, ~9 h
python3 analyze.py && python3 phases.py && python3 quantiles.py && python3 plots.py && python3 report.py
python3 diagrams/g4_layers.py
```

| Path | Contents |
|---|---|
| `experiment.json` | every run parameter: rate, levels, windows, capacity probe |
| `results/cells/<run>/` | per run: client summaries, per-second request aggregates, telemetry, cpusets, SPEC result files and logs |
| `results/analysis/` | `vllm_phase_runs.csv` (one row per run and class), `vllm_phase_impact.csv`, `phase_summary.csv`, `latency_quantiles.csv`, `spec_impact.csv`, `cells.csv` |
| `results/plots/` | all figures |
| `PEER_REVIEW.md` | independent review of this analysis |

Per-request client logs (about 600 MB) are not in git; `phases.py` and `quantiles.py` need them and they are
available on request. All tables and figures can be regenerated from the committed `results/analysis/` files.

## Appendix A: cost of the 8-vCPU limit (single-process config)

Same image and node, vLLM with one API server process, 250 concurrent requests: 8 vCPU / 64 GiB reached 726 RPS (two
runs: 728.1, 723.8) vs 738 RPS with 40 vCPU / 150 GiB (739.3, 737.5), i.e. −1.7%. Not repeated for the 4-process
configuration.
