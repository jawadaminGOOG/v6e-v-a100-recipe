### GPU workload: co-located vs solo

| Metric | Solo | Colo 50% (20 vCPU) | Colo 95% (38 vCPU, max allocatable; core 0 reserved by GKE) |
|---|---|---|---|
| TTFT p50 | 35.7 ms | 37.2 ms (+4.1% [+3.7, +4.5]) | 37.6 ms (+5.3% [+5.0, +5.5]) |
| TTFT p99 | 55.1 ms | 57.2 ms (+3.9% [+2.5, +5.1]) | 58.7 ms (+6.7% [+5.2, +7.6]) |
| ITL p50 | 11.8 ms | 11.8 ms (+0.4% [+0.0, +0.9]) | 12.0 ms (+1.5% [+1.2, +1.8]) |
| E2E latency p50 | 154.5 ms | 156.4 ms (+1.3% [+0.8, +1.7]) | 158.1 ms (+2.3% [+2.0, +2.6]) |
| E2E latency p99 | 219.3 ms | 223.0 ms (+1.7% [-0.4, +3.5]) | 228.2 ms (+4.1% [+2.1, +5.5]) |
| Output tokens/s | 6,916 | 6,906 (-0.1% [-0.3, +0.0]) | 6,916 (-0.0% [-0.1, +0.1]) |

Mean of 5 reps; brackets = 95% bootstrap CI of the change vs solo. Failed requests: solo 0, colo 0.

### Verdict per colo level

| Colo level | Spare vCPU used | Pass (≤ +10% latency, ≥ −2% throughput, 0 failures) | Strict (CI includes 0) |
|---|---|---|---|
| 50% (20 vCPU) | 20 of 40 | ✅ PASS | no |
| 95% (38 vCPU, max allocatable; core 0 reserved by GKE) | 38 of 40 | ✅ PASS | no |

**Headline colo ratio: 38/40 = 95% of the spare vCPU.**

### CPU workload: SPEC CPU 2026 rate runtime, co-located vs solo

| Benchmark | 50% (20 vCPU): solo s → colo s (Δ, 95% CI) | 95% (38 vCPU, max allocatable; core 0 reserved by GKE): solo s → colo s (Δ, 95% CI) |
|---|---|---|
| 706.stockfish_r | 50.5 → 49.7 (-1.62% [-5.40, +0.87]) | 52.7 → 51.3 (-2.54% [-5.12, -0.76]) |
| 707.ntest_r | 46.6 → 48.5 (+4.07% [+3.73, +4.36]) | 47.9 → 49.1 (+2.49% [+2.14, +2.80]) |
| 708.sqlite_r | 28.0 → 28.1 (+0.55% [+0.16, +0.91]) | 28.8 → 29.0 (+0.80% [+0.60, +0.93]) |
| 710.omnetpp_r | 25.0 → 25.2 (+0.69% [+0.12, +1.33]) | 25.9 → 25.9 (-0.06% [-0.45, +0.34]) |
| 714.cpython_r | 25.8 → 25.8 (-0.03% [-1.48, +1.45]) | 26.3 → 26.1 (-0.81% [-1.45, -0.08]) |
| 721.gcc_r | 39.0 → 39.9 (+2.19% [+1.78, +2.56]) | 40.3 → 41.1 (+1.89% [+1.62, +2.10]) |
| 723.llvm_r | 40.1 → 40.4 (+0.76% [-0.27, +1.54]) | 41.2 → 41.5 (+0.76% [+0.45, +1.12]) |
| 727.cppcheck_r | 64.7 → 65.2 (+0.77% [-0.39, +2.42]) | 67.3 → 67.6 (+0.46% [-0.28, +1.15]) |
| 729.abc_r | 51.5 → 51.9 (+0.81% [+0.10, +1.51]) | 53.7 → 54.0 (+0.61% [+0.38, +0.83]) |
| 734.vpr_r | 50.4 → 50.8 (+0.68% [-0.33, +1.89]) | 54.2 → 54.6 (+0.76% [-0.24, +1.73]) |
| 735.gem5_r | 36.5 → 36.7 (+0.52% [+0.23, +0.81]) | 37.4 → 37.5 (+0.14% [-0.57, +0.83]) |
| 749.fotonik3d_r | 112.1 → 109.8 (-2.09% [-3.31, -0.77]) | 129.1 → 133.3 (+3.25% [+1.96, +4.17]) |
| 750.sealcrypto_r | 57.0 → 53.8 (-5.71% [-7.10, -3.97]) | 57.4 → 55.5 (-3.36% [-4.66, -1.71]) |
| 753.ns3_r | 32.8 → 31.7 (-3.39% [-6.58, +1.71]) | 32.1 → 32.1 (+0.01% [-1.76, +1.21]) |
| 777.zstd_r | 97.4 → 97.8 (+0.38% [-0.17, +1.02]) | 103.3 → 103.9 (+0.61% [+0.35, +0.89]) |
| 782.lbm_r | 129.7 → 128.0 (-1.35% [-4.18, +1.48]) | 150.3 → 150.5 (+0.11% [-0.93, +1.12]) |

Estimated SPEC CPU 2026 results (train size, not SPEC-reviewed). Positive Δ = slower when co-located.

### What the node looks like

| Scenario | Node CPU busy (48 vCPU) | Memory used | GPU util | Max CPU steal |
|---|---|---|---|---|
| solo | 5.2% | 10.9 GiB | 98% | 0.00% |
| 50% (20 vCPU) | 45.5% | 14.6 GiB | 98% | 0.00% |
| 95% (38 vCPU, max allocatable; core 0 reserved by GKE) | 79.9% | 18.3 GiB | 98% | 0.00% |
