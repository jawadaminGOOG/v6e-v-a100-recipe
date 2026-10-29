"""Unit and mock-API tests for g4_colo/demo."""
from __future__ import annotations

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
os.environ["DEMO_MOCK"] = "1"

from fastapi.testclient import TestClient  # noqa: E402
import app as demo_app  # noqa: E402
from state import hist_percentile, parse_cpu, parse_cpuset, parse_mem_gib, summarize  # noqa: E402
from scenarios import SCENARIOS, resolve_name  # noqa: E402


def test_parsers():
    assert parse_cpuset("0-3,24-27") == [0, 1, 2, 3, 24, 25, 26, 27]
    assert parse_cpuset("") == []
    assert parse_cpu("500m") == 0.5
    assert parse_cpu("8") == 8.0
    assert abs(parse_mem_gib("64Gi") - 64.0) < 1e-6
    assert hist_percentile([0, 50, 49, 1], 0.99, 2.0) == 6.0


def test_scenario_aliases():
    assert set(SCENARIOS) == {"gpu-scale-out", "gpu-scale-in", "cpu-scale-out", "guard-integer-vs-memory"}
    assert resolve_name("1") == "gpu-scale-out"
    assert resolve_name("4") == "guard-integer-vs-memory"


def test_mock_api_routes():
    c = TestClient(demo_app.app)
    r = c.get("/api/state")
    assert r.status_code == 200
    st = r.json()
    assert st["mode"] == "mock"
    assert len(st["nodes"]) >= 2
    s = summarize(st)
    assert s["g4_nodes"] >= 1
    assert c.post("/api/gpu-rate", json={"rate": 600}).status_code == 200
    assert c.post("/api/demand", json={"sandbox": 4, "toolcall": 4, "analytics": 0}).status_code == 200
    assert c.post("/api/guard", json={"enabled": True}).status_code == 200
    assert c.post("/api/baseline").status_code == 200
    assert c.get("/api/scenarios").status_code == 200
