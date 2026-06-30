#!/usr/bin/env bash
# TPU Docker Template (Emulating GKE v6e-1t node slices)
#
# Variables to replace:
#   <HF_TOKEN>      - Hugging Face Read Access Token
#   <MODEL_ID>      - Hugging Face Model ID (e.g. Qwen/Qwen3-1.7B-Base)
#   <PORT>          - Port to expose locally (e.g. 8000)
#   <PADDING_GAP>   - JAX bucket padding gap (e.g. 256 for baseline, 64 for optimized)
#   <BLOCK_SIZE>    - KV Cache Block Size (e.g. 256 for baseline, 16 for GPU-parity)

docker run -d \
  --name vllm-tpu-server \
  --net=host \
  --shm-size=16g \
  --privileged \
  --cpus=30 \
  --memory=128g \
  -e TPU_VISIBLE_DEVICES=0 \
  -e HF_TOKEN=<HF_TOKEN> \
  -e VLLM_TPU_BUCKET_PADDING_GAP=<PADDING_GAP> \
  -e MAX_PROMPT_LEN=2048 \
  -e VLLM_XLA_CACHE_PATH=/tmp/xla-cache \
  -e JAX_COMPILATION_CACHE_DIR=/tmp/xla-cache/jax \
  -e JAX_LOG_COMPILES=1 \
  -v /tmp/xla-cache:/tmp/xla-cache \
  -v ~/.cache/huggingface:/root/.cache/huggingface \
  vllm/vllm-tpu:latest \
  --model <MODEL_ID> \
  --port <PORT> \
  --max-model-len 2048 \
  --block-size <BLOCK_SIZE> \
  --max-num-seqs 256
