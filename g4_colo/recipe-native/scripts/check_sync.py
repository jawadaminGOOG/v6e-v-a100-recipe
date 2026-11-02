#!/usr/bin/env python3
"""Fail if a manifest the native recipe shares with recipe/ has drifted.

Kustomize will not load files from outside an overlay's root, so recipe-native/ keeps copies of the manifests
that must stay identical to recipe/ (the GPU workload, its load generator, the metrics adapter, the kubelet
config). This check keeps the copies honest; the comparison between the two recipes depends on it.

    python3 g4_colo/recipe-native/scripts/check_sync.py      # exit 0 = in sync
"""
import filecmp
import sys
from pathlib import Path

G4 = Path(__file__).resolve().parents[2]
SHARED = ["base/vllm.yaml", "base/loadgen.yaml", "base/custom-metrics-adapter.yaml", "infra/kubelet-config.yaml"]


def main() -> int:
    bad = [f for f in SHARED if not filecmp.cmp(G4 / "recipe" / f, G4 / "recipe-native" / f, shallow=False)]
    for f in bad:
        print(f"DRIFT: recipe-native/{f} != recipe/{f}  (cp g4_colo/recipe/{f} g4_colo/recipe-native/{f})")
    print("PASS: shared manifests in sync" if not bad else f"FAIL: {len(bad)} file(s) drifted")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
