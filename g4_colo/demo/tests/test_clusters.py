"""Tests for the two-cluster demo: cluster routing, native-recipe state, native actions, DEMO_CLUSTERS parsing,
and per-backend kubernetes ApiClient isolation."""
from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
import os  # noqa: E402

os.environ["DEMO_MOCK"] = "1"

from fastapi.testclient import TestClient  # noqa: E402

import app as demo_app  # noqa: E402
import backend as be  # noqa: E402
import cluster  # noqa: E402
import scenarios as sc  # noqa: E402
from state import build_state, summarize  # noqa: E402

CTX_0726 = "gke_northam-ce-mlai-tpu_us-west8_g4cluster-jawadamin-us-west8-0726"
CTX_1126 = "gke_northam-ce-mlai-tpu_us-west8_g4cluster-jawadamin-us-west8-1126"


def _node(name, role, cpu=46, mem=176, labels=None):
    lb = {"colo.role": role, **(labels or {})}
    return {"name": name, "labels": lb, "allocatable": {"cpu": str(cpu), "memory": f"{mem}Gi"}, "ready": True,
            "created": "2026-11-01T00:00:00Z"}


def _pod(name, node, app, tier=None, cls=None, ns="colo-svc", phase="Running", ready=True, cpu=2, mem=4):
    labels = {"app": app}
    if tier:
        labels["colo.tier"] = tier
    if cls:
        labels["colo.class"] = cls
    return {"name": name, "namespace": ns, "uid": name, "labels": labels, "node": node, "phase": phase, "ready": ready,
            "cpu_req": cpu, "mem_req_gib": mem, "priority_class": None}


def _dep(name, replicas, ready, ns="colo-svc"):
    return {"namespace": ns, "name": name, "replicas": replicas, "ready": ready}


# ------------------------------------------------------------------ native state building
def _native_raw():
    nodes = [_node("g4a", "g4"), _node("g4b", "g4"), _node("n4d0", "n4d", 15.89, 57)]
    pods = [
        _pod("vllm-a", "g4a", "vllm", ns="inference", cpu=8, mem=64),  # Ready vLLM -> g4a eligible
        _pod("vllm-b", "g4b", "vllm", ns="inference", ready=False, cpu=8, mem=64),  # starting -> g4b not eligible
    ]
    for i in range(3):
        pods.append(_pod(f"sandbox-backfill-{i}", "g4a", "sandbox", "backfill", "integer"))
    for i in range(2):  # by-design Pending: no slot next to vLLM
        pods.append(_pod(f"sandbox-backfill-p{i}", None, "sandbox", "backfill", "integer", phase="Pending", ready=False))
    pods.append(_pod("sandbox-overflow-0", "n4d0", "sandbox", "overflow", "integer"))
    pods.append(_pod("sandbox-overflow-1", "n4d0", "sandbox", "overflow", "integer"))
    pods.append(_pod("analytics-overflow-0", "n4d0", "analytics", "overflow", "memory-heavy", mem=6))
    deployments = [
        _dep("sandbox-backfill", 5, 3), _dep("sandbox-overflow", 2, 2),
        _dep("toolcall-backfill", 0, 0), _dep("toolcall-overflow", 1, 0),
        _dep("analytics-overflow", 4, 1),
        _dep("colo-buffer", 1, 1, ns="colo-system"), _dep("vllm", 2, 1, ns="inference"),
    ]
    hpas = [
        {"name": "vllm", "namespace": "inference", "target": "vllm", "min": 1, "max": 2, "current": 2, "desired": 2, "metric": "120"},
        {"name": "sandbox-overflow", "namespace": "colo-svc", "target": "sandbox-overflow", "min": 1, "max": 60,
         "current": 2, "desired": 2, "metric": "2"},
        {"name": "toolcall-overflow", "namespace": "colo-svc", "target": "toolcall-overflow", "min": 1, "max": 60,
         "current": 1, "desired": 1, "metric": "0"},
    ]
    return {"nodes": nodes, "pods": pods, "deployments": deployments, "hpas": hpas,
            "controller": {"state": None, "config": None}, "configmap": None, "errors": []}


def test_native_state_eligibility_pending_and_demand():
    st = build_state(_native_raw(), recipe="native", cluster={"id": "native", "name": "x", "region": "r", "project": "p"})
    assert st["recipe"] == "native" and st["control"]["kind"] == "native"
    assert st["control"]["label"] == "HPA + scheduler" and st["control"]["guard_available"] is False
    g4 = {n["name"]: n for n in st["nodes"] if n["role"] == "g4"}
    assert g4["g4a"]["eligible"] is True and g4["g4b"]["eligible"] is False  # eligible iff a Ready vLLM pod
    assert st["control"]["eligible_g4"] == 1
    sb = st["services"]["sandbox"]
    assert sb["backfill"]["waiting"] == 2 and sb["backfill"]["on_g4"] == 3 and sb["backfill"]["effective"] == 3
    assert sb["overflow"]["hpa"]["metric"] == 2.0 and sb["overflow"]["hpa"]["desired"] == 2
    # demand: <svc>-backfill replicas for integer services, analytics-overflow replicas for analytics
    assert st["controller"]["demand"] == {"sandbox": 5, "toolcall": 0, "analytics": 4}
    assert st["services"]["analytics"]["demand"] == 4
    assert st["services"]["analytics"]["backfill"] == {"desired": 0, "ready": 0, "waiting": 0, "on_g4": 0, "effective": 0}
    # no controller -> no controller alert, guard fields empty
    assert st["controller"]["reachable"] is False and st["controller"]["threshold_ms"] is None
    assert not any(a["kind"] == "controller" for a in st["alerts"])
    m = summarize(st)
    assert m["backfill_waiting"] == 2 and m["controller_reachable"] is False
    assert st["cluster"]["id"] == "native"


def test_controller_state_unchanged_shape():
    raw = _native_raw()
    raw["controller"] = {"state": {"demand": {"sandbox": 7}, "guard": {"enabled": True}}, "config": None}
    st = build_state(raw)
    assert st["recipe"] == "controller" and st["control"]["label"] == "colo-controller"
    assert st["controller"]["demand"] == {"sandbox": 7}
    assert "waiting" not in st["services"]["sandbox"]["backfill"]
    assert summarize(st)["backfill_waiting"] == 0


# ------------------------------------------------------------------ native live actions (no cluster)
def _bare_native():
    lb = object.__new__(cluster.NativeLiveBackend)
    lb._events_cache = None
    lb._pod_ips = {}
    lb.fast_scale = True
    lb.fast_scale_forced = False
    return lb


def test_native_set_demand_patches_deployment_scale():
    lb = _bare_native()
    calls = []
    lb.apps = types.SimpleNamespace(patch_namespaced_deployment_scale=lambda n, ns, body: calls.append((n, ns, body)))
    asyncio.run(lb.set_demand({"sandbox": 4, "toolcall": 2, "analytics": 3}))
    assert sorted(calls) == sorted([
        ("sandbox-backfill", "colo-svc", {"spec": {"replicas": 4}}),
        ("toolcall-backfill", "colo-svc", {"spec": {"replicas": 2}}),
        ("analytics-overflow", "colo-svc", {"spec": {"replicas": 3}}),
    ])


def test_native_guard_and_baseline_unsupported():
    lb = _bare_native()
    with pytest.raises(cluster.UnsupportedOnRecipe):
        asyncio.run(lb.set_guard(True))
    with pytest.raises(cluster.UnsupportedOnRecipe):
        asyncio.run(lb.baseline())


def test_native_raw_skips_controller_and_reads_colo_svc_hpas():
    lb = _bare_native()
    assert lb.has_controller is False and "colo-svc" in lb.hpa_namespaces
    assert cluster.LiveBackend.hpa_namespaces == ("inference",)  # controller cluster: unchanged RBAC needs
    lb._nodes = lambda: []
    lb._pods = lambda: []
    lb._deployments = lambda: []
    lb._hpas = lambda: []
    lb._events = lambda: []
    lb._ensure_adc = lambda: None
    lb._configmap = lambda: pytest.fail("native must not read colo-config")
    raw = asyncio.run(lb.raw())
    assert raw["controller"] == {"state": None, "config": None}
    assert not any("controller" in e for e in raw["errors"])


# ------------------------------------------------------------------ app routing
def test_clusters_endpoint_and_routing():
    c = TestClient(demo_app.app)
    j = c.get("/api/clusters").json()
    assert j["default"] == "controller"
    assert [x["id"] for x in j["clusters"]] == ["controller", "native"]
    assert all("endpoint" not in x and "ca_cert" not in x for x in j["clusters"])
    a = c.get("/api/state").json()
    b = c.get("/api/state?cluster=native").json()
    assert a["cluster_id"] == "controller" and a["recipe"] == "controller"
    assert b["cluster_id"] == "native" and b["recipe"] == "native"
    assert b["demo"]["scenarios_unavailable"].keys() == {"guard-integer-vs-memory"}
    assert c.get("/api/state?cluster=nope").status_code == 404
    assert set(c.get("/api/scenarios?cluster=native").json()) == {"gpu-scale-out", "gpu-scale-in", "cpu-scale-out"}


def test_native_demand_routes_to_native_backend_only():
    c = TestClient(demo_app.app)
    nat = demo_app.CTXS["native"].backend
    ctl = demo_app.CTXS["controller"].backend
    before = dict(ctl.demand)
    r = c.post("/api/demand?cluster=native", json={"sandbox": 3, "toolcall": 2, "analytics": 1})
    assert r.status_code == 200
    assert nat.demand == {"sandbox": 3, "toolcall": 2, "analytics": 1}
    assert ctl.demand == before
    scaled = {x["deployment"]: x["replicas"] for x in r.json()["result"]["scaled"]}
    assert scaled == {"colo-svc/sandbox-backfill": 3, "colo-svc/toolcall-backfill": 2, "colo-svc/analytics-overflow": 1}


def test_guard_baseline_and_scenario4_409_on_native():
    c = TestClient(demo_app.app)
    r = c.post("/api/guard?cluster=native", json={"enabled": True})
    assert r.status_code == 409 and "admission policy" in r.json()["detail"]
    assert c.post("/api/baseline?cluster=native").status_code == 409
    r = c.post("/api/scenario/4?cluster=native")
    assert r.status_code == 409 and "admission policy" in r.json()["detail"]
    assert c.post("/api/scenario/nope?cluster=native").status_code == 404
    assert c.post("/api/guard?cluster=controller", json={"enabled": True}).status_code == 200


def test_scenario_on_one_cluster_does_not_block_the_other(monkeypatch):
    with TestClient(demo_app.app) as c:
        nat = demo_app.CTXS["native"]
        monkeypatch.setattr(nat.runner, "time_scale", 50.0)
        r = c.post("/api/scenario/cpu-scale-out?cluster=native")
        assert r.status_code == 200, r.text
        assert c.post("/api/demand?cluster=native", json={"sandbox": 1}).status_code == 409
        assert c.post("/api/demand?cluster=controller", json={"sandbox": 6, "toolcall": 6, "analytics": 2}).status_code == 200
        assert c.get("/api/scenario/status").json()["status"] == "idle"
        assert c.get("/api/scenario/status?cluster=native").json()["name"] == "cpu-scale-out"
        c.post("/api/scenario/cancel?cluster=native")
        for _ in range(50):
            if not nat.runner.running:
                break
            import time as _t
            _t.sleep(0.05)
        assert not nat.runner.running


def test_native_scenarios_are_controller_free():
    assert set(sc.NATIVE_SCENARIOS) == {"gpu-scale-out", "gpu-scale-in", "cpu-scale-out"}
    for name, s in sc.NATIVE_SCENARIOS.items():
        assert s["steps"][0]["do"] == "require" and s["steps"][0].get("msg"), name
        for step in s["steps"]:
            assert step["do"] not in ("guard", "baseline"), name
            for cond in step.get("all", []) + step.get("any", []):
                assert cond[0] != "controller_reachable", name
                assert cond[0] in sc.METRIC_LABELS or cond[0] in summarize(build_state({"nodes": [], "pods": []})), cond
    assert sc.scenarios_for("native") is sc.NATIVE_SCENARIOS and sc.scenarios_for("controller") is sc.SCENARIOS


# ------------------------------------------------------------------ native mock semantics
def test_native_mock_pending_backfill_covered_by_overflow(monkeypatch):
    import mock

    monkeypatch.setattr(mock, "NATIVE_METRIC_LAG_S", 0.0)
    monkeypatch.setattr(mock, "NATIVE_HPA_DOWN_WINDOW_S", 0.0)
    b = mock.NativeMockBackend(cluster_info={"id": "native", "name": "n", "region": "r", "project": "p"})
    asyncio.run(b.set_demand({"sandbox": 15, "toolcall": 15, "analytics": 2}))
    st = asyncio.run(b.state())
    svc = st["services"]
    waiting = svc["sandbox"]["backfill"]["waiting"] + svc["toolcall"]["backfill"]["waiting"]
    assert waiting == 30 - mock.G4_SLOTS  # one G4 node: 19 slots, the rest Pending by design
    assert svc["sandbox"]["backfill"]["desired"] == 15  # backfill replicas = demand
    assert svc["analytics"]["backfill"]["desired"] == 0 and svc["analytics"]["overflow"]["desired"] == 2
    for p in st["nodes"]:
        if p["role"] == "g4":
            assert not any(x["app"] == "analytics" for x in p["pods"])  # memory-heavy never on G4
    # once the bound backfill pods are Ready, the overflow HPA covers exactly the waiting ones (min 1)
    for p in b.svc_pods:
        if p["ready_at"]:
            p["ready_at"] = 0
    b._step(b.t + 1, force=True)
    st = asyncio.run(b.state())
    for name in ("sandbox", "toolcall"):
        s = st["services"][name]
        assert s["overflow"]["desired"] == max(1, s["backfill"]["waiting"]), (name, s)
    assert st["controller"]["reachable"] is False and not st["alerts"]


# ------------------------------------------------------------------ DEMO_CLUSTERS parsing
def test_parse_demo_clusters():
    env = {"DEMO_CLUSTERS": json.dumps([
        {"id": "controller", "label": "With colo-controller", "recipe": "controller", "context": CTX_0726},
        {"id": "native", "label": "Native K8s only", "recipe": "native", "context": CTX_1126},
    ])}
    specs = be.parse_clusters(env)
    assert [s["id"] for s in specs] == ["controller", "native"]
    assert specs[1]["recipe"] == "native" and specs[1]["context"] == CTX_1126
    assert specs[1]["name"] == "g4cluster-jawadamin-us-west8-1126"  # derived from the context
    assert not specs[0].get("legacy")


def test_parse_demo_clusters_cloud_run_entry_and_errors():
    env = {"DEMO_CLUSTERS": json.dumps([{"id": "native", "recipe": "native", "endpoint": "1.2.3.4", "ca_cert": "Zm9v"}])}
    s = be.parse_clusters(env)[0]
    assert s["endpoint"] == "1.2.3.4" and s["ca_cert"] == "Zm9v" and s["label"] == "Native K8s only"
    bad = [
        "not json", "[]", json.dumps([{"id": "a", "recipe": "weird"}]),
        json.dumps([{"id": "a"}, {"id": "a"}]),
        json.dumps([{"id": "a", "context": "c", "endpoint": "e", "ca_cert": "x"}]),
        json.dumps([{"id": "a", "endpoint": "e"}]),  # endpoint without CA
        json.dumps([{"id": "Bad Id"}]),
    ]
    for raw in bad:
        with pytest.raises(ValueError):
            be.parse_clusters({"DEMO_CLUSTERS": raw})


def test_single_cluster_fallback_when_unset():
    specs = be.parse_clusters({"DEMO_KUBE_CONTEXT": CTX_0726})
    assert len(specs) == 1 and specs[0]["id"] == "controller" and specs[0]["recipe"] == "controller"
    assert specs[0]["legacy"] is True and specs[0]["context"] == CTX_0726
    specs = be.parse_clusters({"GKE_ENDPOINT": "1.2.3.4", "GKE_CA_CERT": "Zm9v"})
    assert len(specs) == 1 and specs[0]["legacy"] and specs[0]["endpoint"] is None  # env ADC chain, as before
    mock_specs = be.parse_clusters({"DEMO_MOCK": "1"})
    assert [s["recipe"] for s in mock_specs] == ["controller", "native"]


def test_unavailable_cluster_does_not_break_the_others(monkeypatch):
    def boom(spec, mock=None):
        if spec["id"] == "native":
            raise RuntimeError("context not found")
        return types.SimpleNamespace(recipe="controller")

    monkeypatch.setattr(be, "_backends", None)
    monkeypatch.setattr(be, "_specs", be.parse_clusters({"DEMO_MOCK": "1"}))
    monkeypatch.setattr(be, "make_backend", boom)
    out = be.get_backends()
    assert isinstance(out["native"], be.UnavailableBackend)
    st = asyncio.run(out["native"].state())
    assert st["recipe"] == "native" and st["alerts"] and "context not found" in st["errors"][0]


# ------------------------------------------------------------------ per-backend ApiClient isolation
def test_kube_contexts_get_separate_api_clients(monkeypatch):
    from kubernetes import client, config

    made = []

    def fake_new_client(config_file=None, context=None, persist_config=True):
        cfg = client.Configuration()
        cfg.host = f"https://{context}"
        api = client.ApiClient(cfg)
        made.append(context)
        return api

    def forbid(*a, **k):
        raise AssertionError("process-global kube config must not be touched")

    monkeypatch.setattr(config, "new_client_from_config", fake_new_client)
    monkeypatch.setattr(config, "load_kube_config", forbid)
    monkeypatch.setattr(client.Configuration, "set_default", classmethod(lambda cls, c: forbid()))
    a = cluster.LiveBackend(context=CTX_0726, strict=True, cluster_id="controller")
    b = cluster.NativeLiveBackend(context=CTX_1126, strict=True, cluster_id="native")
    assert made == [CTX_0726, CTX_1126]
    assert a.api_client is not b.api_client
    assert a.core.api_client is a.api_client and a.apps.api_client is a.api_client and a.autoscaling.api_client is a.api_client
    assert b.core.api_client.configuration.host == f"https://{CTX_1126}"
    assert a.core.api_client.configuration.host == f"https://{CTX_0726}"


def test_strict_context_never_falls_back(monkeypatch):
    from kubernetes import config

    def missing(config_file=None, context=None, persist_config=True):
        raise config.ConfigException(f"context {context} not found")

    monkeypatch.setattr(config, "new_client_from_config", missing)
    monkeypatch.setattr(config, "load_incluster_config", lambda **k: pytest.fail("strict must not try in-cluster"))
    with pytest.raises(config.ConfigException):
        cluster.NativeLiveBackend(context=CTX_1126, strict=True, cluster_id="native")


def test_adc_backends_isolated_with_token_refresh_hook(monkeypatch, tmp_path):
    from kubernetes import client
    import google.auth

    scopes_seen = []

    class Creds:
        def __init__(self, tok):
            self.tok, self.valid, self.token, self.refreshes = tok, False, None, 0

        def refresh(self, _req):
            self.refreshes += 1
            self.valid, self.token = True, f"{self.tok}-{self.refreshes}"

    toks = iter(["tokA", "tokB"])

    def fake_default(scopes=None):
        scopes_seen.append(scopes)
        return Creds(next(toks)), "proj"

    monkeypatch.setattr(google.auth, "default", fake_default)
    monkeypatch.setattr(client.Configuration, "set_default", classmethod(lambda cls, c: pytest.fail("no set_default")))
    a = cluster.LiveBackend(endpoint="10.0.0.1", ca_cert="Y2Ex", cluster_id="controller")
    b = cluster.NativeLiveBackend(endpoint="https://10.0.0.2", ca_cert="Y2Ey", cluster_id="native")
    ca, cb = a.api_client.configuration, b.api_client.configuration
    assert ca is not cb and ca.host == "https://10.0.0.1" and cb.host == "https://10.0.0.2"
    assert ca.ssl_ca_cert != cb.ssl_ca_cert  # one CA file per cluster
    assert all("https://www.googleapis.com/auth/userinfo.email" in s for s in scopes_seen)  # wave-2 RBAC fix kept
    # the hook refreshes per request on the Configuration the ApiClient really uses
    a._adc_creds.valid = False
    assert ca.get_api_key_with_prefix("authorization") == "Bearer tokA-2"
    assert cb.get_api_key_with_prefix("authorization").startswith("Bearer tokB")


def test_shutdown_restores_hpa_only_if_this_process_forced_it(monkeypatch):
    """A revision swap or a failed start must not reset an HPA minReplicas another instance set."""
    calls = []
    for ctx in demo_app.CTXS.values():
        async def _restore(c=ctx):
            calls.append(c.id)
        monkeypatch.setattr(ctx, "restore_accelerator", _restore)
        ctx.backend.fast_scale_forced = False
    with TestClient(demo_app.app):
        pass
    assert calls == []
    first = next(iter(demo_app.CTXS.values()))
    first.backend.fast_scale_forced = True
    with TestClient(demo_app.app):
        pass
    assert calls == [first.id]
    first.backend.fast_scale_forced = False


def test_customer_daemonsets_get_their_own_cpu_budget():
    """Customer DaemonSet pods (outside GKE/recipe namespaces) are split out of sys_cpu as agent_cpu for the UI."""
    raw = _native_raw()
    agent = _pod("sample-node-agent-x", "g4a", "sample-node-agent", ns="colo-agents", cpu=0.2, mem=0.125)
    agent["owner_kind"] = "DaemonSet"
    gke = _pod("fluentbit-gke-x", "g4a", "fluentbit-gke", ns="kube-system", cpu=0.1, mem=0.1)
    gke["owner_kind"] = "DaemonSet"
    ours = _pod("colo-node-agent-x", "g4a", "colo-node-agent", ns="colo-system", cpu=0.05, mem=0.06)
    ours["owner_kind"] = "DaemonSet"
    raw["pods"] += [agent, gke, ours]
    st = build_state(raw, recipe="native", cluster={"id": "native", "name": "x", "region": "r", "project": "p"})
    g4a = next(n for n in st["nodes"] if n["name"] == "g4a")
    assert g4a["agent_cpu"] == 0.2
    assert [a["ns"] for a in g4a["agents"]] == ["colo-agents"]
    assert g4a["sys_cpu"] >= 0.35  # agents still count toward the shared-pool budget


def test_customer_daemonset_ready_flag_reaches_ui_state():
    raw = _native_raw()
    agent = _pod("sample-node-agent-y", "g4a", "sample-node-agent", ns="colo-agents", cpu=0.2, mem=0.125, ready=False)
    agent["owner_kind"] = "DaemonSet"
    raw["pods"].append(agent)
    st = build_state(raw, recipe="native", cluster={"id": "native", "name": "x", "region": "r", "project": "p"})
    g4a = next(n for n in st["nodes"] if n["name"] == "g4a")
    assert g4a["agents"] == [{"ns": "colo-agents", "name": "sample-node-agent-y", "cpu": 0.2, "ready": False}]
