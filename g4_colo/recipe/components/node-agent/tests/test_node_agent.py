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


def _fake_sysfs(tmp_path, ncpu, l3_sets, l3_size="32768K"):
    """sysfs as seen on GKE nodes: siblings (c, c+ncpu/2), L3 sharing sets as given."""
    half = ncpu // 2
    for c in range(ncpu):
        d = tmp_path / f"cpu{c}"
        (d / "topology").mkdir(parents=True)
        (d / "cache" / "index3").mkdir(parents=True)
        core = c % half
        (d / "topology" / "thread_siblings_list").write_text(f"{core},{core + half}\n")
        (d / "cache" / "index3" / "shared_cpu_list").write_text(next(s for s in l3_sets if c in na.parse_cpulist(s)) + "\n")
        (d / "cache" / "index3" / "size").write_text(l3_size + "\n")
    (tmp_path / "online").write_text(f"0-{ncpu - 1}\n")  # not a cpuN dir: must be ignored
    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / "cpuinfo").write_text("processor\t: 0\nmodel name\t: AMD EPYC 9B14\n")
    return str(tmp_path), str(proc)


def test_parse_cpulist():
    assert na.parse_cpulist("0-2,22-23\n") == [0, 1, 2, 22, 23]
    assert na.parse_cpulist("5") == [5]


def test_topology_v6e_host_8_7_7(tmp_path):
    # ct6e-standard-1t (AMD EPYC 9B14, 44 vCPU, measured): L3 domains 8, 7 and 7 cores.
    sysd, proc = _fake_sysfs(tmp_path, 44, ["0-7,22-29", "8-14,30-36", "15-21,37-43"])
    t = na.read_topology(sysd, proc)
    assert t["model"] == "AMD EPYC 9B14" and t["l3_kib"] == 32768
    by = {c["id"]: c for c in t["cpus"]}
    assert len(by) == 44
    assert [sum(1 for c in t["cpus"] if c["llc"] == l and c["thread"] == 0) for l in range(3)] == [8, 7, 7]
    assert by[22] == {"id": 22, "core": 0, "thread": 1, "llc": 0}
    assert by[14]["llc"] == 1 and by[15]["llc"] == 2 and by[43] == {"id": 43, "core": 21, "thread": 1, "llc": 2}


def test_topology_g4_host_8_8_8(tmp_path):
    sysd, proc = _fake_sysfs(tmp_path, 48, ["0-7,24-31", "8-15,32-39", "16-23,40-47"])
    t = na.read_topology(sysd, proc)
    assert [sum(1 for c in t["cpus"] if c["llc"] == l) for l in range(3)] == [16, 16, 16]


def test_topology_missing_sysfs_is_none(tmp_path):
    assert na.read_topology(str(tmp_path / "nope"), str(tmp_path)) is None
