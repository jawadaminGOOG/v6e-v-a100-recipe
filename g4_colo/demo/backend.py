"""Backend selection shared by the web app, scenarios and the CLI.

One process can show several clusters. `DEMO_CLUSTERS` is a JSON list, one entry per cluster:

    [{"id": "controller", "label": "With colo-controller", "recipe": "controller",
      "context": "gke_northam-ce-mlai-tpu_us-west8_g4cluster-jawadamin-us-west8-0726"},
     {"id": "native", "label": "Native K8s only", "recipe": "native",
      "context": "gke_northam-ce-mlai-tpu_us-west8_g4cluster-jawadamin-us-west8-1126"},
     {"id": "tpu", "label": "TPU v6e · native", "recipe": "native", "accelerator": "tpu-v6e",
      "context": "gke_northam-ce-mlai-tpu_us-east5_v6ecluster-jawadamin-us-east5-1126",
      "name": "v6ecluster-jawadamin-us-east5-1126", "region": "us-east5"}]

Each entry takes either `context` (kubeconfig) or `endpoint` + `ca_cert` (base64 PEM; Cloud Run with ADC), plus
optional `name`, `region`, `project` for the footer. `recipe` is `controller` (g4_colo/recipe/) or `native`
(g4_colo/recipe-native/). Optional `accelerator` is the accelerator host the recipe runs on: `g4` (default;
g4-standard-48, 1 RTX PRO 6000) or `tpu-v6e` (ct6e-standard-1t, 1 TPU v6e chip, 44 vCPU). It sets the node price
(DEMO_PRICE_G4_HR / DEMO_PRICE_V6E_HR), the host vCPU fallback and every G4/GPU word the UI shows (state.ACCELERATORS);
the role classification is unchanged (the TPU node pool keeps compute-class g4-gpu / colo.role=g4). In DEMO_MOCK=1 the
simulator emulates that accelerator. The first entry is the default.

Without DEMO_CLUSTERS: one `controller` cluster from DEMO_KUBE_CONTEXT, or GKE_ENDPOINT/GKE_CA_CERT, or the current
kubeconfig context / in-cluster account (the pre-multi-cluster behaviour). DEMO_MOCK=1 simulates every cluster;
with no DEMO_CLUSTERS it offers both recipes.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from state import ACCELERATORS, CLUSTER, DEFAULT_ACCELERATOR, RECIPES, accel_profile, build_state

log = logging.getLogger("demo.backend")

DEFAULT_LABELS = {"controller": "With colo-controller", "native": "Native K8s only"}
DEFAULT_NAMES = {"controller": "g4cluster-jawadamin-us-west8-0726", "native": "g4cluster-jawadamin-us-west8-1126"}
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

_backends: dict[str, Any] | None = None
_specs: list[dict] | None = None


def _name_from_context(ctx: str | None) -> str | None:
    # gke_<project>_<location>_<cluster>
    if ctx and ctx.startswith("gke_") and ctx.count("_") >= 3:
        return ctx.split("_", 3)[3]
    return None


def _normalize(entry: dict, idx: int) -> dict:
    if not isinstance(entry, dict):
        raise ValueError(f"DEMO_CLUSTERS[{idx}] must be an object")
    recipe = str(entry.get("recipe") or "controller")
    if recipe not in RECIPES:
        raise ValueError(f"DEMO_CLUSTERS[{idx}].recipe must be one of {RECIPES}, got {recipe!r}")
    cid = str(entry.get("id") or recipe)
    if not _ID_RE.match(cid):
        raise ValueError(f"DEMO_CLUSTERS[{idx}].id {cid!r} must match {_ID_RE.pattern}")
    ctx = entry.get("context") or None
    endpoint = entry.get("endpoint") or None
    if ctx and endpoint:
        raise ValueError(f"DEMO_CLUSTERS[{idx}] ({cid}): set context or endpoint, not both")
    if endpoint and not (entry.get("ca_cert") or os.environ.get("GKE_CA_CERT")):
        raise ValueError(f"DEMO_CLUSTERS[{idx}] ({cid}): endpoint needs ca_cert (base64 PEM)")
    accel = str(entry.get("accelerator") or DEFAULT_ACCELERATOR)
    if accel not in ACCELERATORS:
        raise ValueError(f"DEMO_CLUSTERS[{idx}] ({cid}).accelerator must be one of {list(ACCELERATORS)}, got {accel!r}")
    return {
        "id": cid,
        "label": str(entry.get("label") or DEFAULT_LABELS.get(recipe, cid)),
        "recipe": recipe,
        "accelerator": accel,
        "context": ctx,
        "endpoint": endpoint,
        "ca_cert": entry.get("ca_cert") or None,
        "name": entry.get("name") or _name_from_context(ctx) or (CLUSTER["name"] if idx == 0 and recipe == "controller"
                                                                  else DEFAULT_NAMES.get(recipe, cid)),
        "region": entry.get("region") or CLUSTER["region"],
        "project": entry.get("project") or CLUSTER["project"],
    }


def parse_clusters(env: dict | None = None) -> list[dict]:
    """Cluster specs from the environment (see module docstring). Raises ValueError on a bad DEMO_CLUSTERS."""
    env = os.environ if env is None else env
    raw = (env.get("DEMO_CLUSTERS") or "").strip()
    if raw:
        try:
            items = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ValueError(f"DEMO_CLUSTERS is not valid JSON: {e}") from e
        if not isinstance(items, list) or not items:
            raise ValueError("DEMO_CLUSTERS must be a non-empty JSON list")
        specs = [_normalize(e, i) for i, e in enumerate(items)]
        ids = [s["id"] for s in specs]
        if len(set(ids)) != len(ids):
            raise ValueError(f"DEMO_CLUSTERS ids must be unique, got {ids}")
        return specs
    if env.get("DEMO_MOCK", "0") == "1":
        return [_normalize({"id": "controller", "recipe": "controller"}, 0),
                _normalize({"id": "native", "recipe": "native"}, 1)]
    # Single-cluster fallback: exactly the pre-multi-cluster behaviour.
    ctx = env.get("DEMO_KUBE_CONTEXT") or None
    spec = _normalize({"id": "controller", "recipe": "controller", "context": ctx, "name": CLUSTER["name"]}, 0)
    spec["legacy"] = True  # credential chain: kubeconfig -> in-cluster -> GKE_ENDPOINT/GKE_CA_CERT
    return [spec]


def cluster_info(spec: dict) -> dict:
    """What /api/state and /api/clusters expose (never the endpoint or CA)."""
    return {"id": spec["id"], "label": spec["label"], "recipe": spec["recipe"], "name": spec["name"],
            "region": spec["region"], "project": spec["project"],
            "accelerator": spec.get("accelerator") or DEFAULT_ACCELERATOR}


def accelerator_of(spec: dict) -> dict:
    """Display profile of a cluster spec's accelerator host (state.ACCELERATORS)."""
    return accel_profile(spec.get("accelerator"))


class UnavailableBackend:
    """Stands in for a cluster whose credentials could not be loaded, so the other clusters still work."""

    mode = "live"
    fast_scale = False

    def __init__(self, spec: dict, error: str):
        self.recipe = spec["recipe"]
        self.cluster_id = spec["id"]
        self.cluster_info = cluster_info(spec)
        self.error = error

    async def state(self) -> dict:
        return build_state({"nodes": [], "pods": [], "errors": [f"cluster config ({self.cluster_id}): {self.error}"]},
                           mode=self.mode, recipe=self.recipe, cluster=self.cluster_info)

    async def _fail(self, *_a, **_k):
        raise RuntimeError(f"cluster {self.cluster_id} is not configured: {self.error}")

    set_gpu_rate = set_demand = set_guard = _fail

    async def baseline(self):
        return await self._fail()


def make_backend(spec: dict, mock: bool | None = None):
    mock = os.environ.get("DEMO_MOCK", "0") == "1" if mock is None else mock
    info = cluster_info(spec)
    if mock:
        from mock import MockBackend, NativeMockBackend

        cls = NativeMockBackend if spec["recipe"] == "native" else MockBackend
        return cls(cluster_id=spec["id"], cluster_info=info)
    from cluster import LiveBackend, NativeLiveBackend

    cls = NativeLiveBackend if spec["recipe"] == "native" else LiveBackend
    # An explicit DEMO_CLUSTERS context is strict (never falls through to another cluster's credentials); the
    # legacy single-cluster spec and an entry with neither context nor endpoint use the old fallback chain.
    strict = bool(spec.get("context")) and not spec.get("legacy")
    return cls(context=spec.get("context"), endpoint=spec.get("endpoint"), ca_cert=spec.get("ca_cert"),
               strict=strict, cluster_id=spec["id"], cluster_info=info)


def get_cluster_specs() -> list[dict]:
    global _specs
    if _specs is None:
        _specs = parse_clusters()
    return _specs


def get_backends() -> dict[str, Any]:
    """id -> backend, in DEMO_CLUSTERS order. A cluster that fails to load becomes an UnavailableBackend."""
    global _backends
    if _backends is None:
        out: dict[str, Any] = {}
        specs = get_cluster_specs()
        for spec in specs:
            try:
                out[spec["id"]] = make_backend(spec)
            except Exception as e:  # noqa: BLE001 - keep the other clusters usable
                if len(specs) == 1:
                    raise
                log.warning("cluster %s unavailable: %s: %s", spec["id"], type(e).__name__, e)
                out[spec["id"]] = UnavailableBackend(spec, f"{type(e).__name__}: {e}")
        _backends = out
    return _backends


def get_backend(cluster_id: str | None = None):
    """The backend for `cluster_id` (default: the first cluster). KeyError for an unknown id."""
    b = get_backends()
    return b[cluster_id] if cluster_id else next(iter(b.values()))
