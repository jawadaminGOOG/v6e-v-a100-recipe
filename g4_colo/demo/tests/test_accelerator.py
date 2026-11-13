"""Accelerator profiles: a TPU v6e cluster (native recipe on ct6e-standard-1t) next to the G4 clusters.

Covers profile selection from DEMO_CLUSTERS, the unchanged G4 default, TPU pricing and host vCPUs, and that nothing a
person reads on a TPU cluster (/api/state, /api/clusters, /api/scenarios, scenario banner) says G4, GPU or RTX."""
from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
import os  # noqa: E402

os.environ["DEMO_MOCK"] = "1"

from fastapi.testclient import TestClient  # noqa: E402

import app as demo_app  # noqa: E402
import backend as be  # noqa: E402
import mock  # noqa: E402
import scenarios as sc  # noqa: E402
import state  # noqa: E402

TPU_ENTRY = {"id": "tpu", "label": "TPU v6e · native", "recipe": "native", "accelerator": "tpu-v6e",
             "context": "gke_northam-ce-mlai-tpu_us-east5_v6ecluster-jawadamin-us-east5-1126",
             "name": "v6ecluster-jawadamin-us-east5-1126"}
G4_ENTRIES = [
    {"id": "controller", "label": "With colo-controller", "recipe": "controller",
     "context": "gke_northam-ce-mlai-tpu_us-west8_g4cluster-jawadamin-us-west8-0726"},
    {"id": "native", "label": "Native K8s only", "recipe": "native",
     "context": "gke_northam-ce-mlai-tpu_us-west8_g4cluster-jawadamin-us-west8-1126"},
]
# Case-sensitive on purpose: identifiers such as g4_nodes, g4-standard-48 or gke-g4cluster-... are not display text.
FORBIDDEN = re.compile(r"\bG4\b|\bGPUs?\b|RTX")


def _strings(x, path=""):
    if isinstance(x, str):
        yield path, x
    elif isinstance(x, dict):
        for k, v in x.items():
            yield from _strings(v, f"{path}.{k}")
    elif isinstance(x, (list, tuple)):
        for i, v in enumerate(x):
            yield from _strings(v, f"{path}[{i}]")


def _offenders(doc):
    return [(p, s) for p, s in _strings(doc) if FORBIDDEN.search(s)]


def _specs():
    return be.parse_clusters({"DEMO_CLUSTERS": json.dumps(G4_ENTRIES + [TPU_ENTRY])})


# ------------------------------------------------------------------ profile selection
def test_profile_selected_by_accelerator_field():
    specs = {s["id"]: s for s in _specs()}
    tpu = specs["tpu"]
    assert tpu["accelerator"] == "tpu-v6e" and tpu["recipe"] == "native"
    assert tpu["name"] == "v6ecluster-jawadamin-us-east5-1126"
    assert be.cluster_info(tpu)["accelerator"] == "tpu-v6e"
    prof = be.accelerator_of(tpu)
    assert prof["name"] == "TPU v6e" and prof["tag"] == "v6e" and prof["unit"] == "TPU"
    assert prof["vcpu"] == 44 and prof["machine"] == "ct6e-standard-1t"


def test_default_accelerator_is_g4_and_unchanged():
    for s in _specs()[:2]:
        assert s["accelerator"] == "g4"
        prof = be.accelerator_of(s)
        assert prof["name"] == "G4 · RTX PRO 6000" and prof["tag"] == "G4" and prof["unit"] == "GPU"
        assert prof["vcpu"] == 48 and prof["price_hr"] == state.PRICE_G4_HR
    assert [s["accelerator"] for s in be.parse_clusters({"DEMO_MOCK": "1"})] == ["g4", "g4"]
    assert state.accel_profile(None)["id"] == "g4" and state.accel_profile({"name": "x"})["id"] == "g4"
    # G4 text is returned exactly as written.
    g4 = state.accel_profile("g4")
    txt = "GPU scale-out: a load spike adds a 2nd G4 node; G4 #1 stays packed"
    assert state.localize(txt, g4) is txt
    assert sc.scenarios_for("native", g4) is sc.NATIVE_SCENARIOS


def test_unknown_accelerator_rejected():
    with pytest.raises(ValueError):
        be.parse_clusters({"DEMO_CLUSTERS": json.dumps([{**TPU_ENTRY, "accelerator": "tpu-v9"}])})


def test_localize_rules():
    tpu = state.accel_profile("tpu-v6e")
    assert state.localize("GPU scale-out", tpu) == "TPU scale-out"
    assert state.localize("G4 #1 stays packed", tpu) == "v6e #1 stays packed"
    assert state.localize("kept off G4 by admission policy", tpu) == "kept off TPU v6e by admission policy"
    assert state.localize("spare GPU-node cores", tpu) == "spare TPU-node cores"
    assert state.localize("g4_nodes=1 on g4-standard-48", tpu) == "g4_nodes=1 on g4-standard-48"


# ------------------------------------------------------------------ TPU state
def _tpu_backend():
    return be.make_backend(_specs()[2], mock=True)


def test_mock_emulates_tpu_v6e_host():
    b = _tpu_backend()
    assert isinstance(b, mock.NativeMockBackend)
    raw = b.raw()
    host = next(n for n in raw["nodes"] if n["labels"].get("colo.role") == "g4")
    lb = host["labels"]
    assert lb["cloud.google.com/compute-class"] == "g4-gpu"  # the recipe's class identifier is kept
    assert lb["cloud.google.com/gke-tpu-accelerator"] == "tpu-v6e-slice" and lb["cloud.google.com/gke-tpu-topology"] == "1x1"
    assert lb["node.kubernetes.io/instance-type"] == "ct6e-standard-1t"
    st = asyncio.run(b.state())
    g4n = [n for n in st["nodes"] if n["role"] == "g4"]
    assert g4n and all(n["ncpu"] == 44 for n in g4n)
    assert st["story"]["g4_slots_per_node"] == 17  # (42 allocatable - 8 vLLM) / 2


def test_tpu_price_uses_v6e_profile():
    st = asyncio.run(_tpu_backend().state())
    hosts = st["counts"]["g4"]
    assert hosts >= 1
    assert st["cost"]["prices"]["accel_hr"] == st["cost"]["prices"]["g4_hr"] == state.PRICE_V6E_HR == 2.70
    assert st["cost"]["g4_per_hour"] == pytest.approx(hosts * 2.70)
    assert st["accelerator"]["id"] == "tpu-v6e"
    # A G4 cluster still bills the G4 price.
    g4 = asyncio.run(be.make_backend(_specs()[1], mock=True).state())
    assert g4["cost"]["prices"]["g4_hr"] == state.PRICE_G4_HR and g4["accelerator"]["id"] == "g4"


def test_tpu_state_has_no_g4_or_gpu_text():
    st = asyncio.run(_tpu_backend().state())
    assert _offenders(st) == []
    assert st["control"]["guard_reason"] == "memory-heavy services are kept off TPU v6e by admission policy"


def test_tpu_build_state_without_nodes_falls_back_to_44_vcpu():
    raw = {"nodes": [{"name": "t0", "labels": {"cloud.google.com/compute-class": "g4-gpu"},
                      "allocatable": {"cpu": "42", "memory": "165Gi"}, "ready": True}], "pods": []}
    st = state.build_state(raw, recipe="native", cluster={"name": "t", "accelerator": "tpu-v6e"})
    assert st["nodes"][0]["role"] == "g4" and st["nodes"][0]["ncpu"] == 44
    st = state.build_state(raw, recipe="native", cluster={"name": "g"})
    assert st["nodes"][0]["ncpu"] == 48


# ------------------------------------------------------------------ API + scenarios on a TPU cluster
@pytest.fixture
def tpu_client(monkeypatch):
    spec = _specs()[2]
    ctx = demo_app.ClusterCtx(spec, be.make_backend(spec, mock=True))
    monkeypatch.setitem(demo_app.CTXS, "tpu", ctx)
    with TestClient(demo_app.app) as c:
        yield c


def test_api_tpu_cluster_user_facing_text(tpu_client):
    st = tpu_client.get("/api/state?cluster=tpu").json()
    assert st["accelerator"]["name"] == "TPU v6e" and st["demo"]["version"] == demo_app.DEMO_VERSION == "0.7.1"
    assert _offenders(st) == []
    scen = tpu_client.get("/api/scenarios?cluster=tpu").json()
    assert set(scen) == {"gpu-scale-out", "gpu-scale-in", "cpu-scale-out"}
    assert _offenders(scen) == []
    assert scen["gpu-scale-out"]["title"].startswith("TPU scale-out: a load spike adds a 2nd TPU v6e node")
    r = tpu_client.post("/api/scenario/guard-integer-vs-memory?cluster=tpu")
    assert r.status_code == 409 and not FORBIDDEN.search(r.json()["detail"])
    cl = {c["id"]: c for c in tpu_client.get("/api/clusters").json()["clusters"]}
    assert cl["tpu"]["accelerator"] == "tpu-v6e" and cl["tpu"]["accelerator_profile"]["tag"] == "v6e"
    # G4 clusters keep their wording.
    g4 = tpu_client.get("/api/scenarios?cluster=native").json()
    assert g4["gpu-scale-out"]["title"] == sc.NATIVE_SCENARIOS["gpu-scale-out"]["title"]


def test_tpu_scenario_banner_and_log_localized():
    spec = _specs()[2]
    b = be.make_backend(spec, mock=True)
    prof = be.accelerator_of(spec)
    runner = sc.ScenarioRunner(b, poll_s=0.01, time_scale=0.001, scenarios=sc.scenarios_for("native", prof),
                               accelerator=prof)
    snap = asyncio.run(runner.run("gpu-scale-in"))  # precondition fails at once: 1 host, 1 vLLM replica
    assert snap["status"] == "failed"
    assert "TPU v6e nodes" in snap["error"]
    assert _offenders(snap) == [], _offenders(snap)


def _l3_cores(node):
    return [len({c["core"] for c in node["cpus"] if c["llc"] == l}) for l in sorted({c["llc"] for c in node["cpus"]})]


def test_core_map_uses_real_l3_domains_v6e_8_7_7_and_g4_8_8_8():
    # Measured on the live nodes: ct6e-standard-1t (EPYC 9B14) 44 vCPU in L3 domains of 8/7/7 cores;
    # g4-standard-48 (EPYC 9B45) 48 vCPU in 8/8/8. The core map must follow the agent's topology, not 8 per L3.
    st = asyncio.run(_tpu_backend().state())
    host = next(n for n in st["nodes"] if n["role"] == "g4")
    assert _l3_cores(host) == [8, 7, 7] and host["topology_source"] == "sysfs" and host["cpu_model"] == "AMD EPYC 9B14"
    assert {c["id"] for c in host["cpus"] if c["llc"] == 1} == set(range(8, 15)) | set(range(30, 37))
    g4 = asyncio.run(be.make_backend(_specs()[1], mock=True).state())
    assert _l3_cores(next(n for n in g4["nodes"] if n["role"] == "g4")) == [8, 8, 8]
    assert _l3_cores(next(n for n in g4["nodes"] if n["role"] == "n4d")) == [8]


def test_core_map_falls_back_to_8_per_l3_without_agent_topology():
    assert [state._cpu_topology(44, c)[2] for c in (7, 8, 15, 16, 21)] == [0, 1, 1, 2, 2]
