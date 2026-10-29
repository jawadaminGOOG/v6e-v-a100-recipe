"""Rolling fixed-bucket latency histograms and percentile helpers.

Layout (shared with the colo controller and the demo app): `nbuckets = max_ms /
bucket_ms` buckets of width `bucket_ms`, bucket i covering [i*bucket_ms,
(i+1)*bucket_ms), plus one overflow bucket at index `nbuckets` for values >=
max_ms. With the defaults (2 ms, 2000 ms) a histogram is a list of 1001 counts.
"""

from __future__ import annotations

import math
from typing import Iterable, Sequence


def bucket_index(ms: float, bucket_ms: float = 2.0, max_ms: float = 2000.0) -> int:
    nb = int(round(max_ms / bucket_ms))
    if ms < 0 or math.isnan(ms):
        return 0
    if ms >= max_ms:
        return nb
    return min(nb - 1, int(ms // bucket_ms))


def merge_hists(hists: Iterable[Sequence[int]]) -> list[int]:
    """Element-wise sum of equally sized histograms (empty input -> [])."""
    out: list[int] = []
    for h in hists:
        if not out:
            out = list(h)
        else:
            if len(h) != len(out):
                raise ValueError("histogram length mismatch")
            out = [a + b for a, b in zip(out, h)]
    return out


def percentile(
    counts: Sequence[int], q: float, bucket_ms: float = 2.0
) -> float | None:
    """q-th percentile (0-100) in ms, reported as the upper edge of its bucket.

    Returns None for an empty histogram. If the percentile falls in the
    overflow bucket the result is the overflow lower edge (max_ms), i.e. a
    lower bound on the true value.
    """
    total = sum(counts)
    if total == 0:
        return None
    if not 0 <= q <= 100:
        raise ValueError("q must be in [0, 100]")
    rank = max(1, math.ceil(q / 100.0 * total))
    nb = len(counts) - 1  # last bucket is overflow
    acc = 0
    for i, c in enumerate(counts):
        acc += c
        if acc >= rank:
            if i >= nb:
                return nb * bucket_ms
            return (i + 1) * bucket_ms
    return nb * bucket_ms  # unreachable


def mean(counts: Sequence[int], bucket_ms: float = 2.0) -> float | None:
    """Approximate mean in ms using bucket midpoints (overflow at max_ms)."""
    total = sum(counts)
    if total == 0:
        return None
    nb = len(counts) - 1
    s = 0.0
    for i, c in enumerate(counts):
        if c:
            mid = nb * bucket_ms if i >= nb else (i + 0.5) * bucket_ms
            s += c * mid
    return s / total


class RollingHistogram:
    """Histogram over the last `window_s` seconds, kept as 1 s slots.

    record() is O(1); snapshot() is O(window_s * nbuckets) only over slots that
    are non-empty and still inside the window.
    """

    __slots__ = ("window_s", "bucket_ms", "max_ms", "nb", "_slots")

    def __init__(self, window_s: int = 30, bucket_ms: float = 2.0, max_ms: float = 2000.0):
        self.window_s = int(window_s)
        self.bucket_ms = bucket_ms
        self.max_ms = max_ms
        self.nb = int(round(max_ms / bucket_ms))
        # slot k holds (second, counts) for seconds with second % window_s == k
        self._slots: list[list] = [[-1, None] for _ in range(self.window_s)]

    def record(self, ms: float, now: float) -> None:
        sec = int(now)
        slot = self._slots[sec % self.window_s]
        if slot[0] != sec:
            slot[0] = sec
            slot[1] = [0] * (self.nb + 1)
        slot[1][bucket_index(ms, self.bucket_ms, self.max_ms)] += 1

    def snapshot(self, now: float) -> list[int]:
        sec = int(now)
        out = [0] * (self.nb + 1)
        for s, counts in self._slots:
            if counts is not None and sec - self.window_s < s <= sec:
                out = [a + b for a, b in zip(out, counts)]
        return out

    def count(self, now: float) -> int:
        sec = int(now)
        return sum(
            sum(counts)
            for s, counts in self._slots
            if counts is not None and sec - self.window_s < s <= sec
        )
