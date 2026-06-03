#!/usr/bin/env python3
"""Check all running servers for SSH and miner status."""
import json
import subprocess
import sys

# Get instances data
result = subprocess.run(
    ["vastai", "show", "instances", "--raw"],
    capture_output=True, text=True
)
data = json.loads(result.stdout)

running = [i for i in data if i.get('actual_status') == 'running']

print("=" * 80)
print(f"CHECKING {len(running)} RUNNING SERVERS")
print("=" * 80)

for i in running:
    iid = i.get('id')
    host = i.get('ssh_host', '')
    port = i.get('ssh_port', '')
    gpu = i.get('gpu_name', '?')
    dph = i.get('dph_total', 0)
    
    print(f"\n--- Server {iid} ({gpu} @ ${dph}/hr) ---")
    
    if not host or not port:
        print("  [ERROR] No SSH info")
        continue
    
    # Test SSH connection
    ssh_test = subprocess.run(
        ["ssh", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no",
         f"root@{host}", "-p", str(port), "echo 'SSH_OK'"],
        capture_output=True, text=True, timeout=10
    )
    
    if ssh_test.returncode != 0:
        print(f"  [SSH FAIL] {ssh_test.stderr[:100]}")
        continue
    
    print("  [SSH OK]")
    
    # Check miner process
    miner_check = subprocess.run(
        ["ssh", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no",
         f"root@{host}", "-p", str(port), "ps aux | grep -i alpha | grep -v grep"],
        capture_output=True, text=True, timeout=10
    )
    
    if miner_check.stdout.strip():
        print(f"  [MINER RUNNING] {miner_check.stdout.strip()[:80]}")
    else:
        print("  [MINER NOT RUNNING]")
    
    # Check GPU
    gpu_check = subprocess.run(
        ["ssh", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no",
         f"root@{host}", "-p", str(port), 
         "nvidia-smi --query-gpu=temperature.gpu,power.draw,utilization.gpu,name --format=csv,noheader 2>/dev/null || echo 'nvidia-smi failed'"],
        capture_output=True, text=True, timeout=10
    )
    
    print(f"  [GPU] {gpu_check.stdout.strip()}")
    
    # Check if pearl-miner container is running
    docker_check = subprocess.run(
        ["ssh", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no",
         f"root@{host}", "-p", str(port), 
         "docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}' 2>/dev/null | grep -i pearl || echo 'No pearl container'"],
        capture_output=True, text=True, timeout=10
    )
    
    if "pearl" in docker_check.stdout.lower():
        print(f"  [DOCKER] {docker_check.stdout.strip()}")
    else:
        print("  [DOCKER] No pearl-miner container")

print("\n" + "=" * 80)
print("CHECK COMPLETE")
print("=" * 80)
