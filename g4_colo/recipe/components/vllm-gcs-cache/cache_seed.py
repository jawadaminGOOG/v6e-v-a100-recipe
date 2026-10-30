"""cache-seed init container: copy the model and the vLLM startup cache from GCS onto the node before vLLM starts.

Runs in the vLLM image (google-cloud-storage is already installed), so it adds no image pull. On a node that already
has the files (marker per key) it exits at once. It never fails the Pod: on any error vLLM starts the slow way.

  gs://$CACHE_BUCKET/models/$MODEL/...                -> $DEST_DIR/$MODEL/            (safetensors, tokenizer, config)
  gs://$CACHE_BUCKET/vllm-cache/$CACHE_KEY/cache.tar  -> $CACHE_ROOT/                 (vllm_cache: torch.compile,
                                                                                       startup plan, ...)
"""
import io
import os
import sys
import tarfile
import time

t0 = time.time()
bucket_name = os.environ["CACHE_BUCKET"]
model = os.environ.get("MODEL", "")
key = os.environ.get("CACHE_KEY", "")
root = os.environ.get("CACHE_ROOT", "/hf")
# Model files go to DEST_DIR (vllm-patch.yaml mounts a memory-backed emptyDir there). Writing 3+ GiB to a newly
# created node's boot disk delayed the vLLM container's start by ~58 s; RAM avoids it.
model_dir = os.environ.get("DEST_DIR", os.path.join(root, "models"))


def marker(kind, name):
    base = model_dir if kind == "model" else root
    return os.path.join(base, ".cache-seed", f"{kind}-{name.replace('/', '_')}")


def done(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(f"{time.time()}\n")


try:
    from google.cloud import storage
    from google.cloud.storage import transfer_manager as tm

    bucket = storage.Client().bucket(bucket_name)
    if model and not os.path.exists(marker("model", model)):
        prefix = f"models/{model}/"
        dest = os.path.join(model_dir, model)
        os.makedirs(dest, exist_ok=True)
        n = 0
        for blob in bucket.list_blobs(prefix=prefix):
            rel = blob.name[len(prefix):]
            if not rel or rel.endswith("/"):
                continue
            path = os.path.join(dest, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            if blob.size and blob.size > 256 * 2**20:
                tm.download_chunks_concurrently(blob, path, chunk_size=64 * 2**20, max_workers=16)
            else:
                blob.download_to_filename(path)
            n += blob.size or 0
        done(marker("model", model))
        print(f"cache-seed: model {model} {n / 2**30:.2f} GiB in {time.time() - t0:.1f}s", flush=True)
    t1 = time.time()
    if key and not os.path.exists(marker("cache", key)):
        data = bucket.blob(f"vllm-cache/{key}/cache.tar").download_as_bytes()
        with tarfile.open(fileobj=io.BytesIO(data)) as tf:
            tf.extractall(root, filter="data")
        done(marker("cache", key))
        print(f"cache-seed: cache {key} {len(data) / 2**20:.1f} MiB in {time.time() - t1:.1f}s", flush=True)
except Exception as e:  # noqa: BLE001 - a missing cache must never block serving
    print(f"cache-seed: skipped ({type(e).__name__}: {e}); vLLM will start the slow way", flush=True)
print(f"cache-seed: total {time.time() - t0:.1f}s", flush=True)
sys.exit(0)
