import subprocess
import time
import sys

# Serving Pod IPs
POD_A_IP = "10.76.3.6"
POD_B_IP = "10.76.2.7"

concurrencies = [100, 250, 500, 1000]
runs = 2
num_messages = 3000

# Client pod names
clients = [
    "benchmark-client-1",
    "benchmark-client-2",
    "benchmark-client-3",
    "benchmark-client-4"
]

# Mapping to serving IPs
urls = [
    f"http://{POD_A_IP}:8000/v1/completions",
    f"http://{POD_A_IP}:8000/v1/completions",
    f"http://{POD_B_IP}:8000/v1/completions",
    f"http://{POD_B_IP}:8000/v1/completions"
]

def split_concurrency(total, num_parts):
    base = total // num_parts
    rem = total % num_parts
    parts = [base] * num_parts
    for i in range(rem):
        parts[i] += 1
    return parts

for C in concurrencies:
    c_parts = split_concurrency(C, len(clients))
    for r in range(1, runs + 1):
        print(f"\n======================================")
        print(f"Starting Sweep: Concurrency={C}, Run={r}")
        print(f"Split concurrencies: {c_parts}")
        print(f"======================================")
        
        processes = []
        for idx, client in enumerate(clients):
            c_limit = c_parts[idx]
            url = urls[idx]
            seed = 42 + idx + r * 10 # ensure different seeds
            system_label = f"G4-LMC-C{C}-R{r}-Part{idx+1}"
            
            cmd = [
                "kubectl", "exec", client, "--",
                "python3", "/tmp/benchmark.py",
                "--input-file", "/tmp/synthetic_yelp.xlsx",
                "--output-dir", f"/tmp/output_C{C}_R{r}",
                "--url", url,
                "--system", system_label,
                "--tokenizer", "Qwen/Qwen3-1.7B-Base",
                "--semaphores", str(c_limit),
                "--runs-per-semaphore", "1",
                "--num-messages", str(num_messages // len(clients)),
                "--request-timeout", "120",
                "--seed", str(seed)
            ]
            
            # Print simplified cmd
            print(f"Launching on {client}: Concurrency={c_limit}, URL={url}")
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            processes.append((client, p))
            
        # Wait for all in this run to finish
        for client, p in processes:
            stdout, stderr = p.communicate()
            if p.returncode != 0:
                print(f"Error on {client}: Exit code {p.returncode}")
                print(stderr.decode())
            else:
                print(f"Completed on {client}")
                
        print(f"Finished Run {r} for Concurrency {C}. Sleeping 10s...")
        time.sleep(10)

print("\nSweep Complete!")
