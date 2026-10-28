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

