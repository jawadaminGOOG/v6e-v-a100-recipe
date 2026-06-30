import argparse
import sys
import urllib.request
import json

# Hardware specs database
HARDWARE_SPECS = {
    "v6e-1t": {
        "name": "Google TPU v6e (1 chip)",
        "hbm_bandwidth_bytes": 1.63e12, # 1.63 TB/s
        "peak_flops": {
            "bf16": 2.19e14, # 219 TFLOPs
            "fp16": 2.19e14,
            "fp8": 4.38e14,  # 438 TFLOPs
        }
    },
    "A100-80GB-SXM": {
        "name": "NVIDIA A100 80GB SXM4",
        "hbm_bandwidth_bytes": 2.03e12, # 2.03 TB/s
        "peak_flops": {
            "bf16": 3.12e14, # 312 TFLOPs
            "fp16": 3.12e14,
            "fp8": 3.12e14,  # Standard A100 lacks native FP8 cores; computes in FP16
        }
    }
}

# Offline fallback database for common models
OFFLINE_MODELS = {
    "qwen/qwen3-1.7b-base": {
        "num_parameters": 1.7e9,
        "num_hidden_layers": 28,
        "num_attention_heads": 32,
        "num_key_value_heads": 4,
        "hidden_size": 2048,
        "vocab_size": 151643
    },
    "qwen/qwen3-1.7b-fp8": {
        "num_parameters": 1.7e9,
        "num_hidden_layers": 28,
        "num_attention_heads": 32,
        "num_key_value_heads": 4,
        "hidden_size": 2048,
        "vocab_size": 151643
    }
}

def query_hf_api(model_id):
    url = f"https://huggingface.co/api/models/{model_id}"
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=5) as response:
            data = json.loads(response.read().decode())
            # Basic parsing if config metadata is present
            config = data.get("config", {})
            if config:
                return {
                    "num_parameters": data.get("safetensors", {}).get("total", 0) or 1.5e9, # fallback estimate
                    "num_hidden_layers": config.get("num_hidden_layers", 24),
                    "num_attention_heads": config.get("num_attention_heads", 32),
                    "num_key_value_heads": config.get("num_key_value_heads", 32),
                    "hidden_size": config.get("hidden_size", 4096),
                    "vocab_size": config.get("vocab_size", 32000)
                }
    except Exception as e:
        print(f"Hugging Face API lookup skipped: {e}")
    return None

def fetch_model_specs(model_id):
    normalized_id = model_id.lower().strip()
    if normalized_id in OFFLINE_MODELS:
        print(f"Loaded specs for '{model_id}' from offline database.")
        return OFFLINE_MODELS[normalized_id]
    
    print(f"Attempting to query Hugging Face API for '{model_id}'...")
    specs = query_hf_api(model_id)
    if specs:
        return specs
        
    print(f"Warning: Could not fetch specs. Using generic 7B model defaults.")
    return {
        "num_parameters": 7.0e9,
        "num_hidden_layers": 32,
        "num_attention_heads": 32,
        "num_key_value_heads": 8,
        "hidden_size": 4096,
        "vocab_size": 32000
    }

def main():
    parser = argparse.ArgumentParser(description="Theoretical LLM Performance Forecaster")
    parser.add_argument("--model", type=str, required=True, help="Hugging Face Model ID")
    parser.add_argument("--hardware", type=str, required=True, choices=list(HARDWARE_SPECS.keys()), help="Target Hardware profile")
    parser.add_argument("--precision", type=str, required=True, choices=["bf16", "fp16", "fp8"], help="Execution precision")
    args = parser.parse_args()

    hw = HARDWARE_SPECS[args.hardware]
    specs = fetch_model_specs(args.model)

    bytes_per_param = 1 if args.precision == "fp8" else 2
    model_size_bytes = specs["num_parameters"] * bytes_per_param
    
    # Calculate KV Cache footprint per token (K + V tensors)
    head_dim = specs["hidden_size"] / specs["num_attention_heads"]
    kv_cache_bytes_per_token = 2 * specs["num_hidden_layers"] * specs["num_key_value_heads"] * head_dim * bytes_per_param

    # 1. Decode Single-User Latency Limit (HBM Bandwidth Bound)
    # Under single-user decode, we fetch the model weights once per token.
    max_single_user_decode_tps = hw["hbm_bandwidth_bytes"] / model_size_bytes

    # 2. Peak Arithmetic Limit (Compute Bound)
    # Peak TPS = Peak FLOPS / (2 * parameters)
    flops_mode = "fp8" if args.precision == "fp8" else "bf16"
    max_compute_tps = hw["peak_flops"][flops_mode] / (2 * specs["num_parameters"])

    print("\n" + "="*50)
    print(f"THEORETICAL PERFORMANCE FORECAST FOR '{args.model}'")
    print(f"Hardware: {hw['name']}")
    print(f"Precision: {args.precision.upper()} ({bytes_per_param} bytes/parameter)")
    print("-"*50)
    print(f"Model Parameters:     {specs['num_parameters']/1e9:.2f} Billion")
    print(f"Model Size in Memory: {model_size_bytes/1e9:.2f} GB")
    print(f"KV Cache / Token:     {kv_cache_bytes_per_token/1024:.2f} KB (Batch=1)")
    print("-"*50)
    print(f"Theoretical Single-User Decode Limit: {max_single_user_decode_tps:.2f} tokens/sec")
    print(f"(Memory Bandwidth Bound: max speed to stream weights)")
    print(f"Theoretical Compute Throughput Limit:  {max_compute_tps:.2f} tokens/sec")
    print(f"(Compute Bound: max arithmetic rate of Tensor Cores)")
    print("="*50 + "\n")

if __name__ == '__main__':
    main()
