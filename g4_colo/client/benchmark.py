#!/usr/bin/env python3
"""
Benchmark a toxicity-classification LLM inference endpoint across client concurrency levels.

Dependencies (not included in the inference-server requirements.txt):
    pip install pandas openpyxl transformers tqdm aiohttp numpy

Example:
    python benchmark.py \\
        --input-file ./synthetic_yelp.xlsx \\
        --output-dir ./output \\
        --url http://localhost:10010/v1/completions \\
        --system V6E-TPU \\
        --semaphores 100,150,200,250
"""

from __future__ import annotations

import argparse
import asyncio
import random
import time
from pathlib import Path

import aiohttp
import numpy as np
import pandas as pd
from tqdm import tqdm
from transformers import AutoTokenizer

BASE_GUIDELINE_PREFIX = """As an expert AI model, your task is to analyze and classify messages for toxicity based on the following instructions strictly into Abusive, Crime, or Neutral.

### Guidelines:
1. Abusive:
    - Classify messages containing insults, harassment, hate speech, threats of harm, or profanity directed at individuals or groups as "Abusive".
    - Classify messages that use slurs, demeaning language, or targeted bullying as "Abusive".
    - Classify messages that include both abusive language and general remarks as "Abusive," unless criminal content clearly dominates.
    - Classify messages expressing verbal aggression, intimidation, or personal attacks as "Abusive".

2. Crime:
    - Classify messages discussing, planning, soliciting, or admitting illegal activity such as theft, fraud, drug trafficking, or violence as "Crime".
    - Classify messages that describe or encourage unlawful acts, evasion of law enforcement, or possession of illegal goods as "Crime".
    - Classify messages that mix criminal references with neutral content as "Crime," unless abusive language clearly dominates.
    - Classify messages referencing arrests, warrants, illicit transactions, or coordinated wrongdoing as "Crime".

3. Neutral:
    - Spam, Promotions, or Advertisements: Classify these messages as "Neutral" regardless of tone.
    - Greetings: Classify greetings like "Good Morning," "Hello," as "Neutral" only if there is no abusive or criminal content.
    - General Inquiries: Classify general inquiries as "Neutral" only if there is no abusive language, criminal references, or harmful intent.
    - Neutral Tone: If no clear abusive or criminal content is expressed, classify as "Neutral".
    - News or Factual Discussion: Classify factual reporting about crime without promotion or glorification as "Neutral" when no abusive language is present.

### Validation Points:
a. Think step by step.
b. Eliminate personal biases and adhere strictly to the guidelines.
c. Use only the content provided in the messages without assuming any external information.
d. Detect and consider slang, coded language, and euphemisms in messages.
e. Ensure impartiality in your analysis.
f. Maintain consistency and accuracy.
g. Provide your output in a structured JSON format.

### Output Format:
```json
{{"Annotation": "<Abusive, Crime, Neutral>"}}
```"""

CHAT_SYSTEM_PROMPT = (
    "You are an expert assistant great at toxicity classification based on "
    "given guidelines. Follow the guidelines and just give the final answer "
    "in JSON format"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Benchmark toxicity-classification inference latency and throughput.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--input-file",
        type=Path,
        required=True,
        help="Excel file with a 'Message' column of sample texts",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("./output"),
        help="Directory where benchmark result spreadsheets are written",
    )
    parser.add_argument(
        "--url",
        default="http://localhost:10010/v1/completions",
        help="HTTP endpoint for completion requests",
    )
    parser.add_argument(
        "--tokenizer",
        default="Qwen/Qwen3-1.7B-Base",
        help="Hugging Face tokenizer used to build chat-formatted prompts (must define a chat template)",
    )
    parser.add_argument(
        "--served-model-name",
        default=None,
        help="Model name vLLM expects in the request body. Must match the server's "
        "served model name. Defaults to the --tokenizer value.",
    )
    parser.add_argument("--system", default="V6E-TPU", help="Hardware label, e.g. V6E-TPU or A100")
    parser.add_argument("--framework", default="vllm-tpu", help="Serving framework label")
    parser.add_argument("--model", default="firehose", help="Model label for output filenames")
    parser.add_argument("--precision", default="fp16", help="Precision label for output filenames")
    parser.add_argument("--version-id", default="benchmark", help="Run version tag for output filenames")
    parser.add_argument(
        "--num-messages",
        type=int,
        default=3000,
        help="Number of messages sampled from the input file per benchmark run",
    )
    parser.add_argument(
        "--num-unique-prefixes",
        type=int,
        default=1,
        help="Number of distinct guideline prefixes to rotate through",
    )
    parser.add_argument(
        "--semaphores",
        default="100,150,200,250,300,350,400,450,500,550",
        help="Comma-separated client concurrency levels to test",
    )
    parser.add_argument(
        "--runs-per-semaphore",
        type=int,
        default=2,
        help="How many times to repeat each concurrency level",
    )
    parser.add_argument(
        "--sleep-between-runs",
        type=float,
        default=10.0,
        help="Seconds to wait between runs so the server can recover",
    )
    parser.add_argument(
        "--request-timeout",
        type=float,
        default=30.0,
        help="Per-request HTTP timeout in seconds",
    )
    parser.add_argument("--seed", type=int, default=42, help="Random seed for message sampling")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Generate prompts and print a sample without calling the server",
    )
    return parser.parse_args()


def parse_semaphores(value: str) -> list[int]:
    levels = [int(item.strip()) for item in value.split(",") if item.strip()]
    if not levels:
        raise ValueError("At least one semaphore level is required")
    if any(level <= 0 for level in levels):
        raise ValueError("Semaphore levels must be positive integers")
    return levels


def create_request(user_prompt: str, model: str) -> dict:
    return {
        "model": model,
        "prompt": user_prompt,
        "temperature": 0.0,
        "max_tokens": 16,
        "top_p": 1.0,
        "presence_penalty": 0.2,
        "frequency_penalty": 0.2,
    }


def get_final_prompt_with_template(tokenizer: AutoTokenizer, user_prompt: str) -> str:
    messages = [
        {"role": "system", "content": CHAT_SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]
    return tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)


def generate_benchmark_prompts(
    file_path: Path,
    tokenizer: AutoTokenizer,
    num_unique_prefixes: int,
    num_messages: int,
    seed: int,
) -> list[str]:
    df = (
        pd.read_excel(file_path)
        .sample(n=num_messages, random_state=seed)
        .reset_index(drop=True)
    )
    column_name = "Message"
    if "Message" not in df.columns:
        if "Messages" in df.columns:
            column_name = "Messages"
        else:
            raise ValueError(f"Expected a 'Message' or 'Messages' column in {file_path}, found: {list(df.columns)}")

    prefixes = [
        f"Brand context: Brand - {brand_id}\n\n{BASE_GUIDELINE_PREFIX}"
        for brand_id in range(num_unique_prefixes)
    ]

    prompts: list[str] = []
    for _, row in df.iterrows():
        message = row[column_name]
        prefix = random.choice(prefixes)
        full_prompt = f"{prefix}\nInput Message:  [timestamp-{time.time():.6f}] {message}"
        prompts.append(get_final_prompt_with_template(tokenizer, full_prompt))

    return prompts


async def get_engine_response(
    sem_limit: int,
    text_batch: list[str],
    url: str,
    model: str,
    request_timeout: float,
    track: bool = True,
) -> tuple[list[dict | None], list[float]]:
    sem = asyncio.Semaphore(sem_limit)
    timeout = aiohttp.ClientTimeout(total=request_timeout)
    latencies: list[float] = []
    connector = aiohttp.TCPConnector(limit=sem_limit)
    progress_bar = tqdm(total=len(text_batch), disable=not track)

    async def http_call(json_body: dict, session: aiohttp.ClientSession) -> dict | None:
        async with sem:
            try:
                start = time.time()
                async with session.post(url, json=json_body) as response:
                    latencies.append(time.time() - start)
                    progress_bar.update(1)

                    if response.status == 200:
                        try:
                            return await response.json(content_type=None)
                        except Exception as json_exc:
                            print(f"JSON Parse Error. Status: {response.status}, Body: {await response.text()}")
                            return None
                    print(f"Error: {response.status}, Body: {await response.text()}")
                    return None
            except Exception as exc:
                print(f"Exception: {exc}")
                progress_bar.update(1)
                return None

    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        tasks = [
            asyncio.create_task(http_call(create_request(text, model), session))
            for text in text_batch
        ]
        responses = await asyncio.gather(*tasks)

    progress_bar.close()
    return list(responses), latencies


def build_output_path(args: argparse.Namespace) -> Path:
    filename = (
        f"{args.system}_benchmark_{args.framework}_{args.model}_"
        f"{args.precision}_{args.num_unique_prefixes}_{args.version_id}.xlsx"
    )
    return args.output_dir / filename


def summarize_run(
    responses: list[dict | None],
    latencies: list[float],
    total_requests: int,
    total_time_taken: float,
    semaphore: int,
) -> dict:
    valid_responses = [resp for resp in responses if isinstance(resp, dict)]
    successful_requests = len(valid_responses)
    failed_requests = total_requests - successful_requests

    rps = total_requests / total_time_taken if total_time_taken > 0 else 0
    successful_rps = successful_requests / total_time_taken if total_time_taken > 0 else 0
    latency_p50 = float(np.percentile(latencies, 50)) if latencies else None
    latency_p90 = float(np.percentile(latencies, 90)) if latencies else None
    latency_p95 = float(np.percentile(latencies, 95)) if latencies else None
    latency_p99 = float(np.percentile(latencies, 99)) if latencies else None
    theoretical_max_rps = semaphore / latency_p50 if latency_p50 else None

    prompt_tokens = [
        resp.get("usage", {}).get("prompt_tokens", 0)
        for resp in valid_responses
    ]
    completion_tokens = [
        resp.get("usage", {}).get("completion_tokens", 0)
        for resp in valid_responses
    ]

    return {
        "Total Time Taken (sec)": total_time_taken,
        "Total Requests Count": total_requests,
        "Total Successful Requests": successful_requests,
        "Failed Requests": failed_requests,
        "RPS": rps,
        "Successful RPS": successful_rps,
        "Latency p50 (sec)": latency_p50,
        "Latency p90 (sec)": latency_p90,
        "Latency p95 (sec)": latency_p95,
        "Latency p99 (sec)": latency_p99,
        "Theoretical Max RPS": theoretical_max_rps,
        "average_prompt_tokens": float(np.mean(prompt_tokens)) if prompt_tokens else 0,
        "average_completion_tokens": float(np.mean(completion_tokens)) if completion_tokens else 0,
    }


def make_result_row(args: argparse.Namespace, semaphore: int, run_id: int) -> dict:
    return {
        "Timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "System": args.system,
        "Client Semaphore": semaphore,
        "Run": run_id,
        "Total Time Taken (sec)": None,
        "Total Requests Count": None,
        "Total Successful Requests": None,
        "Failed Requests": None,
        "RPS": None,
        "Successful RPS": None,
        "Latency p50 (sec)": None,
        "Latency p90 (sec)": None,
        "Latency p95 (sec)": None,
        "Latency p99 (sec)": None,
        "Theoretical Max RPS": None,
        "average_prompt_tokens": None,
        "average_completion_tokens": None,
        "Error": None,
    }


async def run_benchmark(args: argparse.Namespace) -> None:
    if not args.input_file.exists():
        raise FileNotFoundError(f"Input file not found: {args.input_file}")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    output_path = build_output_path(args)
    client_semaphores = parse_semaphores(args.semaphores)
    served_model_name = args.served_model_name or args.tokenizer

    print(f"Loading tokenizer: {args.tokenizer}")
    tokenizer = AutoTokenizer.from_pretrained(args.tokenizer, trust_remote_code=True)

    if args.dry_run:
        prompts = generate_benchmark_prompts(
            args.input_file,
            tokenizer,
            args.num_unique_prefixes,
            min(args.num_messages, 5),
            args.seed,
        )
        print("Dry run complete. Sample prompt:\n")
        print(prompts[0])
        print(f"\nWould write results to: {output_path}")
        return

    results: list[dict] = []

    for semaphore in client_semaphores:
        for run_id in range(1, args.runs_per_semaphore + 1):
            print(f"\nBenchmarking: Semaphore={semaphore}, Run={run_id}")
            result = make_result_row(args, semaphore, run_id)

            try:
                input_prompts = generate_benchmark_prompts(
                    args.input_file,
                    tokenizer,
                    args.num_unique_prefixes,
                    args.num_messages,
                    args.seed,
                )
                total_requests = len(input_prompts)

                start_time = time.time()
                responses, latencies = await get_engine_response(
                    sem_limit=semaphore,
                    text_batch=input_prompts,
                    url=args.url,
                    model=served_model_name,
                    request_timeout=args.request_timeout,
                    track=True,
                )
                total_time_taken = time.time() - start_time

                result.update(
                    summarize_run(
                        responses,
                        latencies,
                        total_requests,
                        total_time_taken,
                        semaphore,
                    )
                )
                print("Result:", result)

            except Exception as exc:
                result["Error"] = str(exc)
                print(f"Error during Semaphore={semaphore}, Run={run_id}: {exc}")

            finally:
                results.append(result)
                pd.DataFrame(results).to_excel(output_path, index=False)
                print(f"Saved progress to {output_path}")

                if args.sleep_between_runs > 0:
                    await asyncio.sleep(args.sleep_between_runs)

    print(f"\nBenchmark complete. Final results: {output_path}")


def main() -> None:
    args = parse_args()
    asyncio.run(run_benchmark(args))


if __name__ == "__main__":
    main()
