import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import node_agent as na  # noqa: E402

STAT0 = """cpu  100 0 100 800 0 0 0 0 0 0
cpu0 50 0 50 400 0 0 0 0 0 0
cpu1 50 0 50 400 0 0 0 0 0 0
intr 1 2 3
"""
STAT1 = """cpu  300 0 100 1000 0 0 0 0 0 0
cpu0 250 0 50 400 0 0 0 0 0 0
cpu1 50 0 50 550 50 0 0 0 0 0
"""


def test_parse_proc_stat():
    s = na.parse_proc_stat(STAT0)
    assert s == {0: (100, 500), 1: (100, 500)}


def test_busy_pct():
    b = na.busy_pct(na.parse_proc_stat(STAT0), na.parse_proc_stat(STAT1))
    assert b == [100.0, 0.0]


def test_meminfo():
    info = na.parse_meminfo("MemTotal:  2097152 kB\nMemFree: 100 kB\nMemAvailable: 1048576 kB\n")
    assert na.mem_used_gib(info) == (1.0, 2.0)


def test_payload(tmp_path, monkeypatch):
    (tmp_path / "stat").write_text(STAT0)
    (tmp_path / "meminfo").write_text("MemTotal: 2097152 kB\nMemAvailable: 1048576 kB\n")
    (tmp_path / "cpu_manager_state").write_text('{"policyName":"static","defaultCpuSet":"0-1"}')
    monkeypatch.setattr(na, "HOST_PROC", str(tmp_path))
    monkeypatch.setattr(na, "KUBELET_DIR", str(tmp_path))
    p = na.cpus_payload()
    assert p["ncpu"] == 2
    assert p["cpu_manager_state"]["policyName"] == "static"
    assert p["mem_used_gib"] == 1.0
    na.SAMPLER.samples.append((0.0, na.parse_proc_stat(STAT0)))
    na.SAMPLER.samples.append((2.0, na.parse_proc_stat(STAT1)))
    p = na.cpus_payload()
    assert p["busy"] == [100.0, 0.0] and p["sample_window_s"] == 2.0
