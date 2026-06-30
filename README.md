# vLLM Benchmarking Recipes: TPU v6e vs GPU A100

This document contains recipes for benchmarking vLLM on Google Cloud TPUs (v6e) and GPUs (A100-80GB), strictly adhering to resource limits and precision configurations.

---

## 1. System Configurations

### 1.1 TPU v6e Node Pool Emulation (`v6e1t`)
To mimic a GKE `v6e1t` node pool (which allocates 1 TPU chip + a portion of host CPU/Memory), we restrict the Docker container running on a `v6e-4` host.
*   **Host VM:** `v6e-4` (180 CPUs, 708GB RAM)
*   **Target Emulation:** `v6e1t` (30 CPUs, 128GB RAM, 1 TPU Chip)
*   **Docker Limits:** `--cpus=30`, `--memory=128g`

### 1.2 GPU A100 Pool Emulation
To match the README prescription for A100 GKE node pools:
*   **Host VM:** A100 80GB (24 CPUs, 334GB RAM)
*   **Docker Limits:** `--cpus=22`, `--memory=100g`

### 1.3 Model Weights & Caching
All models are pulled dynamically from the Hugging Face Hub at server startup. To avoid network overhead and rate limits during repeated runs, the Hugging Face cache directory is mapped from the host VM to the Docker container:
*   **Hugging Face Repo IDs:**
    *   Base model: `Qwen/Qwen3-1.7B-Base`
    *   Quantized model: `Qwen/Qwen3-1.7B-FP8`
*   **Docker Volume Mount:** `-v ~/.cache/huggingface:/root/.cache/huggingface` maps the host user's HF cache to the container, ensuring weights are only downloaded once.

---

## 2. TPU Benchmarking Recipes

### 2.1 TPU BF16 (Baseline / Limited)

This recipe runs the base model in BF16 precision with strict CPU and Memory limits.

#### Step 1: Start vLLM TPU Server
Run this command on the TPU VM host:
```bash
sudo docker run -d --name vllm-tpu-server \
  --network host \
  --privileged \
  --cpus=30 \
  --memory=128g \
  -e TPU_VISIBLE_DEVICES=0 \
  -e VLLM_TPU_BUCKET_PADDING_GAP=256 \
  -e MAX_PROMPT_LEN=2048 \
  -e VLLM_XLA_CACHE_PATH=/tmp/xla-cache \
  -e JAX_COMPILATION_CACHE_DIR=/tmp/xla-cache/jax \
  -e JAX_LOG_COMPILES=1 \
  -v ~/.cache/huggingface:/root/.cache/huggingface \
  -v /tmp/xla-cache:/tmp/xla-cache \
  vllm/vllm-tpu:latest \
  python3 -m vllm.entrypoints.openai.api_server \
  --model Qwen/Qwen3-1.7B-Base \
  --port 10010 \
  --max-model-len 4096 \
  --gpu-memory-utilization 0.9 \
  --enable-prefix-caching \
  --tensor-parallel-size 1
```
*Note: We mount `/tmp/xla-cache` to preserve JAX compilation cache across container restarts.*

#### Step 2: Verify Server Readiness
Monitor logs until the warm-up pass finishes and the API server starts:
```bash
sudo docker logs vllm-tpu-server --tail 100
# Verify via curl:
curl -s http://localhost:10010/v1/models
```

#### Step 3: Run Benchmark
Run the client benchmark script:
```bash
python3 benchmark.py \
  --input-file ~/synthetic_yelp.xlsx \
  --output-dir ~/output \
  --url http://localhost:10010/v1/completions \
  --system V6E-TPU-BF16-Limited \
  --semaphores 100,150,200,250
```

---

### 2.2 TPU FP8 (Quantized / Limited)

This recipe runs a pre-quantized FP8 model. Note that JAX vLLM currently requires pre-quantized checkpoints and does not support on-the-fly dynamic FP8 quantization for BF16 models.

#### Step 1: Start vLLM TPU Server
```bash
sudo docker run -d --name vllm-tpu-server \
  --network host \
  --privileged \
  --cpus=30 \
  --memory=128g \
  -e TPU_VISIBLE_DEVICES=0 \
  -e VLLM_TPU_BUCKET_PADDING_GAP=256 \
  -e MAX_PROMPT_LEN=2048 \
  -e VLLM_XLA_CACHE_PATH=/tmp/xla-cache \
  -e JAX_COMPILATION_CACHE_DIR=/tmp/xla-cache/jax \
  -e JAX_LOG_COMPILES=1 \
  -v ~/.cache/huggingface:/root/.cache/huggingface \
  -v /tmp/xla-cache:/tmp/xla-cache \
  vllm/vllm-tpu:latest \
  python3 -m vllm.entrypoints.openai.api_server \
  --model Qwen/Qwen3-1.7B-FP8 \
  --port 10010 \
  --max-model-len 4096 \
  --gpu-memory-utilization 0.9 \
  --enable-prefix-caching \
  --tensor-parallel-size 1
```

#### Step 2: Run Benchmark
You must explicitly specify the tokenizer/model name using `--tokenizer` to match the server's served model name:
```bash
python3 benchmark.py \
  --input-file ~/synthetic_yelp.xlsx \
  --output-dir ~/output \
  --url http://localhost:10010/v1/completions \
  --system V6E-TPU-FP8-Limited \
  --semaphores 100,150,200,250 \
  --tokenizer Qwen/Qwen3-1.7B-FP8
```

---

## 3. GPU Benchmarking Recipes

### 3.1 GPU FP16 (Baseline / Limited)

#### Step 1: Start vLLM GPU Server
```bash
sudo docker run -d --name vllm-gpu-server \
  --gpus 'device=0' \
  --network host \
  --shm-size 16G \
  --cpus=22 \
  --memory=100g \
  -v ~/.cache/huggingface:/root/.cache/huggingface \
  vllm/vllm-openai:latest \
  --model Qwen/Qwen3-1.7B-Base \
  --port 10010 \
  --max-model-len 4096 \
  --gpu-memory-utilization 0.9 \
  --enable-prefix-caching \
  --tensor-parallel-size 1
```

#### Step 2: Run Benchmark
```bash
python3 benchmark.py \
  --input-file ~/synthetic_yelp.xlsx \
  --output-dir ~/output \
  --url http://localhost:10010/v1/completions \
  --system A100-GPU-FP16-Limited \
  --semaphores 100,150,200,250
```

---

### 3.2 GPU FP8 (Quantized / Limited)

*Hardware Warning: A100 GPUs do not have native FP8 Tensor Cores. vLLM uses Marlin kernels for weight-only FP8 compression (dequantizing to FP16 at runtime). This improves memory bandwidth but does not provide FP8 compute acceleration.*

#### Step 1: Start vLLM GPU Server
```bash
sudo docker run -d --name vllm-gpu-server \
  --gpus 'device=0' \
  --network host \
  --shm-size 16G \
  --cpus=22 \
  --memory=100g \
  -v ~/.cache/huggingface:/root/.cache/huggingface \
  vllm/vllm-openai:latest \
  --model Qwen/Qwen3-1.7B-FP8 \
  --port 10010 \
  --max-model-len 4096 \
  --gpu-memory-utilization 0.9 \
  --enable-prefix-caching \
  --tensor-parallel-size 1
```

#### Step 2: Run Benchmark
```bash
python3 benchmark.py \
  --input-file ~/synthetic_yelp.xlsx \
  --output-dir ~/output \
  --url http://localhost:10010/v1/completions \
  --system A100-GPU-FP8-Limited \
  --semaphores 100,150,200,250 \
  --tokenizer Qwen/Qwen3-1.7B-FP8
```

---

## 4. Execution Data Summary

### 4.1 Peak Load Comparison (Concurrency 250)
The following table summarizes the performance at peak tested load (concurrency = 250).

| Metric | TPU BF16 Limited | GPU FP16 Limited | TPU FP8 Limited | GPU FP8 Limited |
| :--- | :---: | :---: | :---: | :---: |
| **Precision** | BF16 | FP16 | FP8 (Weights+KV) | FP8 (Weight-only Marlin) |
| **RPS** | 368.52 | 402.66 | 274.24 | 339.99 |
| **Latency p50 (s)** | 0.654 | 0.609 | 0.890 | 0.708 |
| **Latency p99 (s)** | 1.100 | 0.922 | 1.329 | 1.108 |
| **Avg Gen Tokens** | 11.04 | 11.05 | 16.00 (Maxed) | 16.00 (Maxed) |
| **Token Throughput (TPS)**| **4,068** | **4,448** | **4,387** | **5,440** |
| **TPS Speedup vs FP16** | - | - | **+7.8%** | **+22.3%** |

### 4.2 Concurrency Sweep scaling
The tables below show scaling from Concurrency 100 to 250.

#### 4.2.1 Throughput (RPS) scaling
| Configuration | Concurrency 100 | Concurrency 150 | Concurrency 200 | Concurrency 250 |
| :--- | :---: | :---: | :---: | :---: |
| **TPU BF16** | 332.44 | 360.16 | 365.77 | 368.52 |
| **GPU FP16** | 343.10 | 377.86 | 400.65 | 402.66 |
| **TPU FP8** | 241.41 | 266.84 | 272.61 | 274.24 |
| **GPU FP8** | 269.19 | 290.22 | 323.28 | 339.99 |

#### 4.2.2 Tail Latency p99 (seconds) scaling
| Configuration | Concurrency 100 | Concurrency 150 | Concurrency 200 | Concurrency 250 |
| :--- | :---: | :---: | :---: | :---: |
| **TPU BF16** | 0.559 | 0.610 | 0.789 | 1.100 |
| **GPU FP16** | 0.428 | 0.595 | 0.714 | 0.922 |
| **TPU FP8** | 0.672 | 0.740 | 1.032 | 1.329 |
| **GPU FP8** | 0.538 | 0.683 | 0.882 | 1.108 |

### Key Observations:
1.  **Strict Resource Adherence:** Restricting CPU and Memory to match node pool specs (30 CPUs / 128GB for TPU, 22 CPUs / 100GB for GPU) resulted in **<0.5% performance difference** compared to baseline runs with unlimited container access.
2.  **FP8 Model Verbosity:** The `Qwen3-1.7B-FP8` model consistently hit the `max_tokens=16` limit on both devices, generating 45% more tokens than the base model. This artificially lowered the RPS metrics.
3.  **Token Throughput (TPS) Comparison:** 
    *   TPU FP8 shows a **7.8%** improvement in total token throughput over BF16.
    *   GPU FP8 shows a **22.2%** improvement over FP16, despite A100 dequantizing weights to FP16 at runtime.

---

## 5. Optimization Experiments

### 5.1 GPU FP8 KV Cache Activation
*   **Goal:** Evaluate the impact of quantizing the KV cache to FP8 on GPU A100.
*   **Config:**
    *   Server startup command includes `--kv-cache-dtype fp8` (in addition to `--model Qwen/Qwen3-1.7B-FP8`).
    *   Docker limits: `--cpus=22`, `--memory=100g`.
*   **Performance (Semaphore 250):**
    *   **RPS:** 317.51 (vs 339.99 RPS without FP8 KV cache).
    *   **Token Throughput (TPS):** 5,080 TPS (vs 5,440 TPS without FP8 KV cache).
    *   **Latency p50 / p99:** 0.773s / 1.169s (vs 0.708s / 1.108s without FP8 KV cache).
*   **Observations:**
    *   **Performance Degradation:** Enabling FP8 KV cache resulted in a **~6.6% throughput drop** and increased latency.
    *   **Hardware Limitation:** Since A100 lacks native FP8 Tensor Cores, vLLM must dequantize the FP8 KV cache to FP16/BF16 at runtime to perform attention. The overhead of these dequantization kernels exceeds the HBM bandwidth savings.

### 5.2 TPU Bucket Padding Gap Reduction
*   **Goal:** Reduce padding waste in the JAX compiler by narrowing the bucket gap.
*   **Config:**
    *   Server startup includes environment variable `-e VLLM_TPU_BUCKET_PADDING_GAP=64` (default is 256).
    *   Model: `Qwen/Qwen3-1.7B-Base` (BF16).
    *   Docker limits: `--cpus=30`, `--memory=128g`.
*   **Performance (Semaphore 250):**
    *   **RPS:** 368.51 (vs 368.52 RPS with Gap 256).
    *   **Latency p50 / p99:** 0.655s / 1.026s (vs 0.654s / 1.100s with Gap 256).
    *   **Tail Latency (p99) Improvement:** **~6.7% reduction** in p99 latency.
*   **Observations:**
    *   **Padding Waste Analysis:** For the `synthetic_yelp` dataset (average prompt length ~591 tokens), a gap of 256 forces prompts to pad to 768 (44% token waste). A gap of 64 allows padding to 640 (reducing waste to 15%).
    *   **Throughput vs Latency:** The reduction in padding waste did not increase peak RPS (suggesting the bottleneck at Semaphore 250 is decode-bound or system-overhead bound). However, it successfully reduced tail latency (p99) by making prefill execution more efficient.
    *   **Compilation Overhead:** Changing the gap to 64 increased the number of compiled shapes from 12 to 34. This increased initial server startup/warmup time by ~3-4x (compiling 26 new shapes). However, compilation cache mounted at `/tmp/xla-cache` successfully mitigated this on subsequent restarts.

### 5.3 TPU KV Cache Block Size Reduction
*   **Goal:** Evaluate the impact of reducing the KV cache block size on TPU to improve prefix cache hit rate.
*   **Config:**
    *   Server startup includes argument `--block-size 16` (default is 256).
    *   Model: `Qwen/Qwen3-1.7B-Base` (BF16).
    *   Docker limits: `--cpus=30`, `--memory=128g`.
*   **Performance (Semaphore 250):**
    *   **RPS:** 320.43 (vs 368.52 RPS with Block Size 256) | **~13% slowdown**.
    *   **Latency p50 / p99:** 0.752s / 1.130s (vs 0.654s / 1.100s with Block Size 256).
    *   **Cache Hit Rate:** **89.78%** (vs 86.51% with Block Size 256).
*   **Observations:**
    *   **Cache Hit Rate Improvement:** As expected, reducing block size to 16 allowed the TPU to cache 528 tokens of the prefix (matching the GPU) instead of being restricted to 512 tokens (multiples of 256). This increased the cache hit rate by **~3.2%**.
    *   **Throughput Penalty:** Despite the higher cache hit rate, overall throughput **dropped by 13%**. TPU hardware Tensor Cores are highly optimized for larger tile operations, and a small block size of 16 leads to inefficient memory access layouts and higher scheduling overhead in JAX PagedAttention.
    *   **Recommendation:** Keep block size at **256** (or at least 128) on TPUs. The minor cache hit rate gains from smaller block sizes are heavily outweighed by the hardware execution inefficiency.
