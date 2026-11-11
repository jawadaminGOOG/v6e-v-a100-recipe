#!/usr/bin/env python3
"""Liveness check for vLLM-TPU: fail when the engine holds requests but stops producing tokens.

vLLM's /health only proves the API server answers; on 10 Nov 2026 the TPU engine hung with 1 request "running" and
frozen counters while /health stayed 200, so the pod kept receiving traffic. This probe reads /metrics and keeps the
previous sample in /tmp: if requests were in flight at both this and the previous probe and
vllm:generation_tokens_total has not moved in between, it counts a stall. kubelet's failureThreshold turns consecutive stalls into a restart.
Exit 0 = healthy (idle, or progressing); exit 1 = stalled.
"""
import json
import os
import sys
import urllib.request

STATE = "/tmp/engine_progress.json"
URL = "http://127.0.0.1:%s/metrics" % os.environ.get("VLLM_METRICS_PORT", "8000")


def metric(text, name):
    total, seen = 0.0, False
    for line in text.splitlines():
        if line.startswith(name) and (len(line) == len(name) or line[len(name)] in "{ "):
            total += float(line.rsplit(" ", 1)[1])
            seen = True
    return total if seen else None


def main():
    try:
        text = urllib.request.urlopen(URL, timeout=5).read().decode()
    except OSError:
        return 0  # the API server is down: readiness handles it; liveness only targets the silent hang
    busy = (metric(text, "vllm:num_requests_running") or 0) + (metric(text, "vllm:num_requests_waiting") or 0)
    tokens = metric(text, "vllm:generation_tokens_total")
    try:
        prev = json.load(open(STATE))
    except (OSError, ValueError):
        prev = {}
    json.dump({"tokens": tokens, "busy": busy}, open(STATE, "w"))
    # Busy now AND at the previous probe, with no new token in between: a request that arrived just before this
    # probe has not had time to generate yet, so it does not count.
    if busy > 0 and prev.get("busy", 0) > 0 and tokens is not None and prev.get("tokens") == tokens:
        print(f"stalled: {busy:.0f} requests in flight, generation_tokens_total unchanged at {tokens:.0f}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
