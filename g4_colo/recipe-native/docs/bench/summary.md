| metric | controller | native |
|---|---|---|
| ttft_ratio_full | 1.3 | 1.3 |
| step_covered_after_s | 70.3 | 88.3 |
| step_drained_after_s | 12.3 | 38.5 |
| step_n4d_node_minutes | 30.4 | 25.3 |
| vllm2_ready_after_s | 246.5 | 268.6 |
| g4_fill_34_after_vllm2_ready_s | 0.0 | 0.0 |
| scalein_ready_ge_demand_pct | 100.0 | 100.0 |
| node_removed_after_s | 570.5 | 596.8 |
| sweep_n4d_node_minutes | 129.6 | 129.5 |
| sweep_reclaimed_vcpu_minutes | 2945.5 | 2738.2 |

TTFT p99 by backfill pods (ms):

| pods | controller | native |
|---|---|---|
| 0 | 20 | 20 |
| 4 | 20 | 22 |
| 8 | 22 | 22 |
| 12 | 24 | 22 |
| 16 | 24 | 24 |
| 18 | 26 | 26 |
