#!/usr/bin/env python3
"""Corrected server check - verify miners via logs and API."""
import json
import subprocess
import sys
import time

# Get instances
result = subprocess.run(
    ["vastai", "show", "instances", "--raw"],
    capture_output=True, text=True
)
data = json.loads(result.stdout)

running = [i for i in data if i.get('actual_status') == 'running']

print("=" * 100)
print("SERVER REVISION v2 - Checking miners correctly")
print("=" * 100)

# Map vast IDs to worker names from dashboard
# vast-38107373 = 38107373

for i in running:
    iid = i.get('id')
    host = i.get('ssh_host', '')
    port = i.get('ssh_port', '')
    gpu = i.get('gpu_name', '?')
    dph = i.get('dph_total', 0)
    geo = i.get('geolocation', '?')
    
    print(f"\n--- Server {iid} ({gpu} @ ${dph:.3f}/hr, {geo}) ---")
    
    if not host or not port:
        print("  [ERROR] No SSH info")
        continue
    
    # Check for miner log
    log_check = subprocess.run(
        ["ssh", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no",
         f"root@{host}", "-p", str(port), 
         "ls -la /var/log/alpha-miner.log 2>/dev/null || echo 'NO LOG'"],
        capture_output=True, text=True, timeout=10
    )
    
    if "NO LOG" in log_check.stdout:
        print("  [LOG] No miner log found")
    else:
        print("  [LOG] Miner log exists")
        # Get hashrate from log
        hashrate_check = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no",
             f"root@{host}", "-p", str(port), 
             "tail -5 /var/log/alpha-miner.log 2>/dev/null | grep hashrate_th_s | tail -1"],
            capture_output=True, text=True, timeout=10
        )
        if hashrate_check.stdout.strip():
            # Parse hashrate from log line
            line = hashrate_check.stdout.strip()
            if "hashrate_th_s=" in line:
                try:
                    hashrate = line.split("hashrate_th_s=")[1].split()[0]
                    print(f"  [HASHRATE] {hashrate} TH/s")
                except:
                    print(f"  [HASHRATE] {line[-50:]}")
    
    # Check for any miner process (not just alpha-miner)
    ps_check = subprocess.run(
        ["ssh", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no",
         f"root@{host}", "-p", str(port), 
         "ps aux | grep -E '(miner|pearl)' | grep -v grep || echo 'NO MINER PROCESS'"],
        capture_output=True, text=True, timeout=10
    )
    
    if "NO MINER PROCESS" in ps_check.stdout:
        print("  [PROCESS] No miner process found")
    else:
        procs = ps_check.stdout.strip().split('\n')
        print(f"  [PROCESS] {len(procs)} miner-related processes")
    
    # Check docker containers
    docker_check = subprocess.run(
        ["ssh", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no",
         f"root@{host}", "-p", str(port), 
         "docker ps --format '{{.Names}}: {{.Image}}' 2>/dev/null | grep -v '^$' || echo 'NO DOCKER'"],
        capture_output=True, text=True, timeout=10
    )
    
    if "NO DOCKER" in docker_check.stdout or not docker_check.stdout.strip():
        print("  [DOCKER] No containers")
    else:
        for line in docker_check.stdout.strip().split('\n'):
            print(f"  [DOCKER] {line}")
    
    # GPU check
    gpu_check = subprocess.run(
        ["ssh", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no",
         f"root@{host}", "-p", str(port), 
         "nvidia-smi --query-gpu=temperature.gpu,power.draw,utilization.gpu --format=csv,noheader,nounits 2>/dev/null || echo 'GPU FAIL'"],
        capture_output=True, text=True, timeout=10
    )
    
    if "GPU FAIL" not in gpu_check.stdout:
        temps = []
        for line in gpu_check.stdout.strip().split('\n'):
                    if line.strip():
                        parts = line.split(',')
                        if len(parts) >= 3:
                            temps.append(f"{parts[0].strip()}°C")
        if temps:
            print(f"  [GPU TEMP] {', '.join(temps)}")

print("\n" + "=" * 100)
print("DONE - Compare with dashboard: https://pearl.alphapool.tech/")
print("=" * 100)
