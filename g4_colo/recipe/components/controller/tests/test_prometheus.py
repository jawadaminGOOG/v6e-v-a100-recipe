"""Prometheus TTFT source: parsing, quantile, pod->node mapping, fail-safe.

The HTTP tests run a fake Prometheus API with aiohttp's test server; they are
skipped when aiohttp is not installed (colo_logic tests need no dependencies).
"""

import asyncio
import math
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import colo_logic as L  # noqa: E402

LES = ["0.01", "0.02", "0.04", "0.06", "0.08", "0.1", "0.25", "+Inf"]


def series(pod, cum_rates, extra=None):
    """Per-second cumulative bucket rates for one pod -> vector samples."""
    return [({"pod": pod, "le": le, **(extra or {})}, v) for le, v in zip(LES, cum_rates)]


def vector(samples):
    return {"status": "success", "data": {"resultType": "vector", "result": [
        {"metric": m, "value": [1730254800.0, str(v)]} for m, v in samples]}}


# 10 req/s, 99 % under 20 ms: p99 rank 9.9 lands in (0.01, 0.02]
FAST = [5.0, 9.95, 10.0, 10.0, 10.0, 10.0, 10.0, 10.0]
# 10 req/s, 50 % between 40 and 60 ms
SLOW = [0.0, 2.0, 5.0, 10.0, 10.0, 10.0, 10.0, 10.0]


def test_parse_prom_vector_and_nan():
    s = L.parse_prom_vector(vector([({"pod": "a", "le": "+Inf"}, "NaN"),
                                    ({"pod": "a", "le": "0.1"}, 3.5)]))
    assert s == [({"pod": "a", "le": "+Inf"}, 0.0), ({"pod": "a", "le": "0.1"}, 3.5)]


@pytest.mark.parametrize("bad", [{"status": "error", "error": "bad query"},
                                 {"status": "success", "data": {"resultType": "matrix"}},
                                 "not a dict"])
def test_parse_prom_vector_rejects(bad):
    with pytest.raises(ValueError):
        L.parse_prom_vector(bad)


def test_prom_bucket_quantile_matches_promql():
    b = {float(le): v for le, v in zip(LES, FAST)}
    # rank 9.9: bucket (0.01, 0.02] holds 5..9.95 -> 0.01 + 0.01*(4.9/4.95)
    assert L.prom_bucket_quantile(b, 0.99) == pytest.approx(0.01 + 0.01 * 4.9 / 4.95)
    b = {float(le): v for le, v in zip(LES, SLOW)}
    assert L.prom_bucket_quantile(b, 0.99) == pytest.approx(0.04 + 0.02 * 4.9 / 5)
    assert L.prom_bucket_quantile({0.1: 0.0, math.inf: 0.0}, 0.99) is None
    assert L.prom_bucket_quantile({0.1: 1.0}, 0.99) is None  # no +Inf bucket
    assert L.prom_bucket_quantile({0.1: 1.0, math.inf: 10.0}, 0.99) == 0.1  # rank in +Inf


def test_summarize_prom_maps_pods_to_nodes():
    samples = series("vllm-a", FAST) + series("vllm-b", SLOW)
    v = L.summarize_prom_ttft(samples, {"vllm-a": "node-a", "vllm-b": "node-b"}, 60, 200)
    assert set(v.nodes) == {"node-a", "node-b"}
    assert v.nodes["node-a"].requests == 600 and v.nodes["node-a"].sufficient
    assert v.nodes["node-a"].p99_ms == pytest.approx(19.9, abs=0.05)
    assert v.nodes["node-b"].p99_ms == pytest.approx(59.6, abs=0.05)
    assert v.cluster.requests == 1200
    assert v.window_s == 60


def test_summarize_prom_drops_pods_not_in_this_cluster():
    """N4: series of pods not in this cluster's pod->node map (another cluster in
    the same project, same namespace) affect neither node nor cluster p99."""
    other_cluster = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 100.0]  # 100 req/s, all > 250 ms
    mine = {"vllm-a": "node-a"}
    alone = L.summarize_prom_ttft(series("vllm-a", FAST), mine, 60, 200)
    mixed = L.summarize_prom_ttft(series("vllm-a", FAST) + series("vllm-x", other_cluster),
                                  mine, 60, 200)
    assert mixed.cluster == alone.cluster and mixed.nodes == alone.nodes
    assert mixed.skipped == 1
    # nothing of ours in the result -> no signal, so /baseline refuses
    v = L.summarize_prom_ttft(series("vllm-x", other_cluster), mine, 60, 200)
    assert v.cluster.requests == 0 and v.cluster.p99_ms is None and not v.cluster.sufficient


def test_summarize_prom_sums_pods_on_one_node():
    v = L.summarize_prom_ttft(series("p1", FAST) + series("p2", SLOW),
                              {"p1": "n", "p2": "n"}, 60, 200)
    assert v.nodes["n"].requests == 1200
    assert 20 < v.nodes["n"].p99_ms < 60


def test_summarize_prom_insufficient_and_idle():
    low = [x / 100 for x in FAST]  # 0.1 req/s -> 6 requests in 60 s
    v = L.summarize_prom_ttft(series("p", low), {"p": "n"}, 60, 200)
    assert v.nodes["n"].requests == 6 and v.nodes["n"].p99_ms is None
    assert not v.nodes["n"].sufficient
    v = L.summarize_prom_ttft(series("p", [0.0] * len(LES)), {"p": "n"}, 60, 1)
    assert v.nodes["n"].p99_ms is None and not v.nodes["n"].sufficient


def test_summarize_prom_skips_bad_labels():
    v = L.summarize_prom_ttft([({"le": "0.1"}, 1.0), ({"pod": "p", "le": "x"}, 1.0)], {}, 60)
    assert v.skipped == 2 and v.cluster.requests == 0


# --- HTTP source with a fake Prometheus API -------------------------------------

aiohttp = pytest.importorskip("aiohttp")
from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestServer  # noqa: E402

import ttft_source as TS  # noqa: E402

PROM_CFG = L.validate_config({"ttft_source": {"type": "prometheus",
                                              "prometheus": {"project": "proj-x"}}})


async def _token():
    return "tok-123"


async def _with_fake(handler, fn):
    app = web.Application()
    app.router.add_post("/v1/projects/{project}/location/{loc}/prometheus/api/v1/query", handler)
    server = TestServer(app)
    await server.start_server()
    try:
        async with aiohttp.ClientSession() as session:
            return await fn(session, str(server.make_url("")).rstrip("/"))
    finally:
        await server.close()


def _poll(endpoint, session):
    return TS.poll_once(PROM_CFG, loadgen_session=None, prom_session=session, loadgen_ips=[],
                        loadgen_port=8080, namespace="inference", token_fn=_token,
                        prom_endpoint=endpoint)


def test_poll_prometheus_success_sends_query_and_token():
    seen = {}

    async def handler(req):
        form = await req.post()
        seen.update(query=form["query"], auth=req.headers.get("Authorization"),
                    project=req.match_info["project"], loc=req.match_info["loc"])
        return web.json_response(vector(series("vllm-a", FAST)))

    snap = asyncio.run(_with_fake(handler, lambda s, ep: _poll(ep, s)))
    assert snap.ok and snap.kind == "prometheus" and snap.window_s == 60
    assert seen == {"query": 'sum by (pod, le)(rate(vllm:time_to_first_token_seconds_bucket'
                             '{namespace="inference"}[60s]))',
                    "auth": "Bearer tok-123", "project": "proj-x", "loc": "global"}
    view, ok, why = TS.snapshot_view(snap, PROM_CFG, {"vllm-a": "node-a"}, 200,
                                     snap.fetched_at + 1, 30)
    assert ok and why is None and view.nodes["node-a"].p99_ms == pytest.approx(19.9, abs=0.05)


@pytest.mark.parametrize("status,body", [
    (403, {"error": "permission denied: monitoring.timeSeries.list"}),
    (200, {"status": "error", "error": "parse error"}),
    (200, "<html>login</html>"),
])
def test_poll_prometheus_failure_is_no_signal(status, body):
    async def handler(_):
        if isinstance(body, str):
            return web.Response(status=status, text=body, content_type="text/html")
        return web.json_response(body, status=status)

    snap = asyncio.run(_with_fake(handler, lambda s, ep: _poll(ep, s)))
    assert not snap.ok and snap.error
    view, ok, why = TS.snapshot_view(snap, PROM_CFG, {}, 200, snap.fetched_at, 30)
    assert not ok and why and view.nodes == {} and view.cluster.p99_ms is None


def test_token_failure_is_no_signal():
    async def bad_token():
        raise RuntimeError("no ADC credentials")

    async def handler(_):
        return web.json_response(vector([]))

    async def run(session, ep):
        return await TS.poll_once(PROM_CFG, loadgen_session=None, prom_session=session,
                                  loadgen_ips=[], loadgen_port=8080, namespace="inference",
                                  token_fn=bad_token, prom_endpoint=ep)

    snap = asyncio.run(_with_fake(handler, run))
    assert not snap.ok and "no ADC credentials" in snap.error


def test_snapshot_view_stale_wrong_kind_and_none():
    snap = TS.TTFTSnapshot(kind="prometheus", fetched_at=100.0, ok=True, window_s=60,
                           prom_samples=series("p", FAST))
    _, ok, why = TS.snapshot_view(snap, PROM_CFG, {"p": "n"}, 200, 131, 30)
    assert not ok and "old" in why
    _, ok, _ = TS.snapshot_view(snap, L.validate_config({}), {"p": "n"}, 200, 101, 30)
    assert not ok  # config switched to loadgen; prometheus snapshot is ignored
    none_cfg = L.validate_config({"ttft_source": {"type": "none"}})
    _, ok, why = TS.snapshot_view(snap, none_cfg, {"p": "n"}, 200, 101, 30)
    assert not ok and "none" in why
    snap = asyncio.run(TS.poll_once(none_cfg, loadgen_session=None, prom_session=None,
                                    loadgen_ips=[], loadgen_port=8080, namespace="inference",
                                    token_fn=None))
    assert not snap.ok and snap.kind == "none"


def test_guard_never_acts_without_signal():
    """End-to-end fail-safe: a breaching node with no signal is neither blocked nor evicted."""
    cfg = L.GuardConfig(baseline_ttft_p99_ms=20)
    snap = TS.TTFTSnapshot(kind="prometheus", fetched_at=100.0, ok=False, error="HTTP 403")
    view, ok, _ = TS.snapshot_view(snap, PROM_CFG, {"p": "n"}, 200, 101, 30)
    summ = view.nodes.get("n") if ok else None
    assert L.guard_signal(summ, ok, cfg) == L.GUARD_SIGNAL_NONE
    g, acts = L.guard_step(L.NodeGuard(), None, 0, 101, cfg, has_memory_heavy=True)
    assert g.state is L.GuardState.HEALTHY and acts == []


def test_prom_query_cluster_matcher():
    assert TS.prom_ttft_query("m", "inference", 60) == \
        'sum by (pod, le)(rate(m_bucket{namespace="inference"}[60s]))'
    assert TS.prom_ttft_query("m", "inference", 60, "g4-prod") == \
        'sum by (pod, le)(rate(m_bucket{namespace="inference",cluster="g4-prod"}[60s]))'


def test_poll_prometheus_sends_cluster_matcher():
    cfg = L.validate_config({"ttft_source": {"type": "prometheus", "prometheus": {
        "project": "proj-x", "cluster": "g4cluster-a"}}})
    seen = {}

    async def handler(req):
        seen["query"] = (await req.post())["query"]
        return web.json_response(vector([]))

    async def run(session, ep):
        return await TS.poll_once(cfg, loadgen_session=None, prom_session=session,
                                  loadgen_ips=[], loadgen_port=8080, namespace="inference",
                                  token_fn=_token, prom_endpoint=ep)

    snap = asyncio.run(_with_fake(handler, run))
    assert snap.ok and 'cluster="g4cluster-a"' in seen["query"]
