import os

def parse_gpu_log(filepath):
    if not os.path.exists(filepath):
        print(f"File not found: {filepath}")
        return None
        
    utils = []
    with open(filepath, 'r') as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    # nvidia-smi output might have some warnings or empty lines, try to parse
                    val = int(line)
                    utils.append(val)
                except ValueError:
                    pass
    return utils

def analyze_gpu(name, utils):
    if not utils:
        print(f"No data for {name}")
        return
        
    peak = max(utils)
    # Filter out idle times (e.g. < 5% utilization) to get active average
    active_utils = [u for u in utils if u >= 5]
    
    avg_all = sum(utils) / len(utils) if utils else 0.0
    avg_active = sum(active_utils) / len(active_utils) if active_utils else 0.0
    
    print(f"GPU {name}:")
    print(f"  Total samples: {len(utils)}")
    print(f"  Active samples (>=5%): {len(active_utils)}")
    print(f"  Peak Utilization: {peak}%")
    print(f"  Average (Active): {avg_active:.2f}%")
    print(f"  Average (All): {avg_all:.2f}%")

log_dir = "/usr/local/google/home/jawadamin/.gemini/jetski/brain/8efd12ab-10c2-4f4b-acf5-833de551e91d/g4_multinode_logs/exp_2_rerun"
analyze_gpu("Pod-A", parse_gpu_log(os.path.join(log_dir, "gpu_util_pod_A.log")))
analyze_gpu("Pod-B", parse_gpu_log(os.path.join(log_dir, "gpu_util_pod_B.log")))
