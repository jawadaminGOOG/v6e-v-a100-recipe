"""Tests for the Agent Substrate tab (recipe "substrate"): WorkerPool / actor-stats parsing, Worker pods on the node map,
the empty state while Substrate is not installed, the live backend's error handling, mock mode and the API."""
from __future__ import annotations

import asyncio
import json
import re
import os
import sys
import time
import types
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
os.environ["DEMO_MOCK"] = "1"

from fastapi.testclient import TestClient  # noqa: E402

import app as demo_app  # noqa: E402
import backend as be  # noqa: E402
import cluster  # noqa: E402
import mock  # noqa: E402
from state import build_state, parse_actor_stats, parse_workerpool  # noqa: E402

CTX_0726 = "gke_northam-ce-mlai-tpu_us-west8_g4cluster-jawadamin-us-west8-0726"
ENTRIES = [
    {"id": "controller", "label": "With colo-controller", "recipe": "controller", "context": CTX_0726},
    {"id": "substrate", "label": "G4 · Agent Substrate", "recipe": "substrate", "context": CTX_0726},
]


def _node(name, role, cpu=46, mem=176):
    return {"name": name, "labels": {"colo.role": role}, "allocatable": {"cpu": str(cpu), "memory": f"{mem}Gi"},
            "ready": True, "created": "2026-11-01T00:00:00Z"}


def _pod(name, node, labels, ns, phase="Running", ready=True, cpu=2, mem=4, prio=None):
    return {"name": name, "namespace": ns, "uid": name, "labels": labels, "node": node, "phase": phase, "ready": ready,
            "cpu_req": cpu, "mem_req_gib": mem, "priority_class": prio}


def _worker(name, node, pool, phase="Running", ready=True):
    return _pod(name, node, {"ate.dev/worker-pool": pool, "app": "ate-worker"}, "substrate-exp", phase, ready, cpu=6,
                mem=12, prio="colo-backfill" if pool == "g4-workers" else None)


def _wp(name, spec, replicas=None, ready=None, status=True):
    o = {"apiVersion": "ate.dev/v1", "kind": "WorkerPool", "metadata": {"name": name, "namespace": "substrate-exp"},
         "spec": {"replicas": spec, "workerImage": "img"}}
    if status:
        o["status"] = {"replicas": replicas}
        if ready is not None:
            o["status"]["readyReplicas"] = ready
    return o


STATS = {"updated": "2026-11-11T15:00:00Z", "actors_total": 40, "actors_running": 30,
         "actors_by_node": {"g4a": 12, "n4d0": 18, "gone-node": 1}, "resumes_per_s": 22.5, "resume_p50_ms": 14,
         "resume_p99_ms": 50.5, "resume_p99_ms_by_pool": {"g4-workers": 41.0, "n4d-workers": 57.25}, "lost_actors": 2}


def _raw(substrate=None, extra_pods=()):
    nodes = [_node("g4a", "g4"), _node("n4d0", "n4d", 15.89, 57)]
    pods = [
        _pod("vllm-a", "g4a", {"app": "vllm"}, "inference", cpu=8, mem=64),
        _pod("sandbox-backfill-0", "g4a", {"app": "sandbox", "colo.tier": "backfill", "colo.class": "integer"}, "colo-svc"),
        _worker("g4-workers-0", "g4a", "g4-workers"),
        _worker("g4-workers-1", None, "g4-workers", phase="Pending", ready=False),
        _worker("n4d-workers-0", "n4d0", "n4d-workers"),
        {**_pod("atelet-x", "g4a", {"app": "atelet"}, "ate-system", cpu=0.1, mem=0.1), "owner_kind": "DaemonSet"},
        *extra_pods,
    ]
    agents = {"g4a": {"ncpu": 48, "busy": [10] * 48, "cpu_manager_state": {"entries": {
        "vllm-a": {"main": "1-4,25-28"}, "sandbox-backfill-0": {"main": "5,29"}, "g4-workers-0": {"main": "6-8,30-32"}}}}}
    raw = {"nodes": nodes, "pods": pods, "deployments": [], "hpas": [], "agents": agents,
           "controller": {"state": None, "config": None}, "errors": []}
    if substrate is not None:
        raw["substrate"] = substrate
    return raw


def _sub_ok(**over):
    d = {"workerpools": [_wp("g4-workers", 2, 2, 1), _wp("n4d-workers", 1, 1, 1)],
         "hpas": [{"name": "n4d-workers", "namespace": "substrate-exp", "target": "n4d-workers", "min": 1, "max": 10,
                   "current": 1, "desired": 2, "metric": "1"}],
         "actor_stats": json.dumps(STATS), "notes": []}
    d.update(over)
    return d


# ------------------------------------------------------------------ WorkerPool parsing
def test_parse_workerpool_full_and_partial():
    assert parse_workerpool(_wp("g4-workers", 3, 3, 2)) == {"name": "g4-workers", "namespace": "substrate-exp",
                                                          "desired": 3, "replicas": 3, "ready": 2}
    # readyReplicas is omitempty: a status without it means 0 ready.
    assert parse_workerpool(_wp("p", 2, 2))["ready"] == 0
    # No status yet (just created): unknown, not 0.
    wp = parse_workerpool(_wp("p", 2, status=False))
    assert wp["desired"] == 2 and wp["replicas"] is None and wp["ready"] is None
    assert parse_workerpool({"metadata": {"name": "p"}, "spec": {"replicas": "x"}, "status": "bad"})["desired"] is None


@pytest.mark.parametrize("bad", [None, "str", 3, [], {}, {"metadata": {}}, {"metadata": "x"}])
def test_parse_workerpool_rejects_garbage(bad):
    assert parse_workerpool(bad) is None


# ------------------------------------------------------------------ actor-stats parsing
def test_parse_actor_stats_valid_str_and_dict():
    for src in (json.dumps(STATS), STATS, json.dumps(STATS).encode()):
        st, problem = parse_actor_stats(src)
        assert problem is None
        assert st["actors_running"] == 30 and st["lost_actors"] == 2 and st["resumes_per_s"] == 22.5
        assert st["resume_p99_ms_by_pool"] == {"g4-workers": 41.0, "n4d-workers": 57.25}
        assert st["actors_by_node"]["n4d0"] == 18


@pytest.mark.parametrize("missing", [None, ""])
def test_parse_actor_stats_missing(missing):
    assert parse_actor_stats(missing) == (None, None)


@pytest.mark.parametrize("bad, why", [("{not json", "not valid JSON"), ("[1, 2]", "not a JSON object"),
                                      ("42", "not a JSON object"), ('"x"', "not a JSON object")])
def test_parse_actor_stats_malformed(bad, why):
    st, problem = parse_actor_stats(bad)
    assert st is None and why in problem


def test_parse_actor_stats_tolerates_absent_and_wrong_typed_fields():
    st, problem = parse_actor_stats(json.dumps({"actors_running": "7", "resumes_per_s": "fast", "lost_actors": True,
                                                "actors_by_node": ["x"], "resume_p99_ms_by_pool": {"g4-workers": None},
                                                "resume_p99_ms": "NaN", "updated": 5}))
    assert problem is None
    assert st["actors_running"] == 7
    assert st["resumes_per_s"] is None and st["lost_actors"] is None and st["resume_p99_ms"] is None
    assert st["actors_by_node"] is None and st["resume_p99_ms_by_pool"] == {} and st["updated"] is None
    assert st["actors_total"] is None  # absent


# ------------------------------------------------------------------ state building
def test_substrate_state_pools_hpa_actors_and_worker_tiles():
    st = build_state(_raw(_sub_ok()), recipe="substrate")
    sub = st["substrate"]
    assert sub["installed"] and sub["has_data"] and sub["notes"] == []
    g4, n4 = sub["pools"]["g4-workers"], sub["pools"]["n4d-workers"]
    assert (g4["desired"], g4["replicas"], g4["ready"], g4["pending"], g4["on_g4"]) == (2, 2, 1, 1, 1)
    assert (n4["desired"], n4["ready"], n4["on_n4d"]) == (1, 1, 1)
    assert sub["g4_pending"] == 1 and sub["workers_on_g4"] == 1 and sub["workers_on_n4d"] == 1
    assert sub["n4d_hpa"]["desired"] == 2 and sub["n4d_hpa"]["metric"] == 1.0
    a = sub["actors"]
    assert (a["actors_g4"], a["actors_n4d"], a["actors_other"]) == (12, 18, 1)
    assert a["resume_p99_g4_ms"] == 41.0 and a["resume_p99_n4d_ms"] == 57.25 and a["lost_actors"] == 2
    g4n = next(n for n in st["nodes"] if n["name"] == "g4a")
    cats = [c["cat"] for c in g4n["cpus"]]
    assert cats.count("worker") == 6 and cats.count("vllm") == 8 and cats.count("integer") == 2
    assert any(p["cat"] == "worker" and p["ns"] == "substrate-exp" for p in g4n["pods"])
    n4d = next(n for n in st["nodes"] if n["name"] == "n4d0")
    assert [p["cat"] for p in n4d["pods"]] == ["worker"]
    # Workers are not counted as system overhead; the G4 one is reclaimed capacity next to the backfill pod.
    assert g4n["sys_cpu"] == pytest.approx(0.1)
    assert st["story"]["reclaimed_vcpu"] == pytest.approx(2 + 6)
    assert st["control"]["label"] == "colo-controller + Agent Substrate" and st["control"]["guard_available"]
    assert st["alerts"] == []


def test_substrate_not_installed_is_an_empty_state_not_an_alert():
    notes = ["WorkerPools (workerpools.ate.dev/v1 in substrate-exp): not found (not installed yet)"]
    for sub in ({"workerpools": None, "hpas": None, "actor_stats": None, "notes": notes}, None, {}):
        raw = _raw(sub)
        raw["pods"] = [p for p in raw["pods"] if not p["labels"].get("ate.dev/worker-pool")]
        st = build_state(raw, recipe="substrate")
        s = st["substrate"]
        assert s["installed"] is False and s["has_data"] is False
        assert s["pools"] == {} and s["n4d_hpa"] is None and s["actors"] is None and s["g4_pending"] == 0
        assert st["alerts"] == [] and st["errors"] == []
    assert build_state(_raw({"workerpools": None, "notes": notes}), recipe="substrate")["substrate"]["notes"] == notes


def test_substrate_pools_absent_but_configmap_malformed():
    st = build_state(_raw(_sub_ok(workerpools=[], hpas=[], actor_stats="{oops")), recipe="substrate")
    s = st["substrate"]
    assert s["installed"] is True and s["actors"] is None and s["n4d_hpa"] is None
    assert any("not valid JSON" in n for n in s["notes"])
    # Worker pods still count even without their WorkerPool object.
    assert s["pools"]["g4-workers"]["exists"] is False and s["pools"]["g4-workers"]["pending"] == 1
    assert st["alerts"] == []


def test_other_recipes_unchanged_by_worker_pods():
    raw = _raw(_sub_ok())
    st = build_state(raw, recipe="controller")
    assert "substrate" not in st
    g4n = next(n for n in st["nodes"] if n["name"] == "g4a")
    assert "worker" not in {c["cat"] for c in g4n["cpus"]} and "worker" not in {p["cat"] for p in g4n["pods"]}
    assert st["story"]["reclaimed_vcpu"] == pytest.approx(2)
    assert build_state(raw, recipe="native").get("substrate") is None


# ------------------------------------------------------------------ live backend: errors become notes
def _api_exc(status):
    from kubernetes.client.exceptions import ApiException

    return ApiException(status=status, reason="x")


def _bare_live(wp=None, hpa=None, cm=None, apps=None):
    lb = cluster.SubstrateLiveBackend.__new__(cluster.SubstrateLiveBackend)

    def raiser_or(v):
        def f(*a, **k):
            if isinstance(v, Exception):
                raise v
            return v
        return f

    lb.custom = types.SimpleNamespace(list_namespaced_custom_object=raiser_or(wp))
    lb.autoscaling = types.SimpleNamespace(list_namespaced_horizontal_pod_autoscaler=raiser_or(hpa))
    lb.core = types.SimpleNamespace(read_namespaced_config_map=raiser_or(cm))
    empty = types.SimpleNamespace(items=[])
    a = apps or {}
    lb.apps = types.SimpleNamespace(list_namespaced_deployment=raiser_or(a.get("deployments", empty)),
                                    list_namespaced_stateful_set=raiser_or(a.get("statefulsets", empty)),
                                    list_namespaced_daemon_set=raiser_or(a.get("daemonsets", empty)))
    return lb


def test_live_substrate_absent_crd_and_forbidden_are_notes():
    lb = _bare_live(wp=_api_exc(404), hpa=types.SimpleNamespace(items=[]), cm=_api_exc(403))
    sub = asyncio.run(lb._substrate())
    assert sub["workerpools"] is None and sub["hpas"] == [] and sub["actor_stats"] is None
    assert any("not installed yet" in n for n in sub["notes"])
    assert any("no read access (403)" in n for n in sub["notes"])
    st = build_state({"nodes": [], "pods": [], "substrate": sub, "errors": []}, recipe="substrate")
    assert st["substrate"]["has_data"] is False and st["alerts"] == []


def test_live_substrate_reads_objects():
    cm = types.SimpleNamespace(data={"stats.json": json.dumps(STATS)})
    lb = _bare_live(wp={"items": [_wp("g4-workers", 2, 2, 2)]}, hpa=types.SimpleNamespace(items=[]), cm=cm)
    sub = asyncio.run(lb._substrate())
    assert sub["notes"] == [] and sub["workerpools"][0]["metadata"]["name"] == "g4-workers"
    assert json.loads(sub["actor_stats"])["lost_actors"] == 2
    lb = _bare_live(wp={"items": []}, hpa=types.SimpleNamespace(items=[]), cm=types.SimpleNamespace(data=None))
    assert any("no stats.json" in n for n in asyncio.run(lb._substrate())["notes"])


def test_live_substrate_raw_merges_into_controller_raw(monkeypatch):
    lb = _bare_live(wp=_api_exc(404), hpa=types.SimpleNamespace(items=[]), cm=_api_exc(404))

    async def base_raw(self):
        return {"nodes": [], "pods": [], "errors": []}

    monkeypatch.setattr(cluster.LiveBackend, "raw", base_raw)
    raw = asyncio.run(lb.raw())
    assert raw["errors"] == [] and raw["substrate"]["workerpools"] is None


# ------------------------------------------------------------------ DEMO_CLUSTERS + mock + API
def test_parse_clusters_substrate_shares_context_with_controller():
    specs = be.parse_clusters({"DEMO_CLUSTERS": json.dumps(ENTRIES)})
    assert [s["recipe"] for s in specs] == ["controller", "substrate"]
    assert specs[0]["context"] == specs[1]["context"] == CTX_0726
    assert specs[1]["name"] == "g4cluster-jawadamin-us-west8-0726" and specs[1]["label"] == "G4 · Agent Substrate"
    assert be.parse_clusters({"DEMO_CLUSTERS": json.dumps([{"id": "s", "recipe": "substrate"}])})[0]["label"] == "G4 · Agent Substrate"
    assert isinstance(be.make_backend(specs[1], mock=True), mock.SubstrateMockBackend)


def test_mock_substrate_and_absent(monkeypatch):
    st = asyncio.run(mock.SubstrateMockBackend().state())
    s = st["substrate"]
    # Option 2 default: Total Workers 4, all fit next to vLLM (G4 share 4), nothing for N4D.
    assert s["has_data"] and s["pools"]["g4-workers"]["desired"] == 4 and s["n4d_hpa"]["desired"] == 0
    assert s["actors"]["actors_running"] > 0 and s["actors"]["lost_actors"] == 0
    assert any(c["cat"] == "worker" for n in st["nodes"] for c in n["cpus"])
    monkeypatch.setenv("DEMO_MOCK_SUBSTRATE", "absent")
    st = asyncio.run(mock.SubstrateMockBackend().state())
    assert st["substrate"]["has_data"] is False and st["substrate"]["notes"] and st["alerts"] == []
    assert not any(c["cat"] == "worker" for n in st["nodes"] for c in n["cpus"])


@pytest.fixture
def sub_client(monkeypatch):
    spec = be.parse_clusters({"DEMO_CLUSTERS": json.dumps(ENTRIES)})[1]
    monkeypatch.setitem(demo_app.CTXS, "substrate", demo_app.ClusterCtx(spec, be.make_backend(spec, mock=True)))
    with TestClient(demo_app.app) as c:
        yield c


def test_api_substrate_tab(sub_client):
    cl = {c["id"]: c for c in sub_client.get("/api/clusters").json()["clusters"]}
    assert cl["substrate"]["recipe"] == "substrate" and cl["substrate"]["control"] == "colo-controller + Agent Substrate"
    assert cl["controller"]["recipe"] == "controller" and cl["controller"]["control"] == "colo-controller"
    st = sub_client.get("/api/state?cluster=substrate").json()
    assert st["cluster_id"] == "substrate" and st["recipe"] == "substrate" and st["substrate"]["has_data"]
    assert "substrate" not in sub_client.get("/api/state?cluster=controller").json()
    # Scenarios stay on the controller tab (same cluster, one runner).
    assert sub_client.get("/api/scenarios?cluster=substrate").json() == {}
    r = sub_client.post("/api/scenario/1?cluster=substrate")
    assert r.status_code == 409 and "controller tab" in r.json()["detail"]
    assert set(st["demo"]["scenarios_unavailable"]) == {"gpu-scale-out", "gpu-scale-in", "cpu-scale-out",
                                                        "guard-integer-vs-memory"}


# ------------------------------------------------------------------ Substrate control plane
from state import ate_component, build_control_plane  # noqa: E402


def _cp_raw(workloads=None, router_ready=True, with_atelet=True):
    nodes = [_node("g4a", "g4"), _node("n4d0", "n4d", 15.89, 57),
             {**_node("cp-infra", "x", 3.92, 13), "labels": {"ate.dev/workloadType": "ate-system-infra",
                                                              "node.kubernetes.io/instance-type": "n4d-standard-4"}},
             {**_node("cp-pg", "x", 3.92, 13), "labels": {"ate.dev/workloadType": "ate-postgres"}}]
    ns = "ate-system"
    pods = [
        {**_pod("ate-api-server-1-a", "cp-infra", {"app": "ate-api-server"}, ns, cpu=0.5), "restarts": 0},
        {**_pod("ate-controller-1-a", "cp-infra", {"app": "ate-controller"}, ns), "restarts": 2},
        {**_pod("atenet-router-1-a", "cp-infra", {"app": "atenet-router"}, ns, ready=router_ready), "restarts": 9},
        {**_pod("pod-certificate-controller-1-a", "cp-infra", {"app": "pcc"}, ns), "restarts": 0},
        {**_pod("otel-collector-1-a", "cp-infra", {"app": "otel"}, ns), "restarts": 0},
        {**_pod("ate-postgres-0", "cp-pg", {"app": "ate-postgres"}, ns, cpu=1), "restarts": 0},
        {**_pod("ate-idp-server-1-a", None, {"app": "idp"}, ns, phase="Pending", ready=False), "restarts": 0},
        _pod("vllm-a", "g4a", {"app": "vllm"}, "inference", cpu=8, mem=64),
    ]
    if with_atelet:
        pods += [{**_pod("atelet-g4", "g4a", {"app": "atelet"}, ns, cpu=0.1), "owner_kind": "DaemonSet", "restarts": 1},
                 {**_pod("atelet-n4d", "n4d0", {"app": "atelet"}, ns, ready=False, cpu=0.1), "owner_kind": "DaemonSet",
                  "restarts": 4}]
    wl = workloads if workloads is not None else {
        "deployments": [{"name": "ate-api-server", "replicas": 2, "ready": 1}, {"name": "ate-controller", "replicas": 1, "ready": 1},
                        {"name": "atenet-router", "replicas": 1, "ready": 0 if not router_ready else 1},
                        {"name": "atenet-egress", "replicas": 0, "ready": 0},
                        {"name": "pod-certificate-controller", "replicas": 1, "ready": 1}],
        "statefulsets": [{"name": "ate-postgres", "replicas": 1, "ready": 1}],
        "daemonsets": [{"name": "atelet", "desired": 2, "ready": 1, "available": 1}]}
    return {"nodes": nodes, "pods": pods, "errors": [], "substrate": {"ate_workloads": wl}}


def test_ate_component_matching():
    assert ate_component("pod-certificate-controller-6f9d7c8b5-x") == "podcert"  # not the generic controller
    assert ate_component("ate-controller-7f8c6d9b5-x") == "controller"
    assert ate_component("ate-api-server") == "api-server" and ate_component("ate-idp-server-1") == "idp"
    assert ate_component("atenet-router-1") == "router" and ate_component("atenet-egress-1") == "egress"
    assert ate_component("ate-postgres-0") == "postgres" and ate_component("atelet-abcde") == "atelet"
    assert ate_component("otel-collector-1") is None and ate_component(None) is None


def test_control_plane_nodes_tiles_and_health():
    st = build_state(_cp_raw(router_ready=False), recipe="substrate")
    cp = st["substrate"]["control_plane"]
    assert cp["present"] and cp["health"] == "down"
    assert [n["kind"] for n in cp["nodes"]] == ["infra", "postgres"]
    infra = cp["nodes"][0]
    assert infra["instance_type"] == "n4d-standard-4"
    tiles = {p["component"]: p for p in infra["pods"]}
    assert tiles["router"]["ready"] is False and tiles["router"]["restarts"] == 9 and tiles["router"]["letter"] == "C"
    assert tiles[None]["name"].startswith("otel")  # other control-plane pods still get a C tile
    assert cp["nodes"][1]["pods"][0]["letter"] == "P"
    comps = {c["key"]: c for c in cp["components"]}
    assert [c["key"] for c in cp["components"]] == ["api-server", "controller", "router", "egress", "idp", "podcert", "postgres"]
    assert (comps["api-server"]["ready"], comps["api-server"]["desired"], comps["api-server"]["health"]) == (1, 2, "degraded")
    assert comps["router"]["health"] == "down" and comps["router"]["restarts"] == 9
    assert comps["egress"]["health"] == "scaled-to-zero"
    assert comps["postgres"]["source"] == "StatefulSet" and comps["postgres"]["health"] == "ok"
    # No Deployment for idp: falls back to its pods (one Pending).
    assert comps["idp"]["source"] == "pods" and (comps["idp"]["ready"], comps["idp"]["desired"]) == (0, 1)
    assert [p["name"] for p in cp["unscheduled"]] == ["ate-idp-server-1-a"]
    at = cp["atelet"]
    assert (at["source"], at["desired"], at["ready"], at["health"], at["restarts"]) == ("DaemonSet", 2, 1, "degraded", 5)
    # Control-plane nodes are their own role: not N4D capacity, not in the N4D cost.
    assert st["counts"].get("n4d") == 1 and st["counts"].get("ate-cp") == 2
    by = {n["name"]: n for n in st["nodes"]}
    assert by["cp-infra"]["role"] == "ate-cp"
    assert by["g4a"]["atelet"]["ready"] is True and by["n4d0"]["atelet"] == {"pod": "atelet-n4d", "ready": False,
                                                                             "phase": "Running", "restarts": 4}
    assert st["alerts"] == []


def test_control_plane_missing_component_and_pods_only_atelet():
    raw = _cp_raw(workloads={"deployments": None, "statefulsets": None, "daemonsets": None})
    raw["pods"] = [p for p in raw["pods"] if not p["name"].startswith("ate-postgres")]
    cp = build_state(raw, recipe="substrate")["substrate"]["control_plane"]
    comps = {c["key"]: c for c in cp["components"]}
    assert comps["postgres"]["found"] is False and comps["postgres"]["health"] == "missing"
    assert comps["egress"]["health"] == "missing"
    assert cp["atelet"]["source"] == "pods" and (cp["atelet"]["ready"], cp["atelet"]["desired"]) == (1, 2)


def test_control_plane_absent_is_empty_state():
    for sub in (None, {}, {"ate_workloads": {"deployments": [], "statefulsets": [], "daemonsets": []}}):
        raw = {"nodes": [_node("g4a", "g4")], "pods": [], "errors": [], "substrate": sub}
        st = build_state(raw, recipe="substrate")
        cp = st["substrate"]["control_plane"]
        assert cp["present"] is False and cp["nodes"] == [] and cp["health"] == "unknown"
        assert all(c["health"] == "missing" for c in cp["components"]) and cp["atelet"]["found"] is False
        assert st["nodes"][0]["atelet"] is None and st["alerts"] == []
    assert build_control_plane("garbage", [], [])["present"] is False


def test_control_plane_nodes_unchanged_on_other_recipes():
    st = build_state(_cp_raw(), recipe="controller")
    assert "substrate" not in st and st["counts"].get("ate-cp") is None
    assert {n["role"] for n in st["nodes"] if n["name"].startswith("cp-")} == {"other"}
    assert all("atelet" not in n for n in st["nodes"])


def test_live_reads_ate_workloads_and_forbidden_is_a_note():
    D = types.SimpleNamespace
    dep = D(metadata=D(name="ate-api-server"), spec=D(replicas=2), status=D(ready_replicas=None))
    ds = D(metadata=D(name="atelet"), status=D(desired_number_scheduled=3, number_ready=3, number_available=2))
    lb = _bare_live(wp={"items": []}, hpa=D(items=[]), cm=D(data={"stats.json": "{}"}),
                    apps={"deployments": D(items=[dep]), "statefulsets": _api_exc(403), "daemonsets": D(items=[ds])})
    sub = asyncio.run(lb._substrate())
    assert sub["ate_workloads"]["deployments"] == [{"name": "ate-api-server", "replicas": 2, "ready": 0}]
    assert sub["ate_workloads"]["statefulsets"] is None
    assert sub["ate_workloads"]["daemonsets"] == [{"name": "atelet", "desired": 3, "ready": 3, "available": 2}]
    assert any("StatefulSets in ate-system: no read access (403)" in n for n in sub["notes"])


def test_mock_control_plane(monkeypatch):
    st = asyncio.run(mock.SubstrateMockBackend().state())
    cp = st["substrate"]["control_plane"]
    assert cp["present"] and cp["health"] == "ok" and len(cp["nodes"]) == 2
    assert all(c["health"] == "ok" for c in cp["components"]) and cp["atelet"]["health"] == "ok"
    assert all(n["atelet"] and n["atelet"]["ready"] for n in st["nodes"] if n["role"] in ("g4", "n4d"))
    monkeypatch.setenv("DEMO_MOCK_ATE_DEGRADED", "1")
    cp = asyncio.run(mock.SubstrateMockBackend().state())["substrate"]["control_plane"]
    assert cp["health"] == "down" and {c["key"]: c for c in cp["components"]}["router"]["health"] == "down"
    monkeypatch.setenv("DEMO_MOCK_SUBSTRATE", "absent")
    st = asyncio.run(mock.SubstrateMockBackend().state())
    assert st["substrate"]["control_plane"]["present"] is False


def test_podcert_read_from_its_own_namespace():
    raw = _cp_raw()
    raw["substrate"]["ate_workloads"]["podcert_deployments"] = [{"name": "podcertificate-controller", "replicas": 1, "ready": 1}]
    raw["pods"].append({**_pod("podcertificate-controller-5d-x", "cp-infra", {"app": "pcc"}, "podcertificate-controller-system"),
                        "restarts": 3})
    cp = build_state(raw, recipe="substrate")["substrate"]["control_plane"]
    pc = {c["key"]: c for c in cp["components"]}["podcert"]
    assert (pc["source"], pc["workloads"], pc["ready"], pc["desired"], pc["health"]) == (
        "Deployment", ["podcertificate-controller"], 1, 1, "ok")
    assert pc["restarts"] == 3  # from the podcertificate-controller-system pod only
    assert any(p["name"].startswith("podcertificate-controller") for p in cp["nodes"][0]["pods"])
    # Pods-only fallback in that namespace; an ate-system Deployment of a similar name is ignored.
    raw["substrate"]["ate_workloads"]["podcert_deployments"] = None
    pc = {c["key"]: c for c in build_state(raw, recipe="substrate")["substrate"]["control_plane"]["components"]}["podcert"]
    assert (pc["source"], pc["ready"], pc["desired"]) == ("pods", 1, 1)
    assert ate_component("podcertificate-controller") == "podcert"


# ------------------------------------------------------------------ actor load controls
from state import actor_load_patch, parse_actor_load  # noqa: E402

CUR = {"exists": True, "total": 40, "awake": 16, "hold_s": 5.0}


def test_actor_load_patch_body_only_sets_given_keys():
    spec, patch = actor_load_patch(CUR, {"total": 100, "awake": 32})
    assert spec == {"total": 100, "awake": 32, "hold_s": 5.0}
    assert patch == {"data": {"total": "100", "awake": "32"}}
    spec, patch = actor_load_patch(CUR, {"awake": "4"})
    assert spec["total"] == 40 and patch == {"data": {"awake": "4"}}
    spec, patch = actor_load_patch(CUR, {"hold_s": 2.5})
    assert patch == {"data": {"hold_s": "2.5"}} and spec["hold_s"] == 2.5
    # ConfigMap absent/empty: defaults 0 / 0 / 5 s.
    assert actor_load_patch(None, {"total": 10}) == ({"total": 10, "awake": 0, "hold_s": 5.0}, {"data": {"total": "10"}})
    assert actor_load_patch(CUR, {"total": 200, "awake": 200})[0]["awake"] == 200


@pytest.mark.parametrize("body, why", [
    ({}, "at least one"), ([], "JSON object"), ({"total": 201}, "total must be 0..200"), ({"total": -1}, "total must be"),
    ({"total": 10}, "awake must be 0..total (10), got 16"),  # lowering total below the current awake
    ({"awake": 41}, "awake must be 0..total (40)"), ({"total": "x"}, "integer"), ({"total": 1.5}, "integer"),
    ({"awake": True}, "integer"), ({"hold_s": 0}, "hold_s must be"), ({"hold_s": 301}, "hold_s must be"),
    ({"hold_s": "soon"}, "number of seconds"), ({"total": 5, "foo": 1}, "unknown field"),
])
def test_actor_load_patch_validation(body, why):
    with pytest.raises(ValueError, match=re.escape(why)):
        actor_load_patch(CUR, body)


def test_parse_actor_load_and_stats_extras():
    assert parse_actor_load({"total": "40", "awake": "16", "hold_s": "5"}) == CUR
    assert parse_actor_load(None)["exists"] is False
    assert parse_actor_load({"total": "lots"})["total"] is None
    extra = {**STATS, "actors_running_by_pool": {"g4-workers": 8, "n4d-workers": "8", "x": "bad"}, "target_total": 40,
             "target_awake": 16, "hold_s": 5, "source": "actor-load"}
    st = build_state(_raw(_sub_ok(actor_stats=json.dumps(extra), actor_load={"total": "40", "awake": "16"})), recipe="substrate")
    a = st["substrate"]["actors"]
    assert (a["running_g4"], a["running_n4d"], a["target_awake"], a["target_total"], a["source"]) == (8, 8, 16, 40, "actor-load")
    assert a["actors_running_by_pool"] == {"g4-workers": 8, "n4d-workers": 8}
    assert st["substrate"]["actor_load"]["awake"] == 16 and st["substrate"]["actor_load"]["hold_s"] is None


def test_live_set_actor_load_patches_configmap():
    patched = []
    lb = _bare_live(cm=types.SimpleNamespace(data={"total": "40", "awake": "16", "hold_s": "5"}))
    lb.core.patch_namespaced_config_map = lambda name, ns, body, **k: patched.append((name, ns, body))
    res = asyncio.run(lb.set_actor_load({"awake": 32, "total": 100}))
    assert patched == [("actor-load", "substrate-exp", {"data": {"total": "100", "awake": "32"}})]
    assert res["spec"] == {"total": 100, "awake": 32, "hold_s": 5.0}
    with pytest.raises(ValueError):
        asyncio.run(lb.set_actor_load({"awake": 101}))
    assert len(patched) == 1  # nothing written on a bad body
    lb = _bare_live(cm=_api_exc(404))
    with pytest.raises(RuntimeError, match="not found"):
        asyncio.run(lb.set_actor_load({"total": 1}))


def test_mock_actor_load_one_awake_actor_per_worker_g4_first():
    assert mock.SUB_ACTOR_SLOTS == 1  # an awake actor occupies its whole Worker
    b = mock.SubstrateMockBackend()

    def settle():
        for w in b.sub_workers:
            if w["ready_at"]:
                w["ready_at"] = 0
        s = asyncio.run(b.state())["substrate"]
        return s, s["actors"], s["pools"]

    asyncio.run(b.set_actor_load({"total": 10, "awake": 1}))
    s, a, pools = settle()
    assert (a["actors_running"], a["running_g4"], a["running_n4d"], a["actors_total"]) == (1, 1, 0, 10)  # G4 first
    asyncio.run(b.set_actor_load({"total": 40, "awake": 16}))
    s, a, pools = settle()
    g4_ready, n4d_ready = pools["g4-workers"]["pods_ready"], pools["n4d-workers"]["pods_ready"]
    assert a["running_g4"] == min(16, g4_ready) and a["running_n4d"] == min(16 - a["running_g4"], n4d_ready)
    assert a["actors_running"] == a["running_g4"] + a["running_n4d"] < 16  # the rest wait (UI: ▲)
    assert a["actors_running"] <= g4_ready + n4d_ready and a["target_awake"] == 16
    assert s["n4d_hpa"]["desired"] > 2  # overflow beyond G4 asks for more n4d-workers
    asyncio.run(b.set_actor_load({"awake": 0}))  # suspended actors take no Worker
    s, a, pools = settle()
    assert a["actors_running"] == 0 and a["actors_total"] == 40 and a["actors_by_node"] == {}
    with pytest.raises(ValueError):
        asyncio.run(b.set_actor_load({"awake": 41}))


def test_api_substrate_actors(sub_client):
    r = sub_client.post("/api/substrate/actors?cluster=substrate", json={"total": 100, "awake": 32})
    assert r.status_code == 200 and r.json()["spec"] == {"total": 100, "awake": 32, "hold_s": 5.0}
    assert sub_client.get("/api/state?cluster=substrate").json()["substrate"]["actor_load"]["total"] == 100
    r = sub_client.post("/api/substrate/actors?cluster=substrate", json={"awake": 500})
    assert r.status_code == 400 and "awake must be" in r.json()["detail"]
    r = sub_client.post("/api/substrate/actors?cluster=controller", json={"total": 1})
    assert r.status_code == 409


def test_actor_stats_failure_counters():
    raw = {**STATS, "lost_actors": 1, "resumes_waited_no_room": "37", "memory_resets": 2, "failed_requests": None}
    st, problem = parse_actor_stats(json.dumps(raw))
    assert problem is None
    assert (st["lost_actors"], st["resumes_waited_no_room"], st["memory_resets"], st["failed_requests"]) == (1, 37, 2, None)
    st, _ = parse_actor_stats(json.dumps({"actors_running": 3}))  # absent counters tolerated
    assert st["resumes_waited_no_room"] is None and st["memory_resets"] is None and st["lost_actors"] is None
    a = build_state(_raw(_sub_ok(actor_stats=json.dumps(raw))), recipe="substrate")["substrate"]["actors"]
    assert a["lost_actors"] == 1 and a["resumes_waited_no_room"] == 37 and a["memory_resets"] == 2


def test_mock_waits_accumulate_when_awake_exceeds_workers():
    b = mock.SubstrateMockBackend()
    for w in b.sub_workers:
        if w["ready_at"]:
            w["ready_at"] = 0
    a0 = asyncio.run(b.state())["substrate"]["actors"]
    assert a0["resumes_waited_no_room"] == 0 and a0["lost_actors"] == 0 and a0["failed_requests"] == 0
    asyncio.run(b.set_actor_load({"total": 100, "awake": 50}))
    a1 = asyncio.run(b.state())["substrate"]["actors"]
    a2 = asyncio.run(b.state())["substrate"]["actors"]
    assert 0 < a1["resumes_waited_no_room"] < a2["resumes_waited_no_room"]  # cumulative
    assert a2["memory_resets"] >= a1["memory_resets"] >= 0 and a2["lost_actors"] == 0


# ------------------------------------------------------------------ Worker occupancy (running_workers)
def test_running_workers_mark_occupied_and_idle_workers():
    stats = {**STATS, "running_workers": ["n4d-workers-0", "g4-workers-0", "g4-workers-0", 7, ""]}
    st = build_state(_raw(_sub_ok(actor_stats=json.dumps(stats))), recipe="substrate")
    assert st["substrate"]["actors"]["running_workers"] == ["g4-workers-0", "n4d-workers-0"]
    by = {n["name"]: n for n in st["nodes"]}
    g4_owners = {c["owner"]["pod"]: c["owner"].get("actor") for c in by["g4a"]["cpus"] if c["cat"] == "worker"}
    assert g4_owners == {"g4-workers-0": True}
    assert [p["actor"] for p in by["n4d0"]["pods"] if p["cat"] == "worker"] == [True]
    # A Worker not in the list is idle (False), not unknown.
    stats["running_workers"] = []
    st = build_state(_raw(_sub_ok(actor_stats=json.dumps(stats))), recipe="substrate")
    by = {n["name"]: n for n in st["nodes"]}
    assert {c["owner"].get("actor") for c in by["g4a"]["cpus"] if c["cat"] == "worker"} == {False}


def test_running_workers_absent_leaves_occupancy_unknown():
    st = build_state(_raw(_sub_ok()), recipe="substrate")  # STATS has no running_workers (older generator)
    assert st["substrate"]["actors"]["running_workers"] is None
    assert all("actor" not in c["owner"] for n in st["nodes"] for c in n["cpus"] if c["cat"] == "worker")
    assert all("actor" not in p for n in st["nodes"] for p in n["pods"] if p["cat"] == "worker")
    st, _ = parse_actor_stats(json.dumps({"running_workers": "g4-workers-0"}))
    assert st["running_workers"] is None  # not a list


def test_mock_marks_occupied_workers_g4_first():
    b = mock.SubstrateMockBackend()
    for w in b.sub_workers:
        if w["ready_at"]:
            w["ready_at"] = 0
    asyncio.run(b.set_actor_load({"total": 40, "awake": 2}))
    st = asyncio.run(b.state())
    a = st["substrate"]["actors"]
    assert len(a["running_workers"]) == a["actors_running"] == 2
    occ = {c["owner"]["pod"]: c["owner"]["actor"] for n in st["nodes"] for c in n["cpus"] if c["cat"] == "worker"}
    assert sum(occ.values()) == 2 and any(v is False for v in occ.values())
    g4_occ = [p for n in st["nodes"] if n["role"] == "g4" for p in n["pods"] if p["cat"] == "worker" and p["actor"]]
    assert len(g4_occ) == a["running_g4"] >= 1  # G4 first


# ------------------------------------------------------------------ substrate on the native recipe (base_recipe)
NATIVE_ENTRY = {"id": "substrate", "label": "G4 · Agent Substrate", "recipe": "substrate", "base_recipe": "native",
                "context": CTX_0726}


def test_parse_clusters_base_recipe():
    specs = be.parse_clusters({"DEMO_CLUSTERS": json.dumps([NATIVE_ENTRY])})
    assert specs[0]["base_recipe"] == "native" and be.cluster_info(specs[0])["base_recipe"] == "native"
    # Default stays controller (backward compatible); other recipes carry none.
    assert be.parse_clusters({"DEMO_CLUSTERS": json.dumps(ENTRIES)})[1]["base_recipe"] == "controller"
    assert be.parse_clusters({"DEMO_CLUSTERS": json.dumps(ENTRIES)})[0]["base_recipe"] is None
    with pytest.raises(ValueError, match="base_recipe must be one of"):
        be.parse_clusters({"DEMO_CLUSTERS": json.dumps([{**NATIVE_ENTRY, "base_recipe": "tpu"}])})
    with pytest.raises(ValueError, match="only applies to recipe substrate"):
        be.parse_clusters({"DEMO_CLUSTERS": json.dumps([{"id": "n", "recipe": "native", "base_recipe": "native"}])})
    assert isinstance(be.make_backend(specs[0], mock=True), mock.SubstrateNativeMockBackend)
    assert cluster.SubstrateNativeLiveBackend.has_controller is False
    assert "colo-svc" in cluster.SubstrateNativeLiveBackend.hpa_namespaces


def test_state_substrate_on_native_uses_native_semantics():
    raw = _raw(_sub_ok())
    raw["nodes"][0]["labels"]["colo.active"] = "false"  # ignored on native: eligibility = Ready vLLM pod
    raw["deployments"] = [{"namespace": "colo-svc", "name": "sandbox-backfill", "replicas": 7, "ready": 1}]
    raw["hpas"] = [{"name": "sandbox-overflow", "namespace": "colo-svc", "target": "sandbox-overflow", "min": 1, "max": 60,
                    "current": 1, "desired": 3, "metric": "3"}]
    raw["configmap"] = {"demand": {"sandbox": 99}}  # a stale colo-config must not be used on native
    st = build_state(raw, recipe="substrate", cluster={"name": "c", "base_recipe": "native"})
    assert st["base_recipe"] == "native" and st["control"]["kind"] == "native"
    assert st["control"]["label"] == "native K8s + Agent Substrate" and st["control"]["guard_available"] is False
    assert st["controller"]["demand"] == {"sandbox": 7} and st["controller"]["reachable"] is False
    assert st["services"]["sandbox"]["overflow"]["hpa"]["desired"] == 3
    assert next(n for n in st["nodes"] if n["name"] == "g4a")["eligible"] is True
    assert st["substrate"]["pools"]["g4-workers"]["on_g4"] == 1
    st = build_state(raw, recipe="substrate")  # default base: controller, unchanged
    assert st["base_recipe"] == "controller" and st["control"]["label"] == "colo-controller + Agent Substrate"
    assert next(n for n in st["nodes"] if n["name"] == "g4a")["eligible"] is False  # colo.active=false on controller


def test_mock_substrate_native_and_actions():
    b = mock.SubstrateNativeMockBackend()
    st = asyncio.run(b.state())
    assert st["recipe"] == "substrate" and st["base_recipe"] == "native" and st["control"]["kind"] == "native"
    assert not any((p.get("labels") or {}).get("app") == "colo-controller" for p in b.raw()["pods"])
    assert st["substrate"]["pools"]["g4-workers"]["on_g4"] >= 1 and st["substrate"]["control_plane"]["present"]
    g4w = [p for p in b.raw()["pods"] if (p["labels"] or {}).get("ate.dev/worker-pool") == "g4-workers"]
    assert {p["priority_class"] for p in g4w} == {"substrate-worker"}
    assert asyncio.run(b.set_demand({"sandbox": 10}))["scaled"][0]["deployment"] == "colo-svc/sandbox-backfill"
    with pytest.raises(cluster.UnsupportedOnRecipe):
        asyncio.run(b.set_guard(True))


def test_api_substrate_native_tab(monkeypatch):
    spec = be.parse_clusters({"DEMO_CLUSTERS": json.dumps([{**NATIVE_ENTRY, "id": "subnat"}])})[0]
    monkeypatch.setitem(demo_app.CTXS, "subnat", demo_app.ClusterCtx(spec, be.make_backend(spec, mock=True)))
    with TestClient(demo_app.app) as c:
        cl = {x["id"]: x for x in c.get("/api/clusters").json()["clusters"]}["subnat"]
        assert cl["control"] == "native K8s + Agent Substrate" and cl["guard_available"] is False
        assert cl["base_recipe"] == "native"
        assert c.post("/api/guard?cluster=subnat", json={"enabled": True}).status_code == 409
        assert c.post("/api/baseline?cluster=subnat").status_code == 409
        assert c.post("/api/demand?cluster=subnat", json={"sandbox": 8}).status_code == 200
        st = c.get("/api/state?cluster=subnat").json()
        assert st["services"]["sandbox"]["demand"] == 8 and st["base_recipe"] == "native"
        assert c.post("/api/substrate/actors?cluster=subnat", json={"awake": 4}).status_code == 200


# ------------------------------------------------------------------ Total Workers (option 2: HPA g4-workers maxReplicas)
from state import cpu_budget, validate_worker_replicas  # noqa: E402


@pytest.mark.parametrize("body, why", [({}, "set total"), ([], "JSON object"), ({"total": 61}, "0..60"),
                                       ({"total": -1}, "0..60"), ({"total": "x"}, "integer"),
                                       ({"total": 2.5}, "integer"), ({"total": True}, "integer"),
                                       ({"total": 3, "pool": "x"}, "unknown field"),
                                       ({"total": 3, "replicas": 3}, "not both")])
def test_validate_worker_total_rejects(body, why):
    with pytest.raises(ValueError, match=re.escape(why)):
        validate_worker_replicas(body)


def test_validate_worker_total_ok():
    assert validate_worker_replicas({"total": 0}) == 0
    assert validate_worker_replicas({"total": "60"}) == 60
    assert validate_worker_replicas({"replicas": 7}) == 7  # old spelling still accepted


def _hpa_obj(min_replicas=1):
    return types.SimpleNamespace(spec=types.SimpleNamespace(min_replicas=min_replicas))


def test_live_total_workers_patches_hpa_max_when_hpa_exists():
    patched, scaled = [], []
    lb = _bare_live()
    lb.autoscaling.read_namespaced_horizontal_pod_autoscaler = lambda name, ns, **k: _hpa_obj(1)
    lb.autoscaling.patch_namespaced_horizontal_pod_autoscaler = lambda name, ns, body, **k: patched.append((name, ns, body, k.get("_content_type")))
    lb.custom.patch_namespaced_custom_object_scale = lambda *a, **k: scaled.append(a)
    res = asyncio.run(lb.set_g4_workers({"total": 24}))
    assert patched == [("g4-workers", "substrate-exp", {"spec": {"maxReplicas": 24}}, "application/merge-patch+json")]
    assert scaled == [] and res == {"target": "hpa/substrate-exp/g4-workers", "mode": "hpa-max", "total": 24}
    lb.autoscaling.read_namespaced_horizontal_pod_autoscaler = lambda name, ns, **k: _hpa_obj(3)
    with pytest.raises(ValueError, match="at least 3"):
        asyncio.run(lb.set_g4_workers({"total": 2}))
    with pytest.raises(ValueError):
        asyncio.run(lb.set_g4_workers({"total": 99}))
    assert len(patched) == 1  # nothing written on a bad body


def test_live_total_workers_falls_back_to_workerpool_scale_without_hpa():
    calls = []
    lb = _bare_live()

    def no_hpa(*a, **k):
        raise _api_exc(404)

    lb.autoscaling.read_namespaced_horizontal_pod_autoscaler = no_hpa
    lb.custom.patch_namespaced_custom_object_scale = lambda *a, **k: calls.append(a)
    res = asyncio.run(lb.set_g4_workers({"total": 12}))
    assert calls == [("ate.dev", "v1", "substrate-exp", "workerpools", "g4-workers", {"spec": {"replicas": 12}})]
    assert res["mode"] == "workerpool-replicas" and res["total"] == 12

    def missing(*a, **k):
        raise _api_exc(404)

    lb.custom.patch_namespaced_custom_object_scale = missing
    with pytest.raises(RuntimeError, match="neither HPA nor WorkerPool"):
        asyncio.run(lb.set_g4_workers({"total": 1}))

    def forbidden(*a, **k):
        raise _api_exc(403)

    lb.autoscaling.read_namespaced_horizontal_pod_autoscaler = forbidden  # not "absent": never silently fall back
    with pytest.raises(Exception) as e:
        asyncio.run(lb.set_g4_workers({"total": 1}))
    assert getattr(e.value, "status", None) == 403


def test_state_total_workers_and_shares():
    sub = _sub_ok(hpas=[{"name": "g4-workers", "namespace": "substrate-exp", "target": "g4-workers", "min": 1, "max": 24,
                         "current": 2, "desired": 2, "metric": "2"},
                        {"name": "n4d-workers", "namespace": "substrate-exp", "target": "n4d-workers", "min": 0, "max": 24,
                         "current": 1, "desired": 22, "metric": "0"}])
    s = build_state(_raw(sub), recipe="substrate")["substrate"]
    assert s["total_workers"] == 24 and s["total_workers_source"] == "hpa" and s["g4_hpa"]["max"] == 24
    assert s["n4d_hpa"]["desired"] == 22  # the n4d HPA is still found next to the g4 one
    s = build_state(_raw(_sub_ok()), recipe="substrate")["substrate"]  # no g4-workers HPA: WorkerPool replicas
    assert s["total_workers"] == 2 and s["total_workers_source"] == "workerpool" and s["g4_hpa"] is None


def test_cpu_budget_counts_non_backfill_and_pending_workers():
    nodes_out = [{"name": "g4a", "role": "g4", "ready": True, "alloc_cpu": 46.0},
                 {"name": "n4d0", "role": "n4d", "ready": True, "alloc_cpu": 15.89},
                 {"name": "lg", "role": "loadgen", "ready": True, "alloc_cpu": 8.0}]
    pods = [
        _pod("vllm-a", "g4a", {"app": "vllm"}, "inference", cpu=8),
        _pod("ds-1", "g4a", {"app": "agent"}, "kube-system", cpu=0.5),
        _worker("g4-workers-0", "g4a", "g4-workers"),                               # 6 vCPU Running Worker
        _worker("g4-workers-p", None, "g4-workers", phase="Pending", ready=False),  # Pending -> 2 vCPU
        _worker("n4d-workers-p", None, "n4d-workers", phase="Pending", ready=False),
        _pod("sandbox-backfill-0", "g4a", {"app": "sandbox", "colo.tier": "backfill", "colo.class": "integer"}, "colo-svc", cpu=2),
        _pod("toolcall-overflow-0", "n4d0", {"app": "toolcall", "colo.tier": "overflow", "colo.class": "integer"}, "colo-svc", cpu=3),
        _pod("colo-buffer-1", "n4d0", {"app": "colo-buffer"}, "colo-system", cpu=2),  # preemptible: not subtracted
        _pod("loadgen-1", "lg", {"app": "loadgen"}, "inference", cpu=1),             # not a G4/N4D node
    ]
    b = cpu_budget(nodes_out, pods)
    assert b["alloc_vcpu"] == pytest.approx(61.89) and b["non_backfill_vcpu"] == pytest.approx(14.5)
    assert b["pending_workers"] == 2 and b["vcpu_left"] == pytest.approx(61.89 - 14.5 - 4)
    assert b["per_replica_vcpu"]["sandbox"] == 2 and b["per_replica_vcpu"]["toolcall"] == 3
    assert b["max_replicas"] == {"sandbox": 21, "toolcall": 14, "analytics": 21}
    st = build_state(_raw(_sub_ok()), recipe="substrate")
    assert st["substrate"]["cpu_budget"]["vcpu_left"] >= 0
    assert "substrate" not in build_state(_raw(_sub_ok()), recipe="controller")  # other tabs: no budget, no clamp


@pytest.mark.parametrize("cls", [mock.SubstrateMockBackend, mock.SubstrateNativeMockBackend])
def test_mock_option2_g4_share_then_n4d(cls):
    b = cls()

    def settle():
        for w in b.sub_workers:
            if w["ready_at"]:
                w["ready_at"] = 0
        return asyncio.run(b.state())["substrate"]

    assert asyncio.run(b.set_g4_workers({"total": 4}))["mode"] == "hpa-max"
    s = settle()
    assert s["total_workers"] == 4 and s["g4_hpa"]["max"] == 4
    assert s["pools"]["g4-workers"]["desired"] == 4 and s["pools"]["g4-workers"]["on_g4"] == 4  # outrank backfill
    assert s["pools"]["n4d-workers"]["desired"] == 0
    asyncio.run(b.set_g4_workers({"total": 20}))
    s = settle()
    g4_share = s["pools"]["g4-workers"]["desired"]
    assert 0 < g4_share < 20 and s["pools"]["g4-workers"]["pending"] == 0  # G4 share = what fits next to vLLM
    assert s["pools"]["n4d-workers"]["desired"] == 20 - g4_share  # the rest on N4D (no awake overflow at 2 awake)
    assert s["cpu_budget"]["max_replicas"]["sandbox"] < 20  # Workers squeeze the CPU-backfill sliders
    with pytest.raises(ValueError, match="at least 1"):
        asyncio.run(b.set_g4_workers({"total": 0}))


def test_mock_without_g4_hpa_falls_back_to_workerpool(monkeypatch):
    monkeypatch.setenv("DEMO_MOCK_NO_G4_HPA", "1")
    b = mock.SubstrateMockBackend()
    assert asyncio.run(b.set_g4_workers({"total": 6}))["mode"] == "workerpool-replicas"
    s = asyncio.run(b.state())["substrate"]
    assert s["g4_hpa"] is None and s["total_workers_source"] == "workerpool" and s["total_workers"] == 6
    g4 = s["pools"]["g4-workers"]
    assert g4["pods"] == 6 and g4["on_g4"] + g4["pending"] == 6  # pre-option-2: extras stay Pending
    asyncio.run(b.set_g4_workers({"total": 0}))
    assert asyncio.run(b.state())["substrate"]["pools"]["g4-workers"]["pods"] == 0


def test_api_substrate_workers(sub_client):
    r = sub_client.post("/api/substrate/workers?cluster=substrate", json={"total": 5})
    assert r.status_code == 200 and r.json()["total"] == 5 and r.json()["mode"] == "hpa-max"
    st = sub_client.get("/api/state?cluster=substrate").json()["substrate"]
    assert st["total_workers"] == 5 and st["g4_hpa"]["max"] == 5
    assert sub_client.post("/api/substrate/workers?cluster=substrate", json={"total": 61}).status_code == 400
    assert sub_client.post("/api/substrate/workers?cluster=substrate", json={"total": 0}).status_code == 400
    assert sub_client.post("/api/substrate/workers?cluster=controller", json={"total": 1}).status_code == 409


# ------------------------------------------------------------------ cost: Workers in reclaimed + counterfactual
def test_counterfactual_counts_workers_on_g4_and_n4d():
    n4d_workers = [_pod(f"n4d-workers-{i}", f"n4d{i // 6}", {"ate.dev/worker-pool": "n4d-workers"}, "substrate-exp",
                        cpu=2, mem=4) for i in range(18)]
    raw = _raw(_sub_ok())
    raw["nodes"] += [_node("n4d1", "n4d", 15.89, 57), _node("n4d2", "n4d", 15.89, 57)]
    raw["pods"] = [p for p in raw["pods"] if p["name"] != "n4d-workers-0"] + n4d_workers
    st = build_state(raw, recipe="substrate")
    cf = st["cost"]["counterfactual"]
    # G4: sandbox backfill (2) + g4-workers-0 (6); N4D: 18 n4d-workers x 2 vCPU.
    assert cf["pods"] == 2 + 18 and cf["vcpu"] == pytest.approx(2 + 6 + 36) and cf["workers"] == 19
    assert cf["n4d_nodes_now"] == 3 and cf["n4d_nodes"] >= 3
    assert "Agent Substrate Workers" in cf["method"]
    assert st["story"]["reclaimed_vcpu"] == pytest.approx(8)  # backfill 2 + G4 Worker 6; N4D Workers are not reclaimed
    ctl = build_state(raw, recipe="controller")["cost"]["counterfactual"]  # other tabs unchanged: no Workers
    assert ctl["pods"] == 1 and ctl.get("workers") == 0 and "Agent Substrate" not in ctl["method"]


def test_mock_saving_not_zero_when_workers_fill_g4_and_n4d():
    b = mock.SubstrateNativeMockBackend()
    asyncio.run(b.set_g4_workers({"total": 40}))
    for _ in range(4):
        for n in b.nodes.values():
            n["ready_at"] = min(n["ready_at"], time.time() - 1)
        for w in b.sub_workers:
            if w["ready_at"]:
                w["ready_at"] = 0
        b._step(time.time() + 1, force=True)
    cf = asyncio.run(b.state())["cost"]["counterfactual"]
    assert cf["workers"] >= 30 and cf["saved_per_hour"] > 0
