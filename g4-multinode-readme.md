# G4 GKE Multi-Node LLM Serving Research Report

This repository contains the results, manifests, and logs from a research project evaluating various LLM serving topologies and optimizations on a GKE cluster using **NVIDIA RTX PRO 6000 GPUs** (G4 node pool) under reservation `pm-g4s`.

The model evaluated throughout is **Qwen-1.7B-Base** (served as `Qwen/Qwen3-1.7B-Base`), using a toxicity classification workload with prompts averaging ~600 tokens and 11-token generation.

---

## 1. Directory Structure

All artifacts generated during this research are preserved in the following structure:
-   `g4_multinode_results/`: Contains detailed summary reports and client benchmark spreadsheets (Excel).
    -   [exp_1a_summary.md](file:///usr/local/google/home/jawadamin/.gemini/jetski/brain/8efd12ab-10c2-4f4b-acf5-833de551e91d/g4_multinode_results/exp_1a_summary.md): Ray Tensor Parallel (TP=2) evaluation.
    -   [exp_1b_summary.md](file:///usr/local/google/home/jawadamin/.gemini/jetski/brain/8efd12ab-10c2-4f4b-acf5-833de551e91d/g4_multinode_results/exp_1b_summary.md): Ray Data Parallel (DP=2) evaluation.
    -   [exp_1c_summary.md](file:///usr/local/google/home/jawadamin/.gemini/jetski/brain/8efd12ab-10c2-4f4b-acf5-833de551e91d/g4_multinode_results/exp_1c_summary.md): Vanilla GKE Data Parallel (DP=2) evaluation.
    -   [exp_2_summary.md](file:///usr/local/google/home/jawadamin/.gemini/jetski/brain/8efd12ab-10c2-4f4b-acf5-833de551e91d/g4_multinode_results/exp_2_summary.md): LMCache + GCS Fuse Prefix Caching evaluation.
    -   [exp_4_summary.md](file:///usr/local/google/home/jawadamin/.gemini/jetski/brain/8efd12ab-10c2-4f4b-acf5-833de551e91d/g4_multinode_results/exp_4_summary.md): Disaggregated Serving (Prefill-Decode Split) evaluation.
    -   `exp_2/results.xlsx`: Excel raw metrics for Exp 2.
    -   `exp_4/results_opt.xlsx`: Excel raw metrics for Exp 4.
-   `g4_multinode_logs/`: Contains logs from container runtimes.
    -   `exp_2/`: Logs for vLLM and GCS Fuse containers during Exp 2.
    -   `exp_4/`: Logs for Prefiller, Decoder, and Proxy pods during Exp 4.

---

## 2. Consolidated Results Comparison

The table below summarizes the performance metrics across all completed experiments (averages across runs for each client concurrency level):

| Experiment | Topology / Optimization | Concurrency | RPS | p50 Latency (s) | p99 Latency (s) |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Exp 1a** | Ray TP=2 (Tensor Parallel, 2 GPUs) | 100 | 258 | 0.38 | 0.54 |
| | | 250 | 260 | 0.94 | 1.15 |
| | | 500 | 265 | 1.86 | 2.10 |
| **Exp 1b** | Ray DP=2 (Data Parallel, 2 GPUs) | 100 | 580 | 0.17 | 0.28 |
| | | 250 | 851 | 0.29 | 0.44 |
| | | 500 | 954 | 0.52 | 0.81 |
| **Exp 1c** | **Vanilla GKE DP=2 (Baseline Champion)** | 100 | **585** | **0.17** | **0.28** |
| (Baseline) | *No Ray, 2 independent replicas* | 250 | **878** | **0.28** | **0.40** |
| | | 500 | **994** | **0.49** | **0.78** |
| **Exp 2** | **LMCache + GCS Fuse (Prefix Cache)** | 100 | **651** | **0.15** | **0.22** |
| (Optimized)| *2 replicas, GCS Fuse storage, Redis metadata* | 250 | **914** | **0.26** | **0.39** |
| | | 500 | **1018** | **0.41** | **0.78** |
| **Exp 4** | **Disaggregated Serving** | 100 | **239** | **0.25** | **1.74** |
| | *1 Prefiller, 1 Decoder, Redis storage, Proxy*| 250 | **187** | **0.66** | **5.76** |
| | | 500 | **243** | **1.46** | **6.15** |

---

## 3. Key Research Insights

### A. Topology Selection: Data Parallelism vs. Tensor Parallelism
-   **Ray TP=2 (Exp 1a)** performed significantly worse than DP configurations (Ray DP=2 and Vanilla GKE). At 500 Concurrency, TP=2 only achieved **265 RPS** compared to **994 RPS** for Vanilla GKE.
-   **Why?** The Qwen-1.7B model is extremely small. Distributing its parameters across 2 GPUs via Tensor Parallelism introduces heavy inter-GPU communication overhead (NCCL) that far outweighs any compute speedup. Data Parallelism (replicated serving) is the correct topology for small-to-medium models.
-   **Ray vs. Vanilla GKE**: Vanilla GKE (Exp 1c) performed slightly better and with lower variance than Ray DP=2 (Exp 1b), while having a much simpler infrastructure stack.

### B. Distributed Prefix Caching (LMCache + GCS Fuse)
-   **LMCache (Exp 2)** achieved the best overall performance, pushing throughput to **1018 RPS** at Concurrency 500 (a **2.4% increase** over baseline) and reducing tail latency (p99) at Concurrency 100 by **21%** (from 0.28s to 0.22s).
-   Using **GCS Fuse** as the storage backend proved highly efficient because the OS page cache absorbs local write system calls instantly (non-blocking), while the `gcsfuse` daemon flushes data to Cloud Storage asynchronously in the background.

### C. Disaggregated Serving Bottlenecks
-   **Disaggregated Serving (Exp 4)** performed poorly (**243 RPS** at Concurrency 500, a **75% drop** from Experiment 2).
-   **Why?**
    1.  **Asymmetric GPU Utilization**: Prefill compute is short, while decode is long. In a 1:1 split, the Prefiller GPU sits idle (0-15% util), while the Decoder GPU is 100% bottlenecked. Replicated serving (Exp 2) allows both GPUs to run decode, doubling capacity.
    2.  **Proxy Overhead**: Routing requests through a custom HTTP proxy adds serialization cost and extra network hops (2 internal roundtrips per request), saturating the Python event loop.
    3.  **GIL & Remote Storage Blocking**: Writing KV cache bytes directly to Redis over the network causes CPU-bound serialization blocks (GIL) in Python, freezing the engine.

---

## 4. Architectural Recommendations

1.  **For Small Models (e.g., < 8B parameters)**: Always prefer **Vanilla GKE Data Parallelism (replicated serving)** over Tensor Parallelism. Avoid Ray unless multi-node orchestration features are explicitly needed.
2.  **For Prefix Caching**: Use **LMCache with GCS Fuse** for storage and a lightweight **Redis instance for metadata sharing only**. Do not store raw KV cache chunks directly in Redis RAM unless you have a high-bandwidth, non-blocking asynchronous writing pipeline that bypasses Python's GIL.
3.  **For Disaggregated Serving**: Only deploy this architecture if:
    -   Prompts are extremely long (e.g., >32K tokens) where prefill time dominates.
    -   You use a highly asymmetric node ratio (e.g., 1 Prefiller to 4 Decoders) to prevent the decode phase from becoming a single-GPU bottleneck.
    -   The proxy/router is implemented in a high-performance, multi-threaded language (e.g., Go or Rust) rather than Python.
