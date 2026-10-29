"""Unit tests for colo_logic (pure; no cluster)."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import colo_logic as L  # noqa: E402

GI = L.GI


# --- quantities -------------------------------------------------------------

@pytest.mark.parametrize("q,m", [("2", 2000), ("500m", 500), ("47.5", 47500), (1, 1000),
                                 ("100000n", 1), (None, 0), ("", 0)])
def test_parse_cpu(q, m):
    assert L.parse_cpu(q) == m


@pytest.mark.parametrize("q,b", [("4Gi", 4 * GI), ("512Mi", 512 * 1024**2), ("1G", 10**9),
                                 ("1000", 1000), ("1k", 1000), (None, 0)])
def test_parse_mem(q, b):
    assert L.parse_mem(q) == b


def test_pod_requests_init_and_overhead():
    cpu, mem = L.pod_requests([{"cpu": "1", "memory": "1Gi"}, {"cpu": "500m"}],
                              [{"cpu": "3", "memory": "512Mi"}], {"cpu": "250m", "memory": "1Mi"})
    assert cpu == 3250
    assert mem == GI + 1024**2


# --- config -----------------------------------------------------------------

def test_config_defaults_and_merge():
    c = L.validate_config({"demand": {"sandbox": 4}, "guard": {"factor": 2}})
    assert c["demand"] == {"sandbox": 4, "toolcall": 0, "analytics": 0}
    assert c["guard"]["factor"] == 2 and c["guard"]["cooldown_s"] == 600
    assert c["guard"]["min_requests"] == 200


@pytest.mark.parametrize("bad", [{"demand": {"sandbox": -1}}, {"demand": {"bogus": 1}},
                                 {"demand": {"sandbox": 1.5}}, {"guard": {"factor": 0.5}},
                                 {"guard": {"enabled": "yes"}}, {"buffer_replicas": -1},
                                 {"guard": {"baseline_ttft_p99_ms": 0}}])
def test_config_rejects(bad):
    with pytest.raises(ValueError):
        L.validate_config(bad)


def test_deep_merge_does_not_mutate():
    base = {"a": {"b": 1, "c": 2}}
    out = L.deep_merge(base, {"a": {"b": 9}})
    assert out == {"a": {"b": 9, "c": 2}} and base == {"a": {"b": 1, "c": 2}}


# --- (a) slots --------------------------------------------------------------

def g4(name="n1", alloc=47_000, used=8_500, blocks=(), mem=None, used_mem=None, reserve=0):
    return L.NodeCapacity(name, alloc, used, frozenset(blocks), mem, used_mem, reserve)


def test_slots_cpu_only():
    n = g4(alloc=47_000, used=8_500)  # 38.5 spare -> 19 x 2-CPU pods
    assert n.spare_cpu_m == 38_500
    assert L.node_slots(n) == {"sandbox": 19, "toolcall": 19, "analytics": 19}


def test_slots_respect_blocks():
    n = g4(blocks={L.CLASS_MEMORY_HEAVY})
    s = L.node_slots(n)
    assert s["analytics"] == 0 and s["sandbox"] == 19
    n = g4(blocks={L.CLASS_MEMORY_HEAVY, L.CLASS_INTEGER})
    assert set(L.node_slots(n).values()) == {0}


def test_slots_memory_limits():
    n = g4(alloc=47_000, used=8_000, mem=100 * GI, used_mem=64 * GI)  # 36Gi spare
    s = L.node_slots(n)
    assert s == {"sandbox": 9, "toolcall": 18, "analytics": 6}


def test_slots_overcommitted_node_and_reserve():
    assert set(L.node_slots(g4(alloc=8_000, used=9_000)).values()) == {0}
    assert L.node_slots(g4(alloc=10_000, used=4_000, reserve=1_000))["sandbox"] == 2


def test_blocks_from_labels():
    assert L.blocks_from_labels({"colo.block.integer": "true", "colo.block.memory-heavy": "false"}) \
        == frozenset({L.CLASS_INTEGER})
    assert L.blocks_from_labels(None) == frozenset()


# --- (b) allocation ---------------------------------------------------------

def test_allocate_order_and_overflow():
    a = L.allocate({"sandbox": 15, "toolcall": 10, "analytics": 5}, [g4(alloc=47_000, used=8_500)])
    assert a.backfill == {"sandbox": 15, "toolcall": 4, "analytics": 0}
    assert a.overflow == {"sandbox": 0, "toolcall": 6, "analytics": 5}


def test_allocate_no_nodes():
    a = L.allocate({"sandbox": 3, "toolcall": 0, "analytics": 2}, [])
    assert a.backfill == {"sandbox": 0, "toolcall": 0, "analytics": 0}
    assert a.overflow == {"sandbox": 3, "toolcall": 0, "analytics": 2}


def test_allocate_zero_and_missing_demand():
    a = L.allocate({}, [g4()])
    assert a.backfill == {s: 0 for s in L.SERVICE_ORDER}
    assert a.overflow == {s: 0 for s in L.SERVICE_ORDER}


def test_allocate_blocked_class_goes_to_other_node():
    blocked = g4("a", alloc=20_000, used=0, blocks={L.CLASS_MEMORY_HEAVY})
    free = g4("b", alloc=8_000, used=0)
    a = L.allocate({"sandbox": 0, "toolcall": 0, "analytics": 10}, [blocked, free])
    assert a.backfill["analytics"] == 4 and a.overflow["analytics"] == 6
    assert a.placement["a"] == {} and a.placement["b"] == {"analytics": 4}


def test_allocate_spreads_and_is_deterministic():
    a = L.allocate({"sandbox": 4, "toolcall": 0, "analytics": 0},
                   [g4("b", alloc=10_000, used=0), g4("a", alloc=10_000, used=0)])
    assert a.placement == {"a": {"sandbox": 2}, "b": {"sandbox": 2}}


def test_allocate_memory_constrained():
    n = g4(alloc=40_000, used=0, mem=20 * GI, used_mem=0)
    a = L.allocate({"sandbox": 2, "toolcall": 0, "analytics": 5}, [n])
    # sandbox takes 8Gi; 12Gi left -> 2 analytics
    assert a.backfill == {"sandbox": 2, "toolcall": 0, "analytics": 2}
    assert a.overflow["analytics"] == 3


def test_allocate_odd_cpu_remainder():
    a = L.allocate({"sandbox": 10}, [g4(alloc=7_900, used=0)])
    assert a.backfill["sandbox"] == 3 and a.leftover_cpu_m["n1"] == 1_900


# --- (b) scale plan ---------------------------------------------------------

def test_plan_grow_raises_overflow_first():
    p = L.plan_scale(10, 0, 10, 0, 15, 5)
    assert (p.backfill, p.overflow, p.order) == (15, 5, ("overflow", "backfill"))


def test_plan_shrink_backfill_first():
    p = L.plan_scale(15, 5, 15, 5, 10, 0)
    assert (p.backfill, p.overflow, p.order) == (10, 0, ("backfill", "overflow"))


def test_plan_lost_slots_holds_backfill_until_overflow_ready():
    # guard blocked: target moves 10 pods from backfill to overflow
    p = L.plan_scale(15, 5, 15, 5, 5, 15)
    assert (p.backfill, p.overflow) == (15, 15)
    assert p.order == ("overflow", "backfill")
    p = L.plan_scale(15, 15, 12, 9, 5, 15)  # 9 overflow ready -> keep 11 backfill
    assert (p.backfill, p.overflow) == (11, 15)
    p = L.plan_scale(11, 15, 11, 15, 5, 15)
    assert (p.backfill, p.overflow, p.order) == (5, 15, ("backfill", "overflow"))


def test_plan_freed_slots_holds_overflow_until_backfill_ready():
    p = L.plan_scale(5, 15, 5, 15, 15, 5)
    assert (p.backfill, p.overflow) == (15, 15)
    p = L.plan_scale(15, 15, 12, 15, 15, 5)
    assert (p.backfill, p.overflow) == (15, 8)
    p = L.plan_scale(15, 8, 15, 8, 15, 5)
    assert (p.backfill, p.overflow) == (15, 5)


def test_plan_unchanged():
    p = L.plan_scale(3, 4, 3, 4, 3, 4)
    assert (p.backfill, p.overflow) == (3, 4)


def test_plan_to_zero():
    p = L.plan_scale(5, 5, 5, 5, 0, 0)
    assert (p.backfill, p.overflow) == (0, 0)


# --- (c) histograms ---------------------------------------------------------

def hist(values_ms, n_regular=1000, bucket=2):
    h = [0] * (n_regular + 1)
    for v in values_ms:
        i = int(v // bucket)
        h[min(i, n_regular)] += 1
    return h


def test_merge_hists():
    assert L.merge_hists([[1, 2, 3], [0, 1, 1]]) == [1, 3, 4]
    assert L.merge_hists([]) == []
    with pytest.raises(ValueError):
        L.merge_hists([[1, 2], [1, 2, 3]])


def test_quantile_basic():
    h = hist([10] * 99 + [100])  # 99th of 100 = 10ms bucket -> upper edge 12
    assert L.hist_quantile(h, 0.99) == 12.0
    h = hist([10] * 98 + [100] * 2)
    assert L.hist_quantile(h, 0.99) == 102.0


def test_quantile_empty_and_overflow():
    assert L.hist_quantile([0] * 1001) is None
    assert L.hist_quantile(hist([5000] * 10)) == 2000.0
    assert L.hist_quantile(hist([1999.9] * 10)) == 2000.0  # last regular bucket upper edge


def stats_payload(per_pod):
    return {"rate": 10, "window_s": 30, "bucket_ms": 2,
            "per_pod": {p: {"node": n, "ttft_hist": h, "inflight": 0, "ok": sum(h), "err": 0}
                        for p, (n, h) in per_pod.items()}}


def test_summarize_merges_replicas_per_node():
    # two loadgen replicas see the same vLLM pod on node a; node b on its own
    r1 = stats_payload({"vllm-1": ("a", hist([20] * 150)), "vllm-2": ("b", hist([40] * 300))})
    r2 = stats_payload({"vllm-1": ("a", hist([20] * 148 + [500] * 2))})
    v = L.summarize_ttft([r1, r2], min_requests=200)
    assert v.nodes["a"].requests == 300 and v.nodes["a"].p99_ms == 22.0
    assert v.nodes["b"].p99_ms == 42.0
    assert v.cluster.requests == 600 and v.cluster.sufficient
    assert v.window_s == 30 and v.skipped == 0


def test_summarize_insufficient_data():
    r = stats_payload({"vllm-1": ("a", hist([20] * 199))})
    v = L.summarize_ttft([r], min_requests=200)
    assert v.nodes["a"].p99_ms is None and not v.nodes["a"].sufficient
    assert v.nodes["a"].requests == 199
    assert v.cluster.p99_ms is None


def test_summarize_skips_bad_entries():
    good = stats_payload({"vllm-1": ("a", hist([20] * 300))})
    bad_len = stats_payload({"vllm-2": ("a", [1, 2, 3])})
    bad_bucket = dict(stats_payload({"vllm-3": ("a", hist([20] * 300))}), bucket_ms=5)
    v = L.summarize_ttft([good, bad_len, bad_bucket, {"per_pod": {"x": {"node": "a"}}}])
    assert v.nodes["a"].requests == 300 and v.skipped == 3


def test_summarize_empty():
    v = L.summarize_ttft([])
    assert v.nodes == {} and v.cluster.requests == 0 and v.cluster.p99_ms is None


# --- (d) guard --------------------------------------------------------------

CFG = L.GuardConfig(enabled=True, baseline_ttft_p99_ms=60, factor=1.5, cooldown_s=600,
                    escalate_s=60, min_requests=200)
BAD, GOOD = 120.0, 70.0  # threshold 90


def run(seq, g=None, cfg=CFG):
    """seq: list of (t, p99, requests). Returns final guard and all actions."""
    g = g or L.NodeGuard()
    acts = []
    for t, p, n in seq:
        g, a = L.guard_step(g, p, n, t, cfg)
        acts += [(t, x.kind, x.classes, x.reason) for x in a]
    return g, acts


def test_guard_stays_healthy():
    g, acts = run([(0, GOOD, 500), (5, 90.0, 500)])  # == threshold is not a breach
    assert g.state is L.GuardState.HEALTHY and acts == []


def test_guard_blocks_memory_heavy_then_integer():
    g, acts = run([(0, BAD, 500)])
    assert g.state is L.GuardState.BLOCK_MEMORY_HEAVY and g.blocks == {L.CLASS_MEMORY_HEAVY}
    assert acts == [(0, "block", ("memory-heavy",), "ColoGuardEvict")]
    g, acts = run([(30, BAD, 500), (55, BAD, 500)], g)
    assert g.state is L.GuardState.BLOCK_MEMORY_HEAVY and acts == []
    g, acts = run([(60, BAD, 500)], g)
    assert g.state is L.GuardState.BLOCK_ALL
    assert g.blocks == {L.CLASS_MEMORY_HEAVY, L.CLASS_INTEGER}
    assert acts == [(60, "block", ("integer",), "ColoGuardEvict")]


def test_guard_no_escalation_if_recovered_in_between():
    g, acts = run([(0, BAD, 500), (30, GOOD, 500), (65, BAD, 500), (100, BAD, 500)])
    assert g.state is L.GuardState.BLOCK_MEMORY_HEAVY and len(acts) == 1
    g, acts = run([(125, BAD, 500)], g)  # 60 s after the new breach run started at 65
    assert g.state is L.GuardState.BLOCK_ALL


def test_guard_release_after_cooldown():
    g, _ = run([(0, BAD, 500), (60, BAD, 500)])
    assert g.state is L.GuardState.BLOCK_ALL
    g, acts = run([(100, GOOD, 500), (699, GOOD, 500)], g)
    assert g.state is L.GuardState.BLOCK_ALL and acts == []
    g, acts = run([(700, GOOD, 500)], g)
    assert g.state is L.GuardState.HEALTHY and g.blocks == frozenset()
    assert acts == [(700, "release", ("integer", "memory-heavy"), "ColoGuardRelease")]


def test_guard_cooldown_resets_on_breach():
    g, _ = run([(0, BAD, 500)])
    g, acts = run([(10, GOOD, 500), (500, GOOD, 500), (505, BAD, 500), (520, GOOD, 500),
                   (1100, GOOD, 500)], g)
    assert g.state is L.GuardState.BLOCK_MEMORY_HEAVY and acts == []
    g, acts = run([(1120, GOOD, 500)], g)
    assert g.state is L.GuardState.HEALTHY
    assert acts == [(1120, "release", ("memory-heavy",), "ColoGuardRelease")]


def test_guard_insufficient_data_no_transition():
    g, acts = run([(0, BAD, 199), (5, None, 0)])
    assert g.state is L.GuardState.HEALTHY and acts == []
    g, _ = run([(0, BAD, 500)])
    g, acts = run([(10, GOOD, 500), (300, GOOD, 50), (650, GOOD, 500)], g)
    # gap restarted the healthy run at 650
    assert g.state is L.GuardState.BLOCK_MEMORY_HEAVY and g.healthy_since == 650 and acts == []
    g, acts = run([(1250, GOOD, 500)], g)
    assert g.state is L.GuardState.HEALTHY


def test_guard_insufficient_data_does_not_escalate():
    g, acts = run([(0, BAD, 500), (70, BAD, 10)])
    assert g.state is L.GuardState.BLOCK_MEMORY_HEAVY and len(acts) == 1
    g, acts = run([(75, BAD, 500)], g)  # breach timer kept from t=0
    assert g.state is L.GuardState.BLOCK_ALL


def test_guard_no_baseline_no_decision():
    cfg = L.GuardConfig(baseline_ttft_p99_ms=None)
    g, acts = run([(0, 5000.0, 10_000)], cfg=cfg)
    assert g.state is L.GuardState.HEALTHY and acts == []


def test_guard_disabled_releases_immediately():
    g, _ = run([(0, BAD, 500), (60, BAD, 500)])
    off = L.GuardConfig(enabled=False)
    g, acts = run([(61, BAD, 500)], g, cfg=off)
    assert g.state is L.GuardState.HEALTHY
    assert acts[0][1] == "release" and acts[0][2] == ("integer", "memory-heavy")
    g, acts = run([(62, BAD, 500)], g, cfg=off)
    assert acts == [] and g.state is L.GuardState.HEALTHY


def test_guard_from_labels():
    assert L.guard_from_labels({"colo.block.memory-heavy": "true"}).state \
        is L.GuardState.BLOCK_MEMORY_HEAVY
    assert L.guard_from_labels({"colo.block.integer": "true"}).state is L.GuardState.BLOCK_ALL
    assert L.guard_from_labels({}).state is L.GuardState.HEALTHY


def test_guard_config_from_config():
    c = L.GuardConfig.from_config(L.validate_config({"guard": {"factor": 2.0,
                                                              "baseline_ttft_p99_ms": 50}}))
    assert c.threshold_ms == 100.0 and c.escalate_s == 60 and c.min_requests == 200
