import argparse
import pandas as pd
import numpy as np
import os
from pathlib import Path

def parse_csv(filepath):
    df = pd.read_csv(filepath)
    # Average runs per client semaphore
    grouped = df.groupby("Client Semaphore").agg({
        "RPS": "mean",
        "Latency p50 (sec)": "mean",
        "Latency p99 (sec)": "mean",
        "average_prompt_tokens": "mean",
        "average_completion_tokens": "mean"
    }).reset_index()
    return grouped

def check_quantization_bug(df, max_tokens=16):
    # Check if completion tokens are stuck at max_tokens cap
    avg_gen = df["average_completion_tokens"].mean()
    if abs(avg_gen - max_tokens) < 0.05:
        return True, avg_gen
    return False, avg_gen

def generate_report(results, forecast_tps, output_report_path):
    report_lines = []
    report_lines.append("# Hardware Benchmark Evaluation Report\n")
    report_lines.append("## 1. High-Level Summary (Peak Load: Concurrency 250)\n")
    
    # Peak table
    report_lines.append("| Metric | Measured Value | Theoretical Peak | Efficiency |")
    report_lines.append("| :--- | :---: | :---: | :---: |")
    
    # Extract peak row (concurrency 250) for each result
    for label, df in results.items():
        peak_row = df[df["Client Semaphore"] == 250]
        if peak_row.empty:
            peak_row = df.iloc[-1:] # fallback to last row if 250 isn't found
            
        sem = peak_row["Client Semaphore"].values[0]
        rps = peak_row["RPS"].values[0]
        p50 = peak_row["Latency p50 (sec)"].values[0]
        p99 = peak_row["Latency p99 (sec)"].values[0]
        avg_gen = peak_row["average_completion_tokens"].values[0]
        measured_tps = rps * avg_gen
        
        eff = (measured_tps / forecast_tps) * 100 if forecast_tps else 0
        
        report_lines.append(f"| **{label} RPS (Concurrency {sem})** | {rps:.2f} | - | - |")
        report_lines.append(f"| **{label} Latency p50 / p99** | {p50:.3f}s / {p99:.3f}s | - | - |")
        report_lines.append(f"| **{label} Token Throughput (TPS)** | **{measured_tps:.1f}** | **{forecast_tps:.1f}** | **{eff:.1f}%** |")
        report_lines.append("|" + "-"*40 + "|")
        
    report_lines.append("\n## 2. Concurrency Scaling Sweep\n")
    
    # RPS Table
    report_lines.append("### 2.1 Throughput (RPS)")
    headers = "| Configuration | " + " | ".join([f"Concurrency {s}" for s in [100, 150, 200, 250]]) + " |"
    sep = "| :--- | " + " | ".join([":---:" for _ in [100, 150, 200, 250]]) + " |"
    report_lines.append(headers)
    report_lines.append(sep)
    for label, df in results.items():
        row = f"| **{label}** "
        for sem in [100, 150, 200, 250]:
            val = df[df["Client Semaphore"] == sem]["RPS"].values
            row += f"| {val[0]:.2f} " if len(val) > 0 else "| N/A "
        row += "|"
        report_lines.append(row)
        
    report_lines.append("\n### 2.2 Tail Latency p99 (seconds)")
    report_lines.append(headers)
    report_lines.append(sep)
    for label, df in results.items():
        row = f"| **{label}** "
        for sem in [100, 150, 200, 250]:
            val = df[df["Client Semaphore"] == sem]["Latency p99 (sec)"].values
            row += f"| {val[0]:.3f} " if len(val) > 0 else "| N/A "
        row += "|"
        report_lines.append(row)
        
    report_lines.append("\n## 3. Diagnostic Observations\n")
    
    for label, df in results.items():
        report_lines.append(f"### {label} Diagnostics")
        
        # Check stop token bug
        is_buggy, avg_gen = check_quantization_bug(df)
        if is_buggy:
            report_lines.append(f"*   **[WARNING] Stop-Token Quantization Bug Detected:** Average generated tokens is exactly **{avg_gen:.2f}** (hitting the hard cap). The model failed to generate stop tokens. Throughput (RPS) is artificially low. Rely on Token Throughput (TPS) for comparison.")
        else:
            report_lines.append(f"*   **Stop-Token Behavior:** normal. Average generated tokens: {avg_gen:.2f} (base model completion).")
            
        # Check efficiency
        peak_row = df.iloc[-1:]
        rps = peak_row["RPS"].values[0]
        avg_gen = peak_row["average_completion_tokens"].values[0]
        measured_tps = rps * avg_gen
        eff = (measured_tps / forecast_tps) * 100 if forecast_tps else 0
        if eff < 50:
            report_lines.append(f"*   **[WARNING] Low Hardware Efficiency ({eff:.1f}%):** Measured TPS is less than 50% of the theoretical HBM bandwidth decode limit ({forecast_tps:.1f} TPS). Check GKE resource caps, XLA compile logs, or token padding configurations (`VLLM_TPU_BUCKET_PADDING_GAP`).")
        else:
            report_lines.append(f"*   **Hardware Efficiency ({eff:.1f}%):** Healthy. Memory bandwidth utilization is within expected bounds.")
        report_lines.append("")

    # Write report
    with open(output_report_path, 'w') as f:
        f.write("\n".join(report_lines))
    print(f"Sanity Check Report compiled to {output_report_path}")

def main():
    parser = argparse.ArgumentParser(description="Performance Evaluation and Sanity Checker")
    parser.add_argument("--csv-files", type=str, required=True, help="Comma-separated 'Label:Path' strings of result CSVs")
    parser.add_argument("--forecast-tps", type=float, default=0.0, help="Theoretical peak TPS from forecast.py")
    parser.add_argument("--output-report", type=str, default="./results_evaluation_report.md", help="Path to write report")
    args = parser.parse_args()
    
    results = {}
    file_mappings = [item.split(":") for item in args.csv_files.split(",")]
    
    for label, path in file_mappings:
        if not os.path.exists(path):
            print(f"Error: file not found {path}")
            sys.exit(1)
        results[label] = parse_csv(path)
        
    generate_report(results, args.forecast_tps, args.output_report)

if __name__ == '__main__':
    main()
