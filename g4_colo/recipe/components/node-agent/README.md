# colo-node-agent

Per-node DaemonSet (on `colo.role in (g4,n4d)`) that exposes which CPUs the kubelet
static CPU manager has pinned, and how busy each CPU is. Stdlib only (`http.server`).

## API (:8080)
- `GET /cpus`
  ```json
  {"node": "gke-...", "ncpu": 48,
   "cpu_manager_state": {"policyName": "static", "defaultCpuSet": "0-1,10-47",
                         "entries": {"<podUID>": {"<container>": "2-9"}}, "checksum": 123},
   "busy": [3.5, 98.0, ...], "sample_window_s": 2.0,
   "mem_used_gib": 71.3, "mem_total_gib": 188.7, "time": 1793287260.1, "error": null}
  ```
  `busy[i]` = % of CPU i not idle/iowait over the last ~2 s (sampled every 0.5 s in a
  background thread). `cpu_manager_state` is the raw kubelet file; on read error it is
  `{"error": "..."}`.
- `GET /healthz` -> `{"ok": true}`

## Environment
| Var | Default |
|---|---|
| `NODE_NAME` | hostname (set from `spec.nodeName` via downward API) |
| `HOST_PROC` | `/host/proc` (hostPath `/proc`, read-only) |
| `KUBELET_DIR` | `/host/var/lib/kubelet` (hostPath, read-only) |
| `PORT` | `8080` |
| `SAMPLE_INTERVAL_S` / `WINDOW_S` | `0.5` / `2` |

No hostPID: `/proc/stat` and `/proc/meminfo` are host-global. Requests 50m CPU.

Tests: `pytest tests/`.
