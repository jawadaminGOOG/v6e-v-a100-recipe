# colo-loadgen

Open-loop Poisson load generator for the vLLM pods. Runs as `inference/loadgen`
(4 replicas on `colo.role=loadgen`). asyncio + aiohttp (+uvloop), one client session,
connector `limit=0`. Measured locally: 250 rps against fake streaming servers used
~0.3 of one core, so a 2 vCPU replica has ~6x headroom.

## Behaviour
- Every 2 s, lists Ready pods `VLLM_LABEL` in `VLLM_NAMESPACE` (pod IP + `nodeName`)
  and counts Ready loadgen replicas `REPLICAS_LABEL` in its own namespace.
- Arrivals: Poisson at `rate_total / replicas`; requests round-robin across Ready pods.
- Request: `POST http://<podIP>:VLLM_PORT/v1/completions`, `max_tokens=16`,
  `temperature=0`, `stream=true`, `stream_options.include_usage=true`.
  TTFT = first SSE data chunk with a choice; success = HTTP 200 and `[DONE]` seen.
- Prompt: fixed generic toxicity-classification instruction + synthetic message from word
  lists + unique id/timestamp. 545 tokens with the Qwen3 tokenizer (measured offline;
  no tokenizer at runtime). No customer data.
- Per pod: rolling 30 s TTFT and E2E histograms, 2 ms buckets to 2000 ms + overflow
  (1001 counts), in-flight, cumulative ok/err, windowed ok/err.

## API (:8080)
| Route | Notes |
|---|---|
| `GET /stats` | `{"replica", "rate" (=total), "rate_total", "rate_local", "replicas", "targets", "inflight", "per_pod": {pod: {"node", "ready", "ttft_hist", "e2e_hist", "inflight", "ok", "err", "ok_window", "err_window", "ttft_p50_ms", "ttft_p99_ms", "e2e_p99_ms"}}, "window_s": 30, "bucket_ms": 2, "max_ms": 2000, "time"}` |
| `POST /rate` | body `{"rate": total_rps}` (total across all replicas) |
| `GET /rate[?rate=x]` | read, or set when `rate` is given |
| `GET /metrics` | `loadgen_ttft_seconds{pod,node}`, `loadgen_e2e_seconds{pod,node}`, `loadgen_inflight{pod}`, `loadgen_requests_total{code}`, `loadgen_rate_rps{scope}`, `loadgen_targets`, `loadgen_replicas` |
| `GET /healthz` | liveness |

Histogram layout and percentile helpers live in `hist.py` (bucket `i` = `[2i, 2i+2)` ms,
index 1000 = overflow; percentiles report the bucket upper edge, overflow -> 2000).
To merge across replicas, sum `ttft_hist` element-wise per pod (`hist.merge_hists`).

`loadgen_requests_total` codes: `200`, HTTP status, `timeout`, `conn_error`,
`incomplete`, `exception`, `dropped` (local in-flight cap reached).

## Environment
| Var | Default |
|---|---|
| `VLLM_NAMESPACE` / `VLLM_LABEL` / `VLLM_PORT` | `inference` / `app=vllm` / `8000` |
| `MODEL` | `Qwen/Qwen3-1.7B-Base` |
| `INITIAL_RATE` | `0` (total rps) |
| `REPLICAS_LABEL` | `app=loadgen` |
| `REPLICAS` | unset; a non-zero value overrides the API replica count |
| `POD_NAME` / `POD_NAMESPACE` | downward API; namespace falls back to the SA namespace file |
| `MAX_TOKENS` / `MAX_INFLIGHT` / `REQUEST_TIMEOUT_S` | `16` / `4000` / `60` |
| `DISCOVERY_INTERVAL_S` | `2` |
| `VLLM_TARGETS` | dev only: comma list of base URLs, skips the k8s API |

RBAC: `get,list` on `pods` in `inference` (its own namespace and the vLLM namespace).

## Local test
```sh
python tests/fake_vllm.py 18001 20 &
VLLM_TARGETS=http://127.0.0.1:18001 INITIAL_RATE=50 PORT=18080 python loadgen.py &
curl -s localhost:18080/stats | jq '.per_pod[] | {ok_window, ttft_p99_ms}'
pytest tests/
```
