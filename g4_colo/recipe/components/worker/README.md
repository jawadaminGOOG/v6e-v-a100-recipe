# colo-worker

Synthetic CPU services for the colo-at-scale recipe. One image, three modes.

| `MODE` | Class | What a work unit is |
|---|---|---|
| `sandbox` | integer | one generated Python tool snippet (text parse / sort dicts / regex / json / sqlite) run in a fresh `python -c` subprocess with a timeout |
| `toolcall` | integer | one 20-50 KB JSON request: parse, regex, sha256, zlib round-trip, JSON response |
| `analytics` | memory-heavy | one full 3D 7-point stencil sweep over the per-process float64 grids (8 in-place full-array passes, no temporaries: DRAM-bandwidth bound) |

## Environment

| Var | Default | Meaning |
|---|---|---|
| `MODE` | `toolcall` | `sandbox`, `toolcall` or `analytics` |
| `NPROC` | CPUs in `/sys/fs/cgroup/cpuset.cpus.effective` (capped by `cpu.max` quota if set), fallback `os.cpu_count()` | worker processes |
| `ARRAY_MB` | `1500` | analytics: total working set per process (two n^3 float64 grids) |
| `SANDBOX_TIMEOUT_S` | `10` | sandbox: per-snippet subprocess timeout |
| `METRICS_PORT` | `9100` | Prometheus port |
| `POD_NAME`, `NODE_NAME` | - | optional, exported as labels on `colo_worker_info` |

Memory sizing for analytics: `NPROC x ARRAY_MB` plus ~100 MB; the 2 CPU / 6Gi pod uses 2 x 1500 MB.

## Metrics (`:9100/metrics`)
- `colo_worker_units_total{mode}` counter
- `colo_worker_info{mode,nproc,array_mb,pod,node}` = 1
- `colo_worker_processes`

SIGTERM: workers stop after the current unit; stragglers are killed at 4 s.

## Local run
```sh
docker build -t colo-worker .
docker run --rm -e MODE=analytics -e NPROC=1 -e ARRAY_MB=200 -p 9100:9100 colo-worker
curl -s localhost:9100/metrics | grep colo_worker
```
Tests: `pytest tests/`.
