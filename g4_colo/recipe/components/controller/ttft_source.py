"""TTFT sources for the guard: network I/O only (parsing lives in colo_logic).

Two sources, chosen by `ttft_source.type` in colo-config:

* `loadgen`: GET http://<loadgen-pod-ip>:<port>/stats?window_s=N on every Ready
  loadgen pod (demo only; the payload already names each vLLM pod's node).
* `prometheus`: one instant query against Google Managed Prometheus
      sum by (pod, le)(rate(<metric>_bucket{namespace="<ns>"}[<w>s]))
  authenticated with Application Default Credentials (Workload Identity on GKE).
  colo_logic.summarize_prom_ttft maps pods to nodes and applies the
  histogram_quantile(0.99, ...) algorithm per node and cluster-wide.

`none` fetches nothing; the guard then reports no_signal and never acts.
The controller polls a source in a background task and the reconcile tick only
reads the last snapshot, so a slow or hung source never delays a tick.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import aiohttp

import colo_logic as L

log = logging.getLogger("colo-controller.ttft")

GMP_ENDPOINT = "https://monitoring.googleapis.com"
MONITORING_READ_SCOPE = "https://www.googleapis.com/auth/monitoring.read"

TokenFn = Callable[[], Awaitable[str]]


@dataclass
class TTFTSnapshot:
    kind: str = "none"  # loadgen | prometheus | none
    fetched_at: float = 0.0  # unix time of the last fetch attempt that finished
    ok: bool = False  # last fetch succeeded
    error: str | None = None
    window_s: float | None = None
    loadgen_stats: list[dict[str, Any]] = field(default_factory=list)
    prom_samples: list[tuple[dict[str, str], float]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Prometheus (Google Managed Prometheus)
# ---------------------------------------------------------------------------


def prom_query_url(project: str, location: str = "global", endpoint: str = GMP_ENDPOINT) -> str:
    return f"{endpoint}/v1/projects/{project}/location/{location}/prometheus/api/v1/query"


def prom_ttft_query(metric: str, namespace: str, window_s: float) -> str:
    return (f'sum by (pod, le)(rate({metric}_bucket{{namespace="{namespace}"}}'
            f'[{int(window_s)}s]))')


class AdcToken:
    """OAuth token from Application Default Credentials (GKE Workload Identity).

    google-auth is imported lazily so unit tests and the loadgen-only path do
    not need it installed.
    """

    def __init__(self) -> None:
        self._creds: Any = None

    def _token(self) -> str:
        import google.auth  # noqa: PLC0415
        import google.auth.transport.requests  # noqa: PLC0415

        if self._creds is None:
            self._creds, _ = google.auth.default(scopes=[MONITORING_READ_SCOPE])
        if not self._creds.valid:
            self._creds.refresh(google.auth.transport.requests.Request())
        return self._creds.token

    async def __call__(self) -> str:
        return await asyncio.to_thread(self._token)


async def fetch_prometheus(
    session: aiohttp.ClientSession, url: str, query: str, token_fn: TokenFn
) -> list[tuple[dict[str, str], float]]:
    """Run one instant query; raise on any HTTP, auth or payload error."""
    token = await token_fn()
    async with session.post(url, data={"query": query},
                            headers={"Authorization": f"Bearer {token}"}) as r:
        text = await r.text()
        if r.status != 200:
            raise ValueError(f"prometheus HTTP {r.status}: {text[:300]}")
        try:
            payload = json.loads(text)
        except ValueError as e:
            raise ValueError(f"prometheus returned non-JSON: {text[:120]!r}") from e
    return L.parse_prom_vector(payload)


# ---------------------------------------------------------------------------
# loadgen
# ---------------------------------------------------------------------------


async def _fetch_stats(session: aiohttp.ClientSession, ip: str, port: int,
                       window_s: float) -> dict[str, Any] | None:
    url = f"http://{ip}:{port}/stats"
    try:
        async with session.get(url, params={"window_s": str(int(window_s))}) as r:
            if r.status != 200:
                log.warning("loadgen %s: HTTP %s", ip, r.status)
                return None
            return await r.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
        log.warning("loadgen %s: %s", ip, e)
        return None


async def fetch_loadgen(session: aiohttp.ClientSession, ips: list[str], port: int,
                        window_s: float) -> list[dict[str, Any]]:
    res = await asyncio.gather(*(_fetch_stats(session, ip, port, window_s) for ip in ips))
    return [r for r in res if isinstance(r, dict)]


# ---------------------------------------------------------------------------
# One poll, dispatching on config
# ---------------------------------------------------------------------------


async def poll_once(
    cfg: dict[str, Any],
    *,
    loadgen_session: aiohttp.ClientSession | None,
    prom_session: aiohttp.ClientSession | None,
    loadgen_ips: list[str],
    loadgen_port: int,
    namespace: str,
    token_fn: TokenFn | None,
    prom_endpoint: str = GMP_ENDPOINT,
) -> TTFTSnapshot:
    """Fetch from the configured source. Never raises: errors land in the snapshot."""
    src = cfg.get("ttft_source") or {}
    kind = src.get("type", "loadgen")
    snap = TTFTSnapshot(kind=kind)
    try:
        if kind == "loadgen":
            assert loadgen_session is not None
            w = float(cfg["guard"]["window_s"])
            snap.loadgen_stats = await fetch_loadgen(loadgen_session, loadgen_ips, loadgen_port, w)
            snap.window_s = w
            snap.ok = True
        elif kind == "prometheus":
            assert prom_session is not None and token_fn is not None
            p = src["prometheus"]
            w = float(p["query_window_s"])
            url = prom_query_url(p["project"], p.get("location") or "global", prom_endpoint)
            snap.prom_samples = await fetch_prometheus(
                prom_session, url, prom_ttft_query(p["metric"], namespace, w), token_fn)
            snap.window_s = w
            snap.ok = True
        else:
            snap.error = "ttft_source.type is none"
    except Exception as e:  # noqa: BLE001 - fail safe: guard sees no_signal
        snap.ok = False
        snap.error = f"{type(e).__name__}: {e}"[:500]
        log.warning("ttft source %s failed: %s", kind, snap.error)
    snap.fetched_at = time.time()
    return snap


def snapshot_view(
    snap: TTFTSnapshot | None,
    cfg: dict[str, Any],
    pod_node: dict[str, str],
    min_requests: int,
    now: float,
    stale_s: float,
) -> tuple[L.TTFTView, bool, str | None]:
    """(view, source_ok, why_not) for this tick from the last snapshot.

    source_ok is False when the type is none, the snapshot is from another
    source type, the last fetch failed, or it is older than stale_s.
    """
    kind = (cfg.get("ttft_source") or {}).get("type", "loadgen")
    empty = L.summarize_ttft([], min_requests)
    if kind == "none":
        return empty, False, "ttft_source.type is none"
    if snap is None or snap.kind != kind or snap.fetched_at == 0:
        return empty, False, "no data from source yet"
    if not snap.ok:
        return empty, False, snap.error or "source error"
    if now - snap.fetched_at > stale_s:
        return empty, False, f"source data {now - snap.fetched_at:.0f}s old"
    if kind == "loadgen":
        return L.summarize_ttft(snap.loadgen_stats, min_requests), True, None
    return (L.summarize_prom_ttft(snap.prom_samples, pod_node, snap.window_s or 60, min_requests),
            True, None)
