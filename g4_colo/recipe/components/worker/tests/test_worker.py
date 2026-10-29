import os
import random
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import worker  # noqa: E402


def test_parse_cpuset():
    assert worker.parse_cpuset("0-3,8,10-11\n") == [0, 1, 2, 3, 8, 10, 11]
    assert worker.parse_cpuset("5") == [5]
    assert worker.parse_cpuset("") == []


def test_parse_cpu_max():
    assert worker.parse_cpu_max("max 100000") is None
    assert worker.parse_cpu_max("200000 100000") == 2
    assert worker.parse_cpu_max("150000 100000") == 2


def test_detect_cpus(tmp_path):
    (tmp_path / "cpuset.cpus.effective").write_text("2-3\n")
    assert worker.detect_cpus(str(tmp_path)) == 2
    (tmp_path / "cpuset.cpus.effective").write_text("0-47\n")
    (tmp_path / "cpu.max").write_text("400000 100000\n")
    assert worker.detect_cpus(str(tmp_path)) == 4
    empty = tmp_path / "none"
    empty.mkdir()
    assert worker.detect_cpus(str(empty)) == (os.cpu_count() or 1)


@pytest.mark.parametrize("seed", range(10))
def test_sandbox_snippets_run(seed):
    assert worker.sandbox_unit(random.Random(seed), timeout=20)


def test_toolcall_unit():
    assert worker.toolcall_unit(random.Random(1)) > 50


def test_stencil():
    n = worker.stencil_dims(8)  # 8 MB total -> two ~4 MB grids
    assert 70 < n < 90
    st = worker.Stencil(32)
    before = st.a.copy()
    st.sweep()
    st.sweep()
    assert st.a.shape == (32 ** 3,)
    assert not (st.a == before).all()
    assert 0.0 <= st.a.min() and st.a.max() <= 1.0  # convex combination of [0,1) values
    # one interior cell matches the 7-point formula
    st2 = worker.Stencil(8, seed=1)
    g = st2.a.reshape(8, 8, 8).copy()
    st2.sweep()
    exp = 0.4 * g[3, 3, 3] + 0.1 * (g[2, 3, 3] + g[4, 3, 3] + g[3, 2, 3] + g[3, 4, 3] + g[3, 3, 2] + g[3, 3, 4])
    assert abs(st2.a.reshape(8, 8, 8)[3, 3, 3] - exp) < 1e-12
