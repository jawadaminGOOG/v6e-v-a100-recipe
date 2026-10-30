"""Tests for the Wave-2 demo changes: counterfactual, story, error alerts, scenario runner, live backend I/O."""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import types
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
os.environ["DEMO_MOCK"] = "1"

from fastapi.testclient import TestClient  # noqa: E402

import app as demo_app  # noqa: E402
import cluster  # noqa: E402
import scenarios as sc  # noqa: E402
from state import PRICE_N4D16_HR, build_state, classify_errors, counterfactual_n4d, summarize  # noqa: E402


def _pod(name, node, cpu, mem, ns="colo-svc", tier="backfill", cls="integer", app="sandbox", phase="Running"):
    labels = {"app": app}
    if tier:
        labels["colo.tier"] = tier
    if cls:
        labels["colo.class"] = cls
    return {"name": name, "namespace": ns, "uid": name, "labels": labels, "node": node, "phase": phase,
            "ready": True, "cpu_req": cpu, "mem_req_gib": mem, "priority_class": None}


def _node(name, role, cpu, mem):
    return {"name": name, "labels": {"colo.role": role}, "allocatable": {"cpu": str(cpu), "memory": f"{mem}Gi"},
            "ready": True, "created": "2026-10-30T00:00:00Z"}


# ------------------------------------------------------------------ counterfactual
def test_counterfactual_packs_cpu_bound_pods():
    pods = [{"cpu_req": 2, "mem_req_gib": 4}] * 14  # 28 vCPU; 7 per 15-vCPU node
    cf = counterfactual_n4d(pods, 15.0, 58.0, price_hr=0.3)
    assert cf["n4d_nodes"] == 2
    assert cf["per_hour"] == pytest.approx(0.6)
    assert cf["vcpu"] == 28 and cf["pods"] == 14


def test_counterfactual_memory_bound_and_overhead():
    pods = [{"cpu_req": 1, "mem_req_gib": 25}] * 4  # memory decides: 2 per 58 GiB node, 1 per node after 10 GiB overhead
    assert counterfactual_n4d(pods, 15.0, 58.0)["n4d_nodes"] == 2
    assert counterfactual_n4d(pods, 15.0, 58.0, overhead_mem_gib=10.0)["n4d_nodes"] == 4
    # daemonset CPU overhead shrinks usable CPU: 7 x 2 vCPU fit in 15, only 6 fit in 15 - 2
    two = [{"cpu_req": 2, "mem_req_gib": 1}] * 7
    assert counterfactual_n4d(two, 15.0, 58.0)["n4d_nodes"] == 1
    assert counterfactual_n4d(two, 15.0, 58.0, overhead_cpu=2.0)["n4d_nodes"] == 2


def test_counterfactual_edges():
    assert counterfactual_n4d([], 15.0, 58.0)["n4d_nodes"] == 0
    assert counterfactual_n4d([], 15.0, 58.0)["per_hour"] == 0
    # A pod bigger than a node still needs (at least) one node: never under-count.
    assert counterfactual_n4d([{"cpu_req": 20, "mem_req_gib": 1}], 15.0, 58.0)["n4d_nodes"] == 1


def test_build_state_story_and_counterfactual():
    raw = {
        "nodes": [_node("g4a", "g4", 46, 176), _node("n4da", "n4d", 15.89, 57)],
        "pods": [
            _pod("vllm-1", "g4a", 8, 64, ns="inference", tier=None, cls=None, app="vllm"),
            _pod("ds-g4", "g4a", 0.5, 0.5, ns="kube-system", tier=None, cls=None, app="ds"),
            _pod("ds-n4d", "n4da", 0.89, 2, ns="kube-system", tier=None, cls=None, app="ds"),
            *[_pod(f"sb-{i}", "g4a", 2, 4) for i in range(10)],
            *[_pod(f"an-{i}", "g4a", 2, 6, cls="memory-heavy", app="analytics") for i in range(2)],
            *[_pod(f"of-{i}", "n4da", 2, 4, tier="overflow") for i in range(3)],
            _pod("pending-bf", None, 2, 4, phase="Pending"),
        ],
    }
    st = build_state(raw, mode="test")
    s = st["story"]
    assert s["gpu_slice_vcpu"] == 8
    assert s["reclaimed_vcpu"] == 24 and s["reclaimed_pods"] == 12
    assert s["g4_spare_vcpu"] == pytest.approx(46 - 8 - 0.5)
    assert s["reclaimed_pct_of_spare"] == pytest.approx(round(100 * 24 / 37.5, 1))
    assert s["overflow_pods"] == 3 and s["overflow_vcpu"] == 6
    cf = st["cost"]["counterfactual"]
    # usable per N4D node = 15.89 - 0.89 = 15 vCPU -> 7 x 2-vCPU pods per node;
    # 12 backfill + 3 overflow = 15 pods need 3 N4D nodes; 1 is running -> 2 saved
    assert cf["node_cpu"] == pytest.approx(15.0)
    assert cf["n4d_nodes"] == 3 and cf["saved_nodes"] == 2
    assert st["cost"]["saved_per_hour"] == pytest.approx(2 * PRICE_N4D16_HR)
    assert cf["fleet_per_hour"] == pytest.approx(st["cost"]["g4_per_hour"] + 3 * PRICE_N4D16_HR, abs=1e-3)
    assert s["saved_per_hour"] == cf["saved_per_hour"]
    m = summarize(st)
    assert m["saved_per_hour"] == cf["saved_per_hour"]
    assert m["g4_slots_per_node"] == 18


def test_build_state_no_n4d_uses_default_capacity():
    raw = {"nodes": [_node("g4a", "g4", 46, 176)], "pods": [_pod(f"sb-{i}", "g4a", 2, 4) for i in range(8)]}
    cf = build_state(raw)["cost"]["counterfactual"]
    assert cf["n4d_nodes"] == 2  # 15.89 vCPU default -> 7 pods per node


# ------------------------------------------------------------------ error alerts
def test_classify_errors_auth_and_unreachable():
    errs = ["list nodes: ApiException: (401)\nReason: Unauthorized\nHTTP response headers: ..."]
    a = classify_errors(errs, have_nodes=False)
    assert a[0]["kind"] == "auth" and a[0]["severity"] == "error"
    errs = ["list nodes: MaxRetryError: HTTPSConnectionPool(host='34.37.1.143'): Max retries exceeded (Connection refused)"]
    a = classify_errors(errs, have_nodes=False)
    assert a[0]["kind"] == "unreachable" and a[0]["title"] == "Cluster unreachable"
    assert classify_errors(["controller: no Ready pod app=colo-controller in colo-system"], True)[0]["kind"] == "controller"
    assert classify_errors([], True) == []


def test_build_state_surfaces_alerts():
    st = build_state({"nodes": [], "pods": [], "errors": ["list pods: ApiException: (403) Forbidden"]})
    assert st["alerts"] and st["alerts"][0]["kind"] == "auth"
    assert st["errors"]  # raw list kept for back-compat


def test_backend_errors_logged_rate_limited(caplog, monkeypatch):
    demo_app._err_logged.clear()
    t = [1000.0]
    monkeypatch.setattr(demo_app.time, "time", lambda: t[0])
    with caplog.at_level(logging.WARNING, logger="demo"):
        demo_app._log_backend_errors(["list nodes: (401) Unauthorized"])
        demo_app._log_backend_errors(["list nodes: (401) Unauthorized"])
        t[0] += 31
        demo_app._log_backend_errors(["list nodes: (401) Unauthorized"])
    assert sum("backend error" in r.message for r in caplog.records) == 2


# ------------------------------------------------------------------ scenarios
def test_render_note_templates():
    assert sc.render_note("threshold {threshold_ms:.0f} ms", {"threshold_ms": 27.4}) == "threshold 27 ms"
    assert sc.render_note("slots {g4_slots_per_node}", {}) == "slots ?"
    assert sc.render_note("bad {x:.0f}", {"x": "str"}) == "bad str"
    for s in sc.SCENARIOS.values():  # no hardcoded threshold numbers left in notes
        for step in s["steps"]:
            assert "27 ms" not in step.get("note", "")


def test_every_scenario_starts_with_precondition():
    for name, s in sc.SCENARIOS.items():
        assert s["steps"][0]["do"] == "require", name
        assert s["steps"][0].get("msg"), name


class FakeBackend:
    mode = "fake"

    def __init__(self, state):
        self._state = state
        self.calls = []

    async def state(self):
        raise AssertionError("runner must use state_fn, not backend.state()")

    async def set_gpu_rate(self, r):
        self.calls.append(("rate", r))

    async def set_demand(self, d):
        self.calls.append(("demand", d))

    async def set_guard(self, e):
        self.calls.append(("guard", e))


def _fake_state(**over):
    st = build_state({"nodes": [_node("g4a", "g4", 46, 176)], "pods": []})
    st["controller"]["reachable"] = True
    st.update(over)
    return st


def test_runner_precondition_fails_fast_and_uses_state_fn():
    st = _fake_state()  # 1 G4 node, 0 vLLM ready: scale-in precondition fails
    b = FakeBackend(st)
    finished = []

    async def state_fn():
        return st

    async def on_finish():
        finished.append(1)

    r = sc.ScenarioRunner(b, poll_s=0.01, state_fn=state_fn, on_finish=on_finish)
    snap = asyncio.run(r.run("gpu-scale-in"))
    assert snap["status"] == "failed"
    assert "Run scenario 1" in snap["error"]
    assert b.calls == []  # nothing changed on the cluster
    assert finished == [1]


def test_runner_wait_exposes_progress_and_on_finish_on_cancel():
    st = _fake_state()
    b = FakeBackend(st)
    finished = []

    async def state_fn():
        return st

    async def on_finish():
        finished.append(1)

    async def go():
        r = sc.ScenarioRunner(b, poll_s=0.01, state_fn=state_fn, on_finish=on_finish)
        r.start("gpu-scale-out")
        for _ in range(200):
            await asyncio.sleep(0.01)
            snap = r.snapshot()
            if snap["wait"] and snap["wait"]["conds"]:
                break
        c = snap["wait"]["conds"][0]
        assert c["metric"] == "vllm_desired" and c["label"] == "vLLM replicas wanted" and c["ok"] is False
        assert 0 < snap["wait"]["remaining_s"] <= 180
        assert r.cancel()
        with pytest.raises(asyncio.CancelledError):
            await r.task
        assert r.status == "cancelled"

    asyncio.run(go())
    assert finished == [1]
    assert ("rate", 1200) in b.calls


# ------------------------------------------------------------------ app
def test_api_state_back_compat_and_new_fields():
    c = TestClient(demo_app.app)
    st = c.get("/api/state").json()
    for k in ("ts", "mode", "nodes", "counts", "vllm", "loadgen", "services", "controller", "events", "cost", "errors", "scenario"):
        assert k in st
    for k in ("per_hour", "reclaimed_vcpu", "reclaimed_per_hour", "prices"):
        assert k in st["cost"]
    assert "story" in st and "alerts" in st and "demo" in st
    assert st["story"]["saved_per_hour"] == st["cost"]["counterfactual"]["saved_per_hour"]


def test_manual_controls_locked_while_scenario_runs_and_cancel_restores_hpa(monkeypatch):
    with TestClient(demo_app.app) as c:
        b = demo_app.backend
        c.post("/api/gpu-rate", json={"rate": 1200})
        assert c.get("/api/state").json()["demo"]["accelerator_active"] is True
        monkeypatch.setattr(demo_app.runner, "time_scale", 50.0)
        r = c.post("/api/scenario/cpu-scale-out")
        # cpu-scale-out requires exactly one G4 node; the mock starts with one
        assert r.status_code == 200
        assert c.post("/api/demand", json={"sandbox": 1}).status_code == 409
        assert c.post("/api/gpu-rate", json={"rate": 100}).status_code == 409
        c.post("/api/scenario/cancel")
        for _ in range(50):
            if not demo_app.runner.running:
                break
            import time as _t
            _t.sleep(0.05)
        assert b.hpa_min == 1 and not b.fast_scale_forced
        assert c.post("/api/demand", json={"sandbox": 6, "toolcall": 6, "analytics": 2}).status_code == 200


def test_static_assets_local():
    c = TestClient(demo_app.app)
    html = c.get("/").text
    assert "cdn." not in html and "https://" not in html
    assert c.get("/static/chart.umd.min.js").status_code == 200
    assert c.get("/static/fonts/plex-sans-400.woff2").status_code == 200


# ------------------------------------------------------------------ live backend I/O (no cluster)
def _bare_live():
    lb = object.__new__(cluster.LiveBackend)
    lb._events_cache = None
    lb._pod_ips = {}
    lb.fast_scale = True
    lb.fast_scale_forced = False
    return lb


def test_post_config_sends_patch_only():
    lb = _bare_live()
    sent = []

    async def ctrl_pod():
        return "colo-controller-x"

    async def proxy(ns, pod, port, path, method="GET", body=None):
        sent.append((method, path, body))
        return {"ok": True}

    lb._controller_pod = ctrl_pod
    lb.proxy = proxy
    asyncio.run(lb.set_guard(True))
    assert sent == [("POST", "config", {"guard": {"enabled": True}})]


def test_fast_scale_opt_out_and_restore():
    lb = _bare_live()
    calls = []
    hpa = types.SimpleNamespace(spec=types.SimpleNamespace(min_replicas=2))
    lb.autoscaling = types.SimpleNamespace(
        patch_namespaced_horizontal_pod_autoscaler=lambda n, ns, body: calls.append(("hpa", body)),
        read_namespaced_horizontal_pod_autoscaler=lambda n, ns: hpa,
    )
    lb.apps = types.SimpleNamespace(patch_namespaced_deployment_scale=lambda n, ns, body: calls.append(("scale", body)))
    lb._fast_scale_vllm(1200)
    assert ("hpa", {"spec": {"minReplicas": 2}}) in calls and lb.fast_scale_forced
    calls.clear()
    assert asyncio.run(lb.restore_hpa_min()) is True
    assert calls == [("hpa", {"spec": {"minReplicas": 1}})] and not lb.fast_scale_forced
    calls.clear()
    lb.fast_scale = False
    lb._fast_scale_vllm(1200)
    assert calls == []


def test_events_scoped_and_cached():
    lb = _bare_live()
    seen = []

    def list_events(**kw):
        seen.append(kw)
        return types.SimpleNamespace(items=[])

    lb.core = types.SimpleNamespace(list_event_for_all_namespaces=list_events)
    lb._events()
    lb._events()
    assert len(seen) == 1  # second call within TTL is served from cache
    assert seen[0]["limit"] == cluster.EVENTS_LIMIT <= 200
    assert "reason!=FailedScheduling" in seen[0]["field_selector"]


def _live_like(n4d_nodes: int, overflow: int, buffer_on_first: bool = False):
    nodes = [_node("g4a", "g4", 46, 176), _node("g4b", "g4", 46, 176)]
    pods = []
    for i in range(n4d_nodes):
        nodes.append(_node(f"n4d{i}", "n4d", 15.89, 57))
        pods.append(_pod(f"ds-{i}", f"n4d{i}", 0.56, 1, ns="kube-system", tier=None, cls=None, app="ds"))
    for i in range(36):
        pods.append(_pod(f"bf-{i}", "g4a" if i < 18 else "g4b", 2, 3))
    for i in range(overflow):
        pods.append(_pod(f"of-{i}", "n4d0", 2, 3, tier="overflow"))
    if buffer_on_first:
        pods.append(_pod("colo-buffer-x", "n4d1", 2, 1, ns="colo-system", tier=None, cls=None, app="colo-buffer"))
    return build_state({"nodes": nodes, "pods": pods})


def test_counterfactual_counts_existing_n4d_capacity():
    # Review M1 (live shape): 3 N4D nodes (2 empty, 1 with 2 overflow pods) + 36 backfill pods on G4.
    # All-CPU-on-N4D needs pack(38 x 2 vCPU onto 15.33) = 6 nodes; 3 run today -> 3 saved (old code said 6).
    st = _live_like(3, 2)
    cf = st["cost"]["counterfactual"]
    assert cf["n4d_nodes"] == 6 and cf["saved_nodes"] == 3
    assert st["story"]["saved_per_hour"] == pytest.approx(3 * PRICE_N4D16_HR)
    # 2 empty N4D nodes + 36 backfill: 6 needed - 2 running = 4 saved (old code: 6)
    assert _live_like(2, 0)["cost"]["counterfactual"]["saved_nodes"] == 4


def test_counterfactual_ignores_buffer_and_uses_min_overhead():
    st = _live_like(3, 2, buffer_on_first=True)
    cf = st["cost"]["counterfactual"]
    assert cf["node_cpu"] == pytest.approx(15.89 - 0.56)  # buffer's 2 vCPU are not daemonset overhead
    assert cf["saved_nodes"] == 3


def test_buffer_deployment_read_from_colo_system():
    st = build_state({"nodes": [], "pods": [], "deployments": [{"namespace": "colo-system", "name": "colo-buffer", "replicas": 2, "ready": 1}]})
    assert st["buffer"] == {"desired": 2, "ready": 1}
