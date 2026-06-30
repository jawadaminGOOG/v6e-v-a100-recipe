# Benchmarking Recipe: Model Hardware Benchmarker

This document guides any AI agent (Jetski, Claude Code, Cursor, Copilot) or human engineer through benchmarking an LLM on TPU and GPU platforms, applying GKE resource caps, and comparing measured vs. theoretical performance.

---

## Workflow Steps

### Step 1: Clarification Phase
Before executing any shell commands, you MUST ask the user these clarifying questions and wait for their response:
1.  **Optimization Target:** Latency (lowest p99) vs. Throughput (max RPS).
2.  **Precision Level:** BF16/FP16, FP8 (Weight-only), FP8 (Weights+KV).
3.  **Optimization Parameter:** Cost-Efficiency (RPS/$) vs. Peak Performance (max scale).
4.  **Benchmark Input Option:**
    *   *Option A: Custom File* (Local path to Excel/JSON/CSV file).
    *   *Option B: Public Dataset* (e.g. `ShareGPT` - script will auto-download).
    *   *Option C: Native vLLM Benchmarks* (will execute vLLM's repository scripts).
5.  **Model Hugging Face ID** (e.g., `Qwen/Qwen3-1.7B-Base`).

---

### Step 2: Theoretical Forecasting Phase
Run `scripts/forecast.py` to calculate the theoretical limits of the model on the target hardware.
```bash
python3 scripts/forecast.py --model <model_id> --hardware <tpu|gpu> --precision <bf16|fp16|fp8>
```
Keep the outputs of this calculation to include in your Plan of Attack.

---

### Step 3: Plan of Attack & Approval Gate
Generate a Markdown document outlining your execution plan. You MUST include:
*   **Theoretical Forecast:** Estimated peak RPS/TPS limit for Prefill and Decode.
*   **Docker Server Configs:** The exact container startup scripts with memory and CPU constraints matching GKE specs (e.g. 30 CPUs / 128GB for TPU).
*   **Test Matrix:** Concurrency levels (e.g. 100, 150, 200, 250) and repeat counts.
*   **Input Dataset Details:** How the inputs will be loaded (custom file, ShareGPT, or vLLM native).

At the end of your plan, output this exact prompt:
> `[PLAN] Review and approve plan? (Reply 'yes' to proceed)`

**CRITICAL:** You MUST pause execution and wait for the user to explicitly reply "yes" before calling any server startup or benchmarking commands.

---

### Step 4: Server Deployment & Warmup
1.  Execute the approved docker command on the target VM.
2.  Monitor server logs. Wait for loading and XLA compilation to complete.
3.  Send a single test `curl` query to confirm the server responds.
    *   *Note: JAX compiling JIT shaders can take up to 2-3 minutes on the first query. Confirm it finishes.*

---

### Step 5: Traffic Generation Sweep
*   **If using Option A or B:** Execute the local `scripts/benchmark.py` client to send concurrent requests.
*   **If using Option C:** Execute vLLM's native `benchmarks/benchmark_throughput.py` client.
*   Ensure results are saved as CSV files.

---

### Step 6: Log Audit & Sanity Check Report
1.  Run `scripts/vm_log_checker.py` to ensure the host OS and drivers remained stable (no memory leaks or driver exceptions).
2.  Run `scripts/analyze.py` to compare measured TPS vs. the theoretical limit calculated in Step 2.
3.  Output a final Markdown report containing:
    *   High-level comparison table (RPS, Latency p50/p99, TPS, Speedup).
    *   Measured vs. Theoretical efficiency analysis.
    *   Diagnostics section (flags if measured throughput is <50% of HBM speed, or if the stop-token quantization bug occurred).

---

## Rule: Code Modification & Extensibility
As an AI agent, you are fully authorized to modify or rewrite the scripts under `scripts/` (e.g., `forecast.py`, `benchmark.py`, `analyze.py`) if:
*   The user requests a custom metric (like prompt token throughput vs generation token throughput).
*   You need to support a new hardware chip not in the standard spec database.
*   The user requests custom graphing or output formats.
*   You need to adjust mathematical formulas for a new model architecture (e.g. mixture of experts, MoE).

Document any code modifications in your Plan of Attack.
