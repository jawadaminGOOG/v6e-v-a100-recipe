#!/usr/bin/env bash
# Seed the GCS bucket used by components/vllm-gcs-cache from a running vLLM pod.
#
#   OVERLAY=g4_colo/recipe/overlays/demo bash g4_colo/recipe/scripts/seed-vllm-cache.sh
#   (or set CACHE_BUCKET, CACHE_KEY and MODEL directly; OVERLAY reads them from its settings.yaml)
#
# What it uploads:
#   gs://$CACHE_BUCKET/models/$MODEL/                     model files (safetensors, tokenizer, config), flat
#   gs://$CACHE_BUCKET/vllm-cache/$CACHE_KEY/cache.tar    /hf/vllm_cache + /hf/inductor_cache from the node:
#                                                         torch.compile artifacts and the vLLM startup plan
#
# The cache is only valid for the exact vLLM image, model, GPU type, vLLM args and vLLM env of the pod it is taken
# from: vLLM keys its compile cache on all of them, and a miss costs ~19 s of torch.compile on every new node. After
# any such change: set a new cacheKey in settings.yaml, apply, let vLLM start, and run this script again.
#
# Steps:
#   1. Upload the model if gs://$CACHE_BUCKET/models/$MODEL/config.json does not exist (from the pod's /models copy
#      or its Hugging Face cache).
#   2. The compile cache must come from a vLLM that serves the local /models copy (the path is part of the cache
#      key). If the pod still serves the Hugging Face id (first seeding), the script stops; re-run with RESTART=1 to
#      let it restart the vllm Deployment once (all replicas restart together: strategy Recreate) and wait for Ready.
#   3. Upload cache.tar. Nodes that already hold a cache for this key keep it; new nodes get the new one.
set -euo pipefail
NS=${NS:-inference}
if [[ -n "${OVERLAY:-}" ]]; then
  get() { sed -n "s/^  $1: *\([^ #]*\).*/\1/p" "$OVERLAY/settings.yaml" | head -1; }
  CACHE_BUCKET=${CACHE_BUCKET:-$(get cacheBucket)}
  CACHE_KEY=${CACHE_KEY:-$(get cacheKey)}
  MODEL=${MODEL:-$(get model)}
fi
: "${CACHE_BUCKET:?set CACHE_BUCKET or OVERLAY}"; : "${CACHE_KEY:?set CACHE_KEY or OVERLAY}"; : "${MODEL:?set MODEL or OVERLAY}"
MK=${MODEL//\//_}
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT

pod() {
  kubectl -n "$NS" get pod -l app=vllm -o jsonpath='{range .items[?(@.status.containerStatuses[0].ready==true)]}{.metadata.name}{"\n"}{end}' | head -1
}
POD=${POD:-$(pod)}
[[ -n "$POD" ]] || { echo "no Ready vllm pod in namespace $NS" >&2; exit 1; }
echo "pod $POD, bucket gs://$CACHE_BUCKET, model $MODEL, key $CACHE_KEY"

# 1. Model
if gcloud storage ls "gs://$CACHE_BUCKET/models/$MODEL/config.json" >/dev/null 2>&1; then
  echo "model already in gs://$CACHE_BUCKET/models/$MODEL/"
else
  src=$(kubectl -n "$NS" exec "$POD" -c vllm -- bash -c \
    "if [ -f /models/$MODEL/config.json ]; then echo /models/$MODEL; else ls -d /hf/hub/models--${MODEL//\//--}/snapshots/*/ | head -1; fi")
  echo "uploading model from $POD:$src"
  mkdir -p "$TMP/model"
  kubectl -n "$NS" exec "$POD" -c vllm -- tar -C "$src" -chf - --exclude=.cache-seed . | tar -C "$TMP/model" -xf -
  gcloud storage cp -r "$TMP/model/*" "gs://$CACHE_BUCKET/models/$MODEL/"
fi

# 2. The pod must serve the local copy
if ! kubectl -n "$NS" exec "$POD" -c vllm -- test -f "/models/.cache-seed/model-$MK"; then
  if [[ "${RESTART:-0}" != 1 ]]; then
    echo "$POD serves the Hugging Face id, so its compile cache does not match pods that serve /models/$MODEL." >&2
    echo "Re-run with RESTART=1 to restart the vllm Deployment once and seed from the restarted pod." >&2
    exit 2
  fi
  kubectl -n "$NS" rollout restart deploy/vllm
  kubectl -n "$NS" rollout status deploy/vllm --timeout=15m
  POD=$(pod)
  kubectl -n "$NS" exec "$POD" -c vllm -- test -f "/models/.cache-seed/model-$MK" \
    || { echo "$POD still does not serve /models/$MODEL; check: kubectl -n $NS logs $POD -c cache-seed" >&2; exit 1; }
fi

# 3. Startup cache
kubectl -n "$NS" exec "$POD" -c vllm -- bash -c \
  'cd /hf && tar -cf - $(ls -d vllm_cache inductor_cache 2>/dev/null)' > "$TMP/cache.tar"
gcloud storage cp "$TMP/cache.tar" "gs://$CACHE_BUCKET/vllm-cache/$CACHE_KEY/cache.tar"
gcloud storage ls -l "gs://$CACHE_BUCKET/vllm-cache/$CACHE_KEY/cache.tar"
