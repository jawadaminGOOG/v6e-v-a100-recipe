import argparse
import asyncio
import time
import json
import urllib.request
import os
import numpy as np
import pandas as pd
from pathlib import Path
import aiohttp
from transformers import AutoTokenizer

# Standard public ShareGPT split URL
SHAREGPT_URL = "https://huggingface.co/datasets/anon8231489123/ShareGPT_Vicuna_unfiltered/resolve/main/ShareGPT_V3_unfiltered_cleaned_split.json"

def download_sharegpt(destination):
    print(f"Downloading ShareGPT dataset from {SHAREGPT_URL}...")
    try:
        urllib.request.urlretrieve(SHAREGPT_URL, destination)
        print(f"Dataset saved to {destination}")
        return True
    except Exception as e:
        print(f"Failed to download ShareGPT: {e}")
        return False

def load_prompts(input_path, dataset_type, tokenizer, max_samples=3000):
    prompts = []
    
    if dataset_type == "sharegpt":
        if not os.path.exists(input_path):
            success = download_sharegpt(input_path)
            if not success:
                raise FileNotFoundError("Could not acquire ShareGPT dataset.")
        
        with open(input_path, 'r') as f:
            data = json.load(f)
        
        # Extract conversations
        count = 0
        for entry in data:
            if "conversations" in entry and len(entry["conversations"]) > 0:
                # Use the first human prompt
                human_text = entry["conversations"][0]["value"]
                prompts.append(human_text)
                count += 1
                if count >= max_samples:
                    break
        print(f"Loaded {len(prompts)} prompts from ShareGPT dataset.")
        
    elif dataset_type == "custom":
        path = Path(input_path)
        if not path.exists():
            raise FileNotFoundError(f"Input file not found: {input_path}")
            
        if path.suffix == ".xlsx":
            df = pd.read_excel(path)
            # Expecting prompt to be in a column named 'prompt' or the first column
            col = 'prompt' if 'prompt' in df.columns else df.columns[0]
            prompts = df[col].astype(str).tolist()
        elif path.suffix == ".json":
            with open(path, 'r') as f:
                data = json.load(f)
            prompts = data if isinstance(data, list) else data.get("prompts", [])
        elif path.suffix in [".csv", ".txt"]:
            df = pd.read_csv(path, header=None)
            prompts = df[0].astype(str).tolist()
            
        prompts = prompts[:max_samples]
        print(f"Loaded {len(prompts)} prompts from custom file {input_path}")
        
    else:
        raise ValueError(f"Unknown dataset type: {dataset_type}")
        
    return prompts

async def send_request(session, url, payload, semaphore, run_stats):
    async with semaphore:
        start = time.perf_counter()
        try:
            async with session.post(url, json=payload) as response:
                content = await response.json()
                latency = time.perf_counter() - start
                
                # Check structure (vLLM output format)
                choices = content.get("choices", [])
                if choices:
                    text = choices[0].get("text", "")
                    # Note: We can count tokens if model returned it, or estimate
                    # vLLM completions endpoint doesn't return token count unless requested,
                    # but we can count characters or check usage
                    usage = content.get("usage", {})
                    completion_tokens = usage.get("completion_tokens", len(text.split())) # fallback estimate
                    prompt_tokens = usage.get("prompt_tokens", len(payload["prompt"].split()))
                    
                    run_stats["latencies"].append(latency)
                    run_stats["completion_tokens"].append(completion_tokens)
                    run_stats["prompt_tokens"].append(prompt_tokens)
                    run_stats["successes"] += 1
                else:
                    run_stats["failures"] += 1
        except Exception as e:
            run_stats["failures"] += 1

async def run_semaphore_sweep(args, prompts):
    semaphores = [int(s) for s in args.semaphores.split(",")]
    results = []
    
    url = f"{args.url}/v1/completions"
    
    print(f"Starting benchmark sweep over semaphores: {semaphores}")
    
    for sem_limit in semaphores:
        for run_id in range(1, args.runs + 1):
            print(f"Running Semaphore={sem_limit}, Run={run_id}/{args.runs}...")
            
            # Select first N prompts for this benchmark
            test_prompts = prompts[:args.num_requests]
            
            run_stats = {
                "latencies": [],
                "completion_tokens": [],
                "prompt_tokens": [],
                "successes": 0,
                "failures": 0
            }
            
            semaphore = asyncio.Semaphore(sem_limit)
            start_time = time.perf_counter()
            
            async with aiohttp.ClientSession() as session:
                tasks = []
                for prompt in test_prompts:
                    payload = {
                        "model": args.model,
                        "prompt": prompt,
                        "max_tokens": args.max_tokens,
                        "temperature": 0.0,
                        "stream": False
                    }
                    tasks.append(send_request(session, url, payload, semaphore, run_stats))
                
                await asyncio.gather(*tasks)
                
            total_time = time.perf_counter() - start_time
            
            # Calculate metrics
            total_reqs = len(test_prompts)
            rps = run_stats["successes"] / total_time if total_time > 0 else 0
            
            lats = run_stats["latencies"]
            p50 = np.percentile(lats, 50) if lats else 0
            p90 = np.percentile(lats, 90) if lats else 0
            p99 = np.percentile(lats, 99) if lats else 0
            
            avg_comp_tokens = np.mean(run_stats["completion_tokens"]) if run_stats["completion_tokens"] else 0
            avg_prompt_tokens = np.mean(run_stats["prompt_tokens"]) if run_stats["prompt_tokens"] else 0
            
            row = {
                "Timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                "System": args.system,
                "Client Semaphore": sem_limit,
                "Run": run_id,
                "Total Time Taken (sec)": total_time,
                "Total Requests Count": total_reqs,
                "Total Successful Requests": run_stats["successes"],
                "Failed Requests": run_stats["failures"],
                "RPS": rps,
                "Latency p50 (sec)": p50,
                "Latency p90 (sec)": p90,
                "Latency p99 (sec)": p99,
                "average_prompt_tokens": avg_prompt_tokens,
                "average_completion_tokens": avg_comp_tokens
            }
            results.append(row)
            print(f"Finished: RPS={rps:.2f}, p99={p99:.3f}s, AvgGen={avg_comp_tokens:.1f}")
            
    # Output to Excel/CSV
    df = pd.DataFrame(results)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.suffix == ".xlsx":
        df.to_excel(output_path, index=False)
    else:
        df.to_csv(output_path, index=False)
    print(f"Results saved to {output_path}")

def main():
    parser = argparse.ArgumentParser(description="Asynchronous Model Benchmark Client")
    parser.add_argument("--url", type=str, default="http://localhost:8000", help="vLLM server base URL")
    parser.add_argument("--model", type=str, required=True, help="HF model ID used on server")
    parser.add_argument("--dataset-type", type=str, required=True, choices=["custom", "sharegpt"], help="Type of benchmark input")
    parser.add_argument("--input-file", type=str, required=True, help="Path to custom file or local ShareGPT download location")
    parser.add_argument("--tokenizer", type=str, required=True, help="Tokenizer HF path")
    parser.add_argument("--semaphores", type=str, default="100,150,200,250", help="Comma-separated concurrencies to sweep")
    parser.add_argument("--runs", type=int, default=2, help="Number of runs per concurrency level")
    parser.add_argument("--num-requests", type=int, default=3000, help="Number of requests to run per test")
    parser.add_argument("--max-tokens", type=int, default=16, help="max completion tokens parameter")
    parser.add_argument("--system", type=str, default="benchmark-system", help="System label for logs")
    parser.add_argument("--output", type=str, required=True, help="Path to output file (.csv or .xlsx)")
    
    args = parser.parse_args()
    
    print(f"Initializing tokenizer: {args.tokenizer}")
    tokenizer = AutoTokenizer.from_pretrained(args.tokenizer, trust_remote_code=True)
    
    prompts = load_prompts(args.input_file, args.dataset_type, tokenizer, max_samples=args.num_requests)
    
    asyncio.run(run_semaphore_sweep(args, prompts))

if __name__ == '__main__':
    main()
