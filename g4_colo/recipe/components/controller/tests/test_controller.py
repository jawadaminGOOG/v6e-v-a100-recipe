"""Controller wiring tests: one reconcile tick against fake Kubernetes APIs.

Checks the I/O layer follows colo_logic's plan (no cluster). Skipped when the
controller's runtime dependencies (aiohttp, kubernetes) are not installed.
"""

import asyncio
import json
import os
import sys
from types import SimpleNamespace as NS

import pytest

pytest.importorskip("aiohttp")
pytest.importorskip("kubernetes")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import colo_logic as L  # noqa: E402
import controller as C  # noqa: E402
import ttft_source as TS  # noqa: E402

READY = [NS(type="Ready", status="True")]


def k8s_node(name, labels=None, unschedulable=False, taints=()):
    return NS(metadata=NS(name=name, uid=f"uid-{name}", labels={"colo.role": "g4", **(labels or {})}),
              spec=NS(unschedulable=unschedulable, taints=[NS(key=k) for k in taints]),
              status=NS(conditions=READY, allocatable={"cpu": "47", "memory": "180Gi"}))


def k8s_pod(name, ns, labels, node, cpu="2", terminating=False, ready=True):
    return NS(metadata=NS(name=name, namespace=ns, labels=labels,
                          deletion_timestamp="2026-10-30T00:00:00Z" if terminating else None),
              spec=NS(node_name=node, containers=[NS(resources=NS(requests={"cpu": cpu,
                                                                            "memory": "2Gi"}))],
                      init_containers=None, overhead=None),
              status=NS(conditions=READY if ready else [], pod_ip="10.0.0.9"))


def vllm_pod(node, name="vllm-1", terminating=False):
    return k8s_pod(name, "inference", {"app": "vllm"}, node, cpu="8", terminating=terminating)


def bf_pod(name, node, svc="sandbox", cls="integer"):
    return k8s_pod(name, "colo-svc", {"app": svc, "colo.tier": "backfill", "colo.class": cls}, node)


class FakeCore:
    def __init__(self, nodes, pods, config):
        self.nodes, self.pods, self.config = nodes, pods, config
        self.patches, self.deletes, self.events = [], [], []

    def list_node(self):
        return NS(items=self.nodes)

    def list_pod_for_all_namespaces(self, field_selector=None):
        return NS(items=self.pods)

    def read_namespaced_config_map(self, name, ns):
        return NS(data={"config.json": json.dumps(self.config)})

    def patch_node(self, name, body):
        self.patches.append((name, body["metadata"]["labels"]))

    def delete_namespaced_pod(self, name, ns, grace_period_seconds=None):
        self.deletes.append(name)

    def create_namespaced_event(self, ns, ev):
        self.events.append(ev.reason)


class FakeApps:
    def __init__(self, deploys):
        self.deploys = deploys  # name -> (replicas, ready)
        self.scales = []

    def read_namespaced_deployment(self, name, ns):
        r, ready = self.deploys.get(name, (0, 0))
        return NS(spec=NS(replicas=r), status=NS(ready_replicas=ready))

    def patch_namespaced_deployment_scale(self, name, ns, body):
        self.scales.append((name, body["spec"]["replicas"]))


def deploys(sb_bf=(2, 2), sb_ov=(0, 0)):
    d = {f"{s}-{t}": (0, 0) for s in L.SERVICE_ORDER for t in ("backfill", "overflow")}
    d["sandbox-backfill"], d["sandbox-overflow"] = sb_bf, sb_ov
    d["colo-buffer"] = (1, 1)
    return d


def make(nodes, pods, cfg, dep):
    core, apps = FakeCore(nodes, pods, cfg), FakeApps(dep)
    ctl = C.Controller(core=core, apps=apps, token_fn=lambda: None)
    return ctl, core, apps


CFG = {"demand": {"sandbox": 2}, "guard": {"baseline_ttft_p99_ms": 20}}


def test_rollout_keeps_backfill_and_drops_colo_active():
    nodes = [k8s_node("g4-a", {"colo.active": "true"})]
    pods = [vllm_pod("g4-a", terminating=True), bf_pod("sb-1", "g4-a"), bf_pod("sb-2", "g4-a")]
    ctl, core, apps = make(nodes, pods, CFG, deploys())
    asyncio.run(ctl.tick())
    assert core.patches == [("g4-a", {"colo.active": None})]
    assert core.deletes == []
    # node dropped from allocation: overflow scaled up first, backfill held (ready pods)
    assert ("sandbox-overflow", 2) in apps.scales
    assert not any(n == "sandbox-backfill" for n, _ in apps.scales)
    row = ctl.nodes_state[0]
    assert row["eligible"] is False and row["draining_pods"] == 2
    assert row["guard_signal"] == "idle"


def test_scale_in_deletes_after_overflow_ready():
    nodes = [k8s_node("g4-a")]
    pods = [bf_pod("sb-1", "g4-a"), bf_pod("sb-2", "g4-a")]
    ctl, core, apps = make(nodes, pods, CFG, deploys(sb_bf=(2, 2), sb_ov=(2, 2)))
    asyncio.run(ctl.tick())
    assert sorted(core.deletes) == ["sb-1", "sb-2"]
    assert ("sandbox-backfill", 0) in apps.scales


def test_ca_taint_deletes_now_and_guard_evicts_blocked_class():
    nodes = [k8s_node("g4-a", {"colo.active": "true"}, taints=[L.CA_TAINT]),
             k8s_node("g4-b", {"colo.active": "true", "colo.block.memory-heavy": "true"})]
    pods = [vllm_pod("g4-a"), bf_pod("sb-1", "g4-a"),
            vllm_pod("g4-b", "vllm-2"), bf_pod("an-1", "g4-b", "analytics", "memory-heavy")]
    ctl, core, _ = make(nodes, pods, CFG, deploys())
    asyncio.run(ctl.tick())
    assert sorted(core.deletes) == ["an-1", "sb-1"]
    assert ("g4-a", {"colo.active": None}) in core.patches


def test_no_signal_reported_and_no_guard_action():
    nodes = [k8s_node("g4-a", {"colo.active": "true"})]
    pods = [vllm_pod("g4-a"), bf_pod("an-1", "g4-a", "analytics", "memory-heavy")]
    ctl, core, _ = make(nodes, pods, {**CFG, "ttft_source": {"type": "none"}}, deploys())
    asyncio.run(ctl.tick())
    assert core.deletes == [] and core.events == []
    st = ctl.state()
    assert st["guard_state"] == "no_signal" and st["guard_data_sufficient"] is False
    assert st["nodes"][0]["guard_state"] == "no_signal"
    assert "colo_guard_data_sufficient 0.0" in ctl.metrics()


def test_guard_blocks_with_fresh_signal():
    nodes = [k8s_node("g4-a", {"colo.active": "true"})]
    pods = [vllm_pod("g4-a"), bf_pod("an-1", "g4-a", "analytics", "memory-heavy")]
    ctl, core, _ = make(nodes, pods, CFG, deploys())
    hist = [0] * 1001
    hist[50] = 500  # 100 ms vs threshold 30 ms
    ctl.ttft_snap = TS.TTFTSnapshot(kind="loadgen", fetched_at=C.time.time(), ok=True,
                                    window_s=30, loadgen_stats=[{"bucket_ms": 2, "per_pod": {
                                        "vllm-1": {"node": "g4-a", "ttft_hist": hist}}}])
    asyncio.run(ctl.tick())
    assert core.deletes == ["an-1"] and core.events == ["ColoGuardEvict"]
    assert ("g4-a", {"colo.block.memory-heavy": "true"}) in core.patches
    assert ctl.state()["guard_data_sufficient"] is True


def test_liveness():
    ctl, _, _ = make([], [], CFG, deploys())
    t0 = ctl.started_at
    assert ctl.live(t0 + 1)[0]  # startup grace
    assert not ctl.live(t0 + C.STARTUP_GRACE_S + C.LIVENESS_STALE_S + 1)[0]
    ctl.last_loop = t0 + 100
    assert ctl.live(t0 + 100 + C.LIVENESS_STALE_S)[0]
    assert not ctl.live(t0 + 100 + C.LIVENESS_STALE_S + 0.1)[0]


def test_healthz_and_readyz_routes():
    from aiohttp.test_utils import TestClient, TestServer

    async def run():
        ctl, _, _ = make([], [], CFG, deploys())
        app = C.make_app(ctl, start_loops=False)
        async with TestClient(TestServer(app)) as cl:
            ok = (await cl.get("/healthz")).status
            ctl.started_at -= C.STARTUP_GRACE_S + C.LIVENESS_STALE_S + 1  # never looped
            stale = await cl.get("/healthz")
            ready = (await cl.get("/readyz")).status
            return ok, stale.status, (await stale.json())["reason"], ready

    ok, stale, reason, ready = asyncio.run(run())
    assert (ok, stale, ready) == (200, 503, 200) and "stalled" in reason
