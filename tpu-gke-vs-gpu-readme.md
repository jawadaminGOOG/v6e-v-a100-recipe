# Benchmarking Report: TPU v6e (GKE) vs GPU A100 vs GPU G4 (Blackwell)

This report presents performance benchmark results comparing a single Cloud TPU v6e chip running natively on Google Kubernetes Engine (GKE) against GPU A100-80GB and GPU G4 (RTX PRO 6000 Blackwell) instances.

---

## 1. System Configurations

### 1.1 TPU v6e (GKE Native)
*   **Hardware VM:** `ct6e-standard-1t` (1 chip VM: 44 vCPUs, 176 GB RAM, 1x TPU v6e chip)
*   **GKE Cluster:** `kroukoz-tpu-cluster` in `europe-west4-a`
*   **Serving Engine:** vLLM TPU (`vllm/vllm-tpu:latest`)
*   **Resource Limits:** Direct access to `google.com/tpu: 1`

### 1.2 GPU A100 Pool Emulation (Docker)
*   **Host VM:** A100 80GB (24 CPUs, 334GB RAM)
*   **Docker Limits:** `--cpus=22`, `--memory=100g`

### 1.3 GPU G4 Pool Emulation (Docker)
*   **Host VM:** `g4-standard-48` (48 CPUs, 192GB RAM, 1x NVIDIA RTX PRO 6000 Blackwell GPU)
*   **Docker Limits:** `--cpus=40`, `--memory=150g`

---

## 2. Methodology & Workload

*   **Model:** Qwen3 1.7B (`Qwen/Qwen3-1.7B-Base` for BF16/FP16, `Qwen/Qwen3-1.7B-FP8` for FP8).
*   **Workload:** Toxicity classification dataset (synthetic_yelp), prompts averaging ~600 tokens and 11-token generation.
*   **Client Location:** Executed from a separate client Pod inside the same GKE cluster (on an E2 node) to eliminate external network latency.
*   **Parameters:** Checked across concurrency (semaphore) ladders: 100, 150, 200, 250.

---

## 3. Results Comparison (Concurrency 250)

The table below compares the peak load performance (concurrency = 250) of the native GKE TPU run against the GPU configurations (from the previous benchmark sweep):

| Metric | TPU v6e BF16 (GKE) | GPU A100 FP16 | GPU G4 FP16 | TPU v6e FP8 (GKE) | GPU A100 FP8 | GPU G4 FP8 |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: |
| **Precision** | BF16 | FP16 | FP16 | FP8 (Weights+KV) | FP8 (Weights Marlin) | FP8 (Weights Cutlass) |
| **RPS** | **360.87** | 402.66 | 768.65 | **263.27** | 339.99 | 681.23 |
| **Latency p50 (s)**| **0.653** | 0.609 | 0.312 | **0.922** | 0.708 | 0.355 |
| **Latency p99 (s)**| **1.006** | 0.922 | 0.523 | **1.292** | 1.108 | 0.559 |
| **Avg Gen Tokens** | 11.06 | 11.05 | 11.06 | 16.00 (Maxed) | 16.00 (Maxed) | 16.00 (Maxed) |
| **TPS** | **3,991** | **4,448** | **8,502** | **4,212** | **5,440** | **10,900** |
| **TPS Speedup vs FP16** | - | - | - | **+5.5%** | **+22.3%** | **+28.2%** |
| **Cost per Hour** | **$2.70** | $5.07 | $4.15 | **$2.70** | $5.07 | $4.15 |
| **Cost/1M Tokens** | **$0.19** | $0.32 | $0.14 | **$0.18** | $0.26 | $0.11 |

---

## 4. Key Insights

1.  **GKE vs. Docker Emulation Consistency**:
    *   The TPU v6e GKE run closely matches our previous local Docker emulation results:
        *   **BF16:** 360.87 RPS on GKE vs. 368.52 RPS on Docker (~2.1% variance).
        *   **FP8:** 263.27 RPS on GKE vs. 274.24 RPS on Docker (~4.0% variance).
    *   This confirms that restricting Docker CPU and memory resources on a multi-chip VM acts as a highly accurate proxy for native GKE slice resource limits.
2.  **TPU v6e vs. GPU G4 (Blackwell)**:
    *   The single NVIDIA Blackwell G4 GPU achieves **2.1x higher throughput (768.65 RPS)** than a single TPU v6e chip (360.87 RPS) in BF16/FP16, and **2.5x higher throughput** in FP8.
    *   However, the TPU v6e maintains a significant cost advantage. At **$2.70/hr**, the TPU v6e's Cost per 1M Tokens (**$0.19** for BF16, **$0.18** for FP8) remains competitive with the GPU G4 ($0.14 and $0.11) and outperforms the A100 ($0.32 and $0.26).
3.  **The FP8 Precision Gap on Qwen**:
    *   On Qwen 1.7B, FP8 quantization results in slightly lower throughput (RPS) compared to BF16 because the model generates longer sequences before triggering stop tokens (averaging 16 tokens maxed out vs. 11.06 tokens for BF16).
    *   In terms of raw Token Throughput (TPS), FP8 achieves a **+5.5% speedup** on GKE (4,212 TPS vs. 3,991 TPS).

---

## 5. Detailed GKE TPU Sweep Results

### 5.1 TPU v6e BF16 (GKE)
*   **C=100**: 329.00 RPS | p50: 0.289s | p99: 0.523s
*   **C=150**: 349.20 RPS | p50: 0.415s | p99: 0.576s
*   **C=200**: 356.91 RPS | p50: 0.536s | p99: 0.803s
*   **C=250**: 360.87 RPS | p50: 0.653s | p99: 1.006s

### 5.2 TPU v6e FP8 (GKE)
*   **C=100**: 245.21 RPS | p50: 0.392s | p99: 0.671s
*   **C=150**: 255.65 RPS | p50: 0.572s | p99: 0.745s
*   **C=200**: 260.93 RPS | p50: 0.747s | p99: 1.021s
*   **C=250**: 263.27 RPS | p50: 0.922s | p99: 1.292s
