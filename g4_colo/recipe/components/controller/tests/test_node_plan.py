"""Unit tests for per-node planning (colo.active, dehydrate, guard reset), config
additions and plan_scale under external pod loss. Pure; no cluster."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import colo_logic as L  # noqa: E402

GRACE = 60.0
NO_COVER: dict = {}


def vllm(name="vllm-1", terminating=False, ready=True):
    return L.PodFacts(name, L.ROLE_VLLM, terminating=terminating, ready=ready, cpu_m=8000)


def bf(name, svc="sandbox", cls=L.CLASS_INTEGER, ready=True, terminating=False):
    return L.PodFacts(name, L.ROLE_BACKFILL, service=svc, cls=cls, ready=ready,
                      terminating=terminating, cpu_m=2000, mem=4 * L.GI)


def node(name="g4-a", labels=None, ready=True, unschedulable=False, taints=()):
    return L.NodeFacts(name, dict(labels or {}), ready, unschedulable, frozenset(taints),
                       47_000, 180 * L.GI)


ACTIVE = {L.ACTIVE_LABEL: "true"}
BACKFILL = [bf("sb-1"), bf("sb-2"), bf("an-1", "analytics", L.CLASS_MEMORY_HEAVY)]


# --- eligibility --------------------------------------------------------------

@pytest.mark.parametrize("n,pods,reason", [
    (node(), [vllm()], "ok"),
    (node(), [vllm(ready=False)], "ok"),  # compiling vLLM still packs backfill
    (node(ready=False), [vllm()], "not-ready"),
    (node(unschedulable=True), [vllm()], "cordoned"),
    (node(taints=[L.CA_TAINT]), [vllm()], "ca-scale-down"),
    (node(), [vllm(terminating=True)], "no-vllm"),
    (node(), [], "no-vllm"),
])
def test_node_eligibility(n, pods, reason):
    e = L.node_eligibility(n, pods)
    assert e.reason == reason and e.eligible == (reason == "ok")


def test_eligible_node_gets_colo_active_once():
    p = L.plan_node(node(), [vllm()] + BACKFILL, 0, L.NodeTrack(), NO_COVER, GRACE)
    assert p.eligible and p.label_patch == {L.ACTIVE_LABEL: "true"}
    assert p.delete_pods == [] and p.track.ineligible_since is None
    p = L.plan_node(node(labels=ACTIVE), [vllm()] + BACKFILL, 1, L.NodeTrack(), NO_COVER, GRACE)
    assert p.label_patch == {}


def test_eligible_again_clears_timer():
    p = L.plan_node(node(labels={}), [vllm()], 50, L.NodeTrack(10.0), NO_COVER, GRACE)
    assert p.eligible and p.track.ineligible_since is None


# --- rollout (4.2) --------------------------------------------------------------

def test_rollout_within_grace_deletes_nothing():
    """vLLM Recreate: every vLLM pod terminating, then new pods bind within the grace."""
    pods = [vllm(terminating=True)] + BACKFILL
    track = L.NodeTrack()
    p = L.plan_node(node(labels=ACTIVE), pods, 100, track, NO_COVER, GRACE)
    assert not p.eligible and p.reason == "no-vllm"
    assert p.label_patch == {L.ACTIVE_LABEL: None}  # stop new placements at once
    assert p.delete_pods == [] and p.draining == ["an-1", "sb-1", "sb-2"]
    assert p.reset_guard
    track = p.track
    # vLLM gone entirely, 40 s later: still inside the grace
    p = L.plan_node(node(), BACKFILL, 140, track, NO_COVER, GRACE)
    assert p.delete_pods == [] and p.label_patch == {}
    track = p.track
    # new vLLM pod binds at 150 (not Ready yet): node active again, nothing deleted
    p = L.plan_node(node(), [vllm("vllm-2", ready=False)] + BACKFILL, 150, track, NO_COVER, GRACE)
    assert p.eligible and p.delete_pods == [] and p.label_patch == {L.ACTIVE_LABEL: "true"}
    assert p.track.ineligible_since is None


def test_rollout_longer_than_grace_dehydrates():
    p = L.plan_node(node(labels=ACTIVE), BACKFILL, 0, L.NodeTrack(), NO_COVER, GRACE)
    p = L.plan_node(node(), BACKFILL, 59.9, p.track, NO_COVER, GRACE)
    assert p.delete_pods == []
    p = L.plan_node(node(), BACKFILL, 60, p.track, NO_COVER, GRACE)
    assert p.delete_pods == ["an-1", "sb-1", "sb-2"] and "60s" in p.drain_reason
    assert p.draining == []


# --- scale-in: make-before-break (1.4 / 4.1) -------------------------------------

def test_scale_in_deletes_only_covered_services():
    pods = list(BACKFILL)  # vLLM scaled in: no vLLM pod on the node
    p = L.plan_node(node(labels=ACTIVE), pods, 0, L.NodeTrack(), NO_COVER, GRACE)
    assert p.delete_pods == []
    # overflow for sandbox is ready, analytics not yet
    cover = {"sandbox": True, "analytics": False}
    p = L.plan_node(node(), pods, 5, p.track, cover, GRACE)
    assert p.delete_pods == ["sb-1", "sb-2"] and p.draining == ["an-1"]
    assert p.drain_reason == "overflow ready covers demand"
    p = L.plan_node(node(), [pods[2]], 10, p.track, {"analytics": True}, GRACE)
    assert p.delete_pods == ["an-1"]


def test_drain_cover_counts_overflow_and_eligible_backfill():
    demand = {"sandbox": 20, "toolcall": 0, "analytics": 6}
    c = L.drain_cover(demand, {"sandbox": 10, "analytics": 0}, {"sandbox": 9, "analytics": 6})
    assert c == {"sandbox": False, "toolcall": True, "analytics": True}
    c = L.drain_cover(demand, {"sandbox": 11}, {"sandbox": 9, "analytics": 6})
    assert c["sandbox"] is True


def test_ready_backfill_by_service_skips_terminating_and_unready():
    pods = [bf("a"), bf("b", ready=False), bf("c", terminating=True),
            bf("d", "analytics", L.CLASS_MEMORY_HEAVY), vllm()]
    assert L.ready_backfill_by_service(pods) == {"sandbox": 1, "analytics": 1}


# --- cordon / CA taint (4.3) ------------------------------------------------------

@pytest.mark.parametrize("n,reason", [
    (node(labels=ACTIVE, unschedulable=True), "cordoned"),
    (node(labels=ACTIVE, taints=[L.CA_TAINT]), "ca-scale-down"),
])
def test_cordon_or_ca_taint_deletes_at_once(n, reason):
    p = L.plan_node(n, [vllm()] + BACKFILL, 0, L.NodeTrack(), NO_COVER, GRACE)
    assert not p.eligible and p.reason == reason
    assert p.label_patch == {L.ACTIVE_LABEL: None}
    assert p.delete_pods == ["an-1", "sb-1", "sb-2"] and p.drain_reason == reason
    assert not p.reset_guard  # vLLM still there: guard keeps running


def test_not_ready_node_waits_for_grace():
    p = L.plan_node(node(labels=ACTIVE, ready=False), [vllm()] + BACKFILL, 0, L.NodeTrack(),
                    NO_COVER, GRACE)
    assert p.reason == "not-ready" and p.delete_pods == []


# --- terminating pods -------------------------------------------------------------

def test_terminating_backfill_not_deleted_again():
    pods = [bf("sb-1", terminating=True), bf("sb-2")]
    p = L.plan_node(node(unschedulable=True), pods, 0, L.NodeTrack(), NO_COVER, GRACE)
    assert p.delete_pods == ["sb-2"]


def test_terminating_vllm_with_new_vllm_stays_eligible():
    p = L.plan_node(node(labels=ACTIVE), [vllm("old", terminating=True), vllm("new")] + BACKFILL,
                    0, L.NodeTrack(), NO_COVER, GRACE)
    assert p.eligible and p.label_patch == {} and p.delete_pods == []


# --- guard reset (4.4) ------------------------------------------------------------

def test_no_vllm_resets_guard_and_clears_block_labels():
    labels = {L.ACTIVE_LABEL: "true", "colo.block.memory-heavy": "true",
              "colo.block.integer": "true"}
    p = L.plan_node(node(labels=labels), BACKFILL, 0, L.NodeTrack(), NO_COVER, GRACE)
    assert p.reset_guard
    assert p.label_patch == {L.ACTIVE_LABEL: None, "colo.block.memory-heavy": None,
                             "colo.block.integer": None}


def test_vllm_present_keeps_block_labels():
    labels = {L.ACTIVE_LABEL: "true", "colo.block.memory-heavy": "true"}
    p = L.plan_node(node(labels=labels), [vllm()], 0, L.NodeTrack(), NO_COVER, GRACE)
    assert not p.reset_guard and p.label_patch == {}


def test_guard_label_patch():
    assert L.guard_label_patch({}, frozenset({L.CLASS_MEMORY_HEAVY})) == \
        {"colo.block.memory-heavy": "true"}
    assert L.guard_label_patch({"colo.block.integer": "true"}, frozenset()) == \
        {"colo.block.integer": None}
    assert L.guard_label_patch({"colo.block.memory-heavy": "true"},
                               frozenset({L.CLASS_MEMORY_HEAVY})) == {}


def test_guard_signal():
    cfg = L.GuardConfig()
    ok = L.TTFTSummary(30.0, 500, True)
    assert L.guard_signal(ok, True, cfg) == L.GUARD_SIGNAL_OK
    assert L.guard_signal(ok, False, cfg) == L.GUARD_SIGNAL_NONE
    assert L.guard_signal(None, True, cfg) == L.GUARD_SIGNAL_NONE
    assert L.guard_signal(L.TTFTSummary(None, 10, False), True, cfg) == L.GUARD_SIGNAL_NONE
    assert L.guard_signal(ok, True, L.GuardConfig(baseline_ttft_p99_ms=None)) == L.GUARD_SIGNAL_NONE
    assert L.guard_signal(ok, True, L.GuardConfig(enabled=False)) == L.GUARD_SIGNAL_DISABLED


# --- capacity ---------------------------------------------------------------------

def test_node_capacity_excludes_backfill_and_applies_reserve():
    c = L.node_capacity(node(), [vllm(), L.PodFacts("ds", cpu_m=500)] + BACKFILL,
                        frozenset({L.CLASS_MEMORY_HEAVY}), 2000)
    assert c.used_cpu_m == 8500 and c.spare_cpu_m == 47_000 - 8500 - 2000
    assert L.node_slots(c)["analytics"] == 0 and L.node_slots(c)["sandbox"] == 18


# --- plan_scale with external pod loss (4.1) --------------------------------------

def test_plan_scale_tolerates_backfill_deleted_externally():
    # dehydrate removed all 19 ready backfill pods; overflow not ready yet
    p = L.plan_scale(19, 0, 0, 0, 0, 19)
    assert (p.backfill, p.overflow) == (0, 19)  # do not recreate 19 Pending backfill
    # partial loss: 10 of 19 still ready elsewhere, overflow 4 ready
    p = L.plan_scale(19, 19, 10, 4, 0, 19)
    assert (p.backfill, p.overflow) == (10, 19)


def test_plan_scale_hold_still_protects_ready_pods():
    p = L.plan_scale(15, 5, 15, 5, 5, 15)
    assert (p.backfill, p.overflow) == (15, 15)


# --- config additions ---------------------------------------------------------------

def test_config_new_defaults():
    c = L.validate_config({})
    assert c["reserve_cpu_per_node"] == 2 and c["dehydrate_grace_s"] == 60
    assert c["ttft_source"]["type"] == "loadgen"
    assert c["ttft_source"]["prometheus"]["location"] == "global"
    assert c["ttft_source"]["prometheus"]["query_window_s"] == 60
    assert c["max_total_cpu"] is None


def test_config_prometheus_requires_project():
    with pytest.raises(ValueError, match="project"):
        L.validate_config({"ttft_source": {"type": "prometheus"}})
    c = L.validate_config({"ttft_source": {"type": "prometheus", "prometheus": {"project": "p"}}})
    assert c["ttft_source"]["prometheus"] == {"project": "p", "location": "global",
                                              "query_window_s": 60,
                                              "metric": "vllm:time_to_first_token_seconds"}


@pytest.mark.parametrize("bad", [{"ttft_source": {"type": "gateway"}},
                                 {"ttft_source": {"type": "none", "prometheus": {"query_window_s": 1}}},
                                 {"dehydrate_grace_s": -1}, {"max_total_cpu": 0},
                                 {"max_total_cpu": 40, "demand": {"sandbox": 21}}])
def test_config_rejects_new_keys(bad):
    with pytest.raises(ValueError):
        L.validate_config(bad)


def test_max_total_cpu_allows_demand_at_limit():
    c = L.validate_config({"max_total_cpu": 40, "demand": {"sandbox": 10, "analytics": 10}})
    assert L.demand_cpu_cores(c["demand"]) == 40
