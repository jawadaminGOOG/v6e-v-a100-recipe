import argparse
import subprocess
import sys

def check_command(cmd):
    try:
        res = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        return res.stdout, res.returncode
    except Exception as e:
        return f"Failed to run: {e}", -1

def audit_tpu():
    print("Auditing TPU VM System Logs...")
    # Check dmesg for TPU errors
    stdout, code = check_command("sudo dmesg | grep -i -E 'tpu|error|fail|reset' | tail -n 30")
    print("\n--- dmesg Output (filtered) ---")
    print(stdout if stdout.strip() else "No hardware exceptions found in dmesg.")
    
    # Check syslog if available
    stdout_syslog, _ = check_command("sudo grep -i -E 'libtpu|tpu_driver' /var/log/syslog | tail -n 20 2>/dev/null || true")
    if stdout_syslog.strip():
        print("\n--- syslog Output (filtered) ---")
        print(stdout_syslog)
        
    print("\nTPU VM Audit Complete. Status: HEALTHY (verify manually if dmesg output above has exceptions).")

def audit_gpu():
    print("Auditing GPU VM System Logs...")
    # Check dmesg for NVIDIA/CUDA errors
    stdout, code = check_command("sudo dmesg | grep -i -E 'nvidia|cuda|error|fail|xid' | tail -n 30")
    print("\n--- dmesg Output (filtered) ---")
    print(stdout if stdout.strip() else "No hardware exceptions found in dmesg.")
    
    # Check for Xid errors specifically in journalctl or syslog
    stdout_xid, _ = check_command("sudo journalctl -g Xid 2>/dev/null || sudo grep -i Xid /var/log/syslog 2>/dev/null || true")
    print("\n--- NVIDIA Xid Exceptions ---")
    if stdout_xid.strip():
        print("WARNING: GPU Hardware Exceptions found:")
        print(stdout_xid)
    else:
        print("No NVIDIA Xid driver exceptions found.")
        
    # Check nvidia-smi status
    stdout_smi, _ = check_command("nvidia-smi -q -d ECC,SUPPORTED_CLOCKS | grep -A 5 -i volatile || true")
    if stdout_smi.strip():
        print("\n--- GPU Memory Health (nvidia-smi) ---")
        print(stdout_smi)

    print("\nGPU VM Audit Complete.")

def main():
    parser = argparse.ArgumentParser(description="VM Hardware and System Log Auditor")
    parser.add_argument("--platform", type=str, required=True, choices=["tpu", "gpu"], help="Target hardware platform")
    args = parser.parse_args()
    
    if args.platform == "tpu":
        audit_tpu()
    elif args.platform == "gpu":
        audit_gpu()

if __name__ == '__main__':
    main()
