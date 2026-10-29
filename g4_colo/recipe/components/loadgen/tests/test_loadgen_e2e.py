"""Runs loadgen against fake vLLM servers (no k8s) and checks /stats and /rate."""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(__file__)
ROOT = os.path.join(HERE, "..")


def _port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _get(url, data=None):
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.read()


def _wait(url, t=10):
    end = time.time() + t
    while time.time() < end:
        try:
            return _get(url)
        except OSError:
            time.sleep(0.1)
    raise TimeoutError(url)


def test_loadgen_against_fake_vllm():
    p1, p2, lp = _port(), _port(), _port()
    fakes = [subprocess.Popen([sys.executable, os.path.join(HERE, "fake_vllm.py"), str(p), "20"]) for p in (p1, p2)]
    env = dict(os.environ, VLLM_TARGETS=f"http://127.0.0.1:{p1},http://127.0.0.1:{p2}",
               PORT=str(lp), INITIAL_RATE="0", REPLICAS="2")
    lg = subprocess.Popen([sys.executable, os.path.join(ROOT, "loadgen.py")], env=env, cwd=ROOT)
    try:
        time.sleep(1.0)
        _wait(f"http://127.0.0.1:{lp}/healthz")
        r = json.loads(_get(f"http://127.0.0.1:{lp}/rate", json.dumps({"rate": 80}).encode()))
        assert r["rate_total"] == 80 and r["rate_local"] == 40
        assert json.loads(_get(f"http://127.0.0.1:{lp}/rate?rate=100"))["rate_local"] == 50
        time.sleep(4)
        s = json.loads(_get(f"http://127.0.0.1:{lp}/stats"))
        assert s["window_s"] == 30 and s["bucket_ms"] == 2 and s["rate"] == 100
        assert len(s["per_pod"]) == 2
        total_ok = 0
        for pod in s["per_pod"].values():
            assert len(pod["ttft_hist"]) == 1001 and len(pod["e2e_hist"]) == 1001
            assert pod["err"] == 0
            assert 20 <= pod["ttft_p50_ms"] <= 60
            total_ok += pod["ok"]
        assert 120 <= total_ok <= 280  # ~50 rps * ~4 s
        m = _get(f"http://127.0.0.1:{lp}/metrics").decode()
        assert "loadgen_ttft_seconds_bucket" in m and 'loadgen_requests_total{code="200"}' in m
    finally:
        lg.terminate()
        lg.wait(timeout=10)
        for f in fakes:
            f.terminate()
            f.wait(timeout=5)
