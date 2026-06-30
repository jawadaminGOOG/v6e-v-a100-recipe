#!/usr/bin/env bash
# GPU Docker Template (Emulating GKE A100 node slices)
#
# Variables to replace:
#   <HF_TOKEN>      - Hugging Face Read Access Token
#   <MODEL_ID>      - Hugging Face Model ID (e.g. Qwen/Qwen3-1.7B-Base)
#   <PORT>          - Port to expose locally (e.g. 8000)
#   <KV_DTYPE>      - KV Cache data type (auto for FP16, fp8 for FP8 KV cache)

docker run -d \
  --name vllm-gpu-server \
  --gpus '"device=0"' \
  --shm-size=16g \
  --cpus=22 \
  --memory=100g \
  -e HF_TOKEN=<HF_TOKEN> \
  -v ~/.cache/huggingface:/root/.cache/huggingface \
  vllm/vllm-openai:latest \
  --model <MODEL_ID> \
  --port <PORT> \
  --max-model-len 4096 \
  --gpu-memory-utilization 0.9 \
  --enable-prefix-caching \
  --tensor-parallel-size 1 \
  --kv-cache-dtype <KV_DTYPE>
