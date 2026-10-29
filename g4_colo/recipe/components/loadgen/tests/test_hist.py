import os
import random
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from hist import RollingHistogram, bucket_index, mean, merge_hists, percentile  # noqa: E402
import prompts  # noqa: E402


def test_bucket_index():
    assert bucket_index(0) == 0
    assert bucket_index(1.99) == 0
    assert bucket_index(2.0) == 1
    assert bucket_index(1999.9) == 999
    assert bucket_index(2000) == 1000
    assert bucket_index(1e9) == 1000
    assert bucket_index(-5) == 0


def test_percentile_basic():
    h = [0] * 1001
    assert percentile(h, 99) is None
    for ms in range(100):  # 1 sample in each of 0..99 ms
        h[bucket_index(ms)] += 1
    assert percentile(h, 50) == 50.0  # 50th sample is 49 ms -> bucket [48,50)
    assert percentile(h, 99) == 100.0
    assert percentile(h, 100) == 100.0
    assert percentile(h, 0) == 2.0


def test_percentile_overflow_and_bounds():
    h = [0] * 1001
    h[5] = 98
    h[1000] = 2
    assert percentile(h, 98) == 12.0
    assert percentile(h, 99) == 2000.0
    with pytest.raises(ValueError):
        percentile(h, 101)


def test_mean_and_merge():
    a = [0] * 1001
    b = [0] * 1001
    a[10] = 2
    b[20] = 2
    m = merge_hists([a, b])
    assert sum(m) == 4 and m[10] == 2 and m[20] == 2
    assert mean(m) == pytest.approx((21 + 41) / 2)
    assert merge_hists([]) == []
    with pytest.raises(ValueError):
        merge_hists([[1, 2], [1]])


def test_rolling_window():
    r = RollingHistogram(window_s=30)
    t0 = 1_000_000.0
    r.record(10, t0)
    r.record(10, t0 + 0.5)
    r.record(20, t0 + 10)
    assert r.count(t0 + 10) == 3
    assert r.snapshot(t0 + 10)[5] == 2
    # t0 slot falls out of the window at t0+30
    assert r.count(t0 + 29.9) == 3
    assert r.count(t0 + 30) == 1
    # slot reuse after wrap-around clears old data
    r.record(30, t0 + 30)
    s = r.snapshot(t0 + 30)
    assert s[5] == 0 and s[10] == 1 and s[15] == 1
    assert r.count(t0 + 100) == 0


def test_rolling_matches_naive():
    rng = random.Random(0)
    r = RollingHistogram(window_s=30)
    events = []
    t = 5000.0
    for _ in range(5000):
        t += rng.expovariate(50)
        ms = rng.lognormvariate(3.5, 0.8)
        r.record(ms, t)
        events.append((t, ms))
    now = t
    naive = [0] * 1001
    for et, ms in events:
        if int(now) - 30 < int(et) <= int(now):
            naive[bucket_index(ms)] += 1
    assert r.snapshot(now) == naive


def test_prompt():
    rng = random.Random(3)
    a, b = prompts.build_prompt(rng), prompts.build_prompt(rng)
    assert a != b
    assert a.startswith(prompts.INSTRUCTION) and a.endswith("Label:")
    assert 500 <= prompts.estimate_tokens(a) <= 600
